# 模块 3 与模块 4 实施计划

> 本计划基于 `main` 分支（`2b24c67` feat: add job registry, scope hierarchy, and lane scheduling）在独立 worktree（`.worktrees/jobs-modules-3-4`，分支 `feat/jobs-modules-3-4`）中执行。

## 总体目标

模块 3 解决“Job 如何实际运行和停止”；模块 4 解决“Job 如何进入 Agent Runtime 的生命周期和事件体系”。

两模块均以前置模块 1 的 Job 核心模型、模块 2 的 Registry/Scope/调度能力为基础。**本阶段只实现通用载体与生命周期承载，不迁移具体业务功能**；命令、MCP、Chat、Subagent、Sync 和 Snapshot 的业务迁移另行规划。

## 模块 3：执行载体与进程树控制

### 1. 现状确认

当前项目中存在三类需要统一管理的执行载体：

- `backend/src/tools/command.py` 直接调用 `subprocess.Popen`，并自行处理 Windows/POSIX 进程树终止。
- `backend/src/mcp/client.py` 自行创建事件循环和后台线程，并通过第三方 `stdio_client` 启动外部 MCP 进程。
- `backend/src/sync/client.py`、`backend/src/runtime/subagents.py` 和快照模块使用裸线程或线程池。

模块 3 先提供通用载体，**不直接改动**这些业务模块。

### 2. 现状要点（实现时的参考接口）

- `Job` 基类（`backend/src/jobs/base.py`）：`start/cancel/wait/close/info/add_listener/remove_listener`；适配器重写 `start()`（先调用 `super().start()`）并实现 `_request_cancel()`；用 `_mark_running/_mark_succeeded/_mark_failed/_mark_cancelled/_set_process_info` 报告进度；`JobInfo.error` 只保存 `ErrorFormatter` 生成的文本。
- `JobRegistry`（`backend/src/jobs/registry.py`）：`register/start/submit/get/list/unregister/close_all`，Scope 绑定 API 在 `JobScope` 上（`child/register/start/submit/get/list/cancel/close`）。注册要求 job 为 `pending`；`JobScopeKind`：SYSTEM/USER/RUNNER/RUN/TASK。
- 进程树终止参考实现：`backend/src/tools/command.py` 的 `WorkspaceCommand._terminate_process_tree`（Windows `taskkill /PID <pid> /T /F`、POSIX `os.killpg(pid, SIGKILL)`、兜底 `process.kill()`）与 `_format_output`（stdout:/stderr: 标签、20 000 字符按流预算截断）。
- 输出与消息格式兼容边界：`WorkspaceCommand.run` 的三类用户可见文本 —— `"Command timed out after {N} seconds."`、`"Command exited with code {N}."`、以及 `stdout:`/`stderr:` 分节输出。
- 红action 管线：`backend/src/runtime/persistence/recording.py` 的 `persistent_event` / `_redact_text`（用于模块 4 事件转审计）。

## 模块 4：Runtime 生命周期与事件桥接

### 1. 现状确认

当前 Runtime 相关生命周期分布在：

- `AgentRunner._resources` 和 `close()`（`backend/src/runtime/execution/runner.py`）。
- `AgentApplication.close()`（`backend/src/runtime/application/services.py`）。
- `WebAppState.close()`（`backend/src/api/state.py`）。
- Web chat 路由中直接创建的 worker 线程（`backend/src/api/chat/routes.py`，`threading.Thread(target=worker, daemon=True).start()`）。
- `RuntimeServices`（`backend/src/runtime/core/context.py`，非序列化依赖包）中的事件、取消和工具调用上下文。
- `RunEventPublisher`、JSONL 和运行时持久化事件链路（`backend/src/runtime/execution/lifecycle/publisher.py`、`backend/src/runtime/persistence/recording.py`）。

模块 4 的重点是统一这些边界，而不是在一个地方重复保存资源引用。

### 2. 交付目标（模块 3 + 模块 4 完成后系统应具备）

- 统一载体接口（ProcessGroup / SubprocessJob / ThreadJob / ServiceJob）。
- 统一 Scope 关闭入口（Runner 只管自己的 Runner Scope；WebAppState 关闭先关系统 Job Scope）。
- 跨线程安全的 Job 事件通道。
- 不影响现有业务行为的兼容层。

## 全局约束（Global Constraints，逐字约束所有任务）

1. 不迁移 `WorkspaceCommand`；不重写 MCP client；不迁移 Web chat worker；不迁移 Subagent、SyncCoordinator 或 SnapshotManager；不新增 `/api/jobs`；不修改前端展示；不引入新的持久化表或同步数据结构。
2. 不引入 `psutil` 或任何新的系统级依赖（新增依赖需要人工批准）。
3. `backend.jobs` 保持中立：顶层 `import backend.jobs` 不加载 runtime、tools、MCP、storage 或第三方模块。
4. `JobInfo.error` 只保存 `ErrorFormatter` 生成的文本，任何路径（包括新载体）不得把环境值、凭据、命令行或原始异常写入 `JobInfo.error` 或事件。
5. Scope、Registry、线程对象、Job 内部句柄绝不进入 `RuntimeState`、SQLite、日志快照或云同步数据。
6. Job 事件不参与 Runtime checkpoint 序列化，不进模型上下文；可以进入前端事件流和审计记录。
7. 所有关闭路径保持幂等，保留现有资源去重行为（`id(resource)` 去重、`_closed` 标记）。
8. 线程保持 daemon 的现有安全边界，未能及时结束的线程必须被记录（日志）。
9. 事件监听器不得在跨线程路径直接修改 `RunEventPublisher` 或 `RuntimeState`。
10. 现有命令、MCP、同步和子 Agent 测试必须继续通过；业务模块（tools/mcp/sync/runtime/subagents 的既有逻辑）不得被本计划改动（除计划明确列出的 Runner/Application/WebAppState/RuntimeServices 接入点）。
11. 测试遵循仓库约定：`tests/test_*.py`、`test_<behavior>`；mock HTTP/provider；绝不调用付费模型 API；输出必须干净（pristine）。
12. 代码规范：四空格缩进、公开 API 类型标注、snake_case、PascalCase 类名；每个文件一个清晰职责（约 300 行为指导线）。

## 实施任务

### Task 1: ProcessGroup — 跨平台进程组抽象

**目标文件（新建）**：`backend/src/jobs/process_group.py`；导出加入 `backend/src/jobs/__init__.py`。测试：`tests/test_job_process_group.py`（或并入 `tests/test_jobs_carriers.py`，由实现者决定并在报告中说明）。

**需求**：

1. 定义跨平台进程组边界：统一进程启动、轮询、等待、终止和 PID 查询。`subprocess.Popen` 的创建细节、环境、工作目录、命令行参数全部由调用方显式传入（方法签名显式携带，不读取进程环境）。禁止隐式继承 `os.environ`。
2. 平台行为：
   - Windows：创建时 `CREATE_NEW_PROCESS_GROUP`；终止时先 `taskkill /PID <pid> /T /F`（带 5 秒超时，吞掉 OSError/TimeoutExpired），再对根进程兜底 `kill()`。
   - POSIX：创建时 `start_new_session=True`；终止时 `os.killpg(pid, SIGKILL)`（吞掉 ProcessLookupError/OSError），再兜底 `kill()`。
   - 参考现有实现 `backend/src/tools/command.py` 的 `_terminate_process_tree`，但提炼为可复用抽象。
3. 终止语义：显式终止超时（如 5 秒）；进程已经退出（`poll() is not None`）时终止调用须幂等返回而不抛错；终止超时后备选策略（标记为无法确认终止 + 报告已收集的 PID）。
4. 测试性：进程工厂（`popen_factory`）与树终止器（`tree_terminator`）可注入替换；`is_windows` 可注入。保持与 `WorkspaceCommand` 相同的注入风格。
5. 线程安全：同一 ProcessGroup 实例可被取消/关闭线程与监控线程并发使用；终止与轮询不得互相破坏状态。
6. 禁止 `psutil`；不读取/记录环境变量内容。
7. 为 Task 2 提供接口：`SubprocessJob` 将通过此抽象启动、轮询、等待、终止进程树，且不直接操作 `Popen`。

**验收标准**：

- 单元测试覆盖：启动成功并拿到 PID、等待返回码、正常退出、非零退出、终止整个进程树（真实子进程 + 孙进程；Windows 分支真实执行 `taskkill`，POSIX 分支用 `skipif` 保留）、进程已退出时终止幂等、注入工厂模拟启动失败（FileNotFoundError/OSError）、注入终止器模拟终止器超时。
- 不改变 `tools/command.py` 的任何行为（本任务不修改该文件）。

### Task 2: SubprocessJob — 一次性进程 Job

**目标文件（新建）**：`backend/src/jobs/subprocess_job.py`；可能伴随 `backend/src/jobs/output.py`（输出格式化助手）；导出加入 `__init__.py`。测试：`tests/test_job_subprocess.py`。

**需求**：

1. `SubprocessJob(Job)`，`kind=JobKind.SUBPROCESS`。构造参数显式接收：`job_id`、`argv`（`Sequence[str]`）、`env`（显式 `Mapping[str, str]`，不隐式继承）、`cwd`、`timeout_seconds`、输出上限等；基于 Task 1 的 `ProcessGroup` 启动。
2. 启动后由 Job 自己监控进程完成（内部 daemon 监控线程等待进程 + 超时），统一处理：
   - 启动失败（工厂抛 FileNotFoundError/OSError）→ `_mark_failed`（经 `ErrorFormatter`，消息格式与 `WorkspaceCommand` 兼容，见下）。
   - 正常退出（exit 0）→ `_mark_succeeded(exit_code)`。
   - 非零退出 → `_mark_failed(exit_code=...)`（如果是退出码，Job 仍可提供输出文本）。
   - 超时 → 终止整个进程树 → `_mark_failed`（`"Command timed out after {N} seconds."` 兼容文本，经消息透传 formatter）。
   - 主动取消 → `_request_cancel` 终止整个进程树 → `_mark_cancelled`。
3. 输出兼容边界（不迁移 `WorkspaceCommand`，只提供能力）：在 `backend/src/jobs/output.py` 提供与 `tools/command.py._format_output` 相同的格式化/截断算法（stdout:/stderr: 分节、20 000 字符按流预算截断 + omitted 标记），从 jobs 包独立实现（jobs 不得 import tools）。`SubprocessJob` 暴露截断后的输出快照（如 `output` 属性或输出通道），并保持 `"Command exited with code {N}."` 这类错误消息的文本形状。
4. 错误信息：`JobInfo.error` 只保存 formatter 输出。默认 `ClassNameErrorFormatter` 不变；本任务提供一个可注入的“消息透传”format（如 jobs 包内的 `MessageErrorFormatter`，仅允许出现在进程结果消息上，不暴露环境/凭据/命令行），用于与 `WorkspaceCommand` 消息文本对齐的测试。
5. 生命周期幂等：`start/wait/cancel/close` 重复调用安全；`close(timeout)` 幂等；终止后监控线程必须退出（join），不得留下监控线程或根进程。
6. 与 Registry 配合：`close` 后 Job 进入终态即释放 Registry 槽位（registry 的 listener 机制负责）；测试中通过 `JobRegistry`/`JobScope` 注册验证槽位释放与状态快照。

**验收标准**：

- 测试覆盖：启动失败、正常退出、非零退出、超时（真实 timeout 场景用小超时）、取消（含取消后进程树终止）、重复 close、进程已退出后 cancel、监控线程无泄漏（线程计数断言）、输出截断算法与 `tools/command.py` 对相同样本输出一致。
- `tests/test_jobs.py`、`tests/test_job_registry.py` 等既有测试不受影响。

### Task 3: ThreadJob — 线程 Job

**目标文件（新建）**：`backend/src/jobs/thread_job.py`；导出加入 `__init__.py`。测试：`tests/test_job_thread.py`。

**需求**：

1. `ThreadJob(Job)`，`kind=JobKind.THREAD`：在一个 daemon 线程上运行可调用目标（`target`、`args`、`kwargs`）。
2. 协作取消：不尝试强杀 Python 线程。Job 内持有一个取消事件；`_request_cancel()` 置位事件；目标代码通过注入的取消检查（如 `cancel_event`/`is_cancelled()` 回调）协作退出。
3. 结果区分：
   - 正常完成 → `_mark_succeeded`。
   - 目标抛异常 → `_mark_failed`（经 `ErrorFormatter`）。
   - 收到取消（已置位且目标返回）→ `_mark_cancelled`。
   - 关闭超时：`close(timeout)` 内目标未退出 → 记录状态与诊断（日志记录 job id、线程名/ident，明确线程未能在超时内结束），Job 保持非终态；Registry 行为：scope close 会把它计入 `timed_out` 报告（registry 现有逻辑），本任务只需确保 ThreadJob 在超时后仍可再次 close/cancel。
4. 线程退出后一定释放 Registry 资源（终态 → listener 释放槽位；测试通过 registry 验证）。
5. 记录未能及时结束的线程：必须打日志（`logging`，包含 job id），供运维可见。
6. 为后续包装 Web worker、Subagent worker、SyncCoordinator、Snapshot worker 提供通用语义；保持 daemon 安全边界。
7. 生命周期幂等：`start/wait/cancel/close` 安全重复；`close()` 无泄漏线程句柄。

**验收标准**：

- 测试覆盖：正常完成、异常退出、协作取消（目标响应事件）、取消后目标立即返回、忽略取消导致 close 超时（断言日志记录 + Job 状态非终态 + registry `timed_out` 行为）、重复 close、daemon 属性断言。

### Task 4: ServiceJob — 长驻服务适配层

**目标文件（新建）**：`backend/src/jobs/service_job.py`；导出加入 `__init__.py`。测试：`tests/test_job_service.py`。

**需求**：

1. `ServiceJob(Job)`，`kind=JobKind.SERVICE`，与具体协议无关的长驻服务适配层。为后续 MCP 接入提供驱动接口，但**本模块不实现任何 MCP 协议**。
2. 驱动接口（`ServiceDriver` Protocol，最小面）：`start() -> handle`（返回服务实例句柄）、`check(handle) -> bool`（健康探针）、`stop(handle)`（关闭当前实例）。驱动实例由调用方构造注入（测试用假驱动）。
3. 生命周期：
   - 启动：`start()` 后进入初始化，初始化超时（可配置）内健康探针未通过 → 启动失败 → `_mark_failed`。
   - 运行：健康状态与 Job 主状态分离 —— Job 保持 `running` 期间，健康状态在 `healthy / degraded / down` 之间变化；健康变化通过 Job 状态通知的 reason（如 `"service_degraded"` / `"service_recovered"`）对外发布，不改变主状态。
   - 连续失败（探针连续失败达到阈值）→ 进入降级；降级后可触发重建（重启实例）：重建 = 停止旧实例（独立关闭边界 + 资源句柄释放）→ 启动新实例 → 重新初始化。
   - 恢复：重建后探针通过 → 回到 healthy。
   - 重建耗尽：达到最大重建次数仍不健康 → `_mark_failed`（主状态失败）。
   - 主动取消/关闭：`_request_cancel` 停止当前实例；`close(timeout)` 停止实例并等待。每个服务实例拥有独立的关闭边界和资源句柄（不同代次实例句柄不同，且旧句柄在重建时关闭）。
4. 文档化约束：第三方 MCP stdio 启动器无法直接注入进程控制和最小环境，因此后续 MCP 模块需要单独实现受控 transport——在模块 docstring 中明确写出这一边界（不实现）。
5. 生命周期幂等：重复 start/close/cancel 安全；重建过程中收到取消不会泄漏旧实例。

**验收标准**：

- 测试覆盖（均用假驱动）：初始化失败（超时/探针失败）、正常启动、探针降级 → 恢复、连续失败 → 重建（断言旧句柄被关闭）、重建耗尽 → 最终失败、取消关闭当前实例、重建期间取消无泄漏、健康 event reason 序列。

### Task 5: 模块 3 边界与验证测试

**目标文件**：`tests/test_job_carriers_integration.py`（或并入既有 carrier 测试文件，由实现者决定并说明）。

**需求**：

1. Windows 真实进程树终止测试：启动一个会再派生孙进程的命令（PowerShell `Start-Process` 或 `cmd /c start`），终止后断言根进程与孙进程都已退出（`os.kill(pid, 0)` 或 `tasklist` 检查），并在成功/失败时充分清理；POSIX 分支用 `skipif`（或实现 `killpg` 等价真实测试，视平台）。
2. 边界矩阵（尽量真实、少量假件）：进程启动失败、超时、取消、非零退出、重复关闭、进程不存在、权限失败、终止器超时。
3. 安全错误处理：构造场景证明 `JobInfo.error` 与失败事件不包含环境值/凭据/命令行文本（用携带敏感内容的错例 + formatter 断言）。
4. 回归验证：确认 `tests/test_jobs.py`、`tests/test_job_registry.py`、命令工具相关测试（`tests/test_tools.py` 中命令用例，若存在）仍通过；`git diff` 确认未修改 `backend/src/tools/command.py`、`backend/src/mcp/`、`backend/src/sync/`、`backend/src/runtime/subagents.py`。

**验收标准**：上述测试全部通过且输出干净；验证记录（命令 + 摘要）写入报告。

### Task 6: Registry/Scope 接入 — WebAppState 系统 Registry 与 Scope 层级

**目标文件**：`backend/src/api/state.py`、`backend/src/runtime/execution/runner.py`、`backend/src/runtime/application/services.py`、`backend/src/runtime/application/factory.py`（局部）、`tests/test_job_scope_runtime.py`（新建）。

**需求**：

1. 明确的生命周期层级：
   - `WebAppState` 持有系统级 `JobRegistry`（`self.job_registry = JobRegistry(...)`，在 `__init__` 创建）。
   - 用户运行使用用户 Scope；`WebAppState` 提供按 `user_id` 获取/缓存用户 Scope 的方法（如 `user_job_scope(user_id) -> JobScope`，`JobScopeKind.USER`，owner.user_id）。
   - 每个 `AgentRunner` 使用 Runner Scope：`AgentRunner` 构造新增可选参数（如 `job_registry` 与 `parent_scope`，或 `job_scope` 直接注入）；Runner 创建并持有自己的 `JobScopeKind.RUNNER` scope。
   - 每次 Agent Run 使用临时 `JobScopeKind.RUN` scope（由 Runner 在 `new_runtime`/`run` 时创建，run 结束时关闭；与 RuntimeState 分离，仅控制面使用）。
   - 子 Agent 与后续后台任务使用 `TASK` scope（本阶段只提供创建能力与文档，不接入 SubagentCoordinator）。
2. 处理 Web 共享 Registry 与本地独立 Registry 的差异：本地 `build_application`/`build_runner` 创建独立 `JobRegistry`；Web 路径把共享 registry 传给各 Runner。`AgentApplication` 增加字段（如 `job_registry: JobRegistry | None`、`owns_registry: bool`）。
3. Runner 只关闭自身 Scope，**不能关闭共享系统 Registry**：`Runner.close()` 关闭 Runner scope 及后代（不调用 `registry.close_all()`）；只有 Owner（Application/WebAppState）才能 close registry。
4. `bind`/`resume` 重建非序列化 Scope：绑定流程把 Runner scope 与当前 run scope 重新接回 `RuntimeServices`，不改变持久化 `RuntimeState`。
5. Application 构建失败时关闭已经创建的 Scope（`build_application`/`_build_subagent_runner` 的异常路径）。
6. Scope 层级不能绕过用户配额或跨用户访问 Job：测试证明用户 scope 下的 runner scope/run scope 受到的配额（`_can_admit_locked` 的用户级上限）与直接用户 scope 一致；`get/list/cancel` 越出所属 scope 树返回不可见（registry 现有 `_scope_contains_locked` 语义）。

**验收标准**：

- 单元测试（可注入假 registry/scope）：WebAppState 持有 registry；用户 scope 按 user 隔离；runner scope 创建与持有；run scope 生命周期（run 结束即关闭）；Runner.close 不关共享 registry；跨用户 scope 访问被拒；构建失败路径清理已建 scope。

### Task 7: Runner 与 Application 生命周期改造

**目标文件**：`backend/src/runtime/execution/runner.py`、`backend/src/runtime/application/services.py`、`backend/src/api/state.py`；`tests/test_lifecycle_jobs.py`（新建）。

**需求**：

1. `AgentRunner`：
   - 创建并持有 Runner Scope（Task 6 的产物），`close()` 时**先停止 Scope 下的 Job**（`job_scope.close(timeout)`），再关闭尚未迁移的 legacy resources（现有 `_resources` 逆序关闭逻辑保留）。
   - 保持幂等（`_closed` 标记）与现有资源去重行为；`_resources` 关闭顺序兼容现有测试。
2. `AgentApplication`：
   - 明确自己是否拥有根 Registry（`owns_registry` 字段 + 文档）；`close()` 先 `runner.close()`（内部先关 scope 再关 legacy），若 `owns_registry` 再 `registry.close_all()`。
   - 保持现有资源清理兼容：一个资源失败不得跳过其他资源（try/finally 结构，现有实现已满足，需测试锁定）。
3. `WebAppState`：
   - `close()` 顺序：先关闭系统 Job Scope（`job_registry.close_all(...)`），再关闭 SnapshotManager、settings、auth、cloud、project stores（现有顺序与去重保留）。
   - 所有关闭路径幂等。
4. 测试：多个 Runner 之间 Scope 隔离；关闭一个 Runner 不影响其他 Runner 与共享 registry；WebAppState 关闭能够停止所有后代 Job（注册一组分布在用户/runner/run scope 的 job，close 后全部终态）；关闭中途某个资源抛错仍完成其余清理（注入抛错资源）；重复 close 安全。

**验收标准**：上述场景测试通过；既有 `test_runner.py`、`test_web.py` 等不受影响。

### Task 8: RuntimeServices 与工具上下文 Job 接入

**目标文件**：`backend/src/runtime/core/context.py`、`backend/src/runtime/execution/runner.py`（new_runtime/bind 接线）、新助手（如 `backend/src/runtime/jobs.py` 提供上下文访问器）；`tests/test_runtime_job_context.py`（新建）。

**需求**：

1. `RuntimeServices` 增加非序列化字段：`job_scope: JobScope | None = None`、`job_events: object | None = None`（事件通道，Task 9 类型）。不得写入 `RuntimeState`。
2. Runner 在 `new_runtime`/`bind` 时把 Runner scope（及 run scope，若已创建）注入 `RuntimeServices`；run scope 每次 run 创建并在 run 结束前关闭。
3. 工具调用上下文能够取得当前 Run Scope：提供安全访问器（如 `backend/src/runtime/jobs.py` 中 `current_job_scope(runtime)` 或 `runtime.services.job_scope` 直读），命令工具和其他载体可通过上下文向 Registry 注册 Job（`submit/register`），**无需导入具体 Runtime 实现**——提供绑定当前 scope 的最小封装（如 `JobContext.submit(job, lane=..., admission=...)`，默认 lane/admission 可配）。
4. Scope 及其内容绝不进入 `RuntimeState`/SQLite/日志快照/云同步：测试序列化 `RuntimeState`（`to_dict`/JSON）后断言不含 scope/registry/线程/Job 句柄字段。
5. 旧工具调用与 RuntimeRunner 用法保持兼容：`bind`/`resume`/`new_runtime` 现有签名不变，job 字段全部可选默认 `None`。

**验收标准**：访问器可用 + 默认缺省兼容 + 序列化不泄漏测试通过；既有 `tests/test_runtime_context.py` 等不受影响。

### Task 9: 跨线程 Job 事件队列

**目标文件（新建）**：`backend/src/runtime/job_events.py`；`tests/test_job_event_queue.py`（新建）。

**需求**：

1. 有界、线程安全的 Job 事件队列：`JobEventQueue`（maxlen 可配，默认如 1000；内部锁 + 条件变量；`put_nowait`/`get_nowait`/`drain`/`close`/`pending_count`/`dropped_count`）。
2. 生产者：`JobStateListener` 适配器（如 `JobEventProducer`）把 Job 状态变化放入队列。**队列满时不阻塞 Job 取消和关闭**：生产者用 `put_nowait`，满则丢弃（计数），绝不抛给 Job/registry 调用方。
3. 消费者：由 Runtime 所属线程在**模型边界、工具边界和 Run 结束前**消费事件（接线点：`AgentRunner._run_attempt`/执行工作流边界，具体位置由实现者结合 `execution/workflows` 代码确定；消费回调注入，默认 no-op）。
4. 前端展示 sink：线程安全的 `JobEventSink` Protocol（`emit(event)`）+ 实现（如 `CallbackJobEventSink`/`QueuedJobEventSink`），可接入后续 SSE 通道，本阶段不接前端。
5. 语义：事件丢失只影响展示/审计副本，Registry 状态仍是权威来源（docstring 明确）。
6. 竞态处理：Job 完成与 Runtime 关闭同时发生——`drain` 后 `close()`；关闭后再 put 被拒绝或安全丢弃；不抛跨线程异常。
7. 事件对象：最小不可变 dataclass（如 `JobEvent`：`job_id, job_kind, lane, state, reason, session_id, run_id`），任何字段不得含环境/凭据/命令行；生产端已经过 `ErrorFormatter`（error 文本可选携带）。

**验收标准**：队列容量/丢弃/多线程读写/drain/close 竞态测试通过；不阻塞取消的测试（慢消费者 + 满队列 + cancel 快速返回）。

### Task 10: Job 生命周期 RuntimeEvent 桥接

**目标文件**：`backend/src/runtime/jobs.py`（桥接器，含 Task 8 的上下文访问器）或独立模块；`backend/src/runtime/execution/lifecycle/publisher.py` 不动（除非最小接入）；`tests/test_job_event_bridge.py`（新建）。

**需求**：

1. 统一 Job 生命周期事件：排队（pending 入队/或注册）、启动、成功、失败、取消、服务降级（及恢复）。
2. 桥接规则：
   - Job 状态变化（`JobStateChange`）转换为安全的 `RuntimeEvent`（kind 前缀如 `job_`：`job_queued/job_started/job_succeeded/job_failed/job_cancelled/job_degraded/job_recovered`），data 携带 job_id、job_kind、lane、state、session_id/run_id（来自 scope owner；**不携带 user_id 等内部 owner 信息**）。
   - 事件不携带命令行、环境值、原始异常或内部 owner 信息；错误文本经现有 redaction 管线（`persistent_event`/`_redact_text` 所在模块的公开函数）。
   - Job 事件**不参与 Runtime checkpoint**（不在 checkpoint record 内；不改变 `RuntimeState` 序列化）。
   - Job 事件可以进入前端事件流和审计记录（调用方通过 `runtime.services.on_event`/审计通道转发），**不能进入模型上下文**（不得加入 planner 消息）。
   - 监听器不直接跨线程修改 `RunEventPublisher` 或 `RuntimeState`：桥接器只产出事件对象交给 Task 9 的队列；消费线程负责转发。
3. 接线：Runner 在 `_run_attempt` 绑定 Job 事件消费（Run 结束前消费并转发审计/前端，若配置）；registry 或 scope close 时 job 终态事件仍能发出（队列 producer 在 registry listener 上）。
4. 测试：状态→事件映射（全状态）、redaction（含敏感文本的 error → 事件 redacted）、不参与 checkpoint、不进模型上下文（断言模型消息列表不含 job 事件）、跨线程环境下事件一致到达（配合 Task 9）。

**验收标准**：桥接测试通过 + redaction 测试 + checkpoint 排除测试 + 模型上下文排除测试。

### Task 11: 模块 4 集成与回归验证

**目标文件**：`tests/test_job_runtime_integration.py`（新建）；必要时少量调整前序任务文件。

**需求**（逐条对应计划的“模块 4 验证计划”）：

1. 本地 Application 创建独立 Registry；Web Application 使用共享 Registry（测试两种组合）。
2. 多个 Runner 之间的 Scope 隔离；Runner 关闭不影响其他 Runner。
3. WebAppState 关闭能够停止所有后代 Job。
4. 构建失败和关闭失败时仍完成其余资源清理。
5. `RuntimeState` 序列化中不出现 Scope、Registry、线程对象或 Job 内部句柄。
6. Job 事件经过 redaction 后可进入审计和前端，但不会进入模型请求。
7. 跨线程事件不会破坏 `RuntimeEventPublisher`、JSONL 或运行时持久化（跑相关既有持久化测试）。
8. 既有 Runner、Application、MCP ownership、sync shutdown 测试继续通过（`tests/test_runner.py`、`tests/test_web*.py`、`tests/test_external_mcp.py`、`tests/test_sync_*.py` 等）。

**验收标准**：所有上述测试通过；报告列出运行的测试文件与结果。

### Task 12: 最终验证

**需求**：

1. 在 worktree 内执行完整 `python -m pytest -q`，全部通过（或仅记录并说明环境性 skip）。
2. `python -m ruff check .` 与 `python -m ruff format --check .` 全部通过（仅针对本分支变更的违规需修复；仓库既有问题仅在涉及改动文件时处理）。
3. Windows 真实进程树终止测试执行并通过（Task 5 产物）。
4. 确认交付边界：`git diff --stat main...HEAD` 显示未触碰 `backend/src/tools/command.py`、`backend/src/mcp/`、`backend/src/sync/`（除计划允许）、`backend/src/runtime/subagents.py`、`frontend/`、不存在新持久化表/迁移。
5. 汇总：新载体接口、统一 Scope 关闭入口、跨线程 Job 事件通道三项交付物逐一说明。

**验收标准**：完整 pytest 通过 + ruff check/format 通过 + 边界确认清单齐全。