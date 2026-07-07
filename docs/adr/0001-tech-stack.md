# ADR 0001: 技术栈锁定

## 状态
Accepted

## 上下文
AIive 需要从 V0 起锁定技术栈，避免后期频繁切换导致工程债务。

## 决策

### 后端
- **语言**: Python 3.12+
- **API 框架**: FastAPI
- **数据校验**: Pydantic v2
- **数据库**: PostgreSQL 16+
- **ORM**: SQLAlchemy 2.x
- **数据库迁移**: Alembic
- **数据库驱动**: psycopg 3
- **HTTP 客户端**: httpx
- **配置管理**: pydantic-settings + .env
- **测试框架**: pytest
- **LLM 接入**: OpenAI-compatible Chat Completions（自研 client，面向 DeepSeek 等兼容服务）
- **后台任务**: PostgreSQL outbox worker + Python worker loop

### 前端
- **框架**: React + Vite + TypeScript
- **请求状态**: TanStack Query
- **路由**: React Router
- **样式**: Tailwind CSS

### 存储
- **主存储**: PostgreSQL（从早期接入）
- **Object Store**: 早期使用本地 filesystem object store（接口兼容 S3/MinIO）
- **向量索引**: Qdrant（中期引入，可重建）
- **Markdown Projection**: 早期只做输出投影

## 后果
- 不允许替换为 Django、Flask、SQLite-only、MongoDB
- 不允许早期引入 Redis/Celery/Kafka/RabbitMQ
- 不允许使用 Next.js 全栈后端或 Electron
