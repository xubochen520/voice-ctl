"""ASR：SenseVoice int8 封装（sherpa-onnx，CTC，纯 CPU）。

实测结论（scripts/probe_asr.py，本机 RTX4060 笔记本 / CPU 推理）：
  * 5.59s 音频 217ms，RTF≈0.039 —— 比实时快约 25 倍，CPU 完全够
  * use_itn=True 对命令场景正确（数字正常化）
  * **不支持 hotwords**：SenseVoice 是 CTC 模型，sherpa-onnx 的 C++ 层会打印
    "Only transducer models support contextual biasing." 并**直接 abort 进程**
    （不是 Python 异常，try/except 抓不住）。所以本模块绝不调用 hotwords 路径。
  * language 参数（zh/auto/en）对本模型输出无影响，实测三种设置结果完全一致；
    结果文本自带语言标记（如 <|yue|>）。
  * 纠错只能靠自己：见 normalize.py。
"""

from __future__ import annotations

import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
"""SenseVoice 固定要求 16kHz。"""


class AsrError(Exception):
    """识别失败。消息要说清是模型缺失、音频格式不对，还是别的。"""


@dataclass
class AsrResult:
    text: str
    language: str = ""
    emotion: str = ""
    event: str = ""
    duration_s: float = 0.0
    """音频时长。"""

    infer_ms: float = 0.0
    """纯推理耗时（不含模型加载）。"""

    tokens: int = 0

    @property
    def rtf(self) -> float:
        """实时率：推理秒数 / 音频秒数。越小越快，<1 表示比实时快。"""
        if self.duration_s <= 0 or self.infer_ms <= 0:
            return 0.0
        return (self.infer_ms / 1000.0) / self.duration_s

    def summary(self) -> str:
        return (
            f"{self.duration_s:.2f}s 音频 → {self.infer_ms:.0f}ms  "
            f"(RTF {self.rtf:.3f}, {self.tokens} tokens)  lang={self.language}"
        )


def _clean_tag(value: str) -> str:
    """SenseVoice 把 lang/emotion/event 返回成 '<|yue|>' 这种标记，剥成 'yue'。"""
    v = (value or "").strip()
    if v.startswith("<|") and v.endswith("|>"):
        return v[2:-2]
    return v


def load_wave_mono16k(path: str | Path) -> np.ndarray:
    """读 16-bit / 16kHz / 单声道 wav，返回 float32 [-1, 1]。

    只接受这一种格式：不满足就报错，而不是偷偷重采样——静默重采样会让
    识别质量悄悄变差，是最难查的那类 bug。
    """
    p = Path(path)
    if not p.is_file():
        raise AsrError(f"音频文件不存在：{p}")
    with wave.open(str(p), "rb") as f:
        rate, ch, width, frames = (
            f.getframerate(),
            f.getnchannels(),
            f.getsampwidth(),
            f.getnframes(),
        )
        raw = f.readframes(frames)
    if rate != SAMPLE_RATE:
        raise AsrError(f"{p.name}: 采样率必须是 {SAMPLE_RATE}，实际 {rate}")
    if ch != 1:
        raise AsrError(f"{p.name}: 必须是单声道，实际 {ch} 声道")
    if width != 2:
        raise AsrError(f"{p.name}: 必须是 16-bit，实际 {width * 8}-bit")
    if not raw:
        raise AsrError(f"{p.name}: 音频为空")
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


class Asr:
    """SenseVoice 识别器。加载一次，常驻复用——冷启动要 1-3 秒，不能每次重建。"""

    def __init__(
        self,
        model_dir: str | Path,
        *,
        language: str = "auto",
        use_itn: bool = True,
        num_threads: int = 2,
        provider: str = "cpu",
    ) -> None:
        self.model_dir = Path(model_dir).expanduser().resolve()
        self.language = language
        self.use_itn = use_itn
        self.num_threads = num_threads
        self.provider = provider
        self._rec = None
        self.load_ms = 0.0

    # -- 资源 ------------------------------------------------------------- #

    def _paths(self) -> tuple[Path, Path]:
        model = self.model_dir / "model.int8.onnx"
        tokens = self.model_dir / "tokens.txt"
        if not model.is_file() or not tokens.is_file():
            missing = [p.name for p in (model, tokens) if not p.is_file()]
            raise AsrError(
                f"模型文件缺失：{', '.join(missing)}（目录 {self.model_dir}）。"
                "跑 `voice-ctl download` 自动下载，或看 README 的手动下载说明。"
            )
        return model, tokens

    def load(self) -> None:
        """构建识别器。幂等：已加载则直接返回。"""
        if self._rec is not None:
            return
        model, tokens = self._paths()
        try:
            import sherpa_onnx
        except ImportError as e:  # pragma: no cover - 装机问题
            raise AsrError(
                "sherpa-onnx 没装。执行：.venv\\Scripts\\pip install sherpa-onnx"
            ) from e

        t0 = time.perf_counter()
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model),
            tokens=str(tokens),
            num_threads=self.num_threads,
            language=self.language,
            use_itn=self.use_itn,
            debug=False,
            provider=self.provider,
        )
        self.load_ms = (time.perf_counter() - t0) * 1000

    @property
    def loaded(self) -> bool:
        return self._rec is not None

    # -- 识别 ------------------------------------------------------------- #

    def transcribe(self, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> AsrResult:
        """识别一段 float32 [-1,1] 波形。"""
        if sample_rate != SAMPLE_RATE:
            raise AsrError(f"输入必须是 {SAMPLE_RATE}Hz，实际 {sample_rate}Hz")
        self.load()
        assert self._rec is not None

        audio = np.ascontiguousarray(samples, dtype=np.float32)
        duration = len(audio) / SAMPLE_RATE

        t0 = time.perf_counter()
        stream = self._rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        self._rec.decode_stream(stream)
        infer_ms = (time.perf_counter() - t0) * 1000

        r = stream.result
        raw_tokens = list(getattr(r, "tokens", []) or [])
        return AsrResult(
            text=(r.text or "").strip(),
            language=_clean_tag(getattr(r, "lang", "")),
            emotion=_clean_tag(getattr(r, "emotion", "")),
            event=_clean_tag(getattr(r, "event", "")),
            duration_s=duration,
            infer_ms=infer_ms,
            tokens=len(raw_tokens),
        )

    def transcribe_file(self, path: str | Path) -> AsrResult:
        return self.transcribe(load_wave_mono16k(path))
