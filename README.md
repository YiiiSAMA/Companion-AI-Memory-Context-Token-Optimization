# Companion-AI-Memory-Context-Token-Optimization

> Human-inspired memory and context/token optimization for AI companions.
>
> 让长期陪伴型 AI 在节省 token 的同时，不把重要的人和事忘掉。

这是一个 **OpenAI 兼容的 Memory Proxy**。它坐在聊天客户端和模型 API 之间，负责：

- 保存完整对话（情景记忆）
- 提取稳定事实和偏好（语义记忆）
- 把旧对话压缩成摘要（巩固）
- 每轮按 token 预算检索相关记忆
- 在预算内保留完整近期上下文，超预算才裁剪
- 记录每次请求节省了多少 token、命中了哪些记忆

它不绑定 AstrBot。任何使用 OpenAI Chat Completions 协议的客户端都可以接入。

设计细节见 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 为什么不是简单截断历史

普通做法是保留最近 N 条消息，或者超过阈值就 summarize。它们的问题：

| 做法 | 问题 |
|---|---|
| 只保留最近 N 条 | 更早的事实、偏好、承诺会消失 |
| 每轮都总结 | 额外 token 开销，而且总结会丢细节 |
| 向量库全量召回 | 召回噪声大，仍然占用大量上下文 |
| 固定窗口 | 窗口内的对话明明放得下，却被提前裁掉 |

本项目的策略：

1. **预算内不裁剪**：完整历史能放进预算，就完整保留。
2. **超预算才裁剪**：保留最新工作记忆，旧的交给长期记忆。
3. **语义事实独立保存**：名字、生日、喜好、厌恶、工作等不会被摘要吞掉。
4. **冲突事实可替换**：换工作、换城市时，旧事实标记为 `superseded`，不会同时注入矛盾信息。
5. **可观察**：每次请求都记录原始 token、优化后 token、节省比例、记忆命中数。

---

## 记忆模型

```text
                  ┌────────────────────────────┐
                  │        Working Memory      │
                  │  最近对话，预算内完整保留  │
                  └─────────────┬──────────────┘
                                │ overflow
                                ▼
┌───────────────┐      ┌────────────────────┐
│ Episodic      │      │ Consolidation      │
│ raw messages  │─────▶│ extractive summary │
└───────────────┘      └─────────┬──────────┘
                                 │
                                 ▼
                       ┌────────────────────┐
                       │ Semantic Memory    │
                       │ facts / preferences│
                       │ supersede / pinned │
                       └─────────┬──────────┘
                                 │ retrieval
                                 ▼
                       ┌────────────────────┐
                       │ Budgeted Context   │
                       │ system + memory +  │
                       │ recent turns       │
                       └────────────────────┘
```

### 工作记忆

- `messages` 表保存原始 user/assistant/tool 轮次。
- 预算内：全部保留。
- 超预算：从最新消息向前保留，直到预算用尽。

### 语义记忆

规则提取（MVP 默认，零额外 token）：

- 名字：`我叫…` / `我的名字是…`
- 生日：`我的生日是…`
- 喜欢 / 不喜欢：`我喜欢…` / `我讨厌…`
- 地点 / 工作：`我住在…` / `我在…工作`
- 显式记忆：`记住…` / `帮我记着…`

同一 key 的新事实会替换旧事实。例如：

```text
我在阿里工作  -> active
我在字节工作  -> active
阿里那条      -> superseded
```

### 巩固与摘要

- 默认 `extractive`：从旧对话中抽取用户侧关键句，生成紧凑摘要。超预算请求会在组装前先做一次廉价摘要，避免第一条超长历史被直接裁掉。

设置 `MEMORY_CONSOLIDATION_MODE=llm` 可以让上游模型做更高质量的中文摘要；摘要调用失败会自动回退到抽取式摘要。这会增加一次模型调用，适合对记忆质量要求更高的场景。
- 摘要写入 `summary` 记忆，并标记原消息 `consolidated=1`。
- 原消息不会删除，只是不再每轮注入；需要时可以导出或重建摘要。

### 检索

- SQLite FTS5 索引，CJK 字符逐字切分，兼容中文短语。
- 先做高精度 AND 查询，再放宽到 OR，按 BM25 排序。
- 核心事实（名字、生日、偏好）始终优先注入。
- 记忆块有独立 token 预算，不会挤掉最新用户消息。

---

## 快速开始

要求：Python 3.10+，无需第三方依赖。

```bash
cd companion-ai-memory
cp .env.example .env
# 编辑 .env，填入 UPSTREAM_API_KEY 和 MEMORY_PROXY_KEY
python -m companion_memory serve
```

默认监听：

```text
http://127.0.0.1:8787
```

健康检查：

```bash
curl http://127.0.0.1:8787/health
```

发送一条 OpenAI 兼容请求：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -H "X-Conversation-Id: qq-main" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "system", "content": "你是知言。"},
      {"role": "user", "content": "我叫条猫，我喜欢喝冰美式。"},
      {"role": "user", "content": "今天想喝冰美式。"}
    ]
  }'
```

查看省了多少 token：

```bash
curl "http://127.0.0.1:8787/v1/memory/stats?conversation_id=qq-main" \
  -H "Authorization: Bearer change-me"
```

搜索记忆：

```bash
curl "http://127.0.0.1:8787/v1/memory/search?conversation_id=qq-main&q=冰美式" \
  -H "Authorization: Bearer change-me"
```

---

## AstrBot 接入

AstrBot 使用 OpenAI 兼容 Provider 时，把请求指向本代理即可。

| 配置项 | 值 |
|---|---|
| API Base | `http://127.0.0.1:8787/v1` |
| API Key | `.env` 里的 `MEMORY_PROXY_KEY` |
| Model | 与上游一致，例如 `deepseek-chat` |

如果 AstrBot 在 Docker 中，代理在宿主机：

- macOS / Windows：`http://host.docker.internal:8787/v1`
- Linux：`http://172.17.0.1:8787/v1`
- 同一 Docker 网络：`http://companion-memory:8787/v1`

建议在 Provider 的 `custom_headers` 中加：

```json
{"X-Conversation-Id": "qq-main"}
```

当前 MVP 默认按会话 ID 隔离记忆；单用户陪伴场景用一个固定 ID 即可。多用户按 QQ 号隔离需要客户端把用户 ID 传进来，属于下一阶段。

---

## 实测效果

合成对话，预算 `6000` prompt tokens，保留输出 `800` tokens：

| 历史规模 | 原始 tokens | 优化后 | 节省 | 比例 |
|---|---:|---:|---:|---:|
| 60 轮 | 3,435 | 3,489 | 0 | 0% |
| 1,500 轮 | 84,075 | 5,166 | 78,909 | **93.9%** |

第一行是刻意保留的：**预算内不做无意义裁剪**。  
第二行说明长对话才会触发压缩；代理会先固化旧对话摘要，再裁剪原文，具体数字取决于消息长度和模型 tokenizer。

运行测试：

```bash
python -m unittest discover -s tests -v
```

测试覆盖：

- 事实提取与中文 FTS 检索
- 新事实替换旧事实
- 预算裁剪但保留最新用户消息
- 摘要巩固与消息标记
- 代理注入记忆并报告节省
- SSE 流式透传
- 代理鉴权

---

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MEMORY_HOST` | `127.0.0.1` | 监听地址 |
| `MEMORY_PORT` | `8787` | 监听端口 |
| `MEMORY_DB` | `./data/memory.db` | SQLite 路径 |
| `MEMORY_PROXY_KEY` | 空 | 客户端访问代理的 Bearer Key |
| `UPSTREAM_BASE_URL` | `https://api.deepseek.com/v1` | 上游 OpenAI 兼容地址 |
| `UPSTREAM_API_KEY` | 空 | 上游 API Key |
| `UPSTREAM_MODEL` | `deepseek-chat` | 默认模型 |
| `MEMORY_MAX_PROMPT_TOKENS` | `6000` | 上下文总预算 |
| `MEMORY_RESERVE_OUTPUT_TOKENS` | `800` | 为回复预留 |
| `MEMORY_RECENT_TURNS` | `8` | 巩固时保留的近期轮数 |
| `MEMORY_TOP_K` | `6` | 记忆检索数量 |
| `MEMORY_MAX_MEMORY_TOKENS` | `1100` | 记忆块预算 |
| `MEMORY_CONSOLIDATION_THRESHOLD` | `30` | 触发巩固的消息数 |
| `MEMORY_CONSOLIDATION_BATCH` | `20` | 每次巩固的消息数 |
| `MEMORY_CONSOLIDATION_MODE` | `extractive` | `extractive` 或 `llm` |
| `MEMORY_SUMMARY_MODEL` | 空 | LLM 摘要模型，空则用上游模型 |

---

## CLI

```bash
# 统计
python -m companion_memory stats --conversation-id qq-main

# 搜索
python -m companion_memory search "冰美式" --conversation-id qq-main

# 导出完整对话与记忆
python -m companion_memory export --conversation-id qq-main

# 手动巩固
python -m companion_memory consolidate --conversation-id qq-main

# 重建 FTS 索引
python -m companion_memory rebuild-fts
```

---

## Docker

```bash
cp .env.example .env
docker compose up -d --build
```

Compose 默认只把端口绑定到 `127.0.0.1`，不会直接暴露到公网。

---

## 安全与隐私

- 代理会保存**完整对话原文**和提取出的记忆，数据库在 `data/memory.db`。
- 默认只监听 `127.0.0.1`；如果要跨机器访问，请设置 `MEMORY_PROXY_KEY` 并用防火墙限制来源。
- 上游 API Key 只存在代理环境变量中，不会返回给客户端。
- 日志默认不打印对话正文；调试时也不要打开完整请求体日志。
- 这是陪伴型记忆系统，不应用于未经同意收集他人隐私。

---

## 当前限制 / Roadmap

MVP 已实现：

- [x] 预算内完整保留
- [x] FTS5 中文检索
- [x] 规则事实提取
- [x] 事实替换
- [x] 抽取式摘要
- [x] SSE 流式代理
- [x] token 节省统计
- [x] AstrBot 接入说明

下一步：

- [ ] 摘要任务后台队列（当前 LLM 摘要是同步调用）
- [ ] 记忆重要性衰减与复习曲线
- [ ] 向量检索 + FTS 混合召回
- [ ] 多用户 / 多会话路由
- [ ] 记忆编辑、删除、pin 的 Web UI
- [ ] 真实 tokenizer（tiktoken）可选接入
- [ ] 对 tool call / 多模态消息的完整支持
- [ ] Docker Compose 与 AstrBot 同网络部署示例
- [ ] 更细粒度的记忆 pin / 编辑 / 删除 API

---

## License

MIT. See [LICENSE](LICENSE).
