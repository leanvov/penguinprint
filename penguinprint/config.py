"""penguinprint（企鹅指纹）全局参数与强度档位。

参数含义：

block_size       : 指纹块边长（像素）。
coefficients     : 使用的 DCT 中频系数对 (u, v)，每块在这些频率上各写一遍同一 bit。
delta            : 奇偶量化的半格宽：栅格步长 = 2·delta，单系数最大改动量也是 2·delta。
                   越大越稳、越易见。
sigma_min        : 只在高方差（有纹理）块上嵌入；方差太小的块会被 JPEG 抹平。
sigma_cap        : **归一化量封顶**。块活跃度 σ_ex 在高对比边缘上很大，
                   而扰动幅度 ∝ σ_ex，于是"指纹只长在边缘上、且边缘上最显眼"，
                   表现为肉眼可见的方块状伪影。封顶后取 σ_eff = min(σ_ex, σ_cap)：
                   嵌入与提取两端用同一个公式，判决栅格不受影响，
                   只是把极端块的振幅压下来（实际值仍受像素取整与饱和截断的微小影响）。
luma_min/max     : 过暗/过亮的块不嵌入（避免裁切饱和带来的偏差）。
normalizer_scales: 提取端额外搜索的"归一化倍率" k（chat = c / (k·σ_eff)）。
                   整体调色 / 曲线 / 对比压缩会把"系数 : 归一化量"的比例整体缩放，
                   一维扫描 k 就能把晶格重新对上，成本远低于多尺度重采样。
id_bytes         : 嵌入的指纹 ID 字节数（HMAC-SHA256(密钥, 标识) 的前缀；
                   只有无密钥时才退化为裸 SHA-256）。
crc_bytes        : 校验和字节数（实现是多个种子的 CRC-32 拼接后截断）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path


def _runtime_base_dir() -> Path:
    """运行期"程序所在目录"。

    * 源码运行：项目根目录（`penguinprint/` 的上一级）；
    * PyInstaller 冻结运行：**可执行文件所在目录**（`__file__` 在 onefile 模式下
      指向临时解包目录 `_MEIPASS`，每次运行都不同、还会被删掉，
      数据库和输出在那儿会消失）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".penguinprint_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001 - 任何原因不可写都退回用户目录
        return False


def _data_dir() -> Path:
    """默认数据目录：优先 **用户配置目录** `%APPDATA%\\PenguinPrint`，不可写才退回程序旁边。

    用户目录是 Windows 给每个用户的私有可写位置，普通权限即可读写；
    程序旁边（Program Files、只读 U 盘、网络盘）常常不可写。
    """
    appdata = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    user_dir = Path(appdata) / "PenguinPrint" if appdata else Path.home() / ".penguinprint"
    if _writable(user_dir):
        return user_dir
    beside = _runtime_base_dir()
    if _writable(beside):
        return beside
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


PROJECT_ROOT = _runtime_base_dir()
DATA_DIR = _data_dir()
DEFAULT_DB_PATH = DATA_DIR / "fingerprints.db"
DEFAULT_OUTPUT_DIRNAME = "_penguinprint_out"

# 用户设置（目前只有"数据目录"一项）存哪里：
#   * 默认写到 **用户配置目录** `%APPDATA%\PenguinPrint\settings.json`；
#   * **读取**时优先看"程序旁边的 penguinprint.json"，想要便携（设置跟着 U 盘走）
#     的人可以手动放一个。
_PORTABLE_SETTINGS = PROJECT_ROOT / "penguinprint.json"


def _user_settings_dir() -> Path:
    """设置文件所在目录 = **默认数据目录**（已做过可写性判断，且不随用户改目录而移动）。"""
    return DATA_DIR


USER_SETTINGS_PATH = _user_settings_dir() / "settings.json"


def settings_file() -> Path:
    """当前生效的设置文件：程序旁的便携文件优先，否则用户目录里的那个。"""
    return _PORTABLE_SETTINGS if _PORTABLE_SETTINGS.exists() else USER_SETTINGS_PATH


def load_settings() -> dict:
    """读用户设置；文件不存在或损坏都当作空设置，不影响启动。"""
    try:
        p = settings_file()
        if p.exists():
            import json

            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_settings(**kwargs: object) -> None:
    """合并写入用户设置（写不进去就静默忽略）。"""
    try:
        import json

        cur = load_settings()
        cur.update(kwargs)
        USER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        USER_SETTINGS_PATH.write_text(
            json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def data_dir() -> Path:
    """当前"数据目录"：用户设过就用它，否则用默认的 DATA_DIR
    （用户配置目录 `%APPDATA%\\PenguinPrint`，不可写时才退回程序目录）。"""
    raw = load_settings().get("data_dir")
    if raw:
        try:
            return Path(str(raw)).expanduser()
        except Exception:  # noqa: BLE001
            pass
    return DATA_DIR


def set_data_dir(path: str | Path | None) -> Path:
    """设置数据目录（传 None 表示恢复默认），返回生效后的目录。"""
    if path in (None, ""):
        save_settings(data_dir=None)
        return DATA_DIR
    p = Path(str(path)).expanduser()
    save_settings(data_dir=str(p))
    return p


def effective_db_path() -> Path:
    """指纹库的最终位置：数据目录 / fingerprints.db。"""
    return data_dir() / "fingerprints.db"


@dataclass(frozen=True)
class WatermarkConfig:
    """指纹嵌入 / 提取参数（不可变，用 :meth:`with_delta` 派生新配置）。"""

    block_size: int = 16
    coefficients: tuple[tuple[int, int], ...] = ((2, 3), (3, 2), (1, 5), (5, 1))
    delta: float = 0.45
    sigma_min: float = 4.0
    sigma_cap: float = 24.0
    luma_min: float = 3.0
    luma_max: float = 252.0
    id_bytes: int = 7
    crc_bytes: int = 5
    strip_rows: int = 512
    refine_passes: int = 0
    min_blocks: int = 24
    min_coverage: float = 0.60
    # "检测到指纹痕迹"的门限：存在性检测的显著性
    #   z = (E|软判决| − 2/π) / (0.25/√票数)，只统计 |归一化系数| ≥ δ/2 的块。
    # 无水印时 E|score| = 2/π，有水印时它们落在栅格上 ≈ 1。
    trace_presence_z: float = 6.0
    # 局部栅格一致性判据：非刚性局部变形（液化/变形）里全图相位失配、还原不出载荷，
    # 但小窗口内仍能看出"水印还在"（水印图的局部一致性高于干净图，所以还要求该窗
    # 至少有 min_blocks 个可用块）。
    local_trace_align: float = 0.85
    local_trace_window: int = 256
    # 提取端的归一化倍率搜索（1.0 必须排在第一个，先试常规路径）
    normalizer_scales: tuple[float, ...] = (1.0, 1.15, 1.3, 1.5, 1.75, 2.0, 0.85, 0.7, 2.5, 0.6)
    k_search_top_phases: int = 16
    # 噪声自适应门限：估计逐像素噪声 σ_n 后，把门限抬到 max(σ_min, min(noise_k·σ_n, σ_cap))。
    # 加杂色会把平坦/纯白区的 σ_ex 抬过门限，让"从没嵌过"的块变成投票者，把真正的指纹票淹掉；
    # 抬门限能把它们挡掉，且只在估计噪声 > noise_floor_trigger 时启用。
    noise_k: float = 2.5
    noise_floor_trigger: float = 1.5
    # 跨频率一致性加权：同一块的多个频率编码同一个 bit，真水印块的一致度高、
    # 随机内容块的一致度低，用它再压一次非水印块的噪声投票。
    consistency_weight: bool = True
    # 投票权重下限：权重 = clip(|归一化系数|/2δ, floor, 1)，用于压制"被模糊/重压缩
    # 压塌的块"（它们会整体偏向 bit 0，属于有偏噪声）
    vote_weight_floor: float = 0.4
    # 软判决纠错：(最大翻转数, 参与搜索的最不可靠比特数, 尝试的相位数)
    # 相位数 0 表示"所有候选相位"。先用极便宜的广扫，再逐级加深。
    repair_schedule: tuple[tuple[int, int, int], ...] = ((2, 8, 0), (4, 16, 8), (6, 20, 1))
    save_quality: int = 95
    save_subsampling: str = "auto"  # auto = 跟随源图（源为 4:4:4 就存 4:4:4）
    verify: bool = True
    max_retries: int = 2
    retry_delta_gain: float = 1.45

    # ---------------------------------------------------------------- 派生量
    @property
    def payload_bytes(self) -> int:
        return self.id_bytes + self.crc_bytes

    @property
    def payload_bits(self) -> int:
        return self.payload_bytes * 8

    @property
    def quant_step(self) -> float:
        """奇偶量化的栅格间距（bit0 落在偶数倍、bit1 落在奇数倍）。"""
        return 2.0 * self.delta

    # ---------------------------------------------------------------- 校验
    def validate(self) -> None:
        if self.block_size < 8 or self.block_size % 2:
            raise ValueError("block_size 必须是 >=8 的偶数")
        if not self.coefficients:
            raise ValueError("coefficients 不能为空")
        for u, v in self.coefficients:
            if not (0 <= u < self.block_size and 0 <= v < self.block_size):
                raise ValueError(f"DCT 系数 {(u, v)} 超出块范围 {self.block_size}")
            if u == 0 and v == 0:
                raise ValueError("不能使用直流系数 (0, 0)")
        if self.delta <= 0:
            raise ValueError("delta 必须为正")
        if self.sigma_min <= 0:
            raise ValueError("sigma_min 必须为正")
        if self.sigma_cap and self.sigma_cap <= self.sigma_min:
            raise ValueError("sigma_cap 必须大于 sigma_min（或设 0 表示不封顶）")
        if not self.normalizer_scales or abs(self.normalizer_scales[0] - 1.0) > 1e-9:
            raise ValueError("normalizer_scales 必须存在，且第一个必须是 1.0")
        if any(k <= 0 for k in self.normalizer_scales):
            raise ValueError("normalizer_scales 必须为正")
        if self.id_bytes < 4:
            raise ValueError("id_bytes 太小，指纹 ID 至少 4 字节")
        if self.crc_bytes < 2:
            raise ValueError("crc_bytes 至少 2 字节")

    def with_delta(self, delta: float) -> "WatermarkConfig":
        return replace(self, delta=float(delta))

    def replace(self, **kwargs) -> "WatermarkConfig":
        return replace(self, **kwargs)


# 三档强度，GUI / CLI 直接选档即可。
# 三档的 δ·σ_cap 取值接近，使"单系数最大扰动"基本一致，
# 差异主要来自冗余与纠错能力。
# sigma_min 取 4：块活跃度 4~9 的那些块载体只有 1~4 个灰阶（肉眼不可见），
# 但在裁剪/调色这类"保幅"攻击里仍是有效投票。
PROFILES: dict[str, WatermarkConfig] = {
    "隐形": WatermarkConfig(delta=0.30, sigma_min=4.0, sigma_cap=36.0),
    "标准": WatermarkConfig(delta=0.45, sigma_min=4.0, sigma_cap=24.0),
    "强韧": WatermarkConfig(
        delta=0.65,
        sigma_min=4.0,
        sigma_cap=17.0,
        coefficients=((2, 3), (3, 2), (1, 5), (5, 1), (2, 5), (5, 2)),
    ),
}
PROFILE_NAMES: list[str] = list(PROFILES)

for _cfg in PROFILES.values():
    _cfg.validate()


def get_profile(name: str | None) -> WatermarkConfig:
    """按名称取强度档位，默认"标准"。"""
    if not name:
        return PROFILES["标准"]
    if name in PROFILES:
        return PROFILES[name]
    raise KeyError(f"未知强度档位 {name!r}，可选：{', '.join(PROFILE_NAMES)}")


def strip_ranges(origin_count: int, strip_rows: int, block: int) -> list[tuple[int, int]]:
    """把 [0, origin_count) 切成若干条带，条带起点与长度都是 block 的整数倍（末条除外）。

    块状处理保证大图也不会一次性占用过多内存。
    """
    if origin_count <= 0:
        return []
    step = max(block, (int(strip_rows) // block) * block)
    out: list[tuple[int, int]] = []
    start = 0
    while start < origin_count:
        end = min(start + step, origin_count)
        out.append((start, end))
        start = end
    return out
