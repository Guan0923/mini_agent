# Prompt 模板

该目录保存运行时系统提示模板。

- `instruction.md`：稳定的基础身份与安全边界。
- `default.md`：共享工作规则及唯一 `{{MODE_PROMPT}}` 插槽。
- `agent.md`、`plan.md`：执行模式与计划模式差异。
- `title.md`：首消息标题生成约束。
- `__init__.py`：读取并由 `compose_system_prompt` 组合模板。

模板文本属于运行协议；模式切换必须替换插槽而非同时拼接两种模式。
