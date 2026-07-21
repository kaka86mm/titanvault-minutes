"""hotword_discover 的单元测试。"""
from backend.app.hotword_discover import _parse_llm_json


def test_parse_clean_json():
    """标准 JSON 对象。"""
    content = '{"terms": [{"word": "金蝶", "kind": "产品", "confidence": 0.9, "example": "金蝶接口报错", "is_uncertain": false}]}'
    terms = _parse_llm_json(content)
    assert len(terms) == 1
    assert terms[0]["word"] == "金蝶"
    assert terms[0]["kind"] == "产品"


def test_parse_markdown_wrapped_json():
    """markdown 代码块包裹的 JSON。"""
    content = '```json\n{"terms": [{"word": "ERP", "kind": "行业", "confidence": 0.85, "example": "ERP系统", "is_uncertain": false}]}\n```'
    terms = _parse_llm_json(content)
    assert len(terms) == 1
    assert terms[0]["word"] == "ERP"


def test_parse_empty_response():
    """空响应返回空列表。"""
    assert _parse_llm_json("") == []
    assert _parse_llm_json("无术语") == []


def test_parse_json_with_extra_text():
    """JSON 前后有解释文本。"""
    content = '好的，以下是抽取的术语：\n{"terms": [{"word": "MES", "kind": "行业", "confidence": 0.8, "example": "MES升级", "is_uncertain": false}]}\n希望有帮助'
    terms = _parse_llm_json(content)
    assert len(terms) == 1
    assert terms[0]["word"] == "MES"


def test_parse_missing_terms_key():
    """JSON 没有 terms 键（LLM 没按格式）返回空。"""
    content = '{"result": []}'
    assert _parse_llm_json(content) == []


# -------- _dedupe_against_db --------

def _ensure_schema():
    from backend.app.db import ensure_schema
    ensure_schema()


def test_dedupe_skips_existing_active(tmp_home):
    _ensure_schema()
    """已在库的 active 热词跳过。"""
    from backend.app.hotword_discover import _dedupe_against_db
    from backend.app.db import db
    import uuid
    with db() as conn:
        conn.execute(
            "insert into hotwords(id,word,kind,source,scope,weight,active,state) values(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "金蝶", "产品", "manual", "global", 8, 1, "active"),
        )
    candidates = [{"word": "金蝶", "kind": "产品"}, {"word": "新词", "kind": "术语"}]
    result = _dedupe_against_db(candidates, "rec-1")
    assert len(result) == 1
    assert result[0]["word"] == "新词"


def test_dedupe_skips_discarded(tmp_home):
    _ensure_schema()
    """用户已丢弃的词不再推。"""
    from backend.app.hotword_discover import _dedupe_against_db
    from backend.app.db import db
    import uuid
    with db() as conn:
        conn.execute(
            "insert into hotwords(id,word,kind,source,scope,weight,active,state) values(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "废弃词", "术语", "llm-discover", "global", 5, 0, "discarded"),
        )
    candidates = [{"word": "废弃词", "kind": "术语"}, {"word": "好词", "kind": "产品"}]
    result = _dedupe_against_db(candidates, "rec-1")
    assert len(result) == 1
    assert result[0]["word"] == "好词"


def test_dedupe_same_batch_duplicates(tmp_home):
    _ensure_schema()
    """同批候选内的重复词去重（大小写）。"""
    from backend.app.hotword_discover import _dedupe_against_db
    candidates = [
        {"word": "自研系统", "kind": "行业"},
        {"word": "自研系统", "kind": "行业"},
        {"word": "独有术语", "kind": "行业"},
    ]
    result = _dedupe_against_db(candidates, "rec-1")
    assert len(result) == 2


def test_dedupe_skips_candidate_state(tmp_home):
    _ensure_schema()
    """已是 candidate 状态的词跳过。"""
    from backend.app.hotword_discover import _dedupe_against_db
    from backend.app.db import db
    import uuid
    with db() as conn:
        conn.execute(
            "insert into hotwords(id,word,kind,source,scope,weight,active,state) values(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "待审词", "术语", "llm-discover", "global", 5, 1, "candidate"),
        )
    candidates = [{"word": "待审词", "kind": "术语"}, {"word": "新词", "kind": "产品"}]
    result = _dedupe_against_db(candidates, "rec-1")
    assert len(result) == 1
    assert result[0]["word"] == "新词"
