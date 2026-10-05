# voice-ctl 0.3.1：内置 llama.cpp

按住快捷键说话 → **本地离线**识别 → 理解整句话 → 执行。全程不联网、零调用成本。

0.3.0 把小模型层做成了"客户端"——它假设用户自己在跑 LM Studio 或 Ollama。
这一版把另一半补齐：**应用自己带 llama.cpp**，不需要装任何外部软件。

## 两步就能用

```powershell
voice-ctl llm --install      # llama.cpp 运行时：17.7MB 下载 / 39.8MB 磁盘
voice-ctl llm --download     # 模型：默认 0.5B，469MB
voice-ctl llm --status       # 看看齐了没
voice-ctl llm "有个文件要改一下"   # 试一句，看它挑谁、花多久
# 满意了再把 config.toml 的 [llm].enabled 改成 true
```

不想内置也行：`backend = "server"` + `endpoint = "http://127.0.0.1:11434/v1"`
就回到接外部服务的路子。LM Studio、Ollama、自己起的 llama-server 都认
（它们都是 OpenAI 的 `/chat/completions` 协议）。

## 为什么它不可能编造动作

这一层唯一的真实危险是模型吐出一个不存在的动作 id。两道锁：

1. **GBNF 语法约束**。llama.cpp 解码时按语法剪枝，只允许输出候选编号或 `null`
   ——模型**结构上不可能**越界。实测 20 次八竿子打不着的输入（「把冰箱里的牛奶
   拿出来」「abcdefg」「12345」），一次都没越界。
   这跟"在提示词里求它别乱说"是两回事：一个是约束，一个是请求。
2. **注册表校验**。没有语法约束的路径（外部服务走普通 JSON）回来的 id 也必须在
   注册表里，否则丢弃并说明。

## 实测：这个档位到底能不能用

这台机器（i7-13650HX，CPU 推理，无 GPU）。10 条用例 = 6 条真该命中 + 4 条负样本：

| 模型 | 加载 | 单次 | 真该命中 | 负样本误触发 |
|---|---|---|---|---|
| qwen2.5-0.5b Q4_K_M（469MB） | 0.7s | 90ms | **0/6** | 0/4 |
| qwen3-0.6b Q8_0（610MB） | 0.9s | 199ms | **0/6** | 0/4 |
| qwen2.5-1.5b Q4_K_M（1GB） | 1.4s | 110ms | **6/6** | 0/4 |

**0.5B / 0.6B 档位几乎只会答 `null`**。我没把这条藏起来，因为它是选型时最该知道
的事：这个档位不误触发（四类闲聊全被正确拒绝），但也基本帮不上忙。

- 想要真的有用 → `model = "qwen2.5-1.5b-instruct"`（1GB）
- 已经开了 Laya 语义层（`[decision]`，20 选项实测 6/6）→ 这一层能补的很有限，
  它的价值在"补充"而不是"替代"
- 不想背任何额外权重 → 保持 `enabled = false`，前三层覆盖绝大多数指令

## 工程上的三个决定

**CPU 版，不是 CUDA。** llama.cpp 的 Windows 构建里**没有 Vulkan**（只有 CPU /
CUDA / OpenVINO / SYCL / ROCm），而这个项目的前提是"无 GPU、CPU 即可"。
CPU 版还带一整套 `ggml-cpu-*.dll`（haswell / zen4 / sapphirerapids… 共 15 个），
运行时按 CPU 指令集自动挑一个，所以同一个包从 Sandy Bridge 到最新的机器都能跑，
用户不用挑也不用配。

**只解压 39.8MB，不是 120MB+。** 完整 zip 里六成是 bench / quantize / 多模态那些
永远用不上的 exe 和 impl.dll。留下的 22 个文件是**逐个删掉再跑
`llama-server --version`** 试出来的下限——包括看着像多模态才用的 `mtmd.dll`，
少一个就是 `0xC0000135`（DLL 找不到），而那个报错完全不指向真因。

**进程必须收得掉。** llama-server 在第一次推理时懒启动，`Engine.close()` 会显式
收掉它。不收的话它会活到用户重启——一个占着几百 MB 内存、在 127.0.0.1 上监听的
孤儿进程，比这个功能不存在更糟。端口也不写死（llama.cpp 自己的默认值就是 8080，
装过它的人机器上多半已经被占）。

## 打包

三个独立开关，默认都不开（精简版按需下载）：

```powershell
$env:VOICE_CTL_BUNDLE_LLAMA = "1"      # 把 39.8MB 运行时打进 exe —— 推荐
$env:VOICE_CTL_BUNDLE_LLM_MODEL = "1"  # 连 469MB 模型也打进去 —— 慎用
$env:VOICE_CTL_BUNDLE_MODEL = "1"      # 识别模型（0.2.0 就有）
```

**为什么不建议把 GGUF 模型打进 exe**：PyInstaller 的单文件 exe **每次启动都要把
内嵌数据解包到临时目录**。0.2.0 实测过这条路的代价——226MB 的识别模型让启动从
1.7s 变成 3.1s。469MB 只会更糟，而且是每次启动都白写 469MB 到磁盘。
让用户下一次，比每次启动都解包划算。

运行时那 39.8MB 则值得打进去：它是这一层能工作的前提，而且从 GitHub 下载要用户
能访问 github.com。

## 升级注意

- 不需要改配置。新增的 `[llm]` 字段都有默认值，`enabled` 仍是 `false`。
- 想从 0.3.0 的"外部服务"路子继续用：`backend = "server"`，其余不变。
- 内置小模型层**不引入任何 Python 依赖**——只用标准库 `urllib` 加一份 llama.cpp
  二进制。

## 实测（这台机器）

| 项 | 实测 |
|---|---|
| llama.cpp 运行时 | 17.7MB 下载 / 39.8MB 磁盘 / 22 个文件 |
| llama-server 冷启动 | **0.7s**（0.5B）/ 1.4s（1.5B） |
| 单次推理 | 90ms（0.5B）/ 110ms（1.5B） |
| 前三层 | 意图层约 1ms + 别名匹配约 1ms，不需要任何模型 |
| 单测 | **943 个，约 39 秒** |
