"""Explicit invocation of class tools for domain tests (no runtime callable registry)."""

import inspect

from aiive.tools.base import BaseTool, ToolExecutionContext
from aiive.tools.executor import DirectAdapter
from aiive.tools.registry import CapabilitySafetySchema, ToolDefinition


def tool_method(tool):
    if isinstance(tool, type):
        tool = tool(
            ToolDefinition(
                CapabilitySafetySchema(tool.__name__, "test", "trusted", "low")
            )
        )
    return tool._run


def invoke_test_tool(tool, *args, **kwargs):
    if isinstance(tool, type):
        tool = tool(
            ToolDefinition(
                CapabilitySafetySchema(tool.__name__, "test", "trusted", "low")
            )
        )
    assert isinstance(tool, BaseTool)
    bound = inspect.signature(tool._run).bind_partial(*args, **kwargs)
    params = dict(bound.arguments)
    run = params.pop("ctx", None)
    db = params.pop("db", None)
    context = ToolExecutionContext(tool.safety.capability_id, run, db=db)
    return DirectAdapter.invoke(tool, context, params)
