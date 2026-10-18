# -*- coding: utf-8 -*-
"""EPUB 工具箱 —— 图形化工具
功能：
  1. 目录打包：目录内每个 txt/md 文件 = 一章，整体打成一本 epub
  2. 单文件切割打包：把单个 txt 按切割方式切分章节后打包（切割方式可扩展/下拉选择）
  3. EPUB 追加章节：指定 epub + 若干 txt（每个 txt 一章），解包-追加-重打包

运行：python epub_tool.py        （或双击 启动工具.bat）
自检：python epub_tool.py --selftest
"""
import os
import re
import sys
import html
import uuid
import zipfile
import datetime
import shutil
import queue
import threading
import posixpath
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

APP_NAME = "EPUB 工具箱"
VERSION = "1.0"

# ============================================================
# 一、核心逻辑（无 GUI 依赖，可独立测试）
# ============================================================

def read_text(path):
    """自动识别编码读取文本文件（utf-8 / utf-16 / gbk）。"""
    with open(path, 'rb') as f:
        data = f.read()
    for enc in ('utf-8-sig', 'utf-16', 'gbk'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def first_nonempty(lines):
    """返回 (首个非空行, 其余行)；无非空行返回 ('', [])。"""
    for j, ln in enumerate(lines):
        if ln.strip():
            return ln.strip(), lines[j + 1:]
    return "", []


def to_html_blocks(lines):
    """文本行 -> HTML 块；支持简单 markdown 标题（# 开头）。"""
    blocks = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith('#'):
            level = min(len(s) - len(s.lstrip('#')), 6) or 1
            text = html.escape(s.lstrip('#').strip())
            blocks.append('<h%d>%s</h%d>' % (level, text, level))
        else:
            blocks.append('<p>%s</p>' % html.escape(s))
    return '\n'.join(blocks)


def chapter_xhtml(title, body_lines, css='style.css'):
    esc = html.escape(title, quote=True)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
        '<head><meta charset="UTF-8"/><title>%s</title>'
        '<link rel="stylesheet" type="text/css" href="%s"/></head>\n'
        '<body>\n<h1 class="chapter-title">%s</h1>\n%s\n</body>\n</html>\n'
        % (esc, html.escape(css, quote=True), esc, to_html_blocks(body_lines))
    )


def _make_uuid(seed):
    return "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def build_epub(chapters, title, out_path, log=None):
    """把 [(标题, [正文行]), ...] 打成标准 EPUB3（含 NCX 兼容目录）。
    log: 可选回调 log(str)。"""
    def L(*a):
        if log:
            log(' '.join(str(x) for x in a))

    L("开始打包：共 %d 章 -> %s" % (len(chapters), out_path))
    assert chapters, "章节列表为空"
    uid = _make_uuid(title + "|" + str(len(chapters)))
    base = os.path.dirname(os.path.abspath(out_path))
    tmp = os.path.join(base, ".epub_build_" + uuid.uuid4().hex[:8])
    oebps = os.path.join(tmp, "OEBPS")
    meta = os.path.join(tmp, "META-INF")
    os.makedirs(oebps, exist_ok=True)
    os.makedirs(meta, exist_ok=True)

    # 章节文件
    chap_files = []
    for i, (t, body) in enumerate(chapters, start=1):
        fn = "chap_%04d.xhtml" % i
        with open(os.path.join(oebps, fn), 'w', encoding='utf-8') as f:
            f.write(chapter_xhtml(t, body))
        chap_files.append((fn, t))

    # 封面
    with open(os.path.join(oebps, "cover.xhtml"), 'w', encoding='utf-8') as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
            '<head><meta charset="UTF-8"/><title>%s</title>'
            '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            '<body><div style="text-align:center;margin-top:40%%;">'
            '<h1 style="font-size:2em;">%s</h1>'
            '<p style="text-indent:0;color:#666;margin-top:2em;">共 %d 章</p>'
            '</div></body></html>\n' % (html.escape(title), html.escape(title), len(chapters))
        )

    # CSS
    with open(os.path.join(oebps, "style.css"), 'w', encoding='utf-8') as f:
        f.write(
            'body { font-family: "Source Han Serif SC", "Noto Serif CJK SC", "SimSun", serif;'
            ' margin: 0 1em; line-height: 1.8; }\n'
            'h1.chapter-title { text-align:center; font-size:1.4em; margin:1.2em 0 1.5em;'
            ' page-break-before:always; page-break-after:avoid; }\n'
            'p { text-indent:2em; margin:0.4em 0; text-align:justify; }\n'
        )

    # nav (EPUB3)
    items = "\n".join('    <li><a href="%s">%s</a></li>' % (fn, html.escape(t, quote=True))
                      for fn, t in chap_files)
    with open(os.path.join(oebps, "nav.xhtml"), 'w', encoding='utf-8') as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
            ' xml:lang="zh-CN" lang="zh-CN">\n<head><meta charset="UTF-8"/><title>目录</title>'
            '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            '<body><nav epub:type="toc" id="toc">\n<h1 class="chapter-title">目录</h1>\n'
            '<ol>\n%s\n</ol></nav></body></html>\n' % items
        )

    # ncx (EPUB2 兼容)
    navpoints = "\n".join(
        '    <navPoint id="navpoint-%d" playOrder="%d"><navLabel><text>%s</text></navLabel>'
        '<content src="%s"/></navPoint>' % (i, i, html.escape(t), fn)
        for i, (fn, t) in enumerate(chap_files, start=1)
    )
    with open(os.path.join(oebps, "toc.ncx"), 'w', encoding='utf-8') as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            '<head><meta name="dtb:uid" content="%s"/>'
            '<meta name="dtb:depth" content="1"/>'
            '<meta name="dtb:totalPageCount" content="0"/>'
            '<meta name="dtb:maxPageNumber" content="0"/></head>\n'
            '<docTitle><text>%s</text></docTitle>\n<navMap>\n%s\n</navMap>\n</ncx>\n'
            % (uid, html.escape(title), navpoints)
        )

    # content.opf
    manifest = (
        '    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>\n'
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '    <item id="css" href="style.css" media-type="text/css"/>\n'
    )
    for i, (fn, _t) in enumerate(chap_files, start=1):
        manifest += '    <item id="c%04d" href="%s" media-type="application/xhtml+xml"/>\n' % (i, fn)
    spine = '    <itemref idref="cover"/>\n' + \
            "".join('    <itemref idref="c%04d"/>\n' % i for i in range(1, len(chap_files) + 1))
    modified = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(oebps, "content.opf"), 'w', encoding='utf-8') as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
            ' unique-identifier="bookid" xml:lang="zh-CN">\n'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            '  <dc:identifier id="bookid">%s</dc:identifier>\n'
            '  <dc:title>%s</dc:title>\n  <dc:language>zh-CN</dc:language>\n'
            '  <meta property="dcterms:modified">%s</meta>\n</metadata>\n'
            '<manifest>\n%s</manifest>\n<spine toc="ncx">\n%s</spine>\n</package>\n'
            % (uid, html.escape(title), modified, manifest, spine)
        )

    # container.xml
    with open(os.path.join(meta, "container.xml"), 'w', encoding='utf-8') as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles><rootfile full-path="OEBPS/content.opf"'
            ' media-type="application/oebps-package+xml"/></rootfiles>\n</container>\n'
        )

    # 打包
    if os.path.exists(out_path):
        os.remove(out_path)
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.write(os.path.join(meta, "container.xml"), 'META-INF/container.xml')
        for root, _d, files in os.walk(oebps):
            for fn in sorted(files):
                z.write(os.path.join(root, fn), 'OEBPS/' + fn)
    shutil.rmtree(tmp, ignore_errors=True)
    L("完成：%s（%.2f MB，%d 章）" % (out_path, os.path.getsize(out_path) / 1048576, len(chapters)))
    return out_path


# ---------- 切割方式注册表（扩展口：新增方式 = 在此注册一个函数） ----------

def split_by_dash(lines):
    """整行 12 个横线分隔（大明测字天师式）。未找到分隔符返回 None。"""
    seps = [i for i, ln in enumerate(lines)
            if re.fullmatch(r'-{12}', ln.strip())]
    if not seps:
        return None
    chapters = []
    for idx, s in enumerate(seps):
        end = seps[idx + 1] if idx + 1 < len(seps) else len(lines)
        title, rest = first_nonempty(lines[s + 1:end])
        chapters.append((title, rest))
    return chapters


def split_by_title_regex(lines):
    """按“第X章/回/卷”标题行切分。"""
    pat = re.compile(r'^第[一二三四五六七八九十百千0-9０-９]+[章回卷]')
    idxs = [i for i, ln in enumerate(lines) if pat.match(ln.strip())]
    if not idxs:
        return None
    chapters = []
    for k, i in enumerate(idxs):
        end = idxs[k + 1] if k + 1 < len(idxs) else len(lines)
        chapters.append((lines[i].strip(), lines[i + 1:end]))
    return chapters


def split_by_fixed_lines(lines, n=1000):
    """每 n 行切一章，章节标题自动生成。"""
    chapters = []
    for i in range(0, len(lines), n):
        chunk = lines[i:i + n]
        title, rest = first_nonempty(chunk)
        if not title:
            title = '第%d部分' % (i // n + 1)
        chapters.append((title, rest))
    return chapters


def split_by_llm(lines):
    """【预留】LLM 智能切割扩展口。
    实现建议：把全文按段落分批提交给 LLM，由模型返回每个章节的标题与起始行，
    据此切分。返回格式须与其它切割函数一致：[(标题, [正文行]), ...]。"""
    raise NotImplementedError('LLM 智能切割尚未接入，可在 split_by_llm 中实现')


# (显示名, 函数, 参数说明{param: 默认值})；函数为 None 表示预留未实现
SPLITTERS = [
    ("整行12个横线分隔（大明测字天师式）", split_by_dash, {}),
    ("章节标题正则（第X章）", split_by_title_regex, {}),
    ("固定行数切分", split_by_fixed_lines, {"n": 1000}),
    ("LLM智能切割（预留，未接入）", None, {}),
]


def split_file(path, splitter_key, extra=None):
    """读文件并按切割方式切分；返回 [(标题, [正文行]), ...]。"""
    lines = read_text(path).splitlines()
    for label, func, params in SPLITTERS:
        if label == splitter_key:
            if func is None:
                raise NotImplementedError('该切割方式为预留项，尚未实现')
            merged = dict(params)
            if extra:
                merged.update(extra)
            chapters = func(lines, **merged) if merged else func(lines)
            if chapters is None:
                raise ValueError('未能在文件中找到该切割方式对应的分隔标记')
            return chapters
    raise ValueError('未知切割方式: %s' % splitter_key)


# ---------- 追加章节 ----------

CHAP_NAME_RE = re.compile(r'^chap_(\d{4})\.xhtml$')


def _find_opf_dir(extracted):
    for root, _d, files in os.walk(extracted):
        if "content.opf" in files:
            return root
    raise RuntimeError("解包后未找到 content.opf，无法解析 epub 结构")


def append_chapters(epub, chapter_files, out_epub, use_filename_title=True, log=None):
    """把若干 txt（每个 = 一章）追加到 epub 末尾并重打包。
    use_filename_title: True 用文件名作标题；False 用文件内首个非空行。"""
    def L(*a):
        if log:
            log(' '.join(str(x) for x in a))

    L("解包 %s ..." % epub)
    base = os.path.dirname(os.path.abspath(epub))
    tmp = os.path.join(base, ".epub_append_" + uuid.uuid4().hex[:8])
    try:
        with zipfile.ZipFile(epub) as z:
            bad = z.testzip()
            if bad:
                raise RuntimeError("epub 已损坏: " + bad)
            z.extractall(tmp)

        # 读取新章节
        chapters = []
        for cf in chapter_files:
            lines = read_text(cf).splitlines()
            title, rest = first_nonempty(lines)
            if use_filename_title or not title:
                title = os.path.splitext(os.path.basename(cf))[0]
            chapters.append((title, rest))
        if not chapters:
            raise RuntimeError("没有可追加的章节文件")
        L("新章节 %d 个：" % len(chapters), [t for t, _ in chapters])

        opf_dir = _find_opf_dir(tmp)
        opf_path = os.path.join(opf_dir, "content.opf")
        with open(opf_path, 'r', encoding='utf-8') as f:
            opf = f.read()

        manifest_items = re.findall(r'<item id="([^"]+)" href="([^"]+)"', opf)
        hrefs = [h for _i, h in manifest_items]
        content_hrefs = [h for h in hrefs
                         if h.lower().endswith(('.xhtml', '.html'))
                         and os.path.basename(h) not in ('cover.xhtml', 'nav.xhtml')]
        if content_hrefs and all(CHAP_NAME_RE.match(h) for h in content_hrefs):
            nums = [int(CHAP_NAME_RE.match(h).group(1)) for h in content_hrefs]
            start = max(nums) + 1
            new_files = ["chap_%04d.xhtml" % (start + i) for i in range(len(chapters))]
        else:
            existing = set(hrefs)
            new_files, n = [], 1
            while len(new_files) < len(chapters):
                fn = "newchap_%03d.xhtml" % n
                if fn not in existing:
                    new_files.append(fn)
                n += 1

        css_href = next((h for h in hrefs if h.endswith('.css')), 'style.css')
        used_ids = set(i for i, _h in manifest_items)
        new_ids = []
        for fn, (t, body) in zip(new_files, chapters):
            with open(os.path.join(opf_dir, fn), 'w', encoding='utf-8') as f:
                f.write(chapter_xhtml(t, body, css_href))
            iid = re.sub(r'\.x?html$', '', fn)
            n, base_iid = 1, iid
            while iid in used_ids:
                iid = "%s_%d" % (base_iid, n)
                n += 1
            used_ids.add(iid)
            new_ids.append(iid)
        L("新章节文件:", new_files)

        # opf: manifest + spine
        opf = opf.replace('</manifest>',
                          "".join('    <item id="%s" href="%s" media-type="application/xhtml+xml"/>\n'
                                  % (iid, fn) for iid, fn in zip(new_ids, new_files))
                          + '</manifest>', 1)
        opf = opf.replace('</spine>',
                          "".join('    <itemref idref="%s"/>\n' % iid for iid in new_ids)
                          + '</spine>', 1)
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        opf = re.sub(r'<meta property="dcterms:modified">[^<]*</meta>',
                     '<meta property="dcterms:modified">' + now + '</meta>', opf, count=1)
        with open(opf_path, 'w', encoding='utf-8') as f:
            f.write(opf)

        # nav
        nav_path = os.path.join(opf_dir, "nav.xhtml")
        if os.path.exists(nav_path):
            with open(nav_path, 'r', encoding='utf-8') as f:
                nav = f.read()
            if '</ol>' in nav:
                ins = "".join('    <li><a href="%s">%s</a></li>\n'
                              % (fn, html.escape(t, quote=True)) for fn, (t, _b) in zip(new_files, chapters))
                nav = nav.replace('</ol>', ins + '</ol>', 1)
                with open(nav_path, 'w', encoding='utf-8') as f:
                    f.write(nav)

        # ncx
        ncx_path = os.path.join(opf_dir, "toc.ncx")
        if os.path.exists(ncx_path):
            with open(ncx_path, 'r', encoding='utf-8') as f:
                ncx = f.read()
            plays = [int(m) for m in re.findall(r'playOrder="(\d+)"', ncx)]
            nxt = max(plays) + 1 if plays else 1
            ins = "".join(
                '    <navPoint id="np_%s" playOrder="%d"><navLabel><text>%s</text></navLabel>'
                '<content src="%s"/></navPoint>\n'
                % (re.sub(r'[^0-9A-Za-z]', '', fn), nxt + i, html.escape(t), fn)
                for i, (fn, (t, _b)) in enumerate(zip(new_files, chapters)))
            if '</navMap>' in ncx:
                ncx = ncx.replace('</navMap>', ins + '</navMap>', 1)
                with open(ncx_path, 'w', encoding='utf-8') as f:
                    f.write(ncx)

        # 重打包
        if os.path.exists(out_epub):
            os.remove(out_epub)
        with zipfile.ZipFile(out_epub, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            for root, _d, files in os.walk(tmp):
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, tmp).replace('\\', '/')
                    if arc == 'mimetype':
                        continue
                    z.write(full, arc)
        L("完成：%s（%.2f MB）" % (out_epub, os.path.getsize(out_epub) / 1048576))
        L("已保留原 epub 封面（如需更换封面，请使用“封面管理”页）")
        return out_epub
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 封面：提取 / 替换 ----------

MEDIA_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
               '.gif': 'image/gif', '.webp': 'image/webp', '.svg': 'image/svg+xml'}


def find_cover_ref(opf):
    """从 content.opf 文本中找封面引用，返回 (item_id, href) 或 None。
    识别顺序：<meta name="cover"> -> item properties="cover-image"。"""
    m = re.search(r'<meta[^>]*name="cover"[^>]*content="([^"]+)"', opf)
    if m:
        cid = m.group(1)
        for i, h in re.findall(r'<item id="([^"]+)" href="([^"]+)"', opf):
            if i == cid:
                return i, h
    m2 = re.search(r'<item id="([^"]+)" href="([^"]+)"[^>]*properties="[^"]*cover-image[^"]*"', opf)
    if m2:
        return m2.group(1), m2.group(2)
    return None


def extract_cover(epub, out_path, log=None):
    """提取 epub 封面图片到 out_path；无封面返回 None。"""
    def L(*a):
        if log:
            log(' '.join(str(x) for x in a))
    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        opf_name = next((n for n in names if n.endswith('content.opf')), None)
        if not opf_name:
            raise RuntimeError("epub 中未找到 content.opf")
        opf = z.read(opf_name).decode('utf-8', errors='replace')
        ref = find_cover_ref(opf)
        if not ref:
            L("该 epub 未声明封面（无 meta cover / cover-image）")
            return None
        _iid, href = ref
        full = posixpath.join(posixpath.dirname(opf_name), href).lstrip('./')
        if full not in names:
            raise RuntimeError("封面文件不存在于包内: %s" % href)
        data = z.read(full)
        with open(out_path, 'wb') as f:
            f.write(data)
        L("封面已提取 -> %s（%d 字节）" % (out_path, len(data)))
        return out_path


def set_cover(epub, image_path, out_epub, log=None):
    """设置/替换 epub 封面（保留其余内容），重打包输出到 out_epub。
    - 已有封面：更新 manifest 引用与图片文件，保留 item id 与 meta cover
    - 无封面：新增 cover-image item + meta name="cover"
    - cover.xhtml 同步显示新封面图"""
    def L(*a):
        if log:
            log(' '.join(str(x) for x in a))

    if not os.path.isfile(image_path):
        raise RuntimeError("封面图片不存在: %s" % image_path)
    ext = os.path.splitext(image_path)[1].lower()
    mtype = MEDIA_TYPES.get(ext, 'image/jpeg')
    if ext not in MEDIA_TYPES:
        L("警告：未知图片格式 %s，按 image/jpeg 处理" % ext)

    base = os.path.dirname(os.path.abspath(epub))
    tmp = os.path.join(base, ".epub_cover_" + uuid.uuid4().hex[:8])
    try:
        with zipfile.ZipFile(epub) as z:
            bad = z.testzip()
            if bad:
                raise RuntimeError("epub 已损坏: " + bad)
            z.extractall(tmp)
        opf_dir = _find_opf_dir(tmp)
        opf_path = os.path.join(opf_dir, "content.opf")
        with open(opf_path, 'r', encoding='utf-8') as f:
            opf = f.read()

        images_dir = os.path.join(opf_dir, "images")
        os.makedirs(images_dir, exist_ok=True)
        new_name = "cover" + ext
        rel = "images/" + new_name
        shutil.copyfile(image_path, os.path.join(images_dir, new_name))

        ref = find_cover_ref(opf)
        if ref:
            old_id, old_href = ref
            # 更新 manifest 中该 item 的 href 与 media-type（保留 id 与属性）
            pat = re.compile(r'<item\b[^>]*\bid="%s"[^>]*/>' % re.escape(old_id))
            m = pat.search(opf)
            if not m:
                raise RuntimeError("manifest 中找不到 item id=%s" % old_id)
            old_line = m.group(0)
            has_props = 'properties=' in old_line
            new_line = '<item id="%s" href="%s" media-type="%s"%s/>' % (
                old_id, rel, mtype, ' properties="cover-image"' if has_props else '')
            opf = opf.replace(old_line, new_line, 1)
            # 删除旧封面文件（若与新文件不同）
            old_full = posixpath.join(posixpath.dirname(
                os.path.relpath(opf_path, tmp)), old_href).replace('\\', '/')
            old_abs = os.path.normpath(os.path.join(tmp, old_full))
            if os.path.abspath(old_abs) != os.path.abspath(os.path.join(images_dir, new_name)):
                if os.path.isfile(old_abs) and os.path.abspath(old_abs) != os.path.abspath(opf_path):
                    try:
                        os.remove(old_abs)
                    except OSError:
                        pass
            L("替换封面：%s -> %s" % (old_href, rel))
        else:
            # 新增封面
            opf = opf.replace('</manifest>',
                              '    <item id="cover-image" href="%s" media-type="%s" properties="cover-image"/>\n</manifest>'
                              % (rel, mtype), 1)
            if '<meta name="cover"' not in opf:
                opf = opf.replace('</metadata>',
                                  '  <meta name="cover" content="cover-image"/>\n</metadata>', 1)
            L("新增封面：%s" % rel)

        # cover.xhtml：同步显示新封面
        cover_path = os.path.join(opf_dir, "cover.xhtml")
        if os.path.exists(cover_path):
            with open(cover_path, 'r', encoding='utf-8') as f:
                cover = f.read()
            if '<img' in cover:
                cover = re.sub(r'<img[^>]*src="[^"]*"', '<img src="%s"' % rel, cover, count=1)
            else:
                t = re.search(r'<h1[^>]*>(.*?)</h1>', cover, re.S)
                title_text = t.group(1) if t else None
                title_html = ('<h1 style="margin-top:1em;font-size:1.6em;">%s</h1>'
                              % title_text) if title_text else ''
                body_new = ('<body>\n<div style="text-align:center;margin-top:8%%;">\n'
                            '<img src="%s" style="max-width:90%%;max-height:75%%;"/>\n%s\n</div>\n</body>'
                            % (rel, title_html))
                cover = re.sub(r'<body[^>]*>.*?</body>', body_new, cover, count=1, flags=re.S)
            with open(cover_path, 'w', encoding='utf-8') as f:
                f.write(cover)

        # 更新 modified 时间并写回 opf
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        opf = re.sub(r'<meta property="dcterms:modified">[^<]*</meta>',
                     '<meta property="dcterms:modified">' + now + '</meta>', opf, count=1)
        with open(opf_path, 'w', encoding='utf-8') as f:
            f.write(opf)

        # 重打包
        if os.path.exists(out_epub):
            os.remove(out_epub)
        with zipfile.ZipFile(out_epub, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            for root, _d, files in os.walk(tmp):
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, tmp).replace('\\', '/')
                    if arc == 'mimetype':
                        continue
                    z.write(full, arc)
        L("完成：%s（%.2f MB）" % (out_epub, os.path.getsize(out_epub) / 1048576))
        return out_epub
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 二、自检模式（不启动 GUI，验证三个功能）
# ============================================================

def selftest():
    print("== EPUB 工具箱自检 ==")
    work = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest_output")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            ok = False

    # 准备素材
    book_dir = os.path.join(work, "book_dir")
    os.makedirs(book_dir, exist_ok=True)
    for i, t in enumerate(["第一章 开始", "第二章 发展", "第三章 结局"], start=1):
        with open(os.path.join(book_dir, "%02d_%s.txt" % (i, t[3:])), 'w', encoding='utf-8') as f:
            f.write("%s\n\n这是第 %d 章的正文内容。\n" % (t, i))
    single = os.path.join(work, "single.txt")
    with open(single, 'w', encoding='utf-8') as f:
        f.write("------------\n第一章 A\n正文A1。\n正文A2。\n------------\n"
                "第二章 B\n正文B1。\n------------\n第三章 C\n正文C1。\n")
    add1 = os.path.join(work, "番外一.txt")
    add2 = os.path.join(work, "番外二.txt")
    with open(add1, 'w', encoding='utf-8') as f:
        f.write("这是番外一的正文。\n")
    with open(add2, 'w', encoding='utf-8') as f:
        f.write("这是番外二的正文。\n")

    # 功能1：目录 -> epub
    e1 = os.path.join(work, "目录书.epub")
    chs = []
    for fn in sorted(os.listdir(book_dir)):
        if fn.lower().endswith(('.txt', '.md')):
            lines = read_text(os.path.join(book_dir, fn)).splitlines()
            t, rest = first_nonempty(lines)
            chs.append((t or fn, rest))
    build_epub(chs, "目录书", e1)
    with zipfile.ZipFile(e1) as z:
        nav = z.read("OEBPS/nav.xhtml").decode('utf-8')
        n1 = len(re.findall(r'<li><a href="chap_\d+\.xhtml">', nav))
    check("功能1 目录打包（3 章）", n1 == 3)

    # 功能2：单文件切割 -> epub（横线分隔）
    e2 = os.path.join(work, "切割书.epub")
    chs2 = split_file(single, "整行12个横线分隔（大明测字天师式）")
    check("功能2 横线切割识别 3 章", len(chs2) == 3 and chs2[0][0] == "第一章 A")
    build_epub(chs2, "切割书", e2)
    with zipfile.ZipFile(e2) as z:
        nav = z.read("OEBPS/nav.xhtml").decode('utf-8')
        check("功能2 切割书打包（3 章）", len(re.findall(r'<li><a href="chap_\d+\.xhtml">', nav)) == 3)

    # 功能2b：标题正则切割
    chs2b = split_file(single, "章节标题正则（第X章）")
    check("功能2b 标题正则切割识别 3 章", len(chs2b) == 3 and chs2b[0][0] == "第一章 A")

    # 功能3：追加章节
    e3 = os.path.join(work, "目录书_追加.epub")
    append_chapters(e1, [add1, add2], e3, use_filename_title=True)
    with zipfile.ZipFile(e3) as z:
        nav = z.read("OEBPS/nav.xhtml").decode('utf-8')
        ncx = z.read("OEBPS/toc.ncx").decode('utf-8')
        names = z.namelist()
        tail = re.findall(r'<li><a href="(chap_\d+\.xhtml)">(.*?)</a></li>', nav)
        check("功能3 追加后章节数=5", len(tail) == 5)
        check("功能3 新章节在末尾且标题正确",
              tail[-2][1] == "番外一" and tail[-1][1] == "番外二")
        check("功能3 新章节文件存在且进 ncx",
              "OEBPS/chap_0004.xhtml" in names and "OEBPS/chap_0005.xhtml" in names
              and "chap_0005.xhtml" in ncx)

    # 封面功能：最小合法 PNG 生成器（无 PIL 依赖）
    def make_png(color):
        import zlib, struct

        def chunk(t, d):
            c = t + d
            return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
        sig = b'\x89PNG\r\n\x1a\n'
        ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', 4, 4, 8, 2, 0, 0, 0))
        raw = b''.join(b'\x00' + bytes(color) * 4 for _ in range(4))
        idat = chunk(b'IDAT', zlib.compress(raw))
        return sig + ihdr + idat + chunk(b'IEND', b'')

    red_png = os.path.join(work, "cover_red.png")
    blue_png = os.path.join(work, "cover_blue.png")
    with open(red_png, 'wb') as f:
        f.write(make_png((200, 30, 30)))
    with open(blue_png, 'wb') as f:
        f.write(make_png((30, 30, 200)))

    # 功能4：无封面 epub -> 设置封面 -> 提取封面
    e_cov = os.path.join(work, "封面书.epub")
    build_epub([("第一章", ["正文。"])], "封面书", e_cov)
    with zipfile.ZipFile(e_cov) as z:
        check("功能4 初始无封面声明",
              '<meta name="cover"' not in z.read("OEBPS/content.opf").decode('utf-8'))
    set_cover(e_cov, red_png, os.path.join(work, "封面书_红.epub"))
    got1 = extract_cover(os.path.join(work, "封面书_红.epub"), os.path.join(work, "提取_红.png"))
    check("功能4 设置后能提取封面", got1 is not None
          and open(got1, 'rb').read() == open(red_png, 'rb').read())

    # 功能5：追加章节后封面保留
    e_cov2 = os.path.join(work, "封面书_红_追加.epub")
    append_chapters(os.path.join(work, "封面书_红.epub"), [add1], e_cov2)
    got2 = extract_cover(e_cov2, os.path.join(work, "提取_红_追加.png"))
    check("功能5 追加后封面保留且内容不变", got2 is not None
          and open(got2, 'rb').read() == open(red_png, 'rb').read())
    with zipfile.ZipFile(e_cov2) as z:
        nav = z.read("OEBPS/nav.xhtml").decode('utf-8')
        check("功能5 追加后章节数=2", len(re.findall(r'<li><a href="chap_\d+\.xhtml">', nav)) == 2)

    # 功能6：替换封面
    set_cover(e_cov2, blue_png, os.path.join(work, "封面书_蓝.epub"))
    got3 = extract_cover(os.path.join(work, "封面书_蓝.epub"), os.path.join(work, "提取_蓝.png"))
    check("功能6 替换封面生效（蓝色）", got3 is not None
          and open(got3, 'rb').read() == open(blue_png, 'rb').read())
    with zipfile.ZipFile(os.path.join(work, "封面书_蓝.epub")) as z:
        opf = z.read("OEBPS/content.opf").decode('utf-8')
        check("功能6 替换后 manifest 指向新封面",
              'href="images/cover.png"' in opf and 'properties="cover-image"' in opf)

    print("== 自检%s ==" % ("通过" if ok else "失败"))
    print("输出目录:", work)
    sys.exit(0 if ok else 1)


# ============================================================
# 三、GUI
# ============================================================

class LogBus:
    """跨线程日志：后台线程 emit，UI 线程 drain。"""
    def __init__(self):
        self.q = queue.Queue()

    def emit(self, msg):
        self.q.put(str(msg))

    def drain(self, widget):
        try:
            while True:
                m = self.q.get_nowait()
                widget.insert('end', m + '\n')
                widget.see('end')
        except queue.Empty:
            pass


def run_job(state_widget, log, fn):
    """在后台线程执行 fn(log)，运行期间禁用按钮，结束后恢复。"""
    def worker():
        try:
            fn(log)
        except Exception as e:
            log("错误：%s" % e)
            log("---- 任务中止 ----")
        finally:
            log("---- 结束 ----")
            state_widget.after(0, lambda: state_widget.configure(state='normal'))
    state_widget.configure(state='disabled')
    threading.Thread(target=worker, daemon=True).start()


class EpubToolApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("%s v%s" % (APP_NAME, VERSION))
        self.geometry("760x560")
        self.minsize(680, 480)
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=8, pady=8)
        self.tab1 = self._build_tab1(nb)
        self.tab2 = self._build_tab2(nb)
        self.tab3 = self._build_tab3(nb)
        self.tab4 = self._build_tab4(nb)

    # ---- 通用小部件 ----
    def _path_row(self, parent, row, label, browse_cb, var, btn_text="浏览..."):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=4, pady=4)
        e = ttk.Entry(parent, textvariable=var)
        e.grid(row=row, column=1, sticky='ew', padx=4, pady=4)
        ttk.Button(parent, text=btn_text, command=browse_cb).grid(row=row, column=2, padx=4, pady=4)
        return e

    def _log_area(self, parent, row):
        txt = scrolledtext.ScrolledText(parent, height=10, state='normal', wrap='word')
        txt.grid(row=row, column=0, columnspan=3, sticky='nsew', padx=4, pady=6)
        return txt

    # ---- Tab1 目录打包 ----
    def _build_tab1(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  1 目录→整本EPUB  ")
        var_dir = tk.StringVar()
        var_title = tk.StringVar()
        var_out = tk.StringVar()

        def browse():
            d = filedialog.askdirectory(title="选择包含章节文件的目录")
            if d:
                var_dir.set(d)
                var_title.set(os.path.basename(d) or "我的书")
                var_out.set(os.path.join(d, (os.path.basename(d) or "book") + ".epub"))

        ttk.Label(tab, text="目录内每个 txt/md 文件将作为一章（按文件名排序）。").grid(
            row=0, column=0, columnspan=3, sticky='w', padx=4, pady=2)
        self._path_row(tab, 1, "源目录:", browse, var_dir)
        self._path_row(tab, 2, "书名:", lambda: None, var_title)
        self._path_row(tab, 3, "输出:", self._browse_out(var_out), var_out)
        log = self._log_area(tab, 4)

        def run():
            d = var_dir.get().strip()
            if not d or not os.path.isdir(d):
                log.insert('end', "请先选择有效的源目录\n")
                return
            title = var_title.get().strip() or os.path.basename(d)
            out = var_out.get().strip()
            if not out:
                out = os.path.join(d, title + ".epub")
                var_out.set(out)
            files = sorted(f for f in os.listdir(d)
                           if f.lower().endswith(('.txt', '.md'))
                           and os.path.isfile(os.path.join(d, f)))
            if not files:
                log.insert('end', "目录内没有 txt/md 文件\n")
                return
            bus = LogBus()

            def job(lg):
                lg("找到 %d 个章节文件：%s" % (len(files), ", ".join(files)))
                chapters = []
                for fn in files:
                    lines = read_text(os.path.join(d, fn)).splitlines()
                    t, rest = first_nonempty(lines)
                    chapters.append((t or fn, rest))
                build_epub(chapters, title, out, lg)

            run_job(run_btn, bus.emit, job)
            self._poll(bus, log, run_btn)

        run_btn = ttk.Button(tab, text="开始打包", command=run)
        run_btn.grid(row=5, column=0, columnspan=3, pady=8)
        return tab

    # ---- Tab2 单文件切割打包 ----
    def _build_tab2(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  2 单文件→切割→EPUB  ")
        var_file = tk.StringVar()
        var_split = tk.StringVar(value=SPLITTERS[0][0])
        var_lines = tk.StringVar(value="1000")
        var_out = tk.StringVar()
        row = 0

        def browse_file():
            f = filedialog.askopenfilename(title="选择要切割的文本文件",
                                           filetypes=[("文本文件", "*.txt *.md"), ("所有文件", "*.*")])
            if f:
                var_file.set(f)
                var_out.set(os.path.splitext(f)[0] + ".epub")

        def browse_dir():
            d = filedialog.askdirectory(title="选择目录（取其内唯一可转换文件）")
            if not d:
                return
            files = [f for f in os.listdir(d)
                     if f.lower().endswith(('.txt', '.md'))
                     and os.path.isfile(os.path.join(d, f))]
            if len(files) == 1:
                var_file.set(os.path.join(d, files[0]))
                var_out.set(os.path.join(d, os.path.splitext(files[0])[0] + ".epub"))
            elif not files:
                messagebox.showinfo("提示", "该目录内没有 txt/md 文件")
            else:
                messagebox.showinfo("提示", "目录内有 %d 个可转换文件，请直接用“选择文件”明确指定一个" % len(files))

        ttk.Label(tab, text="选择单个 txt/md 文件，或选择一个仅含一个文本文件的目录。").grid(
            row=row, column=0, columnspan=3, sticky='w', padx=4, pady=2); row += 1
        ttk.Label(tab, text="源文件:").grid(row=row, column=0, sticky='w', padx=4, pady=4)
        ttk.Entry(tab, textvariable=var_file).grid(row=row, column=1, sticky='ew', padx=4, pady=4)
        f1 = ttk.Frame(tab)
        f1.grid(row=row, column=2, padx=4)
        ttk.Button(f1, text="选择文件", command=browse_file).pack(side='left')
        ttk.Button(f1, text="选目录(唯一文件)", command=browse_dir).pack(side='left', padx=4)
        row += 1

        ttk.Label(tab, text="切割方式:").grid(row=row, column=0, sticky='w', padx=4, pady=4)
        cb = ttk.Combobox(tab, textvariable=var_split, state='readonly',
                          values=[s[0] for s in SPLITTERS], width=34)
        cb.grid(row=row, column=1, sticky='ew', padx=4, pady=4)
        ttk.Label(tab, text="每章行数(固定行数切分时生效):").grid(row=row + 1, column=0, sticky='w', padx=4, pady=4)
        ttk.Entry(tab, textvariable=var_lines, width=10).grid(row=row + 1, column=1, sticky='w', padx=4, pady=4)
        row += 2

        self._path_row(tab, row, "输出:", self._browse_out(var_out), var_out); row += 1
        log = self._log_area(tab, row); row += 1

        def run():
            f = var_file.get().strip()
            if not f or not os.path.isfile(f):
                log.insert('end', "请先选择有效的源文件\n")
                return
            out = var_out.get().strip() or os.path.splitext(f)[0] + ".epub"
            split_key = var_split.get()
            bus = LogBus()

            def job(lg):
                extra = {}
                if split_key == "固定行数切分":
                    try:
                        extra = {"n": int(var_lines.get().strip() or 1000)}
                    except ValueError:
                        raise ValueError("每章行数必须是整数")
                chapters = split_file(f, split_key, extra)
                lg("切割成功：%d 章" % len(chapters))
                for t, _b in chapters[:10]:
                    lg("  - " + (t or "(无标题)"))
                if len(chapters) > 10:
                    lg("  ... 共 %d 章" % len(chapters))
                build_epub(chapters, os.path.splitext(os.path.basename(f))[0], out, lg)

            run_job(run_btn, bus.emit, job)
            self._poll(bus, log, run_btn)

        run_btn = ttk.Button(tab, text="切割并打包", command=run)
        run_btn.grid(row=row, column=0, columnspan=3, pady=8)
        return tab

    # ---- Tab3 追加章节 ----
    def _build_tab3(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  3 EPUB追加章节  ")
        var_epub = tk.StringVar()
        var_out = tk.StringVar()
        var_title_mode = tk.BooleanVar(value=True)
        self._added_files = []

        def browse_epub():
            f = filedialog.askopenfilename(title="选择 epub", filetypes=[("EPUB 文件", "*.epub")])
            if f:
                var_epub.set(f)
                var_out.set(os.path.splitext(f)[0] + "_追加.epub")

        ttk.Label(tab, text="每个 txt 文件 = 一章，追加到 epub 末尾（解包→追加→重打包）。").grid(
            row=0, column=0, columnspan=3, sticky='w', padx=4, pady=2)
        self._path_row(tab, 1, "EPUB:", browse_epub, var_epub)

        def add_files():
            fs = filedialog.askopenfilenames(title="选择追加的章节 txt",
                                             filetypes=[("文本文件", "*.txt *.md"), ("所有文件", "*.*")])
            if fs:
                self._added_files.extend(fs)
                log.insert('end', "已添加 %d 个文件（共 %d 个）\n" % (len(fs), len(self._added_files)))

        def add_dir():
            d = filedialog.askdirectory(title="选择目录（目录内所有 txt/md 均为追加章节）")
            if d:
                fs = sorted(os.path.join(d, f) for f in os.listdir(d)
                            if f.lower().endswith(('.txt', '.md'))
                            and os.path.isfile(os.path.join(d, f)))
                self._added_files.extend(fs)
                log.insert('end', "从目录添加 %d 个文件（共 %d 个）\n" % (len(fs), len(self._added_files)))

        self._lbl_count = tk.StringVar(value="（尚未添加）")
        ttk.Label(tab, text="追加章节:").grid(row=2, column=0, sticky='w', padx=4, pady=4)
        ttk.Label(tab, textvariable=self._lbl_count,
                  foreground="#666").grid(row=2, column=1, sticky='w', padx=4, pady=4)
        f2 = ttk.Frame(tab)
        f2.grid(row=2, column=2, padx=4)
        ttk.Button(f2, text="添加文件", command=add_files).pack(side='left')
        ttk.Button(f2, text="添加目录", command=add_dir).pack(side='left', padx=4)
        ttk.Checkbutton(tab, text="用文件名作为章节标题（不勾选则用文件内首个非空行）",
                        variable=var_title_mode).grid(row=3, column=0, columnspan=3, sticky='w', padx=4, pady=4)
        self._path_row(tab, 4, "输出:", self._browse_out(var_out), var_out)
        log = self._log_area(tab, 5)

        def refresh_label():
            self._lbl_count.set("已添加 %d 个章节文件" % len(self._added_files))
            log.after(100, refresh_label)

        def run():
            e = var_epub.get().strip()
            if not e or not os.path.isfile(e):
                log.insert('end', "请先选择 epub 文件\n")
                return
            if not self._added_files:
                log.insert('end', "请先添加要追加的章节文件\n")
                return
            out = var_out.get().strip() or os.path.splitext(e)[0] + "_追加.epub"
            bus = LogBus()

            def job(lg):
                append_chapters(e, list(self._added_files), out,
                                use_filename_title=var_title_mode.get(), log=lg)

            run_job(run_btn, bus.emit, job)
            self._poll(bus, log, run_btn)

        run_btn = ttk.Button(tab, text="解包并追加", command=run)
        run_btn.grid(row=6, column=0, columnspan=3, pady=8)
        log.after(100, refresh_label)
        return tab

    def _browse_out(self, var):
        def cb():
            d = filedialog.asksaveasfilename(title="选择输出位置",
                                             defaultextension=".epub",
                                             filetypes=[("EPUB 文件", "*.epub")],
                                             initialfile=os.path.basename(var.get() or "book.epub"))
            if d:
                var.set(d)
        return cb

    # ---- Tab4 封面管理 ----
    def _build_tab4(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  4 封面管理  ")
        var_epub = tk.StringVar()

        def browse_epub():
            f = filedialog.askopenfilename(title="选择 epub", filetypes=[("EPUB 文件", "*.epub")])
            if f:
                var_epub.set(f)

        ttk.Label(tab, text="查看/替换 epub 封面。追加章节后原封面会自动保留，需要换封面时在这里操作。").grid(
            row=0, column=0, columnspan=3, sticky='w', padx=4, pady=2)
        self._path_row(tab, 1, "EPUB:", browse_epub, var_epub)
        log = self._log_area(tab, 2)
        btns = ttk.Frame(tab)
        btns.grid(row=3, column=0, columnspan=3, pady=8)

        def view_cover():
            e = var_epub.get().strip()
            if not e or not os.path.isfile(e):
                log.insert('end', "请先选择 epub 文件\n")
                return
            bus = LogBus()

            def job(lg):
                ext = os.path.splitext(e)[1] or ".png"
                prev = os.path.join(os.path.dirname(e),
                                    os.path.splitext(os.path.basename(e))[0] + "_封面预览" + ext)
                p = extract_cover(e, prev, lg)
                if p:
                    lg("正在用系统看图器打开预览...")
                    os.startfile(p)

            run_job(view_btn, bus.emit, job)
            self._poll(bus, log, view_btn)

        def replace_cover():
            e = var_epub.get().strip()
            if not e or not os.path.isfile(e):
                log.insert('end', "请先选择 epub 文件\n")
                return
            img = filedialog.askopenfilename(
                title="选择封面图片",
                filetypes=[("图片文件", "*.jpg *.jpeg *.png *.gif *.webp"), ("所有文件", "*.*")])
            if not img:
                return
            out = os.path.join(os.path.dirname(e),
                               os.path.splitext(os.path.basename(e))[0] + "_新封面.epub")
            bus = LogBus()

            def job(lg):
                lg("封面图片: %s" % img)
                set_cover(e, img, out, lg)

            run_job(replace_btn, bus.emit, job)
            self._poll(bus, log, replace_btn)

        view_btn = ttk.Button(btns, text="查看封面（提取并用看图器打开）", command=view_cover)
        view_btn.pack(side='left', padx=6)
        replace_btn = ttk.Button(btns, text="替换/设置封面（输出新文件）", command=replace_cover)
        replace_btn.pack(side='left', padx=6)
        return tab

    def _poll(self, bus, log, state_widget):
        def step():
            bus.drain(log)
            if state_widget['state'] == 'disabled':
                self.after(120, step)
        step()


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        app = EpubToolApp()
        app.mainloop()
    except Exception as e:
        # 启动失败时给出可见的中文提示，而不是无声退出
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                APP_NAME,
                "界面启动失败：%s\n\n请确认使用的是 python.org 官方 Python 3（自带 tkinter），"
                "不要用 Microsoft Store 版本。可运行 python epub_tool.py --selftest 自检。" % e)
            root.destroy()
        except Exception:
            print("界面启动失败：%s" % e)
            input("按回车键退出...")
        sys.exit(1)


if __name__ == "__main__":
    main()
