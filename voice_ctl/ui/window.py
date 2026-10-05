"""主窗口：顶栏状态 + 左侧导航 + 内容区 + 底栏控制。

线程模型很简单，只有一条规则：**Tk 只能被主线程碰**。
worker 线程（识别、预检、下载）只往事件总线里丢事件或往一个槽里放结果，
主线程每 120ms 轮询一次再更新界面。这样就不需要任何锁，也不会出现
「窗口偶尔卡死」那类只在用户机器上复现的问题。
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import ttk
from typing import Any

from .. import __version__, bootstrap, events
from ..actions import HANDLERS, ActionContext
from ..config import ActionConfig, AppConfig, ConfigError, load_config
from ..confedit import ConfigEditor
from ..runner import Engine
from . import theme, widgets as W
from .tab_actions import ActionsTab
from .tab_about import AboutTab
from .tab_hotkey import HotkeyTab
from .tab_logs import LogsTab
from .tab_run import RunTab
from .tab_settings import SettingsTab
from .theme import PALETTE as P
from .theme import S

POLL_MS = 120

NAV = [
    ("run", "运行", "▶"),
    ("logs", "日志", "≡"),
    ("hotkey", "快捷键", "⌨"),
    ("actions", "功能", "✦"),
    ("settings", "设置", "⚙"),
    ("about", "关于", "ⓘ"),
]


class App:
    """窗口控制器。页面通过它访问引擎、配置和状态栏。"""

    def __init__(self, root: tk.Tk, cfg: AppConfig, config_path: Path | None) -> None:
        self.root = root
        self.cfg = cfg
        self.config_path = config_path
        self.bus = events.get_bus()
        self.cursor = self.bus.last_seq
        """从「打开界面那一刻」开始算，日志面板显示的是实时活动而不是回放。"""
        self.dry_run = False
        self.engine = Engine(cfg)
        self.current = ""
        self.tabs: dict[str, Any] = {}
        self._nav: dict[str, W.NavItem] = {}
        self._badge = 0
        self._busy = False

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #

    def build(self) -> None:
        self._build_header()
        body = tk.Frame(self.root, bg=P["bg"])
        body.pack(fill="both", expand=True)
        self._build_sidebar(body)
        self._build_content(body)
        self._build_footer()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show_tab("run")
        self.engine.prepare()
        # 映射之后再设一次深色标题栏：有些 Windows 版本在窗口可见前设了不生效
        self.root.after(80, self._late_polish)
        self._poll()

    def _late_polish(self) -> None:
        events.debug(
            f"深色标题栏：{'已生效' if theme.apply_dark_titlebar(self.root) else '未生效（只影响观感）'}",
            kind="ui",
        )

    def _build_header(self) -> None:
        head = tk.Frame(self.root, bg=P["surface"], height=S(64))
        head.pack(fill="x")
        head.pack_propagate(False)
        W.hline(self.root, soft=True)

        left = tk.Frame(head, bg=P["surface"])
        left.pack(side="left", fill="y", padx=S(18))
        title = tk.Frame(left, bg=P["surface"])
        title.pack(anchor="w", pady=(S(12), 0))
        tk.Label(title, text="voice-ctl", bg=P["surface"], fg=P["text"],
                 font=theme.FONTS["title"]).pack(side="left")
        tk.Label(title, text=f" {__version__} ", bg=P["surface2"], fg=P["muted"],
                 font=theme.FONTS["tiny"], padx=S(4), pady=S(1)).pack(side="left", padx=S(8))
        self._subtitle = tk.Label(
            left, text="按住快捷键说话 → 本地离线识别 → 执行", bg=P["surface"],
            fg=P["faint"], font=theme.FONTS["tiny"],
        )
        self._subtitle.pack(anchor="w")

        right = tk.Frame(head, bg=P["surface"])
        right.pack(side="right", fill="y", padx=S(18))
        self._state_pill = W.StatusPill(right, "未启动", color=P["faint"])
        self._state_pill.pack(anchor="e", pady=(S(14), 0))
        self._hot_lbl = tk.Label(right, text="", bg=P["surface"], fg=P["faint"],
                                 font=theme.FONTS["tiny"])
        self._hot_lbl.pack(anchor="e")

    def _build_sidebar(self, parent: tk.Frame) -> None:
        side = tk.Frame(parent, bg=P["surface"], width=S(158))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(parent, bg=P["border"], width=1).pack(side="left", fill="y")
        self._side = side

        for key, label, glyph in NAV:
            item = W.NavItem(side, label, glyph=glyph, command=lambda k=key: self.show_tab(k))
            item.pack(fill="x")
            self._nav[key] = item

        foot = tk.Frame(side, bg=P["surface"])
        foot.pack(side="bottom", fill="x", pady=S(10))
        tk.Label(
            foot, text="纯本地离线\n不联网、零调用成本", bg=P["surface"], fg=P["faint"],
            font=theme.FONTS["tiny"], justify="left",
        ).pack(anchor="w", padx=S(14))

    def _build_content(self, parent: tk.Frame) -> None:
        self._content = tk.Frame(parent, bg=P["bg"])
        self._content.pack(side="left", fill="both", expand=True)
        self.tabs = {
            "run": RunTab(self._content, self),
            "logs": LogsTab(self._content, self),
            "hotkey": HotkeyTab(self._content, self),
            "actions": ActionsTab(self._content, self),
            "settings": SettingsTab(self._content, self),
            "about": AboutTab(self._content, self),
        }

    def _build_footer(self) -> None:
        W.hline(self.root, soft=True)
        foot = tk.Frame(self.root, bg=P["surface"], height=S(52))
        foot.pack(fill="x")
        foot.pack_propagate(False)

        left = tk.Frame(foot, bg=P["surface"])
        left.pack(side="left", fill="y", padx=S(18))
        self._btn_toggle = ttk.Button(left, text="启动监听", style="Primary.TButton",
                                      command=self.toggle_engine)
        self._btn_toggle.pack(side="left", pady=S(11))
        self._btn_model = ttk.Button(left, text="加载模型", command=self.load_model)
        self._btn_model.pack(side="left", padx=S(8))

        right = tk.Frame(foot, bg=P["surface"])
        right.pack(side="right", fill="y", padx=S(18))
        self._stats_lbl = tk.Label(right, text="", bg=P["surface"], fg=P["muted"],
                                   font=theme.FONTS["small"])
        self._stats_lbl.pack(side="right", pady=S(16))

    # ------------------------------------------------------------------ #
    # 页面切换
    # ------------------------------------------------------------------ #

    def show_tab(self, key: str) -> None:
        if key == self.current:
            tab = self.tabs.get(key)
            if tab is not None and hasattr(tab, "on_show"):
                tab.on_show()
            return
        for k, item in self._nav.items():
            item.set_active(k == key)
        old = self.tabs.get(self.current)
        if old is not None:
            old.pack_forget()
        tab = self.tabs[key]
        tab.pack(fill="both", expand=True)
        self.current = key
        if hasattr(tab, "on_show"):
            tab.on_show()

    # ------------------------------------------------------------------ #
    # 轮询
    # ------------------------------------------------------------------ #

    def _poll(self) -> None:
        try:
            incoming = self.bus.since(self.cursor)
            if incoming:
                self.cursor = incoming[-1].seq
                self.tabs["logs"].feed(incoming)
                self._on_events(incoming)
            self.engine.tick()
            tab = self.tabs.get(self.current)
            if tab is not None and hasattr(tab, "on_tick"):
                tab.on_tick()
            self._refresh_chrome()
        except tk.TclError:  # pragma: no cover - 窗口正在销毁
            return
        except Exception as e:  # noqa: BLE001 - 轮询里抛异常会让界面整个冻住
            events.error(f"界面刷新出错：{type(e).__name__}: {e}", kind="ui")
        self.root.after(POLL_MS, self._poll)

    def _on_events(self, incoming: list[events.Event]) -> None:
        if self.current != "logs":
            self._badge += sum(1 for e in incoming if e.level == "error")
        else:
            self._badge = 0

    def _refresh_chrome(self) -> None:
        eng = self.engine
        self._state_pill.set(eng.state_label, W.STATE_COLOR.get(eng.state, P["muted"]))
        self._hot_lbl.configure(text=self._nav_hint())
        self._stats_lbl.configure(text=eng.status_line())
        running = eng.running
        self._btn_toggle.configure(
            text="停止监听" if running else "启动监听",
            style="TButton" if running else "Primary.TButton",
        )
        self._btn_model.state(["disabled"] if eng.model_loaded else ["!disabled"])

        nav = self._nav["logs"]
        label = "日志"
        nav.set_text(f"{label}  ●{self._badge}" if self._badge else label)

    def _nav_hint(self) -> str:
        eng = self.engine
        if eng.state == "running":
            return f"按住 {eng.hotkey_spec} 说话"
        if eng.state == "loading":
            return "正在加载模型 …"
        if eng.state == "error":
            return eng.error[:80]
        return f"热键 {eng.hotkey_spec}（未监听）"

    # ------------------------------------------------------------------ #
    # 引擎
    # ------------------------------------------------------------------ #

    def background(self, fn: Callable[[], Any], *, name: str = "voice-ctl-bg") -> None:
        """把慢活丢到后台线程。UI 线程绝不能做超过几十毫秒的事。"""
        threading.Thread(target=fn, name=name, daemon=True).start()

    def toggle_engine(self) -> None:
        if self.engine.running:
            self.engine.stop()
            return
        if not self.engine.model_loaded:
            self.load_model(start_after=True)
            return
        self.engine.start()

    def load_model(self, *, start_after: bool = False) -> None:
        if self._busy:
            events.warn("已经有一个加载任务在跑了", kind="ui")
            return
        self._busy = True
        eng = self.engine

        def work() -> None:
            try:
                if eng.load_model() and start_after:
                    eng.start()
            finally:
                self._busy = False

        events.info("开始加载识别模型（几秒钟，界面不会卡）…", kind="engine")
        self.background(work, name="voice-ctl-loadmodel")

    def verify_asr(self) -> None:
        eng = self.engine

        def work() -> None:
            if not eng.load_model():
                return
            rt = eng.runtime
            if rt is None:
                return
            from .. import bootstrap as bs

            md = bs.resolve_model_dir(self.cfg.model_path())
            wavs = sorted((md / "test_wavs").glob("*.wav"))
            if not wavs:
                events.warn(f"{md / 'test_wavs'} 里没有样例音频，下载模型时会一起下来", kind="asr")
                return
            for w in wavs:
                try:
                    r = rt.asr.transcribe_file(w)
                except Exception as e:  # noqa: BLE001
                    events.error(f"{w.name} 识别失败：{e}", kind="asr")
                    continue
                events.ok(f"{w.name}: {r.text!r}", kind="asr")
                events.info(r.summary(), kind="asr")

        self.background(work, name="voice-ctl-verify")

    # ------------------------------------------------------------------ #
    # 配置写入
    # ------------------------------------------------------------------ #

    def _editor(self) -> ConfigEditor:
        path = self.config_path
        if path is None:
            path = bootstrap.data_dir() / "config.toml"
        return ConfigEditor(path, create=True)

    def _after_write(self, ed: ConfigEditor, note: str, *, quiet: bool = False) -> None:
        for line in ed.changes:
            events.debug(f"写入 {line}", kind="config")
        events.ok(f"{note}（共 {len(ed.changes)} 处改动，注释已保留）", kind="config")
        self.reload_config(quiet=quiet)
        # 模型目录可能被改过，运行页缓存的存在性判断要作废
        tab = self.tabs.get("run")
        if tab is not None:
            tab._model_present = None  # noqa: SLF001

    def save_settings(self, changes: dict[tuple[str, str], Any], *, note: str = "已保存") -> bool:
        try:
            ed = self._editor()
            for (section, key), value in changes.items():
                ed.set(section, key, value)
            if not ed.dirty:
                events.info("这些值和文件里一样，没有需要写的", kind="config")
                return True
            ed.commit()
        except Exception as e:  # noqa: BLE001
            events.error(f"保存失败：{type(e).__name__}: {e}", kind="config")
            return False
        self._after_write(ed, note)
        return True

    def save_cfg(self, new_cfg: AppConfig) -> bool:
        try:
            ed = self._editor()
            ed.apply_settings(new_cfg)
            if not ed.dirty:
                events.info("没有需要写入的改动", kind="config")
                return True
            ed.commit()
        except Exception as e:  # noqa: BLE001
            events.error(f"保存失败：{type(e).__name__}: {e}", kind="config")
            return False
        self._after_write(ed, "设置已保存")
        return True

    def save_action(self, index: int, fields: dict[str, Any]) -> bool:
        try:
            ed = self._editor()
            if not ed.set_action(index, **fields):
                return False
            ed.commit()
        except Exception as e:  # noqa: BLE001
            events.error(f"保存动作失败：{type(e).__name__}: {e}", kind="config")
            return False
        self._after_write(ed, f"动作已更新（第 {index + 1} 个）")
        return True

    def add_action(self, action: dict[str, Any]) -> int | None:
        try:
            ed = self._editor()
            index = ed.append_action(action, comment="# 下面这个动作是在图形界面里新建的")
            ed.commit()
        except Exception as e:  # noqa: BLE001
            events.error(f"新建动作失败：{type(e).__name__}: {e}", kind="config")
            return None
        self._after_write(ed, f"已新建动作 {action.get('id')}")
        return index

    def remove_action(self, index: int) -> bool:
        try:
            ed = self._editor()
            if not ed.remove_action(index):
                return False
            ed.commit()
        except Exception as e:  # noqa: BLE001
            events.error(f"删除动作失败：{type(e).__name__}: {e}", kind="config")
            return False
        self._after_write(ed, "动作已删除")
        return True

    def move_action(self, index: int, delta: int) -> bool:
        try:
            ed = self._editor()
            if not ed.move_action(index, delta):
                return False
            ed.commit()
        except Exception as e:  # noqa: BLE001
            events.error(f"移动动作失败：{type(e).__name__}: {e}", kind="config")
            return False
        self._after_write(ed, "动作顺序已调整", quiet=True)
        return True

    def reload_config(self, *, quiet: bool = False) -> bool:
        """重新读配置并重建引擎。正在监听的话会自动恢复监听。"""
        was_running = self.engine.running
        had_model = self.engine.model_loaded
        self.engine.close()

        try:
            cfg = load_config(self.config_path)
        except ConfigError as e:
            events.error(f"配置文件有问题，已保留旧的运行时：{e}", kind="config")
            self.engine = Engine(self.cfg, dry_run=self.dry_run)
            self.engine.prepare()
            return False

        self.cfg = cfg
        eng = Engine(cfg, dry_run=self.dry_run)
        self.engine = eng
        if not eng.prepare():
            return False

        if had_model or was_running:
            # 重建引擎等于把加载好的模型丢了，得重新加载——这有几秒，
            # 不能放在 UI 线程里，否则保存一下设置界面就僵住
            self.load_model(start_after=was_running)
        if not quiet:
            events.ok("配置已重新加载", kind="config")
        return True

    def model_dir(self) -> Path:
        """当前实际会用的模型目录（含打包内嵌模型兜底）。"""
        from ..runner import model_dir_for

        return model_dir_for(self.cfg)

    # ------------------------------------------------------------------ #
    # 直接执行某个动作（功能页的「试运行 / 真的执行」）
    # ------------------------------------------------------------------ #

    def run_action_form(self, index: int, fields: dict[str, Any], *, dry_run: bool) -> None:
        handler = fields.get("handler", "")
        cls = HANDLERS.get(handler)
        if cls is None:
            events.error(f"不认识的类型 {handler!r}", kind="ui")
            return
        try:
            cfg = ActionConfig(**fields)
        except TypeError as e:
            events.error(f"字段填得不对：{e}", kind="ui")
            return
        action = cls(cfg)
        ctx = ActionContext(
            text=fields.get("id", ""),
            normalized=fields.get("id", ""),
            matched_alias=(fields.get("aliases") or [""])[0],
            dry_run=dry_run,
        )

        def work() -> None:
            try:
                res = action.execute(ctx)
            except Exception as e:  # noqa: BLE001
                events.error(f"{cfg.id} 抛出异常：{type(e).__name__}: {e}", kind="action")
                return
            events.emit(
                "ok" if res.ok else "error",
                f"{'[试运行] ' if dry_run else ''}{cfg.id}：{res.describe()}",
                kind="action",
            )

        self.background(work, name="voice-ctl-tryaction")

    # ------------------------------------------------------------------ #
    # 杂项
    # ------------------------------------------------------------------ #

    def clear_log_badge(self) -> None:
        self._badge = 0

    def on_close(self) -> None:
        try:
            self.engine.close()
        except Exception:  # noqa: BLE001
            pass
        events.info("界面已关闭", kind="ui")
        self.bus.flush(0.4)
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover
            pass


def build_window(
    cfg: AppConfig,
    config_path: Path | None,
    *,
    dry_run: bool = False,
    root: tk.Misc | None = None,
) -> App:
    """建窗口并完成主题初始化。调用方负责 mainloop()。

    `root` 可以传入一个已有的 Toplevel：测试里反复 Tk()/destroy() 会让
    Tcl 反复 Init/Finalize，几次之后连自己的库都找不到了
    （"Can't find a usable init.tcl"）。共用一个解释器就没这问题，
    顺带也让整轮 UI 测试快一个数量级。
    """
    owns_root = root is None
    if owns_root:
        root = tk.Tk()
    root.title(f"voice-ctl {__version__}")
    # theme.init 必须先跑：它才会从实际显示器算出 SCALE，之后 S() 才是对的。
    # 反过来的话在 150% 缩放的屏幕上窗口只有应有大小的三分之二，内容直接被裁掉。
    # 传入已有 root 时也要调——字体表和 ttk 样式是模块级的，不初始化就是空的。
    theme.init(root)
    if owns_root:
        root.minsize(S(940), S(600))
        root.geometry(f"{S(1080)}x{S(720)}")
    theme.apply_dark_titlebar(root)
    app = App(root, cfg, config_path)
    app.dry_run = dry_run
    app.engine.dry_run = dry_run
    app.build()
    return app
