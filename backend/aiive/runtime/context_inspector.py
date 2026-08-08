"""离线导出 Agent 完整上下文结构（用于人工审查提示词与上下文组成）。

设计目标：在不连接数据库、不绑定线程、不依赖任何会话状态的前提下，
把一个真实会话中 Agent 能拿到的全部上下文分区「可视化」出来，方便
定位臃肿、重复或缺失的部分，服务于 debug 与提示词精简。

分区来源对齐 runtime.context_assembler.ContextAssembler.assemble 的真实组装顺序：

- 静态部分（离线即可拿到真实文本/真实结构）：
  · 系统提示词 stable_contract（受版本控制的 .md 资源，纯文件读取）；
  · Core Memory 的来源键 → 角色映射（来自 MemoryKeyRegistry，纯内存）；
  · Working State 的字段模板（render_for_context 的结构，来自代码）。
- 动态部分（仅真实会话中存在，以 {xxx} 占位）：
  · runtime identity / active policies 的实时值；
  · history / attention / recall / segment summary / epoch checkpoint /
    sealing bridge / current message 的实时值。
- 工具定义（Tools Schema）虽在运行时按 token 预算截断，但其来源是纯静态的
  ToolRegistry 配置，离线即可真实渲染，归类为静态。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

from aiive.memory.memory_key_registry import get_memory_key_registry
from aiive.prompts.registry import get_prompt_registry


# Core Memory 角色的中文可读标签，便于非工程读者理解各区块语义。
_ROLE_LABELS: dict[str, str] = {
    "core.human_identity": "用户身份",
    "core.interaction_defaults": "交互默认偏好",
    "core.agent_persona": "智能体人格",
}


def estimate_tokens(text: str) -> int:
    """保守估算 token 数（UTF-8 字节数 / 2），与项目内多处估算口径一致。"""
    return max(1, len(text.encode("utf-8")) // 2)


@dataclass
class ContextSection:
    """一个上下文分区：要么是静态真实文本，要么是动态占位说明。"""

    key: str                       # 分区标识（对应 assemble 中的分区名）
    title: str                     # 中文标题
    kind: str                      # "static" | "dynamic"
    content: str                   # 真实文本或占位说明
    source: str                    # 真实来源描述（文件/函数/数据库表）
    note: str = ""                 # 为何静态 / 为何只能占位

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.content)


@dataclass
class ContextDump:
    """一次离线导出的完整结构，供脚本渲染为 Markdown。"""

    generated_at: str = ""
    sections: list[ContextSection] = field(default_factory=list)

    @property
    def total_token_estimate(self) -> int:
        return sum(s.token_estimate for s in self.sections)


# ── 静态分区：真实文本/真实结构 ──

def get_system_contract() -> ContextSection:
    """渲染 stable_contract 提示词真实文本（离线纯文件读取，含版本指纹）。

    注意：Runtime Identity 与 Active Policies 由 DB 中的 memory_records 投影，
    离线无法获取其实时值，见 get_runtime_identity_placeholder /
    get_active_policies_placeholder。
    """
    rendered = get_prompt_registry().render("agent.stable_contract")
    return ContextSection(
        key="stable_contract",
        title="Stable Contract（系统提示词）",
        kind="static",
        content=rendered.content,
        source=f"prompts/agent/stable_contract.md @{rendered.version} "
               f"(sha256:{rendered.sha256[:12]})",
        note="纯文件资源，离线即可完整渲染。",
    )


def get_runtime_identity_placeholder() -> ContextSection:
    """Runtime Identity 的渲染模板（真实结构）+ 实时值占位。

    已精简：仅保留 Core Memory 投影不了的内容（agent_runtime_id /
    agent_display_name / relationship_style），与 Core Memory 同源重复的
    user_display_name / response_style 已移除，避免同一份数据出现两次。
    """
    spec = (
        "## Runtime Identity\n"
        "- agent_runtime_id: {agent_runtime_id}      # runtime config，非 memory，Core Memory 投影不了\n"
        "- agent_display_name: {agent_display_name}  # 来自 agent.display_name，不在 Core Memory 投影键\n"
        "- relationship_style: {relationship_style}  # 来自 agent.persona.relationship，不在 Core Memory 投影键"
    )
    body = (
        "【渲染模板（真实，来自 agent_graph._build_runtime_identity）】\n"
        f"{spec}\n\n"
        "【实时值占位 —— 需 DB：MemoryReadModel.resolve_identity()】\n"
        "{runtime_identity}"
    )
    return ContextSection(
        key="runtime_identity",
        title="Runtime Identity（运行时身份）",
        kind="dynamic",
        content=body,
        source="memory/memory_read_model.py:resolve_identity",
        note="身份字段来自 memory_records（context_role=runtime_identity），"
             "离线不连 DB 仅展示模板；仅保留 Core Memory 投影不了的字段。",
    )


def get_active_policies_placeholder() -> ContextSection:
    """Active Policies 的渲染模板（真实结构）+ 实时值占位。"""
    body = (
        "【渲染模板（真实，来自 agent_graph._build_stable_contract）】\n"
        "## Active Policies (user-defined rules — instructions, not memory)\n"
        "- {policy_content}\n\n"
        "【实时值占位 —— 需 DB：MemoryReadModel.resolve_policies()】\n"
        "{active_policies}"
    )
    return ContextSection(
        key="active_policies",
        title="Active Policies（用户自定义策略）",
        kind="dynamic",
        content=body,
        source="memory/memory_read_model.py:resolve_policies",
        note="策略来自 memory_records（context_role=policy），离线不连 DB 仅展示模板。",
    )


# 注入型参数（LangChain InjectedToolCallId）由运行时注入，不出现在发送给 LLM 的 schema 中。
_INJECTED_TOOL_PARAMS: tuple[str, ...] = ("tool_call_id",)


def _strip_injected_params(schema: dict[str, Any]) -> dict[str, Any]:
    """剔除工具 JSON schema 中运行时会注入的参数，返回副本。"""
    props = schema.get("properties")
    if not isinstance(props, dict) or not any(
        name in props for name in _INJECTED_TOOL_PARAMS
    ):
        return schema
    cleaned = dict(schema)
    cleaned["properties"] = {
        k: v for k, v in props.items() if k not in _INJECTED_TOOL_PARAMS
    }
    required = schema.get("required")
    if isinstance(required, list):
        cleaned["required"] = [r for r in required if r not in _INJECTED_TOOL_PARAMS]
    return cleaned


def get_tools_schema_static() -> ContextSection:
    """离线枚举工具注册表，真实渲染全部工具 schema（不连接 DB、不依赖会话）。

    工具定义来自 ToolRegistry（纯静态代码/配置），只按 token 预算做前缀截断，
    因此离线即可拿到完整真实文本。这里直接列出 name + description + 参数 schema，
    便于审查「工具是否过多、schema 是否臃肿」。
    """
    from aiive.tools.registry import get_tool_registry
    from aiive.tools.langchain_adapter import build_langchain_tools
    from aiive.context.run_context import RunContext, RUN_CTX_USER_CHAT

    registry = get_tool_registry()
    run_ctx = RunContext(thread_id="offline-inspect", trace_id="inspector", source=RUN_CTX_USER_CHAT)
    langchain_tools = build_langchain_tools(registry, run_context=run_ctx)

    blocks: list[str] = [f"工具总数：{len(langchain_tools)}", ""]
    for idx, t in enumerate(langchain_tools, 1):
        schema: dict[str, Any] = {}
        args_schema = getattr(t, "args_schema", None)
        if args_schema:
            schema = args_schema if isinstance(args_schema, dict) else args_schema.model_json_schema()
            schema = _strip_injected_params(schema)
        blocks.append(f"{idx}. `{getattr(t, 'name', '')}`")
        desc = getattr(t, "description", "") or "（无描述）"
        blocks.append(f"   描述：{desc}")
        params = schema.get("properties", {})
        if params:
            blocks.append("   参数：")
            for pname, pinfo in params.items():
                ptype = pinfo.get("type", pinfo.get("anyOf", "any"))
                required = pname in schema.get("required", [])
                req_tag = "必填" if required else "可选"
                blocks.append(f"     - {pname} ({ptype}, {req_tag})：{pinfo.get('description', '')}")
        blocks.append("")

    return ContextSection(
        key="tools_schema",
        title="Tools Schema（工具定义）",
        kind="static",
        content="\n".join(blocks).rstrip(),
        source="tools/registry.py + tools/langchain_adapter.py:build_langchain_tools",
        note="纯静态配置，离线即可真实渲染；运行时仅按 token 预算做前缀截断。",
    )


def get_core_memory_source_map() -> ContextSection:
    """Core Memory 的来源键 → 角色映射（真实，来自 MemoryKeyRegistry）。

    展示「哪些 canonical_key 会投影进 Core Memory」，但不展示投影结果本身
    （投影结果依赖 DB，见 get_core_memory_placeholder）。
    """
    registry = get_memory_key_registry()
    key_to_role = registry.get_core_memory_keys()
    lines = ["| canonical_key | core_memory_role | 中文 |", "|---|---|---|"]
    for key, role in sorted(key_to_role.items()):
        lines.append(f"| {key} | {role} | {_ROLE_LABELS.get(role, role)} |")
    table = "\n".join(lines)
    body = (
        "【来源键映射（真实，来自 memory_key_registry.MemoryKeyRegistry）】\n"
        f"{table}\n\n"
        "【实时区块内容占位 —— 需 DB：core_memory_projection.load_core_memory()】\n"
        "{core_memory_blocks}"
    )
    return ContextSection(
        key="core_memory",
        title="Core Memory（核心记忆投影）",
        kind="static",
        content=body,
        source="memory/memory_key_registry.py:get_core_memory_keys",
        note="区块结构离线可见；区块实时内容来自 core_memory_blocks 表 / "
             "memory_records 投影，离线不连 DB 以占位表示。",
    )


def get_working_state_template() -> ContextSection:
    """Working State 的字段模板（真实，来自 WorkingStateService.render_for_context）。"""
    fields = [
        ("当前目标", "current_objective"),
        ("未完成循环", "open_loops（带 priority / description）"),
        ("活跃约束", "active_constraints（带 description / source）"),
        ("待审批", "pending_approvals（带 action / requested_at）"),
        ("引用的 Artifact", "artifact_refs（带 ref / kind / description）"),
    ]
    lines = [f"- {label}：{field}" for label, field in fields]
    body = (
        "【字段模板（真实，来自 runtime/working_state.py:render_for_context）】\n"
        "## Working State（当前操作上下文，有界）\n"
        + "\n".join(lines)
        + "\n\n【实时值占位 —— 需 DB：WorkingStateService.render_for_context()】\n"
        "{working_state_text}"
    )
    return ContextSection(
        key="working_state",
        title="Working State（操作上下文）",
        kind="static",
        content=body,
        source="runtime/working_state.py:render_for_context",
        note="字段结构离线可见；实时值来自 WorkingState 表，离线不连 DB 以占位表示。",
    )


# ── 动态分区：实时值占位（仅真实会话中存在）──

def _dynamic_section(key: str, title: str, placeholder: str, source: str, note: str) -> ContextSection:
    """构造一个动态分区：内容即占位符本身，附来源说明。"""
    body = (
        f"【实时值占位 —— {note}】\n"
        f"{placeholder}"
    )
    return ContextSection(
        key=key,
        title=title,
        kind="dynamic",
        content=body,
        source=source,
        note=note,
    )


def build_context_dump() -> ContextDump:
    """离线组装全部上下文分区，返回结构化结果（不连接 DB、不绑定线程）。"""
    sections: list[ContextSection] = [
        # 1. 稳定系统契约（真实）
        get_system_contract(),
        get_runtime_identity_placeholder(),
        get_active_policies_placeholder(),
        # 2. Core Memory（结构真实 + 值占位）
        get_core_memory_source_map(),
        # 3. 对话历史
        _dynamic_section(
            "history_messages",
            "History（对话历史）",
            "{history_messages}",
            "runtime/context_assembler.py:_load_history_bounded",
            "来自 ThreadState，仅真实会话中存在",
        ),
        # 4. 注意力状态
        _dynamic_section(
            "attention",
            "Attention（注意力状态）",
            "{attention_text}",
            "runtime/attention_manager.py:render_for_context",
            "来自 AttentionManager.resolve_for_turn，逐轮变化",
        ),
        # 5. Working State（结构真实 + 值占位）
        get_working_state_template(),
        # 6. 历史摘要（统一检索命中）
        _dynamic_section(
            "history_summary",
            "History Summary（统一检索命中摘要）",
            "{history_summary_text}",
            "runtime/context_assembler.py:_render_history_summary",
            "来自 UnifiedRetriever 的历史摘要/检查点命中",
        ),
        # 7. 自动召回记忆
        _dynamic_section(
            "recall",
            "Recall（自动召回记忆）",
            "{recall_messages}",
            "memory/context_assembly.py + UnifiedRetriever",
            "来自 Automatic Recall，按查询命中，标记为非系统指令",
        ),
        # 8. Segment 摘要 / Epoch 检查点
        _dynamic_section(
            "segment_summaries",
            "Segment Summary（近期摘要）",
            "{segment_summary_text}",
            "runtime/context_assembler.py:_load_segment_summaries",
            "来自 SegmentSummary 表，未被 checkpoint 覆盖的部分",
        ),
        _dynamic_section(
            "epoch_checkpoint",
            "Epoch Checkpoint（阶段检查点）",
            "{epoch_checkpoint_text}",
            "runtime/context_assembler.py:_load_epoch_checkpoint",
            "来自 EpochCheckpoint 表，最新有效检查点",
        ),
        # 9. Sealing Bridge
        _dynamic_section(
            "sealing_bridge",
            "Sealing Bridge（密封桥接）",
            "{sealing_bridge_text}",
            "runtime/context_assembler.py:_load_sealing_bridge",
            "来自 sealing Segment 的摘要或 bounded raw tail",
        ),
        # 10. 工具 schema（静态真实渲染）
        get_tools_schema_static(),
        # 11. 当前消息
        _dynamic_section(
            "current_message",
            "Current Message（当前用户输入）",
            "{current_message}",
            "调用入口传入的 message",
            "来自本轮请求，角色由来源决定（user / system）",
        ),
    ]
    return ContextDump(
        generated_at=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        sections=sections,
    )


def render_markdown(dump: ContextDump) -> str:
    """将 ContextDump 渲染为 Markdown，分区顺序对齐 assemble 真实组装顺序。"""
    parts: list[str] = []
    parts.append("# Agent 上下文离线导出")
    parts.append("")
    parts.append(f"> 生成时间：{dump.generated_at}")
    parts.append(">")
    parts.append("> 本文件由 `scripts/dump_agent_context.py` 离线生成，**不连接数据库、"
                 "不绑定线程、不依赖任何会话状态**。")


    parts.append("> 静态部分展示真实文本/真实结构；动态部分（仅真实会话中存在）"
                 "以 `{xxx}` 占位，便于阅读者判断上下文骨架是否合理、哪里需要精简。")
    parts.append("")

    # 分区总览表
    parts.append("## 分区总览")
    parts.append("")
    parts.append("| # | 分区 | 类型 | 来源 | 估算 token |")
    parts.append("|---|------|------|------|-----------|")
    for i, s in enumerate(dump.sections, 1):
        kind_label = "静态" if s.kind == "static" else "动态"
        parts.append(
            f"| {i} | {s.title} | {kind_label} | `{s.source}` | {s.token_estimate} |"
        )
    parts.append(f"| — | **合计** | — | — | **{dump.total_token_estimate}** |")
    parts.append("")

    # 逐个分区详述
    for i, s in enumerate(dump.sections, 1):
        kind_label = "静态（真实）" if s.kind == "static" else "动态（占位）"
        parts.append(f"## {i}. {s.title} [{kind_label}]")
        parts.append("")
        parts.append(f"- **来源**：`{s.source}`")
        if s.note:
            parts.append(f"- **说明**：{s.note}")
        parts.append(f"- **估算 token**：{s.token_estimate}")
        parts.append("")
        parts.append("```")
        parts.append(s.content)
        parts.append("```")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"
