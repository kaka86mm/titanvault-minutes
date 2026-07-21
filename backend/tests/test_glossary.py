"""规范名词表（glossary）构建与纪要注入的单元测试。

验证：
1. build_hotword_package 输出的 glossary 按 kind 分组、只含被选中的规范名
2. glossary_prompt 正确渲染成给 LLM 的中文提示，且空输入安全降级
3. 持久化/读取闭环：latest_hotword_package 能取回 glossary

注意：DB 测试用独立临时库（monkeypatch backend.app.db.DB_PATH），
不依赖 tmp_home（那个 fixture 只 reload config，不隔离 db），避免被
其他测试的残留数据污染。
"""
import json
import uuid

import pytest

from backend.app.summary import glossary_prompt


# -------- glossary_prompt 渲染（纯函数，无需 DB） --------

def test_glossary_prompt_empty():
    """空词表返回空字符串（调用方据此决定是否注入）。"""
    assert glossary_prompt({}) == ""
    assert glossary_prompt(None) == ""


def test_glossary_prompt_basic_grouping():
    """按 kind 分组渲染，重要类型排在前面。"""
    glossary = {
        "人员": ["张总"],
        "产品": ["TitanVault Minutes", "金蝶接口"],
        "项目": ["智慧园区"],
    }
    text = glossary_prompt(glossary)
    # 产品必须排在人员前面（kind_order 中产品优先级更高）
    assert text.index("产品") < text.index("人员")
    assert "TitanVault Minutes、金蝶接口" in text
    assert "智慧园区" in text
    assert "必须使用规范名" in text


def test_glossary_prompt_dedup_across_kinds():
    """同一个词在多个分类出现只保留一次。"""
    glossary = {
        "产品": ["金蝶"],
        "项目": ["金蝶"],
    }
    text = glossary_prompt(glossary)
    assert text.count("金蝶") == 1


def test_glossary_prompt_custom_kind_fallback():
    """kind_order 之外的分类也能渲染（兜底分支）。"""
    glossary = {"自定义类型": ["某专有词"]}
    text = glossary_prompt(glossary)
    assert "自定义类型：某专有词" in text


def test_glossary_prompt_filters_empty_words():
    """空字符串词被过滤。"""
    glossary = {"产品": ["", "有效词"]}
    text = glossary_prompt(glossary)
    assert "有效词" in text
    assert text.count("有效词") == 1


# -------- build_hotword_package 输出 glossary（需 DB） --------

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """每个测试用独立的空临时库，保证数据隔离。

    db() 在函数体内动态读取 backend.app.db.DB_PATH，monkeypatch 它即可重定向。
    同时 reload config 让其路径常量指向 tmp_path。
    """
    import importlib
    monkeypatch.setenv("RECORDING_AI_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("AHAMVOICE_MODELS_DIR", str(tmp_path / "models"))
    from backend.app import config
    importlib.reload(config)
    # 先 reload db，再 monkeypatch 它的 DB_PATH 到独立文件
    from backend.app import db as db_module
    importlib.reload(db_module)
    db_path = tmp_path / "test.sqlite3"
    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    db_module.ensure_schema()
    yield db_module
    importlib.reload(config)
    importlib.reload(db_module)


def _seed_hotwords(conn):
    """插入 3 条热词（不同 kind），用于构建包。"""
    rows = [
        ("TitanVault Minutes", "产品", "titan", "系统内置", 10),
        ("智慧园区", "项目", "园区项目", "manual", 8),
        ("张总", "人员", "", "manual", 6),
    ]
    for word, kind, aliases, source, weight in rows:
        conn.execute(
            "insert into hotwords(id,word,kind,aliases,source,scope,weight,active,state) values(?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), word, kind, aliases, source, "global", weight, 1, "active"),
        )


def test_build_package_contains_glossary(isolated_db):
    """build_hotword_package 返回的 package 含 glossary 字段，按 kind 分组。"""
    from backend.app.hotwords import build_hotword_package

    rec = {"id": str(uuid.uuid4()), "title": "测试会议", "meeting_type": "客户调研"}
    user = {"id": "u1", "team_id": None}
    with isolated_db.db() as conn:
        _seed_hotwords(conn)
        package = build_hotword_package(conn, rec, user, persist=False)

    assert "glossary" in package
    g = package["glossary"]
    assert isinstance(g, dict)
    assert "TitanVault Minutes" in g.get("产品", [])
    assert "智慧园区" in g.get("项目", [])
    assert "张总" in g.get("人员", [])


def test_build_package_glossary_persist_and_reload(isolated_db):
    """持久化后 latest_hotword_package 能取回 glossary（闭环）。"""
    from backend.app.hotwords import build_hotword_package, latest_hotword_package

    rec_id = str(uuid.uuid4())
    rec = {"id": rec_id, "title": "测试会议", "meeting_type": "客户调研"}
    user = {"id": "u1", "team_id": None}
    with isolated_db.db() as conn:
        _seed_hotwords(conn)
        build_hotword_package(conn, rec, user, persist=True)
        conn.commit()

    with isolated_db.db() as conn:
        loaded = latest_hotword_package(conn, rec_id)

    assert loaded is not None
    assert "glossary" in loaded
    assert "TitanVault Minutes" in loaded["glossary"].get("产品", [])


def test_glossary_excludes_discarded_words(isolated_db):
    """glossary 不包含 state=discarded 的词。"""
    from backend.app.hotwords import build_hotword_package

    rec = {"id": str(uuid.uuid4()), "title": "测试", "meeting_type": "内部会议"}
    user = {"id": "u1", "team_id": None}
    with isolated_db.db() as conn:
        _seed_hotwords(conn)
        conn.execute(
            "insert into hotwords(id,word,kind,source,scope,weight,active,state) values(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "废弃词", "产品", "manual", "global", 5, 1, "discarded"),
        )
        package = build_hotword_package(conn, rec, user, persist=False)

    g = package["glossary"]
    assert "废弃词" not in g.get("产品", [])
    assert "TitanVault Minutes" in g.get("产品", [])

