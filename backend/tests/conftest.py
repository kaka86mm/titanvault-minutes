"""共享 pytest fixtures。

原项目零测试，本文件随 Task 1 建立，后续 task 按需扩充。
"""
import importlib

import pytest


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """把 RECORDING_AI_HOME / AHAMVOICE_MODELS_DIR 指向临时目录，隔离测试不污染真实数据。

    config.py 的路径常量（BASE/CONFIG_PATH 等）在 import 时求值，所以设完 env
    后要 reload(config)，让常量基于新 env 重新求值。这样 save_user_config 等
    写操作就落到 tmp_path 下，不碰真实数据目录。

    注意 env 名：数据目录是 RECORDING_AI_HOME（原项目历史命名，未改），
    模型目录是 AHAMVOICE_MODELS_DIR。
    """
    monkeypatch.setenv("RECORDING_AI_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("AHAMVOICE_MODELS_DIR", str(tmp_path / "models"))
    from backend.app import config
    importlib.reload(config)
    yield tmp_path
    # 测完还原（避免污染后续测试看到的 module 状态）
    importlib.reload(config)
