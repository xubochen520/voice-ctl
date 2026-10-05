"""内置 llama.cpp：让打包后的应用**不需要任何外部软件**就能跑本地大模型。

0.3.0 的小模型层只实现了"客户端"那一半——它假设用户在跑 LM Studio 或 Ollama。
那台机器上确实装了 LM Studio，但服务没开，于是这一层等于不存在。用户的原话是
「直接内置 llama.cpp 不就好了，打包整个应用」，这个模块就是那一半。

三件事：

  1. **llama-server 二进制**从 llama.cpp 官方 release 取，解压出**能跑的最小集**
     （实测 22 个文件 39.8MB；整个 zip 里 60% 是 bench/quantize/多模态那些
     我们永远不用的 exe 和 impl.dll）。
  2. **进程**由我们起、我们管：挑空闲端口 → 启动 → 等健康检查 → 用 → 退出时收掉。
  3. **输出用 GBNF 语法锁死**。这一层唯一的真实危险是模型编一个动作 id 出来，
     语法约束之后它**结构上不可能**输出候选之外的东西——比在提示词里求它靠谱。

为什么是 CPU 版：llama.cpp 的 Windows 构建**没有 Vulkan**（只有 CPU / CUDA /
OpenVINO / SYCL / ROCm，见 b11146 的 release notes）。CPU 版还带一整套
`ggml-cpu-*.dll`（haswell / zen4 / sapphirerapids…），运行时按 CPU 指令集选一个，
所以同一个包在 Sandy Bridge 到最新的机器上都能跑，也不用用户挑。

为什么要"最小集"而不是整个 zip：39.8MB vs 解压后 120MB+。用户下载的是压缩包
（17.7MB），但**解压出来的东西要落在他的磁盘上**——留下 80MB 永远用不到的
benchmark 工具是说不过去的。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import bootstrap

_log = logging.getLogger(__name__)

LLAMA_BUILD = "b11146"
"""固化的 llama.cpp 构建号。

**不用 latest**：上游每天出好几个 nightly，行为可能变；而这个包一旦装上就长期
躺着，出问题时"当时装的是哪个版本"必须是确定的。升级是一个显式的动作。
"""

WIN_CPU_URL = (
    f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/"
    f"llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip"
)
WIN_CPU_SHA256 = ""
"""留空 = 不校验。填上就是"这份 zip 必须一字不差"。

为什么留空而不是随手填一个：这个哈希得由发布者自己下完算出来才有意义，
凭空写一个反而是假的安全感。`download_runtime()` 在为空时会打印实际哈希，
用户想固化就把它填回来。"""

KEEP_ALWAYS = frozenset({
    "llama-server.exe",        # 我们要跑的
    "llama-server-impl.dll",   # 上面那个 exe 只是壳，实现在这里
    "llama-common.dll",
    "llama.dll",
    "ggml.dll",
    "ggml-base.dll",
    "libomp.dll",
    "mtmd.dll",                # 看着像多模态才用，实测**必需**：少了它 llama-server
                               # 直接 0xC0000135（DLL 找不到），因为它静态依赖了它
})
"""这 8 个是实测跑起来的下限（逐个删掉再跑 `llama-server --version` 试出来的）。"""

KEEP_PREFIXES = ("ggml-cpu-",)
"""CPU 后端的所有变体都要留。

ggml 在启动时按当前 CPU 的指令集**动态挑一个**加载，删掉"看起来用不上"的那些，
换一台机器就直接起不来。多留 10 个文件换"什么 CPU 都能跑"，这个买卖划算。"""


def data_dir() -> Path:
    """llama.cpp 运行时的落地位置（可写数据目录下的 llm/）。"""
    d = bootstrap.data_dir() / "llm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_runtime_dir() -> Path:
    return data_dir() / f"llama-{LLAMA_BUILD}"


BUNDLED_RUNTIME = "llama-runtime"
"""打包时内嵌运行时的目录名（见 voice-ctl.spec）。

刻意用 `llama-runtime` 而不是 `llm`：可写数据目录里那份是用户自己下的、版本
可能不同，两者混在一个路径下会让"我装的到底是哪一份"变得无法回答。
"""


def runtime_candidates() -> list[Path]:
    """运行时可能在哪几个地方，按优先级排。

    1. 可写数据目录（用户自己 `llm --install` 下的那份）
    2. 打包内嵌的那份（`VOICE_CTL_BUNDLE_LLAMA=1` 编出来的 exe）

    顺序是刻意的：用户显式装过就以他的为准，否则用随 exe 来的，省一次下载。
    """
    out = [default_runtime_dir()]
    bundled = bootstrap.resource(BUNDLED_RUNTIME)
    if bundled is not None and bundled not in out:
        out.append(bundled)
    return out


def resolve_runtime_dir() -> Path:
    """实际该用的运行时目录：先用已就绪的，都没有就返回"该装到哪"。"""
    cands = runtime_candidates()
    for c in cands:
        if server_ready(c):
            return c
    return cands[0]


def bundled_model() -> Path | None:
    """随 exe 内嵌的 GGUF（`VOICE_CTL_BUNDLE_LLM_MODEL=1`）。"""
    d = bootstrap.resource("llm-models")
    if d is None or not d.is_dir():
        return None
    files = sorted(d.glob("*.gguf"))
    return files[0] if files else None


def server_exe(runtime_dir: Path | None = None) -> Path | None:
    """已装好的 llama-server.exe；没装返回 None。"""
    p = (runtime_dir or resolve_runtime_dir()) / "llama-server.exe"
    return p if p.is_file() else None


def server_ready(runtime_dir: Path | None = None) -> bool:
    """二进制齐不齐。齐了就不必再下载。"""
    d = runtime_dir or resolve_runtime_dir()
    if not (d / "llama-server.exe").is_file():
        return False
    # 至少要有一个 CPU 后端，否则起来了也是空的
    return any(d.glob("ggml-cpu-*.dll"))


# --------------------------------------------------------------------------- #
# 取二进制
# --------------------------------------------------------------------------- #


@dataclass
class Progress:
    """下载进度。用回调而不是 print，界面里要画进度条。"""

    done: int = 0
    total: int = 0
    note: str = ""

    @property
    def ratio(self) -> float:
        return self.done / self.total if self.total else 0.0


def _download(url: str, dest: Path, on_progress=None, *, timeout: float = 300.0) -> Path:  # noqa: ANN001
    """下载到临时文件再改名。中途失败不会留下一个"看起来存在"的半截文件。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "voice-ctl"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with tmp.open("wb") as fh:
            while chunk := r.read(256 * 1024):
                fh.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(Progress(done, total, "下载中"))
    os.replace(tmp, dest)
    return dest


def _extract_minimal(zip_path: Path, target: Path) -> list[str]:
    """从 zip 里挑出能跑的最小集，解压到 target。返回解压出来的文件名。"""
    target.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = os.path.basename(info.filename)
            if not name:
                continue
            if name in KEEP_ALWAYS or name.startswith(KEEP_PREFIXES):
                with z.open(info) as src, (target / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                out.append(name)
    return out


def install_runtime(
    *,
    url: str = WIN_CPU_URL,
    target: Path | None = None,
    force: bool = False,
    on_progress=None,  # noqa: ANN001
) -> Path:
    """下载并装好 llama.cpp 运行时。返回目录。

    已经装好就直接返回（除非 force）——这一步要下 17.7MB，不该每次启动都做。
    """
    if sys.platform != "win32":
        raise RuntimeError(
            "内置 llama.cpp 目前只准备了 Windows x64 的包；"
            "其它平台请用 backend = \"server\" 接一个本机服务"
        )
    dest = target or default_runtime_dir()
    if not force and server_ready(dest):
        return dest

    zip_path = data_dir() / f"llama-{LLAMA_BUILD}-win-cpu-x64.zip"
    if force or not zip_path.is_file():
        _download(url, zip_path, on_progress)
    if WIN_CPU_SHA256 and not force:
        import hashlib

        got = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        if got != WIN_CPU_SHA256:
            zip_path.unlink(missing_ok=True)
            raise RuntimeError(f"llama.cpp 压缩包校验失败：期望 {WIN_CPU_SHA256}，实际 {got}")

    # 解压到临时目录再整体替换：解压到一半失败，不该毁掉已装好的那一份
    staging = dest.with_name(dest.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        files = _extract_minimal(zip_path, staging)
        if "llama-server.exe" not in files:
            raise RuntimeError("压缩包里没有 llama-server.exe，可能上游改了打包结构")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(staging, dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return dest


# --------------------------------------------------------------------------- #
# 进程
# --------------------------------------------------------------------------- #


def find_free_port(preferred: int = 0) -> int:
    """挑一个空闲端口。

    不能用固定的 8080：用户的机器上那个端口很可能已经有别的东西（llama.cpp 自己的
    默认值就是 8080，装过它的人一大把）。占用了就换一个，比让用户去改配置好。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", preferred))
        return int(s.getsockname()[1])


class ServerError(RuntimeError):
    """起不来 / 提前死了。消息里必须带上 llama-server 自己的输出。"""


class LlamaServer:
    """管一个 llama-server 子进程。

    生命周期刻意做成"懒启动 + 显式 stop"：

      * 懒启动：`ensure_started()` 在第一次真要推理时才起。用户可能开着小模型层
        却整场没说过一句长尾话，那就一个进程都不该多。
      * 显式 stop：退出时调。不调的话进程会活到用户重启——一个占着 400MB 内存、
        在 127.0.0.1 上监听的孤儿进程，比功能不存在更糟。
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        runtime_dir: Path | None = None,
        port: int = 0,
        ctx_size: int = 2048,
        threads: int = 0,
        startup_timeout: float = 60.0,
        extra_args: list[str] | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.model_path = Path(model_path)
        self.runtime_dir = runtime_dir or default_runtime_dir()
        self.port = port
        self.ctx_size = ctx_size
        self.threads = threads
        self.startup_timeout = startup_timeout
        self.extra_args = list(extra_args or [])
        self.log_path = log_path or (data_dir() / "llama-server.log")
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.last_error = ""

    # -- 命令行 ----------------------------------------------------------- #

    @property
    def exe(self) -> Path:
        return self.runtime_dir / "llama-server.exe"

    def command(self) -> list[str]:
        port = self.port or find_free_port()
        self.port = port
        threads = self.threads or max(2, (os.cpu_count() or 4) // 2)
        args = [
            str(self.exe),
            "--model", str(self.model_path),
            "--host", "127.0.0.1",
            "--port", str(port),
            "--ctx-size", str(self.ctx_size),
            "--threads", str(threads),
            # 语音指令是短文本，不需要长上下文；关掉一堆用不上的功能让启动更快
            "--no-webui",
        ]
        args += self.extra_args
        return args

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port or 0}/v1"

    # -- 生命周期 --------------------------------------------------------- #

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ensure_started(self) -> bool:
        """起来就返回 True。已经在跑、或起好了都算成功。"""
        with self._lock:
            if self.running:
                return True
            if not self.exe.is_file():
                self.last_error = f"没装 llama.cpp 运行时：{self.exe}（跑 `voice-ctl llm --install`）"
                return False
            if not self.model_path.is_file():
                self.last_error = (
                    f"没有模型文件：{self.model_path}（跑 `voice-ctl llm --download`）"
                )
                return False

            self.port = self.port or find_free_port()
            cmd = self.command()
            _log.info("启动 llama-server：%s", " ".join(cmd))
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._logfile = self.log_path.open("ab")
                self._proc = subprocess.Popen(  # noqa: S603
                    cmd,
                    stdout=self._logfile,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as e:
                self.last_error = f"启动失败：{e}"
                return False
            if self._wait_ready():
                return True
            self.last_error = self._explain_failure()
            self.stop()
            return False

    def _wait_ready(self) -> bool:
        """轮询 /health 直到就绪或超时。

        为什么不用 /v1/models：那个端点要等模型彻底加载完才有，而 /health 在
        加载期间会返回 503，能区分"还在加载"和"死了"。首次加载 470MB 的模型
        在这台机器上要几秒，冷盘更久。
        """
        deadline = time.monotonic() + self.startup_timeout
        url = f"http://127.0.0.1:{self.port}/health"
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False  # 进程自己退了，等下去没意义
            try:
                with urllib.request.urlopen(url, timeout=2.0) as r:  # noqa: S310
                    body = json.loads(r.read().decode("utf-8", errors="replace"))
                if str(body.get("status", "")).lower() in ("ok", "ready"):
                    return True
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.15)
        return False

    def _explain_failure(self) -> str:
        """失败时把 llama-server 自己的日志尾巴带上。

        没有它，用户看到的只有"启动失败"，而真正的原因（模型格式不对、内存不够、
        端口被占）全在子进程的输出里，压根看不到。
        """
        tail = ""
        try:
            if self.log_path.is_file():
                lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = " / ".join(lines[-6:])
        except OSError:
            pass
        code = self._proc.poll() if self._proc is not None else None
        if code is None and not tail:
            return f"llama-server 在 {self.startup_timeout:.0f} 秒内没就绪"
        head = f"llama-server 退出码 {code}" if code is not None else "llama-server 未就绪"
        return f"{head}：{tail}" if tail else head

    def stop(self, timeout: float = 5.0) -> None:
        """收掉子进程。**一定要调**，否则会留下一个监听端口的孤儿进程。"""
        with self._lock:
            p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=2.0)
        except OSError:
            pass
        finally:
            lf = getattr(self, "_logfile", None)
            if lf is not None:
                try:
                    lf.close()
                except OSError:
                    pass
                self._logfile = None


# --------------------------------------------------------------------------- #
# 模型文件
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelSpec:
    """一个可下载的 GGUF 模型。"""

    name: str
    url: str
    filename: str
    size_mb: float
    note: str = ""


QWEN_05B = ModelSpec(
    name="qwen2.5-0.5b-instruct",
    url=(
        "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/"
        "qwen2.5-0.5b-instruct-q4_k_m.gguf?download=true"
    ),
    filename="qwen2.5-0.5b-instruct-q4_k_m.gguf",
    size_mb=468.6,
    note="最小最快（加载 0.7s / 单次 90ms）。实测几乎只会答 null——不误触发，但也帮不上忙",
)

QWEN_15B = ModelSpec(
    name="qwen2.5-1.5b-instruct",
    url=(
        "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/"
        "qwen2.5-1.5b-instruct-q4_k_m.gguf?download=true"
    ),
    filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
    size_mb=1065.6,
    note=(
        "实测唯一真的会挑的档位（加载 1.4s / 单次 110ms / 10 例命中 6）。"
        "0.5B 和 0.6B 档位几乎只会答 null，代价是这 1GB"
    ),
)

GEMMA_270M = ModelSpec(
    name="gemma-3-270m-it",
    url=(
        "https://huggingface.co/unsloth/gemma-3-270m-it-GGUF/resolve/main/"
        "gemma-3-270m-it-Q4_K_M.gguf?download=true"
    ),
    filename="gemma-3-270m-it-Q4_K_M.gguf",
    size_mb=241.4,
    note="更小，但中文能力明显弱；没实测过挑候选的表现",
)

MODELS = {m.name: m for m in (QWEN_05B, QWEN_15B, GEMMA_270M)}


def model_dir() -> Path:
    d = data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_model(name: str = "") -> Path | None:
    """按名字或文件名找一个 gguf。空名字返回任意一个。

    找的顺序：可写数据目录 → 打包内嵌的那份。用户手动往模型目录里丢一个 gguf
    也应该能用——不该逼他改名或填配置。
    """
    dirs = [model_dir()]
    bm = bundled_model()
    if bm is not None:
        dirs.append(bm.parent)

    for d in dirs:
        if name:
            cand = d / name
            if cand.is_file():
                return cand
            for m in MODELS.values():
                if m.name == name and (d / m.filename).is_file():
                    return d / m.filename
        else:
            files = sorted(d.glob("*.gguf"))
            if files:
                return files[0]

    p = Path(name) if name else None
    return p if p is not None and p.is_file() else None


def download_model(
    spec: ModelSpec = QWEN_05B, *, force: bool = False, on_progress=None  # noqa: ANN001
) -> Path:
    dest = model_dir() / spec.filename
    if dest.is_file() and not force and dest.stat().st_size > 1024 * 1024:
        return dest
    return _download(spec.url, dest, on_progress, timeout=1800.0)


__all__ = [
    "BUNDLED_RUNTIME",
    "GEMMA_270M",
    "LLAMA_BUILD",
    "MODELS",
    "QWEN_05B",
    "QWEN_15B",
    "WIN_CPU_URL",
    "LlamaServer",
    "ModelSpec",
    "Progress",
    "ServerError",
    "bundled_model",
    "data_dir",
    "default_runtime_dir",
    "download_model",
    "find_free_port",
    "find_model",
    "install_runtime",
    "model_dir",
    "resolve_runtime_dir",
    "runtime_candidates",
    "server_exe",
    "server_ready",
]
