from alembic import context
from sqlalchemy import create_engine, pool

configuration = context.config


def run_migrations() -> None:
    if context.is_offline_mode():
        context.configure(url=configuration.get_main_option("sqlalchemy.url"), literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    supplied_connection = configuration.attributes.get("connection")
    if supplied_connection is not None:
        context.configure(connection=supplied_connection, render_as_batch=True)
        context.run_migrations()
        return

    engine = create_engine(configuration.get_main_option("sqlalchemy.url"), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA busy_timeout=5000")
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        connection.commit()
        connection.exec_driver_sql("BEGIN EXCLUSIVE")
        try:
            context.configure(connection=connection, render_as_batch=True)
            context.run_migrations()
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                raise RuntimeError("迁移后外键核对失败")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    engine.dispose()


run_migrations()
