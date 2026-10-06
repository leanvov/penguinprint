"""penguinprint（企鹅指纹）—— 面向图片交易的鲁棒指纹 / 溯源系统。

核心思想（对应 README 需求）：
1. 抗裁剪：指纹以极低码率（96 bit）在整个画面按块状冗余重复上千次，
   提取时用"块栅格相位搜索 + 循环移位搜索"重新对齐，任意裁剪区域都能恢复。
2. 抗画质降低 / 调色 / 局部修图：在亮度通道的 DCT 中频系数上做
   "逐块统计量归一化 + 奇偶量化索引调制（Parity QIM）"，
   软判决（-cos(pi*r)）加权多数表决 + 校验和验证。
3. GUI 全流程：设定图片目录 + 买家标识 → 批量嵌入 → 可溯源提取。
"""

from .config import (
    DEFAULT_DB_PATH,
    DEFAULT_OUTPUT_DIRNAME,
    PROFILE_NAMES,
    PROFILES,
    WatermarkConfig,
    get_profile,
)
from .payload import (
    build_payload,
    check_payload,
    fingerprint_bytes,
    normalize_identifier,
    payload_bits,
    short_code,
)
from .registry import Registry
from .embedder import embed_bits
from .extractor import ExtractResult, extract_bits
from .pipeline import embed_directory, extract_many, write_rows_csv

__version__ = "1.0.0"

__all__ = [
    "WatermarkConfig",
    "PROFILES",
    "PROFILE_NAMES",
    "get_profile",
    "DEFAULT_DB_PATH",
    "DEFAULT_OUTPUT_DIRNAME",
    "Registry",
    "build_payload",
    "check_payload",
    "fingerprint_bytes",
    "normalize_identifier",
    "payload_bits",
    "short_code",
    "embed_bits",
    "extract_bits",
    "ExtractResult",
    "embed_directory",
    "extract_many",
    "write_rows_csv",
    "__version__",
]
