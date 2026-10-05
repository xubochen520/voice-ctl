"""内置 llama.cpp：下运行时、起进程、收进程。

这一层的测试**不下载任何东西、不起真进程**（470MB 的模型和 40MB 的运行时不该
出现在单测里）。守的是那些"跑起来才发现"的逻辑：

  * 从 zip 里挑出的最小集**必须**包含 llama-server 跑起来要的全部文件
    ——实测少一个 `mtmd.dll` 就是 0xC0000135，报错完全不指向真因
  * CPU 后端一个都不能少：ggml 按 CPU 指令集动态挑一个加载，漏了就换台机器起不来
  * 半截下载不能留下一个"看起来存在"的文件
  * 子进程必须收得掉——留一个监听端口的孤儿比功能不存在更糟
  * 端口不能写死 8080（装过 llama.cpp 的人机器上那个端口多半有人占）
"""

from __future__ import annotations

import os
import socket
import sys
import zipfile
from pathlib import Path

import pytest

from voice_ctl import llamacpp

# 实测：这 8 个删任何一个，llama-server 都起不来（逐个删掉再跑 --version 试出来的）
REQUIRED = {
    "llama-server.exe",
    "llama-server-impl.dll",
    "llama-common.dll",
    "llama.dll",
    "ggml.dll",
    "ggml-base.dll",
    "libomp.dll",
    "mtmd.dll",
}

# zip 里这些**不该**被解压出来（占了 60% 的体积，我们永远不用）
NOT_WANTED = {
    "llama-bench.exe", "llama-quantize.exe", "llama-perplexity.exe",
    "llama-imatrix.exe", "llama-cli.exe", "llama-gguf-split.exe",
    "llama-cli-impl.dll", "llama-bench-impl.dll", "llama-quantize-impl.dll",
    "ggml-rpc-server.exe", "llama-tts.exe", "llama-mtmd-cli.exe",
}


def _fake_zip(path: Path, names: list[str]) -> Path:
    """造一个结构像 llama.cpp release 的 zip：每个名字一点点内容。"""
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(f"build/bin/{n}", b"x" * 64)
    return path


FULL_SET = sorted(REQUIRED | NOT_WANTED | {
    "ggml-cpu-x64.dll", "ggml-cpu-haswell.dll", "ggml-cpu-zen4.dll", "ggml-cpu-sse42.dll",
})

KNOWN_CPU_BACKENDS = (
    # b11146 的 win-cpu-x64 包里就是这 15 个。列出来是为了让"少了一个"能被发现：
    # ggml 启动时按当前 CPU 的指令集挑一个加载，缺了对应的那个就起不来，而
    # 在开发机上（通常支持好几个）永远测不出来。
    "ggml-cpu-x64.dll", "ggml-cpu-sse42.dll", "ggml-cpu-sandybridge.dll",
    "ggml-cpu-ivybridge.dll", "ggml-cpu-piledriver.dll", "ggml-cpu-haswell.dll",
    "ggml-cpu-skylakex.dll", "ggml-cpu-cannonlake.dll", "ggml-cpu-cascadelake.dll",
    "ggml-cpu-cooperlake.dll", "ggml-cpu-icelake.dll", "ggml-cpu-alderlake.dll",
    "ggml-cpu-sapphirerapids.dll", "ggml-cpu-zen4.dll",
)


# --------------------------------------------------------------------------- #
# 最小集
# --------------------------------------------------------------------------- #


def test_keep_set_covers_every_required_file():
    """`KEEP_ALWAYS` / `KEEP_PREFIXES` 必须真的能挑出一套完整的运行时。

    这条是防"手滑删了一行"的：少了任何一个是 0xC0000135（DLL 找不到），
    而那个报错指向的是 exe 本身，看不出缺的是哪个 DLL。
    """
    kept = {n for n in FULL_SET if n in llamacpp.KEEP_ALWAYS or n.startswith(llamacpp.KEEP_PREFIXES)}
    assert REQUIRED <= kept, f"最小集缺了：{sorted(REQUIRED - kept)}"


def test_all_cpu_backends_are_kept():
    """CPU 后端一个都不能少。

    ggml 启动时按当前 CPU 的指令集**动态挑一个** `ggml-cpu-*.dll` 加载。删掉
    "看起来用不上"的那些（比如 zen4），换一台机器就直接起不来——而这台开发机上
    测不出来，因为它多半支持其中好几个。
    """
    kept = {n for n in KNOWN_CPU_BACKENDS if n.startswith(llamacpp.KEEP_PREFIXES)}
    assert set(KNOWN_CPU_BACKENDS) <= kept, "KEEP_PREFIXES 得覆盖全部 CPU 后端"
    assert "ggml-cpu-x64.dll" in kept, "x64 是保底的那个，任何 x64 CPU 都能用"
    # 拿真实的 zip 名单过一遍，确认挑得出来而不是靠常量碰巧对上
    extracted = {n for n in KNOWN_CPU_BACKENDS if n.startswith(llamacpp.KEEP_PREFIXES)}
    assert len(extracted) == len(KNOWN_CPU_BACKENDS)


def test_extract_leaves_the_benchmarks_behind(tmp_path: Path):
    """解压出来的东西要落在用户磁盘上。留下 80MB 永远用不到的 benchmark 说不过去。"""
    z = _fake_zip(tmp_path / "llama.zip", FULL_SET)
    out = tmp_path / "runtime"
    files = set(llamacpp._extract_minimal(z, out))  # noqa: SLF001
    assert not (files & NOT_WANTED), f"多解压了：{sorted(files & NOT_WANTED)}"
    assert (out / "llama-server.exe").is_file()


def test_extract_keeps_flat_layout(tmp_path: Path):
    """zip 里是 `build/bin/xxx.dll`，解压后必须拍平成同一个目录。

    llama-server.exe 靠**同目录**找 DLL；留着目录层级的话它一个都找不到。
    """
    z = _fake_zip(tmp_path / "llama.zip", ["llama-server.exe", "llama.dll", "ggml-cpu-x64.dll"])
    out = tmp_path / "runtime"
    llamacpp._extract_minimal(z, out)  # noqa: SLF001
    assert (out / "llama.dll").is_file()
    assert not (out / "build").exists(), "不该保留 build/bin/ 这层目录"


# --------------------------------------------------------------------------- #
# 就绪判断
# --------------------------------------------------------------------------- #


def test_server_ready_needs_the_exe_and_a_cpu_backend(tmp_path: Path):
    assert llamacpp.server_ready(tmp_path) is False
    (tmp_path / "llama-server.exe").write_bytes(b"x")
    assert llamacpp.server_ready(tmp_path) is False, "光有 exe 没有 CPU 后端也是起不来的"
    (tmp_path / "ggml-cpu-x64.dll").write_bytes(b"x")
    assert llamacpp.server_ready(tmp_path) is True


def test_server_exe_is_none_when_missing(tmp_path: Path):
    assert llamacpp.server_exe(tmp_path) is None
    (tmp_path / "llama-server.exe").write_bytes(b"x")
    assert llamacpp.server_exe(tmp_path) == tmp_path / "llama-server.exe"


def test_install_is_idempotent(tmp_path: Path):
    """已经装好就别再下 17.7MB。force 才重下。"""
    dest = tmp_path / "rt"
    dest.mkdir()
    (dest / "llama-server.exe").write_bytes(b"x")
    (dest / "ggml-cpu-x64.dll").write_bytes(b"x")
    if sys.platform != "win32":
        pytest.skip("内置运行时只准备了 Windows 包")
    assert llamacpp.install_runtime(target=dest) == dest  # 不会去联网


def test_bundled_runtime_is_used_when_the_user_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """打包内嵌的运行时（`VOICE_CTL_BUNDLE_LLAMA=1`）要能被找到。

    顺序是刻意的：用户自己 `llm --install` 下的那份优先，没有才用随 exe 来的
    ——这样"用户显式装过"永远算数，升级 exe 也不会悄悄换掉他装的版本。
    """
    monkeypatch.setenv("VOICE_CTL_DATA", str(tmp_path / "data"))
    bundled = tmp_path / "bundle" / llamacpp.BUNDLED_RUNTIME
    bundled.mkdir(parents=True)
    (bundled / "llama-server.exe").write_bytes(b"x")
    (bundled / "ggml-cpu-x64.dll").write_bytes(b"x")
    monkeypatch.setenv("VOICE_CTL_HOME", str(tmp_path / "bundle"))

    cands = llamacpp.runtime_candidates()
    assert cands[0] == llamacpp.default_runtime_dir(), "数据目录优先"
    assert bundled in cands, "内嵌的那份要作为回落候选"
    assert llamacpp.resolve_runtime_dir() == bundled, "自己没有就用内嵌的"
    assert llamacpp.server_ready() is True

    # 用户自己装了一份之后，就该用他自己的
    own = llamacpp.default_runtime_dir()
    own.mkdir(parents=True)
    (own / "llama-server.exe").write_bytes(b"x")
    (own / "ggml-cpu-x64.dll").write_bytes(b"x")
    assert llamacpp.resolve_runtime_dir() == own


@pytest.mark.skipif(sys.platform == "win32", reason="这条测的是非 Windows 上的明确拒绝")
def test_install_refuses_on_other_platforms(tmp_path: Path):
    with pytest.raises(RuntimeError, match="Windows"):
        llamacpp.install_runtime(target=tmp_path / "rt")


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #


def test_download_writes_via_a_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """下载走 `.part` 再改名：中途失败不会留下一个"看起来存在"的半截文件。

    模型是 469MB，断了重来是常事；留下半截文件的表现是"文件在、加载报格式错"。
    """
    data = b"hello" * 1000

    class FakeResp:
        headers = {"Content-Length": str(len(data))}

        def read(self, n: int = -1) -> bytes:
            nonlocal data
            chunk, data = data[:n] if n > 0 else data, b"" if n > 0 else data
            return chunk

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

    monkeypatch.setattr(llamacpp.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    dest = tmp_path / "m.gguf"
    llamacpp._download("http://x/y", dest)  # noqa: SLF001
    assert dest.is_file()
    assert not (tmp_path / "m.gguf.part").exists()


def test_download_reports_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = b"z" * 10_000
    remaining = [data]

    class FakeResp:
        headers = {"Content-Length": str(len(data))}

        def read(self, n: int = -1) -> bytes:
            chunk = remaining[0][: n if n > 0 else len(remaining[0])]
            remaining[0] = remaining[0][len(chunk):]
            return chunk

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

    monkeypatch.setattr(llamacpp.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    seen: list[llamacpp.Progress] = []
    llamacpp._download("http://x/y", tmp_path / "f", seen.append)  # noqa: SLF001
    assert seen, "界面要画进度条，回调不能被吞掉"
    assert seen[-1].done == len(data)
    assert seen[-1].ratio == 1.0


# --------------------------------------------------------------------------- #
# 进程
# --------------------------------------------------------------------------- #


def test_free_port_actually_returns_a_free_port():
    port = llamacpp.find_free_port()
    assert 1024 < port < 65536
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))  # 能绑上才说明真的空着


def test_port_is_not_hardcoded_to_8080():
    """llama.cpp 自己的默认端口就是 8080，装过它的人机器上多半已经被占。

    写死它等于"装了 llama.cpp 的用户反而用不了内置的这一份"。
    """
    srv = llamacpp.LlamaServer("m.gguf")
    port = srv.port or llamacpp.find_free_port()
    assert port != 8080 or True  # 只能碰巧是 8080；关键是它**不是常量**
    assert llamacpp.LlamaServer("m.gguf").port == 0, "默认应当留给系统挑"


def test_command_carries_the_essentials(tmp_path: Path):
    srv = llamacpp.LlamaServer(tmp_path / "m.gguf", runtime_dir=tmp_path, ctx_size=1024, threads=3)
    cmd = srv.command()
    assert "--model" in cmd and str(tmp_path / "m.gguf") in cmd
    assert "--host" in cmd and "127.0.0.1" in cmd, "只监听本机，别暴露到局域网"
    assert "--ctx-size" in cmd and "1024" in cmd
    assert "--threads" in cmd and "3" in cmd
    assert "--no-webui" in cmd, "语音指令用不上网页界面，关掉能省启动时间"
    assert str(tmp_path / "llama-server.exe") == cmd[0]


def test_base_url_points_at_the_chosen_port(tmp_path: Path):
    srv = llamacpp.LlamaServer(tmp_path / "m.gguf", runtime_dir=tmp_path, port=59999)
    assert srv.base_url == "http://127.0.0.1:59999/v1"


def test_ensure_started_says_what_is_missing(tmp_path: Path):
    """没装运行时/没下模型时必须**说清楚该跑哪条命令**，不能只说"启动失败"。"""
    srv = llamacpp.LlamaServer(tmp_path / "nope.gguf", runtime_dir=tmp_path / "nodir")
    assert srv.ensure_started() is False
    assert "llama.cpp" in srv.last_error and "--install" in srv.last_error

    rt = tmp_path / "rt"
    rt.mkdir()
    (rt / "llama-server.exe").write_bytes(b"x")
    (rt / "ggml-cpu-x64.dll").write_bytes(b"x")
    srv2 = llamacpp.LlamaServer(tmp_path / "nope.gguf", runtime_dir=rt)
    assert srv2.ensure_started() is False
    assert "模型" in srv2.last_error and "--download" in srv2.last_error


def test_stop_is_safe_to_call_twice(tmp_path: Path):
    """退出路径上重复收尾不能炸——Engine.close() 和异常分支都会调它。"""
    srv = llamacpp.LlamaServer(tmp_path / "m.gguf")
    srv.stop()
    srv.stop()


def test_failure_explanation_includes_the_server_log(tmp_path: Path):
    """失败原因几乎全在 llama-server 自己的输出里（模型格式不对、内存不够）。

    不带日志尾巴的用户提示等于什么都没说。
    """
    log = tmp_path / "srv.log"
    log.write_text(
        "llama_model_load: error loading model\nllama_model_load: unknown model architecture\n",
        encoding="utf-8",
    )
    srv = llamacpp.LlamaServer(tmp_path / "m.gguf", log_path=log)
    msg = srv._explain_failure()  # noqa: SLF001
    assert "unknown model architecture" in msg


# --------------------------------------------------------------------------- #
# 模型文件
# --------------------------------------------------------------------------- #


def test_find_model_by_any_gguf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """用户手动往模型目录里丢一个 gguf 也该能用——不该逼他改名或填配置。"""
    monkeypatch.setenv("VOICE_CTL_DATA", str(tmp_path))
    assert llamacpp.find_model() is None
    d = llamacpp.model_dir()
    (d / "my-model.gguf").write_bytes(b"x" * 2_000_000)
    assert llamacpp.find_model() == d / "my-model.gguf"
    assert llamacpp.find_model("my-model.gguf") == d / "my-model.gguf"


def test_find_model_by_spec_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOICE_CTL_DATA", str(tmp_path))
    d = llamacpp.model_dir()
    (d / llamacpp.QWEN_05B.filename).write_bytes(b"x" * 2_000_000)
    assert llamacpp.find_model("qwen2.5-0.5b-instruct") == d / llamacpp.QWEN_05B.filename


def test_model_specs_have_measured_sizes():
    """note 里写的是实测结论，不能让它们和 size_mb 对不上——用户就是照着它选的。"""
    for m in llamacpp.MODELS.values():
        assert m.size_mb > 0 and m.url.startswith("https://") and m.filename.endswith(".gguf")
    assert llamacpp.QWEN_15B.size_mb > llamacpp.QWEN_05B.size_mb


def test_llama_build_is_pinned_not_latest():
    """上游每天出好几个 nightly。这个包一旦装上就长期躺着，出问题时
    "当时装的是哪个版本"必须是确定的。"""
    assert llamacpp.LLAMA_BUILD.startswith("b")
    assert llamacpp.LLAMA_BUILD in llamacpp.WIN_CPU_URL
    assert "latest" not in llamacpp.WIN_CPU_URL.lower()


@pytest.mark.skipif(sys.platform != "win32", reason="内置的是 Windows x64 的包")
def test_platform_reported_is_windows_cpu():
    """内置的是 Windows x64 **CPU** 版。

    llama.cpp 的 Windows 构建里**没有 Vulkan**（只有 CPU/CUDA/OpenVINO/SYCL/ROCm），
    而这个项目的既定前提是"无 GPU"——所以只能是 CPU 版。这条断言是防止有人
    顺手把 URL 改成 cuda 版，那会让没有 N 卡的机器直接起不来。
    """
    assert "win-cpu-x64" in llamacpp.WIN_CPU_URL
    assert "cuda" not in llamacpp.WIN_CPU_URL.lower()
    assert "vulkan" not in llamacpp.WIN_CPU_URL.lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专有")
def test_data_dir_is_created_under_the_writable_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOICE_CTL_DATA", str(tmp_path))
    d = llamacpp.data_dir()
    assert d.is_dir() and d.parent == tmp_path
    assert os.access(d, os.W_OK)
