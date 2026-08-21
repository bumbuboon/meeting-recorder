#!/usr/bin/env python3
"""Shared title and keep/test classification contract for meeting runs."""

from __future__ import annotations

import math
import re
from typing import Any


MANIFEST_KEYS = ("title", "disposition", "confidence", "reason")
INTERPRET_KEYS = ("meeting_title", "disposition", "confidence", "reason")
MIN_LLM_CHARACTERS = 16
TITLE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
GENERIC_TITLES = {"会議", "ミーティング", "打ち合わせ", "議事録", "テスト", "録音"}

CLASSIFICATION_INSTRUCTION = """meeting_title は内容を表す具体的で短い日本語句（40文字以内）にしてください。
挨拶だけ、動作確認、マイクや録音のテスト、文章の読み上げ、数十秒だけの内容は disposition=test です。
録画・文字起こし・議事録生成の機能検証だと自称する内容は、議題や決定事項の形式を取っていても disposition=test です。
実際の議題、相談、意思決定、共有事項など実会議の内容があれば disposition=keep です。
confidence は 0 から 1、reason は改行なしの簡潔な日本語にしてください。"""


def validate_result(value: object, *, title_key: str = "title", exact: bool = True) -> dict[str, Any]:
    keys = {title_key, "disposition", "confidence", "reason"}
    if not isinstance(value, dict) or (exact and set(value) != keys) or not keys.issubset(value):
        raise ValueError(f"triage result must contain exactly {', '.join(sorted(keys))}")
    title = value[title_key]
    disposition = value["disposition"]
    confidence = value["confidence"]
    reason = value["reason"]
    if (
        not isinstance(title, str)
        or title != title.strip()
        or not title
        or len(title) > 40
        or "\n" in title
        or not TITLE_RE.search(title)
        or title in GENERIC_TITLES
        or title.endswith(("。", "！", "？", ".", "!", "?"))
    ):
        raise ValueError("title must be a concrete, short Japanese phrase")
    if disposition not in {"keep", "test"}:
        raise ValueError("disposition must be keep or test")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
    ):
        raise ValueError("confidence must be a finite number from 0 through 1")
    if not isinstance(reason, str) or reason != reason.strip() or not reason or len(reason) > 200 or "\n" in reason:
        raise ValueError("reason must be one non-empty line of at most 200 characters")
    return {
        "title": title,
        "disposition": disposition,
        "confidence": float(confidence),
        "reason": reason,
    }


def from_interpretation(value: object) -> dict[str, Any]:
    return validate_result(value, title_key="meeting_title", exact=False)


def insufficient_transcript_result() -> dict[str, Any]:
    return {
        "title": "文字起こし不十分な録音テスト",
        "disposition": "test",
        "confidence": 1.0,
        "reason": "文字起こしが空、または判定に足りない極小内容です",
    }
