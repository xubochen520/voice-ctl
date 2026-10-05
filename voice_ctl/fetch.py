"""模型下载。CLI 和 UI 共用，避免「命令行能下、界面里下不了」。

进度走事件总线而不是 print：界面要显示进度条、命令行要显示百分比，
两者拿到的应该是同一串事件。
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from . import events

HF_BASE = (
    "https://huggingface.co/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/resolve/main"
)

MODEL_FILES: list[tuple[str, float]] = [
    ("model.int8.onnx", 226.0),
    ("tokens.txt", 0.3),
    ("test_wavs/zh.wav", 0.17),
    ("test_wavs/en.wav", 0.22),
]

CHUNK = 1 << 20


class DownloadError(Exception):
    """下载失败。消息里要带上「哪个文件、为什么」，以及镜像建议。"""


def mirror_hint() -> str:
    return (
        "网络不通时可用 HF 镜像：把环境变量 HF_ENDPOINT 设成 https://hf-mirror.com 后重试。"
    )


def download_asr_model(
    target: str | Path,
    *,
    force: bool = False,
    bus: events.EventBus | None = None,
    should_stop=None,  # noqa: ANN001 - 可调用对象，返回 True 就中止
) -> list[str]:
    """把 SenseVoice int8 下到 target。返回失败的文件名列表（空 = 全成功）。

    逐文件 .part 落盘再改名：中途断网/关窗口不会留下一个"看起来存在但其实
    是半个"的模型文件——那种文件会让后续启动报一个完全看不懂的 ONNX 错误。
    """
    bus = bus or events.get_bus()
    dest_root = Path(target).expanduser().resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    bus.emit("info", f"下载 SenseVoice int8 → {dest_root}", kind="download")
    bus.emit("info", "来源：HuggingFace csukuangfj/sherpa-onnx-sense-voice-…-int8-2025-09-09",
             kind="download")

    failed: list[str] = []
    for rel, approx_mb in MODEL_FILES:
        if should_stop is not None and should_stop():
            bus.emit("warn", "下载已取消", kind="download")
            return failed
        dest = dest_root / Path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not force and dest.is_file() and dest.stat().st_size > 1024:
            bus.emit(
                "info",
                f"跳过 {rel}（已存在 {dest.stat().st_size / 1024 / 1024:.1f} MB）",
                kind="download",
            )
            continue

        url = f"{HF_BASE}/{rel}"
        tmp = dest.with_suffix(dest.suffix + ".part")
        bus.emit("info", f"下载 {rel}（约 {approx_mb} MB）…", kind="download")
        try:
            _fetch(url, tmp, bus, rel)
            tmp.replace(dest)
            bus.emit(
                "ok",
                f"{rel} 完成（{dest.stat().st_size / 1024 / 1024:.1f} MB）",
                kind="download",
            )
        except Exception as e:  # noqa: BLE001 - 网络异常种类太多，统一收口
            tmp.unlink(missing_ok=True)
            bus.emit("error", f"{rel} 下载失败：{e}", kind="download")
            failed.append(rel)

    if failed:
        bus.emit("error", f"以下文件没下成：{', '.join(failed)}。{mirror_hint()}", kind="download")
    else:
        bus.emit("ok", "识别模型就绪。", kind="download")
    return failed


def _fetch(url: str, tmp: Path, bus: events.EventBus, label: str) -> None:
    """带进度上报的单文件下载。每 10% 报一次，避免刷屏。"""
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        step = max(1, total // 10) if total else 0
        next_mark = step
        with tmp.open("wb") as fh:
            while chunk := resp.read(CHUNK):
                fh.write(chunk)
                done += len(chunk)
                if step and done >= next_mark:
                    pct = done * 100 // total
                    bus.emit(
                        "info",
                        f"{label} {pct}%（{done / 1024 / 1024:.0f}/{total / 1024 / 1024:.0f} MB）",
                        kind="download",
                        percent=pct,
                    )
                    next_mark += step


__all__ = ["HF_BASE", "MODEL_FILES", "DownloadError", "download_asr_model", "mirror_hint"]
