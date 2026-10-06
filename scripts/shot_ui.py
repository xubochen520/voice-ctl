"""逐页截图当前界面（纯 ctypes，不依赖 Pillow）。

用法：python scripts/_tmp_shots.py [输出前缀] [页面,页面...]
产物是 _ui_<前缀>_<页面>.png（已在 .gitignore 里）。
"""
from __future__ import annotations

import ctypes
import shutil
import struct
import sys
import time
import zlib
from ctypes import wintypes
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def grab(hwnd: int) -> np.ndarray:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, 2)  # PW_RENDERFULLCONTENT
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
    return arr[:, :, [2, 1, 0]].copy()


def grab_screen(x: int, y: int, w: int, h: int) -> np.ndarray:
    """屏幕上一块区域（含所有顶层窗口，弹出层也在内）。窗口必须真的显示在最上面。"""
    hdc = user32.GetDC(0)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    gdi32.BitBlt(mdc, 0, 0, w, h, hdc, x, y, 0x00CC0020 | 0x40000000)  # SRCCOPY | CAPTUREBLT
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(0, hdc)
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
    return arr[:, :, [2, 1, 0]].copy()


def write_png(path: Path, rgb: np.ndarray) -> None:
    h, w, _ = rgb.shape
    raw = np.zeros((h, 1 + w * 3), dtype=np.uint8)
    raw[:, 1:] = rgb.reshape(h, w * 3)
    comp = zlib.compress(raw.tobytes(), 6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", comp) + chunk(b"IEND", b"")
    path.write_bytes(png)


def pump(root, seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        root.update()
        time.sleep(0.01)


def main() -> None:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "cur"
    only = sys.argv[2].split(",") if len(sys.argv) > 2 else None

    from voice_ctl import events
    from voice_ctl.config import load_config
    from voice_ctl.ui import theme
    from voice_ctl.ui.window import build_window

    theme.enable_dpi_awareness()
    cfgdir = ROOT / "_shotcfg"
    cfgdir.mkdir(exist_ok=True)
    cfg_path = cfgdir / "config.toml"
    shutil.copyfile(ROOT / "config.toml", cfg_path)

    app = build_window(load_config(cfg_path), cfg_path)
    root = app.root
    root.geometry("+40+30")
    root.attributes("-topmost", True)
    pump(root, 0.6)

    # 灌一点日志，免得日志页是空的
    events.info("界面启动，配置文件：" + str(cfg_path), kind="ui")
    events.debug("深色标题栏：已生效", kind="ui")
    events.ok("识别模型已加载（1170ms）", kind="engine")
    events.info("手动测试：打开记事本", kind="ui")
    events.ok("open.notepad：已启动 notepad.exe", kind="action")
    events.warn("麦克风电平偏低：峰值 0.012", kind="audio")
    events.error("下载出错：ConnectionError: 网络不通", kind="download")
    events.info("听到  : \"打开微信\"\n命中  : open.wechat  via intent\n执行  : 已启动", kind="pipeline")
    pump(root, 0.4)

    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    tabs = only or ["run", "logs", "hotkey", "actions", "settings", "about"]
    for key in tabs:
        app.show_tab(key)
        pump(root, 0.9)
        if key == "about":
            app.tabs["about"].run_doctor()
            pump(root, 0.3)
        img = grab(hwnd)
        out = ROOT / f"_ui_{prefix}_{key}.png"
        write_png(out, img)
        print(out.name, img.shape[1], "x", img.shape[0])

    app.on_close()


if __name__ == "__main__":
    main()
