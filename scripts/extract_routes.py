#!/usr/bin/env python3
"""extract_routes.py — Phase G mechanical extraction tool.

Pulls one or more HTTPRequestHandler methods out of server.py and
appends them to routes.py as standalone handle_* functions.

For each method:
  1. Extract lines [start, end] from server.py — end is the line
     BEFORE the next `    def ` (next sibling method) OR the
     next 0-space class/def (end of the class).
  2. Re-indent (strip the 4-space class indent).
  3. Rewrite signature: `def _name(self, payload):` →
     `def handle_name(payload, send):`
  4. Substitute self.X → call form:
       self._send( → send(
       self._resolve_target( → resolve_target(
       self._update_last_analysis( → update_last_analysis(
  5. AST-walk the transformed function to find external names
     referenced (excluding locals, builtins, stdlib, and names
     already imported at routes.py top) — emit a per-function
     `from server import (...)` block scoped right after the
     docstring.
  6. Append to routes.py with a group header.
  7. Delete the source lines from server.py.
  8. Rewrite do_POST dispatch: `self._NAME(payload)` →
     `handle_NAME(payload, self._send)`.
  9. Extend the `from routes import (...)` block in server.py.

Usage:
  python3 scripts/extract_routes.py <group_title> <method>...
"""
import ast
import builtins
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = ROOT / "hermes-bridge" / "server.py"
ROUTES_PY = ROOT / "hermes-bridge" / "routes.py"

STDLIB = {
    "json", "os", "random", "re", "subprocess", "sys", "time",
    "urllib", "uuid", "BaseHTTPRequestHandler", "ThreadingHTTPServer",
    "datetime", "concurrent",
}

ROUTES_ALREADY_IMPORTED = {
    "_psql", "_lit", "_redis", "log",
}


def find_method_range(src_lines, name):
    """Return (start_line, end_line) for `    def <name>(` in server.py.

    end_line is inclusive. The next boundary is:
      - the next `    def ` (sibling method at 4-space indent), OR
      - the next 0-space top-level `def `/`class ` (end of the
        Handler class — must NOT slurp module-level code into the
        method), OR
      - end of file.
    """
    target_start = None
    for i, line in enumerate(src_lines, 1):
        m = re.match(r"^    def ([a-z_][a-z0-9_]*)\s*\(", line)
        if m and m.group(1) == name:
            target_start = i
            break
    if target_start is None:
        raise KeyError(f"{name} not found at 4-space indent in server.py")
    # Find the next boundary after target_start.
    for j in range(target_start + 1, len(src_lines) + 1):
        line = src_lines[j - 1]
        # Next sibling method (4-space `def`)
        if re.match(r"^    def [a-z_]", line):
            return target_start, j - 1
        # End of the class — any 0-space `def `, `class `, or constant
        # at column 0 (excluding blank lines / comments / continuation
        # lines indented further).
        if re.match(r"^(def |class |[A-Z_][A-Z0-9_]*\s*=)", line):
            return target_start, j - 1
    return target_start, len(src_lines)


def transform_body(method_src, name):
    """Re-indent + rename + substitute self.X."""
    lines = method_src.splitlines(keepends=True)
    if not lines:
        return ""
    handle_name = "handle_" + name.lstrip("_")
    out = []
    for ln in lines:
        if ln.startswith("    "):
            out.append(ln[4:])
        else:
            out.append(ln)
    body = "".join(out)
    body = re.sub(
        rf"^def _{re.escape(name.lstrip('_'))}\(self,\s*payload\):",
        f"def {handle_name}(payload, send):",
        body, flags=re.MULTILINE)
    body = body.replace("self._send(", "send(")
    body = body.replace("self._resolve_target(", "resolve_target(")
    body = body.replace("self._update_last_analysis(",
                        "update_last_analysis(")
    return body


def find_server_referenced_names(transformed_body, server_names):
    """Parse the function source; return names that need
    `from server import X` injection."""
    try:
        tree = ast.parse(transformed_body)
    except SyntaxError as e:
        print(f"WARN: ast.parse failed for body:\n{transformed_body[:300]}",
              file=sys.stderr)
        raise
    locals_ = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for arg in node.args.args:
                locals_.add(arg.arg)
            for arg in node.args.kwonlyargs:
                locals_.add(arg.arg)
            if node.args.vararg:
                locals_.add(node.args.vararg.arg)
            if node.args.kwarg:
                locals_.add(node.args.kwarg.arg)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        locals_.add(sub.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            locals_.add(node.target.id)
        elif isinstance(node, ast.For):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    locals_.add(sub.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for n in node.names:
                locals_.add(n.asname or n.name.split(".")[0])
        elif isinstance(node, ast.comprehension):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    locals_.add(sub.id)
        elif isinstance(node, ast.Lambda):
            for arg in node.args.args:
                locals_.add(arg.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            locals_.add(node.name)
        elif isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars:
                    for sub in ast.walk(item.optional_vars):
                        if isinstance(sub, ast.Name):
                            locals_.add(sub.id)
    builtin_names = set(dir(builtins))
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            n = node.id
            if (n in server_names
                    and n not in locals_
                    and n not in builtin_names
                    and n not in STDLIB
                    and n not in ROUTES_ALREADY_IMPORTED):
                used.add(n)
    return used


def inject_imports(func_src, names_to_import):
    if not names_to_import:
        return func_src
    lines = func_src.splitlines(keepends=True)
    if len(lines) < 2:
        return func_src
    body_start = 1
    while body_start < len(lines) and lines[body_start].strip() == "":
        body_start += 1
    insert_at = body_start
    if body_start < len(lines):
        ls = lines[body_start].lstrip()
        for q in ('"""', "'''"):
            if ls.startswith(q):
                # Strip quote from start when checking for closing
                rest = ls[len(q):]
                if q in rest:
                    insert_at = body_start + 1
                else:
                    j = body_start + 1
                    while j < len(lines) and q not in lines[j]:
                        j += 1
                    insert_at = j + 1
                break
    names_sorted = sorted(names_to_import)
    if len(names_sorted) <= 3:
        imp_line = ("    from server import "
                    + ", ".join(names_sorted) + "\n")
    else:
        imp_line = "    from server import (\n"
        for n in names_sorted:
            imp_line += f"        {n},\n"
        imp_line += "    )\n"
    return ("".join(lines[:insert_at])
            + imp_line
            + "".join(lines[insert_at:]))


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: extract_routes.py <group_title> <method>...")
    group_title = sys.argv[1]
    method_names = sys.argv[2:]

    sys.path.insert(0, str(ROOT / "hermes-bridge"))
    import server  # noqa
    server_names = set(n for n in dir(server) if not n.startswith("__"))

    src_lines = SERVER_PY.read_text().splitlines(keepends=True)

    ranges = []
    for n in method_names:
        s, e = find_method_range(src_lines, n)
        ranges.append((n, s, e))
    print("Ranges:")
    for n, s, e in ranges:
        print(f"  {n:30s} {s:5d}-{e:5d}  ({e-s+1} lines)")

    extracted_blocks = []
    for n, s, e in ranges:
        body = "".join(src_lines[s - 1:e])
        transformed = transform_body(body, n)
        names = find_server_referenced_names(transformed, server_names)
        transformed = inject_imports(transformed, names)
        extracted_blocks.append((n, transformed, sorted(names)))
        head = ", ".join(sorted(names))[:120]
        print(f"  {n}: {len(names)} server names → {head}")

    routes_src = ROUTES_PY.read_text()
    addition = (
        "\n\n# ============================================================================\n"
        f"# Group: {group_title}\n"
        "# ============================================================================\n\n"
        + "\n\n".join(block.rstrip() for _, block, _ in extracted_blocks)
        + "\n"
    )
    ROUTES_PY.write_text(routes_src + addition)
    print(f"\nAppended {len(method_names)} handler(s) to routes.py")

    skip = set()
    for _, s, e in ranges:
        for i in range(s, e + 1):
            skip.add(i)
    kept = []
    in_skip = False
    for i, line in enumerate(src_lines, 1):
        if i in skip:
            if not in_skip:
                kept.append("    # (moved to routes.py — handle_<name>(payload, self._send))\n")
                in_skip = True
            continue
        in_skip = False
        kept.append(line)
    SERVER_PY.write_text("".join(kept))
    print(f"Removed {len(skip)} lines from server.py")

    src = SERVER_PY.read_text()
    for n in method_names:
        handle = "handle_" + n.lstrip("_")
        old = f"self.{n}(payload)"
        new = f"{handle}(payload, self._send)"
        if old in src:
            src = src.replace(old, new)
            print(f"  dispatch: {old} → {new}")
    SERVER_PY.write_text(src)

    src = SERVER_PY.read_text()
    handle_names = [f"handle_{n.lstrip('_')}" for n in method_names]
    # Robust import-block parse — find the literal "from routes import ("
    # line, then walk forward line-by-line collecting names until the
    # closing ")". This handles the # noqa comment on the opening line,
    # multi-line bodies, and trailing whitespace without the regex/split
    # fragility that dropped entries previously.
    block_start = src.find("from routes import (")
    if block_start >= 0:
        block_end = src.find(")", block_start)
        block_text = src[block_start:block_end + 1]
        existing = []
        for ln in block_text.splitlines()[1:]:  # skip the opening line
            name = ln.strip().rstrip(",").strip()
            if not name or name.startswith("#") or name.startswith(")"):
                continue
            # Defensive: drop any inline comment
            name = name.split("#", 1)[0].strip().rstrip(",")
            if name:
                existing.append(name)
        all_handles = sorted(set(existing) | set(handle_names))
        new_block = ("from routes import (  # noqa: F401\n"
                     + "".join(f"    {h},\n" for h in all_handles)
                     + ")")
        src = src[:block_start] + new_block + src[block_end + 1:]
        SERVER_PY.write_text(src)
        print(f"Updated routes import block: {len(all_handles)} handlers total")
    else:
        # First run — insert a fresh block after the review-import.
        marker = "from review import (  # noqa: F401\n"
        idx = src.find(marker)
        if idx >= 0:
            close = src.find(")\n", idx)
            insert_at = close + 2
            new_block = ("from routes import (  # noqa: F401\n"
                         + "".join(f"    {h},\n"
                                   for h in sorted(handle_names))
                         + ")\n")
            src = src[:insert_at] + new_block + src[insert_at:]
            SERVER_PY.write_text(src)
            print(f"Inserted new routes import block "
                  f"({len(handle_names)} handlers)")


if __name__ == "__main__":
    main()
