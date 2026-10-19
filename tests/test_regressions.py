# -*- coding: utf-8 -*-
"""epub_tool 回归测试：覆盖 v1.2 修复的正确性缺陷。

运行：
    python -m unittest discover -s tests -v
"""
import os
import re
import sys
import shutil
import uuid
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import epub_tool as et  # noqa: E402
import epub_ops  # noqa: E402


# ---------------------------------------------------------------- 测试夹具

CHAPTER = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
           '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{t}</title></head>'
           '<body><h1>{t}</h1><p>{body}</p></body></html>')


def read_file(path, encoding='utf-8'):
    with open(path, 'r', encoding=encoding) as f:
        return f.read()


def make_epub(path, chapters, nav=None, ncx=None, opf_extra='', encoding='utf-8'):
    """按给定章节/目录/opf 片段生成一个最小 epub。

    chapters: [(文件名, 标题)]，文件写入 OEBPS 根目录。
    nav:      nav.xhtml 的 <nav> 内容；None 表示不生成。
    ncx:      toc.ncx 的 <navMap> 内容；None 表示不生成。
    """
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml',
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        for name, title in chapters:
            raw = CHAPTER.format(t=title, body=title + '正文内容。').encode(encoding)
            z.writestr('OEBPS/' + name, raw)
        if nav is not None:
            z.writestr('OEBPS/nav.xhtml',
                       '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html>'
                       '<html xmlns="http://www.w3.org/1999/xhtml" '
                       'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>目录</title></head>'
                       '<body>' + nav + '</body></html>')
        if ncx is not None:
            z.writestr('OEBPS/toc.ncx',
                       '<?xml version="1.0" encoding="UTF-8"?>'
                       '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                       '<navMap>' + ncx + '</navMap></ncx>')
        manifest = ''.join(
            '<item id="c%d" href="%s" media-type="application/xhtml+xml"/>' % (i, n)
            for i, (n, _t) in enumerate(chapters, 1))
        if nav is not None:
            manifest += ('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"'
                         ' properties="nav"/>')
        if ncx is not None:
            manifest += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        manifest += opf_extra
        spine = ''.join('<itemref idref="c%d"/>' % i for i in range(1, len(chapters) + 1))
        z.writestr('OEBPS/content.opf',
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                   'unique-identifier="bookid"><metadata '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:identifier id="bookid">urn:uuid:test</dc:identifier>'
                   '<dc:title>测试书</dc:title><dc:language>zh-CN</dc:language>'
                   '<meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>'
                   '</metadata><manifest>' + manifest + '</manifest>'
                   '<spine toc="ncx">' + spine + '</spine></package>')
    return path


class TempDirCase(unittest.TestCase):
    # 默认在仓库内 .testtmp/ 下建临时目录：Windows 上系统临时目录
    # 有时不可写（受终端/沙箱策略限制），也便于事后排查产物。
    # 可用环境变量 EPUBTOOL_TEST_TMP 覆盖。
    @classmethod
    def setUpClass(cls):
        cls._base = os.environ.get('EPUBTOOL_TEST_TMP') or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.testtmp')
        os.makedirs(cls._base, exist_ok=True)

    def setUp(self):
        self.tmp = _mktempdir(self._base)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


def _mktempdir(base):
    """在 base 下建一个唯一目录。

    不用 tempfile.mkdtemp：它以 0o700 创建目录，在部分 Windows 环境下
    （含本项目的受限运行环境）创建出的目录连创建者自己都写不进去。"""
    for _ in range(100):
        d = os.path.join(base, 'run_' + uuid.uuid4().hex[:8])
        try:
            os.makedirs(d)
            return d
        except FileExistsError:
            continue
    raise RuntimeError("无法创建测试临时目录: %s" % base)


# ---------------------------------------------------------------- P0-1 前导内容

class TestPreamble(TempDirCase):
    LINES = ["本书简介：这里是位于第一个分隔符之前的介绍。", "",
             "------------", "第一章 A", "正文A", "",
             "------------", "第二章 B", "正文B"]

    def test_dash_split_keeps_preamble(self):
        chapters = et.split_by_dash(self.LINES)
        self.assertEqual(len(chapters), 3)
        self.assertEqual(chapters[0][0], et.PREFACE_TITLE)
        self.assertIn("本书简介", "\n".join(chapters[0][1]))
        self.assertEqual(chapters[1][0], "第一章 A")

    def test_title_regex_keeps_preamble(self):
        lines = ["内容简介：简介文字。", "第一章 A", "正文A", "第二章 B", "正文B"]
        chapters = et.split_by_title_regex(lines)
        self.assertEqual(len(chapters), 3)
        self.assertEqual(chapters[0][0], et.PREFACE_TITLE)
        self.assertIn("内容简介", "\n".join(chapters[0][1]))
        self.assertEqual(chapters[1][0], "第一章 A")

    def test_no_preamble_when_file_starts_with_separator(self):
        lines = ["------------", "第一章 A", "正文A", "------------", "第二章 B", "正文B"]
        chapters = et.split_by_dash(lines)
        self.assertEqual([t for t, _ in chapters], ["第一章 A", "第二章 B"])

    def test_blank_leading_lines_do_not_create_preamble(self):
        lines = ["", "   ", "------------", "第一章 A", "正文A"]
        chapters = et.split_by_dash(lines)
        self.assertEqual([t for t, _ in chapters], ["第一章 A"])

    def test_separator_before_first_chapter_is_not_preamble(self):
        # 以分隔符开头、但按标题正则切分：不应凭空多出“前言”章
        lines = ["------------", "第一章 A", "正文A", "------------", "第二章 B", "正文B"]
        chapters = et.split_by_title_regex(lines)
        self.assertEqual([t for t, _ in chapters], ["第一章 A", "第二章 B"])

    def test_cli_reader_keeps_preamble(self):
        p = self.path("new.txt")
        with open(p, 'w', encoding='utf-8') as f:
            f.write("\n".join(self.LINES))
        chapters = epub_ops.read_new_chapters(p)
        self.assertEqual(chapters[0][0], "前言")
        self.assertIn("本书简介", "\n".join(chapters[0][1]))


# ---------------------------------------------------------------- P0-2 小标题

class TestSubheadings(TempDirCase):
    XHTML = ('<html xmlns="http://www.w3.org/1999/xhtml"><body>'
             '<h1>第一章</h1><p>第一段。</p><h2>第一节 小节</h2><p>第二段。</p>'
             '<h3>细节</h3><p>第三段。</p></body></html>')

    def test_title_only_uses_first_h1(self):
        title, _body = et._extract_chapter_text(self.XHTML)
        self.assertEqual(title, "第一章")

    def test_subheadings_stay_in_body(self):
        # 章节标题占 h1，正文小标题整体下沉一级：h2 -> "#"，h3 -> "##"
        _title, body = et._extract_chapter_text(self.XHTML)
        self.assertIn("# 第一节 小节", body)
        self.assertIn("## 细节", body)
        self.assertIn("第一段。", body)

    def test_subheading_roundtrips_to_html(self):
        _title, body = et._extract_chapter_text(self.XHTML)
        html = et.to_html_blocks(body)
        self.assertIn("<h2>第一节 小节</h2>", html)
        self.assertIn("<h3>细节</h3>", html)

    def test_heading_levels_never_collide_with_chapter_title(self):
        # 正文里的 "#" 不应生成 h1（h1 留给章节标题）
        html = et.to_html_blocks(["# 一级", "## 二级", "###### 六级", "####### 七级"])
        self.assertIn("<h2>一级</h2>", html)
        self.assertIn("<h3>二级</h3>", html)
        self.assertIn("<h6>六级</h6>", html)
        self.assertIn("<h6>七级</h6>", html)      # 超过 6 级截断到 h6
        self.assertNotIn("<h1>", html)

    def test_chapter_without_h1_falls_back_to_title_tag(self):
        xhtml = ('<html><head><title>备用标题</title></head>'
                 '<body><h2>小标题</h2><p>正文。</p></body></html>')
        title, body = et._extract_chapter_text(xhtml)
        self.assertEqual(title, "备用标题")
        self.assertIn("# 小标题", body)


# ---------------------------------------------------------------- P0-3 nav 层级

class TestNavInsertion(TempDirCase):
    def test_plain_list_appends_at_end(self):
        nav = '<nav epub:type="toc"><ol><li><a href="a.xhtml">A</a></li></ol></nav>'
        out, ok = et.insert_list_items(nav, '<li><a href="z.xhtml">Z</a></li>')
        self.assertTrue(ok)
        self.assertLess(out.index("z.xhtml"), out.index("</ol>"))
        self.assertEqual(out.count("<ol>"), 1)

    def test_nested_list_appends_to_outer_list_only(self):
        nav = ('<nav epub:type="toc"><ol>'
               '<li><a href="a.xhtml">A</a><ol><li><a href="a1.xhtml">A1</a></li></ol></li>'
               '</ol></nav>')
        out, ok = et.insert_list_items(nav, '<li><a href="z.xhtml">Z</a></li>')
        self.assertTrue(ok)
        # 新条目必须落在最外层 </ol> 之前，且位于嵌套 </ol> 之后
        self.assertGreater(out.index("z.xhtml"), out.index("a1.xhtml"))
        self.assertGreater(out.index("z.xhtml"), out.rindex("</ol>") - len(out))
        outer_end = out.rindex("</ol>")
        self.assertLess(out.index("z.xhtml"), outer_end)
        # 嵌套层没有被撑开：<ol> 仍只有两个
        self.assertEqual(out.count("<ol>"), 2)

    def test_ul_list_supported(self):
        nav = '<nav><ul><li><a href="a.xhtml">A</a></li></ul></nav>'
        out, ok = et.insert_list_items(nav, '<li>Z</li>')
        self.assertTrue(ok)
        self.assertLess(out.index("Z"), out.index("</ul>"))

    def test_missing_list_reports_failure(self):
        out, ok = et.insert_list_items('<nav><a href="a.xhtml">A</a></nav>', '<li>Z</li>')
        self.assertFalse(ok)
        self.assertNotIn("Z", out)

    def test_append_chapters_places_new_entry_at_top_level(self):
        nav = ('<nav epub:type="toc"><ol>'
               '<li><a href="chap_0001.xhtml">第一章</a>'
               '<ol><li><a href="chap_0002.xhtml">第一节</a></li></ol></li>'
               '</ol></nav>')
        src = make_epub(self.path("nested.epub"),
                        [("chap_0001.xhtml", "第一章"), ("chap_0002.xhtml", "第一节")],
                        nav=nav,
                        ncx='<navPoint id="n1" playOrder="1"><navLabel><text>第一章</text>'
                            '</navLabel><content src="chap_0001.xhtml"/></navPoint>')
        new_txt = self.path("新章.txt")
        with open(new_txt, 'w', encoding='utf-8') as f:
            f.write("新的一章\n正文。\n")
        out = self.path("out.epub")
        et.append_chapters(src, [new_txt], out)
        nav_out = zipfile.ZipFile(out).read("OEBPS/nav.xhtml").decode('utf-8')
        self.assertLess(nav_out.index("chap_0003.xhtml"), nav_out.rindex("</ol>"))
        self.assertGreater(nav_out.index("chap_0003.xhtml"), nav_out.index("chap_0002.xhtml"))


# ---------------------------------------------------------------- P0-4 章节顺序

class TestChapterOrder(TempDirCase):
    def test_partial_nav_does_not_drop_chapters(self):
        nav = ('<nav epub:type="toc"><ol>'
               '<li><a href="a.xhtml">A章</a></li>'
               '<li class="x"><a href="b.xhtml">B章</a></li>'
               '</ol></nav>')
        ncx = ('<navPoint id="n1"><navLabel><text>A章</text></navLabel>'
               '<content src="a.xhtml"/></navPoint>'
               '<navPoint id="n2"><navLabel><text>B章</text></navLabel>'
               '<content src="b.xhtml"/></navPoint>')
        src = make_epub(self.path("book.epub"),
                        [("a.xhtml", "A章"), ("b.xhtml", "B章")], nav=nav, ncx=ncx)
        out = self.path("book.txt")
        et.epub_to_txt(src, out)
        text = read_file(out)
        self.assertIn("A章", text)
        self.assertIn("B章", text)
        self.assertEqual(text.count("------------"), 2)

    def test_chapters_missing_from_toc_are_appended(self):
        # nav/ncx 只登记了 a，但包内还有 b（正文页），不能丢
        nav = '<nav epub:type="toc"><ol><li><a href="a.xhtml">A章</a></li></ol></nav>'
        ncx = ('<navPoint id="n1"><navLabel><text>A章</text></navLabel>'
               '<content src="a.xhtml"/></navPoint>')
        src = make_epub(self.path("book2.epub"),
                        [("a.xhtml", "A章"), ("b.xhtml", "B章")], nav=nav, ncx=ncx)
        out = self.path("book2.txt")
        et.epub_to_txt(src, out)
        text = read_file(out)
        self.assertIn("B章", text)

    def test_cover_and_nav_pages_excluded(self):
        nav = '<nav epub:type="toc"><ol><li><a href="a.xhtml">A章</a></li></ol></nav>'
        src = make_epub(self.path("book3.epub"),
                        [("a.xhtml", "A章"), ("cover.xhtml", "封面"), ("nav2.xhtml", "目录页")],
                        nav=nav)
        out = self.path("book3.txt")
        et.epub_to_txt(src, out)
        text = read_file(out)
        self.assertIn("A章", text)
        self.assertNotIn("封面", text)

    def test_fragment_and_subdir_hrefs(self):
        nav = '<nav epub:type="toc"><ol><li><a href="text/a.xhtml#top">A章</a></li></ol></nav>'
        src = self.path("sub.epub")
        with zipfile.ZipFile(src, 'w') as z:
            z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            z.writestr('OEBPS/nav.xhtml',
                       '<html xmlns="http://www.w3.org/1999/xhtml"><body>' + nav + '</body></html>')
            z.writestr('OEBPS/text/a.xhtml', CHAPTER.format(t="A章", body="正文"))
            z.writestr('OEBPS/content.opf',
                       '<package><manifest><item id="nav" href="nav.xhtml" properties="nav"/>'
                       '<item id="a" href="text/a.xhtml"/></manifest>'
                       '<spine><itemref idref="a"/></spine></package>')
        out = self.path("sub.txt")
        et.epub_to_txt(src, out)
        self.assertIn("A章", read_file(out))


# ---------------------------------------------------------------- P0-5 封面识别

class TestCoverRef(unittest.TestCase):
    def test_properties_after_href(self):
        opf = ('<manifest><item id="cimg" href="img/c.png" media-type="image/png"'
               ' properties="cover-image"/></manifest>')
        self.assertEqual(et.find_cover_ref(opf), ("cimg", "img/c.png"))

    def test_properties_before_href(self):
        opf = ('<manifest><item properties="cover-image" id="cimg" href="img/c.png"'
               ' media-type="image/png"/></manifest>')
        self.assertEqual(et.find_cover_ref(opf), ("cimg", "img/c.png"))

    def test_meta_cover_wins(self):
        opf = ('<metadata><meta name="cover" content="mycover"/></metadata>'
               '<manifest><item id="mycover" href="images/x.jpg" media-type="image/jpeg"/>'
               '<item id="other" href="images/y.png" media-type="image/png"'
               ' properties="cover-image"/></manifest>')
        self.assertEqual(et.find_cover_ref(opf), ("mycover", "images/x.jpg"))

    def test_single_quoted_attributes(self):
        opf = "<manifest><item id='c' href='img/c.png' properties='cover-image'/></manifest>"
        self.assertEqual(et.find_cover_ref(opf), ("c", "img/c.png"))

    def test_no_cover(self):
        self.assertIsNone(et.find_cover_ref('<manifest><item id="a" href="a.xhtml"/></manifest>'))

    def test_rewrite_tag_keeps_other_attributes(self):
        raw = '<item properties="cover-image" id="c" href="old.png" media-type="image/png"/>'
        out = et.rewrite_tag(raw, {'href': 'new.jpg', 'media-type': 'image/jpeg'})
        self.assertIn('href="new.jpg"', out)
        self.assertIn('properties="cover-image"', out)
        self.assertNotIn('old.png', out)


# ---------------------------------------------------------------- P0-6 编码

class TestEncoding(TempDirCase):
    def test_gbk_chapter_is_decoded(self):
        src = self.path("gbk.epub")
        with zipfile.ZipFile(src, 'w') as z:
            z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            z.writestr('OEBPS/nav.xhtml',
                       '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                       '<nav epub:type="toc"><ol><li><a href="c1.xhtml">第一章</a></li></ol></nav>'
                       '</body></html>')
            xhtml = ('<?xml version="1.0" encoding="GBK"?>'
                     '<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="GBK"/>'
                     '<title>第一章</title></head><body><h1>第一章</h1>'
                     '<p>这是一段中文正文。</p></body></html>')
            z.writestr('OEBPS/c1.xhtml', xhtml.encode('gbk'))
            z.writestr('OEBPS/content.opf',
                       '<package><manifest><item id="nav" href="nav.xhtml" properties="nav"/>'
                       '<item id="c1" href="c1.xhtml"/></manifest>'
                       '<spine><itemref idref="c1"/></spine></package>')
        out = self.path("gbk.txt")
        et.epub_to_txt(src, out)
        text = read_file(out)
        self.assertIn("第一章", text)
        self.assertIn("这是一段中文正文。", text)
        self.assertNotIn("\ufffd", text)

    def test_gbk_text_file(self):
        p = self.path("gbk.txt")
        with open(p, 'wb') as f:
            f.write("第一章 中文标题\n正文内容。\n".encode('gbk'))
        self.assertEqual(et.read_text(p), "第一章 中文标题\n正文内容。\n")

    def test_utf8_bom(self):
        p = self.path("bom.txt")
        with open(p, 'wb') as f:
            f.write('\ufeff第一章\n正文\n'.encode('utf-8'))
        self.assertEqual(et.read_text(p), "第一章\n正文\n")

    def test_utf16_bom(self):
        p = self.path("u16.txt")
        with open(p, 'wb') as f:
            f.write("第一章\n正文\n".encode('utf-16'))
        self.assertEqual(et.read_text(p), "第一章\n正文\n")


# ---------------------------------------------------------------- P1-1 临时目录

class TestCoverEmbedding(TempDirCase):
    """打包时把封面图片嵌进 epub。"""

    def _cover(self, name='c.png'):
        p = self.path(name)
        with open(p, 'wb') as f:
            f.write(_tiny_png())
        return p

    def test_build_embeds_cover_image(self):
        cover = self._cover()
        out = self.path("with_cover.epub")
        et.build_epub([("第一章", ["正文。"])], "封面书", out, cover_image=cover)
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            opf = z.read("OEBPS/content.opf").decode('utf-8')
            cover_page = z.read("OEBPS/cover.xhtml").decode('utf-8')
        self.assertIn("OEBPS/images/cover.png", names)
        self.assertIn('properties="cover-image"', opf)
        self.assertIn('<meta name="cover" content="cover-image"/>', opf)
        self.assertIn("<img", cover_page)

    def test_read_cover_roundtrip(self):
        cover = self._cover()
        out = self.path("with_cover2.epub")
        et.build_epub([("第一章", ["正文。"])], "封面书", out, cover_image=cover)
        data, ext = et.read_cover(out)
        self.assertEqual(ext, ".png")
        self.assertEqual(data, _tiny_png())

    def test_build_without_cover_keeps_text_page(self):
        out = self.path("plain.epub")
        et.build_epub([("第一章", ["正文。"]), ("第二章", ["正文。"])], "无封面", out)
        with zipfile.ZipFile(out) as z:
            cover_page = z.read("OEBPS/cover.xhtml").decode('utf-8')
        self.assertNotIn("<img", cover_page)
        self.assertIn("共 2 章", cover_page)
        self.assertIsNone(et.read_cover(out))

    def test_missing_cover_file_raises(self):
        with self.assertRaises(ValueError):
            et.build_epub([("第一章", ["正文。"])], "书",
                          self.path("x.epub"), cover_image=self.path("nope.png"))

    def test_append_with_new_cover(self):
        book = self.path("base.epub")
        et.build_epub([("第一章", ["正文。"])], "书", book)
        cover = self._cover("new.png")
        add = self.path("add.txt")
        with open(add, 'w', encoding='utf-8') as f:
            f.write("新章\n正文。\n")
        out = self.path("out.epub")
        et.append_chapters(book, [add], out, cover_image=cover)
        data, ext = et.read_cover(out)
        self.assertEqual(ext, ".png")
        self.assertEqual(data, _tiny_png())


class TestWebPCover(TempDirCase):
    """WebP 封面：GDI+ 不认，要靠 WIC 解码并转成 PNG 再嵌入。"""

    # 4×6 的极小 WebP（base64），避免测试依赖 Pillow
    TINY_WEBP = (
        'UklGRjoAAABXRUJQVlA4IC4AAACwAQCdASoEAAYAAUAmJaACdLoABDAAAP7x3I/4DdfFtMv/'
        'vYL/3YL/3YL/WwAA')

    def _webp_bytes(self):
        import base64
        return base64.b64decode(self.TINY_WEBP)

    def test_webp_preview_supported_or_graceful(self):
        data = self._webp_bytes()
        png = et.image_preview_png(data, 112, 150)
        if png is None:
            self.skipTest('当前系统没有 WebP 解码器（WIC codec 未安装）')
        self.assertTrue(png.startswith(b'\x89PNG'))

    def test_webp_cover_becomes_png_in_epub(self):
        src = self.path("c.webp")
        with open(src, 'wb') as f:
            f.write(self._webp_bytes())
        out = self.path("webp.epub")
        et.build_epub([("第一章", ["正文。"])], "书", out, cover_image=src)
        data, ext = et.read_cover(out)
        if ext == '.jpg' and data == self._webp_bytes():
            self.skipTest('当前系统没有 WebP 解码器，回退为原样嵌入')
        self.assertEqual(ext, '.png')
        self.assertTrue(data.startswith(b'\x89PNG'))
        with zipfile.ZipFile(out) as z:
            opf = z.read("OEBPS/content.opf").decode('utf-8')
        self.assertIn('media-type="image/png"', opf)

    def test_set_cover_with_webp(self):
        base = self.path("base.epub")
        et.build_epub([("第一章", ["正文。"])], "书", base)
        src = self.path("c2.webp")
        with open(src, 'wb') as f:
            f.write(self._webp_bytes())
        out = self.path("out.epub")
        et.set_cover(base, src, out)
        data, ext = et.read_cover(out)
        if ext == '.jpg':
            self.skipTest('当前系统没有 WebP 解码器')
        self.assertEqual(ext, '.png')


class TestBookStructure(TempDirCase):
    """v1.3 新增：元数据、分卷、书内目录页、guide、封面 linear。"""

    def _opf(self, path):
        with zipfile.ZipFile(path) as z:
            return z.read("OEBPS/content.opf").decode('utf-8')

    def test_metadata_written(self):
        out = self.path("meta.epub")
        et.build_epub([("第一章", ["正文。"])], "书", out, meta={
            'creator': '烽火戏诸侯',
            'date': '2022',
            'description': '一句话简介',
            'rights': '版权所有',
            'series': '雪中悍刀行',
            'series_index': '1',
        })
        opf = self._opf(out)
        self.assertIn('<dc:creator>烽火戏诸侯</dc:creator>', opf)
        self.assertIn('<dc:date>2022</dc:date>', opf)
        self.assertIn('<dc:description>一句话简介</dc:description>', opf)
        self.assertIn('<dc:rights>版权所有</dc:rights>', opf)
        self.assertIn('name="calibre:series" content="雪中悍刀行"', opf)
        self.assertIn('name="calibre:series_index" content="1"', opf)

    def test_metadata_optional(self):
        out = self.path("plain_meta.epub")
        et.build_epub([("第一章", ["正文。"])], "书", out)
        opf = self._opf(out)
        self.assertNotIn('calibre:series', opf)
        self.assertNotIn('<dc:creator>', opf)

    def test_volume_structure(self):
        chapters = [
            ("第一卷 白马出凉州", "第001章", ["正文一。"]),
            ("第一卷 白马出凉州", "第002章", ["正文二。"]),
            ("第二卷 剑来", "第003章", ["正文三。"]),
        ]
        out = self.path("vol.epub")
        et.build_epub(chapters, "书", out)
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            nav = z.read("OEBPS/nav.xhtml").decode('utf-8')
            ncx = z.read("OEBPS/toc.ncx").decode('utf-8')
            opf = z.read("OEBPS/content.opf").decode('utf-8')
        self.assertIn("OEBPS/vol_001.xhtml", names)
        self.assertIn("OEBPS/vol_002.xhtml", names)
        # nav 里卷是父级，章在 <ol> 里
        self.assertIn('href="vol_001.xhtml"', nav)
        self.assertIn('<ol>', nav)
        self.assertGreaterEqual(nav.count('<ol>'), 2)
        # ncx 深度 2，子条目缩进在卷条目内部
        self.assertIn('name="dtb:depth" content="2"', ncx)
        self.assertIn('<navPoint id="np1" playOrder="1">', ncx)
        self.assertIn('      <navPoint id="np2" playOrder="2">', ncx)
        # spine 顺序：卷页在它自己的章节之前
        spine = re.findall(r'<itemref idref="([^"]+)"[^>]*/>', opf)
        self.assertLess(spine.index("vol_001"), spine.index("c0001"))
        self.assertLess(spine.index("vol_002"), spine.index("c0003"))

    def test_toc_page_and_guide(self):
        out = self.path("toc.epub")
        et.build_epub([("第一章", ["正文。"])], "书", out)
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            toc = z.read("OEBPS/book-toc.xhtml").decode('utf-8')
            opf = z.read("OEBPS/content.opf").decode('utf-8')
        self.assertIn("OEBPS/book-toc.xhtml", names)
        self.assertIn("第一章", toc)
        self.assertIn('idref="book-toc" linear="yes"', opf)
        self.assertIn('<reference type="toc"', opf)
        self.assertIn('<reference type="cover"', opf)
        self.assertIn('<reference type="text"', opf)

    def test_toc_page_can_be_disabled(self):
        out = self.path("notoc.epub")
        et.build_epub([("第一章", ["正文。"])], "书", out, toc_page=False)
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            opf = z.read("OEBPS/content.opf").decode('utf-8')
        self.assertNotIn("OEBPS/book-toc.xhtml", names)
        self.assertNotIn('<reference type="toc"', opf)

    def test_cover_not_linear(self):
        out = self.path("cover_linear.epub")
        png = self.path("c.png")
        with open(png, 'wb') as f:
            f.write(_tiny_png())
        et.build_epub([("第一章", ["正文。"])], "书", out, cover_image=png)
        opf = self._opf(out)
        self.assertIn('<itemref idref="cover" linear="no"/>', opf)


class TestTempCleanup(TempDirCase):
    def test_build_epub_cleans_up_on_error(self):
        # 正文里放一个非字符串元素，让写章节文件时抛错（此时临时目录已创建）
        with self.assertRaises((AttributeError, TypeError)):
            et.build_epub([("第一章", [123])], "坏书", self.path("bad.epub"))
        leftovers = [d for d in os.listdir(self.tmp) if d.startswith(".epub_build_")]
        self.assertEqual(leftovers, [])

    def test_build_epub_rejects_empty_chapters(self):
        with self.assertRaises(ValueError):
            et.build_epub([], "空书", self.path("empty.epub"))


# ---------------------------------------------------------------- 端到端

class TestEndToEnd(TempDirCase):
    def test_split_build_extract_roundtrip(self):
        source = self.path("book.txt")
        with open(source, 'w', encoding='utf-8') as f:
            f.write("------------\n第一章 A\n正文A1。\n正文A2。\n"
                    "------------\n第二章 B\n正文B1。\n")
        chapters = et.split_file(source, "整行12个横线分隔（大明测字天师式）")
        epub = self.path("book.epub")
        et.build_epub(chapters, "测试书", epub)
        txt = self.path("back.txt")
        et.epub_to_txt(epub, txt)
        restored = et.split_by_dash(read_file(txt).splitlines())
        self.assertEqual([t for t, _ in restored], ["第一章 A", "第二章 B"])
        self.assertIn("正文A2。", restored[0][1])

    def test_append_preserves_cover_after_refactor(self):
        png = self.path("cover.png")
        with open(png, 'wb') as f:
            f.write(_tiny_png())
        src = self.path("base.epub")
        et.build_epub([("第一章", ["正文。"])], "封面书", src)
        with_cover = self.path("with_cover.epub")
        et.set_cover(src, png, with_cover)
        self.assertIsNotNone(et.extract_cover(with_cover, self.path("out.png")))


def _tiny_png():
    import zlib
    import struct

    def chunk(t, d):
        c = t + d
        return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', 4, 4, 8, 2, 0, 0, 0))
    raw = b''.join(b'\x00' + bytes((200, 30, 30)) * 4 for _ in range(4))
    idat = chunk(b'IDAT', zlib.compress(raw))
    return sig + ihdr + idat + chunk(b'IEND', b'')


if __name__ == '__main__':
    unittest.main(verbosity=2)
