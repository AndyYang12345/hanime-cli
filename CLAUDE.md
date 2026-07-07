# CLAUDE.md — hanime1.me Scraper

## 项目概述

Python 爬虫工具集，用于抓取 hanime1.me 视频网站首页信息和搜索视频源。

## 环境

- **包管理**: Conda 环境 `hanime-scraper` (Python 3.12)
- **核心依赖**: cloudscraper, beautifulsoup4, lxml, textual
- **安装**: `conda env create -f environment.yml` (待添加) 或手动 `conda create -n hanime-scraper python=3.12 && conda activate hanime-scraper && pip install cloudscraper beautifulsoup4 lxml textual textual-dev`

## 项目结构

```
get-hanime/
├── scrape_hanime.py    # 首页爬虫 — 终端输出
├── hanime_tui.py       # TUI 搜索 + 视频源提取 — 交互式界面
├── CLAUDE.md           # 本文件
├── README.md           # 项目说明
└── .gitignore
```

## 脚本说明

### scrape_hanime.py

抓取 hanime1.me 首页，按三层结构输出到终端：
1. **头版推荐** (Banner) — 标题、元信息、标签
2. **分类快速导览** — 9 个分类标签及搜索链接
3. **各分类视频详情** — 12 行 × 12 部/行 = 最多 144 部

运行：
```bash
conda activate hanime-scraper
python scrape_hanime.py
```

### hanime_tui.py

基于 Textual 的 TUI 应用，支持搜索视频并提取视频源 URL：
- 输入关键词 → 搜索
- 鼠标/键盘浏览结果 → 选择视频
- 自动爬取视频页 → 提取 1080p/720p/480p 源 URL
- 一键复制到剪贴板

运行：
```bash
conda activate hanime-scraper
python hanime_tui.py [可选搜索词]
```

## 技术要点

### 反爬
- 站点使用 Cloudflare JS Challenge 防护
- 使用 `cloudscraper` 库模拟 Chrome 浏览器指纹绕过

### HTML 解析
- `beautifulsoup4` + `lxml` 解析页面
- 视频页的 `<video#player>` 元素中包含 `<source>` 子元素，按分辨率提供 mp4 URL
- 搜索需过滤 `erolabs`/`erodalabs` 广告链接
- 页面使用 `&nbsp;`（`\xa0`）作为标题分隔符

### TUI
- `textual` 8.x 框架
- `@work(thread=True)` 装饰器实现不阻塞 UI 的后台网络请求
- `self.app.call_from_thread()` 将结果传回主线程更新 UI
- `ListItem.compose()` 用于自定义列表项内容
- 预置 `ListView` 在 `compose()` 中，通过 `clear()` + `mount()` 刷新

## 常见问题

### MountError: Can't mount widget(s) before X is mounted
在 Textual 中，不能向尚未挂载到 DOM 的 widget 添加子组件。确保先挂载父组件，再挂载子组件；或使用 `compose()` 延迟创建子组件。

### DuplicateIds
避免在运行时反复创建/销毁同名 ID 的 widget。改为在 `compose()` 中预置 widget，运行时只清空/填充内容。

### 403 / Cloudflare 拦截
cloudscraper 需要保持较新版本。如果持续被拦，可尝试更新：`pip install --upgrade cloudscraper`。
