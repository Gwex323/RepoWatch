# 本地记忆检索组件事实

调查日期：2026-11-02。仅查官方文档与源码，未安装依赖、下载模型或实测 Windows。本文提供 Q32 的选型证据；混合检索路线后来已获确认，具体决定及失效边界见[接入设计](../integration-discussion.md)与[实施契约](../implementation-contracts.md)，不表示已经实现。

## 关键词与向量组件

- SQLite FTS5 提供关键词匹配、BM25 和索引重建能力；实际 Python 链接的 SQLite 需要具备 FTS5。默认分词不能被当作完整中文分词或代码标识符召回保证；[官方文档](https://www.sqlite.org/fts5.html)。
- sqlite-vec 0.1.9 发布了 Windows x86-64 Python wheel；连接需支持动态扩展加载，二进制与运行环境需匹配。存在发行包不等于本产品在所有 Windows 环境均通过验收；[发行文件](https://pypi.org/project/sqlite-vec/0.1.9/#files)、[Python 接入](https://alexgarcia.xyz/sqlite-vec/python.html)、[Python 加载扩展条件](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.enable_load_extension)。
- sqlite-vec 支持近邻检索与部分过滤，但过滤、候选合并和融合排序仍需设计，不能假定任意 SQL 过滤都能在近邻查询内部执行；[KNN](https://alexgarcia.xyz/sqlite-vec/features/knn.html)、[vec0](https://alexgarcia.xyz/sqlite-vec/features/vec0.html)。

## LangGraph Store

- 调查版本 `langgraph-checkpoint-sqlite 3.1.3` 已有 `SqliteStore`／`AsyncSqliteStore`，提供 namespace、持久键值和配置后的向量搜索。它不自动把现有业务表转换为索引源；[发行](https://github.com/langchain-ai/langgraph/releases/tag/checkpointsqlite==3.1.3)、[固定源码](https://github.com/langchain-ai/langgraph/blob/5bd231cbfd913e05ebc662f519eb9a655bc766df/libs/checkpoint-sqlite/langgraph/store/sqlite/base.py)。
- namespace 支持前缀查询，不自动承担仓库授权和跨仓去敏。语义检索须配置 embedding 和支持它的后端；[Store 文档](https://docs.langchain.com/oss/python/langgraph/stores)。
- 调查版本的查询编码路径、字段配置须与所选 embedding 规则核对，不应假定所有模型的 query／document 编码等价。

## 本地 embedding

- FastEmbed 支持 CPU ONNX 路径及本地缓存／离线加载，不要求另起 HTTP 服务；模型首次使用可能下载，缓存需使用稳定路径或持久卷；[CPU 实现](https://github.com/qdrant/fastembed/blob/v0.9.1/fastembed/common/onnx_model.py)、[下载与离线逻辑](https://github.com/qdrant/fastembed/blob/v0.9.1/fastembed/common/model_management.py)。
- SentenceTransformers 支持 CPU、本地模型路径、缓存及离线加载；保存模型本身不等于检索向量已持久化；[官方 API](https://sbert.net/docs/package_reference/sentence_transformer/model.html)。
- 使用本地 embedding 仍需选择模型、许可证、版本、维度和编码规则；只有选择远程 embedding 时才额外需要服务配置和凭据。本次未确定最终模型。

## 应用层仍需承担

业务记录作为事实源，FTS／向量作为可重建派生索引的路线技术可行。内容版本、证据引用、切块、增量同步、删除传播、模型更新重建、融合排序和权限过滤均需集成；Deep Agents 的 Store backend 不自动交付完整的混合检索系统。

待实验：中文与代码召回、过滤后 top-k、CPU 时延与内存、离线重启、删除一致性、索引重建、WAL 竞争，以及 Windows 原生与 Linux 容器各自的依赖兼容性。
