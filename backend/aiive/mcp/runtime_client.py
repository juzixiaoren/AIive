"""
MCP 运行时客户端：真实 stdio 子进程 + 官方 MCP Python SDK 协议握手。

架构：
- 项目主体是同步代码，本模块提供同步封装：
  模块级后台事件循环线程（threading.Thread + asyncio 事件循环）承载所有
  async MCP 会话；对外公开同步方法 list_tools / call_tool。
- 每个能力（capability）对应一个 _SessionWorker：事件循环内的长驻 task，
  持有 stdio_client + ClientSession 的生命周期（anyio cancel scope 必须在
  同一个 task 内进入/退出，因此所有协议请求经 asyncio.Queue 提交给该 task）。
- 会话按能力缓存复用；空闲超时（默认 5 分钟）自动关闭子进程；
  进程退出 / broken pipe 时自动重启一次。

安全约束：
- 启动命令严格由安装记录（Capability.definition["launch"]）的 bin 入口拼装
  （node <entry_js> <args...>），不接受调用方传入的命令字符串。
- MCP 工具输出始终视为不信任内容（tool_result_is_untrusted）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 会话空闲超时（秒）：超过后自动关闭子进程
DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0
# 会话启动（子进程 spawn + initialize 握手）超时（秒）
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60.0
# 单次协议请求默认超时（秒）
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0

try:  # MCP 官方 SDK：延迟失败——导入失败时首次使用会抛出可读错误
    from mcp import ClientSession, StdioServerParameters  # pyright: ignore[reportMissingImports]
    from mcp.client.stdio import get_default_environment, stdio_client  # pyright: ignore[reportMissingImports]
    _mcp_sdk_import_error: Exception | None = None
except Exception as _import_error:  # pragma: no cover - SDK 已在依赖中
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    get_default_environment = None  # type: ignore[assignment]
    _mcp_sdk_import_error = _import_error


def tool_result_is_untrusted() -> bool:
    """MCP 工具输出始终视为不信任内容。"""
    return True


@dataclass
class MCPToolResult:
    """MCP 工具调用结果。

    属性:
        ok: 调用是否成功
        result: 成功时的返回数据（{"content": [...], "structured": ...}）
        error: 失败时的错误信息
        untrusted: MCP 输出始终为不信任内容，恒为 True
    """
    ok: bool
    result: Any = None
    error: str | None = None
    untrusted: bool = True


@dataclass(frozen=True)
class MCPLaunchSpec:
    """MCP server 启动规格（由安装记录拼装，非调用方任意命令）。

    属性:
        command: 可执行文件绝对路径（正常链路恒为 node）
        args: 命令行参数（entry_js 及安装时声明的附加参数）
        env_keys: 必须从宿主进程环境透传的环境变量名
        optional_env_keys: 存在时透传、缺省时不告警的环境变量名
    """
    command: str
    args: tuple[str, ...] = ()
    env_keys: tuple[str, ...] = ()
    optional_env_keys: tuple[str, ...] = ()


def build_launch_spec(launch_config: dict[str, Any]) -> MCPLaunchSpec:
    """从安装记录的 launch 配置构造启动规格。

    只接受 installer 写入的结构化字段（runner=node + entry_js + args），或
    AIive 固定路径的 bundled Python MCP server，
    不接受任意命令字符串。entry_js 必须真实存在。

    参数:
        launch_config: Capability.definition["launch"]

    返回:
        MCPLaunchSpec

    异常:
        ValueError: 配置缺失/入口不存在/runner 不受支持
    """
    runner = str(launch_config.get("runner", "node"))
    args = tuple(str(a) for a in launch_config.get("args", []) or [])
    env_keys = tuple(str(k) for k in launch_config.get("env_keys", []) or [])
    optional_env_keys = tuple(
        str(k) for k in launch_config.get("optional_env_keys", []) or []
    )
    if runner == "python_builtin":
        entry_py = Path(str(launch_config.get("entry_py", ""))).resolve()
        expected = Path(__file__).with_name("builtin_server.py").resolve()
        if entry_py != expected or not entry_py.is_file():
            raise ValueError("bundled MCP entry_py is not the trusted builtin server")
        return MCPLaunchSpec(
            command=sys.executable,
            args=(str(entry_py),) + args,
            env_keys=env_keys,
            optional_env_keys=optional_env_keys,
        )
    if runner != "node":
        raise ValueError(f"unsupported MCP runner: {runner}")
    entry_js = str(launch_config.get("entry_js", "")).strip()
    if not entry_js:
        raise ValueError("launch config missing entry_js")
    entry_path = Path(entry_js)
    if not entry_path.is_file():
        raise ValueError(f"MCP entry_js not found: {entry_js}")
    node_path = shutil.which("node")
    if not node_path:
        raise ValueError("node executable not found in PATH")
    return MCPLaunchSpec(
        command=node_path,
        args=(str(entry_path),) + args,
        env_keys=env_keys,
        optional_env_keys=optional_env_keys,
    )


# ---------------------------------------------------------------------------
# 后台事件循环线程（模块级单例）
# ---------------------------------------------------------------------------

class _LoopThread:
    """承载所有 MCP async 会话的后台事件循环线程。"""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self.thread: threading.Thread = threading.Thread(
            target=self._run, name="mcp-runtime-loop", daemon=True,
        )
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call_soon(self, fn: Any, *args: Any) -> None:
        self.loop.call_soon_threadsafe(fn, *args)


_loop_thread: _LoopThread | None = None
_loop_lock = threading.Lock()


def _get_loop_thread() -> _LoopThread:
    """获取（必要时创建）模块级事件循环线程。"""
    global _loop_thread
    with _loop_lock:
        if _loop_thread is None or not _loop_thread.thread.is_alive():
            _loop_thread = _LoopThread()
        return _loop_thread


# ---------------------------------------------------------------------------
# 会话 worker：一个能力一个长驻 task，持有 stdio 子进程会话
# ---------------------------------------------------------------------------

class _SessionWorker:
    """事件循环内的长驻会话任务。

    anyio 的 cancel scope 要求在同一个 task 内进入/退出上下文管理器，
    因此 stdio_client/ClientSession 的生命周期完全封闭在 _run 协程里，
    同步侧通过队列提交请求（(kind, payload, concurrent.futures.Future)）。
    """

    def __init__(
        self,
        capability_id: str,
        spec: MCPLaunchSpec,
        loop_thread: _LoopThread,
        idle_timeout: float,
    ) -> None:
        self._capability_id: str = capability_id
        self._spec: MCPLaunchSpec = spec
        self._loop: asyncio.AbstractEventLoop = loop_thread.loop
        self._idle_timeout: float = idle_timeout
        self._queue: asyncio.Queue[Any] | None = None
        self._ready: concurrent.futures.Future[bool] = concurrent.futures.Future()
        self._closed: bool = False
        loop_thread.call_soon(self._start_in_loop)

    # -- 事件循环内部 --------------------------------------------------

    def _start_in_loop(self) -> None:
        self._queue = asyncio.Queue()
        self._loop.create_task(self._run(), name=f"mcp-session:{self._capability_id}")

    def _build_env(self) -> dict[str, str]:
        """构造子进程环境：SDK 默认安全环境 + 安装记录声明的透传变量。"""
        if get_default_environment is not None:
            env = dict(get_default_environment())
        else:  # pragma: no cover
            env = {k: v for k, v in os.environ.items() if not k.startswith("_")}
        for key in self._spec.env_keys:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
            else:
                logger.warning(
                    "MCP 能力 %s 声明的环境变量缺失: %s", self._capability_id, key,
                )
        for key in self._spec.optional_env_keys:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        return env

    async def _run(self) -> None:
        assert self._queue is not None
        try:
            if StdioServerParameters is None or stdio_client is None or ClientSession is None:
                raise RuntimeError(f"MCP Python SDK unavailable: {_mcp_sdk_import_error}")
            params = StdioServerParameters(
                command=self._spec.command,
                args=list(self._spec.args),
                env=self._build_env(),
            )
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    if not self._ready.done():
                        self._ready.set_result(True)
                    await self._serve(session)
        except Exception as e:
            if not self._ready.done():
                self._ready.set_exception(e)
            else:
                logger.warning(
                    "MCP 会话异常退出: capability=%s error=%s",
                    self._capability_id, e,
                )
        finally:
            self._closed = True
            self._drain_pending()

    async def _serve(self, session: Any) -> None:
        """处理队列请求直到空闲超时 / 收到停止信号 / 连接层错误。"""
        assert self._queue is not None
        while True:
            try:
                item = await asyncio.wait_for(
                    self._queue.get(), timeout=self._idle_timeout,
                )
            except asyncio.TimeoutError:
                logger.info(
                    "MCP 会话空闲超时，关闭子进程: capability=%s", self._capability_id,
                )
                return
            if item is None:  # 停止信号
                return
            kind, payload, fut = item
            try:
                if kind == "list_tools":
                    result = await session.list_tools()
                elif kind == "call_tool":
                    result = await session.call_tool(
                        payload["name"], payload.get("arguments") or {},
                    )
                else:
                    raise ValueError(f"unknown MCP request kind: {kind}")
                if not fut.cancelled():
                    fut.set_result(result)
            except Exception as e:
                if not fut.cancelled():
                    fut.set_exception(e)
                # 连接层错误：结束会话，交由客户端按需重启
                if isinstance(e, (BrokenPipeError, ConnectionError, EOFError, OSError)):
                    logger.warning(
                        "MCP 会话连接错误，关闭: capability=%s error=%s",
                        self._capability_id, e,
                    )
                    return

    def _drain_pending(self) -> None:
        """会话结束后将排队中的请求标记失败。"""
        if self._queue is None:
            return
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None:
                _, _, fut = item
                if not fut.done():
                    fut.set_exception(
                        RuntimeError(f"MCP session closed: {self._capability_id}")
                    )

    # -- 同步侧 API ----------------------------------------------------

    def wait_ready(self, timeout: float) -> None:
        """等待子进程启动并完成 initialize 握手。"""
        self._ready.result(timeout)

    @property
    def alive(self) -> bool:
        return (
            not self._closed
            and self._ready.done()
            and self._ready.exception() is None
        )

    def submit(self, kind: str, payload: dict[str, Any], timeout: float) -> Any:
        """线程安全地提交一个协议请求并同步等待结果。"""
        fut: concurrent.futures.Future[Any] = concurrent.futures.Future()

        def _enqueue() -> None:
            if self._closed or self._queue is None:
                if not fut.done():
                    fut.set_exception(
                        RuntimeError(f"MCP session closed: {self._capability_id}")
                    )
            else:
                self._queue.put_nowait((kind, payload, fut))

        self._loop.call_soon_threadsafe(_enqueue)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            message = (
                f"MCP request timed out after {timeout:.0f}s: "
                + f"{self._capability_id}/{kind}"
            )
            raise TimeoutError(message) from None

    def stop(self) -> None:
        """请求会话优雅退出（幂等）。"""
        def _enqueue() -> None:
            if self._queue is not None and not self._closed:
                self._queue.put_nowait(None)

        self._loop.call_soon_threadsafe(_enqueue)


# ---------------------------------------------------------------------------
# 同步客户端
# ---------------------------------------------------------------------------

class MCPRuntimeClient:
    """真实 MCP stdio 运行时客户端（同步封装）。

    用法：
        client.configure(capability_id, spec)   # 登记启动规格（惰性启动）
        client.list_tools(capability_id)        # tools/list
        client.call_tool(capability_id, name, args)  # tools/call
    """

    def __init__(self, idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS):
        self._idle_timeout: float = idle_timeout_seconds
        self._specs: dict[str, MCPLaunchSpec] = {}
        self._workers: dict[str, _SessionWorker] = {}
        self._lock: threading.Lock = threading.Lock()

    # -- 配置 -----------------------------------------------------------

    def configure(self, capability_id: str, spec: MCPLaunchSpec) -> None:
        """登记能力的启动规格；子进程在首次请求时惰性启动。"""
        with self._lock:
            self._specs[capability_id] = spec

    def is_configured(self, capability_id: str) -> bool:
        with self._lock:
            return capability_id in self._specs

    # -- 会话管理 -------------------------------------------------------

    def _ensure_worker(
        self, capability_id: str, startup_timeout: float,
    ) -> _SessionWorker:
        with self._lock:
            worker = self._workers.get(capability_id)
            if worker is not None and worker.alive:
                return worker
            spec = self._specs.get(capability_id)
            if spec is None:
                raise RuntimeError(
                    f"MCP capability not configured: {capability_id}"
                )
            if _mcp_sdk_import_error is not None:  # pragma: no cover
                raise RuntimeError(
                    f"MCP Python SDK unavailable: {_mcp_sdk_import_error}"
                )
            worker = _SessionWorker(
                capability_id, spec, _get_loop_thread(), self._idle_timeout,
            )
            self._workers[capability_id] = worker
        worker.wait_ready(startup_timeout)
        return worker

    def _drop_worker(self, capability_id: str, worker: _SessionWorker) -> None:
        with self._lock:
            if self._workers.get(capability_id) is worker:
                del self._workers[capability_id]
        worker.stop()

    def _submit_with_restart(
        self,
        capability_id: str,
        kind: str,
        payload: dict[str, Any],
        timeout: float,
        startup_timeout: float,
    ) -> Any:
        """提交请求；会话已死 / 管道断裂时自动重启一次后重试。"""
        worker = self._ensure_worker(capability_id, startup_timeout)
        try:
            return worker.submit(kind, payload, timeout)
        except (RuntimeError, BrokenPipeError, ConnectionError, EOFError) as e:
            logger.warning(
                "MCP 请求失败，尝试重启会话一次: capability=%s error=%s",
                capability_id, e,
            )
            self._drop_worker(capability_id, worker)
            worker = self._ensure_worker(capability_id, startup_timeout)
            return worker.submit(kind, payload, timeout)

    # -- 协议操作（同步） -----------------------------------------------

    def list_tools(
        self,
        capability_id: str,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> list[dict[str, Any]]:
        """真实 tools/list：返回 [{"name", "description", "input_schema"}]。"""
        result = self._submit_with_restart(
            capability_id, "list_tools", {}, timeout, startup_timeout,
        )
        tools: list[dict[str, Any]] = []
        for tool in result.tools:
            tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema or {},
            })
        return tools

    def call_tool(
        self,
        capability_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> MCPToolResult:
        """真实 tools/call：结果始终带 untrusted=True 标记。"""
        try:
            raw = self._submit_with_restart(
                capability_id, "call_tool",
                {"name": tool_name, "arguments": arguments or {}},
                timeout, startup_timeout,
            )
        except Exception as e:
            logger.warning(
                "MCP 工具调用失败: capability=%s tool=%s error=%s",
                capability_id, tool_name, e,
            )
            return MCPToolResult(ok=False, error=str(e), untrusted=True)
        return _convert_call_result(raw)

    # -- 生命周期 -------------------------------------------------------

    def close(self, capability_id: str | None = None) -> None:
        """关闭指定能力的会话；capability_id 为 None 时关闭全部。"""
        with self._lock:
            if capability_id is None:
                workers = list(self._workers.items())
                self._workers.clear()
            else:
                worker = self._workers.pop(capability_id, None)
                workers = [(capability_id, worker)] if worker else []
        for _, worker in workers:
            worker.stop()

    def tool_result_is_untrusted(self) -> bool:
        """检查工具结果是否来自不信任来源。
        MCP 输出始终被视为不信任内容。

        返回:
            始终为 True
        """
        return True


def _convert_call_result(raw: Any) -> MCPToolResult:
    """把 SDK 的 CallToolResult 转换为 MCPToolResult。"""
    content_parts: list[str] = []
    for item in getattr(raw, "content", None) or []:
        text = getattr(item, "text", None)
        content_parts.append(text if text is not None else str(item))
    payload: dict[str, Any] = {"content": content_parts}
    structured = getattr(raw, "structuredContent", None)
    if structured is not None:
        payload["structured"] = structured
    if bool(getattr(raw, "isError", False)):
        return MCPToolResult(
            ok=False,
            error="\n".join(content_parts) or "MCP tool returned isError",
            untrusted=True,
        )
    return MCPToolResult(ok=True, result=payload, untrusted=True)


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

_runtime_client: MCPRuntimeClient | None = None
_runtime_client_lock = threading.Lock()


def get_runtime_client() -> MCPRuntimeClient:
    """获取进程级 MCP 运行时客户端单例。"""
    global _runtime_client
    with _runtime_client_lock:
        if _runtime_client is None:
            _runtime_client = MCPRuntimeClient()
        return _runtime_client
