"""命令行入口：``python -m penguinprint`` / ``penguinprint``。

子命令::

    embed     把某个买家标识的指纹批量嵌入整个目录
    extract   对图片/目录做溯源提取（可导出 CSV）
    registry  查看 / 管理本地指纹库
    gui       打开图形界面（等价于 python run_gui.py）
    selftest  快速自检（合成图 → 嵌入 → JPEG → 提取）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import PROFILE_NAMES, effective_db_path, get_profile
from .pipeline import embed_directory, extract_many, write_rows_csv
from .registry import Registry

__all__ = ["main"]


def _progress(done: int, total: int, message: str) -> None:
    bar_len = 28
    frac = 0.0 if total <= 0 else done / total
    filled = int(bar_len * frac)
    bar = "█" * filled + "·" * (bar_len - filled)
    sys.stderr.write(f"\r[{bar}] {done:>4}/{total:<4} {message[:60]:<60}")
    sys.stderr.flush()
    if done >= total:
        sys.stderr.write("\n")


def _registry(args) -> Registry | None:
    if getattr(args, "no_registry", False):
        return None
    return Registry(args.registry)


def cmd_embed(args) -> int:
    cfg = get_profile(args.strength)
    if args.quality:
        cfg = cfg.replace(save_quality=int(args.quality))
    registry = _registry(args)
    rows, summary = embed_directory(
        args.input,
        args.identifier,
        cfg=cfg,
        out_dir=args.output,
        recursive=not args.no_recursive,
        registry=registry,
        progress=None if args.quiet else _progress,
        verify=not args.no_verify,
        batch=getattr(args, "batch", "") or "",
    )
    print(
        f"\n完成：共 {summary['total_found']} 张，成功 {summary['ok']}，"
        f"未校验 {summary['unverified']}，冗余不足 {summary.get('low_redundancy', 0)}，"
        f"失败 {summary['failed']}"
    )
    print(f"买家标识 : {summary['identifier']}")
    print(f"指纹 ID  : {summary['id_hex']}  (短码 {summary['short_code']})")
    if getattr(args, "batch", ""):
        print(f"批次     : {args.batch}")
    if registry is not None:
        print(f"密钥指纹 : {registry.key_fp}（备份指纹库即备份密钥）")
    print(
        f"画质     : 平均 PSNR {summary['avg_psnr']:.1f} dB（最低 {summary['min_psnr']:.1f} dB），"
        f"局部最坏失真(99.9 分位) ≤ {summary['max_distortion_p999']:.0f} 灰阶"
    )
    print(
        f"冗余     : 可用纹理块平均占全图 {summary['avg_usable_ratio'] * 100:.1f}%"
        "（纯色/过曝区域不嵌入，避免可见失真）"
    )
    if summary.get("retried"):
        print(f"注意     : {summary['retried']} 张回读校验失败并自动提档重试（画质会略降）")
    print(f"输出目录 : {summary['out_dir']}")
    print(f"耗时     : {summary['elapsed']:.1f}s")
    if args.csv:
        write_rows_csv(rows, args.csv)
        print(f"明细已导出: {args.csv}")
    for r in rows:
        if r.status not in ("成功", "已嵌入（未校验）"):
            print(f"  ! {Path(r.src).name}: {r.status} {r.note}")
    return 0 if summary["failed"] == 0 else 1


def cmd_extract(args) -> int:
    cfg = get_profile("标准")
    registry = _registry(args)
    rows = extract_many(
        [args.target],
        cfg=cfg,
        registry=registry,
        progress=None if args.quiet else _progress,
    )
    print()
    hits = [r for r in rows if r.status == "成功"]
    for r in rows:
        who = r.identifier or ("库里无此 ID" if r.status == "成功" else "")
        print(
            f"{r.status:<8} {Path(r.src).name:<28} {r.id_hex or '-':<18} "
            f"{r.short_code or '-':<14} {who:<26} 置信度={r.confidence:.2f} "
            f"{'' if r.status == '成功' else r.note[:40]}"
        )
    print(f"\n共 {len(rows)} 张，检出 {len(hits)} 张。")
    if args.csv:
        write_rows_csv(rows, args.csv)
        print(f"明细已导出: {args.csv}")
    return 0 if hits else 1


def cmd_registry(args) -> int:
    reg = Registry(args.registry)
    if args.action == "list":
        rows = reg.list_all()
        if not rows:
            print(f"指纹库为空：{reg.path}")
            return 0
        for r in rows:
            print(
                f"{r['short_code']:<14} {r['identifier']:<28} {r['id_hex']:<18} "
                f"{r['created_at']:<20} 嵌入 {r.get('embeds', 0)} 次  {r.get('note') or ''}"
            )
        print(f"\n共 {len(rows)} 条，库文件：{reg.path}")
    elif args.action == "add":
        # --identifier 在本子命令里是可选参数（list/delete/export 用不到），
        # 但 add 必须有值，这里显式校验。
        identifier = (args.identifier or "").strip()
        if not identifier:
            print("错误：registry add 需要 --identifier", file=sys.stderr)
            return 2
        cfg_add = get_profile(args.strength)
        rec = reg.register(identifier, id_bytes=cfg_add.id_bytes, note=args.note or "")
        print(f"已登记：{rec['identifier']}  短码 {rec['short_code']}  ID {rec['id_hex']}")
    elif args.action == "delete":
        print("已删除" if reg.delete(args.id) else "未找到该 ID")
    elif args.action == "export":
        path = reg.export_csv(args.csv or "fingerprints_export.csv")
        print(f"已导出：{path}")
    elif args.action == "export-embeds":
        path = reg.export_embeds_csv(args.csv or "embeds_export.csv")
        print(f"已导出交付记录：{path}")
    else:  # pragma: no cover
        print("未知操作")
        return 2
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main

    gui_main(registry_path=args.registry)
    return 0


def cmd_selftest(args) -> int:
    """不依赖外部素材的快速自检。"""
    import io

    import numpy as np
    from PIL import Image

    from .embedder import embed_bits
    from .extractor import extract_bits
    from .payload import payload_bits

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
    try:
        import synth  # type: ignore
    except Exception:  # pragma: no cover
        synth = None

    if synth is not None:
        rgb = synth.textured(600, 800, seed=7)
    else:
        # 取不到 synth 素材时自己造一张**有纹理**的图：
        # 纯白噪声的块统计与真实照片差别太大，嵌入后无法回读，会让自检误报失败。
        # 做法是"低分辨率噪声 + 双三次放大 + 高斯平滑 + 轻微色偏"。
        from scipy.ndimage import gaussian_filter, zoom

        rng = np.random.default_rng(7)
        small = rng.normal(0, 1, (45, 60))
        base_img = zoom(small, (600 / 45, 800 / 60), order=3)
        base_img = gaussian_filter(base_img, 1.2)
        lo, hi = float(base_img.min()), float(base_img.max())
        base_img = (base_img - lo) / max(1e-6, hi - lo) * 210.0 + 20.0
        rgb = np.clip(
            np.stack([base_img, base_img * 0.94 + 7, base_img * 0.88 + 14], -1), 0, 255
        ).astype("uint8")

    cfg = get_profile(args.strength)
    identifier = args.identifier
    bits = payload_bits(identifier, cfg.id_bytes, cfg.crc_bytes)
    out, stats = embed_bits(rgb, bits, cfg)
    print(f"强度 {args.strength}（delta={cfg.delta}） PSNR={stats.psnr:.1f}dB "
          f"可用块={stats.blocks_used}/{stats.blocks_total}")

    buf = io.BytesIO()
    Image.fromarray(out).save(buf, format="JPEG", quality=70)
    buf.seek(0)
    jpeg = np.asarray(Image.open(buf).convert("RGB"))

    cases = [
        ("PNG 无损", out),
        ("JPEG q=70", jpeg),
        ("裁剪 50%", jpeg[150:450, 200:600]),
        ("提亮+对比", np.clip(out.astype(np.float32) * 1.15 + 12, 0, 255).astype("uint8")),
    ]
    ok = True
    for name, arr in cases:
        res = extract_bits(arr, cfg, quick=True)
        good = res.found and res.id_hex == __import__(
            "penguinprint.payload", fromlist=["fingerprint_bytes"]
        ).fingerprint_bytes(identifier, cfg.id_bytes).hex().upper()
        ok = ok and good
        print(f"  [{'OK' if good else '失败'}] {name:<12} 置信度={res.confidence:.2f} {res.message[:40]}")
    print("自检通过" if ok else "自检未通过")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="penguinprint",
        description="penguinprint 企鹅指纹 —— 图片指纹溯源系统（抗裁剪 / 抗压缩 / 抗调色）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  python -m penguinprint embed ./photos --identifier buyer@shop.com\n"
        "  python -m penguinprint extract ./photos/_penguinprint_out --csv report.csv\n"
        "  python -m penguinprint registry list\n"
        "  python -m penguinprint gui\n",
    )
    parser.add_argument(
        "--registry", default=str(effective_db_path()),
        help="指纹库路径",
    )
    # 再给每个子命令挂一份 --registry：用 SUPPRESS 默认值，只在显式给出时才覆盖全局值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--registry", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("embed", parents=[common], help="批量嵌入买家指纹")
    p.add_argument("input", help="图片目录")
    p.add_argument("--identifier", required=True, help="买家标识（标识）")
    p.add_argument("-o", "--output", help="输出目录（默认 <输入目录>/_penguinprint_out）")
    p.add_argument("--strength", default="标准", choices=PROFILE_NAMES, help="强度档位")
    p.add_argument("--quality", type=int, default=None, help="JPEG/WebP 输出质量，默认 95")
    p.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    p.add_argument("--no-verify", action="store_true", help="跳过嵌入后的回读校验（更快）")
    p.add_argument("--no-registry", action="store_true", help="不写入指纹库")
    p.add_argument("--batch", default="", help="订单号 / 批次（写进交付记录）")
    p.add_argument("--csv", help="导出逐张明细 CSV")
    p.add_argument("-q", "--quiet", action="store_true", help="不显示进度条")
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser("extract", parents=[common], help="溯源提取")
    p.add_argument("target", help="图片文件或目录")
    p.add_argument("--no-registry", action="store_true")
    p.add_argument("--csv", help="导出结果 CSV")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("registry", parents=[common], help="指纹库管理")
    p.add_argument("action", choices=["list", "add", "delete", "export", "export-embeds"])
    p.add_argument("--identifier", help="add 时必填：买家标识")
    p.add_argument("--note", help="备注")
    p.add_argument("--id", help="delete 时的指纹 ID(hex)")
    p.add_argument("--csv", help="export / export-embeds 的目标文件")
    p.add_argument("--strength", default="标准", choices=PROFILE_NAMES, help="add 时的强度档位")
    p.set_defaults(func=cmd_registry)

    p = sub.add_parser("gui", parents=[common], help="打开图形界面")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("selftest", parents=[common], help="快速自检")
    p.add_argument("--identifier", default="selftest@example.com")
    p.add_argument("--strength", default="标准", choices=PROFILE_NAMES)
    p.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
