import pytest
from pydantic import ValidationError

from repowatch.domain.protocols import (
    ArtifactReference,
    EvidencePackage,
    TaskEnvelope,
    ToolPolicy,
    validate_result,
)


def result_payload(envelope):
    return {
        "context": envelope.context.model_dump(),
        "mode": envelope.mode,
        "status": "completed",
        "summary": "待程序核验",
        "artifacts": [],
    }


def artifact_payload(context, artifact_id="log-1", kind="command_log"):
    return {
        "artifact_id": artifact_id,
        "context": context.model_dump(),
        "relative_path": f"runs/{artifact_id}.json",
        "sha256": "e" * 64,
        "size_bytes": 12,
        "kind": kind,
    }


def evidence_payload(envelope):
    review_context = envelope.context.model_copy(update={"run_id": "review-1"})
    return {
        "context": envelope.context.model_dump(),
        "acceptance_kind": "bug",
        "artifacts": [
            artifact_payload(envelope.context),
            artifact_payload(review_context, "review-1", "review"),
        ],
        "checks": [
            {
                "check_id": "check-1",
                "side": "candidate",
                "commit_sha": "b" * 40,
                "tree_sha": "c" * 40,
                "environment_sha256": "f" * 64,
                "command": ["pytest", "test_bug.py"],
                "exit_code": 0,
                "outcome": "passed",
                "log_artifact_id": "log-1",
            }
        ],
        "review_context": review_context.model_dump(),
        "review_artifact_id": "review-1",
    }


def test_envelope_json_round_trip(envelope):
    assert TaskEnvelope.model_validate_json(envelope.model_dump_json()) == envelope
    assert validate_result(envelope, result_payload(envelope)).status == "completed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "other"),
        ("run_id", "other"),
        ("repository_id", "other"),
        ("input_revision", 2),
        ("acceptance_revision", 2),
        ("execution_generation", 2),
        ("configuration_id", "other"),
        ("configuration_revision", 2),
    ],
)
def test_reject_result_from_other_authorization(envelope, field, value):
    payload = result_payload(envelope)
    payload["context"][field] = value
    with pytest.raises(ValueError, match="交接结果"):
        validate_result(envelope, payload)


@pytest.mark.parametrize(
    "field", ["base_sha", "candidate_sha", "tree_sha", "content_manifest_sha256"]
)
def test_reject_result_from_other_code(envelope, field):
    payload = result_payload(envelope)
    payload["context"]["code"][field] = "f" * len(payload["context"]["code"][field])
    with pytest.raises(ValueError, match="交接结果"):
        validate_result(envelope, payload)


@pytest.mark.parametrize("mode", ["scan", "review", "distill", "diagnose"])
def test_read_only_modes_cannot_get_write_permission(mode):
    with pytest.raises(ValidationError, match="超出任务模式"):
        ToolPolicy(mode=mode, allowed=("write_source",))


def test_reject_unknown_versions_and_fields(envelope):
    for field, value in [("protocol_version", "2"), ("publish", True)]:
        payload = envelope.model_dump()
        payload[field] = value
        with pytest.raises(ValidationError):
            TaskEnvelope.model_validate(payload)


@pytest.mark.parametrize("path", ["/etc/file", "../secret", "a/../b", "C:/secret", "a\\b"])
def test_reject_unsafe_archive_paths(envelope, path):
    payload = artifact_payload(envelope.context)
    payload["relative_path"] = path
    with pytest.raises(ValidationError):
        ArtifactReference.model_validate(payload)


def test_reject_other_run_artifact(envelope):
    payload = result_payload(envelope)
    payload["artifacts"] = [
        artifact_payload(envelope.context.model_copy(update={"run_id": "other"}))
    ]
    with pytest.raises(ValidationError, match="产物不属于"):
        validate_result(envelope, payload)


def test_evidence_schema_is_not_acceptance(envelope):
    payload = evidence_payload(envelope)
    assert EvidencePackage.model_validate(payload).acceptance_kind == "bug"
    payload["checks"][0]["outcome"] = "unable"
    assert EvidencePackage.model_validate(payload).checks[0].outcome == "unable"


@pytest.mark.parametrize(
    "field,value",
    [
        ("commit_sha", "a" * 40),
        ("tree_sha", "a" * 40),
        ("log_artifact_id", "missing"),
        ("probe_artifact_id", "missing"),
    ],
)
def test_reject_mismatched_evidence(envelope, field, value):
    payload = evidence_payload(envelope)
    payload["checks"][0][field] = value
    with pytest.raises(ValidationError):
        EvidencePackage.model_validate(payload)


def test_reject_review_in_processing_session(envelope):
    payload = evidence_payload(envelope)
    payload["review_context"]["run_id"] = envelope.context.run_id
    with pytest.raises(ValidationError, match="独立运行"):
        EvidencePackage.model_validate(payload)


def test_review_can_use_distinct_explicit_configuration(envelope):
    payload = evidence_payload(envelope)
    payload["review_context"]["configuration_id"] = "review-provider"
    payload["artifacts"][1]["context"]["configuration_id"] = "review-provider"
    assert (
        EvidencePackage.model_validate(payload).review_context.configuration_id == "review-provider"
    )
