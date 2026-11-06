"""正式业务存储；P0 的 prototype 不参与此端口实现。"""

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from sqlalchemy import MetaData, create_engine, event, select
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from repowatch.application.storage import ConflictError, RecordPort, SchemaMismatch, StorageError
from repowatch.configuration import StorageSettings

SCHEMA_REVISION = "0021_distillation_state"


def sqlite_engine(settings: StorageSettings, *, create: bool = False):
    path = settings.business_database
    if create:
        url = URL.create("sqlite", database=str(path))
    else:
        if not path.is_file():
            raise SchemaMismatch("业务数据库未初始化，请在服务停写后执行迁移")
        # mode=rw prevents recreating a removed database between validation and connect.
        url = URL.create("sqlite", database=path.as_uri(), query={"mode": "rw", "uri": "true"})
    engine = create_engine(url, poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def configure(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms}")
            cursor.execute("PRAGMA synchronous=FULL")
        finally:
            cursor.close()

    return engine


class SqlRecords:
    def __init__(self, connection: Connection, metadata: MetaData, *, writable: bool):
        self.connection = connection
        self.metadata = metadata
        self.writable = writable

    def _table(self, collection: str):
        if collection == "alembic_version" or collection not in self.metadata.tables:
            raise ValueError("未知业务集合")
        return self.metadata.tables[collection]

    @staticmethod
    def _where(table, identity):
        if not identity:
            raise ValueError("写操作必须限定记录")
        return [table.c[key] == value for key, value in identity.items()]

    def one(self, collection: str, **identity: Any) -> Mapping[str, Any] | None:
        table = self._table(collection)
        return (
            self.connection.execute(select(table).where(*self._where(table, identity)))
            .mappings()
            .one_or_none()
        )

    def find(self, collection: str, **identity: Any) -> list[Mapping[str, Any]]:
        table = self._table(collection)
        return list(
            self.connection.execute(
                select(table)
                .where(*(table.c[key] == value for key, value in identity.items()))
                .order_by(*table.primary_key.columns)
            ).mappings()
        )

    def _require_write(self):
        if not self.writable:
            raise PermissionError("只读事务不能修改业务记录")

    def add(self, collection: str, values: Mapping[str, Any]) -> None:
        self._require_write()
        self.connection.execute(self._table(collection).insert().values(**values))

    def change(
        self, collection: str, identity: Mapping[str, Any], values: Mapping[str, Any]
    ) -> int:
        self._require_write()
        table = self._table(collection)
        return self.connection.execute(
            table.update().where(*self._where(table, identity)).values(**values)
        ).rowcount

    def remove(self, collection: str, **identity: Any) -> int:
        self._require_write()
        table = self._table(collection)
        return self.connection.execute(table.delete().where(*self._where(table, identity))).rowcount

    def events(self, *, after, limit, task_id=None):
        table = self._table("business_events")
        query = select(table).where(table.c.sequence > after)
        if task_id is not None:
            query = query.where(table.c.task_id == task_id)
        return list(
            self.connection.execute(query.order_by(table.c.sequence).limit(limit)).mappings()
        )


class SQLiteBusinessStore:
    def __init__(self, settings: StorageSettings):
        self._execution_stopped = False
        self.settings = settings
        self.engine = sqlite_engine(settings)
        self.metadata = MetaData()
        try:
            with self.engine.connect() as connection:
                version = connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).all()
                if version != [(SCHEMA_REVISION,)]:
                    raise SchemaMismatch("业务 schema 不兼容，请停写后显式迁移")
                if connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() != "wal":
                    raise SchemaMismatch("业务数据库未配置 WAL")
                self.metadata.reflect(connection)
        except SQLAlchemyError as error:
            self.engine.dispose()
            raise SchemaMismatch("业务数据库不可读取或 schema 未初始化") from error
        except BaseException:
            self.engine.dispose()
            raise

    @contextmanager
    def _scope(self, *, writable: bool) -> Iterator[RecordPort]:
        try:
            with self.engine.connect() as connection:
                if writable:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    connection.exec_driver_sql("PRAGMA query_only=ON")
                    connection.exec_driver_sql("BEGIN")
                try:
                    yield SqlRecords(connection, self.metadata, writable=writable)
                    if writable:
                        connection.commit()
                finally:
                    if connection.in_transaction():
                        connection.rollback()
        except IntegrityError as error:
            raise ConflictError("记录唯一性或业务关联约束冲突") from error
        except SQLAlchemyError as error:
            self.stop_execution()
            raise StorageError("业务持久化失败，必须停止新执行和发布") from error

    def assert_execution_available(self):
        if self._execution_stopped:
            raise StorageError("本进程持久化曾失败，停止新执行；修复后重启并对账")

    def stop_execution(self):
        self._execution_stopped = True

    def read(self) -> AbstractContextManager[RecordPort]:
        return self._scope(writable=False)

    def write(self) -> AbstractContextManager[RecordPort]:
        return self._scope(writable=True)

    def close(self) -> None:
        self.engine.dispose()
