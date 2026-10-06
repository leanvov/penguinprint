"""奇偶量化索引调制（Parity QIM）。

每个可用块在若干中频系数上写同一个 bit，采用**奇偶量化**：

* bit = 0 -> 归一化系数落在 ``2δ`` 的偶数倍
* bit = 1 -> 归一化系数落在 ``2δ`` 的奇数倍

相较 dither-QIM（两个错开 δ/2 的栅格），奇偶量化在**同样最大失真 δ** 下把
判决边界推到离栅格点 ±δ，容噪余量翻倍。

软判决值 ``score = -cos(pi * r)``，``r = mod(x / 2δ, 2)``：

* 完美 bit0 -> r=0 -> score=-1；完美 bit1 -> r=1 -> score=+1
* 判决边界 r=0.5 / 1.5 -> score=0
* ``|score|`` 落在 [0,1]，可直接作为软判决的置信权重。
"""

from __future__ import annotations

import numpy as np

__all__ = ["qim_target", "qim_soft", "qim_hard"]


def qim_target(x: np.ndarray, bit: np.ndarray, delta: float) -> np.ndarray:
    """把归一化系数 ``x`` 量化到与 ``bit`` 同奇偶的最近栅格点。

    最大改动量 = ``delta``。
    """
    step = 2.0 * delta
    q = np.asarray(x, dtype=np.float32) / np.float32(step)
    k = np.rint(q)
    parity = np.mod(k, 2.0)
    need = parity != np.asarray(bit, dtype=np.float32)
    if not np.any(need):
        return (k * np.float32(step)).astype(np.float32)
    k_adj = np.where(q < k, k - 1.0, k + 1.0)
    k = np.where(need, k_adj, k)
    return (k * np.float32(step)).astype(np.float32)


def qim_soft(x: np.ndarray, delta: float) -> np.ndarray:
    """软判决值 ∈ [-1, 1]：正 -> bit 1，负 -> bit 0，绝对值即置信度。"""
    step = 2.0 * delta
    r = np.mod(np.asarray(x, dtype=np.float32) / np.float32(step), 2.0)
    return (-np.cos(np.pi * r)).astype(np.float32)


def qim_hard(score: np.ndarray) -> np.ndarray:
    return (np.asarray(score) >= 0).astype(np.uint8)
