"""voice-ctl：按住快捷键说话 → 本地离线识别 → 匹配动作 → 执行。

设计约束（来自需求）：
  * 纯本地离线，不联网、零调用成本
  * 低开销：模型常驻，识别走 int8 ONNX，CPU 即可
  * 可拓展：新增能力 = 新增一个动作文件 + 配置里加几行

分层：
  热键  ->  录音  ->  ASR  ->  归一化  ->  [第0层 别名匹配 | 第1层 语义决策]  ->  执行
"""

from .config import AppConfig, ConfigError, load_config

__version__ = "0.2.0"

__all__ = ["AppConfig", "ConfigError", "load_config", "__version__"]
