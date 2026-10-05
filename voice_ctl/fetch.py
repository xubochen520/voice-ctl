"""模型下载。CLI 和 UI 共用，避免「命令行能下、界面里下不了」。

进度走事件总线而不是 print：界面要显示进度条、命令行要显示百分比，
两者拿到的应该是同一串事件。

三条硬约束（都是实测撞出来的）：

  1. **声明了 Content-Length 就必须收满。** `http.client` 对"连接中途断开"不抛
     异常（源码注释写着为了兼容性不抛 IncompleteRead），`read()` 只是悄悄返回
     空字节。实测：服务器声明 200 万字节、只发 70 万就断开，旧版 `_fetch` 照常
     返回并把半截文件改名成最终文件，之后永远显示"已存在，跳过"，直到 ONNX
     加载时报一个完全看不懂的错。
  2. **下完要校验。** 模型文件的大小和 sha256 是固化的（来自 Hugging Face 返回的
     `X-Linked-Size` / `X-Linked-ETag`，与本地能正常加载的那份逐字节一致）。
  3. **能续传、能换镜像。** 226MB 在不稳的网络上断一次很常见；
     huggingface.co 在国内经常连不上，连不上就别等满 2 分钟超时再放弃。
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import events

HF_REPO_PATH = (
    "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/resolve/main"
)
HF_HOSTS = ("https://huggingface.co", "https://hf-mirror.com")
HF_BASE = f"{HF_HOSTS[0]}/{HF_REPO_PATH}"
"""兼容旧名。"""

MODEL_FILES: list[tuple[str, float]] = [
    ("model.int8.onnx", 226.0),
    ("tokens.txt", 0.3),
    ("test_wavs/zh.wav", 0.17),
    ("test_wavs/en.wav", 0.22),
]

CHUNK = 1 << 20
CONNECT_TIMEOUT = 20.0
"""建立连接 / 每次读取的超时。短一点：连不上就该尽快换下一个镜像。"""


@dataclass(frozen=True)
class Expected:
    size: int
    sha256: str


# 只固化大文件：它才是"半截了没人知道"的那个。其余几个文件很小，下完就能看出来。
EXPECTED: dict[str, Expected] = {
    "model.int8.onnx": Expected(
        237115547,
        "12ca1a2ae7ecf3e0019ef2822307ee0b5cadc9196569e379b4c4026f8205276d",
    ),
}

SKIP_HASH_ENV = "VOICE_CTL_SKIP_HASH"
"""上游万一替换了文件，设成 1 跳过 sha256 校验（大小仍然会校验）。"""


class DownloadError(Exception):
    """下载失败。消息里要带上「哪个文件、为什么」，以及镜像建议。"""


class _Stopped(Exception):
    """调用方要求中止。"""


def endpoints() -> list[str]:
    """要依次尝试的站点。用户用 HF_ENDPOINT 指定了就只用它（那是明确的选择）。"""
    env = os.environ.get("HF_ENDPOINT", "").strip().rstrip("/")
    return [env] if env else list(HF_HOSTS)


def mirror_hint() -> str:
    return (
        "默认会依次尝试 huggingface.co 和 hf-mirror.com；"
        "想指定站点可把环境变量 HF_ENDPOINT 设成别的镜像后重试。"
        "已下载的部分会保留，重试时接着下。"
    )


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def check_file(path: Path, expect: Expected | None, *, hash_it: bool = True) -> str | None:
    """文件是否符合预期。返回 None 表示没问题，否则是一句人话的原因。"""
    if not path.is_file():
        return "文件不存在"
    size = path.stat().st_size
    if expect is None:
        return None if size > 1024 else f"只有 {size} 字节，太小了"
    if size != expect.size:
        return f"大小不对：{size} 字节，应为 {expect.size}"
    if hash_it and not os.environ.get(SKIP_HASH_ENV):
        actual = sha256_of(path)
        if actual != expect.sha256:
            return f"sha256 不一致：{actual[:12]}…，应为 {expect.sha256[:12]}…"
    return None


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def download_asr_model(
    target: str | Path,
    *,
    force: bool = False,
    bus: events.EventBus | None = None,
    should_stop=None,  # noqa: ANN001 - 可调用对象，返回 True 就中止
    hosts: list[str] | None = None,
    files: list[tuple[str, float]] | None = None,
    expected: dict[str, Expected] | None = None,
    repo_path: str = HF_REPO_PATH,
) -> list[str]:
    """把 SenseVoice int8 下到 target。返回失败的文件名列表（空 = 全成功）。

    逐文件 .part 落盘再改名：中途断网/关窗口不会留下一个"看起来存在但其实
    是半个"的模型文件。`.part` 会保留，下次接着下（HTTP Range）。
    """
    bus = bus or events.get_bus()
    files = MODEL_FILES if files is None else files
    expected = EXPECTED if expected is None else expected
    sites = hosts if hosts is not None else endpoints()
    dest_root = Path(target).expanduser().resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    bus.emit("info", f"下载 SenseVoice int8 → {dest_root}", kind="download")
    bus.emit("info", "来源：HuggingFace csukuangfj/sherpa-onnx-sense-voice-…-int8-2025-09-09",
             kind="download")

    failed: list[str] = []
    for rel, approx_mb in files:
        if should_stop is not None and should_stop():
            bus.emit("warn", "下载已取消", kind="download")
            return failed
        dest = dest_root / Path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        expect = expected.get(rel)

        if not force and dest.is_file():
            problem = check_file(dest, expect)
            if problem is None:
                bus.emit(
                    "info",
                    f"跳过 {rel}（已存在 {dest.stat().st_size / 1024 / 1024:.1f} MB"
                    + ("，校验通过）" if expect else "）"),
                    kind="download",
                )
                continue
            # 旧版本会把半截文件留在这里：不能再信"文件存在"
            bus.emit("warn", f"已有的 {rel} 不完整（{problem}），重新下载", kind="download")
            dest.unlink(missing_ok=True)

        tmp = dest.with_suffix(dest.suffix + ".part")
        if force:
            tmp.unlink(missing_ok=True)
        bus.emit("info", f"下载 {rel}（约 {approx_mb} MB）…", kind="download")

        last_error = ""
        done_ok = False
        for site in sites:
            url = f"{site.rstrip('/')}/{repo_path}/{rel}"
            try:
                _fetch(url, tmp, bus, rel, expect=expect, should_stop=should_stop)
            except _Stopped:
                bus.emit("warn", f"下载已取消（{rel} 已下载的部分保留，下次接着下）", kind="download")
                return failed + [rel]
            except Exception as e:  # noqa: BLE001 - 网络异常种类太多，统一收口
                last_error = f"{type(e).__name__}: {e}" if not isinstance(e, DownloadError) else str(e)
                host = site.split("//")[-1]
                bus.emit("warn", f"{rel} 从 {host} 下载失败：{last_error}", kind="download")
                continue
            tmp.replace(dest)
            bus.emit(
                "ok",
                f"{rel} 完成（{dest.stat().st_size / 1024 / 1024:.1f} MB）",
                kind="download",
            )
            done_ok = True
            break

        if not done_ok:
            bus.emit("error", f"{rel} 下载失败：{last_error or '没有可用的站点'}", kind="download")
            failed.append(rel)

    if failed:
        bus.emit("error", f"以下文件没下成：{', '.join(failed)}。{mirror_hint()}", kind="download")
    else:
        bus.emit("ok", "识别模型就绪。", kind="download")
    return failed


def _content_range_total(value: str | None) -> int | None:
    """'bytes 100-999/1000' → 1000。"""
    if not value or "/" not in value:
        return None
    tail = value.rsplit("/", 1)[1].strip()
    return int(tail) if tail.isdigit() else None


def _open(url: str, start: int, timeout: float):  # noqa: ANN202
    headers = {"User-Agent": "voice-ctl"}
    if start:
        headers["Range"] = f"bytes={start}-"
    return urllib.request.urlopen(  # noqa: S310
        urllib.request.Request(url, headers=headers), timeout=timeout
    )


def _fetch(
    url: str,
    tmp: Path,
    bus: events.EventBus,
    label: str,
    *,
    expect: Expected | None = None,
    should_stop=None,  # noqa: ANN001
    timeout: float = CONNECT_TIMEOUT,
) -> None:
    """带进度、断点续传、完整性校验的单文件下载。

    成功时 `tmp` 里就是完整且校验过的文件；失败时：
      * 网络断了 / 收不满 → 保留 `tmp`（下次从这里接着下）
      * 校验不过 → 删掉 `tmp`（里面是坏数据，续传只会越续越坏）
    """
    start = tmp.stat().st_size if tmp.is_file() else 0
    if expect is not None and start > expect.size:
        tmp.unlink()  # 比完整文件还大，肯定是坏的
        start = 0

    try:
        resp = _open(url, start, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 416 or not start:
            raise
        # Range 越界：服务器说"你要的起点已经在文件末尾之后"——多半是上次其实下完了
        if expect is not None and start == expect.size:
            _verify(tmp, expect, label)
            return
        tmp.unlink(missing_ok=True)
        start = 0
        resp = _open(url, 0, timeout)

    with resp:
        status = getattr(resp, "status", 200)
        length = int(resp.headers.get("Content-Length") or 0)
        if start and status == 206:
            total = _content_range_total(resp.headers.get("Content-Range")) or (start + length)
            mode = "ab"
            bus.emit("info", f"{label} 接着上次的 {start / 1024 / 1024:.0f} MB 继续", kind="download")
        else:
            # 没要求续传，或者服务器无视了 Range 直接给了整个文件：从头来
            start = 0
            total = length
            mode = "wb"

        done = start
        step = max(1, total // 10) if total else 0
        next_mark = (done // step + 1) * step if step else 0
        with tmp.open(mode) as fh:
            while chunk := resp.read(CHUNK):
                if should_stop is not None and should_stop():
                    raise _Stopped
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

    if total and done != total:
        raise DownloadError(
            f"{label} 下载不完整：收到 {done}/{total} 字节（连接中途断开）。"
            f"已保留 {done / 1024 / 1024:.0f} MB，重试会接着下"
        )
    if expect is not None:
        _verify(tmp, expect, label)


def _verify(tmp: Path, expect: Expected, label: str) -> None:
    problem = check_file(tmp, expect)
    if problem is not None:
        tmp.unlink(missing_ok=True)
        raise DownloadError(
            f"{label} 校验没通过（{problem}），已删除。"
            f"如果确定上游换了文件，设环境变量 {SKIP_HASH_ENV}=1 跳过 sha256 校验后重试"
        )


__all__ = [
    "EXPECTED",
    "HF_BASE",
    "HF_HOSTS",
    "HF_REPO_PATH",
    "MODEL_FILES",
    "DownloadError",
    "Expected",
    "check_file",
    "download_asr_model",
    "endpoints",
    "mirror_hint",
    "sha256_of",
]
