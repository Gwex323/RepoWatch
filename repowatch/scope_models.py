"""Bounded model proposals; none of these fields authorizes completion by itself."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScopeRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=200,
                       description="Required short title for this acceptance item; never omit.")
    expected: str = Field(min_length=1, max_length=1600)
    paths: list[str] = Field(min_length=1, max_length=12)
    required_evidence: list[Literal["regression", "integration", "client"]] = Field(
        min_length=1, max_length=3,
    )


class ScopeSourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=240)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=1200)


class ScopeConclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=64)
    outcome: Literal["investigating", "confirmed", "no_defect", "blocked"]
    rationale: str = Field(min_length=1, max_length=1600)
    evidence: list[ScopeSourceEvidence] = Field(max_length=5)
    finding_ids: list[str] = Field(default_factory=list, max_length=32)


class ScopeExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=300)
    outcome: Literal["required", "unrelated", "unknown"]
    required_keys: list[str] = Field(max_length=32)
    rationale: str = Field(min_length=1, max_length=1600)
    evidence: list[ScopeSourceEvidence] = Field(max_length=5)


class ScopeReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1, strict=True)
    accepted: bool = Field(strict=True)
    rationale: str = Field(min_length=1, max_length=1600)
    items: list[ScopeConclusion] = Field(min_length=1, max_length=32)
    exclusions: list[ScopeExclusion] = Field(default_factory=list, max_length=64)


def scope_mapping_issues(findings, review, requirements, finding_ids):
    """Shared, value-free reference checks before model acceptance and persistence."""
    issues = []
    for index, finding in enumerate(findings, 1):
        positions = [position for position, key in enumerate(finding.get("scope_keys", []), 1)
                     if key not in requirements]
        if positions:
            issues.append({"section": "findings", "item": index, "field": "scope_keys",
                           "reason": "unknown_scope_key", "positions": positions})
    for index, item in enumerate((review or {}).get("items", []), 1):
        positions = [position for position, identity in enumerate(item["finding_ids"], 1)
                     if identity not in finding_ids]
        if positions:
            issues.append({"section": "scope_items", "item": index, "field": "finding_ids",
                           "reason": "unknown_finding_id", "positions": positions})
    return issues


def _invalid_scope_review_structure():
    return {
        "exclusion_key": None, "target_key": None,
        "required_evidence": None, "available_evidence": [],
        "reason": "invalid_scope_review_structure",
    }


def _valid_scope_exclusion(exclusion):
    return (
        isinstance(exclusion, dict)
        and isinstance(exclusion.get("key"), str)
        and exclusion.get("outcome") in ("required", "unrelated", "unknown")
        and isinstance(exclusion.get("evidence"), list)
        and (
            exclusion.get("outcome") != "required"
            or (
                isinstance(exclusion.get("required_keys"), list)
                and all(isinstance(value, str) for value in exclusion["required_keys"])
            )
        )
    )


def scope_review_issues(review, requirements, limits):
    """Return value-free exclusion checks shared by review and persistence."""
    requirements = {item["key"]: item for item in requirements}
    limits = {item["key"]: item for item in limits}
    issues = []
    exclusions = review.get("exclusions", []) if isinstance(review, dict) else None
    if not isinstance(exclusions, list) or not all(
        _valid_scope_exclusion(item) for item in exclusions
    ):
        return [_invalid_scope_review_structure()]
    for exclusion in sorted(exclusions, key=lambda item: item["key"]):
        key = exclusion["key"]
        if exclusion["outcome"] == "unrelated" and not exclusion["evidence"]:
            issues.append({
                "exclusion_key": key, "target_key": None,
                "required_evidence": None, "available_evidence": [],
                "reason": "unrelated_without_evidence",
            })
        if exclusion["outcome"] != "required":
            continue
        required_kind = "client" if set(limits.get(key, {}).get("reasons", [])) & {
            "windows_native", "gui",
        } else "integration"
        targets = sorted(set(exclusion["required_keys"]))
        if not targets:
            issues.append({
                "exclusion_key": key, "target_key": None,
                "required_evidence": required_kind, "available_evidence": [],
                "reason": "missing_required_keys",
            })
            continue
        for target_key in targets:
            target = requirements.get(target_key)
            available = sorted(target.get("required_evidence", [])) if target else []
            if target is None:
                reason = "unknown_scope_requirement"
            elif required_kind not in available:
                reason = "missing_evidence_kind"
            else:
                continue
            issues.append({
                "exclusion_key": key, "target_key": target_key,
                "required_evidence": required_kind, "available_evidence": available,
                "reason": reason,
            })
    return sorted(issues, key=lambda item: (
        item["exclusion_key"], item["target_key"] or "", item["reason"],
    ))


def _label(value):
    """Hash text so progress material never contains candidate prose or paths."""
    if not isinstance(value, str):
        return {"type": type(value).__name__}
    return {"sha256": hashlib.sha256(value.encode()).hexdigest()}


def _progress_sort_key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def scope_review_progress(review):
    """Return a text-free, order-independent shape for no-progress comparisons."""
    if not isinstance(review, dict):
        return {"type": type(review).__name__}
    items = review.get("items")
    exclusions = review.get("exclusions")
    item_rows = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            item_rows.append({"invalid": type(item).__name__})
            continue
        evidence = item.get("evidence")
        finding_ids = item.get("finding_ids")
        item_rows.append({
            "key": _label(item.get("key")),
            "outcome": _label(item.get("outcome")),
            "evidence": len(evidence) if isinstance(evidence, list) else type(evidence).__name__,
            "finding_ids": sorted(
                _label(identity).get("sha256", "")
                for identity in finding_ids if isinstance(identity, str)
            ) if isinstance(finding_ids, list) else type(finding_ids).__name__,
        })
    exclusion_rows = []
    for item in exclusions if isinstance(exclusions, list) else []:
        if not isinstance(item, dict):
            exclusion_rows.append({"invalid": type(item).__name__})
            continue
        required_keys = item.get("required_keys")
        evidence = item.get("evidence")
        exclusion_rows.append({
            "key": _label(item.get("key")),
            "outcome": _label(item.get("outcome")),
            "required_keys": sorted(
                _label(key).get("sha256", "")
                for key in required_keys if isinstance(key, str)
            ) if isinstance(required_keys, list) else type(required_keys).__name__,
            "evidence": len(evidence) if isinstance(evidence, list) else type(evidence).__name__,
            "invalid_required_keys": (
                sum(not isinstance(key, str) for key in required_keys)
                if isinstance(required_keys, list) else None
            ),
        })
    return {
        "version": review.get("version") if type(review.get("version")) is int
        else type(review.get("version")).__name__,
        "accepted": review.get("accepted") if type(review.get("accepted")) is bool
        else type(review.get("accepted")).__name__,
        "items": sorted(item_rows, key=_progress_sort_key),
        "exclusions": sorted(exclusion_rows, key=_progress_sort_key),
        "items_type": type(items).__name__,
        "exclusions_type": type(exclusions).__name__,
    }


class RepairScopeCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=64)
    regression_paths: list[str] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=1600)
