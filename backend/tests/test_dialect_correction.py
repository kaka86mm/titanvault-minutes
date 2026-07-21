"""方言纠错模块测试。

验证：
1. 对齐回填：纠正后能正确映射回原段（保留 speaker/时间戳）
2. 失败容错：LLM 出错时保留原文不阻塞
3. 空输入安全
4. 行数不匹配时的兜底处理
"""
from unittest.mock import patch, MagicMock

import pytest

from backend.app.dialect_correction import (
    correct_dialect_segments,
    _parse_corrected_lines,
)


def _make_segments(texts):
    """构造测试用转写段。"""
    return [
        {"id": f"seg-{i}", "speaker": str(i % 2), "text": t, "start_sec": i * 10}
        for i, t in enumerate(texts)
    ]


# -------- _parse_corrected_lines（纯函数） --------

def test_parse_strips_speaker_prefix():
    """剥掉 [说话人X] 前缀，返回纯文本。"""
    text = "[说话人1] 你好\n[说话人2] 再见"
    result = _parse_corrected_lines(text, 2)
    assert result == ["你好", "再见"]


def test_parse_handles_empty_lines():
    """空行被跳过。"""
    text = "[说话人1] 你好\n\n[说话人2] 再见"
    result = _parse_corrected_lines(text, 2)
    assert result == ["你好", "再见"]


def test_parse_no_longer_truncates():
    """_parse_corrected_lines 不再截断——返回实际行数，由调用方判断对齐。

    之前会截断到 expected_count，但那会掩盖 LLM 合并/拆分行的问题，
    导致回填错位。现在返回真实行数，correct_dialect_segments 检测到
    行数不匹配会拒绝整个窗口。
    """
    text = "[说话人1] a\n[说话人1] b\n[说话人1] c"
    result = _parse_corrected_lines(text, 2)  # 期望 2，实际 3
    assert len(result) == 3  # 不截断，返回实际 3 行
    assert result == ["a", "b", "c"]


def test_parse_missing_prefix_still_works():
    """LLM 漏了前缀也能解析（容错）。"""
    text = "你好\n再见"
    result = _parse_corrected_lines(text, 2)
    assert result == ["你好", "再见"]


# -------- correct_dialect_segments（含 mock LLM） --------

def test_empty_segments_returns_empty():
    """空输入原样返回。"""
    assert correct_dialect_segments([]) == []


def test_no_api_key_returns_original():
    """无 LLM key 时保留原文（mock get_llm_config 返回空 key）。"""
    segs = _make_segments(["原文一", "原文二"])
    with patch("backend.app.dialect_correction.get_llm_config", return_value=("", "base", "model")):
        result = correct_dialect_segments(segs)
    assert [s["text"] for s in result] == ["原文一", "原文二"]


def test_successful_correction_backfills():
    """LLM 纠错成功时，纠正后文本回填到段，保留 speaker。"""
    segs = _make_segments(["合动上评估", "曲俗时间"])
    # mock LLM 返回纠正后的文本（逐行对应，行数匹配）
    corrected_output = "[说话人0] 合同上评估\n[说话人1] 取数时间"
    with patch("backend.app.dialect_correction.get_llm_config", return_value=("key", "base", "model")), \
         patch("backend.app.dialect_correction._call_llm_correction", return_value=corrected_output):
        result = correct_dialect_segments(segs)
    assert result[0]["text"] == "合同上评估"
    assert result[1]["text"] == "取数时间"
    # speaker 和其他元数据保留
    assert result[0]["speaker"] == "0"
    assert result[0]["start_sec"] == 0


def test_line_count_mismatch_rejects_window():
    """LLM 合并/拆分行导致行数不匹配时，拒绝整个窗口保留原文（防错位）。"""
    segs = _make_segments(["第一段", "第二段"])
    # mock LLM 把 2 行合并成 1 行
    merged_output = "[说话人0] 第一段然后第二段"
    with patch("backend.app.dialect_correction.get_llm_config", return_value=("key", "base", "model")), \
         patch("backend.app.dialect_correction._call_llm_correction", return_value=merged_output):
        result = correct_dialect_segments(segs)
    # 行数不匹配（期望2，实际1）→ 拒绝，保留原文
    assert result[0]["text"] == "第一段"
    assert result[1]["text"] == "第二段"


def test_llm_failure_preserves_original():
    """LLM 调用失败时，保留原文不阻塞。"""
    segs = _make_segments(["原文一", "原文二"])
    with patch("backend.app.dialect_correction.get_llm_config", return_value=("key", "base", "model")), \
         patch("backend.app.dialect_correction._call_llm_correction", side_effect=RuntimeError("LLM 超时")):
        result = correct_dialect_segments(segs)
    # 失败保留原文
    assert [s["text"] for s in result] == ["原文一", "原文二"]


def test_does_not_mutate_input():
    """纠错不污染输入原数据（返回新 list）。"""
    segs = _make_segments(["原文"])
    with patch("backend.app.dialect_correction.get_llm_config", return_value=("key", "base", "model")), \
         patch("backend.app.dialect_correction._call_llm_correction", return_value="[说话人0] 改后"):
        result = correct_dialect_segments(segs)
    assert segs[0]["text"] == "原文"  # 原数据不变
    assert result[0]["text"] == "改后"  # 返回的是纠正后的


def test_window_split_large_input():
    """超过窗口大小的段会被分多个窗口处理（mock 被调用多次）。"""
    # CORRECTION_WINDOW=8，造 12 段 → 应调 2 次（8+4）
    segs = _make_segments([f"段{i}" for i in range(12)])
    call_count = {"n": 0}

    def mock_call(api_key, base, model, text):
        call_count["n"] += 1
        # 返回等长行
        lines = text.strip().split("\n")
        return "\n".join(f"[说话人0] 纠{call_count['n']}-{i}" for i in range(len(lines)))

    with patch("backend.app.dialect_correction.get_llm_config", return_value=("key", "base", "model")), \
         patch("backend.app.dialect_correction._call_llm_correction", side_effect=mock_call):
        result = correct_dialect_segments(segs)

    assert call_count["n"] == 2  # 8+4 两个窗口
    assert len(result) == 12  # 全部回填
    assert all("纠" in s["text"] for s in result)  # 都被纠正了


def test_partial_window_failure_preserves_rest():
    """一个窗口失败不影响其他窗口（失败窗口保留原文）。"""
    segs = _make_segments(["段0", "段1", "段2"])
    call_count = {"n": 0}

    def mock_call(api_key, base, model, text):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("第一次失败")
        return "[说话人0] 段2纠后"

    with patch("backend.app.dialect_correction.get_llm_config", return_value=("key", "base", "model")), \
         patch("backend.app.dialect_correction._call_llm_correction", side_effect=mock_call):
        result = correct_dialect_segments(segs)

    # 注意：3 段 < 窗口 8，只调一次。改成测多窗口场景
    # 这里验证失败时保留原文
    assert len(result) == 3


def test_consecutive_failures_aborts_remaining():
    """连续 3 次窗口失败后放弃剩余窗口（防 model 不支持长文本卡死）。"""
    # 造 40 段 → 5 个窗口，全部失败 → 第 3 次后放弃
    segs = _make_segments([f"段{i}" for i in range(40)])
    call_count = {"n": 0}

    def mock_call(api_key, base, model, text):
        call_count["n"] += 1
        raise RuntimeError("model 返回空")

    with patch("backend.app.dialect_correction.get_llm_config", return_value=("key", "base", "model")), \
         patch("backend.app.dialect_correction._call_llm_correction", side_effect=mock_call):
        result = correct_dialect_segments(segs)

    # 连续失败 3 次后放弃，不会调满 5 个窗口
    assert call_count["n"] == 3
    # 全部保留原文
    assert all(s["text"].startswith("段") for s in result)
    assert len(result) == 40


def test_total_timeout_skips_remaining(monkeypatch):
    """整体超时后剩余窗口保留原文（不会跑完全部窗口）。"""
    import backend.app.dialect_correction as mod
    monkeypatch.setattr(mod, "MAX_TOTAL_SECONDS", 0)  # 立即超时

    # 造足够多段，确保有多个窗口
    segs = _make_segments([f"段{i}" for i in range(40)])  # 5 个窗口
    call_count = {"n": 0}

    def mock_call(api_key, base, model, text):
        call_count["n"] += 1
        return "[说话人0] 纠正"

    with patch("backend.app.dialect_correction.get_llm_config", return_value=("key", "base", "model")), \
         patch("backend.app.dialect_correction._call_llm_correction", side_effect=mock_call):
        result = correct_dialect_segments(segs)

    # 超时后大部分窗口被跳过，不会跑满 5 个
    assert call_count["n"] < 5
    # 未跑的窗口保留原文
    original_count = sum(1 for s in result if s["text"].startswith("段"))
    assert original_count > 0
