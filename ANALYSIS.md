# epub_tool 项目分析报告

> 分析时间：2026-10-19 ｜ 代码基线：`447e8b3 v1.3 基于 1920x1080 重新排版`（工作树干净）
> 方式：全量通读源码 + 17 项内置自检 + 3 轮针对性探针（探针位于 `C:\projects\_scratch\probe*.py`）
>
> **注**：本报告行号对应 v1.3 基线。P0 问题已在该版本修复，修复清单与验证结果见文末
> 《附录 B：v1.2 修复记录》。

---

## 一、项目概览

| 项 | 内容 |
| --- | --- |
| 定位 | 零第三方依赖（标准库 + tkinter）的 EPUB 制作/维护小工具，中文网络小说场景 |
| 规模 | 3 个 Python 文件共约 1800 行：`epub_tool.py`(1336) / `epub_ops.py`(264) / `build_epub.py`(210) |
| 入口 | `epub_tool.py`（GUI 5 个标签页 + `--selftest`）、`epub_ops.py`（CLI：unpack/append）、`build_epub.py`（一次性硬编码脚本） |
| 功能 | ①目录→EPUB ②单文件切割→EPUB ③EPUB 追加章节 ④封面查看/替换 ⑤EPUB→TXT 拆解 |
| 依赖 | 运行时 0；构建 `pyinstaller>=6.0` |
| 现状 | `--selftest` 17/17 全部通过；三个提交（v1.1→v1.3）功能闭环完整 |

**一句话评价**：功能闭环已经跑通、单文件工具链完整，用户视角"能用"；但核心逻辑三处重复实现且已开始分叉，XML 全靠正则字符串拼改，章节切分与导出存在若干**静默丢内容**的缺陷，工程化（测试/打包/文档）仍是脚本级水准。

---

## 二、架构与代码地图

```
epub_tool.py ──── 核心逻辑(33-715) ──┬── read_text / chapter_xhtml / build_epub
    │                                ├── SPLITTERS 注册表 (262-267)
    │                                ├── append_chapters (299-425)
    │                                ├── find_cover_ref / extract_cover / set_cover (434-585)
    │                                └── _chapter_order_from_epub / epub_to_txt (640-715)
    ├───────── selftest(722-867)：17 条断言，直接 sys.exit
    └───────── GUI(874-1304)：5 个 Tab + LogBus + run_job 后台线程

epub_ops.py ───── 复制了上面 5 段逻辑（chapter_xhtml / opf 追加 / nav / ncx / 重打包），
                  但**不支持 markdown 标题**、CSS 解析方式不同 → 已分叉

build_epub.py ─── 又一个独立实现，写死 C:\Users\vasts\Downloads\大明测字天师.txt，
                  并 assert 章节数 == 896
```

**关键判断**：`epub_ops.py` 和 `build_epub.py` 不是"命令行封装"，而是**同一套逻辑的第二、第三次拷贝**。任何一处协议修正（比如导航嵌套、OPF 属性顺序）都要改三遍，事实上现在已经改漏了（见 P0-5、P1-2）。

数据流：

```
txt/md ──read_text──> lines ──split_*──> [(标题, 正文行)]
                                            │
                                            ├─build_epub──> 临时目录 ──zip──> epub
                                            └─z.write 直写（目前没有，属优化点）

epub ──extractall──> 文件树 ──正则改 OPF/NAV/NCX──> 重新 zip
epub ──zip 读取──> xhtml ──HTMLParser──> [(标题, 正文行)] ──> txt
```

---

## 三、问题清单

### P0 — 正确性 / 静默丢内容（用户会真实踩到）

| # | 问题 | 位置 | 实测证据 | 影响 |
| --- | --- | --- | --- | --- |
| P0-1 | **首个分隔符之前的内容被整段丢弃**（前言、内容简介、作者的话） | `epub_tool.py:215-226`、`epub_tool.py:229-239`、`epub_ops.py:61-86`、`build_epub.py:19-37` | 喂入"本书简介…" + 2 个横线章节 → 切出 2 章，`前导内容是否保留: False` | 静默丢数据，用户无感知 |
| P0-2 | **EPUB→TXT 把章节内 h2/h3 小标题并进章节标题并从正文删除** | `epub_tool.py:590-637` | `<h1>第一章</h1><h2>第一节 小节</h2><h3>细节</h3>` → 标题变成 `'第一章第一节 小节细节'`，正文只剩 3 段 | 有分节/小标题的书导出后结构错乱 |
| P0-3 | **追加章节时，nav 为多级目录会把新章节插进别人的子目录** | `epub_tool.py:380-390`、`epub_ops.py:194-206` | 实测新 `<li>` 出现在 `<ol>` 嵌套层里、位于"第一节"之后，而非顶层末尾 | 目录层级错乱，阅读器内导航错位 |
| P0-4 | **章节顺序解析只用 nav 的部分匹配结果，不再回退 ncx/spine** | `epub_tool.py:640-666` | 2 章的 nav（第二条带 `class="x"`）→ 只解析出 `['a.xhtml']`，B 章整章丢失 | 拆解导出**静默漏章**，最严重的一条 |
| P0-5 | **`find_cover_ref` 对属性顺序敏感** | `epub_tool.py:434-446` | `properties` 写在 `href` 之前 → 返回 `None`；`properties` 在后 → 正常 | 换封面时会**再插一个** `cover-image`，生成双封面、OPF 不合规 |
| P0-6 | **非 UTF-8 章节内容被硬当 UTF-8 解码** | `epub_tool.py:692`、`epub_tool.py:645`、`epub_tool.py:459` | GBK 编码的 xhtml → 导出 txt 全是 `�` | GBK/GB18030 老书拆解后正文报废 |

> 说明：P0-1 在三个文件里各有一份实现，**修的时候要一起修**，否则 CLI 与 GUI 行为不一致。

### P1 — 健壮性与一致性

| # | 问题 | 位置 | 说明 |
| --- | --- | --- | --- |
| P1-1 | `build_epub` 没有 `try/finally` | `epub_tool.py:96-208` | 实测中途抛错后残留 `.epub_build_c6ebd945`；`append/set_cover` 都有 finally，唯独主流程没有 |
| P1-2 | 三份重复实现已分叉 | `epub_tool.py` / `epub_ops.py` / `build_epub.py` | `epub_ops` 不支持 md 标题；CSS 定位方式不同；编号策略不同（一处续号 `chap_0004`，另一处退化为 `newchap_001`） |
| P1-3 | `build_epub.py` 是一次性脚本 | `build_epub.py:10-14,67` | 绝对路径写死、`assert len(chapters) == 896`，对任何其他书都直接崩；`assert` 在 `python -O` 下失效 |
| P1-4 | 预留功能在 UI 里可选 | `epub_tool.py:266` + `epub_tool.py:1052-1054` | "LLM智能切割（预留，未接入）"出现在下拉框，选中必然抛 `NotImplementedError` |
| P1-5 | 版本号与仓库不一致 | `epub_tool.py:27` | 代码里 `VERSION = "1.0"`，而 git 已到 v1.3 |
| P1-6 | 用正则改 XML（OPF/NAV/NCX） | `epub_tool.py:333-378` | 要求 `<itemref idref="x"/>` 紧邻；单引号属性、`<spine>` 带其他属性、自闭合、命名空间前缀都会漏改且不报错 |
| P1-7 | 固定行数切分把首行吃掉当标题 | `epub_tool.py:242-251` | 无章节标题的书，每块第一行会变成 h1，正文少一行 |
| P1-8 | 输出与源同路径、已存在直接覆盖，无备份 | `epub_tool.py:200-201,410-411,571-572` | 覆盖前不提示、不备份 |
| P1-9 | GUI 硬编码 1920×1080 | `epub_tool.py:911` | 1366×768/笔记本上窗口超出屏幕；`minsize(800,600)` 只是下限 |
| P1-10 | 无进度、无取消、多标签可并发跑 | `epub_tool.py:892-904` | 只置灰当前按钮；两个 Tab 可同时改同一输出；长任务只能等 |
| P1-11 | 日志区无上限、Tab3 每 100ms 永久轮询 | `epub_tool.py:934-940`、`epub_tool.py:1140-1142` | 大量日志后卡顿；`refresh_label` 永不停止 |
| P1-12 | 输出正文行可能撞上分隔符 | `epub_tool.py:711` | 正文里若出现整行 12 个横线，往返切割会多切一章 |

### P2 — 工程化与体验

- **测试**：`selftest()` 内嵌在 GUI 文件里、直接 `sys.exit`、结果写到程序目录（`epub_tool.py:724`），打包成 exe 后写入 `_MEI` 临时目录、跑完即丢；无法断言异常路径，也不是 pytest。
- **打包**：`.gitignore` 忽略 `*.spec`，仓库里却躺着 `epub_tool.spec`（未跟踪）；`build_exe.bat` 用命令行参数重新生成 spec，二者不一致（spec 里 `upx=True` 实际不生效）；无图标；onefile 26 MB，每次启动解压。
- **仓库卫生**：`build/` + `dist/` 共 27 MB 产物留在工作树；无 LICENSE；无 lint/format 配置。
- **控制台编码**：`--selftest` 在 Windows 默认代码页下中文全乱码（本次已观察到），需 `sys.stdout.reconfigure(encoding='utf-8')` 或 `chcp 65001`。
- **缺少校验环节**：没有 epubcheck 之类的合规检查，P0-5 那类问题只能靠人肉发现。
- **性能**：先写约 1000 个文件到磁盘再 `zip` 打包，实测 500 章 / 10 万行耗时 3.18 秒、产物 0.42 MB；可直接 `ZipFile.writestr` 流式写入，省掉整轮磁盘 I/O。
- **能力缺口**：无拖拽、无批量、无章节预览/手动调整顺序、无元数据（作者/简介/出版日期）编辑、无多书合并、不支持加密 EPUB、`build_epub` 只吃纯文本（无法保留图片/字体资源）。
- **可发现性**：`_make_uuid` 用"标题+章节数"做种子，同名同章数的两本书会得到同一个 `dc:identifier`。

---

## 四、优化路线图

原则：**保持零第三方运行时依赖**（这是项目最有价值的卖点），只在开发侧引入 pytest/ruff。

### 阶段 0 — 建立回归基线（0.5 天）
1. 把 `selftest()` 拆成 `tests/` 下的 pytest 用例，现有 17 条断言原样迁移；
2. 为上面 6 个 P0 各写一个**复现用例**，先标 `xfail`，修完转正；
3. 准备 3 个真实样本：纯 UTF-8、GBK、多级 nav 的 EPUB，纳入 fixtures。

### 阶段 1 — 修 P0（1–2 天）
- 切分保留前导内容（作为"前言"章或并入首章）；
- 小标题按层级转成 `## 标题` 文本行，不再吞进章节名；
- 重写 nav/NCX 编辑：定位**顶层 `<ol>`**、支持 `<li>` 带属性、多级目录保持原层级；
- 章节顺序解析：nav 解析"拿到多少算多少，缺失的用 ncx/spine 补齐"，绝不因部分匹配而丢章；
- 封面识别改为**解析 manifest 的 item 属性集合**，不再依赖顺序；
- 所有 xhtml/txt 读取走统一编码探测（BOM → 声明的 charset → utf-8 → gb18030 → 兜底）。

### 阶段 2 — 抽核心层，消重复（1–2 天）
目标结构（保持单文件入口，不破坏 bat 脚本）：

```
epubkit/            # 纯逻辑包，无 tkinter
  __init__.py
  textio.py         # 编码探测 + 切分注册表（规则化，可扩展）
  build.py          # 目录/切分 → EPUB（流式写 zip）
  edit.py           # 追加章节、封面、元数据
  extract.py        # EPUB → 文本
  package.py        # OPF/NAV/NCX 的结构化读写（ElementTree，替代全部正则）
cli.py              # argparse：build / append / cover / extract / split
gui.py              # tkinter，只调用 epubkit
epub_tool.py        # 入口薄壳：无参数开 GUI，--cli 转交，--selftest 转 pytest
```
- `epub_ops.py`、`build_epub.py` 降级为一行转发的兼容包装（或直接删除并在 README 注明）；
- 用 `dataclass` 描述 `Book/Chapter/ManifestItem`，杜绝字符串拼接。

### 阶段 3 — GUI 与体验（1 天）
- 布局自适应（`winfo_screenwidth` 比例 + 可缩放三栏），不再写死分辨率；
- 文件/目录拖拽（标准库无原生支持，可用 `tkinterdnd2` 作为**可选**依赖，缺失时自动降级）；
- 进度条 + 取消按钮（`job` 传入 `cancel_event`，切分/打包循环里检查）；
- 全局任务互斥（任务总线），日志区限 5000 行；
- 章节清单预览：切分后先列出"标题/字数"再确认打包。

### 阶段 4 — 可选增强
元数据编辑（作者/简介/系列/封面一体化表单）、批量转换、EPUB 合并、epubcheck 集成（调用 jar，可选）、导出 EPUB2 兼容包、合并时保留图片/字体。

---

## 五、结论与建议

1. 这个项目**不是"写得烂"，而是"长成了三份"**。功能完整、自检覆盖也做到了 17 条，说明作者有测试意识；真正拖累它的是重复实现和字符串级 XML 操作。
2. **优先级建议**：P0-4（拆解漏章）和 P0-3（nav 层级错位）对读者可见性最强，应最先修；P0-1/P0-2 是静默丢内容，修复成本最低但收益最大；P0-5 影响成品合规性。
3. **不建议**先做 UI 美化。当前 GUI 只是核心逻辑的一层薄壳，等阶段 2 抽完核心层后，GUI 重写会便宜得多。
4. 阶段 0+1+2 完成后，这个项目就从"能用的脚本"升级为"可长期维护的小工具"，总工作量约 3–5 天。

---

## 附：本次分析使用的验证手段

| 手段 | 结果 |
| --- | --- |
| `python epub_tool.py --selftest` | 17/17 PASS（功能链路本身是通的） |
| `probe.py` | 复现 P0-3、P0-5、P1-1，并测得构建性能 500 章 3.18 s |
| `probe2.py` | 复现 P0-6、章节在子目录时的编号退化 |
| `probe3.py` | 复现 P0-1、P0-2，确认固定行数切分吃首行、bat 行尾为 CRLF 正常 |
| `py_compile` | 三个源文件语法正常 |
| `git status` / `git ls-files` | 工作树干净；`build/`、`dist/`、`*.spec` 未被跟踪 |

---

## 附录 B：v1.2 修复记录

修复范围按约定只做 P0（正确性），外加一条零成本的 P1（临时目录清理）。

| 编号 | 问题 | 修复方式 | 位置（v1.2） |
| --- | --- | --- | --- |
| P0-1 | 前导内容被丢弃 | 新增 `PREFACE_TITLE` / `_with_preamble()`：首个分隔标记前的非空内容作为「前言」章；分隔符行本身不算内容 | `epub_tool.py` 切分区、`epub_ops.read_new_chapters` |
| P0-2 | h2–h6 被吞进章节名 | 重写 `_ChapterTextParser`：首个 `h1` 作标题，其余标题输出为 `## ` / `### ` 文本行，`to_html_blocks` 重新打包时还原 | `epub_tool.py` `_ChapterTextParser` |
| P0-3 | 多级 nav 插入错位 | 新增 `insert_list_items()`：按标签深度配对，只在最外层 `<ol>/<ul>` 闭合处插入；找不到列表时显式告警 | `epub_tool.append_chapters`、`epub_ops.append_chapters` |
| P0-4 | 章节顺序漏章 | 重写 `_chapter_order_from_epub()`：nav → ncx → spine → 包内其余正文页，逐级合并去重；href 去锚点、URL 解码、支持子目录 | `epub_tool.py` |
| P0-5 | 封面识别依赖属性顺序 | 新增 `parse_attrs()` / `iter_tags()` / `rewrite_tag()`，改为解析属性集合；替换封面时合并而非覆盖 `properties` | `epub_tool.py` 封面区 |
| P0-6 | GBK 章节乱码 | 新增 `decode_bytes()`：BOM → 文件头声明 → utf-8 → gb18030 → big5；opf/nav/ncx 按原编码写回 | `epub_tool.py`、`epub_ops.py` |
| P1-1 | 临时目录残留 | `build_epub` 拆成 `build_epub` + `_write_epub_tree`，用 `try/finally` 清理 | `epub_tool.py` |

**验证**：

- 新增 `tests/test_regressions.py`，33 项用例全部通过（`python -m unittest discover -s tests -v`）；
- 原有 `--selftest` 17 项仍全部通过；
- 三个探针脚本重跑，原先复现的问题全部转为预期行为（前导内容保留、小标题保留、
  nav 插到顶层、两章都解析出来、封面两种属性顺序均识别、GBK 正文可读、无临时目录残留）。

**未做（留待下一阶段）**：P1-2 三份实现合并、P1-6 结构化 XML、P1-7 固定行数切分吃首行、
P1-8 覆盖前备份、P1-9/H GUI 布局与并发、P2 全部。`build_epub.py` 作为一次性脚本保持原样，
计划在抽取 `epubkit/` 时删除。
