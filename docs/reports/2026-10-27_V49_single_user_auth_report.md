# V49 单用户登录认证 — 交付报告

日期：2026-10-27
范围：`backend/aiive/auth/`、`api/routes_auth.py`、全局认证守卫、`frontend/`、`apps/android`、`apps/desktop`
约束：单用户个人部署；不引入多用户/角色/会话表/Refresh Token；不改既有权限与审批语义。

## 1. 设计取舍

| 决策 | 取值 | 理由 |
|---|---|---|
| 账号模型 | 全库唯一所有者账号（`auth_owners` 单行） | 个人 Agent OS 没有多用户需求；`singleton` 唯一约束让并发注册由数据库裁决 |
| 凭据 | 单个长期 `access_token`（HS256，默认 365 天） | 个人设备长期登录，避免频繁续期；不做 Refresh/轮换/服务端会话表 |
| 密码 | Argon2id（argon2-cffi 默认参数） | 抗离线爆破，且不引入额外服务 |
| 签名密钥 | `AIIVE_AUTH_SECRET` 优先；留空则生成一次并落盘 `AIIVE_AUTH_SECRET_FILE` | 进程重启后已签发 Token 继续有效；换密钥即全体失效 |
| 默认开关 | `AIIVE_AUTH_ENABLED=true` | 私人数据接口默认不裸奔；只有 loopback 允许关闭，非 loopback 启动即拒绝 |
| 边界 | 认证只回答"是不是本人" | 操作权限与审批仍由 `TaskPolicyEngine` / 工具审批决定，认证不替代授权 |

## 2. 后端实现

| 文件 | 内容 |
|---|---|
| `backend/aiive/auth/guard.py` | `EXEMPT_PATHS` 仅 `/health`、`/api/auth/status`、`/api/auth/register`、`/api/auth/login`；`auth_guard` 作为 HTTP 中间件在 CORS **之前**注册（Starlette 后注册者在外层，OPTIONS 预检先被 CORS 处理，不会被 401 拦截）；`authenticate_websocket` 在 accept 后 10s 内要求首帧 `{"type":"auth","access_token":...}`，失败 `close(1008)` 且不发送任何业务数据 |
| `backend/aiive/auth/security.py` | Argon2id `hash_password`/`verify_password`；HS256 `create_access_token`/`decode_access_token`（强制 `sub/iat/exp` + `iss=aiive`，过期与签名错误分别映射 `token_expired`/`invalid_token`）；`signing_secret` 落盘时 `chmod 600` |
| `backend/aiive/auth/service.py` | `status`/`register`/`login`/`me`；并发注册靠 `IntegrityError` → 409；登录失败按 `(client, username)` 进程内滑动窗口限流（5 次 / 900s → 429）；账号不存在时用固定 dummy hash 做等价耗时校验，响应时间不泄露账号是否存在 |
| `backend/aiive/api/routes_auth.py` | `GET /api/auth/status`、`POST /api/auth/register`、`POST /api/auth/login`、`GET /api/auth/me`；错误统一 `{"detail": code}` |
| `backend/aiive/api/routes_ws.py` | 先 `accept` → 首帧认证 → 才 `ws_manager.attach`，未认证连接不进广播集合 |
| `backend/aiive/api/routes_agent_tasks.py` | 任务事件 WS 同样先认证再读事件，失败直接返回 |
| `backend/aiive/api/routes_desktop.py` | 桌面节点 WS 双凭据：节点令牌（`hmac.compare_digest`）+ 用户 `access_token`（`decode_access_token`），任一失败 `close(1008)` |
| `backend/aiive/db/models.py` + `alembic/versions/a7c4e9f1b8d2_add_auth_owner.py` | `auth_owners` 表：`singleton` 恒 1 且唯一、`username` 唯一、Argon2id `password_hash`、`created_at`/`last_login_at`；约束名在模型与迁移中显式一致，避免 `alembic check` 把匿名唯一约束判成结构漂移 |
| `backend/aiive/config.py` | 6 个 `aiive_auth_*` 配置 + 启动校验：密钥长度 <32 拒绝、天数/次数/窗口必须为正、非 loopback 关闭认证直接拒绝 |

## 3. 前端与客户端

**Web（`frontend/`）**

- `src/api/client.ts` 是唯一允许 `fetch` 的地方：同源请求自动注入 `Authorization: Bearer`；第三方地址绝不带 Token；`401` 清 Token 并派发 `AUTH_EXPIRED_EVENT`；登录/注册/status 三个端点不参与 401 自动登出（密码错也是 401）。
- `src/hooks/useAuth.tsx`：`loading → anonymous | authenticated` 三态；启动先 `GET api/auth/status`，再按本地 Token 调 `api/auth/me` 恢复会话；监听过期事件被动登出；登录/登出同步桌面 Token。
- `src/App.tsx`：`AppGate` 未登录只渲染 `LoginPage`（私人页面与通知 WS 都不挂载）；登录后经 `safeInternalPath` 校验回跳原路径（拒绝 `//` 与含 `://` 的地址，避免开放重定向）；顶部显示账号与退出登录。
- `src/pages/LoginPage.tsx`：`status.initialized === false` 走首次注册，否则走登录；错误码映射为中文文案。
- 三个 WebSocket 入口（`ChatPage`、`useNotificationSocket`、`useTaskEvents`）在 `onopen` 统一调用 `sendWsAuth` 发首帧认证。

**Android（`apps/android/`）**

- `src/api/auth.ts`：`authFetch` 统一注入 Bearer，401 清 Token；Token 存 WebView `localStorage`（键 `aiive.android.auth.v1`），APK 重启后仍保持登录。
- `src/LoginScreen.tsx` + `App.tsx` 登录门禁：未登录只显示登录/注册，登录后进入原有遥控界面。

**Desktop（`apps/desktop/`）**

- 主进程新增 `aiive-desktop:set-auth-token` IPC，Token 只驻留内存，经 `utilityProcess.postMessage` 下发给 Node Host（不写磁盘、不进日志、不出现在命令行）。
- Node Host 在 `hello` 帧带 `access_token`；无 Token 时不连接，登出即停连并停止重连退避。

## 4. 验证

新增 34 例离线测试（`tests/unit/backend/test_auth.py` 23 例 + `test_auth_frontend_contract.py` 11 例）：

| 覆盖 | 说明 |
|---|---|
| 初始化与唯一注册 | `status` 只回 `initialized`；第二个账号 409；数据库层唯一约束在绕过应用检查时仍拒绝 |
| 密码与 Token | Argon2id 哈希不含明文；过期/签名错误/issuer 不符分别拒绝；密钥落盘后"重启"（清缓存重解析）旧 Token 仍有效 |
| 登录失败不可区分 | 错误密码与不存在账号返回同一 `invalid_credentials`；连续失败触发 429 |
| 匿名访问矩阵 | 遍历全部 HTTP 路由，白名单外一律 401；白名单内正常 |
| WebSocket | 无首帧/错误 Token 关闭且不推数据；正确 Token 收到 `auth_ok` 后可通信；桌面节点缺用户 Token 被拒 |
| 前端契约（源码扫描） | 页面禁止裸 `fetch`、页面/API 模块不得自管 Token、每个 WS 入口必须发认证帧、`App` 必须有登录门禁与安全回跳、桌面 IPC 只传 Token |

命令与结果（本机，2026-10-27）：

```
.venv/Scripts/python.exe -m pytest tests/unit/backend/test_auth.py tests/unit/backend/test_auth_frontend_contract.py -q   # 34 passed
.venv/Scripts/python.exe -m pytest tests -q                                                                             # 1309 passed, 23 skipped (199s)
.venv/Scripts/python.exe -m ruff check backend/aiive --select F,E9                                                      # All checks passed
/e/python/Scripts/basedpyright                                                                                          # 0 errors, 0 warnings, 0 notes
cd frontend && npx tsc --noEmit && npm run build                                                                        # 通过（509 modules）
cd apps/android && npx tsc --noEmit                                                                                     # 通过
cd apps/desktop && node --test src/node-host/journal.test.mjs                                                            # 7 passed
```

迁移链校验：`a7c4e9f1b8d2` 接在 `d5c1a7b9e4f2`（V46 知识分块向量）之后，全库 22 个迁移仍为**单头**；模型侧约束名与迁移逐字一致。

## 5. 已知取舍与风险

1. **单 Token 无吊销**：登出只清客户端 Token；泄露的 Token 在过期前仍有效。要强制全体失效只能更换 `AIIVE_AUTH_SECRET`（代价是所有人重新登录）。这是单用户长期登录的刻意取舍。
2. **限流是进程内状态**：按 `(client, username)` 计数，重启清零；多 worker 部署各自计数。当前单进程 uvicorn 部署下语义正确，横向扩容需外部存储。
3. **没有改密/找回接口**：忘记密码只能删 `auth_owners` 行后重新注册（本轮未提供 UI 或 CLI）。
4. **认证开启后本地脚本需带 Token**：`curl` 调 `/api/*` 前需先 `POST /api/auth/login` 取 Token；仅 loopback 可用 `AIIVE_AUTH_ENABLED=false` 临时关闭。
5. **WS 首帧认证有 10s 超时**：客户端必须在 `onopen` 立即发送认证帧；反向代理需正常转发首帧（既有 `/ws/` 反代已满足）。
6. **前端 Token 存 localStorage**：XSS 可读取。当前无第三方脚本、CSP 由部署层控制；若后续引入富文本外部内容，需要重新评估。

## 6. 部署与运维

- 首次启动：浏览器打开 `/aiive/login` → 注册唯一账号（注册成功即登录）。
- `.env.example` 新增 6 个变量：`AIIVE_AUTH_ENABLED`、`AIIVE_AUTH_SECRET`、`AIIVE_AUTH_SECRET_FILE`、`AIIVE_AUTH_TOKEN_DAYS`、`AIIVE_AUTH_LOGIN_MAX_ATTEMPTS`、`AIIVE_AUTH_LOGIN_WINDOW_SECONDS`。
- Docker：`aiive_data` 卷挂到 `/app/.data`，`auth_secret` 随卷持久化；容器 `HOST=0.0.0.0`，因此**不能**关闭认证（启动校验会直接拒绝）。
- 备份：`auth_owners` 与 `auth_secret` 一起备份；只备份数据库而丢密钥会导致所有已登录设备 Token 失效（重新登录即可恢复）。
