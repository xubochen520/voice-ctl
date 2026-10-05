# 打包成 Windows exe

用 PyInstaller 出单文件 exe。两个变体：

| 变体 | 体积 | 首次使用 | 启动耗时 | 适合谁 |
|---|---|---|---|---|
| **lite** | 84.1 MB | 需跑一次 `download`（226MB） | **2.4s** | 日常使用（模型放 exe 旁边，启动不解包） |
| **full** | 236.1 MB | 开箱即用 | 3.8s | 想零配置、或不方便单独下模型 |

两者功能完全一致，只差识别模型是否内嵌。**常用建议选 lite**：虽然要多下一次，
但每次启动快一倍——`full` 每次运行都要把 226MB 解包到临时目录。

（0.3.1 起两个变体都内嵌了 39.8MB 的 llama.cpp 运行时，见下。）

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
```

产物：`dist\voice-ctl.exe` / `dist-full\voice-ctl.exe`。

## 四个打包开关

| 环境变量 | 内容 | 建议 |
|---|---|---|
| `VOICE_CTL_BUNDLE_LLAMA` | 39.8MB llama.cpp 运行时 | **打**——它是"内置小模型层"的前提，而从 GitHub 下载要用户能访问 github.com |
| `VOICE_CTL_BUNDLE_LLM_MODEL` | 469MB~1GB 的 GGUF 模型 | **别打**——单文件 exe 每次启动都要解包它 |
| `VOICE_CTL_BUNDLE_MODEL` | 226MB 识别模型 | 看情况（`full` 变体就是它） |
| `VOICE_CTL_BUNDLE_DECISION` | torch + transformers + laya，约 600MB | **默认别打**——只有要开 Laya 语义层才需要 |
| （依赖）`psutil` | 约 0.1MB | **打**——关闭应用靠它列进程，16ms vs WMI 的 677ms |

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

**为什么 GGUF 不该打进 exe**：PyInstaller 的单文件 exe 每次启动都把内嵌数据
解包到临时目录。0.2.0 实测过这条路的代价——226MB 的识别模型让启动从 1.7s
变成 3.1s。469MB 只会更糟，而且是**每次启动**都白写 469MB 到磁盘。
让用户下一次，比每次启动都解包划算。同一条理由也适用于语义层那 600MB。

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
