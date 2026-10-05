# voice-ctl 0.3.4：语义模型内置（但**默认不开**）

按住快捷键说话 → **本地离线**识别 → 理解整句话 → 执行。全程不联网、零调用成本。

这一版做三件事：把语义层的 **906MB 权重内置**进包、修掉一个会让"内置"白做的
接线缺口、修掉一个不该发生的 906MB 重复下载。

还有一件**没做**的事，以及为什么——见第四节，那是这一版最该被记住的部分。

## 一、内置权重（新增打包变体）

以前语义层要跑起来得手动下 906MB 权重（`voice-ctl fetch-decision`）。
现在有一个变体把权重一起打进去：

```powershell
$env:VOICE_CTL_BUNDLE_MODEL='1'             # 226MB 识别模型
$env:VOICE_CTL_BUNDLE_LLAMA='1'             # 39.8MB llama.cpp 运行时
$env:VOICE_CTL_BUNDLE_DECISION='1'          # 语义层代码（torch + transformers 约 600MB）
$env:VOICE_CTL_BUNDLE_DECISION_WEIGHTS='1'  # 906MB ONNX 权重
.venv\Scripts\python.exe -m PyInstaller voice-ctl.spec --noconfirm `
    --distpath dist-semantic --workpath build-semantic
```

出来是**目录版**（onedir），解压即用，不下载任何东西。

### 为什么必须是目录版

单文件版每次启动都要把内嵌数据解到临时目录。这条实测过：226MB 的识别模型
就让 `--version` 从 4.3s 变成 20.8s。906MB 权重只会更糟，而且是每次启动白写 906MB
到磁盘。所以 `BUNDLE_DECISION` 或 `BUNDLE_DECISION_WEIGHTS` 任一打开时，
默认出目录版；`VOICE_CTL_ONE_FILE=1` 仍然可以强制单文件。

### 体积的取舍

这是个 unfriendly 的档位：**约 1.8GB**。

| 组成 | 大小 |
|---|---|
| 语义层权重 `model_int8.onnx` | 873MB |
| tokenizer | 33MB |
| torch + transformers + onnxruntime | 约 500MB |
| 识别模型 + llama.cpp + 其余 | 约 270MB |

换来的是：**不需要任何下载，也不依赖 github.com 或 huggingface.co 可达**。
不需要语义层的话，84MB 精简版和 236MB 完整版都还在。

## 二、修掉「打进包也白打」的缺口

这是这一版**技术上最要紧**的部分。

ASR 模型一直有个兜底函数：

```python
bootstrap.resolve_model_dir(configured)   # 配置路径不存在 -> 去 _MEIPASS 里找
```

语义层**没有**对应函数，四处调用点全是裸的 `cfg.decision_path()`。
而 `decision_path()` 只做一件事：相对路径按**配置文件所在目录**拼接。
打包后那份 config.toml 在 exe 同级，那里没有权重——权重在 `_MEIPASS` 里。

所以直接加打包开关的后果是：**程序照样报「缺少 *.onnx」，而且报错完全不指向真因**
（用户会以为是自己没下权重，去下 906MB，下完还是同样的报错）。

现在补上了 `bootstrap.resolve_decision_dir()`，优先级：

```
配置指向的目录存在      → 用它（源码运行、或用户自己下了权重）
exe 同级 models/...     → 用它（用户手动放进来，可以换掉内置那份而不必重新打包）
_MEIPASS/models/...     → 用它（打包内嵌的那份）
都没有                  → 原样返回，报"缺少权重"并给下载指引
```

**第二条是刻意的**：用户放进来的文件是显式意图，应当赢过打包内那份。

四处调用点全部改用它：`runner.build_runtime()`、`doctor`、界面「设置」页的状态、
「关于」页的路径显示。后两处还会标出「（打包内嵌）」。

实测（在干净临时目录里跑打包产物）：

```
> --- 语义层 ---
  ✓ 可用—— 就绪：model_int8.onnx (873MB)
>   权重目录：C:\...\vcsem\_internal\models\laya-onnx\multilingual  （打包内嵌，不用下载）
```

## 三、权重已就绪时不再重复下载

`voice-ctl fetch-decision` 以前无条件下载 906MB——**内置了权重也照下**。
对一个"完全离线"的包来说这尤其荒唐。

现在先检查，已就绪就短路：

```
✓ 语义层权重已就绪，无需下载：
  就绪：model_int8.onnx (873MB)
  想强制重下加 --force；想下到别处用 --dir。
```

顺带修了落盘位置：以前用 `cfg.decision.onnx_dir` 的**原始字符串**，
相对路径会落到**当前工作目录**——用户可能从 `C:\Windows\System32` 里跑这个命令，
于是 906MB 下到那儿去了。现在没给 `--dir` 就钉在可写数据目录下。

### 还修了一个：精简版不该骗用户下 906MB

验证时顺手在精简版上跑了一次 `fetch-decision`，发现它**老老实实下完了 906MB**，
然后报：

```
✓ 权重就绪：C:\...\models\laya-onnx
  在 config.toml 里设 [decision].enabled = true 即可启用。
```

**但这个包加载不了语义层**——精简版没带 torch。用户照它说的去做，只会看到：

```
⚠ 语义层启用失败：这个精简版 exe 没带语义层（No module named 'torch'）
```

906MB 白下，而且报错发生在**最不该发生的时候**——用户以为已经装好了。

（这条在 0.3.2 就存在：`fetch-decision` 从来没检查过构建能不能用，
只管权重文件在不在。所以"内置权重"这个功能反而把它暴露了出来。）

现在下载**之前**就问一次这个构建能不能用，不能就明确劝阻并要求确认：

```
⚠ 这个构建加载不了语义层：
  这个精简版 exe 没带语义层（No module named 'torch'）

  也就是说：下面这 906MB 下完之后**仍然用不了**。
  想真正启用语义层，得换一个带语义层的构建
  （VOICE_CTL_BUNDLE_DECISION=1 重新打包），或直接跑源码版。
  权重本身没问题——只是这个 exe 缺了它需要的那一半。

仍然下载？[y/N]
```

判据用 `available()` 的返回语，而不是猜：以「缺少」开头的是**权重不存在**
（下载能解决），其余都是「这个构建缺依赖」（下载解决不了）。`--force` 跳过询问。

## 四、**没有**默认打开语义层，以及为什么

本来这一版打算做的第四件事是：既然 906MB 权重都内置了，就顺手把
`[decision] enabled` 自动打开，"装完即用"。**写完、测完，撤销了。**

在 21 个候选动作的真实配置下，用 11 条口语 + 14 条非命令量了一遍：

| 该不该命中 | 说法 | 置信度 | 语义层选了什么 |
|---|---|---|---|
| 该 | 帮我截个图 | 0.933 | sys.screenshot ✓ |
| 该 | 把音量调到最大 | 0.891 | sys.volume_up ✓ |
| 该 | 我想上网查点东西 | 0.796 | open.browser ✓ |
| 该 | 我想聊个天 | **0.153** | open.wechat ✓（但和 calc/schedule 几乎并列） |
| 该 | 屏幕别让人看了 | **0.290** | sys.lock ✓ |
| **不该** | 这个多少钱 | **1.000** | **web.bilibili** ✗ |
| **不该** | 今天天气怎么样 | 0.921 | schedule ✗ |
| 不该 | 明天 | 0.760 | schedule ✗ |
| 不该 | 算了 | 0.552 | open.calc ✗ |

**置信度和正确性不相关。** 正确判断低到 0.153，错误判断高到 1.000。
所以任何阈值都拦不住后者——它比正确判断还高。实测阈值扫描也是这个结论：

| 阈值 | 真该命中通过 | 不该命中漏过 |
|---|---|---|
| 0.30 | 9/11 | **11/14** |
| 0.45 | 6/11 | 6/14 |
| 0.60 | 3/11 | 4/14 |

`0.6` 这个值是在 **5 候选**下定的（当时负样本落在 0.589）。同一批权重、
同一批句子，换成 21 个候选后正确判断的分数被摊薄：

| 说法 | 5 候选 | 21 候选 |
|---|---|---|
| 有个文件要改一下 | 0.999 | 0.401 |
| 算个数 | 0.786 | 0.515 |
| 我想聊个天 | 0.933 | 0.153 |

**top-1 五条全对**——判别本身是准的，不准的是那个分数。但这不影响结论：
分数是唯一的出口，既然它不表示可靠性，就不能拿它当默认开关的依据。

端到端跑一遍（`simulate`）确认了实际影响：

```
这个多少钱  → 命中: web.bilibili via decision (score=1.00)
              执行: ✓ 已打开 https://www.bilibili.com     ← 真的开了
```

**默认打开等于默认乱执行。** 那比"少一个功能"严重得多，所以撤销。

顺带一个好消息：**句首否定是安全的**。意图层排在语义层之前，
「不要打开计算器」「别关微信」在意图层就被拦下，根本走不到语义层：

```
不要打开计算器 → 意图: none/negate —— 句首是否定词，不执行
```

（孤立调用语义层时，「不要打开计算器」会被判成 `open.calc` 0.833 —— 
所以这一类只能端到端量，单独测语义层会把危害量高。）

### 所以这一版给你的是什么

权重内置**解决的是"装完不用下载"**，不是"默认该开"。这两件事被分开了：

- 解压即用，一个字节不下载 ✓
- 语义层仍然要手动把 `[decision].enabled` 改成 `true`
- `config.toml` 里那一段的注释换成了上面这张实测表，
  用户开之前能看到真实代价
- 想开的话建议先用 `voice-ctl simulate "你的说法"` 试几条，这也是注释里的建议

### 留给以后

要让它能默认开，缺的不是阈值调参，是**拒识能力**：候选集之外的输入
（"这个多少钱"）必须能被判成"都不像"，而不是被 softmax 硬摊到某个动作上。
`logits` 那一路的输出（marker 打分）本来是给这个用的，现在没用上。

## 五、顺带量清楚的：torch 在推理路径上其实没用

为了判断"能不能不打包 torch"，我做了四次桩实验（把 `torch` 换成假的，
看 ONNX 语义层还能不能推理）。结论分两半：

**ONNX 图里已经到动作头了。** 图的输出是：

```
输入: input_ids, attention_mask, marker_pos, marker_mask, qtype
输出: logits (batch, markers)      ← marker 打分
      act_logits (batch, 2)        ← 动作分布
```

而 `laya/common.py` 的 `DecisionModel.forward()` 算的就是这两件事。
也就是说**纯推理不需要 torch 做任何计算**，它是 import 链上的负债。

**但 laya 包脱离 torch 加载不了。** 卡点三层，逐层试出来的：

1. `common.py:472` 模块级 `class DecisionModel(nn.Module)`、
   `_DynamicMultiheadAttention(nn.MultiheadAttention)` —— 光**继承**就要真 torch
   （桩得支持 `__mro_entries__` 才能过）
2. `onnx_agent.py` 没有 `from __future__ import annotations`，所以类型注解是
   **运行时求值**的，会碰 `torch.FloatTensor` 参与 `|` 运算
   （桩得支持 `__or__` / `__ror__` / `__getitem__` 才能过）
3. `transformers` 的懒加载用 `is_torch_available()` 判断后端
   （`AutoTokenizer` → `GenerationMixin`）——它要**真 torch**，不是"能导入的假模块"

前两层桩能过，第三层过不去。所以这一版照常打包 torch。真想做"零 torch 语义层"，
正确路子是**绕开 laya 自己跑 ONNX**（图输入输出已量清，缺的只有 `common.py` 里的
tokenize / marker 拼装 / 解码，全是纯 Python 算术），而不是想办法骗过
`is_torch_available()`。

## 升级注意

- **不需要改配置。** `[decision].onnx_dir` 保持相对路径即可，内嵌兜底会自动接管。
- 语义层仍然默认关闭。要开就把 `[decision].enabled` 改成 `true`——**先读那一段
  的注释**，那是实测数字。
- 如果你已经把权重下到 exe 同级 `models/laya-onnx/multilingual`，它**优先于**内嵌那份
  ——所以想换权重不必重新打包。
- 精简版和完整版的 exe 尺寸没变。

## 实测

| 项 | 结果 |
|---|---|
| 权重目录校验 | `check_weights` 通过，906MB（873 + 33 + 两个 json） |
| 内嵌后路径兜底 | 干净临时目录里 `doctor` 认出「（打包内嵌，不用下载）」 |
| 语义层在冻结 exe 里 | **真的加载并推理**：决策 108ms，首次加载 14s（含 873MB 图 + torch 导入） |
| 精简版误下防护 | `fetch-decision` 在下载前劝阻，回答问题 n 时**一个字节都没下** |
| 新增单测 | 21 个 |
| 单测总数 | **1094 个通过，2 跳过**（约 45 秒） |
| 打包开关去重 | torch/onnxruntime 动态库重复收集被去掉（16 → 14） |
| 语义版体积 | 1.77GB（6078 个文件），内嵌 exe 本体只有 60.7MB |

