"""前端与客户端认证契约的静态守卫（V49，全离线）。

目的：认证接入最容易腐化的地方是"某个页面又直接 fetch 了一次"或"某个 WebSocket
忘了发认证帧"。这里用源码扫描把这些约定固化成测试，避免只测登录接口就宣称
所有页面都受保护。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WEB_SRC = ROOT / "frontend" / "src"
ANDROID_SRC = ROOT / "apps" / "android" / "src"
DESKTOP_SRC = ROOT / "apps" / "desktop" / "src"

RAW_FETCH = re.compile(r"(?<![A-Za-z0-9_$.])fetch\(")


def _sources(base: Path, patterns: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        files.extend(base.rglob(pattern))
    return sorted({path for path in files if "node_modules" not in path.parts})


def test_web_pages_never_call_fetch_directly() -> None:
    """所有 Web 请求都必须经过统一客户端，避免漏带 Token。"""
    offenders: list[str] = []
    for path in _sources(WEB_SRC, ("*.ts", "*.tsx")):
        if path.name == "client.ts":
            continue  # 统一客户端内部就是唯一允许调用 fetch 的地方
        text = path.read_text(encoding="utf-8")
        if RAW_FETCH.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"以下文件绕过统一客户端直接 fetch: {offenders}"


def test_web_client_injects_bearer_token() -> None:
    text = (WEB_SRC / "api" / "client.ts").read_text(encoding="utf-8")
    assert "Authorization" in text and "Bearer" in text
    assert "localStorage" in text
    assert "status === 401" in text
    assert "AUTH_EXPIRED_EVENT" in text


def test_web_pages_do_not_manage_tokens_themselves() -> None:
    """页面与业务 API 模块不得自行读写 Token 或手拼 Authorization。"""
    offenders: list[str] = []
    for path in _sources(WEB_SRC / "pages", ("*.tsx",)) + _sources(WEB_SRC / "api", ("*.ts",)):
        if path.name in {"client.ts", "auth.ts"}:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"localStorage\.(?:get|set|remove)Item\([^)]*token", text, re.IGNORECASE):
            offenders.append(str(path.relative_to(ROOT)))
        if "Authorization" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"以下文件自行维护认证信息: {offenders}"


def test_web_websockets_send_auth_frame() -> None:
    """每个 WebSocket 连接都必须在 onopen 时发送首帧认证。"""
    sockets: list[str] = []
    for path in _sources(WEB_SRC, ("*.ts", "*.tsx")):
        text = path.read_text(encoding="utf-8")
        if "new WebSocket(" not in text:
            continue
        sockets.append(str(path.relative_to(ROOT)))
        assert "sendWsAuth" in text, f"{path} 建立 WebSocket 但未发送认证帧"
    assert len(sockets) == 3, f"WebSocket 入口数量变化，请复核认证覆盖: {sockets}"


def test_app_gates_private_pages_behind_login() -> None:
    text = (WEB_SRC / "App.tsx").read_text(encoding="utf-8")
    assert "AuthProvider" in text and "useAuth" in text
    assert 'lazy(() => import("./pages/LoginPage"))' in text
    assert 'phase === "anonymous"' in text
    assert "${BASE_PATH}login" in text
    # 登录成功必须回到站内原路径，且只接受站内地址
    assert "safeInternalPath" in text
    assert "退出登录" in text


def test_login_page_supports_setup_and_login_modes() -> None:
    text = (WEB_SRC / "pages" / "LoginPage.tsx").read_text(encoding="utf-8")
    for marker in ("个人 Agent OS", "确认密码", "显示密码", "创建所有者账号", "initialized"):
        assert marker in text, f"登录页缺少 {marker}"
    # 不提供第三方登录或找回密码等超出范围的入口
    assert "OAuth" not in text and "忘记密码" not in text


def test_no_access_token_in_urls_or_query_strings() -> None:
    """Token 只能走 Authorization 头或 WebSocket 首帧，不得出现在 URL 里。"""
    patterns = ("access_token=", "token=")
    for base in (WEB_SRC, ANDROID_SRC, DESKTOP_SRC):
        for path in _sources(base, ("*.ts", "*.tsx", "*.mjs", "*.cjs")):
            text = path.read_text(encoding="utf-8")
            for pattern in patterns:
                assert pattern not in text, f"{path} 把 Token 放进了 URL 参数"
            # WebSocket 地址不得拼接 token
            for line in text.splitlines():
                if "new WebSocket(" in line:
                    assert "token" not in line.lower(), f"{path} 在 WebSocket URL 中拼接了 token"


def test_android_requests_use_shared_auth_client() -> None:
    offenders: list[str] = []
    for path in _sources(ANDROID_SRC, ("*.ts", "*.tsx")):
        if path.name == "auth.ts":
            continue  # auth.ts 是 Android 侧唯一允许直接 fetch 的模块
        text = path.read_text(encoding="utf-8")
        if RAW_FETCH.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"Android 绕过统一客户端: {offenders}"

    auth = (ANDROID_SRC / "api" / "auth.ts").read_text(encoding="utf-8")
    assert "Authorization" in auth and "Bearer" in auth
    assert "localStorage" in auth
    assert "auth/status" in auth and "auth/register" in auth and "auth/login" in auth
    # APK 内不得硬编码服务器令牌或签名密钥
    assert "AIIVE_AUTH_SECRET" not in auth
    assert "dev-token" not in auth


def test_android_app_shows_login_before_chat() -> None:
    text = (ANDROID_SRC / "App.tsx").read_text(encoding="utf-8")
    assert "LoginScreen" in text
    assert "AUTH_EXPIRED_EVENT" in text
    assert 'phase === "anonymous"' in text


def test_desktop_node_host_sends_user_token_and_waits_for_it() -> None:
    client = (DESKTOP_SRC / "node-host" / "client.mjs").read_text(encoding="utf-8")
    assert "access_token: accessToken" in client
    assert "authRequired && !accessToken" in client
    # 令牌不得写进日志
    for line in client.splitlines():
        if "console." in line:
            assert "accessToken" not in line, "Node Host 日志中出现了令牌"

    preload = (DESKTOP_SRC / "preload" / "index.cjs").read_text(encoding="utf-8")
    assert "setAuthToken" in preload

    main = (DESKTOP_SRC / "main" / "index.mjs").read_text(encoding="utf-8")
    assert "aiive-desktop:set-auth-token" in main
    assert "AIIVE_AUTH_ACCESS_TOKEN: authToken" in main
    for line in main.splitlines():
        if "console." in line:
            assert "authToken" not in line, "主进程日志中出现了令牌"


def test_backend_ws_routes_authenticate_before_business_data() -> None:
    for relative in (
        "backend/aiive/api/routes_ws.py",
        "backend/aiive/api/routes_agent_tasks.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "authenticate_websocket" in text, f"{relative} 未做 WebSocket 首帧认证"
        accept = text.index("await ws.accept()") if "await ws.accept()" in text else text.index("await websocket.accept()")
        # 只看调用点（带括号），import 行不算
        auth = text.index("authenticate_websocket(")
        assert accept < auth, f"{relative} 认证顺序异常"

    desktop = (ROOT / "backend/aiive/api/routes_desktop.py").read_text(encoding="utf-8")
    assert "decode_access_token(hello.access_token)" in desktop
