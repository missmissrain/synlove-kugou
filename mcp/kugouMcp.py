"""酷狗音乐工具（MCP stdio）：搜索、点播、暂停/切歌、登录。

路由约定：
- 用户说"想听/放一首/点歌/搜一下 XX" → `kugou_play` / `kugou_search`
- 用户说"暂停/继续/下一首/上一首/停止" → `kugou_control`
- 用户问"在放什么/登录了吗" → `kugou_status`
- **不要用 GUI 点击/截图去控制酷狗**；搜索与播放都走本工具。
- 付费歌需要登录一次：`kugou_login` 生成二维码，发图给用户扫。

工具：
    kugou_search(keyword, limit?)
    kugou_play(query, limit?)
    kugou_control(action)
    kugou_status()
    kugou_playlists()
    kugou_login()
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from client.kugouApi import kugouApi  # noqa: E402
from client.kugouPlayback import kugouPlayback  # noqa: E402

CONFIG = json.loads((ROOT / "config" / "kugou.json").read_text(encoding="utf-8-sig"))
API = kugouApi(ROOT, CONFIG)
PLAYER = kugouPlayback(API, ROOT / "runtime" / "kugou-player-state.json")

TOOLS = [
    {
        "name": "kugou_search",
        "description": "在酷狗音乐里搜索歌曲，返回歌名/歌手/hash 等。只想确认有没有某首歌时用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "歌名或 歌手+歌名"},
                "limit": {"type": "integer", "description": "返回条数，默认 10"},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "kugou_play",
        "description": (
            "点歌并立即播放（走酷狗 API 取地址，用本地播放器播放）。"
            "用户说\"想听 XX\"\"放一首 XX\"\"点歌 XX\"时用。"
            "付费歌曲需要已登录（用 kugou_login）；不要用 GUI 搜索/点击。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "歌名，或 歌手+歌名"},
                "limit": {"type": "integer", "description": "候选数量，默认 10"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kugou_control",
        "description": "控制当前点播：play 继续、pause 暂停、next 下一首、prev 上一首、stop 停止。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["play", "pause", "next", "prev", "stop"]},
            },
            "required": ["action"],
        },
    },
    {
        "name": "kugou_status",
        "description": "查看酷狗服务、登录状态与当前在放的歌。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "kugou_playlists",
        "description": "查看登录账号的歌单（需要先登录）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "kugou_playlist_tracks",
        "description": "查看某个歌单里有哪些歌（按名字或 collection id）。只读，不播放。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "playlist": {"type": "string", "description": "歌单名字（如“战歌”）或 collection id"},
                "limit": {"type": "integer", "description": "最多列几首，默认 20"},
            },
            "required": ["playlist"],
        },
    },
    {
        "name": "kugou_play_playlist",
        "description": (
            "播放整个歌单：按歌单名字/id 取曲目，逐首请求播放地址并自动连播"
            "（到页尾自动补下一页，绝不批量请求）。用户说“放我的XX歌单/歌单里的歌”时用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "playlist": {"type": "string", "description": "歌单名字（如“战歌”）或 collection id"},
                "limit": {"type": "integer", "description": "最多播多少首，默认 50"},
            },
            "required": ["playlist"],
        },
    },
    {
        "name": "kugou_login",
        "description": (
            "生成酷狗登录二维码（用户用酷狗 App 扫码一次，之后就能取 VIP 歌曲地址）。"
            "生成后把二维码图片发给用户。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def text(value):
    """统一返回文本块。"""
    return [{"type": "text", "text": value}]


def do_search(arguments):
    """搜索。"""
    keyword = str(arguments.get("keyword") or "").strip()
    if not keyword:
        raise ValueError("keyword 不能为空")
    songs = API.search(keyword, arguments.get("limit"))
    if not songs:
        return "没搜到。"
    lines = [f"搜到 {len(songs)} 首："]
    for index, song in enumerate(songs[:10], 1):
        lines.append(f"{index}. {song['name']} - {song['artist']}")
    return "\n".join(lines)


def do_play(arguments):
    """点歌并播放。"""
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    songs = API.search(query, arguments.get("limit"))
    if not songs:
        return "没搜到这首歌。"
    result = PLAYER.play(songs, 0)
    song = result.get("song") or songs[0]
    if result.get("ok"):
        return f"正在播放：{song['name']} - {song['artist']}（{_quality_label(result.get('format'))}）"
    if result.get("needs_auth"):
        return (f"找到《{song['name']}》 - {song['artist']}，但这是付费/VIP 歌，需要先登录一次。"
                "让我生成二维码，你用酷狗 App 扫一下就行。")
    return f"播放失败：{result.get('error')}"


def do_control(arguments):
    """播放控制。"""
    action = str(arguments.get("action") or "").strip()
    if action == "pause":
        result = PLAYER.pause_or_resume()
        if not result.get("ok"):
            return result.get("error", "当前没有在播放")
        return "已暂停。" if result.get("paused") else "已继续。"
    if action == "next":
        result = PLAYER.next()
    elif action == "prev":
        result = PLAYER.prev()
    elif action == "stop":
        PLAYER.stop()
        return "已停止。"
    elif action == "play":
        status = PLAYER.status()
        if status.get("playing"):
            return "已经在播放。"
        return do_play({"query": (status.get("song") or {}).get("name") or ""}) if status.get("song") else "队列是空的。"
    else:
        raise ValueError(f"未知操作：{action}")
    song = result.get("song") or {}
    if result.get("ok"):
        return f"正在播放：{song.get('name')} - {song.get('artist')}（{_quality_label(result.get('format'))}）"
    return str(result.get("error"))


def _quality_label(fmt):
    """把返回的扩展名翻译成音质说明。"""
    value = str(fmt or "").lower()
    if value in ("flac", "ape", "wav"):
        return "无损"
    if value == "mp3":
        return "mp3"
    return value or "未知音质"


def do_status():
    """状态。"""
    hw = API.status()
    pb = PLAYER.status()
    lines = [
        f"API: {'在线' if hw['server'] else '未启动'}",
        f"登录: {'已登录' if hw['auth'] else '未登录'}",
    ]
    if pb.get("song"):
        state = "暂停" if pb.get("paused") else ("播放中" if pb.get("playing") else "已停止")
        lines.append(f"当前: {pb['song'].get('name')} - {pb['song'].get('artist')}（{state}）")
    else:
        lines.append("当前: 没有在放歌")
    return "\n".join(lines)


def do_playlists():
    """歌单。"""
    if not API.status().get("auth"):
        return "还没登录，先扫码登录一次。"
    items = API.user_playlists()
    if not items:
        return "没拿到歌单。"
    return "\n".join(f"- {item['name']}" for item in items[:30])


def _resolve_playlist(name_or_id):
    """按名字/id 找歌单；找不到时给出候选。"""
    target = str(name_or_id or "").strip()
    found = API.find_playlist(target)
    if found is not None:
        return found, ""
    names = [item["name"] for item in API.user_playlists()]
    return None, "没找到这个歌单。现有：" + "、".join(names[:15])


def do_playlist_tracks(arguments):
    """列出歌单曲目。"""
    playlist = str(arguments.get("playlist") or "").strip()
    if not playlist:
        raise ValueError("playlist 不能为空")
    found, error = _resolve_playlist(playlist)
    if found is None:
        return error
    limit = max(1, int(arguments.get("limit") or 20))
    tracks = API.playlist_page(found["gcid"], 1, min(limit, 30))
    if not tracks:
        return f"《{found['name']}》里没取到歌。"
    lines = [f"《{found['name']}》共取到 {len(tracks)} 首（最多 {limit}）："]
    for index, song in enumerate(tracks, 1):
        lines.append(f"{index}. {song['name']} - {song['artist']}")
    return "\n".join(lines)


def do_play_playlist(arguments):
    """播放整个歌单（逐首取地址 + 自动连播）。"""
    playlist = str(arguments.get("playlist") or "").strip()
    if not playlist:
        raise ValueError("playlist 不能为空")
    found, error = _resolve_playlist(playlist)
    if found is None:
        return error
    limit = max(1, int(arguments.get("limit") or 50))
    gcid = found["gcid"]
    first = API.playlist_page(gcid, 1, min(limit, 30))
    if not first:
        return f"《{found['name']}》里没取到歌。"
    cursor = {"page": 1, "left": max(0, limit - len(first))}

    def refill():
        """到页尾补下一页（逐页请求，不批量）。"""
        if cursor["left"] <= 0:
            return []
        cursor["page"] += 1
        rows = API.playlist_page(gcid, cursor["page"], min(30, cursor["left"]))
        cursor["left"] -= len(rows)
        return rows

    result = PLAYER.play(first, 0, refill=refill)
    song = result.get("song") or first[0]
    if result.get("ok"):
        return (f"开始播放歌单《{found['name']}》：{song['name']} - {song['artist']}"
                f"（{_quality_label(result.get('format'))}），共 {len(first)} 首起，播完自动下一首。")
    if result.get("needs_auth"):
        return f"《{found['name']}》需要登录一次才能取到地址，先扫码。"
    return f"播放失败：{result.get('error')}"


def do_login():
    """生成登录二维码（后台跑登录助手，等二维码文件出现）。"""
    qr = ROOT / "runtime" / "kugou-login-qr.png"
    try:
        qr.unlink()
    except OSError:
        pass
    subprocess.Popen(
        [sys.executable, str(ROOT / "kugou" / "tools" / "kugouLogin.py"), "--qr"],
        cwd=str(ROOT), creationflags=0x08000000,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    for _ in range(20):
        time.sleep(0.5)
        if qr.is_file() and qr.stat().st_size > 0:
            return f"二维码已生成：{qr}\n把这个图片发给用户，用酷狗 App 扫码并确认。"
    return "二维码生成超时，稍后再试一次。"


def handle(message):
    """JSON-RPC 分发。"""
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "kugou-music", "version": "1.0.0"}}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "kugou_search":
                result = do_search(args)
            elif name == "kugou_play":
                result = do_play(args)
            elif name == "kugou_control":
                result = do_control(args)
            elif name == "kugou_status":
                result = do_status()
            elif name == "kugou_playlists":
                result = do_playlists()
            elif name == "kugou_playlist_tracks":
                result = do_playlist_tracks(args)
            elif name == "kugou_play_playlist":
                result = do_play_playlist(args)
            elif name == "kugou_login":
                result = do_login()
            else:
                raise ValueError(f"未知工具：{name}")
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": text(result)}}
        except Exception as error:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": text(f"酷狗操作失败：{type(error).__name__}: {error}"), "isError": True}}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unsupported: {method}"}}


def main():
    """stdio 循环。"""
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
