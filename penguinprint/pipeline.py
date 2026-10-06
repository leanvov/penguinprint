"""批量流水线：目录 → 逐张嵌入 → 回读校验 → 记录指纹库 → 生成报表。"""

from __future__ import annotations

import csv
import hashlib
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import DEFAULT_OUTPUT_DIRNAME, WatermarkConfig, get_profile
from .embedder import EmbedStats, embed_bits
from .extractor import ExtractResult, extract_bits
from .imgio import IMAGE_EXTS, discover_images, load_image, save_image
from .payload import payload_bits
from .registry import Registry

__all__ = [
    "EmbedRow",
    "ExtractRow",
    "embed_directory",
    "embed_one",
    "extract_many",
    "extract_path",
    "write_rows_csv",
]

Progress = Callable[[int, int, str], None]
StopFlag = Callable[[], bool]


def _noop_progress(done: int, total: int, message: str) -> None:  # pragma: no cover
    return None


def _never_stop() -> bool:  # pragma: no cover
    return False


@dataclass
class EmbedRow:
    src: str
    dst: str
    status: str
    identifier: str
    id_hex: str
    short_code: str
    batch: str = ""
    psnr: float = 0.0
    blocks_used: int = 0
    max_abs_diff: int = 0
    distortion_p999: float = 0.0
    usable_ratio: float = 0.0
    verified: bool = False
    confidence: float = 0.0
    retries: int = 0
    delta: float = 0.0
    elapsed: float = 0.0
    note: str = ""


@dataclass
class ExtractRow:
    src: str
    status: str
    id_hex: str = ""
    short_code: str = ""
    identifier: str = ""
    confidence: float = 0.0
    quality_best: float = 0.0
    presence_z: float = 0.0
    blocks: int = 0
    coverage: float = 0.0
    votes_per_bit: float = 0.0
    phase: str = ""
    stage: int = 0
    elapsed: float = 0.0
    note: str = ""


def _row_from_result(path: Path, res: ExtractResult) -> ExtractRow:
    return ExtractRow(
        src=str(path),
        status="成功" if res.found else ("疑似痕迹" if res.trace else "未检出"),
        id_hex=res.id_hex,
        short_code=res.short_code,
        identifier=res.identifier or "",
        confidence=res.confidence,
        quality_best=res.quality_best,
        presence_z=res.presence_z,
        blocks=res.blocks,
        coverage=res.coverage,
        votes_per_bit=res.votes_per_bit,
        phase="" if res.phase is None else f"{res.phase[0]},{res.phase[1]}",
        stage=res.stage,
        elapsed=res.elapsed,
        note=res.note or res.message,
    )


# --------------------------------------------------------------------------- 嵌入


def _sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """文件的 SHA-256（十六进制）。用于交付记录里"证明是哪一份文件"。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()
    except Exception:  # noqa: BLE001 - 哈希失败不影响嵌入本身
        return ""


def embed_one(
    src: str | Path,
    dst: str | Path,
    identifier: str,
    cfg: WatermarkConfig,
    registry: Registry | None = None,
    verify: bool | None = None,
    key: bytes | str | None = None,
    batch: str = "",
) -> EmbedRow:
    """单张嵌入 + 回读校验（校验失败时逐级加强 delta 重试）。

    ``key`` 为 HMAC 密钥：给了它，指纹就只有持密钥者能生成（防伪造）；
    不给则按无密钥规则由标识直接派生 ID。``batch`` 是批次/订单号，只写进交付记录。
    """
    src, dst = Path(src), Path(dst)
    do_verify = cfg.verify if verify is None else verify
    bits = payload_bits(identifier, cfg.id_bytes, cfg.crc_bytes, key=key)
    id_hex = _id_hex(identifier, cfg, key)
    row = EmbedRow(
        src=str(src),
        dst=str(dst),
        status="失败",
        identifier=identifier,
        id_hex=id_hex,
        short_code=_short(identifier, cfg, key),
        batch=str(batch or ""),
        delta=cfg.delta,
    )
    t0 = time.perf_counter()
    bundle = load_image(src)
    attempt_cfg = cfg
    stats: EmbedStats | None = None
    res: ExtractResult | None = None
    for attempt in range(cfg.max_retries + 1):
        try:
            out, stats = embed_bits(bundle.rgb, bits, attempt_cfg)
        except Exception as exc:  # noqa: BLE001
            row.status = "失败"
            row.note = f"嵌入失败：{exc}"
            row.elapsed = time.perf_counter() - t0
            return row
        save_bundle = bundle
        save_bundle.rgb = out
        save_image(
            save_bundle,
            dst,
            quality=attempt_cfg.save_quality,
            subsampling=attempt_cfg.save_subsampling,
        )
        row.retries = attempt
        if not do_verify:
            row.status = "已嵌入（未校验）"
            row.note = "未启用回读校验"
            break
        reloaded = load_image(dst)
        res = extract_bits(reloaded.rgb, attempt_cfg, registry=None, quick=True)
        if res.found:
            row.status = "成功"
            row.verified = True
            row.confidence = res.confidence
            row.note = "回读校验通过"
            break
        # 如果失败原因是"可用纹理块太少/覆盖率不足"，提高 delta 没用，只会放大可见伪影
        if res.coverage < attempt_cfg.min_coverage or res.blocks < attempt_cfg.min_blocks:
            row.status = "已嵌入（冗余不足）"
            row.verified = False
            row.note = (
                f"可用纹理块的比特覆盖率仅 {res.coverage * 100:.0f}%"
                f"（需 ≥{attempt_cfg.min_coverage * 100:.0f}%），"
                "属于该图纹理分布问题，提高强度无法解决，已按原始强度输出"
            )
            break
        attempt_cfg = attempt_cfg.with_delta(attempt_cfg.delta * cfg.retry_delta_gain)
        row.status = "校验未通过"
        row.note = f"回读校验未通过（已尝试 {attempt + 1} 次，delta 提升至 {attempt_cfg.delta:.2f}）"

    if stats is not None:
        row.psnr = stats.psnr
        row.blocks_used = stats.blocks_used
        row.max_abs_diff = stats.max_abs_diff
        row.distortion_p999 = stats.distortion_p999
        row.usable_ratio = stats.coverage
        row.delta = stats.delta
        if stats.warning and row.status == "成功":
            row.note = f"{row.note}；{stats.warning}"
    row.elapsed = time.perf_counter() - t0
    if registry is not None and row.status != "失败":
        try:
            registry.register(identifier, cfg.id_bytes)
            registry.log_embed(
                id_hex,
                str(src),
                str(dst),
                row.psnr,
                row.blocks_used,
                row.verified,
                batch=batch,
                src_sha256=_sha256_file(src),
                dst_sha256=_sha256_file(dst),
            )
        except Exception:  # noqa: BLE001 - 指纹库故障不应中断批量任务
            pass
    return row


def _id_hex(identifier: str, cfg: WatermarkConfig, key: bytes | str | None = None) -> str:
    from .payload import fingerprint_bytes

    return fingerprint_bytes(identifier, cfg.id_bytes, key).hex().upper()


def _short(identifier: str, cfg: WatermarkConfig, key: bytes | str | None = None) -> str:
    from .payload import fingerprint_bytes, short_code

    return short_code(fingerprint_bytes(identifier, cfg.id_bytes, key))


def _process_write_context() -> str:
    """诊断用：当前进程的权限上下文（仅 Windows 有内容，其它平台返回空串）。

    "输出目录写不了"只有三种可能：进程被降权（完整性级别不是 Medium）、
    目标目录的 ACL 不允许、或者安全软件拦截。把进程这边的关键事实打出来，
    便于定位是哪一种。
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return ""
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
        ]
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
        ]

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)  # TOKEN_QUERY
        ):
            return ""
        try:
            def query(info_class: int):
                size = wintypes.DWORD()
                advapi32.GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))
                if not size.value:
                    return None
                buf = ctypes.create_string_buffer(size.value)
                if not advapi32.GetTokenInformation(
                    token, info_class, buf, size.value, ctypes.byref(size)
                ):
                    return None
                return buf

            parts: list[str] = []
            buf = query(25)  # TokenIntegrityLevel
            if buf is not None:
                sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                count = advapi32.GetSidSubAuthorityCount(sid)
                if count and count[0]:
                    rid = advapi32.GetSidSubAuthority(sid, count[0] - 1)[0]
                    # 正常桌面程序是 Medium；Low/Untrusted = 进程被限制了
                    parts.append("完整性级别=" + {
                        0x0000: "Untrusted", 0x1000: "Low", 0x2000: "Medium",
                        0x3000: "High", 0x4000: "System",
                    }.get(rid, hex(rid)))
            buf = query(29)  # TokenIsAppContainer
            if buf is not None:
                inside = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
                parts.append("受限容器=" + ("是" if inside else "否"))
            buf = query(20)  # TokenElevation
            if buf is not None:
                elevated = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
                parts.append("管理员提权=" + ("是" if elevated else "否"))
            buf = query(1)  # TokenUser
            if buf is not None:
                sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                text = wintypes.LPWSTR()
                if advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                    parts.append(f"用户={text.value}")
                    kernel32.LocalFree(text)
            return "，".join(parts)
        finally:
            kernel32.CloseHandle(token)
    except Exception:  # noqa: BLE001 - 诊断失败不覆盖原始错误
        return ""


def _ensure_writable_dir(out_dir: Path) -> None:
    """开始嵌入前先确认输出目录可写。

    不确认的话，每一张图都会在写文件时各失败一次，用户只看到一堆
    ``[WinError 5] 拒绝访问``，看不出问题出在目录上。常见原因两类：
    图片放在只读位置（U 盘写保护、网络盘、系统目录），或者程序跑在只放行
    部分目录的受限环境里（沙箱、AppContainer 等）。两种都不是算法问题，
    换一个可写的输出目录即可。
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".penguinprint_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        context = _process_write_context()
        raise PermissionError(
            f"输出目录不可写：{out_dir}\n\n"
            f"{exc}\n\n"
            + (f"当前进程权限：{context}\n\n" if context else "")
            + "请改用另一个输出目录（例如在「文档」下新建一个文件夹）后重试；原图不会被修改。"
        ) from exc


def embed_directory(
    input_dir: str | Path,
    identifier: str,
    cfg: WatermarkConfig | None = None,
    out_dir: str | Path | None = None,
    recursive: bool = True,
    registry: Registry | None = None,
    progress: Progress | None = None,
    should_stop: StopFlag | None = None,
    verify: bool | None = None,
    batch: str = "",
) -> tuple[list[EmbedRow], dict]:
    """把一个目录**或单张图片**嵌上同一买家指纹。返回 (逐张结果, 汇总)。

    传目录：递归（默认）处理其中所有图片；
    传单个图片文件：只处理这一张，输出默认写到**该图片所在目录**下的
    `_penguinprint_out`（原图不动，输出同名文件）。

    密钥（HMAC）不需要显式传：给了 ``registry`` 就用指纹库里的那把
    （嵌入用的 ID 与库里登记的 ID 由同一把密钥与同一长度派生，因而一致）。
    ``batch`` 是订单/批次号，连同源图与输出图的 SHA-256 一起写进交付记录。
    """
    cfg = cfg or get_profile("标准")
    cfg.validate()
    identifier = str(identifier).strip()
    # 买家标识可以是邮箱/手机号/订单号/昵称等**任意**非空字符串（不要求含 @）。
    # 这里只做与 Registry.register() 一致的合理性检查。
    if not identifier:
        raise ValueError("请填写买家标识（用于生成指纹 ID）")
    if len(identifier) > 200:
        raise ValueError(f"买家标识过长（{len(identifier)} 字符，最多 200）")
    target = Path(input_dir)
    single_file: Path | None = None
    if target.is_file():
        single_file = target
        base_dir = target.parent
    elif target.is_dir():
        base_dir = target
    else:
        raise ValueError(f"输入路径不存在：{target}")
    out_dir = Path(out_dir) if out_dir else base_dir / DEFAULT_OUTPUT_DIRNAME
    progress = progress or _noop_progress
    should_stop = should_stop or _never_stop

    if single_file is not None:
        files = [single_file]
    else:
        files = discover_images(base_dir, recursive)
    out_resolved = out_dir.resolve()
    files = [p for p in files if out_resolved not in p.resolve().parents and p.resolve() != out_resolved]

    rows: list[EmbedRow] = []
    total = len(files)
    # 先确认输出目录可写，再登记指纹库、再开跑：否则白等一场，还留下一堆失败记录
    if total:
        _ensure_writable_dir(out_dir)
    # 密钥只在指纹库里：有 registry 就用它，保证嵌入的 ID 与库里登记的 ID 一致
    key = getattr(registry, "key", None) if registry is not None else None
    if registry is not None:
        registry.register(identifier, cfg.id_bytes)
    progress(0, total, f"发现 {total} 张图片")
    t0 = time.perf_counter()
    for i, src in enumerate(files, start=1):
        if should_stop():
            progress(i - 1, total, "用户已停止")
            break
        # 必须相对 base_dir 求相对路径：单文件模式下 input_dir 指向文件本身，
        # 用它做 relative_to 会得到 "."，输出路径就退化成了目录本身。
        rel = src.relative_to(base_dir)
        dst = out_dir / rel
        try:
            row = embed_one(
                src, dst, identifier, cfg, registry=registry, verify=verify,
                key=key, batch=batch,
            )
        except Exception as exc:  # noqa: BLE001
            row = EmbedRow(
                src=str(src),
                dst=str(dst),
                status="失败",
                identifier=identifier,
                id_hex=_id_hex(identifier, cfg, key),
                short_code=_short(identifier, cfg, key),
                batch=str(batch or ""),
                note=f"异常：{exc}",
            )
        rows.append(row)
        progress(i, total, f"[{i}/{total}] {rel.name} → {row.status}")

    summary = _summarize_embed(
        rows, total, time.perf_counter() - t0, out_dir, identifier, cfg, key=key
    )
    summary["batch"] = str(batch or "")
    return rows, summary


def _summarize_embed(
    rows: Sequence[EmbedRow],
    total: int,
    elapsed: float,
    out_dir: Path,
    identifier: str,
    cfg,
    key: bytes | str | None = None,
) -> dict:
    ok = sum(1 for r in rows if r.status == "成功")
    unverified = sum(1 for r in rows if r.status == "已嵌入（未校验）")
    low_red = sum(1 for r in rows if r.status == "已嵌入（冗余不足）")
    bad = sum(1 for r in rows if r.status in ("校验未通过", "失败"))
    psnrs = [r.psnr for r in rows if r.psnr > 0]
    p999 = [r.distortion_p999 for r in rows if r.psnr > 0]
    ratios = [r.usable_ratio for r in rows if r.blocks_used >= 0 and r.psnr > 0]
    retried = sum(1 for r in rows if r.retries > 0)
    return {
        "identifier": identifier,
        "id_hex": _id_hex(identifier, cfg, key),
        "short_code": _short(identifier, cfg, key),
        "total_found": total,
        "processed": len(rows),
        "ok": ok,
        "unverified": unverified,
        "low_redundancy": low_red,
        "failed": bad,
        "skipped": max(0, total - len(rows)),
        "retried": retried,
        "avg_psnr": float(sum(psnrs) / len(psnrs)) if psnrs else 0.0,
        "min_psnr": float(min(psnrs)) if psnrs else 0.0,
        "max_distortion_p999": float(max(p999)) if p999 else 0.0,
        "avg_usable_ratio": float(sum(ratios) / len(ratios)) if ratios else 0.0,
        "delta": cfg.delta,
        "sigma_cap": cfg.sigma_cap,
        "out_dir": str(out_dir),
        "elapsed": elapsed,
    }


# --------------------------------------------------------------------------- 提取


def extract_path(
    path: str | Path, cfg: WatermarkConfig, registry: Registry | None = None, quick: bool = True
) -> ExtractRow:
    path = Path(path)
    try:
        bundle = load_image(path)
        res = extract_bits(bundle.rgb, cfg, registry=registry, quick=quick)
    except Exception as exc:  # noqa: BLE001
        return ExtractRow(src=str(path), status="读取失败", note=str(exc))
    return _row_from_result(path, res)


def extract_many(
    targets: Iterable[str | Path],
    cfg: WatermarkConfig | None = None,
    registry: Registry | None = None,
    progress: Progress | None = None,
    should_stop: StopFlag | None = None,
    quick: bool = True,
) -> list[ExtractRow]:
    """对一批图片/目录做溯源提取。"""
    cfg = cfg or get_profile("标准")
    progress = progress or _noop_progress
    should_stop = should_stop or _never_stop
    files: list[Path] = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            files.extend(discover_images(p, recursive=True))
        elif p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            files.append(p)
    # 去重保序
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in files:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    rows: list[ExtractRow] = []
    total = len(uniq)
    for i, p in enumerate(uniq, start=1):
        if should_stop():
            break
        row = extract_path(p, cfg, registry=registry, quick=quick)
        rows.append(row)
        progress(i, total, f"[{i}/{total}] {p.name} → {row.status} {row.identifier or row.short_code}")
    return rows


# --------------------------------------------------------------------------- 导出


def write_rows_csv(rows: Sequence[object], path: str | Path) -> Path:
    """把 dataclass 行导出为 CSV（UTF-8-BOM，Excel 直接可读）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    dicts = [asdict(r) if hasattr(r, "__dataclass_fields__") else dict(r) for r in rows]
    if not dicts:
        path.write_text("", encoding="utf-8-sig")
        return path
    fields: list[str] = []
    for d in dicts:
        for k in d:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for d in dicts:
            writer.writerow(d)
    return path
