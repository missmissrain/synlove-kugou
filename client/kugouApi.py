"""酷狗 API 客户端（本机 KuGouMusicApi + 反风控策略）。

职责：
- 管理本机 KuGouMusicApi 进程（端口没起就后台拉起）；
- 统一请求出口：串行 + 最小间隔、结果 TTL 缓存、失败冷却（参考 N.E.K.O 的做法）；
- dfid（设备指纹）持久化，遇到"本次请求需要验证"自动刷新一次；
- 登录凭据（token/userid/mid/cookie）从 `kugou/config/auth.json` 读取，文件变更自动热重载；
- 对外提供 search / song_url / user_playlists / status。

注册参数（config 段）：
    apiBase / apiPort / apiDir / logFile / authFile / stateFile
    quality / minIntervalSeconds / searchCacheSeconds / urlCacheSeconds
    failureCooldownSeconds / dfidTtlSeconds / maxResults
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class kugouApi:
    """酷狗 API 的轻量客户端（线程安全、带缓存与冷却）。"""

    def __init__(self, root, config):
        """root：项目根目录；config：kugou.json 的内容。"""
        self.root = Path(root)
        self.base = str(config.get("apiBase") or "http://127.0.0.1:3001").rstrip("/")
        self.port = int(config.get("apiPort") or 3001)
        self.host = str(config.get("apiHost") or "127.0.0.1")
        self.api_dir = self.root / str(config.get("apiDir") or "KuGouMusicApi")
        self.log_file = self.root / str(config.get("logFile") or "runtime/kugou-api.log")
        self.auth_file = self.root / str(config.get("authFile") or "config/auth.json")
        self.state_file = self.root / str(config.get("stateFile") or "config/state.json")
        self.quality = config.get("quality") or "flac"
        self.quality_fallback = config.get("qualityFallback") or ["320", "128"]
        self.min_interval = float(config.get("minIntervalSeconds") or 1.0)
        self.search_ttl = float(config.get("searchCacheSeconds") or 60)
        self.url_ttl = float(config.get("urlCacheSeconds") or 600)
        self.cooldown = float(config.get("failureCooldownSeconds") or 1800)
        self.dfid_ttl = float(config.get("dfidTtlSeconds") or 86400)
        self.max_results = int(config.get("maxResults") or 10)

        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._cache = {}
        self._cooldown_until = 0.0
        self._auth = {}
        self._auth_mtime = 0.0
        self._auth_broken_until = 0.0
        self._state = self._read_json(self.state_file)
        self._load_auth()

    # ── 对外 ────────────────────────────────────────────────────────────────

    def ensure_server(self):
        """确保本机 API 在监听；没起就后台拉一个。"""
        if self._port_open():
            return True
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PORT"] = str(self.port)
        env["HOST"] = self.host
        flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
        log = open(self.log_file, "a", encoding="utf-8")
        subprocess.Popen(
            [self._node(), "app.js"],
            cwd=str(self.api_dir), env=env, stdout=log, stderr=log, creationflags=flags,
        )
        for _ in range(40):
            time.sleep(0.5)
            if self._port_open():
                return True
        return False

    def search(self, keyword, limit=None):
        """按关键词搜歌，返回归一化列表。"""
        limit = min(int(limit or self.max_results), 30)
        data = self._request("/search", {
            "keywords": keyword, "type": "song", "page": 1, "pagesize": limit,
        }, ttl=self.search_ttl)
        lists = ((data or {}).get("data") or {}).get("lists") or []
        out = []
        seen = set()
        for item in lists:
            title = str(item.get("SongName") or "").strip()
            artist = str(item.get("SingerName") or "").strip()
            song_hash = str(item.get("FileHash") or "").strip()
            if not title or not song_hash:
                continue
            key = (title.casefold(), artist.casefold())
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "name": title,
                "artist": artist,
                "hash": song_hash,
                "album_id": int(item.get("AlbumID") or 0),
                "album_audio_id": int(item.get("EMixSongID") or item.get("Audioid") or 0),
                "duration": int(item.get("Duration") or 0),
            })
            if len(out) >= limit:
                break
        return out

    def song_url(self, song_hash, album_id=0, album_audio_id=0, quality=None):
        """取播放地址：默认无损（flac），失败逐级降级到 320/128；token 不可用时退回 dfid-only。

        注意：`album_audio_id` 传 0 时服务器才会返回真正的 flac；传搜索里的 Audioid
        会被降级成 mp3。所以先试 0，再试调用方给的 id。
        """
        ladder = [quality or self.quality] + [
            item for item in (self.quality_fallback or ()) if item != (quality or self.quality)
        ]
        audio_ids = [0, int(album_audio_id or 0)]
        last = None
        for level in ladder:
            for audio_id in audio_ids:
                params = {
                    "hash": song_hash,
                    "album_id": int(album_id or 0),
                    "album_audio_id": audio_id,
                    "quality": level,
                }
                data = self._request("/song/url", params, ttl=self.url_ttl)
                if self._needs_verify(data) and self._auth.get("token"):
                    # 登录 token 不可用时，带 token 反而会让免费歌也验证失败
                    self._auth_broken_until = time.time() + self.cooldown
                    data = self._request("/song/url", params, ttl=self.url_ttl, use_auth=False)
                if data is None:
                    last = {"ok": False, "needs_auth": False, "error": "请求被冷却或失败", "quality": level}
                    continue
                urls = data.get("url") or []
                if isinstance(urls, str):
                    urls = [urls]
                if urls:
                    return {
                        "ok": True,
                        "urls": urls,
                        "backup": data.get("backupUrl") or [],
                        "ext": data.get("extName") or "",
                        "quality": level,
                        "album_audio_id": audio_id,
                        "needs_auth": False,
                        "error": "",
                    }
                last = {
                    "ok": False,
                    "urls": [],
                    "ext": data.get("extName") or "",
                    "quality": level,
                    "needs_auth": bool(data.get("error_code") in (35104, 35105) or data.get("status") == 2),
                    "error": str(data.get("error") or "")[:60],
                }
        return last or {"ok": False, "needs_auth": False, "error": "拿不到播放地址"}

    @staticmethod
    def _needs_verify(data):
        """是否命中"本次请求需要验证"（20028）。"""
        if not isinstance(data, dict):
            return False
        return str(data.get("errcode")) == "20028" or data.get("error") == "本次请求需要验证"

    def user_playlists(self):
        """取登录用户的歌单（需要 token）。"""
        data = self._request("/user/playlist", {"pagesize": 50}, ttl=300)
        items = ((data or {}).get("data") or {}).get("info") or (data or {}).get("data") or []
        out = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("listid"):
                out.append({
                    "id": item.get("listid"),
                    "name": item.get("name", ""),
                    "gcid": item.get("global_collection_id", ""),
                })
        return out

    def playlist_page(self, gcid, page=1, pagesize=30):
        """取歌单一页曲目（逐首请求，不批量）。返回归一化歌曲列表。"""
        data = self._request("/playlist/track/all", {
            "id": gcid, "page": int(page), "pagesize": int(pagesize),
        }, ttl=300)
        payload = (data or {}).get("data") or {}
        rows = payload.get("info") or payload.get("songs") or payload.get("list") or []
        out = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            song_hash = str(row.get("hash") or row.get("FileHash") or "").strip()
            if not song_hash:
                continue
            name, artist = _split_filename(str(
                row.get("filename") or row.get("name") or row.get("songname") or ""
            ))
            out.append({
                "name": name,
                "artist": artist,
                "hash": song_hash,
                "album_id": int(row.get("album_id") or 0),
                "album_audio_id": int(row.get("audio_id") or row.get("album_audio_id") or 0),
            })
        return out

    def find_playlist(self, name_or_id):
        """按名字（精确→包含）或 gcid 找歌单；返回 {(gcid, name)} 或 None。"""
        target = str(name_or_id or "").strip()
        if not target:
            return None
        if target.startswith("collection_"):
            return {"gcid": target, "name": target}
        playlists = self.user_playlists()
        exact = [item for item in playlists if item["name"] == target]
        if exact:
            return {"gcid": exact[0]["gcid"], "name": exact[0]["name"]}
        fuzzy = [item for item in playlists if target in item["name"]]
        if len(fuzzy) == 1:
            return {"gcid": fuzzy[0]["gcid"], "name": fuzzy[0]["name"]}
        return None

    def status(self):
        """当前状态：服务、登录、dfid。"""
        return {
            "server": self._port_open(),
            "auth": bool(self._auth.get("token")),
            "userid": self._auth.get("userid", ""),
            "dfid": self._state.get("dfid", ""),
            "cooldown": max(0.0, self._cooldown_until - time.time()),
        }

    # ── 请求与风控 ──────────────────────────────────────────────────────────

    def _request(self, path, params, ttl=0, retry_auth=True, use_auth=True):
        """统一请求：缓存 → 冷却 → 串行限速 → dfid 注入 → 失败处理。"""
        key = (path, use_auth, tuple(sorted((k, str(v)) for k, v in params.items() if k not in ("cookie",))))
        now = time.time()
        if ttl > 0:
            cached = self._cache.get(key)
            if cached and now - cached[0] < ttl:
                return cached[1]
        if now < self._cooldown_until:
            return None
        if not self.ensure_server():
            return None

        self._load_auth()
        query = dict(params)
        cookie = self._cookie_string(use_auth)
        if cookie:
            query["cookie"] = cookie

        with self._lock:
            wait = self.min_interval - (time.time() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            try:
                data = self._http_get(path, query)
            except Exception:
                data = None
            self._last_request_at = time.time()

        if data is None:
            self._cooldown_until = time.time() + self.cooldown
            return None

        # "本次请求需要验证"：登录 token 不可用时先退回 dfid-only；再不行才刷新 dfid
        if self._needs_verify(data):
            if use_auth and self._auth.get("token"):
                self._auth_broken_until = time.time() + self.cooldown
                return self._request(path, params, ttl=ttl, retry_auth=retry_auth, use_auth=False)
            if retry_auth:
                self._refresh_dfid()
                return self._request(path, params, ttl=ttl, retry_auth=False, use_auth=False)

        if ttl > 0 and isinstance(data, dict) and data.get("status") == 1 and not self._needs_verify(data):
            self._cache[key] = (time.time(), data)
        return data

    def _http_get(self, path, params):
        """真正发 HTTP 请求。cookie 必须原样传（服务端按 ';' 切分，不能 URL 编码）。"""
        cookie = params.get("cookie")
        query = {key: value for key, value in params.items() if key != "cookie"}
        url = self.base + path + "?" + urllib.parse.urlencode(query)
        if cookie:
            url += "&cookie=" + str(cookie)
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def _cookie_string(self, use_auth=True):
        """把登录凭据与 dfid 拼成 cookie；token 失效期间只用 dfid。"""
        parts = []
        with_auth = use_auth and time.time() >= self._auth_broken_until
        keys = ("token", "userid", "mid") if with_auth else ()
        for key in (*keys, "dfid"):
            value = self._auth.get(key) or (self._state.get("dfid") if key == "dfid" else "")
            if value:
                parts.append(f"{key}={value}")
        return ";".join(parts)

    def _refresh_dfid(self):
        """重新注册设备 dfid 并落盘。"""
        data = self._http_get("/register/dev", {})
        dfid = ((data or {}).get("data") or {}).get("dfid")
        if dfid:
            self._state["dfid"] = dfid
            self._state["dfid_at"] = time.time()
            self._write_json(self.state_file, self._state)

    def _load_auth(self):
        """读 auth.json（带 mtime 热重载）。"""
        try:
            stat = self.auth_file.stat()
        except OSError:
            self._auth = {}
            return
        if stat.st_mtime == self._auth_mtime:
            return
        self._auth = self._read_json(self.auth_file)
        self._auth_mtime = stat.st_mtime

    # ── 工具 ────────────────────────────────────────────────────────────────

    def _port_open(self):
        """端口是否可连。"""
        with socket.socket() as probe:
            probe.settimeout(1.0)
            return probe.connect_ex((self.host, self.port)) == 0

    def _node(self):
        """找 node 可执行文件。"""
        return shutil.which("node") or "node"

    @staticmethod
    def _read_json(path):
        """读 JSON（不存在返回空 dict）。"""
        try:
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _write_json(path, data):
        """原子写 JSON。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def _split_filename(text):
    """把酷狗的 `歌手 - 歌名` 拆成 (歌名, 歌手)。"""
    value = str(text or "").strip()
    for sep in (" - ", " -", "- "):
        if sep in value:
            artist, _, name = value.partition(sep)
            return name.strip(), artist.strip()
    return value, ""
