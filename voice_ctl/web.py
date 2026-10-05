"""网页解析：把「百度」这种名字变成能打开的网址。

为什么需要这一层：别名匹配和已安装应用索引都只认**本机装了什么**。而
「打开百度」「打开淘宝」说的是**网站**，本机没装它们的客户端是完全正常的——
实测这两句都会走到"没找到叫「百度网页」的应用"，然后什么都不做。

三层解析，从最确定到最宽松：

  1. **站点表**（本文件）：常见站点的名字/别名/域名，写死在这里，查表即得，
     不问网络、不看运气。「百度」「B站」「知乎」都直接命中。
  2. **看起来像域名**（`looks_like_domain`）：`打开 example.com` 直接用。
  3. **搜索兜底**（`search_url`，默认开，可关）：表里没有、也不像域名时，
     走一次搜索引擎。用户说「打开XX网页」而 XX 是个没收录的站名时，
     给搜索结果页远好过什么都不做。

刻意**不**做的两件事：

  * 不问网络猜域名（`名字.com` 这种）。实测「打开淘宝」会猜成 `淘宝.com`，
    在中文环境里它多半解析不了；而搜索引擎一定给得出结果。
  * 不把「打开X」无差别地变成搜索。只有**没命中任何本地应用、也没命中站点表**、
    或者用户明说了「网页/官网」时才走网页路线——否则「打开计算器」会变成
    搜索"计算器"而不是打开计算器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .lexicon import strip_punct, strip_trailing_glue, strip_web_cue

DEFAULT_SEARCH = "https://www.baidu.com/s?wd={q}"
"""搜索兜底用的引擎。换成必应只要改这一行。"""


@dataclass(frozen=True)
class Site:
    name: str
    """显示名，也是用户最可能说的那个词。"""
    url: str
    aliases: tuple[str, ...] = ()
    """其它说法。「B站」「哔哩哔哩」都要能落到 bilibili。"""
    note: str = ""
    """给用户看的一句话补充（比如"直达网盘首页，不保证自动登录"）。"""


_CURATED: tuple[Site, ...] = (
    # --- 搜索与门户 -------------------------------------------------------
    Site("百度", "https://www.baidu.com", ("baidu", "度娘")),
    Site("百度网盘", "https://pan.baidu.com", ("网盘", "baidupan", "百度云")),
    Site("必应", "https://www.bing.com", ("bing",)),
    Site("谷歌", "https://www.google.com", ("google", "谷哥")),
    Site("搜狗", "https://www.sogou.com", ()),
    Site("360搜索", "https://www.so.com", ("好搜",)),
    Site("hao123", "https://www.hao123.com", ()),
    # --- 购物 -------------------------------------------------------------
    Site("淘宝", "https://www.taobao.com", ("taobao",)),
    Site("天猫", "https://www.tmall.com", ("tmall",)),
    Site("京东", "https://www.jd.com", ("jd",)),
    Site("拼多多", "https://www.pinduoduo.com", ("pdd",)),
    Site("闲鱼", "https://www.goofish.com", ("goofish",)),
    Site("唯品会", "https://www.vip.com", ()),
    Site("苏宁", "https://www.suning.com", ()),
    Site("亚马逊", "https://www.amazon.cn", ("amazon",)),
    # --- 视频与直播 -------------------------------------------------------
    Site("哔哩哔哩", "https://www.bilibili.com", ("b站", "bilibili", "小破站")),
    Site("腾讯视频", "https://v.qq.com", ("v.qq",)),
    Site("爱奇艺", "https://www.iqiyi.com", ("iqiyi", "奇异果")),
    Site("优酷", "https://www.youku.com", ("youku",)),
    Site("芒果tv", "https://www.mgtv.com", ("芒果", "mgtv")),
    Site("抖音", "https://www.douyin.com", ("douyin",)),
    Site("快手", "https://www.kuaishou.com", ("kuaishou",)),
    Site("西瓜视频", "https://www.ixigua.com", ("西瓜",)),
    Site("youtube", "https://www.youtube.com", ("油管",)),
    Site("斗鱼", "https://www.douyu.com", ()),
    Site("虎牙", "https://www.huya.com", ()),
    # --- 社交与社区 -------------------------------------------------------
    Site("知乎", "https://www.zhihu.com", ("zhihu",)),
    Site("微博", "https://weibo.com", ("新浪微博", "weibo")),
    Site("豆瓣", "https://www.douban.com", ("douban",)),
    Site("贴吧", "https://tieba.baidu.com", ("百度贴吧",)),
    Site("小红书", "https://www.xiaohongshu.com", ("xhs",)),
    Site("天涯", "https://www.tianya.cn", ()),
    Site("虎扑", "https://www.hupu.com", ("hupu",)),
    Site("v2ex", "https://www.v2ex.com", ()),
    # --- 工具与生产力 -----------------------------------------------------
    Site("github", "https://github.com", ("git hub", "代码仓库")),
    Site("gitee", "https://gitee.com", ("码云",)),
    Site("csdn", "https://www.csdn.net", ()),
    Site("掘金", "https://juejin.cn", ()),
    Site("stackoverflow", "https://stackoverflow.com", ("stack overflow",)),
    Site("博客园", "https://www.cnblogs.com", ()),
    Site("语雀", "https://www.yuque.com", ("yuque",)),
    Site("飞书", "https://www.feishu.cn", ("lark",)),
    Site("notion", "https://www.notion.so", ()),
    Site("chatgpt", "https://chat.openai.com", ("gpt",)),
    Site("claude", "https://claude.ai", ()),
    Site("deepseek", "https://chat.deepseek.com", ("深度求索",)),
    Site("通义千问", "https://tongyi.aliyun.com", ("通义", "千问")),
    Site("文心一言", "https://yiyan.baidu.com", ("文心",)),
    Site("kimi", "https://kimi.moonshot.cn", ()),
    # --- 影音与阅读 -------------------------------------------------------
    Site("网易云音乐", "https://music.163.com", ("网易云",)),
    Site("qq音乐", "https://y.qq.com", ()),
    Site("酷狗音乐", "https://www.kugou.com", ("酷狗",)),
    Site("酷我音乐", "https://www.kuwo.cn", ("酷我",)),
    Site("喜马拉雅", "https://www.ximalaya.com", ("喜马",)),
    Site("起点中文网", "https://www.qidian.com", ("起点",)),
    Site("番茄小说", "https://fanqienovel.com", ("番茄",)),
    Site("微信读书", "https://weread.qq.com", ()),
    # --- 邮箱与办公 -------------------------------------------------------
    Site("qq邮箱", "https://mail.qq.com", ("邮箱",)),
    Site("网易邮箱", "https://mail.163.com", ("163邮箱",)),
    Site("gmail", "https://mail.google.com", ()),
    Site("wps", "https://www.wps.cn", ()),
    # --- 出行与生活 -------------------------------------------------------
    Site("12306", "https://www.12306.cn", ("火车票", "铁路")),
    Site("高德地图", "https://www.amap.com", ("高德",)),
    Site("百度地图", "https://map.baidu.com", ()),
    Site("携程", "https://www.ctrip.com", ("ctrip",)),
    Site("美团", "https://www.meituan.com", ()),
    Site("大众点评", "https://www.dianping.com", ("点评", "大众")),
    Site("饿了么", "https://www.ele.me", ()),
    Site("天气", "https://weather.com.cn", ("中国天气", "天气预报")),
    # --- 游戏与其它 -------------------------------------------------------
    Site("steam", "https://store.steampowered.com", ("蒸汽平台",)),
    Site("米哈游", "https://www.mihoyo.com", ("mihoyo",)),
    Site("原神", "https://ys.mihoyo.com", ()),
    Site("4399", "https://www.4399.com", ()),
)


def _compact(s: str) -> str:
    """只留字母数字和汉字。比较站点名时忽略空格、点、连字符。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s.lower())


_WEAK_SUFFIXES = ("网", "网站", "官网", "网页", "主页", "首页", "页面", "网址")
r"""单独跟在站名后面、不改变意思的尾巴。

「淘宝网」=「淘宝」，「中国天气网」的尾巴是「网」。这些尾巴可以剥掉。
但**「网盘」不行**：「百度网盘」是另一个站（pan.baidu.com），把「网盘」当尾巴
就会把「打开百度网盘」开成 baidu.com——这是实测踩过的坑。
所以剥之前要确认剩下的部分**恰好**是个站点名，而不是"包含"。
"""


def _strip_weak(text: str) -> str:
    """剥掉一个弱尾巴。剥不动就原样返回。"""
    for suf in sorted(_WEAK_SUFFIXES, key=len, reverse=True):
        if text.endswith(suf) and len(text) > len(suf):
            return text[: -len(suf)]
    return text


class WebIndex:
    """站点表 + 查找。

    查找是纯字符串比较（毫秒级），不碰网络。做法和 AppIndex 一致：
    精确 → 包含 → 相似度，逐级放宽，并且**只在明确指向某个站时才给结果**
    （相似度不够就返回 None，让上层去走搜索兜底，而不是硬猜一个站）。
    """

    MIN_SCORE = 0.82
    """相似度门槛。比应用索引的 0.6 高得多：站点表里"长得像"的名字太多
    （腾讯视频 vs 腾讯、百度 vs 百度网盘），宁可退回搜索也不要打开错的站。"""

    def __init__(self, sites: tuple[Site, ...] = _CURATED) -> None:
        self.sites = list(sites)
        self._by_key: dict[str, Site] = {}
        self._alias_keys: list[tuple[str, str, Site]] = []
        """(归一化别名, 原始别名, 站点)，长的排前面——「百度网盘」要先于「百度」命中。"""
        for site in self.sites:
            for alias in (site.name, *site.aliases):
                key = _compact(alias)
                if not key:
                    continue
                self._by_key.setdefault(key, site)
                self._alias_keys.append((key, alias, site))
        self._alias_keys.sort(key=lambda t: len(t[0]), reverse=True)

    def find(self, query: str) -> tuple[Site, str, str, float] | None:
        """按名字找站。返回 (站点, 命中的说法, 吻合程度, 分数)，找不到返回 None。

        吻合程度是给上层判断"该不该让位给本机同名程序"用的（`intent.resolve_web`）：

          exact      名字本身就是站点名或别名（「百度」「B站」「12306」）
          suffix     剥掉一个弱尾巴之后恰好是站点名（「淘宝网」→ 淘宝）
          contained  名字里带着站点名（「百度一下」→ 百度）

        `exact` 和 `suffix` 都算"用户说的就是这个站"——「淘宝网」和「淘宝」是同一
        个站，不该因为多了个「网」字就被判成不确定。`contained` 不算：它表示用户
        说的比站名多，多出来的那截可能是另一个东西（实测「打开百度网盘」里的
        「百度」+「网盘」就是这种情况，而网盘是另一个站）。
        """
        q = strip_punct(strip_trailing_glue(query))
        if not q:
            return None
        key = _compact(q)
        if not key:
            return None

        # 1) 完全一致
        if (site := self._by_key.get(key)) is not None:
            return site, q, "exact", 1.0

        # 1b) 剥掉弱尾巴再看：「淘宝网」→「淘宝」。只认**恰好**是站点名的情况，
        #     所以「百度网盘」剥出「百度」之后**不会**被当成百度——它在第 1 步
        #     就已经作为自己的站点命中了。
        stripped = _strip_weak(key)
        if stripped != key and (site := self._by_key.get(stripped)) is not None:
            return site, q, "suffix", 0.97

        # 2) 名字里带着站点名：「百度一下」→ 百度。长别名优先，避免「腾讯视频」
        #    被「腾讯」抢先（表里没有"腾讯"，但真加了的话这条顺序就是保险）。
        for alias_key, alias, site in self._alias_keys:
            if len(alias_key) >= 2 and alias_key in key:
                coverage = len(alias_key) / len(key)
                if coverage >= 0.5:
                    return site, alias, "contained", 0.9 + 0.1 * coverage

        # 3) 相似度（容忍 ASR 的一两个字出入）
        best: tuple[float, str, Site] | None = None
        for alias_key, alias, site in self._alias_keys:
            s = _similar(key, alias_key)
            if s > (best[0] if best else 0.0):
                best = (s, alias, site)
        if best is not None and best[0] >= self.MIN_SCORE:
            return best[2], best[1], "fuzzy", best[0]
        return None

    def names(self) -> list[str]:
        return [s.name for s in self.sites]


def _similar(a: str, b: str) -> float:
    """字符集 Jaccard + 长度惩罚。够用就行：站点名都很短，不需要编辑距离那一套。"""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    if not union:
        return 0.0
    jaccard = inter / union
    # 长度差得太多就再压一档：「百度」和「百度网盘」Jaccard 是 1.0（子集），
    # 不压的话"百度网盘"会被判成百度。
    shorter, longer = sorted((a, b), key=len)
    return jaccard * (len(shorter) / len(longer))


_DOMAIN_RE = re.compile(
    r"^(?:https?://)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}(?::\d{1,5})?(?:/\S*)?$"
)


def looks_like_domain(text: str) -> bool:
    """`example.com` / `https://a.b.cn/x` 这类。用来放行用户直接说域名。

    刻意要求**真正的顶级域**（两个以上字母），否则「12306」这种会被误判成域名
    而去打开一个不存在的站。
    """
    return bool(_DOMAIN_RE.match(strip_punct(text).lower()))


def normalize_url(text: str) -> str:
    """补协议。`example.com` → `https://example.com`。"""
    t = strip_punct(text)
    if "://" in t or t.endswith(":"):
        return t
    return "https://" + t


def search_url(query: str, template: str = DEFAULT_SEARCH) -> str:
    """搜索兜底的网址。"""
    from urllib.parse import quote

    return template.format(q=quote(strip_punct(query)))


__all__ = [
    "DEFAULT_SEARCH",
    "Site",
    "WebIndex",
    "looks_like_domain",
    "normalize_url",
    "search_url",
]
