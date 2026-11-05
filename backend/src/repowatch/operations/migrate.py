"""显式停写迁移入口；正常 API／Worker 启动不得调用。"""

import argparse
import sqlite3
from importlib.metadata import version
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from langgraph.checkpoint.sqlite import SqliteSaver

from repowatch.adapters.storage.checkpoints import CHECKPOINT_PACKAGE, _validate
from repowatch.adapters.storage.sqlite import SCHEMA_REVISION, sqlite_engine
from repowatch.application.storage import SchemaMismatch
from repowatch.configuration import StorageSettings


def migration_directory() -> Path:
    packaged = Path(__file__).resolve().parents[1] / "_migrations"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[3] / "migrations"


def migrate(settings: StorageSettings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if settings.checkpoint_database.exists():
        with sqlite3.connect(settings.checkpoint_database) as connection:
            _validate(connection)
    configuration = Config()
    configuration.set_main_option("script_location", str(migration_directory()).replace("%", "%%"))
    if ScriptDirectory.from_config(configuration).get_current_head() != SCHEMA_REVISION:
        raise SchemaMismatch("迁移脚本与应用要求的 schema 不一致")
    engine = sqlite_engine(settings, create=True)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            connection.exec_driver_sql("BEGIN EXCLUSIVE")
            try:
                configuration.attributes["connection"] = connection
                command.upgrade(configuration, "head")
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                    raise SchemaMismatch("迁移后外键核对失败，保留原数据")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    finally:
        engine.dispose()

    with sqlite3.connect(settings.checkpoint_database) as connection:
        connection.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms}")
        # The SDK owns its own schema; initialize only at this explicit operation.
        SqliteSaver(connection).setup()
        connection.execute(
            "CREATE TABLE IF NOT EXISTS repowatch_schema ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1), package_version TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO repowatch_schema VALUES(1,?)", (version(CHECKPOINT_PACKAGE),)
        )
        connection.commit()
        _validate(connection)
    settings.artifacts_dir.mkdir(exist_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="RepoWatch 停写迁移；请先停止 API／Worker")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    settings = (
        StorageSettings(args.data_dir) if args.data_dir else StorageSettings.from_environment()
    )
    migrate(settings)
    print("业务库和独立检查点存储已初始化；保留原暂停状态，未启动任务。")


if __name__ == "__main__":
    main()
