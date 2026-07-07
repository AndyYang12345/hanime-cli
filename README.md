# hanime1.me Scraper

Python 爬虫工具集，用于抓取 [hanime1.me](https://hanime1.me) 视频网站信息。

## 功能

| 脚本 | 功能 | 界面 |
|------|------|------|
| `scrape_hanime.py` | 抓取首页全部视频信息（头版推荐 + 12 分类 × 12 视频） | 终端美化输出 |
| `hanime_tui.py` | 搜索视频 → 选择 → 提取 1080p/720p/480p 视频源 URL | Textual TUI（鼠标/键盘） |

### scrape_hanime.py

```
python scrape_hanime.py
```

输出结构：
- 🎬 **头版推荐** — 首页 Banner 推荐视频（标题 / 作者 / 观看次数 / 标签）
- 📂 **分类快速导览** — 9 个视频分类的搜索链接
- 🎥 **各分类视频详情** — `最新上市` `最新上傳` `裏番` `泡麵番` `Motion Anime` `3DCG` `2.5D動畫` `2D動畫` `AI生成` `MMD` `Cosplay` `他們在看`
- 📊 **汇总统计**

### hanime_tui.py

```
python hanime_tui.py [搜索关键词]
```

操作方式：

| 动作 | 快捷键 / 操作 |
|------|-------------|
| 搜索 | 输入关键词 → `Enter` |
| 浏览结果 | `↑↓` 或鼠标滚轮 |
| 选择视频 | `Enter` 或鼠标点击 |
| 返回搜索 | `Esc` 或点击按钮 |
| 复制 1080p URL | 点击按钮 |
| 复制全部 URL | 点击按钮 |
| 退出 | `Ctrl+Q` |

## 环境要求

- Python 3.12+
- Conda（推荐）

## 安装

```bash
# 创建环境
conda create -n hanime-scraper python=3.12
conda activate hanime-scraper

# 安装依赖
pip install cloudscraper beautifulsoup4 lxml textual textual-dev
```

## 依赖

| 包 | 用途 |
|----|------|
| [cloudscraper](https://github.com/venomous/cloudscraper) | 绕过 Cloudflare JS Challenge |
| [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) | HTML 解析 |
| [lxml](https://lxml.de/) | 高性能 XML/HTML 解析器 |
| [textual](https://textual.textualize.io/) | TUI 框架（鼠标/键盘交互） |
| [textual-dev](https://github.com/Textualize/textual-dev) | Textual 开发工具 |

## 项目结构

```
get-hanime/
├── scrape_hanime.py    # 首页爬虫
├── hanime_tui.py       # TUI 搜索 + 视频源提取
├── CLAUDE.md           # AI 助手指引
├── README.md           # 本文件
└── .gitignore
```

## 技术要点

- 目标站点使用 **Cloudflare** 防护，通过 `cloudscraper` 模拟 Chrome 浏览器指纹绕过
- 视频页中 `<video#player>` 含多个 `<source>` 子元素，按 `size` 属性区分分辨率
- 搜索结果中需过滤 `erolabs`/`erodalabs` 广告链接
- TUI 基于 `textual` 8.x，使用 `@work(thread=True)` 实现不阻塞 UI 的后台网络请求

## 许可

[MIT License](https://opensource.org/licenses/MIT)

Copyright (c) 2026
