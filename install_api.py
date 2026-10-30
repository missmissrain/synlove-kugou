"""拉取并安装上游 KuGouMusicApi（本仓库只做客户端，不带第三方代码）。

职责：
- 在仓库根放置 `KuGouMusicApi/`（已存在则 git pull）；
- 执行 `npm install` 安装依赖，产出可用 `node app.js` 启动的 API 服务。

注册参数（命令行）：
    --repo   git 地址，默认 https://github.com/MakcRe/KuGouMusicApi.git
    --ref    分支/标签，默认 master
    --force  已存在时也重新安装依赖
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "KuGouMusicApi"
ENTRY = TARGET / "app.js"


def run(cmd, cwd):
    """执行外部命令，失败时带最后几行输出抛错。"""
    print("+", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        raise SystemExit(f"命令失败（{result.returncode}）：{' '.join(cmd)}\n" + "\n".join(tail))
    return result.stdout


def ensure_clone(repo, ref):
    """确保上游源码存在：没有就 clone，有就 fetch 更新。"""
    if not (TARGET / "package.json").is_file():
        run(["git", "clone", "--depth", "1", "--branch", ref, repo, str(TARGET)], ROOT)
        return "cloned"
    run(["git", "fetch", "--depth", "1", "origin", ref], TARGET)
    run(["git", "checkout", "FETCH_HEAD"], TARGET)
    return "updated"


def ensure_npm(force):
    """安装 node 依赖（已有 node_modules 且没 force 时跳过）。"""
    if not shutil.which("node"):
        raise SystemExit("没找到 node，请先安装 Node 18+ 并加入 PATH")
    if (TARGET / "node_modules").is_dir() and not force:
        return "skipped"
    run(["npm", "install"], TARGET)
    return "installed"


def main(argv=None):
    """入口：拉源码 + 装依赖。"""
    parser = argparse.ArgumentParser(description="安装上游 KuGouMusicApi")
    parser.add_argument("--repo", default="https://github.com/MakcRe/KuGouMusicApi.git", help="git 仓库地址")
    parser.add_argument("--ref", default="master", help="分支或标签")
    parser.add_argument("--force", action="store_true", help="已存在时也重新安装依赖")
    args = parser.parse_args(argv)

    print("源码：", ensure_clone(args.repo, args.ref))
    print("依赖：", ensure_npm(args.force))
    print("入口：", ENTRY)
    print("启动：cd KuGouMusicApi && set PORT=3001 && node app.js")
    print("提示：客户端第一次调工具时会自动拉起它（runtime/kugou-api.log）")


if __name__ == "__main__":
    main()
