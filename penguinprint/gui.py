"""Tkinter 图形界面：目录 + 买家标识 → 一键批量嵌入；可疑图片 → 一键溯源。

设计目标（对应 README 第 3 条）：把"上手成本"压到最低 ——
三个页签、一个标识输入框、一个目录选择框，其余全部有默认值。
所有耗时操作放在工作线程，界面通过队列刷新，不会假死。
"""

from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import (
    DEFAULT_OUTPUT_DIRNAME,
    PROFILE_NAMES,
    effective_db_path,
    get_profile,
    load_settings,
    set_data_dir,
)
from .helptext import HELP_TEXT
from .imgio import IMAGE_EXTS, load_image
from .pipeline import embed_directory, extract_many, write_rows_csv
from .registry import Registry

__all__ = ["main", "PenguinPrintApp", "build_app"]

def _enable_dpi_awareness() -> None:
    """把当前进程设为 **系统 DPI 感知** —— 必须在创建 Tk 根窗口之前调用。

    tkinter 默认不是 DPI 感知，高分屏上由 Windows 拉伸窗口位图显示；
    而弹出系统原生对话框（资源管理器目录/文件选择框）时 Tk 会临时设置 DPI 感知，
    导致窗口与字体当场改变大小并保持到进程结束。

    这里选"系统 DPI 感知"（而不是 Per-Monitor V2）：Tk 8.6 不处理
    WM_DPICHANGED，用 V2 在跨不同缩放比的显示器拖动时容易错位。
    """
    try:
        import ctypes

        try:  # Win8.1+：1 = PROCESS_SYSTEM_DPI_AWARE
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            return
        except Exception:  # noqa: BLE001
            pass
        try:  # Vista+
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 - 非 Windows 或已被设置过：忽略
        pass


def _system_dpi() -> int:
    """系统 DPI（96 = 100%）。失败时按 96 处理。"""
    try:
        import ctypes

        return int(ctypes.windll.user32.GetDpiForSystem())  # Win10 1607+
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes

        hdc = ctypes.windll.user32.GetDC(0)
        dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return dpi or 96
    except Exception:  # noqa: BLE001
        return 96


APP_TITLE = "penguinprint 企鹅指纹"

# 买家标识的建议长度（只提示、不强制）：太短容易撞车，太长不好输入/记
ID_LEN_RANGE = (6, 32)
ID_HINT_IDLE = "#666666"
ID_HINT_OK = "#1a7f37"
ID_HINT_WARN = "#b35c00"


def _asset_path(name: str) -> Path | None:
    """定位随程序分发的素材（图标等）。

    源码运行时在项目根的 ``assets/``；PyInstaller 冻结运行时在解包目录
    ``sys._MEIPASS/assets``（由打包配置的 ``datas`` 带进去）。
    找不到就返回 None —— 图标只是装饰，缺失不影响程序启动。
    """
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / name)
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / name)
    for p in candidates:
        if p.exists():
            return p
    return None


def apply_app_icon(win: tk.Misc) -> None:
    """给一个窗口（主窗或 Toplevel）设置程序图标：标题栏左上角 + 任务栏。

    两种方式都设：``iconbitmap`` 用 .ico（Windows 任务栏/Alt-Tab 效果最好），
    ``iconphoto`` 用 .png（跨平台且支持透明）。任一失败都忽略。
    """
    try:
        ico = _asset_path("penguinprint.ico")
        if ico is not None:
            win.iconbitmap(default=str(ico))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    try:
        png = _asset_path("penguinprint_icon.png")
        if png is not None:
            img = tk.PhotoImage(file=str(png))
            win.iconphoto(True, img)  # type: ignore[attr-defined]
            win._icon_img = img  # 保住引用，否则会被回收  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


class PenguinPrintApp:
    def __init__(self, root: tk.Tk, registry_path: str | Path | None = None):
        self.root = root
        self.root.title(APP_TITLE)
        apply_app_icon(self.root)
        # 按真实 DPI 缩放窗口尺寸：进程已被设为"系统 DPI 感知"（见 _enable_dpi_awareness），
        # 所以这里必须自己乘缩放比，否则高分屏上窗口会显得比过去小一圈。
        scale = self._dpi_scale()
        self.root.geometry(f"{int(1000 * scale)}x{int(740 * scale)}")
        self.root.minsize(int(860 * scale), int(620 * scale))
        self.registry_path = Path(registry_path or effective_db_path())
        self.registry = Registry(self.registry_path)
        self.queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.embed_rows: list = []
        self.extract_rows: list = []
        self.last_out_dir: Path | None = None

        self.var_input = tk.StringVar()
        self.var_output = tk.StringVar()
        self.var_identifier = tk.StringVar()
        self.var_batch = tk.StringVar()          # 订单号 / 批次（可选，只写进交付记录）
        self.var_strength = tk.StringVar(value="标准")
        self.var_recursive = tk.BooleanVar(value=True)
        self.var_verify = tk.BooleanVar(value=True)
        self.var_target = tk.StringVar()
        self.var_status = tk.StringVar(value="就绪")
        self.var_embed_info = tk.StringVar(value="尚未嵌入")
        self.var_extract_info = tk.StringVar(value="尚未提取")
        self.var_registry_info = tk.StringVar(value="")

        self._build_menu()
        self._build_layout()
        self._refresh_registry()
        self.root.after(120, self._poll_queue)

    def _dpi_scale(self) -> float:
        """当前窗口的 DPI 缩放比（100% → 1.0），限制在 1.0~3.0 之间防呆。"""
        try:
            raw = float(self.root.winfo_fpixels("1i")) / 96.0
        except Exception:  # noqa: BLE001
            raw = _system_dpi() / 96.0
        return max(1.0, min(3.0, raw))

    # ------------------------------------------------------------------ 界面
    def _refresh_id_hint(self) -> None:
        """实时显示"买家标识"长度：给出建议范围并提示当前字符数。

        只是提示，不拦提交 —— 买家标识可以是邮箱、手机号、订单号、昵称等任意字符串，
        内部会做"去空白 + 统一小写"归一化，保证同一买家不会算出两个指纹。
        """
        text = str(self.var_identifier.get())
        n = len(text.strip())
        lo, hi = ID_LEN_RANGE
        if not n:
            msg, color = f"用于生成指纹 ID；建议 {lo}~{hi} 个字符", ID_HINT_IDLE
        elif n < lo:
            msg, color = f"当前 {n} 字符，偏短（建议 {lo}~{hi}）", ID_HINT_WARN
        elif n > hi:
            msg, color = f"当前 {n} 字符，偏长（建议 {lo}~{hi}）", ID_HINT_WARN
        else:
            msg, color = f"当前 {n} 字符 ✓（建议 {lo}~{hi}）", ID_HINT_OK
        try:
            self.lbl_id_hint.configure(text=f"（{msg}）", foreground=color)
        except Exception:  # noqa: BLE001 - 控件未建好时忽略
            pass

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="使用说明 / 原理", command=self._show_help)
        helpmenu.add_command(label="打开指纹库所在目录", command=self._open_registry_dir)
        helpmenu.add_separator()
        helpmenu.add_command(label="数据目录…", command=self._choose_data_dir)
        helpmenu.add_command(label="数据目录：恢复默认", command=self._reset_data_dir)
        helpmenu.add_separator()
        helpmenu.add_command(label="退出", command=self.root.destroy)
        menubar.add_cascade(label="帮助", menu=helpmenu)
        self.root.config(menu=menubar)

    def _build_layout(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:  # pragma: no cover - 非 Windows
            pass
        style.configure("Hint.TLabel", foreground="#666666")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.tab_embed = ttk.Frame(nb, padding=10)
        self.tab_extract = ttk.Frame(nb, padding=10)
        self.tab_registry = ttk.Frame(nb, padding=10)
        nb.add(self.tab_embed, text="  嵌入指纹  ")
        nb.add(self.tab_extract, text="  溯源提取  ")
        nb.add(self.tab_registry, text="  指纹库  ")
        self._build_embed_tab()
        self._build_extract_tab()
        self._build_registry_tab()

        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(fill="x")
        ttk.Label(bar, textvariable=self.var_status, style="Hint.TLabel").pack(side="left")
        # 记住这个标签：切换「数据目录」后要立刻刷新显示新的指纹库路径
        self.lbl_db = ttk.Label(
            bar,
            text=f"指纹库：{self.registry_path}   ·   密钥 {self.registry.key_fp}",
            style="Hint.TLabel",
        )
        self.lbl_db.pack(side="right")

    # ---------------------------------------------------------------- 嵌入页
    def _build_embed_tab(self) -> None:
        f = self.tab_embed
        f.columnconfigure(1, weight=1)
        row = 0
        ttk.Label(f, text="图片或目录：").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.var_input).grid(row=row, column=1, sticky="ew", padx=4)
        ttk.Button(f, text="选择图片…", command=self._pick_input_file).grid(row=row, column=2)
        ttk.Button(f, text="选择目录…", command=self._pick_input).grid(row=row, column=3, padx=4)
        row += 1
        ttk.Label(f, text="输出目录：").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.var_output).grid(row=row, column=1, sticky="ew", padx=4)
        ttk.Button(f, text="浏览…", command=self._pick_output).grid(row=row, column=2)
        row += 1
        ttk.Label(
            f,
            text=(
                f"（留空 = 图片所在目录下的 {DEFAULT_OUTPUT_DIRNAME}；"
                "原图不会被覆盖；选单张图片时输出同名文件）"
            ),
            style="Hint.TLabel",
        ).grid(row=row, column=1, sticky="w", padx=4)
        row += 1
        ttk.Label(f, text="买家标识：").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.var_identifier).grid(row=row, column=1, sticky="ew", padx=4)
        # 提示里给出建议长度，并实时显示当前字符数（超出建议范围时变色）
        self.lbl_id_hint = ttk.Label(f, text="", style="Hint.TLabel")
        self.lbl_id_hint.grid(row=row, column=2, sticky="w")
        self.var_identifier.trace_add("write", lambda *_a: self._refresh_id_hint())
        self._refresh_id_hint()
        row += 1
        ttk.Label(f, text="订单号 / 批次：").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.var_batch).grid(row=row, column=1, sticky="ew", padx=4)
        ttk.Label(
            f,
            text="（可选。填了就记进交付记录，便于区分同一买家的不同批次）",
            style="Hint.TLabel",
        ).grid(row=row, column=2, columnspan=2, sticky="w")
        row += 1
        opts = ttk.Frame(f)
        opts.grid(row=row, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(opts, text="强度：").pack(side="left")
        for name in PROFILE_NAMES:
            ttk.Radiobutton(opts, text=name, value=name, variable=self.var_strength).pack(side="left")
        ttk.Checkbutton(opts, text="包含子目录", variable=self.var_recursive).pack(side="left", padx=12)
        ttk.Checkbutton(opts, text="回读校验（推荐）", variable=self.var_verify).pack(side="left")
        row += 1
        btns = ttk.Frame(f)
        btns.grid(row=row, column=0, columnspan=3, sticky="w", pady=4)
        self.btn_embed = ttk.Button(btns, text="开始嵌入", command=self.start_embed)
        self.btn_embed.pack(side="left")
        ttk.Button(btns, text="停止", command=self.request_stop).pack(side="left", padx=6)
        ttk.Button(btns, text="打开输出目录", command=self._open_output_dir).pack(side="left", padx=6)
        ttk.Button(btns, text="导出明细 CSV", command=self._export_embed_csv).pack(side="left", padx=6)
        row += 1
        self.pb_embed = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.pb_embed.grid(row=row, column=0, columnspan=3, sticky="ew", pady=6)
        row += 1
        ttk.Label(f, textvariable=self.var_embed_info).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.log_embed = self._make_log(f, row)
        f.rowconfigure(row, weight=1)

    # ---------------------------------------------------------------- 提取页
    def _build_extract_tab(self) -> None:
        f = self.tab_extract
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="目标：").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.var_target).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(f, text="选择文件…", command=self._pick_target_file).grid(row=0, column=2)
        ttk.Button(f, text="选择目录…", command=self._pick_target_dir).grid(row=0, column=3, padx=4)
        btns = ttk.Frame(f)
        btns.grid(row=1, column=0, columnspan=4, sticky="w", pady=4)
        self.btn_extract = ttk.Button(btns, text="开始溯源", command=self.start_extract)
        self.btn_extract.pack(side="left")
        ttk.Button(btns, text="停止", command=self.request_stop).pack(side="left", padx=6)
        ttk.Button(btns, text="导出结果 CSV", command=self._export_extract_csv).pack(side="left", padx=6)
        ttk.Button(btns, text="打开图片所在目录", command=self._open_target_dir).pack(side="left")
        self.pb_extract = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.pb_extract.grid(row=2, column=0, columnspan=4, sticky="ew", pady=6)
        ttk.Label(f, textvariable=self.var_extract_info).grid(row=3, column=0, columnspan=4, sticky="w")

        cols = ("file", "status", "id", "code", "identifier", "conf", "note")
        heads = ("文件", "状态", "指纹 ID", "短码", "买家标识", "置信度", "说明")
        widths = (200, 70, 150, 110, 200, 60, 240)
        self.tree_extract = ttk.Treeview(f, columns=cols, show="headings", height=14)
        for c, h, w in zip(cols, heads, widths):
            self.tree_extract.heading(c, text=h)
            self.tree_extract.column(c, width=w, anchor="w")
        self.tree_extract.grid(row=4, column=0, columnspan=4, sticky="nsew", pady=(6, 0))
        vsb = ttk.Scrollbar(f, orient="vertical", command=self.tree_extract.yview)
        vsb.grid(row=4, column=4, sticky="ns")
        self.tree_extract.configure(yscrollcommand=vsb.set)
        self.tree_extract.tag_configure("hit", background="#e7f6e7")
        self.tree_extract.tag_configure("miss", background="#fdf1e0")
        f.rowconfigure(4, weight=1)
        self.tree_extract.bind("<Double-1>", self._on_extract_double_click)

    # -------------------------------------------------------------- 指纹库页
    def _build_registry_tab(self) -> None:
        f = self.tab_registry
        btns = ttk.Frame(f)
        btns.grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Button(btns, text="刷新", command=self._refresh_registry).pack(side="left")
        ttk.Button(btns, text="手动登记标识", command=self._add_registry).pack(side="left", padx=6)
        ttk.Button(btns, text="导出 CSV", command=self._export_registry).pack(side="left", padx=6)
        ttk.Button(btns, text="导出交付记录", command=self._export_embeds_csv).pack(side="left", padx=6)
        ttk.Button(btns, text="删除选中", command=self._delete_registry).pack(side="left", padx=6)
        ttk.Button(btns, text="查看嵌入记录", command=self._show_embeds).pack(side="left", padx=6)
        ttk.Label(f, textvariable=self.var_registry_info).grid(row=1, column=0, sticky="w")

        cols = ("code", "identifier", "id", "created", "embeds", "key", "note")
        heads = ("短码", "买家标识", "指纹 ID", "登记时间", "嵌入张数", "密钥", "备注")
        widths = (110, 200, 170, 150, 80, 80, 180)
        self.tree_registry = ttk.Treeview(f, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            self.tree_registry.heading(c, text=h)
            self.tree_registry.column(c, width=w, anchor="w")
        self.tree_registry.grid(row=2, column=0, sticky="nsew", pady=6)
        vsb = ttk.Scrollbar(f, orient="vertical", command=self.tree_registry.yview)
        vsb.grid(row=2, column=1, sticky="ns")
        self.tree_registry.configure(yscrollcommand=vsb.set)
        f.rowconfigure(2, weight=1)
        f.columnconfigure(0, weight=1)

    def _make_log(self, parent: ttk.Frame, row: int) -> tk.Text:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        # wrap="word"：日志里常有很长的路径与参数，按词换行、超长单词自动断行，
        # 避免被右侧裁掉而看不全。
        text = tk.Text(frame, height=12, wrap="word", font=("Consolas", 9))
        text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        sb.pack(side="right", fill="y")
        text.configure(yscrollcommand=sb.set, state="disabled")
        return text

    # ------------------------------------------------------------------ 工具
    def _log(self, text: str) -> None:
        widget = self.log_embed
        widget.configure(state="normal")
        widget.insert("end", text + "\n")
        widget.see("end")
        widget.configure(state="disabled")

    def _pick_input_file(self) -> None:
        """选择单张图片（嵌入页）—— 不想整目录处理时用。"""
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))
        path = filedialog.askopenfilename(
            title="选择图片", filetypes=[("图片", exts), ("全部", "*.*")]
        )
        if path:
            self.var_input.set(path)

    def _pick_input(self) -> None:
        path = filedialog.askdirectory(title="选择待嵌入的图片目录")
        if path:
            self.var_input.set(path)
            if not self.var_output.get():
                self.var_output.set(str(Path(path) / DEFAULT_OUTPUT_DIRNAME))

    def _pick_output(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.var_output.set(path)

    def _pick_target_file(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))
        path = filedialog.askopenfilename(title="选择图片", filetypes=[("图片", exts), ("全部", "*.*")])
        if path:
            self.var_target.set(path)

    def _pick_target_dir(self) -> None:
        path = filedialog.askdirectory(title="选择图片目录")
        if path:
            self.var_target.set(path)

    # ------------------------------------------------------------ 打开目录 / 定位文件
    #
    # 打开目录优先交给系统（资源管理器）；同时自带一个**应用内浏览窗口**作为兜底：
    # 某些受限环境里系统打开会静默失败（调用成功却没有窗口），此时仍能在程序内浏览文件。

    def _try_shell_open(self, target: str, is_dir: bool) -> bool:
        """用系统默认方式打开目标（目录交给资源管理器，文件则打开所在目录并选中）。

        返回值语义：调用本身没抛异常即视为已交给系统；只有抛异常才返回 False，
        调用方再退回应用内浏览窗口 —— 两条路互斥，不会同时出现两个窗口。
        """
        try:
            if sys.platform.startswith("win"):
                if is_dir:
                    # 目录：交给资源管理器（可能复用已有窗口，这是系统自身行为）
                    os.startfile(target)
                else:
                    # 文件：打开所在目录并选中该文件
                    subprocess.Popen(["explorer.exe", f"/select,{target}"])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
            return True
        except Exception:  # noqa: BLE001 - 打不开才退回应用内浏览
            return False

    def _open_path(self, path: str | Path) -> None:
        """打开目录/定位文件：只用系统资源管理器打开一次；系统打不开才在窗口内浏览。"""
        raw = str(path).strip().strip('"')
        if not raw:
            return
        # normpath 统一正/反斜杠；abspath 补全相对路径（explorer 的相对路径基准不是本进程）
        target = os.path.abspath(os.path.normpath(raw))
        if not os.path.exists(target):
            self.var_status.set(f"路径不存在：{target}")
            messagebox.showinfo(
                APP_TITLE, f"路径不存在：\n{target}\n\n请检查「目标 / 输出目录」是否填对。"
            )
            return
        is_dir = os.path.isdir(target)
        if self._try_shell_open(target, is_dir):
            self.var_status.set(f"已用资源管理器打开：{target}")
            return
        # 只有在系统打开抛异常时才走这里（两条路互斥，不会同时开两个窗口）
        self.var_status.set(f"已在本窗口内浏览：{target}（系统打开失败）")
        self._open_folder_window(target if is_dir else os.path.dirname(target), focus=target)

    def _open_folder_window(self, folder: str, focus: str | None = None) -> None:
        """应用内文件浏览窗口（纯列表）：不依赖任何外部程序，必然可用。"""
        win = tk.Toplevel(self.root)
        apply_app_icon(win)
        win.title(f"浏览：{folder}")
        win.geometry("760x460")
        try:
            win.transient(self.root)
        except Exception:  # noqa: BLE001
            pass

        top = ttk.Frame(win, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="目录：", font=("Microsoft YaHei UI", 9)).pack(side="left")
        var_dir = tk.StringVar(value=folder)
        entry = ttk.Entry(top, textvariable=var_dir, font=("Consolas", 9))
        entry.pack(side="left", fill="x", expand=True, padx=4)

        body = ttk.Frame(win, padding=(10, 0))
        body.pack(fill="both", expand=True)
        cols = ("name", "size", "time")
        tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="browse")
        tree.heading("name", text="名称")
        tree.heading("size", text="大小")
        tree.heading("time", text="修改时间")
        tree.column("name", width=380, anchor="w")
        tree.column("size", width=90, anchor="e")
        tree.column("time", width=150, anchor="center")
        vsb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        info = tk.StringVar(value="双击图片用系统默认程序打开；选中后也可用下方按钮。")
        ttk.Label(win, textvariable=info, padding=(10, 4), font=("Microsoft YaHei UI", 9)).pack(
            anchor="w"
        )

        state: dict[str, object] = {"folder": folder, "items": []}

        def load(path: str) -> None:
            p = Path(os.path.abspath(os.path.normpath(path)))
            if not p.is_dir():
                info.set(f"不是目录：{p}")
                return
            state["folder"] = str(p)
            var_dir.set(str(p))
            try:
                win.title(f"浏览：{p}")
            except Exception:  # noqa: BLE001
                pass
            tree.delete(*tree.get_children())
            items: list[tuple[str, str, str, str]] = []
            if p.parent != p:
                items.append(("..", "", "<上一级>", str(p.parent)))
            try:
                entries = sorted(
                    (e for e in os.scandir(p) if e.is_dir() or Path(e.name).suffix.lower() in IMAGE_EXTS),
                    key=lambda e: (not e.is_dir(), e.name.lower()),
                )
            except OSError as exc:
                info.set(f"无法读取目录：{exc}")
                return
            for e in entries:
                try:
                    st = e.stat()
                    size = "<目录>" if e.is_dir() else f"{st.st_size / 1024:.0f} KB"
                    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                except OSError:
                    size, ts = "?", "?"
                items.append(("[" + e.name + "]" if e.is_dir() else e.name, size, ts, e.path))
            state["items"] = items
            for i, (name, size, ts, _full) in enumerate(items):
                tree.insert("", "end", iid=str(i), values=(name, size, ts))
            info.set(f"{p}  共 {max(0, len(items) - 1)} 项" + (f"（已定位 {Path(focus).name}）" if focus else ""))
            if focus:
                for i, (_n, _s, _t, full) in enumerate(items):
                    if os.path.normcase(full) == os.path.normcase(focus):
                        tree.selection_set(str(i))
                        tree.see(str(i))
                        break
            elif items:
                tree.selection_set("0")

        def current() -> str | None:
            sel = tree.selection()
            if not sel:
                return None
            try:
                return str(state["items"][int(sel[0])][3])  # type: ignore[index]
            except Exception:  # noqa: BLE001
                return None

        def activate(_event=None) -> None:
            path = current()
            if not path:
                return
            if os.path.isdir(path):
                load(path)
            else:
                self._open_file_with_system(path)

        btns = ttk.Frame(win, padding=(10, 6))
        btns.pack(fill="x")
        ttk.Button(btns, text="打开所选", command=activate).pack(side="left")
        ttk.Button(btns, text="上一级", command=lambda: load(str(Path(str(state["folder"])).parent))).pack(
            side="left", padx=6
        )
        ttk.Button(btns, text="刷新", command=lambda: load(str(state["folder"]))).pack(side="left")
        ttk.Button(btns, text="转到路径", command=lambda: load(var_dir.get())).pack(side="left", padx=6)
        ttk.Button(
            btns,
            text="复制路径",
            command=lambda: self._copy_to_clipboard(current() or str(state["folder"])),
        ).pack(side="left")
        def shell_open_current() -> None:
            path = current() or str(state["folder"])
            if not self._try_shell_open(path, os.path.isdir(path)):
                self.var_status.set("系统资源管理器无法启动，请直接用本窗口浏览 / 复制路径")

        ttk.Button(
            btns, text="在资源管理器中打开", command=shell_open_current
        ).pack(side="right")
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="right", padx=6)
        tree.bind("<Double-1>", activate)
        tree.bind("<Return>", activate)
        load(folder)

    def _copy_to_clipboard(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.var_status.set(f"已复制路径：{text}")
        except Exception as exc:  # noqa: BLE001
            self.var_status.set(f"复制失败：{exc}")

    def _open_file_with_system(self, path: str) -> None:
        """用系统默认程序打开一个文件；打不开就如实提示（不谎报成功）。"""
        if sys.platform.startswith("win"):
            try:
                ctypes.windll.shell32.ShellExecuteW(None, "open", path, None, None, 1)
            except Exception as exc:  # noqa: BLE001
                self.var_status.set(f"无法启动系统程序：{exc}")
                self._copy_to_clipboard(path)
                return
            self.var_status.set(f"已请求系统打开：{path}")
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            self.var_status.set(f"已请求系统打开：{path}")
        except Exception as exc:  # noqa: BLE001
            self.var_status.set(f"无法启动系统程序：{exc}")

    def _open_output_dir(self) -> None:
        """打开输出目录；没设置就按默认规则推导（输入目录下的 _penguinprint_out）。"""
        candidates: list[Path] = []
        if self.last_out_dir:
            candidates.append(Path(self.last_out_dir))
        if self.var_output.get().strip():
            candidates.append(Path(self.var_output.get().strip()))
        if self.var_input.get().strip():
            _in = Path(self.var_input.get().strip())
            candidates.append((_in.parent if _in.is_file() else _in) / DEFAULT_OUTPUT_DIRNAME)
        for path in candidates:
            if path.exists():
                self._open_path(path)
                return
        messagebox.showinfo(
            APP_TITLE,
            "还没有可打开的输出目录。\n\n先选好「图片或目录」并点一次「开始嵌入」，"
            f"输出会放在 <图片所在目录>\\{DEFAULT_OUTPUT_DIRNAME} 下。",
        )

    def _open_target_dir(self) -> None:
        target = self.var_target.get().strip()
        if not target:
            messagebox.showinfo(APP_TITLE, "请先在「目标」里选择图片或目录")
            return
        p = Path(target)
        self._open_path(p if p.is_dir() else p.parent)

    def _open_registry_dir(self) -> None:
        self._open_path(self.registry_path.parent)

    def _show_help(self) -> None:
        win = tk.Toplevel(self.root)
        apply_app_icon(win)
        win.title("使用说明 / 原理")
        win.geometry("760x560")
        text = tk.Text(win, wrap="word", font=("Microsoft YaHei UI", 10))
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")

    # ------------------------------------------------------------------ 任务
    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def request_stop(self) -> None:
        if self._busy():
            self.stop_event.set()
            self.var_status.set("正在停止…")

    def _start_worker(self, fn) -> None:
        if self._busy():
            messagebox.showinfo(APP_TITLE, "已有任务在运行，请等待完成或点击停止")
            return
        self.stop_event.clear()
        self.worker = threading.Thread(target=self._worker_main, args=(fn,), daemon=True)
        self.worker.start()

    def _worker_main(self, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("idle", None))

    def _progress(self, done: int, total: int, message: str) -> None:
        self.queue.put(("progress", done, total, message))

    def _stop_flag(self) -> bool:
        return self.stop_event.is_set()

    # -------------------------------------------------------------- 嵌入任务
    def start_embed(self) -> None:
        raw_input = self.var_input.get().strip()
        target = Path(raw_input) if raw_input else None
        identifier = self.var_identifier.get().strip()
        if target is None or not target.exists():
            messagebox.showwarning(APP_TITLE, "请选择有效的图片文件或目录")
            return
        input_dir = raw_input
        if not identifier:
            messagebox.showwarning(APP_TITLE, "请填写买家标识（用于生成指纹 ID）")
            return
        if len(identifier) > 200:
            messagebox.showwarning(
                APP_TITLE, f"买家标识过长（{len(identifier)} 字符，最多 200 个字符）"
            )
            return
        out_dir = self.var_output.get().strip() or None
        cfg = get_profile(self.var_strength.get())
        recursive = bool(self.var_recursive.get())
        verify = bool(self.var_verify.get())
        self.btn_embed.configure(state="disabled")
        self.pb_embed.configure(value=0)
        kind = "单张图片" if target.is_file() else "整个目录"
        self._log(f"=== 开始嵌入（{kind}）：{input_dir} → {out_dir or DEFAULT_OUTPUT_DIRNAME} ===")
        self._log(f"    买家 {identifier}，强度 {self.var_strength.get()}（delta={cfg.delta}）")
        _b = self.var_batch.get().strip()
        self._log(f"    批次：{_b or '（未填）'}｜密钥指纹：{self.registry.key_fp}")

        def task() -> None:
            # pipeline.embed_directory 现在同时支持"目录"和"单张图片"
            rows, summary = embed_directory(
                input_dir,
                identifier,
                cfg=cfg,
                out_dir=out_dir,
                recursive=recursive,
                registry=self.registry,
                progress=self._progress,
                should_stop=self._stop_flag,
                verify=verify,
                batch=self.var_batch.get().strip(),
            )
            self.queue.put(("embed_done", rows, summary))

        self._start_worker(task)

    # -------------------------------------------------------------- 提取任务
    def start_extract(self) -> None:
        target = self.var_target.get().strip()
        if not target or not Path(target).exists():
            messagebox.showwarning(APP_TITLE, "请选择要溯源的图片或目录")
            return
        cfg = get_profile("标准")
        self.btn_extract.configure(state="disabled")
        self.pb_extract.configure(value=0)
        for item in self.tree_extract.get_children():
            self.tree_extract.delete(item)
        self.var_extract_info.set("提取中…")

        def task() -> None:
            rows = extract_many(
                [target],
                cfg=cfg,
                registry=self.registry,
                progress=self._progress,
                should_stop=self._stop_flag,
            )
            self.queue.put(("extract_done", rows, None))

        self._start_worker(task)

    # ---------------------------------------------------------------- 队列泵
    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.queue.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "progress":
            _, done, total, text = msg
            self.var_status.set(text)
            pct = 0 if total <= 0 else done * 100 / total
            self.pb_embed.configure(value=pct)
            self.pb_extract.configure(value=pct)
            self._log(f"[{done}/{total}] {text}")
        elif kind == "error":
            self.var_status.set("出错")
            messagebox.showerror(APP_TITLE, msg[1])
            self._log(f"!! {msg[1]}")
        elif kind == "embed_done":
            self._on_embed_done(msg[1], msg[2])
        elif kind == "extract_done":
            self._on_extract_done(msg[1])
        elif kind == "idle":
            self.btn_embed.configure(state="normal")
            self.btn_extract.configure(state="normal")

    def _on_embed_done(self, rows, summary) -> None:
        self.embed_rows = rows
        self.last_out_dir = Path(summary["out_dir"])
        self.var_embed_info.set(
            f"完成：成功 {summary['ok']} / 共 {summary['processed']} 张，"
            f"平均 PSNR {summary['avg_psnr']:.1f}dB（最坏失真 ≤{summary['max_distortion_p999']:.0f} 灰阶），"
            f"可用块占比 {summary['avg_usable_ratio'] * 100:.0f}%，指纹 ID {summary['id_hex']}"
        )
        self.var_status.set("嵌入完成")
        self._log(
            f"=== 完成：成功 {summary['ok']}，未校验 {summary['unverified']}，"
            f"冗余不足 {summary.get('low_redundancy', 0)}，"
            f"失败 {summary['failed']}，耗时 {summary['elapsed']:.1f}s ==="
        )
        for r in rows:
            if r.status == "成功":
                continue
            self._log(f"  [{r.status}] {Path(r.src).name}  {r.note}")
        self._refresh_registry()
        messagebox.showinfo(
            APP_TITLE,
            f"嵌入完成！\n\n成功 {summary['ok']} / {summary['processed']} 张\n"
            f"指纹 ID：{summary['id_hex']}\n短码：{summary['short_code']}\n"
            f"平均 PSNR：{summary['avg_psnr']:.1f} dB"
            f"（局部最坏失真 ≤ {summary['max_distortion_p999']:.0f} 灰阶）\n"
            f"可用纹理块占比：{summary['avg_usable_ratio'] * 100:.0f}%\n"
            f"输出目录：{summary['out_dir']}",
        )

    def _on_extract_done(self, rows) -> None:
        self.extract_rows = rows
        hits = 0
        for r in rows:
            hit = r.status == "成功"
            hits += hit
            self.tree_extract.insert(
                "",
                "end",
                values=(
                    Path(r.src).name,
                    r.status,
                    r.id_hex or "-",
                    r.short_code or "-",
                    r.identifier or ("（库中无此 ID）" if hit else "-"),
                    f"{r.confidence:.2f}" if hit else "-",
                    r.note,
                ),
                tags=("hit" if hit else "miss",),
            )
        mails = sorted({r.identifier for r in rows if r.identifier})
        self.var_extract_info.set(
            f"共 {len(rows)} 张，检出 {hits} 张"
            + (f"，买家：{'、'.join(mails)}" if mails else "")
        )
        self.var_status.set("溯源完成")
        if hits:
            messagebox.showinfo(
                APP_TITLE,
                f"检出 {hits} 张带指纹图片\n\n买家：" + ("、".join(mails) if mails else "库里没有对应记录"),
            )
        else:
            messagebox.showwarning(APP_TITLE, "未检出有效指纹（可能被缩放或严重破坏）")

    # ------------------------------------------------------------ 指纹库操作
    def _refresh_registry(self) -> None:
        for item in self.tree_registry.get_children():
            self.tree_registry.delete(item)
        rows = self.registry.list_all()
        for r in rows:
            self.tree_registry.insert(
                "",
                "end",
                iid=r["id_hex"],
                values=(
                    r["short_code"],
                    r["identifier"],
                    r["id_hex"],
                    r["created_at"],
                    r.get("embeds", 0),
                    r.get("key_fp") or self.registry.key_fp,
                    r.get("note") or "",
                ),
            )
        stats = self.registry.stats()
        self.var_registry_info.set(
            f"共 {stats['fingerprints']} 个买家指纹，累计嵌入记录 {stats['embeds']} 条"
            f"（其中回读校验通过 {stats['verified']} 条）"
            f"　｜　当前密钥 {self.registry.key_fp}"
            f"（备份指纹库即备份密钥；换库会让同一标识算出不同 ID）"
        )

    def _reset_data_dir(self) -> None:
        """把数据目录恢复为默认（用户配置目录，不可写时为程序所在目录）。"""
        from .config import DATA_DIR, set_data_dir

        set_data_dir(None)
        new_db = DATA_DIR / "fingerprints.db"
        old_db = self.registry_path
        try:
            self.registry_path = new_db
            self.registry = Registry(self.registry_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"恢复默认数据目录失败：\n{exc}")
            return
        self._refresh_registry()
        try:
            self.lbl_db.configure(
                text=f"指纹库：{self.registry_path}   ·   密钥 {self.registry.key_fp}"
            )
        except Exception:  # noqa: BLE001
            pass
        messagebox.showinfo(
            APP_TITLE,
            f"已恢复默认数据目录：\n{self.registry_path}\n"
            f"（刚才用的库仍在：{old_db}，未删除、未合并）",
        )

    def _choose_data_dir(self) -> None:
        """选择"数据目录"：指纹库放在哪里（立即生效，下次启动沿用）。

        单文件版启动时会先在**系统临时目录**（`%TEMP%`）解包出 _MEIxxxx，
        这一步由打包时的 runtime_tmpdir 决定，程序内部改不了；文件夹版没有这个临时文件夹。
        """
        cur = self.registry_path.parent
        chosen = filedialog.askdirectory(
            parent=self.root, title="选择数据存放目录（指纹库将放在这里）", initialdir=str(cur)
        )
        if not chosen:
            return
        target = Path(chosen).resolve()   # 存绝对路径：换工作目录也不会失效
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".penguinprint_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"该目录不可写：\n{target}\n\n{exc}")
            return

        new_db = target / "fingerprints.db"
        if new_db.exists():
            ok = messagebox.askyesno(
                APP_TITLE,
                f"该目录里已经有指纹库：\n{new_db}\n\n改用它吗？（不会合并，也不会删除原来的库）",
            )
            if not ok:
                return
        old_db = self.registry_path
        try:
            set_data_dir(target)
            self.registry_path = new_db
            self.registry = Registry(self.registry_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"切换数据目录失败：\n{exc}")
            return
        self._refresh_registry()
        try:
            self.lbl_db.configure(text=f"指纹库：{self.registry_path}")
        except Exception:  # noqa: BLE001
            pass
        # 设置若写不进去（权限/杀毒拦截），本次仍生效但下次启动会回默认
        saved = load_settings().get("data_dir") == str(target)
        extra = (
            ""
            if saved
            else "\n\n⚠ 设置无法写入用户配置目录，本次运行有效，但**下次启动会恢复默认**。"
        )
        messagebox.showinfo(
            APP_TITLE,
            "数据目录已切换，立即生效并在下次启动沿用：\n"
            f"新指纹库：{self.registry_path}\n"
            f"（原指纹库仍在：{old_db}，未删除、未合并）"
            f"{extra}\n\n"
            "说明：单文件版运行时会在**系统临时目录**（`%TEMP%`）解包一个 _MEIxxxx 文件夹，"
            "正常关闭后自动删除；文件夹版不会产生临时文件夹。",
        )

    def _add_registry(self) -> None:
        win = tk.Toplevel(self.root)
        apply_app_icon(win)
        win.title("手动登记买家")
        win.geometry("420x160")
        ttk.Label(win, text="买家标识：").pack(anchor="w", padx=12, pady=(14, 2))
        identifier_var = tk.StringVar()
        ttk.Entry(win, textvariable=identifier_var, width=44).pack(padx=12)
        ttk.Label(win, text="备注（可选）：").pack(anchor="w", padx=12, pady=(8, 2))
        note_var = tk.StringVar()
        ttk.Entry(win, textvariable=note_var, width=44).pack(padx=12)

        def save() -> None:
            try:
                # 必须与嵌入时使用同一个 id_bytes，否则手动登记的 ID 与图上嵌入的 ID 对不上。
                cfg = get_profile(self.var_strength.get())
                rec = self.registry.register(
                    identifier_var.get(), id_bytes=cfg.id_bytes, note=note_var.get()
                )
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(APP_TITLE, str(exc))
                return
            self._refresh_registry()
            win.destroy()
            messagebox.showinfo(APP_TITLE, f"已登记：{rec['identifier']}\n短码 {rec['short_code']}")

        ttk.Button(win, text="保存", command=save).pack(pady=10)

    def _selected_registry_id(self) -> str | None:
        sel = self.tree_registry.selection()
        return sel[0] if sel else None

    def _delete_registry(self) -> None:
        id_hex = self._selected_registry_id()
        if not id_hex:
            messagebox.showinfo(APP_TITLE, "请先在列表中选择一条记录")
            return
        if messagebox.askyesno(APP_TITLE, f"确定删除指纹 {id_hex} ？\n（已嵌入的图片不受影响）"):
            self.registry.delete(id_hex)
            self._refresh_registry()

    def _show_embeds(self) -> None:
        id_hex = self._selected_registry_id()
        if not id_hex:
            messagebox.showinfo(APP_TITLE, "请先在列表中选择一条记录")
            return
        rec = self.registry.get(id_hex) or {}
        rows = self.registry.embeds_for(id_hex)
        win = tk.Toplevel(self.root)
        apply_app_icon(win)
        win.title(f"嵌入记录 - {rec.get('identifier', id_hex)}")
        win.geometry("1020x440")
        ttk.Label(
            win,
            text=(
                f"买家：{rec.get('identifier', '')}   指纹 ID：{id_hex}"
                f"   密钥：{rec.get('key_fp') or self.registry.key_fp}"
            ),
        ).pack(anchor="w", padx=10, pady=(6, 0))
        ttk.Label(
            win,
            text=(
                "哈希为 SHA-256（此处只显示前 12 位，完整值在库里与 CSV 导出中）；"
                "批次为嵌入时填的订单号/批次，未填则为空。"
            ),
            style="Hint.TLabel",
        ).pack(anchor="w", padx=10, pady=(0, 6))
        cols = ("time", "batch", "src", "dst", "shash", "dhash", "psnr", "ok")
        heads = ("时间", "批次", "源文件", "输出文件", "源图哈希", "输出图哈希", "PSNR", "校验")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        for c, h in zip(cols, heads):
            tree.heading(c, text=h)
            tree.column(c, width=120, anchor="w")
        tree.column("time", width=140)
        tree.column("batch", width=110)
        tree.column("shash", width=110)
        tree.column("dhash", width=110)
        tree.column("psnr", width=60)
        tree.column("ok", width=50)
        tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for r in rows:
            tree.insert(
                "",
                "end",
                values=(
                    r["embedded_at"],
                    r.get("batch") or "—",
                    Path(r["src_path"]).name,
                    Path(r["dst_path"]).name,
                    (r.get("src_sha256") or "—")[:12],
                    (r.get("dst_sha256") or "—")[:12],
                    f"{r['psnr']:.1f}" if r["psnr"] else "-",
                    "通过" if r["verified"] else "—",
                ),
            )

    def _export_registry(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出指纹库", defaultextension=".csv", initialfile="fingerprints.csv"
        )
        if path:
            self.registry.export_csv(path)
            messagebox.showinfo(APP_TITLE, f"已导出：{path}")

    def _export_embeds_csv(self) -> None:
        """导出全部交付记录（含批次号、时间、源图/输出图 SHA-256）。"""
        if not self.registry.embed_count():
            messagebox.showinfo(APP_TITLE, "还没有交付记录可导出")
            return
        path = filedialog.asksaveasfilename(
            title="导出交付记录", defaultextension=".csv", initialfile="embeds.csv"
        )
        if path:
            self.registry.export_embeds_csv(path)
            messagebox.showinfo(APP_TITLE, f"已导出：{path}")

    def _export_embed_csv(self) -> None:
        if not self.embed_rows:
            messagebox.showinfo(APP_TITLE, "还没有嵌入结果可导出")
            return
        path = filedialog.asksaveasfilename(
            title="导出嵌入明细", defaultextension=".csv", initialfile="embed_report.csv"
        )
        if path:
            write_rows_csv(self.embed_rows, path)
            messagebox.showinfo(APP_TITLE, f"已导出：{path}")

    def _export_extract_csv(self) -> None:
        if not self.extract_rows:
            messagebox.showinfo(APP_TITLE, "还没有提取结果可导出")
            return
        path = filedialog.asksaveasfilename(
            title="导出溯源结果", defaultextension=".csv", initialfile="trace_report.csv"
        )
        if path:
            write_rows_csv(self.extract_rows, path)
            messagebox.showinfo(APP_TITLE, f"已导出：{path}")

    def _on_extract_double_click(self, _event) -> None:
        sel = self.tree_extract.selection()
        if not sel:
            return
        name = self.tree_extract.item(sel[0], "values")[0]
        for r in self.extract_rows:
            if Path(r.src).name == name:
                try:
                    bundle = load_image(r.src)
                except Exception as exc:  # noqa: BLE001
                    messagebox.showerror(APP_TITLE, str(exc))
                    return
                self._show_preview(bundle, r)
                return

    def _show_preview(self, bundle, row) -> None:
        from PIL import ImageTk

        win = tk.Toplevel(self.root)
        apply_app_icon(win)
        win.title(f"溯源详情 - {Path(row.src).name}")
        img = bundle.to_pil()
        img.thumbnail((520, 520))
        photo = ImageTk.PhotoImage(img)
        label = ttk.Label(win, image=photo)
        label.image = photo  # type: ignore[attr-defined]
        label.pack(padx=10, pady=10)
        info = (
            f"文件：{row.src}\n状态：{row.status}\n指纹 ID：{row.id_hex or '-'}\n"
            f"短码：{row.short_code or '-'}\n买家标识：{row.identifier or '（指纹库中无此 ID）'}\n"
            f"置信度：{row.confidence:.2f}    可用块：{row.blocks}    "
            f"覆盖率：{row.coverage * 100:.0f}%    相位：{row.phase or '-'}\n"
            f"说明：{row.note}"
        )
        ttk.Label(win, text=info, justify="left").pack(anchor="w", padx=10, pady=(0, 10))


def build_app(registry_path: str | Path | None = None) -> tuple[tk.Tk, PenguinPrintApp]:
    """创建（但不进入事件循环）主窗口，便于自动化测试。"""
    # 必须在 tk.Tk() 之前：否则打开系统对话框时 Tk 才临时设置 DPI 感知，界面会当场缩放
    _enable_dpi_awareness()
    root = tk.Tk()
    try:
        # 让字体按"像素/点"正确换算：控件里的字号都是点（9pt 等），不设就会在高分屏偏小
        root.tk.call("tk", "scaling", float(root.winfo_fpixels("1i")) / 72.0)
    except Exception:  # noqa: BLE001
        pass
    app = PenguinPrintApp(root, registry_path=registry_path)
    return root, app


def main(registry_path: str | Path | None = None) -> int:
    try:
        root, _app = build_app(registry_path)
    except tk.TclError as exc:  # pragma: no cover - 无显示环境
        print(f"无法启动图形界面（{exc}）。可改用命令行：python -m penguinprint embed …", file=sys.stderr)
        return 2
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
