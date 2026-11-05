---
status: accepted
---

# 本地单用户版默认采用 SQLite WAL

用户选择 SQLite WAL 作为最终本地单用户版的默认持久化后端，保留 PostgreSQL 适配边界，优先控制本地部署成本。该选择接受单写与锁等待约束，须以短事务管理 API／Worker 交接，并通过目标扫描负载、双进程竞争和崩溃恢复验收；不以 LangGraph 存在 SQLite saver 作为已获生产负载保证的依据。
