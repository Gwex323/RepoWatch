"""Docker CLI boundary: fixed images, named volumes and bounded process lifetimes."""

import ipaddress
import json
import os
import re
import subprocess
import tarfile
import threading
import time
from uuid import uuid4

from .preview_proxy import SCRIPT
from .repository_source import IMAGES, ExecutionError

LOG_LIMIT = 64000


class DockerRuntime:
    def __init__(self, namespace):
        self.namespace = namespace

    def command(self, args, *, timeout=30, check=lambda: None, stdin=None, allow_failure=False):
        output = bytearray()
        try:
            process = subprocess.Popen(
                ["docker", *args], stdin=stdin or subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError:
            raise ExecutionError("docker_unavailable") from None

        def read():
            while chunk := process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > LOG_LIMIT:
                    del output[:-LOG_LIMIT]

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                check()
                if time.monotonic() > deadline:
                    raise ExecutionError("execution_timeout")
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join(timeout=5)
            process.stdout.close()
        text = bytes(output).decode("utf-8", errors="replace")
        if process.returncode and not allow_failure:
            raise ExecutionError("docker_command_failed", log=text)
        return process.returncode, text

    def readiness(self):
        try:
            _, raw = self.command(["context", "inspect"], timeout=10)
            endpoint = json.loads(raw)[0]["Endpoints"]["docker"]["Host"]
            if not endpoint.startswith(("npipe://", "unix://")):
                raise ExecutionError("docker_local_engine_required")
            _, raw = self.command(["info", "--format", "{{.OSType}}"], timeout=10)
            if raw.strip() != "linux":
                raise ExecutionError("docker_linux_required")
            return {"ready": True, "reason": ""}
        except (ExecutionError, ValueError, KeyError, IndexError) as exc:
            reason = str(exc)
            return {"ready": False, "reason": reason if reason in (
                "docker_local_engine_required", "docker_linux_required",
            ) else "docker_unavailable"}

    def names(self, env):
        base = f"rw-{self.namespace}-{env['id']}"
        return base, base + "-data", base + "-net"

    def labels(self, env):
        labels = ["--label", f"repowatch.instance={self.namespace}",
                  "--label", f"repowatch.environment={env['id']}"]
        if env["id"].startswith("cache_"):
            labels.extend(["--label", f"repowatch.image={env['image_id']}"])
        return labels

    def image(self, language, check):
        image = IMAGES[language]
        code, raw = self.command(["image", "inspect", image, "--format", "{{.Id}}"],
                                 allow_failure=True)
        if code:
            self.command(["pull", image], timeout=600, check=check)
            _, raw = self.command(["image", "inspect", image, "--format", "{{.Id}}"])
        image_id = raw.strip()
        if not re.fullmatch("sha256:[0-9a-f]{64}", image_id):
            raise ExecutionError("invalid_image")
        return image_id

    def remove(self, env):
        name, _, _ = self.names(env)
        for container in (name + "-preview", name):
            _, raw = self.command(["container", "ls", "-aq", "--filter", f"name=^/{container}$"])
            if not raw.strip():
                continue
            _, label = self.command(
                ["inspect", "--format", '{{index .Config.Labels "repowatch.instance"}}', container])
            if label.strip() != self.namespace:
                raise ExecutionError("docker_resource_mismatch")
            self.command(["rm", "-f", container])

    def seed(self, env, source, check):
        import tempfile

        name, volume, _ = self.names(env)
        self.remove(env)
        self.command(["volume", "create", *self.labels(env), volume], check=check)
        self._owned_volume(env)  # volume create can return an existing, foreign-named volume.
        self.command(self.create_args(env, "setup", "true", network="none"), check=check)
        try:
            # Docker cp reads trusted tar metadata; no host directory is mounted into the project.
            with tempfile.TemporaryFile() as payload:
                with tarfile.open(fileobj=payload, mode="w") as archive:
                    for directory in ("project", "home"):
                        info = tarfile.TarInfo(directory)
                        info.type, info.mode = tarfile.DIRTYPE, 0o755
                        info.uid = info.gid = 1000
                        archive.addfile(info)
                    for file in (sorted(source.rglob("*")) if source is not None else ()):
                        check()
                        if file.is_symlink():
                            raise ExecutionError("unsafe_archive")
                        if file.is_file() or file.is_dir():
                            info = archive.gettarinfo(str(file), "project/" +
                                                      file.relative_to(source).as_posix())
                            info.uid = info.gid = 1000
                            info.uname = info.gname = ""
                            if file.is_dir():
                                info.mode = 0o755
                                archive.addfile(info)
                            else:
                                with file.open("rb") as content:
                                    archive.addfile(info, content)
                payload.seek(0)
                self.command(["cp", "-a", "-", f"{name}:/workspace"], timeout=120,
                             check=check, stdin=payload)
        finally:
            self.remove(env)

    @staticmethod
    def _cache_env(env, cache_id):
        if not re.fullmatch(r"cache_[a-f0-9]{16}", cache_id):
            raise ExecutionError("dependency_cache_unavailable")
        return {**env, "id": cache_id}

    def _owned_volume(self, env):
        _, volume, _ = self.names(env)
        _, raw = self.command(["volume", "inspect", volume])
        try:
            labels = json.loads(raw)[0].get("Labels") or {}
            valid = (labels.get("repowatch.instance") == self.namespace
                     and labels.get("repowatch.environment") == env["id"])
            if env["id"].startswith("cache_"):
                valid = valid and labels.get("repowatch.image") == env["image_id"]
            if not valid:
                raise ExecutionError("docker_resource_mismatch")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ExecutionError("dependency_cache_unavailable") from None
        return volume

    def _copy_dependencies(self, source_env, target_env, check):
        source_volume = self._owned_volume(source_env)
        target_volume = self._owned_volume(target_env)
        name = self.names(target_env)[0] + "-copy-" + uuid4().hex[:8]
        self.command([
            "create", "--name", name, *self.labels(target_env), *self.policy(),
            "--network", "none", "--mount",
            f"type=volume,src={source_volume},dst=/from,readonly", "--mount",
            f"type=volume,src={target_volume},dst=/workspace", target_env["image_id"],
            "/bin/sh", "-c", "cp -a /from/home/. /workspace/home/ && "
            "if [ -d /from/project/node_modules ]; then "
            "cp -a /from/project/node_modules /workspace/project/; fi",
        ], check=check)
        try:
            self.command(["start", "--attach", name], timeout=120, check=check)
        finally:
            self.command(["rm", "-f", name], allow_failure=True)

    def snapshot_dependencies(self, env, cache_id, check):
        cache_env = self._cache_env(env, cache_id)
        self.seed(cache_env, None, check)
        self._copy_dependencies(env, cache_env, check)

    def restore_dependencies(self, env, source, cache_id, check):
        cache_env = self._cache_env(env, cache_id)
        self._owned_volume(cache_env)  # Refuse missing or foreign cache before touching the target.
        self.seed(env, source, check)
        self._copy_dependencies(cache_env, env, check)

    def delete_dependencies(self, env, cache_id):
        cache_env = self._cache_env(env, cache_id)
        self._owned_volume(cache_env)
        self.cleanup(cache_env)

    @staticmethod
    def policy():
        return [
            "--init",
            "--user", "1000:1000", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--pids-limit", "256",
            "--cpus", "2", "--memory", "1g", "--memory-swap", "1g",
            "--ulimit", "nofile=1024:1024", "--ulimit", "fsize=268435456:268435456",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m,mode=1777",
            "--log-driver", "local", "--log-opt", "max-size=1m", "--log-opt", "max-file=2",
        ]

    def create_args(self, env, action, command, *, network):
        name, volume, _ = self.names(env)
        args = [
            "create", "--name", name, *self.labels(env), *self.policy(),
            "--mount", f"type=volume,src={volume},dst=/workspace",
            "--workdir", "/workspace/project", "--network", network,
            "--env", "HOME=/workspace/home", "--env", "GOCACHE=/workspace/home/go-build",
            "--env", "GOPATH=/workspace/home/go", "--env", "GOTOOLCHAIN=local",
            "--env", "PIP_NO_INPUT=1", "--env", "CI=true",
        ]
        return [*args, env["image_id"], "timeout", "--signal=TERM", "--kill-after=10s",
                "1800s" if action == "start" else "600s", "/bin/sh", "-c", command]

    def preview(self, env, check):
        name, _, network = self.names(env)
        _, raw = self.command(["inspect", "--format", "{{json .NetworkSettings.Networks}}", name])
        address = json.loads(raw)[network]["IPAddress"]
        ipaddress.IPv4Address(address)
        image = self.image("python", check)
        proxy = name + "-preview"
        self.command([
            "create", "--name", proxy, *self.labels(env), *self.policy(),
            "--network", "bridge", "--sysctl", "net.ipv4.ip_forward=0",
            "--publish", "127.0.0.2::8000", image,
            "timeout", "--signal=TERM", "--kill-after=10s", "1800s",
            "python", "-c", SCRIPT, address, str(env["config"]["port"]),
        ], check=check)
        self.command(["start", proxy], check=check)
        self.command(["network", "connect", network, proxy], check=check)
        _, raw = self.command(["inspect", "--format", "{{json .NetworkSettings.Ports}}", proxy])
        bindings = json.loads(raw).get("8000/tcp") or []
        if not bindings or bindings[0]["HostIp"] != "127.0.0.2":
            raise ExecutionError("port_unavailable")
        return int(bindings[0]["HostPort"])

    def run(self, env, action, check):
        name, _, network = self.names(env)
        self.remove(env)
        if action == "start":
            code, _ = self.command(["network", "inspect", network], allow_failure=True)
            if code:
                self.command(["network", "create", "--internal", *self.labels(env), network])
        else:
            network = "bridge" if action == "setup" and env["config"]["install_network"] else "none"
        command = env["config"]["install" if action == "setup" else action]
        self.command(self.create_args(env, action, command or "true", network=network), check=check)
        try:
            self.command(["start", name], check=check)
            if action == "start":
                return self.preview(env, check)
            deadline = time.monotonic() + 620
            while True:
                check()
                state = self.inspect(env)
                if not state["Running"]:
                    return state["ExitCode"], self.logs(env)
                if time.monotonic() > deadline:
                    raise ExecutionError("execution_timeout")
                time.sleep(0.25)
        except BaseException as exc:
            if isinstance(exc, ExecutionError) and not exc.log:
                try:
                    exc.log = self.logs(env)
                except ExecutionError:
                    pass
            self.remove(env)
            raise
        finally:
            if action != "start":
                self.remove(env)

    def inspect(self, env):
        name, _, _ = self.names(env)
        _, raw = self.command(["inspect", "--format", "{{json .State}}", name])
        try:
            state = json.loads(raw)
            if env["state"] == "running" and state["Running"]:
                _, proxy = self.command(["inspect", "--format", "{{json .State}}",
                                         name + "-preview"])
                state["Running"] = json.loads(proxy)["Running"]
            return state
        except ValueError:
            raise ExecutionError("docker_invalid_response") from None

    def listening(self, env):
        name, _, network = self.names(env)
        _, raw = self.command(["inspect", "--format", "{{json .NetworkSettings.Networks}}", name])
        address = json.loads(raw)[network]["IPAddress"]
        ipaddress.IPv4Address(address)
        code, _ = self.command([
            "exec", name + "-preview", "python", "-c",
            "import socket,sys; socket.create_connection((sys.argv[1],int(sys.argv[2])),1).close()",
            address, str(env["config"]["port"]),
        ], timeout=5, allow_failure=True)
        return code == 0

    def logs(self, env):
        name, _, _ = self.names(env)
        _, raw = self.command(["logs", "--tail", "250", name], allow_failure=True)
        return raw[-LOG_LIMIT:]

    def cleanup(self, env):
        name, volume, network = self.names(env)
        self.remove(env)
        for kind, resource in (("volume", volume), ("network", network)):
            code, raw = self.command([kind, "inspect", resource], allow_failure=True)
            if code:
                continue
            if json.loads(raw)[0].get("Labels", {}).get("repowatch.instance") != self.namespace:
                raise ExecutionError("docker_resource_mismatch")
            self.command([kind, "rm", resource])
