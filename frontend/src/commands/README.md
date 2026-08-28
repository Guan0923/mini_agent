# Composer 命令

该包解析斜杠命令和文件补全交互。

- `index.ts`：命令表、`parseCommand` 与 `HELP_TEXT`。
- `completion.ts`：命令候选、补全文本、键盘动作和循环索引。
- `fileCompletion.ts`：`FileTrigger`、候选转换、`@` 文件 token 与键盘动作。
- `fileCompletion.test.ts`：文件触发与候选行为回归。

Chat 页面调用纯函数完成交互；该包不依赖 React 状态或 API。
