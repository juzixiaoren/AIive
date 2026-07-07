# AIive

AIive 是面向单个用户的本地个人 Agent OS，不是普通聊天机器人。

## 快速开始

```bash
# 安装依赖
pip install -e .

# 启动后端
uvicorn aiive.main:app --reload

# 健康检查
curl http://localhost:8000/health
```

## 项目结构

```
aiive/
  README.md
  pyproject.toml
  .env.example
  docker-compose.yml
  docs/               # 设计文档与阶段计划
  backend/aiive/      # FastAPI 后端
  frontend/           # React + Vite 前端
  tests/              # 单元测试与集成测试
  scripts/            # smoke 脚本与开发工具
```
