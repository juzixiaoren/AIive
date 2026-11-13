# V1 本地备份与完整恢复实测（2026-11-14）

结果：**PASS，集成脚本退出码 0**。同一 Part 完成备份、保留、删卷、新卷恢复及失败路径演练。源代码基线 main `534932e630ad39495b67b87cac5099165800c1a6`；测试部署目录是脚本生成的独立 Git 仓库，其 Commit 与源码基线不同，日志中的 `commit=58bf27fd...` 指该测试部署仓库。

## 环境与证据

Windows Docker Desktop 的 Linux Engine 29.9.3；一次性 Ubuntu 24.04 编排容器；Compose 2.40.3、Restic 0.16.4、宿主脚本 Python 3.12.3；Backend Python 3.12；PG 16.15。没有生产容器或生产卷参与测试。

标准 Backend Dockerfile 构建遇到 Docker Hub 认证网络超时；本次使用缓存 `aiive-backend:latest` 依赖基础层，复制当前 Backend、Alembic、pyproject，并重新执行 `pip install .` 安装当前项目声明依赖，随后应用直接 Uvicorn CMD，生成 `aiive-backend:backup-test-v2`。PG 使用本机 `aiive-pg-bm25:pg16`，Frontend 使用本机 `aiive-frontend:latest`。此证据不代表全新基础镜像构建或 Ubuntu 服务器 systemd 已部署验收。

- 原项目：`aiive-restore-test-1794588438-12822`。
- 恢复项目：`aiive-restore-test-1794588438-12822-new`。
- 原卷经 `compose down -v` 删除，再创建恢复项目的全新两个卷。
- 成功恢复快照：`0cbb52cf8e3c42a34a72346920f258750949fbbab55f401e7e649e2fe73753cc`。
- 原始日志时间为 UTC；`2026-11-13 16:49:05Z` 即北京时间 `2026-11-14 00:49:05`。
- [完整控制台](2026-11-14_backup_evidence/console.txt)、[备份](2026-11-14_backup_evidence/backup.txt)、[中断](2026-11-14_backup_evidence/interrupted.txt)、[保留](2026-11-14_backup_evidence/retention.txt)、[恢复](2026-11-14_backup_evidence/restore.txt)、[退出状态](2026-11-14_backup_evidence/result.txt)、[最终快照清单](2026-11-14_backup_evidence/snapshots.json)。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| PG `pg_dump -Fc` / `pg_restore --list` | 通过 |
| Restic 新快照全量读取、再还原 SHA256 复核 | 通过 |
| 所有 public 表计数及排序行摘要、完整卷文件 SHA256 恢复前后比较 | 一致 |
| 真实 Thread、Event、Attachment、Task 模型记录 | 恢复一致，无新增业务表 |
| ObjectRef、27 字节原始对象、SHA256 | 一致 |
| Auth Secret 摘要、备份前签发的 Token 调用恢复后 `/api/auth/me` | 通过 |
| 历史消息 API、附件内容下载 API | 返回原消息及原始附件字节 |
| Sandbox / Worktree 卷内测试标记 | 字节一致 |
| Backend Alembic lifespan 启动及健康、Frontend 页面及 `/api/auth/status` 代理 | 通过 |
| `.env` 与部署配置 | 快照 SHA256 及恢复 `.env` 比较通过（测试 `.env` 为空） |
| 错误密码 | 备份脚本退出 1，服务不受影响 |
| 仓库并发操作 | 退出 3 |
| 空间不足 | 停写前退出 4，Backend 仍健康 |
| dump 期间 TERM | 退出 143，最终日志包含状态；服务恢复健康，原快照仍可读 |
| 拒绝覆盖既有恢复目录/项目 | 非零退出 |
| 人工损坏独立仓库副本 | Restic 数据包哈希/密文校验失败，非零退出；原仓库不修改 |
| 默认 7 每日保留 | 构造 8 个历史日期快照，再执行正式备份，最终恰好 7 个 |

历史日期通过 Restic `--time` 构造，不代表连续运行 8 天。测试业务记录通过 ORM 写入，恢复后用真实 HTTP API 验证历史消息、附件和认证；不调用外部 LLM。生产流量、真实用户数据、大容量性能与生产服务器的 timer/journald 仍需部署环境验证。

## 磁盘数据

- 初次成功备份仓库逻辑字节：46,718。
- 完成每日保留演练后：**88,592 逻辑字节**，**1,208,320 分配字节**（Docker Linux 文件系统块分配）。
- 首份数据库归档：240,852 字节；完整应用文件 tar：20,480 字节。
- 对象 SHA256：`fe0e4df155c273f2b00fd3e8aaeabd2916a9a32750c7cbda8ceea5d0ae22c20b`。

小数据集结果不能外推生产容量。预检仍按 3 ×（卷大小 + 数据库大小）+ 余量估算；生产首次备份应确认实际空间与停机窗口。

## 本次实测修复

1. Backend Dockerfile 改为直接执行 Uvicorn，解决 shell 未转发停止信号导致等待停止超时的问题。
2. 状态日志保留原始输出 FD，避免 dump 重定向期间信号 trap 的日志被写进临时 dump 并随清理丢失；收尾忽略重复 INT/TERM，服务恢复失败仍退出 20。
3. 恢复网络按新项目命名，并拒绝启用服务的宿主 bind mount、network_mode 和外部配置资源。
4. Shell 与 systemd 文件固定 LF；测试夹具修正事件外键写入顺序与当前 Thread，未改变业务逻辑。

测试项目与新旧测试卷已自动清理。一次性 runner 已停止；原始加密测试仓库、测试恢复密码和调试文件保留于其 root-only 测试目录 `/var/tmp/aiive-backup-test.i74kyIjW`，不提交 Git。归档证据不包含密码、Token 或原始 Auth Secret。
