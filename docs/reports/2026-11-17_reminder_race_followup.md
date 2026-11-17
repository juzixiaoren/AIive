# Reminder 激活与收尾竞态补充审查

日期：2026-11-17。基线：main `60fad88`。本报告是前一轮 Reminder / 共享聊天契约收尾报告的补充。

## 问题确认

投递 Worker 的前置检查在模型调用前提交事务，不能保证模型随后执行 remind_alert 时 Task 仍可投递。原工具未锁定和检查 Task/Event，可能把取消后的 Event 改回 alerting；Worker 收尾也无条件覆写事件状态，可能覆盖模型执行期间用户写入的 confirmed 或 snoozed。

## 修复与修改文件

- `backend/aiive/tools/builtin_reminders.py`：先读取提醒身份，再按 Task→Event 顺序加行锁并 populate_existing 重新读取。关联 Task 缺失、非 reminder、cancelled/failed，或 Event dismissed / 非 pending、alerting 时拒绝激活；alerting 幂等返回 already_applied，不重复写入；仅可执行 Task 的 pending Event 可以推进至 alerting。
- `backend/aiive/runtime/task_manager.py`：取消时刷新锁定的 Task；按 ID 顺序刷新并锁定关联 Event，避免会话缓存及并发确认造成 payload 丢失。
- `backend/aiive/worker/outbox_handlers.py`：前置检查和收尾统一采用 Task→Outbox 锁序，与取消、入队一致。收尾按传入 Turn 的原始 reminder_id 锁定 Event，并重新读取。若事件已取消/隐藏，则剩余 dispatching Task 收敛为 cancelled；若事件已确认/延期或为其他不可激活状态，则收敛为 completed，保留 Event。上述路径不生成兜底通知、不再次写 alerting，也不广播旧 Turn 回复。正常 pending/alerting 路径沿用原投递及降级行为。
- `docs/CODE_MAP.md`：同步锁序和状态保护说明。

## 并发语义

取消先拿到 Task 锁：后续激活等待，读到 cancelled 后拒绝。激活先拿到 Task 锁：取消等待激活提交，随后以 cancelled 覆盖，最终不会回退至 alerting。确认/延期与 Worker 收尾通过同一 Event 行锁串行化；Worker 若后获得锁，会读到并保留最新事件状态。锁只覆盖短数据库事务，不跨模型调用持有。

## 验证与限制

按用户要求，本轮不执行测试，不启动新的 pytest/npm 测试，也不等待 GitHub 自动 CI 测试。仅审阅真实执行路径、检查差异与静态缺陷。前一轮报告中的测试结果不作为本轮竞态修补的验证结果。

没有数据库 Schema、迁移或依赖变化。已经开始的模型调用不会因此中止；保护的是后续业务状态写入与收尾通知，已经完成的广播也无法撤回。真实 PostgreSQL 并发回归尚未运行，应在后续获准测试时补齐。
