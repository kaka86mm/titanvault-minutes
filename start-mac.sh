#!/usr/bin/env bash
# TitanVault Minutes — Mac 一键启动脚本（原生 MPS 加速）
#
# 用法：
#   chmod +x start-mac.sh && ./start-mac.sh
#
# 首次运行自动：装 ffmpeg → 创建 venv → 装依赖 → 下载模型 → 启动服务
# 后续运行：直接启动（跳过已完成的步骤）
# 浏览器打开 http://localhost:8765
#
# 可选环境变量（写入同目录 .env 或 export）：
#   AHAMVOICE_ACCESS_PASSWORD=你的密码   # 密码门（空=不启用）
#   LLM_API_KEY=sk-xxx                   # 大模型 Key（纪要用）
#   LLM_API_BASE=https://api.deepseek.com
#   LLM_MODEL=deepseek-chat

set -euo pipefail
cd "$(dirname "$0")"

# ─── 颜色输出 ───
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'
log()  { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
step() { echo -e "${CYAN}▶${NC} $1"; }

VENV=".venv-mac"
PYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"

# ─── 1. 检查 macOS + Apple Silicon ───
if [[ "$(uname)" != "Darwin" ]]; then
    echo "这个脚本是给 macOS 用的。Linux/Windows 请用 Docker。"
    exit 1
fi
ARCH=$(uname -m)
if [[ "$ARCH" == "arm64" ]]; then
    DEVICE="mps"
    log "检测到 Apple Silicon ($ARCH) — 将使用 MPS GPU 加速"
else
    DEVICE="cpu"
    warn "检测到 Intel Mac ($ARCH) — 将使用 CPU（较慢，M 系列芯片有 MPS 加速）"
fi

# ─── 2. 检查/安装 ffmpeg ───
step "检查 ffmpeg..."
if ! command -v ffmpeg &>/dev/null; then
    if command -v brew &>/dev/null; then
        step "通过 Homebrew 安装 ffmpeg..."
        brew install ffmpeg
        log "ffmpeg 安装完成"
    else
        warn "未检测到 ffmpeg，也未检测到 Homebrew。"
        echo "  请先安装 Homebrew：https://brew.sh"
        echo "  然后 brew install ffmpeg，再重新运行本脚本。"
        exit 1
    fi
else
    log "ffmpeg 已安装"
fi

# ─── 3. 检查 Python 3.10+ ───
step "检查 Python..."
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0")
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)' 2>/dev/null || echo "0")
if [[ "$PY_OK" != "1" ]]; then
    echo "需要 Python 3.10+，当前是 $PY_VERSION。"
    echo "  建议：brew install python@3.12"
    exit 1
fi
log "Python $PY_VERSION"

# ─── 4. 创建 venv（首次） ───
if [[ ! -d "$VENV" ]]; then
    step "创建虚拟环境 $VENV..."
    python3 -m venv "$VENV"
    log "虚拟环境创建完成"
else
    log "虚拟环境已存在"
fi

# ─── 5. 安装依赖（检测 requirements 变化，变了就自动重装） ───
REQS_HASH=$(cat backend/requirements.txt | shasum | awk '{print $1}')
STAMP_HASH=$(cat "$VENV/.deps-installed" 2>/dev/null || echo "")

if [[ "$REQS_HASH" != "$STAMP_HASH" ]] || [[ "${1:-}" == "--reinstall" ]]; then
    step "安装依赖（首次较慢，约 3-5 分钟）..."
    $PIP install --upgrade pip
    $PIP install -r backend/requirements.txt
    $PIP install torch torchaudio
    $PIP install funasr modelscope librosa soundfile scipy scikit-learn
    $PIP install addict datasets simplejson
    echo "$REQS_HASH" > "$VENV/.deps-installed"
    log "依赖安装完成"
else
    log "依赖已安装（如需重装：./start-mac.sh --reinstall）"
fi

# ─── 6. 默认环境变量 ───
# 注意：ASR_DEVICE 的默认值在 source .env 之后再设，让 .env 能覆盖
export AHAMVOICE_PORT="${AHAMVOICE_PORT:-8765}"
export AHAMVOICE_HOME="${AHAMVOICE_HOME:-$(pwd)/data}"
export AHAMVOICE_MODELS_DIR="${AHAMVOICE_MODELS_DIR:-$(pwd)/models}"

# ─── 7. 加载 .env（如果存在） ───
if [[ -f .env ]]; then
    log "加载 .env 配置"
    set -a
    source .env 2>/dev/null || true
    set +a
fi

# ASR 设备：.env 里设了就用 .env 的（允许用户强制 cpu），否则用自动检测（mps/cpu）
export AHAMVOICE_ASR_DEVICE="${AHAMVOICE_ASR_DEVICE:-$DEVICE}"

# ─── 8. 启动 ───
PORT="${AHAMVOICE_PORT:-8765}"
step "启动 TitanVault Minutes（端口 $PORT，$DEVICE 加速）..."
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🎙️  TitanVault Minutes 正在启动..."
echo "  📍 浏览器打开：http://localhost:$PORT"
echo "  ⚡ GPU 加速：$DEVICE"
echo "  📁 数据目录：$AHAMVOICE_HOME"
echo "  首​​次启动会自动下载模型（约 4GB），请耐心等待"
echo "  按 Ctrl+C 停止"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

exec $PYTHON -m uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT"
