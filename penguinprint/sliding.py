"""滑窗特征：一次算出"任意块起点"的 DCT 中频系数 / 均值 / 标准差，以及其转置（叠加）。

设计要点（抗裁剪的基础设施）
------------------------------------
嵌入时只用固定栅格（起点为 block 的整数倍）的块；裁剪会把栅格整块错位，
提取时必须遍历全部 ``block x block`` 种相位。若对每种相位都重算一遍分块 DCT，
代价是 256 倍。这里改成"滑窗"计算：

* DCT 系数 ``C(u,v)`` 是块与频率基外积 ``P = d_u ⊗ d_v`` 的内积，而 ``P`` 可分离，
  于是对整幅图沿 x、y 各做一次一维相关，即可得到**所有起点**的系数图（O(N·B)）::

      coeff_map[y, x] == dctn(Y[y:y+B, x:x+B], norm='ortho')[u, v]

* 块均值/标准差用积分图（cumsum）求，同样是 O(N)。

* ``scatter_delta`` 是系数图算子的**精确转置**：把每个块的系数增量按基外积摊回像素，
  即 ::

      out[y+i, x+j] == sum_blocks canvas[by, bx] * P[i, j]   (by + i == y, bx + j == x)

  它同样只用两次一维相关，避免 Python 逐块循环。
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

from .config import WatermarkConfig, strip_ranges

__all__ = [
    "dct_basis",
    "scatter_delta",
    "activity_std",
    "estimate_noise_sigma",
    "block_stats_strip",
    "block_coeff_strip",
    "iter_strips",
]

_BASIS_CACHE: dict[tuple[int, int], np.ndarray] = {}


def dct_basis(n: int, u: int) -> np.ndarray:
    """DCT-II 正交基的第 u 个向量（长度 n，正交归一）。"""
    k = np.arange(n, dtype=np.float64)
    scale = np.sqrt(1.0 / n) if u == 0 else np.sqrt(2.0 / n)
    return (scale * np.cos(np.pi * u * (2.0 * k + 1.0) / (2.0 * n))).astype(np.float32)


def _basis(n: int, u: int) -> np.ndarray:
    key = (n, u)
    vec = _BASIS_CACHE.get(key)
    if vec is None:
        vec = dct_basis(n, u)
        _BASIS_CACHE[key] = vec
    return vec


def _basis_rev(n: int, u: int) -> np.ndarray:
    """翻转（共轭）基向量，用于把"相关"变成"卷积"。"""
    key = (n, -1 - u)
    vec = _BASIS_CACHE.get(key)
    if vec is None:
        vec = np.ascontiguousarray(_basis(n, u)[::-1])
        _BASIS_CACHE[key] = vec
    return vec


def iter_strips(height: int, cfg: WatermarkConfig) -> list[tuple[int, int]]:
    """产出块起点行区间 [(y0, y1), ...]。"""
    return strip_ranges(height - cfg.block_size + 1, cfg.strip_rows, cfg.block_size)


# --------------------------------------------------------------------------- 统计量


def _window_sums_2d(sub: np.ndarray, block: int, h: int, width: int) -> np.ndarray:
    """对 ``sub``(h+B-1, W) 求所有 (block x block) 窗口的和，输出 (h, width)。"""
    H, W = sub.shape
    c = np.cumsum(sub, axis=0)
    c = np.concatenate([np.zeros((1, W), dtype=np.float64), c], axis=0)
    rowsum = c[block : block + h] - c[0:h]
    cc = np.cumsum(rowsum, axis=1)
    cc = np.concatenate([np.zeros((h, 1), dtype=np.float64), cc], axis=1)
    return cc[:, block : block + width] - cc[:, 0:width]


def block_stats_strip(Y: np.ndarray, block: int, y0: int, y1: int) -> tuple[np.ndarray, np.ndarray]:
    """条带内所有块起点 (y0..y1-1, 0..W-block) 的均值与标准差，形状 (h, W-block+1)。"""
    Y = np.asarray(Y, dtype=np.float64)
    H, W = Y.shape
    h = y1 - y0
    sub = Y[y0 : y1 + block - 1]
    width = W - block + 1
    area = float(block * block)
    sums = _window_sums_2d(sub, block, h, width)
    sq = _window_sums_2d(sub * sub, block, h, width)
    mean = sums / area
    var = sq / area - mean * mean
    np.maximum(var, 0.0, out=var)
    return mean.astype(np.float32), np.sqrt(var).astype(np.float32)


def block_coeff_strip(Y: np.ndarray, block: int, u: int, v: int, y0: int, y1: int) -> np.ndarray:
    """条带内所有块起点的 DCT 系数 C(u,v)，形状 (h, W-block+1)。"""
    Y = np.asarray(Y, dtype=np.float32)
    H, W = Y.shape
    h = y1 - y0
    sub = Y[y0 : y1 + block - 1]
    origin = -(block // 2)  # 偶数长度核的半个像素偏移：不补偿的话块起点会错开一格
    t = ndimage.correlate1d(
        sub, _basis(block, v), axis=1, mode="constant", cval=0.0, origin=origin
    )
    t = ndimage.correlate1d(
        t, _basis(block, u), axis=0, mode="constant", cval=0.0, origin=origin
    )
    return t[0:h, 0 : W - block + 1]


# --------------------------------------------------------------------------- 噪声水平估计


def estimate_noise_sigma(
    Y: np.ndarray,
    block: int = 32,
    step: int = 2,
    percentile: float = 10.0,
    clip_guard: float = 4.0,
) -> float:
    """估计图像的逐像素噪声标准差 σ_n（对纹理与"削顶"都稳健）。

    做法：先按 ``step``（默认 2）抽稀，再算水平相邻差 ``d = |Y[:,1:] - Y[:,:-1]|``
    （白噪声下 ``Var(d) = 2σ²``），然后把抽稀图切成 ``block``×``block`` 的小块
    （默认 32×32，对应原图 64×64），取每块的 MAD（中位绝对偏差），
    最后取"最平坦那批块"的低分位，以尽量排除纹理的贡献。

    * **排除削顶块**：过曝纯白区域叠加杂色后会被 255 削顶，削顶会压低噪声估计，
      所以只统计 min/max 都落在 ``[clip_guard, 255-clip_guard]`` 内的块。
    * 取**低分位**而不是均值：纹理只会把 MAD 抬高，低分位对纹理更稳健。

    用途：叠加杂色会把平坦/纯白区域的 ``σ_ex`` 抬过门限，让"从没嵌过指纹"的块
    变成投票者；提取端据此抬高门限。
    """
    Y = np.asarray(Y, dtype=np.float32)
    if Y.ndim != 2 or min(Y.shape) < 2 * block:
        d = np.abs(np.diff(Y, axis=1)).ravel()
        mad = float(np.median(np.abs(d - np.median(d)))) if d.size else 0.0
        return mad / 0.6745 / np.sqrt(2.0)
    sub = Y[::step, ::step]
    d = np.abs(np.diff(sub, axis=1))
    h, w = d.shape
    bh, bw = h // block, w // block
    if bh < 2 or bw < 2:
        mad = float(np.median(np.abs(d - np.median(d))))
        return mad / 0.6745 / np.sqrt(2.0)
    tiles = d[: bh * block, : bw * block].reshape(bh, block, bw, block)
    tiles = tiles.transpose(0, 2, 1, 3).reshape(-1, block * block)
    src = sub[: bh * block, : bw * block].reshape(bh, block, bw, block)
    src = src.transpose(0, 2, 1, 3).reshape(-1, block * block)
    keep = (src.min(axis=1) >= clip_guard) & (src.max(axis=1) <= 255.0 - clip_guard)
    if int(keep.sum()) >= 8:
        tiles = tiles[keep]
    med = np.median(tiles, axis=1, keepdims=True)
    mads = np.median(np.abs(tiles - med), axis=1)
    low = float(np.percentile(mads, percentile))
    return low / 0.6745 / np.sqrt(2.0)


# --------------------------------------------------------------------------- 不变量归一化


def activity_std(
    std: np.ndarray,
    coeff_grids: list[np.ndarray],
    block: int,
    cap: float | None = None,
) -> np.ndarray:
    """归一化量 ``σ_ex``：对嵌入严格不变的块活跃度。

    ::

        σ_ex² = 块方差 - Σ_{被修改频率} c_f² / B²

    块方差 = 除直流外全部系数平方和 / B²（Parseval）。
    把"会被修改的那部分频率能量"减掉后，嵌入前后 σ_ex 在系数层面相等：
    修改把 ``c_f`` 变成 ``c_f + δ_f``，块方差与减去的能量同时增加
    ``(2Σc_fδ_f + Σδ_f²)/B²``，两项相消。

    因此提取端算出的 ``c_f / σ_ex`` 不会有量化漂移，也不需要迭代补偿
    （像素取整与饱和截断会带来少量偏差）。

    ``cap`` 给归一化量封顶：``σ_eff = min(σ_ex, cap)``。σ_ex 对嵌入不变，
    "再取个 min"同样是两端一致的确定性函数，判决栅格不受影响；
    它把高对比边缘（σ_ex 很大）那种振幅失控的块压下来 ——
    即"指纹只长在边缘上、且边缘上最显眼"的可见方块伪影来源。
    """
    inv = 1.0 / float(block * block)
    energy = np.zeros_like(std)
    for c in coeff_grids:
        energy += c * c
    var = std * std - energy * inv
    np.maximum(var, 0.0, out=var)
    sig = np.sqrt(var)
    if cap:
        sig = np.minimum(sig, np.float32(cap))
    return sig


# --------------------------------------------------------------------------- 转置（叠加）


def scatter_delta(
    canvas: np.ndarray, block: int, u: int, v: int, width: int
) -> np.ndarray:
    """把"块起点系数增量"摊回像素域（:func:`block_coeff_strip` 的精确转置）。

    参数
    ----
    canvas : (h, width-block+1) 数组，``canvas[a, x]`` 是块起点 (y0+a, x) 的系数增量。
    width  : 目标图像宽度。

    返回
    ----
    (h+block-1, width) 的像素增量，可直接 ``+=`` 到该条带对应的像素行。
    """
    canvas = np.asarray(canvas, dtype=np.float32)
    h, nc = canvas.shape
    pad_lo = block // 2 - 1
    pad_hi = block // 2
    padded = np.zeros((h + pad_lo + pad_hi, nc + pad_lo + pad_hi), dtype=np.float32)
    padded[pad_lo : pad_lo + h, pad_lo : pad_lo + nc] = canvas
    t = ndimage.correlate1d(
        padded, _basis_rev(block, v), axis=1, mode="constant", cval=0.0
    )
    t = ndimage.correlate1d(t, _basis_rev(block, u), axis=0, mode="constant", cval=0.0)
    return t[0 : h + block - 1, 0:width]
