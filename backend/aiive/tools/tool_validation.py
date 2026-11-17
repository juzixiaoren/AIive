"""工具参数的本地、供应商无关校验。

模型侧只使用普通 Function Calling；本模块以 handler 签名和工具注册元数据
派生 Canonical JSON Schema，并在所有执行入口前严格校验。显式 MCP/Desktop
inputSchema 优先，模型注入与服务端校验共享同一份完整定义。
"""

from __future__ import annotations

import inspect
import operator
import types
import typing
from dataclasses import dataclass, field
from functools import reduce
from typing import Annotated, Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, create_model


@dataclass(frozen=True)
class ToolArgumentIssue:
    """单个可安全返回给模型的参数问题。"""

    path: str
    code: str
    message: str
    expected: str = ""
    received: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "path": self.path,
                "code": self.code,
                "message": self.message,
                "expected": self.expected,
                "received": self.received,
            }.items()
            if value
        }


@dataclass(frozen=True)
class ToolValidationResult:
    """工具参数验证结果。"""

    ok: bool
    validated_params: dict[str, Any] = field(default_factory=dict)
    issues: list[ToolArgumentIssue] = field(default_factory=list)

    def error_payload(self, tool_name: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error_type": "argument_validation_error",
            "retryable": True,
            "tool_name": tool_name,
            "capability_id": tool_name,
            "issues": [issue.as_dict() for issue in self.issues[:12]],
            "instruction": (
                "请只修正上述参数后重新调用工具；不要声称工具已经执行。"
                "如果缺少必要信息，请向用户询问，不要猜测。"
            ),
        }


_TYPE_MAP: dict[str, type[Any]] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list[Any],
    "dict": dict[str, Any],
}


def _handler_signature(handler: Any) -> inspect.Signature:
    """读取工具业务方法的类型签名。"""
    target = handler
    try:
        return inspect.signature(target)
    except (TypeError, ValueError):
        return inspect.Signature()


def _handler_type_hints(handler: Any) -> dict[str, Any]:
    target = handler
    try:
        return typing.get_type_hints(target, include_extras=True)
    except (NameError, TypeError):
        return {}


def _annotation_for(
    parameter: inspect.Parameter | None,
    declared_type: str,
    resolved_annotation: Any = None,
) -> Any:
    if resolved_annotation is not None:
        return resolved_annotation
    if parameter is not None and parameter.annotation is not inspect.Signature.empty:
        return parameter.annotation
    return _TYPE_MAP.get(declared_type, str)


def _allows_none(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is types.UnionType or str(origin) == "typing.Union":
        return type(None) in get_args(annotation)
    return annotation is Any or annotation is type(None)


def _optional(annotation: Any) -> Any:
    if _allows_none(annotation):
        return annotation
    return annotation | None


def _without_none(annotation: Any) -> Any:
    if not _allows_none(annotation):
        return annotation
    remaining = tuple(arg for arg in get_args(annotation) if arg is not type(None))
    if not remaining:
        return Any
    if len(remaining) == 1:
        return remaining[0]
    return reduce(operator.or_, remaining)


def _field_from_metadata(default: Any, metadata: dict[str, Any]) -> Any:
    kwargs: dict[str, Any] = {
        "description": str(metadata.get("description", "")),
    }
    constraint_map = {
        "minimum": "ge",
        "maximum": "le",
        "exclusiveMinimum": "gt",
        "exclusiveMaximum": "lt",
        "min_length": "min_length",
        "max_length": "max_length",
        "minLength": "min_length",
        "maxLength": "max_length",
    }
    for source, target in constraint_map.items():
        if source in metadata:
            kwargs[target] = metadata[source]
    return Field(default=default, **kwargs)


def build_tool_args_model(
    registration: Any,
    *,
    include_injected_tool_call_id: bool = False,
) -> type[BaseModel] | None:
    """从注册信息构建严格参数模型。

    ``parameters is None`` 表示从业务方法派生契约；显式参数存在时按元数据构建；显式空字典表示工具不接受任何业务参数。
    """
    parameters = registration.parameters
    if parameters is None:
        return None

    signature = _handler_signature(registration._run)
    signature_params = signature.parameters
    type_hints = _handler_type_hints(registration._run)
    fields: dict[str, Any] = {}

    for name, raw_definition in parameters.items():
        metadata = raw_definition if isinstance(raw_definition, dict) else {"type": raw_definition}
        declared_type = str(metadata.get("type", "str"))
        parameter = signature_params.get(name)
        annotation = _annotation_for(parameter, declared_type, type_hints.get(name))

        explicit_required = metadata.get("required")
        if explicit_required is None:
            required = (
                parameter is not None
                and parameter.default is inspect.Signature.empty
                and parameter.kind not in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                )
            )
        else:
            required = bool(explicit_required)

        if required:
            annotation = _without_none(annotation)
            default = ...
        elif "default" in metadata:
            default = metadata["default"]
        elif parameter is not None and parameter.default is not inspect.Signature.empty:
            default = parameter.default
        else:
            default = None
            annotation = _optional(annotation)

        enum_values = metadata.get("enum")
        field_info = _field_from_metadata(default, metadata)
        if isinstance(enum_values, list) and enum_values:
            # Pydantic 的 Field 本身不产生 enum 约束；在本地 JSON Schema 校验
            # 和 validate_tool_params 的显式枚举检查中统一处理。
            field_info.json_schema_extra = {"enum": list(enum_values)}
        fields[str(name)] = (annotation, field_info)

    if include_injected_tool_call_id:
        from langchain_core.tools import InjectedToolCallId

        fields["tool_call_id"] = (
            Annotated[str, InjectedToolCallId],
            Field(default=""),
        )

    return create_model(
        f"{registration.safety.capability_id}_args",
        __config__=ConfigDict(extra="forbid", strict=True),
        **fields,
    )


def validate_tool_params(registration: Any, params: object) -> ToolValidationResult:
    """严格验证并规范化一次工具调用参数。"""
    if not isinstance(params, dict):
        return ToolValidationResult(
            ok=False,
            issues=[
                ToolArgumentIssue(
                    path="$",
                    code="dict_type",
                    message="工具参数必须是 JSON 对象",
                    expected="object",
                    received=type(params).__name__,
                )
            ],
        )

    from jsonschema import Draft202012Validator, validators
    def strict_integer(_checker: Any, value: Any) -> bool:
        return type(value) is int
    strict_validator = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", strict_integer))

    schema = canonical_tool_schema(registration)
    issues = []
    def error_sort_key(error: Any) -> str:
        return str(list(error.path))
    for error in sorted(strict_validator(schema).iter_errors(params), key=error_sort_key):
        path = ".".join(str(part) for part in error.absolute_path)
        if error.validator == "additionalProperties" and isinstance(error.instance, dict):
            extras = set(error.instance) - set(error.schema.get("properties", {}))
            for name in sorted(extras):
                issues.append(ToolArgumentIssue(
                    path=".".join(filter(None, [path, name])), code="extra_forbidden",
                    message="不允许的额外参数", expected="no additional properties",
                    received=type(error.instance[name]).__name__,
                ))
            continue
        if error.validator == "required":
            for name in error.validator_value:
                if name not in error.instance:
                    issues.append(ToolArgumentIssue(
                        path=".".join(filter(None, [path, name])), code="missing",
                        message="缺少必填参数", expected=str(error.schema.get("properties", {}).get(name, {})),
                        received="missing",
                    ))
            continue
        issues.append(ToolArgumentIssue(
            path=path or "$", code=str(error.validator), message="参数不符合工具契约",
            expected=str(error.validator_value)[:240], received=type(error.instance).__name__,
        ))
    return ToolValidationResult(ok=not issues, validated_params=dict(params) if not issues else {}, issues=issues)


def canonical_tool_schema(registration: Any) -> dict[str, Any]:
    """Explicit input_schema wins; otherwise derive annotations then overlay declared metadata.

    The returned copy is the sole effective contract for injection, validation and hashing.
    """
    from copy import deepcopy
    if registration.input_schema is not None:
        return deepcopy(registration.input_schema)
    model = build_tool_args_model(registration)
    if model is None:
        signature = _handler_signature(registration._run)
        parameters = {
            name: {} for name, parameter in signature.parameters.items()
            if name not in {"ctx", "db", "tool_call_id", "_execution"}
            and parameter.kind not in {inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL}
        }
        from dataclasses import replace
        model = build_tool_args_model(registration.with_definition(replace(registration.definition, parameters=parameters)))
    schema: dict[str, Any] = model.model_json_schema() if model else {"type": "object", "properties": {}}
    for name, metadata in (registration.parameters or {}).items():
        if not isinstance(metadata, dict):
            continue
        child = schema["properties"][name]
        # Types come from annotations. Explicit nested JSON Schema is authoritative.
        if "schema" in metadata:
            nested = deepcopy(metadata["schema"])
            if any(branch.get("type") == "null" for branch in child.get("anyOf", [])) and isinstance(nested.get("type"), str):
                nested["type"] = [nested["type"], "null"]
            if "default" in child:
                nested.setdefault("default", child["default"])
            definitions = nested.pop("$defs", {})
            if definitions:
                schema.setdefault("$defs", {}).update(definitions)
            schema["properties"][name] = nested
            continue
        for key, value in metadata.items():
            if key not in {"type", "required"}:
                child[key] = deepcopy(value)
    return schema
