"""模型下载：完整性、续传、镜像回退。

起点是一次实测：服务器声明 200 万字节、只发 70 万就断开，旧版 `_fetch` 正常返回
并把半截文件改名成最终文件，之后永远显示「已存在，跳过」。这里用本地 HTTP 服务器
把各种"网络不老实"的情形都复现一遍。
"""

from __future__ import annotations

import hashlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from voice_ctl import events, fetch
from voice_ctl.fetch import DownloadError, Expected, download_asr_model

PAYLOAD = bytes(range(256)) * 4000  # 1,024,000 字节，内容有结构，错位一定能被发现
EXPECT = Expected(len(PAYLOAD), hashlib.sha256(PAYLOAD).hexdigest())
FILES = [("blob.bin", 1.0)]


class Server:
    def __init__(self) -> None:
        self.payload = PAYLOAD
        self.honor_range = True
        self.truncate_to: int | None = None
        """第一次请求只发这么多字节就断开（之后恢复正常）。"""
        self.always_status: int | None = None
        self.requests: list[str | None] = []
        self._first = True

        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:  # noqa: ANN002
                pass

            def do_GET(self) -> None:  # noqa: N802
                rng = self.headers.get("Range")
                outer.requests.append(rng)
                if outer.always_status:
                    self.send_response(outer.always_status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = outer.payload
                start = 0
                status = 200
                if rng and outer.honor_range:
                    start = int(rng.split("=")[1].split("-")[0])
                    if start >= len(body):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(body)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
                chunk = body[start:]
                self.send_response(status)
                self.send_header("Content-Length", str(len(chunk)))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                self.end_headers()
                if outer._first and outer.truncate_to is not None:
                    outer._first = False
                    self.wfile.write(chunk[: outer.truncate_to])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)  # 声明了更长，却提前断开
                    return
                outer._first = False
                self.wfile.write(chunk)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(
            target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True
        ).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def server():  # noqa: ANN201
    s = Server()
    yield s
    s.close()


@pytest.fixture()
def bus() -> events.EventBus:
    return events.EventBus()


def run(tmp_path: Path, server_url: str | list[str], bus: events.EventBus, **kw):  # noqa: ANN003, ANN201
    hosts = [server_url] if isinstance(server_url, str) else server_url
    return download_asr_model(
        tmp_path, bus=bus, hosts=hosts, files=FILES, expected={"blob.bin": EXPECT},
        repo_path="r", **kw,
    )


def texts(bus: events.EventBus) -> str:
    bus.flush()
    return "\n".join(e.text for e in bus.snapshot())


# --------------------------------------------------------------------------- #


def test_complete_download_is_verified_and_part_is_gone(server, tmp_path, bus):  # noqa: ANN001
    assert run(tmp_path, server.url, bus) == []
    out = tmp_path / "blob.bin"
    assert out.read_bytes() == PAYLOAD
    assert not (tmp_path / "blob.bin.part").exists()


def test_truncated_download_is_not_accepted(server, tmp_path, bus):  # noqa: ANN001
    """这是起点：旧版会把 70 万字节的半截文件当成功。"""
    server.truncate_to = 400_000
    failed = run(tmp_path, server.url, bus)
    assert failed == ["blob.bin"]
    assert not (tmp_path / "blob.bin").exists(), "半截文件绝不能出现在最终路径上"
    part = tmp_path / "blob.bin.part"
    assert part.exists() and part.stat().st_size == 400_000, "已下载的部分要保留，给续传用"
    assert "下载不完整" in texts(bus)


def test_resume_continues_from_the_part_file(server, tmp_path, bus):  # noqa: ANN001
    server.truncate_to = 400_000
    assert run(tmp_path, server.url, bus) == ["blob.bin"]
    server.requests.clear()
    assert run(tmp_path, server.url, bus) == []
    assert server.requests == ["bytes=400000-"], "第二次应当带 Range 从断点接着要"
    assert (tmp_path / "blob.bin").read_bytes() == PAYLOAD, "续传拼出来的必须和原文件逐字节一致"
    assert "继续" in texts(bus)


def test_server_that_ignores_range_restarts_cleanly(server, tmp_path, bus):  # noqa: ANN001
    """不支持 Range 的服务器会回 200 整个文件：不能把整个文件追加到半截文件后面。"""
    server.truncate_to = 300_000
    run(tmp_path, server.url, bus)
    server.honor_range = False
    assert run(tmp_path, server.url, bus) == []
    assert (tmp_path / "blob.bin").read_bytes() == PAYLOAD


def test_hash_mismatch_deletes_the_bad_data(server, tmp_path, bus):  # noqa: ANN001
    server.payload = PAYLOAD[:-1] + bytes([PAYLOAD[-1] ^ 0xFF])  # 同样长，最后一个字节不同
    failed = run(tmp_path, server.url, bus)
    assert failed == ["blob.bin"]
    assert not (tmp_path / "blob.bin").exists()
    assert not (tmp_path / "blob.bin.part").exists(), "校验不过的数据续传只会越续越坏，必须删"
    assert "校验没通过" in texts(bus)


def test_skip_hash_env_still_checks_size(server, tmp_path, bus, monkeypatch):  # noqa: ANN001
    monkeypatch.setenv(fetch.SKIP_HASH_ENV, "1")
    server.payload = PAYLOAD[:-1] + bytes([PAYLOAD[-1] ^ 0xFF])
    assert run(tmp_path, server.url, bus) == [], "上游换了文件时允许跳过 sha256"
    server.payload = PAYLOAD[:-10]
    (tmp_path / "blob.bin").unlink()
    assert run(tmp_path, server.url, bus) == ["blob.bin"], "但大小不对仍然必须拒绝"


def test_existing_truncated_final_file_is_detected_and_redownloaded(server, tmp_path, bus):  # noqa: ANN001
    """旧版本留下的半截文件：不能再信「文件存在」。"""
    (tmp_path / "blob.bin").write_bytes(PAYLOAD[:50_000])
    assert run(tmp_path, server.url, bus) == []
    assert (tmp_path / "blob.bin").read_bytes() == PAYLOAD
    assert "不完整" in texts(bus)


def test_existing_good_file_is_skipped_without_network(server, tmp_path, bus):  # noqa: ANN001
    (tmp_path / "blob.bin").write_bytes(PAYLOAD)
    server.requests.clear()
    assert run(tmp_path, server.url, bus) == []
    assert server.requests == [], "校验通过的文件不该再发请求"
    assert "校验通过" in texts(bus)


def test_force_redownloads_and_ignores_stale_part(server, tmp_path, bus):  # noqa: ANN001
    (tmp_path / "blob.bin.part").write_bytes(b"garbage" * 10)
    assert run(tmp_path, server.url, bus, force=True) == []
    assert (tmp_path / "blob.bin").read_bytes() == PAYLOAD


def test_part_already_complete_gets_416_and_is_verified(server, tmp_path, bus):  # noqa: ANN001
    """上次其实下完了、只是没来得及改名：服务器会对越界的 Range 回 416。"""
    (tmp_path / "blob.bin.part").write_bytes(PAYLOAD)
    assert run(tmp_path, server.url, bus) == []
    assert (tmp_path / "blob.bin").read_bytes() == PAYLOAD


def test_oversized_part_is_discarded(server, tmp_path, bus):  # noqa: ANN001
    (tmp_path / "blob.bin.part").write_bytes(PAYLOAD + b"extra")
    assert run(tmp_path, server.url, bus) == []
    assert (tmp_path / "blob.bin").read_bytes() == PAYLOAD


def test_falls_back_to_next_site_when_first_is_unreachable(server, tmp_path, bus):  # noqa: ANN001
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"  # 绑完即关，端口没人听
    assert run(tmp_path, [dead, server.url], bus) == []
    assert (tmp_path / "blob.bin").read_bytes() == PAYLOAD
    assert "下载失败" in texts(bus), "第一个站点失败要留痕，不然用户不知道为什么慢"


def test_http_error_from_first_site_falls_through(server, tmp_path, bus):  # noqa: ANN001
    bad = Server()
    bad.always_status = 503
    try:
        assert run(tmp_path, [bad.url, server.url], bus) == []
    finally:
        bad.close()
    assert (tmp_path / "blob.bin").read_bytes() == PAYLOAD


def test_all_sites_failing_reports_and_keeps_going(server, tmp_path, bus):  # noqa: ANN001
    server.always_status = 500
    assert run(tmp_path, server.url, bus) == ["blob.bin"]
    t = texts(bus)
    assert "没下成" in t and "HF_ENDPOINT" in t


def test_should_stop_aborts_mid_file_and_keeps_part(server, tmp_path, bus, monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(fetch, "CHUNK", 64 * 1024)  # 让这 1MB 的载荷分成多个块，块之间才有机会检查
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # 第一次是文件之间的检查；之后是块之间

    failed = run(tmp_path, server.url, bus, should_stop=stop)
    assert failed == ["blob.bin"]
    assert not (tmp_path / "blob.bin").exists()
    assert "取消" in texts(bus)


def test_progress_events_carry_percent(server, tmp_path, bus):  # noqa: ANN001
    assert run(tmp_path, server.url, bus) == []
    bus.flush()
    pcts = [e.data["percent"] for e in bus.snapshot() if "percent" in e.data]
    assert pcts == sorted(pcts), "进度应当单调不减"


def test_endpoints_honours_hf_endpoint(monkeypatch):  # noqa: ANN001
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    assert fetch.endpoints() == list(fetch.HF_HOSTS)
    monkeypatch.setenv("HF_ENDPOINT", "https://my.mirror/")
    assert fetch.endpoints() == ["https://my.mirror"], "用户明确指定了就只用它，且去掉末尾斜杠"


def test_pinned_model_hash_matches_what_huggingface_publishes():
    """固化值的来源：HF 对 resolve 请求返回的 X-Linked-Size / X-Linked-ETag，
    与本地能正常加载的 model.int8.onnx 逐字节一致（2026-10-05 核对）。"""
    e = fetch.EXPECTED["model.int8.onnx"]
    assert e.size == 237_115_547
    assert e.sha256 == "12ca1a2ae7ecf3e0019ef2822307ee0b5cadc9196569e379b4c4026f8205276d"


def test_real_model_on_disk_if_present_passes_the_pinned_check():
    """开发机上有模型时，顺手确认固化值没写错（没有模型就跳过）。"""
    p = Path(__file__).resolve().parent.parent / "models" / "sense-voice-int8" / "model.int8.onnx"
    if not p.is_file():
        pytest.skip("本机没有模型文件")
    assert fetch.check_file(p, fetch.EXPECTED["model.int8.onnx"]) is None


def test_check_file_messages_are_actionable(tmp_path):  # noqa: ANN001
    p = tmp_path / "x"
    assert "不存在" in fetch.check_file(p, EXPECT)
    p.write_bytes(b"abc")
    assert "大小不对" in fetch.check_file(p, EXPECT)
    assert "太小" in fetch.check_file(p, None)


def test_download_error_is_an_exception_with_message():
    assert "x" in str(DownloadError("x"))
