"""路径常量、env 读取、DeepSeek 配置读写。

从 main.py 搬迁，逻辑不变。注意：BASE 等路径常量在 import 时求值
（依赖 RECORDING_AI_HOME env），测试如需隔离要用 importlib.reload(config)。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        from dotenv import dotenv_values
    except ImportError:
        # python-dotenv is in requirements.txt; missing only when the venv is
        # not bootstrapped. Keep the API bootable without env overrides.
        return
    try:
        values = dotenv_values(path) or {}
    except Exception:
        # Malformed .env should not block server start.
        return
    for key, value in values.items():
        if value is not None:
            os.environ.setdefault(key, str(value))


load_env_file(ROOT / ".env.local")
load_env_file(ROOT / ".env")


def env_compat(name: str) -> str | None:
    """读环境变量，TITANVAULT_* 优先，AHAMVOICE_* 回退（改名兼容）。

    项目从 aham-voice-web 改名为 titanvault-minutes，环境变量前缀从
    AHAMVOICE_ 改成 TITANVAULT_。老用户的 .env 用 AHAMVOICE_* 仍生效。
    调用方传新名（TITANVAULT_*），本函数自动回退旧名。
    """
    val = os.environ.get(name)
    if val is not None:
        return val
    if name.startswith("TITANVAULT_"):
        legacy = "AHAMVOICE_" + name[len("TITANVAULT_"):]
        return os.environ.get(legacy)
    return None


def _default_base() -> Path:
    # Per-user writable data dir (DB, recordings, config.json). Overridable
    # via RECORDING_AI_HOME.
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AhamVoice"
    return Path.home() / ".cache" / "recording-ai"


# 数据根目录。优先 AHAMVOICE_HOME（新名，Docker/compose 用），回退 RECORDING_AI_HOME
# （原项目历史名，保留兼容），再回退平台默认。
BASE = Path(env_compat("TITANVAULT_HOME") or os.environ.get("RECORDING_AI_HOME") or _default_base())
APP_DATA = BASE / "app-data"
DB_PATH = APP_DATA / "ahamvoice.sqlite3"
RECORDINGS = APP_DATA / "recordings"
EXPORTS = APP_DATA / "exports"
TMP = APP_DATA / "tmp"
# Models and ffmpeg are read-only assets. AHAMVOICE_MODELS_DIR / AHAMVOICE_BIN_DIR
# override for the Docker volume layout; default to BASE/... for local dev.
MODELS = Path(env_compat("TITANVAULT_MODELS_DIR") or (BASE / "models" / "modelscope" / "iic"))
VAD = MODELS / "speech_fsmn_vad_zh-cn-16k-common-pytorch"
PUNC = MODELS / "punc_ct-transformer_cn-en-common-vocab471067-large"
PARAFORMER = MODELS / "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
CAMPLUS = MODELS / "speech_campplus_sv_zh-cn_16k-common"
EMOTION = MODELS / "emotion2vec_plus_large"
# MOSS-Transcribe-Diarize (可选端到端转写+分离引擎, 替代 FunASR Paraformer+CAM++)
# 路径指向 HF snapshot 目录 (含 config.json + safetensors)
MOSS_MODEL = Path(env_compat("TITANVAULT_MOSS_MODEL") or (MODELS / "moss-transcribe-diarize"))
# ASR 引擎选择: funasr (默认, Paraformer+CAM++) / moss (端到端, 需 BUILD_MOSS=1 构建镜像)
ASR_ENGINE = (env_compat("TITANVAULT_ASR_ENGINE") or "funasr").lower()
VOICEPRINTS = BASE / "voiceprints"
BIN_DIR = Path(env_compat("TITANVAULT_BIN_DIR") or (BASE / "bin"))
os.environ["PATH"] = f"{BIN_DIR}:{os.environ.get('PATH', '')}"
# ffmpeg/ffprobe：优先 BIN_DIR（原 Mac 桌面版静态化），找不到则从系统 PATH 解析
# （Docker 版 apt 装在 /usr/bin/ffmpeg）。
import shutil as _shutil
_bin_ffmpeg = BIN_DIR / "ffmpeg"
_bin_ffprobe = BIN_DIR / "ffprobe"
FFMPEG = _bin_ffmpeg if _bin_ffmpeg.exists() else Path(_shutil.which("ffmpeg") or "ffmpeg")
FFPROBE = _bin_ffprobe if _bin_ffprobe.exists() else Path(_shutil.which("ffprobe") or "ffprobe")

for path in [APP_DATA, RECORDINGS, EXPORTS, TMP, VOICEPRINTS]:
    path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Runtime config (config.json in the writable data dir). Single-user mode
# keeps the DeepSeek API key here instead of .env so it can be edited from the
# in-app Settings page. Env vars still win.
# ---------------------------------------------------------------------------
CONFIG_PATH = BASE / "config.json"


def load_user_config() -> dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_user_config(updates: dict[str, Any]) -> dict[str, Any]:
    data = load_user_config()
    data.update(updates)
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(CONFIG_PATH)
    return data


def get_llm_config() -> tuple[str, str, str]:
    """Return (api_key, api_base, model) for an OpenAI-compatible chat endpoint.

    Priority: new LLM_* env / llm_* config key wins, then legacy DEEPSEEK_* env /
    deepseek_* config key (backward compat), then default. Default base/model point
    at DeepSeek so the box stays usable out-of-the-box; switch in the Settings page
    to target any OpenAI-compatible endpoint (通义/Kimi/Ollama/vLLM/OpenAI ...).
    """
    cfg = load_user_config()
    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or cfg.get("llm_api_key")
        or cfg.get("deepseek_api_key")
        or ""
    ).strip()
    base = (
        os.environ.get("LLM_API_BASE")
        or os.environ.get("DEEPSEEK_API_BASE")
        or cfg.get("llm_api_base")
        or cfg.get("deepseek_api_base")
        or "https://api.deepseek.com"
    ).rstrip("/")
    model = (
        os.environ.get("LLM_MODEL")
        or os.environ.get("DEEPSEEK_MODEL")
        or cfg.get("llm_model")
        or cfg.get("deepseek_model")
        or "deepseek-chat"
    ).strip()
    return api_key, base, model


def get_deepseek_config() -> tuple[str, str, str]:
    """Deprecated alias for get_llm_config (kept for backward-compat imports)."""
    return get_llm_config()


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env_compat(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = env_compat(name)
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_compat(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_json(name: str, default: Any) -> Any:
    raw = env_compat(name) or ""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default
