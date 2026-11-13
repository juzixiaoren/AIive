"""Read-only recovery inventory: all public table counts and persistent file hashes."""
import hashlib
import json
from pathlib import Path
from sqlalchemy import create_engine, text
from aiive.config import settings
from aiive.storage.data_paths import data_root
engine = create_engine(settings.database_url)
with engine.connect() as conn:
    tables = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")).scalars().all()
    counts = {name: conn.execute(text('SELECT count(*) FROM "' + name.replace('"', '""') + '"')).scalar() for name in tables}
    rows = {}
    for name in tables:
        quoted = '"' + name.replace('"', '""') + '"'
        hashes = conn.execute(text('SELECT md5(row_to_json(t)::text) FROM ' + quoted + ' t ORDER BY 1')).scalars()
        h = hashlib.sha256()
        for value in hashes:
            h.update(value.encode())
        rows[name] = h.hexdigest()
root = data_root()
files = {}
for p in sorted(root.rglob('*')):
    if p.is_file():
        with p.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        files[p.relative_to(root).as_posix()] = digest
        if '/sha256/' in p.as_posix():
            assert p.name == digest, 'Content-addressed object mismatch'
print(json.dumps(dict(tables=counts, rows=rows, files=files), sort_keys=True))
