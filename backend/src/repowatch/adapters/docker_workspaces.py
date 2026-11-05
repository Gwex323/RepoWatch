import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4


class WorkspaceError(RuntimeError):
    pass


def source_path(value):
    path = PurePosixPath(value)
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or "\0" in value
        or any(part in {".", "..", ".git"} for part in value.split("/"))
        or str(path) != value
    ):
        raise PermissionError("源码路径越界或不规范")
    return value


class DockerWorkspaces:
    def __init__(self, root: Path, objects, *, executable=None):
        self.root, self.objects = root.resolve(), objects
        self.root.mkdir(parents=True, exist_ok=True)
        self.executable = executable or shutil.which("docker")
        if not self.executable:
            raise WorkspaceError("Docker 不可用")

    def docker(self, *arguments, input_bytes=None, timeout=180, check=True):
        environment = {
            key: os.environ[key]
            for key in ("SystemRoot", "WINDIR", "TEMP", "TMP", "PATH", "USERPROFILE")
            if key in os.environ
        }
        try:
            result = subprocess.run(
                [self.executable, *arguments],
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise WorkspaceError("Docker 操作无法完成，需核对容器状态") from None
        if check and result.returncode:
            raise WorkspaceError("Docker 操作失败，请核对环境或镜像")
        return result

    def archive(self, repository_id, sha):
        entries = self.objects.entries(repository_id, sha)
        stream, manifest, gaps = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024), [], []
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for entry in entries:
                path = source_path(entry["path"])
                record = dict(entry)
                if entry["mode"] == "160000":
                    gaps.append(
                        {"path": path, "reason": "submodule_content_missing", "sha": entry["oid"]}
                    )
                    manifest.append(record)
                    continue
                content = self.objects.blob(repository_id, entry["oid"])
                record.update(sha256=hashlib.sha256(content).hexdigest(), size=len(content))
                member = tarfile.TarInfo(path)
                member.mode = 0o755 if entry["mode"] == "100755" else 0o644
                if entry["mode"] == "120000":
                    target = content.decode("utf-8")
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                    record["link_target"] = target
                    if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
                        gaps.append({"path": path, "reason": "external_symlink"})
                        manifest.append(record)
                        continue
                    member.type, member.linkname = tarfile.SYMTYPE, target
                    archive.addfile(member)
                else:
                    if content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                        gaps.append({"path": path, "reason": "lfs_entity_missing"})
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                manifest.append(record)
        stream.seek(0)
        return stream, manifest, gaps

    def create(self, repository_id, sha, *, task_id, mode, image="alpine/git:latest"):
        if mode not in {"scan", "review", "implement", "check"} or not task_id:
            raise ValueError("工作区需要任务归属和有效模式")
        archive, manifest, gaps = self.archive(repository_id, sha)
        if gaps:
            archive.close()
            return {"ready": False, "gaps": gaps, "manifest": manifest}
        digest = (
            self.docker("image", "inspect", image, "--format", "{{.Id}}").stdout.decode().strip()
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise WorkspaceError("镜像必须已安装且可固定摘要")
        identifier = "repowatch-" + uuid4().hex
        volume, loader = identifier + "-source", identifier + "-load"
        self.docker("volume", "create", volume)
        try:
            self.docker(
                "create",
                "--name",
                loader,
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--mount",
                "type=volume,src=" + volume + ",dst=/source",
                "--entrypoint",
                "sh",
                digest,
                "-c",
                "tar -xf /snapshot.tar -C /source",
            )
            with tempfile.TemporaryDirectory(prefix="snapshot-", dir=self.root) as temporary:
                bundle = Path(temporary) / "snapshot.tar"
                with bundle.open("wb") as destination:
                    shutil.copyfileobj(archive, destination, length=1024 * 1024)
                self.docker("cp", str(bundle), loader + ":/snapshot.tar")
            self.docker("start", "-a", loader)
            state = json.loads(self.docker("inspect", loader).stdout)[0]["State"]
            if state["ExitCode"] != 0:
                raise WorkspaceError("快照物化命令失败")
        except BaseException:
            self.docker("volume", "rm", "-f", volume, check=False)
            raise
        finally:
            archive.close()
            self.docker("rm", "-f", loader, check=False)
        return {
            "ready": True,
            "id": identifier,
            "volume": volume,
            "repository_id": repository_id,
            "task_id": task_id,
            "mode": mode,
            "sha": sha,
            "image": digest,
            "image_reference": image,
            "manifest": manifest,
            "gaps": [],
            "manifest_sha256": hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode()
            ).hexdigest(),
        }

    @staticmethod
    def require_workspace(workspace, task_id):
        if not workspace.get("ready") or workspace["task_id"] != task_id:
            raise PermissionError("工作区不属于当前任务或尚未物化")
        if not re.fullmatch(r"repowatch-[0-9a-f]{32}-source", workspace["volume"]):
            raise PermissionError("受管卷身份无效")

    def read_source(self, workspace, task_id, path):
        self.require_workspace(workspace, task_id)
        path = source_path(path)
        entries = {entry["path"]: entry for entry in workspace["manifest"]}
        if path not in entries or entries[path]["mode"] in {"120000", "160000"}:
            raise PermissionError("只允许读取清单内的实际文件")
        return self.objects.blob(workspace["repository_id"], entries[path]["oid"])

    def remove(self, workspace):
        self.require_workspace(workspace, workspace["task_id"])
        self.docker("volume", "rm", workspace["volume"])

    def exists(self, workspace):
        self.require_workspace(workspace, workspace["task_id"])
        return self.docker("volume", "inspect", workspace["volume"], check=False).returncode == 0

    def write_source(self, workspace, task_id, path, content):
        self.require_workspace(workspace, task_id)
        if workspace["mode"] != "implement":
            raise PermissionError("扫描与独审不能写入源码")
        path = source_path(path)
        if not isinstance(content, bytes) or len(content) > 16 * 1024 * 1024:
            raise ValueError("写入文件过大或内容无效")
        script = (
            'target=/source/$1; parent=$(dirname "$target"); test ! -L "$target"; '
            'while test "$parent" != /source; do test ! -L "$parent"; '
            'parent=$(dirname "$parent"); done; '
            'mkdir -p "$(dirname "$target")"; cat > "$target"'
        )
        self.docker(
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            "type=volume,src=" + workspace["volume"] + ",dst=/source",
            "--entrypoint",
            "sh",
            workspace["image"],
            "-eu",
            "-c",
            script,
            "writer",
            path,
            input_bytes=content,
        )

    def command_status(self, identity, *, stop=False):
        name = identity.get("container")
        if not isinstance(name, str) or not re.fullmatch(r"repowatch-command-[0-9a-f]{32}", name):
            raise PermissionError("命令容器身份无效")
        inspected = self.docker("inspect", name, check=False)
        if inspected.returncode:
            listed = self.docker(
                "ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.ID}}"
            )
            if listed.stdout.strip():
                return {"stopped": False, "identity_verified": False, "reason": "inspect_failed"}
            return {"stopped": True, "identity_verified": True, "reason": "container_absent"}
        container = json.loads(inspected.stdout)[0]
        if container.get("Config", {}).get("Labels", {}).get("repowatch.command") != name:
            raise PermissionError("容器标签与命令身份不匹配")
        if stop and container["State"]["Running"]:
            self.docker("kill", name)
            return self.command_status(identity)
        state = container["State"]
        return {
            "stopped": not state["Running"],
            "identity_verified": True,
            "exit_code": state["ExitCode"],
            "reason": "container_state",
            "started": state.get("StartedAt", "").startswith("0001-") is False,
            "oom_killed": state.get("OOMKilled", False),
        }
