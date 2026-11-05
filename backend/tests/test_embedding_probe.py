from repowatch.runtime.embedding_probe import (
    CANDIDATES,
    QUERIES,
    component_probe,
    metrics,
    select_model,
)


def test_fts_and_vec_components_load():
    result = component_probe()
    assert result["fts5"] is True
    assert result["sqlite_vec"] is True
    assert result["sqlite_vec_distance"] == "cosine"
    assert result["fts"]["unicode61"]["create_app"]["matches"] == 1
    assert result["fts"]["trigram"]["create_app"]["matches"] == 1
    assert result["fts"]["trigram"]["令牌刷新失败"]["matches"] == 1
    assert result["fts"]["unicode61"]["令牌刷新失败"]["matches"] == 0
    assert result["fts"]["trigram"]["登录超时"]["matches"] == 0
    assert result["fts"]["trigram"]["登录"]["matches"] == 0
    assert result["like"]["登录"] == 1
    assert result["like"]["令牌刷新失败"] == 1


def test_metrics_report_overall_and_subsets():
    rankings = [
        [QUERIES[index][1]]
        + [item[1] for position, item in enumerate(QUERIES) if position != index]
        for index in range(len(QUERIES))
    ]
    result = metrics(rankings)
    assert result["overall"] == {"recall_at_3": 1.0, "mrr": 1.0}
    assert result["zh"] == {"recall_at_3": 1.0, "mrr": 1.0}
    assert result["code"] == {"recall_at_3": 1.0, "mrr": 1.0}


def test_candidates_cover_chinese_and_code():
    assert any("zh" in model for model in CANDIDATES)
    assert any("code" in model for model in CANDIDATES)


def candidate(model, languages, mrr, zh_recall, code_recall):
    return {
        "model": model,
        "error": None,
        "offline_load_ok": True,
        "declared_languages": languages,
        "load_seconds": 1.0,
        "size_in_gb": 0.6,
        "metrics": {
            "overall": {"mrr": mrr, "recall_at_3": 1.0},
            "zh": {"recall_at_3": zh_recall},
            "code": {"recall_at_3": code_recall},
        },
    }


def test_selection_prefers_declared_chinese_support():
    results = [
        candidate("code-model", ["en", "code"], 0.9, 0.9, 1.0),
        candidate("zh-model", ["zh"], 0.85, 0.9, 0.9),
        candidate("weak-zh", ["zh"], 0.8, 0.5, 1.0),
    ]
    selection = select_model(results)
    assert selection["model"] == "zh-model"
    assert selection["alternates"] == ["code-model"]
    assert selection["fallback"] is None
    assert results[2]["eligible"] is False
    assert results[2]["rejection"].startswith("recall_below_gate")


def test_selection_falls_back_to_keyword_without_eligible_model():
    results = [candidate("weak-zh", ["zh"], 0.8, 1.0, 0.5)]
    selection = select_model(results)
    assert selection["model"] is None
    assert selection["fallback"] == "keyword"
