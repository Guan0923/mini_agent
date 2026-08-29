# Mini-Agent Memory 系统代码逐文件导读

> 阅读顺序按照运行时依赖排列。相邻且作用相同的代码行合并为一个行段解释；因此每个文件的所有行都被覆盖。纯空行只用于视觉分隔，没有运行时行为。

## 总调用链

```text
MemorySettings
  -> MemoryStore(SQLite + FTS5)
  -> Phase 1 eligibility -> sanitization -> extraction -> evidence/candidate/watermark
  -> Phase 2 consolidation -> semantic/procedural -> soft delete -> projections
  -> retrieval(BM25/scope/recency/confidence/evidence)
  -> prompt injector
  -> LLM planner(normal/plan/compact)
  -> scheduler(manual + idle background)
```

三个开关的含义：

```text
generate_memories          允许手动生产
use_memories               允许读取和注入
automatic_memory_enabled   允许后台扫描空闲会话
```

## 1. `backend/src/domain/memory.py`

**作用：** 定义 Memory 的领域对象、枚举和所有基础校验；不访问 SQLite、不调用模型、不处理 HTTP。

### 逐行导读

- **1～15 行**：模块说明、未来注解、标准库导入、统一 UTC 时间函数，以及安全 ID 正则。安全 ID 只允许字母数字开头，后续只能出现字母、数字、`_`、`-`，防止把路径穿越字符串当成用户、项目或会话 ID。
- **17～25 行**：`MemoryKind` 和 `MemoryScope`。前者区分 episodic、semantic、procedural；后者区分全局记忆和项目记忆。
- **27～49 行**：`MemoryStatus`、`MemoryCandidateStatus`、`MemoryJobKind`、`MemoryJobStatus`。这些字符串枚举同时约束 Python、SQLite、API 和前端状态。
- **51～65 行**：不可变 `MemorySettings` 字段。三个开关默认关闭；检索数量、注入条数、Token 和字节预算都有安全上限。
- **66～84 行**：`MemorySettings.__post_init__`。检查布尔类型、模型名长度、数值范围，并禁止后台调度在生成开关关闭时单独启用。
- **85～110 行**：`from_mapping` 和 `to_dict`。前者把不可信配置合并到安全默认值并重新验证，后者把强类型配置序列化为 TOML/API 可用字典。
- **132～170 行**：`MemoryItem`。长期记忆的权威对象，包含类型、标题、内容、作用域、置信度、状态、时间和软删除时间；构造时校验 ID、文本、范围、状态和时间戳。
- **171～197 行**：`MemoryItem.new`。生成 `memory_<uuid>`，填充 UTC 创建和更新时间，供 Phase 2 或测试创建条目。
- **200～224 行**：`MemoryEvidence`。表示一条可追溯来源，必须指向 Memory、会话和可选 turn；保存来源类型、摘要和内容 SHA-256。
- **225～244 行**：`MemoryEvidence.new`。生成 `evidence_<uuid>`，作为手动来源或会话来源的便捷构造器。
- **247～281 行**：`MemoryCandidate`。Phase 1 生成、Phase 2 消费的候选记录；保留 session、project、episodic memory、置信度和候选状态。
- **282～308 行**：`MemoryCandidate.new`。生成 `candidate_<uuid>` 并使用当前 UTC 时间。
- **311～328 行**：`MemoryWatermark`。保存某个会话已经处理到的消息位置和事件 ID，只允许非负位置及安全 ID。
- **329～397 行**：`MemoryJob`。描述提取、整合和投影重建任务，包含租约、尝试次数、可执行时间和错误；运行中必须有租约，非运行状态不能残留租约。
- **398～402 行**：`MemorySearchResult`，把 Memory 条目和 FTS 排名绑定起来。
- **404～425 行**：`MemorySelectionDiff`。Phase 2 的原子结果，要求 added、retained、removed 三组 ID 不重复且互斥。
- **426～449 行**：`EpisodicMemoryRecord`。把候选、episodic 条目和证据绑定；强制候选与条目类型一致、候选仍为 pending、证据来自同一会话。
- **450～522 行**：底层校验函数。分别校验安全 ID、必填文本、可选文本、枚举、置信度、整数范围、模型选择器、标签、作用域和 UTC ISO-8601 时间。
- **523～543 行**：可选时间校验和 `__all__` 导出列表，明确本模块的公共领域 API。

## 2. `tests/test_memory_domain.py`

**作用：** 用最小测试验证领域对象的不可变约束和安全边界。

- **1～19 行**：导入 pytest、`replace`、领域枚举和测试对象。
- **21～40 行**：验证三种 Memory 类型都能创建，项目作用域必须带 project_id，全局作用域不能带 project_id。
- **42～53 行**：参数化验证路径穿越、空 ID、空白和特殊字符都会被拒绝。
- **55～70 行**：验证时间戳必须是带时区的规范 UTC，同时验证 Job 租约状态一致性。
- **72～76 行**：验证 SelectionDiff 的 added、retained、removed 不能重复或交叉。

## 3. `tests/test_memory_storage.py`

**作用：** 存储层的行为规格，阅读 `storage/memory.py` 前先看它。

- **1～69 行**：测试辅助工厂，构造 MemoryItem、Candidate、固定时间和路径。
- **71～96 行**：验证 Memory 目录懒创建；只读查询和 AGENTS 操作不会创建目录，第一次真正写入才创建。
- **98～163 行**：验证 FTS、用户隔离、项目作用域、更新、软删除、恢复边界，以及 Evidence、Candidate、Watermark、Job 不会跨用户泄漏。
- **164～205 行**：验证 Evidence 去重、删除级联、Candidate 状态迁移和 Phase 1 原子批处理。
- **206～239 行**：验证 Job 租约、过期恢复、失败次数和完成者校验。
- **240～280 行**：验证重复 Job 幂等、取消、清空事务、禁用条目从 FTS 隐藏后可恢复。
- **281～345 行**：验证 schema 初始化、未来版本拒绝、v1/v2/v3 迁移和外键完整性。
- **346～377 行**：验证 raw Markdown 和 rollout summary 从数据库重建，删除投影文件后仍可恢复，旧 stale 文件会被清理。
- **378～416 行**：验证数据库、目录、投影文件和符号链接路径安全。

## 4. `backend/src/runtime/memory/eligibility.py`

**作用：** 只做资格判断，不读数据库、不调用模型。

- **1～10 行**：导入、来源 ID 正则和模块常量。
- **12～22 行**：`MemoryEligibilityReason`，列出 disabled、running、subagent、too_short、external_context、no_new_events 等原因。
- **24～75 行**：`MemoryEligibilityInput` 及其校验，确保会话状态、位置、文本字节数和策略参数类型正确。
- **76～82 行**：不可变 `MemoryEligibilityDecision`，同时返回是否通过、原因和处理位置。
- **83～109 行**：`evaluate_memory_eligibility` 按固定顺序短路：生成关闭、运行中、subagent、外部上下文、没有新事件、过短，全部通过才允许模型调用。

## 5. `backend/src/runtime/memory/sanitization.py`

**作用：** 在任何模型调用前删除指令载荷并脱敏秘密。

- **1～30 行**：定义 AGENTS/Skill XML 块、未闭合标记、指令标题、PEM 私钥、命名秘密、Bearer、常见 token 和 JWT 的正则。
- **32～37 行**：`MemorySanitizationResult`，记录清洗后文本、脱敏次数、是否移除指令、是否截断。
- **39～45 行**：`MemorySanitizer` 构造器，限制最大 UTF-8 字节数。
- **47～60 行**：`sanitize` 主流程：删除指令块和标题，遇到未闭合指令时截断；替换私钥、命名秘密、Bearer、token、JWT，移除 NUL，并按 UTF-8 边界截断。
- **61～91 行**：命名秘密替换函数、结果对象构造和公共导出。

## 6. `tests/test_memory_phase1.py`

**作用：** Phase 1 的完整行为规格，模型是固定 mock。

- **1～98 行**：固定提取模型、会话快照和 extractor 工厂；mock 同时返回正常、空输出和非法输出。
- **104～170 行**：验证清洗、脱敏、Episodic/Candidate/Evidence/Watermark 写入、源码可重建内容过滤、重复执行幂等，以及 SQLite/Markdown 不保存秘密。
- **171～180 行**：非法模型 Schema 输出必须原子失败，不能留下部分候选或 watermark。
- **181～190 行**：模型返回空候选时仍推进 watermark，避免重复扫描同一批消息。
- **192～222 行**：验证运行中、subagent、外部上下文和过短会话不会调用模型。
- **225～253 行**：验证从运行时工具消息识别 Web/MCP 外部上下文，并能从真实 session store 装配快照。
- **254～274 行**：验证持久化会话的消息、项目 ID 和 Evidence turn ID 能正确映射。
- **275～287 行**：直接测试 Sanitizer 对 Authorization、Cookie、token 和未闭合 Skill 块的处理。

## 7. `backend/src/runtime/memory/extraction.py`

**作用：** 实现 Phase 1：会话快照 → 清洗 → mock/provider 模型 → Schema → Episodic 记录。

- **1～80 行**：导入、Phase 1 JSON Schema、模型提示词和外部工具前缀。
- **82～108 行**：`MemoryModelOutputError` 和 `MemoryExtractionPolicy`，限制最小消息数、最小字节数、输入上限、候选数量和外部上下文策略。
- **110～150 行**：`MemorySourceMessage`、`MemorySessionSnapshot`、`CleanMemoryMessage`，验证位置递增、状态、项目 ID、subagent 和外部上下文标记。
- **151～184 行**：`EpisodicExtractionRequest`、`MemoryExtractionResult` 及模型/存储 Protocol，定义 Provider 端口而不依赖具体 Provider。
- **187～218 行**：存储 Protocol 和兼容性 recorder，规定 watermark、batch 和候选写入接口。
- **219～268 行**：`ManualEpisodicExtractor` 构造器和 `from_settings`，把用户配置绑定到提取策略和模型选择器。
- **269～304 行**：`extract_session` 从 session source 读取 summary、runtime、conversation records，转换为快照并进入统一 `extract`。
- **305～351 行**：`extract` 读取旧 watermark，准备增量消息，执行 eligibility；未通过不调用模型，通过后创建请求、解析输出、生成记录、计算 watermark，并以一个存储批次提交。
- **352～379 行**：`_prepare_messages` 调用 Sanitizer，跳过空内容，限制 UTF-8 输入预算，生成稳定的 turn ID。
- **380～454 行**：`_parse_output` 严格检查顶层字段、候选数量、必填字段、Evidence ID、用户消息证据、置信度、标签、文本长度和 rediscoverable 标记；重复候选和源码可重建候选被丢弃。
- **455～513 行**：`_build_record` 把候选转换为 Episodic Memory、Candidate 和 Evidence，生成确定性 ID，确保同一会话同一内容可以幂等重放。
- **514～573 行**：模型文本清洗、会话状态推断、工具上下文识别、SHA-256 ID、UTF-8 安全截断等辅助函数。

## 8. `backend/src/runtime/memory/consolidation.py`

**作用：** 实现 Phase 2：Episodic candidates → Semantic/Procedural，并处理冲突和遗忘。

- **1～120 行**：导入、Phase 2 JSON Schema 和整合提示词；提示词明确不创建 Skill、不执行 Procedural。
- **121～199 行**：CandidateView、ExistingMemoryView、ConsolidationRequest/Result，以及模型和存储 Protocol。
- **200～214 行**：轻量 `MemoryConsolidator.apply`，把 SelectionDiff 应用到存储并按需重建投影。
- **215～254 行**：`ManualMemoryConsolidator` 构造器、配置绑定和模型名选择。
- **255～310 行**：读取指定项目或全局的 pending candidate，加载现有 Memory，构造模型请求；空候选直接返回，不调用模型。
- **311～391 行**：解析 Phase 2 输出，验证 added、retained、removed、rejected 四组结果覆盖且互斥，避免模型漏处理候选。
- **392～469 行**：解析新增 Semantic/Procedural 条目，清洗字段、校验候选引用、作用域、标签和置信度；所有新增条目必须能关联候选证据。
- **470～499 行**：复制和扩展 Evidence，把新长期记忆追溯到原始会话。
- **500～565 行**：解析结构、现有条目视图、候选 ID、字符串列表、文本清洗、归一化和哈希辅助函数。

## 9. `tests/test_memory_phase2.py`

**作用：** Phase 2 的固定 mock 行为规格。

- **1～110 行**：构造 Episodic 记录和 mock 整合模型，覆盖 Semantic 与 Procedural 输出及秘密清洗。
- **111～141 行**：验证跨会话合并、Evidence 追溯、投影生成、Procedural 仅作为建议，以及重复整合不再次调用模型。
- **142～194 行**：验证新证据修正旧事实，旧 Memory 软删除、Evidence 审计保留，并支持恢复。
- **195～228 行**：验证 retained 记忆会增加新 Evidence。
- **229～254 行**：验证 Schema 不完整时所有候选保持 pending，不发生半成功。
- **255～266 行**：验证项目候选不会被全局或其他项目 Phase 2 消费。

## 10. `backend/src/runtime/memory/retrieval.py`

**作用：** 检索、排序、预算控制、Prompt 注入和可观测记录。

- **1～29 行**：导入、Memory Prompt 前后缀；明确 Memory 不可信、可能过时，不能覆盖 AGENTS、安全或权限，Procedural 只是建议。
- **30～47 行**：`MemorySearchStore` Protocol，只依赖搜索和 Evidence 查询端口。
- **48～73 行**：`MemoryScoreComponents`，将 BM25、作用域、时间、置信度、Evidence 质量加权成总分。
- **75～124 行**：`RankedMemory`、`MemoryRetrievalResult` 和诊断序列化，记录选择原因、预算和候选信息。
- **125～154 行**：进程内 `MemoryDiagnosticsRegistry`，按 user_id/session_id 隔离最近一次检索诊断。
- **155～171 行**：`MemoryRetriever`，提供轻量的原始搜索接口。
- **172～218 行**：`MemoryContextSelector.select`，限制 query，调用 FTS，计算 Evidence 和复合排序分数，再进入预算裁剪。
- **219～255 行**：按条数、字节数、Token 数逐条选择；超限条目保留 `item_limit`、`byte_budget` 或 `token_budget` 原因。
- **256～311 行**：`MemoryPromptInjector`。关闭读取时原样返回 SystemMessage；开启时捕获检索异常、记录诊断，并把渲染后的 Memory 追加到系统提示。
- **312～366 行**：Token 估算、排序决策、BM25 归一化、时间衰减和 Evidence 质量计算。
- **367～410 行**：HTML 转义 Memory 内容、渲染上下文、从 runtime 提取 query、限制 query 字节数和导出列表。

## 11. `tests/test_memory_retrieval.py`

**作用：** 检索、Prompt 和配置 API 的行为规格。

- **1～104 行**：RecordingClient、失败存储、固定模型和 Memory/Evidence 工厂。
- **105～122 行**：验证默认配置全部 opt-in、类型严格、预算边界和自动调度依赖生成开关。
- **123～155 行**：验证生成配置控制 Phase 1/Phase 2，并正确选择模型。
- **156～201 行**：验证 FTS/BM25、项目优先级、全局回退、时间、置信度和 Evidence 评分。
- **202～226 行**：验证字节预算、HTML 转义和 Memory 安全提示词。
- **227～266 行**：验证普通请求、Plan 请求和压缩请求共享同一个 Memory 注入器。
- **267～296 行**：验证关闭 Memory 时三类请求与无 Memory 基线逐字节相同，并且不创建目录。
- **297～322 行**：验证 Memory 检索故障不会让正常模型请求失败。
- **323～433 行**：通过 TestClient 验证配置 API、dry-run、Evidence 查询、注入历史、禁用、恢复、软删除、手动任务、清空确认和三步灰度配置。

## 12. `backend/src/runtime/memory/provider_models.py`

**作用：** 把现有 LLM Provider 转成 Memory 的可替换模型端口。

- **1～26 行**：导入、Provider 不可用和额度不可用异常类型。
- **27～53 行**：`extract_episodic` 把清洗后的消息、位置和项目 ID 组织成 JSON payload，要求 Provider 返回 Phase 1 Schema。
- **54～90 行**：`consolidate_memories` 把候选、Evidence 和已有 Memory 组织成 Phase 2 payload。
- **91～159 行**：`_complete_json` 创建临时 LLMClient，使用温度 0、JSON 输出和严格 Schema；把配置错误、认证错误、额度错误分类，关闭 transport，不泄露原始凭据。
- **160 行**：公共导出列表。

## 13. `backend/src/runtime/memory/scheduler.py`

**作用：** 负责任务持久化调度、租约、并发、取消、退避和进程退出。

- **1～55 行**：导入和 `MemoryJobStore` Protocol，声明 enqueue、claim、complete、fail、cancel 端口。
- **56～95 行**：`MemoryJobScheduler` 是对持久化 JobStore 的薄封装。
- **96～114 行**：`MemoryAutomationSettings`，校验空闲阈值、扫描间隔、Phase 1 并发和重试范围。
- **115～137 行**：服务初始化，建立锁、用户锁、活动任务表、Phase 2 全局串行标记和唤醒事件。
- **138～168 行**：`start` 启动服务 lane，`close` 取消服务，`wake` 触发提前扫描。
- **169～199 行**：手动提取和整合入口；只检查 `generate_memories`，因此自动调度关闭时手动任务仍可运行。
- **200～228 行**：取消用户任务和清空用户 Memory；清空前取消活动任务，随后清空数据库并重建空投影。
- **229～247 行**：`scan_once`、循环等待和错误隔离；单个用户失败不会影响其他用户。
- **248～266 行**：发现已启用生成的用户；只把 `automatic_memory_enabled` 放在 `_scan_user` 判断中，避免自动扫描和手动派发混在一起。
- **267～296 行**：跳过删除、运行中、过短或未到空闲阈值的会话；根据 watermark 增量排队 Phase 1。
- **297～351 行**：有限并发派发 Phase 1、全局串行派发 Phase 2，使用用户锁和 JobRegistry。
- **352～405 行**：执行任务；再次检查取消和配置，调用 extractor/consolidator，Phase 1 有候选时自动排队 Phase 2，写入前再次检查取消，模型不可用时静默取消。
- **406～437 行**：失败指数退避、取消检查、安全取消、活动任务检测和用户锁。
- **438～473 行**：读取用户 MemorySettings、取消异常、可取消模型包装器；模型调用期间取消会阻止迟到写入。

## 14. `tests/test_memory_automation.py`

**作用：** 验证 Scheduler 的实际并发和失败隔离。

- **1～124 行**：构造可控 Settings、项目、固定模型、状态和持久化会话。
- **126～154 行**：验证空闲会话经过 Phase 1 后进入全局串行 Phase 2，并更新 watermark。
- **155～189 行**：验证灰度第一步：自动开关关闭时不扫描，但手动提取仍可执行；开启自动开关后才扫描新的空闲会话。
- **190～213 行**：Provider 不可用时任务取消，不抛到聊天请求，watermark 不推进。
- **214～246 行**：模型调用期间取消，释放后也不会迟到写入 Memory。

## 15. `backend/src/runtime/memory/__init__.py`

**作用：** 统一导出 Memory runtime 公共 API。

- **1～30 行**：从 consolidation、eligibility、extraction、provider_models、retrieval、sanitization、scheduler 导入公共类型。
- **31～74 行**：`__all__` 明确对外暴露的 Schema、入口类、结果类型、异常、Provider 和 Scheduler；内部辅助函数不导出。

## 16. `backend/src/storage/memory.py`

**作用：** Memory 唯一权威存储，约 1500 行；建议结合 `test_memory_storage.py` 阅读。

- **1～38 行**：模块说明、导入和常量。
- **39～174 行**：SQLite schema 字符串，定义 metadata、items、evidence、candidates、watermarks、jobs 和 FTS5 虚拟表、索引及约束。
- **175～193 行**：查询 token 正则和存储异常类型。
- **194～219 行**：`MemoryStore` 初始化、数据库存在判断、懒创建、schema 版本查询。
- **220～385 行**：Memory CRUD、状态切换和 FTS 搜索；所有查询默认隐藏 disabled/deleted，并通过作用域条件过滤项目。
- **386～434 行**：Evidence 新增、查询、去重和删除。
- **435～509 行**：Candidate 新增、查询、状态转换和删除。
- **510～572 行**：Phase 1 的 Candidate/Episodic/Evidence/Watermark 原子批处理，以及 Phase 1 记录读取。
- **573～588 行**：watermark 读取、单调推进和回退冲突保护。
- **589～758 行**：Job enqueue、幂等 enqueue、查询、列表、claim 租约、complete、fail、cancel。
- **759～795 行**：清空所有 Memory 数据和旧的 SelectionDiff 应用。
- **796～875 行**：Phase 2 原子批处理：新增长期 Memory、更新 Candidate 状态、软删除旧 Memory、复制 Evidence，并在事务中保持一致。
- **876～912 行**：从数据库读取 active 条目和 Evidence，重建 raw Markdown、rollout summary，删除 stale 投影并拒绝符号链接。
- **913～989 行**：SQLite connection 上下文、事务提交/回滚、schema 初始化和外键设置。
- **990～1022 行**：目录、文件类型、schema 版本和必需列验证。
- **1023～1150 行**：v1→v2、v2→v3 数据迁移，新增 memory_id、disabled 状态和活动任务唯一索引。
- **1151～1367 行**：数据库行与领域对象之间的双向转换、insert-or-match 幂等逻辑和 watermark/job 序列化。
- **1368～1468 行**：作用域 SQL 子句、raw/rollout Markdown 渲染、limit/FTS query、时间、标题和路径安全辅助函数。
- **1469～1497 行**：原子文本写入和模块公共导出。

## 17. `backend/src/storage/settings_contract.py`

**作用：** 统一本地配置和可选云端配置的验证契约。

- **1～59 行**：默认 profile、agent、provider、memory、runtime、sandbox 配置。
- **60～72 行**：runtime 配置合并和数值校验。
- **73～83 行**：Memory 配置合并；关闭生成时自动把后台调度也关闭，然后交给 `MemorySettings` 做最终验证。
- **84～148 行**：Sandbox 配置验证；与 Memory 无直接业务关系，但共享用户配置契约。
- **149～174 行**：Agent 配置规范化。
- **175～221 行**：Provider 配置规范化，避免 API Key 进入 TOML。
- **222～223 行**：时区选项导出。

## 18. `backend/src/storage/user_settings.py`

**作用：** 每用户 `user.db` 和配置代理。

- **1～66 行**：用户设置 schema，包含 profile、provider、sync 和默认 Memory 配置。
- **67～139 行**：`UserSettingsStore` 初始化、默认值和数据库连接。
- **140～275 行**：metadata、sync 偏好、sync 状态和事件批次管理。
- **276～353 行**：`PerUserSettingsRepository` 按 user_id 缓存和分发设置操作，确保不同用户不会共享 Memory 配置。

## 19. `backend/src/configuration.py`

**作用：** 统一用户路径、会话路径、Memory 路径和配置文件安全。

- **1～56 行**：配置异常、安全 ID 和配置密钥过滤正则。
- **57～109 行**：`ClientPaths` 计算 config、user.db、projects.db、`memories/`、`memory.db`、raw Markdown 和 rollout 目录。
- **110～157 行**：runtime、sync、skills、logs、plugins、MCP 路径属性。
- **158～267 行**：会话 DB、workspace、uploads、旧 uploads 迁移和符号链接防护。
- **268～350 行**：用户根目录懒创建、Memory 目录懒创建和路径验证。
- **351～414 行**：TOML 读取和默认配置初始化，其中 Memory 默认三个开关都为 false。
- **415～471 行**：TOML 序列化、原子写入和配置文件安全写入。
- **472～543 行**：`UserConfigStore` 的读取、更新、替换 section、默认合并和锁文件。
- **544～602 行**：递归合并、递归删除配置秘密、认证用户根判断和 device_id 补全。
- **603 行**：模块末尾。

## 20. `backend/src/api/user_data.py`

**作用：** Web 用户数据根目录和会话文件复制；保证 Memory 路径的用户隔离。

- **1～67 行**：数据根目录访问、用户 ID 校验、用户根目录解析。
- **68～119 行**：`user_paths` 创建用户配置默认值，其中包含 Memory 三个开关。
- **120～181 行**：会话 workspace、文件和 uploads 复制；Memory 不参与会话文件复制。
- **182～256 行**：guest session 导入和用户边界校验。
- **257～343 行**：无符号链接复制、活动会话检查和辅助路径函数。
- **344～353 行**：benchmark 用户目录辅助函数和导出。

## 21. `backend/src/api/auth/routes.py`

**作用：** 用户设置 API；Memory 相关代码只占其中一小段。

- **1～96 行**：认证请求模型、用户资料、Agent、Runtime 和 Sandbox 请求定义。
- **97～109 行**：`MemoryConfigPayload`，声明三个开关、模型选择器和检索/注入预算，并使用 Strict 类型。
- **110～245 行**：其他认证模型、错误转换、Origin 检查、用户准备和 session cookie 逻辑。
- **246～483 行**：guest、注册、登录、资料、settings、sandbox、agent、runtime 等非 Memory API。
- **484～508 行**：`PUT /api/auth/memory-config`。合并配置，必要时懒创建 Memory 目录；生成关闭时取消生成任务，生成开启或配置变化时唤醒 Scheduler。
- **509～753 行**：Provider 配置、模型发现、登出和设备授权 API，与 Memory Provider 选择间接相关。

## 22. `backend/src/api/memory_routes.py`

**作用：** 受认证保护的 Memory 管理和内部测试 API。

- **1～48 行**：请求模型：提取、整合、启停条目和清空确认。
- **49～96 行**：列 Memory 和 Evidence，并按用户、项目作用域检查访问权限。
- **97～134 行**：dry-run 检索；返回实际排序、预算、是否会注入和渲染上下文，但不修改模型 Prompt。
- **135～169 行**：最近一次注入、注入历史和 Job 列表。
- **170～211 行**：手动 Phase 1/Phase 2 入队；检查会话存在、项目绑定和生成开关。
- **212～241 行**：取消 Job、启用/禁用 Memory。
- **242～288 行**：软删除、恢复和强确认清空。
- **289～365 行**：按用户创建 Store、项目检查、条目/Evidence/Job JSON 序列化和统一错误映射。

## 23. `backend/src/api/state.py`

**作用：** 组装 Web 应用的全局状态和 Memory 自动服务。

- **1～25 行**：导入、默认数据根目录和状态依赖。
- **26～154 行**：`WebAppState.__init__`。创建每用户 settings、JobRegistry、MemoryDiagnosticsRegistry、ProviderMemoryModel 工厂和 `MemoryAutomationService`。
- **155～162 行**：启动和关闭后台 Memory Scheduler。
- **163～215 行**：用户路径、项目、workspace、uploads 访问，并始终以 user_id 做边界。
- **216～256 行**：sync token、benchmark 和云端辅助逻辑。
- **257～322 行**：组合 settings、provider、agent、runtime 配置，供 Runner 和 API 使用。
- **323～345 行**：关闭资源、退出时安全停止 Scheduler 和其他后台组件。

## 24. `backend/src/api/app.py`

**作用：** FastAPI 应用入口。

- **1～27 行**：导入应用依赖和路由模块。
- **28～69 行**：`create_app` 创建 FastAPI，注册 auth、session、project、memory 等路由和统一异常处理。
- **70～88 行**：health/ready 接口和应用事件。
- **89～96 行**：启动 `WebAppState` 服务、关闭资源并导出应用工厂。

## 25. `backend/src/runtime/application/factory.py`

**作用：** 把配置、Store、Selector、Injector、Planner、Runner 和工具组装起来。

- **1～48 行**：导入运行时、Provider、工具、Skill、Memory 和 Sandbox 依赖。
- **49～81 行**：客户端路径、session store 和 application 构造入口。
- **82～146 行**：解析配置、Provider、用户偏好、MemorySettings 和运行时资源。
- **147～195 行**：Runner、subagent 和 job scope 装配。
- **196～276 行**：创建子 Runner 工厂，继承用户、项目、Memory 和安全上下文。
- **277～361 行**：`_build_runner`。读取 Skills、发现 AGENTS；为 LLM Planner 创建 `MemoryStore → MemoryContextSelector → MemoryPromptInjector`，然后注入 `LLMPlanner`。
- **362～441 行**：外部资源、Sandbox policy 和工具运行时。
- **442～490 行**：用户配置、终端、sync coordinator 和其他工厂辅助函数。

## 26. `backend/src/planning/llm/requests.py`

**作用：** 定义普通、当前轮、压缩请求如何构造消息。

- **1～51 行**：导入和完整的压缩提示词模板。
- **52～101 行**：`_messages_for_request`。顺序是用户偏好 → AGENTS → Active Skills → Memory → 历史/工具。
- **102～122 行**：当前轮请求使用同样的 AGENTS、Skill、Memory 顺序，只缩小历史范围。
- **123～140 行**：追加用户偏好并明确其低于系统、安全、AGENTS 和 Skill。
- **141～185 行**：追加 AGENTS 和 Skill；Skill 内容保持不可信并受权限边界限制。
- **186～198 行**：调用 MemoryPromptInjector；没有注入器时完全返回原 SystemMessage。
- **199～222 行**：压缩请求也调用 `_with_memory_context`，因此普通、Plan、compact 行为一致。
- **223～274 行**：底层普通文本/JSON 请求和 Provider 参数装配。

## 27. `backend/src/planning/llm/planner.py`

**作用：** LLM Planner 的组合类，真正的消息构造逻辑来自 `RequestMixin`。

- **1～23 行**：导入多个 planner mixin，声明 `LLMPlanner` 组合类。
- **24～47 行**：构造 Planner，保存 client、工具规格、用户偏好、AGENTS 和 Memory injector。
- **48～58 行**：普通决策和上下文压缩入口，最终都会经过 requests.py 的注入逻辑。
- **59～60 行**：工具规格兼容转换辅助函数。

## 28. `tests/test_memory_runtime.py`

**作用：** 验证运行时适配器、手动提取、整合、检索和调度之间的连接。

- **1～32 行**：导入固定 mock、Runner、Store 和 Runtime 依赖。
- **33～42 行**：验证 eligibility 是纯函数并支持增量位置。
- **43～78 行**：验证 runtime adapter 能记录、读取、整合 Memory 并把任务交给 Scheduler。
- **79 行**：文件结束。

## 29. 前端对应文件（Python 调用链之后阅读）

前端不是 Python 文件，但它是后端 API 的可视化入口：

- `frontend/src/api/auth.ts`：MemoryConfig TypeScript 契约。
- `frontend/src/api/memory.ts`：列表、Evidence、dry-run、提取、整合、取消、软删除和清空请求。
- `frontend/src/pages/MemoryPage.tsx`：三步灰度开关、手动任务、Memory/Evidence/Job/注入记录管理。
- `frontend/src/app/AgentShell.tsx`、`frontend/src/components/AppSidebar.tsx`：把 Memory 页面接入导航。
- `frontend/src/pages/MemoryPage.test.tsx`：验证开关依赖和强确认清空。

## 30. 建议的实际阅读方法

1. 先读本文件第 1、4、5、6、7、8、10、13 章，掌握数据和主链路。
2. 再读 `test_memory_storage.py`、`test_memory_phase1.py`、`test_memory_phase2.py`，把每条约束和实现对应起来。
3. 最后读 `storage/memory.py`，重点看事务、幂等、迁移和投影，不要先从 1500 行头读到底。
4. 读 `requests.py` 时重点观察 Memory 的插入位置：它永远低于 AGENTS 和 Skill。
5. 读 `scheduler.py` 时重点观察两个边界：手动任务只依赖 `generate_memories`，自动扫描额外依赖 `automatic_memory_enabled`。

## 31. 手动测试入口

推荐使用完整生产链路：

```text
打开 Memory 页面
→ 开启“允许生成 Memory”
→ 创建包含至少两条用户消息的会话
→ 点击“手动提取”
→ 等待 extract 和 consolidate 成功
→ 查看 Memory/Evidence
→ 开启“读取并注入 Memory”
→ 新会话询问相关内容
→ 查看实际注入记录
```

当前没有直接输入任意 Memory 的页面/API。直接调用 `MemoryStore.create_item()` 只适合开发测试，而且必须同时写入经过脱敏的 `MemoryEvidence`，否则会绕过正常的生产链路和证据约束。

