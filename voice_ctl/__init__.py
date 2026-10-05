"""voice-ctl：按住快捷键说话 → 本地离线识别 → 理解 → 执行。

设计约束（来自需求）：
  * 纯本地离线，不联网、零调用成本
  * 低开销：模型常驻，识别走 int8 ONNX，CPU 即可
  * 可拓展：新增能力 = 新增一个动作文件 + 配置里加几行

分层（**顺序不能变**，越往前越便宜、越确定）：

    热键 → 录音 → ASR → 归一化
        → 意图层（动词/否定/时间/动态应用词典，约 1ms）
        → 别名匹配（约 1ms，与 0.2.0 行为一致）
        → 语义决策（Laya ONNX，可选，40-100ms）
        → 小模型层（内置 llama.cpp，可选，90-110ms）
        → 执行（日程 / 开关应用 / 系统操作 / 按键 / 命令）

前两层不需要任何模型，覆盖绝大多数指令；后两层只在前面都没结果时才被调用。
"""

from .config import AppConfig, ConfigError, load_config

__version__ = "0.3.4"

__all__ = ["AppConfig", "ConfigError", "load_config", "__version__"]
