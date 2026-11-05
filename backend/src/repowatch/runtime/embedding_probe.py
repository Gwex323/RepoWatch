import hashlib
import math
import sqlite3
import sys
import time
from pathlib import Path

DOCS = (
    ("doc-1", "登录页面按钮样式在移动端错位，需要调整响应式布局"),
    ("doc-2", "会话令牌刷新失败导致用户被登出"),
    ("doc-3", "会话缓存未清理导致旧权限仍然生效"),
    ("doc-4", "数据库迁移在 Windows 路径下失败，需要处理反斜杠和大小写"),
    ("doc-5", "数据库索引重建耗时过长，需要分批执行"),
    ("doc-6", "前端构建缓存未失效，页面仍旧加载旧样式"),
    ("doc-7", "前端多语言资源未加载，界面显示英文"),
    ("doc-8", "GitHub Issue 分诊需要识别重复问题并关联已有任务"),
    ("doc-9", "GitHub Issue 标签映射缺失导致分诊结果错误"),
    ("doc-10", "GitHub token 权限不足导致只读同步失败"),
    ("doc-11", "create_app initializes the FastAPI application and registers routes"),
    ("doc-12", "parse_manifest validates Git tree entries modes and blob hashes"),
    ("doc-13", "sqlite_vec.load enables the vec0 virtual table for KNN search"),
    ("doc-14", "scan_dependencies prevents claiming a repair before scan completion"),
    ("doc-15", "claim acquires the execution lease and increments the generation"),
    ("doc-16", "LFS pointer must not be treated as the actual binary content"),
    ("doc-17", "docker volume materializes the Linux worktree from a Git bundle"),
    ("doc-18", "FTS5 trigram tokenizer supports Chinese substring queries"),
    ("doc-19", "lost acknowledgement requires remote reconciliation before retry"),
    ("doc-20", "budget ledger accumulates usage across runs"),
    ("doc-21", "independent reviewer reads source and evidence without write access"),
    ("doc-22", "business memory records are the source of truth for retrieval"),
)

QUERIES = (
    ("用户被强制退出", "doc-2", "zh"),
    ("缓存导致旧权限", "doc-3", "zh"),
    ("迁移路径反斜杠", "doc-4", "zh"),
    ("页面样式还是旧的", "doc-6", "zh"),
    ("避免重复 Issue", "doc-8", "zh"),
    ("令牌权限不足", "doc-10", "zh"),
    ("FastAPI 应用工厂", "doc-11", "code"),
    ("Git 树对象校验", "doc-12", "code"),
    ("最近邻向量表", "doc-13", "code"),
    ("修复等待扫描完成", "doc-14", "code"),
    ("执行租约代次", "doc-15", "code"),
    ("LFS 指针不是实体", "doc-16", "code"),
)

CANDIDATES = (
    "BAAI/bge-small-zh-v1.5",
    "jinaai/jina-embeddings-v2-base-zh",
    "jinaai/jina-embeddings-v2-base-code",
)

CANDIDATE_LANGUAGES = {
    "BAAI/bge-small-zh-v1.5": ("zh",),
    "jinaai/jina-embeddings-v2-base-zh": ("zh",),
    "jinaai/jina-embeddings-v2-base-code": ("en", "code"),
}

FTS_QUERIES = (
    "登录",
    "登录超时",
    "用户被强制退出",
    "令牌刷新失败",
    "数据库迁移",
    "create_app",
)


def corpus_digest() -> str:
    payload = "\n".join(f"{doc_id}\t{text}" for doc_id, text in DOCS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fts_probe(connection: sqlite3.Connection) -> dict:
    output = {}
    for tokenizer in ("unicode61", "trigram"):
        table = f"fts_{tokenizer}"
        connection.execute(f"CREATE VIRTUAL TABLE {table} USING fts5(body, tokenize='{tokenizer}')")
        for _, text in DOCS:
            connection.execute(f"INSERT INTO {table}(body) VALUES (?)", (text,))
        results = {}
        for query in FTS_QUERIES:
            try:
                count = connection.execute(
                    f"SELECT count(*) FROM {table} WHERE {table} MATCH ?", (query,)
                ).fetchone()[0]
                results[query] = {"matches": count, "error": None}
            except sqlite3.Error as error:
                results[query] = {"matches": 0, "error": type(error).__name__}
        output[tokenizer] = results
    return output


def like_probe(connection: sqlite3.Connection) -> dict:
    table = "fts_like"
    connection.execute(f"CREATE TABLE {table}(body TEXT)")
    for _, text in DOCS:
        connection.execute(f"INSERT INTO {table}(body) VALUES (?)", (text,))
    return {
        query: connection.execute(
            f"SELECT count(*) FROM {table} WHERE body LIKE ?", (f"%{query}%",)
        ).fetchone()[0]
        for query in FTS_QUERIES
    }


def component_probe() -> dict:
    connection = sqlite3.connect(":memory:")
    connection.enable_load_extension(True)
    try:
        import sqlite_vec

        sqlite_vec.load(connection)
        connection.execute(
            "CREATE VIRTUAL TABLE vec_probe USING vec0(embedding float[3] distance_metric=cosine)"
        )
        connection.execute(
            "INSERT INTO vec_probe(rowid, embedding) VALUES (1, ?)",
            (sqlite_vec.serialize_float32(normalize([0.1, 0.2, 0.3])),),
        )
        vec_ok = connection.execute("SELECT count(*) FROM vec_probe").fetchone()[0] == 1
    except Exception as error:
        vec_ok = False
        vec_error = type(error).__name__
    else:
        vec_error = None
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "fts5": True,
        "fts": fts_probe(connection),
        "like": like_probe(connection),
        "sqlite_vec": vec_ok,
        "sqlite_vec_error": vec_error,
        "sqlite_vec_distance": "cosine",
    }


def normalize(vector) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def cosine(left, right) -> float:
    return sum(a * b for a, b in zip(normalize(left), normalize(right), strict=True))


def rank_documents(doc_vectors, query_vector) -> list[str]:
    scored = [
        (cosine(vector, query_vector), doc_id)
        for (doc_id, _), vector in zip(DOCS, doc_vectors, strict=True)
    ]
    scored.sort(reverse=True)
    return [doc_id for _, doc_id in scored]


def metrics(rankings: list[list[str]]) -> dict:
    def score(subset: list[int]) -> dict:
        recall = 0
        reciprocal = 0.0
        for index in subset:
            expected = QUERIES[index][1]
            ranking = rankings[index]
            if expected in ranking[:3]:
                recall += 1
            reciprocal += 1 / (ranking.index(expected) + 1)
        total = len(subset)
        return {"recall_at_3": recall / total, "mrr": reciprocal / total}

    zh = [index for index, item in enumerate(QUERIES) if item[2] == "zh"]
    code = [index for index, item in enumerate(QUERIES) if item[2] == "code"]
    return {
        "overall": score(list(range(len(QUERIES)))),
        "zh": score(zh),
        "code": score(code),
    }


def vec_probe(dim: int, doc_vectors, query_vector) -> dict:
    connection = sqlite3.connect(":memory:")
    connection.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(connection)
    connection.execute(
        f"CREATE VIRTUAL TABLE vec_docs USING vec0(embedding float[{dim}] distance_metric=cosine)"
    )
    for index, vector in enumerate(doc_vectors, start=1):
        connection.execute(
            "INSERT INTO vec_docs(rowid, embedding) VALUES (?, ?)",
            (index, sqlite_vec.serialize_float32(normalize(vector))),
        )
    rows = connection.execute(
        "SELECT rowid FROM vec_docs WHERE embedding MATCH ? ORDER BY distance LIMIT 3",
        (sqlite_vec.serialize_float32(normalize(query_vector)),),
    ).fetchall()
    return {"top_ids": [DOCS[row[0] - 1][0] for row in rows]}


def evaluate_model(name: str, cache_dir: Path, fastembed_path: Path | None) -> dict:
    if fastembed_path is not None:
        sys.path.insert(0, str(fastembed_path))
    try:
        import psutil
        from fastembed import TextEmbedding
    except ImportError as error:
        return {"model": name, "error": f"dependency_missing:{type(error).__name__}"}
    metadata = next(
        (item for item in TextEmbedding.list_supported_models() if item["model"] == name),
        None,
    )
    if metadata is None:
        return {"model": name, "error": "model_not_supported"}
    process = psutil.Process()
    rss_before = process.memory_info().rss
    started = time.perf_counter()
    try:
        model = TextEmbedding(
            model_name=name,
            cache_dir=str(cache_dir),
            threads=4,
        )
        load_seconds = time.perf_counter() - started
        rss_loaded = process.memory_info().rss
        started = time.perf_counter()
        doc_vectors = list(model.embed([text for _, text in DOCS], batch_size=8))
        query_vectors = list(model.query_embed([query for query, _, _ in QUERIES]))
        encode_seconds = time.perf_counter() - started
    except Exception as error:
        return {
            "model": name,
            "error": f"{type(error).__name__}:{str(error)[:160]}",
            "license": metadata["license"],
            "dim": metadata["dim"],
            "size_in_gb": metadata["size_in_GB"],
        }
    offline_ok = True
    try:
        offline_started = time.perf_counter()
        TextEmbedding(
            model_name=name,
            cache_dir=str(cache_dir),
            threads=4,
            local_files_only=True,
        )
        offline_seconds = time.perf_counter() - offline_started
    except Exception:
        offline_ok = False
        offline_seconds = None
    rankings = [rank_documents(doc_vectors, query_vector) for query_vector in query_vectors]
    return {
        "model": name,
        "error": None,
        "license": metadata["license"],
        "dim": metadata["dim"],
        "size_in_gb": metadata["size_in_GB"],
        "description": metadata["description"],
        "sources": metadata["sources"],
        "model_file": metadata["model_file"],
        "load_seconds": round(load_seconds, 3),
        "offline_load_ok": offline_ok,
        "offline_load_seconds": None if offline_seconds is None else round(offline_seconds, 3),
        "encode_seconds": round(encode_seconds, 3),
        "rss_mb_after_load": round(rss_loaded / (1024 * 1024), 1),
        "rss_mb_delta": round((rss_loaded - rss_before) / (1024 * 1024), 1),
        "metrics": metrics(rankings),
        "declared_languages": list(CANDIDATE_LANGUAGES.get(name, ())),
        "rankings": {
            query: ranking[:3] for (query, _, _), ranking in zip(QUERIES, rankings, strict=True)
        },
        "sqlite_vec_top3": vec_probe(len(doc_vectors[0]), doc_vectors, query_vectors[0]),
    }


def select_model(results: list[dict]) -> dict:
    eligible = []
    for item in results:
        if item.get("error") is not None:
            item["eligible"] = False
            item["rejection"] = f"load_failed:{item['error']}"
            continue
        zh_recall = item["metrics"]["zh"]["recall_at_3"]
        code_recall = item["metrics"]["code"]["recall_at_3"]
        if zh_recall < 0.75 or code_recall < 0.75:
            item["eligible"] = False
            item["rejection"] = f"recall_below_gate:zh={zh_recall:.3f},code={code_recall:.3f}"
            continue
        if not item.get("offline_load_ok"):
            item["eligible"] = False
            item["rejection"] = "offline_load_failed"
            continue
        item["eligible"] = True
        item["rejection"] = None
        eligible.append(item)

    chinese = [item for item in eligible if "zh" in item["declared_languages"]]
    pool = chinese or eligible
    pool.sort(
        key=lambda item: (
            item["metrics"]["overall"]["mrr"],
            item["metrics"]["overall"]["recall_at_3"],
            -item["load_seconds"],
            -item["size_in_gb"],
        ),
        reverse=True,
    )
    selected = pool[0] if pool else None
    ranked = sorted(
        eligible,
        key=lambda item: (
            item["metrics"]["overall"]["mrr"],
            item["metrics"]["overall"]["recall_at_3"],
        ),
        reverse=True,
    )
    alternates = [item["model"] for item in ranked if item is not selected]
    return {
        "model": None if selected is None else selected["model"],
        "alternates": alternates,
        "fallback": "keyword" if selected is None else None,
        "rule": (
            "中文与代码 recall@3 均不低于 0.75、离线加载成功且模型声明支持中文；"
            "在满足条件的候选中按总体 MRR、recall@3、加载时间与体积排序；无候选时降级关键词检索"
        ),
        "corpus_sha256": corpus_digest(),
        "corpus_size": len(DOCS),
        "query_count": len(QUERIES),
    }


def run_experiment(models: tuple[str, ...], cache_dir: Path, fastembed_path: Path | None) -> dict:
    components = component_probe()
    results = [evaluate_model(name, cache_dir, fastembed_path) for name in models]
    return {
        "experiment": "1.9",
        "status": "passed" if components["fts5"] and components["sqlite_vec"] else "failed",
        "components": components,
        "encoding": {
            "normalization": "l2",
            "distance": "cosine",
            "doc_method": "TextEmbedding.embed",
            "query_method": "TextEmbedding.query_embed",
            "prefixes": "none",
            "batch_size": 8,
            "threads": 4,
            "index_metadata": [
                "model",
                "license",
                "dim",
                "normalization",
                "distance",
                "sources.hf",
                "model_file",
                "corpus_sha256",
            ],
        },
        "models": results,
        "selection": select_model(results),
    }
