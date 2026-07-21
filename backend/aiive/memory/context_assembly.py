"""Context Assembly — serialize the layered agent context.

Layers (per V2 §十):
  - Stable System Contract  (Kernel Contract; immutable-ish, not historical memory)
  - Core Memory Blocks       (small, stable, token-budgeted)
  - Thread Working State     (short-term; rendered as messages, not here)
  - Automatic Recall Pack     (dynamic, query-aware, clearly marked as evidence)

The stable contract prefix is built by the caller (AgentGraph) and passed in;
this module renders the Core Memory and Recall layers with explicit labels so
the model never confuses retrieved historical memory with system instructions.
"""
from __future__ import annotations

from aiive.memory.recall_models import CoreMemoryBlock, MemoryRecallPack


def render_core_memory(blocks: list[CoreMemoryBlock]) -> str:
    """Render Core Memory blocks as a clearly-labeled, small section."""
    if not blocks:
        return ""
    parts = ["## Core Memory (stable, small — current explicit user request overrides these defaults)", ""]
    for b in blocks:
        parts.append(f"### {b.block_name}")
        parts.append(b.content)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def assemble_system_content(
    stable_contract: str,
    core_memory: list[CoreMemoryBlock],
    _recall_pack: MemoryRecallPack | None,
) -> str:
    """Concatenate stable contract + core memory.

    Recall results are NO LONGER part of system_content — they are rendered
    as separate SystemMessages placed right before the current user message
    so the Context Inspector shows them inline with the triggering round.
    """
    sections = [stable_contract.rstrip()]
    cm = render_core_memory(core_memory)
    if cm:
        sections.append(cm)
    return "\n\n".join(sections).rstrip() + "\n"
