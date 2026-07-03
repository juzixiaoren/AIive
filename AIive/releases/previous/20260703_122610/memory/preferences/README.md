# 偏好记忆

本目录用于存储用户的长期偏好。

## 记忆类型

- 咖啡偏好
- 交互风格偏好
- 工具使用偏好
- 推荐偏好

## 文件格式

每个偏好文件建议包含：

```text
## YYYY-MM-DD HH:mm

### Source
用户原话或事件来源。

### Content
沉淀后的记忆内容。

### Type
preference

### Scope
适用场景。

### Confidence
high / medium / low

### Notes
备注。
```

## 示例

### coffee.md

```text
## 2026-07-02 18:51

### Source
"我不爱喝瑞幸，我爱喝星巴克。"

### Content
用户不喜欢瑞幸咖啡，更偏好星巴克。

### Type
preference

### Scope
咖啡推荐、点单场景

### Confidence
high

### Notes
瑞幸相关能力如果以后存在，不会被删除，但会降低主动推荐优先级。
```