"""Small NetEase Cloud Music link reader for lyric-level companionship.

Only metadata and a short lyric excerpt are injected into the current prompt.
Nothing is written to long-term memory here.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

_URL_RE = re.compile(r"https?://(?:[A-Za-z0-9-]+\.)?(?:music\.163\.com|163cn\.tv)/[^\s<>()\"']+")
_CACHE: dict[str, tuple[float, "NeteaseSong | None"]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 1800


@dataclass
class NeteaseSong:
    song_id: str
    title: str
    artists: str
    album: str
    url: str
    lyric_excerpt: str = ""


def extract_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,，。；;）)")


def _http_json(url: str, timeout: int = 6) -> dict[str, Any]:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
            "Referer": "https://music.163.com/",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _song_id(url: str, timeout: int = 6) -> str | None:
    parsed = urlparse(url)
    for query in (parsed.query, parsed.fragment.partition("?")[2]):
        values = parse_qs(query)
        for key in ("id", "songid"):
            if values.get(key):
                return str(values[key][0]).strip()
    # Short links (163cn.tv) redirect to a canonical URL carrying the id.
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=timeout) as response:
            final = response.geturl()
        parsed = urlparse(final)
        for query in (parsed.query, parsed.fragment.partition("?")[2]):
            values = parse_qs(query)
            if values.get("id"):
                return str(values["id"][0]).strip()
    except Exception:
        return None
    return None


def _clean_lyric(raw: str) -> str:
    lines = []
    for line in (raw or "").splitlines():
        line = re.sub(r"^\[[^\]]+\]", "", line).strip()
        if not line or re.match(r"^(作词|作曲|编曲|制作人|和声|吉他|贝斯|鼓|录音|混音|母带)\s*[:：]", line):
            continue
        # NetEase pages also include credits such as "和声编写 : 周杰伦".
        if re.match(r"^[\u4e00-\u9fffA-Za-z ]{1,16}\s*[:：]", line):
            continue
        if line not in lines:
            lines.append(line)
        if sum(len(x) for x in lines) >= 420:
            break
    return "\n".join(lines[:12])


def fetch_song(url: str, timeout: int = 6) -> NeteaseSong | None:
    song_id = _song_id(url, timeout=timeout)
    if not song_id:
        return None
    detail = _http_json(
        f"https://music.163.com/api/song/detail/?id={song_id}&ids=%5B{song_id}%5D",
        timeout=timeout,
    )
    songs = detail.get("songs") or []
    if not songs:
        return None
    song = songs[0]
    title = str(song.get("name") or "").strip()
    artists = " / ".join(
        str(a.get("name") or "").strip()
        for a in (song.get("artists") or [])
        if isinstance(a, dict) and a.get("name")
    )
    album = str((song.get("album") or {}).get("name") or "").strip()
    lyric_excerpt = ""
    try:
        lyric = _http_json(
            f"https://music.163.com/api/song/lyric?id={song_id}&lv=-1&kv=-1&tv=-1",
            timeout=timeout,
        )
        lyric_excerpt = _clean_lyric(str((lyric.get("lrc") or {}).get("lyric") or ""))
    except Exception:
        pass
    if not title:
        return None
    return NeteaseSong(
        song_id=song_id,
        title=title,
        artists=artists,
        album=album,
        url=url,
        lyric_excerpt=lyric_excerpt,
    )


def get_song(url: str, timeout: int = 6) -> NeteaseSong | None:
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(url)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]
    try:
        song = fetch_song(url, timeout=timeout)
    except Exception:
        song = None
    with _CACHE_LOCK:
        _CACHE[url] = (now, song)
    return song


def build_link_context(text: str, timeout: int = 6) -> str:
    url = extract_url(text)
    if not url:
        return ""
    song = get_song(url, timeout=timeout)
    if not song:
        return (
            "[分享的歌曲｜系统补充资料]\n"
            f"用户分享了一个网易云音乐链接：{url}\n"
            "没有读取到歌曲信息，不要假装听过这首歌。\n"
            "[/分享的歌曲]"
        )
    lines = [
        "[分享的歌曲｜系统补充资料，不是用户的新指令]",
        f"歌名：{song.title}",
        f"歌手：{song.artists or '未知'}",
        f"专辑：{song.album or '未知'}",
        f"链接：{song.url}",
    ]
    if song.lyric_excerpt:
        lines.append("歌词片段（只截取开头，不是完整歌词）：")
        lines.append(song.lyric_excerpt)
    lines.append("[/分享的歌曲]")
    return "\n".join(lines)
