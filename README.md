# EPUB 工具箱（epub_tool）

零第三方依赖的 EPUB 图形化工具（Python 标准库 + tkinter）。

## 功能

1. **目录 → 整本 EPUB**：目录内每个 txt/md 文件作为一章（按文件名排序），整体打包。
2. **单文件 → 切割 → EPUB**：把单个文本按切割方式切分章节后打包。切割方式下拉可选，注册表式扩展：
   - 整行 12 个横线分隔（`------------`，大明测字天师式）
   - 章节标题正则（`第X章`）
   - 固定行数切分
   - LLM 智能切割（预留扩展口，见 `epub_tool.py` 中 `split_by_llm`）
3. **EPUB 追加章节**：指定 epub + 若干 txt（每个 txt 一章），解包 → 末尾追加 → 重打包；**原封面自动保留**。
4. **封面管理**：查看（提取封面用系统看图器打开）/ 替换（更新 cover-image 声明与封面文件，输出新文件）。

编码：自动识别 utf-8 / utf-16 / gbk。

## 运行

- 双击 `启动工具.bat`（需 python.org 官方 Python 3，自带 tkinter）
- 或命令行：`python epub_tool.py`
- 自检：`python epub_tool.py --selftest`（验证三个功能 + 封面保留/替换）

## 打包 exe

双击 `build_exe.bat`，或手动执行：

```
py -3 -m pip install pyinstaller
py -3 -m PyInstaller --onefile --windowed --name epub_tool --clean epub_tool.py
```

产物：`dist\epub_tool.exe`（免安装 Python 双击即用）。

## 项目结构

| 文件 | 说明 |
| --- | --- |
| `epub_tool.py` | 主程序（GUI + 核心逻辑 + 切割注册表 + 封面处理） |
| `epub_ops.py` | 命令行工具：解包 / 末尾追加章节 |
| `build_epub.py` | 命令行：按分隔符切分并打包 EPUB |
| `demo_new_chapters.txt` | 追加章节的文本格式示例 |
| `启动工具.bat` | 双击启动脚本 |
| `build_exe.bat` | exe 打包脚本 |
