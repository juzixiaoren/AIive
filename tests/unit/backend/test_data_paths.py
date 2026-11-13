from pathlib import Path

import pytest

from aiive.config import settings
from aiive.storage import data_paths


def test_roots_and_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, 'aiive_data_dir', '.data')
    assert data_paths.data_root() == tmp_path / '.data'
    monkeypatch.setattr(settings, 'aiive_data_dir', 'custom')
    assert data_paths.data_root() == tmp_path / 'custom'
    fixed = tmp_path / 'absolute'
    monkeypatch.setattr(settings, 'aiive_data_dir', str(fixed))
    nested = tmp_path / 'nested'
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert data_paths.data_root() == fixed
    assert data_paths.resolve_configured_data_path('.data/foo', 'unused') == fixed / 'foo'
    assert data_paths.resolve_configured_data_path('', 'foo') == fixed / 'foo'
    assert data_paths.resolve_configured_data_path(str(tmp_path), 'foo') == tmp_path
    assert data_paths.resolve_configured_data_path('relative', 'foo') == nested / 'relative'
    monkeypatch.setattr(settings, 'aiive_data_dir', '.data')
    assert data_paths.data_root() == nested / '.data'
    with pytest.raises(ValueError):
        data_paths.data_path('..', 'escape')


def test_probe(tmp_path, monkeypatch):
    root = tmp_path / 'new'
    monkeypatch.setattr(settings, 'aiive_data_dir', str(root))
    assert data_paths.ensure_data_root() == root
    assert list(root.iterdir()) == []
    file = tmp_path / 'file'
    file.write_text('keep')
    monkeypatch.setattr(settings, 'aiive_data_dir', str(file))
    with pytest.raises(RuntimeError, match='AIIVE_DATA_DIR'):
        data_paths.ensure_data_root()
    assert file.read_text() == 'keep'
    monkeypatch.setattr(settings, 'aiive_data_dir', str(root))
    def denied(*args, **kwargs):
        raise PermissionError('denied')
    monkeypatch.setattr(data_paths.tempfile, 'NamedTemporaryFile', denied)
    with pytest.raises(RuntimeError, match='denied'):
        data_paths.ensure_data_root()


def test_consumers(tmp_path, monkeypatch):
    from aiive.storage import object_store
    from aiive.mcp import installer
    from aiive.knowledge.access import allowed_knowledge_roots
    from aiive.runtime.tool_output_store import tool_output_root
    from aiive.auth import security
    from aiive.tools import safe_delete
    from aiive.supervisor.control import ControlPaths

    monkeypatch.setattr(settings, 'aiive_data_dir', str(tmp_path))
    monkeypatch.setattr(object_store, 'ROOT', data_paths.data_path('object_store'))
    monkeypatch.setattr(installer, 'SANDBOX_ROOT', data_paths.data_path('mcp_sandbox'))
    monkeypatch.setattr(settings, 'aiive_knowledge_roots', '')
    monkeypatch.setattr(settings, 'tool_output_dir', '.data/tool_outputs')
    monkeypatch.setattr(settings, 'aiive_auth_secret', '')
    monkeypatch.setattr(settings, 'aiive_auth_secret_file', '.data/auth_secret')
    monkeypatch.setattr(settings, 'supervisor_state_dir', '')
    assert tmp_path / 'knowledge' in allowed_knowledge_roots()
    assert tool_output_root() == tmp_path / 'tool_outputs'
    assert ControlPaths.resolve().base == tmp_path / 'runtime/supervisor'
    assert security.signing_secret() == (tmp_path / 'auth_secret').read_text()
    registry = safe_delete._build_default_registry()
    assert registry.get_scope_root('object_store') == object_store.ROOT
    registry.register('wide', safe_delete.REPO_ROOT)
    assert not safe_delete.safe_delete(str(safe_delete.REPO_ROOT), 'wide', registry=registry).allowed


def test_fresh_process_consumers(tmp_path):
    import os
    import subprocess
    import sys

    env = dict(os.environ, AIIVE_DATA_DIR=str(tmp_path))
    result = subprocess.run([sys.executable, '-c', """
from aiive.storage.data_paths import data_root, resolve_configured_data_path
from aiive.storage.object_store import ROOT
from aiive.mcp.installer import SANDBOX_ROOT
from aiive.config import settings
from aiive.runtime.tool_output_store import tool_output_root
from aiive.auth.security import signing_secret
root = data_root()
assert ROOT == root / 'object_store'
assert SANDBOX_ROOT == root / 'mcp_sandbox'
settings.tool_output_dir = '.data/tool_outputs'
assert tool_output_root() == root / 'tool_outputs'
assert resolve_configured_data_path('.data/memory_projection', 'memory_projection') == root / 'memory_projection'
settings.aiive_auth_secret = ''
settings.aiive_auth_secret_file = '.data/auth_secret'
assert signing_secret() == (root / 'auth_secret').read_text()
"""], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
