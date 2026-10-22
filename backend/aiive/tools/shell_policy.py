"""本机 Shell 命令的静态风险分类：只决定审批/网络校验，不参与执行。

设计原则：
- 只有"单条、无复合操作符、可执行文件在只读白名单内"的命令才算只读，直接放行；
- 其余一律进入审批（包括 rm/mv/dd/git push 等），不做"一概禁止"；
- 命令里出现 `; | & > < ` $()` 换行等复合结构时，即使首命令只读也按非只读处理，
  避免 `cat x; rm -rf y` 这类拼接绕过。
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from aiive.control.scope import path_within

SHELL_CAPABILITY_ID = "run_shell"

# 只读命令白名单：仅读取文件/进程/系统信息，不写盘、不发起网络连接。
READ_ONLY_COMMANDS: frozenset[str] = frozenset({
    "ls", "cat", "head", "tail", "wc", "pwd", "echo", "printf", "date", "whoami",
    "id", "uname", "df", "du", "stat", "file", "sort", "uniq", "cut", "tr", "grep",
    "rg", "basename", "dirname", "realpath", "readlink", "which", "ps", "tree",
    # git 需再按子命令细分（见 _git_read_only），只有列举/查看类放行。
    "git",
})

# 会访问网络的命令：除审批外还要求任务 allow_network。
NETWORK_COMMANDS: frozenset[str] = frozenset({
    "curl", "wget", "nc", "netcat", "ncat", "ssh", "scp", "sftp", "rsync",
    "ping", "dig", "nslookup", "host", "telnet", "ftp",
})

GIT_READ_ONLY_SUBCOMMANDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "rev-parse", "describe", "blame", "ls-files",
    "ls-tree", "shortlog", "reflog", "grep",
})
_GIT_BRANCH_LIST_FLAGS = frozenset({"-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--show-current"})
_GIT_REMOTE_LIST_FLAGS = frozenset({"-v", "--verbose"})

# 复合命令结构：出现任意一个就不按只读放行。
_COMPOUND = re.compile(r"[;&|<>`\n\r]|\$\(")


@dataclass(frozen=True)
class ShellVerdict:
    """一次分类结论。read_only=True 才允许跳过审批。"""

    read_only: bool
    uses_network: bool
    reason: str


def classify_shell_command(command: str) -> ShellVerdict:
    """对命令做保守的静态分类；无法确定时一律视为需要审批。"""
    text = (command or "").strip()
    if not text:
        return ShellVerdict(read_only=False, uses_network=False, reason="empty_command")
    if _COMPOUND.search(text):
        return ShellVerdict(False, False, "compound_command_requires_approval")
    try:
        argv = shlex.split(text, posix=True)
    except ValueError:
        return ShellVerdict(False, False, "unparsable_command_requires_approval")
    if not argv:
        return ShellVerdict(False, False, "empty_command")
    executable = Path(argv[0]).name.casefold()
    if executable in NETWORK_COMMANDS:
        return ShellVerdict(False, True, "network_command_requires_network_scope")
    if executable not in READ_ONLY_COMMANDS:
        return ShellVerdict(False, False, "unclassified_command_requires_approval")
    if executable == "git" and not _git_read_only(argv[1:]):
        return ShellVerdict(False, False, "git_write_subcommand_requires_approval")
    return ShellVerdict(True, False, "read_only_command")


def _git_read_only(args: list[str]) -> bool:
    """git 只放行明确只读的子命令；branch/remote 仅放行列举形式。"""
    if not args:
        return False
    subcommand = args[0].casefold()
    rest = args[1:]
    if subcommand in GIT_READ_ONLY_SUBCOMMANDS:
        return True
    if subcommand == "branch":
        return all(flag in _GIT_BRANCH_LIST_FLAGS for flag in rest)
    if subcommand == "remote":
        return all(flag in _GIT_REMOTE_LIST_FLAGS for flag in rest)
    return False


# 形如绝对路径 / 家目录 / 上级目录 / 含分隔符的相对路径，需要做 scope 校验。
_PATHLIKE = re.compile(r"^([A-Za-z]:[\\/]|/|~|\.\.|.*[\\/])")
_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(/.*)?$")


def _windows_from_msys(token: str) -> str:
    """Windows 上把 Git Bash 的 /c/foo 还原成 C:/foo，避免把作用域内的路径判成越界。"""
    if os.name != "nt":
        return token
    match = _MSYS_DRIVE.match(token)
    if not match:
        return token
    rest = (match.group(2) or "").lstrip("/")
    return f"{match.group(1).upper()}:/{rest}"


def shell_path_violations(
    command: str, *, cwd: str, allowed_roots: tuple[str, ...] | list[str],
) -> tuple[list[str], bool]:
    """保守检查命令中的路径参数是否越出 allowed_roots。

    返回 (违规 token 列表, 是否存在无法静态解析的动态路径)。动态路径（$VAR / 反引号）
    无法静态判定，交由调用方要求审批，而不是直接放行。
    """
    try:
        argv = shlex.split(command or "", posix=True)
    except ValueError:
        return [], True
    if not argv:
        return [], False
    base = Path(cwd).expanduser() if cwd else Path.cwd()
    violations: list[str] = []
    dynamic = False
    for raw in argv[1:]:
        token = raw.split("=", 1)[1] if raw.startswith("--") and "=" in raw else raw
        if not token or token.startswith("-"):
            continue
        if not _PATHLIKE.match(token):
            continue
        if "$" in token or "`" in token:
            dynamic = True
            continue
        candidate = Path(_windows_from_msys(token)).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if not any(path_within(str(resolved), root) for root in allowed_roots):
            violations.append(token)
    return violations, dynamic
