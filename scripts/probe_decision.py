"""验证 Laya 语义层：从中文口语文本映射到动作。

这是 decision.py 的实证工具。目的是回答两个问题：
  1. ONNXAgent 到底怎么实例化？（实测签名是 ONNXAgent(dir, onnx_path=...)，不是 from_pretrained）
  2. 中文短句上它给的动作和置信度是什么水平？
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models" / "laya-onnx" / "multilingual"

# 口语化说法，故意不含动作别名——这些正是第 0 层匹配打不中、需要语义层的输入
CASES = [
    ("有个文件要改一下", "open.notepad"),
    ("算个数", "open.calc"),
    ("我想聊个天", "open.wechat"),
    ("把声音关小", "sys.volume_down"),
    ("帮我截个图", "sys.screenshot"),
    ("屏幕别让人看了", "sys.lock"),
    ("随便说点什么", None),
]

QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "用户想执行哪个操作？只选最贴近的一个。",
        "criteria": {
            "open.notepad": "打开记事本写字、改文件、记录",
            "open.calc": "打开计算器算数",
            "open.wechat": "打开微信聊天、发消息",
            "sys.screenshot": "截屏、截图",
            "sys.volume_down": "调低音量、声音小一点",
            "sys.lock": "锁定屏幕",
        },
    }
}


def main() -> int:
    onnx = MODEL_DIR / "model_int8.onnx"
    if not onnx.is_file():
        print(f"✗ 找不到 {onnx}")
        return 2

    from laya.onnx_agent import ONNXAgent

    print(f"模型目录 : {MODEL_DIR}")
    print(f"ONNX 图  : {onnx.name}  ({onnx.stat().st_size / 1024 / 1024:.1f} MB)")
    print("加载中 ...", end="", flush=True)
    t0 = time.perf_counter()
    agent = ONNXAgent(str(MODEL_DIR), onnx_path=str(onnx))
    print(f" ok（{time.perf_counter() - t0:.1f}s）")

    print("\n" + "=" * 88)
    print(f"{'输入':<18} {'判定动作':<18} {'置信度':>8} {'耗时':>8}  {'对不对'}")
    print("-" * 88)

    ok = total = 0
    for text, want in CASES:
        t = time.perf_counter()
        try:
            r = agent.predict({"text": text}, QUESTIONS)
        except Exception as e:  # noqa: BLE001
            print(f"{text!r:<18} 预测失败: {type(e).__name__}: {str(e)[:40]}")
            continue
        dt = (time.perf_counter() - t) * 1000

        ans = (r.get("answers") or {}).get("action") or {}
        got = ans.get("choice")
        conf = ans.get("answer_confidence", ans.get("confidence"))
        conf_s = f"{conf:.3f}" if isinstance(conf, (int, float)) else str(conf)

        if want is not None:
            total += 1
            if got == want:
                ok += 1
            mark = "✓" if got == want else f"✗ 期望 {want}"
        else:
            mark = "（无期望）"
        print(f"{text!r:<18} {str(got):<18} {conf_s:>8} {dt:>7.0f}ms  {mark}")

    print("=" * 88)
    if total:
        print(f"准确率: {ok}/{total} = {ok / total:.1%}")
    print("\n注意：这是 6 选项、无微调、零样本的结果。")
    print("官方 benchmark 里 laya-multilingual 在 20 选项 MASSIVE 意图任务上是 0.451。")
    print("所以它只适合兜住第 0 层漏掉的口语化表达，不该当主判据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
