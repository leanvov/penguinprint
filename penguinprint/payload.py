"""指纹载荷：买家标识 → （HMAC-SHA256）→ 短 ID + 校验和 → 比特序列。

载荷结构（默认 12 字节 = 96 bit）::

    [ ID: 7 字节 = HMAC-SHA256(密钥, 规范化标识) 前 7 字节 ][ 校验: 5 字节 CRC ]

这是**应用档位**（``config.WatermarkConfig``）的默认切分。本模块函数的默认参数是
``id_bytes=8, crc_bytes=4``，总长同为 96 bit 但切分不同 —— 直接调用本模块时请显式
传参，否则与程序自身嵌入/提取的载荷互不兼容。

**密钥的作用**：
* 没有密钥时，`ID = SHA256(标识)`，任何人都能为任意标识算出指纹，可栽赃买家，
  也可拿买家名单逐个比对、把图上的 ID 反推回邮箱/手机号；
* 加了密钥，`ID = HMAC(密钥, 标识)`：除持密钥者外无人能生成一致指纹，也无法离线枚举。
* 密钥是 32 字节随机值，存在指纹库里而不是代码里，备份指纹库即备份密钥；
  提取端不需要密钥（读出 ID 查库即可）。

密钥为空（`key=None`）时退化为直接使用 `SHA256(标识)`。
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import zlib

import numpy as np

__all__ = [
    "normalize_identifier",
    "sha256_hex",
    "new_key_hex",
    "fingerprint_bytes",
    "short_code",
    "bytes_to_bits",
    "bits_to_bytes",
    "build_payload",
    "check_payload",
    "payload_bits",
    "spread_pair",
    "bit_index_map",
    "validity_value",
    "crc_delta_table",
    "flip_subsets",
]


def normalize_identifier(identifier: str) -> str:
    """大小写/首尾空白不敏感，避免"同一买家两个指纹"。"""
    return " ".join(str(identifier).strip().split()).lower()


def sha256_hex(identifier: str) -> str:
    return hashlib.sha256(normalize_identifier(identifier).encode("utf-8")).hexdigest()


def new_key_hex(nbytes: int = 32) -> str:
    """生成一个新的 HMAC 密钥（十六进制字符串）。"""
    import os

    return os.urandom(nbytes).hex()


def _key_bytes(key: bytes | str | None) -> bytes | None:
    """把密钥统一成 bytes：支持 bytes 或十六进制字符串；空值返回 None。"""
    if key is None:
        return None
    if isinstance(key, bytes):
        return key or None
    s = str(key).strip()
    if not s:
        return None
    try:
        return bytes.fromhex(s)
    except ValueError:
        return s.encode("utf-8")          # 允许直接用普通口令当密钥


def fingerprint_bytes(
    identifier: str, id_bytes: int = 8, key: bytes | str | None = None
) -> bytes:
    """买家标识 → 嵌入用的短指纹 ID。

    ID = HMAC-SHA256(key, 规范化标识) 的前 ``id_bytes`` 字节；
    未提供 ``key`` 时退化为 SHA256(规范化标识) 的前缀（无密钥，不可防伪造）。
    """
    if id_bytes <= 0:
        raise ValueError("id_bytes 必须为正")
    msg = normalize_identifier(identifier).encode("utf-8")
    kb = _key_bytes(key)
    if kb:
        return _hmac.new(kb, msg, hashlib.sha256).digest()[:id_bytes]
    return hashlib.sha256(msg).digest()[:id_bytes]


def short_code(fp: bytes) -> str:
    """给人看的短码（Base32，8 字节 → 13 字符），用于客服/工单沟通。"""
    return base64.b32encode(fp).decode("ascii").rstrip("=")


_CHECK_SEEDS = (0, 0x9E3779B9, 0x85EBCA6B, 0xC2B2AE35)


def _checksum(data: bytes, size: int = 4) -> bytes:
    """生成 ``size`` 字节校验和。

    要求校验和对消息比特是**仿射函数**：翻转任意 bit 只让校验差值异或一个常量，
    软判决纠错（:func:`crc_delta_table`）才能把组合搜索变成几次异或，
    因此只能用 CRC 这类线性校验。

    多于 4 字节时，用不同初始种子分别算 CRC 再拼接；拼接后整体仍是仿射的。
    本函数缺省 4 字节，应用侧（``WatermarkConfig.crc_bytes``）默认 5 字节（40 bit）。
    """
    out = bytearray()
    for seed in _CHECK_SEEDS:
        out += zlib.crc32(data, seed).to_bytes(4, "big")
        if len(out) >= size:
            break
    return bytes(out[:size])


def build_payload(fp: bytes, crc_bytes: int = 4) -> bytes:
    if len(fp) == 0:
        raise ValueError("指纹 ID 不能为空")
    return bytes(fp) + _checksum(bytes(fp), crc_bytes)


def check_payload(payload: bytes, id_bytes: int = 8, crc_bytes: int = 4) -> tuple[bool, bytes]:
    """校验载荷完整性，返回 (是否通过, 指纹 ID)。"""
    if len(payload) != id_bytes + crc_bytes:
        return False, payload[:id_bytes]
    fp = bytes(payload[:id_bytes])
    expect = _checksum(fp, crc_bytes)
    return expect == bytes(payload[id_bytes:]), fp


def bytes_to_bits(data: bytes) -> np.ndarray:
    """大端序展开为 0/1 的 uint8 数组。"""
    return np.unpackbits(np.frombuffer(bytes(data), dtype=np.uint8)).astype(np.uint8)


def bits_to_bytes(bits: np.ndarray) -> bytes:
    bits = np.asarray(bits, dtype=np.uint8).reshape(-1)
    if bits.size % 8:
        bits = np.concatenate([bits, np.zeros(8 - bits.size % 8, dtype=np.uint8)])
    return np.packbits(bits).tobytes()


def payload_bits(
    identifier: str, id_bytes: int = 8, crc_bytes: int = 4, key: bytes | str | None = None
) -> np.ndarray:
    """买家标识 → 待嵌入比特序列（给了 key 就用 HMAC 派生 ID）。"""
    fp = fingerprint_bytes(identifier, id_bytes, key)
    return bytes_to_bits(build_payload(fp, crc_bytes))


# --------------------------------------------------------------------------- 校验的线性结构
#
# 载荷 = [ID][校验和]，"校验和 == checksum(ID)" 这个条件对 ID 的每个比特都是**仿射**的，
# 于是可以预计算"翻转第 j 个比特会让校验差值异或上什么"，
# 之后判断任意翻转组合是否合法只需几次异或。


def validity_value(
    bits: np.ndarray, id_bytes: int = 8, crc_bytes: int = 4
) -> int:
    """返回 0 表示载荷合法；非 0 的值等于"还差多少才能对上校验"。"""
    data = bits_to_bytes(bits)
    fp = data[:id_bytes]
    stored = int.from_bytes(data[id_bytes : id_bytes + crc_bytes], "big")
    expect = int.from_bytes(_checksum(fp, crc_bytes), "big")
    return int(expect ^ stored)


def crc_delta_table(nbits: int, id_bytes: int = 8, crc_bytes: int = 4) -> np.ndarray:
    """``delta[j]`` = 翻转第 j 个比特时校验差值的异或增量（与基准向量无关）。

    末尾额外补一个 0，供组合搜索时做"空翻转"占位。
    ``crc_bytes`` 可到 8 字节，因此用 uint64 存储。
    """
    base = np.zeros(nbits, dtype=np.uint8)
    zero = validity_value(base, id_bytes, crc_bytes)
    table = np.zeros(nbits + 1, dtype=np.uint64)
    probe = base.copy()
    for j in range(nbits):
        probe[j] ^= 1
        table[j] = np.uint64(validity_value(probe, id_bytes, crc_bytes) ^ zero)
        probe[j] ^= 1
    return table  # 最后一个元素为 0（sentinel）


def flip_subsets(positions: np.ndarray, max_flips: int, sentinel: int) -> "np.ndarray":
    """列出 ``positions`` 中 1..max_flips 个位置的所有组合，用 sentinel 补齐。"""
    import itertools

    pos = [int(p) for p in positions]
    rows: list[tuple[int, ...]] = []
    for k in range(1, max_flips + 1):
        for combo in itertools.combinations(pos, k):
            rows.append(combo + (sentinel,) * (max_flips - k))
    if not rows:
        return np.zeros((0, max_flips), dtype=np.int64)
    return np.asarray(rows, dtype=np.int64)


# --------------------------------------------------------------------------- 空间铺展
#
# 块 (r, c) 承载载荷第 (A*r + B*c) mod L 个 bit。
#
# 为什么必须是"线性"的：裁剪会让栅格整体平移 (r, c) -> (r+my, c+mx)，
# 于是索引整体加上常数 (A*my + B*mx)，也就是载荷序列的**循环移位**——
# 提取端遍历 L 种循环移位即可复原，无需知道裁剪位置。
# 任何非线性（例如按行优先编号 r*W+c）都会被裁剪破坏，因为 W 会变。
#
# A、B 的取值：两者都取与载荷长度成固定比例的值，且都不等于 1。
# 若取 (1, 1)，索引只能取到 r+c ∈ [0, nr+nc-1]，小图/小裁剪下大部分 bit 一票都没有，
# 必然译码失败。
#
# 覆盖范围：一个 bit 只有在 (A*r + B*c) ≡ 该下标 (mod L) 的块上才有票，
# 因此拿到票的 bit 数随块栅格增大而增加 —— 块太少（例如只有 3x3 块）时，
# 只有部分 bit 能取到值，其余 bit 没有任何投票者。这是小图/小裁剪的译码前提。

_SPREAD_CACHE: dict[int, tuple[int, int]] = {}
_SPREAD_RATIO = (0.0938, 0.2604)  # L=96 时即 (9, 25)


def spread_pair(bits: int) -> tuple[int, int]:
    """返回铺展系数 ``(A, B)``，两者取与载荷长度成固定比例的值，使 ``bit = (A*r + B*c) % bits``。"""
    pair = _SPREAD_CACHE.get(bits)
    if pair is None:
        a = max(1, int(round(_SPREAD_RATIO[0] * bits)))
        b = max(1, int(round(_SPREAD_RATIO[1] * bits)))
        pair = (a, b)
        _SPREAD_CACHE[bits] = pair
    return pair


def bit_index_map(bits: int, rows: int, cols: int, row_offset: int = 0) -> np.ndarray:
    """栅格 (rows, cols) 对应的载荷比特下标，形状 (rows, cols)。"""
    a, b = spread_pair(bits)
    r = (np.arange(rows, dtype=np.int64) + int(row_offset))[:, None]
    c = np.arange(cols, dtype=np.int64)[None, :]
    return (a * r + b * c) % bits
