"""Runtime data paths, independent of the installed Python package location."""
from pathlib import Path
import tempfile

from aiive.config import settings


def data_root() -> Path:
    return Path(settings.aiive_data_dir.strip() or ".data").expanduser().resolve()


def data_path(*parts: str) -> Path:
    root = data_root()
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root):
        raise ValueError("data_path must remain inside AIIVE_DATA_DIR")
    return path


def resolve_configured_data_path(value: str | None, default_suffix: str) -> Path:
    raw = (value or "").strip()
    if not raw:
        return data_path(default_suffix)
    path = Path(raw).expanduser()
    if not path.is_absolute() and path.parts and path.parts[0] == ".data":
        return data_path(*path.parts[1:])
    return path.resolve()


def ensure_data_root() -> Path:
    root = data_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, prefix=".aiive-write-probe-", delete=True) as probe:
            payload = b"aiive-data-root-write-read-check"
            probe.write(payload)
            probe.flush()
            probe.seek(0)
            if probe.read() != payload:
                raise OSError("data root write/read verification failed")
    except OSError as exc:
        raise RuntimeError(f"AIIVE_DATA_DIR is not a writable directory: {root}: {exc}") from exc
    return root
