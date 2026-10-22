# Supervisor 进程托管（A/B 槽位真实切换）

> 目标：晋升不再只是改 `runtime/active_slot` 指针，而是真正启动候选进程、验证健康、
> 切换流量，失败时把旧版本重新拉起来并验证恢复结果。

## 1. 进程拓扑

```
                 ┌──────────────────────────────┐
                 │ Supervisor（独立 OS 进程）    │
                 │ python -m aiive.supervisor.daemon │
                 │  · 拥有应用进程               │
                 │  · 轮询健康 / 监测退出        │
                 │  · 写 state.json / events.jsonl│
                 └───────────┬──────────────────┘
                             │ spawn / stop（受控参数 + 记录表所有权）
                             ▼
                 ┌──────────────────────────────┐
                 │ 应用进程（槽位代码副本）      │
                 │ python -m uvicorn aiive.main:app │
                 │  · 只通过文件提交切换请求     │
                 └──────────────────────────────┘
```

- **Supervisor 不导入 FastAPI 应用**，应用重启/切槽不会带走它；它也不在应用进程的
  lifespan 里，应用崩溃不会影响它的循环。
- 应用侧（`supervisor/client.py`）只能写请求文件，不能直接启动/杀进程；进程控制
  仅存在于 Supervisor 进程内。
- 控制面目录 `runtime/supervisor/`（`AIIVE_SUPERVISOR_STATE_DIR` 可改）：

| 文件 | 作用 |
| --- | --- |
| `state.json` | 当前阶段、活动槽位、进程记录、观察窗截止时间 |
| `events.jsonl` | 只追加事件流（切换、健康、回滚、恢复的原因） |
| `requests/` | 应用提交的控制请求（promote / rollback / restart / status） |
| `results/` | 每个请求的结果（请求 ID 一一对应，幂等键可回查） |
| `processes.json` | 受管进程清单，即"进程所有权"的唯一凭据 |
| `supervisor.lock` / `switch.lock` | 单实例锁 / 切换临界区锁 |
| `logs/{slot}-{port}.log` | 被托管进程的 stdout/stderr |

## 2. 一次晋升的顺序

1. **应用侧闸门**：`ReleaseManager.promote(run_gate=True)` 先做槽位静态健康检查，再跑
   `evaluate_promotion_gate`（Kernel Contract + Replay Eval + 候选评测，fail-closed）；
   REST 入口在此之前还要求 `targeted_test_report.ok`。
2. **提交请求**：闸门通过后写 `requests/*.json`，载荷带 `gate_ok: true`，立即返回
   `status=switch_requested`（本进程随后会被替换，不能同步等结果）。
3. **守护进程复核**：拒绝没有 `gate_ok` 的请求（`gate_not_passed`），再对候选槽位做
   静态健康检查。
4. **暂存端口验证**：在 `port+1` 启动候选进程（`AIIVE_PROCESS_ROLE=candidate`），等待
   HTTP `/health` 200；进程提前退出立即判定 `candidate_start_failed`，不占用主端口。
   候选进程跳过 Alembic 迁移、调度器与 Outbox worker——它与旧版本同时在线，不能重复执行
   周期任务或并发迁移；它验证的是"这份代码能启动、能响应请求"。
5. **主端口切换**：停旧进程 → 主端口启动候选 → 再次健康检查。
6. **提交状态**：只有第 5 步通过才写 `active_slot`、`state.json`、`release_state.json`
   （status=monitoring，观察窗默认 600s）。
7. **失败恢复**：第 4/5 步失败 → 停候选 → 重启旧槽位 → **验证旧版本健康** →
   `active_slot` 保持旧值，结果里 `rollback.verified=true`。

## 3. 运行期监测与恢复

| 场景 | 行为 |
| --- | --- |
| 观察窗内进程退出 | 记录 `app_exited_unexpectedly` → 回滚到上一槽位并验证 |
| 观察窗内健康检查失败 | 同上（`monitor_health_failed`） |
| 观察窗外进程退出 | 在 `max_restarts`/`restart_window` 预算内重启同槽位；预算耗尽 → 回滚 |
| 回滚也失败 | 进入 `degraded`，停止重试并保留最后错误，不做静默循环 |
| Supervisor 重启 | 读 state.json：进程存活且健康 → 接管（adopt）；否则清理后重启活动槽位 |
| 上次停在 `switching` | 判定为中断切换：停候选与残留进程，恢复 previous 槽位并验证 |

一致性以"真实进程"为准：接管时若进程槽位与 `active_slot` 不一致，以进程为准回写指针
并记录 `reconciled_active_slot`。

## 4. 并发与幂等

- `supervisor.lock`（O_EXCL + pid 存活检测）保证同机只有一个 Supervisor；
  陈旧锁按 pid 死亡 + 超时回收。
- `switch.lock` 保证同一时刻只有一个切换；拿不到锁的请求立即返回
  `switch_in_progress`，不排队堆积。
- 请求可携带 `idempotency_key`：命中历史结果时直接复用并标记 `deduplicated`，
  不会重复启动进程。
- 目标槽位已是活动槽位且健康 → `already_active`，不重启进程。

## 5. 安全边界

- **进程所有权**：只操作 `processes.json` 里登记过的 pid；从不按进程名/端口扫描杀进程；
  未登记 pid 返回 `process_not_owned`。
- **受控参数**：启动命令来自配置（`AIIVE_SUPERVISOR_APP_COMMAND` 模板或默认 uvicorn），
  不经 LLM 生成，也不使用 Agent 的 `run_shell` 工具。
- **闸门不可绕过**：文件协议只在本机同用户可写；即便如此，没有 `gate_ok` 的 promote
  请求一律拒绝。
- **候选不产生副作用**：`AIIVE_PROCESS_ROLE=candidate` 的进程只加载应用与 `/health`，
  不跑迁移、不启调度/Outbox、不注册 MCP 能力，避免与在运行的旧版本互相干扰。
- **数据隔离**：切换只动槽位代码副本；`data/`、`postgres/`、`object_store/`、`logs/`、
  `.env`、`slots/`、`runtime/` 都在 manifest excludes 中，版本切换不会覆盖用户数据。
- **进程退出语义**：POSIX 用进程组 SIGTERM→SIGKILL；Windows 对自有子进程用
  TerminateProcess 快路径，接管来的进程用 `taskkill /T /F`（保证子树一起结束）。

## 6. 部署与启用

- 原生部署（非容器）：
  1. `AIIVE_SUPERVISOR_ENABLED=true`；
  2. 用 `python -m aiive.supervisor.daemon` 代替直接 `uvicorn` 启动应用；
  3. 应用与 Supervisor 使用同一 `runtime/` 目录（同机同用户）。
- 容器部署（`docker compose`）：容器生命周期由编排负责，`AIIVE_SUPERVISOR_ENABLED`
  保持 false；此时 `ReleaseManager.promote` 退回"只切指针"的兼容行为，并在返回体
  `status` 上不出现 `switch_requested`。
- 运维接口：`GET /api/selfdev/supervisor`（只读：守护进程存活、阶段、受管进程、最近事件）；
  `python -m aiive.supervisor.daemon --status` 等价。

## 7. 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `AIIVE_SUPERVISOR_ENABLED` | false | 是否启用守护进程托管 |
| `AIIVE_SUPERVISOR_HOST` / `_PORT` | 127.0.0.1 / 8000 | 被托管应用监听地址 |
| `AIIVE_SUPERVISOR_HEALTH_PATH` | /health | 健康端点 |
| `AIIVE_SUPERVISOR_APP_MODULE` | aiive.main:app | uvicorn 导入串 |
| `AIIVE_SUPERVISOR_APP_COMMAND` | 空 | 覆盖启动命令（占位符 `{python} {host} {port} {slot} {slot_root} {app_module}`） |
| `AIIVE_SUPERVISOR_START_TIMEOUT_SECONDS` | 90 | 候选启动到健康的最长等待 |
| `AIIVE_SUPERVISOR_STOP_TIMEOUT_SECONDS` | 15 | 停止优雅等待上限 |
| `AIIVE_SUPERVISOR_POLL_SECONDS` | 2 | 守护循环间隔 |
| `AIIVE_SUPERVISOR_MAX_RESTARTS` / `_RESTART_WINDOW_SECONDS` | 3 / 300 | 崩溃重启预算 |
| `AIIVE_SUPERVISOR_MONITOR_SECONDS` | 600 | 晋升观察窗 |
| `AIIVE_SUPERVISOR_STATE_DIR` | runtime/supervisor | 控制面目录 |

## 8. 验证

`tests/unit/backend/test_supervisor_lifecycle.py`（真实本地子进程，离线）：

- 进程启动/停止/所有权拒绝（`process_not_owned`）；
- 晋升成功：进程 pid 变化、`active_slot` 与 `release_state` 一致、事件完整；
- 候选启动失败 / 暂存端口不健康 → 旧版本继续运行且指针不变；
- 主端口健康失败 → 回滚到旧版本并 `verified=true`，旧 pid 已退出；
- 观察窗内异常退出 → `auto_rollback`，回到旧版本且健康；
- 无 `gate_ok` 的请求被拒绝；重复幂等键只切换一次；并发切换返回 `switch_in_progress`；
- Supervisor 重启接管存活进程；中断切换（`phase=switching`）恢复为旧版本。

## 9. 已知限制

1. 切换采用"停旧→启新"的单端口模型，存在秒级不可用窗口；双端口 + 反向代理的零停机
   切换不在本轮范围（需要额外的流量入口）。
2. 接管来的进程只能按 pid 判断存活，无法校验 pid 是否被系统复用；所有权凭据依赖
   `processes.json` 与本机单用户假设。
3. 容器部署不启用 Supervisor，晋升仍是指针切换 + 容器编排重启。
4. 候选进程在暂存端口验证通过后会被停止，再在主端口重启一次，启动成本约为两倍；
   候选模式不验证迁移与后台任务路径（这些只在主端口进程里生效）。
