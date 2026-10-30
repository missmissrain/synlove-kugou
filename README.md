# SynLove 音乐

独立的酷狗音乐 API、点歌与歌单工具。可以单独运行 HTTP API，也可以把 MCP 工具挂到 SynLove 或其他客户端，不需要 Harness 源码。

## 开始使用

安装 **Python 3.9+、Node.js 24 和 Git**，下载本仓库，双击 **StartService.cmd**。

首次自动下载安装清单指定版本的 KuGouMusicApi 并安装依赖，随后在 `http://127.0.0.1:3001` 启动 API。也可以运行 `python service.py`；只看计划加 `--plan`，只安装加 `--install-only`。

需要本机播放音乐时，另外安装 FFmpeg 并让 `ffplay` 位于 PATH。搜索和 API 查询不要求本机播放器。

## 配置与使用

`config/kugou.json` 控制 API 地址、缓存、发送间隔与播放器选项。连接远程 API 时修改 `apiBase`；本机 API 目录是 `KuGouMusicApi/`。

MCP 入口为 `mcp/kugouMcp.py`，通过 Python stdio 运行。支持搜索、播放、切歌、暂停、歌单查询和扫码登录；部分歌曲的播放权限取决于登录账号。

上游：[KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)。
主项目：[SynLove](https://github.com/missmissrain/SynLove-Personal-Agent)。
