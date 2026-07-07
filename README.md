# hanime1.me Scraper

Python 爬虫工具集，用于抓取 [hanime1.me](https://hanime1.me) 视频网站信息。

提供三种使用方式：CLI 终端输出、Textual TUI、Kitty 原生 TUI（像素级缩略图渲染）。

## 功能对比

| 脚本 | 界面 | 缩略图 | 图片质量 | 鼠标支持 | 终端要求 |
|------|------|--------|----------|----------|----------|
| `scrape_hanime.py` | CLI 美化输出 | ❌ | — | ❌ | 任意 |
| `hanime_tui.py` | Textual TUI | Unicode 字符 | 低 | ✅ | 任意 |
| **`hanime_kitty.py`** | Kitty 原生 TUI | **Kitty Graphics Protocol** | **像素级** | ✅ | **Kitty ≥ 0.29** |

---

## 环境要求

- Python 3.12+
- Conda（推荐）
- Kitty 终端（仅 `hanime_kitty.py` 需要，其他脚本任意终端均可）

## 安装

```bash
# 克隆仓库
git clone git@github.com:AndyYang12345/hanime-cli.git
cd get-hanime

# 创建环境
conda create -n hanime-scraper python=3.12
conda activate hanime-scraper

# 安装依赖
pip install cloudscraper beautifulsoup4 lxml textual textual-dev Pillow
```

| 包 | 用途 |
|----|------|
| [cloudscraper](https://github.com/venomous/cloudscraper) | 绕过 Cloudflare JS Challenge |
| [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) | HTML 解析 |
| [lxml](https://lxml.de/) | 高性能 XML/HTML 解析器 |
| [textual](https://textual.textualize.io/) | Textual TUI 框架（仅 `hanime_tui.py`） |
| [textual-dev](https://github.com/Textualize/textual-dev) | Textual 开发工具（仅 `hanime_tui.py`） |
| [Pillow](https://pillow.readthedocs.io/) | JPEG → PNG 转换（仅 `hanime_kitty.py`） |

---

## 项目结构

```
get-hanime/
├── scrape_hanime.py     # CLI 首页爬虫 — 终端美化输出
├── hanime_tui.py        # Textual TUI (旧版) — 搜索 + 视频源提取
├── hanime_kitty.py      # Kitty Native TUI (新版) — 像素级缩略图渲染 ⭐
├── kitty_ui.py          # Kitty 协议基础设施 (Terminal, Graphics, Keyboard, UI)
├── kitty_scraper.py     # 爬虫模块 (搜索, 详情, 首页, 数据模型)
├── CLAUDE.md            # AI 助手指引
├── README.md            # 本文件
└── .gitignore
```

---

## 使用教程

### 1. scrape_hanime.py — CLI 首页抓取

```bash
conda activate hanime-scraper
python scrape_hanime.py
```

按三层结构输出首页全部视频：
- 🎬 **头版推荐** — Banner 推荐视频（标题 / 作者 / 观看次数 / 标签）
- 📂 **分类快速导览** — 9 个分类搜索链接
- 🎥 **各分类视频详情** — 12 分类 × 最多 12 部

### 2. hanime_tui.py — Textual TUI（搜索 + 视频源）

```bash
conda activate hanime-scraper
python hanime_tui.py [可选搜索词]
```

| 操作 | 快捷键 |
|------|--------|
| 搜索 | 输入关键词 → `Enter` |
| 浏览结果 | `↑` `↓` / 鼠标滚轮 |
| 选择视频 | `Enter` / 鼠标点击 |
| 返回搜索 | `Esc` |
| 复制 1080p URL | 点击按钮 |
| 退出 | `Ctrl+Q` |

### 3. hanime_kitty.py — Kitty Native TUI ⭐（推荐）

**需要 Kitty 终端 ≥ 0.29**。使用 Kitty Graphics Protocol 实现像素级缩略图渲染，Kitty Keyboard Protocol 实现精确输入事件处理。

```bash
conda activate hanime-scraper
kitty python hanime_kitty.py [可选搜索词]
```

#### 三屏导航

```
HomeScreen ──Enter/点击搜索──▶ SearchScreen ──Enter/点击结果──▶ VideoDetailScreen
    │                              │       ◀── Esc ──              │
    └── 点击视频 ──▶ VideoDetailScreen                              │
                         │          ◀── 点击标签 ── SearchScreen    │
                         └── Esc ──▶ 返回上级                       │
```

#### 操作方式

##### 首页 (HomeScreen)

| 动作 | 快捷键 / 操作 |
|------|-------------|
| 搜索 | `Enter` / `/` / `s` / 直接输入 |
| 滚动分类列表 | `↑` `↓` / 鼠标滚轮 |
| 翻页 | `Shift+↑` `Shift+↓` |
| 进入视频详情 | 点击缩略图 |
| 退出 | `q` |

##### 搜索结果 (SearchScreen)

| 动作 | 快捷键 / 操作 |
|------|-------------|
| 输入搜索词 | 直接输入字符 |
| 删除字符 | `Backspace` |
| 触发搜索 | `Enter`（输入框为空时） |
| 浏览结果 | `↑` `↓` / 鼠标滚轮 |
| 翻页 | `Shift+↑` `Shift+↓` |
| 选择结果 | `Enter` / 鼠标点击 |
| 返回首页 | `Esc` |

##### 视频详情 (VideoDetailScreen)

| 动作 | 快捷键 / 操作 |
|------|-------------|
| 打开视频（1080p） | 点击海报 |
| 打开指定分辨率 | 点击源 URL |
| 打开页面 URL | 点击链接 |
| 按标签搜索 | 点击任意标签 |
| 返回上级 | `Esc` |

#### 技术特点

- **像素级缩略图** — Kitty Graphics Protocol 直传 PNG，非 Unicode 字符模拟
- **响应式布局** — 缩略图宽度、banner 大小、每行数量随终端尺寸自动适配
- **增量滚动** — 使用 ANSI 滚动区域 (`CSI r` + `CSI S/T`)，滚动时仅绘制新增内容，已渲染图片无需重复传输
- **两重缓存** — `URL → PNG bytes`（网络缓存）+ `URL → kitty image_id`（终端缓存），二次浏览即时显示
- **真实鼠标支持** — SGR Mouse Tracking (1000h+1003h+1006h)，支持滚轮、点击、拖拽
- **精确键盘事件** — Kitty Keyboard Protocol (CSI > 31 u)，区分 press/release/repeat + 修饰键
- **SIGWINCH 支持** — 终端 resize 时自动重绘布局

#### 常见问题

**Q: 图片不显示？**
A: 确保在 Kitty 终端中运行（`kitty python hanime_kitty.py`），不要使用普通终端。检查 `echo $TERM` 应包含 `kitty`。

**Q: 滚动时图片消失？**
A: 全量重绘时（resize、跨屏导航）会清屏重绘，已有缓存图片会立即显示。增量滚动时图片不会消失。

**Q: 启动时报 TIOCGWINSZ 错误？**
A: 在 Kitty 终端内直接运行，不要管道重定向 stdout（如 `python hanime_kitty.py > debug.txt`）。

---

## 技术要点

- 目标站点使用 **Cloudflare** 防护，通过 `cloudscraper` 模拟 Chrome 浏览器指纹绕过
- 视频页中 `<video#player>` 含多个 `<source>` 子元素，按 `size` 属性区分分辨率（1080p / 720p / 480p）
- 搜索结果中需过滤 `erolabs` / `erodalabs` 广告链接
- 页面使用 `&nbsp;`（`\xa0`）作为标题分隔符
- 缩略图原图为 JPEG，通过 Pillow 实时转换为 PNG（Kitty Graphics Protocol `f=100` 仅接受 PNG）
- TUI 基于 `textual` 8.x（`hanime_tui.py`），使用 `@work(thread=True)` 实现不阻塞 UI 的后台网络请求
- Kitty TUI 使用 `threading.Thread` + `queue.Queue` 后台网络请求，回调通过 `_job_queue` 回到主线程

## 许可

[MIT License](https://opensource.org/licenses/MIT)

Copyright (c) 2026
