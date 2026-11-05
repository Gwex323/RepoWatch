import os

import pytest

from repowatch.runtime.git_handoff import (
    SUBMODULE_SHA,
    create_fixture,
    delivery_blockers,
    expected_entry,
    parse_manifest,
    reconcile,
    run_experiment,
)


def test_fixture_index_contains_special_modes(tmp_path):
    fixture = create_fixture(tmp_path)
    entries = {entry["path"]: entry for entry in fixture["entries"]}
    assert entries["run.sh"]["mode"] == "100755"
    assert entries["run.sh"]["exec"] is True
    assert entries["link.txt"]["mode"] == "120000"
    assert entries["link.txt"]["target"] == "lf.txt"
    assert entries["vendor/lib"]["mode"] == "160000"
    assert entries["vendor/lib"]["blob"] == SUBMODULE_SHA
    assert entries["Dir/File.txt"]["sha256"] != entries["Dir/file.txt"]["sha256"]
    assert entries["crlf.txt"]["sha256"] != entries["lf.txt"]["sha256"]
    assert entries["large.bin"]["lfs"] is True
    assert fixture["bundle"].is_file()


def test_manifest_reconciliation_reports_specific_mismatch():
    expected = [
        expected_entry("run.sh", "100755", "a" * 40, b"#!/bin/sh\n"),
        expected_entry("link.txt", "120000", "b" * 40, b"target"),
    ]
    actual = [
        {**expected[0], "exec": False, "mode": "100644"},
        expected[1],
    ]
    checks, mismatches = reconcile(expected, actual)
    assert checks["exec_bit_preserved"] is False
    assert "run.sh:mode" in mismatches
    assert "run.sh:exec" in mismatches


def test_manifest_parser_preserves_boolean_and_empty_fields():
    text = (
        "100644\t" + "a" * 40 + "\tlf.txt\tfile\ttrue\t" + "b" * 64 + "\t12\t-\tfalse\t-\n"
        "160000\t" + "1" * 40 + "\tvendor/lib\tgitlink\tfalse\t-\t-\t-\tfalse\tfalse\n"
    )
    entries = parse_manifest(text)
    assert entries[0]["exec"] is True
    assert entries[0]["size"] == 12
    assert entries[0]["target"] is None
    assert entries[1]["object_available"] is False


def test_missing_lfs_and_submodule_block_delivery():
    entries = [
        expected_entry(
            "large.bin", "100644", "a" * 40, b"version https://git-lfs.github.com/spec/v1\n"
        ),
        expected_entry("vendor/lib", "160000", SUBMODULE_SHA, None),
    ]
    entries[1]["object_available"] = False
    assert delivery_blockers(entries) == [
        "lfs_entity_missing:large.bin",
        "submodule_object_missing:vendor/lib",
    ]


@pytest.mark.skipif(
    os.environ.get("REPOWATCH_DOCKER_E2E") != "1",
    reason="需要显式启用真实 Docker 对象交接",
)
def test_docker_object_handoff_end_to_end():
    report = run_experiment()
    assert report["status"] == "passed"
    assert report["delivery"]["ready"] is False
    assert report["delivery"]["blockers"] == [
        "lfs_entity_missing:large.bin",
        "submodule_object_missing:vendor/lib",
    ]
    assert report["checks"]["newline_preserved"] is True
    assert report["checks"]["case_distinct_paths"] is True
    assert report["checks"]["exec_bit_preserved"] is True
    assert report["checks"]["symlink_preserved"] is True
    assert report["checks"]["candidate_tree_reconciles"] is True
