<div align="center">

# 本地语料检索器

**将按内容哈希命名的归档集合，构建为可按名称检索、按整季导出的本地索引**

[![version](https://img.shields.io/badge/version-v0.2-blue.svg)](VERSION)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.9%2B-yellow.svg)](#环境要求)
[![deps](https://img.shields.io/badge/third--party%20deps-0-brightgreen.svg)](#依赖策略)

[功能](#功能) · [快速开始](#快速开始) · [语料格式](#语料格式) · [原理](#它是怎么工作的) · [命令](#命令速查)

</div>

---

## ⚠️ 版权声明（请先阅读）

**本软件是一个通用的本地归档文本检索工具。它不包含、不托管、不提供、不分发任何数据。**

- 本仓库**只有程序代码**，没有任何语料、字幕、索引或元数据
- 本工具**不访问网络**，不下载任何内容，仅读取使用者置于本机目录中的文件
- 使用者加载的全部数据，**其版权合规性由使用者自行承担全部责任**
- **请勿**使用本工具处理你无权使用的材料，包括未经著作权人许可的影视字幕等
- 开发者**不鼓励、不推荐、不指导**任何人获取任何未经授权的数据

> 本软件按「原样」提供，不附带任何明示或默示担保。
> 作者不对使用者的行为及其后果承担任何责任。

---

## 解决的问题

大量公开语料以**归档集合**形式分发：数万个归档文件，文件名即内容哈希，

```
corpus/zip/2019/3/36efa49f2191e68c6c8db1bd22b2e1df.zip
corpus/rar/2009/c/c93749152900f479ae80f7760006fdee20080315010030.rar
```

仅凭文件名无法判断内容，全部解压又需数百 GB 磁盘空间。结果是已知目标却无法定位，
归档集合等同于不可用。

本工具将此类归档集合构建为**可检索索引**：

```
输入：关键词 + 季号
     ↓
  第 2 季 —— 共 22 集有记录
    S02E01  简英   19 个候选   <release>.S02E01.720p.WEB-DL.简体&英文.ass
    S02E02  简英   19 个候选   ...
    ...
    S02E22  简英   19 个候选   ...
                                     [ ⬇︎ 打包下载整季 ]
```

---

## 功能

### 🔍 按名称检索

支持三种输入形式：中文名、英文名，或完整文件名。

### 📦 三层结果结构：剧名 → 季 → 整季下载 / 分集下载

输入剧名时，结果首先**按季分组**（未指定季号时列出**全部**季）：

```
《某剧》  共 19 季 · 400+ 集 · 5 万个文件

  第 1 季    9 集 · 97 个文件 · 4 个版本      [整季下载] [分集下载]
  第 2 季    27 集 · 79 个文件 · 1 个版本     [整季下载] [分集下载]
  ...
  特别篇     1 个特别篇 · 9 个文件 · 1 个版本  [整季下载] [分集下载]
```

**整季下载** —— 页面顶部给出一个**推荐整季包**，规则如下：

| 情形 | 推荐内容 |
|---|---|
| 存在单一版本覆盖全部集数 | 推荐该版本，整季为同一版本 |
| 没有任何版本覆盖全部集数 | 推荐由多个版本拼成的**补全包**，并注明各集版本可能不同 |

补全包同样保证每集恰好一个字幕文件；若某些集数所有版本都缺，会明确列出缺哪些集。

**推荐以「完整性」为准，不参考画质。** 字幕是文本，与画面分辨率无关：
发布名中的 720p / 1080p 描述的是字幕对应的片源版本，不影响其能否用于其它分辨率的片源，
部分 720p 发布的字幕质量反而更高。因此选取推荐版本时只比较覆盖是否完整
（完整的语言越多越齐全），不做画质排序。

其下按「发布版本」整理，**一个版本一个包**，供自行选择：

```
  ①  720p.HDTV.X264-DIMENSION                 22/22 集
      [下载 简英 22 集 · 5.4 MB] [下载 简体 …] [下载 繁英 …] [下载 繁体 …]

  ②  720p.WEB-DL.DD5.1.H.264-KiNGS            20/22 集  缺 2 集
      [下载 简英 20 集 · 4.9 MB] …
```

每个包内**每集仅包含一个字幕文件**（`S02E01.zh.ass` … `S02E22.zh.ass`），
文件名遵循播放器约定，解压后可直接使用。语言由使用者自行选择。

**分集下载** —— 每一集的**全部版本**逐条罗列，不提供推荐。
**默认收起**，一行一集，便于快速定位目标集数；展开集号可查看该集全部版本，
亦可使用「全部展开」。按集分页（可选 4 / 8 / 16 / 32 集每页）。

剧名仅匹配到一季时自动跳过总览。

进入某一季后，页面顶部提供「← 返回《剧名》全部季」回到季列表；
顶栏标题亦可点击返回首页。

字幕体积统一以 **KB / MB** 显示。

### 🧪 自测

```bash
./run.sh test            # 全部
./run.sh test webui      # 只跑某一类
```

四类检查，都对着**外部事实**或**真实 HTTP 交互**，不做自我比对：

| 类别 | 检查什么 |
|---|---|
| `groundtruth` | 拿 20 部剧的真实季集数（`tests/ground_truth.json`，TVmaze 数据）对答案：有没有幻影季、有没有丢季、集号有没有超 |
| `search` | 30 例单集检索、11 例整季集数、剧名解析、中文名覆盖全部季、`E00` 归类 |
| `webui` | 真起服务、爬总览→整季/分集页、**真下压缩包并解包**，验证每集恰好一个文件、zip 不损坏 |
| `units` | 特别篇识别（含反例）、体积格式、版本标识、GBK/UTF-16 编码往返 |

> 数据集是字幕归档不是完整剧集库，某些季本来就是缺的。所以 ground-truth
> 只查「工具自己造成」的错误：**幻影季、丢季、集号超出现实**。

### 🌗 明暗主题

右上角一键切换，自动记住选择；首次访问跟随系统设置。
也可以用 URL 指定：`?theme=light` / `?theme=dark`。

### 🎬 导出至目标目录

```bash
./run.sh match find /path/to/some.file.mkv
```

```
✅ 已写出: /path/to/some.file.zh.ass
```

文件名遵循播放器约定（**VLC / IINA / MPV / Infuse / Plex / Jellyfin** 都认），
同名视频打开时字幕自动挂上。

| 后缀 | 含义 |
|---|---|
| `.zh.ass` | 简体 / 简英双语 |
| `.zh-Hant.ass` | 繁体 / 繁英双语 |
| `.en.srt` | 英文 |

### 📚 整目录批量补齐

```bash
./run.sh match scan /path/to/library          # 先体检
./run.sh match scan /path/to/library --apply  # 确认后写入
```

已存在的自动跳过，缺失项汇总为清单。

### 🔤 自动处理编码

文本文件可能为 UTF-8 / UTF-16 / GBK / Big5 / Shift_JIS。工具采用**候选打分**
（目标语言字符多者为优，半角片假名、生僻区、控制符为劣）选取最优结果，而非顺序试错：

> GBK 的「为什么」字节是 `CE AA CA B2 C3 B4`，**恰好是合法的 UTF-8**，
> 会被解成 `Ϊʲô` 而不报错；按 cp932 解又会变成半角片假名 `ﾗﾖﾄｻ`。
> 顺序试错两种都会中招，打分不会。

### 🧩 同名不同集不会混淆

`csi` 这类前缀会同时命中名称相近的多个分组。程序不做推断，而是将主列表与
相近分组分开列出，由使用者自行选择：

```
主列表：24 集
⚠️ 还匹配到 3 个相近的分组，可能不是同一批：
    ·《……纽约篇》 18 集
    ·《……迈阿密篇》 12 集
```

不将不同来源的记录合并统计，而是分列呈现。

---

## 快速开始

### 环境要求

- **Python 3.9+**（macOS 自带 `/usr/bin/python3` 即可）
- **`bsdtar`**（macOS 自带；Linux 装 `libarchive-tools`）—— 只有解 rar/7z 用得到
- 约 **600 MB** 磁盘给索引与元数据（语料本身另算）

### 三步启动

```bash
# 1. 克隆
git clone <你的仓库地址>
cd <仓库目录>

# 2. 将语料放入 dataset/（见下一节）

# 3. 一键启动：建环境 → 构建索引 → 启动服务
./start.sh --build-index
```

出现以下输出即表示启动成功：

```
✅ 服务已就绪: http://127.0.0.1:8777/
```

浏览器访问上述地址即可检索。停止服务使用 `./stop.sh`。

> 首次运行约需 1 分钟构建索引（仅读取归档目录列表，不解压正文）。后续启动约需 2 秒。

### 依赖策略

- **零第三方 Python 依赖**，只用标准库（`sqlite3` / `zipfile` / `http.server` …）
- `requirements.txt` 里只有一个**可选**的 `charset-normalizer`，装不上也能跑
- **所有 pip 安装只进仓库内的 `.venv/`，绝不写全局环境**：
  `start.sh` 内部强制 `PIP_REQUIRE_VIRTUALENV=true` 并校验 `sys.prefix != sys.base_prefix`

```bash
# 手动装包也必须走 venv
./.venv/bin/python -m pip install <包名>
```

---

## 语料格式

**本仓库不提供任何语料。** 你需要自己准备，并自行确保其来源与使用方式合法。

### 存放位置

默认是仓库下的 `dataset/` 目录：

```
<仓库目录>/
├── dataset/                  ← 语料归档存放于此
│   ├── data.zip.001
│   ├── data.zip.002
│   ├── data.zip.003
│   ├── data.zip.004
│   └── data2.zip
├── start.sh
└── ...
```

### 归档命名可自定义

默认假设为「一个 4 分卷 zip + 一个独立 zip」。若语料结构不同，
修改 `config.json` 即可，**代码中未硬编码任何具体文件名**：

```json
{
  "corpus_dir": "dataset",
  "split_archive": "data.zip",
  "split_parts": 4,
  "extra_archives": ["data2.zip"]
}
```

- `corpus_dir` —— 语料目录（相对路径按仓库根目录解析，也可写绝对路径）
- `split_archive` + `split_parts` —— 分卷 zip 的名字与卷数（会拼成一个逻辑文件随机读取）
- `extra_archives` —— 其它独立归档

也支持用环境变量临时覆盖：

```bash
CORPUS_DIR=/Volumes/Media/corpus ./start.sh
APP_STORAGE_DIR=/Volumes/Media/store ./start.sh
```

### 支持哪些归档

外层与内层都支持 **zip / rar / 7z / 裸文本文件**。
本工具**不要求语料是字幕** —— `.srt` `.ass` `.ssa` `.txt` `.sub` 都会建索引。

---

## 工作原理

**核心设计：不把语料解压进数据库，索引只存「图书馆的卡片」。**

```
dataset/*.zip              原始归档，正文一直躺在这里
      │
      │  只读「文件名」，不解压正文
      ▼
storage/index/corpus.sqlite    卡片目录
  ├─ archives      归档级信息（路径 / 大小 / 可读名字 / 分类）
  ├─ inner_files   归档内部的**文件名**（一行字符串，不是内容）
  ├─ subs          归一化检索键（release / series / title + 季 + 集 + 语言）
  └─ token_df      词频，用于 IDF 加权打分
```

对比体积即可看出：**索引远小于归档本身**，因为索引中存储的是路径字符串，而非正文。

取文件是**按需读取**：

```
输入检索词
  → 索引表查出候选（字符串匹配，毫秒级）
  → 选取最优记录
  → 打开对应归档，读出其中那个成员，必要时转码
  → 输出结果
```

因此构建索引速度较快，但**归档文件不可删除或移动**。

### 索引构建流程

| 表 | 来源 | 耗时 |
|---|---|---|
| `archives` | 可选的元数据表（放 `storage/meta/`）；没有就只用文件名 | 秒级 |
| `inner_files` | 逐个打开归档读取其**目录**（zip 读中央目录；rar/7z 用 `bsdtar -tvf`） | 视语料规模 |
| `subs` | 规范化文件名：剥语言后缀、解析季集号、生成检索键 | 秒级 |

### 匹配的三级回退

| 级别 | 依据 | 场景 |
|---|---|---|
| **L3** | 发布名完全一致 | 目标文件与归档里的命名完全对上，最准 |
| **L2** | 名字 + `SxxExx` / 第N季 | 换了压制版本也能中 —— 主力路径 |
| **L1** | 关键词相似（IDF 加权） | 电影、被改过名的文件 |

打分采用 **IDF（逆文档频率）**：稀有词权重高，常见词权重低，
所以 `The.Wandering.Earth` 不会误配到 `The.Pillars.of.the.Earth`。

### 可选：可读元数据

归档名为哈希时，仅凭文件名检索效果有限。若持有
「哈希 ↔ 可读名称」对照表，按下列两个文件放入 `storage/meta/`，
检索结果中即会显示中文名、英文名与分类：

```
storage/meta/records.tsv    id  hash  size  file  fullpath  error  name
storage/meta/catalog.tsv    id  cnname  enname  ...  category  lang  format  file  filename
```

两个文件都是 **UTF-8、Tab 分隔、第一行表头**。
**本工具不附带、不下载该元数据** —— 存在则使用，缺失时仍可构建索引。

---

## 文件位置

**代码和配置在根目录，语料和生成物各占一个目录**：

```
<仓库目录>/
├── dataset/          ← 使用者准备的语料（不可删除）
├── storage/          ← 所有生成物，随时可以删掉重建
│   ├── index/        └─ corpus.sqlite + CSV 导出
│   ├── meta/         └─ 可选的元数据 TSV
│   ├── logs/  run/   └─ 日志、PID
├── scripts/          ← 代码
├── .venv/            ← Python 环境
└── start.sh  stop.sh  run.sh  config.json  requirements.txt
```

`dataset/`、`storage/`、`.venv/` **全部在 `.gitignore` 里，不会被 push**。

生成物均可删除后重建：

```bash
rm -rf storage
./start.sh --build-index     # 全部重建
```

---

## 命令速查

```bash
# ── 服务 ──────────────────────────────────
./start.sh                     一键启动（检查环境 → 安装依赖 → 启动服务）
./start.sh --build-index       首次：一并构建索引
./start.sh --foreground        前台跑，Ctrl-C 停
./stop.sh                      一键停止（含全部相关进程）
./stop.sh --status             只看状态
./run.sh status                同上

# ── 检索与导出 ────────────────────────────
./run.sh match find "文件名或关键词" [--lang chs,chs_eng] [--dry-run] [--out 目录]
./run.sh match season "关键词" -s 8 [--out 目录 | --zip 文件] [--episodes 1-5,8]
./run.sh match scan /path/to/library [--apply] [--force]

# ── 索引维护 ──────────────────────────────
./run.sh index build-meta      用 storage/meta/ 里的元数据建表
./run.sh index scan-inner      扫归档内部文件名
./run.sh index verify          校验归档完整性（对 sha256）
./run.sh index stats           统计
./run.sh index extract 关键词 --out /tmp/out
./run.sh match build-subs      重建匹配索引

# ── 手动装依赖（永远只进 .venv）──────────
./.venv/bin/python -m pip install <包名>
```

---

## 配置

`config.json`：

```json
{
  "corpus_dir": "dataset",
  "split_archive": "data.zip",
  "split_parts": 4,
  "extra_archives": ["data2.zip"],
  "host": "127.0.0.1",
  "port": 8777,
  "auto_open_browser": true
}
```

| 字段 | 说明 |
|---|---|
| `corpus_dir` | 语料目录。相对路径按仓库根目录解析，也可写绝对路径 |
| `split_archive` / `split_parts` | 分卷归档的名字与卷数 |
| `extra_archives` | 其它独立归档 |
| `host` | 默认 `127.0.0.1` 只监听本机。改成 `0.0.0.0` 可让局域网设备访问 |
| `port` | 网页端口 |
| `auto_open_browser` | 启动后自动开浏览器 |

环境变量覆盖：`CORPUS_DIR` / `APP_HOST` / `APP_PORT` / `APP_AUTO_OPEN` / `APP_STORAGE_DIR`

---

## 项目结构

```
.
├── start.sh  stop.sh  run.sh     一键启停 + 命令行入口
├── config.json                   配置
├── requirements.txt              依赖清单（只装进 .venv）
├── dataset/                      语料放这里（仓库里只有说明文件）
├── scripts/
│   ├── config.py                 配置与路径解析
│   ├── daemon.py                 启停的进程管理
│   ├── indexer.py                索引构建 / 校验 / 解包
│   └── searcher.py               匹配器 + 网页服务
├── storage/      (gitignored)    生成物：索引 / 元数据 / 日志 / PID
└── .venv/        (gitignored)    Python 环境
```

---

## 运维须知

**重建索引前必须先停止服务。**

```bash
./stop.sh
./start.sh --build-index      # 或 ./run.sh match build-subs
```

服务与重建命令会同时访问同一个 SQLite 库。重建会 DROP 并重写整张表，
服务端持有长连接，并发之下会读到被替换的页，报
`database disk image is malformed`，此后所有检索都会失败。
`build-subs`、`build-meta`、`scan-inner` 已内置检查：服务在运行时会直接拒绝执行。

> 若已出现上述症状：停止服务后执行一次 `PRAGMA wal_checkpoint(TRUNCATE)`，
> 再启动即可恢复（数据本身未损坏）。

---

## 已知限制

- **季集号依据文件名推断**，少数命名不规范的记录无法解析。
- **整批导出为「每条记录取一条」**，不包含同一集的全部语言版本；切换语言使用 `--lang`。
- **不同命名习惯会被拆成不同分组**，程序分开列出而不合并（见[功能](#功能)最后一节）。
- **依赖 `bsdtar`**：没有它时 zip 和裸文本仍可用，rar/7z 不行。
- **元数据是可选的**：没有它就只能靠归档内的文件名检索。

---

## 常见问题

**Q：启动报「语料不齐」？**
将归档放入 `dataset/`，或在 `config.json` 中将 `corpus_dir` 指向语料所在目录。

**Q：启动提示「缺少元数据」？**
元数据为可选项。可将 `records.tsv` 与 `catalog.tsv` 放入 `storage/meta/`，
或忽略此项；此时仍可构建索引并检索，仅结果中不含可读片名。

**Q：取出的文本是乱码？**
在网页上选择「转UTF-8」。命令行 `find` / `season` 默认已转码。

**Q：能放到 NAS 上给电视用吗？**
可以，将 `host` 改为 `0.0.0.0`。但此举会将整个语料库暴露于局域网，请自行评估风险。

**Q：为什么不用 Elasticsearch / 向量检索？**
数十万条记录的字符串匹配与 IDF 打分在 SQLite 上为毫秒级，
为此引入重型依赖缺乏必要性。

---

## 许可证

代码采用 [MIT License](LICENSE)。
**该许可证不适用于任何语料、元数据或第三方内容。**
