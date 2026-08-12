# 本地知识库

使用 `ingest_document` 保存原始文件并生成纯文本分块；使用 `search_knowledge` 检索，用 `reindex_document` 从持久原文重建索引。导入后至少查询一个文档中的关键短语作为验证。重复内容应复用同一内容哈希，不要因为源路径变化而复制知识记录。
