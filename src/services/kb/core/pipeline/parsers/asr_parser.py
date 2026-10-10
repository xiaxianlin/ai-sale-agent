# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""语音 ASR 解析器（FR-KB-010）：本地 Whisper 转写 + 时间戳分段。

分段条件二选一（实现方案 §6.5）：与下一段语音间隔 > pause_sec（停顿）
或句末标点（。！？.!?）；每个段落块保留 t_start/t_end 时间戳溯源。

质量闸门：空识别 / 无语音 → stats.degrade=['low_speech'] +
needs_manual_review=True，转人工兜底复核（防静默丢失）。

引擎注入：AsrParser 接受任意实现 transcribe(audio_path) → list[AsrSegment]
的引擎（默认为本地 Whisper，NFR-SEC-06 数据不出域），便于测试与替换。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §6.5、§7
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Mapping

from .base import (
    BLOCK_TYPE_PARAGRAPH,
    BaseParser,
    Block,
    ParseError,
    UnsupportedFormatError,
    cfg_get,
)

# 句末标点：命中即在当前分段收尾（§6.5 分段条件之二）
_SENTENCE_END_MARKS = ("。", "！", "？", ".", "!", "?")
# 质量闸门降级标记（写入 stats.degrade）
DEGRADE_LOW_SPEECH = "low_speech"


@dataclass
class AsrSegment:
    """ASR 引擎分段结果契约（含词级时间戳聚合后的句段时间戳，单位秒）。"""

    start: float
    end: float
    text: str


class _WhisperEngine:
    """默认本地 Whisper 引擎封装（惰性加载模型；首次 transcribe 才加载防拖慢启动）。

    引擎契约标记 needs_wav16k=True：由解析器先做 ffmpeg 16kHz 单声道预处理。
    外部注入引擎不携带该标记时，视为已自行负责预处理，解析器直接透传路径。
    """

    needs_wav16k = True

    def __init__(self, cfg: Mapping[str, Any] | None = None) -> None:
        self._cfg = dict(cfg or {})
        self._model: Any = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            import whisper  # 延迟导入：本地 Whisper，数据不出域（NFR-SEC-06）

            model_name = self._cfg.get("parse.asr.model", "medium")  # §7：Whisper 默认 medium
            self._model = whisper.load_model(model_name)
        return self._model

    def transcribe(self, path: str) -> list[AsrSegment]:
        """本地 Whisper 转写并适配为 AsrSegment 契约结构（词级时间戳已按句聚合）。"""
        model = self._ensure_model()
        try:
            result = model.transcribe(path, word_timestamps=True)
        except Exception as exc:  # noqa: BLE001 —— 引擎调用失败统一转 ParseError
            raise ParseError(f"Whisper 转写失败：{exc}") from exc
        return [
            AsrSegment(start=float(seg["start"]), end=float(seg["end"]), text=str(seg.get("text", "")))
            for seg in result.get("segments", [])
        ]


def default_whisper_engine(cfg: Mapping[str, Any] | None = None) -> _WhisperEngine:
    """构造默认本地 Whisper 引擎；未安装 whisper 时抛带中文提示的不支持格式错误（import 守护）。"""
    try:
        import whisper  # noqa: F401 —— 仅探测可导入性（openai-whisper）
    except ImportError as exc:
        raise UnsupportedFormatError(
            "未安装 Whisper（openai-whisper），无法执行语音 ASR 解析；请安装依赖：pip install openai-whisper"
        ) from exc
    return _WhisperEngine(cfg)


class AsrParser(BaseParser):
    """语音解析器：本地 Whisper ASR + 时间戳分段；引擎可注入替换。"""

    parser_name = "asr_parser"
    fmt = "audio_asr"

    def __init__(self, whisper_engine: Any = None, cfg: Mapping[str, Any] | None = None) -> None:
        # 引擎为 None 时延迟取默认引擎（首次 _do_parse 才触发 import 守护，便于测试注入替身）
        self._injected_engine = whisper_engine
        self.cfg: dict[str, Any] = {
            "parse.asr.pause_sec": 1.5,  # §7：时间戳分段停顿阈值（秒）
        }
        if cfg:
            self.cfg.update(cfg)

    @property
    def whisper(self) -> Any:
        """ASR 引擎惰性获取：注入引擎优先，否则默认本地 Whisper（import 守护）。"""
        if self._injected_engine is not None:
            return self._injected_engine
        return default_whisper_engine(self.cfg)

    def _do_parse(self, path: str) -> list[Block]:
        engine = self.whisper
        # 默认 Whisper 引擎需要 16kHz 单声道 wav：先做 ffmpeg 预处理；
        # 注入引擎（无 needs_wav16k 标记）自行负责预处理，直接透传原路径
        if getattr(engine, "needs_wav16k", False):
            audio_path = self._to_wav16k_mono(path)
        else:
            audio_path = path
        segs = engine.transcribe(audio_path)

        blocks = self._segment(segs, float(self.cfg["parse.asr.pause_sec"]))

        # 质量闸门：空识别 / 无有效文本 → 降级标记 + 人工复核（覆盖率 100% 的兜底入口）
        if not segs or not any(b.text for b in blocks):
            self._set_stats(degrade=[DEGRADE_LOW_SPEECH], needs_manual_review=True)
        return blocks

    @staticmethod
    def _segment(segs: list[AsrSegment], pause_sec: float) -> list[Block]:
        """按「停顿 > pause_sec 或句末标点」切段；块 meta 保留 t_start/t_end 时间戳溯源（§6.5）。"""
        blocks: list[Block] = []
        buf: list[AsrSegment] = []
        for i, seg in enumerate(segs):
            buf.append(seg)
            gap_to_next = segs[i + 1].start - seg.end if i + 1 < len(segs) else None
            ends_sentence = seg.text.rstrip().endswith(_SENTENCE_END_MARKS)
            if gap_to_next is None or gap_to_next > pause_sec or ends_sentence:
                text = "".join(s.text.strip() for s in buf).strip()
                if text:
                    blocks.append(
                        Block(
                            type=BLOCK_TYPE_PARAGRAPH,
                            text=text,
                            meta={"t_start": buf[0].start, "t_end": buf[-1].end},
                        )
                    )
                buf = []
        return blocks

    @staticmethod
    def _to_wav16k_mono(path: str) -> str:
        """ffmpeg 预处理：任意音频格式 → 16kHz 单声道 wav（供本地 Whisper 推理）。"""
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise ParseError("未安装 ffmpeg，无法完成音频预处理（ASR 需要 16kHz 单声道 wav 输入）")
        out_path = tempfile.NamedTemporaryFile(prefix="kb_asr_", suffix=".wav", delete=False).name
        cmd = [ffmpeg, "-y", "-i", path, "-ac", "1", "-ar", "16000", out_path]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        except FileNotFoundError as exc:
            raise ParseError(f"ffmpeg 调用失败：{exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ParseError("ffmpeg 音频预处理超时（>600s），请检查音频文件是否异常") from exc
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", errors="ignore")[-500:] if exc.stderr else ""
            raise ParseError(f"ffmpeg 音频预处理失败：{detail}") from exc
        return out_path
