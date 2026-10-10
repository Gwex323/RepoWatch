"""Trusted, deliberately faulty fixture. No downloaded code is executed."""

import hashlib
from pathlib import Path

REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
TITLE = "Empty pages return the preceding page"


def broken_window(total: int, page: int, size: int) -> list[int]:
    items = list(range(total))
    start = (page - 1) * size
    if start >= len(items) and items:
        start = max(0, len(items) - size)
    return items[start : start + size]


def correct_window(total: int, page: int, size: int) -> list[int]:
    return list(range(total))[(page - 1) * size : page * size]


def experiment(total: int, page: int, size: int) -> dict:
    expected = correct_window(total, page, size)
    actual = broken_window(total, page, size)
    return {
        "input": {"total": total, "page": page, "size": size},
        "expected": expected,
        "actual": actual,
        "reproduced": actual != expected,
        "revision": REVISION,
    }


def verify_patch() -> dict:
    cases = [(20, 3, 10), (20, 1, 10), (20, 2, 10), (0, 1, 10), (21, 3, 10)]
    assert experiment(20, 3, 10)["reproduced"]
    for total, page, size in cases:
        expected = [n for n in range(total) if (page - 1) * size <= n < page * size]
        assert correct_window(total, page, size) == expected
    return {"passed": len(cases), "failed": 0, "scope": "trusted fixture functions only"}


PATCH = """--- a/page_window.py
+++ b/page_window.py
@@ -1,7 +1,4 @@
 def window(total, page, size):
     items = list(range(total))
     start = (page - 1) * size
-    if start >= len(items) and items:
-        start = max(0, len(items) - size)
     return items[start : start + size]
"""
