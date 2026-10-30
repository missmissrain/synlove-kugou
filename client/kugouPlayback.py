"""酷狗点播播放器：ffplay 后台播放 + 队列 + 自动连播 + 暂停/继续。

职责：
- 把 API 解析出的音频地址用 ffplay 无界面播放（不依赖酷狗客户端、不点界面）；
- 队列播放：一首结束自动下一首；歌单可以按页补充（refill），**逐首取地址**；
- 暂停/继续通过挂起/恢复 ffplay 进程实现（Windows ntdll）；
- 队列与状态落 `runtime/kugou-player-state.json`。

注册参数（构造）：api（kugouApi 实例）、state_path（队列状态文件）、ffplay（可执行路径）
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class kugouPlayback:
    """API 点播播放器（ffplay，自动连播）。"""

    def __init__(self, api, state_path, ffplay=None):
        """api：kugouApi；state_path：队列状态文件；ffplay：ffplay 路径。"""
        self.api = api
        self.state_path = Path(state_path)
        self.ffplay = ffplay or os.environ.get("FFPLAY") or shutil.which("ffplay") or "ffplay"
        self.state = self._load()
        self._gen = 0
        self._refill = None
        self._lock = threading.Lock()

    # ── 对外 ────────────────────────────────────────────────────────────────

    def play(self, songs, start=0, refill=None):
        """开始播放一组候选歌单；refill 用于歌单播到页尾时补充下一页。"""
        if not songs:
            raise ValueError("没有可播放的歌曲")
        with self._lock:
            self._gen += 1
            self._refill = refill
            self.state = {
                "queue": list(songs),
                "index": max(0, min(start, len(songs) - 1)),
                "pid": 0,
                "paused": False,
            }
        return self._play_current()

    def next(self):
        """下一首（到页尾会尝试 refill）。"""
        return self._advance(1)

    def prev(self):
        """上一首。"""
        return self._advance(-1)

    def pause_or_resume(self):
        """暂停 / 继续（切换）。"""
        pid = int(self.state.get("pid") or 0)
        if pid <= 0 or not self._alive(pid):
            return {"ok": False, "error": "当前没有在播放"}
        if self.state.get("paused"):
            self._resume(pid)
            self.state["paused"] = False
        else:
            self._suspend(pid)
            self.state["paused"] = True
        self._save()
        return {"ok": True, "paused": self.state["paused"], "song": self.current()}

    def stop(self):
        """停止播放（并让自动连播失效）。"""
        with self._lock:
            self._gen += 1
            pid = int(self.state.get("pid") or 0)
            self.state["pid"] = 0
            self.state["paused"] = False
        if pid > 0:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        self._save()
        return {"ok": True}

    def current(self):
        """当前曲目信息。"""
        queue = self.state.get("queue") or []
        index = int(self.state.get("index") or 0)
        return queue[index] if 0 <= index < len(queue) else None

    def status(self):
        """播放器状态。"""
        pid = int(self.state.get("pid") or 0)
        return {
            "playing": pid > 0 and self._alive(pid),
            "paused": bool(self.state.get("paused")),
            "song": self.current(),
            "queue": len(self.state.get("queue") or []),
            "index": int(self.state.get("index") or 0),
        }

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _advance(self, delta):
        """切到上/下一首；到队尾时尝试 refill。"""
        with self._lock:
            queue = list(self.state.get("queue") or [])
            index = int(self.state.get("index") or 0) + delta
            if index >= len(queue) and self._refill is not None:
                try:
                    more = self._refill() or []
                except Exception:
                    more = []
                if more:
                    queue.extend(more)
                    self.state["queue"] = queue
            if index < 0 or index >= len(queue):
                return {"ok": False, "error": "歌单播完了"}
            self.state["index"] = index
        return self._play_current()

    def _play_current(self):
        """解析并播放当前曲目；启动守护线程做自动连播。"""
        song = self.current()
        if not song:
            return {"ok": False, "error": "没有当前曲目"}
        with self._lock:
            self._gen += 1
            gen = self._gen
            old_pid = int(self.state.get("pid") or 0)
        if old_pid > 0:
            subprocess.run(["taskkill", "/F", "/PID", str(old_pid)], capture_output=True)
        resolved = self.api.song_url(song.get("hash"), song.get("album_id"), song.get("album_audio_id"))
        if not resolved.get("ok"):
            if resolved.get("needs_auth"):
                return {"ok": False, "needs_auth": True, "song": song,
                        "error": "这首需要登录（VIP/音乐包）才能取到播放地址"}
            return {"ok": False, "song": song, "error": resolved.get("error") or "拿不到播放地址"}
        url = resolved["urls"][0]
        flags = 0x08000000 | 0x00000008
        process = subprocess.Popen(
            [self.ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", "80", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
        )
        with self._lock:
            if gen != self._gen:  # 已被更新的请求取代
                subprocess.run(["taskkill", "/F", "/PID", str(process.pid)], capture_output=True)
                return {"ok": False, "error": "播放被新的请求取代"}
            self.state["pid"] = process.pid
            self.state["paused"] = False
        self._save()
        threading.Thread(target=self._watch, args=(gen, process), daemon=True).start()
        return {
            "ok": True, "song": song, "pid": process.pid,
            "format": resolved.get("ext") or "", "url": url[:60] + "...",
        }

    def _watch(self, gen, process):
        """等当前曲目播完，自动下一首（被停止/切换则退出）。"""
        process.wait()
        with self._lock:
            if gen != self._gen or self.state.get("paused"):
                return
        result = self._advance(1)
        if not result.get("ok"):
            with self._lock:
                self._gen += 1
                self.state["pid"] = 0
            self._save()

    def _kill(self):
        """杀掉上一首的 ffplay。"""
        pid = int(self.state.get("pid") or 0)
        if pid > 0:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        self.state["pid"] = 0
        self.state["paused"] = False

    @staticmethod
    def _alive(pid):
        """进程是否还在。"""
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        return str(pid) in (result.stdout or "")

    @staticmethod
    def _suspend(pid):
        """挂起进程。"""
        handle = ctypes.windll.kernel32.OpenProcess(0x0800, False, pid)
        if handle:
            ctypes.windll.ntdll.NtSuspendProcess(handle)
            ctypes.windll.kernel32.CloseHandle(handle)

    @staticmethod
    def _resume(pid):
        """恢复进程。"""
        handle = ctypes.windll.kernel32.OpenProcess(0x0800, False, pid)
        if handle:
            ctypes.windll.ntdll.NtResumeProcess(handle)
            ctypes.windll.kernel32.CloseHandle(handle)

    def _load(self):
        """读状态。"""
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"queue": [], "index": 0, "pid": 0, "paused": False}

    def _save(self):
        """写状态。"""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)
