# 贡献指南

感谢你对 Aham Voice Web 的兴趣！本项目基于 [aham-voice](https://github.com/li599198347-svg/aham-voice)（MIT）改造。

## 开发环境

```bash
# 后端（Python 3.10+）
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt          # 轻量依赖
pip install -r backend/requirements-asr.txt       # ASR 重栈（torch/funasr，可选）
pip install pytest httpx                          # 测试

# 前端
cd frontend-src && npm install
```

## 本地运行

```bash
# 后端
AHAMVOICE_HOME=/tmp/aham-dev python -m uvicorn backend.app.main:app --port 8765 --reload

# 前端（另一个终端）
cd frontend-src && npm run dev    # Vite 5174，/api 代理到 8765
```

## 测试

```bash
python -m pytest backend/tests/ -v
```

新增功能请写测试（参考 `test_config.py` / `test_security.py` / `test_hotword_discover.py`）。

## 前端构建

改完前端后必须重新构建（后端托管 dist）：
```bash
cd frontend-src && npm run build    # 产出 ../frontend/dist
```

## 代码结构

```
backend/app/
├── main.py            # FastAPI app + 路由 + startup + 静态托管
├── config.py          # 路径/env/LLM 配置
├── state.py           # 共享状态（锁/模型单例/单用户身份）
├── db.py              # SQLite 连接/schema/迁移/中断恢复
├── security.py        # 单密码门
├── asr.py             # FunASR 转写（枢纽）
├── hotwords.py        # 热词打分/双轨包
├── hotword_discover.py # 热词智能发现（LLM 抽取）
├── voiceprint.py      # 声纹匹配
├── emotion.py         # 情绪分析
├── summary.py         # 会议纪要
├── deepseek.py        # LLM 传输层
└── model_download.py  # 模型自动下载
```

## Commit 规范

```
<type>: <描述>

type: feat/fix/refactor/docs/chore/test
```

## License

MIT（见 [LICENSE](LICENSE)）
