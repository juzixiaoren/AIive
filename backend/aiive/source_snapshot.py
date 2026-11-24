"""Source-tree exclusions shared by project discovery and deployment snapshots."""
from pathlib import PurePosixPath

EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".next", "coverage",
    ".data", "slots", "runtime", "pgdata", "postgres", "qdrant", "object_store", "logs",
})
EXCLUDED_FILES = frozenset({
    "id_rsa", "id_ed25519", "credentials.json", ".netrc", ".npmrc", ".pypirc",
    "keystore.properties", "signing.properties",
})
EXCLUDED_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".sqlite", ".sqlite3", ".db",
    ".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm", ".db-wal", ".db-shm",
    ".log", ".pyc", ".pyo", ".egg-info",
)


def excluded_source_path(relative: PurePosixPath, *, directory: bool = False) -> bool:
    parts = tuple(part.lower() for part in relative.parts)
    directories = parts if directory else parts[:-1]
    for index, part in enumerate(directories):
        if (index == 0 and part == "data") or part.endswith(".egg-info"):
            return True
        if part in EXCLUDED_DIRS and parts[:index + 1] != ("backend", "aiive", "runtime"):
            return True
    name = relative.name.lower()
    return (
        name in EXCLUDED_FILES or name == ".git" or name.endswith(EXCLUDED_SUFFIXES)
        or (name.startswith(".env") and name != ".env.example")
    )
