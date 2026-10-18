# -*- coding: utf-8 -*-
"""EPUB 章节操作工具
用法:
  python epub_ops.py unpack <epub> <输出目录>         # 解包 epub 为章节文件
  python epub_ops.py append <epub> <新章节.txt> [输出epub]  # 末尾追加章节并重打包

新章节.txt 格式（与主书相同的分隔约定）:
------------
新章标题一
正文段落……
------------
新章标题二
正文段落……
"""
import os
import re
import sys
import html
import uuid
import zipfile
import datetime
import shutil

SEP_RE = re.compile(r'^-{12}$')
CHAP_NAME_RE = re.compile(r'^chap_(\d{4})\.xhtml$')


def log(*args):
    print(*args)


# ---------------- 解包 ----------------
def unpack(epub, outdir):
    if os.path.exists(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    with zipfile.ZipFile(epub) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError("epub 已损坏: " + bad)
        z.extractall(outdir)
    # 列出章节文件
    oebps = os.path.join(outdir, "OEBPS")
    if not os.path.isdir(oebps):
        # 有些 epub 的 opf 不在 OEBPS 目录，扫描所有 xhtml
        hits = []
        for root, _d, files in os.walk(outdir):
            for f in files:
                if f.lower().endswith((".xhtml", ".html")):
                    hits.append(os.path.relpath(os.path.join(root, f), outdir))
        log("未找到 OEBPS 目录，章节文件示例:", hits[:5])
        return hits
    chaps = sorted(f for f in os.listdir(oebps) if CHAP_NAME_RE.match(f))
    log("解包完成 ->", outdir)
    log("章节文件数:", len(chaps))
    log("首个:", chaps[0] if chaps else "无", " 末尾:", chaps[-1] if chaps else "无")
    return chaps


# ---------------- 读取新章节 ----------------
def read_new_chapters(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    lines = text.splitlines()
    seps = [i for i, ln in enumerate(lines) if SEP_RE.match(ln)]
    if not seps:
        # 没有分隔符：整个文件当作一个章节，首非空行为标题
        title, rest = "", []
        for j, ln in enumerate(lines):
            if ln.strip():
                title = ln.strip()
                rest = lines[j + 1:]
                break
        return [(title, rest)]
    chapters = []
    for idx, s in enumerate(seps):
        end = seps[idx + 1] if idx + 1 < len(seps) else len(lines)
        body = lines[s + 1:end]
        title, rest = "", []
        for j, ln in enumerate(body):
            if ln.strip():
                title = ln.strip()
                rest = body[j + 1:]
                break
        chapters.append((title, rest))
    return chapters


# ---------------- 追加章节 ----------------
def _escape(s):
    return html.escape(s, quote=True)


def _chapter_xhtml(title, body, css_ref="style.css"):
    paras = "\n".join("<p>" + _escape(ln.strip()) + "</p>" for ln in body if ln.strip())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
        '<head><meta charset="UTF-8"/><title>' + _escape(title) + '</title>'
        '<link rel="stylesheet" type="text/css" href="' + _escape(css_ref) + '"/></head>\n'
        '<body>\n<h1 class="chapter-title">' + _escape(title) + '</h1>\n'
        + paras + '\n</body>\n</html>\n'
    )


def _find_opf_dir(extracted):
    """定位含 content.opf 的目录。"""
    for root, _d, files in os.walk(extracted):
        for f in files:
            if f == "content.opf":
                return root
    raise RuntimeError("未找到 content.opf，无法解析 epub 结构")


def append_chapters(epub, new_chapters_path, out_epub):
    """末尾追加章节：更新 opf manifest/spine、nav、ncx，重打包。"""
    tmp = os.path.join(os.path.dirname(os.path.abspath(epub)),
                       ".epub_append_tmp_" + uuid.uuid4().hex[:8])
    try:
        unpack(epub, tmp)
        chapters = read_new_chapters(new_chapters_path)
        if not chapters:
            raise RuntimeError("新章节文件为空或格式不正确")
        log("新章节数:", len(chapters), [t for t, _ in chapters])

        opf_dir = _find_opf_dir(tmp)
        opf_path = os.path.join(opf_dir, "content.opf")
        with open(opf_path, "r", encoding="utf-8") as f:
            opf = f.read()

        # 已有章节命名模式
        manifest_items = re.findall(r'<item id="([^"]+)" href="([^"]+)"', opf)
        hrefs = [h for _i, h in manifest_items]
        chap_hrefs = [h for h in hrefs if CHAP_NAME_RE.match(h)]
        if len(chap_hrefs) == len([h for h in hrefs if h.lower().endswith((".xhtml", ".html"))]):
            # 全部内容页都是 chap_NNNN 命名，继续编号
            nums = [int(CHAP_NAME_RE.match(h).group(1)) for h in chap_hrefs]
            next_no = max(nums) + 1 if nums else 1
            new_files = ["chap_%04d.xhtml" % (next_no + i) for i in range(len(chapters))]
        else:
            # 其他命名：用独立前缀避免冲突
            existing = set(hrefs)
            n = 1
            new_files = []
            while len(new_files) < len(chapters):
                fn = "newchap_%03d.xhtml" % n
                if fn not in existing:
                    new_files.append(fn)
                n += 1

        css_ref = "style.css" if os.path.exists(os.path.join(opf_dir, "style.css")) else \
            (os.path.basename([h for h in hrefs if h.endswith(".css")][0])
             if any(h.endswith(".css") for h in hrefs) else "")

        # 写新章节文件
        used_ids = set(manifest_items and [i for i, _h in manifest_items])
        new_ids = []
        for fn, (title, body) in zip(new_files, chapters):
            with open(os.path.join(opf_dir, fn), "w", encoding="utf-8") as f:
                f.write(_chapter_xhtml(title, body, css_ref))
            # 生成唯一 id
            base = re.sub(r'\.x?html$', '', fn)
            iid = base
            n = 1
            while iid in used_ids:
                iid = base + "_%d" % n
                n += 1
            used_ids.add(iid)
            new_ids.append(iid)
        log("新章节文件:", new_files)

        # ---- 更新 opf ----
        # manifest：在 </manifest> 前插入
        ins_manifest = "".join(
            '    <item id="%s" href="%s" media-type="application/xhtml+xml"/>\n'
            % (iid, fn) for iid, fn in zip(new_ids, new_files)
        )
        if '</manifest>' not in opf:
            raise RuntimeError("opf 中未找到 </manifest>")
        opf = opf.replace('</manifest>', ins_manifest + '</manifest>', 1)
        # spine：在 </spine> 前插入
        ins_spine = "".join('    <itemref idref="%s"/>\n' % iid for iid in new_ids)
        if '</spine>' not in opf:
            raise RuntimeError("opf 中未找到 </spine>")
        opf = opf.replace('</spine>', ins_spine + '</spine>', 1)
        # 更新 modified 时间
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        opf = re.sub(r'<meta property="dcterms:modified">[^<]*</meta>',
                     '<meta property="dcterms:modified">' + now + '</meta>', opf, count=1)
        with open(opf_path, "w", encoding="utf-8") as f:
            f.write(opf)

        # ---- 更新 nav.xhtml ----
        nav_path = os.path.join(opf_dir, "nav.xhtml")
        if os.path.exists(nav_path):
            with open(nav_path, "r", encoding="utf-8") as f:
                nav = f.read()
            if '</ol>' in nav:
                ins = "".join('    <li><a href="%s">%s</a></li>\n'
                              % (fn, _escape(t)) for fn, (t, _b) in zip(new_files, chapters))
                nav = nav.replace('</ol>', ins + '</ol>', 1)
                with open(nav_path, "w", encoding="utf-8") as f:
                    f.write(nav)
            else:
                log("警告: nav.xhtml 无 <ol>，跳过 nav 更新")

        # ---- 更新 toc.ncx ----
        ncx_path = os.path.join(opf_dir, "toc.ncx")
        if os.path.exists(ncx_path):
            with open(ncx_path, "r", encoding="utf-8") as f:
                ncx = f.read()
            plays = [int(m) for m in re.findall(r'playOrder="(\d+)"', ncx)]
            next_play = max(plays) + 1 if plays else 1
            ins = ""
            for i, (fn, (t, _b)) in enumerate(zip(new_files, chapters)):
                ins += ('    <navPoint id="np_%s" playOrder="%d">'
                        '<navLabel><text>%s</text></navLabel>'
                        '<content src="%s"/></navPoint>\n'
                        % (re.sub(r'[^0-9A-Za-z]', '', fn), next_play + i,
                           _escape(t), fn))
            if '</navMap>' in ncx:
                ncx = ncx.replace('</navMap>', ins + '</navMap>', 1)
                with open(ncx_path, "w", encoding="utf-8") as f:
                    f.write(ncx)
            else:
                log("警告: toc.ncx 无 </navMap>，跳过 ncx 更新")

        # ---- 重新打包 ----
        if os.path.exists(out_epub):
            os.remove(out_epub)
        with zipfile.ZipFile(out_epub, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            for root, _d, files in os.walk(tmp):
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, tmp).replace("\\", "/")
                    if arc == "mimetype":
                        continue
                    z.write(full, arc)
        log("已生成:", out_epub, os.path.getsize(out_epub), "bytes")
    finally:
        if os.path.exists(tmp):
            shutil.rmtree(tmp)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "unpack" and len(sys.argv) >= 4:
        unpack(sys.argv[2], sys.argv[3])
    elif cmd == "append" and len(sys.argv) >= 4:
        out = sys.argv[4] if len(sys.argv) >= 5 else \
            re.sub(r'\.epub$', '_追加.epub', sys.argv[2], flags=re.I)
        append_chapters(sys.argv[2], sys.argv[3], out)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
