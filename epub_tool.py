# -*- coding: utf-8 -*-
"""EPUB 工具箱 —— 图形化工具
功能：
  1. 目录打包：目录内每个 txt/md 文件 = 一章，整体打成一本 epub
  2. 单文件切割打包：把单个 txt 按切割方式切分章节后打包（切割方式可扩展/下拉选择）
  3. EPUB 追加章节：指定 epub + 若干 txt（每个 txt 一章），解包-追加-重打包
  4. 封面管理：检测 / 预览 / 替换 epub 封面
  5. EPUB→TXT 拆解：把 epub 还原成可再次合包的 txt
  6. 漫画→CBZ：单行本 PDF 或条漫/散图目录打包成 CBZ（v1.3，需 Pillow/pypdf）

运行：python epub_tool.py        （或双击 启动工具.bat）
自检：python epub_tool.py --selftest
"""
import os
import re
import sys
import html
import uuid
import zlib
import base64
import ctypes
import struct
import zipfile
import datetime
import shutil
import queue
import tempfile
import threading
import posixpath
from urllib.parse import unquote
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

APP_NAME = "EPUB 工具箱"
VERSION = "1.3"

# ============================================================
# 一、核心逻辑（无 GUI 依赖，可独立测试）
# ============================================================

# ---------- 编码识别 ----------

# 顺序敏感：utf-32 的 BOM 前两字节与 utf-16 相同，必须先判断
_BOMS = (
    (b'\xef\xbb\xbf', 'utf-8-sig'),
    (b'\xff\xfe\x00\x00', 'utf-32'),
    (b'\x00\x00\xfe\xff', 'utf-32'),
    (b'\xff\xfe', 'utf-16'),
    (b'\xfe\xff', 'utf-16'),
)

# 文件头里的 encoding="..." / charset="..." 声明
_CHARSET_RE = re.compile(rb'(?:encoding|charset)\s*=\s*["\']?\s*([A-Za-z0-9_.:-]+)', re.I)

_CHARSET_ALIASES = {
    'utf8': 'utf-8',
    'gbk': 'gb18030', 'gb2312': 'gb18030', 'gb_2312-80': 'gb18030',
    'x-gbk': 'gb18030', 'cp936': 'gb18030', 'ms936': 'gb18030',
    'big5-hkscs': 'big5hkscs',
}


def _normalize_encoding(name):
    key = (name or '').strip().lower()
    return _CHARSET_ALIASES.get(key, key)


def decode_bytes(data):
    """把字节解码为文本，返回 (文本, 实际使用的编码)。

    顺序：BOM → 文件头声明 → utf-8 → gb18030 → big5 → 带替换的 utf-8。
    gb18030 是 gbk/gb2312 的超集，因此统一用它兜住简体中文老书。
    """
    for bom, enc in _BOMS:
        if data.startswith(bom):
            try:
                return data.decode(enc), enc
            except (UnicodeDecodeError, LookupError):
                break

    candidates = []
    m = _CHARSET_RE.search(data[:2048])
    if m:
        declared = _normalize_encoding(m.group(1).decode('ascii', 'ignore'))
        if declared:
            candidates.append(declared)
    candidates += ['utf-8', 'gb18030', 'big5']

    seen = set()
    for enc in candidates:
        if enc in seen:
            continue
        seen.add(enc)
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode('utf-8', errors='replace'), 'utf-8'


def read_text(path):
    """自动识别编码读取文本文件（BOM / 声明 / utf-8 / gb18030 / big5）。"""
    with open(path, 'rb') as f:
        return decode_bytes(f.read())[0]


def read_text_with_encoding(path):
    """读取文本并返回 (文本, 编码)，便于按原编码写回。"""
    with open(path, 'rb') as f:
        return decode_bytes(f.read())


def write_text_with_encoding(path, text, encoding='utf-8'):
    """按指定编码写回文本，保留原有换行风格。"""
    with open(path, 'w', encoding=encoding, newline='') as f:
        f.write(text)


def first_nonempty(lines):
    """返回 (首个非空行, 其余行)；无非空行返回 ('', [])。"""
    for j, ln in enumerate(lines):
        if ln.strip():
            return ln.strip(), lines[j + 1:]
    return "", []


TERMINAL_PUNCT = '。！？…”』」】）.!?'


def reconstruct_paragraphs(lines):
    """物理行 -> 段落列表。

    中文小说 txt 常见形态：行首「　　」标记段落，其余换行只是按固定宽度折行。
    若直接一行一段，折行会被当成新段落（一本书能多出几万段）。这里：
      1. 原文确实大量使用行首全角空格时，只有带标记的行才开新段，其余为续行；
      2. 段尾不是句末标点（，、：等）或引号未闭合时，与下一行合并——修复折行处
         被误插段落标记造成的假分段；但下一段以「“」开头时不再合并，避免把
         新对话吞进上一段；
      3. 完全没有全角空格标记时保持旧的「一行一段」行为，避免把普通文本并成一片。
    """
    marked = sum(1 for ln in lines if ln.startswith('\u3000'))
    total = sum(1 for ln in lines if ln.strip())
    use_marker = marked >= 2 and marked >= total * 0.3

    paras = []
    for ln in lines:
        if not ln.strip():
            continue
        s = ln.strip()
        if use_marker and ln.startswith('\u3000'):
            paras.append(s)
        elif use_marker and paras:
            paras[-1] += s
        else:
            paras.append(s)

    merged = []
    for p in paras:
        if merged:
            prev = merged[-1]
            broken = (prev.count('“') > prev.count('”') and not p.startswith('“')) \
                or prev[-1] not in TERMINAL_PUNCT
            if broken and len(prev) + len(p) <= 300:
                merged[-1] = prev + p
                continue
        merged.append(p)
    return merged


def to_html_blocks(lines, base_level=2):
    """文本行 -> HTML 块；支持简单 markdown 标题（# 开头）。

    章节标题本身已经是 h1，所以正文里的小标题从 h2 起：
    `#` -> <h2>、`##` -> <h3>……最多到 <h6>，不会和章节标题抢层级。

    段落先按行首全角空格还原（见 reconstruct_paragraphs），其余换行视为折行。
    每章首段额外写入两个全角空格并内联 text-indent:0——部分阅读器（如 Komga
    的 web 阅读器）会把「标题后第一段」的缩进强制归零，文字级缩进可以绕开；
    其余段落仍由 CSS text-indent:2em 负责，两者不会叠加。"""
    blocks = []
    buf = []
    state = {'first': True}

    def flush():
        for p in reconstruct_paragraphs(buf):
            text = html.escape(p)
            if state['first']:
                blocks.append('<p style="text-indent:0">\u3000\u3000%s</p>' % text)
                state['first'] = False
            else:
                blocks.append('<p>%s</p>' % text)
        del buf[:]

    for ln in lines:
        s = ln.strip()
        if s.startswith('#'):
            flush()
            n = len(s) - len(s.lstrip('#')) or 1
            level = min(6, base_level + n - 1)
            text = html.escape(s.lstrip('#').strip())
            blocks.append('<h%d>%s</h%d>' % (level, text, level))
        else:
            buf.append(ln)
    flush()
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


def _normalize_chapters(chapters):
    """统一成 [(卷名或 None, 标题, [正文行]), ...]。

    兼容原来的 (标题, [正文行]) 两元组，以及新的 (卷名, 标题, [正文行]) 三元组。"""
    out = []
    for item in chapters:
        if item is None:
            continue
        if len(item) == 3:
            vol, t, body = item
        else:
            t, body = item
            vol = None
        out.append(((vol or '').strip() or None, t, list(body or [])))
    return out


def _group_chapters(chapters):
    """把连续的同一卷章节归为一组：[(卷名或 None, [(标题, 正文行), ...]), ...]。"""
    groups = []
    for vol, title, body in chapters:
        if groups and groups[-1][0] == vol:
            groups[-1][1].append((title, body))
        else:
            groups.append((vol, [(title, body)]))
    return groups


def build_epub(chapters, title, out_path, log=None, cover_image=None,
               meta=None, toc_page=True):
    """把章节列表打成标准 EPUB3（含 NCX 兼容目录）。

    chapters: [(标题, [正文行])] 或 [(卷名, 标题, [正文行])]；相邻且同卷的章节归为一卷。
    log: 可选回调 log(str)。
    cover_image: 可选封面图片路径；给出时嵌入图片并把 cover.xhtml 改成整页封面。
    meta: 可选元数据 dict（creator/date/description/rights/series/series_index/
          publisher/subject/contributor）。
    toc_page: 是否在书内生成可见目录页（放进 spine）。"""
    def L(*a):
        if log:
            log(' '.join(str(x) for x in a))

    chapters = _normalize_chapters(chapters)
    L("开始打包：共 %d 章 -> %s" % (len(chapters), out_path))
    if not chapters:
        raise ValueError("章节列表为空")
    if cover_image:
        if not os.path.isfile(cover_image):
            raise ValueError("封面图片不存在: %s" % cover_image)
        L("封面：%s" % cover_image)
    volumes = []
    for v, _t, _b in chapters:
        if v and v not in volumes:
            volumes.append(v)
    if volumes:
        L("分卷：%d 卷 -> %s" % (len(volumes), "、".join(volumes[:8])))
    uid = _make_uuid(title + "|" + str(len(chapters)))
    base = os.path.dirname(os.path.abspath(out_path))
    tmp = os.path.join(base, ".epub_build_" + uuid.uuid4().hex[:8])
    try:
        return _write_epub_tree(tmp, uid, chapters, title, out_path, L,
                                cover_image, meta or {}, toc_page)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _write_epub_tree(tmp, uid, chapters, title, out_path, L, cover_image=None,
                     book_meta=None, toc_page=True):
    oebps = os.path.join(tmp, "OEBPS")
    meta_dir = os.path.join(tmp, "META-INF")
    os.makedirs(oebps, exist_ok=True)
    os.makedirs(meta_dir, exist_ok=True)
    book_meta = book_meta or {}

    # 章节文件，按卷分组
    plan = []              # [(卷名|None, 卷首页文件名|None, [(章节文件名, 标题, 正文行)])]
    written = 0
    for vi, (vol, items) in enumerate(_group_chapters(chapters), start=1):
        vol_fn = ("vol_%03d.xhtml" % vi) if vol else None
        chaps = []
        for t, body in items:
            written += 1
            fn = "chap_%04d.xhtml" % written
            with open(os.path.join(oebps, fn), 'w', encoding='utf-8') as f:
                f.write(chapter_xhtml(t, body))
            chaps.append((fn, t, body))
        plan.append((vol, vol_fn, chaps))
    has_volume = any(vol for vol, _fn, _c in plan)
    chap_files = [(fn, t) for _v, _vn, cs in plan for fn, t, _b in cs]

    # 封面：有图片时整页显示图片，否则用文字封面页
    cover_item = ''
    cover_meta = ''
    if cover_image:
        data, ext, media = cover_payload(cover_image)
        img_rel = 'images/cover' + ext
        img_dir = os.path.join(oebps, 'images')
        os.makedirs(img_dir, exist_ok=True)
        with open(os.path.join(oebps, img_rel.replace('/', os.sep)), 'wb') as f:
            f.write(data)
        cover_item = ('    <item id="cover-image" href="%s" media-type="%s"'
                      ' properties="cover-image"/>\n' % (img_rel, media))
        cover_meta = '  <meta name="cover" content="cover-image"/>\n'
        cover_body = ('<body>\n<div style="text-align:center;margin:0;padding:0;">\n'
                      '<img src="%s" alt="cover" style="max-width:100%%;"/>\n</div>\n</body>\n'
                      % img_rel)
    else:
        cover_body = ('<body><div style="text-align:center;margin-top:40%%;">'
                      '<h1 style="font-size:2em;">%s</h1>'
                      '<p style="text-indent:0;color:#666;margin-top:2em;">共 %d 章</p>'
                      '</div></body>\n' % (html.escape(title), len(chapters)))

    with open(os.path.join(oebps, "cover.xhtml"), 'w', encoding='utf-8') as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
            '<head><meta charset="UTF-8"/><title>%s</title>'
            '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n%s</html>\n'
            % (html.escape(title), cover_body)
        )

    # CSS
    with open(os.path.join(oebps, "style.css"), 'w', encoding='utf-8') as f:
        f.write(
            '@page { margin: 0; }\n'
            'body { font-family: "Source Han Serif SC", "Noto Serif CJK SC", "SimSun", serif;'
            ' margin: 0 1em; line-height: 1.8; orphans: 2; widows: 2; }\n'
            'h1.chapter-title { text-align:center; font-size:1.4em; margin:1.2em 0 1.5em;'
            ' page-break-before:always; page-break-after:avoid; }\n'
            'h1.volume-title { text-align:center; font-size:1.8em; margin:30% 0 2em;'
            ' page-break-after:avoid; }\n'
            'p { text-indent:2em; margin:0.4em 0; text-align:justify; }\n'
            'p:last-of-type { margin-bottom:0; }\n'
            'ul.book-toc, ul.volume-toc { list-style:none; padding-left:0; }\n'
            'ul.book-toc li, ul.volume-toc li { margin:0.35em 0; line-height:1.6; }\n'
            'ul.book-toc a, ul.volume-toc a { text-decoration:none; color:inherit; }\n'
            'ul.book-toc ol { list-style:none; padding-left:1.2em; }\n'
        )

    # 卷首页（每卷一个标题页 + 卷内目录）
    for vol, vol_fn, chaps in plan:
        if not vol:
            continue
        links = "\n".join('<li><a href="%s">%s</a></li>' % (fn, html.escape(t))
                          for fn, t, _b in chaps)
        with open(os.path.join(oebps, vol_fn), 'w', encoding='utf-8') as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
                '<head><meta charset="UTF-8"/><title>%s</title>'
                '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
                '<body>\n<h1 class="volume-title">%s</h1>\n'
                '<ul class="volume-toc">\n%s\n</ul>\n</body>\n</html>\n'
                % (html.escape(vol), html.escape(vol), links)
            )

    # 目录列表（nav 与书内目录页共用）
    def toc_lines(indent='    '):
        lines = []
        for vol, vol_fn, chaps in plan:
            if vol:
                lines.append('%s<li><a href="%s">%s</a>' %
                             (indent, vol_fn, html.escape(vol, quote=True)))
                lines.append('%s  <ol>' % indent)
                for fn, t, _b in chaps:
                    lines.append('%s    <li><a href="%s">%s</a></li>' %
                                 (indent, fn, html.escape(t, quote=True)))
                lines.append('%s  </ol>' % indent)
                lines.append('%s</li>' % indent)
            else:
                for fn, t, _b in chaps:
                    lines.append('%s<li><a href="%s">%s</a></li>' %
                                 (indent, fn, html.escape(t, quote=True)))
        return '\n'.join(lines)

    # nav (EPUB3)
    with open(os.path.join(oebps, "nav.xhtml"), 'w', encoding='utf-8') as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
            ' xml:lang="zh-CN" lang="zh-CN">\n<head><meta charset="UTF-8"/><title>目录</title>'
            '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            '<body><nav epub:type="toc" id="toc">\n<h1 class="chapter-title">目录</h1>\n'
            '<ol>\n%s\n</ol></nav></body></html>\n' % toc_lines()
        )

    # 书内可见目录页（放进 spine，兼容不认 nav 的阅读器）
    toc_item = ''
    if toc_page:
        with open(os.path.join(oebps, "book-toc.xhtml"), 'w', encoding='utf-8') as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
                '<head><meta charset="UTF-8"/><title>目录</title>'
                '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
                '<body>\n<h1 class="chapter-title">目录</h1>\n<ol class="book-toc">\n%s\n</ol>\n'
                '</body>\n</html>\n' % toc_lines()
            )
        toc_item = ('    <item id="book-toc" href="book-toc.xhtml"'
                    ' media-type="application/xhtml+xml"/>\n')

    # ncx (EPUB2 兼容，卷/章两级)
    play = 0
    np_lines = []
    for vol, vol_fn, chaps in plan:
        if vol:
            play += 1
            np_lines.append(
                '    <navPoint id="np%d" playOrder="%d"><navLabel><text>%s</text></navLabel>'
                '<content src="%s"/>' % (play, play, html.escape(vol), vol_fn))
            for fn, t, _b in chaps:
                play += 1
                np_lines.append(
                    '      <navPoint id="np%d" playOrder="%d">'
                    '<navLabel><text>%s</text></navLabel>'
                    '<content src="%s"/></navPoint>' % (play, play, html.escape(t), fn))
            np_lines.append('    </navPoint>')
        else:
            for fn, t, _b in chaps:
                play += 1
                np_lines.append(
                    '    <navPoint id="np%d" playOrder="%d">'
                    '<navLabel><text>%s</text></navLabel>'
                    '<content src="%s"/></navPoint>' % (play, play, html.escape(t), fn))
    with open(os.path.join(oebps, "toc.ncx"), 'w', encoding='utf-8') as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            '<head><meta name="dtb:uid" content="%s"/>'
            '<meta name="dtb:depth" content="%d"/>'
            '<meta name="dtb:totalPageCount" content="0"/>'
            '<meta name="dtb:maxPageNumber" content="0"/></head>\n'
            '<docTitle><text>%s</text></docTitle>\n<navMap>\n%s\n</navMap>\n</ncx>\n'
            % (uid, 2 if has_volume else 1, html.escape(title), '\n'.join(np_lines))
        )

    # content.opf
    manifest = (
        '    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>\n'
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '    <item id="css" href="style.css" media-type="text/css"/>\n'
    ) + cover_item + toc_item
    for _vol, vol_fn, _c in plan:
        if vol_fn:
            manifest += ('    <item id="%s" href="%s" media-type="application/xhtml+xml"/>\n'
                         % (vol_fn[:-6], vol_fn))
    for fn, _t in chap_files:
        manifest += ('    <item id="c%s" href="%s" media-type="application/xhtml+xml"/>\n'
                     % (fn[5:9], fn))

    spine_items = ['    <itemref idref="cover" linear="no"/>\n']
    if toc_page:
        spine_items.append('    <itemref idref="book-toc" linear="yes"/>\n')
    for _vol, vol_fn, chaps in plan:
        if vol_fn:
            spine_items.append('    <itemref idref="%s"/>\n' % vol_fn[:-6])
        for fn, _t, _b in chaps:
            spine_items.append('    <itemref idref="c%s"/>\n' % fn[5:9])
    spine = "".join(spine_items)

    # 元数据
    def dc(tag, value):
        v = (value or '').strip()
        return ('  <dc:%s>%s</dc:%s>\n' % (tag, html.escape(v), tag)) if v else ''

    meta_xml = ''
    for tag in ('creator', 'contributor', 'publisher', 'date', 'subject', 'rights'):
        meta_xml += dc(tag, book_meta.get(tag))
    meta_xml += dc('description', book_meta.get('description'))
    series = (book_meta.get('series') or '').strip()
    if series:
        meta_xml += ('  <meta name="calibre:series" content="%s"/>\n'
                     % html.escape(series, quote=True))
        idx = (book_meta.get('series_index') or '').strip()
        if idx:
            meta_xml += ('  <meta name="calibre:series_index" content="%s"/>\n'
                         % html.escape(idx, quote=True))

    # EPUB2 兼容的 guide（老阅读器靠它定位封面/目录/正文起点）
    guide = '<guide>\n  <reference type="cover" title="封面" href="cover.xhtml"/>\n'
    if toc_page:
        guide += '  <reference type="toc" title="目录" href="book-toc.xhtml"/>\n'
    if chap_files:
        guide += '  <reference type="text" title="正文" href="%s"/>\n' % chap_files[0][0]
    guide += '</guide>\n'

    modified = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(oebps, "content.opf"), 'w', encoding='utf-8') as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
            ' unique-identifier="bookid" xml:lang="zh-CN">\n'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            '  <dc:identifier id="bookid">%s</dc:identifier>\n'
            '  <dc:title>%s</dc:title>\n  <dc:language>zh-CN</dc:language>\n'
            '%s'
            '  <meta property="dcterms:modified">%s</meta>\n</metadata>\n'
            '<manifest>\n%s</manifest>\n<spine toc="ncx">\n%s</spine>\n%s</package>\n'
            % (uid, html.escape(title), cover_meta + meta_xml, modified,
               manifest, spine, guide)
        )

    # container.xml
    with open(os.path.join(meta_dir, "container.xml"), 'w', encoding='utf-8') as f:
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
        z.write(os.path.join(meta_dir, "container.xml"), 'META-INF/container.xml')
        for root, _d, files in os.walk(oebps):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                arc = 'OEBPS/' + os.path.relpath(full, oebps).replace('\\', '/')
                z.write(full, arc)
    L("完成：%s（%.2f MB，%d 章）" % (out_path, os.path.getsize(out_path) / 1048576, len(chapters)))
    return out_path


# ---------- 切割方式注册表（扩展口：新增方式 = 在此注册一个函数） ----------

PREFACE_TITLE = "前言"


def _with_preamble(lines, first_index, chapters, title=PREFACE_TITLE):
    """第一个分隔标记之前若有内容（内容简介、前言、作者的话），
    作为独立一章保留在开头，避免被静默丢弃。

    分隔符本身不算内容：像“------------ 第一章”这种以分隔符开头的文件，
    按标题正则切分时不应凭空多出一个“前言”章。"""
    if first_index <= 0:
        return chapters
    head = [ln for ln in lines[:first_index]
            if not re.fullmatch(r'-{12}', ln.strip())]
    if not any(ln.strip() for ln in head):
        return chapters
    return [(title, head)] + chapters


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
    return _with_preamble(lines, seps[0], chapters)


def split_by_title_regex(lines):
    """按“第X章/回/卷”标题行切分。"""
    pat = re.compile(r'^第[〇零一二三四五六七八九十百千万两0-9０-９]+[章回卷节]')
    idxs = [i for i, ln in enumerate(lines) if pat.match(ln.strip())]
    if not idxs:
        return None
    chapters = []
    for k, i in enumerate(idxs):
        end = idxs[k + 1] if k + 1 < len(idxs) else len(lines)
        chapters.append((lines[i].strip(), lines[i + 1:end]))
    return _with_preamble(lines, idxs[0], chapters)


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


VOLUME_RE = re.compile(r'^第[一二三四五六七八九十百千0-9０-９]+卷')
CHAPTER_NUM_RE = re.compile(r'^第[一二三四五六七八九十百千0-9０-９]+[章回]')


def split_by_volume_and_chapter(lines):
    """按“第X卷”分卷、“第X章/回”分章，返回 [(卷名, 标题, 正文行), ...]。

    卷名行为卷边界，本身不进入正文；卷之前的散章作为无卷章节保留。"""
    marks = []
    volume = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if VOLUME_RE.match(s):
            volume = s
            marks.append((i, volume, True))
        elif CHAPTER_NUM_RE.match(s):
            marks.append((i, volume, False))
    if not any(not is_vol for _i, _v, is_vol in marks):
        return None

    chapters = []

    def add_head(upto):
        head = [ln for ln in lines[:upto]
                if not re.fullmatch(r'-{12}', ln.strip())]
        if any(ln.strip() for ln in head):
            chapters.append((None, PREFACE_TITLE, head))

    if marks:
        add_head(marks[0][0])

    for k, (i, vol, is_vol) in enumerate(marks):
        if is_vol:
            continue
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        chapters.append((vol, lines[i].strip(), lines[i + 1:end]))
    return chapters or None


def split_by_llm(lines):
    """【预留】LLM 智能切割扩展口。
    实现建议：把全文按段落分批提交给 LLM，由模型返回每个章节的标题与起始行，
    据此切分。返回格式须与其它切割函数一致：[(标题, [正文行]), ...]。"""
    raise NotImplementedError('LLM 智能切割尚未接入，可在 split_by_llm 中实现')


# (显示名, 函数, 参数说明{param: 默认值})；函数为 None 表示预留未实现
SPLITTERS = [
    ("整行12个横线分隔（大明测字天师式）", split_by_dash, {}),
    ("章节标题正则（第X章）", split_by_title_regex, {}),
    ("卷/章两级（第X卷 + 第X章）", split_by_volume_and_chapter, {}),
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


_LIST_OPEN_RE = re.compile(r'<(ol|ul)\b', re.I)
_LIST_TOKEN_RE = re.compile(r'<(/?)(ol|ul)\b', re.I)


def insert_list_items(markup, items):
    """把 HTML 片段 items 插入到 markup 中最外层 <ol>/<ul> 的末尾。

    旧实现直接替换第一个 '</ol>'：当目录是多级嵌套列表时，
    新章节会被插进别人的子目录里。这里按标签深度配对，
    只在深度归零（最外层列表闭合）处插入。
    返回 (新文本, 是否成功)；未找到列表时原样返回。"""
    first = _LIST_OPEN_RE.search(markup)
    if not first:
        return markup, False
    tag = first.group(1).lower()
    depth = 0
    for m in _LIST_TOKEN_RE.finditer(markup, first.start()):
        if m.group(2).lower() != tag:
            continue
        depth += -1 if m.group(1) else 1
        if depth == 0:
            pos = m.start()
            return markup[:pos] + items + markup[pos:], True
    return markup, False


def append_chapters(epub, chapter_files, out_epub, use_filename_title=True, log=None,
                    cover_image=None):
    """把若干 txt（每个 = 一章）追加到 epub 末尾并重打包。
    use_filename_title: True 用文件名作标题；False 用文件内首个非空行。
    cover_image: 可选新封面图片路径；给出时在追加完成后一并替换封面。"""
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
        opf, opf_enc = read_text_with_encoding(opf_path)

        manifest_items = [(a.get('id'), a['href']) for _s, _e, a in iter_tags(opf, 'item')
                          if a.get('href')]
        hrefs = [h for _i, h in manifest_items]

        def is_chapter_href(h):
            base = os.path.basename(h).lower()
            return (h.lower().endswith(('.xhtml', '.html'))
                    and base not in _SKIP_PAGE_NAMES
                    and not _VOL_PAGE_RE.match(base))

        content_hrefs = [h for h in hrefs if is_chapter_href(h)]
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
        write_text_with_encoding(opf_path, opf, opf_enc)

        # nav
        nav_path = os.path.join(opf_dir, "nav.xhtml")
        if os.path.exists(nav_path):
            nav, nav_enc = read_text_with_encoding(nav_path)
            ins = "".join('    <li><a href="%s">%s</a></li>\n'
                          % (fn, html.escape(t, quote=True))
                          for fn, (t, _b) in zip(new_files, chapters))
            nav, ok = insert_list_items(nav, ins)
            if ok:
                write_text_with_encoding(nav_path, nav, nav_enc)
            else:
                L("警告：nav.xhtml 中未找到 <ol>/<ul> 目录列表，跳过 nav 更新")

        # ncx
        ncx_path = os.path.join(opf_dir, "toc.ncx")
        if os.path.exists(ncx_path):
            ncx, ncx_enc = read_text_with_encoding(ncx_path)
            plays = [int(m) for m in re.findall(r'playOrder="(\d+)"', ncx)]
            nxt = max(plays) + 1 if plays else 1
            ins = "".join(
                '    <navPoint id="np_%s" playOrder="%d"><navLabel><text>%s</text></navLabel>'
                '<content src="%s"/></navPoint>\n'
                % (re.sub(r'[^0-9A-Za-z]', '', fn), nxt + i, html.escape(t), fn)
                for i, (fn, (t, _b)) in enumerate(zip(new_files, chapters)))
            if '</navMap>' in ncx:
                ncx = ncx.replace('</navMap>', ins + '</navMap>', 1)
                write_text_with_encoding(ncx_path, ncx, ncx_enc)

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
        if cover_image:
            set_cover(out_epub, cover_image, out_epub, log)
        else:
            L("已保留原 epub 封面（如需更换封面，请使用“封面管理”页）")
        return out_epub
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 封面：提取 / 替换 ----------

MEDIA_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
               '.gif': 'image/gif', '.webp': 'image/webp', '.svg': 'image/svg+xml'}

# EPUB3 核心图片格式（WebP 不在其中）
EPUB_IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.svg')


def cover_payload(image_path):
    """读取封面图片，返回 (字节, 扩展名, media-type)。

    EPUB3 只认 jpg/png/gif/svg；WebP 之类能解码就转成 PNG 再嵌入，
    避免生成阅读器打不开的封面。"""
    ext = os.path.splitext(image_path)[1].lower()
    with open(image_path, 'rb') as f:
        data = f.read()
    if ext not in EPUB_IMAGE_EXT:
        png = _wic_thumbnail_png(data, 2400, 2400)
        if png:
            return png, '.png', 'image/png'
        return data, '.jpg', 'image/jpeg'
    return data, ext, MEDIA_TYPES.get(ext, 'image/jpeg')


_ATTR_RE = re.compile(r'([\w:.-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')


def parse_attrs(raw):
    """把标签内的属性串解析为 {小写名: 值}。"""
    attrs = {}
    for m in _ATTR_RE.finditer(raw):
        value = m.group(2) if m.group(2) is not None else m.group(3)
        attrs[m.group(1).lower()] = value
    return attrs


def iter_tags(text, tag):
    """迭代 <tag ...>，产出 (起始位置, 结束位置, 属性字典)。

    与正则直接抓属性不同，这里先取完整标签再解析属性，
    因此属性顺序、引号风格、自闭合写法都不影响识别。"""
    pattern = re.compile(r'<%s\b([^>]*?)(/?)>' % re.escape(tag), re.I | re.S)
    for m in pattern.finditer(text):
        yield m.start(), m.end(), parse_attrs(m.group(1))


def rewrite_tag(raw_tag, updates):
    """在原始标签文本上就地替换或追加属性，保留其余写法与顺序。"""
    pending = dict(updates)
    spans = []
    for m in _ATTR_RE.finditer(raw_tag):
        key = m.group(1).lower()
        if key in pending:
            group = 2 if m.group(2) is not None else 3
            spans.append((m.start(group), m.end(group), pending.pop(key)))
    out = raw_tag
    for start, end, value in reversed(spans):
        out = out[:start] + html.escape(str(value), quote=True) + out[end:]
    if pending:
        extra = ''.join(' %s="%s"' % (k, html.escape(str(v), quote=True))
                        for k, v in pending.items())
        m = re.match(r'^(.*?)(\s*/?>)$', out, re.S)
        out = (m.group(1) + extra + m.group(2)) if m else (out + extra)
    return out


def find_cover_ref(opf):
    """从 content.opf 文本中找封面引用，返回 (item_id, href) 或 None。
    识别顺序：<meta name="cover"> -> item properties="cover-image"。
    属性顺序不影响识别（旧版正则要求 href 在 properties 之前）。"""
    items = [(s, e, a) for s, e, a in iter_tags(opf, 'item') if a.get('href')]

    cover_id = None
    for _s, _e, attrs in iter_tags(opf, 'meta'):
        if attrs.get('name', '').lower() == 'cover' and attrs.get('content'):
            cover_id = attrs['content']
            break
    if cover_id:
        for _s, _e, attrs in items:
            if attrs.get('id') == cover_id:
                return attrs.get('id'), attrs['href']

    for _s, _e, attrs in items:
        if 'cover-image' in attrs.get('properties', '').split():
            return attrs.get('id'), attrs['href']
    return None


def read_cover(epub):
    """读取 epub 封面，返回 (图片字节, 扩展名) 或 None（未声明封面）。"""
    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        opf_name = next((n for n in names if n.endswith('content.opf')), None)
        if not opf_name:
            raise RuntimeError("epub 中未找到 content.opf")
        opf = decode_bytes(z.read(opf_name))[0]
        ref = find_cover_ref(opf)
        if not ref:
            return None
        _iid, href = ref
        full = _resolve_href(posixpath.dirname(opf_name), href)
        if full not in names:
            raise RuntimeError("封面文件不存在于包内: %s" % href)
        return z.read(full), (os.path.splitext(full)[1].lower() or '.jpg')


def extract_cover(epub, out_path, log=None):
    """提取 epub 封面图片到 out_path；无封面返回 None。"""
    def L(*a):
        if log:
            log(' '.join(str(x) for x in a))
    got = read_cover(epub)
    if not got:
        L("该 epub 未声明封面（无 meta cover / cover-image）")
        return None
    data, _ext = got
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
    src_ext = os.path.splitext(image_path)[1].lower()
    if src_ext not in MEDIA_TYPES:
        L("警告：未知图片格式 %s，按 image/jpeg 处理" % src_ext)

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
        opf, opf_enc = read_text_with_encoding(opf_path)

        # WebP 等非 EPUB 核心格式会先转成 PNG，避免生成读不出来的封面
        img_data, ext, mtype = cover_payload(image_path)
        if ext != src_ext:
            L("封面格式 %s 不受 EPUB 支持，已转换为 %s" % (src_ext, ext))
        images_dir = os.path.join(opf_dir, "images")
        os.makedirs(images_dir, exist_ok=True)
        new_name = "cover" + ext
        rel = "images/" + new_name
        with open(os.path.join(images_dir, new_name), 'wb') as f:
            f.write(img_data)

        ref = find_cover_ref(opf)
        if ref:
            old_id, old_href = ref
            # 更新 manifest 中该 item 的 href 与 media-type（保留 id 与其它属性）
            span = next(((s, e, a) for s, e, a in iter_tags(opf, 'item')
                         if a.get('id') == old_id), None)
            if span is None:
                raise RuntimeError("manifest 中找不到 item id=%s" % old_id)
            start, end, old_attrs = span
            props = [p for p in old_attrs.get('properties', '').split() if p]
            if 'cover-image' not in props:
                props.append('cover-image')
            new_tag = rewrite_tag(opf[start:end], {
                'href': rel, 'media-type': mtype, 'properties': ' '.join(props)})
            opf = opf[:start] + new_tag + opf[end:]
            # 删除旧封面文件（若与新文件不同）
            old_rel = posixpath.join(posixpath.dirname(
                os.path.relpath(opf_path, tmp).replace('\\', '/')), unquote(old_href))
            old_abs = os.path.normpath(os.path.join(tmp, old_rel.replace('/', os.sep)))
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
            cover, cover_enc = read_text_with_encoding(cover_path)
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
            write_text_with_encoding(cover_path, cover, cover_enc)

        # 更新 modified 时间并写回 opf
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        opf = re.sub(r'<meta property="dcterms:modified">[^<]*</meta>',
                     '<meta property="dcterms:modified">' + now + '</meta>', opf, count=1)
        write_text_with_encoding(opf_path, opf, opf_enc)

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


# ---------- EPUB -> TXT（拆解，功能2 的逆功能） ----------

class _ChapterTextParser(HTMLParser):
    """提取章节 XHTML 的标题与正文纯文本。

    规则：第一处 <h1> 作为章节标题；其余标题（h2-h6，以及第二个及以后的 h1）
    以 markdown 形式（## / ###）保留在正文里，重新打包时会还原为对应层级的标题。
    旧版把 h2/h3 也塞进标题并从正文删除，有分节的书导出后结构会错乱。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.body = []
        self.skip = 0
        self.heading_level = 0
        self.heading_buf = []
        self.title_done = False

    def _flush_heading(self, level):
        text = ''.join(self.heading_buf).strip()
        self.heading_buf = []
        self.heading_level = 0
        if not text:
            return
        if level == 1 and not self.title_done:
            self.title.append(text)
            self.title_done = True
        else:
            # 章节标题占 h1，正文里的标题整体下沉一级：h2 -> "# x"、h3 -> "## x"
            marker = '#' * max(1, min(level - 1, 6))
            self.body.append('\n' + marker + ' ' + text + '\n')

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'title'):
            self.skip += 1
        elif re.fullmatch(r'h[1-6]', tag):
            self.heading_level = int(tag[1])
            self.heading_buf = []
            self.body.append('\n')
        elif tag in ('p', 'div', 'li', 'blockquote', 'br'):
            self.body.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'title'):
            self.skip = max(0, self.skip - 1)
        elif re.fullmatch(r'h[1-6]', tag):
            self._flush_heading(int(tag[1]))
            self.body.append('\n')
        elif tag in ('p', 'div', 'li', 'blockquote'):
            self.body.append('\n')

    def handle_data(self, data):
        if self.skip:
            return
        if self.heading_level:
            self.heading_buf.append(data)
        else:
            self.body.append(data)

    def close(self):
        if self.heading_level:
            self._flush_heading(self.heading_level)
        super().close()


def _extract_chapter_text(xhtml):
    """从章节 XHTML 提取 (标题, [正文行])。"""
    p = _ChapterTextParser()
    p.feed(xhtml)
    p.close()
    title = ''.join(p.title).strip()
    if not title:
        m = re.search(r'<title[^>]*>(.*?)</title>', xhtml, re.S | re.I)
        if m:
            title = html.unescape(m.group(1)).strip()
    lines = [ln.strip() for ln in ''.join(p.body).splitlines()]
    lines = [ln for ln in lines if ln]
    return title, lines


# 目录/封面页不当作正文章节
_SKIP_PAGE_NAMES = ('cover.xhtml', 'cover.html', 'nav.xhtml', 'nav.html',
                    'toc.xhtml', 'toc.html', 'book-toc.xhtml', 'book-toc.html')
# 本工具生成的卷首页（vol_001.xhtml）也不当正文
_VOL_PAGE_RE = re.compile(r'^vol_\d+\.x?html?$')


def _resolve_href(opf_dir, href):
    """把 manifest/nav 里的 href 解析成包内全路径（去锚点、解码 %xx）。"""
    h = unquote((href or '').split('#', 1)[0].strip())
    if not h:
        return None
    full = posixpath.normpath(posixpath.join(opf_dir, h)).replace('\\', '/')
    if full == '..' or full.startswith('../'):
        return None
    return full.lstrip('/')


def _chapter_order_from_epub(z, opf_dir, names):
    """按阅读顺序返回 [(href, full), ...]。

    依次合并 nav -> ncx -> spine 的顺序，最后补上包内其余正文页。
    任何一份结构解析不全（只匹配到一部分条目、或 nav 写法不规范）都不会丢章；
    旧版一旦 nav 命中就完全不再回退，遇到带 class 的 <li> 会静默少章。"""
    name_set = set(names)
    ordered, seen = [], set()

    def add_full(full):
        if not full or full not in name_set or full in seen:
            return
        base = posixpath.basename(full).lower()
        if (base in _SKIP_PAGE_NAMES or _VOL_PAGE_RE.match(base)
                or not base.endswith(('.xhtml', '.html', '.htm'))):
            return
        seen.add(full)
        ordered.append(full)

    def add_href(href):
        add_full(_resolve_href(opf_dir, href))

    def read(name):
        return decode_bytes(z.read(name))[0] if name and name in name_set else ''

    opf_name = posixpath.join(opf_dir, 'content.opf') if opf_dir else 'content.opf'
    opf = read(opf_name)
    items = [a for _s, _e, a in iter_tags(opf, 'item')]
    manifest = {a['id']: a['href'] for a in items if a.get('id') and a.get('href')}
    spine = [a['idref'] for _s, _e, a in iter_tags(opf, 'itemref') if a.get('idref')]

    # 1) nav：取所有指向正文页的链接，写法不规范也不影响（不做整体成败判断）
    nav_href = next((a['href'] for a in items
                     if 'nav' in a.get('properties', '').split()), None)
    nav_name = _resolve_href(opf_dir, nav_href) if nav_href else \
        (posixpath.join(opf_dir, 'nav.xhtml') if opf_dir else 'nav.xhtml')
    for m in re.finditer(r'<a\b[^>]*?\bhref\s*=\s*(["\'])(.*?)\1', read(nav_name), re.I | re.S):
        add_href(m.group(2))

    # 2) ncx
    ncx_href = next((a['href'] for a in items
                     if a.get('media-type', '').lower().startswith('application/x-dtbncx')), None)
    ncx_name = _resolve_href(opf_dir, ncx_href) if ncx_href else \
        (posixpath.join(opf_dir, 'toc.ncx') if opf_dir else 'toc.ncx')
    for m in re.finditer(r'<content\b[^>]*?\bsrc\s*=\s*(["\'])(.*?)\1', read(ncx_name), re.I | re.S):
        add_href(m.group(2))

    # 3) spine
    for idref in spine:
        if idref in manifest:
            add_href(manifest[idref])

    # 4) 兜底：OPF 目录下未被覆盖的正文页，按文件名顺序补到末尾
    prefix = (opf_dir + '/') if opf_dir else ''
    for n in sorted(name_set):
        if prefix and not n.startswith(prefix):
            continue
        if n in (opf_name, nav_name, ncx_name):
            continue
        add_full(n)

    result = []
    for full in ordered:
        href = full[len(prefix):] if prefix and full.startswith(prefix) else full
        result.append((href, full))
    return result


def epub_to_txt(epub, out_txt, as_dir=None, log=None):
    """epub 拆解为可识别的 txt。
    - as_dir=None：输出单个 txt，章节间用整行 12 横线分隔（可被“单文件切割”的
      “横线分隔”方式原样还原，也符合项目统一的章节分隔约定）
    - as_dir=目录：每章一个 txt（首行为标题），可被“目录打包”或“追加章节”直接识别
    返回输出路径/目录。"""
    def L(*a):
        if log:
            log(' '.join(str(x) for x in a))
    with zipfile.ZipFile(epub) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError("epub 已损坏: " + bad)
        names = z.namelist()
        opf_name = next((n for n in names if n.endswith('content.opf')), None)
        if not opf_name:
            raise RuntimeError("epub 中未找到 content.opf")
        opf_dir = posixpath.dirname(opf_name)
        order = _chapter_order_from_epub(z, opf_dir, names)
        if not order:
            raise RuntimeError("未能从 epub 解析出任何章节")
        chapters = []
        for href, full in order:
            xhtml = decode_bytes(z.read(full))[0]
            title, lines = _extract_chapter_text(xhtml)
            if not title:
                title = posixpath.basename(href)
            chapters.append((title, lines))
    L("解析出 %d 章" % len(chapters))

    if as_dir:
        os.makedirs(as_dir, exist_ok=True)
        for i, (t, body) in enumerate(chapters, 1):
            safe = re.sub(r'[\\/:*?"<>|\r\n]', '_', t)[:80]
            fn = "%03d_%s.txt" % (i, safe if safe else "第%d章" % i)
            with open(os.path.join(as_dir, fn), 'w', encoding='utf-8') as f:
                f.write(t + '\n\n' + '\n'.join(body) + '\n')
        L("每章一个 txt 已导出 -> %s（%d 个文件）" % (as_dir, len(chapters)))
        return as_dir

    parts = []
    for t, body in chapters:
        parts.append('------------\n\n' + t + '\n\n' + '\n'.join(body))
    with open(out_txt, 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(parts) + '\n')
    L("单个 txt 已导出 -> %s（%d 章，横线分隔，可再次切割还原）" % (out_txt, len(chapters)))
    return out_txt


# ============================================================
# 二、自检模式（不启动 GUI，验证三个功能）
# ============================================================

def selftest():
    print("== EPUB 工具箱自检 ==")
    # 打包后 __file__ 指向 PyInstaller 的临时解包目录，产物会丢在那里；
    # 用 app_dir() 让源码运行和 exe 运行都把结果放在程序目录下。
    work = os.path.join(app_dir(), "selftest_output")
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

    # 功能2c：段落还原（行首全角空格分段 / 折行合并 / 首段缩进）
    para_txt = os.path.join(work, "段落.txt")
    with open(para_txt, 'w', encoding='utf-8') as f:
        f.write("------------\n第一章 段落测试\n"
                "\u3000\u3000第一段开头，这一句被硬折行拆成了\n"
                "\u3000\u3000两半，应当合成一段。\n"
                "\u3000\u3000“这是对话？\n"
                "\u3000\u3000不可能，不可能啊！”\n"
                "\u3000\u3000旁白另起一段。\n")
    e_para = os.path.join(work, "段落书.epub")
    build_epub(split_file(para_txt, "整行12个横线分隔（大明测字天师式）"), "段落书", e_para)
    with zipfile.ZipFile(e_para) as z:
        html_para = z.read("OEBPS/chap_0001.xhtml").decode('utf-8')
    ps = re.findall(r'<p[^>]*>(.*?)</p>', html_para, re.S)
    check("功能2c 段落还原为 3 段", len(ps) == 3)
    check("功能2c 首段带缩进", bool(ps) and ps[0].startswith('\u3000\u3000')
          and 'style="text-indent:0"' in html_para)
    check("功能2c 折行已合并", len(ps) > 1 and ps[0].endswith('应当合成一段。')
          and ps[1] == '“这是对话？不可能，不可能啊！”')
    check("功能2c 旁白独立成段", len(ps) > 2 and ps[2] == '旁白另起一段。')

    # 功能2d：章号含「零」也能识别
    zero_txt = os.path.join(work, "零章.txt")
    with open(zero_txt, 'w', encoding='utf-8') as f:
        f.write("第一百零一章 甲\n正文甲。\n第一百零二章 乙\n正文乙。\n")
    chs_zero = split_file(zero_txt, "章节标题正则（第X章）")
    check("功能2d 含「零」章号可切分", chs_zero is not None and len(chs_zero) == 2)

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

    # 功能7：EPUB -> TXT 拆解（单 txt，横线分隔可还原）
    e_txt = os.path.join(work, "目录书_拆解.txt")
    epub_to_txt(e1, e_txt)
    with open(e_txt, 'r', encoding='utf-8') as f:
        txt = f.read()
    check("功能7 拆解出单个 txt", os.path.isfile(e_txt))
    restored = split_by_dash(txt.splitlines())
    check("功能7 拆解 txt 可被横线切割还原（3 章）",
          restored is not None and len(restored) == 3
          and restored[0][0] == "第一章 开始" and restored[2][0] == "第三章 结局")

    # 功能8：EPUB -> TXT（每章一个 txt 到目录，可被目录打包识别）
    e_dir = os.path.join(work, "目录书_章节txt")
    epub_to_txt(e1, e_dir, as_dir=e_dir)
    files8 = sorted(f for f in os.listdir(e_dir) if f.lower().endswith('.txt'))
    check("功能8 每章一个 txt（3 个文件）", len(files8) == 3)
    with open(os.path.join(e_dir, files8[0]), 'r', encoding='utf-8') as f:
        first_line = f.readline().strip()
    check("功能8 首行即章节标题", first_line == "第一章 开始")

    # 功能9：漫画 -> CBZ（可选依赖，缺库时跳过而不是判失败）
    try:
        import comic_ops
        deps_ok, deps_why = comic_ops.deps_available()
        if not deps_ok:
            print("SKIP 功能9 漫画→CBZ（%s）" % deps_why.splitlines()[0])
        else:
            from PIL import Image as _Image
            comic_src = os.path.join(work, "comic_src")
            os.makedirs(comic_src, exist_ok=True)
            for idx in ("001_01", "001_02"):
                im = _Image.new('RGB', (120, 400), (30, 30, 30))
                for y in range(150, 250):
                    for x in range(120):
                        im.putpixel((x, y), (255, 255, 255))
                im.save(os.path.join(comic_src, idx + ".jpg"))
            comic_out = os.path.join(work, "comic_out")
            res = comic_ops.convert(comic_src, comic_out, "自检条漫", mode='images',
                                    img_kw={'split': True, 'pad': 0})
            check("功能9 生成 1 个 CBZ", len(res) == 1)
            cbz_path = res[0][0]
            with zipfile.ZipFile(cbz_path) as z:
                pages9 = [n for n in z.namelist() if n.endswith('.jpg')]
                info9 = z.read("ComicInfo.xml").decode('utf-8')
            # 每张图中间那条白带切成两页 => 2 源文件 x 2 页
            check("功能9 白带切分成 4 页", len(pages9) == 4)
            check("功能9 ComicInfo 为条漫(No)", "<Manga>No</Manga>" in info9)
    except Exception as e:
        check("功能9 漫画→CBZ（%s）" % e, False)

    print("== 自检%s ==" % ("通过" if ok else "失败"))
    print("输出目录:", work)
    sys.exit(0 if ok else 1)


# ============================================================
# 二、封面预览（界面内渲染，不调用系统看图器）
# ============================================================

def app_dir():
    """程序所在目录：源码运行时是 .py 所在目录，打包后是 exe 所在目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _temp_dir(prefix):
    """在系统临时目录下建唯一子目录（不用 mkdtemp：它的 0700 权限在部分环境不可写）。"""
    d = os.path.join(tempfile.gettempdir(), prefix + uuid.uuid4().hex[:8])
    os.makedirs(d, exist_ok=True)
    return d


# ---------- Windows 图像组件（WIC）后备解码：GDI+ 不支持的格式（如 WebP） ----------

class _GUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16),
                ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8)]


def _guid(d1, d2, d3, data4):
    return _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*data4))


def _com_method(ptr, index, restype, *argtypes):
    """取 COM 接口第 index 个虚表方法（0=QueryInterface 1=AddRef 2=Release）。"""
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtable[index])


def _com_release(ptr):
    if ptr:
        try:
            _com_method(ptr, 2, ctypes.c_ulong)(ptr)
        except Exception:
            pass


def _png_bytes(width, height, rgb):
    """RGB 像素字节 -> PNG 字节（只用 zlib）。"""
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)                       # 每行过滤器类型 0
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag, payload):
        body = tag + payload
        return (struct.pack('>I', len(payload)) + body
                + struct.pack('>I', zlib.crc32(body) & 0xffffffff))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(raw), 6))
            + chunk(b'IEND', b''))


def _wic_thumbnail_png(data, max_w, max_h):
    """用 Windows 图像组件（WIC）解码并转成 PNG 字节。

    GDI+ 只认 jpg/png/gif/bmp/tif，WebP 等格式要靠 WIC
    （系统装了对应解码器才能成功，Win11 一般自带）。失败返回 None。"""
    if os.name != 'nt':
        return None
    try:
        ole32 = ctypes.WinDLL('ole32')
        shlwapi = ctypes.WinDLL('shlwapi')
        ole32.CoInitializeEx(None, 2)        # 已初始化时返回 S_FALSE，忽略即可
    except Exception:
        return None

    factory = ctypes.c_void_p()
    stream = None
    decoder = ctypes.c_void_p()
    frame = ctypes.c_void_p()
    conv = ctypes.c_void_p()
    try:
        clsid = _guid(0xCACAF262, 0x9370, 0x4615,
                      (0xA1, 0x3B, 0x9F, 0x55, 0x39, 0xDA, 0x4C, 0x0A))
        iid = _guid(0xEC5EC8A9, 0xC395, 0x4314,
                    (0x9C, 0x77, 0x54, 0xD7, 0xA9, 0x35, 0xFF, 0x70))
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, 1,
                                  ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return None

        shlwapi.SHCreateMemStream.restype = ctypes.c_void_p
        shlwapi.SHCreateMemStream.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        stream = shlwapi.SHCreateMemStream(data, len(data))
        if not stream:
            return None

        # CreateDecoderFromStream(IStream*, GUID*, WICDecodeOptions, IWICBitmapDecoder**)
        if _com_method(factory, 4, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                factory, stream, None, 0, ctypes.byref(decoder)) != 0:
            return None

        # GetFrameCount / GetFrame 的虚表序号在不同 Windows 版本上差一位，
        # 用“取帧数”探测：两种可能的序号都只写一个出参，先用 8 字节缓冲接住，
        # 值落在合理范围内就说明拿对了。
        frame_index = None
        for count_index in (11, 12):
            probe = ctypes.c_uint64(0)
            hr = _com_method(decoder, count_index, ctypes.c_long,
                             ctypes.POINTER(ctypes.c_uint64))(decoder, ctypes.byref(probe))
            if hr == 0 and 0 < probe.value < 100000:
                frame_index = count_index + 1
                break
        if frame_index is None:
            return None

        if _com_method(decoder, frame_index, ctypes.c_long, ctypes.c_uint,
                       ctypes.POINTER(ctypes.c_void_p))(decoder, 0, ctypes.byref(frame)) != 0:
            return None

        w = ctypes.c_uint(0)
        h = ctypes.c_uint(0)
        if _com_method(frame, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_uint),
                       ctypes.POINTER(ctypes.c_uint))(frame, ctypes.byref(w),
                                                      ctypes.byref(h)) != 0:
            return None
        width, height = int(w.value), int(h.value)
        if width <= 0 or height <= 0:
            return None

        # 统一转成 32bpp BGRA
        if _com_method(factory, 10, ctypes.c_long,
                       ctypes.POINTER(ctypes.c_void_p))(factory, ctypes.byref(conv)) != 0:
            return None
        bgra = _guid(0x6FDDC324, 0x4E03, 0x4BFE,
                     (0xB1, 0x85, 0x3D, 0x77, 0x76, 0x8D, 0xC9, 0x0F))
        if _com_method(conv, 8, ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID),
                       ctypes.c_int, ctypes.c_void_p, ctypes.c_double, ctypes.c_int)(
                conv, frame, ctypes.byref(bgra), 0, None, 0.0, 0) != 0:
            return None

        stride = width * 4
        buf = ctypes.create_string_buffer(stride * height)
        if _com_method(conv, 7, ctypes.c_long, ctypes.c_void_p, ctypes.c_uint,
                       ctypes.c_uint, ctypes.c_void_p)(
                conv, None, stride, stride * height, buf) != 0:
            return None

        pixels = buf.raw
        factor = max(1, -(-width // max_w), -(-height // max_h))
        nw, nh = max(1, width // factor), max(1, height // factor)
        rgb = bytearray(nw * nh * 3)
        for y in range(nh):
            src_row = (y * factor) * stride
            dst = y * nw * 3
            for x in range(nw):
                p = src_row + (x * factor) * 4
                rgb[dst] = pixels[p + 2]
                rgb[dst + 1] = pixels[p + 1]
                rgb[dst + 2] = pixels[p]
                dst += 3
        return _png_bytes(nw, nh, rgb)
    except Exception:
        return None
    finally:
        for ptr in (conv, frame, decoder):
            _com_release(ptr)
        if stream:
            _com_release(stream)
        _com_release(factory)


def _gdiplus_thumbnail_png(data, max_w, max_h):
    """用 Windows 自带的 GDI+ 把图片（jpg/png/gif/bmp/tiff）缩成 PNG 字节。

    非 Windows、GDI+ 不可用或解码失败时返回 None。不依赖 Pillow。"""
    if os.name != 'nt':
        return None
    try:
        import ctypes

        class Guid(ctypes.Structure):
            _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16),
                        ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8)]

        class StartupInput(ctypes.Structure):
            _fields_ = [('GdiplusVersion', ctypes.c_uint32),
                        ('DebugEventCallback', ctypes.c_void_p),
                        ('SuppressBackgroundThread', ctypes.c_int),
                        ('SuppressExternalCodecs', ctypes.c_int)]

        gdiplus = ctypes.WinDLL('gdiplus')
    except Exception:
        return None

    # PNG 编码器 CLSID {557CF406-1A04-11D3-9A73-0000F81EF32E}
    png_clsid = Guid(0x557CF406, 0x1A04, 0x11D3,
                     (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))
    token = ctypes.c_void_p()
    startup = StartupInput(1, None, 0, 0)
    if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None) != 0:
        return None

    work = None
    image = ctypes.c_void_p()
    thumb = ctypes.c_void_p()
    try:
        work = _temp_dir('epubtool_prev_')
        src = os.path.join(work, 'src.img')
        dst = os.path.join(work, 'thumb.png')
        with open(src, 'wb') as f:
            f.write(data)
        if gdiplus.GdipCreateBitmapFromFile(ctypes.c_wchar_p(src), ctypes.byref(image)) != 0:
            return None
        if gdiplus.GdipGetImageThumbnail(image, int(max_w), int(max_h),
                                         ctypes.byref(thumb), None, None) != 0:
            return None
        if gdiplus.GdipSaveImageToFile(thumb, ctypes.c_wchar_p(dst),
                                       ctypes.byref(png_clsid), None) != 0:
            return None
        with open(dst, 'rb') as f:
            return f.read()
    except Exception:
        return None
    finally:
        try:
            if thumb:
                gdiplus.GdipDisposeImage(thumb)
            if image:
                gdiplus.GdipDisposeImage(image)
        except Exception:
            pass
        try:
            gdiplus.GdiplusShutdown(token)
        except Exception:
            pass
        if work:
            shutil.rmtree(work, ignore_errors=True)


def image_preview_png(data, max_w, max_h):
    """把封面字节转成能交给 Tk 显示的 PNG 字节；不支持时返回 None。

    依次尝试：GDI+（最快，jpg/png/gif/bmp/tif）→ WIC（WebP 等系统解码器）
    → Tk 原生（png/gif）。"""
    png = _gdiplus_thumbnail_png(data, max_w, max_h)
    if png:
        return png
    png = _wic_thumbnail_png(data, max_w, max_h)
    if png:
        return png
    if data[:8].startswith(b'\x89PNG\r\n\x1a\n') or data[:3] == b'GIF':
        return data          # Tk 8.6 原生支持，由调用方再按整数倍缩小
    return None


def load_preview_image(data, max_w, max_h):
    """图片字节 -> 已缩放到不超过 max_w×max_h 的 tk.PhotoImage；失败返回 None。"""
    png = image_preview_png(data, max_w, max_h)
    if not png:
        return None
    try:
        photo = tk.PhotoImage(data=base64.b64encode(png).decode('ascii'))
    except tk.TclError:
        return None
    factor = max(1, -(-photo.width() // max_w), -(-photo.height() // max_h))
    if factor > 1:
        photo = photo.subsample(factor, factor)
    return photo


def image_size(data):
    """尽量读出图片宽高（用于状态提示）；读不出返回 None。"""
    if data[:8].startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        return (int.from_bytes(data[16:20], 'big'),
                int.from_bytes(data[20:24], 'big'))
    if data[:6] in (b'GIF87a', b'GIF89a') and len(data) >= 10:
        return (int.from_bytes(data[6:8], 'little'),
                int.from_bytes(data[8:10], 'little'))
    if data[:2] == b'\xff\xd8':          # JPEG：扫描 SOF 段
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return (int.from_bytes(data[i + 7:i + 9], 'big'),
                        int.from_bytes(data[i + 5:i + 7], 'big'))
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + int.from_bytes(data[i + 2:i + 4], 'big')
    return None


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


class CoverPanel(ttk.Frame):
    """封面预览 + 选择面板。

    预览完全在界面内渲染（GDI+ 缩略图 -> Tk 显示），不调用系统看图器。
    两种展示来源：
      * 用户选择的图片文件（image_path 非空，打包/替换时使用）
      * 已有 epub 的当前封面（只读预览，image_path 仍为 None）
    """

    def __init__(self, master, on_change=None, size=(112, 150), extra_buttons=None):
        super().__init__(master)
        self.on_change = on_change
        self.image_path = None
        self._photo = None
        self._preview_w, self._preview_h = size

        self.box = ttk.Frame(self, style='CoverBox.TFrame',
                             width=self._preview_w, height=self._preview_h)
        self.box.pack(side='left')
        self.box.pack_propagate(False)
        self.preview = ttk.Label(self.box, text='无封面', anchor='center',
                                 style='CoverHint.TLabel')
        self.preview.pack(fill='both', expand=True)

        right = ttk.Frame(self)
        right.pack(side='left', fill='both', expand=True, padx=(14, 0))
        self.status = ttk.Label(right, text='未选择封面（将生成文字封面页）',
                                style='Hint.TLabel', wraplength=520, justify='left')
        self.status.pack(anchor='w')
        btns = ttk.Frame(right)
        btns.pack(anchor='w', pady=(10, 0))
        self.btn_pick = ttk.Button(btns, text='选择封面图片...', command=self.pick)
        self.btn_pick.pack(side='left')
        self.btn_clear = ttk.Button(btns, text='清除', command=self.clear, state='disabled')
        self.btn_clear.pack(side='left', padx=8)
        for text, command in (extra_buttons or []):
            ttk.Button(btns, text=text, command=command).pack(side='left', padx=4)

    # ---------- 对外 ----------
    def get_path(self):
        """返回用户选择的封面文件；没有选择时返回 None。"""
        return self.image_path

    def pick(self):
        path = filedialog.askopenfilename(
            title='选择封面图片',
            filetypes=[('图片文件', '*.jpg *.jpeg *.png *.gif *.bmp *.tif *.tiff'), ('所有文件', '*.*')])
        if path:
            self.set_file(path)

    def set_file(self, path):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError as e:
            messagebox.showerror(APP_NAME, '读取图片失败：%s' % e)
            return False
        if not self._show(data):
            messagebox.showerror(APP_NAME, '无法预览该图片（格式不支持或文件已损坏）：\n%s' % path)
            return False
        self.image_path = path
        size = image_size(data)
        dim = '，%d×%d' % size if size else ''
        self.status.configure(text='已选择：%s%s\n打包时以整页图片作为封面' %
                                   (os.path.basename(path), dim))
        self.btn_clear.configure(state='normal')
        self._notify()
        return True

    def show_bytes(self, data, note, notify=True):
        """只预览、不改变封面选择（用于展示已有 epub 的当前封面）。"""
        ok = self._show(data)
        if not ok:
            note = note + "\n（该图片格式无法在界面内预览，可用“用系统看图器打开”查看）"
        self.status.configure(text=note)
        self.btn_clear.configure(state='disabled')
        if notify:
            self._notify()
        return ok

    def clear(self, notify=True):
        self._photo = None
        self.image_path = None
        self.preview.configure(image='', text='无封面')
        self.status.configure(text='未选择封面（将生成文字封面页）')
        self.btn_clear.configure(state='disabled')
        if notify:
            self._notify()

    # ---------- 内部 ----------
    def _show(self, data):
        photo = load_preview_image(data, self._preview_w, self._preview_h)
        if photo is None:
            self._photo = None
            self.preview.configure(image='', text='无法预览')
            return False
        self._photo = photo          # 必须保持引用，否则会被回收
        self.preview.configure(image=photo, text='')
        return True

    def _notify(self):
        if self.on_change:
            self.on_change()


class MetadataDialog(tk.Toplevel):
    """书籍元数据编辑对话框（作者 / 系列 / 日期 / 出版社 / 版权 / 简介）。"""

    FIELDS = [
        ('creator', '作者'),
        ('series', '系列'),
        ('series_index', '系列序号'),
        ('date', '出版日期'),
        ('publisher', '出版社'),
        ('rights', '版权信息'),
    ]

    def __init__(self, master, meta=None):
        super().__init__(master)
        self.title('书籍元数据')
        self.transient(master)
        self.resizable(False, False)
        self.result = None
        meta = meta or {}
        self._vars = {}

        body = ttk.Frame(self, padding=16)
        body.pack(fill='both', expand=True)
        row = 0
        for key, label in self.FIELDS:
            ttk.Label(body, text=label + ':').grid(row=row, column=0, sticky='e',
                                                   padx=(0, 10), pady=6)
            var = tk.StringVar(value=meta.get(key, ''))
            ttk.Entry(body, textvariable=var, width=46).grid(row=row, column=1, sticky='ew', pady=6)
            self._vars[key] = var
            row += 1

        ttk.Label(body, text='简介:').grid(row=row, column=0, sticky='ne', padx=(0, 10), pady=6)
        self.desc = tk.Text(body, width=46, height=6, wrap='word')
        self.desc.insert('1.0', meta.get('description', ''))
        self.desc.grid(row=row, column=1, sticky='ew', pady=6)
        row += 1

        btns = ttk.Frame(body)
        btns.grid(row=row, column=0, columnspan=2, sticky='e', pady=(14, 0))
        ttk.Button(btns, text='取消', command=self._cancel).pack(side='right')
        ttk.Button(btns, text='确定', command=self._ok).pack(side='right', padx=8)

        self.bind('<Escape>', lambda _e: self._cancel())
        self.grab_set()

    def _ok(self):
        data = {k: v.get().strip() for k, v in self._vars.items()}
        data['description'] = self.desc.get('1.0', 'end').strip()
        self.result = {k: v for k, v in data.items() if v}
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class EpubToolApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("%s v%s" % (APP_NAME, VERSION))
        self._init_style()
        # 书籍元数据（tab1/tab2 共用）与“书内目录页”开关
        self.meta = {}
        self._meta_var = tk.StringVar()
        self._toc_page_var = tk.BooleanVar(value=True)
        self._refresh_meta_label()
        # 按屏幕自适应，避免 1920x1080 这种写死尺寸在小屏上被裁掉
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1560, sw - 60), min(920, sh - 110)
        self.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 2)))
        self.minsize(1020, 680)
        # 窗口映射后再最大化：在 Windows 上过早调用 state('zoomed') 会被忽略
        self.after(60, self._maximize)
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=8, pady=8)
        self.tab1 = self._build_tab1(nb)
        self.tab2 = self._build_tab2(nb)
        self.tab3 = self._build_tab3(nb)
        self.tab4 = self._build_tab4(nb)
        self.tab5 = self._build_tab5(nb)
        self.tab6 = self._build_tab6(nb)

    # ---- 元数据 ----
    def _meta_summary(self):
        m = self.meta
        parts = []
        if m.get('creator'):
            parts.append('作者：' + m['creator'])
        if m.get('series'):
            s = '系列：' + m['series']
            if m.get('series_index'):
                s += ' #' + m['series_index']
            parts.append(s)
        if m.get('date'):
            parts.append(m['date'])
        return ' · '.join(parts) if parts else '未设置（作者 / 系列 / 简介…）'

    def _refresh_meta_label(self):
        self._meta_var.set(self._meta_summary())

    def _open_meta(self):
        dlg = MetadataDialog(self, self.meta)
        self.wait_window(dlg)
        if dlg.result is not None:
            self.meta = dlg.result
            self._refresh_meta_label()

    def _options_row(self, parent, row, extra=None):
        """元数据按钮 + 摘要 + 书内目录页开关（tab1/tab2 共用）。"""
        f = ttk.Frame(parent)
        f.grid(row=row, column=0, columnspan=3, sticky='ew', padx=18, pady=(8, 0))
        ttk.Button(f, text='元数据...', command=self._open_meta).pack(side='left')
        ttk.Label(f, textvariable=self._meta_var, style='Hint.TLabel').pack(
            side='left', padx=(12, 0))
        ttk.Checkbutton(f, text='生成书内目录页', variable=self._toc_page_var).pack(side='right')
        if extra:
            extra(f)
        return f

    def _maximize(self):
        try:
            self.state('zoomed')
        except tk.TclError:
            pass

    def _init_style(self):
        """统一主题与配色；选中的标签页用高对比蓝底白字，一眼能确认当前位置。"""
        style = ttk.Style(self)
        try:
            style.theme_use('clam')      # vista 主题不允许自定义标签页颜色
        except tk.TclError:
            pass
        ui = ('Microsoft YaHei UI', 10)
        style.configure('.', font=ui)
        style.configure('TFrame', background='#f7f9fc')
        style.configure('TLabel', background='#f7f9fc')
        style.configure('TButton', padding=(16, 7))
        style.configure('TEntry', padding=5)
        style.configure('TCombobox', padding=4)
        style.configure('TCheckbutton', background='#f7f9fc')
        style.configure('TRadiobutton', background='#f7f9fc')
        style.configure('TNotebook', background='#f7f9fc', borderwidth=0, tabmargins=(8, 6, 8, 0))
        style.configure('TNotebook.Tab', padding=(22, 9),
                        font=('Microsoft YaHei UI', 10, 'bold'),
                        background='#dde3ea', foreground='#4a5462', borderwidth=0)
        style.map('TNotebook.Tab',
                  background=[('selected', '#2563eb'), ('active', '#c7d2fe')],
                  foreground=[('selected', '#ffffff'), ('active', '#1e3a8a')],
                  expand=[('selected', (1, 1, 1, 0))])
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 10, 'bold'),
                        foreground='#334155', background='#f7f9fc')
        style.configure('Hint.TLabel', foreground='#64748b', background='#f7f9fc')
        style.configure('CoverBox.TFrame', background='#ffffff',
                        relief='solid', borderwidth=1)
        style.configure('CoverHint.TLabel', foreground='#94a3b8', background='#ffffff')

    # ---- 通用小部件 ----
    def _path_row(self, parent, row, label, browse_cb, var, btn_text="浏览..."):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=10, pady=10)
        e = ttk.Entry(parent, textvariable=var, font=('', 11))
        e.grid(row=row, column=1, sticky='ew', padx=10, pady=10)
        ttk.Button(parent, text=btn_text, command=browse_cb).grid(row=row, column=2, padx=10, pady=10)
        return e

    def _log_area(self, parent, row):
        txt = scrolledtext.ScrolledText(parent, height=18, state='normal', wrap='word',
                                        font=('Consolas', 11))
        txt.grid(row=row, column=0, columnspan=3, sticky='nsew', padx=10, pady=10)
        parent.grid_rowconfigure(row, weight=1)
        parent.grid_columnconfigure(1, weight=3)
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

        ttk.Label(tab, text="目录内每个 txt/md 文件作为一章；勾选“子目录作为卷”时，每个子文件夹是一卷。",
                  font=('', 12)).grid(row=0, column=0, columnspan=3, sticky='w', padx=12, pady=(14, 6))
        self._path_row(tab, 1, "源目录:", browse, var_dir)
        # 书名与输出并排两列，充分利用宽度
        f2 = ttk.Frame(tab)
        f2.grid(row=2, column=0, columnspan=3, sticky='ew', padx=6)
        f2.grid_columnconfigure(1, weight=1)
        f2.grid_columnconfigure(3, weight=1)
        ttk.Label(f2, text="书名:").grid(row=0, column=0, sticky='w', padx=10, pady=10)
        ttk.Entry(f2, textvariable=var_title, font=('', 11)).grid(row=0, column=1, sticky='ew', padx=10, pady=10)
        ttk.Label(f2, text="输出:").grid(row=0, column=2, sticky='w', padx=10, pady=10)
        ttk.Entry(f2, textvariable=var_out, font=('', 11)).grid(row=0, column=3, sticky='ew', padx=10, pady=10)
        ttk.Button(f2, text="浏览...", command=self._browse_out(var_out)).grid(row=0, column=4, padx=10, pady=10)

        var_use_vol = tk.BooleanVar(value=True)

        def volume_check(parent):
            ttk.Checkbutton(parent, text='子目录作为卷',
                            variable=var_use_vol).pack(side='right', padx=(0, 16))

        self._options_row(tab, 3, extra=volume_check)

        cover_row = ttk.Frame(tab)
        cover_row.grid(row=4, column=0, columnspan=3, sticky='ew', padx=18, pady=(8, 2))
        ttk.Label(cover_row, text="封面:", style='Title.TLabel').pack(side='left', anchor='n', pady=4)
        self.cover1 = CoverPanel(cover_row)
        self.cover1.pack(side='left', fill='x', expand=True, padx=(12, 0))

        log = self._log_area(tab, 5)

        def collect_entries(base):
            """返回 [(卷名或 None, 文件路径)]：子目录 = 卷，根目录散章排在最前。"""
            entries = []
            for fn in sorted(f for f in os.listdir(base)
                             if f.lower().endswith(('.txt', '.md'))
                             and os.path.isfile(os.path.join(base, f))):
                entries.append((None, os.path.join(base, fn)))
            if var_use_vol.get():
                for sd in sorted(x for x in os.listdir(base)
                                 if os.path.isdir(os.path.join(base, x))):
                    sd_path = os.path.join(base, sd)
                    for fn in sorted(os.listdir(sd_path)):
                        full = os.path.join(sd_path, fn)
                        if fn.lower().endswith(('.txt', '.md')) and os.path.isfile(full):
                            entries.append((sd, full))
            return entries

        def run():
            d = var_dir.get().strip()
            if not d or not os.path.isdir(d):
                log.insert('end', "请先选择有效的源目录\n")
                return
            entries = collect_entries(d)
            if not entries:
                log.insert('end', "目录内没有 txt/md 文件\n")
                return
            title = var_title.get().strip() or os.path.basename(d)
            out = var_out.get().strip() or os.path.join(d, title + ".epub")
            var_out.set(out)
            bus = LogBus()

            def job(lg):
                vols = sorted({v for v, _p in entries if v})
                if vols:
                    lg("按子目录分 %d 卷：%s" % (len(vols), "、".join(vols)))
                lg("共 %d 个章节文件" % len(entries))
                chapters = []
                for vol, path in entries:
                    lines = read_text(path).splitlines()
                    t, rest = first_nonempty(lines)
                    name = t or os.path.basename(path)
                    chapters.append((vol, name, rest) if vol else (name, rest))
                build_epub(chapters, title, out, lg,
                           cover_image=self.cover1.get_path(),
                           meta=self.meta, toc_page=self._toc_page_var.get())

            run_job(run_btn, bus.emit, job)
            self._poll(bus, log, run_btn)

        run_btn = ttk.Button(tab, text="开始打包", command=run)
        run_btn.grid(row=6, column=0, columnspan=3, pady=12)
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

        ttk.Label(tab, text="选择单个 txt/md 文件（或仅含一个文本文件的目录），按切割方式分章后打包。",
                  font=('', 12)).grid(row=0, column=0, columnspan=3, sticky='w', padx=12, pady=(14, 6))
        ttk.Label(tab, text="源文件:").grid(row=1, column=0, sticky='w', padx=10, pady=10)
        ttk.Entry(tab, textvariable=var_file, font=('', 11)).grid(row=1, column=1, sticky='ew', padx=10, pady=10)
        f1 = ttk.Frame(tab)
        f1.grid(row=1, column=2, padx=10)
        ttk.Button(f1, text="选择文件", command=browse_file).pack(side='left', padx=4)
        ttk.Button(f1, text="选目录(唯一文件)", command=browse_dir).pack(side='left', padx=4)

        # 切割方式与每章行数并排
        f2 = ttk.Frame(tab)
        f2.grid(row=2, column=0, columnspan=3, sticky='ew', padx=6)
        f2.grid_columnconfigure(1, weight=1)
        ttk.Label(f2, text="切割方式:").grid(row=0, column=0, sticky='w', padx=10, pady=10)
        cb = ttk.Combobox(f2, textvariable=var_split, state='readonly',
                          values=[s[0] for s in SPLITTERS], font=('', 11), width=42)
        cb.grid(row=0, column=1, sticky='ew', padx=10, pady=10)
        ttk.Label(f2, text="每章行数(固定行数切分时生效):").grid(row=0, column=2, sticky='w', padx=10, pady=10)
        ttk.Entry(f2, textvariable=var_lines, width=12, font=('', 11)).grid(row=0, column=3, sticky='w', padx=10, pady=10)

        self._path_row(tab, 3, "输出:", self._browse_out(var_out), var_out)

        self._options_row(tab, 4)

        cover_row = ttk.Frame(tab)
        cover_row.grid(row=5, column=0, columnspan=3, sticky='ew', padx=18, pady=(8, 2))
        ttk.Label(cover_row, text="封面:", style='Title.TLabel').pack(side='left', anchor='n', pady=4)
        self.cover2 = CoverPanel(cover_row)
        self.cover2.pack(side='left', fill='x', expand=True, padx=(12, 0))

        log = self._log_area(tab, 6)

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
                vols = {ch[0] for ch in chapters if len(ch) == 3 and ch[0]}
                lg("切割成功：%d 章%s" % (len(chapters),
                                        "，%d 卷" % len(vols) if vols else ""))
                for ch in chapters[:10]:
                    lg("  - " + ((ch[-2] if len(ch) >= 2 else '') or "(无标题)"))
                if len(chapters) > 10:
                    lg("  ... 共 %d 章" % len(chapters))
                build_epub(chapters, os.path.splitext(os.path.basename(f))[0], out, lg,
                           cover_image=self.cover2.get_path(),
                           meta=self.meta, toc_page=self._toc_page_var.get())

            run_job(run_btn, bus.emit, job)
            self._poll(bus, log, run_btn)

        run_btn = ttk.Button(tab, text="切割并打包", command=run)
        run_btn.grid(row=7, column=0, columnspan=3, pady=12)
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
                detect_cover()

        ttk.Label(tab, text="指定 epub，把若干 txt（每个文件一章）追加到末尾：解包 → 追加 → 重打包，原封面自动保留。",
                  font=('', 12)).grid(row=0, column=0, columnspan=3, sticky='w', padx=12, pady=(14, 6))
        self._path_row(tab, 1, "EPUB:", browse_epub, var_epub)

        # 封面：自动检测该 epub 是否已有封面并就地预览，可直接换图
        self._cover3_state = tk.StringVar(value="当前 epub：\n未选择文件")
        cover_row = ttk.Frame(tab)
        cover_row.grid(row=2, column=0, columnspan=3, sticky='ew', padx=18, pady=(8, 2))
        left = ttk.Frame(cover_row)
        left.pack(side='left', anchor='n')
        ttk.Label(left, text="封面:", style='Title.TLabel').pack(anchor='w', pady=(4, 6))
        ttk.Label(left, textvariable=self._cover3_state, style='Hint.TLabel',
                  wraplength=140, justify='left').pack(anchor='w')
        self.cover3 = CoverPanel(cover_row,
                                 extra_buttons=[("重新检测", lambda: detect_cover())])
        self.cover3.pack(side='left', fill='x', expand=True, padx=(12, 0))

        def detect_cover():
            e = var_epub.get().strip()
            if not e or not os.path.isfile(e):
                self._cover3_state.set("当前 epub：\n未选择文件")
                self.cover3.clear(notify=False)
                return
            try:
                got = read_cover(e)
            except Exception as ex:
                self._cover3_state.set("当前 epub：\n读取失败")
                self.cover3.clear(notify=False)
                self.cover3.status.configure(text="读取封面失败：%s" % ex)
                return
            if not got:
                self._cover3_state.set("当前 epub：\n无封面")
                self.cover3.clear(notify=False)
                self.cover3.status.configure(
                    text="该 epub 没有声明封面。可点“选择封面图片...”为它添加封面。")
                return
            data, _ext = got
            size = image_size(data)
            dim = "，%d×%d" % size if size else ""
            self._cover3_state.set("当前 epub：\n有封面 %.0f KB" % (len(data) / 1024.0))
            self.cover3.clear(notify=False)
            self.cover3.show_bytes(data, "当前 epub 的封面（仅预览%s；选择新图片即可替换）" % dim,
                                   notify=False)

        def on_cover_change():
            if self.cover3.get_path():
                self._cover3_state.set("追加后将\n替换封面")
            else:
                detect_cover()

        self.cover3.on_change = on_cover_change

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
        ttk.Label(tab, text="追加章节:").grid(row=3, column=0, sticky='w', padx=10, pady=10)
        ttk.Label(tab, textvariable=self._lbl_count,
                  foreground="#666").grid(row=3, column=1, sticky='w', padx=10, pady=10)
        f2 = ttk.Frame(tab)
        f2.grid(row=3, column=2, padx=10)
        ttk.Button(f2, text="添加文件", command=add_files).pack(side='left', padx=4)
        ttk.Button(f2, text="添加目录", command=add_dir).pack(side='left', padx=4)
        ttk.Checkbutton(tab, text="用文件名作为章节标题（不勾选则用文件内首个非空行）",
                        variable=var_title_mode).grid(row=4, column=0, columnspan=3, sticky='w', padx=12, pady=10)
        self._path_row(tab, 5, "输出:", self._browse_out(var_out), var_out)
        log = self._log_area(tab, 6)

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
                                use_filename_title=var_title_mode.get(), log=lg,
                                cover_image=self.cover3.get_path())

            run_job(run_btn, bus.emit, job)
            self._poll(bus, log, run_btn)

        run_btn = ttk.Button(tab, text="解包并追加", command=run)
        run_btn.grid(row=7, column=0, columnspan=3, pady=12)
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

        ttk.Label(tab, text="查看 / 替换 epub 封面：预览直接显示在窗口内，可另存为图片文件。",
                  font=('', 12)).grid(row=0, column=0, columnspan=3, sticky='w', padx=12, pady=(14, 6))
        self._path_row(tab, 1, "EPUB:", lambda: browse_epub_placeholder(), var_epub)

        cover_row = ttk.Frame(tab)
        cover_row.grid(row=2, column=0, columnspan=3, sticky='ew', padx=18, pady=(10, 2))
        ttk.Label(cover_row, text="封面:", style='Title.TLabel').pack(side='left', anchor='n', pady=4)
        self.cover4 = CoverPanel(cover_row, size=(150, 200))
        self.cover4.pack(side='left', fill='x', expand=True, padx=(12, 0))

        btns = ttk.Frame(tab)
        btns.grid(row=3, column=0, columnspan=3, sticky='w', padx=18, pady=10)
        log = self._log_area(tab, 4)

        def current_cover(log_error=True):
            e = var_epub.get().strip()
            if not e or not os.path.isfile(e):
                log.insert('end', "请先选择 epub 文件\n")
                return None
            try:
                return read_cover(e)
            except Exception as ex:
                if log_error:
                    log.insert('end', "读取封面失败：%s\n" % ex)
                return None

        def show_cover():
            got = current_cover()
            if got is None:
                return
            e = var_epub.get().strip()
            if not got:
                self.cover4.clear(notify=False)
                self.cover4.status.configure(text="该 epub 没有声明封面")
                log.insert('end', "该 epub 没有封面（可用“替换/设置封面”添加）\n")
                return
            data, ext = got
            size = image_size(data)
            note = "当前封面：%s，%.0f KB%s" % (ext, len(data) / 1024.0,
                                            "，%d×%d" % size if size else "")
            self.cover4.show_bytes(data, note, notify=False)
            log.insert('end', "已读取封面：%s\n" % note)

        def browse_epub_placeholder():
            f = filedialog.askopenfilename(title="选择 epub", filetypes=[("EPUB 文件", "*.epub")])
            if f:
                var_epub.set(f)
                show_cover()

        def save_cover():
            got = current_cover()
            if not got:
                return
            data, ext = got
            if ext not in MEDIA_TYPES:
                ext = '.jpg'
            out_dir = os.path.join(app_dir(), '封面导出')     # 默认存到程序目录下
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError:
                out_dir = os.path.dirname(var_epub.get())
            name = os.path.splitext(os.path.basename(var_epub.get()))[0] + '_封面' + ext
            path = filedialog.asksaveasfilename(
                title='保存封面到...', initialdir=out_dir, initialfile=name,
                defaultextension=ext,
                filetypes=[('图片文件', '*' + ext), ('所有文件', '*.*')])
            if not path:
                return
            try:
                with open(path, 'wb') as f:
                    f.write(data)
            except OSError as ex:
                messagebox.showerror(APP_NAME, '保存失败：%s' % ex)
                return
            log.insert('end', "封面已保存：%s\n" % path)

        def open_external():
            got = current_cover()
            if not got:
                return
            data, ext = got
            e = var_epub.get().strip()
            tmp = os.path.join(os.path.dirname(os.path.abspath(e)),
                               os.path.splitext(os.path.basename(e))[0] + '_封面预览' + ext)
            with open(tmp, 'wb') as f:
                f.write(data)
            log.insert('end', "用系统看图器打开：%s\n" % tmp)
            os.startfile(tmp)

        def replace_cover():
            e = var_epub.get().strip()
            if not e or not os.path.isfile(e):
                log.insert('end', "请先选择 epub 文件\n")
                return
            img = filedialog.askopenfilename(
                title="选择封面图片",
                filetypes=[("图片文件", "*.jpg *.jpeg *.png *.gif *.bmp *.tif *.tiff"),
                           ("所有文件", "*.*")])
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

        ttk.Button(btns, text="读取封面并预览", command=show_cover).pack(side='left', padx=4)
        ttk.Button(btns, text="保存封面...", command=save_cover).pack(side='left', padx=4)
        ttk.Button(btns, text="用系统看图器打开", command=open_external).pack(side='left', padx=4)
        replace_btn = ttk.Button(btns, text="替换/设置封面（输出新文件）", command=replace_cover)
        replace_btn.pack(side='left', padx=4)
        return tab

    # ---- Tab5 EPUB -> TXT 拆解 ----
    def _build_tab5(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  5 EPUB→TXT拆解  ")
        var_epub = tk.StringVar()
        var_mode = tk.StringVar(value="single")
        var_out = tk.StringVar()

        def browse_epub():
            f = filedialog.askopenfilename(title="选择 epub", filetypes=[("EPUB 文件", "*.epub")])
            if f:
                var_epub.set(f)
                var_out.set(os.path.splitext(f)[0] + ".txt")

        def on_mode():
            e = var_epub.get()
            if var_mode.get() == "single":
                var_out.set(os.path.splitext(e)[0] + ".txt" if e else "")
            else:
                var_out.set(os.path.splitext(e)[0] + "_章节txt" if e else "")

        ttk.Label(tab, text="把 epub 拆解为可再次合包的 txt（“单文件切割→EPUB”的逆功能）。",
                  font=('', 12)).grid(row=0, column=0, columnspan=3, sticky='w', padx=12, pady=(14, 6))
        self._path_row(tab, 1, "EPUB:", browse_epub, var_epub)
        ttk.Label(tab, text="导出方式:").grid(row=2, column=0, sticky='w', padx=10, pady=10)
        mf = ttk.Frame(tab)
        mf.grid(row=2, column=1, sticky='w', padx=10, pady=10)
        ttk.Radiobutton(mf, text="单个 txt（整行12横线分隔，可被“单文件切割”原样还原）",
                        variable=var_mode, value="single", command=on_mode).pack(anchor='w', pady=2)
        ttk.Radiobutton(mf, text="每章一个 txt 到目录（可被“目录打包 / 追加章节”直接识别）",
                        variable=var_mode, value="dir", command=on_mode).pack(anchor='w', pady=2)
        self._path_row(tab, 3, "输出:", self._browse_out(var_out), var_out)
        log = self._log_area(tab, 4)

        def run():
            e = var_epub.get().strip()
            if not e or not os.path.isfile(e):
                log.insert('end', "请先选择 epub 文件\n")
                return
            out = var_out.get().strip()
            if not out:
                out = os.path.splitext(e)[0] + (".txt" if var_mode.get() == "single" else "_章节txt")
                var_out.set(out)
            bus = LogBus()

            def job(lg):
                if var_mode.get() == "single":
                    epub_to_txt(e, out, as_dir=None, log=lg)
                else:
                    epub_to_txt(e, out, as_dir=out, log=lg)

            run_job(run_btn, bus.emit, job)
            self._poll(bus, log, run_btn)

        run_btn = ttk.Button(tab, text="拆解导出", command=run)
        run_btn.grid(row=5, column=0, columnspan=3, pady=12)
        return tab

    # ---- Tab6 漫画/条漫 -> CBZ ----
    def _build_tab6(self, nb):
        import comic_ops
        tab = ttk.Frame(nb)
        nb.add(tab, text="  6 漫画→CBZ  ")
        var_src = tk.StringVar()
        var_series = tk.StringVar()
        var_out = tk.StringVar()
        var_mode = tk.StringVar(value='auto')
        var_strip = tk.BooleanVar(value=True)     # 图片：按白带切分长图
        var_pdfsplit = tk.BooleanVar(value=False)  # PDF：拆双页跨页
        var_quality = tk.StringVar(value='92')

        ok, why = comic_ops.deps_available()
        tracker = comic_ops.DefaultTracker()

        def fill(d):
            var_src.set(d)
            var_series.set(tracker.apply('series', var_series.get(),
                                         comic_ops.default_series(d)))
            var_out.set(tracker.apply('out', var_out.get(),
                                      comic_ops.default_output(d)))

        def browse_dir():
            d = filedialog.askdirectory(title="选择目录（内含 PDF 或图片）")
            if d:
                fill(d)

        def browse_pdf():
            f = filedialog.askopenfilename(title="选择 PDF", filetypes=[("PDF 文件", "*.pdf")])
            if f:
                fill(f)

        ttk.Label(tab, text="单行本 PDF → 每卷一个 CBZ（跨页原样保留）；"
                            "条漫/散图目录 → 每话一个 CBZ（按白带切回分镜）。",
                  font=('', 12)).grid(row=0, column=0, columnspan=4, sticky='w',
                                      padx=12, pady=(14, 6))
        ttk.Label(tab, text="输入:").grid(row=1, column=0, sticky='w', padx=10, pady=10)
        ttk.Entry(tab, textvariable=var_src, font=('', 11)).grid(
            row=1, column=1, sticky='ew', padx=10, pady=10)
        ttk.Button(tab, text="选目录...", command=browse_dir).grid(row=1, column=2, padx=4, pady=10)
        ttk.Button(tab, text="选PDF...", command=browse_pdf).grid(row=1, column=3, padx=(0, 10), pady=10)

        f2 = ttk.Frame(tab)
        f2.grid(row=2, column=0, columnspan=4, sticky='ew', padx=6)
        f2.grid_columnconfigure(1, weight=1)
        ttk.Label(f2, text="系列名:").grid(row=0, column=0, sticky='w', padx=4, pady=6)
        ttk.Entry(f2, textvariable=var_series, font=('', 11)).grid(
            row=0, column=1, sticky='ew', padx=4, pady=6)
        ttk.Label(f2, text="质量:").grid(row=0, column=2, sticky='w', padx=(16, 4), pady=6)
        ttk.Combobox(f2, textvariable=var_quality, values=('95', '92', '88', '85'),
                     width=5, state='readonly').grid(row=0, column=3, padx=4, pady=6)

        f3 = ttk.Frame(tab)
        f3.grid(row=3, column=0, columnspan=4, sticky='ew', padx=18)
        ttk.Checkbutton(f3, text="图片输入：按白带切分长图（推荐）",
                        variable=var_strip).pack(side='left')
        ttk.Checkbutton(f3, text="PDF 输入：拆双页跨页（默认保留）",
                        variable=var_pdfsplit).pack(side='left', padx=(18, 0))

        self._path_row(tab, 4, "输出目录:", self._browse_out(var_out), var_out)
        log = self._log_area(tab, 5)
        if not ok:
            log.insert('end', why + '\n\n安装后重启本工具即可启用这个标签页。\n')

        def run():
            src = var_src.get().strip()
            if not src or not os.path.exists(src):
                log.insert('end', "请先选择输入（PDF 文件或图片目录）\n")
                return
            out = var_out.get().strip()
            if not out:
                out = comic_ops.default_output(src)
                var_out.set(out)
            bus = LogBus()

            def job(lg):
                comic_ops.convert(
                    src, out, var_series.get().strip(), mode=var_mode.get(),
                    log=lg, quality=int(var_quality.get()),
                    pdf_kw={'split': var_pdfsplit.get()},
                    img_kw={'split': var_strip.get()})
                lg("完成，输出目录：%s" % out)

            run_job(run_btn, bus.emit, job)
            self._poll(bus, log, run_btn)

        run_btn = ttk.Button(tab, text="开始转换", command=run,
                             state=('normal' if ok else 'disabled'))
        run_btn.grid(row=6, column=0, columnspan=4, pady=12)
        return tab

    def _poll(self, bus, log, state_widget):
        # 注意：ttk 控件取 state_widget['state'] 得到的是 _tkinter.Tcl_Obj，
        # 它和字符串比较恒为 False（`Tcl_Obj('disabled') == 'disabled'` 是 False）。
        # 必须用 instate()，否则这里第一次就退出，日志区永远空着。
        def step():
            bus.drain(log)
            if state_widget.instate(('disabled',)):
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
