"""图形界面。

    voice-ctl ui        打开界面（日志 / 快捷键 / 功能 / 设置 / 关于）

为什么用 tkinter 而不是 PySide / Web 界面：这是个**后台常驻的小工具**，
不该为了一个设置窗口多背 150MB 的 Qt，也不该让用户为改个热键去开浏览器。
tkinter 是标准库自带的，打包后只多几 MB，双击 exe 就有窗口。

开销上的取舍：界面只在打开时占内存，监听热键本身仍然是那个 350MB 的
常驻进程，识别照旧跑在同一进程里——界面上看到的日志就是真实发生的事，
不存在「UI 显示的和实际跑的不是一套」。
"""

from __future__ import annotations

from pathlib import Path

from .. import bootstrap, events
from ..config import ConfigError, load_config


def available() -> tuple[bool, str]:
    """tkinter 在不在。精简的 Python 发行版可能没带。"""
    try:
        import tkinter  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"这个 Python 没有 tkinter：{e}"
    return True, "ok"


def run_ui(config_path: str | Path | None = None, *, dry_run: bool = False) -> int:
    ok, why = available()
    if not ok:
        print(f"✗ {why}")
        return 2

    from . import theme

    # 必须在 Tk() 之前声明 DPI 感知，否则 125%/150% 缩放下整个界面是糊的
    theme.enable_dpi_awareness()

    from ..bootstrap import resolve_config

    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        print(f"✗ 配置有问题：{e}")
        print("  改好 config.toml 再打开界面；或者先跑 `voice-ctl doctor` 看是哪一行。")
        return 2

    path = Path(config_path).expanduser().resolve() if config_path else cfg.source
    if path is None:
        path = resolve_config(None)
    if path is None:
        path = bootstrap.data_dir() / "config.toml"

    # 控制台只在「是给我们单独开的」时候才藏起来；从终端启动时那是用户的窗口，
    # 藏掉等于把人家终端弄没了。判定在 bootstrap.hide_own_console 里。
    bootstrap.hide_own_console()

    events.info(f"界面启动，配置文件：{path}", kind="ui")

    from .window import build_window

    try:
        app = build_window(cfg, path, dry_run=dry_run, show_first=True)
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"✗ 打不开界面：{type(e).__name__}: {e}")
        return 1

    try:
        app.root.mainloop()
    except KeyboardInterrupt:
        app.on_close()
    finally:
        events.get_bus().flush(0.5)
    return 0


__all__ = ["available", "run_ui"]
