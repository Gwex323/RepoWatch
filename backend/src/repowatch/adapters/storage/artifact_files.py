import os
import tempfile
from hashlib import sha256
from pathlib import Path


def verify_file(root: Path, relative_path: str, expected_sha256: str) -> dict:
    root = root.resolve()
    relative = Path(relative_path)
    target = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(root):
        return {"valid": False, "reason": "outside_archive"}
    try:
        digest = sha256()
        with target.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return {"valid": digest.hexdigest() == expected_sha256, "sha256": digest.hexdigest()}
    except OSError:
        return {"valid": False, "reason": "missing_or_unreadable"}


def write_immutable(root: Path, relative_path: str, data: bytes, expected_sha256: str):
    root = root.resolve()
    target = (root / relative_path).resolve()
    if Path(relative_path).name != relative_path or not target.is_relative_to(root):
        raise ValueError("产物路径越界")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".artifact-", dir=root, delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if not verify_file(root, temporary.name, expected_sha256)["valid"]:
            raise OSError("临时产物核验失败")
        try:
            if os.name == "nt":
                os.rename(temporary, target)
            else:
                os.link(temporary, target)
        except FileExistsError:
            if not verify_file(root, relative_path, expected_sha256)["valid"]:
                raise OSError("不可变产物已存在且内容冲突") from None
        if os.name != "nt":
            descriptor = os.open(root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
