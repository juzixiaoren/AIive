"""Run from repository root against a disposable Compose test stack."""
import json
import subprocess
import time

PROBE = r"""
import hashlib, json, sys
from pathlib import Path
from aiive.config import settings
from aiive.storage.data_paths import data_root, resolve_configured_data_path
from aiive.storage import object_store
from aiive.auth.security import signing_secret
from aiive.mcp.installer import SANDBOX_ROOT
from aiive.knowledge.access import allowed_knowledge_roots
from aiive.runtime.tool_output_store import tool_output_root
root = data_root()
assert root == Path('/app/.data'), root
assert object_store.ROOT == root / 'object_store'
paths = [object_store.ROOT, SANDBOX_ROOT, tool_output_root(),
 resolve_configured_data_path(settings.aiive_memory_file_projection_dir, 'memory_projection'),
 resolve_configured_data_path(settings.aiive_auth_secret_file, 'auth_secret')]
assert root / 'knowledge' in allowed_knowledge_roots()
assert all(p.is_relative_to(root) for p in paths), paths
payload = b'aiive-compose-persistence-v1'
if len(sys.argv) > 1:
    previous = json.loads(sys.argv[1])
    ref = object_store.ObjectRef(previous['bucket'], previous['key'])
    digest = previous['sha256']
else:
    ref, digest = object_store.put_content_addressed('ci-persistence', payload)
assert object_store.get_verified(ref, digest) == payload
result = dict(root=str(root), paths=[str(p) for p in paths], bucket=ref.bucket,
 key=ref.key, sha256=digest, secret_sha256=hashlib.sha256(signing_secret().encode()).hexdigest())
if len(sys.argv) > 1:
    assert result == previous, (result, previous)
print(json.dumps(result, sort_keys=True))
"""


def compose(*args):
    return subprocess.check_output(['docker', 'compose', *args], text=True).strip()


def main():
    before = compose('exec', '-T', 'backend', 'python', '-c', PROBE)
    print('Before recreate:', before, flush=True)
    compose('up', '-d', '--force-recreate', 'backend')
    for _ in range(60):
        probe = subprocess.run(['docker', 'compose', 'exec', '-T', 'backend',
                                'curl', '-fsS', 'http://localhost:8000/health'], capture_output=True)
        if probe.returncode == 0:
            break
        time.sleep(3)
    else:
        raise RuntimeError('Recreated backend did not become healthy')
    previous = json.dumps(json.loads(before))
    after = compose('exec', '-T', 'backend', 'python', '-c', PROBE, previous)
    print('After recreate:', after, flush=True)


if __name__ == '__main__':
    main()
