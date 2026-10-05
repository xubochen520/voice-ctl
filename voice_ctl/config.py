"""配置：TOML 读取 + 校验 + 默认值。

设计要点：**加一个新能力不需要改代码**——在 config.toml 里加一段 [[action]] 即可。
只有需要全新行为（模拟按键、系统操作）才写 Python 类。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

_FALLBACK_HANDLERS = frozenset({
    "open_app", "open_target", "open_path", "open_url", "sysctl", "keys", "shell", "close_app", "schedule",
})
"""handler 名单的兜底。

正常情况下从 `actions.HANDLERS` 现取——**名单只该有一份**。以前这里写死了一份
副本，结果新加的 handler（open_target / close_app / schedule）在 `build_registry`
里认得、在配置校验里却不认得，报错说"不认识"，而它明明就在注册表里躺着。
兜底只在 import 失败时用得上（比如只读环境里跑配置校验）。"""


def valid_handlers() -> frozenset[str]:
    try:
        from .actions import HANDLERS

        return frozenset(HANDLERS)
    except Exception:  # noqa: BLE001 - 拿不到就用兜底，不要因为校验名单让配置读不了
        return _FALLBACK_HANDLERS


VALID_HANDLERS = _FALLBACK_HANDLERS
"""历史名字，保持向后兼容。新代码请用 `valid_handlers()`。"""


class ConfigError(Exception):
    """配置非法。消息里必须说清哪个键、期望什么、实际是什么。"""


# --------------------------------------------------------------------------- #
# 各段
# --------------------------------------------------------------------------- #


@dataclass
class HotkeyConfig:
    """全局热键。按住说话（push-to-talk），松开即停。"""

    keys: str = "<ctrl>+<alt>+space"
    """pynput 语法。'<ctrl>+<alt>+space' / '<ctrl>+<alt>+j' / '<f9>'。"""

    min_duration_ms: int = 200
    """短于此时长的按键视为误触，不触发识别。"""

    max_duration_ms: int = 15000
    """超过此时长强制停止录音，防止按键卡住导致无限录音。"""

    def validate(self) -> None:
        if not self.keys.strip():
            raise ConfigError("[hotkey].keys 不能为空")
        if self.min_duration_ms < 0:
            raise ConfigError(f"[hotkey].min_duration_ms 不能为负，实际 {self.min_duration_ms}")
        if self.max_duration_ms <= self.min_duration_ms:
            raise ConfigError(
                f"[hotkey].max_duration_ms ({self.max_duration_ms}) 必须大于 "
                f"min_duration_ms ({self.min_duration_ms})"
            )


@dataclass
class AudioConfig:
    samplerate: int = 16000
    channels: int = 1
    device: int | str | None = None
    """留空用系统默认输入设备。可用 `voice-ctl devices` 列出序号。"""

    min_peak: float = 0.01
    """整段峰值低于此值判为静音，直接跳过识别（0 = 关闭该过滤）。

    **用峰值而不是 RMS 做静音判据**，这是个实测得出的结论：
    安静环境下的真实说话，RMS 可能只有 0.002（和纯底噪同一量级），
    但峰值通常在 0.05 以上。用 RMS 当阈值会把用户小声说话静默丢掉——
    表现为"有时候喊了没反应"，极难排查。峰值判据对这两种情况区分得很干净。
    """

    def validate(self) -> None:
        if self.samplerate != 16000:
            raise ConfigError(
                f"[audio].samplerate 必须是 16000（SenseVoice 要求），实际 {self.samplerate}"
            )
        if self.channels != 1:
            raise ConfigError(f"[audio].channels 必须是 1（单声道），实际 {self.channels}")
        if not 0.0 <= self.min_peak <= 1.0:
            raise ConfigError(f"[audio].min_peak 必须在 0.0-1.0，实际 {self.min_peak}")


@dataclass
class ModelConfig:
    dir: str = "models/sense-voice-int8"
    language: str = "auto"
    use_itn: bool = True
    num_threads: int = 2
    provider: str = "cpu"

    pad_ms: int = 300
    """识别前在波形头尾各补多少毫秒静音（0 = 不补）。

    实测不补时首字会随机丢失（「明天」→「天」），补 200ms 以上就稳了。
    见 asr.DEFAULT_PAD_MS 的说明。"""

    def validate(self) -> None:
        if not 0 <= self.pad_ms <= 2000:
            raise ConfigError(f"[model].pad_ms 必须在 0-2000，实际 {self.pad_ms}")
        if self.language not in ("auto", "zh", "en", "ja", "ko", "yue"):
            raise ConfigError(
                f"[model].language 只能是 auto/zh/en/ja/ko/yue 之一，实际 {self.language!r}"
            )
        if self.num_threads < 1:
            raise ConfigError(f"[model].num_threads 至少为 1，实际 {self.num_threads}")
        if self.provider not in ("cpu", "cuda", "coreml"):
            raise ConfigError(f"[model].provider 不支持 {self.provider!r}（cpu/cuda/coreml）")


@dataclass
class MatchConfig:
    threshold: int = 80
    """别名模糊匹配阈值 0-100。越高越严格。"""

    strip_prefixes: list[str] = field(
        default_factory=lambda: ["请", "帮我", "帮忙", "麻烦", "给我", "我要", "我想", "我要你", "呃", "嗯"]
    )
    """识别结果开头的口语词，匹配前剥掉。"""

    strip_suffixes: list[str] = field(
        default_factory=lambda: ["谢谢", "吧", "啊", "呀", "呢", "一下", "好么", "好吗"]
    )
    """结尾的语气词，匹配前剥掉。"""

    inline_fillers: list[str] = field(default_factory=lambda: ["一下", "一个", "那个", "这个"])
    """句中填充词，任意位置都删（"打开一下微信" → "打开微信"）。

    只放不会出现在正式命令词里的词——「打开」这种放进来会把命令本身吃掉。
    """

    def validate(self) -> None:
        if not 0 <= self.threshold <= 100:
            raise ConfigError(f"[match].threshold 必须在 0-100，实际 {self.threshold}")


@dataclass
class DecisionConfig:
    """第 1 层语义决策（可选）。第 0 层别名匹配搞不定时才用。"""

    enabled: bool = False
    model: str = "multilingual"
    """务必用 multilingual：中文用 english checkpoint 接近随机。"""

    min_confidence: float = 0.6
    """低于此置信度不执行，改为提示确认。"""

    onnx_dir: str = "models/laya-onnx/multilingual"
    """Laya 的 ONNX 权重目录（放 rl_agent_config.json + tokenizer/ + *.onnx）。

    相对路径按**配置文件所在目录**解析，不是当前工作目录——否则从别处
    运行 voice-ctl 时会找不到权重。
    """

    def validate(self) -> None:
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ConfigError(
                f"[decision].min_confidence 必须在 0.0-1.0，实际 {self.min_confidence}"
            )


@dataclass
class NormalizeCfg:
    """文本归一化的用户覆盖项。

    默认表在 normalize.py 的 DEFAULT_SUBSTITUTIONS；这里只写你要**追加或覆盖**
    的条目，改配置不用动代码。
    """

    substitutions: dict[str, str] = field(default_factory=dict)
    """同音/近音误识别替换：{"识别错的写法": "正确写法"}。

    例：{"围信": "微信", "记时本": "记事本"}
    发现 ASR 老是把某个词听错，往这里加一条即可，不用改代码。
    """

    use_pinyin: bool = True
    """是否用 pypinyin 做同音判定（装了才生效；也用于匹配时的拼音键）。"""


@dataclass
class IntentConfig:
    """意图层：动词/否定/时间/动态应用词典（见 voice_ctl/intent.py）。

    它修的是别名匹配的三个实测缺陷：「设置今天下午三点的日程」被当成打开设置、
    「关闭微信」反而打开微信、「打开QQ」因为没有对应 [[action]] 而什么都匹配不上。

    默认开着——这三个缺陷都比意图层本身的风险更常见。真觉得它误判了，
    把它关掉就退回纯别名匹配，行为和 0.2.0 完全一致。
    """

    enabled: bool = True

    confirm_timeout: float = 12.0
    """确认卡等多久。超时按"放弃"处理——总比无限期占着工作线程好。"""

    def validate(self) -> None:
        if self.confirm_timeout <= 0:
            raise ConfigError("intent.confirm_timeout 必须大于 0")


@dataclass
class ScheduleConfig:
    """日程 / 提醒（见 voice_ctl/schedule.py）。

    默认落地在可写数据目录的 schedule.json，不依赖任何账号或云端日历。
    """

    enabled: bool = True

    data_file: str = ""
    """日程文件位置。留空 = 可写数据目录下的 schedule.json。相对路径按配置文件所在目录解析。"""

    remind_before: int = 0
    """默认提前几分钟提醒。"""

    def validate(self) -> None:
        if self.remind_before < 0:
            raise ConfigError("schedule.remind_before 不能是负数")


@dataclass
class WebConfig:
    """网页解析：「打开百度」→ baidu.com（见 voice_ctl/web.py）。

    为什么单独一段而不是往动作表里塞几十条 `[[action]]`：动作表是**用户自己配
    固定几个站**的地方（`target = "https://..."`），而这里是"说得出名字就该开得
    出来"的那层兜底。两者互补，不重复。
    """

    enabled: bool = True
    """关掉就退回旧行为：只有配置里写了 target 的站才开得了。"""

    search_fallback: bool = True
    """站点表里没有的名字，走一次搜索而不是直接失败。

    默认开着，因为它把"什么都没发生"变成"至少给了你搜索结果"。代价是**联网**
    ——搜索页要能上网。彻底离线用的话把它关掉：站点表本身不联网，关了之后
    「打开百度」照样能用。"""

    search_url: str = "https://www.baidu.com/s?wd={q}"
    """搜索模板，`{q}` 会被替换成 URL 编码后的查询词。"""

    def validate(self) -> None:
        if self.search_fallback and "{q}" not in self.search_url:
            raise ConfigError("web.search_url 里必须有 {q} 占位符，否则查询词没地方放")


@dataclass
class LLMConfig:
    """可选的小模型层：用本地跑着的大模型兜住前几层的长尾（见 voice_ctl/llm.py）。

    默认**关闭**，因为它要额外下 40MB 运行时 + 241MB~1GB 模型。

    两种后端：

        local（默认）  自己下 llama.cpp 的 llama-server 和一个小 GGUF 模型。
                       打包后的应用**不需要任何外部软件**——这是内置的全部意义。
        server         接一个已经在跑的服务（LM Studio 1234 / Ollama 11434）。

    **实测（一台 i7-13650HX，CPU 推理，10 条用例里 6 条真该命中、4 条是负样本）**：

        模型                        加载     单次      命中      负样本误触发
        qwen2.5-0.5b Q4_K_M        0.7s    90ms     0/6       0/4
        qwen3-0.6b Q8_0            0.9s   199ms     0/6       0/4
        qwen2.5-1.5b Q4_K_M        1.4s   110ms     6/6       0/4

    0.5B/0.6B 档位几乎只会答 null：**不误触发，但也帮不上忙**。真想让它干活，
    用 1.5B（`model = "qwen2.5-1.5b-instruct"`，1GB）。
    如果你已经开了 Laya 语义层（`[decision]`，20 选项下实测 6/6），这一层能补的
    很有限——它的价值在"补充"而不是"替代"。
    """

    enabled: bool = False

    backend: str = "local"
    """local = 内置 llama.cpp；server = 外部 OpenAI 兼容服务。"""

    endpoint: str = "http://127.0.0.1:1234/v1"
    """backend = "server" 时的服务地址。Ollama 用 http://127.0.0.1:11434/v1。"""

    model: str = "qwen2.5-0.5b-instruct"
    """backend = "local"：MODELS 里的名字，或模型目录里任意 .gguf 的文件名。
    backend = "server"：留空用服务上加载的第一个模型。"""

    timeout: float = 4.0
    """外部服务单次请求超时（秒）。本机小模型通常 0.3-2 秒；超过说明它在算别的。"""

    request_timeout: float = 30.0
    """内置 llama.cpp 单次请求超时（秒）。第一次请求要把模型读进内存，给宽一点。"""

    ctx_size: int = 2048
    """内置 llama.cpp 的上下文长度。抽槽位只要几百 token，不必给大。"""

    threads: int = 0
    """内置 llama.cpp 的线程数。0 = 自动（CPU 核数的一半）。"""

    startup_timeout: float = 60.0
    """等 llama-server 就绪的上限（秒）。冷盘上加载 1GB 模型可能要十几秒。"""

    max_candidates: int = 12
    """给模型看几个候选。它越多越容易乱挑（官方 20 选项任务只有 0.451）。"""

    def validate(self) -> None:
        if self.timeout <= 0 or self.request_timeout <= 0:
            raise ConfigError("llm.timeout / llm.request_timeout 必须大于 0")
        if self.max_candidates < 1:
            raise ConfigError("llm.max_candidates 至少是 1")
        if self.ctx_size < 256:
            raise ConfigError("llm.ctx_size 太小了，至少 256")
        if self.threads < 0:
            raise ConfigError("llm.threads 不能是负数")
        if self.backend not in ("local", "server"):
            raise ConfigError(f"llm.backend 只支持 local / server，实际是 {self.backend!r}")
        if self.enabled and self.backend == "server" and not self.endpoint.strip():
            raise ConfigError("llm.backend = \"server\" 时必须给出 endpoint")


@dataclass
class FeedbackConfig:
    beep: bool = True
    """开始/结束录音的提示音。"""

    beep_start_hz: int = 880
    beep_end_hz: int = 1320
    beep_ms: int = 70

    print_result: bool = True
    """把 识别文本 / 匹配结果 / 是否执行 打到控制台。"""


@dataclass
class ActionConfig:
    """一个可被语音触发的动作。"""

    id: str
    handler: str
    """open_app / open_path / open_url / sysctl / keys / shell"""

    aliases: list[str] = field(default_factory=list)
    """语音里可能出现的说法，全部小写比较。第 0 层匹配靠它。"""

    describe: str = ""
    """自然语言描述，第 1 层语义决策当 criteria 用。留空则用 aliases 拼。"""

    target: str = ""
    """handler 的目标：exe 路径 / 文件夹 / URL / 系统操作名。"""

    args: list[str] = field(default_factory=list)
    """额外参数（open_app 的命令行参数、keys 的键序列等）。"""

    enabled: bool = True

    def validate(self) -> None:
        if not self.id.strip():
            raise ConfigError("[[action]] 缺少 id")
        if self.handler not in valid_handlers():
            raise ConfigError(
                f"动作 {self.id!r} 的 handler={self.handler!r} 不认识；"
                f"支持：{', '.join(sorted(valid_handlers()))}"
            )
        if not self.aliases and not self.describe and not self.target:
            raise ConfigError(f"动作 {self.id!r} 既没有 aliases 也没有 target，无法匹配")
        # open_url 允许 target 留空：地址可以由意图层在运行时给出（`open.web`
        # 那条就是——「打开百度」的网址来自站点表，见 voice_ctl/web.py）。
        # 其它 handler 没有"运行时才知道目标"这回事，照旧要求写死。
        if self.handler in ("open_path", "shell", "sysctl") and not self.target:
            raise ConfigError(f"动作 {self.id!r} 的 handler={self.handler} 必须提供 target")


# --------------------------------------------------------------------------- #
# 顶层
# --------------------------------------------------------------------------- #


@dataclass
class AppConfig:
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    normalize: NormalizeCfg = field(default_factory=NormalizeCfg)
    match: MatchConfig = field(default_factory=MatchConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    intent: IntentConfig = field(default_factory=IntentConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    web: WebConfig = field(default_factory=WebConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    actions: list[ActionConfig] = field(default_factory=list)

    source: Path | None = None
    """配置来源文件，便于报错时指出位置。"""

    def validate(self) -> None:
        self.hotkey.validate()
        self.audio.validate()
        self.model.validate()
        self.match.validate()
        self.decision.validate()
        self.intent.validate()
        self.schedule.validate()
        self.web.validate()
        self.llm.validate()

        if not self.actions:
            raise ConfigError("配置里没有任何 [[action]]，程序无可执行的动作")

        seen: set[str] = set()
        for a in self.actions:
            a.validate()
            if a.id in seen:
                raise ConfigError(f"动作 id 重复：{a.id!r}")
            seen.add(a.id)

        if not any(a.enabled for a in self.actions):
            raise ConfigError("所有动作都是 enabled = false，程序无事可做")

    @property
    def enabled_actions(self) -> list[ActionConfig]:
        return [a for a in self.actions if a.enabled]

    def model_path(self) -> Path:
        p = Path(self.model.dir).expanduser()
        if not p.is_absolute() and self.source is not None:
            p = self.source.parent / p
        return p.resolve()

    def decision_path(self) -> Path:
        """语义层权重目录。相对路径按配置文件所在目录解析。"""
        p = Path(self.decision.onnx_dir).expanduser()
        if not p.is_absolute() and self.source is not None:
            p = self.source.parent / p
        return p.resolve()

    def schedule_path(self) -> Path | None:
        """日程文件位置。留空返回 None，由 schedule.default_store_path() 决定
        （它会去看 VOICE_CTL_SCHEDULE_FILE 环境变量和可写数据目录）。"""
        raw = (self.schedule.data_file or "").strip()
        if not raw:
            return None
        p = Path(raw).expanduser()
        if not p.is_absolute() and self.source is not None:
            p = self.source.parent / p
        return p.resolve()

    def describe(self) -> str:
        lines = [
            f"配置文件   : {self.source}",
            f"热键       : {self.hotkey.keys}  (按住说话，最短 {self.hotkey.min_duration_ms}ms)",
            f"模型目录   : {self.model_path()}",
            f"识别参数   : language={self.model.language} itn={self.model.use_itn} "
            f"threads={self.model.num_threads} provider={self.model.provider}",
            f"匹配阈值   : {self.match.threshold}",
            f"语义决策   : {'开启' if self.decision.enabled else '关闭'}",
            f"意图层     : {'开启' if self.intent.enabled else '关闭'}"
            + ("（动词/否定/动态应用/日程）" if self.intent.enabled else "（只用别名匹配）"),
            f"日程提醒   : {'开启' if self.schedule.enabled else '关闭'}"
            + (f"  {self.schedule_path() or '（默认数据目录）'}" if self.schedule.enabled else ""),
            f"小模型层   : {'开启 ' + self.llm.endpoint if self.llm.enabled else '关闭'}",
            f"动作数     : {len(self.enabled_actions)} / {len(self.actions)} 启用",
        ]
        for a in self.enabled_actions:
            alias = ", ".join(a.aliases[:5]) or "(无语)"
            lines.append(f"  - {a.id:24} [{a.handler}] {alias}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #


def _builtin_actions() -> list[ActionConfig]:
    """内置动作兜底。

    正常情况下动作来自 config.toml；但打包成 exe 后用户可能把配置文件删了或改坏了，
    此时**不该让程序直接不可用**——给一份够用的默认集，让 doctor 能提示他去恢复配置。

    与仓库里 config.toml 的动作集保持一致（那份是权威，这里是安全网）。
    """
    raw: list[tuple[str, str, list[str], str, str]] = [
        ("open.wechat", "open_app", ["微信", "威信", "wechat", "聊天", "发消息"], "打开微信聊天发消息", ""),
        ("open.browser", "open_url", ["浏览器", "打开浏览器", "上网", "chrome"], "打开浏览器上网", "https://www.bing.com"),
        ("open.explorer", "sysctl", ["文件夹", "资源管理器", "我的电脑"], "打开文件资源管理器", "explorer"),
        ("open.notepad", "open_app", ["记事本", "notepad", "笔记本", "记录"], "打开记事本写字", "notepad.exe"),
        ("open.calc", "open_app", ["计算器", "calc", "算一下"], "打开计算器", "calc.exe"),
        ("open.terminal", "open_app", ["终端", "命令行", "cmd", "控制台"], "打开终端命令行", "wt.exe"),
        ("open.taskmgr", "open_app", ["任务管理器", "进程管理"], "打开任务管理器", "taskmgr.exe"),
        ("open.settings", "open_url", ["设置", "系统设置"], "打开系统设置", "ms-settings:"),
        ("sys.volume_up", "sysctl", ["音量加", "声音大点", "大声点", "音量大"], "调高系统音量", "volume_up"),
        ("sys.volume_down", "sysctl", ["音量减", "声音小点", "小声点", "音量小"], "调低系统音量", "volume_down"),
        ("sys.mute", "sysctl", ["静音", "别出声", "关声音"], "系统静音或取消静音", "mute"),
        ("sys.lock", "sysctl", ["锁屏", "锁定电脑"], "锁定电脑屏幕", "lock"),
        ("sys.screenshot", "sysctl", ["截屏", "截图", "屏幕截图"], "截取整个屏幕", "screenshot"),
        ("sys.show_desktop", "sysctl", ["显示桌面", "回到桌面"], "最小化所有窗口显示桌面", "show_desktop"),
    ]
    return [
        ActionConfig(id=i, handler=h, aliases=a, describe=d, target=t)
        for i, h, a, d, t in raw
    ]


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    v = raw.get(name, {})
    if not isinstance(v, dict):
        raise ConfigError(f"[{name}] 必须是一个表（[section]），实际是 {type(v).__name__}")
    return v


def _check_type(value: Any, hint: Any, where: str) -> None:
    """按 dataclass 的注解做基础类型校验。

    为什么需要：TOML 里把布尔写成字符串（`use_itn = "true"`）或把数字写成
    字符串都能解析成功，然后一路静默地当成真值用下去——这类 bug 极难查。
    另外 TOML 的布尔**必须小写**（`true`），写成 Python 风格的 `True` 会直接
    是语法错误，所以这里只负责挡住"能解析但类型不对"的情况。
    """
    if hint is Any or hint is None:
        return
    origin = getattr(hint, "__origin__", None)
    if origin is Union or str(origin) == "<class 'types.UnionType'>":  # Optional[X] / X | Y
        for sub in getattr(hint, "__args__", ()):
            if sub is type(None):
                continue
            try:
                _check_type(value, sub, where)
                return
            except ConfigError:
                continue
        raise ConfigError(f"{where} 的类型不对（期望 {hint}，实际 {type(value).__name__}）")
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{where} 必须是数组，实际是 {type(value).__name__}")
        (item_hint,) = getattr(hint, "__args__", (Any,)) or (Any,)
        for i, item in enumerate(value):
            _check_type(item, item_hint, f"{where}[{i}]")
        return
    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{where} 必须是表，实际是 {type(value).__name__}")
        return

    expected = {int: (int,), float: (int, float), str: (str,), bool: (bool,)}.get(hint)
    if expected is None:
        return
    if hint is bool:
        # bool 是 int 的子类，所以必须先单独判 bool，否则 True 会被当成合法 int
        if not isinstance(value, bool):
            raise ConfigError(
                f"{where} 必须是布尔值 true/false（TOML 里是小写），"
                f"实际是 {type(value).__name__}：{value!r}"
            )
        return
    if hint in (int, float) and isinstance(value, bool):
        raise ConfigError(f"{where} 期望数字，实际是布尔值 {value!r}")
    if not isinstance(value, expected):
        raise ConfigError(
            f"{where} 期望 {hint.__name__}，实际是 {type(value).__name__}：{value!r}"
        )


def _resolved_hints(cls: Any) -> dict[str, Any]:
    """解析 dataclass 的类型注解。

    本模块有 `from __future__ import annotations`，所有注解都是**字符串**，
    直接拿 `field.type` 去比较会永远不相等——防护会变成摆设。必须解析。
    """
    import sys
    import typing

    module = sys.modules[__name__]
    try:
        return typing.get_type_hints(cls, vars(module))
    except Exception:  # noqa: BLE001 - 解析不了就退化为不校验
        return {name: Any for name in cls.__dataclass_fields__}


def _build(cls, data: dict[str, Any], section: str):
    """按 dataclass 字段过滤未知键 + 校验类型。未知键报错而不是静默忽略。"""
    fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
    known = set(fields)
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"[{section}] 有无法识别的键：{', '.join(sorted(unknown))}；"
            f"可用键：{', '.join(sorted(known))}"
        )
    hints = _resolved_hints(cls)
    for key, value in data.items():
        _check_type(value, hints.get(key, Any), f"[{section}].{key}")
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError(f"[{section}] 参数有误：{e}") from e


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载配置。

    path 为空时用 bootstrap.resolve_config() 定位：
        --config 显式指定 > 可写数据目录（用户改过的那份）> 打包内模板 > cwd
    都找不到时**回落到内置默认值**而不是报错——打包成 exe 后用户机器上
    本来就没有配置文件，此时应当能直接跑起来。
    """
    if path is None:
        from .bootstrap import resolve_config

        found = resolve_config(None)
        if found is None:
            cfg = AppConfig(actions=_builtin_actions())
            cfg.validate()
            return cfg
        path = found

    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ConfigError(f"配置文件不存在：{p}")

    with p.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"TOML 语法错误（{p}）：{e}") from e

    unknown_top = set(raw) - {
        "hotkey", "audio", "model", "normalize", "match", "decision", "intent", "schedule",
        "web", "llm", "feedback", "action"
    }
    if unknown_top:
        raise ConfigError(
            f"顶层有无法识别的段：{', '.join(sorted(unknown_top))}；"
            "可用：hotkey / audio / model / normalize / match / decision / intent / "
            "schedule / web / llm / feedback / action"
        )

    raw_actions = raw.get("action", [])
    if not isinstance(raw_actions, list):
        raise ConfigError("[[action]] 必须是数组表（双中括号）")

    cfg = AppConfig(
        hotkey=_build(HotkeyConfig, _section(raw, "hotkey"), "hotkey"),
        audio=_build(AudioConfig, _section(raw, "audio"), "audio"),
        model=_build(ModelConfig, _section(raw, "model"), "model"),
        normalize=_build(NormalizeCfg, _section(raw, "normalize"), "normalize"),
        match=_build(MatchConfig, _section(raw, "match"), "match"),
        decision=_build(DecisionConfig, _section(raw, "decision"), "decision"),
        intent=_build(IntentConfig, _section(raw, "intent"), "intent"),
        schedule=_build(ScheduleConfig, _section(raw, "schedule"), "schedule"),
        web=_build(WebConfig, _section(raw, "web"), "web"),
        llm=_build(LLMConfig, _section(raw, "llm"), "llm"),
        feedback=_build(FeedbackConfig, _section(raw, "feedback"), "feedback"),
        actions=[
            _build(ActionConfig, a, f"action[{i}]")
            for i, a in enumerate(raw_actions)
            if isinstance(a, dict)
        ],
        source=p,
    )
    cfg.validate()
    return cfg
