"""Source screening: Python syntax can prove references are not secret literals."""

import ast
import io
import re
import tokenize
from pathlib import Path

from .diagnosis import CREDENTIAL_ASSIGNMENT, redact, redact_known

_KEY_NAME = re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|token|password|authorization)$")
_ANNOTATION_NODES = (
    ast.Expression, ast.Name, ast.Attribute, ast.Subscript, ast.Tuple, ast.List,
    ast.Load, ast.BinOp, ast.BitOr,
)


def _first_change(text, redacted):
    index = next((i for i, (a, b) in enumerate(zip(text, redacted)) if a != b), 0)
    return text.count("\n", 0, index) + 1


def _default_literal(node):
    if isinstance(node, ast.Constant):
        return node if isinstance(node.value, (str, bytes)) and node.value else None
    if isinstance(node, ast.JoinedStr):
        return next((item for item in node.values if _default_literal(item)), None)
    if isinstance(node, ast.BinOp):
        return _default_literal(node.left) or _default_literal(node.right)
    if isinstance(node, ast.BoolOp):
        return next((found for value in node.values if (found := _default_literal(value))), None)
    if isinstance(node, ast.IfExp):
        return _default_literal(node.body) or _default_literal(node.orelse)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return next((found for value in node.elts if (found := _default_literal(value))), None)
    if isinstance(node, ast.Dict):
        return next((found for value in node.values if (found := _default_literal(value))), None)
    if isinstance(node, ast.Call):
        # Environment lookup keys and field descriptions are symbols/metadata,
        # but non-empty fallback values remain credential candidates.
        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
        defaults = [kw.value for kw in node.keywords if kw.arg == "default"]
        if name in ("get", "getenv"):
            defaults.extend(node.args[1:])
        else:
            defaults.extend(node.args)
        return next((found for value in defaults if (found := _default_literal(value))), None)
    return None


def _bindings(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            name = getattr(node.target, "id", getattr(node.target, "attr", ""))
            yield name, node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                yield getattr(target, "id", getattr(target, "attr", "")), node.value
        elif isinstance(node, ast.keyword):
            yield node.arg or "", node.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    yield key.value, value
        elif isinstance(node, ast.arguments):
            positional = [*node.posonlyargs, *node.args]
            for argument, value in zip(positional[-len(node.defaults):], node.defaults):
                yield argument.arg, value
            for argument, value in zip(node.kwonlyargs, node.kw_defaults):
                yield argument.arg, value


def _safe_annotation(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval")
        except (SyntaxError, ValueError):
            return False
    return all(
        isinstance(item, _ANNOTATION_NODES)
        or isinstance(item, ast.Constant) and item.value in (None, Ellipsis)
        for item in ast.walk(node)
    )


def source_problem(text, name):
    """Return only a line/rule; never include source, matched values or hashes."""
    if "\0" in text:
        return {"line": 1, "rule": "binary_content"}
    known = redact_known(text, source_screening=True)
    if known != text:
        return {"line": _first_change(text, known), "rule": "credential_pattern"}
    matches = list(CREDENTIAL_ASSIGNMENT.finditer(text))
    if Path(name).suffix.lower() != ".py":
        return ({"line": text.count("\n", 0, matches[0].start()) + 1,
                 "rule": "unclassified_assignment"} if matches else None)
    try:
        tree = ast.parse(text)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (SyntaxError, ValueError, tokenize.TokenError, IndentationError, RecursionError):
        redacted = redact(text)
        return ({"line": _first_change(text, redacted), "rule": "unclassified_assignment"}
                if redacted != text else None)
    for binding, value in _bindings(tree):
        if _KEY_NAME.search(binding) and (literal := _default_literal(value)):
            return {"line": literal.lineno, "rule": "credential_literal"}
    lines = text.splitlines(keepends=True)
    starts, size = [], 0
    for line in lines:
        starts.append(size)
        size += len(line)

    def offset(row, col, *, utf8=False):
        prefix = lines[row - 1].encode()[:col].decode() if utf8 else lines[row - 1][:col]
        return starts[row - 1] + len(prefix)

    symbols = {
        offset(*token.start) for token in tokens if token.type == tokenize.NAME
    }
    annotations = []
    for node in ast.walk(tree):
        annotation = getattr(node, "annotation", None) or getattr(node, "returns", None)
        if annotation is not None and _safe_annotation(annotation):
            annotations.append((
                offset(annotation.lineno, annotation.col_offset, utf8=True),
                offset(annotation.end_lineno, annotation.end_col_offset, utf8=True),
            ))
    for match in matches:
        value_start = match.end(1)
        if value_start in symbols or any(start <= value_start < end for start, end in annotations):
            continue
        return {"line": text.count("\n", 0, match.start()) + 1, "rule": "credential_literal"}
    return None
