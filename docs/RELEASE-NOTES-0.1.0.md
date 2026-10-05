按住快捷键说话 → **本地离线**识别 → 匹配动作 → 执行。全程不联网、零调用成本。

说「打开微信」就打开微信，说「把音量调小一点」就调低音量。

## 下载哪个

| 文件 | 体积 | 首次使用 | 启动 |
|---|---|---|---|
| **`voice-ctl-0.1.0-win64-lite.exe`** | 54.9 MB | 需跑一次 `download`（226MB） | **1.6s** |
| `voice-ctl-0.1.0-win64-full.exe` | 206.9 MB | 开箱即用 | 3.0s |

**推荐 lite**——多下一次模型，但每次启动快一倍。`full` 内嵌了模型，代价是每次运行都要把 226MB 解包到临时目录。

两者功能完全一致，只差模型是否内嵌。

## 快速开始

```powershell
# 精简版：先下模型
.\voice-ctl-0.1.0-win64-lite.exe download

# 自检：确认依赖、原生 DLL、模型、麦克风都正常（不碰麦克风，安全）
.\voice-ctl-0.1.0-win64-lite.exe --selftest

# 体检，然后常驻运行
.\voice-ctl-0.1.0-win64-lite.exe doctor
.\voice-ctl-0.1.0-win64-lite.exe
```

跑起来后**按住 `Ctrl+Alt+Space` 说话，松开执行**。

不需要装 Python，单文件 exe。

## 环境要求

- Windows 10/11 x64
- 一个能用的麦克风
- 首次使用需要联网（下载模型或首次校验），之后完全离线

## 首次使用必读

**热键是 `Ctrl+Alt+Space` 而不是 `Ctrl+Space`** —— 中文 Windows 上后者是输入法切换键，会被系统抢走。

想换成别的，改 exe 旁边的 `config.toml`：

```toml
[hotkey]
keys = "<ctrl>+<shift>+j"   # 或 <f9> 等
```

**没有反应时先跑 `--selftest`**，它会把问题直接指出来（缺模型、缺 DLL、麦克风异常）。

**「打开微信」提示找不到应用**：程序按 PATH → 开始菜单（含 UWP 应用）→ 注册表 → 常见路径依次查找。装在很偏的位置时，在 `config.toml` 里写死路径：

```toml
[[action]]
id = "open.wechat"
handler = "open_app"
aliases = ["微信"]
target = "E:\\weixin\\Weixin.exe"
```

## 加一个新能力

改 exe 旁边的 `config.toml` 即可，不用改代码：

```toml
[[action]]
id = "open.vscode"
handler = "open_app"
aliases = ["vscode", "代码编辑器", "写代码"]
describe = "打开 VS Code 写代码"
target = "Code.exe"
```

内置 handler：`open_app` / `open_path` / `open_url` / `sysctl` / `keys` / `shell`。

## 实测数据

本机（Windows 11 / AMD64 / 纯 CPU，无 GPU）：

| 项 | 结果 |
|---|---|
| 识别速度 | RTF **0.037–0.045**（比实时快约 25 倍） |
| 短命令识别耗时 | 中位 **88ms** |
| 匹配耗时 | **约 1ms** |
| 端到端匹配准确率 | **18/18**（Windows 内置中文 TTS 合成的 18 条命令） |
| 常驻内存 | **约 350MB** |
| 单元测试 | **269 个** |

## 已知限制

- **准确率数字来自 TTS 合成语音**，和真人说话有差距。真人识别率请自己实测：`voice-ctl.exe record --seconds 3 --transcribe`
- **只支持 Windows x64**
- **需要联网下载模型**（`full` 版除外，但首次启动仍会联网校验一次）
- **语义决策层未包含**。想说「有个文件要改」这种没有明确别名的口语化指令，需要额外装 `laya[onnx]` + 900MB ONNX 权重，见仓库 README
- **未经代码签名**。首次运行 Windows Defender SmartScreen 会拦，点「更多信息 → 仍要运行」

## 校验

```
voice-ctl-0.1.0-win64-lite.exe
  大小   : 57,616,915 字节
  SHA256 : d39a2aa5b72e4a1490f44a69186b90189c04b7eb3d1f61e7113d4b19014f2aaf

voice-ctl-0.1.0-win64-full.exe
  大小   : 216,977,880 字节
  SHA256 : b415e30f3c1b7f2133ee1c93713c228a31daa1cd509bc3659bdc8705c110af9b
```

## 第三方组件

- 识别模型 [SenseVoice](https://github.com/FunAudioLLM/SenseVoice)（FunAudioLLM），经 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 导出为 int8 ONNX
- 运行库：sherpa-onnx / onnxruntime / numpy / sounddevice(PortAudio) / pynput / pypinyin

各组件遵循其原始许可，使用前请自行确认。
