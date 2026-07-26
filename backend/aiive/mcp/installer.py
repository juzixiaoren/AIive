"""
MCP 安装器：管理 MCP（Model Context Protocol）服务器的真实沙箱安装和冒烟记账。

真实安装流程：
1. 包名白名单校验（只允许 discovery 目录中列出的 npm 包）
2. `npm install <package> --prefix <sandbox_dir>` 真实安装到仓库根 .data/mcp_sandbox/
3. 解析已安装包 package.json 的 bin 字段，得到 stdio server 的 js 入口
4. 将启动信息（entry_js / args / env_keys / sandbox_path）写入 Capability.definition["launch"]
   与 MCPInstallRecord.sandbox_path，供运行时客户端拼装启动命令。

安全约束：启动命令严格由安装记录的 bin 入口拼装，不接受调用方传入的命令字符串。
"""

import hashlib
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import Capability, CapabilityVersion, MCPInstallRecord

logger = logging.getLogger(__name__)

# npm install 超时（秒）
NPM_INSTALL_TIMEOUT_SECONDS = 300

# 仓库根目录：backend/aiive/mcp/installer.py 向上 3 级
_REPO_ROOT = Path(__file__).resolve().parents[3]
# 沙箱根目录（跨平台，不用 /tmp）
SANDBOX_ROOT = _REPO_ROOT / ".data" / "mcp_sandbox"


def _hash(data: str) -> str:
    """计算数据的 SHA256 哈希（取前 16 位）。

    参数:
        data: 要哈希的字符串

    返回:
        16 位十六进制哈希字符串
    """
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def sanitize_dir_name(name: str) -> str:
    """把候选名称转换为跨平台安全的目录名（@scope/pkg → scope_pkg）。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return cleaned or "unnamed"


def _npm_package_from_ref(package_ref: str) -> str | None:
    """从 package_ref（npm:xxx）解析出 npm 包名；非 npm 引用返回 None。"""
    if not package_ref.startswith("npm:"):
        return None
    return package_ref[len("npm:"):].strip() or None


def _resolve_bin_entry(sandbox_dir: Path, package_name: str) -> Path | None:
    """读取已安装包 package.json 的 bin 字段，解析 stdio server 的 js 入口。

    bin 可能是字符串或 {命令名: 相对路径} 字典；取第一个可用条目。
    没有 bin 时回退到 main 字段。

    返回:
        入口 js 的绝对路径；解析失败返回 None
    """
    pkg_dir = sandbox_dir / "node_modules" / Path(*package_name.split("/"))
    pkg_json_path = pkg_dir / "package.json"
    if not pkg_json_path.is_file():
        logger.warning("MCP 安装后未找到 package.json: %s", pkg_json_path)
        return None
    try:
        pkg_meta = json.loads(pkg_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("MCP package.json 解析失败: %s", pkg_json_path, exc_info=True)
        return None

    bin_field = pkg_meta.get("bin")
    entry_rel: str | None = None
    if isinstance(bin_field, str):
        entry_rel = bin_field
    elif isinstance(bin_field, dict) and bin_field:
        entry_rel = str(next(iter(bin_field.values())))
    if not entry_rel:
        main_field = pkg_meta.get("main")
        entry_rel = str(main_field) if main_field else None
    if not entry_rel:
        return None

    entry_path = (pkg_dir / entry_rel).resolve()
    # 入口必须落在沙箱目录内（防止 package.json 恶意跳转）
    try:
        entry_path.relative_to(sandbox_dir.resolve())
    except ValueError:
        logger.warning("MCP bin 入口越出沙箱目录，拒绝: %s", entry_path)
        return None
    if not entry_path.is_file():
        logger.warning("MCP bin 入口文件不存在: %s", entry_path)
        return None
    return entry_path


def _run_npm_install(
    package_name: str, version: str, sandbox_dir: Path
) -> dict[str, Any]:
    """执行真实 npm 安装并解析 bin 入口。

    返回:
        {"ok": bool, "entry_js": str | None, "error": str | None,
         "sandbox_path": str}
    """
    npm_path = shutil.which("npm")
    if not npm_path:
        return {"ok": False, "entry_js": None, "error": "npm not found in PATH",
                "sandbox_path": str(sandbox_dir)}

    sandbox_dir.mkdir(parents=True, exist_ok=True)
    pkg_spec = package_name if version in ("", "latest") else f"{package_name}@{version}"
    cmd = [
        npm_path, "install", pkg_spec,
        "--prefix", str(sandbox_dir),
        "--no-audit", "--no-fund", "--loglevel", "error",
    ]
    logger.info("MCP npm install: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=NPM_INSTALL_TIMEOUT_SECONDS,
            cwd=str(sandbox_dir),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "entry_js": None,
                "error": f"npm install timed out after {NPM_INSTALL_TIMEOUT_SECONDS}s",
                "sandbox_path": str(sandbox_dir)}
    except OSError as e:
        return {"ok": False, "entry_js": None, "error": f"npm launch failed: {e}",
                "sandbox_path": str(sandbox_dir)}

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "")[-800:]
        return {"ok": False, "entry_js": None,
                "error": f"npm install exit={proc.returncode}: {stderr_tail}",
                "sandbox_path": str(sandbox_dir)}

    entry = _resolve_bin_entry(sandbox_dir, package_name)
    if entry is None:
        return {"ok": False, "entry_js": None,
                "error": f"installed but no runnable bin entry found for {package_name}",
                "sandbox_path": str(sandbox_dir)}
    return {"ok": True, "entry_js": str(entry), "error": None,
            "sandbox_path": str(sandbox_dir)}


def install_sandbox(
    db: Session,
    candidate_name: str,
    package_ref: str,
    version: str,
    transport: str,
    declared_tools: list[str],
    definition: dict[str, Any],
    *,
    launch_args: list[str] | None = None,
    env_keys: list[str] | None = None,
) -> dict[str, Any]:
    """将 MCP 候选服务器真实安装到沙箱环境并记账。

    流程：
    1. 包名白名单校验（只接受 discovery 目录中列出的 npm 包）
    2. 真实执行 npm install 到仓库根 .data/mcp_sandbox/{sanitized_name}/
    3. 解析 bin 入口，把启动信息写入 Capability.definition["launch"]
    4. 创建/更新 Capability、MCPInstallRecord、CapabilityVersion 记录

    参数:
        db: 数据库会话
        candidate_name: 候选服务器名称
        package_ref: 包引用（如 npm:@modelcontextprotocol/server-filesystem）
        version: 版本号
        transport: 通信传输方式
        declared_tools: 声明的工具列表
        definition: 服务器定义字典（含 name、description）
        launch_args: 启动 server 时附加的命令行参数（如 filesystem server 的
                     允许目录）；仅作为参数传给 bin 入口，不构成命令本身
        env_keys: 运行时需要从宿主环境透传的环境变量名列表（如 API key）

    返回:
        安装结果字典；成功时包含 capability_id、state、entry_js 等，
        失败时如实返回 {"ok": False, "installed": False, "error": ...}
    """
    if transport != "stdio":
        return {"ok": False, "installed": False,
                "error": f"unsupported transport: {transport} (only stdio)"}

    package_name = _npm_package_from_ref(package_ref)
    if not package_name:
        return {"ok": False, "installed": False,
                "error": f"unsupported package_ref (only npm:): {package_ref}"}

    # 白名单校验：只允许安装 discovery 目录中列出的包
    from aiive.mcp.discovery import allowed_npm_packages
    if package_name not in allowed_npm_packages():
        return {"ok": False, "installed": False,
                "error": f"package not in MCP catalog whitelist: {package_name}"}

    sandbox_dir = SANDBOX_ROOT / sanitize_dir_name(candidate_name)
    npm_result = _run_npm_install(package_name, version, sandbox_dir)
    if not npm_result["ok"]:
        logger.warning("MCP 沙箱安装失败: %s error=%s", candidate_name, npm_result["error"])
        return {"ok": False, "installed": False, "error": npm_result["error"]}

    # 启动信息：运行时客户端只用它拼装 `node <entry_js> <args...>`
    launch_config: dict[str, Any] = {
        "runner": "node",
        "entry_js": npm_result["entry_js"],
        "args": [str(a) for a in (launch_args or [])],
        "env_keys": [str(k) for k in (env_keys or [])],
        "sandbox_path": npm_result["sandbox_path"],
        "package": package_name,
    }
    definition = dict(definition)
    definition["launch"] = launch_config

    capability_id = f"mcp:{candidate_name}"
    descriptor_hash = _hash(json.dumps(
        {k: v for k, v in definition.items() if k != "launch"}, sort_keys=True,
    ))
    tool_list_hash = _hash(",".join(sorted(declared_tools)))

    # 检查是否已有同名能力
    existing = (
        db.query(Capability)
        .filter(Capability.capability_id == capability_id)
        .first()
    )

    if existing:
        # 已有能力：如果描述符变化，标记为需要重新审查
        if existing.descriptor_hash and existing.descriptor_hash != descriptor_hash:
            existing.state = "needs_review"
        existing.descriptor_hash = descriptor_hash
        # 合并 launch 等最新定义信息（重装后入口路径可能变化）
        merged = dict(existing.definition or {})
        merged.update(definition)
        existing.definition = merged
        db.flush()
        cap = existing
    else:
        # 新建能力：初始状态为 sandbox
        cap = Capability(
            capability_id=capability_id,
            name=candidate_name,
            state="sandbox",
            descriptor_hash=descriptor_hash,
            definition=definition,
        )
        db.add(cap)
        db.flush()

    # 记录安装信息（sandbox_path 为真实目录）
    install = MCPInstallRecord(
        capability_id=cap.id,
        server_name=candidate_name,
        package_ref=package_ref,
        version=version,
        transport=transport,
        declared_tools=list(declared_tools),
        sandbox_path=npm_result["sandbox_path"],
    )
    db.add(install)

    # 记录版本信息
    cv = CapabilityVersion(
        capability_id=cap.id,
        version=version,
        descriptor_hash=descriptor_hash,
        tool_list_hash=tool_list_hash,
    )
    db.add(cv)
    db.flush()

    return {
        "ok": True,
        "capability_id": cap.capability_id,
        "state": cap.state,
        "descriptor_hash": descriptor_hash,
        "tool_list_hash": tool_list_hash,
        "entry_js": npm_result["entry_js"],
        "sandbox_path": npm_result["sandbox_path"],
        "installed": True,
    }


def run_smoke(
    db: Session,
    capability_id: str,
    smoke_result: dict[str, Any],
) -> dict[str, Any]:
    """记录 MCP 能力的冒烟测试结果并推进状态机。

    冒烟测试通过 -> 状态变为 active
    冒烟测试失败 -> 状态变为 needs_review

    允许从 sandbox 或 needs_review 状态发起冒烟（修复单向死路：
    此前一旦进入 needs_review 便无法再次冒烟）。

    参数:
        db: 数据库会话
        capability_id: 能力标识符
        smoke_result: 冒烟测试结果字典（含 ok 字段）

    返回:
        测试结果字典，包含 capability_id、state、smoke_passed
    """
    cap = (
        db.query(Capability)
        .filter(Capability.capability_id == capability_id)
        .first()
    )
    if not cap:
        return {"ok": False, "error": f"Capability not found: {capability_id}"}

    if cap.state not in ("sandbox", "needs_review"):
        return {
            "ok": False,
            "error": f"Capability must be in sandbox/needs_review, current: {cap.state}",
        }

    # 更新最新版本的冒烟测试结果
    version = (
        db.query(CapabilityVersion)
        .filter(CapabilityVersion.capability_id == cap.id)
        .order_by(CapabilityVersion.created_at.desc())
        .first()
    )
    if version:
        version.smoke_result = smoke_result
        # 冒烟拿到了真实工具列表时，用真实列表更新 tool_list_hash
        real_tools = smoke_result.get("real_tools")
        if isinstance(real_tools, list) and real_tools:
            version.tool_list_hash = _hash(",".join(sorted(str(t) for t in real_tools)))

    # 根据冒烟测试结果更新能力状态
    if smoke_result.get("ok") is True:
        cap.state = "active"
    else:
        cap.state = "needs_review"

    db.flush()
    return {
        "ok": True,
        "capability_id": cap.capability_id,
        "state": cap.state,
        "smoke_passed": smoke_result.get("ok") is True,
    }
