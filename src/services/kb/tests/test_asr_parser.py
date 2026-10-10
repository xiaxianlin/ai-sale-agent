# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""ASR 语音解析器单元测试（FR-KB-010，注入引擎替身，不依赖 Whisper/ffmpeg）。

覆盖（对应实现方案 §9 test_asr_segment 及 §6.5）：
- 停顿切段：段间隔 > parse.asr.pause_sec → 切段，块 meta 携带 t_start/t_end；
- 句末标点切段：句末（。！？.!?）即使无停顿也收尾；
- 无触发不分段：近距离无标点行并同段；
- 质量闸门：空识别 → stats.degrade=['low_speech'] + needs_manual_review=True；
- import 守护：whisper 缺失时抛带中文提示的 UnsupportedFormatError。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §6.5、§7、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import sys

import pytest

from services.kb.core.pipeline.parsers.asr_parser import (
    DEGRADE_LOW_SPEECH,
    AsrParser,
    AsrSegment,
)
from services.kb.core.pipeline.parsers.base import (
    BLOCK_TYPE_PARAGRAPH,
    UnsupportedFormatError,
)


class _FakeWhisperEngine:
    """Whisper 引擎替身：返回预设分段（不携带 needs_wav16k 标记 → 跳过 ffmpeg 预处理）。"""

    def __init__(self, segments: list[AsrSegment]) -> None:
        self.segments = segments
        self.calls: list[str] = []

    def transcribe(self, path: str) -> list[AsrSegment]:
        self.calls.append(path)
        return self.segments


class TestAsrSegmentation:
    """时间戳分段逻辑（§6.5：停顿 > pause_sec 或句末标点二选一切段）。"""

    def test_segment_by_pause(self) -> None:
        """含 2s 停顿的语音：停顿处切段，块 meta 含 t_start/t_end 时间戳。"""
        engine = _FakeWhisperEngine(
            [
                AsrSegment(start=0.0, end=2.0, text="第一段介绍产品。"),
                AsrSegment(start=4.0, end=6.0, text="第二段介绍价格"),  # 前后间隔 2s > 1.5s
            ]
        )
        doc = AsrParser(whisper_engine=engine).parse("meeting.mp3")
        assert doc.source_format == "audio_asr"
        blocks = [b for b in doc.blocks if b.type == BLOCK_TYPE_PARAGRAPH]
        # 第 1 段以句号收尾；第 2 段因「与前段间隔 2s」起段（停顿切段）
        assert len(blocks) == 2
        assert blocks[0].text == "第一段介绍产品。"
        assert blocks[0].meta["t_start"] == 0.0
        assert blocks[0].meta["t_end"] == 2.0
        assert blocks[1].text == "第二段介绍价格"
        assert blocks[1].meta["t_start"] == 4.0
        assert blocks[1].meta["t_end"] == 6.0
        # 注入引擎不做 ffmpeg 预处理，直接透传原路径
        assert engine.calls == ["meeting.mp3"]

    def test_segment_by_sentence_punctuation(self) -> None:
        """句末标点切段：段间隔仅 0.2s（< pause_sec）但前句以「。」结尾 → 切段。"""
        engine = _FakeWhisperEngine(
            [
                AsrSegment(start=0.0, end=2.0, text="今天介绍产品。"),
                AsrSegment(start=2.2, end=4.0, text="下面开始新的话题"),
            ]
        )
        doc = AsrParser(whisper_engine=engine).parse("voice.wav")
        blocks = [b for b in doc.blocks if b.type == BLOCK_TYPE_PARAGRAPH]
        assert len(blocks) == 2
        assert blocks[0].text == "今天介绍产品。"
        assert blocks[1].text == "下面开始新的话题"

    def test_no_trigger_no_split(self) -> None:
        """近距离且无句末标点 → 并为同一段。"""
        engine = _FakeWhisperEngine(
            [
                AsrSegment(start=0.0, end=2.0, text="这一句没有标号"),
                AsrSegment(start=2.4, end=4.4, text="仍然属于同一段内容"),
            ]
        )
        doc = AsrParser(whisper_engine=engine).parse("long.mp3")
        blocks = [b for b in doc.blocks if b.type == BLOCK_TYPE_PARAGRAPH]
        assert len(blocks) == 1
        assert blocks[0].text == "这一句没有标号仍然属于同一段内容"
        assert blocks[0].meta["t_start"] == 0.0
        assert blocks[0].meta["t_end"] == 4.4

    def test_english_sentence_end_marks(self) -> None:
        """英文句末标点（. ! ?）同样触发切段。"""
        engine = _FakeWhisperEngine(
            [
                AsrSegment(start=0.0, end=1.0, text="Hello there."),
                AsrSegment(start=1.2, end=2.0, text="Next point"),
            ]
        )
        doc = AsrParser(whisper_engine=engine).parse("en.mp3")
        assert len([b for b in doc.blocks if b.type == BLOCK_TYPE_PARAGRAPH]) == 2

    def test_pause_threshold_configurable(self) -> None:
        """pause_sec 阈值外置可调（NFR-MAI-02）：阈值 3s 时 2s 间隔不再切段。"""
        engine = _FakeWhisperEngine(
            [
                AsrSegment(start=0.0, end=2.0, text="第一小段"),
                AsrSegment(start=4.0, end=6.0, text="第二小段"),
            ]
        )
        doc = AsrParser(whisper_engine=engine, cfg={"parse.asr.pause_sec": 3.0}).parse("cfg.mp3")
        blocks = [b for b in doc.blocks if b.type == BLOCK_TYPE_PARAGRAPH]
        assert len(blocks) == 1  # 2s 停顿 < 3s 阈值 → 并为一段


class TestAsrQualityGate:
    """质量闸门：空识别降级（§6.5：degrade=['low_speech'] + 人工复核）。"""

    def test_empty_transcription_degrades(self) -> None:
        """空识别 → stats.degrade 含 low_speech 且 needs_manual_review=True。"""
        doc = AsrParser(whisper_engine=_FakeWhisperEngine([])).parse("silent.mp3")
        assert doc.blocks == []
        assert doc.stats["degrade"] == [DEGRADE_LOW_SPEECH]
        assert doc.stats["needs_manual_review"] is True

    def test_blank_text_segments_degrades(self) -> None:
        """仅有空白文本的分段（无有效语音内容）→ 同样降级。"""
        engine = _FakeWhisperEngine([AsrSegment(start=0.0, end=1.0, text="   ")])
        doc = AsrParser(whisper_engine=engine).parse("noise.mp3")
        assert doc.stats["degrade"] == [DEGRADE_LOW_SPEECH]
        assert doc.stats["needs_manual_review"] is True


class TestAsrImportGuard:
    """import 守护与默认引擎路径。"""

    def test_default_engine_import_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """whisper 缺失（模拟）→ 带中文提示的 UnsupportedFormatError。"""
        monkeypatch.setitem(sys.modules, "whisper", None)  # 模拟未安装（确定性）
        with pytest.raises(UnsupportedFormatError) as exc_info:
            AsrParser().parse("a.mp3")
        assert "Whisper" in str(exc_info.value)
        assert exc_info.value.code == "PARSE-1001"

    def test_injected_engine_skips_default_guard(self) -> None:
        """注入引擎时不触发默认 Whisper 加载（引擎替身直接可用）。"""
        engine = _FakeWhisperEngine([AsrSegment(start=0.0, end=1.0, text="内容。")])
        doc = AsrParser(whisper_engine=engine).parse("x.mp3")
        assert doc.stats["chars"] > 0
        assert "degrade" not in doc.stats
