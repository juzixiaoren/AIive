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
import re
from dataclasses import dataclass, field, replace
from typing import Any

from aiive.tools.base import BaseTool, CancellationPolicy

from aiive.config import settings
from aiive.context.run_context import RunContext

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


def canonical_schema(reg: BaseTool) -> dict[str, Any]:
    from aiive.tools.tool_validation import canonical_tool_schema
    return canonical_tool_schema(reg)


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


def _parameter_names(reg: BaseTool) -> list[str]:
    """收集参数名，供检索命中参数维度（不返回任何 Schema 内容）。"""
    names: list[str] = []
    if isinstance(reg.parameters, dict):
        names.extend(str(name) for name in reg.parameters)
    schema = reg.input_schema
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        names.extend(str(name) for name in schema["properties"])
    return names


def _score_registration(reg: BaseTool, tokens: list[str]) -> int:
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
        )
        )
        self._tools[reg.safety.capability_id] = reg

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
                "parameters": canonical_schema(r).get("properties", {}),
                "input_schema": canonical_schema(r),
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
        scored: list[tuple[int, str, BaseTool]] = []
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
