# 打包成 Windows exe

用 PyInstaller 出单文件 exe。三个变体：

| 变体 | 体积 | 首次使用 | 启动耗时 | 适合谁 |
|---|---|---|---|---|
| **lite** | 84.1 MB | 需跑一次 `download`（226MB） | **2.4s** | 日常使用（模型放 exe 旁边，启动不解包） |
| **full** | 236.1 MB | 开箱即用 | 3.8s | 想零配置、或不方便单独下模型 |
| **semantic** | 1.8 GB 目录 / 1.4 GB zip | **完全离线**，什么都不用下 | 目录版，与 full 同级 | 要用语义层、且要求全程不联网 |

前两者功能一致，只差识别模型是否内嵌。**常用建议选 lite**：虽然要多下一次，
但每次启动快一倍——`full` 每次运行都要把 226MB 解包到临时目录。

（0.3.1 起这些变体都内嵌了 39.8MB 的 llama.cpp 运行时，见下。）

## 构建

```powershell
.venv\Scripts\pip install pyinstaller

# 内置小模型层要先有 llama.cpp 运行时（39.8MB），不然 spec 会警告并跳过
.venv\Scripts\voice-ctl llm --install

# 精简版
$env:VOICE_CTL_BUNDLE_LLAMA='1'
.venv\Scripts\python -m PyInstaller voice-ctl.spec --noconfirm
Remove-Item Env:\VOICE_CTL_BUNDLE_LLAMA

# 完整版（内嵌识别模型）
$env:VOICE_CTL_BUNDLE_MODEL='1'; $env:VOICE_CTL_BUNDLE_LLAMA='1'
.venv\Scripts\python -m PyInstaller voice-ctl.spec --noconfirm --distpath dist-full --workpath build-full
Remove-Item Env:\VOICE_CTL_BUNDLE_MODEL

# 语义版（0.3.4）：权重也内置，出来是**目录版**
$env:VOICE_CTL_BUNDLE_MODEL='1'; $env:VOICE_CTL_BUNDLE_LLAMA='1'
$env:VOICE_CTL_BUNDLE_DECISION='1'; $env:VOICE_CTL_BUNDLE_DECISION_WEIGHTS='1'
.venv\Scripts\python -m PyInstaller voice-ctl.spec --noconfirm --distpath dist-semantic --workpath build-semantic
```

产物：`dist\voice-ctl.exe` / `dist-full\voice-ctl.exe` / `dist-semantic\voice-ctl\voice-ctl.exe`。

语义版发布前要打成 zip（GitHub Release 的资产是单个文件，而它是目录版）：

```powershell
.venv\Scripts\python.exe scripts\zip_semantic.py
# -> dist-semantic\voice-ctl-semantic.zip（1.4GB；873MB 的 int8 图已量化过，
#    按不压缩存入，压它只是白烧 CPU）
```

## 五个打包开关

| 环境变量 | 内容 | 建议 |
|---|---|---|
| `VOICE_CTL_BUNDLE_LLAMA` | 39.8MB llama.cpp 运行时 | **打**——它是"内置小模型层"的前提，而从 GitHub 下载要用户能访问 github.com |
| `VOICE_CTL_BUNDLE_LLM_MODEL` | 469MB~1GB 的 GGUF 模型 | **别打**——每次启动都要解包它 |
| `VOICE_CTL_BUNDLE_MODEL` | 226MB 识别模型 | 看情况（`full` 变体就是它） |
| `VOICE_CTL_BUNDLE_DECISION` | torch + transformers + laya，约 600MB | 只有要开 Laya 语义层才需要 |
| `VOICE_CTL_BUNDLE_DECISION_WEIGHTS` | 906MB ONNX 权重 | 要"装完即用、不联网"就打；否则让用户 `voice-ctl fetch-decision` |
| `VOICE_CTL_DECISION_WEIGHTS` | 权重的**来源**目录 | 默认 `models/laya-onnx/multilingual`；想用别处的权重时指过去 |
| `VOICE_CTL_ONE_FILE` | 强制单文件 | 语义层/权重变体默认**目录版**，别强行单文件（见下） |
| （依赖）`psutil` | 约 0.1MB | **打**——关闭应用靠它列进程，16ms vs WMI 的 677ms |

### 内嵌权重必须配合路径兜底（0.3.4）

**这是最容易做错的一处，做错了表现是"打了包但完全没效果"。**

配置里的 `[decision].onnx_dir` 是相对路径，`cfg.decision_path()` 按**配置文件
所在目录**解析。打包后那份 config.toml 在 exe 同级，那里没有权重——权重在
`_MEIPASS` 里。所以只加打包开关、不加兜底函数，程序照样报「缺少 *.onnx」，
而且报错完全不指向真因（用户会以为是自己没下权重）。

ASR 模型一直有 `bootstrap.resolve_model_dir()` 这个兜底，语义层在 0.3.4 才补上
`bootstrap.resolve_decision_dir()`，优先级：

```
配置指向的目录存在      → 用它（源码运行、或用户自己下了权重）
exe 同级 models/...     → 用它（用户手动放进来，可换掉内置那份而不必重新打包）
_MEIPASS/models/...     → 用它（打包内嵌的那份）
都没有                  → 原样返回，报"缺少权重"并给下载指引
```

改打包路径时（spec 里的 `models/laya-onnx/multilingual`）**必须同步改**
`resolve_decision_dir()` 里找的那两条，否则又变成白打。

### 为什么语义层要单独一个开关（0.3.2 更正）

以前这里写着"用 ONNX 权重就不需要 torch，所以默认排除"。**这句是错的**：

```
File "laya\onnx_agent.py", line 15, in <module>
    from laya.common import (...)
File "laya\common.py", line 13, in <module>
    import torch
ModuleNotFoundError: No module named 'torch'
```

`laya.onnx_agent` 顶层 import `laya.common`，而 `laya.common` 第一行就
`import torch`（单文件里 40 多处直接用 `torch.nn` / `torch.softmax` /
`torch.device("meta")`）。**torch 是硬依赖，跟权重是不是 ONNX 无关。**
实测 `torch/` 502MB + `transformers/` 100MB。

后果：默认构建出的 exe 一开 `[decision].enabled = true` 就报
`No module named 'torch'`，而且提示是"去 pip install laya[onnx]"——用户装了也
没用，因为缺的是 exe 里那一半。0.3.2 改了提示语，指明是"这个 exe 没带语义层"。

```powershell
# 全量版：连语义层一起打进去（约 800MB+）
$env:VOICE_CTL_BUNDLE_MODEL='1'; $env:VOICE_CTL_BUNDLE_LLAMA='1'
$env:VOICE_CTL_BUNDLE_DECISION='1'
.venv\Scripts\python -m PyInstaller voice-ctl.spec --noconfirm --distpath dist-decision --workpath build-decision
```

torch 的 DLL 全在 `torch/lib/` 下（9 个共 314MB），`collect_dynamic_libs("torch")`
会把它们放到 `torch/lib/`——**必须显式收集**，缺 `c10.dll` 时是启动即崩，
而报错完全不指向真因。

### torch 到底用在哪（0.3.4 量过）

做这个判断时我试过"能不能不打包 torch"，结论值得记下来，免得下次重复劳动。

**ONNX 图里已经到动作头了**，图的输出是：

```
输入: input_ids, attention_mask, marker_pos, marker_mask, qtype
输出: logits (batch, markers)      ← marker 打分
      act_logits (batch, 2)        ← 动作分布
```

而 `laya/common.py` 的 `DecisionModel.forward()` 算的就是这两件事。
所以**纯推理不需要 torch 做任何计算**，它是 import 链上的负债。

**但 laya 包脱离 torch 加载不了**，卡点三层（用桩实验逐层试出来的）：

1. `common.py:472` 模块级 `class DecisionModel(nn.Module)`、
   `_DynamicMultiheadAttention(nn.MultiheadAttention)` —— 光**继承**就要真 torch
   （桩得支持 `__mro_entries__` 才能过）
2. `onnx_agent.py` 没有 `from __future__ import annotations`，所以类型注解是
   **运行时求值**的，会碰 `torch.FloatTensor` 参与 `|` 运算
   （桩得支持 `__or__` / `__ror__` / `__getitem__` 才能过）
3. `transformers` 的懒加载用 `is_torch_available()` 判断后端
   （`AutoTokenizer` → `GenerationMixin`）——它要**真 torch**，不是"能导入的假模块"

前两层桩能过，第三层过不去。真想做"零 torch 语义层"，正确路子是
**绕开 laya 自己跑 ONNX**（图输入输出已量清，缺的只有 `common.py` 里的
tokenize / marker 拼装 / 解码，全是纯 Python 算术），而不是想办法骗过
`is_torch_available()`。这条留给以后需要缩体积时做。

**为什么 GGUF 不该打进 exe**：PyInstaller 的单文件 exe 每次启动都把内嵌数据
解包到临时目录。0.2.0 实测过这条路的代价——226MB 的识别模型让启动从 1.7s
变成 3.1s。469MB 只会更糟，而且是**每次启动**都白写 469MB 到磁盘。
让用户下一次，比每次启动都解包划算。

同一条理由决定了**语义层那 600MB + 873MB 权重必须走目录版**：spec 里
`BUNDLE_DECISION` 或 `BUNDLE_DECISION_WEIGHTS` 任一打开、且没显式设
`VOICE_CTL_ONE_FILE` 时，自动切成 onedir。单文件版实测过——`--version`
从 4.3s 变成 20.8s，而 873MB 权重只会更糟。

**为什么运行时可以打**：内嵌的是 22 个 DLL，只有真的开出小模型层才会被加载。
不开的话它们只是磁盘上的几行目录项。

## 打包后必须验证

```powershell
# 关键：**不要在源码目录里测**。那里有 config.toml 和 models/，
# exe 会误读它们，掩盖真实问题（比如模型没打进去也看不出来）。
$t="$env:TEMP\vctest"; mkdir $t -Force
copy dist\voice-ctl.exe $t
cd $t
.\voice-ctl.exe --selftest
```

`--selftest` 不碰麦克风和热键，只验证文件与质量：依赖导入、原生 DLL、
模型、识别器实际加载、音频设备枚举、动作注册。**打包漏了东西一定在这里暴露**，
而且输出可以直接贴进 issue。

再跑一次真实按键验证热键与录音（PyInstaller 最容易在这里出问题）：

```powershell
Start-Process .\voice-ctl.exe -ArgumentList run -NoNewWindow
# 然后在另一个窗口模拟按键，或直接手工按住 Ctrl+Alt+Space 说话
```

再验一遍内嵌的 llama.cpp（`--selftest` 不覆盖这一块）：

```powershell
.\voice-ctl.exe llm --status     # 运行时那行要显示「（exe 内嵌）」
```

想连推理一起验，往 `llm\models\` 里放一个 gguf 再跑
`.\voice-ctl.exe llm "算个数"`。**注意**内嵌的那份运行时在 `_MEIPASS`
（每次启动路径都不同），所以 `--status` 里的路径长得像
`...\Temp\_MEI0000c8482\llama-runtime`，这是对的。

## 改界面之后：截图看，别只看单测

单测能保证"控件搭得起来、逻辑没断"，但**保证不了好不好看**——版式错位、
颜色发闷、圆角发毛、文字掉行，这些只有看图才知道。改过 `voice_ctl/ui/` 就截一轮：

```powershell
.venv\Scripts\python.exe scripts\shot_ui.py <前缀> [页面,页面...]
# -> _ui_<前缀>_{run,logs,hotkey,actions,settings,about}.png（已在 .gitignore 里）
```

纯 ctypes 抓窗口（`PrintWindow` + `PW_RENDERFULLCONTENT`）+ 自带 PNG 编码，
**不依赖 Pillow**，所以不用为了截图动环境。窗口会临时置顶，抓完自己关。

两个用它的理由：

* **改前改后各截一轮**，对着看。没有"改前"，"改后"就没有判据。
* 顺带验 DPI：`_shotcfg/` 里那份配置是脚本自己复制的，不会碰你的 `config.toml`。

## 四个必须显式处理的依赖

PyInstaller 的 import 分析抓不全这些，漏了就是运行时 ImportError 或设备打不开：

1. **`sherpa_onnx/lib/`** —— 里面有 `onnxruntime.dll`、`sherpa-onnx-c-api.dll`
   和 `_sherpa_onnx.cp312-win_amd64.pyd`。用 `collect_dynamic_libs("sherpa_onnx")`。
2. **`_sounddevice_data/`** —— PortAudio 的 DLL 放在这个**独立顶层包**里，
   不在 `sounddevice` 包内，import 分析发现不了。用 `collect_data_files`。
3. **模型与配置** —— 纯数据文件，手动加进 `datas`。
4. **llama.cpp 的 22 个文件** —— 同理，而且**一个都不能少**。实测少一个
   `mtmd.dll` 就是 `0xC0000135`（DLL 找不到），而那个报错完全不指向真因；
   15 个 `ggml-cpu-*.dll` 也一个都不能删，ggml 按 CPU 指令集动态挑一个加载。

## 编码：打包后第一个会踩的坑

中文 Windows 控制台默认代码页是 GBK，输出 `✓`/`✗` 会抛 `UnicodeEncodeError`。
源码运行时 `cli.main()` 里有重配置所以没事，但 `--selftest` 走的是
`exe_entry.py`，**在重配置之前就打印了**——结果自检第一行就崩，报错还指向打印语句。

修法是把编码设置提到 `bootstrap.setup_console()`，在**任何输出之前**调用，
并给 `safe_print()` 兜底。见 `voice_ctl/bootstrap.py`。

## 冻结环境的路径语义

打包后「当前目录」不可信（用户可能在任意位置双击），PyInstaller 的
`sys._MEIPASS` 又是**只读、用完即删**的。所以路径分三类：

| 类别 | 冻结时位置 | 源码时位置 |
|---|---|---|
| 只读资源（内置 config 模板、内嵌模型） | `_MEIPASS` | 项目根 |
| **可写数据（下载的模型、用户配置）** | **exe 同级目录** | 项目根 |
| 用户显式指定（`--config`/`--model-dir`） | 绝对化后直接用 | 同左 |

关键点：**下载的模型绝不能放 `_MEIPASS`**——那样每次启动都要重新解包 226MB，
而且退出就没了。`bootstrap.data_dir()` 还会在 exe 目录不可写时（比如装在
Program Files）自动退回 `~/.voice-ctl`。

首次运行时，内置 `config.toml` 会**复制一份到 exe 旁边**，之后用户改的就是
这一份，升级 exe 不会覆盖它。
