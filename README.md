# voice-ctl

按住快捷键说话 → **本地离线**识别 → 理解整句话 → 执行。

说「打开微信」就打开微信，说「把音量调小一点」就调低音量，
说「设置今天下午三点的日程我要玩游戏」就真的多一条下午三点的提醒。全程不联网、零调用成本。

- **纯离线**：识别模型跑在本地，音频不出机器
- **低开销**：无 GPU；默认路径不需要 torch，模型常驻约 350MB，识别 RTF≈0.04
- **有界面**：双击就有窗口——实时日志、录热键、改动作、调参数，不用碰配置文件
- **听得懂整句话**：「关闭微信」不会打开微信，「设置…日程」不会被当成打开设置
- **说得出名字的网站就能开**：「打开百度」「打开B站」→ 对应网站；本机装了同名程序时以程序为先
- **说错了能收回**：「打开百度网盘，呸」不执行；「打开百度，不对，打开淘宝」打开淘宝
- **打开任何装了的软件**：「打开QQ」不用先在配置里写一段
- **能记日程**：说一句话就有一条会准时响的提醒，还能导出 `.ics` 进手机日历
- **可拓展**：加一个新能力 = 在 `config.toml` 里加一段 `[[action]]`，不用改代码
- **可解释**：`--dry-run` 能看到它听成了什么、判断走的是哪一层、为什么

---

## 下载

最新版 **[v0.3.5](https://github.com/xubochen520/voice-ctl/releases/latest)**：

| 产物 | 体积 | 说明 |
|---|---|---|
| [voice-ctl-0.3.5-win64-lite.exe](https://github.com/xubochen520/voice-ctl/releases/download/v0.3.5/voice-ctl-0.3.5-win64-lite.exe) | 84MB | **推荐**。首次用要跑一次 `voice-ctl download` 拉识别模型（226MB），之后每次启动都快一倍 |
| [voice-ctl-0.3.5-win64-full.exe](https://github.com/xubochen520/voice-ctl/releases/download/v0.3.5/voice-ctl-0.3.5-win64-full.exe) | 236MB | 开箱即用，识别模型内嵌。代价是每次启动都要把 226MB 解包到临时目录 |
| `voice-ctl-0.3.5-win64-semantic.zip` | 1.4GB | **完全离线**：连语义层和 906MB 权重都内置，什么都不用下。目录版（解压即用） |

前两个都是单文件 exe，双击就出界面（命令行也一样用）。功能完全相同，只差识别模型是否内嵌。
语义版是目录版，因为 1.8GB 每次启动都解包到临时目录不可接受。

**要用语义层只有 `semantic` 那个包可以**——前两个没带 torch（约 500MB），开语义层会报
`No module named 'torch'`。这是体积取舍，不是缺陷。

从源码跑：

```powershell
pip install -e .
voice-ctl ui
```

---

## 0.3.5：修掉「从界面点下载，906MB 白下」

0.3.4 修了 `voice-ctl fetch-decision`：构建加载不了语义层时，下载**之前**劝阻。
但只修了 CLI 那一半——界面「设置」页那个下载按钮走的是**另一份**代码，
没有那道检查，而且落盘还在用原始相对路径（会落到当前工作目录）。

用户实测撞到：跑完整版（不带语义层），从界面点下载，老实下完 906MB，
开语义层才看到 `No module named 'torch'`。

现在判断抽成一份 `decision.preflight_fetch()`，CLI 和界面共用。
界面那边**直接拒绝**并说清该换哪个构建——点一个按钮就该是有效动作，
弹「要不要继续」然后照样下 906MB 算不上保护。

详见 [0.3.5 版本说明](docs/RELEASE-NOTES-0.3.5.md)。

---

## 0.3.4：语义模型内置（但默认不开）

这一版把语义层的 **906MB 权重内置**进包，加了一个 `semantic` 变体：解压即用，
**一个字节都不用下载**，也不依赖 github.com 或 huggingface.co 可达。体积 1.8GB。

真正要紧的不是那个开关，而是它暴露出的一个缺口：ASR 模型一直有
`resolve_model_dir()` 兜底（配置路径不存在就去 `_MEIPASS` 里找），
**语义层没有**。只加打包开关的话，程序照样报「缺少 *.onnx」——
873MB 打进去了却完全没效果，而且报错不指向真因。现在补上了
`resolve_decision_dir()`，四处调用点全部改用它。

顺带修掉一个不该发生的重复下载：`voice-ctl fetch-decision` 以前**无条件**下 906MB
——内置了也照下。现在已就绪就短路，并把落盘位置从"当前工作目录"改成可写数据目录
（以前从 `C:\Windows\System32` 里跑这个命令，906MB 就下到那儿去了）。

### ⚠ 语义层**没有**默认打开，这是量过之后的决定

本来打算"既然权重都内置了，就顺手自动打开"。写完测完撤销了：
在 21 个候选动作的真实配置下，**它的置信度和正确性不相关**。

| 该不该命中 | 说法 | 置信度 | 选了什么 |
|---|---|---|---|
| 该 | 帮我截个图 | 0.933 | sys.screenshot ✓ |
| 该 | 我想聊个天 | **0.153** | open.wechat ✓ |
| **不该** | 这个多少钱 | **1.000** | **web.bilibili** ✗（真的开 B 站） |
| **不该** | 今天天气怎么样 | 0.921 | schedule ✗ |

正确判断低到 0.153，错误判断高到 1.000——**任何阈值都拦不住后者**。
`min_confidence = 0.6` 是在 5 候选下定的，换成 21 个候选后正确判断被摊薄
（「我想聊个天」0.933 → 0.153）。所以默认打开等于默认乱执行。

**权重的内嵌解决的是"装完不用下载"，不是"默认该开"。** 这两件事被分开了：
解压即用、一个字节不下载 ✓，但要开语义层仍得把 `[decision].enabled` 改成 `true`。
开之前建议先 `voice-ctl simulate "你的说法"` 试几条，`config.toml` 里那段注释
也换成了上面这张实测表。

（好消息：句首否定是安全的——「不要打开计算器」「别关微信」在意图层就被拦下了，
走不到语义层。）

详见 [0.3.4 版本说明](docs/RELEASE-NOTES-0.3.4.md)。

---

## 0.3.3：说得出名字的网站就能开

```powershell
打开百度网页        # → baidu.com
打开B站             # → bilibili.com
打开百度网盘，呸     # → 什么都不做（用户自己收回了）
打开浏览器并且打开百度页面   # → 两件事都做
```

内置约 70 个常见站的站点表，不联网。本机装了同名程序时**以程序为先**：
「打开微信」开的是微信，不是网页；但「打开百度网盘」开的是网盘，而「打开百度」
开的是 baidu.com。

改口也认：「打开百度，不对，打开淘宝」会打开淘宝。一句话里说两件事也会拆开做
（「打开记事本然后打开计算器」）。

详见 [0.3.3 版本说明](docs/RELEASE-NOTES-0.3.3.md)。

---

## 0.3.1：内置 llama.cpp

0.3.0 把小模型层做成了"客户端"——它假设用户自己在跑 LM Studio 或 Ollama。
这一版把另一半补齐：**应用自己带 llama.cpp**，不需要装任何外部软件。

```powershell
voice-ctl llm --install      # llama.cpp 运行时：17.7MB 下载 / 39.8MB 磁盘
voice-ctl llm --download     # 模型：默认 0.5B，469MB
voice-ctl llm "有个文件要改一下"
```

⚠ **实测这个档位的能力边界**（10 条用例 = 6 条该命中 + 4 条负样本）：

| 模型 | 加载 | 单次 | 该命中 | 负样本误触发 |
|---|---|---|---|---|
| qwen2.5-0.5b Q4_K_M（469MB） | 0.7s | 90ms | 0/6 | 0/4 |
| qwen3-0.6b Q8_0（610MB） | 0.9s | 199ms | 0/6 | 0/4 |
| qwen2.5-1.5b Q4_K_M（1GB） | 1.4s | 110ms | **6/6** | 0/4 |

0.5B/0.6B **几乎只会答 `null`**——不误触发，但也帮不上忙。真想让它干活用
`model = "qwen2.5-1.5b-instruct"`。细节见下面「④ 小模型层」。

---

## 0.3.0：从「匹配别名」到「理解句子」

0.2.0 的判定模型是一句话：**话里出现了某个别名，就执行那个动作**。它对
「打开微信」很好，对下面这些真实说法就是错的——全部是在这台机器上实测出来的：

| 你说 | 0.2.0 干了什么 | 为什么 |
|---|---|---|
| 设置今天下午三点的日程我要玩游戏 | 打开 Windows 设置 | 别名「设置」被命中（0.912），动词的宾语其实是「日程」 |
| 关闭微信 | **打开**微信 | 别名「微信」被命中（0.950），否定和动词被完全忽略 |
| 不要打开记事本 | 打开记事本 | 「不要」没有意义 |
| 打开QQ | 什么都没发生 | 配置里没有 QQ 这条 `[[action]]` |
| 打开QQ音乐（没装） | 打开浏览器 | 「打开浏览器」以 0.633 勉强命中 |

0.3.0 在别名匹配**前面**加了一层意图识别，并在它**后面**补了两层兜底：

```
按住热键 ─► 录音 ─► SenseVoice ─► 归一化
                                    │
                    ┌───────────────┴───────────────┐
                    │  ① 意图层（约 1ms，不需要模型）│  动词 / 否定 / 时间 / 动态应用词典
                    │  ② 别名匹配（约 1ms）          │  与 0.2.0 完全一致
                    │  ③ 语义决策（可选，40-100ms）  │  Laya ONNX，分类器
                    │  ④ 小模型层（可选，数百 ms）   │  本机大模型服务，只抽槽位
                    └───────────────┬───────────────┘
                                    ▼
                                  执行
```

**顺序不能换**：越往前越便宜、越确定。①②不需要任何模型，覆盖绝大多数指令；
③④只在前面都没结果时才被调用——为一句「打开微信」去等一次模型推理是荒唐的。

### ① 意图层（新）

读的是整句话的结构，不是"里面有没有那个词"：

- **动词**：打开 / 关闭 / 强制关闭 / 设置…（`lexicon.py`，纯词表，可单测）
- **否定**：「不要打开记事本」「我不想打开计算器」→ 不执行，并说明原因
  （否定判定必须在剥客套话**之前**，否则「我不想」会被当成客套话剥掉）
- **时间**：把「今天下午三点」认成**参数**而不是名字的一部分，剥出来交给时间解析器
- **对象**：名字不只查配置，还查**已安装应用索引**——开始菜单里有什么就能开什么

一个关键的取舍：**原文优先**。归一化会把「一个/那个/一下」删掉、把同音字换掉，
那对匹配是好事，对日程标题是灾难。所以 `Intent.source` 逐字保留 ASR 原文。

### 打开任何装了的软件

「打开QQ」以前什么都不做，因为配置里没有 QQ。现在意图层会去开始菜单
（实测这台机器 195 项）里找：

- 名字匹配是**方向敏感**的：说「QQ音乐」不会落到「QQ」上（没装就如实说没找到）
- 拼音匹配兜住 ASR 的常态听错：威信 / 薇信 → 微信
- 昵称表兜住毫无字面关系的叫法：扣扣 / 企鹅 → QQ
- 拿不准时**问**而不是猜：得分够高、且和第二名拉开距离才直接执行

### 关闭应用

「关闭微信」「把微信关掉」「强制关闭微信」现在是真的关闭。三条实测踩出来的规矩：

> **按路径精确定位，不按进程名。** 这台机器上三个完全不同的启动器都叫
> `launcher.exe`（米哈游 / 鸣潮 / 鹰角）。按进程名关，用户说「关闭米哈游启动器」
> 会把鸣潮和鹰角一起关掉——而他多半不会立刻把这两件事联系起来。
> 所以：能拿到完整路径就按路径找到那**一个** PID 再关。
>
> **UWP 应用（记事本、计算器）在开始菜单里是 AUMID，没有 exe 路径。** 这时
> 我们拿系统命令名推出 `notepad.exe`，再从进程表里认领一个**同名**进程的完整
> 路径——认领的前提是名字真的相同（`notepad++.exe` 不算）。
> 直接按映像名 `taskkill /IM` 关它，实测 8 次错 3 次；按 PID 关，12 次全成。
>
> **有窗口的进程不带 `/F`。** `/F` 是立刻终止，Word 里没保存的文档会直接没。
> 所以先发优雅关闭，等 1.5 秒还没退才降级强杀。
> `explorer.exe` 是例外：它就是桌面本身，杀它会把任务栏和图标一起带走，
> 所以只关它的窗口。

还有两条关于**怎么判断成功**的：

- **不看 `taskkill` 的退出码**。实测目标确实关了、退出码却是 1。判据是
  "那个 PID 还在不在"（`OpenProcess` + `WaitForSingleObject`，6ms）。
  照着退出码报错，用户会看到「没能关掉」而窗口其实已经消失。
- **没在跑就说没在跑**。「它本来就没开」和「开着但关不掉（要管理员权限）」
  对用户是两件事，不能都报成一句"没找到"。

### 日程 / 提醒

说「明天早上八点提醒我开会」就有一条会准时响的提醒：

```powershell
voice-ctl schedule                              # 看接下来有什么
voice-ctl schedule --export 日程.ics            # 导出，双击就能进 Windows 日历/Outlook
voice-ctl schedule --clear                      # 清掉已提醒/已错过的
```

- **纯本地**：存在可写数据目录的 `schedule.json`，不依赖账号、不依赖任何云日历
- **本程序常驻，所以它自己负责到点响**——不需要 Outlook、不需要手机
- **标题逐字保留你的原话**：「设置今天下午三点的日程我要玩游戏」→ 标题是
  「我要玩游戏」，`note` 里存着整句原话，事后能核对
- **说两遍不会建两条**（同名同时刻的不再重复创建）
- **睡眠/关机之后不哑火也不翻旧账**：错过的补响一次并标明"已错过"，
  太久以前（默认 12 小时）的只标记不弹窗，否则开机就被三天前的提醒淹没
- **时间有歧义会说出来**：「三点」没说上午下午时按下午算，并在结果里写明
  「没说上午/下午，按下午算」——而不是悄悄猜一个

### ④ 小模型层（可选，默认关）

前三层覆盖的是"说得清楚"的指令。剩下的长尾是**缺字**：「有个文件要改一下」
（想开记事本）、「把声音关小」（想调音量）——这些用通用大模型补比训练分类器合适。

这一层**内置 llama.cpp**，不需要用户装任何外部软件：

```powershell
voice-ctl llm --install            # llama.cpp 运行时：17.7MB 下载 / 39.8MB 磁盘
voice-ctl llm --download           # 模型：默认 0.5B，469MB
voice-ctl llm --status             # 看看齐了没
voice-ctl llm "有个文件要改一下"    # 试一句，看它挑谁、花多久
# 满意了再把 config.toml 的 [llm].enabled 改成 true
```

想接已经在跑的服务（LM Studio / Ollama）也行：`backend = "server"` +
`endpoint = "http://127.0.0.1:11434/v1"`。两家都是 OpenAI 的 `/chat/completions`
协议，所以同一份代码通吃。

#### 实测：这个模型档位能不能用

这台机器（i7-13650HX，CPU 推理）。10 条用例 = 6 条真该命中 + 4 条负样本：

| 模型 | 加载 | 单次 | 真该命中 | 负样本误触发 |
|---|---|---|---|---|
| qwen2.5-0.5b Q4_K_M（469MB） | 0.7s | 90ms | **0/6** | 0/4 |
| qwen3-0.6b Q8_0（610MB） | 0.9s | 199ms | **0/6** | 0/4 |
| qwen2.5-1.5b Q4_K_M（1GB） | 1.4s | 110ms | **6/6** | 0/4 |

**0.5B/0.6B 档位几乎只会答 `null`**：不误触发，但也帮不上忙。我把它留作默认
下载项，是因为它 90ms、不占内存，当"多一道确认"用没有坏处；**真想让它干活得用
1.5B**（`model = "qwen2.5-1.5b-instruct"`）。

如果你已经开了 Laya 语义层（`[decision]`，20 选项下实测 6/6），这一层能补的
很有限——它的价值在"补充"而不是"替代"，以及给不想背 900MB Laya 权重、
想要一个能换模型的通用文本模型的人。

#### 为什么它不可能编造动作

这一层唯一的真实危险是模型吐出一个不存在的动作 id。两道锁：

1. **GBNF 语法约束**。llama.cpp 解码时按语法剪枝，只允许输出候选编号或 `null`
   ——模型**结构上不可能**越界。实测 20 次八竿子打不着的输入，一次都没越界。
   这不是"在提示词里求它别乱说"能比的。
2. **注册表校验**。即便没有语法（外部服务走的是普通 JSON），回来的 id 也必须
   在注册表里，否则丢弃并说出来。

还有三条刻意的约束：

- **只抽槽位，不执行**。模型能填的只有"选第几个候选"；时间由代码解析、
  动作由注册表执行。模型说"打开 rm -rf /"没有意义，它没有那个字段可填。
- **只在前面几层都没结果时才被调用**。为一句「打开微信」等一次推理是荒唐的。
- **不可用等于功能不存在**。没下模型、没装运行时都是常态，探测失败就静默
  退回前三层，日志留一行——绝不让助手因此无法执行本来能执行的指令。

为什么是 **CPU 版**：llama.cpp 的 Windows 构建里**没有 Vulkan**（只有 CPU /
CUDA / OpenVINO / SYCL / ROCm），而这个项目的前提是"无 GPU"。CPU 版还带一整套
`ggml-cpu-*.dll`（haswell / zen4 / sapphirerapids…），运行时按 CPU 指令集自动挑
一个，所以同一个包从 Sandy Bridge 到最新的机器都能跑，用户不用挑。

为什么只解压 **39.8MB**：完整的 zip 解压出来 120MB+，其中六成是 bench / quantize /
多模态那些永远用不上的 exe 和 impl.dll。留下的 22 个文件是逐个删掉再跑
`llama-server --version` 试出来的下限——包括看着像多模态才用的 `mtmd.dll`，
少了它直接 `0xC0000135`（DLL 找不到），而那个报错完全不指向真因。

只用标准库 `urllib`，不引入 openai 包——这一层不该给整个项目加运行时依赖。

---

## 界面

双击 exe（或 `voice-ctl ui`）就打开：

```
┌─ voice-ctl 0.2.0 ──────────────────────── ● 运行中 ─┐
│  ▶ 运行    │  状态   ● 运行中  ● 16 个动作  ● 模型已加载 │
│  ≡ 日志    │        热键 [Ctrl]+[Alt]+[空格]           │
│  ⌨ 快捷键  │        [启动监听] [加载识别模型] [重新加载] │
│  ✦ 功能    │  试一句  [____________________]  [发送]    │
│  ⚙ 设置    │  最近一次  ✓ 已启动 notepad.exe  总 118ms  │
│  ⓘ 关于    │                                          │
├──────────────────────────────────────────────────────┤
│ [启动监听] [加载模型]      未启动·按下 0·完成 0·识别 0  │
└──────────────────────────────────────────────────────┘
```

| 页面 | 能干什么 |
|---|---|
| **运行** | 看状态、启停监听、按住热键说话、**打字试一句**（走完全相同的链路，只是跳过录音） |
| **日志** | 实时事件流，按级别/关键字过滤、暂停、导出；同时写一份到 `voice-ctl.log` |
| **快捷键** | 点一下、按下组合键就录好了；自动拦掉 `Ctrl+Space`（输入法）、`Alt+F4` 这类被系统抢走的组合 |
| **功能** | 列出所有动作和它们的「说法」，逐个显示「现在能不能跑通」；改说法、改目标、增删、试运行 |
| **设置** | 麦克风、静音阈值、匹配阈值、语义层——都带一句「这个值为什么是这个数」 |
| **关于** | 路径、依赖版本、一键完整体检，出问题直接把结果复制到 issue |

三件在别的工具里经常出事、这里专门做了处理的事：

1. **保存只改动的行**。点保存不会把你的 `config.toml` 重写一遍——注释、空行、
   你自己偏好的写法全部原样保留，并且留一份 `config.toml.bak`。
2. **写不出来就不写**。改完的配置会先真的加载一遍，加载不了就整个放弃写入
   （你的文件一个字节都不动），而不是留下一个下次启动就起不来的配置。
3. **界面里看到的日志就是真实发生的事**。界面和识别跑在同一个进程、同一条事件流上，
   不存在「UI 显示一套、实际跑另一套」。

---

## 快速开始

### 方式一：下载 exe（无需 Python）

到 [Releases](https://github.com/xubochen520/voice-ctl/releases) 下 `voice-ctl-*-win64-lite.exe`，**双击**即可：

```
双击              → 打开界面
设置页 → 下载模型  → 226MB，界面里下，进度打在日志页
运行页 → 启动监听  → 按住 Ctrl+Alt+Space 说话
```

也可以走命令行：

```powershell
.\voice-ctl-0.2.0-win64-lite.exe download   # 首次：下识别模型（226MB）
.\voice-ctl-0.2.0-win64-lite.exe --selftest # 自检：依赖/界面/模型/麦克风逐项检查
.\voice-ctl-0.2.0-win64-lite.exe            # 打开界面
.\voice-ctl-0.2.0-win64-lite.exe run        # 不开界面，常驻监听（适合开机自启）
```

首次运行会在 exe 旁边生成 `config.toml`，改热键、加动作都改它。

| 变体 | 体积 | 首次使用 | 启动 |
|---|---|---|---|
| **lite** | 58.1 MB | 界面里点一次下载 | **1.7s** |
| full | 210.0 MB | 开箱即用 | 3.1s（每次解包模型） |

**推荐 lite**——多下一次模型，但每次启动快一倍。
打包细节见 [docs/BUILDING-EXE.md](docs/BUILDING-EXE.md)。

### 方式二：从源码

```powershell
# 1. 建虚拟环境并装依赖
python -m venv .venv
.venv\Scripts\pip install -e .

# 2. 下载识别模型（约 226MB，来自 HuggingFace）
voice-ctl download

# 3. 体检：配置、模型、依赖、动作、麦克风逐项检查
voice-ctl doctor

# 4. 打开界面
voice-ctl ui

# 5. 或者不开界面，先用文字验证匹配链路
voice-ctl simulate "打开记事本" "把音量调小一点" "截屏"

# 6. 常驻运行：按住 Ctrl+Alt+Space 说话，松开执行
voice-ctl run
```

界面用的是**标准库自带的 tkinter**，不额外装 Qt/Electron，打包后只多几 MB。

### 为什么是 Ctrl+Alt+Space 而不是 Ctrl+Space

中文 Windows 上 `Ctrl+Space` 是**输入法切换键**，会被系统抢走。默认加了 `alt`。
想换成别的，改 `config.toml` 的 `[hotkey].keys`，支持 `<f9>`、`<ctrl>+<shift>+j` 等 pynput 写法。

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `ui` | **打开图形界面**（日志 / 快捷键 / 功能 / 设置 / 关于） |
| `doctor` | 体检：配置/模型/依赖/动作/热键/麦克风/意图层/日程，并逐个动作预检 |
| `devices` | 列出可用麦克风（把序号填进 `[audio].device`） |
| `download` | 下载 SenseVoice 识别模型（约 226MB，必做） |
| `fetch-decision` | 下载 Laya 语义层 ONNX 权重（约 900MB，可选） |
| `llm` | 本地小模型层：`--install` 装内置 llama.cpp、`--download` 下模型、`--status` 看状态、给句子试挑动作 |
| `schedule` | 看 / 导出 / 清理日程提醒；`--export x.ics` 导出日历 |
| `test` | 用自带样例音频验证识别能跑通 |
| `simulate <文本...>` | **不开麦克风**，直接测「意图 → 匹配 → 执行」；`--now` 可固定"现在" |
| `record --seconds N --transcribe` | 录一段 wav 并识别，带麦克风质量诊断 |
| `listen --rounds N` | 单次录音并执行（调试用，不装全局热键） |
| `run` | 不开界面，常驻监听热键（适合开机自启） |

所有命令都支持 `--dry-run`（只显示会做什么）和 `-c/--config` 指定配置。
`-q/--quiet` 只输出警告与错误。

`simulate` 的两个开关在排查时特别有用：`--no-intent` 退回纯别名匹配（对比
意图层到底改了什么），`--no-decision` 关掉语义层。加 `--now "2026-10-05 10:00"`
可以把"现在"钉死，日程解析就完全可复现：

```powershell
voice-ctl simulate --dry-run --now "2026-10-05 10:00" "设置今天下午三点的日程我要玩游戏"
```

无参数双击 exe 等于 `ui`；想让它开机就静静挂在后台，用 `run`。

---

## 加一个新能力

### 方式一：改配置（90% 的情况）

在 `config.toml` 末尾加一段就行，**不用碰代码**：

```toml
[[action]]
id = "open.vscode"
handler = "open_app"
aliases = ["vscode", "代码编辑器", "写代码"]
describe = "打开 VS Code 写代码"
target = "Code.exe"
```

加完跑一次 `voice-ctl doctor`，它会告诉你这个目标能不能解析到。
再跑 `voice-ctl simulate "打开代码编辑器"` 验证匹配。

### 内置 handler

| handler | 用途 | `target` 怎么写 |
|---|---|---|
| `open_app` | 启动程序 | 程序名或 exe 名（`notepad.exe`、`微信`） |
| `open_target` | 启动**运行时才知道**的程序 | 留空；目标由意图层从已安装应用里解析 |
| `close_app` | 关闭正在运行的程序 | 留空；目标同上。`args` 里可以写死进程名 |
| `schedule` | 建日程/提醒 | `event` / `reminder` / `alarm` / `timer` |
| `open_path` | 打开文件/文件夹 | 路径 |
| `open_url` | 打开网址或协议 URI | `https://...`、`ms-settings:` |
| `sysctl` | 系统操作 | `volume_up`/`volume_down`/`mute`/`lock`/`screenshot`/`show_desktop`/`sleep`/`explorer` |
| `keys` | 模拟按键 | `ctrl+alt+w` |
| `shell` | 执行命令 | 命令行（有风险，默认关闭示例） |

`open_target` / `close_app` / `schedule` 三条是 0.3.0 加的。它们**没有固定目标**：
要开谁、关谁、什么时候提醒，由意图层当场解析，通过动作槽位传进来。所以
「打开QQ」「关闭微信」「明天八点提醒我开会」都不用各写一段 `[[action]]`。

想固定绑某个程序时，给 `open_target` 写 `target = "..."` 即可——那时它和
`open_app` 一样。用户亲手配的 `target`/`args` 永远优先于动态解析。

### 方式二：写新 handler（需要全新行为时）

在 `voice_ctl/actions/__init__.py` 里继承 `Action` 并注册：

```python
class MyAction(Action):
    handler_name = "my_handler"

    def preflight(self) -> ActionResult:
        """doctor 调用它做静态检查——能提前发现配置错误。"""
        return ActionResult(True, "看起来没问题")

    def execute(self, ctx: ActionContext) -> ActionResult:
        if ctx.dry_run:
            return ActionResult(True, "[dry-run] 将做某事")
        ...
        return ActionResult(True, "做完了")

HANDLERS["my_handler"] = MyAction
```

约束：`execute` **不要抛异常**，一律返回 `ActionResult`——一个动作失败不该让助手整体崩掉。

---

## 它是怎么工作的

```
按住热键 ──► 录音 ──► SenseVoice 识别 ──► 归一化 ──► 意图层 ──► 别名匹配 ──► 执行
   │          16kHz      int8 ONNX       同音纠正    动词/否定   别名/模糊   动作 handler
   └─ 松开停止             ~90ms        口语词剥离   时间/对象    ~1ms
        │                                             ~1ms
        └─► 事件总线 ──► 控制台（带颜色） / 界面（实时日志） / voice-ctl.log
```

### 为什么识别不在热键线程里跑

pynput 的回调就是 Windows 的**低级键盘钩子过程**。整条「识别 + 执行」实测
100–250ms，一旦超过系统的 `LowLevelHooksTimeout`（默认 300ms），Windows 会
**悄悄把钩子摘掉**——表现是「按几次之后热键忽然没反应了」，重启才好，而且
日志上什么都看不到。

所以钩子线程只做状态转移和停止录音，识别与执行丢给一条**单工作线程**。
单线程而不是线程池，是为了保证「日志顺序 = 实际发生顺序」。

开流（建音频输入流）也在同一条规矩下：实测冷开流 **319ms**，压在钩子线程上就是
贴着 300ms 红线走，所以它也在后台线程里做。

### 按下就开口，开头不会丢

录音链路分三段，各解决一段风险：

```
按下修饰键 ──► 预热：后台开流              ← 0.3.2：这时麦克风已经在采了
     │          ~150ms（冷开流 319ms）
     ▼
按下主键   ──► 进入录音态（钩子线程，约 0ms）
     │          预滚缓冲里的音频当作开头交出去
     ▼
松开主键   ──► 停采 → 识别 → 执行
```

- **预热**：多键热键（`Ctrl+Alt+Space`）总是先按修饰键、最后才按主键，手指从 Alt
  挪到空格要 100–300ms。把开流挪进这段空隙，主键按下时麦克风已经开着。
  修饰键按住不放超过 3 秒会自动关流——那是提前量，不是常开。
- **预滚缓冲**：400ms 环形缓冲，`start()` 时把它当成开头。所以哪怕没预热上
  （比如单键热键 `<f9>`），开流那 319ms 里进来的音频也一个字不少。
- **保留而不是清空**：预滚缓冲在 `start()` 时**不清空**。清空等于"按下之前的
  音频一律不要"，而开流慢的时候那正是用户的第一个字。

实测同一句按住 2 秒的话：改动前只有 1664ms 音频（丢掉 317ms），改动后 2106ms。

### 运行期只发事件，不 print

界面要显示日志，但日志不能靠界面去解析 stdout：stdout 一旦被重定向就什么都
收不到，常驻进程的 print 还是块缓冲的。所以运行期只做一件事——emit 结构化
事件，谁来消费由订阅方决定（控制台 sink / 界面轮询 / 文件 sink）。

emit 本身必须**微秒级返回**（它跑在钩子线程里），所以真正的写盘和控制台输出
在唯一的后台线程上做；被 tee 捞进来的裸输出（traceback、C++ 库的打印）也走同一条路。

### 别名匹配（0.2.0 就有，负责绝大部分指令）

开微信这件事用不上模型——精确/模糊匹配 1ms 出结果、行为确定、可解释。
打分规则写死在 `matcher.py` 里，全部可单测：

| 策略 | 得分 | 含义 |
|---|---|---|
| `exact` | 1.00 | 说话内容与别名完全相同 |
| `alias-hit` | 0.90 + 0.10×覆盖比 | 说话里包含完整别名 |
| `scored` | 相似度 | 字符级 / 拼音首字母相似 |
| `+distinct:X` | 1.00 | 区分字消歧翻盘（见下） |

**区分字消歧**解决了一类真实缺陷：`音量加` 和 `音量减` 只差最后一个字，
模糊相似度会把「把音量调小一点」判成**调大**，而且错的那个分数本身过了阈值，
调 `threshold` 救不了。所以当两个候选分差很小时，会再看哪个候选拥有
「输入里出现、对手没有」的字（这里是「小」），有证据的一方翻盘置满分，
并把这个决定记进 `strategy` 里，事后能解释。

0.3.0 给这一层加了一条**安全网**：别名出现在时间表达式旁边时（「设置今天下午
三点的日程」里的「设置」），`alias-hit` 会打七折。它确实包含那个别名，但那个
别名是句子结构的一部分，不是用户想指的东西。打折而不是一票否决——真想说
「设置」时它照样能用（`exact` 仍然排第一）。

### 语义决策（可选，默认关闭）

前三层只能命中「说得清楚」的指令。用户说「有个文件要改」（想开记事本）、
「把声音关小」（想调音量）这类**没有别名的口语化表达**，才需要这一层。

开启三步：

```powershell
.venv\Scripts\pip install "laya[onnx]"   # 1. 装依赖（会拉进 torch，实测 CPU 版 502MB，仅磁盘占用）
voice-ctl fetch-decision                 # 2. 下载 ONNX 权重（约 900MB）
# 3. config.toml 里 [decision].enabled = true，然后：
voice-ctl doctor                         #    确认语义层显示 ✓
voice-ctl simulate "有个文件要改一下"      #    验证
```

**实测效果**（6 选项零样本，本机 CPU，`scripts/probe_decision.py`）：

| 输入 | 判定 | 置信度 | |
|---|---|---|---|
| 有个文件要改一下 | open.notepad | 0.914 | ✓ |
| 算个数 | open.calc | 0.957 | ✓ |
| 我想聊个天 | open.wechat | 0.828 | ✓ |
| 帮我截个图 | sys.screenshot | 0.999 | ✓ |
| 屏幕别让人看了 | sys.lock | 0.701 | ✓ |
| 随便说点什么 | open.wechat | 0.589 | ← 低于 0.6 阈值，正确拒绝 |

加载 10.4s（只加载一次），单次推理 42–58ms，端到端多花约 100ms。

**代价与限制**：

- 内存 +873MB（int8 ONNX 图），磁盘 +约 900MB，外加 torch CPU 版 502MB
- **中文务必用 `model = "multilingual"`**。官方 benchmark 里 english checkpoint
  在非英文文本上接近随机，而且**它的置信度不会报警**（高棉语 0.000 准确率
  配 95.2% 置信度就是这么来的）。
- 它只该**兜住前三层漏掉的输入**，不该当主判据——官方 20 选项意图任务上是 0.451。
- 低于 `min_confidence` 的动作**不执行**，只提示，避免误触发。

**既然有了小模型层（④），还需要这一层吗？** 两者的取向不同：Laya 是个**分类器**，
本地、离线、40-100ms、但只能从封闭集合里挑，且要 873MB 内存 + 900MB 磁盘；
小模型层能处理"缺字"和开放式表达，但依赖一个本机服务、每次几百毫秒。
只开一个也行——④ 覆盖的场景更多，③ 更省心（不依赖任何外部进程）。

#### 关于 Laya 的三个坑（照文档写会崩）

1. **`ONNXAgent` 没有 `from_pretrained`。** 真实签名是
   `ONNXAgent(model_id_or_path, onnx_path=...)`——第一个参数是**目录**
   （放 `rl_agent_config.json` 和 `tokenizer/`），第二个是**导出的 .onnx 图**。
   照其他库的直觉写 `from_pretrained` 会直接 `AttributeError`。
2. **官方仓库不发布 ONNX 权重。** `convaiinnovations/laya` 只有 torch 权重，
   ONNX 图得自己跑 `scripts/export_onnx.py` 导。`fetch-decision` 用的是社区导出。
3. **别选错 checkpoint。** 社区有个只 405MB 的包看着很香，但它的 encoder 是
   `answerdotai/ModernBERT-large`（**english**，max_len 512）——中文用它接近随机。
   中文版是 `jhu-clsp/mmBERT-base`（max_len 1024），int8 就有 873MB。
   `fetch-decision` 默认取后者。

---

## 实测数据

这台机器（Windows 11 / AMD64 / CPU 推理，无 GPU 加速）：

### 识别性能

| 项 | 实测 |
|---|---|
| 模型 | SenseVoice int8，226MB |
| 加载耗时 | 约 1.0s（只加载一次，常驻复用） |
| 推理 RTF | **0.037–0.045**（比实时快约 25 倍） |
| 短命令耗时 | 中位 **88ms**（最快 70ms，最慢 155ms） |
| 匹配耗时 | **约 1ms**（纯 Python，无需模型） |
| 语义层（可选） | 加载 7.6–10.4s，单次推理 **42–103ms** |

**关于「不加载 torch」是真的**：`voice_ctl` 核心模块导入耗时 **125ms**，且
`'torch' in sys.modules` 为 `False`——Laya 的 torch 相关名字是惰性导入的，
走 ONNX 路径时 torch 只是躺在磁盘上。

### 端到端准确率

用 Windows 内置 TTS（Microsoft Huihui, zh-CN）合成 18 条中文命令，
走「识别 → 归一化 → 匹配」全链路：

| 指标 | 结果 |
|---|---|
| 动作匹配正确 | **18/18 = 100%** |
| 识别文本完全正确 | 17/18 = 94.4% |
| 系统应用类 | 9/9 |
| 口语化（"帮我打开一下记事本"） | 4/4 |
| 同音字（"打开威信"、"截个图"） | 2/2 |
| 负样本（不该命中） | 2/2 |

复现：`python scripts/make_tts_samples.py` 然后 `python scripts/bench_e2e.py`。

⚠️ 合成语音与真人语音有差距，**绝对值仅供参考**；真正的价值是改配置后重跑做横向对比。

### 内存占用（全常驻）

**只开第 0 层（默认）**：

| 项 | 大小 |
|---|---|
| SenseVoice int8 | 254 MB |
| sherpa-onnx + onnxruntime | ~60 MB |
| Python + 应用 | ~40 MB |
| **合计** | **~350 MB** |

**加上第 1 层语义决策**：再加 Laya multilingual int8 的 **873 MB**（另有约 900MB
磁盘 + torch CPU 版 502MB 磁盘，但 torch 不加载就不占内存），合计约 **1.2 GB**。

追求低开销就别开语义层——第 0 层已经能覆盖绝大多数指令。

---

## 关于 SenseVoice 的三个坑（都是实测撞出来的）

1. **不支持热词偏置。** SenseVoice 是 CTC 模型，sherpa-onnx 在 C++ 层会打印
   `Only transducer models support contextual biasing.` 然后**直接 abort 进程**
   （不是 Python 异常，`try/except` 抓不住）。所以命令词纠错只能自己做，
   见 `normalize.py` 的同音替换表。

2. **`language` 参数对本模型输出无影响。** 实测 `zh`/`auto`/`en` 三种设置结果完全一致。
   结果文本自带语言标记（如 `<|yue|>`），但那个标记也不总是准的。
   默认 `auto` 即可，不用纠结。

3. **不要用 RMS 做静音判据。** 实测安静环境下真实说话的 RMS 可以低到 0.002，
   和纯底噪同一量级——用 RMS 当阈值会把用户小声说话静默丢掉，表现为
   「有时候喊了没反应」，极难排查。正确判据是**峰值**：安静的真语音峰值通常
   在 0.05 以上，纯底噪接近 0。代码用 `[audio].min_peak`，`record` 命令会
   打印实测的峰值/RMS 比值（>4 才像语音）。

---

## 排查问题

**热键没反应**
1. 先确认进程还活着、且打印了「✓ 就绪」
2. 换一个热键（可能被别的程序占用，或与输入法冲突）
3. 别用纯 `Ctrl+Space`

**按键说话后没反应**
- 看控制台有没有「太短」「静音」的提示。有的话按提示调 `[audio].min_peak`
- 跑 `voice-ctl record --seconds 3 --transcribe`，它会打印麦克风质量诊断

**识别出来的字不对**
- 把常见的错字加进 `config.toml` 的 `[normalize].substitutions`（或直接改
  `normalize.py` 的 `DEFAULT_SUBSTITUTIONS`）
- 也可以在动作的 `aliases` 里直接把错识别写法加上，比如 `"威信"`

**「打开微信」说找不到应用**
- 先跑 `voice-ctl doctor` 看这一行是 `✓` 还是 `·`
- 程序会依次查：PATH → **开始菜单应用列表** → App Paths 注册表 → 常见安装路径 → `start` 兜底
- 开始菜单那一级同时覆盖 UWP/商店应用（用 AUMID 启动）和传统程序（返回 exe 路径），
  所以「计算器」「终端」这类没有独立 exe 的也能开
- 装在很偏的路径时，直接在动作里写死：`target = "E:\\weixin\\Weixin.exe"`
- 想看清每一级的结果：`python scripts/diag_appfind.py`

**改动 `start_apps` 相关行为时注意**
开始菜单列表会缓存（一次 PowerShell 调用约 200-400ms），程序运行期间新装的软件
要重启 voice-ctl 才能被识别。

**想看它到底匹配到了什么**
```powershell
voice-ctl simulate "你说的话" --dry-run
```
输出会写明：听成了什么、归一化后是什么、命中了哪条别名、用了什么策略
（`exact` / `alias-hit` / `scored` / `distinct:字` / `semantic`）、以及每步耗时。

**语义层开了但不生效**
- 看有没有 `⚠ 语义层启用失败，已回落到仅别名匹配：...` 这行（在 stderr）
- 跑 `voice-ctl doctor`，它会说清是缺 `rl_agent_config.json`、缺 tokenizer 还是缺 `.onnx`
- `[decision].onnx_dir` 是相对路径时按**配置文件所在目录**解析，不是当前目录

---

## 开发

```powershell
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest                      # 962 个单测，约 42 秒（含真实建窗的界面冒烟测试）
.venv\Scripts\python scripts\bench_e2e.py           # 端到端基准（需先 make_tts_samples.py）
.venv\Scripts\python scripts\probe_decision.py      # 语义层实测（需先 fetch-decision）
.venv\Scripts\python scripts\diag_appfind.py        # 应用定位排障：逐级打印找没找到
.venv\Scripts\python scripts\test_mic_loopback.py   # 麦克风回环冒烟测试（外放+录音）
.venv\Scripts\python scripts\probe_asr.py           # ASR 行为探针（热词/语种/性能）
```

`probe_asr.py` 记录了几个反直觉的实测结论（SenseVoice 不支持热词、`language`
参数无效），改动 ASR 相关代码时可以用它复核。

测试**不需要麦克风、真权重、真进程、真网络**：`pipeline` 的音频路径用替身 ASR 测，
匹配层与意图层纯文本测，并发状态机用假 Recorder 测，语义层用假 Decider 测，
小模型层用假 SlotExtractor 测，日程用 `VOICE_CTL_SCHEDULE_FILE` 指到 tmp 目录。

关闭进程那部分**只测纯函数**（`close_plan_for` / `normalize_exe`）——真跑一次
`taskkill` 会关掉正在跑测试的人的程序。

## 目录结构

```
voice_ctl/
├── cli.py         命令行入口（ui/doctor/download/fetch-decision/llm/schedule/test/simulate/listen/record/run）
├── runner.py      运行引擎（热键+录音+识别+执行），CLI 与界面共用
├── events.py      事件总线：控制台/文件/界面三个 sink，emit 必须微秒级返回
├── ui/            图形界面（tkinter）
│   ├── window.py     主窗口：顶栏 + 侧栏 + 内容区 + 底栏，轮询事件总线
│   ├── theme.py      配色/字体/DPI，以及全部 ttk 样式
│   ├── widgets.py    通用控件（导航项、卡片、自绘复选框、键位胶囊、日志面板）
│   └── tab_*.py      六个页面：运行 / 日志 / 快捷键 / 功能 / 设置 / 关于
├── config.py      TOML 加载 + 严格类型校验
├── confedit.py    配置回写：只写差异，写前必须能读回来
├── toml_edit.py   保留注释的定点 TOML 改写
├── fetch.py       模型下载（CLI 与界面共用）
├── asr.py         SenseVoice 封装（sherpa-onnx）
├── recorder.py    push-to-talk 录音 + 峰值静音门
├── hotkey.py      全局热键状态机 + 超时守护
├── session.py     录音会话状态机（并发路径，可单测）
├── normalize.py   同音替换 + 口语词剥离 + 拼音键
├── lexicon.py     口令词表：动词 / 否定 / 客套话 / 确认否认 / 自我更正
├── intent.py      意图层：动词+否定+时间+对象 → 一个 Intent
├── web.py         站点表：「打开百度」→ baidu.com（约 70 个常见站，不联网）
├── timeparse.py   中文时间解析：「明天下午三点半」→ datetime
├── matcher.py     别名匹配 + 区分字消歧 + 时间吞掉别名的安全网
├── appfind.py     应用定位（PATH/注册表/常见路径/开始菜单）
├── apps.py        动态应用词典：已安装应用的名字/拼音/昵称索引
├── schedule.py    日程存储 + .ics 导出 + 到点提醒线程
├── reminder.py    把提醒线程和"用户能感知的通知"接起来
├── decision.py    语义决策（Laya ONNX，可选）
├── llm.py         小模型层：两种后端（内置 llama.cpp / 外部服务），GBNF 锁输出
├── llamacpp.py    内置 llama.cpp：下运行时、管 llama-server 进程、下 GGUF 模型
├── singleton.py   单实例保护（避免两个进程抢热键）
├── autostart.py   开机自启（注册表 Run 项）
├── bootstrap.py   冻结/源码两种运行方式下的路径解析
├── app.py         pipeline：把上面这些按顺序串起来
└── actions/       动作注册表 + 9 个 handler
    ├── __init__.py         open_app / open_target / close_app / open_path / open_url /
    │                       sysctl / keys / shell
    └── schedule_action.py  schedule（日程）
```

## 依赖

运行时只用到四个包，全部有预编译 wheel，不需要编译器：

```
sherpa-onnx   语音识别（含 onnxruntime）
sounddevice   录音（PortAudio）
pynput        全局热键
numpy         音频处理
psutil        列进程（关闭应用要用它精确定位是哪一支）
```

可选：`pypinyin`（中文同音消歧，建议装）、`laya[onnx]`（Laya 语义层）。

`psutil` 不是硬依赖：拿不到就退回 PowerShell + WMI，只是慢一些
（实测 16ms vs 677ms，423 个进程）。

**内置小模型层不引入任何 Python 依赖**——它用的是标准库 `urllib` 加一份
llama.cpp 的二进制（39.8MB，`voice-ctl llm --install` 下载）。

## 许可

本项目代码可自由使用。识别模型来自
[FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)，
经 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 导出为 int8 ONNX，
使用前请自行确认模型许可。
