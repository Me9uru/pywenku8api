# pywenku8api

此项目提供基于[轻小说文库（Wenku8）](https://www.wenku8.cc)网页版的 API 实现。普通页面使用常驻 HTTP 客户端并发抓取；只有登录或 HTTP 遇到可解决的 Cloudflare 质询时才启动 Chromium。

为支持 [Wenku8-OPDS](https://github.com/WorldObservationLog/wenku8-opds-readme) 而开发

## 安装与运行

项目使用 [uv](https://docs.astral.sh/uv/) 管理 Python、虚拟环境和依赖，唯一锁文件为
`uv.lock`。安装 uv 后，在项目目录同步环境：

```bash
uv sync --locked
```

本地图形桌面环境下，推荐使用有头 Chromium 运行：

```bash
env DISPLAY=:0 WENKU8_HEADLESS=0 .venv/bin/python -m wenku8.server
```

无桌面的服务器可以先尝试无头模式：

```bash
env WENKU8_HEADLESS=1 .venv/bin/python -m wenku8.server
```

如果服务器上的无头 Chromium 无法通过登录或 Cloudflare 质询，安装 Xvfb 后使用
虚拟桌面运行：

```bash
env WENKU8_HEADLESS=0 xvfb-run -a \
  -s "-screen 0 1280x1024x24" \
  .venv/bin/python -m wenku8.server
```

更新部署时使用锁文件同步，然后重启服务：

```bash
git pull --ff-only
uv sync --locked --no-editable
sudo systemctl restart pywenku8api
```

## 功能列表
- 小说部分
  - 获取信息 (`get_novel_info`)
  - 获取封面 (`get_novel_cover`)
  - 获取目录 (`get_novel_index`)
  - 获取内容 (`get_novel_content`)
  - 搜索小说（书名/作者）(`search_novel/search_novel_by_name/search_novel_by_author`)
  - 获取小说列表 (`get_novel_list`)
  - 按分类(TAG)查看 (`get_novel_by_category`)
  - 获取已完结列表 (`get_finished_novels`)
  - 加入/移出书架 (`add_to_bookshelf`/`remove_from_bookshelf`)
  - 获取他人书架 (`get_user_bookshelf`)
  - 推荐小说/获取推荐 (`vote_novel`/`get_recommend`)
- 用户部分
  - 登录 (`login`)
  - 获取书架 (`get_bookshelf`)
  - 获取个人信息 (`get_user_info`)
- 评论部分
  - 获取书评 (`get_comments`)
  - 获取回复 (`get_replies`)
  - 发表书评 (*TODO*)
  - 回复书评 (*TODO*)
- 杂项
  - 简繁转换

## 限制
- 可能会绕不过 Cloudflare 防火墙
- 版权书目无法阅读
- 日本 IP 无法使用

## 内存缓存

常用的详情、目录、章节、整本、搜索和列表结果使用进程内加权 LRU 缓存；相同
cache key 的并发 miss 会合并为一次上游抓取。缓存默认最多占用 64 MiB，图片不进入
服务端缓存，大正文过期后也不会在后台同时保留新旧两份。

FastAPI 服务可通过 `WENKU8_CACHE_MEMORY_MB` 调整上限，例如在内存紧张时设置为
`32`；设置为 `0` 可关闭结果缓存（相同并发请求的 singleflight 合并仍然有效）。
`GET /cache/status` 可查看命中次数、估算内存占用和正在抓取的请求数。

默认节点为 `https://www.wenku8.cc`，只读请求遇到连接、5xx、限流或 CF 问题时会自动尝试 `.net`；可用
`WENKU8_ENDPOINT` 显式指定首选节点。HTTP 429 仅按 `Retry-After`（最多 5 秒）
重试一次，正常搜索没有固定冷却等待。加入/移除书架和投票不会跨节点重放。
