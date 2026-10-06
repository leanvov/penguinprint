"""图像读写与亮度通道工具。

* 亮度 Y 用 Rec.601 线性组合（权重和为 1）计算；
  因此"给 R/G/B 三个通道同时加上同一个 Δ"会让 Y 精确增加 Δ，
  嵌入/提取不需要做 YCbCr 往返，避免色度往返带来的额外误差。
* 尽量保留原图的元数据与透明度：ICC 各格式都带；EXIF 只对 JPEG/WebP/TIFF 保留
  （PNG 存不下 EXIF）；BMP 只存 RGB，ICC 与 alpha 都会丢。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile

from .config import DEFAULT_OUTPUT_DIRNAME

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

IMAGE_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".webp", ".tif", ".tiff"}
LUMA_WEIGHTS = np.array([0.299, 0.587, 0.114], dtype=np.float32)

__all__ = [
    "IMAGE_EXTS",
    "ImageBundle",
    "luma",
    "apply_luma_delta",
    "load_image",
    "save_image",
    "discover_images",
    "psnr",
    "jpeg_subsampling_of",
    "estimate_jpeg_quality",
]

# JPEG 亮度量化表（IJG 标准基表），用于反推源图质量因子
_STD_LUMA_TABLE = np.array(
    [
        16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
        14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
        18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
        49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
    ],
    dtype=np.float64,
)


def estimate_jpeg_quality(im: Image.Image) -> int | None:
    """从 JPEG 量化表反推保存质量（1~100），非 JPEG 返回 None。"""
    try:
        tables = im.quantization
    except Exception:  # noqa: BLE001
        return None
    if not tables:
        return None
    table = np.array(tables.get(0) or next(iter(tables.values())), dtype=np.float64)
    if table.size != 64:
        return None
    ratio = float(np.mean(table / _STD_LUMA_TABLE))
    if ratio <= 1.0:
        quality = 100.0 - ratio * 50.0
    else:
        quality = 50.0 / max(1e-6, ratio)
    return int(round(min(100.0, max(1.0, quality))))


def jpeg_subsampling_of(im: Image.Image) -> int | None:
    """从 JPEG 分量采样因子推出 PIL 的 subsampling 取值（0=4:4:4, 1=4:2:2, 2=4:2:0）。"""
    layer = getattr(im, "layer", None)
    if not layer:
        return None
    y = layer[0]
    hs, vs = int(y[1]), int(y[2])
    if hs == 1 and vs == 1:
        return 0
    if hs == 2 and vs == 1:
        return 1
    if hs == 2 and vs == 2:
        return 2
    return None


@dataclass
class ImageBundle:
    """一张图的内存表示：RGB 像素 + 可选 alpha + 元数据。"""

    rgb: np.ndarray
    alpha: np.ndarray | None = None
    fmt: str | None = None
    exif: bytes | None = None
    icc: bytes | None = None
    info: dict = field(default_factory=dict)

    @property
    def size(self) -> tuple[int, int]:
        h, w = self.rgb.shape[:2]
        return w, h

    def to_pil(self) -> Image.Image:
        if self.alpha is None:
            return Image.fromarray(self.rgb, "RGB")
        rgba = np.dstack([self.rgb, self.alpha])
        return Image.fromarray(rgba, "RGBA")


def luma(rgb: np.ndarray) -> np.ndarray:
    """RGB(uint8/float) -> 亮度 float32 (H, W)。"""
    return np.asarray(rgb, dtype=np.float32) @ LUMA_WEIGHTS


def apply_luma_delta(rgb: np.ndarray, delta: np.ndarray) -> np.ndarray:
    """给三个通道同时加 delta：未触发饱和截断时亮度精确增加 delta
    （另有 ≤0.5 灰阶的取整误差），返回 uint8。"""
    out = np.asarray(rgb, dtype=np.float32) + np.asarray(delta, dtype=np.float32)[..., None]
    np.rint(out, out=out)
    np.clip(out, 0.0, 255.0, out=out)
    return out.astype(np.uint8)


def load_image(path: str | Path) -> ImageBundle:
    path = Path(path)
    with Image.open(path) as im:
        fmt = im.format
        exif = im.info.get("exif")
        icc = im.info.get("icc_profile")
        info = {k: v for k, v in im.info.items() if k in ("dpi", "transparency")}
        # 记住源图的色度采样与质量，保存时不要"越存越差"
        info["subsampling"] = jpeg_subsampling_of(im)
        info["source_quality"] = estimate_jpeg_quality(im)
        alpha = None
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            arr = np.asarray(rgba)
            rgb = np.ascontiguousarray(arr[..., :3])
            alpha = np.ascontiguousarray(arr[..., 3])
        else:
            rgb = np.ascontiguousarray(np.asarray(im.convert("RGB")))
    return ImageBundle(rgb=rgb, alpha=alpha, fmt=fmt, exif=exif, icc=icc, info=info)


def save_image(
    bundle: ImageBundle,
    path: str | Path,
    quality: int = 95,
    subsampling: int | str = "auto",
) -> None:
    """按扩展名保存。

    ``subsampling="auto"`` 时跟随源图的色度采样（源是 4:4:4 就存 4:4:4）；
    未知时用 4:4:4。JPEG 的质量取 ``min(98, max(quality, 源图估计质量))``，
    避免在源图质量已经很低时再压一遍。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img = bundle.to_pil()
    ext = path.suffix.lower()
    kwargs: dict = {}
    if bundle.icc:
        kwargs["icc_profile"] = bundle.icc
    if bundle.exif and ext in (".jpg", ".jpeg", ".jpe", ".jfif", ".webp", ".tif", ".tiff"):
        kwargs["exif"] = bundle.exif
    if ext in (".jpg", ".jpeg", ".jpe", ".jfif"):
        sub = bundle.info.get("subsampling") if subsampling == "auto" else subsampling
        if sub is None:
            sub = 0  # 未知来源默认 4:4:4，宁可文件大一点也不糊色
        src_q = bundle.info.get("source_quality") or 0
        use_q = int(min(98, max(int(quality), int(src_q))))
        kwargs.update(quality=use_q, subsampling=int(sub), optimize=True)
        img.convert("RGB").save(path, format="JPEG", **kwargs)
    elif ext == ".webp":
        kwargs.update(quality=int(quality), method=4)
        img.save(path, format="WEBP", **kwargs)
    elif ext in (".tif", ".tiff"):
        img.save(path, format="TIFF", **kwargs)
    elif ext == ".bmp":
        img.convert("RGB").save(path, format="BMP")
    else:
        img.save(path, format="PNG", **kwargs)


def discover_images(root: str | Path, recursive: bool = True) -> list[Path]:
    """列出 ``root`` 下支持的图片。

    ``root`` 是文件时只看它自己；是目录时按 ``recursive`` 决定是否递归。

    跳过隐藏目录、``__pycache__``，以及 ``root`` 之下名为 ``DEFAULT_OUTPUT_DIRNAME``
    的目录（避免把程序自己写出的输出图当成待处理图）。
    判断只看相对 ``root`` 的路径分段，因此 ``root`` 自身是输出目录时其内容照常返回；
    目录名按等值比较，``_penguinprint_out_old`` 这类同前缀目录不会被跳过。
    """
    root = Path(root)
    if root.is_file():
        return [root] if root.suffix.lower() in IMAGE_EXTS else []
    pattern = "**/*" if recursive else "*"
    out: list[Path] = []
    for p in sorted(root.glob(pattern)):
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        relative_parts = p.relative_to(root).parts[:-1]  # 不含文件名，也不含 root 自身
        if any(
            part.startswith(".")
            or part == "__pycache__"
            or part == DEFAULT_OUTPUT_DIRNAME
            for part in relative_parts
        ):
            continue
        out.append(p)
    return out


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        return float("nan")
    mse = float(np.mean((a - b) ** 2))
    if mse <= 0:
        return float("inf")
    return float(10.0 * np.log10(255.0 * 255.0 / mse))
