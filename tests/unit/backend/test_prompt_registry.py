"""版本化 Markdown Prompt 目录的加载、渲染与审计测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiive.prompts import PromptCatalogError, PromptRegistry, get_prompt_registry


def _write_catalog(
    root: Path,
    *,
    template: str = "Hello ${name}; JSON: {\"ok\": true}",
    variables: str = '["name"]',
) -> None:
    (root / "sample.md").write_text(template, encoding="utf-8")
    (root / "manifest.toml").write_text(
        "\n".join(
            [
                '[prompts."sample"]',
                'file = "sample.md"',
                'version = "1.2.3"',
                'role = "user"',
                f"variables = {variables}",
            ]
        ),
        encoding="utf-8",
    )


def test_packaged_catalog_loads_and_has_audit_identity() -> None:
    registry = get_prompt_registry()

    assert len(registry.prompt_ids) == 10
    assert "agent.stable_contract" in registry.prompt_ids
    assert len(registry.catalog_sha256) == 64
    assert all("@" in ref and "#" in ref for ref in registry.audit_refs())


def test_render_is_strict_and_preserves_json_braces_and_dollar_values(
    tmp_path: Path,
) -> None:
    _write_catalog(tmp_path)
    registry = PromptRegistry(tmp_path)

    rendered = registry.render("sample", name="A $5 {value}")

    assert rendered.content == 'Hello A $5 {value}; JSON: {"ok": true}'
    assert rendered.audit_ref.startswith("sample@1.2.3#")
    with pytest.raises(PromptCatalogError, match="missing=\\['name'\\]"):
        registry.render("sample")
    with pytest.raises(PromptCatalogError, match="extra=\\['unused'\\]"):
        registry.render("sample", name="A", unused="B")


def test_catalog_rejects_manifest_template_variable_drift(tmp_path: Path) -> None:
    _write_catalog(tmp_path, template="Hello ${name} ${undeclared}")

    with pytest.raises(PromptCatalogError, match="占位变量与 manifest 不一致"):
        PromptRegistry(tmp_path)


def test_catalog_rejects_invalid_dollar_placeholder(tmp_path: Path) -> None:
    _write_catalog(tmp_path, template="Cost is $5 for ${name}")

    with pytest.raises(PromptCatalogError, match=r"无效 \$ 占位符"):
        PromptRegistry(tmp_path)
