"""用扬声器播放 TTS 音频 + 麦克风录回，验证完整音频链路。

这是唯一能端到端验证「录音 → 识别」的自动化手段（TTS 直连识别绕过了麦克风）。
代价：依赖房间声学环境和扬声器音量，结果不如直连稳定。所以它的定位是
**冒烟测试**（证明这条链路是通的），不是精度基准——精度看 bench_e2e.py。

用法：
    .venv\\Scripts\\python scripts\\test_mic_loopback.py
    .venv\\Scripts\\python scripts\\test_mic_loopback.py --source 04 --seconds 3
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voice_ctl.config import load_config  # noqa: E402
from voice_ctl.normalize import NormalizeConfig, Normalizer  # noqa: E402
from voice_ctl.recorder import Recorder, RecorderError  # noqa: E402

TTS_DIR = ROOT / "assets" / "tts"


def amplify(src: Path, dst: Path, gain: float) -> Path:
    """把 TTS 音频放大后另存，让扬声器外放足够响。"""
    with wave.open(str(src), "rb") as f:
        rate, ch, width, n = f.getframerate(), f.getnchannels(), f.getsampwidth(), f.getnframes()
        raw = f.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) * gain
    a = np.clip(a, -32768, 32767).astype("<i2")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dst), "wb") as f:
        f.setnchannels(ch)
        f.setsampwidth(width)
        f.setframerate(rate)
        f.writeframes(a.tobytes())
    return dst


def play_async(path: Path) -> threading.Thread:
    import sounddevice as sd

    with wave.open(str(path), "rb") as f:
        rate = f.getframerate()
        n = f.getnframes()
        a = np.frombuffer(f.readframes(n), dtype="<i2").astype(np.float32) / 32768.0

    def run() -> None:
        sd.play(a, rate)
        sd.wait()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="04", help="用哪个 TTS 样本（默认 04 = 截屏）")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--gain", type=float, default=3.0, help="外放增益")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    manifest = json.loads((TTS_DIR / "manifest.json").read_text(encoding="utf-8"))
    item = next((m for m in manifest if m["file"].startswith(f"{int(args.source):02d}")), None)
    if item is None:
        print(f"✗ manifest 里没有 {args.source}")
        return 2

    src = TTS_DIR / item["file"]
    loud = amplify(src, TTS_DIR / "_loud.wav", args.gain)

    from voice_ctl.asr import Asr

    asr = Asr(
        cfg.model_path(),
        language=cfg.model.language,
        use_itn=cfg.model.use_itn,
        num_threads=cfg.model.num_threads,
        provider=cfg.model.provider,
    )
    print("加载模型 ...", end="", flush=True)
    asr.load()
    print(" ok")

    norm = Normalizer(
        NormalizeConfig(
            strip_prefixes=cfg.match.strip_prefixes,
            strip_suffixes=cfg.match.strip_suffixes,
            inline_fillers=cfg.match.inline_fillers,
        )
    )

    rec = Recorder(device=cfg.audio.device or None, max_duration_ms=int(args.seconds * 1000) + 2000)
    print(f"\n源音频   : {item['file']}  「{item['text']}」  期望动作 {item['expect'] or '(不命中)'}")
    print(f"外放增益 : x{args.gain}    录音 {args.seconds}s")

    try:
        print("\n按回车开始（会先用扬声器播放，同时麦克风录音）...")
        input()
    except EOFError:
        pass

    try:
        rec.start()
    except RecorderError as e:
        print(f"✗ {e}")
        return 2

    player = play_async(loud)
    time.sleep(args.seconds)
    r = rec.stop()
    player.join(timeout=2)

    print(f"\n录音结果 : {r.summary()}")
    if r.rms > 0:
        print(f"峰值/RMS : {r.peak / r.rms:.1f}   （明显高于 1 才像语音；接近 1 说明只是平坦底噪）")

    reason = r.gate_reason(cfg.audio.min_peak)
    if reason:
        print(f"⚠ {reason} —— 这段会被静音过滤丢弃")
        print(f"  阈值 [audio].min_peak = {cfg.audio.min_peak}")
    else:
        print(f"✓ 通过静音过滤（阈值 min_peak={cfg.audio.min_peak}）")

    out = ROOT / "assets" / "mic_loopback.wav"
    pcm = (r.samples * 32767.0).clip(-32768, 32767).astype("<i2")
    with wave.open(str(out), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(pcm.tobytes())

    res = asr.transcribe(r.samples)
    heard = res.text
    print(f"\n识别结果 : {heard!r}   ({res.summary()})")
    print(f"归一化后 : {norm.normalize(heard)!r}")

    from voice_ctl.actions import build_registry
    from voice_ctl.matcher import Matcher

    matcher = Matcher(cfg.enabled_actions, normalizer=norm, threshold=cfg.match.threshold)
    m = matcher.best(heard)
    got = m.action_id if m else None
    want = item["expect"] or None

    print(f"匹配动作 : {got or '未命中'}" + (f"  score={m.score:.2f} {m.strategy}" if m else ""))
    print(f"期望动作 : {want or '未命中'}")

    if got == want:
        print("\n✓ 麦克风回环测试通过 —— 录音、识别、匹配三段都通了")
        rc = 0
    else:
        print("\n✗ 不匹配。注意这是房间声学 + 外放音量的综合结果，")
        print("  若识别文本已正确而匹配错，那是匹配层问题；若识别文本就不对，")
        print("  多半是回放太轻/环境太吵，可调大 --gain 或靠近麦克风重试。")
        rc = 1

    _ = build_registry  # 保持导入可见
    print(f"\n录音已存 {out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
