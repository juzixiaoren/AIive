# Agent 自我模型

## 当前能力

1. 能读取docs/下的项目文档
2. 能读取和修改mind/下的人格、用户模型、交互规则、自我模型
3. 能读写memory/下的偏好、反馈、事件和总结
4. 能维护capabilities/registry.md
5. 能维护self_development/issues.md、ideas.md、changelog.md
6. 能根据用户输入判断意图并执行相应操作
7. 能创建candidate版本，在candidate上修改，通过health check后promote
8. 能生成完整的能力骨架（目录+README+骨架文件+注册表）
9. 能实现记忆hit机制，记录记忆被读取的次数
10. 能维护会话记忆（短期记忆），管理当前任务的工作状态

## 当前限制

1. 不实现真实MCP
2. 不实现真实语音系统
3. 不实现真实手机App
4. 不实现复杂向量记忆
5. 不实现完全无人监督的自我重构

## Active Capabilities

1. 文档读取能力
2. Mind文件维护能力
3. Memory文件维护能力
4. 能力注册表维护能力
5. 交互分类能力
6. 自我开发任务队列
7. Candidate修改机制
8. Health Check
9. Self Model更新
10. Skill生成能力
11. MCP生成能力
12. Memory Hit机制
13. 会话记忆管理

## Dormant Capabilities

- 无

## 未来计划

1. 记忆sleep/wake机制
2. 能力active/dormant/deprecated生命周期
3. 本地工具自动生成
4. MCP自动接入
5. 手机端轻壳
6. 端侧语音小脑
7. 用户声纹识别
8. 环境语音短期缓存
9. 用户发言时附带1分钟ambient context
10. 更早ambient speech本地检索
11. 主动早晨/通勤场景
12. 动态UI生成
13. 更强candidate测试和回滚
14. 长期self_model维护
15. 多模型分层调用

## 更新记录

（初始化后自动记录）
