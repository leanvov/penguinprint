"""嵌入：把指纹比特冗余铺满整幅图。

流程（每张图）
--------------
1. ``Y = 0.299R + 0.587G + 0.114B``（权重和为 1，便于"三通道同加 Δ"精确改亮度）。
2. 按 block_size 栅格切块；对每个块用滑窗统计量判定是否"可用"
   （方差够大、不是死黑/死白），不可用的块不动。
3. 块 (r, c) 承载载荷第 ``(A*r + B*c) mod L`` 个 bit —— 这个"线性铺展"的写法
   保证了**裁剪只是让比特序列整体循环移位**，提取端用循环移位搜索即可复原；
   系数 (A, B) 由 :func:`penguinprint.payload.spread_pair` 给出。
4. 对每个可用块，在每个中频系数上做奇偶量化（:mod:`penguinprint.qim`），
   把系数增量用 :func:`penguinprint.sliding.scatter_delta` 摊回像素域。

归一化量的选择
--------------
若直接用块标准差 σ 做归一化，嵌入本身会改变 σ（低频小方差块尤其明显），
提取端算出的 ``C/σ`` 就会偏离栅格，从而译码失败。

因此改用**对嵌入不变的归一化量**::

    σ_ex² = 块方差 - Σ_{被修改的频率} c_f² / B²

把修改前后的式子相减可知二者相等：设修改把 ``c_f`` 变成 ``c_f + δ_f``，
块方差增加 ``(Σδ² + 2Σc_f δ_f) / B²``，而 ``Σ(c_f+δ_f)² / B²`` 也正好增加同样多，两项相消。
（由正交基 Parseval 关系，块方差 = 除直流外全部系数的平方和 / B²，B = block_size，
单位与 :func:`penguinprint.sliding.activity_std` 一致。）
这是基于系数运算的等式；落到像素上的取整与饱和截断会带来少量偏差。
这样归一化不需要 refiner 迭代，也没有累积漂移。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .config import WatermarkConfig
from .imgio import apply_luma_delta, luma, psnr
from .payload import bit_index_map
from .qim import qim_target
from .sliding import (
    activity_std,
    block_coeff_strip,
    block_stats_strip,
    iter_strips,
    scatter_delta,
)

__all__ = ["EmbedStats", "embed_bits"]


@dataclass
class EmbedStats:
    width: int
    height: int
    blocks_total: int
    blocks_used: int
    payload_bits: int
    delta: float
    passes: int
    psnr: float
    max_abs_diff: int
    distortion_p999: float = 0.0
    coverage: float = 0.0
    warning: str = ""
    elapsed: float = 0.0


def embed_bits(
    rgb: np.ndarray, bits: np.ndarray, cfg: WatermarkConfig
) -> tuple[np.ndarray, EmbedStats]:
    """把 ``bits`` 冗余嵌入 ``rgb``(H,W,3 uint8)，返回 (新图, 统计信息)。"""
    cfg.validate()
    t_start = time.perf_counter()
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("embed_bits 需要 (H, W, 3) 的 RGB 数组")
    H, W = rgb.shape[:2]
    B = cfg.block_size
    if H < 2 * B or W < 2 * B:
        raise ValueError(f"图片太小（{W}x{H}），边长至少需要 {2 * B} 像素")

    bits = np.asarray(bits, dtype=np.uint8).reshape(-1)
    L = int(bits.size)
    if L == 0 or L % 8:
        raise ValueError("载荷比特数必须是 8 的整数倍且非空")

    nc = (W - B) // B + 1
    nr = (H - B) // B + 1
    grid_bits = bits[bit_index_map(L, nr, nc)]

    work = rgb.copy()
    blocks_used = 0
    for it in range(cfg.refine_passes + 1):
        Y = luma(work)
        delta_map = np.zeros((H, W), dtype=np.float32)
        for oy0, oy1 in iter_strips(H, cfg):
            h = oy1 - oy0
            li = np.arange(0, h, B)
            if li.size == 0:
                continue
            lj = np.arange(nc) * B
            mean, std = block_stats_strip(Y, B, oy0, oy1)
            M = mean[np.ix_(li, lj)]
            S = std[np.ix_(li, lj)]
            # 先取齐所有被修改频率的系数，才能算"不变量"归一化 sigma_ex
            grid_coeffs = [
                block_coeff_strip(Y, B, u, v, oy0, oy1)[np.ix_(li, lj)]
                for (u, v) in cfg.coefficients
            ]
            S_ex = activity_std(S, grid_coeffs, B)
            valid = (S_ex >= cfg.sigma_min) & (M >= cfg.luma_min) & (M <= cfg.luma_max)
            if it == 0:
                blocks_used += int(valid.sum())
            if not valid.any():
                continue
            gb = grid_bits[oy0 // B : oy0 // B + li.size, :]
            # 封顶后的归一化量：判决栅格与振幅都用它，提取端用同一公式 ⇒ 自洽
            S_eff = activity_std(S, grid_coeffs, B, cap=cfg.sigma_cap)
            S_safe = np.maximum(S_eff, 1e-6)
            for (u, v), c_grid in zip(cfg.coefficients, grid_coeffs):
                chat = c_grid / S_safe
                target = qim_target(chat, gb, cfg.delta)
                dc = np.where(valid, (target - chat) * S_eff, 0.0).astype(np.float32)
                canvas = np.zeros((h, W - B + 1), dtype=np.float32)
                canvas[li[:, None], lj[None, :]] = dc
                perturb = scatter_delta(canvas, B, u, v, W)
                stop = min(H, oy0 + perturb.shape[0])
                delta_map[oy0:stop, :] += perturb[0 : stop - oy0, :]
        work = apply_luma_delta(work, delta_map)

    diff = np.abs(work.astype(np.int16) - rgb.astype(np.int16)).astype(np.uint8)
    max_diff = int(diff.max())
    p999 = float(np.percentile(diff, 99.9))
    warnings: list[str] = []
    if blocks_used < cfg.min_blocks:
        warnings.append(
            f"可用纹理块仅 {blocks_used} 个（阈值 {cfg.min_blocks}），"
            "指纹冗余不足，建议换更清晰/更少纯色背景的图"
        )
    if p999 > 12.0:
        warnings.append(
            f"局部失真偏大（99.9 分位 {p999:.0f} 灰阶），"
            "可降低强度档位或调小 sigma_cap 以进一步隐形"
        )
    stats = EmbedStats(
        width=W,
        height=H,
        blocks_total=nr * nc,
        blocks_used=blocks_used,
        payload_bits=L,
        delta=float(cfg.delta),
        passes=cfg.refine_passes + 1,
        psnr=psnr(rgb, work),
        max_abs_diff=max_diff,
        distortion_p999=p999,
        coverage=blocks_used / float(nr * nc),
        warning="；".join(warnings),
        elapsed=time.perf_counter() - t_start,
    )
    return work, stats
