"""主窗口：左侧导航栏（品牌 + 页面 + 电源键）+ 内容区。

线程模型很简单，只有一条规则：**Tk 只能被主线程碰**。
worker 线程（识别、预检、下载）只往事件总线里丢事件或往一个槽里放结果，
主线程每 120ms 轮询一次再更新界面。这样就不需要任何锁，也不会出现
「窗口偶尔卡死」那类只在用户机器上复现的问题。

窗口里没有顶栏和底栏：状态与启停都收进了侧栏底部的电源键（任何页面都在），
内容区因此多出一整条高度——以前底栏就是被内容挤出窗口的那一块。
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import __version__, bootstrap, events
from ..actions import HANDLERS, ActionContext
from ..config import ActionConfig, AppConfig, ConfigError, load_config
from ..confedit import ConfigEditor
from ..runner import Engine
from . import gfx, inputs, theme, widgets as W
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
    ("run", "运行", "mic"),
    ("logs", "日志", "list"),
    ("hotkey", "快捷键", "keyboard"),
    ("actions", "功能", "bolt"),
    ("settings", "设置", "sliders"),
    ("about", "关于", "info"),
]

RAIL_W = 100


class App:
    """窗口控制器。页面通过它访问引擎、配置和状态栏。"""

    def __init__(self, root: tk.Misc, cfg: AppConfig, config_path: Path | None) -> None:
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

    def build(self, *, show_first: bool = False) -> None:
        inputs.install_wheel_router(self.root)
        self._set_icon()
        self._build_rail()
        self._build_content()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show_tab("run")
        if show_first:
            # Engine.prepare() 要 ~1 秒（第一次扫「开始菜单」要起一个 PowerShell）。
            # 不先画出来的话，用户双击之后要干等一秒多才看到窗口，像是没启动
            self.root.update()
        self.engine.prepare()
        # 映射之后再设一次标题栏配色：有些 Windows 版本在窗口可见前设了不生效
        self.root.after(80, self._late_polish)
        self._poll()

    def _late_polish(self) -> None:
        events.debug(
            f"标题栏配色：{'已生效' if theme.apply_titlebar(self.root) else '未生效（只影响观感）'}",
            kind="ui",
        )

    def _set_icon(self) -> None:
        """窗口/任务栏图标。不设的话标题栏上是 Tk 默认的那根羽毛。"""
        try:
            imgs = [gfx.app_icon(self.root, s) for s in (16, 24, 32, 48, 64)]
            self.root.iconphoto(True, *imgs)
        except tk.TclError:  # pragma: no cover - 个别环境不支持
            pass

    def _build_rail(self) -> None:
        rail = tk.Frame(self.root, bg=P["chassis"], width=S(RAIL_W))
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        self._rail = rail

        # 底部先 pack：窗口矮的时候被裁掉的该是中间的空白，而不是电源键
        foot = tk.Frame(rail, bg=P["chassis"])
        foot.pack(side="bottom", fill="x", pady=(0, S(18)))
        self._power = W.PowerButton(foot, command=self.toggle_engine)
        self._power.pack()
        self._state_lbl = tk.Label(foot, text="待命", bg=P["chassis"], fg=P["ink3"], font=theme.FONTS["nav_b"])
        self._state_lbl.pack(pady=(S(4), 0))
        self._hot_lbl = tk.Label(foot, text="", bg=P["chassis"], fg=P["ink3"], font=theme.FONTS["caption"])
        self._hot_lbl.pack()

        # 品牌：标志永远在；字标和版本号在侧栏太矮放不下时收起（见 _on_rail_configure）
        self._brand = tk.Frame(rail, bg=P["chassis"])
        self._brand.pack(pady=(S(24), S(22)))
        tk.Label(self._brand, image=gfx.app_icon(self.root, S(40)), bg=P["chassis"], bd=0).pack()
        self._brand_text = tk.Frame(self._brand, bg=P["chassis"])
        self._brand_text.pack()
        tk.Label(self._brand_text, text="voice-ctl", bg=P["chassis"], fg=P["ink"],
                 font=theme.FONTS["brand_s"]).pack(pady=(S(8), 0))
        tk.Label(self._brand_text, text=__version__, bg=P["chassis"], fg=P["ink3"],
                 font=theme.FONTS["caption"]).pack()
        self._compact = False
        rail.bind("<Configure>", self._on_rail_configure)

        for key, label, glyph in NAV:
            item = W.NavItem(rail, label, icon=glyph, command=lambda k=key: self.show_tab(k))
            item.pack(pady=S(2))
            self._nav[key] = item

    def _on_rail_configure(self, e: tk.Event) -> None:
        """侧栏矮于 ~670（设计像素）就进紧凑模式：收起字标和版本号，不然最后一项「关于」会被电源键盖住。
        完整布局要 ~656 高：品牌 130 + 六个导航 408 + 电源键区 118。"""
        compact = e.height < S(670)
        if compact == self._compact:
            return
        self._compact = compact
        if compact:
            self._brand_text.pack_forget()
            self._brand.pack_configure(pady=(S(14), S(10)))
        else:
            self._brand_text.pack()
            self._brand.pack_configure(pady=(S(24), S(22)))

    def _build_content(self) -> None:
        self._content = tk.Frame(self.root, bg=P["chassis"])
        self._content.pack(side="left", fill="both", expand=True)
        self.tabs = {
            "run": RunTab(self._content, self),
            "logs": LogsTab(self._content, self),
            "hotkey": HotkeyTab(self._content, self),
            "actions": ActionsTab(self._content, self),
            "settings": SettingsTab(self._content, self),
            "about": AboutTab(self._content, self),
        }

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
        live = bool(eng.running and eng.capture_state()[0])
        self._power.set_state(eng.state, live=live)
        if live:
            text, color = "正在录音", P["live_ink"]
        else:
            text = eng.state_label
            color = {"running": P["ok"], "loading": P["warn"], "error": P["err"]}.get(eng.state, P["ink3"])
        if self._state_lbl.cget("text") != text:
            self._state_lbl.configure(text=text)
        self._state_lbl.configure(fg=color)
        hot = W.hotkey_text(eng.hotkey_spec)
        if self._hot_lbl.cget("text") != hot:
            self._hot_lbl.configure(text=hot)
            self._power.set_tooltip(f"开始 / 停止监听 · 热键 {hot}")
        self._nav["logs"].set_badge(self._badge)

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
    show_first: bool = False,
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
    # 传入已有 root 时也要调——字体表和配色是模块级的，不初始化就是空的。
    theme.init(root)
    if owns_root:
        root.minsize(S(980), S(640))
        root.geometry(f"{S(1120)}x{S(760)}")
    theme.apply_titlebar(root)
    app = App(root, cfg, config_path)
    app.dry_run = dry_run
    app.engine.dry_run = dry_run
    app.build(show_first=show_first)
    return app
