"""Detect circular import cycles in the `aiive` package.

Mirrors (approximately) how pyright's reportImportCycles works:
- Only *runtime* imports create edges.
- Imports inside a `TYPE_CHECKING` block are excluded (they never execute at runtime).
- Imports inside function/class bodies are excluded (they are deferred).
- Relative imports are resolved against the module's package path.

Usage:
    python3 detect_cycles.py [root_package_dir] [project_root]
"""
from __future__ import annotations

import ast
import sys
from typing import override
from pathlib import Path

PROJECT_ROOT = Path("/Users/donkluo/Documents/UGit/AIive/backend")
PACKAGE_DIR = PROJECT_ROOT / "aiive"


def module_name_for(path: Path) -> str:
    """Return the dotted module name for an absolute file path under PACKAGE_DIR."""
    rel = path.relative_to(PACKAGE_DIR)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return "aiive." + ".".join(parts) if parts else "aiive"


def resolve_relative(current_module: str, level: int, module: str | None) -> str | None:
    """Resolve a relative import (level>0) to an absolute internal module name."""
    cur_parts = current_module.split(".")
    # level=1 means current package; strip `level-1` extra components
    base_parts = cur_parts[: len(cur_parts) - (level - 1)]
    if module:
        return ".".join(base_parts + module.split("."))
    return ".".join(base_parts)


def is_internal(module: str) -> bool:
    return module == "aiive" or module.startswith("aiive.")


def collect_file_imports(path: Path):
    """Return (runtime_edges, typecheck_edges, deferred_edges) as sets of target module names."""
    runtime: set[str] = set()
    typecheck: set[str] = set()
    deferred: set[str] = set()

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return runtime, typecheck, deferred

    current_module = module_name_for(path)

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.in_typecheck = False
            self.in_func = 0
            self.runtime: set[str] = set()
            self.typecheck: set[str] = set()
            self.deferred: set[str] = set()

        def _targets_from_node(self, node) -> set[str]:
            targets: set[str] = set()
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if is_internal(alias.name):
                        targets.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    resolved = resolve_relative(current_module, node.level, node.module)
                    if resolved and is_internal(resolved):
                        targets.add(resolved)
                elif node.module and is_internal(node.module):
                    targets.add(node.module)
            return targets

        @override
        def visit_Import(self, node):
            targets = self._targets_from_node(node)
            if self.in_typecheck:
                self.typecheck |= targets
            elif self.in_func:
                self.deferred |= targets
            else:
                self.runtime |= targets

        @override
        def visit_ImportFrom(self, node):
            targets = self._targets_from_node(node)
            if self.in_typecheck:
                self.typecheck |= targets
            elif self.in_func:
                self.deferred |= targets
            else:
                self.runtime |= targets

        @override
        def visit_FunctionDef(self, node):
            self.in_func += 1
            self.generic_visit(node)
            self.in_func -= 1

        @override
        def visit_AsyncFunctionDef(self, node):
            self.in_func += 1
            self.generic_visit(node)
            self.in_func -= 1

        @override
        def visit_ClassDef(self, node):
            # class body imports count as deferred (not runtime at module import)
            self.in_func += 1
            self.generic_visit(node)
            self.in_func -= 1

        @override
        def visit_If(self, node):
            is_tc = False
            test = node.test
            if isinstance(test, ast.Name):
                is_tc = test.id == "TYPE_CHECKING"
            elif isinstance(test, ast.Attribute):
                if isinstance(test.value, ast.Name) and test.value.id in ("typing", "typing_extensions"):
                    is_tc = test.attr == "TYPE_CHECKING"
            if is_tc:
                self.in_typecheck = True
                for stmt in node.body:
                    self.visit(stmt)
                self.in_typecheck = False
            else:
                self.generic_visit(node)

    v = Visitor()
    v.visit(tree)
    return v.runtime, v.typecheck, v.deferred


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else str(PACKAGE_DIR)
    root = Path(root)
    files = sorted(root.rglob("*.py"))

    graph: dict[str, set[str]] = {}
    for f in files:
        if f.name == "detect_cycles.py":
            continue
        mod = module_name_for(f)
        runtime, _tc, _de = collect_file_imports(f)
        graph.setdefault(mod, set())
        for t in runtime:
            graph[mod].add(t)

    # Tarjan SCC
    index_counter = [0]
    stack = []
    lowlink = {}
    index = {}
    on_stack = {}
    result: list[set[str]] = []

    sys.setrecursionlimit(10000)

    def strongconnect(v):
        index[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True
        for w in graph.get(v, ()):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack.get(w):
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            comp = set()
            while True:
                w = stack.pop()
                on_stack[w] = False
                comp.add(w)
                if w == v:
                    break
            result.append(comp)

    for v in list(graph.keys()):
        if v not in index:
            strongconnect(v)

    cycles = [c for c in result if len(c) > 1]
    # Also detect self-imports (module -> itself) though rare
    print(f"Total modules: {len(graph)}")
    print(f"Modules participating in cycles: {sum(len(c) for c in cycles)}")
    print(f"Number of cycle components (SCC>1): {len(cycles)}\n")
    for comp in sorted(cycles, key=lambda c: min(c)):
        print("CYCLE:")
        for m in sorted(comp):
            print(f"  {m}")
        print()


if __name__ == "__main__":
    main()
