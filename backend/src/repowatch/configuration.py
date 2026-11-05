"""API 与 Worker 共用的宿主侧数据位置；读取配置不创建目录或数据库。"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StorageSettings:
    data_dir: Path
    busy_timeout_ms: int = 5000

    def __post_init__(self) -> None:
        root = Path(self.data_dir).expanduser()
        if not root.is_absolute():
            raise ValueError("数据目录必须为绝对路径，避免 API 与 Worker 使用不同目录")
        if str(root).startswith(("\\\\", "//")):
            raise ValueError("SQLite WAL 数据必须位于宿主本地文件系统")
        if not 0 < self.busy_timeout_ms <= 60_000:
            raise ValueError("锁等待必须在 1—60000 毫秒内")
        object.__setattr__(self, "data_dir", root.resolve())
        paths = [self.business_database, self.checkpoint_database, self.index_database]
        if len({path.resolve() for path in paths}) != len(paths):
            raise ValueError("业务库、检查点和索引库必须分离")
        if any(
            a.exists() and b.exists() and a.samefile(b)
            for i, a in enumerate(paths)
            for b in paths[i + 1 :]
        ):
            raise ValueError("数据库文件不能使用同一文件的硬链接")

    @property
    def business_database(self) -> Path:
        return self.data_dir / "business.sqlite3"

    @property
    def checkpoint_database(self) -> Path:
        return self.data_dir / "checkpoints.sqlite3"

    @property
    def index_database(self) -> Path:
        return self.data_dir / "memory-index.sqlite3"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @classmethod
    def from_environment(cls) -> "StorageSettings":
        configured = os.environ.get("REPOWATCH_DATA_DIR")
        if configured:
            return cls(Path(configured))
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            return cls(base / "RepoWatch")
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return cls(base / "repowatch")
