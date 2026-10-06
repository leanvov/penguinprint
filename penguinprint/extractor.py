"""提取：栅格相位搜索 + 循环移位搜索 + 软判决多数表决。

为什么能抗裁剪
--------------
嵌入用的是"起点为 block 整数倍"的固定栅格。裁剪后栅格整体错位，
但**错位量只有 block x block 种可能**，且每个块承载的 bit 只与
``(A·块行号 + B·块列号) mod L`` 有关（A、B 由 :func:`penguinprint.payload.spread_pair` 给出），
所以裁剪只会让载荷序列整体循环移位。
于是提取分两层搜索：

1. 相位搜索：遍历 (dy, dx) ∈ [0,B)²，对每种相位把滑窗系数图按步长 B 切片；
2. 循环移位搜索：对每种相位累加出的软判决向量，遍历 L 种循环移位并用校验和确认。

错误相位不可能通过校验和；正确相位靠重复投票与纠错还原载荷。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

import numpy as np

from .config import PROFILES, WatermarkConfig
from .imgio import luma
from .payload import (
    bits_to_bytes,
    check_payload,
    crc_delta_table,
    flip_subsets,
    short_code,
    spread_pair,
    validity_value,
)
from .qim import qim_soft
from .sliding import (
    activity_std,
    block_coeff_strip,
    block_stats_strip,
    estimate_noise_sigma,
    iter_strips,
)

__all__ = ["ExtractResult", "extract_bits", "TRACE_HINT"]

# 只有软判决质量超过该门槛的相位才值得做 CRC 循环移位尝试（纯为提速）
_CRC_GATE = 0.15
# 导出行的状态列用：没有通过校验时，质量高于该值就记为"疑似痕迹"
# （界面/日志里"检测到指纹痕迹"的提示不用这个常量，见 extract_bits 末尾的
#  trace_presence_z / local_trace_align / 0.4 门槛）
TRACE_HINT = 0.45
_TRACE_HINT = TRACE_HINT
# 无水印时 |score| 的期望值：E|cos(πU)| = 2/π
_RANDOM_MEAN_ABS_SCORE = 2.0 / np.pi


@dataclass
class ExtractResult:
    found: bool = False
    crc_ok: bool = False
    id_hex: str = ""
    short_code: str = ""
    identifier: str | None = None
    note: str | None = None
    confidence: float = 0.0
    quality_best: float = 0.0
    phase: tuple[int, int] | None = None
    shift: int | None = None
    blocks: int = 0
    coverage: float = 0.0
    votes_per_bit: float = 0.0
    stage: int = 0
    corrections: int = 0
    scale: float = 1.0
    scale_y: float = 1.0
    z_score: float = 0.0
    z_null: float = 0.0
    presence_z: float = 0.0
    trace: bool = False
    probabilities: list[float] = field(default_factory=list)
    message: str = ""
    elapsed: float = 0.0

    def to_row(self) -> dict:
        d = asdict(self)
        d.pop("probabilities", None)
        d["phase"] = "" if self.phase is None else f"{self.phase[0]},{self.phase[1]}"
        d["status"] = "成功" if self.found else ("疑似痕迹" if self.quality_best >= _TRACE_HINT else "未检出")
        return d


# --------------------------------------------------------------------------- 累加


def _accumulate_multi(
    Y: np.ndarray,
    cfg: WatermarkConfig,
    phases: list[tuple[int, int]],
    scales: tuple[float, ...],
) -> tuple[list[np.ndarray], list[np.ndarray], np.ndarray, tuple[np.ndarray, np.ndarray]]:
    """一次遍历同时算出多个归一化倍率 k 的累加器（系数图只算一遍）。

    返回 ``(acc_list, wsum_list, cnt, presence)``，第 i 组对应 ``scales[i]``；
    ``cnt``（票数）与 k 无关，所有 k 共用。

    ``presence = (sum|score|, count)``：只统计"归一化系数明显不在 0 附近
    （``|chat| >= δ/2``）"的块（``count`` 是"块×频率"的样本数）—— 这是**水印存在性**的检测量：

    * 没有水印时，这些块的系数落在任意位置，``E|score| = 2/π``；
    * 有水印时它们被量化到栅格点上，``E|score| ≈ 1``。

    排除近零块是必要的：平滑干净图的系数本来就接近 0，会退化成"全部落在栅格 0 上"，
    看起来 |score|≈1，其实毫无信息量。
    """
    B = cfg.block_size
    H, W = Y.shape
    L = cfg.payload_bits
    nfr = len(cfg.coefficients)
    nph = len(phases)
    index = {ph: i for i, ph in enumerate(phases)}
    acc_list = [np.zeros((nfr, nph, L), dtype=np.float64) for _ in scales]
    wsum_list = [np.zeros((nfr, nph, L), dtype=np.float64) for _ in scales]
    cnt = np.zeros((nph, L), dtype=np.float64)
    pres_sum = np.zeros(nph, dtype=np.float64)
    pres_cnt = np.zeros(nph, dtype=np.float64)
    want_presence = any(abs(k - 1.0) < 1e-9 for k in scales)
    lim = W - B
    a_coef, b_coef = spread_pair(L)
    step = np.float32(cfg.quant_step)
    wfloor = np.float32(cfg.vote_weight_floor)
    floor = _activation_floor(Y, cfg)

    for oy0, oy1 in iter_strips(H, cfg):
        h = oy1 - oy0
        mean, std = block_stats_strip(Y, B, oy0, oy1)
        cms = [block_coeff_strip(Y, B, u, v, oy0, oy1) for (u, v) in cfg.coefficients]
        for dy in range(B):
            rows = np.arange(dy, h, B)
            if rows.size == 0:
                continue
            rlat = ((oy0 + rows) // B).astype(np.int64)
            Mrow = mean[rows]
            Srow = std[rows]
            for dx in range(B):
                p = index.get((dy, dx))
                if p is None:
                    continue
                cols = np.arange(dx, lim + 1, B)
                if cols.size == 0:
                    continue
                clat = (cols // B).astype(np.int64)
                q = (a_coef * rlat[:, None] + b_coef * clat[None, :]) % L
                S = Srow[:, cols]
                M = Mrow[:, cols]
                grid = [cm[np.ix_(rows, cols)] for cm in cms]
                # 与嵌入端一致的"不变量"归一化：块方差 - 被修改频率的能量/B²，再封顶
                S_ex = activity_std(S, grid, B)
                valid = (S_ex >= floor) & (M >= cfg.luma_min) & (M <= cfg.luma_max)
                if not valid.any():
                    continue
                qv = q[valid]
                cnt[p] += np.bincount(qv, minlength=L)
                S_eff = np.minimum(S_ex, np.float32(cfg.sigma_cap)) if cfg.sigma_cap else S_ex
                for ki, k in enumerate(scales):
                    sx = np.maximum(S_eff * np.float32(k), 1e-6)
                    acc, wsum = acc_list[ki], wsum_list[ki]
                    scores = []
                    weights = []
                    for f in range(nfr):
                        chat = grid[f] / sx
                        sc = qim_soft(chat, cfg.delta)
                        w = np.clip(np.abs(chat) / step, wfloor, 1.0)
                        if want_presence and abs(k - 1.0) < 1e-9:
                            # 门限取 δ/2：既排除"系数本来就接近 0"的退化块，
                            # 又不漏掉被 σ_cap 压到 |chat|≈δ 的那些块
                            far = valid & (np.abs(chat) >= cfg.delta * 0.5)
                            if far.any():
                                pres_sum[p] += float(np.abs(sc[far]).sum())
                                pres_cnt[p] += float(far.sum())
                        if cfg.consistency_weight:
                            scores.append(sc)
                            weights.append(w)
                        else:
                            acc[f, p] += np.bincount(
                                qv, weights=(sc * w)[valid].astype(np.float64), minlength=L
                            )
                            wsum[f, p] += np.bincount(
                                qv, weights=w[valid].astype(np.float64), minlength=L
                            )
                    if cfg.consistency_weight and scores:
                        # 跨频率一致性：真水印块各频率同号，随机内容块的不一致
                        stacked = np.stack(scores, axis=0)
                        agree = np.abs(stacked.sum(axis=0)) / np.maximum(
                            np.abs(stacked).sum(axis=0), 1e-6
                        )
                        for f in range(nfr):
                            ww = weights[f] * agree
                            acc[f, p] += np.bincount(
                                qv, weights=(scores[f] * ww)[valid].astype(np.float64), minlength=L
                            )
                            wsum[f, p] += np.bincount(
                                qv, weights=ww[valid].astype(np.float64), minlength=L
                            )
    return acc_list, wsum_list, cnt, (pres_sum, pres_cnt)


def _presence_z(presence: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    """水印存在性的显著性：``(E|score| − 2/π) / (0.25/√n)``，按相位给出。"""
    pres_sum, pres_cnt = presence
    n = np.maximum(pres_cnt, 1.0)
    mean = np.where(pres_cnt > 0, pres_sum / n, _RANDOM_MEAN_ABS_SCORE)
    return np.where(pres_cnt > 0, (mean - _RANDOM_MEAN_ABS_SCORE) / (0.25 / np.sqrt(n)), 0.0)


def _activation_floor(Y: np.ndarray, cfg: WatermarkConfig) -> float:
    """激活门限：干净图就是 ``sigma_min``；有噪声时抬到噪声之上。

    只在"估计噪声明显"时抬高（默认 >1.5 灰阶）：噪声不明显时门限保持 ``sigma_min``；
    抬高门限只会少用一些块，不改变归一化量的口径。
    """
    floor = float(cfg.sigma_min)
    if cfg.noise_k <= 0:
        return floor
    noise = estimate_noise_sigma(Y)
    if noise > cfg.noise_floor_trigger:
        cap = float(cfg.sigma_cap) if cfg.sigma_cap else 1e9
        floor = max(floor, min(cfg.noise_k * noise, cap))
    return floor


# --------------------------------------------------------------------------- 译码


def _decode(
    acc: np.ndarray,
    wsum: np.ndarray,
    cnt: np.ndarray,
    cfg: WatermarkConfig,
    phases: list[tuple[int, int]],
    registry=None,
    schedule=None,
    scale: float = 1.0,
    presence: tuple[np.ndarray, np.ndarray] | None = None,
) -> ExtractResult:
    L = cfg.payload_bits
    nfr = acc.shape[0]
    total = cnt.sum(axis=1)
    voted = cnt > 0
    schedule = cfg.repair_schedule if schedule is None else schedule

    best_quality = float("-inf")
    best_z = 0.0
    z_null = 0.0
    best_phase: int | None = None

    def _zscore(margin_row: np.ndarray, cnt_row: np.ndarray) -> np.ndarray:
        """把每 bit 的加权软判决均值换算成"显著性"（相对随机预期的标准差倍数）。

        随机内容下，每 bit 的归一化软判决均值约为 ``0.5/√(票数×频率数)`` 量级；
        所以 ``z = 2·margin·√(票数×频率数)``。
        不按票数归一的话，"只有一两个块投票"的相位也能刷出很高的质量分，
        "检测到指纹痕迹"的提示就不可信了。
        """
        neff = np.maximum(cnt_row, 1.0) * nfr
        return np.where(voted, 2.0 * margin_row * np.sqrt(neff), 0.0)

    # ---- 第一层：普通校验和 + 循环移位搜索（覆盖绝大多数情况，亚秒级）
    for stage in range(1, nfr + 1):
        score = acc[:stage].sum(axis=0)
        weight = wsum[:stage].sum(axis=0)
        # 每 bit 的加权软判决均值，落在 [0,1]
        margin = np.abs(score) / np.maximum(weight, 1e-9)
        quality = np.where(voted, margin, 0.0).sum(axis=1) / np.maximum(voted.sum(axis=1), 1)
        zs = _zscore(margin, cnt)
        z_mean = np.where(voted, zs, 0.0).sum(axis=1) / np.maximum(voted.sum(axis=1), 1)
        coverage = voted.mean(axis=1)
        eligible = (total >= cfg.min_blocks) & (coverage >= cfg.min_coverage)
        # 最佳相位（只要求块数够，不要求覆盖率），用于诊断"是否检测到痕迹"
        scoreable = np.where(total >= cfg.min_blocks, quality, -1.0)
        if scoreable.size:
            top = int(np.argmax(scoreable))
            if scoreable[top] > best_quality:
                best_quality = float(scoreable[top])
                best_phase = top
                best_z = float(z_mean[top])
                ok_ph = total >= cfg.min_blocks
                z_null = float(np.median(z_mean[ok_ph])) if ok_ph.any() else 0.0

        candidates = np.where(eligible & (quality >= _CRC_GATE))[0]
        for p in candidates[np.argsort(-quality[candidates])]:
            found = _search_shift(
                score[p], phases[p], cfg, registry, quality[p], coverage[p], total[p], stage, scale
            )
            if found is not None:
                return found

    # ---- 第二层：软判决纠错（利用校验和的线性结构，穷举"最不可靠比特"的少量翻转）
    if schedule:
        score_all = acc.sum(axis=0)
        weight_all = wsum.sum(axis=0)
        margin_all = np.abs(score_all) / np.maximum(weight_all, 1e-9)
        quality_all = np.where(voted, margin_all, 0.0).sum(axis=1) / np.maximum(voted.sum(axis=1), 1)
        order = [
            int(p)
            for p in np.argsort(-quality_all)
            if total[p] >= cfg.min_blocks and quality_all[p] >= _CRC_GATE
        ]
        if order:
            fixed = _repair(
                score_all, margin_all, cfg, phases, order, quality_all, total, cnt, registry,
                schedule, scale,
            )
            if fixed is not None:
                return fixed

    res = ExtractResult(
        blocks=int(total[best_phase]) if best_phase is not None else 0,
        quality_best=0.0 if best_quality == float("-inf") else best_quality,
        z_score=best_z,
        z_null=z_null,
        coverage=float(voted[best_phase].mean()) if best_phase is not None else 0.0,
        votes_per_bit=float(total[best_phase] / L) if best_phase is not None else 0.0,
        message="未检出指纹",
    )
    # 痕迹判据有两套（最终提示在 extract_bits 末尾）：
    #   · 存在性 z：统计"归一化系数明显不为 0"（|chat| >= δ/2）的块是否落在栅格上
    #     （无水印 E|score|=2/π，有水印≈1），按票数算显著性；它决定要不要跑
    #     局部栅格一致性检查 —— 单看软判决质量不行：平滑干净图的系数本来就接近 0，
    #     会退化成"全部落在栅格 0 上"，质量很高却毫无信息量。
    #   · 软判决质量：作为"检测到痕迹但读不出载荷"那一步的门槛（>= 0.4）。
    presence_best = 0.0
    if presence is not None:
        pz = _presence_z(presence)
        if pz.size:
            presence_best = float(np.max(pz))
    res.presence_z = presence_best
    if best_phase is not None and presence_best >= cfg.trace_presence_z:
        res.trace = True
        res.quality_best = max(res.quality_best, best_quality)
        res.message = (
            f"检测到指纹痕迹（存在性显著性 z={presence_best:.0f}，软判决质量 {best_quality:.2f}），"
            "但未通过校验：图像被重压缩/裁剪/调色，或裁剪后覆盖率不足，无法还原完整载荷"
        )
    return res


def _decode_scales(
    acc_list: list[np.ndarray],
    wsum_list: list[np.ndarray],
    cnt: np.ndarray,
    cfg: WatermarkConfig,
    phases: list[tuple[int, int]],
    scales: tuple[float, ...],
    registry=None,
    schedule=None,
    presence: tuple[np.ndarray, np.ndarray] | None = None,
) -> ExtractResult | None:
    """在多个归一化倍率 k 上依次译码，返回首个成功结果（否则返回最佳诊断结果）。"""
    best: ExtractResult | None = None
    for acc, wsum, k in zip(acc_list, wsum_list, scales):
        res = _decode(
            acc, wsum, cnt, cfg, phases, registry, schedule=schedule, scale=k, presence=presence
        )
        if res.found:
            if abs(k - 1.0) > 1e-9:
                res.message = f"{res.message}（归一化倍率 k={k:g}）"
            return res
        if best is None or res.quality_best > best.quality_best:
            best = res
    return best


def _search_shift(
    score: np.ndarray,
    phase: tuple[int, int],
    cfg: WatermarkConfig,
    registry,
    quality: float,
    coverage: float,
    blocks: float,
    stage: int,
    scale: float = 1.0,
) -> ExtractResult | None:
    """对单个相位的软判决向量遍历全部循环移位，用校验和确认。"""
    L = cfg.payload_bits
    hard = (score >= 0).astype(np.uint8)
    for s in range(L):
        bits = np.roll(hard, s)
        ok, fp = check_payload(bits_to_bytes(bits), cfg.id_bytes, cfg.crc_bytes)
        if ok:
            votes = float(blocks) / max(1, L)
            return _finish(
                fp, phase, s, cfg, quality, coverage, blocks, votes, stage, registry,
                corrections=0, scale=scale,
            )
    return None


def _repair(
    score_all: np.ndarray,
    margin_all: np.ndarray,
    cfg: WatermarkConfig,
    phases: list[tuple[int, int]],
    order: list[int],
    quality_all: np.ndarray,
    total: np.ndarray,
    cnt: np.ndarray,
    registry=None,
    schedule=None,
    scale: float = 1.0,
) -> ExtractResult | None:
    """软判决纠错：在小范围内穷举"最不可靠比特"的翻转组合。

    校验和只能"发现"错误，不能定位；而被裁剪/压缩后往往只差几个 bit
    （例如某些 bit 一个可用块都没有，或个别块被压缩推到判决边界外）。
    这些出错 bit 的软判决置信度最低，因此在"最弱 W 个 bit 里翻转不超过 F 个"
    的组合空间中搜索即可把它们修回来。

    搜索速度靠校验和的线性：翻转组合是否合法只需要几次异或（见
    :func:`penguinprint.payload.crc_delta_table`），配合 numpy 可以一次算完所有移位。
    """
    L = cfg.payload_bits
    nfr = score_all.shape[0]
    deltas = crc_delta_table(L, cfg.id_bytes, cfg.crc_bytes)
    sentinel = L
    # Drot[s, i] = 翻转"提取向量第 i 位"在移位 s 下对应的载荷比特增量
    # 第 L 列是哨兵（0），用于给不足 max_flips 的组合占位
    idx = (np.arange(L)[:, None] + np.arange(L)[None, :]) % L
    Drot = np.zeros((L, L + 1), dtype=np.uint64)
    Drot[:, :L] = deltas[idx]
    arange = np.arange(L)

    for flips, weak, phase_limit in (cfg.repair_schedule if schedule is None else schedule):
        weak = min(weak, L)
        limit = len(order) if phase_limit <= 0 else min(phase_limit, len(order))
        for p in order[:limit]:
            weak_positions = np.argsort(margin_all[p])[:weak]
            subsets = flip_subsets(weak_positions, flips, sentinel)
            if subsets.shape[0] == 0:
                continue
            hard = (score_all[p] >= 0).astype(np.uint8)
            base = np.array(
                [validity_value(np.roll(hard, s), cfg.id_bytes, cfg.crc_bytes) for s in arange],
                dtype=np.uint64,
            )
            acc = np.zeros((L, subsets.shape[0]), dtype=np.uint64)
            for c in range(flips):
                acc ^= Drot[:, subsets[:, c]]
            hits = np.argwhere((acc ^ base[:, None]) == 0)
            if hits.size == 0:
                continue
            s, k = int(hits[0][0]), int(hits[0][1])
            corrected = np.roll(hard, s).copy()
            for c in range(flips):
                j = int(subsets[k, c])
                if j != sentinel:
                    corrected[(j + s) % L] ^= 1
            ok, fp = check_payload(bits_to_bytes(corrected), cfg.id_bytes, cfg.crc_bytes)
            if not ok:
                continue
            nfix = int(np.sum(subsets[k] != sentinel))
            res = _finish(
                fp,
                phases[p],
                s,
                cfg,
                float(quality_all[p]),
                float((cnt[p] > 0).mean()),
                float(total[p]),
                float(total[p]) / max(1, L),
                nfr,
                registry,
                corrections=nfix,
                scale=scale,
            )
            res.message = f"{res.message}（软判决纠错翻转 {nfix} bit）"
            return res
    return None


def _finish(
    fp: bytes,
    phase: tuple[int, int],
    shift: int,
    cfg: WatermarkConfig,
    quality: float,
    coverage: float,
    blocks: float,
    votes_per_bit: float,
    stage: int,
    registry,
    corrections: int = 0,
    scale: float = 1.0,
) -> ExtractResult:
    res = ExtractResult(
        found=True,
        crc_ok=True,
        id_hex=fp.hex().upper(),
        short_code=short_code(fp),
        confidence=float(quality),
        quality_best=float(quality),
        phase=phase,
        shift=int(shift),
        blocks=int(blocks),
        coverage=float(coverage),
        votes_per_bit=float(votes_per_bit),
        stage=stage,
        corrections=corrections,
        scale=float(scale),
        message=f"指纹校验通过（软判决质量 {quality:.2f}，第 {stage} 级频率译码成功）",
    )
    if registry is not None:
        rec = registry.get(res.id_hex)
        if rec:
            res.identifier = rec["identifier"]
            res.note = rec.get("note") or ""
            res.message = f"命中指纹库：{res.identifier}"
        else:
            res.message = f"校验通过，但指纹库中没有该 ID（{res.id_hex}）"
    return res


# --------------------------------------------------------------------------- 对外接口


def _region_alignment(
    rgb: np.ndarray,
    phases: list[tuple[int, int]],
    cfg: WatermarkConfig,
    want_phase: bool = False,
):
    """区域的"对齐度"：尺度/相位对上时，落在栅格上的块比例高于随机水平。

    只统计 ``|归一化系数| >= δ/2`` 的块（排除系数本来就接近 0 的退化块）：
    未对齐时 ``E|score| = 2/π``，对齐后接近 1。
    ``want_phase=True`` 时额外返回取得峰值的相位。
    """
    accs, _wsums, _cnt, (ps, pc) = _accumulate_multi(luma(rgb), cfg, phases, (1.0,))
    del accs
    if not (pc > 0).any():
        return (0.0, phases[0], 0) if want_phase else 0.0
    mean = np.where(pc > 0, ps / np.maximum(pc, 1.0), 0.0)
    idx = int(np.argmax(mean))
    if want_phase:
        return float(mean[idx]), phases[idx], int(pc[idx])
    return float(mean[idx])


def _local_coherence(rgb: np.ndarray, cfg: WatermarkConfig) -> tuple[float, int, int, int]:
    """逐窗口测"局部栅格一致性"，返回 ``(最高一致性, 该窗票数, 该窗可用块数, 达标窗数)``。

    用途：图像被**非刚性局部变形**（液化/自由变换变形）时，全图相位不再统一，
    任何尺度/相位都还原不出载荷，但"水印仍然存在"这件事在小窗口里看得出来，
    因此这一层给出"**有指纹痕迹、但被局部变形打乱**"的结论，而不是误报成"没检出"。
    """
    B = cfg.block_size
    win = int(cfg.local_trace_window)
    H, W = rgb.shape[:2]
    if min(H, W) < win:
        win = max(4 * B, min(H, W))
    phases = [(dy, dx) for dy in range(B) for dx in range(B)]
    gy_n = max(1, H // max(1, win // 2) - 1)
    gx_n = max(1, W // max(1, win // 2) - 1)
    gy_n, gx_n = min(gy_n, 5), min(gx_n, 4)
    best = (0.0, 0, 0)
    hits = 0
    for gy in range(gy_n):
        y0 = int(H * (gy + 0.5) / gy_n) - win // 2
        for gx in range(gx_n):
            x0 = int(W * (gx + 0.5) / gx_n) - win // 2
            y0 = min(max(0, y0), H - win)
            x0 = min(max(0, x0), W - win)
            sub = rgb[y0 : y0 + win, x0 : x0 + win]
            if float(luma(sub).std()) < 3.0:  # 纯色/过曝区测不出东西
                continue
            v, _ph, votes = _region_alignment(sub, phases, cfg, want_phase=True)
            blocks = votes // max(1, len(cfg.coefficients))
            # 可用块太少的窗口，"一致性"会因小样本偶然刷到 1.0，必须按块数过滤后再比大小
            if blocks < cfg.min_blocks:
                continue
            if v > best[0]:
                best = (v, votes, blocks)
            if v >= cfg.local_trace_align:
                hits += 1
    return best[0], best[1], best[2], hits


def _other_profiles(cfg: WatermarkConfig) -> list[tuple[str, WatermarkConfig]]:
    """列出档位表里与 ``cfg`` 不同的其它档位（调用方已先试过 ``cfg``）。"""
    return [(name, other) for name, other in PROFILES.items() if other is not cfg]


def _extract_core(
    rgb: np.ndarray,
    cfg: WatermarkConfig,
    registry=None,
    quick: bool = True,
    t_start: float | None = None,
) -> ExtractResult:
    """提取主流程（供 :func:`extract_bits` 复用）。"""
    t_start = time.perf_counter() if t_start is None else t_start
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    H, W = rgb.shape[:2]
    B = cfg.block_size
    if H < 2 * B or W < 2 * B:
        return ExtractResult(
            message=f"图片太小（{W}x{H}），边长至少需要 {2 * B} 像素",
            elapsed=time.perf_counter() - t_start,
        )
    Y = luma(rgb)
    scales = tuple(cfg.normalizer_scales) or (1.0,)
    light_schedule = cfg.repair_schedule[:1]

    if quick:
        accs, wsums, cnt, pres = _accumulate_multi(Y, cfg, [(0, 0)], scales)
        res = _decode_scales(
            accs, wsums, cnt, cfg, [(0, 0)], scales, registry,
            schedule=light_schedule, presence=pres,
        )
        if res is not None and res.found:
            res.elapsed = time.perf_counter() - t_start
            return res

    phases = [(dy, dx) for dy in range(B) for dx in range(B)]
    acc, wsum, cnt, pres = _accumulate_multi(Y, cfg, phases, (1.0,))
    acc, wsum = acc[0], wsum[0]
    res = _decode(acc, wsum, cnt, cfg, phases, registry, presence=pres)
    if res.found or len(scales) <= 1:
        res.elapsed = time.perf_counter() - t_start
        return res

    # ---- 第三级：拿 k=1 的相位排序，只对最可信的若干相位做其它 k
    total = cnt.sum(axis=1)
    voted = cnt > 0
    margin = np.abs(acc.sum(axis=0)) / np.maximum(wsum.sum(axis=0), 1e-9)
    quality = np.where(voted, margin, 0.0).sum(axis=1) / np.maximum(voted.sum(axis=1), 1)
    # 相位排序把"存在性显著性"也算进去：裁剪+重压缩时单纯看软判决均值会把真相位排得很后
    presence_bonus = _presence_z(pres)
    rank = quality + 0.02 * presence_bonus
    cand = [int(p) for p in np.argsort(-rank) if total[p] >= cfg.min_blocks]
    top = cand[: max(1, cfg.k_search_top_phases)]
    if not top:
        res.elapsed = time.perf_counter() - t_start
        return res
    top_phases = [phases[p] for p in top]
    accs, wsums, cnt2, _ = _accumulate_multi(Y, cfg, top_phases, scales[1:])
    better = _decode_scales(
        accs, wsums, cnt2, cfg, top_phases, scales[1:], registry, schedule=light_schedule
    )
    if better is not None and better.found:
        better.elapsed = time.perf_counter() - t_start
        return better
    if better is not None:
        # k 扫描阶段没有算存在性检测（只在 k=1 有意义），别把已有信息覆盖掉
        better.presence_z = max(better.presence_z, res.presence_z)
        if better.quality_best > res.quality_best or better.presence_z > res.presence_z:
            res = better
    res.elapsed = time.perf_counter() - t_start
    return res


def extract_bits(
    rgb: np.ndarray,
    cfg: WatermarkConfig,
    registry=None,
    quick: bool = True,
    escalate: bool = True,
) -> ExtractResult:
    """从 RGB 数组提取指纹。

    三级递进（任何一级成功即返回）：

    1. **快路径**：只试无裁剪相位 (0,0)，同时扫描全部归一化倍率 k —— 覆盖"原图/仅重压缩"；
    2. **全相位 × k=1**：256 种块栅格相位 + 移位搜索 + 软判决纠错 —— 覆盖裁剪/局部修图；
    3. **全相位里最可信的若干相位 × 其余 k**：覆盖整体调色、曲线、对比压缩
       （这类攻击把"系数 : 归一化量"的比例整体缩放，k 扫描即可把栅格重新对上）。

    第 3 级按"质量 + 存在性显著性"混合排序后取前若干相位，是因为"几何对齐"与"振幅比例"
    互不耦合，正确的相位在任意 k 下都排在前列，这样 k 扫描的代价可以忽略不计。

    **档位自适应**：``cfg`` 只是"先试哪一档"，它决定判决栅格与门限等读取参数。
    提取方无从得知图片当初用哪一档嵌入，因此 ``escalate=True`` 时，若按 ``cfg``
    没有读出载荷、且图上有值得再试的信号，会自动改用其它档位再试一遍。
    ``escalate=False`` 只按给定档位解一次。

    **关于几何形变**：缩放、旋转、透视，以及"选区被拉大后贴回"这类局部变形，
    当前版本不做几何兜底 —— 载荷铺在固定的块栅格上，画面一旦被重采样，
    栅格就会错位，此时无法解码。这类攻击不在防护范围内（见 README 的"局限"一节）。
    """
    cfg.validate()
    t_start = time.perf_counter()
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    res = _extract_core(rgb, cfg, registry=registry, quick=quick, t_start=t_start)
    if res.found:
        res.elapsed = time.perf_counter() - t_start
        return res

    # ---- 档位自适应：按其它档位再试。两道闸门避免给"图里根本没指纹"的图白跑几遍：
    #   1) 第一遍若完全没有水印迹象（软判决质量与存在性都低），直接判定未检出；
    #   2) 每换一档同样要看到迹象才继续换下一档。
    if escalate:
        for _name, other in _other_profiles(cfg):
            if res.quality_best < 0.3 and res.presence_z < 2.0:
                break
            alt = _extract_core(rgb, other, registry=registry, quick=quick, t_start=t_start)
            if alt.found:
                alt.elapsed = time.perf_counter() - t_start
                return alt
            if alt.quality_best > res.quality_best or alt.presence_z > res.presence_z:
                res = alt

    res.elapsed = time.perf_counter() - t_start
    # 局部一致性判据用的是入参 cfg 的门限（三档在这几项上取值相同或近似）
    _mark_local_trace(res, rgb, cfg)
    return res


def _mark_local_trace(res: ExtractResult, rgb: np.ndarray, cfg: WatermarkConfig) -> None:
    """判据：非刚性局部变形下还原不出载荷，但局部窗口仍能证明"指纹还在"。

    只在前面已经看出"有东西"时才做（否则纯白/无关图白跑几秒），且窗口必须有足够可用块。
    """
    if res.found:
        return
    if res.quality_best >= 0.4 or res.presence_z >= 3.0:
        align, votes, blocks, hits = _local_coherence(rgb, cfg)
        if align >= cfg.local_trace_align and blocks >= cfg.min_blocks:
            res.trace = True
            res.message = (
                f"检测到指纹痕迹（局部栅格一致性 {align:.2f}，最强窗口 {blocks} 个可用块/"
                f"{hits} 个窗口达标）：全图相位已不统一，图像应经过**非刚性局部变形（液化/变形网格）**"
                "或极重压缩，任何单一尺度都无法对齐，故无法还原完整载荷，"
                "但可确认该图带有本系统的指纹痕迹"
            )
            return
    if res.message == "未检出指纹" and res.quality_best >= 0.4:
        res.trace = True
        res.message = (
            f"检测到指纹痕迹（软判决质量 {res.quality_best:.2f}），但未通过校验："
            "图像经过局部变形、重度裁剪或重压缩，无法还原完整载荷"
        )
