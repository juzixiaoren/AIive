"""
工具注册表：管理所有工具（能力）的注册、查询和执行。
提供全局单例，所有内置工具通过 register_builtin_tools 注册。

核心组件:
- CapabilitySafetySchema: 工具的安全元数据（风险等级、来源、权限等）
- BaseTool: 无执行状态的工具类实例
- ToolRegistry: 工具注册表，管理注册、查询、schema 渲染和执行
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any

from aiive.tools.base import BaseTool, CancellationPolicy

from aiive.config import settings
from aiive.context.run_context import RunContext
from aiive.tool_discovery.documents import DiscoveryMetadata, tool_document
from aiive.tool_discovery.index import DiscoveryIndex

logger = logging.getLogger(__name__)

# 所有工具的默认超时时间（秒）：来自配置，未显式声明 timeout 的工具使用此值
DEFAULT_TOOL_TIMEOUT = settings.tool_default_timeout_seconds

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


@dataclass(frozen=True)
class ToolDefinition:
    """工具的完整有效定义；注册表不保存可调用 Handler。

    属性:
        safety: 安全配置 schema
        description: 工具功能描述
        parameters: 参数定义，格式为 {参数名: 类型字符串}
    """
    safety: CapabilitySafetySchema
    description: str = ""
    # None 从工具业务方法派生参数模型；{} 明确不接受业务参数。
    parameters: dict[str, Any] | None = None
    # 显式 input_schema 优先于 parameters；注册后保存有效 Canonical Schema。
    input_schema: dict[str, Any] | None = None
    # local 为后端进程内 handler；desktop_node 为绑定到单个在线 Electron 节点的
    # 远端 handler。后者由 Desktop WebSocket 调度，不进入服务端 Outbox 执行器。
    executor_kind: str = "local"
    executor_node_id: str = ""
    cancellation_policy: CancellationPolicy = CancellationPolicy.NONE
    idempotent: bool = False
    supports_reconciliation: bool = False
    discovery: DiscoveryMetadata = field(default_factory=DiscoveryMetadata)
    discovery_hash: str = ""


def canonical_schema(reg: BaseTool) -> dict[str, Any]:
    from aiive.tools.tool_validation import canonical_tool_schema
    return canonical_tool_schema(reg)


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
        self._tools: dict[str, BaseTool] = {}
        self._index: DiscoveryIndex | None = DiscoveryIndex()
        self._lock: RLock = RLock()

    def register(self, reg: object) -> None:
        """注册工具，保存规范契约并重新计算完整定义指纹。

        参数:
            reg: 工具注册条目
        """
        if not isinstance(reg, BaseTool):
            raise TypeError("ToolRegistry only accepts BaseTool instances")
        reg = reg.with_definition(replace(reg.definition, safety=replace(reg.safety, timeout_seconds=reg.safety.timeout_seconds or DEFAULT_TOOL_TIMEOUT)))
        from aiive.tools.tool_validation import canonical_tool_schema
        effective_schema = canonical_tool_schema(reg)
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
            "input_schema": effective_schema,
            "executor_kind": reg.executor_kind,
            "executor_node_id": reg.executor_node_id,
            "cancellation_policy": reg.definition.cancellation_policy,
            "idempotent": reg.definition.idempotent,
            "supports_reconciliation": reg.definition.supports_reconciliation,
        }
        reg = reg.with_definition(replace(
            reg.definition,
            input_schema=effective_schema,
            safety=replace(
                reg.safety,
                descriptor_hash=compute_descriptor_hash(safety_payload),
            ),
        ))
        metadata, document = tool_document(reg)
        reg = reg.with_definition(replace(reg.definition, discovery=metadata, discovery_hash=document.discovery_hash))
        capability_id = reg.safety.capability_id
        with self._lock:
            if self._index is not None:
                self._index = self._index.updated(capability_id, document, metadata)
            self._tools = {**self._tools, capability_id: reg}

    def discovery_index(self) -> DiscoveryIndex:
        """返回当前版本的索引。执行专用裁剪视图仅在实际检索时构建索引。"""
        with self._lock:
            if self._index is None:
                index = DiscoveryIndex()
                for reg in self._tools.values():
                    metadata, document = tool_document(reg)
                    index = index.updated(reg.safety.capability_id, document, metadata)
                self._index = index
            return self._index

    def get(self, capability_id: str) -> BaseTool | None:
        """按 ID 查询已注册工具。

        参数:
            capability_id: 工具能力标识符

        返回:
            BaseTool 或 None
        """
        return self._tools.get(capability_id)

    def registrations(self) -> list[BaseTool]:
        """按注册顺序返回全部注册项（只读）。

        供 Schema 导出与 LangChain 绑定共用同一份完整契约，避免再从
        list_all() 的摘要字典反查注册项。
        """
        return list(self._tools.values())

    def snapshot(self) -> "ToolRegistry":
        """复制当前注册映射，供单个 Turn 叠加动态节点工具。

        BaseTool 与安全 schema 均视为不可变定义，浅复制映射即可；后续在
        快照注册或覆盖工具不会影响进程级基础注册表。
        """
        cloned = ToolRegistry()
        with self._lock:
            cloned._tools = self._tools
            cloned._index = self._index
        return cloned

    def scoped(self, capability_ids: set[str] | frozenset[str]) -> "ToolRegistry":
        """返回仅含明确 capability allowlist 的不可扩张快照。"""
        cloned = ToolRegistry()
        cloned._tools = {
            capability_id: registration
            for capability_id, registration in self._tools.items()
            if capability_id in capability_ids
        }
        cloned._index = None
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
                "parameters": canonical_schema(r).get("properties", {}),
                "input_schema": canonical_schema(r),
            }
            for r in self._tools.values()
        ]

    def unregister(self, capability_id: str) -> bool:
        """移除一个能力（如 MCP server 卸载、桌面节点离线）。

        注册映射与索引一起发布，已取得的快照继续使用各自版本。
        """
        with self._lock:
            if capability_id not in self._tools:
                return False
            tools = self._tools.copy()
            del tools[capability_id]
            if self._index is not None:
                self._index = self._index.updated(capability_id, None, None)
            self._tools = tools
            return True

    def catalog(self) -> list[dict[str, Any]]:
        """轻量能力目录：只含选择所需的最小元数据，绝不含参数 Schema。

        这是模型可见工具列表的唯一来源。与 list_all 的区别是刻意不返回
        parameters / input_schema，避免"查目录"变成"注入全量 Schema"。
        需要完整定义时用 definition。
        """
        return [self._catalog_entry(reg) for reg in self._tools.values()]

    def summary(self, capability_id: str) -> dict[str, Any] | None:
        reg = self.get(capability_id)
        return self._catalog_entry(reg) if reg is not None else None

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """兼容同步目录调用，复用 V2 的 Exact + BM25。"""
        from aiive.tool_discovery.service import DiscoveryService

        return DiscoveryService().search(self, query, limit=limit)["tools"]

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
            "parameters": canonical_schema(reg).get("properties", {}),
            "input_schema": canonical_schema(reg),
        }

    @staticmethod
    def _catalog_entry(reg: BaseTool) -> dict[str, Any]:
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
            "discovery_hash": reg.definition.discovery_hash,
            "namespace_id": reg.definition.discovery.namespace_id,
            "namespace_name": reg.definition.discovery.namespace_name,
            "executor_kind": reg.executor_kind,
            "executor_node_id": reg.executor_node_id,
            "cancellation_policy": reg.definition.cancellation_policy,
            "idempotent": reg.definition.idempotent,
            "supports_reconciliation": reg.definition.supports_reconciliation,
        }

    def execute(self, capability_id: str, params: dict[str, Any], instruction_source: str,
                run_context: RunContext | None = None, tool_call_id: str = "") -> dict[str, Any]:
        from aiive.tools.executor import ToolExecutor
        return ToolExecutor(self).execute(capability_id, params, instruction_source, run_context, tool_call_id=tool_call_id)

    def execute_approved(self, capability_id: str, params: dict[str, Any], expected_descriptor_hash: str,
                         run_context: RunContext, tool_call_id: str = "") -> dict[str, Any]:
        from aiive.tools.executor import ToolExecutor
        return ToolExecutor(self).execute(capability_id, params, "trusted_user_command", run_context,
                                          tool_call_id=tool_call_id, expected_descriptor_hash=expected_descriptor_hash)

    def execute_brokered(self, capability_id: str, params: dict[str, Any], expected_descriptor_hash: str,
                         run_context: RunContext) -> dict[str, Any]:
        from aiive.tools.executor import ToolExecutor
        return ToolExecutor(self).execute(capability_id, params, "task_runtime", run_context,
                                          expected_descriptor_hash=expected_descriptor_hash, channel="task_action")


# 全局单例
_registry: ToolRegistry | None = None
_registry_lock = RLock()


def get_tool_registry() -> ToolRegistry:
    """获取全局工具注册表单例。

    首次调用时自动初始化并注册所有内置工具。

    返回:
        ToolRegistry 全局单例
    """
    global _registry
    with _registry_lock:
        if _registry is None:
            registry = ToolRegistry()
            _register_builtins(registry)
            _registry = registry
        return _registry


def _register_builtins(registry: ToolRegistry) -> None:
    """向注册表中注册所有内置工具。

    参数:
        registry: 目标注册表实例
    """
    from aiive.tools.builtin_tools import register_builtin_tools

    register_builtin_tools(registry)
