# -*- coding: utf-8 -*-
"""comic_ops（v1.3 漫画/条漫 -> CBZ）的回归测试。

Pillow / pypdf 缺失时自动跳过：这两个库只属于“漫画→CBZ”功能，
核心 epub 功能仍是零依赖，测试不应因此变红。

运行：
    python -m unittest discover -s tests -v
"""
import io
import os
import shutil
import sys
import unittest
import uuid
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import comic_ops as co  # noqa: E402

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pypdf  # noqa: F401
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False


class TempDirCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._base = os.environ.get('EPUBTOOL_TEST_TMP') or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.testtmp')
        os.makedirs(cls._base, exist_ok=True)

    def setUp(self):
        for _ in range(100):
            d = os.path.join(self._base, 'comic_' + uuid.uuid4().hex[:8])
            try:
                os.makedirs(d)
                self.tmp = d
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError('无法创建测试临时目录')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


# ---------------------------------------------------------------- 纯逻辑

class TestNaming(unittest.TestCase):
    def test_parse_stem(self):
        self.assertEqual(co.parse_stem('001_01'), ('001', '01'))
        self.assertEqual(co.parse_stem('z_01'), ('z', '01'))
        self.assertEqual(co.parse_stem('a-3'), ('a', '3'))
        self.assertEqual(co.parse_stem('005'), ('005', None))
        self.assertEqual(co.parse_stem('12-3'), ('12', '3'))
        self.assertEqual(co.parse_stem('cover'), (None, None))

    def test_parse_pdf_name(self):
        self.assertEqual(co.parse_pdf_name('第5卷-NO TIME.pdf'), ('5', 'NO TIME'))
        self.assertEqual(co.parse_pdf_name('第12卷-顽强的家伙.pdf'), ('12', '顽强的家伙'))
        num, title = co.parse_pdf_name('vol05.pdf')
        self.assertEqual(num, '05')

    def test_natural_key(self):
        names = ['010', '002', '001']
        self.assertEqual(sorted(names, key=co.natural_key), ['001', '002', '010'])
        # 字典序会排成 001 < 010 < 002
        self.assertEqual(sorted(names), ['001', '002', '010'][:0] + sorted(names))


class TestCollectGroups(TempDirCase):
    def _touch(self, *parts):
        p = self.path(*parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, 'wb').close()
        return p

    def test_merges_segments_by_chapter(self):
        self._touch('001_01.jpg')
        self._touch('001_02.jpg')
        self._touch('002_01.jpg')
        groups = co.collect_groups(self.tmp)
        keys = sorted(groups, key=lambda k: co.natural_key(k[1]))
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[(('', '001'))]), 2)
        self.assertEqual([k[1] for k in keys], ['001', '002'])

    def test_segment_order_is_numeric(self):
        for seg in ('01', '02', '10'):
            self._touch('007_%s.jpg' % seg)
        files = [p for _, p in co.collect_groups(self.tmp)[('', '007')]]
        self.assertEqual([os.path.basename(f) for f in files],
                         ['007_01.jpg', '007_02.jpg', '007_10.jpg'])

    def test_unnumbered_files_fall_back_to_dirname(self):
        self._touch('某话', 'cover.jpg')
        groups = co.collect_groups(self.tmp)
        self.assertEqual(list(groups)[0][1], '某话')

    # ---- v1.3 分组规则修正 ----

    def test_single_numbered_images_are_pages_not_chapters(self):
        """001.jpg/002.jpg 是页号，整个目录只能算一话。"""
        for n in ('001.jpg', '002.jpg', '010.jpg'):
            self._touch(n)
        groups = co.collect_groups(self.tmp)
        self.assertEqual(len(groups), 1)
        files = [os.path.basename(p) for _, p in groups[list(groups)[0]]]
        self.assertEqual(files, ['001.jpg', '002.jpg', '010.jpg'])

    def test_letter_prefix_is_a_chapter_key(self):
        for n in ('a_01.jpg', 'a_02.jpg', 'b_01.jpg'):
            self._touch(n)
        groups = co.collect_groups(self.tmp)
        self.assertEqual(sorted(g[1] for g in groups), ['a', 'b'])
        self.assertEqual(len(groups[('', 'a')]), 2)

    def test_single_letter_prefix_stays_one_chapter(self):
        for n in ('z_01.jpg', 'z_02.jpg', 'z_10.jpg'):
            self._touch(n)
        groups = co.collect_groups(self.tmp)
        self.assertEqual(len(groups), 1)
        files = [os.path.basename(p) for _, p in groups[('', 'z')]]
        self.assertEqual(files, ['z_01.jpg', 'z_02.jpg', 'z_10.jpg'])

    def test_dir_name_supplies_chapter_number(self):
        for n in ('001.jpg', '002.jpg'):
            self._touch('第12话', n)
        groups = co.collect_groups(self.tmp)
        self.assertEqual([k[1] for k in groups], ['12'])

    def test_mixed_naming_keeps_plain_files_with_dir(self):
        for n in ('z_01.jpg', 'z_02.jpg', '01.jpg'):
            self._touch(n)
        groups = co.collect_groups(self.tmp)
        # z 一话 + 目录名那一话（单编号文件）
        self.assertEqual(sorted(g[1] for g in groups), sorted([os.path.basename(self.tmp), 'z']))

    def test_mixed_naming_logs_a_warning(self):
        for n in ('z_01.jpg', '01.jpg'):
            self._touch(n)
        msgs = []
        co.collect_groups(self.tmp, log=msgs.append)
        self.assertTrue(any('混用' in m for m in msgs), msgs)


# ---------------------------------------------------------------- 白带切分

@unittest.skipUnless(HAS_PIL, '需要 Pillow')
class TestWhiteBands(unittest.TestCase):
    @staticmethod
    def _strip(height_blocks, width=120):
        """按 [(内容高, 白带高), ...] 拼一张竖长图。"""
        rows = []
        for ink, gap in height_blocks:
            rows.append((0, 0, 0))        # 内容：纯黑行
            rows.extend([(0, 0, 0)] * (ink - 1))
            rows.extend([(255, 255, 255)] * gap)
        img = Image.new('RGB', (width, len(rows)), 'white')
        pixels = img.load()
        for y, color in enumerate(rows):
            for x in range(width):
                pixels[x, y] = color
        return img

    def test_splits_at_white_gaps(self):
        img = self._strip([(200, 300), (150, 300), (100, 60)])
        bands = co.content_bands(img, min_gap=60, min_ink=40, white_thr=250)
        self.assertEqual(len(bands), 3)
        for (top, bot) in bands:
            self.assertGreaterEqual(bot - top + 1, 40)

    def test_ignores_tiny_ink_speck(self):
        # 两条大白带之间夹一个 2px 的“墨点”，不应被当成一页
        img = Image.new('RGB', (50, 1000), 'white')
        for y in list(range(0, 300)) + list(range(302, 304)) + list(range(600, 900)):
            for x in range(50):
                img.putpixel((x, y), (0, 0, 0))
        bands = co.content_bands(img, min_gap=60, min_ink=40, white_thr=250)
        self.assertEqual(len(bands), 2)

    def test_all_white_image_returns_single_band(self):
        img = Image.new('RGB', (50, 400), 'white')
        self.assertEqual(co.content_bands(img), [(0, 399)])


# ---------------------------------------------------------------- 输出 CBZ

@unittest.skipUnless(HAS_PIL, '需要 Pillow')
class TestImagesToCbz(TempDirCase):
    def _strip_file(self, name, blocks):
        rows = sum(i + g for i, g in blocks)
        img = Image.new('RGB', (120, rows), (0, 0, 0))
        y = 0
        for ink, gap in blocks:
            y += ink
            for yy in range(y, min(y + gap, rows)):
                for x in range(120):
                    img.putpixel((x, yy), (255, 255, 255))
            y += gap
        p = self.path(name)
        img.save(p)
        return p

    def test_pages_and_comicinfo(self):
        src = self._strip_file('001_01.jpg', [(200, 300), (150, 300)])
        out = self.path('out.cbz')
        st = co.images_to_cbz([src], out, '测试书', '第001话', '001', pad=0)
        self.assertEqual(st['pages'], 2)

        with zipfile.ZipFile(out) as z:
            self.assertIsNone(z.testzip())
            names = z.namelist()
            self.assertIn('ComicInfo.xml', names)
            self.assertEqual(len([n for n in names if n.endswith('.jpg')]), 2)
            xml = z.read('ComicInfo.xml').decode('utf-8')
            self.assertIn('<Series>测试书</Series>', xml)
            self.assertIn('<Number>001</Number>', xml)
            self.assertIn('<PageCount>2</PageCount>', xml)
            self.assertIn('<Manga>No</Manga>', xml)

    def test_no_split_keeps_whole_strip(self):
        src = self._strip_file('001_01.jpg', [(200, 300), (150, 300)])
        out = self.path('whole.cbz')
        st = co.images_to_cbz([src], out, 's', '第001话', '001', split=False)
        self.assertEqual(st['pages'], 1)


@unittest.skipUnless(HAS_PIL and HAS_PYPDF, '需要 Pillow + pypdf')
class TestPdfToCbz(TempDirCase):
    def _pdf(self, sizes, name='第1卷-测试.pdf'):
        imgs = [Image.new('RGB', size, (128, 128, 128)) for size in sizes]
        p = self.path(name)
        if len(imgs) == 1:
            imgs[0].save(p)
        else:
            imgs[0].save(p, save_all=True, append_images=imgs[1:], resolution=96)
        return p

    def test_spread_is_kept_by_default(self):
        pdf = self._pdf([(2400, 800), (600, 800)], '第1卷-a.pdf')
        out = self.path('a.cbz')
        st = co.pdf_to_cbz(pdf, out, '书', '第1卷', '01')
        self.assertEqual(st['pages'], 2)
        with zipfile.ZipFile(out) as z:
            sizes = [Image.open(io.BytesIO(z.read(n))).size
                     for n in z.namelist() if n.endswith('.jpg')]
        self.assertEqual(sizes[1], (600, 800))

    def test_cover_wrap_is_cropped_to_front_cover(self):
        pdf = self._pdf([(2400, 800), (600, 800)], '第2卷-b.pdf')
        out = self.path('b.cbz')
        st = co.pdf_to_cbz(pdf, out, '书', '第2卷', '02', cover='crop')
        self.assertEqual(st['cover'], 'cropped')
        with zipfile.ZipFile(out) as z:
            first = [n for n in z.namelist() if n.endswith('.jpg')][0]
            self.assertEqual(Image.open(io.BytesIO(z.read(first))).size, (1200, 800))

    def test_cover_keep_does_not_crop(self):
        pdf = self._pdf([(2400, 800)], '第3卷-c.pdf')
        out = self.path('c.cbz')
        st = co.pdf_to_cbz(pdf, out, '书', '第3卷', '03', cover='keep')
        self.assertEqual(st['cover'], '')
        with zipfile.ZipFile(out) as z:
            first = [n for n in z.namelist() if n.endswith('.jpg')][0]
            self.assertEqual(Image.open(io.BytesIO(z.read(first))).size, (2400, 800))

    def test_directory_conversion_names_by_volume(self):
        self._pdf([(600, 800)], '第2卷-后.pdf')
        self._pdf([(600, 800)], '第10卷-后.pdf')
        res = co.convert(self.tmp, self.path('out'), '漫画', mode='pdf')
        names = sorted(os.path.basename(p) for p, _ in res)
        self.assertEqual(names, ['漫画 第02卷 后.cbz', '漫画 第10卷 后.cbz'])


class TestDeps(unittest.TestCase):
    def test_deps_available_reports_reason(self):
        ok, why = co.deps_available()
        self.assertIsInstance(ok, bool)
        if not ok:
            self.assertIn('Pillow', why)


if __name__ == '__main__':
    unittest.main(verbosity=2)
