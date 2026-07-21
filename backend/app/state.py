"""模块级共享状态：模型实例单例 + 进程锁。

FunASR 的 AutoModel 和 modelscope speaker_verification 内部有跨调用状态
（cache dict、tensor buffer），线程池并发会 corrupt。用进程级锁串行化。
单例避免重复加载 4GB 模型。

注意：模型实例字段在首次使用时由 asr/voiceprint/emotion 模块填充
（state.asr_model = ...），不是在 import 时。后续 task 抽领域模块时，
get_asr_model/get_speaker_verifier/get_emotion_model 会搬到对应模块，
通过 state.xxx 读写单例。
"""
from __future__ import annotations

import threading
from typing import Any

# 进程级锁：ASR + 声纹验证串行化（防 FunASR 并发状态腐败）
asr_lock = threading.Lock()
asr_init_lock = threading.Lock()
verifier_init_lock = threading.Lock()
emotion_init_lock = threading.Lock()

# 模型单例（懒加载，首次用时由 get_* 函数填充）
asr_model: Any | None = None
speaker_verifier: Any | None = None
emotion_model: Any | None = None

DEFAULT_VOICEPRINT_THRESHOLD = 0.66

# 单用户身份（共享常量）。current_user 返回 _LOCAL_USER，recording_payload/
# recover_queued_recordings 等多处引用，放这里供 db.py / main.py 共用。
LOCAL_USER_ID = "local-admin"

_LOCAL_USER = {
    "id": LOCAL_USER_ID,
    "name": "本机用户",
    "role": "manager",
    "managed_team_ids": ["*"],
    "team_id": None,
}
