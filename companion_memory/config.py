"""Configuration for the Companion Memory Proxy."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    # Local proxy
    host: str = "127.0.0.1"
    port: int = 8787
    db_path: str = "./data/memory.db"
    proxy_api_key: str = ""

    # Upstream OpenAI-compatible API
    upstream_base_url: str = "https://api.deepseek.com/v1"
    upstream_api_key: str = ""
    upstream_model: str = "deepseek-chat"
    upstream_timeout: int = 120

    # Context budget
    max_prompt_tokens: int = 6000
    reserve_output_tokens: int = 800
    recent_turns: int = 8
    memory_top_k: int = 6
    max_memory_tokens: int = 1100
    max_summary_chars: int = 600

    # Memory behavior
    consolidation_threshold: int = 30
    consolidation_batch: int = 20
    consolidation_mode: str = "extractive"  # extractive | llm
    summary_model: str = ""
    enable_fact_extraction: bool = True
    enable_consolidation: bool = True

    # Defaults
    default_conversation_id: str = "default"
    log_level: str = "INFO"

    # Internal
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        values = os.environ if env is None else env
        cfg = cls(
            host=values.get("MEMORY_HOST", "127.0.0.1"),
            port=_env_int("MEMORY_PORT", 8787),
            db_path=values.get("MEMORY_DB", "./data/memory.db"),
            proxy_api_key=values.get("MEMORY_PROXY_KEY", ""),
            upstream_base_url=values.get("UPSTREAM_BASE_URL", "https://api.deepseek.com/v1"),
            upstream_api_key=values.get("UPSTREAM_API_KEY", ""),
            upstream_model=values.get("UPSTREAM_MODEL", "deepseek-chat"),
            upstream_timeout=_env_int("UPSTREAM_TIMEOUT", 120),
            max_prompt_tokens=_env_int("MEMORY_MAX_PROMPT_TOKENS", 6000),
            reserve_output_tokens=_env_int("MEMORY_RESERVE_OUTPUT_TOKENS", 800),
            recent_turns=_env_int("MEMORY_RECENT_TURNS", 8),
            memory_top_k=_env_int("MEMORY_TOP_K", 6),
            max_memory_tokens=_env_int("MEMORY_MAX_MEMORY_TOKENS", 1100),
            max_summary_chars=_env_int("MEMORY_MAX_SUMMARY_CHARS", 600),
            consolidation_threshold=_env_int("MEMORY_CONSOLIDATION_THRESHOLD", 30),
            consolidation_batch=_env_int("MEMORY_CONSOLIDATION_BATCH", 20),
            consolidation_mode=values.get("MEMORY_CONSOLIDATION_MODE", "extractive"),
            summary_model=values.get("MEMORY_SUMMARY_MODEL", ""),
            enable_fact_extraction=values.get("MEMORY_ENABLE_FACT_EXTRACTION", "1") not in ("0", "false", "False"),
            enable_consolidation=values.get("MEMORY_ENABLE_CONSOLIDATION", "1") not in ("0", "false", "False"),
            default_conversation_id=values.get("MEMORY_DEFAULT_CONVERSATION", "default"),
            log_level=values.get("MEMORY_LOG_LEVEL", "INFO"),
        )
        return cfg

    def context_budget(self) -> int:
        """Prompt budget after reserving room for the model's answer."""
        return max(512, self.max_prompt_tokens - self.reserve_output_tokens)

    def effective_summary_model(self) -> str:
        return self.summary_model or self.upstream_model
