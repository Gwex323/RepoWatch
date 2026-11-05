# 持久化、Worker 与外部写入事实核查

调查日期：2026-11-02。官方文档只读调查，未对本机负载、供应商或产品恢复流程进行实测。本文提供选型证据，Q25 后已确认 SQLite WAL 默认及 PostgreSQL 适配边界，当前决定见[设计基线](../design-baseline.md)，恢复实施细则见[实施契约](../implementation-contracts.md)。

## SQLite WAL

- WAL 支持读事务和写事务并行，同一个数据库文件同时仍只有一个写事务；常规 WAL 要求数据库访问进程处于同一主机。
- 长读事务可能阻碍 WAL checkpoint 并导致文件增长，WAL 仍可能出现锁等待。SQLite 的 WAL checkpoint 与 LangGraph 工作流 checkpoint 是不同概念。
- 本机 API 与 Worker 共享数据库文件在模型上可行，但 API、扫描、任务进度和索引都可能产生写操作，需要短事务和锁等待策略。
- SQLite 写锁不代替全局任务执行权，不应把整个长任务放在一个数据库事务里。

来源：[WAL](https://sqlite.org/wal.html)、[适用场景](https://sqlite.org/whentouse.html)、[事务隔离](https://sqlite.org/isolation.html)。

## LangGraph 检查点与恢复

- checkpointer 保存线程和图执行状态；跨线程长期记忆与仓库业务对象不是其自动提供的数据模型。
- 官方将 SQLite saver 定位于本地／实验性工作流，将 Postgres saver 定位于生产使用；选择 SQLite 作为本地产品默认后端仍须对目标负载与崩溃恢复进行实测。
- 同步持久化模式可以在进入下一步前写入检查点；其他模式的持久性边界不同。持久化存储不是仅换一个类名即可得到全部耐久保证。
- 人工 interrupt 恢复需要持久化 checkpointer、相同 `thread_id` 及恢复输入；恢复可能重执行节点内中断前的代码。
- 检查点不会自动保存任意子进程、Git 工作目录和模块内部进度。业务状态与 checkpoint 经不同连接保存时，也不能默认视为一次原子提交。

来源：[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers#durability-modes)、[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[SQLite saver 源码](https://github.com/langchain-ai/langgraph/blob/ee3c043bddcc514012885167dd5916b15d6e4731/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py)、[异步 SQLite saver](https://github.com/langchain-ai/langgraph/blob/ee3c043bddcc514012885167dd5916b15d6e4731/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/aio.py)、[Postgres saver](https://github.com/langchain-ai/langgraph/blob/ee3c043bddcc514012885167dd5916b15d6e4731/libs/checkpoint-postgres/langgraph/checkpoint/postgres/__init__.py)。

## FastAPI 进程内后台任务

- FastAPI／Starlette `BackgroundTasks` 是响应后执行的进程内任务机制，不自动提供持久队列、进程退出恢复或独立 Worker 隔离。
- 需要持久接单、可恢复长任务与独立资源生命周期时，API 与 Worker 的职责应由应用明确分开；FastAPI 并不强制使用某个消息中间件。

来源：[FastAPI BackgroundTasks](https://fastapi.tiangolo.com/tutorial/background-tasks/#caveat)、[Starlette 文档](https://starlette.dev/background/)、[实现](https://github.com/Kludex/starlette/blob/main/starlette/background.py)。

## GitHub 写入与幂等

远程 PR 已创建但本地尚未保存结果时，连接或进程可能失败。检查点重放、本地 outbox 或换用 Postgres 均不能单独消除这个跨系统故障窗口。

应用需要稳定操作标识、保存意图与结果、查询远端核对具体写入是否发生，结果不确定时保留待核对状态。不能声称存在普适的远程“恰好一次”保证。

来源：[GitHub 创建 PR](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request)、[按 head／base 查询 PR](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests)、[查询 PR](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request)、[LangGraph 中断与副作用](https://docs.langchain.com/oss/python/langgraph/interrupts)。
