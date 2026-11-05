"""业务数据端口。用例不依赖 SQLAlchemy、SQLite 或 LangGraph。"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol


class StorageError(RuntimeError):
    """持久化不可用；调用方必须停止执行，不能退回内存继续。"""


class SchemaMismatch(StorageError):
    pass


class ConflictError(ValueError):
    pass


class RecordPort(Protocol):
    """一个业务事务内的记录集合；字段与约束由各业务用例定义。"""

    def one(self, collection: str, **identity: Any) -> Mapping[str, Any] | None: ...

    def find(self, collection: str, **identity: Any) -> Sequence[Mapping[str, Any]]: ...

    def add(self, collection: str, values: Mapping[str, Any]) -> None: ...

    def change(
        self, collection: str, identity: Mapping[str, Any], values: Mapping[str, Any]
    ) -> int: ...

    def remove(self, collection: str, **identity: Any) -> int: ...

    def events(
        self, *, after: int, limit: int, task_id: str | None = None
    ) -> Sequence[Mapping]: ...


class BusinessStore(Protocol):
    def assert_execution_available(self) -> None: ...

    def stop_execution(self) -> None: ...

    def read(self) -> AbstractContextManager[RecordPort]: ...

    def write(self) -> AbstractContextManager[RecordPort]: ...

    def close(self) -> None: ...
