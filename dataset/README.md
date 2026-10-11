# 语料放这里

**本目录不随仓库分发任何数据。** 仓库里只保留这个说明文件，让目录结构保持完整。

---

## 一、放什么

把你自己准备的语料压缩包放进本目录。默认约定（可以用 `config.json` 改）：

```
dataset/
├── data.zip.001      ┐
├── data.zip.002      │ 同一个分卷 zip，会拼成一个逻辑文件随机读取
├── data.zip.003      │ 不要单独解压任何一个，也不要合并
├── data.zip.004      ┘
└── data2.zip         独立归档
```

> ⚠️ 分卷少一个都会失败 —— 工具会跨这几个文件直接 seek。

## 二、格式要求

**几乎没有要求。** 本工具不假设语料是什么内容：

| 层级 | 支持 |
|---|---|
| 外层归档 | zip（含分卷）、rar、7z、裸文件 |
| 内层归档 | zip、rar、7z（会自动展开一层） |
| 文件类型 | `.srt` `.ass` `.ssa` `.txt` `.sub` —— 任意纯文本都会建索引 |

如果你的归档命名不是上面那样，改 `config.json`：

```json
{
  "corpus_dir": "dataset",
  "split_archive": "data.zip",
  "split_parts": 4,
  "extra_archives": ["data2.zip"]
}
```

也支持放在别处：

```bash
CORPUS_DIR=/Volumes/Media/corpus ./start.sh
```

## 三、放好之后

```bash
cd ..                       # 回仓库根目录
./start.sh --build-index    # 检查语料 → 建索引 → 起服务
```

想单独校验归档完整性（逐个比对 sha256，需要 `storage/meta/records.tsv`）：

```bash
./run.sh index verify
```

索引、元数据、日志都会写到仓库的 `storage/` 目录，不会混进本目录：

```
storage/
├── index/    索引
├── meta/     可选的可读元数据（records.tsv / catalog.tsv）
├── logs/  run/
```

## 四、可选：可读元数据

如果归档名是无意义的哈希，你可以准备一份「哈希 ↔ 可读名字」对照表放进
`storage/meta/`，检索结果里就能显示可读片名、分类、语言：

```
storage/meta/records.tsv    id  hash  size  file  fullpath  error  name
storage/meta/catalog.tsv    id  cnname  enname  ...  category  lang  format  file  filename
```

UTF-8、Tab 分隔、第一行表头。

**本工具不附带、不下载这份元数据。** 有就用，没有也能建索引 —— 只是结果里
只能看到归档内的原始文件名。

---

## ⚠️ 版权提示

**你加载进本工具的每一份数据，其来源合法性与使用合规性由你自己负全部责任。**

- 本工具只是本地检索程序：不联网、不下载、不上传、不外传
- 本仓库的 **MIT 许可证只覆盖代码**，**不覆盖**本目录中的任何数据
- 请勿加载你无权使用的材料
- 权利人有权依法要求使用者停止使用并销毁侵权复制品

详见仓库根目录 README 顶部的版权声明。
