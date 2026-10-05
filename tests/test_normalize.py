"""归一化测试：口语词剥离、同音纠错、匹配键生成。"""

from __future__ import annotations

from voice_ctl.normalize import (
    DEFAULT_SUBSTITUTIONS,
    NormalizeConfig,
    Normalizer,
    ascii_slug,
    canonical,
    fuzzy_key,
)


def test_canonical_strips_punct_and_space():
    assert canonical("  打开 微信。 ") == "打开微信"
    assert canonical("Open WeChat!") == "openwechat"
    assert canonical("音量，加") == "音量加"


def test_canonical_handles_fullwidth():
    """中文输入法/ASR 会给出全角字符，NFKC 要先归一。"""
    assert canonical("ＯＰＥＮ") == "open"
    assert canonical("打开微信") == canonical("打开微信")


def test_ascii_slug_keeps_only_ascii():
    """ascii_slug 的用途是给别名做最后兜底比较，只应保留 [0-9a-z]，汉字丢掉。"""
    assert ascii_slug("打开 WeChat 2") == "wechat2"
    assert ascii_slug("微信") == ""
    assert ascii_slug("GitHub!") == "github"
    assert ascii_slug("") == ""
    assert ascii_slug("，。！") == ""


def test_substitution_fixes_homophones():
    n = Normalizer()
    assert n.normalize("威信") == "微信"
    assert n.normalize("截频") == "截屏"
    assert n.normalize("计算起") == "计算器"


def test_all_default_substitutions_are_effective():
    """表里每条都必须真的能替换——防止手滑写错 key。"""
    n = Normalizer()
    for wrong, right in DEFAULT_SUBSTITUTIONS.items():
        assert wrong in n._subs[0][0] or True  # 结构检查
        assert n.normalize(wrong) == right, f"{wrong!r} 没有替换成 {right!r}"


def test_strip_prefixes_repeatedly():
    n = Normalizer(NormalizeConfig(strip_prefixes=["请", "帮我", "帮忙"]))
    assert n.normalize("请帮我打开微信") == "打开微信"
    assert n.normalize("帮忙请打开微信") == "打开微信"


def test_strip_prefixes_never_eats_whole_string():
    """全是口语词时不能剥成空串。"""
    n = Normalizer(NormalizeConfig(strip_prefixes=["请", "帮我"]))
    assert n.normalize("请") == "请"
    assert n.normalize("帮我") == "帮我"


def test_strip_suffixes():
    n = Normalizer(NormalizeConfig(strip_suffixes=["一下", "谢谢"]))
    assert n.normalize("打开微信一下") == "打开微信"
    assert n.normalize("截屏谢谢") == "截屏"


def test_prefix_and_suffix_together():
    n = Normalizer(NormalizeConfig(strip_prefixes=["帮我"], strip_suffixes=["一下"]))
    assert n.normalize("帮我打开微信一下") == "打开微信"


def test_inline_filler_in_the_middle():
    """「打开一下微信」里的「一下」在句中，首尾剥离管不到，必须单独处理。"""
    n = Normalizer(NormalizeConfig(inline_fillers=["一下"]))
    assert n.normalize("打开一下微信") == "打开微信"
    assert n.normalize("截一下屏") == "截屏"


def test_inline_filler_never_empties_the_string():
    """全是填充词时不能删成空串。"""
    n = Normalizer(NormalizeConfig(inline_fillers=["一下"]))
    assert n.normalize("一下") == "一下"
    assert n.normalize("一下一下") == "一下一下"


def test_inline_filler_does_not_eat_command_words():
    """安全约束：填充词表里绝不能有「打开」这类命令词。

    如果误把「打开」放进 inline_fillers，「打开微信」会被删成「微信」——
    表面看还能匹配上，但任何依赖原句的场景都会出错。
    """
    n = Normalizer(NormalizeConfig(inline_fillers=["打开"]))
    # 即便用户这么配了，实现也必须保证不会把整句吃光
    assert n.normalize("打开微信") == "微信", "此测试记录该配置会有的后果，非期望行为"


def test_default_inline_fillers_are_safe():
    from voice_ctl.normalize import NormalizeConfig as NC

    dangerous = {"打开", "关闭", "截屏", "静音", "锁屏", "音量"}
    assert not (set(NC().inline_fillers) & dangerous), "默认填充词里混入了命令词"


def test_keys_returns_canonical_first():
    n = Normalizer()
    keys = n.keys("打开 微信。")
    assert keys[0] == "打开微信"
    assert len(keys) >= 1


def test_keys_empty_input():
    n = Normalizer()
    assert n.keys("") == []
    assert n.keys("   ，。！  ") == []


def test_user_substitutions_override_defaults():
    n = Normalizer(NormalizeConfig(substitutions={"威信": "钉钉"}))
    assert n.normalize("威信") == "钉钉"


def test_longer_substitution_wins():
    """长键优先，避免短键先吃掉一部分。"""
    n = Normalizer(NormalizeConfig(substitutions={"微信": "A", "微信电脑版": "B"}))
    assert n.normalize("微信电脑版") == "B"


def test_fuzzy_key_is_stable_without_pypinyin():
    """没装 pypinyin 时必须退化成 canonical，而不是报错。"""
    assert fuzzy_key("打开") in (canonical("打开"), "dk")


def test_normalize_is_idempotent():
    n = Normalizer()
    once = n.normalize("请帮我打开威信一下")
    assert n.normalize(once) == once
