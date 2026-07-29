"""工具参数的本地、供应商无关校验。

模型侧只使用普通 Function Calling；本模块以 handler 签名和工具注册元数据
构建严格 Pydantic 模型，并在所有执行入口前验证参数。MCP 工具额外保留并
校验服务端提供的 inputSchema，避免注册转换时丢失嵌套约束。
"""

from __future__ import annotations

import inspect
import operator
import types
import typing
from dataclasses import dataclass, field
from functools import reduce
from typing import Annotated, Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model


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
    """优先读取 ``_db_handler`` 保存的原始函数签名。"""
    target = getattr(handler, "_aiive_db_handler", handler)
    try:
        return inspect.signature(target)
    except (TypeError, ValueError):
        return inspect.Signature()


def _handler_type_hints(handler: Any) -> dict[str, Any]:
    target = getattr(handler, "_aiive_db_handler", handler)
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

    ``parameters is None`` 表示旧式、无显式契约的注册项，保留兼容并跳过模型
    构建；显式空字典表示工具不接受任何业务参数。
    """
    parameters = registration.parameters
    if parameters is None:
        return None

    signature = _handler_signature(registration.handler)
    signature_params = signature.parameters
    type_hints = _handler_type_hints(registration.handler)
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


def _pydantic_issues(error: ValidationError) -> list[ToolArgumentIssue]:
    issues: list[ToolArgumentIssue] = []
    for item in error.errors(include_url=False, include_context=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "$"
        received = type(item.get("input")).__name__ if "input" in item else ""
        issues.append(
            ToolArgumentIssue(
                path=location,
                code=str(item.get("type", "validation_error")),
                message=str(item.get("msg", "参数不合法"))[:240],
                received=received,
            )
        )
    return issues


def _json_type_matches(value: Any, expected: Any) -> bool:
    expected_types = expected if isinstance(expected, list) else [expected]
    for expected_type in expected_types:
        if expected_type == "null" and value is None:
            return True
        if expected_type == "string" and isinstance(value, str):
            return True
        if expected_type == "boolean" and isinstance(value, bool):
            return True
        if expected_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if expected_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if expected_type == "array" and isinstance(value, list):
            return True
        if expected_type == "object" and isinstance(value, dict):
            return True
    return False


def _validate_json_schema(
    value: Any,
    schema: dict[str, Any],
    path: str = "$",
) -> list[ToolArgumentIssue]:
    """校验 MCP inputSchema 的常用、确定性子集。"""
    issues: list[ToolArgumentIssue] = []
    expected_type = schema.get("type")
    if expected_type is not None and not _json_type_matches(value, expected_type):
        return [
            ToolArgumentIssue(
                path=path,
                code="json_type",
                message="参数类型不符合工具契约",
                expected=str(expected_type),
                received=type(value).__name__,
            )
        ]

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        issues.append(
            ToolArgumentIssue(
                path=path,
                code="enum",
                message="参数不在允许值范围内",
                expected=", ".join(str(item) for item in enum_values[:12]),
                received=type(value).__name__,
            )
        )

    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        for name in required if isinstance(required, list) else []:
            if name not in value:
                issues.append(
                    ToolArgumentIssue(
                        path=f"{path}.{name}",
                        code="missing",
                        message="缺少必填参数",
                    )
                )
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    issues.append(
                        ToolArgumentIssue(
                            path=f"{path}.{name}",
                            code="extra_forbidden",
                            message="不允许的额外参数",
                        )
                    )
        for name, child in value.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, dict):
                issues.extend(_validate_json_schema(child, child_schema, f"{path}.{name}"))

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        for index, item in enumerate(value):
            issues.extend(_validate_json_schema(item, item_schema, f"{path}[{index}]"))

    if isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            issues.append(ToolArgumentIssue(path, "min_length", "字符串过短", str(min_length)))
        if isinstance(max_length, int) and len(value) > max_length:
            issues.append(ToolArgumentIssue(path, "max_length", "字符串过长", str(max_length)))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            issues.append(ToolArgumentIssue(path, "minimum", "数值低于允许下限", str(minimum)))
        if isinstance(maximum, (int, float)) and value > maximum:
            issues.append(ToolArgumentIssue(path, "maximum", "数值高于允许上限", str(maximum)))
    return issues


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

    input_schema = registration.input_schema
    if isinstance(input_schema, dict) and input_schema:
        schema_issues = _validate_json_schema(params, input_schema)
        if schema_issues:
            return ToolValidationResult(ok=False, issues=schema_issues)

    model = build_tool_args_model(registration)
    if model is None:
        return ToolValidationResult(ok=True, validated_params=dict(params))
    try:
        validated = model.model_validate(params)
    except ValidationError as error:
        return ToolValidationResult(ok=False, issues=_pydantic_issues(error))

    enum_issues: list[ToolArgumentIssue] = []
    for name, raw_definition in (registration.parameters or {}).items():
        if name not in params or not isinstance(raw_definition, dict):
            continue
        enum_values = raw_definition.get("enum")
        if isinstance(enum_values, list) and params[name] not in enum_values:
            enum_issues.append(
                ToolArgumentIssue(
                    path=str(name),
                    code="enum",
                    message="参数不在允许值范围内",
                    expected=", ".join(str(item) for item in enum_values[:12]),
                    received=type(params[name]).__name__,
                )
            )
    if enum_issues:
        return ToolValidationResult(ok=False, issues=enum_issues)

    return ToolValidationResult(
        ok=True,
        validated_params=validated.model_dump(exclude_unset=True),
    )
