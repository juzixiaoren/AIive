"""
工具注册表：管理所有工具（能力）的注册、查询和执行。
提供全局单例，所有内置工具通过 register_builtin_tools 注册。

核心组件:
- CapabilitySafetySchema: 工具的安全元数据（风险等级、来源、权限等）
- ToolRegistration: 工具注册条目，包含安全配置和处理函数
- ToolRegistry: 工具注册表，管理注册、查询、schema 渲染和执行
"""

import concurrent.futures
import hashlib
import inspect
import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from aiive.config import settings
from aiive.context.run_context import RunContext

logger = logging.getLogger(__name__)

# 所有工具的默认超时时间（秒）：来自配置，未显式声明 timeout 的工具使用此值
DEFAULT_TOOL_TIMEOUT = settings.tool_default_timeout_seconds

# 模块级线程池：复用避免重复创建开销，daemon 线程不阻止进程退出。
# 池大小来自配置：超时后子线程不被 cancel（见 execute 说明），
# 阻塞 I/O 型工具接连超时可能占满此池，可按部署环境调大。
_tool_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=settings.tool_executor_max_workers,
    thread_name_prefix="tool-worker",
)


@dataclass(frozen=True)
class CapabilitySafetySchema:
    """工具能力的安全配置 schema（不可变）。

    属性:
        capability_id: 工具唯一标识符，如 "echo"、"schedule_reminder"
        definition_source: 工具来源（local_builtin / generated_by_agent / remote_mcp / user_installed）
        definition_trust_level: 信任等级（trusted / semi_trusted / untrusted）
        risk_level: 风险等级（low / medium / high / critical）
        requires_confirmation: 是否需要用户确认
        writes_external_world: 是否会写入外部世界（文件系统、数据库等）
        can_access_secret: 是否可访问密钥
        can_delete: 是否可删除数据
        uses_network: 是否访问外部网络
        allowed_instruction_sources: 允许的指令来源列表
        descriptor_hash: 安全描述的 SHA256 哈希（用于变更检测）
        tool_description_is_instruction: 工具描述是否可作为指令
        timeout_seconds: float = 0  # 工具执行超时（秒），0 时使用全局默认 DEFAULT_TOOL_TIMEOUT
    """
    capability_id: str
    definition_source: str  # 来源：local_builtin | generated_by_agent | remote_mcp | user_installed
    definition_trust_level: str  # 信任等级：trusted | semi_trusted | untrusted
    risk_level: str  # 风险等级：low | medium | high | critical
    requires_confirmation: bool = False
    writes_external_world: bool = False
    can_access_secret: bool = False
    can_delete: bool = False
    uses_network: bool = False
    allowed_instruction_sources: list[str] = field(default_factory=lambda: ["trusted_user_command"])
    descriptor_hash: str = ""
    tool_description_is_instruction: bool = False
    timeout_seconds: float = 0
    effect_mode: str = "db_transactional"


def compute_descriptor_hash(schema: dict[str, Any]) -> str:
    """计算安全 schema 的 SHA256 哈希值（取前 16 位）。

    用于检测工具定义是否发生变化。

    参数:
        schema: 安全配置字典

    返回:
        16 位十六进制哈希字符串
    """
    raw = json.dumps(schema, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ToolRegistration:
    """工具注册条目，将安全配置与处理函数绑定。

    属性:
        safety: 安全配置 schema
        handler: 工具处理函数（可调用对象）
        description: 工具功能描述
        parameters: 参数定义，格式为 {参数名: 类型字符串}
    """
    safety: CapabilitySafetySchema
    handler: Callable[..., Any]
    description: str = ""
    # None 表示旧式无显式契约（兼容测试/扩展）；{} 表示明确不接受业务参数。
    parameters: dict[str, Any] | None = None
    # MCP 工具保留服务端原始 inputSchema，仅用于本地执行前校验。
    input_schema: dict[str, Any] | None = None
    # local 为后端进程内 handler；desktop_node 为绑定到单个在线 Electron 节点的
    # 远端 handler。后者由 Desktop WebSocket 调度，不进入服务端 Outbox 执行器。
    executor_kind: str = "local"
    executor_node_id: str = ""


_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")


def _search_tokens(query: str) -> list[str]:
    """把查询拆成可匹配词元；中文长词额外展开为二元组，提高召回。"""
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", query.casefold()).strip()
    if not normalized:
        return []
    tokens: list[str] = []
    for part in normalized.split():
        if len(part) > 2 and _CJK_PATTERN.search(part):
            tokens.extend(part[index:index + 2] for index in range(len(part) - 1))
        tokens.append(part)
    unique: list[str] = []
    for token in tokens:
        if token not in unique:
            unique.append(token)
    return unique


def _parameter_names(reg: ToolRegistration) -> list[str]:
    """收集参数名，供检索命中参数维度（不返回任何 Schema 内容）。"""
    names: list[str] = []
    if isinstance(reg.parameters, dict):
        names.extend(str(name) for name in reg.parameters)
    schema = reg.input_schema
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        names.extend(str(name) for name in schema["properties"])
    return names


def _score_registration(reg: ToolRegistration, tokens: list[str]) -> int:
    """确定性的本地相关性打分：id > 描述 > 参数名。"""
    capability_id = reg.safety.capability_id.casefold()
    description = (reg.description or "").casefold()
    parameter_names = " ".join(_parameter_names(reg)).casefold()
    score = 0
    for token in tokens:
        if token == capability_id:
            score += 60
        elif token in capability_id:
            score += 24
        position = description.find(token)
        if position >= 0:
            # 描述开头通常就是该能力的主语，位置越靠前越可能是主题词。
            score += 10 + (8 if position == 0 else 4 if position < 20 else 0)
        if token in parameter_names:
            score += 4
    return score

# ---------------------------------------------------------------------------
# 工具调用辅助函数（在独立线程中执行，支持超时中断）
# ---------------------------------------------------------------------------

def _invoke_handler(
    handler: Callable[..., Any],
    has_ctx: bool,
    run_context: RunContext | None,
    params: dict[str, Any],
) -> Any:
    """在独立线程中调用工具 handler，隔离超时控制。

    Args:
        handler: 工具处理函数
        has_ctx: handler 是否接受 ctx 参数
        run_context: 运行时上下文
        params: 工具调用参数

    Returns:
        handler 的返回值
    """
    if has_ctx:
        return handler(ctx=run_context, **params)
    return handler(**params)


class ToolRegistry:
    """工具注册表：管理所有工具能力的注册、查询和执行。

    功能：
    - register: 注册新工具
    - get: 按 capability_id 查询工具
    - list_all: 列出所有已注册工具
    - render_tool_schemas: 生成注入 system prompt 的工具描述文本
    - execute: 执行工具调用（含安全守卫）
    """

    def __init__(self):
        """初始化空的工具注册表。"""
        self._tools: dict[str, ToolRegistration] = {}

    def register(self, reg: ToolRegistration) -> None:
        """注册工具，并为缺失的安全描述指纹生成稳定值。

        参数:
            reg: 工具注册条目
        """
        if not reg.safety.descriptor_hash:
            safety_payload = {
                "capability_id": reg.safety.capability_id,
                "definition_source": reg.safety.definition_source,
                "definition_trust_level": reg.safety.definition_trust_level,
                "risk_level": reg.safety.risk_level,
                "requires_confirmation": reg.safety.requires_confirmation,
                "writes_external_world": reg.safety.writes_external_world,
                "can_access_secret": reg.safety.can_access_secret,
                "can_delete": reg.safety.can_delete,
                "uses_network": reg.safety.uses_network,
                "allowed_instruction_sources": reg.safety.allowed_instruction_sources,
                "tool_description_is_instruction": reg.safety.tool_description_is_instruction,
                "timeout_seconds": reg.safety.timeout_seconds,
                "effect_mode": reg.safety.effect_mode,
                "description": reg.description,
                "parameters": reg.parameters,
                "input_schema": reg.input_schema,
                "executor_kind": reg.executor_kind,
                "executor_node_id": reg.executor_node_id,
            }
            reg = replace(
                reg,
                safety=replace(
                    reg.safety,
                    descriptor_hash=compute_descriptor_hash(safety_payload),
                ),
            )
        self._tools[reg.safety.capability_id] = reg

    def get(self, capability_id: str) -> ToolRegistration | None:
        """按 ID 查询已注册工具。

        参数:
            capability_id: 工具能力标识符

        返回:
            ToolRegistration 或 None
        """
        return self._tools.get(capability_id)

    def snapshot(self) -> "ToolRegistry":
        """复制当前注册映射，供单个 Turn 叠加动态节点工具。

        ToolRegistration 与安全 schema 均视为不可变定义，浅复制映射即可；后续在
        快照注册或覆盖工具不会影响进程级基础注册表。
        """
        cloned = ToolRegistry()
        cloned._tools = dict(self._tools)
        return cloned

    def scoped(self, capability_ids: set[str] | frozenset[str]) -> "ToolRegistry":
        """返回仅含明确 capability allowlist 的不可扩张快照。"""
        cloned = ToolRegistry()
        cloned._tools = {
            capability_id: registration
            for capability_id, registration in self._tools.items()
            if capability_id in capability_ids
        }
        return cloned

    def list_all(self) -> list[dict[str, Any]]:
        """列出所有已注册工具的完整摘要（管理 API / 诊断专用）。

        警告：返回值包含全部 parameters / input_schema，禁止整段注入模型
        上下文；模型可见的工具面必须走 catalog / search / definition。

        返回:
            工具信息字典列表，每项包含 capability_id、description、风险等级等
        """
        return [
            {
                "capability_id": r.safety.capability_id,
                "description": r.description,
                "definition_source": r.safety.definition_source,
                "definition_trust_level": r.safety.definition_trust_level,
                "risk_level": r.safety.risk_level,
                "requires_confirmation": r.safety.requires_confirmation,
                "writes_external_world": r.safety.writes_external_world,
                "can_access_secret": r.safety.can_access_secret,
                "can_delete": r.safety.can_delete,
                "uses_network": r.safety.uses_network,
                "descriptor_hash": r.safety.descriptor_hash,
                "executor_kind": r.executor_kind,
                "executor_node_id": r.executor_node_id,
                "parameters": r.parameters,
                "input_schema": r.input_schema,
            }
            for r in self._tools.values()
        ]

    def unregister(self, capability_id: str) -> bool:
        """移除一个能力（如 MCP server 卸载、桌面节点离线）。

        目录与检索每次都读取实时注册映射，不维护独立缓存，因此卸载后不会再
        被搜索或注入。返回是否确实移除了条目。
        """
        return self._tools.pop(capability_id, None) is not None

    def catalog(self) -> list[dict[str, Any]]:
        """轻量能力目录：只含选择所需的最小元数据，绝不含参数 Schema。

        这是模型可见工具列表的唯一来源。与 list_all 的区别是刻意不返回
        parameters / input_schema，避免"查目录"变成"注入全量 Schema"。
        需要完整定义时用 definition。
        """
        return [self._catalog_entry(reg) for reg in self._tools.values()]

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """本地关键词检索能力目录（确定性、无外部服务、无向量库）。

        打分规则固定：capability_id 命中权重最高，其次描述，最后参数名。
        相同分数按 capability_id 排序，保证同一注册表状态下结果完全可复现。
        每次调用都基于当前注册映射，注册/卸载/重注册后立即可见。
        """
        tokens = _search_tokens(query)
        if not tokens or limit <= 0:
            return []
        scored: list[tuple[int, str, ToolRegistration]] = []
        for reg in self._tools.values():
            score = _score_registration(reg, tokens)
            if score > 0:
                scored.append((score, reg.safety.capability_id, reg))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            {**self._catalog_entry(reg), "score": score}
            for score, _, reg in scored[:limit]
        ]

    def definition(self, capability_id: str) -> dict[str, Any] | None:
        """单个能力的完整定义（含参数 Schema），供按需加载使用。

        返回的 descriptor_hash 是注册时的定义指纹；调用方应保存它，并在
        下一次使用前与最新值比较，避免沿用已变更的旧 Schema。
        """
        reg = self._tools.get(capability_id)
        if reg is None:
            return None
        return {
            **self._catalog_entry(reg),
            "definition_trust_level": reg.safety.definition_trust_level,
            "timeout_seconds": reg.safety.timeout_seconds,
            "parameters": reg.parameters,
            "input_schema": reg.input_schema,
        }

    @staticmethod
    def _catalog_entry(reg: ToolRegistration) -> dict[str, Any]:
        """目录条目的唯一定义处，避免各查询接口字段漂移。"""
        return {
            "capability_id": reg.safety.capability_id,
            "description": reg.description,
            "definition_source": reg.safety.definition_source,
            "risk_level": reg.safety.risk_level,
            "requires_confirmation": reg.safety.requires_confirmation,
            "writes_external_world": reg.safety.writes_external_world,
            "can_access_secret": reg.safety.can_access_secret,
            "can_delete": reg.safety.can_delete,
            "uses_network": reg.safety.uses_network,
            "descriptor_hash": reg.safety.descriptor_hash,
            "executor_kind": reg.executor_kind,
            "executor_node_id": reg.executor_node_id,
        }

    def execute(
        self,
        capability_id: str,
        params: dict[str, Any],
        instruction_source: str,
        run_context: RunContext | None = None,
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        """执行工具调用，保留来源授权、执行身份和超时守卫。

        AgentGraph 在进入此方法前按安全元数据完成阻止或审批分流。这里继续强制
        指令来源授权；本地副作用工具还需持久化执行身份并进入 operation 执行链，
        Desktop Node 工具则由节点 WebSocket 代理执行。

        Args:
            capability_id: 工具能力标识符
            params: 调用参数
            instruction_source: 指令来源（如 trusted_user_command）
            run_context: 运行时上下文（RunContext），注入到 handler 的 ctx 参数

        Returns:
            包含 ok、result（或 error/timeout）的字典
        """
        logger.info("[TRACE:registry] EXECUTE tool=%s params=%s source=%s", capability_id, params, instruction_source)
        reg = self.get(capability_id)
        if not reg:
            logger.warning("[TRACE:registry] UNKNOWN tool=%s", capability_id)
            return {"ok": False, "error": f"Unknown tool: {capability_id}"}

        if instruction_source not in reg.safety.allowed_instruction_sources:
            logger.warning("[TRACE:registry] UNAUTHORIZED tool=%s source=%s", capability_id, instruction_source)
            return {
                "ok": False,
                "error": "Instruction source not authorized for this tool",
                "source": instruction_source,
                "allowed": reg.safety.allowed_instruction_sources,
            }

        from aiive.tools.tool_validation import validate_tool_params

        validation = validate_tool_params(reg, params)
        if not validation.ok:
            logger.warning(
                "[TRACE:registry] INVALID_ARGS tool=%s issues=%s",
                capability_id,
                [issue.as_dict() for issue in validation.issues],
            )
            return validation.error_payload(capability_id)
        params = validation.validated_params

        timeout = reg.safety.timeout_seconds or DEFAULT_TOOL_TIMEOUT
        if reg.executor_kind == "desktop_node":
            return self._execute_direct(reg, params, run_context, timeout, capability_id)

        if reg.safety.writes_external_world or reg.safety.can_delete:
            if run_context is None or not run_context.turn_record_id or not tool_call_id:
                return {
                    "ok": False,
                    "error_type": "missing_execution_identity",
                    "error": "副作用工具缺少持久化执行身份，已拒绝执行",
                }
            from aiive.tools.operation_executor import enqueue_tool_operation, wait_for_tool_operation

            operation = enqueue_tool_operation(
                capability_id=capability_id,
                params=params,
                descriptor_hash=reg.safety.descriptor_hash,
                effect_mode=reg.safety.effect_mode,
                run_context=run_context,
                tool_call_id=tool_call_id,
            )
            return wait_for_tool_operation(operation.id, timeout)

        return self._execute_direct(reg, params, run_context, timeout, capability_id)

    @staticmethod
    def _execute_direct(
        reg: ToolRegistration,
        params: dict[str, Any],
        run_context: RunContext | None,
        timeout: float,
        capability_id: str,
    ) -> dict[str, Any]:
        """在线程池执行本地或 Desktop WebSocket 代理 handler。"""
        has_ctx = "ctx" in inspect.signature(reg.handler).parameters
        future = _tool_executor.submit(_invoke_handler, reg.handler, has_ctx, run_context, params)
        try:
            result = future.result(timeout=timeout)
            logger.info("[TRACE:registry] SUCCESS tool=%s result_type=%s", capability_id, type(result).__name__)
            return {"ok": True, "result": result}
        except concurrent.futures.TimeoutError:
            # 关键：超时后不 cancel + wait，直接返回错误。
            # 子线程会继续运行直到自行结束（DB I/O 完成后自然退出）。
            # 如果 cancel 并 wait，会因为 DB 阻塞 I/O 无法中断而死锁。
            logger.error(
                "[TRACE:registry] TIMEOUT tool=%s after %.0fs params=%s",
                capability_id, timeout, params,
            )
            remote_unknown = reg.executor_kind == "desktop_node"
            return {
                "ok": False,
                "error": (
                    f"Desktop tool '{capability_id}' timed out after {timeout:.0f}s; final state is unknown"
                    if remote_unknown
                    else f"Tool '{capability_id}' timed out after {timeout:.0f}s"
                ),
                "error_type": "execution_unknown" if remote_unknown else "timeout",
                "execution_status": "execution_unknown" if remote_unknown else "failed",
                "timeout_seconds": timeout,
            }
        except Exception as e:
            logger.exception("[TRACE:registry] FAILED tool=%s error=%s", capability_id, e)
            if reg.executor_kind == "desktop_node" and str(e) in {
                "desktop_node_disconnected",
                "desktop_operation_timeout",
            }:
                return {
                    "ok": False,
                    "error": str(e),
                    "error_type": "execution_unknown",
                    "execution_status": "execution_unknown",
                }
            return {"ok": False, "error": str(e), "error_type": "execution_failed"}

    def execute_approved(
        self,
        capability_id: str,
        params: dict[str, Any],
        expected_descriptor_hash: str,
        run_context: RunContext,
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        """执行已经由服务端审批记录授权的工具调用。

        该入口只跳过重复确认守卫；工具存在性、原始指令来源授权、工具定义
        指纹和超时保护仍然强制校验。调用方必须先原子领取持久化审批记录。
        """
        reg = self.get(capability_id)
        if reg is None:
            return {"ok": False, "error": f"未知工具: {capability_id}", "error_type": "unknown_tool"}
        if "trusted_user_command" not in reg.safety.allowed_instruction_sources:
            return {
                "ok": False,
                "error": "原始用户指令无权调用该工具",
                "error_type": "unauthorized_source",
            }
        if not reg.safety.descriptor_hash or not expected_descriptor_hash:
            return {
                "ok": False,
                "error": "工具审批缺少有效的定义指纹",
                "error_type": "missing_descriptor_hash",
            }
        if reg.safety.descriptor_hash != expected_descriptor_hash:
            return {
                "ok": False,
                "error": "工具定义在审批后发生变化，原审批已失效",
                "error_type": "descriptor_changed",
            }

        from aiive.tools.tool_validation import validate_tool_params

        validation = validate_tool_params(reg, params)
        if not validation.ok:
            return validation.error_payload(capability_id)
        params = validation.validated_params

        logger.info("[TRACE:registry] EXECUTE_APPROVED tool=%s", capability_id)
        timeout = reg.safety.timeout_seconds or DEFAULT_TOOL_TIMEOUT
        if reg.executor_kind == "desktop_node":
            return self._execute_direct(reg, params, run_context, timeout, capability_id)

        if reg.safety.writes_external_world or reg.safety.can_delete:
            if not run_context.turn_record_id or not tool_call_id:
                return {
                    "ok": False,
                    "error_type": "missing_execution_identity",
                    "error": "审批副作用缺少持久化执行身份，已拒绝执行",
                }
            from aiive.tools.operation_executor import enqueue_tool_operation, wait_for_tool_operation

            operation = enqueue_tool_operation(
                capability_id=capability_id,
                params=params,
                descriptor_hash=reg.safety.descriptor_hash,
                effect_mode=reg.safety.effect_mode,
                run_context=run_context,
                tool_call_id=tool_call_id,
            )
            return wait_for_tool_operation(operation.id, timeout)

        return self._execute_direct(reg, params, run_context, timeout, capability_id)

    def execute_brokered(
        self,
        capability_id: str,
        params: dict[str, Any],
        expected_descriptor_hash: str,
        run_context: RunContext,
    ) -> dict[str, Any]:
        """执行已经由 CapabilityBroker 完成 scope/policy 校验的 Task Action。

        此入口不接受模型直接调用，也不复用 Turn-bound ToolOperation 身份；Task
        Action 自身就是持久化幂等单元。仍强制工具定义指纹和参数 schema，防止
        审批后工具定义变化或参数漂移。
        """
        reg = self.get(capability_id)
        if reg is None:
            return {"ok": False, "error": "unknown_capability", "error_type": "unknown_tool"}
        if not expected_descriptor_hash or reg.safety.descriptor_hash != expected_descriptor_hash:
            return {"ok": False, "error": "descriptor_changed", "error_type": "descriptor_changed"}
        from aiive.tools.tool_validation import validate_tool_params

        validation = validate_tool_params(reg, params)
        if not validation.ok:
            return validation.error_payload(capability_id)
        timeout = reg.safety.timeout_seconds or DEFAULT_TOOL_TIMEOUT
        return self._execute_direct(
            reg, validation.validated_params, run_context, timeout, capability_id,
        )


# 全局单例
_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """获取全局工具注册表单例。

    首次调用时自动初始化并注册所有内置工具。

    返回:
        ToolRegistry 全局单例
    """
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        _register_builtins(_registry)
    return _registry


def _register_builtins(registry: ToolRegistry) -> None:
    """向注册表中注册所有内置工具。

    参数:
        registry: 目标注册表实例
    """
    from aiive.tools.builtin_tools import register_builtin_tools

    register_builtin_tools(registry)
