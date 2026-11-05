import json
import math
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, uuid5


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def stable_id(kind: str, *parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, canonical_json(["repowatch", kind, *parts])))


def content_digest(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_time(now: float) -> None:
    if not math.isfinite(now) or now < 0:
        raise ValueError("时间必须为非负有限值")


class TriageResult(StrEnum):
    AUTO_REPAIR = "auto_repair"
    NEEDS_INFORMATION = "needs_information"
    NEEDS_APPROVAL = "needs_approval"
    EXCLUDED = "excluded"
    EXTERNAL_REPAIR = "external_repair"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ExecutionToken:
    run_id: str
    generation: int
    authority_run_id: str | None = None


@dataclass(frozen=True)
class TriageDecision:
    result: TriageResult
    reason_code: str
    evidence: dict[str, Any]

    def __post_init__(self):
        object.__setattr__(self, "result", TriageResult(self.result))
        if not self.reason_code.strip() or not self.evidence:
            raise ValueError("分诊必须记录原因及证据上下文")
        canonical_json(self.evidence)
