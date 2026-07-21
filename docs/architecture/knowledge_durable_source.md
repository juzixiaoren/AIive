# Knowledge 原文持久化架构

## 目标

Knowledge 的来源文件路径只用于 provenance。摄取完成后，即使来源文件被移动或删除，系统仍能读取经过校验的原文，并从原文重新生成分块。

## 数据流

1. 所有 API 和 ToolRegistry 摄取入口统一委托 `KnowledgeIngestor.ingest()`。
2. 摄取器读取原始字节，以 SHA-256 生成内容地址，写入 `knowledge-documents` bucket。
3. ObjectStore 使用同目录临时文件和原子替换；相同内容地址重复写入时幂等复用，地址相同但内容不同时拒绝覆盖。
4. `documents` 表保存 `object_bucket`、`object_key`、`content_hash`、`content_size`、`mime_type`、`doc_type` 和 `status`。
5. `read_source()` 根据数据库对象引用读取原文字节，并重新计算 SHA-256；校验失败时不返回内容。
6. `reindex()` 仅调用 `read_source()` 重建 chunks，不访问 `source_path`。

## 接口与工具

- `POST /api/knowledge/ingest`：摄取并持久化原文。
- `GET /api/knowledge/{document_id}/source`：读取经过哈希校验的原文。
- `POST /api/knowledge/{document_id}/reindex`：从持久原文重建分块。
- `ingest_document`：与 API 共用摄取实现。
- `reindex_document`：从持久原文执行重新索引。

## 一致性与恢复

对象采用内容寻址，数据库事务失败后可能留下未引用对象，但不会产生错误引用，也不会覆盖其他内容。后续相同内容摄取会复用该对象。对象清理由独立的引用扫描流程处理；任何删除必须调用 `safe_delete`，当前摄取事务不会直接删除对象。

旧数据库记录没有可证明的持久对象引用，迁移后状态为 `source_unavailable`，不会根据 `source_path` 伪造已持久化状态。重新摄取仍存在的来源文件后，可按相同 `content_hash` 补齐对象引用并恢复为 `indexed`。

当前重新索引为同步基础能力，没有声明或伪装成异步 Outbox 索引。
