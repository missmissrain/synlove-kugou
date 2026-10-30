"""酷狗登录助手（扫码 / 导入 cookie），把 token+userid 落到 kugou/config/auth.json。

用法：
    python kugou\\tools\\kugouLogin.py --qr          # 生成二维码，扫码一次
    python kugou\\tools\\kugouLogin.py --status      # 看当前登录状态
    python kugou\\tools\\kugouLogin.py --cookie "token=...; userid=..."   # 手动导入

说明：二维码保存到 runtime\\kugou-login-qr.png；扫码确认后自动写入凭据。
"""

import argparse
import base64
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "kugou.json").read_text(encoding="utf-8-sig"))
BASE = CONFIG.get("apiBase", "http://127.0.0.1:3001").rstrip("/")
AUTH_FILE = ROOT / CONFIG.get("authFile", "config/auth.json")
STATE_FILE = ROOT / CONFIG.get("stateFile", "config/state.json")
QR_FILE = ROOT / "runtime" / "kugou-login-qr.png"


def get(path, params=None):
    """GET JSON。"""
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def save_auth(payload):
    """原子写 auth.json。"""
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_name(AUTH_FILE.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(AUTH_FILE)


def status():
    """打印登录状态。"""
    data = {}
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    print("登录:", "已登录" if data.get("token") else "未登录")
    if data.get("token"):
        print("userid:", data.get("userid", ""))
        print("更新时间:", data.get("updated_at", ""))


def login_cookie(cookie):
    """从 cookie 字符串导入 token/userid。"""
    parts = {}
    for piece in str(cookie or "").split(";"):
        if "=" in piece:
            key, _, value = piece.partition("=")
            parts[key.strip()] = value.strip()
    token = parts.get("token") or parts.get("TOKEN")
    userid = parts.get("userid") or parts.get("KugooID") or parts.get("kugouid")
    if not token or not userid:
        raise SystemExit("cookie 里必须包含 token 和 userid")
    save_auth({"token": token, "userid": userid, "mid": parts.get("mid", ""),
               "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    print("已保存登录凭据（token/userid）")


def login_qr():
    """扫码登录：生成二维码 → 轮询 → 保存凭据。"""
    key_data = get("/login/qr/key") or {}
    data0 = key_data.get("data") or {}
    key = data0.get("qrcode") or data0.get("key") or ""
    if not key:
        raise SystemExit(f"拿不到二维码 key：{json.dumps(key_data, ensure_ascii=False)[:200]}")
    # 记下 key，便于外部核对/补救（不是秘密）
    (ROOT / "runtime" / "kugou-login-key.txt").write_text(key, encoding="utf-8")

    # /login/qr/key 已经直接返回二维码图片，优先用它
    data_url = str(data0.get("qrcode_img") or "")
    if not data_url:
        created = get("/login/qr/create", {"key": key, "qrimg": "true"}) or {}
        payload = created.get("data") or {}
        data_url = str(payload.get("base64") or "")
        if not data_url:
            print("二维码地址:", payload.get("url"))
    if data_url.startswith("data:"):
        data_url = data_url.split(",", 1)[1]
    if data_url:
        QR_FILE.parent.mkdir(parents=True, exist_ok=True)
        QR_FILE.write_bytes(base64.b64decode(data_url))
        print("二维码已保存:", QR_FILE)
        print("请用酷狗 App 扫码并在手机上确认。")

    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            check = get("/login/qr/check", {"key": key}) or {}
        except Exception as error:
            print("查询失败，重试:", type(error).__name__)
            time.sleep(3)
            continue
        body = check.get("body") if isinstance(check.get("body"), dict) else check
        data = (body or {}).get("data") or {}
        st = data.get("status")
        print("扫码状态:", st, flush=True)
        if st == 4:
            token = data.get("token") or ""
            userid = data.get("userid") or ""
            if token:
                save_auth({
                    "token": token, "userid": userid, "mid": data.get("mid", ""),
                    "vip_token": data.get("vip_token", ""),
                    "nickname": data.get("nickname", ""),
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                print("登录成功，凭据已保存。userid =", userid)
                return
        time.sleep(3)
    print("二维码已过期，请重新运行。")


def main(argv=None):
    """入口。"""
    parser = argparse.ArgumentParser(description="酷狗登录助手")
    parser.add_argument("--qr", action="store_true", help="扫码登录")
    parser.add_argument("--status", action="store_true", help="查看状态")
    parser.add_argument("--cookie", default="", help="直接导入 cookie 字符串")
    args = parser.parse_args(argv)

    if args.status:
        status()
    elif args.cookie:
        login_cookie(args.cookie)
    else:
        login_qr()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
