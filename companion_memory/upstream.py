"""Minimal OpenAI-compatible upstream client (no third-party dependencies)."""

from __future__ import annotations

import http.client
import json
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import Config


def chat_url(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


@dataclass
class UpstreamResponse:
    conn: http.client.HTTPConnection
    response: http.client.HTTPResponse
    status: int
    content_type: str


class UpstreamClient:
    def __init__(self, config: Config):
        self.config = config
        self.url = chat_url(config.upstream_base_url)
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"Invalid UPSTREAM_BASE_URL: {config.upstream_base_url}")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port
        self.path = parts.path or "/"
        if parts.query:
            self.path += "?" + parts.query

    def open(self, payload: dict, *, stream: bool) -> UpstreamResponse:
        if not self.config.upstream_api_key:
            raise RuntimeError(
                "UPSTREAM_API_KEY is empty. Set it in the environment or .env file."
            )
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.upstream_api_key}",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "companion-memory-proxy/0.1",
        }
        timeout = max(5, int(self.config.upstream_timeout))
        if self.scheme == "https":
            conn = http.client.HTTPSConnection(
                self.host,
                self.port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        conn.request("POST", self.path, body=body, headers=headers)
        response = conn.getresponse()
        return UpstreamResponse(conn, response, response.status, response.getheader("Content-Type", ""))


def parse_sse_line(line: str) -> tuple[str, dict | None]:
    """Return (delta_text, usage) from one SSE line.

    ``usage`` may arrive on the final data chunk when stream_options.include_usage
    is enabled.  Non-JSON lines (comments, heartbeat) are ignored.
    """
    if not line:
        return "", None
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return "", None
    data = stripped[5:].strip()
    if not data or data == "[DONE]":
        return "", None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return "", None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        return "", usage
    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    text = delta.get("content")
    if text is None:
        message = choice.get("message") or {}
        text = message.get("content")
    return text or "", usage
