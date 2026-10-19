# -*- coding: utf-8 -*-
"""漫画 / 条漫 -> CBZ

epub_tool v1.3 新增。两种输入，一个出口：

  1. PDF 单行本：每页一张扫描图，双页跨页**原样保留**（阅读器可自己拆），
     首图若是封底|书脊|封面三段展开，裁出正面当第 1 页。
  2. 图片目录：
       * 拼接长图   —— 下载器把多张原图用白带（几百像素）粘成一张超长图，
                       按白带切回原始分镜，一个分镜一页；
       * 普通散图   —— 自然排序后直接打包。

分组规则：文件名下划线/连字符前的数字是话号，其后是段号。
  001_01.jpg, 001_02.jpg -> 第001话（两段按段号合并）
  005.jpg                -> 第005话

依赖（仅本模块需要，epub_tool 其余功能仍零依赖）：
  Pillow          图片解码 / 编码 / 白带检测
  pypdf           读取 PDF
  cryptography    pypdf 解密 AES 加密的 PDF 时需要

命令行：
  py -3 comic_ops.py pdf    <pdf 文件或目录> <输出目录> --series 书名
  py -3 comic_ops.py images <图片目录>      <输出目录> --series 书名
  py -3 comic_ops.py auto   <任意>          <输出目录> --series 书名
"""
from __future__ import annotations

import io
import os
import re
import time
import zipfile
from xml.sax.saxutils import escape

IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tif', '.tiff')

CHAPTER_RE = re.compile(r'^\s*(\d+|[A-Za-z]+)\s*[_\-. ]\s*(\d+)\s*$')
SINGLE_RE = re.compile(r'^\s*(\d+)\s*$')

DEP_HINT = ('缺少依赖：%s\n\n“漫画→CBZ”功能需要 Pillow / pypdf / cryptography，'
            '其余功能不受影响。安装：\n    py -3 -m pip install pillow pypdf cryptography')


# ---------------------------------------------------------------- 依赖

def _pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        raise ImportError(DEP_HINT % 'Pillow')


def _pypdf():
    try:
        import pypdf
        return pypdf
    except ImportError:
        raise ImportError(DEP_HINT % 'pypdf')


def deps_available():
    """GUI 用来判断是否启用本功能。"""
    try:
        _pil()
        _pypdf()
        return True, ''
    except ImportError as e:
        return False, str(e)


# ---------------------------------------------------------------- 命名与分组

def parse_stem(stem):
    """'001_01' -> ('001', '01')；'z_01' -> ('z', '01')；
    '005' -> ('005', None)；'cover' -> (None, None)

    注意：单段纯数字（'005'）返回的 chapter 只是“疑似话号”，
    真正决定它是不是话号的是 collect_groups 里的整目录判定。
    """
    m = CHAPTER_RE.match(stem)
    if m:
        return m.group(1), m.group(2)
    m = SINGLE_RE.match(stem)
    if m:
        return m.group(1), None
    return None, None


def natural_key(s):
    """按数字大小而非字典序排序：第2话 < 第10话。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


DIR_CHAPTER_RE = re.compile(r'第\s*(\d+)\s*[话話章卷回集部季]')


def dir_chapter(dirpath):
    """目录名 -> 话号：'第12话' -> '12'；'12' -> '12'；其他 -> 目录名本身"""
    name = os.path.basename(os.path.normpath(dirpath)) or '0'
    m = DIR_CHAPTER_RE.search(name)
    if m:
        return m.group(1)
    m = re.fullmatch(r'\s*(\d+)\s*', name)
    if m:
        return m.group(1)
    return name


VOL_RE = re.compile(r'第\s*(\d+)\s*卷[\s\-—_]*(.*)')


def parse_pdf_name(path):
    """'第5卷-NO TIME.pdf' -> ('5', 'NO TIME')；'vol05.pdf' -> ('05', 'vol05')"""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = VOL_RE.search(stem)
    if m:
        return m.group(1), (m.group(2).strip(' -—_') or '第%s卷' % m.group(1))
    m = re.search(r'(\d+)', stem)
    return (m.group(1) if m else '00'), stem


def collect_groups(root, log=None):
    """扫描图片目录 -> {(相对目录, 话号): [(排序键, 路径), ...]}

    分组规则（按目录判定）：
      1. 目录里有 `X_Y`（X 是数字或字母）-> 按 X 分话，Y 只决定页序
         `001_01/001_02/002_01` -> 第001话(2页) + 第002话(1页)
         `a_01/a_02/b_01`       -> a 一话 + b 一话
      2. 目录里只有单段纯数字 `001.jpg/002.jpg` -> **整个目录一话**，按自然序当页序
         （它们是页号，不是话号；早先按话号处理会产出一堆单页 CBZ）
      3. 目录里没有编号 -> 整个目录一话，话号取目录名（'第12话' -> 12）
    """
    groups = {}
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        files.sort()
        rel = os.path.relpath(dirpath, root)
        rel = '' if rel == '.' else rel
        images = [f for f in files if f.lower().endswith(IMAGE_EXT)]
        if not images:
            continue

        parsed = [(f,) + parse_stem(os.path.splitext(f)[0]) for f in images]
        with_seg = [p for p in parsed if p[2] is not None]
        plain = [p for p in parsed if p[2] is None]

        if log and with_seg and plain:
            log('  注意：%s 里混用了「话_页」和单编号命名，单编号文件归到目录那一话'
                % (rel or '.'))

        if with_seg:
            for fn, chapter, seg in with_seg:
                key = (rel, chapter)
                groups.setdefault(key, []).append(
                    ((0, int(seg), natural_key(fn)), os.path.join(dirpath, fn)))
            leftovers = plain
        else:
            leftovers = parsed

        if leftovers:
            key = (rel, dir_chapter(dirpath))
            for fn, _c, _s in leftovers:
                groups.setdefault(key, []).append(
                    ((1, natural_key(fn)), os.path.join(dirpath, fn)))

    for key in groups:
        groups[key].sort(key=lambda t: t[0])
    return groups


# ---------------------------------------------------------------- 白带检测

def _row_is_white(im, thr):
    """每行是否「整行都够白」。

    只用 Pillow：先把像素二值化，再把宽度压成 1 像素取 BOX 均值 ——
    一行里只要有一个暗点，均值就 < 255。比逐像素扫 Python 快两个数量级。
    """
    Image = _pil()
    g = im.convert('L')
    binary = g.point(lambda v: 255 if v >= thr else 0)
    box = getattr(Image, 'Resampling', Image).BOX
    col = binary.convert('F').resize((1, g.height), box)
    data = (col.get_flattened_data() if hasattr(col, 'get_flattened_data')
            else col.getdata())
    return [v >= 255.0 for v in data]


def content_bands(im, white_thr=250, min_gap=60, min_ink=40):
    """按白带切出内容块，返回 [(top, bottom)]（含端点）。"""
    white = _row_is_white(im, white_thr)
    bands = []
    i, h = 0, len(white)
    while i < h:
        j = i
        while j < h and white[j] == white[i]:
            j += 1
        if not white[i] and (j - i) >= min_ink:
            if bands and i - bands[-1][1] - 1 < min_gap:
                bands[-1][1] = j - 1
            else:
                bands.append([i, j - 1])
        i = j
    if not bands:
        return [(0, im.height - 1)]
    return [(a, b) for a, b in bands]


# ---------------------------------------------------------------- 图片编码

def jpeg_bytes(im, quality=92, max_width=0):
    Image = _pil()
    if max_width and im.width > max_width:
        r = max_width / float(im.width)
        lanczos = getattr(Image, 'Resampling', Image).LANCZOS
        im = im.resize((max_width, max(1, int(im.height * r))), lanczos)
    buf = io.BytesIO()
    im.convert('RGB').save(buf, format='JPEG', quality=quality,
                           optimize=True, progressive=True, subsampling=2)
    return buf.getvalue()


def comic_info_xml(series, number, title, page_count, manga='No', year=''):
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema">\n'
            '  <Title>%s</Title>\n'
            '  <Series>%s</Series>\n'
            '  <Number>%s</Number>\n'
            '  <Volume>%s</Volume>\n'
            '  <PageCount>%d</PageCount>\n'
            '  <Manga>%s</Manga>\n'
            '  <LanguageISO>zh</LanguageISO>\n'
            '  <Year>%s</Year>\n'
            '</ComicInfo>\n') % (escape(title), escape(series), escape(number),
                                 escape(number), page_count, manga, escape(str(year)))


class CbzWriter:
    """边写边压缩，避免整本 CBZ 都压在内存里。"""

    def __init__(self, path):
        self.path = path
        self.zf = zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6)
        self.pages = 0

    def add(self, im, quality=92, max_width=0):
        self.pages += 1
        self.zf.writestr('%04d.jpg' % self.pages, jpeg_bytes(im, quality, max_width))

    def add_raw_jpeg(self, data):
        self.pages += 1
        self.zf.writestr('%04d.jpg' % self.pages, data)

    def close(self, series, number, title, manga='No', year=''):
        self.zf.writestr('ComicInfo.xml',
                         comic_info_xml(series, number, title, self.pages, manga, year))
        self.zf.close()
        return self.pages


# ---------------------------------------------------------------- PDF 输入

JPEG_MAGIC = b'\xff\xd8\xff'


def pdf_page_image(page):
    """-> (PIL.Image 或 None, 原始字节 或 None, filter 名)"""
    try:
        imgs = page.images
    except Exception:
        return None, None, ''
    if not imgs:
        return None, None, ''
    img = imgs[0]
    raw = pil = None
    try:
        raw = img.data
    except Exception:
        pass
    try:
        pil = img.image
    except Exception:
        pass
    filt = ''
    try:
        xo = page['/Resources']['/XObject']
        for name in xo:
            obj = xo[name].get_object()
            if obj.get('/Subtype') == '/Image':
                filt = str(obj.get('/Filter'))
                break
    except Exception:
        pass
    return pil, raw, filt


def pdf_to_cbz(pdf_path, out_path, series, title, number,
               quality=92, cover='crop', cover_threshold=1.9,
               split=False, split_threshold=1.15, max_width=0,
               log=None, year=''):
    """单行本 PDF -> CBZ。跨页默认保留（阅读器可自行拆分）。"""
    log = log or (lambda m: None)
    pypdf = _pypdf()
    _pil()
    t0 = time.time()

    reader = pypdf.PdfReader(pdf_path)
    if reader.is_encrypted:
        reader.decrypt('')

    w = CbzWriter(out_path)
    stats = {'pages': 0, 'spreads': 0, 'passthrough': 0, 'reencoded': 0,
             'cover': '', 'failed': []}
    try:
        for idx, page in enumerate(reader.pages):
            try:
                pil, raw, _filt = pdf_page_image(page)
            except Exception as e:
                stats['failed'].append(('第 %d 页' % (idx + 1), str(e)))
                log('    ! 第 %d 页读取失败，已跳过（%s）' % (idx + 1, e))
                continue
            if pil is None and raw is None:
                stats['failed'].append(('第 %d 页' % (idx + 1), '没有内嵌图片'))
                log('    ! 第 %d 页没有内嵌图片，已跳过' % (idx + 1))
                continue
            width, height = pil.size if pil is not None else (0, 0)
            ratio = (width / float(height)) if height else 0
            wide = ratio >= split_threshold
            parts = []

            if idx == 0 and cover == 'crop' and ratio >= cover_threshold and pil is not None:
                mid = width // 2
                parts = [(pil.crop((0, 0, mid, height)), None)]
                stats['cover'] = 'cropped'
                stats['spreads'] += 1
            elif split and wide and pil is not None:
                mid = width // 2
                left = pil.crop((0, 0, mid, height))
                right = pil.crop((mid, 0, width, height))
                parts = [(right, None), (left, None)]      # 日漫右起
                stats['spreads'] += 1
            else:
                parts = [(pil, raw)]
                if wide:
                    stats['spreads'] += 1

            for part_pil, part_raw in parts:
                try:
                    resized = (max_width and part_pil is not None
                               and part_pil.width > max_width)
                    if part_raw is not None and part_raw[:3] == JPEG_MAGIC and not resized:
                        w.add_raw_jpeg(part_raw)
                        stats['passthrough'] += 1
                    elif part_pil is not None:
                        w.add(part_pil, quality, max_width)
                        stats['reencoded'] += 1
                except Exception as e:
                    stats['failed'].append(('第 %d 页' % (idx + 1), str(e)))
                    log('    ! 第 %d 页解码/编码失败，已跳过（%s）' % (idx + 1, e))
        stats['pages'] = w.close(series, number, title, manga='YesAndRightToLeft', year=year)
    except Exception:
        w.zf.close()
        raise

    if stats['pages'] == 0:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise ValueError('这个 PDF 里没有一页能读出来')

    stats['mb'] = round(os.path.getsize(out_path) / 1024.0 / 1024.0, 2)
    stats['seconds'] = round(time.time() - t0, 1)
    log('  %s：%d 页（跨页 %d，原样透传 %d，重编码 %d），%.1f MB，%.0fs%s'
        % (os.path.basename(out_path), stats['pages'], stats['spreads'],
           stats['passthrough'], stats['reencoded'], stats['mb'], stats['seconds'],
           ('，%d 页被跳过' % len(stats['failed'])) if stats['failed'] else ''))
    return stats


# ---------------------------------------------------------------- 图片输入

def _open_image(path):
    """打开图片并强制解码。返回 (image, note)。

    Pillow 默认是惰性解码，坏数据要等到 load()/convert() 才炸；
    先正常解码，失败再开 LOAD_TRUNCATED_IMAGES 容错重试一次
    （截断的 JPEG 通常能解出前面部分，缺失区域是灰/黑）。
    """
    Image = _pil()
    from PIL import ImageFile
    try:
        im = Image.open(path)
        im.load()
        return im, None
    except Exception as first:
        old = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            im = Image.open(path)
            im.load()
            return im, '文件可能已截断，已容错读取（缺失部分会显示为灰/黑）'
        except Exception:
            raise first
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = old


def images_to_cbz(files, out_path, series, title, number,
                  quality=92, split=True, white_thr=250, min_gap=60,
                  min_ink=40, pad=8, max_width=0, log=None, year=''):
    """一组图片（可能是拼接长图）-> CBZ。split=True 时按白带切回分镜。

    单个文件读不出来不会中断整话：跳过、点名、记进 failed。
    """
    log = log or (lambda m: None)
    w = CbzWriter(out_path)
    bands_total = 0
    failed = []
    try:
        for path in files:
            try:
                im, note = _open_image(path)
                if note:
                    log('    ~ %s：%s' % (os.path.basename(path), note))
                im = im.convert('RGB')
            except Exception as e:
                failed.append((path, str(e)))
                log('    ! 读取失败，已跳过：%s（%s）' % (os.path.basename(path), e))
                continue

            if not split:
                w.add(im, quality, max_width)
                bands_total += 1
                continue
            try:
                bands = content_bands(im, white_thr, min_gap, min_ink)
            except Exception as e:
                failed.append((path, '切分失败：%s' % e))
                log('    ! 切分失败，已跳过：%s（%s）' % (os.path.basename(path), e))
                continue
            for (top, bot) in bands:
                t = max(0, top - pad)
                b = min(im.height - 1, bot + pad)
                w.add(im.crop((0, t, im.width, b + 1)), quality, max_width)
                bands_total += 1
        pages = w.close(series, number, title, manga='No', year=year)
    except Exception:
        w.zf.close()
        raise

    if pages == 0:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise ValueError('本话所有图片都读取失败')

    mb = round(os.path.getsize(out_path) / 1024.0 / 1024.0, 2)
    log('  %s：%d 源文件 -> %d 页，%.1f MB%s'
        % (os.path.basename(out_path), len(files), pages, mb,
           ('，%d 个文件被跳过' % len(failed)) if failed else ''))
    return {'pages': pages, 'sources': len(files), 'bands': bands_total,
            'mb': mb, 'failed': failed}


# ---------------------------------------------------------------- 统一入口

def default_series(src):
    """输入路径 -> 默认系列名：目录名，或 PDF 文件名（不带扩展名）。"""
    if os.path.isdir(src):
        base = os.path.basename(os.path.normpath(src))
    else:
        base = os.path.splitext(os.path.basename(src))[0]
    return base or 'comic'


def default_output(src):
    """输入路径 -> 默认输出目录：与输入同级（目录则在其下）的 _cbz。"""
    base = src if os.path.isdir(src) else os.path.dirname(src)
    return os.path.join(base or '.', '_cbz')


class DefaultTracker:
    """界面默认值的跟随规则。

    用户没动过（空，或仍等于上次自动填的值）就跟随新的输入；
    用户手动改过就保留，不再被后续选择覆盖。
    """

    def __init__(self):
        self._auto = {}

    def apply(self, key, current, new_value):
        prev = self._auto.get(key)
        if not current or current == prev:
            self._auto[key] = new_value
            return new_value
        return current


def convert(src, outdir, series='', mode='auto', log=None, progress=None,
            quality=92, pdf_kw=None, img_kw=None):
    """把 src（PDF 文件 / 包含 PDF 或图片的目录）转换成若干 CBZ。

    mode: 'auto' | 'pdf' | 'images'
    pdf_kw / img_kw: 分别传给 pdf_to_cbz / images_to_cbz 的参数。
        两者都有 split，但含义不同（PDF 是拆双页，图片是按白带切分），
        所以必须分开传，不能共用一个 kwargs。
    返回 [(输出文件, 统计), ...]
    """
    log = log or (lambda m: None)
    src = os.path.abspath(src)
    if not os.path.exists(src):
        raise FileNotFoundError(src)
    if mode == 'auto':
        mode = 'pdf' if (os.path.isfile(src) and src.lower().endswith('.pdf')) else 'images'

    if not series:
        series = (os.path.splitext(os.path.basename(src))[0] if os.path.isfile(src)
                  else os.path.basename(os.path.normpath(src))) or 'comic'
    os.makedirs(outdir, exist_ok=True)
    results = []

    if mode == 'pdf':
        files = ([src] if os.path.isfile(src) else
                 sorted((os.path.join(src, f) for f in os.listdir(src)
                         if f.lower().endswith('.pdf')), key=natural_key))
        if not files:
            raise ValueError('目录里没有 PDF')
        kw = dict(pdf_kw or {})
        problems = []
        for i, path in enumerate(files, 1):
            number, title = parse_pdf_name(path)
            number = number.zfill(2) if number.isdigit() else number
            name = re.sub(r'\s+', ' ', '%s 第%s卷 %s.cbz' % (series, number, title)).strip()
            out = os.path.join(outdir, name)
            log('[%d/%d] %s' % (i, len(files), os.path.basename(path)))
            try:
                st = pdf_to_cbz(path, out, series, title, number,
                                quality=quality, log=log, **kw)
            except Exception as e:
                problems.append((os.path.basename(path), str(e)))
                log('  !! 转换失败，已跳过：%s（%s）' % (os.path.basename(path), e))
                continue
            results.append((out, st))
            for name_, why in st.get('failed', []):
                problems.append(('%s %s' % (os.path.basename(path), name_), why))
            if progress:
                progress(i, len(files), out)
        _report(log, problems, len(results))
        return results

    kw = dict(img_kw or {})
    groups = collect_groups(src, log=log)
    if not groups:
        raise ValueError('没有找到图片（jpg / png / webp …）')
    keys = sorted(groups, key=lambda k: (natural_key(k[0]), natural_key(k[1])))
    log('共 %d 话，%d 个源文件' % (len(keys), sum(len(v) for v in groups.values())))
    problems = []
    for i, key in enumerate(keys, 1):
        rel, chapter = key
        files = [p for _, p in groups[key]]
        number = chapter.zfill(3) if chapter.isdigit() else chapter
        title = ('第%s话' % number) if chapter.isdigit() else chapter
        name = '%s %s.cbz' % (series, title)
        if rel:
            name = '%s %s [%s].cbz' % (series, title, rel.replace(os.sep, '_'))
        out = os.path.join(outdir, name)
        log('[%d/%d] %s <- %d 个源文件' % (i, len(keys), title, len(files)))
        try:
            st = images_to_cbz(files, out, series, number, title,
                               quality=quality, log=log, **kw)
        except Exception as e:
            problems.append((title, str(e)))
            log('  !! %s 转换失败，已跳过（%s）' % (title, e))
            continue
        results.append((out, st))
        for path, why in st.get('failed', []):
            try:
                shown = os.path.relpath(path, src)
            except ValueError:
                shown = path
            problems.append((shown, why))
        if progress:
            progress(i, len(keys), out)
    _report(log, problems, len(results))
    return results


def _report(log, problems, ok_count):
    """收尾汇总：哪些文件/页没读出来，一眼能看到。"""
    if not problems:
        log('全部完成：%d 个 CBZ' % ok_count)
        return
    log('完成 %d 个 CBZ，但有 %d 处问题：' % (ok_count, len(problems)))
    for name, why in problems:
        log('  - %s：%s' % (name, why))


# ---------------------------------------------------------------- 命令行

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='漫画 / 条漫 -> CBZ')
    ap.add_argument('mode', choices=['pdf', 'images', 'auto'])
    ap.add_argument('input')
    ap.add_argument('outdir')
    ap.add_argument('--series', default='')
    ap.add_argument('--quality', type=int, default=92)
    ap.add_argument('--max-width', type=int, default=0)
    ap.add_argument('--no-split', action='store_true',
                    help='图片输入时不按白带切分，整张作一页')
    ap.add_argument('--pdf-split', action='store_true',
                    help='PDF 输入时把双页跨页拆成两页（默认保留）')
    ap.add_argument('--white-thr', type=int, default=250)
    ap.add_argument('--min-gap', type=int, default=60)
    ap.add_argument('--min-ink', type=int, default=40)
    ap.add_argument('--pad', type=int, default=8)
    args = ap.parse_args(argv)

    pdf_kw = {'split': args.pdf_split, 'cover_threshold': 1.9,
              'max_width': args.max_width}
    img_kw = {'split': not args.no_split, 'white_thr': args.white_thr,
              'min_gap': args.min_gap, 'min_ink': args.min_ink,
              'pad': args.pad, 'max_width': args.max_width}

    t0 = time.time()
    res = convert(args.input, args.outdir, args.series, mode=args.mode,
                  log=print, quality=args.quality, pdf_kw=pdf_kw, img_kw=img_kw)
    total = sum(st.get('mb', 0) for _o, st in res)
    print('完成 %d 个文件，共 %.1f MB，%.0fs' % (len(res), total, time.time() - t0))


if __name__ == '__main__':
    main()
