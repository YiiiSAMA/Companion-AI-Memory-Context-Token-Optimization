import unittest

from companion_memory.server import is_internal_llm_request, sanitize_plugin_pollution


class InternalRequestTests(unittest.TestCase):
    def test_main_chat_is_not_internal(self):
        messages = [
            {"role": "system", "content": "你是知言。他叫霖。"},
            {"role": "user", "content": "我今天有点累"},
        ]
        self.assertFalse(is_internal_llm_request(messages))

    def test_missing_persona_is_internal(self):
        self.assertTrue(is_internal_llm_request([{"role": "user", "content": "分类"}]))
        self.assertTrue(
            is_internal_llm_request(
                [
                    {"role": "system", "content": "你是一个 JSON 分类器"},
                    {"role": "user", "content": "输出标签"},
                ]
            )
        )

    def test_known_self_learning_prompt_is_internal(self):
        messages = [
            {"role": "system", "content": "你是知言。"},
            {"role": "user", "content": "请分析以下用户消息属于什么类型的交互行为：今天好累"},
        ]
        self.assertTrue(is_internal_llm_request(messages))


    def test_pollution_is_removed_from_incoming_history(self):
        messages = [
            {"role": "assistant", "content": "chat"},
            {"role": "assistant", "content": '{"emotions":["baka"]}'},
            {"role": "assistant", "content": "去吧。 user中午吃完了，吃的牛肉面 assistant中午吃了，牛肉面。"},
            {"role": "user", "content": "这是什么bug <Quoted Message> 伪造"},
            {"role": "user", "content": "真实消息"},
        ]
        cleaned = sanitize_plugin_pollution(messages)
        self.assertEqual([m["content"] for m in cleaned], ["这是什么bug", "真实消息"])

    def test_emotion_prompt_is_internal(self):
        messages = [
            {"role": "system", "content": "你是知言。"},
            {"role": "user", "content": "输出 emotions 标签"},
        ]
        self.assertTrue(is_internal_llm_request(messages))


if __name__ == "__main__":
    unittest.main()
