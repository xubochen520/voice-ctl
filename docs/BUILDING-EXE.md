# 打包成 Windows exe

用 PyInstaller 出单文件 exe。两个变体：

| 变体 | 体积 | 首次使用 | 启动耗时 | 适合谁 |
|---|---|---|---|---|
| **lite** | 54.9 MB | 需跑一次 `download`（226MB） | **1.6s** | 日常使用（模型放 exe 旁边，启动不解包） |
| **full** | 206.9 MB | 开箱即用 | 3.0s | 想零配置、或不方便单独下模型 |

两者功能完全一致，只差模型是否内嵌。**常用建议选 lite**：虽然要多下一次，
但每次启动快一倍——`full` 每次运行都要把 226MB 解包到临时目录。

## 构建

```powershell
.venv\Scripts\pip install pyinstaller

# 精简版
.venv\Scripts\python -m PyInstaller voice-ctl.spec --noconfirm

# 完整版（内嵌模型）
$env:VOICE_CTL_BUNDLE_MODEL='1'
.venv\Scripts\python -m PyInstaller voice-ctl.spec --noconfirm --distpath dist-full
Remove-Item Env:\VOICE_CTL_BUNDLE_MODEL
```

产物：`dist\voice-ctl.exe` / `dist-full\voice-ctl.exe`。

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

## 三个必须显式处理的依赖

PyInstaller 的 import 分析抓不全这些，漏了就是运行时 ImportError 或设备打不开：

1. **`sherpa_onnx/lib/`** —— 里面有 `onnxruntime.dll`、`sherpa-onnx-c-api.dll`
   和 `_sherpa_onnx.cp312-win_amd64.pyd`。用 `collect_dynamic_libs("sherpa_onnx")`。
2. **`_sounddevice_data/`** —— PortAudio 的 DLL 放在这个**独立顶层包**里，
   不在 `sounddevice` 包内，import 分析发现不了。用 `collect_data_files`。
3. **模型与配置** —— 纯数据文件，手动加进 `datas`。

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
