"""Issue 入口、宿主事实门禁和模型分诊结果的受控映射。"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repowatch.domain.task_records import TriageDecision, TriageResult


class TriageAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["bug", "feature", "refactor", "unknown"]
    objective: str = Field(max_length=4000)
    criteria: list[str] = Field(default_factory=list, max_length=100)
    reproduction_steps: list[str] = Field(default_factory=list, max_length=100)
    missing_information: list[str] = Field(default_factory=list, max_length=100)
    contradictions: list[str] = Field(default_factory=list, max_length=100)
    independent_goals: list[str] = Field(default_factory=list, max_length=100)
    sensitive: bool = Field(default=False, strict=True)
    plan: str = Field(default="", max_length=12000)
    structural_goal: str = Field(default="", max_length=4000)
    behavior_preservation: str = Field(default="", max_length=4000)
    ownership: Literal["available", "occupied", "uncertain"] = "uncertain"
    ownership_evidence: list[str] = Field(default_factory=list, max_length=100)
    pr_relations: list[dict] = Field(default_factory=list, max_length=1000)
    issue_relation: dict = Field(default_factory=dict)


def admission_eligible(repository, issue, selected):
    if selected or repository["admission_mode"] == "all_bugs":
        return True
    if repository["admission_mode"] == "manual":
        return False
    policy = json.loads(repository["policy_json"])
    return bool(set(policy.get("allowed_labels", [])) & set(issue.get("labels", [])))


def host_triage_gate(repository, task, issue):
    evidence = {
        "input_revision": task["input_revision"],
        "admission_revision": repository["admission_revision"],
        "retriage_on": ["issue_input", "admission_policy", "ownership", "acceptance"],
    }

    def decision(result, reason, **details):
        return TriageDecision(result, reason, {**evidence, **details})

    if task["user_excluded"] or task["cancel_requested"]:
        return decision(TriageResult.EXCLUDED, "user_excluded")
    if issue.get("state", "open") != "open":
        return decision(TriageResult.EXCLUDED, "issue_closed")
    if not admission_eligible(repository, issue, task["selected"]):
        return decision(TriageResult.EXCLUDED, "admission_required")
    identity = json.loads(repository["identity_json"] or "{}")
    execution_login = identity.get("execution_login")
    assignees = issue.get("assignees", [])
    if assignees and not execution_login:
        return decision(TriageResult.NEEDS_INFORMATION, "execution_identity_unknown")
    if any(login.casefold() != execution_login.casefold() for login in assignees):
        return decision(TriageResult.EXCLUDED, "other_assignee", assignees=assignees)
    policy = json.loads(repository["policy_json"])
    labels = set(issue.get("labels", []))
    if "needs-info" in labels and "needs-info" in policy.get("excluded_labels", []):
        return decision(
            TriageResult.NEEDS_INFORMATION,
            "needs_information_label",
            missing_information=["补充 Issue 所需的定位与复现条件"],
        )
    excluded = sorted(labels & set(policy.get("excluded_labels", [])))
    if excluded:
        return decision(TriageResult.EXCLUDED, "excluded_labels", labels=excluded)
    return None


def classify_analysis(analysis, *, acceptance=None, remote_context=None):
    evidence = {
        "analysis": analysis.model_dump(mode="json"),
        "retriage_on": ["issue_input", "admission_policy", "ownership", "acceptance"],
    }
    if analysis.sensitive:
        return TriageDecision(TriageResult.NEEDS_INFORMATION, "sensitive_disclosure", evidence)
    if analysis.contradictions:
        return TriageDecision(TriageResult.NEEDS_INFORMATION, "contradictory_input", evidence)
    merged_external_pr = None
    if remote_context is not None:
        ownership = remote_context["decisions"].get("ownership", analysis.ownership)
        if ownership == "occupied":
            return TriageDecision(TriageResult.EXCLUDED, "maintainer_occupied", evidence)
        if ownership == "uncertain" or (
            not analysis.ownership_evidence and "ownership" not in remote_context["decisions"]
        ):
            return TriageDecision(TriageResult.NEEDS_INFORMATION, "ownership_uncertain", evidence)
        relations = {str(item.get("id")): item for item in analysis.pr_relations}
        for pull in remote_context["facts"]["pull_requests"]:
            if str(pull["id"]) == remote_context["managed_pr_id"]:
                continue
            relation = remote_context["decisions"].get("pr:" + str(pull["id"]))
            item = relations.get(str(pull["id"]), {})
            if relation is None and item.get("evidence"):
                relation = item.get("relation")
            if relation not in {"same", "unrelated"}:
                return TriageDecision(
                    TriageResult.NEEDS_INFORMATION, "pr_relation_uncertain", evidence
                )
            if relation == "same":
                if pull.get("merged_at") or pull.get("merged"):
                    if merged_external_pr is not None:
                        return TriageDecision(
                            TriageResult.NEEDS_INFORMATION, "multiple_merged_prs", evidence
                        )
                    merged_external_pr = {
                        "pr_id": str(pull["id"]),
                        "merge_commit_sha": pull.get("merge_commit_sha"),
                        "base_ref": pull.get("base", {}).get("ref"),
                    }
                if pull.get("state") == "open":
                    return TriageDecision(
                        TriageResult.EXTERNAL_REPAIR,
                        "external_pr_same_problem",
                        {**evidence, "pr_id": str(pull["id"])},
                    )
    if analysis.missing_information or analysis.kind == "unknown" or not analysis.objective.strip():
        return TriageDecision(TriageResult.NEEDS_INFORMATION, "information_required", evidence)
    if analysis.independent_goals:
        return TriageDecision(TriageResult.NEEDS_APPROVAL, "scope_confirmation", evidence)
    if analysis.kind in {"feature", "refactor"}:
        if not acceptance or not acceptance["approved"] or acceptance["kind"] != analysis.kind:
            return TriageDecision(TriageResult.NEEDS_APPROVAL, "plan_approval_required", evidence)
        content = acceptance["content"]
        if (
            analysis.objective != content["objective"]
            or analysis.criteria != content["criteria"]
            or analysis.plan != content["plan"]
            or (
                analysis.kind == "refactor"
                and (
                    analysis.structural_goal != content["structural_goal"]
                    or analysis.behavior_preservation != content["behavior_preservation"]
                )
            )
        ):
            return TriageDecision(TriageResult.NEEDS_APPROVAL, "approved_scope_mismatch", evidence)
        if merged_external_pr is not None:
            return TriageDecision(
                TriageResult.AUTO_REPAIR,
                "external_merge_verification",
                {**evidence, **merged_external_pr},
            )
        return TriageDecision(TriageResult.AUTO_REPAIR, "approved_scope", evidence)
    if acceptance and acceptance["current"] and acceptance["kind"] != "bug":
        return TriageDecision(TriageResult.NEEDS_APPROVAL, "bug_scope_mismatch", evidence)
    if not analysis.reproduction_steps or not analysis.criteria:
        return TriageDecision(
            TriageResult.NEEDS_INFORMATION, "reproduction_path_required", evidence
        )
    if (
        acceptance
        and acceptance["current"]
        and (
            analysis.objective != acceptance["content"]["objective"]
            or analysis.criteria != acceptance["content"]["criteria"]
        )
    ):
        return TriageDecision(TriageResult.NEEDS_APPROVAL, "acceptance_scope_mismatch", evidence)
    if merged_external_pr is not None:
        return TriageDecision(
            TriageResult.AUTO_REPAIR,
            "external_merge_verification",
            {**evidence, **merged_external_pr},
        )
    return TriageDecision(TriageResult.AUTO_REPAIR, "verifiable_bug", evidence)


def proposed_acceptance(analysis):
    if (
        analysis.kind == "unknown"
        or analysis.sensitive
        or analysis.contradictions
        or analysis.missing_information
        or not analysis.objective.strip()
        or not analysis.criteria
    ):
        return None
    content = {"objective": analysis.objective, "criteria": analysis.criteria}
    if analysis.kind in {"feature", "refactor"}:
        if not analysis.plan.strip():
            return None
        content["plan"] = analysis.plan
    if analysis.kind == "refactor":
        if not analysis.structural_goal.strip() or not analysis.behavior_preservation.strip():
            return None
        content.update(
            structural_goal=analysis.structural_goal,
            behavior_preservation=analysis.behavior_preservation,
        )
    if analysis.independent_goals:
        content["independent_goals"] = analysis.independent_goals
    return {"kind": analysis.kind, "content": content}
