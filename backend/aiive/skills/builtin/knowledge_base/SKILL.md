# 本地知识库

使用 `ingest_document` 保存原始文件并生成纯文本分块。先用 `search_knowledge` 定位有用分块，再用 `read_knowledge_chunk` 读取需要引用的正文，并按 `next_offset` 续读。从持久原文重建索引用 `reindex_document`。重复内容复用同一内容哈希。
