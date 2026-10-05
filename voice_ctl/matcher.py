"""第 0 层：别名匹配。

设计依据：开微信这件事，精确/模糊匹配能做到 0 延迟、0 内存、确定性强，
用模型属于过度设计。所以这一层必须先吃掉绝大多数指令，模型只兜剩下的。

打分策略（都是确定性的，可单测）：
    exact      规范形式完全相同              → 1.0
    alias-hit  用户话里出现了完整别名        → 0.90 + 0.10 * 覆盖比
    scored     difflib 相似度 / 拼音键命中   → 相似度本身
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Iterable

from .normalize import Normalizer, ascii_slug, canonical, fuzzy_key

if TYPE_CHECKING:  # pragma: no cover
    from .config import ActionConfig

# 见 normalize._ID_SEP_RE 的说明：比较前把动作 id 里的下划线换成点，
# canonical() 会把它们一起清掉，于是 sys.show_desktop / sys.showdesktop /
# 用户说的 showdesktop 三者等价。
_ID_SEP = re.compile(r"[_.]+")


def id_segments(action_id: str) -> list[str]:
    """动作 id 的可匹配片段：'sys.show_desktop' → ['sys.showdesktop', 'showdesktop']。"""
    flat = _ID_SEP.sub(".", action_id)
    out = [flat]
    if "." in flat:
        out.append(flat.rsplit(".", 1)[-1])
    return [s for s in out if s]


@dataclass
class Match:
    action_id: str
    score: float
    """0.0 - 1.0。"""

    alias: str
    """命中的那条别名（原始写法，便于向用户解释）。"""

    strategy: str
    """exact / alias-hit / scored —— 命中的方式，用于诊断。"""

    normalized_input: str = ""
    """归一化后的用户输入，用于诊断。"""

    @property
    def hit(self) -> bool:
        return self.score > 0

    def describe(self) -> str:
        return f"{self.action_id} ({self.score:.2f} via {self.strategy}, alias={self.alias!r})"


def ratio(a: str, b: str) -> float:
    """0-1 相似度。

    SequenceMatcher 对中文不算理想（它按字符比，没有语言学信息），但配合
    "完整别名命中"这条规则已经够用。真不够时再上 rapidfuzz，不提前引入依赖。
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    r = SequenceMatcher(None, a, b).ratio()
    # 短串被长串完全包含时给个奖励：「音量加」in「把音量加起来」应当高分
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if short in long:
        cover = len(short) / len(long)
        r = max(r, 0.75 + 0.25 * cover)
    return r


class Matcher:
    """把所有启用动作编译成一个匹配器。动作变化时重建。"""

    def __init__(
        self,
        actions: Iterable["ActionConfig"],
        *,
        normalizer: Normalizer | None = None,
        threshold: int = 80,
        disambiguation_margin: float = 0.25,
    ) -> None:
        self.threshold = threshold / 100.0
        self.disambiguation_margin = disambiguation_margin
        self.norm = normalizer or Normalizer()
        self._actions: list[ActionConfig] = [a for a in actions if a.enabled]
        self._index: list[tuple[str, list[tuple[str, str, str, str, str]]]] = []
        for a in self._actions:
            entries: list[tuple[str, str, str, str, str]] = []
            for alias in list(a.aliases) + id_segments(a.id):
                match_key = _ID_SEP.sub(".", alias)
                ck = canonical(match_key)
                if not ck:
                    continue
                # (用于匹配的键, 规范键, 拼音键, ascii 串, 报告给用户看的原文)
                entries.append((match_key, ck, fuzzy_key(match_key), ascii_slug(match_key), alias))
            if entries:
                self._index.append((a.id, entries))

    # -- 查询 ------------------------------------------------------------- #

    @property
    def action_ids(self) -> list[str]:
        return [aid for aid, _ in self._index]

    def best(self, text: str) -> Match | None:
        """返回得分最高的匹配；低于阈值返回 None。"""
        cands = self.rank(text)
        if not cands:
            return None
        top = cands[0]
        return top if top.score >= self.threshold else None

    def rank(self, text: str) -> list[Match]:
        """按得分降序列出所有候选（不过阈值过滤），供诊断与决策层使用。"""
        norm_text = self.norm.normalize(text)
        keys = self.norm.keys(text)
        if not keys:
            return []
        ck_in = keys[0]
        fk_in = keys[1] if len(keys) > 1 else ""
        ask_in = ascii_slug(norm_text)

        out: list[Match] = []
        for action_id, entries in self._index:
            best: Match | None = None
            for entry in entries:
                _, ck, fk, ask, display = entry
                m = self._score_one(action_id, display, ck, fk, ask, ck_in, fk_in, ask_in, norm_text)
                if best is None or m.score > best.score:
                    best = m
            if best is not None:
                out.append(best)
        out.sort(key=lambda m: (-m.score, m.action_id))

        # 近分候选之间用「区分字」再判一次。
        # 场景：别名「音量加」和「音量减」只差最后一个字，模糊相似度会把它们
        # 排错（实测 "把音量调小一点" 判成了 volume_up）。阈值救不了，因为
        # 错的分数本身就过阈值——必须看哪个候选有输入里出现、对手没有的字。
        if len(out) >= 2:
            out[0], out[1] = self._disambiguate(out[0], out[1], ck_in, norm_text)
        return out

    @staticmethod
    def _distinctive(candidate_ck: str, rival_cks: list[str], text_ck: str) -> str:
        """返回 candidate 独有、且真的出现在输入里的字（没有则空串）。

        保守起见：只要该字在任一对手的别名里出现，就不算「独有」——
        避免用共有字做判断反而把对的判错。
        """
        for ch in candidate_ck:
            if ch not in text_ck:
                continue
            if any(ch in r for r in rival_cks):
                continue
            return ch
        return ""

    def _disambiguate(
        self, top: Match, second: Match, ck_in: str, norm_text: str
    ) -> tuple[Match, Match]:
        """仅当两者分差很小、且只有一个候选拥有「区分字」时交换它们。

        翻盘时把胜者置为满分，理由：两者的原始分差已经很小，说明它们本就是
        同族近义（音量加/音量减、锁屏/截屏…）；此时**区分字是决定性的证据**，
        它比模糊相似度可信得多。若不给满分，胜者会卡在阈值下方变成"未命中"
        ——实测 "把音量调小一点" 就是这样：正确动作排到了第一（0.75），
        却因为低于 0.80 而被丢掉。
        """
        if top.action_id == second.action_id:
            return top, second
        if top.score - second.score > self.disambiguation_margin:
            return top, second

        top_ck = canonical(_ID_SEP.sub(".", top.alias))
        sec_ck = canonical(_ID_SEP.sub(".", second.alias))
        top_d = self._distinctive(top_ck, [sec_ck], ck_in)
        sec_d = self._distinctive(sec_ck, [top_ck], ck_in)

        # 只有「一方有区分字、另一方没有」时才敢翻盘；双方都有或都没有就不动。
        if sec_d and not top_d:
            swapped = Match(
                second.action_id,
                1.0,
                second.alias,
                f"{second.strategy}+distinct:{sec_d}",
                normalized_input=norm_text,
            )
            demoted = Match(
                top.action_id,
                top.score,
                top.alias,
                f"{top.strategy}+lost-to:{second.action_id}",
                normalized_input=norm_text,
            )
            return swapped, demoted
        return top, second

    def _score_one(
        self,
        action_id: str,
        display: str,
        ck: str,
        fk: str,
        ask: str,
        ck_in: str,
        fk_in: str,
        ask_in: str,
        norm_text: str,
    ) -> Match:
        def mk(score: float, strategy: str) -> Match:
            return Match(action_id, score, display, strategy, normalized_input=norm_text)

        # 1. 规范形式完全相同
        if ck == ck_in:
            return mk(1.0, "exact")

        # 2. 用户话里包含完整别名 —— 最可靠的实用信号
        if ck and ck in ck_in:
            cover = len(ck) / len(ck_in)
            return mk(0.90 + 0.10 * cover, "alias-hit")

        # 3. 拼音首字母命中（"威信"→wx，别名"微信"→wx）
        if fk and fk_in and len(fk) >= 2 and fk == fk_in:
            return mk(0.88, "scored")

        # 4. 字符级相似度
        s = ratio(ck, ck_in)
        if s < 0.999 and fk and fk_in:
            s = max(s, ratio(fk, fk_in) * 0.95)
        if ask and ask_in:
            s = max(s, ratio(ask, ask_in) * 0.95)
        return mk(s, "scored")

    # -- 诊断 ------------------------------------------------------------- #

    def explain(self, text: str, top: int = 5) -> str:
        norm = self.norm.normalize(text)
        keys = self.norm.keys(text)
        lines = [
            f"原始输入 : {text!r}",
            f"归一化后 : {norm!r}",
            f"匹配键   : {keys}",
            f"阈值     : {self.threshold:.2f}",
        ]
        ranked = self.rank(text)
        if not ranked:
            lines.append("没有任何候选（输入为空？）")
            return "\n".join(lines)
        lines.append("候选（降序）：")
        for m in ranked[:top]:
            mark = "✓" if m.score >= self.threshold else " "
            lines.append(
                f"  {mark} {m.action_id:24} {m.score:.3f}  {m.strategy:10} alias={m.alias!r}"
            )
        return "\n".join(lines)
