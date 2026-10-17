"""Bounded AST-only desktop discovery. Never import downloaded code on the host."""

import ast
import os
import re
import shlex
import sys
from pathlib import Path

MAX_MODULES = 256
MAX_BYTES = 2 * 1024 * 1024
MAX_TEST_MODULES = 32
GUI = {"tkinter"}
WINDOWS = {"winreg", "msvcrt", "winsound", "win32api", "win32crypt", "pythoncom"}
EXCLUDED = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
UNVERIFIED = ["Windows 桌面窗口与交互", "Windows 原生接口、DPAPI 及真实客户端集成"]


def desktop_profile(source):
    """Conservatively select unittest modules without GUI/native/unknown imports."""
    if not source.is_dir() or source.is_symlink():
        return None
    entries = [name for name in ("app", "main") if (source / f"{name}.py").is_file()]
    if not entries:
        return None
    modules, paths, packages, opaque, total, scanned = {}, {}, set(), [], 0, 0
    try:
        for root, directories, names in os.walk(source, followlinks=False):
            directories[:] = sorted(name for name in directories
                                    if name not in EXCLUDED and not name.startswith(".")
                                    and not (Path(root) / name).is_symlink())
            for name in sorted(names):
                if not name.endswith(".py"):
                    continue
                scanned += 1
                if scanned > MAX_MODULES:
                    return None
                path = Path(root) / name
                relative = path.relative_to(source)
                parts = list(relative.with_suffix("").parts)
                package = parts[-1] == "__init__"
                if package:
                    parts.pop()
                if not parts or not all(re.fullmatch(r"[A-Za-z_]\w*", p, re.ASCII) for p in parts):
                    if relative.parts[0] == "tests" and name.startswith("test"):
                        opaque.append({"path": relative.as_posix()[:240],
                                       "reasons": ["unsupported_module_path"]})
                    continue
                if path.is_symlink() or path.stat().st_size > 256000:
                    return None
                total += path.stat().st_size
                if total > MAX_BYTES or len(modules) >= MAX_MODULES:
                    return None
                module = ".".join(parts)
                if package:
                    packages.add(module)
                modules[module] = ast.parse(path.read_text(encoding="utf-8-sig"))
                paths[module] = relative.as_posix()
    except (OSError, SyntaxError, UnicodeError, ValueError, RecursionError):
        return None
    aliases = {name.removeprefix("tests."): name for name in modules if name.startswith("tests.")}
    dependencies, direct = {}, {}
    for module, tree in modules.items():
        imports, reasons = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    package = module.split(".") if module in packages else module.split(".")[:-1]
                    prefix = package[:len(package) - node.level + 1]
                    base = ".".join([*prefix, node.module] if node.module else prefix)
                    imports.add(base)
                    imports.update(f"{base}.{alias.name}" for alias in node.names)
                else:
                    imports.add(node.module or "")
                    imports.update(f"{node.module}.{a.name}" for a in node.names
                                   if f"{node.module}.{a.name}" in modules)
            elif isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id == "__import__"
                or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
            ):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
                    node.args[0].value, str,
                ):
                    imports.add(node.args[0].value)
                else:
                    reasons.add("dynamic_import")
        local = set()
        for imported in imports:
            head = imported.split(".")[0]
            if head in GUI:
                reasons.add("gui")
            elif head in WINDOWS:
                reasons.add("windows_native")
                if head not in sys.stdlib_module_names:
                    reasons.add("unknown_dependency")
            elif imported in modules:
                local.add(imported)
            elif imported in aliases:
                local.add(aliases[imported])
            elif head in modules:
                local.add(head)
            elif head not in sys.stdlib_module_names and head not in ("", "__future__"):
                # Parent packages are accounted for by their local child import.
                if not any(name.startswith(imported + ".") for name in modules):
                    reasons.add("unknown_dependency")
        for imported in tuple(local):
            parts = imported.split(".")
            local.update(".".join(parts[:index]) for index in range(1, len(parts))
                         if ".".join(parts[:index]) in packages)
        dependencies[module], direct[module] = local, reasons

    def reasons_for(module):
        reasons, seen, pending = set(), set(), [module]
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            reasons.update(direct.get(name, {"unknown_dependency"}))
            pending.extend(dependencies.get(name, set()) - seen)
        return reasons

    runtime = set().union(*(reasons_for(name) for name in entries))
    if "gui" not in runtime:
        return None
    shadowed = any(name.split(".")[0] in sys.stdlib_module_names for name in modules)
    collisions = set(aliases).intersection(modules)
    selected, excluded = [], opaque
    for name in sorted(modules):
        if not name.startswith("tests.") or not name.split(".")[-1].startswith("test"):
            continue
        reasons = reasons_for(name)
        if shadowed:
            reasons.add("stdlib_shadowing")
        if collisions:
            reasons.add("ambiguous_local_import")
        if not any(
            isinstance(n, (ast.Import, ast.ImportFrom)) and (
                isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "unittest"
                or isinstance(n, ast.Import) and any(a.name == "unittest" for a in n.names)
            ) for n in ast.walk(modules[name])
        ):
            reasons.add("test_framework_unknown")
        if (len(selected) >= MAX_TEST_MODULES or
                sum(len(item["module"]) + 1 for item in selected) + len(name) + 160 > 4000):
            reasons.add("selection_limit")
        if reasons:
            excluded.append({"path": paths[name], "reasons": sorted(reasons)})
        else:
            selected.append({"path": paths[name], "module": name.removeprefix("tests.")})
    command = ""
    if selected:
        names = " ".join(shlex.quote(item["module"]) for item in selected)
        command = ("PYTHONPATH=/workspace/project/tests:/workspace/project "
                   "/workspace/home/venv/bin/python -m unittest -v " + names)
    return {
        "kind": "python_desktop", "runtime_stdlib_only": not (
            runtime - {"gui", "windows_native"} or shadowed
        ), "selected": selected, "excluded": excluded,
        "uncovered": list(UNVERIFIED), "test_command": command,
        "complete": False,
    }


def desktop_config(source, config):
    profile = desktop_profile(source)
    if not profile:
        return None
    known = profile["runtime_stdlib_only"] or bool(config.get("install"))
    return {**config, "language": "python", "detected": known,
            "reason": "desktop_offline_proposal" if known else "desktop_dependencies_required",
            "run_mode": "test_only", "start": "",
            "install": "python -m venv /workspace/home/venv" if profile["runtime_stdlib_only"]
            else config.get("install", ""), "test": profile["test_command"],
            "project_profile": profile, "desktop_approved": False}


def offline_test_scope(config, log):
    profile = config.get("project_profile", {})
    if profile.get("kind") != "python_desktop":
        return None
    runs = re.findall(r"(?m)^Ran (\d+) tests?\b", log)
    skipped = re.findall(r"(?m)^(?:OK|FAILED)\s*\([^\n]*?skipped=(\d+)", log)
    total = int(runs[-1]) if runs else None
    skip = int(skipped[-1]) if skipped else 0
    generated = config["test"] == profile.get("test_command")
    return {"kind": "desktop_offline", "total": total, "skipped": skip,
            "executed": max(0, total - skip) if total is not None else None,
            "selection": "generated" if generated else "custom",
            "selected": profile["selected"] if generated else [],
            "excluded": profile["excluded"], "uncovered": profile["uncovered"],
            "complete": False}


def desktop_guard(config):
    if config.get("project_profile", {}).get("kind") != "python_desktop":
        return None
    if config.get("run_mode") != "test_only":
        return "desktop_test_only_required"
    if not config.get("desktop_approved"):
        return "desktop_confirmation_required"
    if not config.get("install", "").strip():
        return "desktop_install_command_required"
    return None
