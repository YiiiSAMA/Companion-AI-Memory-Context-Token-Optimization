import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from companion_memory.config import Config
from companion_memory.server import build_server


class MockUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.last_request = payload
        messages = payload.get("messages") or []
        last_user = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user = str(message.get("content", ""))
                break
        content = "mock reply: " + last_user
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for piece in ["mock ", "stream ", "reply"]:
                self.wfile.write(
                    f'data: {json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False)}\n\n'.encode()
                )
                self.wfile.flush()
            self.wfile.write(
                f'data: {json.dumps({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3}})}\n\n'.encode()
            )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = json.dumps(
            {
                "id": "mock",
                "object": "chat.completion",
                "model": payload.get("model", "mock"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
            },
            ensure_ascii=False,
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mock = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
        self.mock.daemon_threads = True
        self.mock_thread = threading.Thread(target=self.mock.serve_forever, daemon=True)
        self.mock_thread.start()

        self.config = Config(
            host="127.0.0.1",
            port=0,
            db_path=os.path.join(self.tmp.name, "memory.db"),
            proxy_api_key="proxy-key",
            upstream_base_url=f"http://127.0.0.1:{self.mock.server_address[1]}/v1",
            upstream_api_key="upstream-key",
            upstream_model="mock-model",
            max_prompt_tokens=1200,
            reserve_output_tokens=100,
            recent_turns=2,
            memory_top_k=6,
            enable_consolidation=True,
            consolidation_threshold=20,
            consolidation_batch=8,
        )
        self.proxy = build_server(self.config)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        self.base = f"http://127.0.0.1:{self.proxy.server_address[1]}"

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.proxy.RequestHandlerClass.store.close()
        self.mock.shutdown()
        self.mock.server_close()
        self.tmp.cleanup()

    def post(self, payload, stream=False):
        request = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer proxy-key",
                "X-Conversation-Id": "qq-main",
            },
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=15)

    def test_proxy_injects_memory_and_reports_savings(self):
        messages = [{"role": "system", "content": "你是知言。"}, {"role": "user", "content": "我叫条猫，我喜欢喝冰美式。"}]
        messages.append({"role": "assistant", "content": "记住了。"})
        for i in range(30):
            messages.append({"role": "user", "content": f"普通闲聊第{i}轮，" + "今天发生了一些很普通的事情。" * 3})
            messages.append({"role": "assistant", "content": "嗯，我在听。"})
        messages.append({"role": "user", "content": "今天想喝冰美式，顺便问一下晚饭吃什么。"})

        response = self.post({"model": "mock-model", "messages": messages})
        self.assertEqual(response.status, 200)
        saved = int(response.headers["X-Memory-Saved-Tokens"])
        self.assertGreater(saved, 0)
        data = json.loads(response.read().decode("utf-8"))
        self.assertIn("mock reply", data["choices"][0]["message"]["content"])
        upstream_messages = self.mock.last_request["messages"]
        system_text = "\n".join(m.get("content", "") for m in upstream_messages if m.get("role") == "system")
        self.assertIn("冰美式", system_text)

        stats_req = urllib.request.Request(
            self.base + "/v1/memory/stats?conversation_id=qq-main",
            headers={"Authorization": "Bearer proxy-key"},
        )
        stats = json.loads(urllib.request.urlopen(stats_req, timeout=10).read().decode("utf-8"))
        self.assertGreater(stats["saved_tokens"], 0)
        self.assertGreaterEqual(stats["memories_active"], 1)



    def test_llm_summarizer_path_falls_back_to_upstream(self):
        self.config.consolidation_mode = "llm"
        self.config.summary_model = "mock-model"
        messages = [
            {"role": "system", "content": "你是知言。"},
            {"role": "user", "content": "我叫条猫，我喜欢喝冰美式。"},
            {"role": "assistant", "content": "记住了。"},
        ]
        for i in range(70):
            messages.append({"role": "user", "content": f"第{i}轮：今天工作很累，晚上吃了面。"})
            messages.append({"role": "assistant", "content": "先休息一下。"})
        messages.append({"role": "user", "content": "今天想喝冰美式。"})
        response = self.post({"model": "mock-model", "messages": messages})
        self.assertEqual(response.status, 200)
        response.read()
        stats_req = urllib.request.Request(
            self.base + "/v1/memory/stats?conversation_id=qq-main",
            headers={"Authorization": "Bearer proxy-key"},
        )
        stats = json.loads(urllib.request.urlopen(stats_req, timeout=10).read().decode("utf-8"))
        self.assertGreaterEqual(stats["memory_kinds"].get("summary", 0), 1)

    def test_long_request_consolidates_before_assembly(self):
        messages = [
            {"role": "system", "content": "你是知言。"},
            {"role": "user", "content": "我叫条猫，我喜欢喝冰美式。"},
            {"role": "assistant", "content": "记住了。"},
        ]
        for i in range(80):
            messages.append({"role": "user", "content": f"第{i}轮：今天工作很累，晚上吃了面，回家路上有点冷。"})
            messages.append({"role": "assistant", "content": "先休息一下，到家告诉我。"})
        messages.append({"role": "user", "content": "今天还是想喝冰美式，晚饭吃什么好？"})
        response = self.post({"model": "mock-model", "messages": messages})
        self.assertEqual(response.status, 200)
        response.read()
        stats_req = urllib.request.Request(
            self.base + "/v1/memory/stats?conversation_id=qq-main",
            headers={"Authorization": "Bearer proxy-key"},
        )
        stats = json.loads(urllib.request.urlopen(stats_req, timeout=10).read().decode("utf-8"))
        self.assertGreaterEqual(stats["memory_kinds"].get("summary", 0), 1)
        system_text = "\n".join(
            m.get("content", "") for m in self.mock.last_request["messages"] if m.get("role") == "system"
        )
        self.assertIn("[长期记忆", system_text)

    def test_streaming_passthrough_and_storage(self):
        payload = {
            "model": "mock-model",
            "stream": True,
            "messages": [
                {"role": "system", "content": "你是知言。"},
                {"role": "user", "content": "在吗"},
            ],
        }
        response = self.post(payload)
        body = response.read().decode("utf-8")
        self.assertIn("[DONE]", body)
        parts = []
        for line in body.splitlines():
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if not data or data == "[DONE]":
                continue
            chunk = json.loads(data)
            choices = chunk.get("choices") or []
            if choices:
                parts.append((choices[0].get("delta") or {}).get("content") or "")
        self.assertEqual("".join(parts), "mock stream reply")

    def test_auth_required_on_memory_endpoint(self):
        request = urllib.request.Request(self.base + "/v1/memory/stats")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
