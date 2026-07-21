"""config.py 的单元测试。

聚焦运行时函数行为（deepseek 配置优先级、save/load、env_int clamping）。
不测 import 时求值的路径常量（BASE/DB_PATH 等）——那些需要 reload 机制，
且本质是 env 直读，测试价值低。
"""
from backend.app import config


def test_deepseek_config_env_wins_over_file(tmp_home, monkeypatch):
    """env 变量优先于 config.json 文件。"""
    # 先写 config.json
    config.save_user_config({"deepseek_api_key": "from-file"})
    # env 覆盖
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    key, _base, _model = config.get_deepseek_config()
    assert key == "from-env"


def test_deepseek_config_defaults(tmp_home, monkeypatch):
    """无 env 无文件时返回默认 base/model。"""
    # 清掉可能的环境变量（CI 或本机可能设了）
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_BASE", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    key, base, model = config.get_deepseek_config()
    assert key == ""
    assert base == "https://api.deepseek.com"
    assert model == "deepseek-chat"


def test_save_user_config_roundtrip(tmp_home, monkeypatch):
    """save_user_config 写入后 get_deepseek_config 能读回。"""
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    result = config.save_user_config({"deepseek_model": "new-model"})
    assert result["deepseek_model"] == "new-model"
    _key, _base, model = config.get_deepseek_config()
    assert model == "new-model"


def test_load_user_config_missing_file_returns_empty(tmp_home):
    """config.json 不存在时返回空 dict（不抛异常）。"""
    assert config.load_user_config() == {}


def test_env_int_clamping(tmp_home, monkeypatch):
    """env_int 把超范围值夹到区间内。"""
    monkeypatch.setenv("TEST_INT_CLAMP", "99999")
    assert config.env_int("TEST_INT_CLAMP", default=10, minimum=1, maximum=100) == 100
    monkeypatch.setenv("TEST_INT_CLAMP", "0")
    assert config.env_int("TEST_INT_CLAMP", default=10, minimum=1, maximum=100) == 1


def test_env_int_default_when_unset(tmp_home, monkeypatch):
    """env 未设时返回 default。"""
    monkeypatch.delenv("TEST_INT_UNSET", raising=False)
    assert config.env_int("TEST_INT_UNSET", default=42, minimum=1, maximum=100) == 42


def test_env_int_invalid_falls_back(tmp_home, monkeypatch):
    """env 值非整数时回退到 default。"""
    monkeypatch.setenv("TEST_INT_BAD", "not-a-number")
    assert config.env_int("TEST_INT_BAD", default=42, minimum=1, maximum=100) == 42
