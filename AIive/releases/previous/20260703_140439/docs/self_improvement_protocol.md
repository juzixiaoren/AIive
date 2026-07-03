# 自我改进协议

## 1. 目的

自我改进协议定义 Agent 如何从用户需求、系统错误、重复任务中生成自我开发任务，并在 candidate 版本中实现、测试、上线或回滚。

## 2. 自我改进流程

```text
用户需求 / 系统反馈 / 重复任务
↓
分类：偏好、人格、规则、能力、运行机制、文档
↓
如果是简单文本沉淀：直接修改 mind/memory/docs
↓
如果是能力或机制变更：创建 issue
↓
必要时创建 candidate
↓
在 candidate 中修改
↓
运行 health check
↓
通过则 promote
↓
失败则 rollback
↓
记录 changelog
↓
更新 self_model
```

## 3. issue 格式建议

```text
## [Open] Issue Title

- Created At:
- Source:
- User Request:
- Type: capability / runtime / memory / listen_policy / bug / docs
- Related Files:
- Proposed Plan:
- Priority:
- Status:
- Notes:
```

## 4. changelog 格式建议

```text
## YYYY-MM-DD HH:mm

### Change
做了什么。

### Reason
为什么做。

### Files
修改了哪些文件。

### Result
成功 / 失败 / 记录为待开发。

### Next
后续动作。
```

## 5. candidate 修改规则

Agent 不应直接修改生产版本核心代码。

流程：

```text
create_candidate()
modify_candidate()
run_health_check()
if pass:
    promote_candidate()
else:
    rollback()
```

## 6. health check 最小要求

必须检查：

- 必要目录存在。
- 必要文件存在。
- Markdown 文件可读写。
- registry 存在。
- mind 文件存在。
- self_development 文件存在。
- logs 可写。
- 主程序可启动。

## 7. promote 规则

只有 candidate 通过 health check，才能 promote。

promote 后：

- 更新 changelog。
- 更新 self_model。
- 保留 previous。
- 记录版本信息。

## 8. rollback 规则

如果 candidate 失败：

- 不覆盖 current。
- 记录失败原因。
- 创建或更新 issue。
- 回复用户说明未完成。

## 9. 诚实原则

如果只是记录 issue，就说"已记录为待开发任务"。

如果只是注册 candidate capability，就说"已注册为候选能力，尚未实现"。

如果通过测试并上线，才说"已实现并生效"。