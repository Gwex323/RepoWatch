import base64
import os
import re
import shutil
import subprocess
from pathlib import Path


class GitObjectError(RuntimeError):
    pass


class GitObjects:
    def __init__(self, root: Path, *, executable=None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.executable = executable or shutil.which("git")
        if not self.executable:
            raise GitObjectError("Git 未安装")
        self.hooks = self.root / "empty-hooks"
        self.hooks.mkdir(exist_ok=True)

    def repository(self, identifier):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", identifier):
            raise ValueError("受管仓库标识无效")
        path = self.root / (identifier + ".git")
        if path.resolve().parent != self.root or path.is_symlink():
            raise PermissionError("受管对象目录越界")
        return path

    def run(self, repository, *arguments, secret=None, input_bytes=None):
        repository = Path(repository).resolve()
        if repository.parent != self.root:
            raise PermissionError("只能访问受管对象目录")
        environment = {
            key: os.environ[key]
            for key in ("SystemRoot", "WINDIR", "TEMP", "TMP", "PATH")
            if key in os.environ
        }
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_LFS_SKIP_SMUDGE": "1",
            }
        )
        command = [
            self.executable,
            "-c",
            "core.hooksPath=" + str(self.hooks),
            "-c",
            "credential.helper=",
            "-c",
            "core.autocrlf=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
        ]
        if secret is not None:
            encoded = base64.b64encode(("x-access-token:" + secret.reveal()).encode()).decode()
            environment["REPOWATCH_GIT_AUTH"] = "Authorization: Basic " + encoded
            command += ["--config-env=http.https://github.com/.extraHeader=REPOWATCH_GIT_AUTH"]
        try:
            result = subprocess.run(
                command + ["-C", str(repository), *arguments],
                env=environment,
                input=input_bytes,
                capture_output=True,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise GitObjectError("受管 Git 操作无法完成") from None
        finally:
            environment.pop("REPOWATCH_GIT_AUTH", None)
        if result.returncode:
            raise GitObjectError("受管 Git 对象操作失败")
        return result.stdout

    def initialize(self, identifier):
        repository = self.repository(identifier)
        if not repository.exists():
            repository.mkdir()
            self.run(repository, "init", "--bare", "--template=")
        if self.run(repository, "rev-parse", "--is-bare-repository").strip() != b"true":
            raise GitObjectError("对象副本必须为无检出裸仓库")
        return repository

    def fetch(self, identifier, owner, name, branch, *, secret):
        if any(
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) or value in {".", ".."}
            for value in (owner, name)
        ):
            raise ValueError("GitHub 路径无效")
        repository = self.initialize(identifier)
        self.run(repository, "check-ref-format", "refs/heads/" + branch)
        self.run(
            repository,
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "https://github.com/" + owner + "/" + name + ".git",
            "+refs/heads/" + branch + ":refs/repowatch/target",
            secret=secret,
        )
        return self.run(repository, "rev-parse", "refs/repowatch/target^{commit}").decode().strip()

    def import_bundle(self, identifier, bundle: Path, reference="refs/heads/main"):
        bundle = bundle.resolve(strict=True)
        if not bundle.is_file() or bundle.is_symlink():
            raise ValueError("需要受控 bundle 文件")
        repository = self.initialize(identifier)
        self.run(repository, "check-ref-format", reference)
        self.run(
            repository,
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            str(bundle),
            "+" + reference + ":refs/repowatch/target",
        )
        return self.run(repository, "rev-parse", "refs/repowatch/target^{commit}").decode().strip()

    @staticmethod
    def require_sha(sha):
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("需要固定提交或对象 SHA")
        return sha

    def export(self, identifier, sha, destination: Path):
        self.require_sha(sha)
        repository = self.repository(identifier)
        reference = "refs/repowatch/snapshots/" + sha
        self.run(repository, "update-ref", reference, sha)
        self.run(repository, "bundle", "create", str(destination.resolve()), reference)
        return destination

    def commit_files(self, identifier, parent, files, *, message):
        self.require_sha(parent)
        repository = self.repository(identifier)
        tree = {}
        for path, entry in sorted(files.items()):
            parts = path.split("/")
            directory = tree
            for part in parts[:-1]:
                directory = directory.setdefault(part, {})
                if not isinstance(directory, dict):
                    raise GitObjectError("文件与目录路径冲突")
            mode, content = entry
            if mode not in {"100644", "100755", "120000"}:
                raise GitObjectError("候选对象模式不可交付")
            oid = (
                self.run(repository, "hash-object", "-w", "--stdin", input_bytes=content)
                .decode()
                .strip()
            )
            directory[parts[-1]] = (mode, oid)

        def write_tree(directory):
            entries = []
            for name, value in sorted(directory.items()):
                if isinstance(value, dict):
                    mode, kind, oid = "040000", "tree", write_tree(value)
                else:
                    mode, oid = value
                    kind = "blob"
                entries.append((mode + " " + kind + " " + oid + "\t" + name).encode() + b"\0")
            return (
                self.run(repository, "mktree", "-z", input_bytes=b"".join(entries)).decode().strip()
            )

        tree_sha = write_tree(tree)
        commit = (
            self.run(
                repository,
                "-c",
                "user.name=RepoWatch",
                "-c",
                "user.email=repowatch@users.noreply.github.com",
                "commit-tree",
                tree_sha,
                "-p",
                parent,
                input_bytes=message.encode(),
            )
            .decode()
            .strip()
        )
        return {"sha": commit, "tree_sha": tree_sha}

    def entries(self, identifier, sha):
        self.require_sha(sha)
        raw = self.run(self.repository(identifier), "ls-tree", "-rz", "--full-tree", sha)
        entries = []
        for record in raw.split(b"\0"):
            if record:
                metadata, path = record.split(b"\t", 1)
                mode, kind, oid = metadata.decode("ascii").split()
                entries.append(
                    {"path": path.decode("utf-8"), "mode": mode, "kind": kind, "oid": oid}
                )
        return entries

    def blob(self, identifier, oid):
        self.require_sha(oid)
        return self.run(self.repository(identifier), "cat-file", "blob", oid)
