import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

IMAGE = "alpine/git:latest"
SUBMODULE_SHA = "1" * 40
LFS_OID = "a" * 64
LFS_SIZE = 12
LFS_POINTER = (
    f"version https://git-lfs.github.com/spec/v1\noid sha256:{LFS_OID}\nsize {LFS_SIZE}\n"
).encode()
FIXTURE = {
    ".gitattributes": b"* -text\n",
    "lf.txt": b"line1\nline2\n",
    "crlf.txt": b"line1\r\nline2\r\n",
    "Dir/File.txt": b"upper\n",
    "Dir/file.txt": b"lower\n",
    "run.sh": b"#!/bin/sh\nprintf 'ok\\n'\n",
    "link.txt": b"lf.txt",
    "large.bin": LFS_POINTER,
    ".gitmodules": (
        b'[submodule "vendor/lib"]\n\tpath = vendor/lib\n\turl = https://example.invalid/lib.git\n'
    ),
    "tracked.txt": b"before\n",
}

MODES = {
    ".gitattributes": "100644",
    "lf.txt": "100644",
    "crlf.txt": "100644",
    "Dir/File.txt": "100644",
    "Dir/file.txt": "100644",
    "run.sh": "100755",
    "link.txt": "120000",
    "large.bin": "100644",
    ".gitmodules": "100644",
    "vendor/lib": "160000",
    "tracked.txt": "100644",
}

CONTAINER_SCRIPT = r"""set -eu
git config --global user.name fixture
git config --global user.email fixture@example.invalid
git config --global init.defaultBranch main
git config --global core.autocrlf false
mkdir -p /work/in /work/out
cp /fixture.bundle /work/in/fixture.bundle
git clone --no-checkout /work/in/fixture.bundle /work/src >/dev/null 2>&1
git -C /work/src checkout --force main >/dev/null 2>&1
cd /work/src
: > /work/out/manifest.tsv
git ls-files -s | while IFS='	' read -r meta path; do
  mode=${meta%% *}
  rest=${meta#* }
  sha=${rest%% *}
  kind=file
  exec_bit=false
  sha256=-
  size=-
  target=-
  lfs=false
  available=-
  if [ "$mode" = "160000" ]; then
    kind=gitlink
    if git cat-file -e "$sha" 2>/dev/null; then available=true; else available=false; fi
  elif [ "$mode" = "120000" ]; then
    kind=symlink
    target=$(readlink "$path")
    sha256=$(printf '%s' "$target" | sha256sum | cut -d' ' -f1)
    size=$(printf '%s' "$target" | wc -c | tr -d ' ')
  else
    if [ -x "$path" ]; then exec_bit=true; fi
    sha256=$(sha256sum "$path" | cut -d' ' -f1)
    size=$(wc -c < "$path" | tr -d ' ')
    if head -n 1 "$path" | grep -q '^version https://git-lfs.github.com/spec/v1$'; then
      lfs=true
    fi
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$mode" "$sha" "$path" "$kind" "$exec_bit" "$sha256" "$size" "$target" "$lfs" "$available" \
    >> /work/out/manifest.tsv
done
git rev-parse HEAD > /work/out/head.txt
git rev-parse 'HEAD^{tree}' > /work/out/tree.txt
printf 'after\n' >> tracked.txt
git add tracked.txt
git commit -m candidate >/dev/null
git rev-parse HEAD > /work/out/candidate_head.txt
git rev-parse 'HEAD^{tree}' > /work/out/candidate_tree.txt
git rev-parse 'HEAD:tracked.txt' > /work/out/candidate_tracked_blob.txt
git bundle create /work/out/candidate.bundle main >/dev/null
git --version > /work/out/git_version.txt
"""


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def run(command, *, cwd=None, input_bytes=None, env=None, check=True, timeout=180):
    result = subprocess.run(
        command,
        cwd=cwd,
        input=input_bytes,
        capture_output=True,
        check=False,
        env=env,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败：{command[0]}（退出码 {result.returncode}）")
    return result


def git(repo: Path, *arguments: str, input_bytes=None, env=None, check=True):
    return run(
        ["git", "-C", str(repo), *arguments],
        input_bytes=input_bytes,
        env=env,
        check=check,
    )


def git_environment(root: Path) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    }


def expected_entry(path: str, mode: str, blob: str, content: bytes | None) -> dict:
    if mode == "160000":
        return {
            "path": path,
            "mode": mode,
            "blob": blob,
            "kind": "gitlink",
            "exec": False,
            "sha256": None,
            "size": None,
            "target": None,
            "lfs": False,
            "object_available": False,
        }
    if mode == "120000":
        target = content.decode()
        return {
            "path": path,
            "mode": mode,
            "blob": blob,
            "kind": "symlink",
            "exec": False,
            "sha256": sha256_bytes(target.encode()),
            "size": len(target.encode()),
            "target": target,
            "lfs": False,
            "object_available": None,
        }
    return {
        "path": path,
        "mode": mode,
        "blob": blob,
        "kind": "file",
        "exec": mode == "100755",
        "sha256": sha256_bytes(content),
        "size": len(content),
        "target": None,
        "lfs": content.startswith(b"version https://git-lfs.github.com/spec/v1"),
        "object_available": None,
    }


def create_fixture(root: Path) -> dict:
    repository = root / "fixture"
    environment = git_environment(root)
    run(["git", "init", "--initial-branch=main", str(repository)], env=environment)
    git(repository, "config", "core.autocrlf", "false", env=environment)
    git(repository, "config", "core.symlinks", "false", env=environment)
    blobs = {}
    for path, content in FIXTURE.items():
        blobs[path] = (
            git(repository, "hash-object", "-w", "--stdin", input_bytes=content, env=environment)
            .stdout.decode()
            .strip()
        )
    blobs["vendor/lib"] = SUBMODULE_SHA
    for path, mode in MODES.items():
        git(
            repository,
            "update-index",
            "--add",
            "--cacheinfo",
            f"{mode},{blobs[path]},{path}",
            env=environment,
        )
    tree = git(repository, "write-tree", env=environment).stdout.decode().strip()
    commit = (
        git(repository, "commit-tree", tree, "-m", "fixture", env=environment)
        .stdout.decode()
        .strip()
    )
    git(repository, "update-ref", "refs/heads/main", commit, env=environment)
    bundle = root / "fixture.bundle"
    git(repository, "bundle", "create", str(bundle), "--all", env=environment)
    entries = [
        expected_entry(path, MODES[path], blobs[path], FIXTURE.get(path)) for path in sorted(MODES)
    ]
    return {
        "repository": repository,
        "bundle": bundle,
        "tree": tree,
        "commit": commit,
        "entries": entries,
    }


def docker(*arguments: str, check=True, timeout=180):
    return run(["docker", *arguments], check=check, timeout=timeout)


def parse_manifest(text: str) -> list[dict]:
    entries = []
    for line in text.splitlines():
        if not line:
            continue
        columns = line.split("\t")
        if len(columns) != 10:
            raise ValueError("容器清单列数无效")
        mode, blob, path, kind, exec_bit, digest, size, target, lfs, available = columns
        entries.append(
            {
                "path": path,
                "mode": mode,
                "blob": blob,
                "kind": kind,
                "exec": exec_bit == "true",
                "sha256": None if digest == "-" else digest,
                "size": None if size == "-" else int(size),
                "target": None if target == "-" else target,
                "lfs": lfs == "true",
                "object_available": None if available == "-" else available == "true",
            }
        )
    return entries


def reconcile(expected: list[dict], actual: list[dict]) -> tuple[dict, list[str]]:
    expected_by_path = {entry["path"]: entry for entry in expected}
    actual_by_path = {entry["path"]: entry for entry in actual}
    mismatches = []
    if set(expected_by_path) != set(actual_by_path):
        mismatches.append("path_set")
    for path in sorted(set(expected_by_path) & set(actual_by_path)):
        for key in ("mode", "blob", "kind", "exec", "sha256", "size", "target", "lfs"):
            if expected_by_path[path][key] != actual_by_path[path][key]:
                mismatches.append(f"{path}:{key}")
    checks = {
        "paths_match": set(expected_by_path) == set(actual_by_path),
        "modes_match": all(
            expected_by_path[path]["mode"] == actual_by_path[path]["mode"]
            for path in expected_by_path
            if path in actual_by_path
        ),
        "blobs_match": all(
            expected_by_path[path]["blob"] == actual_by_path[path]["blob"]
            for path in expected_by_path
            if path in actual_by_path
        ),
        "contents_match": not any(
            mismatch.endswith((":sha256", ":size", ":target")) for mismatch in mismatches
        ),
        "newline_preserved": all(
            path in actual_by_path
            and actual_by_path[path]["sha256"] == expected_by_path[path]["sha256"]
            for path in ("lf.txt", "crlf.txt")
        ),
        "case_distinct_paths": all(
            path in actual_by_path
            and actual_by_path[path]["sha256"] == expected_by_path[path]["sha256"]
            for path in ("Dir/File.txt", "Dir/file.txt")
        ),
        "exec_bit_preserved": actual_by_path.get("run.sh", {}).get("exec") is True
        and actual_by_path.get("run.sh", {}).get("mode") == "100755",
        "symlink_preserved": actual_by_path.get("link.txt", {}).get("kind") == "symlink"
        and actual_by_path.get("link.txt", {}).get("target") == "lf.txt",
        "lfs_pointer_detected": actual_by_path.get("large.bin", {}).get("lfs") is True,
        "submodule_recorded": actual_by_path.get("vendor/lib", {}).get("kind") == "gitlink"
        and actual_by_path.get("vendor/lib", {}).get("blob") == SUBMODULE_SHA,
    }
    return checks, mismatches


def delivery_blockers(entries: list[dict]) -> list[str]:
    blockers = []
    for entry in entries:
        if entry["lfs"]:
            blockers.append(f"lfs_entity_missing:{entry['path']}")
        if entry["kind"] == "gitlink" and entry["object_available"] is False:
            blockers.append(f"submodule_object_missing:{entry['path']}")
    return blockers


def verify_candidate(root: Path, bundle: Path, manifest: list[dict], candidate: dict) -> dict:
    repository = root / "candidate"
    environment = git_environment(root)
    run(["git", "init", "--bare", str(repository)], env=environment)
    git(
        repository,
        "fetch",
        str(bundle),
        "main:refs/heads/main",
        env=environment,
    )
    tree = git(repository, "rev-parse", "main^{tree}", env=environment).stdout.decode().strip()
    content = git(repository, "cat-file", "-p", "main:tracked.txt", env=environment).stdout
    blob = git(repository, "rev-parse", "main:tracked.txt", env=environment).stdout.decode().strip()
    missing = []
    for entry in manifest:
        if entry["kind"] == "gitlink":
            continue
        result = git(repository, "cat-file", "-e", entry["blob"], env=environment, check=False)
        if result.returncode != 0:
            missing.append(entry["path"])
    return {
        "tree_match": tree == candidate["tree"],
        "tracked_blob_match": blob == candidate["tracked_blob"],
        "tracked_content_match": content == b"before\nafter\n",
        "all_manifest_blobs_present": not missing,
        "missing_blobs": missing,
    }


def run_experiment() -> dict:
    started = time.monotonic()
    stamp = datetime.now(UTC).isoformat()
    root = Path(tempfile.mkdtemp(prefix="repowatch-handoff-"))
    volume = f"repowatch-handoff-{uuid.uuid4().hex[:12]}"
    container = f"repowatch-handoff-{uuid.uuid4().hex[:12]}"
    report = {
        "experiment": "E11/E15",
        "protocol_version": "1",
        "slice": "windows_git_objects_to_linux_worktree_and_candidate_return",
        "started_at": stamp,
        "status": "failed",
        "error": None,
        "stage": "fixture",
        "image": {"reference": IMAGE, "digest": None, "git_version": None},
        "isolation": {
            "network": "none",
            "host_mounts": [],
            "cap_drop": ["ALL"],
            "no_new_privileges": True,
            "credential_env": [],
        },
        "fixture": None,
        "checks": {},
        "manifest": {"count": 0, "entries": []},
        "candidate": None,
        "delivery": {"ready": False, "blockers": []},
        "mismatches": [],
        "container_exit_code": None,
        "finished_at": None,
    }
    try:
        fixture = create_fixture(root)
        report["stage"] = "image"
        report["fixture"] = {
            "commit": fixture["commit"],
            "tree": fixture["tree"],
            "files": [
                {"path": entry["path"], "mode": entry["mode"], "kind": entry["kind"]}
                for entry in fixture["entries"]
            ],
        }
        digest = (
            docker("image", "inspect", IMAGE, "--format", "{{index .RepoDigests 0}}")
            .stdout.decode()
            .strip()
        )
        report["image"]["digest"] = digest or None
        report["stage"] = "volume"
        docker("volume", "create", volume)
        report["stage"] = "container"
        docker(
            "create",
            "--name",
            container,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "sh",
            "-v",
            f"{volume}:/work",
            IMAGE,
            "-eu",
            "/handoff.sh",
        )
        report["stage"] = "copy_in"
        docker("cp", str(fixture["bundle"]), f"{container}:/fixture.bundle")
        script = root / "run.sh"
        script.write_text(CONTAINER_SCRIPT, encoding="utf-8", newline="\n")
        docker("cp", str(script), f"{container}:/handoff.sh")
        report["stage"] = "container_run"
        result = docker("start", "-a", container, check=False)
        report["container_exit_code"] = result.returncode
        if result.returncode != 0:
            report["error"] = "container_failed"
            return report
        report["stage"] = "copy_out"
        outputs = {}
        for name in (
            "manifest.tsv",
            "head.txt",
            "tree.txt",
            "candidate.bundle",
            "candidate_head.txt",
            "candidate_tree.txt",
            "candidate_tracked_blob.txt",
            "git_version.txt",
        ):
            target = root / name
            docker("cp", f"{container}:/work/out/{name}", str(target))
            outputs[name] = target
        report["stage"] = "reconcile"
        manifest = parse_manifest(outputs["manifest.tsv"].read_text(encoding="utf-8"))
        report["manifest"] = {
            "count": len(manifest),
            "entries": [
                {
                    "path": entry["path"],
                    "mode": entry["mode"],
                    "kind": entry["kind"],
                    "sha256": entry["sha256"],
                    "size": entry["size"],
                    "lfs": entry["lfs"],
                    "object_available": entry["object_available"],
                }
                for entry in manifest
            ],
        }
        checks, mismatches = reconcile(fixture["entries"], manifest)
        report["checks"] = checks
        report["mismatches"] = mismatches
        blockers = delivery_blockers(manifest)
        report["delivery"] = {"ready": not blockers, "blockers": blockers}
        candidate = {
            "head": outputs["candidate_head.txt"].read_text(encoding="utf-8").strip(),
            "tree": outputs["candidate_tree.txt"].read_text(encoding="utf-8").strip(),
            "tracked_blob": outputs["candidate_tracked_blob.txt"]
            .read_text(encoding="utf-8")
            .strip(),
        }
        candidate.update(verify_candidate(root, outputs["candidate.bundle"], manifest, candidate))
        candidate["bundle_sha256"] = sha256_bytes(outputs["candidate.bundle"].read_bytes())
        report["candidate"] = candidate
        report["image"]["git_version"] = (
            outputs["git_version.txt"].read_text(encoding="utf-8").strip()
        )
        report["checks"]["head_matches_fixture"] = (
            outputs["head.txt"].read_text(encoding="utf-8").strip() == fixture["commit"]
        )
        report["checks"]["tree_matches_fixture"] = (
            outputs["tree.txt"].read_text(encoding="utf-8").strip() == fixture["tree"]
        )
        report["checks"].update(
            {
                "candidate_tree_reconciles": candidate["tree_match"],
                "candidate_content_reconciles": candidate["tracked_content_match"]
                and candidate["tracked_blob_match"],
                "candidate_objects_complete": candidate["all_manifest_blobs_present"],
            }
        )
        mechanics = [
            value
            for key, value in report["checks"].items()
            if key.startswith(
                (
                    "paths_",
                    "modes_",
                    "blobs_",
                    "contents_",
                    "newline_",
                    "case_",
                    "exec_",
                    "symlink_",
                    "lfs_",
                    "submodule_",
                    "head_",
                    "tree_",
                    "candidate_",
                )
            )
        ]
        report["status"] = "passed" if all(mechanics) else "failed"
        return report
    except Exception as error:
        report["error"] = type(error).__name__
        return report
    finally:
        docker("rm", "-f", container, check=False, timeout=60)
        docker("volume", "rm", "-f", volume, check=False, timeout=60)
        shutil.rmtree(root, ignore_errors=True)
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        report["finished_at"] = datetime.now(UTC).isoformat()
