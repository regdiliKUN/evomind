"""零依赖静态检查：找出「运行到才会 NameError」的未定义名称（等价 ruff/pyflakes 的 F821）。

为什么不直接用 ruff：本机 venv 没装、离线环境也装不上；而 NameError 恰恰是测试覆盖
不到的冷路径（异常分支、少用的 API）里最常见的低级错误。这里只用标准库 `symtable`：

- 函数作用域里被引用、按「隐式全局」解析的名字，必须在模块顶层被绑定或是内置名；
- 模块顶层被引用但从未绑定的名字同理。

`from x import *` 的模块无法静态判定，直接跳过。类体作用域与推导式按 symtable 的
语义处理。误报可在行尾加 `# noqa: F821` 豁免。

用法：python scripts/lint_names.py [路径 ...]   （默认 backend run.py scripts tests）
退出码：发现问题为 1，否则 0。
"""
from __future__ import annotations

import ast
import builtins
import sys
import symtable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ["backend", "run.py", "scripts", "tests", "evomind_launcher.py"]
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__loader__",
                                  "__package__", "__builtins__", "__path__", "__annotations__",
                                  "__dict__", "__module__", "__qualname__", "__class__"}


def _module_bindings(tree: ast.Module, top: symtable.SymbolTable) -> set[str]:
    names = {s.get_name() for s in top.get_symbols()
             if s.is_assigned() or s.is_imported() or s.is_namespace()}
    # `global x` 在函数内首次赋值也会在模块级创建绑定
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.update(node.names)
    return names


def _has_star_import(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
               for n in ast.walk(tree))


def _noqa_lines(src: str) -> set[int]:
    return {i for i, line in enumerate(src.splitlines(), 1) if "noqa: F821" in line or "noqa:F821" in line}


def _name_lines(tree: ast.Module) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out.setdefault(node.id, []).append(node.lineno)
    return out


def _walk(table: symtable.SymbolTable):
    yield table
    for child in table.get_children():
        yield from _walk(child)


def check_file(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8-sig")
    try:
        tree = ast.parse(src, filename=str(path))
        top = symtable.symtable(src, str(path), "exec")
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: 语法错误 {e.msg}"]
    if _has_star_import(tree):
        return []
    bound = _module_bindings(tree, top) | BUILTINS
    lines = _name_lines(tree)
    noqa = _noqa_lines(src)
    problems: set[tuple[str, int]] = set()
    for table in _walk(top):
        for sym in table.get_symbols():
            name = sym.get_name()
            if not sym.is_referenced():
                continue
            if table.get_type() == "module":
                unresolved = not (sym.is_assigned() or sym.is_imported() or sym.is_namespace())
            else:
                # 局部 / 闭包变量由 symtable 保证可解析；只看隐式与显式全局
                unresolved = sym.is_global() and not sym.is_assigned()
            if unresolved and name not in bound:
                for ln in lines.get(name, [table.get_lineno()]):
                    if ln not in noqa:
                        problems.add((name, ln))
    return [f"{path.relative_to(ROOT)}:{ln}: 未定义名称 {name!r}" for name, ln in sorted(problems, key=lambda p: p[1])]


def main(argv: list[str]) -> int:
    targets = argv or DEFAULT
    files: list[Path] = []
    for t in targets:
        p = (ROOT / t) if not Path(t).is_absolute() else Path(t)
        if p.is_dir():
            files.extend(sorted(x for x in p.rglob("*.py") if "__pycache__" not in x.parts))
        elif p.suffix == ".py" and p.exists():
            files.append(p)
    issues: list[str] = []
    for f in files:
        issues.extend(check_file(f))
    for line in issues:
        print(line)
    print(f"检查 {len(files)} 个文件，发现 {len(issues)} 处未定义名称", file=sys.stderr)
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
