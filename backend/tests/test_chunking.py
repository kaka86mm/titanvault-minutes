"""智能切块（split_transcript_into_chunks）单元测试。

验证转写文本按转写段边界切，而不是在任意字符位置硬切——避免把一句话
或一个说话人轮次劈成两半，导致 map 步骤每个分块的话题不完整。
"""
import pytest

from backend.app.summary import split_transcript_into_chunks


def _make_lines(n: int, text_len: int = 50) -> str:
    """生成 n 行转写，每行约 text_len 字符（模拟 [时间戳] 说话人: 文本）。"""
    lines = []
    for i in range(n):
        # 一行：[00:00-00:35] Speaker 1: 占位文本...
        body = "话" * text_len
        lines.append(f"[00:{i:02d}-00:{i:02d}] Speaker {(i % 3) + 1}: {body}")
    return "\n".join(lines)


# -------- 基础边界 --------

def test_short_text_single_chunk():
    """短文本不切，返回单块。"""
    text = _make_lines(3)
    chunks = split_transcript_into_chunks(text, chunk_chars=10000)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_empty_text():
    """空文本返回空列表或单空串，不报错。"""
    chunks = split_transcript_into_chunks("", chunk_chars=10000)
    assert chunks == [] or chunks == [""]


# -------- 换行边界（核心） --------

def test_cut_at_line_boundary():
    """切点必须落在换行处，不能把一行从中间切开。"""
    text = _make_lines(20, text_len=100)  # 20 行，每行 ~100 字符
    chunks = split_transcript_into_chunks(text, chunk_chars=500)
    assert len(chunks) >= 2
    # 每个块要么是原始若干完整行的拼接，要么（极端兜底）单段超长时才段内切。
    # 验证：每个块的行数 >= 1，且块内每行都是完整的一行（以 ] 开头的时间戳格式）。
    for chunk in chunks:
        for line in chunk.split("\n"):
            if line.strip():
                assert line.startswith("["), f"行不完整或被切断: {line[:40]}..."


def test_no_line_split_across_chunks():
    """关键：同一行不会出现在两个块里（即不会被劈成两半）。"""
    # 构造一个明显的场景：10 行，每行 100 字符，目标块 350 字符 → 约 3 行/块
    text = _make_lines(10, text_len=100)
    lines = text.split("\n")
    chunks = split_transcript_into_chunks(text, chunk_chars=350)
    # 每个原始行必须完整地落在恰好一个块里
    for original_line in lines:
        in_chunks = [1 for chunk in chunks if original_line in chunk]
        assert sum(in_chunks) == 1, f"行被分散到多个块或丢失: {original_line[:30]}..."


def test_chunks_cover_all_content():
    """切块合并后等于原文（不丢内容）。"""
    text = _make_lines(30, text_len=80)
    chunks = split_transcript_into_chunks(text, chunk_chars=600)
    assert "\n".join(chunks) == text


# -------- 软硬上限 --------

def test_chunk_size_respects_soft_limit():
    """块大小不超过软上限太多（接近 chunk_chars，而非硬切）。

    软上限：达到 chunk_chars 后找下一个换行切，块略超但不会翻倍。
    """
    text = _make_lines(50, text_len=60)  # 每行 60 字符
    chunks = split_transcript_into_chunks(text, chunk_chars=300)
    # 每个块应该在 chunk_chars 附近，不会远超
    for chunk in chunks:
        # 允许块略大于 chunk_chars（因为要切到行尾），但不能翻倍
        assert len(chunk) <= 300 * 2, f"块过大: {len(chunk)} 字符"


def test_hard_limit_for_single_long_line():
    """单行超过硬上限（1.5x chunk_chars）时，允许段内切（兜底）。

    避免一条极长的转写段导致死循环或单块过大。
    """
    # 一条 2000 字符的超长行 + 几条短行
    long_line = f"[00:00-00:35] Speaker 1: {'长' * 1950}"
    text = long_line + "\n" + _make_lines(3, text_len=50)
    chunks = split_transcript_into_chunks(text, chunk_chars=500)
    assert len(chunks) >= 2
    # 不报错即可（段内切是允许的兜底行为）


def test_min_chunk_chars_validation():
    """chunk_chars 参数有下限保护，过小值会被钳制。"""
    text = _make_lines(5, text_len=40)
    # 即使传 1，也不该崩溃或切出单字符块
    chunks = split_transcript_into_chunks(text, chunk_chars=1)
    assert len(chunks) >= 1
    # 至少不会因为 chunk_chars=1 进入死循环
    assert sum(len(c) for c in chunks) == len(text)
