# Local Settings Storage

该包保存非敏感设置并加密 Provider secret。

- `contract.py`：Runtime/Sandbox/Agent/Provider 配置归一化和时区选项。
- `crypto.py`：`LocalDataKeyStore`、`encrypt_secret`/`decrypt_secret`。
- `store.py`：`LocalSettingsStore` 的读取、更新、Provider 管理和 dirty 标记。
- `__init__.py`：保持 storage settings 公开入口。

`config.toml` 只存非敏感值；API key 使用安装级数据密钥加密后进入 SQLite。
