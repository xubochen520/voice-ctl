"""探测脚本：验证 SenseVoice 的实际行为（编码、热词、性能）。

不是产品代码，是开发期的实证工具。输出写到 UTF-8 文件，避开控制台编码坑。
"""

from __future__ import annotations

import io
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models" / "sense-voice-int8"
OUT = ROOT / "scripts" / "probe_asr.out.txt"


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as f:
        rate = f.getframerate()
        ch = f.getnchannels()
        width = f.getsampwidth()
        raw = f.readframes(f.getnframes())
    if rate != 16000:
        raise ValueError(f"expected 16000 Hz, got {rate}")
    if ch != 1:
        raise ValueError(f"expected mono, got {ch} channels")
    if width != 2:
        raise ValueError(f"expected 16-bit, got {width * 8}-bit")
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def build(language: str = "zh", use_itn: bool = True):
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(MODEL_DIR / "model.int8.onnx"),
        tokens=str(MODEL_DIR / "tokens.txt"),
        num_threads=2,
        language=language,
        use_itn=use_itn,
        debug=False,
        provider="cpu",
    )


def run(rec, samples: np.ndarray, hotwords: str | None = None):
    t0 = time.perf_counter()
    stream = rec.create_stream(hotwords=hotwords)
    stream.accept_waveform(16000, samples)
    rec.decode_stream(stream)
    dt = (time.perf_counter() - t0) * 1000
    return stream.result, dt


def main() -> int:
    # 子进程探针模式：故意触发 C++ abort，用来留下证据。
    if "--hotword-probe" in sys.argv:
        rec = build(language="zh")
        samples = load_wav(MODEL_DIR / "test_wavs" / "zh.wav")
        stream = rec.create_stream(hotwords="微信\n钉钉")
        stream.accept_waveform(16000, samples)
        rec.decode_stream(stream)
        print("UNREACHABLE: hotwords accepted")
        return 0

    # 边跑边写：sherpa-onnx 的 C++ 层会直接 abort 进程，缓冲的输出会全部丢失。
    fh = OUT.open("w", encoding="utf-8")

    def say(*a):
        print(*a)
        print(*a, file=fh)
        fh.flush()

    say("=" * 70)
    say("sherpa-onnx", sherpa_onnx.__version__)
    say("model dir:", MODEL_DIR)

    # 1. 基础识别
    rec = build(language="zh")
    say("\n[1] 基础识别（language=zh, use_itn=True）")
    for name in ("zh.wav", "en.wav"):
        wav = MODEL_DIR / "test_wavs" / name
        if not wav.exists():
            continue
        samples = load_wav(wav)
        r, dt = run(rec, samples)
        say(f"  {name}: dur={len(samples)/16000:.2f}s  infer={dt:.0f}ms  RTF={dt/1000/(len(samples)/16000):.3f}")
        say(f"    text  = {r.text}")
        say(f"    lang  = {r.lang!r}  emotion = {r.emotion!r}  event = {r.event!r}")
        say(f"    ntok  = {len(r.tokens)}")

    # 2. 热词是否真的生效 —— 已实证：sherpa-onnx 在 C++ 层直接 abort
    #    ("Only transducer models support contextual biasing.")，SenseVoice 是 CTC，
    #    不支持 hotwords。此处只在子进程里验证，绝不在主进程调用。
    say("\n[2] 热词（contextual biasing）支持情况")
    say("  结论：不支持。sherpa-onnx C++ 层原话 —— Only transducer models support")
    say("        contextual biasing。SenseVoice 属 CTC 模型，create_stream(hotwords=)")
    say("        会直接终止进程（不是 Python 异常，无法 try/except）。")
    say("  => 中文命令纠错必须靠 hr_dict_dir / rule_fsts（同音词替换）或自建归一化层。")
    hw_probe = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--hotword-probe"],
        capture_output=True, text=True, timeout=120,
    )
    say(f"  子进程实证: exit={hw_probe.returncode}  stderr_tail={hw_probe.stderr.strip()[-120:]!r}")

    # 3. 语种指定 vs 自动
    say("\n[3] language 参数影响（对 zh.wav）")
    samples = load_wav(MODEL_DIR / "test_wavs" / "zh.wav")
    for lang in ("zh", "auto", "en"):
        try:
            r2 = build(language=lang)
            rr, dt = run(r2, samples)
            say(f"  language={lang!r:8} -> {rr.text}   ({dt:.0f}ms)")
        except Exception as e:  # noqa: BLE001
            say(f"  language={lang!r} 失败: {e}")

    fh.close()
    say(f"\n已写入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
