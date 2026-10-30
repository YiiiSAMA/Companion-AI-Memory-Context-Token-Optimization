"""Context assembly: recent working memory + retrieved long-term memory."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .db import MemoryStore, message_text
from .tokens import estimate_messages_tokens, estimate_tokens, text_of, truncate_text


def sanitize_tool_messages(messages: list[dict]) -> list[dict]:
    """Drop orphaned tool results created by budget trimming.

    OpenAI-compatible APIs reject a ``tool`` message unless a preceding
    assistant message contains the matching ``tool_calls`` entry.  When old
    turns are dropped, a tool result can survive without its assistant call;
    that is invalid and causes a 400 from the upstream model.
    """
    filtered: list[dict] = []
    valid_call_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            filtered.append(message)
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    valid_call_ids.add(str(call["id"]))
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id is not None and str(call_id) in valid_call_ids:
                filtered.append(message)
            # An orphaned tool result is silently removed.
        else:
            filtered.append(message)
    return filtered


@dataclass
class PreparedContext:
    messages: list[dict]
    original_tokens: int
    optimized_tokens: int
    memory_tokens: int = 0
    memory_count: int = 0
    recent_count: int = 0
    dropped_messages: int = 0
    self_injected: bool = False
    self_tokens: int = 0
    memories: list[dict] = field(default_factory=list)

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.optimized_tokens)

    @property
    def compression_ratio(self) -> float:
        if not self.original_tokens:
            return 0.0
        return round(self.saved_tokens / self.original_tokens, 4)


class ContextBuilder:
    def __init__(self, store: MemoryStore, config: Config):
        self.store = store
        self.config = config

    def latest_user_text(self, messages: list[dict]) -> str:
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                return message_text(message)
        return ""

    def render_memory_block(self, memories: list[dict]) -> str:
        if not memories:
            return ""
        lines = [
            "[长期记忆｜由历史对话整理，可能不完整；与当前对话冲突时，以当前对话为准]"
        ]
        for memory in memories:
            lines.append(self.store.memory_line(memory))
        lines.append("[/长期记忆]")
        return "\n".join(lines)

    def build(
        self,
        conversation_id: str,
        messages: list[dict],
        *,
        query: str | None = None,
        inject_self: bool = False,
    ) -> PreparedContext:
        """Build a token-bounded request while preserving the latest user turn."""
        original_tokens = estimate_messages_tokens(messages)
        budget = self.config.context_budget()

        systems = [dict(m) for m in messages if isinstance(m, dict) and m.get("role") == "system"]
        dialogue = [dict(m) for m in messages if isinstance(m, dict) and m.get("role") != "system"]

        self_tokens = 0
        self_injected = False
        if inject_self and self.config.self_continuity_enabled:
            note = self.store.latest_self_note(conversation_id)
            if note:
                note_text = str(note.get("content") or "").strip()
                note_text = truncate_text(note_text, self.config.self_note_max_tokens)
                if note_text:
                    self_block = "[自我连续性｜昨天的我]\n" + note_text + "\n[/自我连续性]"
                    self_tokens = estimate_tokens(self_block)
                    if systems:
                        systems[-1]["content"] = (text_of(systems[-1].get("content")) + "\n\n" + self_block).strip()
                    else:
                        systems = [{"role": "system", "content": self_block}]
                    self_injected = True

        query = (query if query is not None else self.latest_user_text(messages)).strip()
        memories = self.store.search_memories(
            conversation_id,
            query,
            limit=self.config.memory_top_k,
            max_tokens=self.config.max_memory_tokens,
        )
        memory_block = self.render_memory_block(memories)
        memory_tokens = estimate_tokens(memory_block)

        # If the raw history already fits, never let a memory block force a raw
        # turn out.  Use only spare budget for memory; otherwise skip it.  This
        # keeps "fits in budget => no amnesia" true even when retrieval is noisy.
        if original_tokens <= budget and memory_block:
            spare = max(0, budget - original_tokens - 16)
            if memory_tokens > spare:
                if spare >= 80:
                    memory_block = truncate_text(memory_block, spare, suffix="\n[/长期记忆]")
                    memory_tokens = estimate_tokens(memory_block)
                else:
                    memory_block = ""
                    memory_tokens = 0
                    memories = []

        # Keep the system contract first. If it is unexpectedly huge, trim it
        # before sacrificing the newest user message.
        system_tokens = estimate_messages_tokens(systems)
        if systems and system_tokens > int(budget * 0.55):
            keep_tokens = max(256, int(budget * 0.55))
            systems[-1]["content"] = truncate_text(systems[-1].get("content", ""), keep_tokens)
            system_tokens = estimate_messages_tokens(systems)

        available_for_memory = max(0, budget - system_tokens - self.config.reserve_output_tokens // 2)
        if memory_block and memory_tokens > available_for_memory:
            memory_block = truncate_text(memory_block, available_for_memory, suffix="\n[/长期记忆]")
            memory_tokens = estimate_tokens(memory_block)
        if memory_block:
            # One system message is friendlier to providers that dislike multiple
            # system turns. Keep the persona first, memory after it.
            if systems:
                systems[-1]["content"] = (text_of(systems[-1].get("content")) + "\n\n" + memory_block).strip()
            else:
                systems = [{"role": "system", "content": memory_block}]
        else:
            memory_tokens = 0

        base_tokens = estimate_messages_tokens(systems)
        room = max(0, budget - base_tokens)

        # Keep the full working history while it fits.  Only when the budget is
        # exceeded do we drop the oldest turns and fall back to long-term memory.
        # This is the key anti-amnesia rule: save tokens on long chats, not by
        # silently deleting a conversation that already fits.
        recent_candidates = dialogue

        selected: list[dict] = []
        selected_tokens = 0
        for message in reversed(recent_candidates):
            cost = estimate_messages_tokens([message])
            if selected and selected_tokens + cost > room:
                break
            if not selected and cost > room:
                # A single oversized newest message: keep a compact version.
                message = dict(message)
                message["content"] = truncate_text(
                    message.get("content", ""),
                    max(64, room - 16),
                    suffix="…",
                )
                cost = estimate_messages_tokens([message])
            selected.insert(0, message)
            selected_tokens += cost

        # Guarantee the latest user message survives even if older turns were
        # dropped for budget reasons.
        latest_user = next(
            (m for m in reversed(dialogue) if m.get("role") == "user"),
            None,
        )
        latest_selected = bool(
            latest_user is not None
            and selected
            and selected[-1].get("role") == "user"
        )
        if latest_user is not None and not latest_selected:
            compact = dict(latest_user)
            compact["content"] = truncate_text(
                compact.get("content", ""),
                max(64, budget - base_tokens - selected_tokens - 16),
                suffix="…",
            )
            selected.append(compact)
            selected_tokens += estimate_messages_tokens([compact])

        sanitized_selected = sanitize_tool_messages(selected)
        assembled = systems + sanitized_selected
        optimized_tokens = estimate_messages_tokens(assembled)
        dropped = max(0, len(dialogue) - len(sanitized_selected))

        return PreparedContext(
            messages=assembled,
            original_tokens=original_tokens,
            optimized_tokens=optimized_tokens,
            memory_tokens=memory_tokens,
            memory_count=len(memories),
            recent_count=len(selected),
            dropped_messages=dropped,
            self_injected=self_injected,
            self_tokens=self_tokens,
            memories=memories,
        )
