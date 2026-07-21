"""首次启动模型检测 + 自动下载。

容器场景模型不进镜像（4GB 太大），volume 挂载 + 首次下载。
find_missing_models 是纯函数（好测）；ensure_models 跑 modelscope
snapshot_download，进度写 stdout（docker logs -f 可见）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 5 个必需模型（与 config.py 的 VAD/PUNC/PARAFORMER/CAMPLUS/EMOTION 对应）
REQUIRED_MODELS = [
    "speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "punc_ct-transformer_cn-en-common-vocab471067-large",
    "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    "speech_campplus_sv_zh-cn_16k-common",
    "emotion2vec_plus_large",
]


def find_missing_models(models_dir: Path) -> list[str]:
    """返回缺失的模型目录名列表。"""
    return [name for name in REQUIRED_MODELS if not (models_dir / name).is_dir()]


def ensure_models(models_dir: Path) -> None:
    """检测并下载缺失模型。容器启动时调用。"""
    missing = find_missing_models(models_dir)
    if not missing:
        print(f"[models] 所有模型已就绪：{models_dir}", flush=True)
        return
    print(f"[models] 检测到 {len(missing)} 个缺失模型，开始下载：{missing}", flush=True)
    try:
        from modelscope import snapshot_download
    except ImportError:
        print("[models] WARNING: modelscope 未安装，跳过自动下载。请手动放置模型。", flush=True)
        return
    for name in missing:
        print(f"[models] 下载 {name} ...", flush=True)
        try:
            # snapshot_download(f"iic/{name}", cache_dir=X) 落到 X/iic/name。
            # 我们要模型直接在 models_dir/name（config 的 VAD/PARAFORMER 等按此找），
            # 所以下载后把 X/iic/name 移到 models_dir/name（去掉 iic 命名空间层）。
            import shutil
            tmp_cache = models_dir / ".dl_cache"
            snapshot_download(f"iic/{name}", cache_dir=str(tmp_cache))
            src = tmp_cache / "iic" / name
            dst = models_dir / name
            if src.exists():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.move(str(src), str(dst))
            print(f"[models] 完成 {name}", flush=True)
        except Exception as exc:
            print(f"[models] 失败 {name}: {exc}", flush=True)
            raise
