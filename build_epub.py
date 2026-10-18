# -*- coding: utf-8 -*-
"""把《大明测字天师.txt》按 12 个横线的分隔符拆分为 896 章，构建标准 EPUB3（含 NCX 兼容）。"""
import os
import re
import uuid
import html
import zipfile
import datetime

SRC = r"C:\Users\vasts\Downloads\大明测字天师.txt"
OUT = r"C:\Users\vasts\Downloads\大明测字天师.epub"
TITLE = "大明测字天师"
LANG = "zh-CN"
UID = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, SRC))

SEP_RE = re.compile(r'^-{12}$')


def read_chapters():
    """按分隔符（整行 12 个横线）切分，返回 [(标题, 正文行列表), ...]"""
    with open(SRC, 'r', encoding='utf-8-sig') as f:
        text = f.read()
    lines = text.splitlines()
    seps = [i for i, ln in enumerate(lines) if SEP_RE.match(ln)]
    chapters = []
    for idx, s in enumerate(seps):
        end = seps[idx + 1] if idx + 1 < len(seps) else len(lines)
        body = lines[s + 1:end]
        title = ""
        rest = []
        for j, ln in enumerate(body):
            if ln.strip():
                title = ln.strip()
                rest = body[j + 1:]
                break
        chapters.append((title, rest))
    return chapters


def para_html(line):
    return "<p>" + html.escape(line.strip()) + "</p>"


def chapter_xhtml(num, title, body):
    paras = "\n".join(para_html(ln) for ln in body if ln.strip())
    esc = html.escape(title)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
        '<head>\n'
        '<meta charset="UTF-8"/>\n'
        '<title>' + esc + '</title>\n'
        '<link rel="stylesheet" type="text/css" href="style.css"/>\n'
        '</head>\n'
        '<body>\n'
        '<h1 class="chapter-title">' + esc + '</h1>\n'
        + paras + '\n'
        '</body>\n'
        '</html>\n'
    )


def main():
    chapters = read_chapters()
    print("章节数:", len(chapters))
    assert len(chapters) == 896, "章节数不是 896，请检查分隔符规则"

    oebps = os.path.join(os.path.dirname(OUT), "epub_build", "oebps")
    os.makedirs(oebps, exist_ok=True)

    # ---------- 生成章节 XHTML ----------
    chap_files = []
    for i, (title, body) in enumerate(chapters, start=1):
        fn = "chap_%04d.xhtml" % i
        with open(os.path.join(oebps, fn), 'w', encoding='utf-8') as f:
            f.write(chapter_xhtml(i, title, body))
        chap_files.append((fn, title))
    print("章节文件已生成:", len(chap_files))

    # ---------- CSS ----------
    css = (
        'body { font-family: "Source Han Serif SC", "Noto Serif CJK SC", "SimSun", serif;'
        ' margin: 0 1em; line-height: 1.8; }\n'
        'h1.chapter-title { text-align: center; font-size: 1.4em; margin: 1.2em 0 1.5em;'
        ' page-break-before: always; page-break-after: avoid; }\n'
        'p { text-indent: 2em; margin: 0.4em 0; text-align: justify; }\n'
    )
    with open(os.path.join(oebps, "style.css"), 'w', encoding='utf-8') as f:
        f.write(css)

    # ---------- cover 页 ----------
    cover = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
        '<head><meta charset="UTF-8"/><title>' + TITLE + '</title>'
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        '<body>\n'
        '<div style="text-align:center; margin-top: 40%;">\n'
        '<h1 style="font-size:2em;">' + TITLE + '</h1>\n'
        '<p style="text-indent:0; color:#666; margin-top:2em;">共 '
        + str(len(chapters)) + ' 章</p>\n'
        '</div>\n</body>\n</html>\n'
    )
    with open(os.path.join(oebps, "cover.xhtml"), 'w', encoding='utf-8') as f:
        f.write(cover)

    # ---------- nav.xhtml (EPUB3 目录) ----------
    items = "\n".join(
        '    <li><a href="%s">%s</a></li>' % (fn, html.escape(t))
        for fn, t in chap_files
    )
    nav = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
        ' xml:lang="zh-CN" lang="zh-CN">\n'
        '<head><meta charset="UTF-8"/><title>目录</title>'
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        '<body>\n'
        '<nav epub:type="toc" id="toc">\n'
        '<h1 class="chapter-title">目录</h1>\n<ol>\n' + items + '\n</ol>\n'
        '</nav>\n</body>\n</html>\n'
    )
    with open(os.path.join(oebps, "nav.xhtml"), 'w', encoding='utf-8') as f:
        f.write(nav)

    # ---------- toc.ncx (EPUB2 兼容目录) ----------
    navpoints = "\n".join(
        '    <navPoint id="navpoint-%d" playOrder="%d"><navLabel><text>%s</text></navLabel>'
        '<content src="%s"/></navPoint>' % (i, i, html.escape(t), fn)
        for i, (fn, t) in enumerate(chap_files, start=1)
    )
    ncx = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        '<head>\n'
        '<meta name="dtb:uid" content="' + UID + '"/>\n'
        '<meta name="dtb:depth" content="1"/>\n'
        '<meta name="dtb:totalPageCount" content="0"/>\n'
        '<meta name="dtb:maxPageNumber" content="0"/>\n'
        '</head>\n'
        '<docTitle><text>' + TITLE + '</text></docTitle>\n'
        '<navMap>\n' + navpoints + '\n</navMap>\n'
        '</ncx>\n'
    )
    with open(os.path.join(oebps, "toc.ncx"), 'w', encoding='utf-8') as f:
        f.write(ncx)

    # ---------- content.opf ----------
    manifest = (
        '    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>\n'
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '    <item id="css" href="style.css" media-type="text/css"/>\n'
    )
    for i, (fn, _t) in enumerate(chap_files, start=1):
        manifest += '    <item id="c%04d" href="%s" media-type="application/xhtml+xml"/>\n' % (i, fn)
    spine = (
        '    <itemref idref="cover"/>\n'
    )
    for i in range(1, len(chap_files) + 1):
        spine += '    <itemref idref="c%04d"/>\n' % i
    modified = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid"'
        ' xml:lang="' + LANG + '">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '  <dc:identifier id="bookid">' + UID + '</dc:identifier>\n'
        '  <dc:title>' + TITLE + '</dc:title>\n'
        '  <dc:language>' + LANG + '</dc:language>\n'
        '  <meta property="dcterms:modified">' + modified + '</meta>\n'
        '</metadata>\n'
        '<manifest>\n' + manifest + '</manifest>\n'
        '<spine toc="ncx">\n' + spine + '</spine>\n'
        '</package>\n'
    )
    with open(os.path.join(oebps, "content.opf"), 'w', encoding='utf-8') as f:
        f.write(opf)

    # ---------- META-INF ----------
    meta_inf = os.path.join(os.path.dirname(OUT), "epub_build", "META-INF")
    os.makedirs(meta_inf, exist_ok=True)
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles>\n'
        '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        '  </rootfiles>\n'
        '</container>\n'
    )
    with open(os.path.join(meta_inf, "container.xml"), 'w', encoding='utf-8') as f:
        f.write(container)

    # ---------- 打包 ----------
    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.write(os.path.join(meta_inf, "container.xml"), 'META-INF/container.xml')
        for root, _dirs, files in os.walk(oebps):
            for fn in sorted(files):
                z.write(os.path.join(root, fn), 'OEBPS/' + fn)
    print("EPUB 已生成:", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
