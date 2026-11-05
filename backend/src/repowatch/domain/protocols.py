from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[\w.-]+$")]
GitObject = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=1)]
Mode = Literal[
    "scan", "investigate", "plan", "reproduce", "implement", "review", "distill", "diagnose"
]
Tool = Literal["read_source", "read_evidence", "write_source", "write_analysis", "request_check"]

MODE_TOOLS: dict[str, frozenset[str]] = {
    "scan": frozenset({"read_source", "write_analysis", "request_check"}),
    "investigate": frozenset({"read_source", "read_evidence", "write_analysis", "request_check"}),
    "plan": frozenset({"read_source", "read_evidence", "write_analysis"}),
    "reproduce": frozenset({"read_source", "read_evidence", "write_analysis", "request_check"}),
    "implement": frozenset({"read_source", "read_evidence", "write_source", "request_check"}),
    "review": frozenset({"read_source", "read_evidence", "request_check"}),
    "distill": frozenset({"read_evidence"}),
    "diagnose": frozenset({"read_evidence", "request_check"}),
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CodeVersion(Contract):
    base_sha: GitObject
    candidate_sha: GitObject | None = None
    tree_sha: GitObject
    content_manifest_sha256: Digest


class RunContext(Contract):
    repository_id: Identifier
    task_id: Identifier
    run_id: Identifier
    input_revision: Revision
    acceptance_revision: Revision
    configuration_id: Identifier
    configuration_revision: Revision
    execution_generation: Revision
    code: CodeVersion


class BudgetReference(Contract):
    owner_task_id: Identifier
    ledger_revision: Revision
    reservation_id: Identifier


class MemoryReference(Contract):
    memory_id: Identifier
    revision: Revision


class ToolPolicy(Contract):
    version: Literal["1"] = "1"
    mode: Mode
    allowed: tuple[Tool, ...]

    @model_validator(mode="after")
    def validate_capabilities(self) -> Self:
        if len(set(self.allowed)) != len(self.allowed):
            raise ValueError("工具权限不能重复")
        if not set(self.allowed) <= MODE_TOOLS[self.mode]:
            raise ValueError("工具权限超出任务模式")
        return self

    def require(self, tool_name: str) -> None:
        if tool_name not in self.allowed:
            raise PermissionError("当前任务未获得此工具权限")


class TaskEnvelope(Contract):
    protocol_version: Literal["1"] = "1"
    context: RunContext
    role: Literal["processor", "reviewer"]
    mode: Mode
    objective: Annotated[str, Field(min_length=1)]
    budget: BudgetReference
    tool_policy: ToolPolicy
    memory_references: tuple[MemoryReference, ...] = ()
    parent_run_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.mode != self.tool_policy.mode:
            raise ValueError("任务与工具策略模式不一致")
        if (self.role == "reviewer") != (self.mode == "review"):
            raise ValueError("独审角色必须使用独审模式")
        if self.parent_run_id == self.context.run_id:
            raise ValueError("父运行不能是当前运行")
        return self


class ArtifactReference(Contract):
    artifact_id: Identifier
    context: RunContext
    relative_path: Annotated[str, Field(min_length=1)]
    sha256: Digest
    size_bytes: Annotated[int, Field(strict=True, ge=0)]
    kind: Literal["analysis", "manifest", "probe", "command_log", "diff", "review"]

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        parts = self.relative_path.split("/")
        if "\\" in self.relative_path or ":" in self.relative_path:
            raise ValueError("产物路径必须为归档内的相对路径")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("产物路径越界或不规范")
        return self


class StructuredResult(Contract):
    protocol_version: Literal["1"] = "1"
    context: RunContext
    mode: Mode
    status: Literal["completed", "waiting", "blocked"]
    summary: Annotated[str, Field(min_length=1)]
    artifacts: tuple[ArtifactReference, ...] = ()
    unresolved: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_artifact_ownership(self) -> Self:
        for artifact in self.artifacts:
            if artifact.context != self.context:
                raise ValueError("产物不属于当前运行与代码版本")
        if len({artifact.artifact_id for artifact in self.artifacts}) != len(self.artifacts):
            raise ValueError("结果包含重复产物")
        return self


class CheckRecord(Contract):
    check_id: Identifier
    side: Literal["base", "previous_head", "candidate"]
    commit_sha: GitObject
    tree_sha: GitObject
    environment_sha256: Digest
    command: Annotated[tuple[str, ...], Field(min_length=1)]
    exit_code: int
    outcome: Literal["passed", "target_failed", "existing_failed", "not_applicable", "unable"]
    log_artifact_id: Identifier
    probe_artifact_id: Identifier | None = None


class EvidencePackage(Contract):
    protocol_version: Literal["1"] = "1"
    context: RunContext
    acceptance_kind: Literal["bug", "feature", "refactor"]
    artifacts: Annotated[tuple[ArtifactReference, ...], Field(min_length=1)]
    checks: Annotated[tuple[CheckRecord, ...], Field(min_length=1)]
    review_context: RunContext
    review_artifact_id: Identifier

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if self.context.code.candidate_sha is None:
            raise ValueError("最终证据必须绑定冻结候选")
        if self.review_context.run_id == self.context.run_id:
            raise ValueError("独审必须使用独立运行")
        expected_review = self.context.model_copy(
            update={
                "run_id": self.review_context.run_id,
                "configuration_id": self.review_context.configuration_id,
                "configuration_revision": self.review_context.configuration_revision,
            }
        )
        if self.review_context != expected_review:
            raise ValueError("独审不属于当前任务或候选版本")
        artifacts = {artifact.artifact_id: artifact for artifact in self.artifacts}
        if len(artifacts) != len(self.artifacts):
            raise ValueError("证据包含重复产物")
        for artifact in self.artifacts:
            if artifact.context not in (self.context, self.review_context):
                raise ValueError("证据产物归属或版本不匹配")
        review = artifacts.get(self.review_artifact_id)
        if review is None or review.kind != "review" or review.context != self.review_context:
            raise ValueError("缺少独立审查产物")
        for check in self.checks:
            log = artifacts.get(check.log_artifact_id)
            if log is None or log.kind != "command_log":
                raise ValueError("检查日志引用无效")
            if check.probe_artifact_id is not None:
                probe = artifacts.get(check.probe_artifact_id)
                if probe is None or probe.kind != "probe":
                    raise ValueError("检查探针引用无效")
            if check.side == "base" and check.commit_sha != self.context.code.base_sha:
                raise ValueError("修改前检查版本不匹配")
            if check.side == "candidate" and (
                check.commit_sha != self.context.code.candidate_sha
                or check.tree_sha != self.context.code.tree_sha
            ):
                raise ValueError("候选检查版本不匹配")
        return self


def validate_result(envelope: TaskEnvelope, payload: dict) -> StructuredResult:
    result = StructuredResult.model_validate(payload)
    if result.context != envelope.context or result.mode != envelope.mode:
        raise ValueError("交接结果不属于当前授权任务、运行、配置或代码版本")
    return result
