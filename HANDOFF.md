# epub_tool 交接文档（HANDOFF）

> **用途**：换一台机器后用 DSH 继续这个项目时，先读这一份。
> 生成时间：2026-10-19 ｜ 作者：上一轮 DSH 会话
> 仓库：<https://github.com/vastsheen/epub_tool.git>，分支 `master`

---

## 0. 一句话现状

`epub_tool` 已经是 **v1.2**：功能完整、48 项回归测试全绿、历史被压缩成 `v1.1 → v1.2` 两条。
当前**真正待办的不是代码**，而是用户侧的阅读器迁移：Kavita 的 EPUB 翻页卡死问题用制品层 CSS 只能缓解（91 章修好、121 章复发），已决定迁到 **Komga + KMReader**，正等用户实测。

---

## 1. 代码状态

| 项 | 值 |
| --- | --- |
| 版本号 | `epub_tool.py` 中 `VERSION = "1.2"` |
| 最新提交 | `cd659b4 v1.2：EPUB→TXT 拆解、分章与封面处理修复、界面/封面/元数据改造` |
| 历史 | `12ad3f2`(v1.1) → `cd659b4`(v1.2)，远端已强推，本地/远端一致 |
| 本地备份分支 | `backup/pre-squash`(375ee28)、`backup/pre-squash-v13`(71b135f)，**只在旧机器上** |
| 测试 | `tests/test_regressions.py` 48 项 + `--selftest` 17 项，均通过 |
| 运行依赖 | 零第三方库（标准库 + tkinter）；仅构建 exe 需要 pyinstaller |

**版本号约定**：用户要求“每次改动都 squash 回 v1.2”，所以**改动后不要自增版本号**，直接并进 v1.2 提交即可（除非用户另行要求）。

---

## 2. 这一轮完成的工作

### 2.1 P0 正确性修复（v1.2 主体）
1. 切分保留首个分隔符 / 首章标题之前的内容（前导内容不再丢）
2. EPUB→TXT 保留 h2–h6 小标题，且章节标题固定 h1、小节从 h2 起（往返层级一致）
3. 多级 nav 追加章节插到**最外层** `<ol>` 末尾（新增 `insert_list_items`）
4. 章节顺序解析改为 nav → ncx → spine → 包内其余正文页 **合并补齐**，不再漏章
5. 封面识别改为解析标签属性，不依赖属性顺序（`parse_attrs`/`iter_tags`/`rewrite_tag`）
6. 编码自适应：BOM → 文件头声明 → utf-8 → gb18030 → big5；opf/nav/ncx 按原编码写回
7. `build_epub` 失败时清理临时目录
8. `epub_ops.py` 同步了前导内容 / nav 层级 / 编码三条，保持 CLI 与 GUI 一致

### 2.2 界面与封面
- 标签页选中态蓝底白字（clam 主题）
- 功能 1/2 可选封面并在界面内预览，打包时整页嵌入（manifest cover-image + meta cover + cover.xhtml `<img>`）
- 功能 3 自动检测 epub 现有封面并预览/替换
- 功能 4 界面内预览 + 保存封面（默认存 `程序目录/封面导出/`）
- 预览零依赖：**GDI+（jpg/png/gif/bmp/tif）→ WIC（WebP 等）→ Tk 原生**；WIC 的虚表序号做了运行时探测
- WebP 封面写入 EPUB 时自动转 PNG（EPUB3 核心格式不含 WebP）
- 窗口按屏幕自适应并最大化

### 2.3 结构与元数据
- 元数据对话框：作者 / 系列(+序号) / 出版日期 / 出版社 / 版权 / 简介 → `dc:*` 与 `calibre:series`
- 卷/章两级：卷首页 `vol_00N.xhtml`，nav 与 NCX 两级嵌套（`dtb:depth=2`）
  - 功能 1：「子目录作为卷」
  - 功能 2：切割方式「卷/章两级（第X卷 + 第X章）」
- 书内目录页 `book-toc.xhtml`（在 spine，`linear="yes"`，界面可关）
- `<guide>` 声明 cover/toc/text；封面 `linear="no"`
- CSS：`@page{margin:0}`、`orphans/widows:2`、`p:last-of-type{margin-bottom:0}`

### 2.4 过程中修掉的连带问题
- `append_chapters` 会把 `book-toc.xhtml`/`vol_*.xhtml` 当成章节 → 续号失效（已用跳过表修正）
- `epub_to_txt` 会把目录页/卷首页导成章节（已在 `_SKIP_PAGE_NAMES` + `_VOL_PAGE_RE` 中排除）
- exe 因目录被打了低完整性标签而无法创建临时目录（沙箱副作用，见第 7 节）

---

## 3. 明确未做 / 用户明确排除

| 项 | 状态 |
| --- | --- |
| P2-6 插图支持（txt 标记图片 → 嵌入 `<img>`） | **未做**，是下一步最大能力缺口 |
| P3-8 脚注/尾注 | 未做 |
| P3-9 ruby 注音、内嵌字体 | 未做 |
| **P3-10 EPUB2 兼容输出模式** | **用户明确要求“一定不做”** |
| `epubkit/` 重构、删除 `build_epub.py` | 未做（README 已知限制里写着） |
| OPF/NAV/NCX 改结构化解析（ElementTree） | 未做，仍是正则 |

---

## 4. 正在处理的问题：Kavita 翻页卡死（重点）

### 4.1 现象
- 书架：`大明测字天师.epub`（本工具生成，896 章）
- Kavita（已从 0.8.9.1 升到 **0.9.1**，镜像 `docker.m.daocloud.io/jvmilazz0/kavita`，端口 5678）
- 双列布局下，某些章节翻到最后一页后**点“下一页”没反应**，进不了下一章
- 特征：该章分页后最后一页只剩一行左右

### 4.2 已确认的机制（Kavita 源码）
`UI/Web/src/app/book-reader/_components/book-reader/book-reader.component.ts`（develop 分支）：

- `maxPages` 来自**服务端**：`this.maxPages.set(chapter.pages)`
- 实际页数由**客户端**按窗口/字号实时算：`getVirtualPage()` → `ceil(totalScroll / pageSize)`
- `nextPage()` 只有在 `oldPageNum + 1 === maxPages` 时才 `loadNextChapter()`
- `setPageNum()` 的钳制是 `Math.min(pageNum, this.maxPages())`（**不是 maxPages-1**）
- 两者对不上时 → `setPageNum` 钳回原值 → `if (oldPageNum === this.pageNum()) return;` → 空转
- 另有 `addEmptyPageIfRequired()`，但只在 2 列且末页不足半页时补空白，覆盖不到所有情况

结论：**服务端页数与客户端排版页数的固有差值**导致，属 Kavita 实现问题，制品无法根治。

### 4.3 已尝试
- 做过一版只改 CSS 的实验稿 `大明测字天师_排版实验.epub`：
  - 去掉 `h1` 的 `page-break-before: always`
  - `p:last-of-type { margin-bottom: 0 }`
  - 段距 `0.4em → 0.35em`
- 结果：**第 91 章从 3 页变 2 页，解开了**；但**第 121 章又卡**（58 段、2336 字，段落多所以对段距更敏感）
- 已向用户说明：这只是把临界点挪了 ~30px，是打地鼠，换字号/窗口还会复发

### 4.4 决策
用户选择**迁移到 Komga + KMReader**（详见第 5 节），Kavita 保留与否待定。
**待办**：等用户用 KMReader 实测 91/121 章，确认 EPUB 阅读器翻章正常。

---

## 5. 选型结论：Komga + KMReader（已确认）

用户硬性条件：**一站式（漫画+小说）、Docker、iOS 客户端能填 http:// 地址、公网也只能 HTTP**。

| 方案 | 分 | 关键点 |
| --- | --- | --- |
| **Komga + KMReader** | **94** | 唯一同时满足：URL 登录、**声明了 ATS 例外可连 HTTP**、原生 EPUB + DIVINA 漫画双阅读器、实时进度同步、多服务器 |
| Komga + Panels | 80 | 漫画一流，EPUB 基本不支持 |
| Calibre-Web + OPDS | 70 | 小说好，无漫画 |
| Audiobookshelf | 60 | 官方文档自称电子书仅“有限支持” |
| Kavita + Komic 等 | 57 | Komic 外网强制 HTTPS，进度同步要付费 |

证据链接：
- KMReader 指南 <https://komga.org/docs/guides/kmreader/>
- KMReader App Store <https://apps.apple.com/app/id6755198424>
- Komga vs Kavita <https://selfhosting.sh/compare/komga-vs-kavita/>

---

## 6. NAS 环境与待执行的 Komga 部署

### 6.1 环境
- 威联通 QNAP，Container Station，Linux；DDNS `vastsheen.mycloudnas.com`
- SSH：`ssh -p 18022 vastsheen@vastsheen.mycloudnas.com`（注意是 `-p`，不是 `host:port`）
- 现有容器端口（节选）：kavita `5678→5000`、jellyfin `8097→8096`、suwayomi `4567`、metatube `8901`

### 6.2 现有 Kavita 挂载（Komga 沿用）
```
/share/resource/manga        -> /manga
/share/resource/novel        -> /novel
/share/resource/adult/book   -> /adult_novel
/share/resource/adult/图     -> /adult_pic
/share/Container/kavita/data -> /kavita/config
```

### 6.3 待执行的 Komga compose（**尚未部署**）
```yaml
services:
  komga:
    image: gotson/komga:latest
    container_name: komga
    ports:
      - "25600:25600"
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - /share/Container/komga/config:/config
      - /share/resource/manga:/data/manga:ro
      - /share/resource/novel:/data/novel:ro
      - /share/resource/adult/book:/data/adult_novel:ro
      - "/share/resource/adult/图:/data/adult_pic:ro"
    restart: unless-stopped
```
注意：`/config` 必须本地盘（H2 文件锁，不能 NFS）；媒体目录只读挂载。

### 6.4 公网访问
用户明确**不加密**，照 Jellyfin 的做法：路由器把外网 25600 映射到 NAS 25600，KMReader 填
`http://vastsheen.mycloudnas.com:25600`。
唯一保留的安全建议：给 KMReader 用**只读账号**，不要用管理员。

### 6.5 迁移注意
- Komga **不支持散图文件夹**（Kavita 支持），漫画需打成 CBZ
- Kavita 的阅读进度不会迁移到 Komga
- Komga 与 Kavita 可同时指向同一目录并行试用，测好了再决定停谁

---

## 7. DSH 沙箱踩坑记录（换机器会复现）

1. **工作区最初不可写**：`grantWrite(C:\projects)` 失败。原因：沙箱进程是**低完整性（Low IL）**，而项目目录没有低完整性标签。
   解决：给项目目录树补 `Low` 标签（`icacls <dir> /setintegritylevel (OI)(CI)L`），并给当前用户补完全控制。
2. **由此带来的副作用**：被打上 Low 标签的 **exe 启动后会被降权**，写不了 `%TEMP%`，报 `Could not create temporary directory!`。
   解决：把 `dist` 目录及产物恢复为 Medium 标签（`icacls ... /setintegritylevel M`），之后再打的 exe 就正常。
3. **`dist` 是 Medium 标签** → 沙箱进程无法往里写：**每次打 exe 都需要 `danger-full-access` 权限**。
4. **`git push` 需要 `danger-full-access`**（沙箱读不到 Windows 凭据管理器）。
5. **无头 Edge 只能在 `danger-full-access` 下跑**（沙箱里会挂起），用于排版测量/截图。
6. `tempfile.mkdtemp()` 创建的目录在沙箱里**不可写**，测试改用 `os.makedirs` 自建临时目录。

---

## 8. 常用命令

```powershell
cd C:\projects\epub_tool

# 回归测试（48 项）
py -3 -m unittest discover -s tests -v

# 端到端冒烟（17 项）
py -3 epub_tool.py --selftest

# 打包 exe（需要 danger-full-access）
py -3 -m PyInstaller --onefile --windowed --name epub_tool --clean --noconfirm epub_tool.py

# 提交并强推（push 需要 danger-full-access）
git add -A
git commit -F <msg file>
git push --force-with-lease origin master
```

---

## 9. 本机（旧机器）文件位置

| 路径 | 内容 |
| --- | --- |
| `C:\projects\epub_tool\` | 项目本体（git 仓库） |
| `C:\projects\epub_tool\ANALYSIS.md` | 最初的分析报告 + 优化路线图（行号对应 v1.3 基线） |
| `C:\projects\epub_tool\tests\` | 48 项回归测试 |
| `C:\projects\epub_tool\dist\epub_tool.exe` | 最近一次打包（v1.2） |
| `C:\projects\_scratch\` | 探针脚本与临时产物：`probe*.py`、`paginate_test.py`、`shot_tabs.py`、`vol_probe.py`、`webp_probe.py` |
| `C:\projects\_scratch\epub\dm.epub` | 《大明测字天师》副本（分析用） |
| `C:\projects\_scratch\ref\xz.epub` | 《雪中悍刀行精校版》副本（结构参考） |
| `C:\projects\epub_tool\大明测字天师_排版实验.epub` | 只改 CSS 的实验稿（91 章已通、121 章未通） |
| `C:\projects\_acl_report\` | 权限修复的备份与回滚脚本 |

> 换机器后这些路径不存在，从仓库 clone 即可拿到代码与文档；`_scratch` 里的探针脚本没入库。

---

## 10. 下一步建议（按优先级）

1. **等 KMReader 实测结果**：漫画一部 + 《大明测字天师》91/121 章；确认后决定 Kavita 去留
2. **部署 Komga**（第 6.3 的 compose），并行试读，不急着停 Kavita
3. 若继续迭代 `epub_tool`：
   - **插图支持（P2-6）**：txt 里用标记引用图片 → 嵌入 `<img>` + 居中样式，这是从“纯文字工具”升级的关键
   - 抽 `epubkit/` 核心包、删掉硬编码的 `build_epub.py`
4. 若 Kavita 那边还要用：可考虑把「排版兼容模式」做进工具（默认关闭），复现第 4.3 的三条 CSS 改动
