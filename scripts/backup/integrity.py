"""Validate snapshot completeness without extracting untrusted tar entries."""
import hashlib
import json
import sys
import tarfile
from pathlib import Path, PurePosixPath

root = Path(sys.argv[2])
required = {"audit.json", "database.dump", "data.tar", "metadata.txt", "database-version.txt",
            "config/.env", "config/docker-compose.yml", "config/backend/Dockerfile",
            "config/deploy/pg/Dockerfile"}
def digest(p):
    with p.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
files = {p.relative_to(root).as_posix(): digest(p) for p in root.rglob("*")
         if p.is_file() and p.name != "SHA256.json"}
assert required <= files.keys(), "Incomplete snapshot"
if sys.argv[1] == "create":
    (root / "SHA256.json").write_text(json.dumps(files, sort_keys=True))
else:
    assert files == json.loads((root / "SHA256.json").read_text()), "SHA256 mismatch"
with tarfile.open(root / "data.tar") as archive:
    for member in archive:
        path = PurePosixPath(member.name)
        assert not path.is_absolute() and ".." not in path.parts, "Unsafe archive path"
        assert not member.isdev() and not member.isfifo(), "Unsafe archive entry"
        if member.issym() or member.islnk():
            target = PurePosixPath(member.linkname)
            assert not target.is_absolute() and ".." not in target.parts, "Unsafe link"
print("Snapshot files and SHA256 verified")
