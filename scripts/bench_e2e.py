"""端到端基准：TTS 音频 → 识别 → 归一化 → 匹配，统计准确率与耗时。

这是验证「短命令词识别」的核心工具。合成语音与真人语音有差距，所以数字
绝对值仅供参考；真正的价值是**横向对比**：改同音表、改剥离词、改阈值之后
重跑，看指标往哪走。

用法：
    .venv\\Scripts\\python scripts\\bench_e2e.py
    .venv\\Scripts\\python scripts\\bench_e2e.py --limit 5 --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voice_ctl.actions import build_registry  # noqa: E402
from voice_ctl.app import Pipeline  # noqa: E402
from voice_ctl.asr import Asr, AsrError, load_wave_mono16k  # noqa: E402
from voice_ctl.config import load_config  # noqa: E402
from voice_ctl.matcher import Matcher  # noqa: E402
from voice_ctl.normalize import NormalizeConfig, Normalizer  # noqa: E402

TTS_DIR = ROOT / "assets" / "tts"


@dataclass
class Row:
    idx: int
    expect: str | None
    truth: str
    kind: str
    heard: str = ""
    matched: str | None = None
    score: float = 0.0
    strategy: str = ""
    asr_ms: float = 0.0
    rtf: float = 0.0
    error: str = ""

    @property
    def asr_exact(self) -> bool:
        """识别文本是否与合成原文完全一致（严格的识别准确率）。"""
        return self.heard == self.truth

    @property
    def match_ok(self) -> bool:
        """匹配是否正确：期望 None 时要求未命中，否则要求命中该动作。"""
        if self.expect is None:
            return self.matched is None
        return self.matched == self.expect


@dataclass
class Summary:
    rows: list[Row] = field(default_factory=list)

    def by(self, key) -> dict[str, list[Row]]:  # noqa: ANN001
        out: dict[str, list[Row]] = {}
        for r in self.rows:
            out.setdefault(key(r), []).append(r)
        return out


def load_manifest() -> list[dict]:
    p = TTS_DIR / "manifest.json"
    if not p.is_file():
        print(f"✗ 找不到 {p}；先跑：.venv\\Scripts\\python scripts\\make_tts_samples.py")
        sys.exit(2)
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--json", default="scripts/bench_e2e.json")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.toml")
    norm = Normalizer(
        NormalizeConfig(
            strip_prefixes=cfg.match.strip_prefixes,
            strip_suffixes=cfg.match.strip_suffixes,
            inline_fillers=cfg.match.inline_fillers,
        )
    )
    matcher = Matcher(cfg.enabled_actions, normalizer=norm, threshold=cfg.match.threshold)
    registry = build_registry(cfg.enabled_actions)
    asr = Asr(
        cfg.model_path(),
        language=cfg.model.language,
        use_itn=cfg.model.use_itn,
        num_threads=cfg.model.num_threads,
        provider=cfg.model.provider,
    )

    print(f"模型     : {cfg.model_path()}")
    print(f"匹配阈值 : {cfg.match.threshold}")
    print("加载中 ...", end="", flush=True)
    asr.load()
    print(f" ok（{asr.load_ms:.0f}ms）\n")

    pipe = Pipeline(
        asr=asr, matcher=matcher, registry=registry,
        actions=cfg.enabled_actions, normalizer=norm,
    )

    items = load_manifest()
    if args.limit:
        items = items[: args.limit]

    s = Summary()
    for i, item in enumerate(items):
        row = Row(
            idx=i,
            expect=item["expect"] or None,
            truth=item["text"],
            kind=item.get("kind", "?"),
        )
        wav = TTS_DIR / item["file"]
        try:
            samples = load_wave_mono16k(wav)
            res = asr.transcribe(samples)
            row.heard = res.text
            row.asr_ms = res.infer_ms
            row.rtf = res.rtf
            m = matcher.best(res.text)
            if m is not None:
                row.matched = m.action_id
                row.score = m.score
                row.strategy = m.strategy
        except (AsrError, OSError) as e:
            row.error = str(e)
        s.rows.append(row)

    # ---- 报告 ----
    total = len(s.rows)
    errors = [r for r in s.rows if r.error]
    exact = sum(1 for r in s.rows if r.asr_exact)
    match_ok = sum(1 for r in s.rows if r.match_ok)
    asr_ms = sorted(r.asr_ms for r in s.rows if r.asr_ms)

    print("=" * 100)
    print(f"{'#':>3}  {'类型':<11} {'期望':<18} {'识别结果':<26} {'匹配':<18} {'得分':>5} {'ms':>5}")
    print("-" * 100)
    for r in s.rows:
        ok = "✓" if r.match_ok else "✗"
        heard = r.heard or (f"<{r.error[:20]}>" if r.error else "")
        print(
            f"{r.idx:>3}  {r.kind:<11} {str(r.expect or '(不命中)'):<18} "
            f"{heard:<26} {ok} {str(r.matched or '-'):<16} {r.score:>5.2f} {r.asr_ms:>5.0f}"
        )

    print("=" * 100)
    print(f"样本数            : {total}" + (f"  (出错 {len(errors)})" if errors else ""))
    print(f"识别完全正确      : {exact}/{total} = {exact / total:.1%}   （识别文本 == 合成原文）")
    print(f"动作匹配正确      : {match_ok}/{total} = {match_ok / total:.1%}   ← 这才是端到端指标")
    if asr_ms:
        print(f"识别耗时          : 中位 {asr_ms[len(asr_ms) // 2]:.0f}ms  "
              f"最快 {asr_ms[0]:.0f}ms  最慢 {asr_ms[-1]:.0f}ms")

    print("\n按类型：")
    for kind, rows in sorted(s.by(lambda r: r.kind).items()):
        ok = sum(1 for r in rows if r.match_ok)
        ex = sum(1 for r in rows if r.asr_exact)
        print(f"  {kind:<12} 匹配 {ok}/{len(rows)}   识别全对 {ex}/{len(rows)}")

    misses = [r for r in s.rows if not r.match_ok]
    if misses:
        print("\n未过关的样本（这是要改配置/同音表的依据）：")
        for r in misses:
            want = r.expect or "(不该命中)"
            print(f"  #{r.idx} [{r.kind}] 期望 {want}")
            print(f"       合成原文 : {r.truth}")
            print(f"       识别结果 : {r.heard}")
            print(f"       归一化后 : {norm.normalize(r.heard)}")
            print(f"       实际匹配 : {r.matched or '未命中'}"
                  + (f"  score={r.score:.2f} {r.strategy}" if r.matched else ""))
            rank = matcher.rank(r.heard)[:3]
            if rank:
                print("       候选前三 : " + ", ".join(f"{m.action_id}={m.score:.2f}" for m in rank))

    out = ROOT / args.json
    out.write_text(
        json.dumps(
            {
                "total": total,
                "asr_exact": exact,
                "match_ok": match_ok,
                "threshold": cfg.match.threshold,
                "rows": [vars(r) for r in s.rows],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n明细已写入 {out}")

    if args.verbose:
        print("\n各类型识别原文分布：")
        for kind, rows in s.by(lambda r: r.kind).items():
            c = Counter(r.heard for r in rows)
            print(f"  {kind}: {dict(c)}")

    return 0 if match_ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
