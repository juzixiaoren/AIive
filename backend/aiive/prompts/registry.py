"""从受版本控制的 Markdown 资源加载、校验并渲染提示词。"""
from __future__ import annotations

import hashlib
import logging
import string
import threading
import tomllib
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


class PromptCatalogError(RuntimeError):
    """提示词目录、元数据或变量契约无效。"""


@dataclass(frozen=True)
class PromptSpec:
    prompt_id: str
    file: str
    version: str
    role: str
    variables: tuple[str, ...]


@dataclass(frozen=True)
class RenderedPrompt:
    """一次确定性渲染结果及其审计身份。"""

    prompt_id: str
    version: str
    role: str
    sha256: str
    content: str

    @property
    def audit_ref(self) -> str:
        return f"{self.prompt_id}@{self.version}#{self.sha256[:12]}"


@dataclass(frozen=True)
class _LoadedPrompt:
    spec: PromptSpec
    template: string.Template
    sha256: str


class PromptRegistry:
    """进程启动时加载一次；生产运行中不热更新提示词文件。"""

    _ALLOWED_ROLES: ClassVar[frozenset[str]] = frozenset({"system", "user"})

    def __init__(self, root: Traversable | None = None) -> None:
        self._root: Traversable = root or resources.files("aiive.prompts")
        self._loaded: dict[str, _LoadedPrompt] = {}
        self._catalog_sha256: str = ""
        self._load_catalog()

    @property
    def catalog_sha256(self) -> str:
        return self._catalog_sha256

    @property
    def prompt_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._loaded))

    def render(self, prompt_id: str, **variables: object) -> RenderedPrompt:
        loaded = self._loaded.get(prompt_id)
        if loaded is None:
            raise PromptCatalogError(f"未知提示词 ID: {prompt_id}")
        expected = set(loaded.spec.variables)
        actual = set(variables)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            raise PromptCatalogError(
                f"提示词变量不匹配: id={prompt_id}, missing={missing}, extra={extra}"
            )
        try:
            content = loaded.template.substitute(
                {key: str(value) for key, value in variables.items()}
            )
        except (KeyError, ValueError) as exc:
            raise PromptCatalogError(f"提示词渲染失败: id={prompt_id}: {exc}") from exc
        return RenderedPrompt(
            prompt_id=prompt_id,
            version=loaded.spec.version,
            role=loaded.spec.role,
            sha256=loaded.sha256,
            content=content,
        )

    def audit_refs(self) -> tuple[str, ...]:
        return tuple(
            f"{item.spec.prompt_id}@{item.spec.version}#{item.sha256[:12]}"
            for item in sorted(self._loaded.values(), key=lambda value: value.spec.prompt_id)
        )

    def _load_catalog(self) -> None:
        manifest_file = self._root.joinpath("manifest.toml")
        try:
            manifest_bytes = manifest_file.read_bytes()
            manifest: dict[str, Any] = tomllib.loads(manifest_bytes.decode("utf-8"))
        except (FileNotFoundError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise PromptCatalogError(f"提示词 manifest 加载失败: {exc}") from exc
        raw_prompts = manifest.get("prompts")
        if not isinstance(raw_prompts, dict) or not raw_prompts:
            raise PromptCatalogError("manifest.toml 必须包含非空 [prompts] 定义")

        loaded: dict[str, _LoadedPrompt] = {}
        for prompt_id, raw_spec in raw_prompts.items():
            if not isinstance(prompt_id, str) or not prompt_id.strip():
                raise PromptCatalogError("提示词 ID 必须是非空字符串")
            if not isinstance(raw_spec, dict):
                raise PromptCatalogError(f"提示词元数据必须是对象: {prompt_id}")
            spec = self._parse_spec(prompt_id, raw_spec)
            loaded[prompt_id] = self._load_prompt(spec)

        catalog_material = "\n".join(
            f"{item.spec.prompt_id}|{item.spec.version}|{item.sha256}"
            for item in sorted(loaded.values(), key=lambda value: value.spec.prompt_id)
        )
        self._loaded = loaded
        self._catalog_sha256 = hashlib.sha256(
            catalog_material.encode("utf-8")
        ).hexdigest()

    def _parse_spec(self, prompt_id: str, raw: dict[str, Any]) -> PromptSpec:
        file = raw.get("file")
        version = raw.get("version")
        role = raw.get("role")
        variables = raw.get("variables", [])
        if not isinstance(file, str) or not file.endswith(".md"):
            raise PromptCatalogError(f"提示词文件必须是 .md: {prompt_id}")
        path_parts = file.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} for part in path_parts):
            raise PromptCatalogError(f"提示词文件路径无效: {prompt_id}: {file}")
        if not isinstance(version, str) or not version.strip():
            raise PromptCatalogError(f"提示词版本不能为空: {prompt_id}")
        if role not in self._ALLOWED_ROLES:
            raise PromptCatalogError(f"提示词 role 无效: {prompt_id}: {role}")
        if (
            not isinstance(variables, list)
            or any(not isinstance(item, str) or not item for item in variables)
            or len(set(variables)) != len(variables)
        ):
            raise PromptCatalogError(f"提示词 variables 无效: {prompt_id}")
        return PromptSpec(
            prompt_id=prompt_id,
            file=file,
            version=version,
            role=role,
            variables=tuple(variables),
        )

    def _load_prompt(self, spec: PromptSpec) -> _LoadedPrompt:
        file = self._root
        for part in spec.file.split("/"):
            file = file.joinpath(part)
        try:
            raw = file.read_bytes()
            text = raw.decode("utf-8")
        except (FileNotFoundError, UnicodeDecodeError) as exc:
            raise PromptCatalogError(
                f"提示词文件加载失败: {spec.prompt_id}: {spec.file}: {exc}"
            ) from exc
        if not text.strip():
            raise PromptCatalogError(f"提示词文件不能为空: {spec.prompt_id}")
        placeholders = self._template_variables(text, spec.prompt_id)
        expected = set(spec.variables)
        if placeholders != expected:
            details = (
                f"manifest={sorted(expected)}, markdown={sorted(placeholders)}"
            )
            raise PromptCatalogError(
                f"提示词占位变量与 manifest 不一致: id={spec.prompt_id}, {details}"
            )
        return _LoadedPrompt(
            spec=spec,
            template=string.Template(text),
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    @staticmethod
    def _template_variables(text: str, prompt_id: str) -> set[str]:
        variables: set[str] = set()
        for match in string.Template.pattern.finditer(text):
            if match.group("invalid") is not None:
                raise PromptCatalogError(
                    f"提示词包含无效 $ 占位符: id={prompt_id}, offset={match.start()}"
                )
            name = match.group("named") or match.group("braced")
            if name:
                variables.add(name)
        return variables


_registry: PromptRegistry | None = None
_registry_lock = threading.Lock()


def get_prompt_registry() -> PromptRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = PromptRegistry()
    return _registry


def validate_prompt_catalog() -> None:
    """启动阶段 fail-fast，并输出可用于审计的目录指纹。"""
    registry = get_prompt_registry()
    logger.info(
        "Prompt catalog loaded: prompts=%d catalog_sha256=%s refs=%s",
        len(registry.prompt_ids),
        registry.catalog_sha256,
        ",".join(registry.audit_refs()),
    )
