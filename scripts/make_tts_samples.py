"""生成中文命令测试音频（用 Windows 内置 SAPI TTS，不需要联网/额外安装）。

用途：给「短命令词识别」做一个可重复的基准。合成语音和真人语音有差距，
所以结果只用于**横向对比**（不同措辞 / 不同同音字 / 不同阈值），
不能当作真人准确率。

两个必须注意的点（都是实际踩过的坑）：
  1. PowerShell 输出在中文 Windows 上默认是 GBK，subprocess 用 text=True
     会按 GBK 解码并在遇到非法字节时抛 UnicodeDecodeError —— 必须显式
     encoding="utf-8"，并让 PowerShell 自己也用 UTF-8 输出。
  2. 中文文本不要走命令行参数（编码会被破坏），改用脚本文件 + 参数传文件路径。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "assets" / "tts"
PS_FILE = ROOT / "scripts" / "_tts_one.ps1"

# 覆盖真实场景：系统应用（一定验证到执行）、同音易错、口语化、负样本
PHRASES: list[dict[str, str]] = [
    {"text": "打开记事本", "expect": "open.notepad", "kind": "system"},
    {"text": "打开计算器", "expect": "open.calc", "kind": "system"},
    {"text": "打开任务管理器", "expect": "open.taskmgr", "kind": "system"},
    {"text": "打开资源管理器", "expect": "open.explorer", "kind": "system"},
    {"text": "截屏", "expect": "sys.screenshot", "kind": "system"},
    {"text": "静音", "expect": "sys.mute", "kind": "system"},
    {"text": "显示桌面", "expect": "sys.show_desktop", "kind": "system"},
    {"text": "音量加", "expect": "sys.volume_up", "kind": "system"},
    {"text": "音量减", "expect": "sys.volume_down", "kind": "system"},
    {"text": "打开记事本吧", "expect": "open.notepad", "kind": "colloquial"},
    {"text": "帮我打开计算器", "expect": "open.calc", "kind": "colloquial"},
    {"text": "请打开一下记事本", "expect": "open.notepad", "kind": "colloquial"},
    {"text": "把音量调小一点", "expect": "sys.volume_down", "kind": "colloquial"},
    {"text": "打开微信", "expect": "open.wechat", "kind": "thirdparty"},
    {"text": "打开威信", "expect": "open.wechat", "kind": "homophone"},
    {"text": "截个图", "expect": "sys.screenshot", "kind": "homophone"},
    {"text": "今天天气怎么样", "expect": None, "kind": "negative"},
    {"text": "帮我写一首诗", "expect": None, "kind": "negative"},
]

PS_SCRIPT = r"""
# 参数：-TextFile（UTF-8 文本文件，内容即要朗读的文字）-Out（输出 wav 路径）
param([Parameter(Mandatory=$true)][string]$TextFile,
      [Parameter(Mandatory=$true)][string]$Out)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Speech

$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8).Trim()
if ([string]::IsNullOrWhiteSpace($text)) { [Console]::Error.WriteLine('EMPTY_TEXT'); exit 2 }

$outFull = [System.IO.Path]::GetFullPath($Out)
$outDir = [System.IO.Path]::GetDirectoryName($outFull)
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $zh = $synth.GetInstalledVoices() |
          Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' } |
          Select-Object -First 1
    if (-not $zh) { [Console]::Error.WriteLine('NO_CHINESE_VOICE'); exit 3 }
    $synth.SelectVoice($zh.VoiceInfo.Name)
    $synth.Rate = 0

    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)

    $synth.SetOutputToWaveFile($outFull, $fmt)
    $synth.Speak($text)
    $synth.SetOutputToNull()
    Write-Output ("OK|" + $zh.VoiceInfo.Name + "|" + $outFull)
} finally {
    $synth.Dispose()
}
"""


def _run_ps(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """跑 PowerShell，统一按 UTF-8 解码（中文 Windows 默认 GBK 会炸）。"""
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def list_voices() -> list[str]:
    ps = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Speech; "
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | "
        "ForEach-Object { $_.VoiceInfo.Name + ' | ' + $_.VoiceInfo.Culture.Name }"
    )
    r = _run_ps(["-Command", ps], timeout=60)
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]


def synth(text: str, out: Path, tmp_dir: Path) -> str:
    """合成一句。中文经由 UTF-8 文件传递，避开命令行编码问题。"""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    txt = tmp_dir / f"{out.stem}.txt"
    txt.write_text(text, encoding="utf-8")
    r = _run_ps(["-File", str(PS_FILE), "-TextFile", str(txt), "-Out", str(out)])
    if r.returncode != 0 or not out.is_file():
        detail = (r.stderr or r.stdout or "").strip().replace("\n", " ")[:300]
        raise RuntimeError(f"exit={r.returncode} {detail}")
    return (r.stdout or "").strip()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PS_FILE.write_text(PS_SCRIPT, encoding="utf-8")

    voices = list_voices()
    print("已安装语音：")
    for v in voices:
        print(f"  {v}")
    if not any("zh" in v.lower() for v in voices):
        print("\n✗ 没有中文语音包。到「设置 → 时间和语言 → 语音」安装中文语音后重试。")
        return 3
    print()

    tmp_dir = OUT_DIR / "_txt"
    manifest: list[dict[str, str]] = []
    failed: list[str] = []
    for i, item in enumerate(PHRASES):
        text = item["text"]
        out = OUT_DIR / f"{i:02d}.wav"
        out.unlink(missing_ok=True)
        try:
            info = synth(text, out, tmp_dir)
        except RuntimeError as e:
            print(f"  ✗ {text}  -> {e}")
            failed.append(text)
            continue
        kb = out.stat().st_size / 1024
        print(f"  ✓ {i:02d}  {text:16} {kb:6.1f} KB")
        manifest.append(
            {
                "file": out.name,
                "text": text,
                "expect": item["expect"] or "",
                "kind": item["kind"],
            }
        )

    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n生成 {len(manifest)} 个音频 → {OUT_DIR}")
    print(f"清单 → {OUT_DIR / 'manifest.json'}")
    if failed:
        print(f"失败 {len(failed)} 个：{failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
