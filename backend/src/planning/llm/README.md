# LLM Planner

该包实现模型驱动的决策循环。

- `planner.py`：LLM Planner 门面和 decide 循环。
- `decisions.py`：模式选择、tool/answer/Plan Review 决策。
- `requests.py`：请求装配；`formats.py`、`repairs.py`：结构化输出解析与修复。
- `selection.py`：Provider/模型选择；`titles.py`：对话标题请求。
- `__init__.py`：公开 Planner 构造。

Plan mode 只暴露只读能力；Agent handoff、输出修复和标题调用不得复用错误的工具/response_format 状态。
