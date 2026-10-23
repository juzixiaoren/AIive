# 能力生命周期（安装 → 验证 → 启用 → 停用 → 升级 → 回滚 → 卸载）

> 范围：MCP 动态能力（`capability_id` 形如 `mcp:<server>`）。内置工具随版本发布，
> 桌面节点能力由节点连接管理，二者不走本流程（`uninstall` 会显式拒绝）。

## 1. 为什么需要这一层

在本轮之前，安装、验证、启用是**同一条直线**：

`~
install_sandbox → state=sandbox → run_smoke(通过) → state=active → 注册进 ToolRegistry
`~

问题在于：

- `run_smoke` 把"验证通过"直接写成 `active`，**验证与启用不可区分**；
- 没有停用/启用开关，想临时下线只能卸载（丢产物、丢配置）；
- 没有升级/回滚，换版本只能重装，旧审批与旧注册无法判断是否还适用；
- 没有卸载，`.data/mcp_sandbox/` 与 DB 记录只增不减；
- 注册表（ToolRegistry）、数据库（`capabilities`）、文件系统（沙箱目录）三者
  没有一致性核查手段。

## 2. 状态机

`backend/aiive/capability/states.py` 是唯一裁决处，非法转换直接抛
`InvalidCapabilityTransition`。

| 状态 | 含义 | 发现索引 | 产物 |
|---|---|---|---|
| `candidate` | 目录候选，未安装（不落库） | 无 | 无 |
| `sandbox` | 安装成功，未验证 | 无 | 有 |
| `needs_review` | 验证失败 / 产物丢失，待修复 | 无 | 可能有 |
| `verified` | 验证通过，未启用 | 无 | 有 |
| `active` | 已注册且已启用 | **有** | 有 |
| `disabled` | 用户停用 | 无 | 有（保留） |

合法迁移：

`~
candidate    → （无；安装会新建 sandbox 行）
sandbox      → verified | needs_review | active（兼容既有"安装-冒烟直达"路径）
needs_review → verified | sandbox | active
verified     → active | needs_review | sandbox
active       → disabled | needs_review（产物丢失等故障态）
disabled     → active | verified
`~

同状态转换视为幂等空操作。

## 3. 四个事实分开记录

`Capability.definition["lifecycle"]` 记录（`_HISTORY_LIMIT=20` 条有界历史）：

| 字段 | 事实 |
|---|---|
| `installed_at` | 安装成功（由 installer 落库） |
| `verified_at` / `verified_fingerprint` / `last_verify_ok` | 验证通过 + 验证时的定义指纹 |
| `registered_at` / `registered_tools` | 注册成功（ToolRegistry 中真实存在的工具 id） |
| `enabled_at` / `disabled_at` | 正式启用 / 停用 |
| `history[]` | 每次动作的 from/to/ok/error/版本 |

`status()` 把四个事实分别算出来返回 `installed / verified / registered / enabled`，
不合并成一个"可用"布尔。

## 4. 各操作的语义与前置条件

| 操作 | 允许的起始状态 | 失败后的状态 |
|---|---|---|
| `verify` | sandbox / needs_review / verified / disabled（active 需先停用） | 保持原状态或 needs_review |
| `enable` | verified / disabled / active（active 为幂等重注册） | 保持原状态 + `last_error` |
| `disable` | active（disabled 幂等） | — |
| `upgrade` | 任意已安装状态 | 旧定义/注册/状态原样保留 |
| `rollback` | 有带定义快照的历史版本 | 失败停在 verified（不半新半旧） |
| `uninstall` | 任意（非 mcp: 前缀拒绝） | 拒绝时不删任何东西 |

### 启用

1. `launch` 必须存在；`launch.sandbox_path` 存在时校验目录仍在（丢失 → needs_review）；
2. 若 `verified_fingerprint` 存在且与当前定义指纹不一致 → `definition_changed`，
   必须重新验证（防止验证后被改定义直接启用）；
3. `activate_capability_tools` 注册真实工具；注册失败 → `registration_failed`，
   状态不变（不会出现"active 但没有工具"）；
4. 成功后写 `registered_at`、`registered_tools`、`enabled_at`。

### 停用

1. 只接受 `active`；
2. `unregister_capability_tools` 按 `registered_tools` + 缓存工具列表**精确**反注册，
   不做前缀清扫（`server-memory` 与 `server-memory-extra` 互为前缀会误删）；
3. 关闭该能力的 MCP 会话；`definition`（含 launch/env_keys/risk_verdict/trust_level）
   原样保留 —— 停用不重置用户配置；
4. **在途规则**：已进入 handler 的调用继续完成（不可中断）；尚未派发的 Action 在
   `CapabilityBroker.dispatch` 得到 `capability_disabled`（与"工具名写错"的
   `unknown_tool` 区分），任务按可恢复错误处理。

### 升级

1. 新版本装到版本化目录 `<sandbox>/<name>@<version>`，**不覆盖**旧目录；
2. 用独立会话键 `<capability_id>#staging` 真实 `tools/list` 验证；失败 → 删除
   staging 目录，旧定义/注册/状态完全不变（`rolled_back=false`，因为还没动过）；
3. 验证通过 → 快照旧版本定义进 `capability_versions.definition_snapshot`；
4. 原子切换：反注册旧工具 → 切定义与 `descriptor_hash` → 关闭旧会话 → 按新工具注册；
   注册失败 → 恢复旧定义、旧 `descriptor_hash`、旧状态并重新注册旧工具
   （`rolled_back=true`），删除 staging 目录；
5. 成功后写新的 `capability_versions` 行（version/descriptor_hash/tool_list_hash/
   source/state/sandbox_path/definition_snapshot）；
6. 用户配置（`risk_verdict`/`trust_level`/启动参数/env 键）从旧定义继承，
   除非调用方显式覆盖。

**旧版本引用策略**：升级后 `descriptor_hash` 变化，任何持有旧指纹的 Action 在
`dispatch` 阶段被 `descriptor_changed` 拒绝；这是"拒绝"而非"兼容"，避免用旧 Schema
解释新定义。

### 回滚

- 从 `capability_versions` 里取"版本 != 当前 且 有定义快照"的最近一条（或指定版本）；
- 校验快照完整（含 launch）与产物存在；
- 先按当前定义反注册，再切回快照定义、旧 `descriptor_hash`、旧状态并重新注册；
- 注册失败 → 停在 `verified`（确定状态），返回 `registration_failed`；
- 回滚本身也新增一条版本记录，保证历史可追溯。

### 卸载

1. 仅 `mcp:` 前缀可卸载；内置工具/桌面节点能力 → `capability_not_uninstallable`；
2. 占用检查：`agent_actions` 中未终结状态（planned/validated/awaiting_approval/
   ready/dispatched/running）引用该能力工具，或未终结 Task 的 `allowed_capabilities`
   引用 → `capability_in_use`，**不删任何东西**；`force=True` 才继续；
3. 清理顺序：反注册工具 → 关闭会话 → 删除 `mcp_install_records` /
   `capability_versions` / `capabilities` → commit → 删除沙箱目录；
4. 目录删除严格限定：必须在 `SANDBOX_ROOT` 之下，且目录名等于
   `sanitize(name)` 或以 `sanitize(name)@` 开头；越界/他人目录只记录 warning 并跳过。

## 5. 一致性与恢复

`audit()` 核对五类漂移：

| kind | 含义 | reconcile 处理 |
|---|---|---|
| `active_not_registered` | active 但工具不在注册表 | 重新注册 |
| `inactive_still_registered` | disabled/verified/needs_review 但仍有注册 | 反注册 |
| `missing_artifacts` | active 但沙箱目录丢失 | 反注册 + 置 needs_review |
| `orphan_registration` | 注册表有 `mcp_*` 工具但无对应能力行 | 反注册 |
| `version_mismatch` | 最新版本行与定义版本不一致 | 仅报告（需人工判断） |

`reconcile()` 只做注册索引与状态修复，**不动文件、不删记录**。

## 6. 并发与幂等

- 进程内：`_capability_lock(capability_id)` 可重入锁串行化同一能力的全部写操作；
- 数据库：`SELECT ... FOR UPDATE`（PostgreSQL 生效，SQLite 退化为普通读）；
- 幂等：`enable` 对 active、`disable` 对 disabled、`upgrade` 对同版本、
  `uninstall` 对不存在，都返回 `ok=True` + `changed=False` + `already_*`/`not_installed`。

## 7. 与安全边界的关系

生命周期只决定"能否被发现"，**不改变执行授权**：

- 发现：`CapabilityBroker.available_registry` / `search_capabilities`（unified，不收窄）；
- 执行：`CapabilityBroker.dispatch` → TaskPolicyEngine → 审批 → 资源锁 →
  `registry.execute_brokered`（校验 `descriptor_hash`）。

停用/卸载后能力从发现结果消失；定义变化后旧指纹失效。两者都不绕过上述链路。

## 8. API

`~
GET    /api/capabilities/lifecycle/audit
POST   /api/capabilities/lifecycle/audit/reconcile
GET    /api/capabilities/lifecycle/{capability_id:path}
POST   /api/capabilities/lifecycle/{capability_id:path}/verify
POST   /api/capabilities/lifecycle/{capability_id:path}/enable
POST   /api/capabilities/lifecycle/{capability_id:path}/disable
POST   /api/capabilities/lifecycle/{capability_id:path}/upgrade   {target_version, launch_args, env_keys}
POST   /api/capabilities/lifecycle/{capability_id:path}/rollback  {target_version}
DELETE /api/capabilities/lifecycle/{capability_id:path}?force=true
`~

`capability_id` 可省略 `mcp:` 前缀。

## 9. 测试

`tests/unit/backend/test_capability_lifecycle.py`（29 例，全离线）：状态机合法性、
安装≠验证≠注册≠启用、验证不启用、启用需验证、定义变更需重验、停用从发现索引消失
且保留配置、停用/启用幂等、停用后 dispatch 返回 `capability_disabled`、同能力写操作串行、
升级成功/验证失败/注册失败回滚/同版本幂等、回滚恢复定义与注册、卸载清理范围、
在途 Action 拒绝与 force、卸载幂等、非 MCP 拒绝、越界目录不删、审计与 reconcile、
历史有界。

## 10. 已知边界

- 跨进程并发仅依赖数据库行锁；SQLite 下退化为单写者语义（生产为 PostgreSQL）。
- 升级不自动清理旧版本目录（保留供回滚）；如需回收，按 `<name>@<version>` 目录人工清理。
- `candidate` 状态不落库（目录条目由 `mcp/discovery.py` 合成）。
