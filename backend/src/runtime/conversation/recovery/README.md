# Conversation Recovery

该包重建中断运行并恢复 Session。

- `reconstruction.py`：`build_preview`、`reconstruct_attempt` 从持久化状态生成恢复视图。
- `resuming.py`：`ResumableConversation` 协议、`prepare_resume`、`resume_session`。
- `__init__.py`：公开恢复入口。

恢复必须保留原 Turn provenance/checkpoint；不可把失败/paused 状态伪装成新 Session。
