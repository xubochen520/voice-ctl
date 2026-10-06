"""gfx.py：抗锯齿光栅器、颜色与对比度、PNG 编解码。

这些都是纯 numpy 函数，不需要 Tk，也就不受"无图形环境整体跳过"的影响——
界面看起来对不对最终要靠眼睛，但"边缘是不是抗锯齿的""颜色算得对不对"可以证明。
"""

from __future__ import annotations

import numpy as np
import pytest

from voice_ctl.ui import gfx
from voice_ctl.ui.theme import PALETTE as P

# --------------------------------------------------------------------------- #
# 颜色
# --------------------------------------------------------------------------- #


def test_color_roundtrip_and_shorthand():
    assert gfx.to_rgb("#ff5b1f") == (255, 91, 31)
    assert gfx.to_rgb("#fff") == (255, 255, 255)
    assert gfx.to_hex((255, 91, 31)) == "#ff5b1f"
    assert gfx.to_hex((300, -5, 31.4)) == "#ff001f", "越界要夹到 0–255，不能抛"


def test_mix_endpoints_and_midpoint():
    assert gfx.mix("#000000", "#ffffff", 0) == "#000000"
    assert gfx.mix("#000000", "#ffffff", 1) == "#ffffff"
    assert gfx.mix("#000000", "#ffffff", 0.5) == "#808080"


def test_contrast_matches_wcag_reference_values():
    assert gfx.contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert gfx.contrast("#ffffff", "#ffffff") == pytest.approx(1.0)
    # WCAG 官方例子：#767676 在白底上刚好 4.54:1（AA 正文的临界色）
    assert gfx.contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.02)


# 设计令牌的对比度底线。这是**可证明**的那一半"好看"：灰字看不清是最常见的审美事故，
# 而且它不会因为换了屏幕/缩放就变。每一对都是界面里真实出现的"字色 on 底色"。
READABLE = [
    # (前景, 背景, 最低对比度)
    ("ink", "chassis", 7.0), ("ink", "panel", 7.0), ("ink", "raised", 7.0),
    ("ink2", "chassis", 4.5), ("ink2", "panel", 4.5),
    ("ink3", "chassis", 4.5), ("ink3", "panel", 4.5), ("ink3", "raised", 4.5),
    ("white", "ink", 7.0),
    ("teal", "chassis", 4.5), ("teal", "panel", 4.5), ("teal", "info_bg", 4.5),
    ("ok", "panel", 4.5), ("warn", "panel", 4.5), ("err", "panel", 4.5), ("live_ink", "panel", 4.5),
    ("ok", "ok_bg", 4.5), ("warn", "warn_bg", 4.5), ("err", "err_bg", 4.5),
    ("scr_ink", "scr", 7.0), ("scr_dim", "scr", 4.5), ("scr_dim", "scr2", 4.5),
    ("ok_lit", "scr", 4.5), ("warn_lit", "scr", 4.5), ("err_lit", "scr", 4.5),
    ("live", "scr", 4.5), ("teal_lit", "scr", 4.5),
    # 调试行是故意压暗的次要信息，只要求大字级别的 3:1
    ("scr_faint", "scr", 3.0),
]


@pytest.mark.parametrize(("fg", "bg", "need"), READABLE, ids=[f"{a}-on-{b}" for a, b, _ in READABLE])
def test_palette_text_is_readable(fg: str, bg: str, need: float):
    got = gfx.contrast(P[fg], P[bg])
    assert got >= need, f"{fg} 在 {bg} 上只有 {got:.2f}:1，需要 ≥ {need}"


def test_every_palette_value_is_a_valid_hex():
    for name, value in P.items():
        r, g, b = gfx.to_rgb(value)
        assert 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255, name


# --------------------------------------------------------------------------- #
# PNG
# --------------------------------------------------------------------------- #


def test_png_roundtrip_is_lossless():
    rng = np.random.default_rng(7)
    img = rng.integers(0, 256, size=(13, 29, 4), dtype=np.uint8)
    data = gfx.png_bytes(img)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert np.array_equal(gfx.decode_png(data), img)


# --------------------------------------------------------------------------- #
# 光栅
# --------------------------------------------------------------------------- #


def test_rrect_is_antialiased_not_binary():
    """圆角必须有中间灰度的边缘像素——全是 0/255 就是锯齿，正是我们要摆脱的 Tk 原生画法。"""
    img = gfx.rrect_rgba(120, 40, 20, "#151a1c", None)
    alpha = img[..., 3]
    partial = np.count_nonzero((alpha > 8) & (alpha < 247))
    assert partial > 40, f"只有 {partial} 个半透明边缘像素，圆角没有抗锯齿"
    assert alpha[20, 60] == 255, "中心必须不透明"
    assert alpha[0, 0] == 0, "圆角外面的角必须透明"


def test_rrect_corner_is_symmetric():
    img = gfx.rrect_rgba(64, 64, 16, "#ffffff", None)
    a = img[..., 3].astype(int)
    assert np.abs(a - a[:, ::-1]).max() <= 1, "左右不对称"
    assert np.abs(a - a[::-1, :]).max() <= 1, "上下不对称"


def test_rrect_border_is_inside_the_shape():
    """描边画在形状内侧：w×h 就是它实际占的大小，几个控件并排才对得齐。"""
    img = gfx.rrect_rgba(60, 30, 8, "#ffffff", "#000000", 2)
    assert tuple(img[15, 0, :3]) == (0, 0, 0) and img[15, 0, 3] == 255, "最外一列应该是描边色"
    assert tuple(img[15, 30, :3]) == (255, 255, 255), "中心是填充色"
    assert img[0, 0, 3] == 0


def test_rrect_focus_ring_leaves_a_gap():
    """焦点环和形状之间要有一圈透明缝，否则看起来只是描边变粗了。"""
    img = gfx.rrect_rgba(80, 40, 10, "#ffffff", "#444444", 1, ring="#00aaaa", ring_w=2)
    row = img[20]
    assert tuple(row[0, :3]) == (0, 170, 170) and row[0, 3] == 255, "最外是环色"
    # 从左往右：环(2px) → 透明缝(1px) → 描边 → 填充
    assert row[2, 3] < 40, f"环与形状之间应有透明缝，实际 alpha={row[2, 3]}"
    assert row[3, 3] == 255


def test_corner_tiles_reassemble_into_the_full_shape():
    r = 12
    full = gfx.rrect_rgba(2 * r, 2 * r, r, "#336699", "#112233", 1)
    tl, tr, bl, br = gfx.corner_tiles(r, "#336699", "#112233", 1)
    top = np.concatenate([tl, tr], axis=1)
    bot = np.concatenate([bl, br], axis=1)
    assert np.array_equal(np.concatenate([top, bot], axis=0), full)


@pytest.mark.parametrize("name", sorted(gfx.ICONS))
def test_every_icon_renders_visible_ink_inside_its_box(name: str):
    size = 48
    img = gfx.icon_rgba(name, size, "#000000")
    alpha = img[..., 3]
    assert alpha.max() > 200, f"{name}: 画不出实心像素"
    ys, xs = np.nonzero(alpha > 16)
    assert len(xs) > size, f"{name}: 墨迹太少（{len(xs)} 像素）"
    # 图标不能被裁掉：四条边上的墨迹要很少（设计网格里都留了边距）
    edge = np.count_nonzero(alpha[0, :] > 16) + np.count_nonzero(alpha[-1, :] > 16) \
        + np.count_nonzero(alpha[:, 0] > 16) + np.count_nonzero(alpha[:, -1] > 16)
    assert edge <= 4, f"{name}: 图形碰到了图标边界（{edge} 个边缘像素），会被裁掉"


def test_icon_color_is_applied():
    img = gfx.icon_rgba("play", 32, "#ff5b1f")
    solid = img[img[..., 3] == 255]
    assert len(solid) > 0
    assert (solid[:, :3] == np.array([255, 91, 31])).all()


def test_icon_scales_with_size():
    small = gfx.icon_rgba("mic", 16, "#000000")[..., 3].astype(float).sum()
    big = gfx.icon_rgba("mic", 64, "#000000")[..., 3].astype(float).sum()
    # 墨量大致按面积涨（16→64 是 4 倍边长）。线宽随尺寸缩放，所以比例应接近 16
    assert 10 < big / small < 22


def test_icon_cut_primitive_punches_a_hole():
    """copy 图标用 "cut" 把前面那个方块被后面方块遮住的部分挖掉——挖掉的地方必须真的透明。"""
    img = gfx.icon_rgba("copy", 48, "#000000")[..., 3]
    assert img.max() > 200
    # 两个方块重叠区域的中心：设计网格里大约是 (12.5, 12.5)；挖洞后那里没有前一个方块的边线
    k = 48 / 24
    centre = img[int(11.2 * k), int(11.2 * k)]
    assert centre < 80, f"重叠区没被挖空（alpha={centre}）"


def test_dot_is_round_and_glow_is_outside():
    d = gfx.dot_rgba(10, 6, "#ff5b1f", glow=1.0)
    size = d.shape[0]
    assert size == 22
    c = size // 2
    assert d[c, c, 3] == 255
    assert d[c, 0, 3] < d[c, 3, 3] or d[c, 0, 3] == 0, "光晕应该向外衰减"
    plain = gfx.dot_rgba(10, 6, "#ff5b1f", glow=0.0)
    assert plain[c, 1, 3] == 0, "不带光晕时圆点外面必须是透明的"
    assert d[c, 3, 3] > 0, "带光晕时圆点外面要有一圈柔光"


def test_keycap_pressed_sinks_the_face():
    up = gfx.keycap_rgba(60, 40, 6, 8, "#ffffff", "#dddddd", "#888888", pressed=False)
    down = gfx.keycap_rgba(60, 40, 6, 8, "#ffffff", "#dddddd", "#888888", pressed=True)
    assert up.shape == down.shape == (46, 60, 4)
    # 弹起时顶上一行就是键面；按下时键面下沉，顶上几行是透明的
    assert up[0, 30, 3] > 200
    assert down[0, 30, 3] == 0
    # 弹起时底部是侧壁色（暗）；按下时底部露出的侧壁只剩 1px
    assert tuple(up[-2, 30, :3]) == (136, 136, 136)


def test_app_icon_has_rounded_corners_and_orange_bars():
    img = gfx.app_icon_rgba(64)
    assert img[0, 0, 3] == 0, "圆角方块的角要透明"
    assert img[32, 4, 3] == 255
    orange = (img[..., 0] > 200) & (img[..., 1] < 140) & (img[..., 2] < 80) & (img[..., 3] == 255)
    assert orange.sum() > 150, "要有一组橙色竖条"
    # 竖条是中心对称布局：左右墨量大致相当
    left, right = orange[:, :32].sum(), orange[:, 32:].sum()
    assert abs(int(left) - int(right)) / max(left, right) < 0.35


def test_layer_compositing_matches_porter_duff():
    lay = gfx.Layer(2, 1)
    lay.paint(np.array([[1.0, 0.0]], dtype=np.float32), "#ff0000")
    lay.paint(np.array([[0.5, 0.5]], dtype=np.float32), "#0000ff")
    out = lay.rgba()
    # 第 0 像素：红完全盖住，再叠 50% 蓝 → (127.5, 0, 127.5)，alpha 255
    assert out[0, 0, 3] == 255
    assert abs(int(out[0, 0, 0]) - 128) <= 1 and abs(int(out[0, 0, 2]) - 128) <= 1
    # 第 1 像素：只有 50% 蓝；straight alpha 下颜色应是纯蓝、alpha 128
    assert tuple(out[0, 1]) == (0, 0, 255, 128)
