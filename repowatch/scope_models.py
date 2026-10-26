"""Bounded model proposals; none of these fields authorizes completion by itself."""

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


class RepairScopeCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=64)
    regression_paths: list[str] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=1600)
