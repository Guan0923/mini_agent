# Planning

该包把模型请求、上下文管理和决策策略组织成 Planner 边界。

- `__init__.py`：公开 Planner 协议、能力描述以及 LLM/规则 Planner 实现。
- `base.py`：Planner 协议；`rule_based.py`：无需模型的规则策略。
- `capabilities.py`：模式与工具能力选择。
- `context_management.py`：上下文预算/压缩接口。
- `model_requests.py`、`model_outputs.py`：模型输入输出契约。
- `llm/`：LLM 决策循环、修复、格式与标题生成。
- `prompts/`：Agent/Plan/Title 模板组合。

Planning 依赖 domain 和 Provider 协议，不负责持久化或 HTTP 展示。

Plan、Agent、Compact、Title、Skill 等流程不得为捕获到的异常添加展示前缀；分类与 repair 诊断放在结构化元数据，展示文本使用统一安全根因投影。
