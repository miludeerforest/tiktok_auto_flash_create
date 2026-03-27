import atexit
import json
import os
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from typing import TypedDict

import flashsale_runner as runner


# Dynamic BASE_DIR: works for both PyInstaller EXE and direct Python execution
if getattr(sys, 'frozen', False):
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))
runner.configure_paths(base_dir)
atexit.register(runner.cleanup_runtime_artifacts)
CONFIG_PATH = os.path.join(base_dir, "gui_config.json")
ANCHOR_FILE = os.path.join(base_dir, ".schedule_anchor.txt")
MANUAL_FLAG = os.path.join(base_dir, ".manual_resume.flag")
LOG_PATH = os.path.join(base_dir, "gui_run.log")


class GuiConfig(TypedDict):
    batch_rounds: int
    seed_names: list[str]
    auto_solve_captcha: bool
    ai_provider: str
    ai_api_key: str
    ai_model: str
    ai_base_url: str


class NotificationTone(TypedDict):
    bg: str
    border: str
    title: str
    text: str


def setup_windows_console():
    """Best-effort setup for UTF-8 console output on Windows to reduce encoding issues."""
    if sys.platform == "win32":
        try:
            os.system("chcp 65001 > NUL")
            # Use safe attribute access to satisfy type checkers like basedpyright
            for stream in (sys.stdout, sys.stderr):
                reconf = getattr(stream, "reconfigure", None)
                if callable(reconf):
                    reconf(encoding="utf-8")
        except Exception:
            pass

def load_cfg() -> GuiConfig:
    if not os.path.exists(CONFIG_PATH):
        return {
            "batch_rounds": 4,
            "seed_names": [
                "引流-2026-3.3-00:30",
                "微利-2026-3.3-01:00",
                "盈利-2026-3.3-01:30",
                "平本-2026-3.3-02:00",
            ],
            "auto_solve_captcha": False,
            "ai_provider": "openai",
            "ai_api_key": "",
            "ai_model": "",
            "ai_base_url": "",
        }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            if "ai_api_key" not in cfg:
                cfg["ai_api_key"] = ""
            if "auto_solve_captcha" not in cfg:
                cfg["auto_solve_captcha"] = False
            if "ai_provider" not in cfg:
                cfg["ai_provider"] = "openai"
            if "ai_model" not in cfg:
                cfg["ai_model"] = ""
            if "ai_base_url" not in cfg:
                cfg["ai_base_url"] = ""
            return {
                "batch_rounds": int(cfg.get("batch_rounds", 4)),
                "seed_names": [str(item).strip() for item in cfg.get("seed_names", ["", "", "", ""])],
                "auto_solve_captcha": bool(cfg.get("auto_solve_captcha", False)),
                "ai_provider": str(cfg.get("ai_provider", "openai")),
                "ai_api_key": str(cfg.get("ai_api_key", "")),
                "ai_model": str(cfg.get("ai_model", "")),
                "ai_base_url": str(cfg.get("ai_base_url", "")),
            }
    except Exception:
        return {
            "batch_rounds": 4, 
            "seed_names": ["", "", "", ""], 
            "auto_solve_captcha": False,
            "ai_provider": "openai",
            "ai_api_key": "",
            "ai_model": "",
            "ai_base_url": "",
        }

def save_cfg(cfg: GuiConfig):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def parse_seed_inputs(seed_values: list[str]) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()

    for raw in seed_values:
        value = str(raw or "").strip()
        if not value:
            continue
        normalized_name = runner.strip_random_suffix(value)
        if not runner.parse_promo_name(normalized_name):
            invalid.append(value)
            continue
        key = normalized_name.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(normalized_name)

    return normalized, invalid


def cdp_check(port: int | None = None, label: str | None = None) -> tuple[bool, str]:
    runner.configure_paths(base_dir)
    if port is None:
        return False, "检测失败：未选择CDP实例。\n请先点击'扫描实例'并从下拉列表选择一个店铺。"

    diag = runner.diagnose_cdp(port)
    display_info = f"{label} (端口: {port})" if label else f"端口: {port}"
    if diag["ok"]:
        return True, f"CDP检测成功！\n{display_info}\n地址: {diag['endpoint']}"

    # Build detailed failure message
    attempts_log = ""
    for a in diag["attempts"]:
        status = "成功" if a["ok"] else f"失败 ({a['error']})"
        attempts_log += f"  - {a['host']}: {status}\n"

    return False, (
        f"CDP检测失败 ({display_info})\n\n"
        f"网络诊断:\n{attempts_log}\n"
        f"可能原因:\n"
        f"1. 该店铺对应的浏览器窗口未打开\n"
        f"2. 紫鸟环境配置中未开启 'WebDriver/CDP' 远程调试\n"
        f"3. 浏览器窗口卡死或端口已被其他程序占用\n\n"
        f"解决建议: 请在紫鸟打开对应店铺，并确认已启用WebDriver。"
    )

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Flash Sale 自动化工具")
        self.root.geometry("1120x760")
        self.root.minsize(1020, 700)
        self._running = False
        self._closing = False
        self._stop_requested = False
        self._cdp_endpoints = []
        self.status_var = tk.StringVar(value="初始化中")
        self.ready_hint_var = tk.StringVar(value="正在准备界面…")
        self._active_notifications: list[tk.Toplevel] = []
        self._last_notification_key = ""

        self._startup_cleanup_count = runner.cleanup_runtime_artifacts()
        self.cfg = load_cfg()
        self._configure_styles()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if self._startup_cleanup_count:
            self._log(f"启动时已清理 {self._startup_cleanup_count} 张历史截图。")
        self._refresh_context_summary(update_status=True)

    def _configure_styles(self):
        self.colors = {
            "app_bg": "#F3F6FB",
            "card_bg": "#FFFFFF",
            "border": "#D7DFEA",
            "text": "#0F172A",
            "muted": "#475569",
            "accent": "#2563EB",
        }
        self.notification_tones: dict[str, NotificationTone] = {
            "info": {"bg": "#F8FAFC", "border": "#CBD5E1", "title": "#0F172A", "text": "#334155"},
            "warning": {"bg": "#FFF7ED", "border": "#FDBA74", "title": "#9A3412", "text": "#7C2D12"},
            "danger": {"bg": "#FEF2F2", "border": "#FCA5A5", "title": "#991B1B", "text": "#7F1D1D"},
            "success": {"bg": "#F0FDF4", "border": "#86EFAC", "title": "#166534", "text": "#166534"},
        }
        try:
            self.root.configure(bg=self.colors["app_bg"])
        except tk.TclError:
            pass

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("App.TFrame", background=self.colors["app_bg"])
        style.configure("Card.TFrame", background=self.colors["card_bg"])
        style.configure(
            "Card.TLabelframe",
            background=self.colors["card_bg"],
            bordercolor=self.colors["border"],
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=self.colors["card_bg"],
            foreground=self.colors["text"],
            font=("Microsoft YaHei", 10, "bold"),
        )
        style.configure(
            "Heading.TLabel",
            background=self.colors["app_bg"],
            foreground=self.colors["text"],
            font=("Microsoft YaHei", 18, "bold"),
        )
        style.configure(
            "Subheading.TLabel",
            background=self.colors["app_bg"],
            foreground=self.colors["muted"],
            font=("Microsoft YaHei", 9),
        )
        style.configure(
            "FieldLabel.TLabel",
            background=self.colors["card_bg"],
            foreground=self.colors["text"],
            font=("Microsoft YaHei", 9, "bold"),
        )
        style.configure(
            "CardText.TLabel",
            background=self.colors["card_bg"],
            foreground=self.colors["text"],
            font=("Microsoft YaHei", 9),
        )
        style.configure(
            "Hint.TLabel",
            background=self.colors["card_bg"],
            foreground=self.colors["muted"],
            font=("Microsoft YaHei", 9),
        )
        style.configure(
            "Muted.TLabel",
            background=self.colors["app_bg"],
            foreground=self.colors["muted"],
            font=("Microsoft YaHei", 9),
        )
        style.configure("Action.TButton", padding=(12, 7))
        style.configure("Small.TButton", padding=(10, 5))
        style.configure("TEntry", padding=6)
        style.configure("TCombobox", padding=5)
        style.configure("TSpinbox", padding=4)

    def _set_status(self, text: str, tone: str = "neutral"):
        palette = {
            "neutral": ("#E2E8F0", "#334155"),
            "success": ("#DCFCE7", "#166534"),
            "warning": ("#FEF3C7", "#92400E"),
            "danger": ("#FEE2E2", "#B91C1C"),
            "running": ("#DBEAFE", "#1D4ED8"),
        }
        bg, fg = palette.get(tone, palette["neutral"])
        self.status_var.set(text)
        if hasattr(self, "status_badge"):
            self.status_badge.configure(bg=bg, fg=fg)

    def _refresh_context_summary(self, update_status: bool = False):
        label = self._get_selected_label() if hasattr(self, "cdp_combo") else None
        if label:
            self.ready_hint_var.set(f"当前已选择实例：{label}")
            if update_status and not self._running:
                self._set_status("已就绪，可开始运行", "success")
        else:
            self.ready_hint_var.set("当前未选择 CDP 实例。请先点击“扫描实例”，再从下拉框选择店铺。")
            if update_status and not self._running:
                self._set_status("等待选择 CDP 实例", "warning")

    def _set_running_state(self, running: bool):
        self._running = running
        normal_state = tk.DISABLED if running else tk.NORMAL
        combo_state = tk.DISABLED if running else "readonly"

        for attr in ("run_button", "save_button", "scan_button", "check_button", "manual_button"):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.configure(state=normal_state)

        if hasattr(self, "cdp_combo"):
            self.cdp_combo.configure(state=combo_state)

        if hasattr(self, "stop_button"):
            self.stop_button.configure(state=(tk.NORMAL if running else tk.DISABLED))

        if running:
            self._set_status("任务运行中，请查看右侧日志", "running")
        else:
            self._refresh_context_summary(update_status=True)

    def _remove_notification(self, toast: tk.Toplevel):
        try:
            if toast in self._active_notifications:
                self._active_notifications.remove(toast)
            toast.destroy()
        except tk.TclError:
            pass
        self._reposition_notifications()

    def _reposition_notifications(self):
        try:
            self.root.update_idletasks()
            root_x = self.root.winfo_rootx()
            root_y = self.root.winfo_rooty()
            root_w = self.root.winfo_width()
        except tk.TclError:
            return

        offset_y = 20
        for toast in list(self._active_notifications):
            try:
                toast.update_idletasks()
                width = toast.winfo_width()
                height = toast.winfo_height()
                x = root_x + root_w - width - 24
                y = root_y + offset_y
                toast.geometry(f"+{x}+{y}")
                offset_y += height + 12
            except tk.TclError:
                continue

    def _show_notification(self, title: str, message: str, tone: str = "info", duration_ms: int = 12000, dedupe_key: str | None = None):
        palette = self.notification_tones.get(tone, self.notification_tones["info"])
        normalized_message = " ".join(str(message).split())
        notify_key = dedupe_key or f"{tone}:{title}:{normalized_message}"
        if notify_key == self._last_notification_key:
            return
        self._last_notification_key = notify_key

        toast = tk.Toplevel(self.root)
        toast.withdraw()
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(bg=palette["border"])

        shell = tk.Frame(toast, bg=palette["bg"], bd=0, highlightthickness=0)
        shell.pack(fill="both", expand=True, padx=1, pady=1)

        top = tk.Frame(shell, bg=palette["bg"])
        top.pack(fill="x", padx=14, pady=(12, 6))

        tk.Label(
            top,
            text=title,
            bg=palette["bg"],
            fg=palette["title"],
            font=("Microsoft YaHei", 10, "bold"),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

        close_btn = tk.Label(
            top,
            text="✕",
            bg=palette["bg"],
            fg=palette["text"],
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda _event, ref=toast: self._remove_notification(ref))

        tk.Label(
            shell,
            text=message,
            bg=palette["bg"],
            fg=palette["text"],
            font=("Microsoft YaHei", 9),
            justify="left",
            wraplength=300,
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 12))

        toast.deiconify()
        self._active_notifications.append(toast)
        self._reposition_notifications()

        if duration_ms > 0:
            toast.after(duration_ms, lambda ref=toast: self._remove_notification(ref))

    def _notify_manual_attention(self, title: str, message: str, tone: str = "warning", duration_ms: int = 15000, dedupe_key: str | None = None):
        self._schedule_ui(lambda: self._show_notification(title, message, tone=tone, duration_ms=duration_ms, dedupe_key=dedupe_key))

    def _maybe_notify_for_log_line(self, line: str):
        text = str(line or "").strip()
        if not text:
            return

        lower = text.lower()
        if "captcha detected. please solve it manually in browser" in lower:
            self._notify_manual_attention(
                "需要手动处理验证码",
                "浏览器中检测到滑块验证码。请切到浏览器完成验证，程序会自动继续。",
                tone="warning",
                duration_ms=18000,
                dedupe_key="captcha-manual-required",
            )
            self._set_status("等待人工处理验证码", "warning")
            return

        if "manual pause:" in lower:
            self._notify_manual_attention(
                "需要人工处理当前步骤",
                text,
                tone="warning",
                duration_ms=16000,
                dedupe_key=f"manual-pause:{text}",
            )
            self._set_status("等待人工继续", "warning")
            return

        if "waiting for manual continue" in lower:
            self._notify_manual_attention(
                "等待点击“手动继续”",
                "当前步骤需要你先处理浏览器中的弹窗或验证码，完成后再点击“手动继续”。",
                tone="warning",
                duration_ms=14000,
                dedupe_key="manual-continue-waiting",
            )
            self._set_status("等待人工继续", "warning")
            return

        if "manual continue detected" in lower:
            self._notify_manual_attention(
                "已收到继续信号",
                "人工处理完成，程序将继续后续步骤。",
                tone="success",
                duration_ms=6000,
                dedupe_key="manual-continue-detected",
            )
            return

        if "error: captcha not solved in time" in lower or "manual continue timeout" in lower:
            self._notify_manual_attention(
                "人工处理超时",
                "验证码或弹窗在等待时间内未处理完成，本轮任务已停止。请检查日志后重新运行。",
                tone="danger",
                duration_ms=18000,
                dedupe_key=f"manual-timeout:{text}",
            )
            self._set_status("人工处理超时", "danger")
            return

        if "late guideline modal dismissed" in lower or "post-captcha guideline modal dismissed" in lower:
            self._notify_manual_attention(
                "弹窗已自动处理",
                "系统检测到的提示弹窗已经被自动关闭，可以继续观察日志。",
                tone="info",
                duration_ms=5000,
                dedupe_key=f"modal-dismissed:{text}",
            )

    def _build_ui(self):
        shell = ttk.Frame(self.root, style="App.TFrame", padding=(18, 16, 18, 16))
        shell.pack(fill="both", expand=True)

        header = ttk.Frame(shell, style="App.TFrame")
        header.pack(fill="x", pady=(0, 14))

        title_wrap = ttk.Frame(header, style="App.TFrame")
        title_wrap.pack(side="left", fill="x", expand=True)
        ttk.Label(title_wrap, text="Flash Sale 自动化工具", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(
            title_wrap,
            text="围绕 4 条参考活动进行轮转复制。当前版本不再依赖 xlsx，适合直接打包为 EXE 日常操作。",
            style="Subheading.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(4, 0))

        self.status_badge = tk.Label(
            header,
            textvariable=self.status_var,
            bg="#E2E8F0",
            fg="#334155",
            font=("Microsoft YaHei", 9, "bold"),
            padx=12,
            pady=6,
            relief="flat",
        )
        self.status_badge.pack(side="right", anchor="n")

        content = ttk.Frame(shell, style="App.TFrame")
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=5, uniform="panel")
        content.columnconfigure(1, weight=6, uniform="panel")
        content.rowconfigure(0, weight=1)

        left_panel = ttk.Frame(content, style="App.TFrame")
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right_panel = ttk.Frame(content, style="App.TFrame")
        right_panel.grid(row=0, column=1, sticky="nsew")

        self.seed_vars = []
        seeds = self.cfg.get("seed_names", ["", "", "", ""])
        seeds = (seeds + ["", "", "", ""])[:4]

        ref_card = ttk.LabelFrame(left_panel, text="1. 参考活动与运行设置", style="Card.TLabelframe", padding=(14, 12))
        ref_card.pack(fill="x", pady=(0, 12))
        ref_card.columnconfigure(1, weight=1)
        ttk.Label(
            ref_card,
            text="按顺序填写 4 条参考活动名称，程序会用它们作为复制轮转模板；名称必须能解析出时间。",
            style="Hint.TLabel",
            wraplength=420,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        for i in range(4):
            ttk.Label(ref_card, text=f"参考活动 {i + 1}", style="FieldLabel.TLabel").grid(
                row=i + 1, column=0, sticky="w", padx=(0, 10), pady=5
            )
            var = tk.StringVar(value=seeds[i])
            self.seed_vars.append(var)
            ttk.Entry(ref_card, textvariable=var).grid(row=i + 1, column=1, sticky="ew", pady=5)

        ttk.Label(
            ref_card,
            text="示例：引流-2026-3.3-00:30 / 微利-2026-3.3-01:00。支持自动去掉随机后缀。",
            style="Hint.TLabel",
            wraplength=420,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 10))

        rounds_row = ttk.Frame(ref_card, style="Card.TFrame")
        rounds_row.grid(row=6, column=0, columnspan=2, sticky="ew")
        ttk.Label(rounds_row, text="运行轮数", style="FieldLabel.TLabel").pack(side="left")
        self.round_var = tk.IntVar(value=int(self.cfg.get("batch_rounds", 4)))
        ttk.Spinbox(rounds_row, from_=1, to=200, textvariable=self.round_var, width=8).pack(side="left", padx=(10, 8))
        ttk.Label(rounds_row, text="按提交条数计数。建议先用小轮数试跑验证。", style="Hint.TLabel").pack(side="left")

        self.auto_solve_captcha_var = tk.BooleanVar(value=bool(self.cfg.get("auto_solve_captcha", False)))
        captcha_toggle_row = ttk.Frame(ref_card, style="Card.TFrame")
        captcha_toggle_row.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(
            captcha_toggle_row,
            text="启用自动验证码处理（默认关闭）",
            variable=self.auto_solve_captcha_var,
        ).pack(side="left")
        ttk.Label(
            captcha_toggle_row,
            text="关闭时不走 AI/视觉自动验证，遇到验证码直接人工处理。",
            style="Hint.TLabel",
        ).pack(side="left", padx=(10, 0))

        ai_card = ttk.LabelFrame(left_panel, text="2. AI 高级设置（可选）", style="Card.TLabelframe", padding=(14, 12))
        ai_card.pack(fill="x")
        ai_card.columnconfigure(1, weight=1)
        ttk.Label(
            ai_card,
            text="只有需要 AI 识别/扩展能力时才填写；留空时使用默认逻辑。",
            style="Hint.TLabel",
            wraplength=420,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(ai_card, text="AI Provider", style="FieldLabel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
        self.ai_provider_var = tk.StringVar(value=self.cfg.get("ai_provider", "openai"))
        ttk.Combobox(ai_card, textvariable=self.ai_provider_var, values=["openai", "gemini"], state="readonly", width=14).grid(
            row=1, column=1, sticky="w", pady=5
        )

        ttk.Label(ai_card, text="AI API Key", style="FieldLabel.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=5)
        self.ai_api_key_var = tk.StringVar(value=self.cfg.get("ai_api_key", ""))
        ttk.Entry(ai_card, textvariable=self.ai_api_key_var).grid(row=2, column=1, sticky="ew", pady=5)

        ttk.Label(ai_card, text="AI Model", style="FieldLabel.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=5)
        self.ai_model_var = tk.StringVar(value=self.cfg.get("ai_model", ""))
        ttk.Entry(ai_card, textvariable=self.ai_model_var).grid(row=3, column=1, sticky="ew", pady=5)
        ttk.Label(ai_card, text="留空使用默认模型：gpt-4o / gemini-2.0-flash", style="Hint.TLabel").grid(row=4, column=1, sticky="w")

        ttk.Label(ai_card, text="Base URL", style="FieldLabel.TLabel").grid(row=5, column=0, sticky="w", padx=(0, 10), pady=5)
        self.ai_base_url_var = tk.StringVar(value=self.cfg.get("ai_base_url", ""))
        ttk.Entry(ai_card, textvariable=self.ai_base_url_var).grid(row=5, column=1, sticky="ew", pady=5)
        ttk.Label(ai_card, text="可留空走官方接口，也可填写 Azure / Ollama 等兼容地址。", style="Hint.TLabel").grid(row=6, column=1, sticky="w")

        workflow_card = ttk.LabelFrame(right_panel, text="3. 推荐操作顺序", style="Card.TLabelframe", padding=(14, 12))
        workflow_card.pack(fill="x", pady=(0, 12))
        ttk.Label(
            workflow_card,
            text="① 填好 4 条参考活动 → ② 扫描实例 → ③ 选择店铺并检测 → ④ 一键运行",
            style="CardText.TLabel",
            wraplength=500,
            justify="left",
        ).pack(anchor="w")
        ttk.Label(
            workflow_card,
            text="运行中若遇验证码，请在浏览器里手动完成，日志会继续往下走。",
            style="Hint.TLabel",
            wraplength=500,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        cdp_card = ttk.LabelFrame(right_panel, text="4. 店铺实例", style="Card.TLabelframe", padding=(14, 12))
        cdp_card.pack(fill="x", pady=(0, 12))
        cdp_card.columnconfigure(0, weight=1)
        ttk.Label(cdp_card, textvariable=self.ready_hint_var, style="CardText.TLabel", wraplength=500, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        self.cdp_combo = ttk.Combobox(cdp_card, state="readonly")
        self.cdp_combo.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.cdp_combo.set('(点击"扫描实例"检测可用浏览器)')
        self.cdp_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_context_summary(update_status=True))

        cdp_button_col = ttk.Frame(cdp_card, style="Card.TFrame")
        cdp_button_col.grid(row=1, column=1, sticky="ne")
        self.scan_button = ttk.Button(cdp_button_col, text="扫描实例", style="Action.TButton", command=self.scan_cdp_clicked)
        self.scan_button.pack(fill="x")
        self.check_button = ttk.Button(cdp_button_col, text="检测当前实例", style="Small.TButton", command=self.check_cdp_clicked)
        self.check_button.pack(fill="x", pady=(8, 0))

        action_card = ttk.LabelFrame(right_panel, text="5. 执行动作", style="Card.TLabelframe", padding=(14, 12))
        action_card.pack(fill="x", pady=(0, 12))
        ttk.Label(
            action_card,
            text="建议先保存配置，再一键运行。停止按钮只负责发出停止请求，关键过程请以日志为准。",
            style="Hint.TLabel",
            wraplength=500,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        secondary_actions = ttk.Frame(action_card, style="Card.TFrame")
        secondary_actions.pack(fill="x")
        self.save_button = ttk.Button(secondary_actions, text="保存配置", style="Action.TButton", command=self.save_config_clicked)
        self.save_button.pack(side="left")
        self.manual_button = ttk.Button(secondary_actions, text="手动继续", style="Action.TButton", command=self.manual_resume_clicked)
        self.manual_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(secondary_actions, text="停止运行", style="Action.TButton", command=self.stop_clicked)
        self.stop_button.pack(side="right")

        self.run_button = tk.Button(
            action_card,
            text="一键运行",
            command=self.run_clicked,
            bg=self.colors["accent"],
            fg="#FFFFFF",
            activebackground="#1D4ED8",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            font=("Microsoft YaHei", 11, "bold"),
            padx=16,
            pady=10,
            cursor="hand2",
        )
        self.run_button.pack(fill="x", pady=(12, 0))

        log_card = ttk.LabelFrame(right_panel, text="6. 运行日志", style="Card.TLabelframe", padding=(14, 12))
        log_card.pack(fill="both", expand=True)
        log_card.rowconfigure(1, weight=1)
        log_card.columnconfigure(0, weight=1)
        ttk.Label(
            log_card,
            text="日志会实时写入窗口和 gui_run.log。出现异常时，优先查看这里最后几行。",
            style="Hint.TLabel",
            wraplength=500,
            justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        log_wrap = ttk.Frame(log_card, style="Card.TFrame")
        log_wrap.grid(row=1, column=0, sticky="nsew")
        log_wrap.rowconfigure(0, weight=1)
        log_wrap.columnconfigure(0, weight=1)

        self.log = tk.Text(
            log_wrap,
            height=20,
            wrap="word",
            bg="#0F172A",
            fg="#E2E8F0",
            insertbackground="#E2E8F0",
            relief="flat",
            bd=0,
            padx=12,
            pady=10,
            font=("Consolas", 10),
            spacing1=2,
            spacing3=2,
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

        self._set_running_state(False)
        self._log('界面已准备好。推荐先点击“扫描实例”，选择店铺后再运行。')

    def _log(self, msg: str):
        try:
            stamp = datetime.now().strftime("%H:%M:%S")
            lines = str(msg).splitlines() or [""]
            for line in lines:
                self.log.insert("end", f"[{stamp}] {line}\n")
            self.log.see("end")
        except tk.TclError:
            pass

    def _schedule_ui(self, callback):
        if self._closing:
            return
        try:
            if self.root.winfo_exists():
                self.root.after(0, callback)
        except tk.TclError:
            pass

    def _cleanup_runtime_artifacts(self, reason: str | None = None) -> int:
        runner.configure_paths(base_dir)
        removed = runner.cleanup_runtime_artifacts()
        if removed and reason:
            self._log(f"{reason}已清理 {removed} 张历史截图。")
        return removed

    def on_close(self):
        self._closing = True
        if self._running:
            self._stop_requested = True
        self.root.destroy()

    def _get_selected_port(self) -> int | None:
        """Get the CDP port from the selected dropdown item."""
        idx = self.cdp_combo.current()
        if 0 <= idx < len(self._cdp_endpoints):
            return self._cdp_endpoints[idx]["port"]
        return None
    def _get_selected_label(self) -> str | None:
        """Get the CDP label from the selected dropdown item."""
        idx = self.cdp_combo.current()
        if 0 <= idx < len(self._cdp_endpoints):
            return self._cdp_endpoints[idx]["label"]
        return None

    def scan_cdp_clicked(self):
        runner.configure_paths(base_dir)
        self._set_status("正在扫描可用实例…", "running")
        self._log("正在扫描CDP实例...")
        endpoints = runner.find_all_cdp_endpoints()
        self._cdp_endpoints = endpoints
        if not endpoints:
            self.cdp_combo["values"] = []
            self.cdp_combo.set("(未检测到可用实例)")
            self._log("未检测到CDP实例。请确认紫鸟已启动并启用CDP。")
            self._refresh_context_summary(update_status=True)
            return
        labels = [ep["label"] for ep in endpoints]
        self.cdp_combo["values"] = labels
        self.cdp_combo.current(0)
        self._log(f"检测到 {len(endpoints)} 个CDP实例:")
        for ep in endpoints:
            self._log(f"  {ep['label']}")
        self._refresh_context_summary(update_status=True)

    def save_config_clicked(self):
        raw_seeds = [v.get().strip() for v in self.seed_vars]
        seeds, invalid = parse_seed_inputs(raw_seeds)
        if invalid:
            invalid_text = "\n".join(f"- {item}" for item in invalid)
            self._log("配置未保存：存在无法解析的参考活动名称。")
            self._set_status("参考活动格式有误", "danger")
            messagebox.showerror(
                "参考活动格式错误",
                "以下参考活动名称无法解析，请按“前缀-YYYY-M.D-HH:MM”格式填写：\n" + invalid_text,
            )
            return False
        if len(seeds) < 4:
            self._log("配置未保存：参考活动不足 4 条。")
            self._set_status("参考活动不足 4 条", "danger")
            messagebox.showerror(
                "参考活动不足",
                "请手动填写 4 条可解析的参考活动名称后再保存/运行。",
            )
            return False
        cfg: GuiConfig = {
            "seed_names": seeds,
            "batch_rounds": int(self.round_var.get()),
            "auto_solve_captcha": bool(self.auto_solve_captcha_var.get()),
            "ai_provider": self.ai_provider_var.get().strip(),
            "ai_api_key": self.ai_api_key_var.get().strip(),
            "ai_model": self.ai_model_var.get().strip(),
            "ai_base_url": self.ai_base_url_var.get().strip()
        }
        save_cfg(cfg)
        self._log("配置已保存。")
        runner.configure_paths(base_dir)
        parse_fn = runner.parse_promo_name
        dts = []
        for s in seeds:
            p = parse_fn(s)
            if p:
                dts.append(p["dt"])
        if dts:
            dtv = max(dts)
            with open(ANCHOR_FILE, "w", encoding="utf-8") as f:
                f.write(dtv.strftime("%Y-%m-%d %H:%M"))
            self._log(f"已保存锚点: {dtv}")

        self._log("配置已保存。")
        self._set_status("配置已保存", "success")
        messagebox.showinfo("提示", "配置已保存")
        return True

    def check_cdp_clicked(self):
        port = self._get_selected_port()
        label = self._get_selected_label()
        ok, info = cdp_check(port, label)
        self._log("-" * 30)
        self._log(f"CDP检测结果: {'成功' if ok else '失败'}")
        self._log(info)
        if ok:
            self._set_status("CDP 检测成功", "success")
            messagebox.showinfo("CDP检测", info)
        else:
            self._set_status("CDP 检测失败", "danger")
            messagebox.showerror("CDP检测", info)

    def manual_resume_clicked(self):
        with open(MANUAL_FLAG, "w", encoding="utf-8") as f:
            f.write("resume")
        self._log("已写入手动继续标记。")
        self._set_status("已发送手动继续信号", "warning")
        self._notify_manual_attention(
            "已发送手动继续",
            "如果浏览器中的验证码或指南弹窗已经处理完成，程序会继续执行。",
            tone="info",
            duration_ms=5000,
            dedupe_key="manual-continue-clicked",
        )

    def stop_clicked(self):
        if self._running:
            self._stop_requested = True
            self._log("已请求停止，等待当前轮次结束...")
            self._set_status("已请求停止，等待当前轮次结束", "warning")
        else:
            self._log("当前没有运行中的任务。")
            self._refresh_context_summary(update_status=True)

    def run_clicked(self):
        if self._running:
            self._notify_manual_attention(
                "已有任务在运行",
                "当前已有运行中的任务。如需重新开始，请先等待结束或点击停止运行。",
                tone="info",
                duration_ms=5000,
                dedupe_key="already-running",
            )
            messagebox.showwarning("运行中", "已有任务在运行，请先停止。")
            return

        port = self._get_selected_port()
        label = self._get_selected_label()
        ok, info = cdp_check(port, label)
        if not ok:
            self._log("-" * 30)
            self._log("无法开始运行: CDP检测失败")
            self._log(info)
            self._set_status("无法开始运行，请先修复 CDP", "danger")
            messagebox.showerror("无法运行", info)
            return

        if not self.save_config_clicked():
            return
        self._cleanup_runtime_artifacts("运行前，")
        rounds = int(self.round_var.get())

        self._set_running_state(True)
        self._stop_requested = False

        def worker():
            self._log(f"开始运行，轮数={rounds}")
            runner.configure_paths(base_dir)
            runner.configure_runtime(
                batch_rounds=rounds,
                cdp_port_override=port,
                auto_solve_captcha_override=bool(self.auto_solve_captcha_var.get()),
            )
            schedule_ui = self._schedule_ui
            append_log = self._log
            notify_for_line = self._maybe_notify_for_log_line

            # Redirect stdout/stderr to GUI log + file
            import io as _io

            class _LogWriter(_io.TextIOBase):
                def __init__(self, logf):
                    self.logf = logf
                    self._closed = False

                def write(self, s):
                    if self._closed or not s or s == '\n':
                        return len(s) if s else 0
                    for line in s.rstrip('\n').split('\n'):
                        try:
                            self.logf.write(line + '\n')
                            self.logf.flush()
                        except ValueError:
                            pass
                        notify_for_line(line)
                        schedule_ui(lambda m=line: append_log(m))
                    return len(s)

                def flush(self):
                    if not self._closed:
                        try:
                            self.logf.flush()
                        except ValueError:
                            pass

                def close_writer(self):
                    self._closed = True

            import sys as _sys
            old_stdout, old_stderr = _sys.stdout, _sys.stderr
            logf = open(LOG_PATH, "w", encoding="utf-8")
            writer = _LogWriter(logf)

            try:
                _sys.stdout = writer
                _sys.stderr = writer
                try:
                    import asyncio
                    asyncio.run(runner.main())
                    schedule_ui(lambda: append_log("执行完成。"))
                    schedule_ui(lambda: self._set_status("执行完成", "success"))
                except Exception as e:
                    err_msg = str(e)
                    schedule_ui(lambda: append_log(f"运行失败: {err_msg}"))
                    schedule_ui(lambda: self._set_status("运行失败，请查看日志", "danger"))
            finally:
                _sys.stdout = old_stdout
                _sys.stderr = old_stderr
                writer.close_writer()
                logf.close()
                schedule_ui(lambda: self._set_running_state(False))

        threading.Thread(target=worker, daemon=True).start()


def main():
    setup_windows_console()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
