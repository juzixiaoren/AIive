# V1 本地加密备份与恢复

基线：`534932e630ad39495b67b87cac5099165800c1a6`。Ubuntu、Bash、Docker Compose v2（支持 `--wait` 和 JSON config）、Restic、Python >=3.11、git、flock、GNU coreutils。无远程存储，无新增常驻容器，无 WAL/PITR。

## 安装与密码

在服务器安装 `restic python3 util-linux`。代码假设部署于 `/opt/aiive`；其他位置同步修改 service ExecStart 与配置 APP_DIR。

```bash
sudo install -d -m 700 /etc/aiive /var/backups/aiive
sudo install -m 600 scripts/backup/backup.conf.example /etc/aiive/backup.conf
sudo sh -c 'umask 077; openssl rand -base64 48 > /etc/aiive/restic-password'
sudo install -m 644 deploy/systemd/aiive-backup.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiive-backup.timer
sudo bash scripts/backup/backup.sh
sudo journalctl -u aiive-backup.service
sudo systemctl list-timers aiive-backup.timer
```

配置与密码文件必须 root 所有、权限 600，父目录不得允许其他用户写入。密码不能放进 Git、`.env`、备份仓库或普通日志。**请另外离线保存恢复密码**；密码丢失无法解密；服务器磁盘整体损坏也会丢失本地仓库，应另行制定异机灾备，此 V1 不实现。

默认仓库 `/var/backups/aiive/repository`，临时目录也位于备份根目录，均在 Docker Volume 外。脚本检查 Docker Volume 挂载点与常见 Docker 路径。该目录必须在独立于应用卷的位置，不得通过额外 bind mount 再挂入应用。可配置 `BACKUP_ROOT`；只使用固定本地 repository 子目录。

## 一致性、容量与保留

独占 flock 覆盖备份、恢复和校验。Backend、Frontend 优雅停止，Backend 内 API/Worker/Scheduler 停写；PG 继续运行。禁止其他服务/外部客户端在此期间写数据库或应用卷。通过临时一次性 Backend 镜像读取业务数据审计，再用 PG 容器 `pg_dump -Fc` 与只读卷 tar 归档。进程 EXIT/INT/TERM 都尝试恢复原服务并检查健康；恢复失败退出 20 并打印 ALERT。SIGKILL、主机掉电无法执行 trap，需管理员启动服务，不能承诺自动恢复。

包含整个 aiive_data（对象、Sandbox、Auth Secret、Task Worktree）、`.env`、Compose、两个 Dockerfile、容器和不可变镜像 ID、Commit、PG 版本、UTC 时间、逐文件 SHA256 清单。审计记录所有 public 表计数、排序后的行摘要和所有应用文件 SHA256；因此历史消息、附件、Task 数据均被纳入恢复前后比对。内容寻址对象额外核对文件名 SHA256。恢复前拒绝绝对路径、父目录穿越和不安全链接归档；含外部绝对 symlink 的工作树需先迁移为卷内相对链接。

空间预检要求可用空间 >= 3 ×（卷大小 + 数据库逻辑大小）+ MIN_FREE_BYTES（默认 1GiB）；这是保守估算，不能保证写入过程中永不耗尽。空间不足在停写前退出。运行中失败仍尝试恢复服务，原卷不修改。Restic 自身可能留下未引用 pack，可由管理员检查，不自动删除旧生产数据。

需求中的“最近 3 个每日快照”与明确执行参数 `--keep-daily 7` 冲突；本实现默认 **7 个每日快照**，可设 `KEEP_DAILY=3`。只有新快照写入、`restic check --read-data`、重新恢复并核对全部 SHA256 都成功后，才执行带 host/tag 分组的 forget/prune。失败快照不会触发旧快照删除。全量读取校验期间仍停写，大数据集会产生较长维护窗口。日志记录退出码、快照 ID、Commit、耗时、仓库实际字节；具体失败命令的 stderr 进入 journald。

systemd timer 在**服务器本地时区**每天 03:00 执行，Persistent=true 补跑错过任务；请确认服务器时区。服务无超时上限，journald 收集 stdout/stderr。`KillMode=mixed` 给主进程 TERM 后等待 trap；超过 300 秒强制终止仍可能需要人工恢复。禁止人为同时运行多份配置指向同一应用的不同仓库。

## 恢复操作手册

```bash
sudo restic -r /var/backups/aiive/repository --password-file /etc/aiive/restic-password snapshots
sudo bash scripts/backup/verify.sh FULL_SNAPSHOT_ID
sudo bash scripts/backup/restore.sh FULL_SNAPSHOT_ID aiive-restore-drill /opt/aiive-restored
# 输入 RESTORE aiive-restore-drill
sudo docker compose -p aiive-restore-drill -f /opt/aiive-restored/restore-compose.json ps
```

恢复项目必须以 `aiive-restore-` 开头，目标目录、容器项目、目标卷必须全新；二次输入完整项目名。恢复绝不删除现有卷。先读完整仓库、还原临时目录、校验归档与清单，再创建全新 aiive_data 和 pgdata。新 PG 初始化空数据库，`pg_restore --exit-on-error --no-owner`，启动前对比所有数据审计；然后启动 Backend（lifespan 自动执行 Alembic 迁移），等待健康，启动 Frontend，从 Backend 请求 Frontend HTTP 完成冒烟。

原镜像须仍在本机（使用 metadata 中镜像 ID，包括 Frontend）。若镜像丢失，应先 checkout 备份 Commit 并按所备份 Dockerfile/部署配置重新构建，核实依赖一致后人工处理镜像映射；仓库不包含镜像层和完整 Git 源码。恢复目录包含敏感 `.env` 和渲染后环境变量，仅 root 可读。默认不发布任何端口；演练通过后由管理员审查恢复配置、外部 LLM/Embedding 等依赖并切换反向代理，避免恢复环境的后台任务访问生产外部资源。失败保留隔离项目用于诊断，脚本不会自动清理生产旧数据。

## 自动化验证与证据

```bash
python3 scripts/backup/test_integrity.py
# 在一次性 Ubuntu Docker 测试宿主机执行；先构建与 main 相符的三个镜像。
sudo AIIVE_TEST_BACKEND_IMAGE=aiive-backend:test \
 AIIVE_TEST_PG_IMAGE=aiive-postgres-bm25:pg16 \
 AIIVE_TEST_FRONTEND_IMAGE=aiive-frontend:test bash scripts/backup/test_restore.sh
```

集成测试生成独立 `aiive-restore-test-*` 项目和临时配置，不读生产 `.env`；写入 PostgreSQL 测试记录、真实 Object Store ObjectRef 与原始字节、Auth Secret；备份后删除**仅测试项目**旧卷，恢复到新项目，比较数据和文件摘要及启动健康。另测错误密码、空间不足、dump 期间 TERM 与恢复服务、独立复制仓库的数据损坏。测试直接写入真实 Thread/Event/Attachment/Task 模型，恢复后验证认证 Token、历史消息及附件下载 API；不调用外部 LLM。结束仅清理测试容器/卷，证据目录保留 root-only 密钥、日志、审计与磁盘字节，分享前脱敏。

2026-11-14 已在 Docker Desktop Linux Engine + Ubuntu 24.04 一次性容器完成真实集成演练，退出码 0：新快照、7 每日保留、删旧卷后新卷恢复、历史消息/附件/认证 API，以及错误密码、并发、空间不足、中断、仓库损坏均通过。仓库逻辑字节 88,592，分配字节 1,208,320。完整证据及缓存镜像构建限制见 [验收报告](../reports/2026-11-14_local_backup_restore.md)。systemd timer/journald 尚未在实际 Ubuntu 部署宿主机运行验证。
