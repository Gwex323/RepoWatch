"""Overview of persisted real repository findings, work and activity."""

import json


def snapshot(db):
    repositories = [dict(row) for row in db.execute(
        "SELECT * FROM repositories WHERE kind='github' ORDER BY created",
    )]
    findings = []
    for row in db.execute(
        "SELECT f.id,f.status,f.data,a.id AS analysis_id,a.env_id,a.repo_id,a.revision,"
        "a.created,a.updated,r.name AS repository,t.status AS repair_status "
        "FROM analysis_findings f JOIN repository_analyses a ON a.id=f.analysis_id "
        "JOIN repositories r ON r.id=a.repo_id "
        "LEFT JOIN repair_tasks t ON t.finding_id=f.id WHERE r.kind='github' "
        "ORDER BY a.created DESC,f.id LIMIT 200",
    ):
        item = dict(row)
        data = json.loads(item.pop("data"))
        item.update(title=data["title"], severity=data["severity"])
        findings.append(item)
    # Counts cover the complete database, independently of the bounded lists.
    waiting = db.execute(
        "SELECT count(*) FROM analysis_findings f JOIN repository_analyses a "
        "ON a.id=f.analysis_id JOIN repositories r ON r.id=a.repo_id "
        "WHERE r.kind='github' AND f.status='waiting_reproduction'",
    ).fetchone()[0]
    active = 0
    for table, states in (
        ("repository_analyses", "'queued','waiting_model','running'"),
        ("repair_tasks", "'queued','running'"),
    ):
        active += db.execute(
            f"SELECT count(*) FROM {table} t JOIN repositories r ON r.id=t.repo_id "
            f"WHERE r.kind='github' AND t.status IN ({states})",
        ).fetchone()[0]
    active += db.execute(
        "SELECT count(*) FROM execution_jobs j JOIN environments e ON e.id=j.env_id "
        "JOIN repositories r ON r.id=e.repo_id WHERE r.kind='github' "
        "AND j.state IN ('queued','running')",
    ).fetchone()[0]
    published = db.execute(
        "SELECT count(*) FROM repair_tasks t JOIN repositories r ON r.id=t.repo_id "
        "WHERE r.kind='github' AND t.status='pr_created'",
    ).fetchone()[0]
    events = [dict(row) for row in db.execute(
        "SELECT * FROM ("
        "SELECT 'analysis:' || e.id AS id,a.repo_id,r.name AS repository,"
        "e.analysis_id AS task_id,a.env_id,'analysis' AS source,e.kind,"
        "substr(e.agent,1,500) AS detail,e.created "
        "FROM analysis_events e JOIN repository_analyses a ON a.id=e.analysis_id "
        "JOIN repositories r ON r.id=a.repo_id WHERE r.kind='github' UNION ALL "
        "SELECT 'repair:' || e.id,t.repo_id,r.name,e.repair_id,t.env_id,'repair',e.kind,"
        "substr(e.value,1,500),e.created FROM repair_events e "
        "JOIN repair_tasks t ON t.id=e.repair_id "
        "JOIN repositories r ON r.id=t.repo_id WHERE r.kind='github' UNION ALL "
        "SELECT 'execution:' || j.id,e.repo_id,r.name,j.id,e.id,'execution',j.state,"
        "j.action,j.updated FROM execution_jobs j JOIN environments e ON e.id=j.env_id "
        "JOIN repositories r ON r.id=e.repo_id WHERE r.kind='github'"
        ") ORDER BY created DESC,id DESC LIMIT 100",
    )]
    return {"repositories": repositories, "findings": findings, "events": events,
            "counts": {"waiting": waiting, "active": active, "published_prs": published}}
