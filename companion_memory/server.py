"""HTTP entry point for the Companion Memory Proxy."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Config
from .context import ContextBuilder
from .db import MemoryStore, message_text
from .markdown import MarkdownMirror
from .tokens import estimate_messages_tokens, estimate_tokens, text_of
from .upstream import UpstreamClient, parse_sse_line

LOG = logging.getLogger("companion_memory")
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.:\-]")


def _conversation_id_from(headers: Any, payload: dict, default: str) -> str:
    for name in ("X-Conversation-Id", "X-Session-Id", "X-User-Id"):
        value = headers.get(name)
        if value:
            return _SAFE_ID.sub("_", value.strip())[:128]
    user = payload.get("user")
    if isinstance(user, str) and user.strip():
        return _SAFE_ID.sub("_", user.strip())[:128]
    return default


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "CompanionMemory/0.1"
    protocol_version = "HTTP/1.0"

    config: Config
    store: MemoryStore
    builder: ContextBuilder
    upstream: UpstreamClient
    mirror: MarkdownMirror

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": {"message": message, "type": "proxy_error"}}, status)

    def _authorized(self) -> bool:
        expected = self.config.proxy_api_key
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return bool(token) and hmac.compare_digest(token, expected)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._send_error_json(401, "Invalid proxy API key.")
        return False

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > 20_000_000:
            raise ValueError("Request body too large")
        return self.rfile.read(length)

    # ------------------------------------------------------------------
    # GET routes
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        cid = _SAFE_ID.sub("_", (query.get("conversation_id") or [self.config.default_conversation_id])[0])[:128]

        if parsed.path == "/health":
            self._send_json(
                {
                    "ok": True,
                    "service": "companion-memory-proxy",
                    "version": "0.1.0",
                    "upstream_configured": bool(self.config.upstream_api_key),
                    "db": os.path.abspath(self.config.db_path),
                }
            )
            return

        if parsed.path == "/":
            self._send_json(
                {
                    "name": "Companion AI Memory Proxy",
                    "chat_endpoint": "/v1/chat/completions",
                    "stats": "/v1/memory/stats",
                    "search": "/v1/memory/search?q=...",
                    "export": "/v1/memory/export",
                }
            )
            return

        if not self._require_auth():
            return

        if parsed.path == "/v1/memory/stats":
            self._send_json(self.store.stats(cid))
            return
        if parsed.path == "/v1/memory/search":
            q = (query.get("q") or [""])[0]
            try:
                limit = int((query.get("limit") or ["8"])[0])
            except ValueError:
                limit = 8
            hits = self.store.search_memories(
                cid,
                q,
                limit=max(1, min(limit, 50)),
                max_tokens=self.config.max_memory_tokens,
            )
            self._send_json({"conversation_id": cid, "query": q, "hits": hits})
            return
        if parsed.path == "/v1/memory/export":
            self._send_json(self.store.export_conversation(cid))
            return
        if parsed.path == "/v1/memory/markdown/export":
            try:
                result = self.mirror.export(cid)
                self._send_json({"ok": True, **result})
            except Exception as exc:
                LOG.exception("markdown export failed")
                self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        if parsed.path == "/v1/memory/recent":
            try:
                limit = int((query.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            self._send_json(
                {
                    "conversation_id": cid,
                    "requests": self.store.recent_requests(cid, limit=limit),
                }
            )
            return
        self._send_error_json(404, "Not found")

    # ------------------------------------------------------------------
    # POST /v1/chat/completions
    # ------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/v1/chat/completions":
            self._send_error_json(404, "Not found")
            return
        if not self._require_auth():
            return

        try:
            body = self._read_body()
            payload = json.loads(body.decode("utf-8"))
        except Exception as exc:
            self._send_error_json(400, f"Invalid JSON body: {exc}")
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            self._send_error_json(400, "messages must be a list")
            return

        messages = [m for m in payload["messages"] if isinstance(m, dict)]
        if not messages:
            self._send_error_json(400, "messages is empty")
            return

        cid = _conversation_id_from(self.headers, payload, self.config.default_conversation_id)
        stream = bool(payload.get("stream"))
        model = str(payload.get("model") or self.config.upstream_model)
        started = time.monotonic()

        self.store.upsert_messages(cid, messages)
        latest_user = self.builder.latest_user_text(messages)
        if self.config.enable_fact_extraction:
            # Process the recent user side, not only the newest turn.  This also
            # makes importing an existing history useful on the first request.
            user_texts = [
                message_text(m) for m in messages
                if isinstance(m, dict) and m.get("role") == "user" and message_text(m)
            ][-40:]
            for text in user_texts:
                try:
                    self.store.extract_facts(cid, text)
                except Exception:
                    LOG.exception("fact extraction failed")

        # If this request is already over budget, make a cheap extractive
        # summary of old turns *before* assembling the prompt.  This keeps the
        # first long request from dropping history without any replacement.
        if self.config.enable_consolidation and estimate_messages_tokens(messages) > self.config.context_budget():
            try:
                keep_recent = max(4, self.config.recent_turns * 2)
                pending = self.store.unconsolidated_messages(cid)
                if len(pending) > keep_recent + 4:
                    self.store.consolidate(
                        cid,
                        threshold=4,
                        batch_size=len(pending) - keep_recent,
                        keep_recent=keep_recent,
                        summarizer=self._summarize_batch
                        if self.config.consolidation_mode == "llm"
                        else None,
                    )
            except Exception:
                LOG.exception("pre-build consolidation failed")

        prepared = self.builder.build(cid, messages, query=latest_user)
        payload = dict(payload)
        payload["messages"] = prepared.messages
        if "stream" in payload:
            payload["stream"] = stream

        try:
            upstream = self.upstream.open(payload, stream=stream)
        except Exception as exc:
            LOG.exception("upstream request failed")
            self._send_error_json(502, f"Upstream request failed: {exc}")
            return

        elapsed_ms = int((time.monotonic() - started) * 1000)
        headers = {
            "X-Memory-Conversation": cid,
            "X-Memory-Original-Tokens": str(prepared.original_tokens),
            "X-Memory-Optimized-Tokens": str(prepared.optimized_tokens),
            "X-Memory-Saved-Tokens": str(prepared.saved_tokens),
            "X-Memory-Count": str(prepared.memory_count),
        }

        if upstream.status >= 400:
            error_body = upstream.response.read()
            upstream.conn.close()
            self._send_json_body(error_body, upstream.status, upstream.content_type)
            self.store.log_request(
                cid,
                original_tokens=prepared.original_tokens,
                optimized_tokens=prepared.optimized_tokens,
                memory_tokens=prepared.memory_tokens,
                memory_count=prepared.memory_count,
                dropped_messages=prepared.dropped_messages,
                model=model,
                upstream_status=upstream.status,
                latency_ms=elapsed_ms,
            )
            return

        if stream:
            self._stream_upstream(cid, upstream, prepared, model, headers, started)
        else:
            self._json_upstream(cid, upstream, prepared, model, headers, started)

    # ------------------------------------------------------------------
    # Responses
    # ------------------------------------------------------------------
    def _send_json_body(self, body: bytes, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json_upstream(
        self,
        cid: str,
        upstream: Any,
        prepared: Any,
        model: str,
        extra_headers: dict,
        started: float,
    ) -> None:
        raw = upstream.response.read()
        upstream.conn.close()
        parsed = None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = None
        content = ""
        usage = {}
        if isinstance(parsed, dict):
            choices = parsed.get("choices") or []
            if choices and isinstance(choices[0], dict):
                content = text_of((choices[0].get("message") or {}).get("content"))
            usage = parsed.get("usage") or {}
        if content.strip():
            self.store.add_message(cid, "assistant", content)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        latency_ms = int((time.monotonic() - started) * 1000)
        self.store.log_request(
            cid,
            original_tokens=prepared.original_tokens,
            optimized_tokens=prepared.optimized_tokens,
            memory_tokens=prepared.memory_tokens,
            memory_count=prepared.memory_count,
            dropped_messages=prepared.dropped_messages,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
            upstream_status=upstream.status,
            latency_ms=latency_ms,
        )
        self._maybe_consolidate(cid)
        self._maybe_export_markdown(cid)

        self.send_response(upstream.status)
        self.send_header("Content-Type", upstream.content_type or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        for key, value in extra_headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _stream_upstream(
        self,
        cid: str,
        upstream: Any,
        prepared: Any,
        model: str,
        extra_headers: dict,
        started: float,
    ) -> None:
        self.send_response(upstream.status)
        self.send_header("Content-Type", upstream.content_type or "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        for key, value in extra_headers.items():
            self.send_header(key, value)
        self.end_headers()

        parts: list[str] = []
        usage: dict = {}
        try:
            while True:
                line = upstream.response.readline()
                if not line:
                    break
                try:
                    self.wfile.write(line)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                delta_text, chunk_usage = parse_sse_line(line.decode("utf-8", "replace"))
                if delta_text:
                    parts.append(delta_text)
                if chunk_usage:
                    usage = chunk_usage
        finally:
            upstream.conn.close()

        content = "".join(parts)
        if content.strip():
            self.store.add_message(cid, "assistant", content)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        latency_ms = int((time.monotonic() - started) * 1000)
        self.store.log_request(
            cid,
            original_tokens=prepared.original_tokens,
            optimized_tokens=prepared.optimized_tokens,
            memory_tokens=prepared.memory_tokens,
            memory_count=prepared.memory_count,
            dropped_messages=prepared.dropped_messages,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
            upstream_status=upstream.status,
            latency_ms=latency_ms,
        )
        self._maybe_consolidate(cid)
        self._maybe_export_markdown(cid)
        self.close_connection = True


    def _summarize_batch(self, batch: list[dict]) -> str:
        """Optional LLM summarizer; always falls back to the extractive pass."""
        fallback = self.store.extractive_summary(batch, self.config.max_summary_chars)
        if self.config.consolidation_mode != "llm" or not self.config.upstream_api_key:
            return fallback

        selected = batch[:20] + batch[-60:] if len(batch) > 80 else batch
        lines = []
        for message in selected:
            role = "用户" if message.get("role") == "user" else "知言"
            text = str(message.get("content") or "")[:600]
            if text:
                lines.append(f"{role}：{text}")
        transcript = "\n".join(lines)
        if len(transcript) > 14_000:
            transcript = transcript[:14_000] + "\n…"

        prompt = [
            {
                "role": "system",
                "content": (
                    "你是长期陪伴机器人的记忆整理器。把对话压缩成可长期保存的记忆。"
                    "保留稳定事实、偏好、承诺、重要事件、关系变化和未完成事项；"
                    "删除寒暄、重复和临时情绪。不要编造。用中文，分号分隔，不超过300字。"
                ),
            },
            {"role": "user", "content": transcript},
        ]
        payload = {
            "model": self.config.effective_summary_model(),
            "messages": prompt,
            "temperature": 0.2,
            "max_tokens": 400,
            "stream": False,
        }
        try:
            upstream = self.upstream.open(payload, stream=False)
            raw = upstream.response.read()
            upstream.conn.close()
            if upstream.status >= 400:
                raise RuntimeError(f"summary upstream status {upstream.status}")
            data = json.loads(raw.decode("utf-8"))
            content = text_of((data.get("choices") or [{}])[0].get("message", {}).get("content"))
            return content.strip() or fallback
        except Exception:
            LOG.exception("LLM summarization failed; using extractive fallback")
            return fallback


    def _maybe_export_markdown(self, cid: str) -> None:
        if not self.config.markdown_auto_export:
            return
        try:
            self.mirror.export(cid)
        except Exception:
            LOG.exception("markdown auto export failed")

    def _maybe_consolidate(self, cid: str) -> None:
        if not self.config.enable_consolidation:
            return
        try:
            self.store.consolidate(
                cid,
                threshold=self.config.consolidation_threshold,
                batch_size=self.config.consolidation_batch,
                keep_recent=max(4, self.config.recent_turns * 2),
                summarizer=self._summarize_batch
                if self.config.consolidation_mode == "llm"
                else None,
            )
        except Exception:
            LOG.exception("consolidation failed")


def build_server(config: Config) -> ThreadingHTTPServer:
    store = MemoryStore(config.db_path)
    builder = ContextBuilder(store, config)
    upstream = UpstreamClient(config)
    mirror = MarkdownMirror(store, config)
    handler = type(
        "BoundProxyHandler",
        (ProxyHandler,),
        {
            "config": config,
            "store": store,
            "builder": builder,
            "upstream": upstream,
            "mirror": mirror,
        },
    )
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.daemon_threads = True
    return server


def run(config: Config) -> None:
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = build_server(config)
    LOG.info("listening on http://%s:%s", config.host, config.port)
    LOG.info("db=%s upstream=%s", config.db_path, config.upstream_base_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.RequestHandlerClass.store.close()  # type: ignore[attr-defined]
