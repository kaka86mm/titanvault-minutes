"""首次启动模型检测 model_download.py 的单元测试（TDD）。"""
from pathlib import Path

from backend.app import model_download


def test_missing_models_detected(tmp_path):
    """空目录 → 5 个模型都报缺失。"""
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    missing = model_download.find_missing_models(models_dir)
    assert set(missing) == {
        "speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "punc_ct-transformer_cn-en-common-vocab471067-large",
        "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "speech_campplus_sv_zh-cn_16k-common",
        "emotion2vec_plus_large",
    }


def test_complete_models_not_missing(tmp_path):
    """5 个模型目录都在 → 无缺失。"""
    models_dir = tmp_path / "models"
    for name in [
        "speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "punc_ct-transformer_cn-en-common-vocab471067-large",
        "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "speech_campplus_sv_zh-cn_16k-common",
        "emotion2vec_plus_large",
    ]:
        (models_dir / name).mkdir(parents=True)
    assert model_download.find_missing_models(models_dir) == []


def test_partial_models(tmp_path):
    """只有 2 个 → 报缺 3 个。"""
    models_dir = tmp_path / "models"
    (models_dir / "speech_fsmn_vad_zh-cn-16k-common-pytorch").mkdir(parents=True)
    (models_dir / "emotion2vec_plus_large").mkdir(parents=True)
    missing = model_download.find_missing_models(models_dir)
    assert len(missing) == 3
