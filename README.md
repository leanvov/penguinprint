<img src="assets/readme_icon.png" width="56" align="left" hspace="10" vspace="6" alt="penguinprint 图标">

# penguinprint · 企鹅指纹

<br clear="all">

给图片嵌入**不可见**的买家指纹，用于图片售出后的泄露溯源：交付前嵌入，拿到可疑图片后可提取出指纹 ID，反查回是哪个买家。

* 嵌入后画质几乎无损，像素改动极小
* 抗 JPEG 重压缩、抗调色/曲线/亮度调整、抗画面裁剪、抗局部涂抹、抗加噪
* 不依赖人工智能，纯 CPU：只需 `numpy` + `scipy` + `Pillow`
* 自带指纹库、交付记录与图形界面，无需任何在线服务

---

## 1. 它解决什么问题

图片交付给买家后，可能被转发、裁剪、重新压缩、调色，甚至被贴上别人的水印。本程序把一段“买家标识”写成极低码率的指纹嵌进画面，之后即使图片被改动过，通常仍能从中读出指纹 ID，反查回是哪个买家。

需要注意的边界：指纹保护的是**高质量原图**的可溯源性。当图片被过度损坏、质量与原图拉开很大差距时，指纹同样会被破坏 —— 但即便如此，也保证了“**消除指纹**”和“**获得高质量原图**”两者不可能同时得到。

## 2. 工作原理（简述）

1. **载荷**：买家标识经 `HMAC-SHA256(密钥, 规范化标识)` 得到一个短指纹 ID，并附上校验位；嵌入时自动登记进本地指纹库。
2. **铺展**：载荷以像素块为单位，在整个画面重复多遍 —— 每块都完整携带全部载荷，因此裁剪掉大部分画面仍然能还原。
3. **嵌入**：每块在中频 DCT 系数上做**奇偶量化**，并按块的活跃度归一化并**封顶** —— 所以整体调色、亮度变化不影响提取，高对比边缘也不会出现肉眼可见的方块。
4. **提取**：搜索块栅格相位、载荷循环移位与归一化倍率，再用**软判决多数表决 + 纠错**还原载荷；只有校验通过才输出结果，因此几乎不会误报。
5. **密钥**：指纹 ID 用一把随机密钥派生（密钥存在指纹库里，不在程序里）。没有密钥的人无法为某个标识造出一致的指纹，也无法拿买家名单逐个比对、把图上的 ID 反推回邮箱/手机号。
6. **交付记录**：每次嵌入都会记录时间、批次号，以及**源图与输出图的 SHA-256** —— 指纹本身只能证明“归属谁”，交付记录补上“是哪一份文件”。

## 3. 获取与安装

### 方式一：下载打包好的程序（推荐，无需 Python）

到本仓库的 **Releases** 页面下载压缩包：

* **单文件版**压缩包：解压后只有一个 `penguinprint.exe`，放到任意目录双击即可运行，不必安装 Python 或任何依赖。
* **文件夹版**压缩包：解压后是一个文件夹（程序与依赖在一起），运行里面的 `penguinprint.exe` 即可。

两种压缩包任选一个即可，功能完全相同。


### 方式二：从源码运行（适合审查代码或二次开发）

需要 Python 3.9+（推荐 3.11），以及 `tkinter`（Windows 官方安装包默认自带，用于图形界面）。

```
pip install -r requirements.txt
python run_gui.py
```

只用到三个库：`numpy`、`scipy`、`Pillow`。

## 4. 使用（图形界面，推荐）

```
python run_gui.py
```

界面分成三个页签：

**① 嵌入指纹**

1. 「图片或目录」选择要交付的图片，或整个目录（也可只选单张图片）；
2. 「输出目录」留空即可 —— 默认写到 `图片所在目录/_penguinprint_out`，**原图不会被覆盖**；
3. 「买家标识」填买家的标识（邮箱、手机号、订单号、昵称等任意字符串，建议 6~32 个字符）；
4. 「订单号 / 批次」可选，填了会记进交付记录，便于区分同一买家的不同批次；
5. 选强度档位，点「开始嵌入」。

**② 溯源提取**：选择可疑图片或目录 → 点「开始溯源」→ 直接看到买家标识与对应短码。

**③ 指纹库**：查看 / 手动登记 / 删除买家指纹，可导出为 CSV。手动登记时会按嵌入页当前选中的强度档位生成指纹 ID。选中一行点「查看嵌入记录」可看到该买家的每一次交付（时间、批次、源图与输出图哈希、画质 PSNR、是否通过回读校验）。

### 强度档位怎么选

| 档位 | 特点 |
| --- | --- |
| 隐形 | 几乎不可见，但更容易在后续处理中丢失指纹 |
| **标准**（默认） | 画质肉眼无法看出差异，兼顾抗 JPEG 与抗裁剪 |
| 强韧 | 最耐用，画质略降（实际上肉眼也难以发现） |

### 数据放在哪里

* **指纹库** `fingerprints.db`：默认放在用户配置目录 `%APPDATA%\PenguinPrint`；该位置不可写时自动退回程序所在目录。可用「帮助 → 数据目录…」改到任意目录（例如放在 U 盘里随程序移动），「帮助 → 数据目录：恢复默认」可还原。
* **嵌入结果**：写在图片目录下的 `_penguinprint_out` 中，不改动原图。
* 指纹库同时保存了**密钥**与**交付记录** —— 备份它即备份全部身份信息；换电脑时把它一起带过去，两台机器算出的指纹 ID 才会一致。

### 支持的图片格式

JPEG、PNG、BMP、WebP、TIFF。输出保留 ICC；JPEG / WebP / TIFF 另保留 EXIF（PNG 存不下 EXIF，BMP 只存 RGB）。

## 5. 使用（命令行）

```bash
# 嵌入（默认写入 <输入目录>/_penguinprint_out）
python -m penguinprint embed ./photos --identifier buyer@example.com --batch ORDER-001

# 溯源
python -m penguinprint extract ./suspect.jpg

# 指纹库管理（add 必须给 --identifier）
python -m penguinprint registry list
python -m penguinprint registry add --identifier buyer@example.com --note "首单"
python -m penguinprint registry delete --id 1A2B3C4D5E6F70
python -m penguinprint registry export --csv fingerprints.csv
python -m penguinprint registry export-embeds --csv embeds.csv

# 环境自检（嵌入 → 提取闭环，不写入指纹库）
python -m penguinprint selftest

# 图形界面
python -m penguinprint gui
```

指定指纹库用 `--registry 路径`，要写在子命令**之前**：

```bash
python -m penguinprint --registry ./data/fingerprints.db embed ./photos --identifier buyer-A
```

常用选项：`--strength {隐形,标准,强韧}`、`--quality`（JPEG/WebP 输出质量）、`-o/--output`（输出目录）、`--no-recursive`、`--no-verify`（跳过回读校验、更快）、`--no-registry`（不写库）、`-q/--quiet`（不显示进度条）。

## 6. 局限（请先读这一节）

* **不支持几何变换**：缩放、旋转、透视、翻拍。载荷铺在固定的块栅格上，画面一旦被重采样，栅格就会错位而无法解码。若交付链路中会改变分辨率（图库缩略图、聊天软件压缩等），请先用真实链路验证。
* **不支持强局部修改**：大面积涂抹、覆盖、重绘可能破坏局部冗余；小范围修改可以承受。
* **密钥是对称的**：它能挡住第三方伪造，但挡不住“你自己伪造”的质疑。对外主张权利时，请把指纹与下单记录、交付文件哈希一起使用，不要单凭一张图片。
* **不构成法律意见**：本项目提供技术手段，证据效力取决于完整的使用与保全流程。

## 7. 目录结构

```
penguinprint/          核心库
├── payload.py         载荷：标识 → 指纹 ID → 比特序列
├── qim.py             奇偶量化（嵌入的最小单元）
├── embedder.py        嵌入：块活跃度归一化 + 封顶
├── extractor.py       提取：相位/移位/倍率搜索 + 软判决纠错
├── sliding.py         滑窗统计（块活跃度的快速计算）
├── registry.py        指纹库：密钥、ID ↔ 标识、交付记录
├── pipeline.py        批量流程（目录/单文件）
├── imgio.py           图片读写与元数据
├── gui.py             图形界面
├── cli.py             命令行
├── helptext.py        界面内的使用说明文本
└── config.py          参数与强度档位
run_gui.py             图形界面入口
assets/                应用图标
penguinprint.spec      打包配置（图标 + assets）
```

打包成可执行程序（需自行安装 `pyinstaller`）：

```bash
pip install pyinstaller
pyinstaller penguinprint.spec
```

生成两个版本：

* `dist/penguinprint.exe`：单文件版；运行时解包到 `%TEMP%\_MEIxxxxxx`，退出后自动删除。
* `dist/penguinprint/penguinprint.exe`：文件夹版；不解包，也不产生临时文件夹。

## 8. 许可

MIT License，见 [LICENSE](LICENSE)。
