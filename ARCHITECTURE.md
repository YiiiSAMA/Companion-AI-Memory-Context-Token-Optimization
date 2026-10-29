# Architecture

## 1. Design goal

Long-running companion chats have two conflicting needs:

1. **Continuity** — the model must remember people, preferences, promises, and recent events.
2. **Cost/latency** — every token in the prompt is paid for on every turn.

The proxy treats context as a managed memory system, not a flat transcript.

## 2. Request pipeline

```text
client request
   │
   ├─ authenticate (optional proxy key)
   ├─ upsert raw turns into messages
   ├─ rule-extract semantic facts from user turns
   ├─ consolidate old turns when threshold is reached
   ├─ retrieve core + relevant memories (FTS5)
   ├─ build budgeted prompt
   ├─ forward to upstream (JSON or SSE)
   ├─ capture assistant response
   └─ log token metrics
```

## 3. Context budget

Let:

- `B` = `MEMORY_MAX_PROMPT_TOKENS - MEMORY_RESERVE_OUTPUT_TOKENS`
- `S` = system/persona tokens
- `M` = retrieved memory block tokens
- `R` = recent raw turns

The builder keeps `S + M + R <= B`.

Order of sacrifice:

1. Reject nothing unless the latest user message itself is oversized.
2. Trim the memory block to its own budget (`MEMORY_MAX_MEMORY_TOKENS`).
3. Drop the oldest raw turns first.
4. If one newest message is oversized, truncate that message rather than dropping it.
5. Truncate the system prompt only when it alone exceeds ~55% of the budget.

**Important anti-amnesia rule:** if the full history fits in `B`, no raw turn is
dropped. Compression only happens under budget pressure.

## 4. Memory types

| Type | Table | Purpose | Example |
|---|---|---|---|
| Working | `messages` | verbatim recent turns | “今天想喝冰美式” |
| Semantic | `memories(kind=identity/fact/preference)` | stable facts | “名字：条猫” |
| Episodic summary | `memories(kind=summary)` | compressed older span | “10-29：聊了工作和晚饭” |
| Request metrics | `request_logs` | observability | original/optimized tokens |

## 5. Fact extraction

The MVP uses deterministic regex patterns:

- name
- birthday
- likes / dislikes
- location
- job
- explicit “记住…”

Facts with a fixed key (`identity:name`, `fact:job`, …) supersede older active
values. Preference keys include a content hash so multiple likes can coexist.

This layer costs **zero extra model tokens**. It is intentionally conservative:
missing a fact is better than inventing one.

## 6. Retrieval

`memories_fts` is an FTS5 table. CJK text is character-segmented before indexing
and querying, so a query like “冰美式” becomes `冰 美 式` and works without a
Chinese tokenizer.

Retrieval order:

1. Core memories: pinned identity + preferences.
2. High-precision FTS query: all terms must match.
3. If fewer than `limit`, widen to OR and rank with BM25.
4. Fallback `LIKE` if FTS cannot parse the query.
5. Budget the final memory block.

Accessed memories update `access_count` and `last_accessed_at`, which can later
feed a review/decay score.

## 7. Consolidation

When unconsolidated user/assistant turns exceed the threshold, the oldest batch
is summarized and marked `consolidated=1`.

The MVP summarizer is extractive:

- group by date
- keep user-side clauses
- cap at `MEMORY_MAX_SUMMARY_CHARS`

This avoids an extra LLM call per turn. An LLM summarizer is a planned option
for higher-quality compression.

## 8. Streaming

The proxy passes SSE bytes through unchanged, while parsing `delta.content` and
the final `usage` chunk to persist the assistant turn and metrics. It does not
buffer the whole response before sending.

## 9. Observability

Every request logs:

- original prompt tokens
- optimized prompt tokens
- saved tokens
- memory block tokens / memory hits
- dropped raw messages
- upstream usage (if provided)
- latency and status

Endpoints:

```text
GET /v1/memory/stats?conversation_id=...
GET /v1/memory/search?q=...&conversation_id=...
GET /v1/memory/recent?conversation_id=...
GET /v1/memory/export?conversation_id=...
```

## 10. Known limitations

- Conversation ID must be supplied by the client (`X-Conversation-Id`) or
  defaults to one global conversation.
- Token counts are approximate without a real tokenizer.
- Fact extraction is rule-based; nuanced facts need the planned LLM extractor.
- Tool-call histories are forwarded but not deeply reasoned about during
  consolidation.
- One SQLite writer per process; suitable for a personal companion, not a
  multi-tenant SaaS.
