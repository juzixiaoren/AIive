"""Classify Shell effects and check explicit paths against task scope."""
from __future__ import annotations

import ntpath
import os
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from aiive.control.scope import path_within

SHELL_CAPABILITY_ID = "run_shell"
SHELL_CAPABILITIES = frozenset({SHELL_CAPABILITY_ID, "desktop_exec"})

# 只读命令白名单：仅读取文件/进程/系统信息，不写盘、不发起网络连接。
READ_ONLY_COMMANDS: frozenset[str] = frozenset({
    "ls", "cat", "head", "tail", "wc", "pwd", "echo", "printf", "date", "whoami",
    "id", "uname", "df", "du", "stat", "file", "sort", "uniq", "cut", "tr", "grep",
    "rg", "basename", "dirname", "realpath", "readlink", "which", "ps", "tree", "type", "dir",
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

FILE_COMMANDS = frozenset({
    "touch", "mkdir", "rmdir", "rm", "cp", "mv", "ln", "unlink", "truncate",
    "tee", "chmod", "chown", "chgrp", "install", "cmp", "diff", "find",
    "md", "rd", "del", "erase", "copy", "move", "ren",
    "rename", "mklink",
})
_SEPARATORS = frozenset({";", "&&", "||", "|", "\n"})
_REDIRECTIONS = frozenset({">", ">>", "<", "<>"})
_DYNAMIC = re.compile(r"[$`]")
# These options can execute programs or write output even under a normally read-only command.
_READER_EFFECTS = {
    "rg": ("--pre",),
    "sort": ("--output", "--compress-program"),
    "git": ("--output", "--ext-diff", "--textconv"),
    "file": ("-C", "--compile"),
}


@dataclass(frozen=True)
class ShellVerdict:
    read_only: bool
    uses_network: bool
    reason: str
    file_only: bool = False


def _command_tokens(command: str, *, windows_shell: bool = False) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    if windows_shell:
        lexer.escape = ""
    return list(lexer)


def classify_shell_command(command: str, *, windows_shell: bool = False) -> ShellVerdict:
    """Recognize scoped file commands, including pipelines and file redirects."""
    try:
        tokens = _command_tokens(command.strip(), windows_shell=windows_shell)
    except ValueError:
        return ShellVerdict(False, False, "unparsable_command_requires_approval")
    if not tokens:
        return ShellVerdict(False, False, "empty_command")
    parts = [[]]
    file_only = True
    redirects = False
    for token in tokens:
        if token in _SEPARATORS or (token and set(token) == {"\n"}):
            if parts[-1]:
                parts.append([])
        elif token in _REDIRECTIONS:
            redirects |= token != "<"
        elif token and set(token) <= set(";&|<>\n"):
            file_only = False
        else:
            parts[-1].append(token)
    read_only = not redirects
    uses_network = False
    for part in filter(None, parts):
        command_name = part[0].replace("\\", "/").casefold()
        executable = posixpath.basename(command_name)
        args = part[1:]
        network = executable in NETWORK_COMMANDS or (
            executable == "git" and bool(args) and args[0] in {"clone", "fetch", "pull", "push", "ls-remote"}
        )
        uses_network |= network
        reader = executable in READ_ONLY_COMMANDS and (
            executable != "git" or _git_read_only(args)
        ) and not any(
            arg == option or arg.startswith(option + "=") or (
                option.startswith("-") and not option.startswith("--") and arg.startswith(option)
            )
            for arg in args for option in _READER_EFFECTS.get(executable, ())
        )
        file_command = executable in FILE_COMMANDS and (
            executable != "find" or not any(arg in {"-exec", "-execdir", "-ok", "-okdir"} for arg in args)
        )
        if executable == "sed":
            expressions = [arg for arg in args if not arg.startswith("-")]
            expression = expressions[0] if expressions else ""
            if len(expression) > 1 and expression[0] == "s" and expression[1] in "/|#":
                fields = re.split(r"(?<!\\)" + re.escape(expression[1]), expression)
                file_command = len(fields) == 4 and bool(re.fullmatch(r"[gIp0-9]*", fields[-1])) and all(
                    re.fullmatch(r"-[iEnr]+(?:\.[\w-]+)?|--in-place(?:=\.[\w-]+)?|--", arg)
                    for arg in args if arg.startswith("-")
                )
        if executable == "sort":
            file_command = not any(arg.startswith("--compress") for arg in args)
            reader &= not any(
                arg.startswith(("--out", "--compress")) or (arg.startswith("-") and not arg.startswith("--") and "o" in arg)
                for arg in args
            )
        if command_name != executable:
            reader = file_command = False
        read_only &= reader
        file_only &= (reader or file_command)
    if _DYNAMIC.search(command) or (windows_shell and re.search(r"%[^%]+%", command)):
        return ShellVerdict(False, uses_network, "dynamic_path_requires_approval")
    if file_only and read_only:
        return ShellVerdict(True, False, "read_only_command", file_only=True)
    if file_only:
        return ShellVerdict(False, False, "filesystem_command", file_only=True)
    return ShellVerdict(False, uses_network, "unclassified_command_requires_approval")


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


_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(/.*)?$")
_CMD_FILE_SWITCHES = re.compile(r"/(?:-?y|[abdefpqsvw]|a:[-a-z]+|a[dh]|o:[-a-z]+)$", re.IGNORECASE)


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
    command: str, *, cwd: str, allowed_roots: tuple[str, ...] | list[str] | None,
    remote: bool = False,
    excluded_root: str = "",
) -> tuple[list[str], bool]:
    """Check explicit arguments, including relative paths and redirect targets."""
    windows_shell = remote and bool(ntpath.splitdrive(cwd)[0])
    try:
        argv = _command_tokens(command, windows_shell=windows_shell)
    except ValueError:
        return [], True
    violations: list[str] = []
    dynamic = bool(_DYNAMIC.search(command) or (windows_shell and re.search(r"%[^%]+%", command)))
    path_module = ntpath if windows_shell else posixpath
    command_start = True
    executable = ""
    redirect_target = False
    for raw in argv:
        if raw in _SEPARATORS or (raw and set(raw) == {"\n"}):
            command_start, redirect_target = True, False
            continue
        if raw in _REDIRECTIONS:
            redirect_target = True
            continue
        if command_start:
            command_start = False
            executable = raw.casefold()
            continue
        token = raw.split("=", 1)[1] if raw.startswith("--") and "=" in raw else raw
        is_target = redirect_target
        redirect_target = False
        if executable == "sort" and token.startswith("-o") and len(token) > 2:
            token = token[2:]
        if not token or token.startswith("-") or _DYNAMIC.search(token):
            continue
        if (executable in {"echo", "printf"} or (windows_shell and _CMD_FILE_SWITCHES.fullmatch(token))) and not is_target:
            continue
        if remote:
            if token.startswith("~"):
                dynamic = True
                continue
            resolved = path_module.normpath(path_module.join(cwd, token))
        else:
            candidate = Path(_windows_from_msys(token)).expanduser()
            if not candidate.is_absolute():
                candidate = (Path(cwd).expanduser() if cwd else Path.cwd()) / candidate
            try:
                resolved = str(candidate.resolve())
            except (OSError, RuntimeError):
                violations.append(token)
                continue
        if excluded_root and (path_within(resolved, excluded_root) or path_within(excluded_root, resolved)):
            violations.append(token)
        elif allowed_roots is not None and not any(path_within(resolved, root) for root in allowed_roots):
            violations.append(token)
    return violations, dynamic
