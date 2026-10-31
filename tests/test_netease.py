import unittest

from companion_memory import netease


class NeteaseTests(unittest.TestCase):
    def test_extracts_common_share_urls(self):
        self.assertEqual(
            netease.extract_url("听这个 https://music.163.com/song?id=186016 好听"),
            "https://music.163.com/song?id=186016",
        )
        self.assertEqual(
            netease.extract_url("https://y.music.163.com/m/song?id=186016"),
            "https://y.music.163.com/m/song?id=186016",
        )
        self.assertEqual(netease.extract_url("https://163cn.tv/abc123"), "https://163cn.tv/abc123")
        self.assertIsNone(netease.extract_url("没有链接"))

    def test_formats_song_context(self):
        song = netease.NeteaseSong(
            song_id="186016",
            title="晴天",
            artists="周杰伦",
            album="叶惠美",
            url="https://music.163.com/song?id=186016",
            lyric_excerpt="故事的小黄花\n从出生那年就飘着",
        )
        original = netease.get_song
        netease.get_song = lambda url, timeout=6: song
        try:
            block = netease.build_link_context("听 https://music.163.com/song?id=186016")
        finally:
            netease.get_song = original
        self.assertIn("歌名：晴天", block)
        self.assertIn("歌手：周杰伦", block)
        self.assertIn("歌词片段", block)
        self.assertIn("不是完整歌词", block)

    def test_failure_does_not_fake_listening(self):
        original = netease.get_song
        netease.get_song = lambda url, timeout=6: None
        try:
            block = netease.build_link_context("https://music.163.com/song?id=1")
        finally:
            netease.get_song = original
        self.assertIn("不要假装听过", block)


if __name__ == "__main__":
    unittest.main()
