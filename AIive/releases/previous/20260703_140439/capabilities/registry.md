# 能力注册表

本文件记录Agent的所有能力。

## 能力状态

- **active**：当前可用，并可参与能力路由
- **dormant**：能力存在，但默认不主动唤醒
- **deprecated**：不推荐使用，保留历史记录
- **candidate**：用户提出或Agent规划中的候选能力，尚未完成

## 能力列表

### 1. 文档读取能力

- Status: active
- Type: local_tool
- Description: 读取docs/下的项目文档
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 启动时、需要理解项目时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: core/file_store.py
- Notes: 基础能力

### 2. Mind文件维护能力

- Status: active
- Type: local_tool
- Description: 读取和修改mind/下的人格、用户模型、交互规则、自我模型
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 用户要求修改人格、交互规则时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: core/self_model_manager.py
- Notes: 基础能力

### 3. Memory文件维护能力

- Status: active
- Type: local_tool
- Description: 读写memory/下的偏好、反馈、事件和总结
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 用户表达偏好、要求记住时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: core/memory_router.py
- Notes: 基础能力

### 4. 能力注册表维护能力

- Status: active
- Type: local_tool
- Description: 维护capabilities/registry.md
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 用户要求新增能力时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: core/capability_router.py
- Notes: 基础能力

### 5. 交互分类能力

- Status: active
- Type: local_tool
- Description: 根据用户输入判断应该：普通回复、写入偏好、修改人格、修改listen policy、写入记忆、创建新能力请求、创建运行机制修改请求、记录bug反馈、创建自我开发任务
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 每次用户输入时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: core/intent_classifier.py
- Notes: 基础能力

### 6. 自我开发任务队列

- Status: active
- Type: local_tool
- Description: 维护self_development/issues.md、ideas.md、changelog.md
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 用户提出新能力请求、运行机制修改请求时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: core/self_model_manager.py
- Notes: 基础能力

### 7. Candidate修改机制

- Status: active
- Type: local_tool
- Description: 创建candidate版本，在candidate上修改，通过health check后promote，失败则保留current
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 需要修改核心代码时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: kernel/version_manager.py
- Notes: 基础能力

### 8. Health Check

- Status: active
- Type: local_tool
- Description: 检查必要目录和文件存在、registry存在、mind文件存在、docs文件存在、self_development文件存在、logs可写、测试模块可导入
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 版本更新前、定期检查时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: kernel/health_check.py
- Notes: 基础能力

### 9. Self Model更新

- Status: active
- Type: local_tool
- Description: 更新mind/self_model.md
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 每次重要修改后
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: core/self_model_manager.py
- Notes: 基础能力