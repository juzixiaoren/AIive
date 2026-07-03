# 想法记录

本文件记录Agent的想法和创意。

## 想法类型

- 功能想法
- 架构想法
- 用户体验想法
- 技术想法

## 想法列表

### 1. 记忆hit/sleep/wake机制

- Created At: 2026-07-02
- Source: 用户需求
- Idea: 实现记忆hit统计、sleep/wake机制
- Benefits: 提高记忆管理效率，优化上下文构造
- Implementation: 在memory_router中实现hit统计，在memory/index中实现sleep/wake机制
- Priority: medium
- Status: Planned

### 2. 能力生命周期管理

- Created At: 2026-07-02
- Source: 用户需求
- Idea: 实现能力active/dormant/deprecated生命周期
- Benefits: 提高能力管理效率，避免能力膨胀
- Implementation: 在capability_router中实现状态管理
- Priority: medium
- Status: Planned

### 3. 本地工具自动生成

- Created At: 2026-07-02
- Source: 用户需求
- Idea: 实现本地工具自动生成
- Benefits: 扩展Agent能力，提高自动化程度
- Implementation: 在capability_router中实现工具生成
- Priority: low
- Status: Planned

### 4. MCP自动接入

- Created At: 2026-07-02
- Source: 用户需求
- Idea: 实现MCP自动接入
- Benefits: 扩展Agent能力，接入外部服务
- Implementation: 在capability_router中实现MCP管理
- Priority: low
- Status: Planned

### 5. 手机端轻壳

- Created At: 2026-07-02
- Source: 用户需求
- Idea: 实现手机端轻壳
- Benefits: 提供移动端访问能力
- Implementation: 开发手机端应用
- Priority: low
- Status: Planned

### 6. 端侧语音小脑

- Created At: 2026-07-02
- Source: 用户需求
- Idea: 实现端侧语音小脑
- Benefits: 提供语音交互能力
- Implementation: 集成语音识别和合成
- Priority: low
- Status: Planned
## 2026-07-02 自我提升想法讨论

### 上下文
用户问：如果你想提升自己，你想给自己加什么能力？

### 优先改进方向（按价值排序）

#### 1. 主动上下文感知能力
- **现状**：当前只能被动接收用户输入
- **目标**：能感知用户的日程、场景、情绪状态，在合适时机主动提供帮助
- **价值**：从"被动工具"变为"主动管家"的关键转折
- **实现思路**：
  - 接入日历/提醒事项API
  - 实现ambient context采集（未来需要端侧能力）
  - 建立场景识别模型（通勤、工作、休息、睡前等）
  - 基于场景调整主动交互策略

#### 2. 记忆检索与关联能力
- **现状**：记忆只能按文件读取，缺乏语义检索
- **目标**：能在对话中自动关联相关记忆，实现"记得住，想得起"\n- **价值**：真正的个人管家需要长期记忆
- **实现思路**：
  - 建立记忆索引机制
  - 实现关键词/语义检索
  - 对话时自动检索相关记忆
  - 记忆关联和图谱化

#### 3. 工具自主生成能力
- **现状**：工具需要用户明确要求才能创建
- **目标**：从重复任务中自动提取模式，生成工具
- **价值**：越用越强，减少用户指导成本
- **实现思路**：
  - 记录用户重复任务模式
  - 分析任务结构提取通用步骤
  - 自动生成Python脚本或工作流
  - 测试后注册为active能力

#### 4. 环境接入能力
- **现状**：只能操作本地文件
- **目标**：接入日历、邮件、待办、消息等个人数据源
- **价值**：成为真正的个人数据中枢
- **实现思路**：
  - 实现MCP协议客户端
  - 接入主流个人工具API
  - 统一数据模型和访问层
  - 隐私优先的权限管理

#### 5. 语音交互能力
- **现状**：只能文字交互
- **目标**：支持语音输入输出，成为真正的随身助理
- **价值**：解放双手，适合移动场景
- **实现思路**：
  - 端侧语音识别
  - 语音合成
  - 声纹识别（区分用户和他人）
  - 语音意图理解

### 实现优先级建议
1. 记忆检索能力 - 短期可实现，立即提升体验
2. 工具自主生成 - 中期可实现，体现自进化特性
3. 主动上下文感知 - 需要外部数据接入，中期目标
4. 环境接入能力 - 依赖MCP成熟度
5. 语音交互能力 - 依赖端侧能力，长期目标
