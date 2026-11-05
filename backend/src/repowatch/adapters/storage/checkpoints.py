"""独立图数据库。运行时只检查已迁移 schema，不调用隐式建表。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import version

from langgraph.checkpoint.sqlite import SqliteSaver

from repowatch.application.storage import SchemaMismatch
from repowatch.configuration import StorageSettings

CHECKPOINT_PACKAGE = "langgraph-checkpoint-sqlite"


def _validate(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute("SELECT package_version FROM repowatch_schema").fetchall()
        if row != [(version(CHECKPOINT_PACKAGE),)]:
            raise SchemaMismatch("检查点存储版本不兼容，请停写后显式迁移")
        for table, required in (
            ("checkpoints", {"thread_id", "checkpoint_ns", "checkpoint_id", "checkpoint"}),
            ("writes", {"thread_id", "checkpoint_ns", "checkpoint_id", "task_path", "value"}),
        ):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not required <= columns:
                raise SchemaMismatch("检查点 schema 不完整")
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            raise SchemaMismatch("检查点数据库未配置 WAL")
    except sqlite3.Error as error:
        raise SchemaMismatch("检查点数据库不可读取或尚未初始化") from error


class PreparedSqliteSaver(SqliteSaver):
    def setup(self) -> None:
        if not self.is_setup:
            _validate(self.conn)
            self.is_setup = True


@contextmanager
def checkpoint_store(settings: StorageSettings) -> Iterator[SqliteSaver]:
    if not settings.checkpoint_database.is_file():
        raise SchemaMismatch("检查点数据库未初始化，请在服务停写后执行迁移")
    uri = settings.checkpoint_database.as_uri() + "?mode=rw"
    connection = sqlite3.connect(
        uri, uri=True, check_same_thread=False, timeout=settings.busy_timeout_ms / 1000
    )
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous=FULL")
        saver = PreparedSqliteSaver(connection)
        saver.setup()
        yield saver
    finally:
        connection.close()
