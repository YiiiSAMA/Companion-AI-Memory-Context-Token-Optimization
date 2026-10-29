"""Small, dependency-free token estimation helpers.

The proxy must work without tiktoken.  The estimator is deliberately a little
conservative: over-estimating is safer than silently overflowing a model's
context window.  Every function is isolated so a real tokenizer can replace it
later.
"""

from __future__ import annotations

import json
import re
from typing import Any

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_SPACE_RE = re.compile(r"\s+")
_MESSAGE_OVERHEAD = 4


def text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        if parts:
            return "\n".join(parts)
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def estimate_tokens(value: Any) -> int:
    """Estimate tokens for text or an arbitrary JSON-ish value.

    Chinese characters are counted close to one token each, English words at
    roughly 1.3 tokens, and punctuation/whitespace at a fraction of a token.
    """
    text = text_of(value)
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    words = len(_WORD_RE.findall(text))
    word_chars = sum(len(m.group(0)) for m in _WORD_RE.finditer(text))
    other = max(0, len(text) - cjk - word_chars)
    estimate = cjk * 0.95 + words * 1.25 + other * 0.28
    return max(1, int(round(estimate)))


def estimate_message_tokens(message: dict) -> int:
    total = _MESSAGE_OVERHEAD
    total += estimate_tokens(message.get("role", ""))
    total += estimate_tokens(message.get("content"))
    if message.get("tool_calls"):
        total += estimate_tokens(message["tool_calls"])
    if message.get("name"):
        total += estimate_tokens(message["name"])
    return total


def estimate_messages_tokens(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(m) for m in messages if isinstance(m, dict))


def truncate_text(value: Any, max_tokens: int, suffix: str = "…") -> str:
    """Truncate text to approximately ``max_tokens`` without cutting mid-word.

    The result may be slightly under the budget.  Returns the original text when
    it already fits.
    """
    text = text_of(value)
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text

    # Binary search on character count keeps this deterministic and cheap.
    low, high = 0, len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip()
        if estimate_tokens(candidate + suffix) <= max_tokens:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    if not best:
        return suffix if estimate_tokens(suffix) <= max_tokens else ""
    # Prefer cutting at a sentence/paragraph boundary when one is nearby.
    for marker in ("\n", "。", "！", "？", ".", "!", "?"):
        idx = best.rfind(marker)
        if idx >= int(len(best) * 0.55):
            best = best[: idx + len(marker)].rstrip()
            break
    return best + suffix


def compact_whitespace(text: str) -> str:
    return _SPACE_RE.sub(" ", text or "").strip()
