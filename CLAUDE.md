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
├── scrape_hanime.py     # CLI 首页爬虫 — 终端文本输出
├── hanime_tui.py        # Textual TUI (旧版) — 搜索 + 视频源提取
├── hanime_kitty.py      # Kitty Native TUI (新版) — 像素级缩略图渲染
├── kitty_ui.py          # Kitty 协议基础设施 (Terminal, Graphics, Keyboard, UI)
├── kitty_scraper.py     # 爬虫模块 (搜索, 详情, 首页, 模型)
├── kitty_native_poc.py  # Kitty 协议概念验证 (参考)
├── CLAUDE.md            # 本文件
├── README.md            # 项目说明
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

### hanime_tui.py (旧版 Textual TUI)

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

### hanime_kitty.py (新版 Kitty Native TUI)

**需要 Kitty 终端 (≥0.29)**。使用 Kitty Graphics Protocol 实现像素级缩略图渲染，Kitty Keyboard Protocol 实现精确按键/鼠标事件处理。

**功能**:
- **HomeScreen**: 首页头版推荐(Banner大图) + 分类行(横向缩略图网格) + 搜索入口
- **SearchScreen**: 实时搜索输入 + 结果列表(缩略图 + 元信息)
- **VideoDetailScreen**: 视频海报(点击→xdg-open打开1080p) + 分辨率源URL + 可点击标签(跳转搜索)

**架构**: `hanime_kitty.py` 导入 `kitty_ui.py`(终端/图形/输入/UI渲染基础设施) 和 `kitty_scraper.py`(数据抓取/解析/模型)。

**导航**:
```
HomeScreen → Enter/点击搜索 → SearchScreen
HomeScreen → 点击视频 → VideoDetailScreen
SearchScreen → Enter/点击结果 → VideoDetailScreen
VideoDetailScreen → 点击标签 → SearchScreen
Esc → 返回上一屏 | q → 退出
```

运行：
```bash
conda activate hanime-scraper
kitty python hanime_kitty.py [可选搜索词]
```

### kitty_ui.py — 基础设施模块

提供 Kitty 协议原语：
- `Terminal` — raw mode, 备用屏幕缓冲区, 光标控制, 尺寸查询(TIOCGWINSZ), 绘制基元
- `KittyGraphics` — Kitty Graphics Protocol: `display_image()` 分块base64传输, `delete_image()`, `display_image_url()`; 支持 `img_cols`/`img_rows` 控制显示尺寸
- `KeyboardReader` — Kitty Keyboard Protocol (CSI > 31 u) + SGR Mouse (1000h+1003h+1006h), `read_event()` / `read_events()`
- `UIRenderer` — 高级UI组件: 标题栏/状态栏(inverse video), 居中文本, 方框(┌┐└┘), 分隔线, 搜索栏, 结果列表项
- `Screen` 基类 — 事件分发(on_key/on_mouse/on_text), 点击区域管理(add_click_zone/clear_click_zones), 生命周期(on_enter/on_leave)

### kitty_scraper.py — 爬虫模块

数据模型和抓取函数：
- **数据类**: `VideoResult`, `VideoDetail`, `Category`, `HomePageData`
- **函数**: `search_videos(query)` → `list[VideoResult]`, `extract_video_detail(url)` → `VideoDetail` (含标签: 井号标签/分类标签/类型链接), `fetch_homepage()` → `HomePageData` (Banner + 分类行 + 类型导航), `download_thumbnail(url)` → `bytes`
- **标签类型**: (1) 井号标签 `#文本` → `/search?query=...`, (2) 分类标签 → `/search?tags%5B%5D=...`, (3) 类型链接 → `/search?genre=...`

## 技术要点

### 反爬
- 站点使用 Cloudflare JS Challenge 防护
- 使用 `cloudscraper` 库模拟 Chrome 浏览器指纹绕过

### HTML 解析
- `beautifulsoup4` + `lxml` 解析页面
- 视频页的 `<video#player>` 元素中包含 `<source>` 子元素，按分辨率提供 mp4 URL
- 搜索需过滤 `erolabs`/`erodalabs` 广告链接
- 页面使用 `&nbsp;`（`\xa0`）作为标题分隔符

### TUI (Textual — hanime_tui.py)
- `textual` 8.x 框架
- `@work(thread=True)` 装饰器实现不阻塞 UI 的后台网络请求
- `self.app.call_from_thread()` 将结果传回主线程更新 UI
- `ListItem.compose()` 用于自定义列表项内容
- 预置 `ListView` 在 `compose()` 中，通过 `clear()` + `mount()` 刷新

### TUI (Kitty Native — hanime_kitty.py)
- **终端管理**: `termios` raw mode + 备用屏幕缓冲区 (`ESC [?1049h/l`) + TIOCGWINSZ ioctl 尺寸查询
- **图片渲染**: Kitty Graphics Protocol — `ESC _ G key=value,... ; base64 ESC \` APC 序列, 4096字节分块传输, `f=100` (PNG), `a=T` (传输+显示), `c=N` (显示宽度/列数)
- **输入**: Kitty Keyboard Protocol `CSI > 31 u` (完整增强模式: 消歧义+事件类型+alternate keys+text as codepoints) + SGR Mouse `ESC [?1000h?1003h?1006h`
- **线程模型**: `threading.Thread` 后台网络请求, `queue.Queue` 将结果传回主线程; 每个回调检查 `self is self.app.current_screen` 防止向非活跃屏幕绘制
- **缩略图加载**: 首页分类缩略图逐个顺序下载，每完成一个立即通过队列更新 UI（渐进式加载）
- **点击区域**: 每个 Screen 维护 `_click_zones` 列表 `(r1, r2, c1, c2, handler_name, data)`, 鼠标事件匹配区域后调用对应 handler
- **导航**: 屏幕栈 `push_screen`/`pop_screen`, `on_enter`/`on_leave` 生命周期管理; 返回已加载屏幕时跳过重新抓取（数据缓存）
- **SIGWINCH**: `signal.signal(SIGWINCH, handler)` 设置模块级 `resize_pending` 标志, 主循环检测后调用当前 screen 的 `on_resize()` 触发全量重绘

## 常见问题

### MountError: Can't mount widget(s) before X is mounted
在 Textual 中，不能向尚未挂载到 DOM 的 widget 添加子组件。确保先挂载父组件，再挂载子组件；或使用 `compose()` 延迟创建子组件。

### DuplicateIds
避免在运行时反复创建/销毁同名 ID 的 widget。改为在 `compose()` 中预置 widget，运行时只清空/填充内容。

### 403 / Cloudflare 拦截
cloudscraper 需要保持较新版本。如果持续被拦，可尝试更新：`pip install --upgrade cloudscraper`。
