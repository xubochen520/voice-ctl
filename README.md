# voice-ctl

按住快捷键说话 → **本地离线**识别 → 匹配动作 → 执行。

说「打开微信」就打开微信，说「把音量调小一点」就调低音量。全程不联网、零调用成本。

- **纯离线**：识别模型跑在本地，音频不出机器
- **低开销**：无 GPU、无 torch，模型常驻约 350MB，识别 RTF≈0.04
- **可拓展**：加一个新能力 = 在 `config.toml` 里加一段 `[[action]]`，不用改代码
- **可解释**：`--dry-run` 能看到它听成了什么、匹配了哪条、为什么

---

## 快速开始

### 方式一：下载 exe（无需 Python）

到 [Releases](https://github.com/xubochen520/voice-ctl/releases) 下 `voice-ctl-*-win64-lite.exe`：

```powershell
.\voice-ctl-0.1.0-win64-lite.exe download   # 首次：下识别模型（226MB）
.\voice-ctl-0.1.0-win64-lite.exe --selftest # 自检：确认依赖/模型/麦克风都正常
.\voice-ctl-0.1.0-win64-lite.exe            # 直接跑，按住 Ctrl+Alt+Space 说话
```

首次运行会在 exe 旁边生成 `config.toml`，改热键、加动作都改它。

| 变体 | 体积 | 首次使用 | 启动 |
|---|---|---|---|
| **lite** | 54.9 MB | 需跑一次 `download` | **1.6s** |
| full | 206.9 MB | 开箱即用 | 3.0s（每次解包模型） |

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

# 4. 不开麦克风，先用文字验证匹配链路
voice-ctl simulate "打开记事本" "把音量调小一点" "截屏"

# 5. 试录音 + 真识别
voice-ctl record --seconds 3 --transcribe

# 6. 常驻运行：按住 Ctrl+Alt+Space 说话，松开执行
voice-ctl run
```

第 6 步之后就可以一直挂着，托盘式后台运行。

### 为什么是 Ctrl+Alt+Space 而不是 Ctrl+Space

中文 Windows 上 `Ctrl+Space` 是**输入法切换键**，会被系统抢走。默认加了 `alt`。
想换成别的，改 `config.toml` 的 `[hotkey].keys`，支持 `<f9>`、`<ctrl>+<shift>+j` 等 pynput 写法。

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `doctor` | 体检：配置/模型/依赖/动作/热键/麦克风，并逐个动作预检 |
| `devices` | 列出可用麦克风（把序号填进 `[audio].device`） |
| `download` | 下载 SenseVoice 识别模型（约 226MB，必做） |
| `fetch-decision` | 下载 Laya 语义层 ONNX 权重（约 900MB，可选） |
| `test` | 用自带样例音频验证识别能跑通 |
| `simulate <文本...>` | **不开麦克风**，直接测「归一化 → 匹配 → 执行」 |
| `record --seconds N --transcribe` | 录一段 wav 并识别，带麦克风质量诊断 |
| `listen --rounds N` | 单次录音并执行（调试用，不装全局热键） |
| `run` | **最终形态**：常驻，按住热键说话 |

所有命令都支持 `--dry-run`（只显示会做什么）和 `-c/--config` 指定配置。

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
| `open_path` | 打开文件/文件夹 | 路径 |
| `open_url` | 打开网址或协议 URI | `https://...`、`ms-settings:` |
| `sysctl` | 系统操作 | `volume_up`/`volume_down`/`mute`/`lock`/`screenshot`/`show_desktop`/`sleep`/`explorer` |
| `keys` | 模拟按键 | `ctrl+alt+w` |
| `shell` | 执行命令 | 命令行（有风险，默认关闭示例） |

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
按住热键 ──► 录音 ──► SenseVoice 识别 ──► 归一化 ──► 匹配 ──► 执行
   │          16kHz      int8 ONNX       同音纠正    别名/模糊   动作 handler
   └─ 松开停止             ~90ms        口语词剥离    ~1ms
```

### 第 0 层：别名匹配（默认，负责绝大部分指令）

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

### 第 1 层：语义决策（可选，默认关闭）

别名匹配只能命中「说话里出现了别名」的情况。用户说「有个文件要改」（想开记事本）、
「把声音关小」（想调音量）这类**没有别名的口语化表达**，才需要语义层。

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
- 它只该**兜住第 0 层漏掉的输入**，不该当主判据——官方 20 选项意图任务上是 0.451。
- 低于 `min_confidence` 的动作**不执行**，只提示，避免误触发。

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
.venv\Scripts\python -m pytest                      # 269 个单测，约 4 秒
.venv\Scripts\python scripts\bench_e2e.py           # 端到端基准（需先 make_tts_samples.py）
.venv\Scripts\python scripts\probe_decision.py      # 语义层实测（需先 fetch-decision）
.venv\Scripts\python scripts\diag_appfind.py        # 应用定位排障：逐级打印找没找到
.venv\Scripts\python scripts\test_mic_loopback.py   # 麦克风回环冒烟测试（外放+录音）
.venv\Scripts\python scripts\probe_asr.py           # ASR 行为探针（热词/语种/性能）
```

`probe_asr.py` 记录了几个反直觉的实测结论（SenseVoice 不支持热词、`language`
参数无效），改动 ASR 相关代码时可以用它复核。

测试**不需要麦克风也不需要真权重**：`pipeline` 的音频路径用替身 ASR 测，
匹配层纯文本测，并发状态机用假 Recorder 测，语义层用假 Decider 测。

## 目录结构

```
voice_ctl/
├── cli.py         命令行入口（doctor/download/fetch-decision/test/simulate/listen/record/run）
├── config.py      TOML 加载 + 严格类型校验
├── asr.py         SenseVoice 封装（sherpa-onnx）
├── recorder.py    push-to-talk 录音 + 峰值静音门
├── hotkey.py      全局热键状态机 + 超时守护
├── session.py     录音会话状态机（并发路径，可单测）
├── normalize.py   同音替换 + 口语词剥离 + 拼音键
├── matcher.py     别名匹配 + 区分字消歧
├── appfind.py     应用定位（PATH/注册表/常见路径/开始菜单）
├── decision.py    第 1 层语义决策（Laya ONNX，可选）
├── app.py         pipeline：把上面这些串起来
└── actions/       动作注册表 + 6 个 handler
```

## 依赖

运行时只用到四个包，全部有预编译 wheel，不需要编译器：

```
sherpa-onnx   语音识别（含 onnxruntime）
sounddevice   录音（PortAudio）
pynput        全局热键
numpy         音频处理
```

可选：`pypinyin`（中文同音消歧，建议装）、`laya[onnx]`（第 1 层语义决策）。

## 许可

本项目代码可自由使用。识别模型来自
[FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)，
经 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 导出为 int8 ONNX，
使用前请自行确认模型许可。
