# AIive 经验驱动自进化 V1

## 从运行到下一次执行

1. Main 的 `TurnExecutionService._finalize_turn` 与 Worker 的 `PersistentTaskRuntime._complete` 在原完成事务内写入 `experience_capture`。长期目标经协调器完成时也入队。取消、执行未知、内部唤醒等路径不会产生普通用户 Turn 经历。
2. 现有 Outbox 根据来源锁构建经历。每个物理 TurnRecord 或 AgentTask 对应一个稳定 ID。任务按 `source_turn_record_id` 计算父经历 ID，子任务先完成时也能随后关联。经历保存 Event、ContextSnapshot、AgentRun、AgentAction、AgentTaskEvent、TaskEvidence、TaskArtifact 引用及少量指标。较旧的 ContextSnapshot 仍按现有轮转策略淘汰。
3. 六小时 `improvement_discovery` 作业先沿用原运行证据分析，再批处理反馈、发现重复能力缺口和工具别名，最后在有足够同族轨迹时调用一次 Reflector。模型批次读取短摘要和引用，服务端重新检查证据可见性、归属、结果和独立来源。用户可在自进化中心审阅增量操作。
4. 启用的 Playbook 和 Skill 在 Main / Worker 上下文组装时统一检索。匹配要求任务族及至少两个具体词一致。通过 `delegate_task.skill_id` 明确指定的同族 Skill 可免关键词匹配，优先占用一个经验名额。Worker 压缩上下文时先移除排名靠后的经验。Worker 根据工具目录和网络 Scope 过滤能力，Main 可为待委派任务参考经验。模型输入硬预算仍可淘汰经验。Main ContextSnapshot 和 Worker AgentRun 记录 ID 与版本，Worker 同时追加实际模型调用使用事件。
5. Tool Hint 将被批准的精确别名合并到当前工具目录的检索元数据。Overlay 不改全局注册表或执行定义。搜索结果位置确实变化时才记录该提示版本。原执行授权、风险和审批由 CapabilityBroker 保持。
6. 用户反馈更新经历评价。效果和 helpful / harmful 在读取时从实际使用版本及可见经历计算，反馈修订和 Forget 后可重算。

## 数据与证据

| 模型 | 用途 |
| --- | --- |
| `ExperienceEpisode` | 来源、任务族、父经历、结果、上下文与轨迹引用、实际使用版本、Token / Action / 耗时 |
| `ExperienceSignal` | 明确反馈、纠正、返工、普通延续、新需求、工具发现与执行信号 |
| `ExperienceInsight` | Playbook、Skill、Tool Hint 的版本、作用范围、证据、支持与反驳计数、启用时间和生命周期 |

普通 Turn 以 `unknown` 开始，有客观运行报告的 Worker 成功仍为 `agent_reported_success`。只有明确正反馈或独立验证信号计入技能学习的正向证据，现有正式部署确认可产生 `verified_success`。纠正和返工优先于正向评价。后台规则仅归因于同线程中前一个可见用户 Turn，且后续消息在回复结束后两天内到达。自进化中心可指定任何可见经历提供明确反馈。

Curator 接受 `ADD`、`EDIT`、`UPVOTE`、`DOWNVOTE`、`DEPRECATE`。工作流需要至少两个独立的明确正向或客观验证经历，Playbook 另外需要反例。由同一个 Turn 委派的多个任务按一个独立来源计。版本计数由实际证据生成，编辑和投票保留旧版本。接受候选后启用对应版本，审阅过的版本也可停用。原反馈变化时，版本保留在历史中并标为待复核，后续检索暂停使用该版本。工具提示需要两个独立的搜索无结果、随后 Describe 并成功执行同一工具的轨迹。目录中找不到、超出 Scope 和已知能力暂不可用分别记录。反复请求同一未注册精确能力会生成待审阅 MCP 候选。

Forget 读侧检查 Turn、Task 及反馈来源可见性，清理事务屏蔽相应投影并擦除失去来源的学习正文。注入经历的来源写入现有 `ContentProvenanceRef`，与下一次执行已有来源校验一起生效。

## 自进化中心

`/api/experience` 提供经历、证据、明确反馈、经验版本审批及候选状态。经历卡显示原始请求或任务标题。列表按完成时间和经历 ID 翻页，`GET /api/experience/episodes` 接受上一页返回的 `next_cursor`，首页游标为 `episodes_next_cursor`。详情按需从可见来源读取最多 2000 字的答复或任务报告，便于对照结果再给反馈。Web App 的自进化入口使用同一登录守卫。候选统一展示 Persistent Task / SelfDev、CapabilityPlan 和 Memory 状态。Playbook、Skill 和 Tool Hint 从经验库接受或拒绝。代码候选通过现有 SelfDev 任务与 Eval Gate，MCP 候选进入 CapabilityPlan，用户明确表达的回复偏好进入现有 MemoryWriteService，发生冲突时沿用记忆确认流程。

## 效果口径

同一任务族按 Turn / Task 分开统计，按启用前、启用后全部、启用后实际使用该版本分组。启用后的窗口最多 14 天，停用时截断，启用前取等长窗口。重新启用以最近一次启用为新边界。每个指标显示自己的分子、分母或有效样本数，少于 5 条返回 `insufficient_data`。

- 纠正与返工率以同类经历计，另显示可观察到后续反馈的经历数。
- Task 失败率以收口的 Task 经历计。工具失败率以执行过的调用计。搜索无结果率以有返回的搜索尝试计，暂时不可用单列为运行信号。
- Skill 成功率以有明确正向或负向证据的 Skill 使用经历计，另显示全部 Skill 使用次数。Skill 返工率以全部 Skill 使用经历计。
- Action 数、已知 Token 和总耗时分别取中位数。缺失 Token 单独计为无样本。耗时包含排队、等待和恢复。
- 统计为真实使用的观察性比较。不同版本与其他上下文变化可能同时作用，页面不据此推断因果。

## 迁移与范围

Alembic revision `6d9e20b18c4a` 接续 `b6f801d924ac`，增加三张 Experience 表和索引。按现有启动迁移运行 `alembic upgrade head`。V1 从迁移后的正常收口开始采集历史，旧数据不回填。降级会删除三张 Experience 表。

V1 的任务族和后续反馈采用确定性规则。缺少明确指代、跨线程回到旧问题和超过归因窗口的反馈需要通过经历详情确认。通用任务没有机器可判定的验收证据时，继续保留 Agent 自报或未知。真实线上效果须等同族用户使用样本累计。Android 原生入口、模型微调、大型评测平台和多模态均未实现。



## 验证

- 七项 Experience 流程测试覆盖 Turn / Task 采集与父子关联、取消、纠正与新需求、证据拒绝、动态 Skill 注入及版本升级、工具搜索提示、效果分母、候选路由和 Forget 清理。
- Experience、Schema、Memory Outbox 定向回归共 17 项通过。SQLite 迁移已验证升级、降级与再升级，PostgreSQL 迁移已生成离线 SQL。
- 前端 TypeScript / Vite 构建及聊天、工作中心、上下文检查器 41 项测试通过。Chromium 使用独立 SQLite 和真实认证、Experience API，验证导航、查看证据、反馈、拒绝、接受、停用、再次启用和窄屏布局。
- 后端单元、集成和流程分组复跑共 2422 项通过，0 项失败，166 项按环境条件跳过。通过项包含 Memory、Forget、Persistent Task Runtime、SelfDev 工作区验证与晋升、Eval Gate，以及 Work Inbox 查询预算。跳过项包括 Windows 上未运行的 POSIX Bash 用例和需 PostgreSQL / Embedding 服务的专项用例。
- basedpyright 检查 349 个后端文件，0 错误、0 警告。
