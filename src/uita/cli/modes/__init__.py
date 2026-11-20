"""CLI Mode 处理器（业务逻辑分发层）。

各 `--mode` 对应一个子模块（`parse_mode.py` / `runner_mode.py` / ...），
`common.py` 存放跨 mode 复用的工具函数（StartupError、版本注入、
cases-dir/tag 解析等）。main.py 只负责从这里导入 handler 并组装
`_MODE_HANDLERS` 分发表，不直接实现业务逻辑（见宪法 R23）。
"""
