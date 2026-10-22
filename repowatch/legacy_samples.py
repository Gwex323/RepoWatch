"""One-way retirement of the discontinued demo schema; never executes demo jobs."""

import re


def retire_samples(db):
    tables = {row["name"] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'",
    )}
    if "runs" not in tables and not db.execute(
        "SELECT 1 FROM repositories WHERE kind='fixture' LIMIT 1",
    ).fetchone():
        return
    # Rebuild the old memory FK without deleting personal notes or their history.
    # PRAGMA foreign_keys must change outside a transaction; all DDL below is atomic.
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        if "memory_reads" in tables:
            db.execute("DROP TABLE memory_reads")
        if "memories" in tables:
            obsolete = ("SELECT id FROM memories WHERE source='workflow' AND repo_id IN "
                        "(SELECT id FROM repositories WHERE kind='fixture')")
            if "memory_history" in tables:
                db.execute(f"DELETE FROM memory_history WHERE memory_id IN ({obsolete})")
            db.execute(f"DELETE FROM memories WHERE id IN ({obsolete})")
            schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories'",
            ).fetchone()["sql"]
            if re.search(r"REFERENCES\s+runs\s*\(", schema, re.I):
                schema = re.sub(r"REFERENCES\s+runs\s*\(\s*id\s*\)", "", schema,
                                flags=re.I)
                schema = re.sub(r'(?i)^CREATE TABLE(?: IF NOT EXISTS)?\s+"?memories"?',
                                "CREATE TABLE memories_retired", schema, count=1)
                db.execute(schema)
                columns = ",".join('"' + row["name"].replace('"', '""') + '"'
                                   for row in db.execute("PRAGMA table_info(memories)"))
                db.execute(f"INSERT INTO memories_retired ({columns}) "
                           f"SELECT {columns} FROM memories")
                db.execute("DROP TABLE memories")
                db.execute("ALTER TABLE memories_retired RENAME TO memories")
        for table in ("artifacts", "events", "jobs", "runs"):
            if table in tables:
                db.execute(f"DROP TABLE {table}")
        # Referenced repositories are retained as archives so personal notes and any
        # real records stay intact. Archives are excluded from all execution lists.
        for repo in db.execute("SELECT id FROM repositories WHERE kind='fixture'").fetchall():
            referenced = False
            for table in tables - {"artifacts", "events", "jobs", "runs", "memory_reads"}:
                name = '"' + table.replace('"', '""') + '"'
                for foreign in db.execute(f"PRAGMA foreign_key_list({name})"):
                    if foreign["table"] != "repositories":
                        continue
                    column = '"' + foreign["from"].replace('"', '""') + '"'
                    if db.execute(f"SELECT 1 FROM {name} WHERE {column}=? LIMIT 1",
                                  (repo["id"],)).fetchone():
                        referenced = True
            if referenced:
                db.execute("UPDATE repositories SET kind='archived',paused=1 WHERE id=?",
                           (repo["id"],))
            else:
                db.execute("DELETE FROM repositories WHERE id=?", (repo["id"],))
        db.execute("DELETE FROM settings WHERE key IN ('samples_enabled','memory_backfill_v1')")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Legacy retirement would violate database references.")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")
