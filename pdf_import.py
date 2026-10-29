"""Convert a PDF into chapters of simple HTML, ready to become an EPUB draft.

Text comes from the PDF's own text layer, or from OCR (MuPDF's built-in
Tesseract) for scanned pages. Layout heuristics then rebuild paragraphs,
drop running headers/footers and split the book into chapters.
"""
import bisect
import os
import re
import shutil
import statistics
import threading
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from html import escape

import pymupdf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TESSDATA_DIR = os.path.join(BASE_DIR, 'tessdata')
TESSDATA_URL = 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{}.traineddata'
WINDOWS_TESSDATA = r'C:\Program Files\Tesseract-OCR\tessdata'

TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
OCR_DPI = 300
OCR_MAX_SIDE = 4000        # px, keeps memory sane for oversized pages
COVER_WIDTH = 480          # Xteink X4 screen width
CHAPTER_MAX_CHARS = 80000  # longer chapters are split into parts
PAGES_PER_SECTION = 15     # used when the book has no recognisable chapters
FRONT_MATTER_TITLE = 'Halaman Awal'

_BOLD_HINTS = ('bold', 'black', 'heavy', 'semibold', 'demi')
_CHAR_MAP = {ord(c): None for c in '\u200b\u200c\u200d\u2060\ufeff\ufffd'}
_CHAR_MAP.update({ord(c): ' ' for c in '\t\u00a0\u2002\u2003\u2009\u202f'})
# a soft hyphen that made it into a PDF is a drawn hyphen (older tools map "-" to it)
_CHAR_MAP.update({0xf001: 'fi', 0xf002: 'fl', 0xad: '-'})
_PUA_RE = re.compile('[\ue000-\uf8ff]')
_TERMINAL_RE = re.compile(r'[.!?…:;]["\'”’»)\]*/\s]*$')  # may trail quotes or note marks: ***)
_SENTENCE_END_RE = re.compile(r'[.,;]["\'”’»)\]*/\s]*$')
_LIST_RE = re.compile(r'^(?:[•●▪■◦○►✓➢❖\-–—*]\s|\(?\d{1,3}[.)]\s|\(?[a-zA-Z][.)]\s)')
_SEPARATOR_RE = re.compile(r'^(?:[*~•·#=_◆◇♦❖✦★☆✽✻❋\-–—\s]{3,}|[-–—~\s]*(?:o0o|0o0|oOo|OoO|ooo)[-–—~\s]*)$')
_LEADER_RE = re.compile(r'(?:\.\s*){4,}|…{2,}|_{4,}')
_PAGE_NUM_RE = re.compile(
    r'^[\W_]*(?:(?:hal(?:aman)?|page|p)\.?\s*)?(?:\d{1,4}|[ivxlcdm]{1,7})'
    r'(?:\s*(?:/|dari|of)\s*\d{1,4})?[\W_]*$', re.I)
_NUMBER_WORDS = ('satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|pertama|'
                 'ke(?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh)|'
                 'one|two|three|four|five|six|seven|eight|nine|ten')
_CHAPTER_RE = re.compile(  # "bab" may lose its space in OCR: BAB1, BABI; inserted chapters: BAB VIIA
    r'^(?:bab\s*|(?:bagian|chapter|part|jilid)\s+)(?:(?:\d{1,3}|(?-i:[IVXLCDM]{1,7}))(?-i:[A-Z])?|'
    + _NUMBER_WORDS + r')\b', re.I)
_SECTION_WORDS = {
    'prolog', 'prologue', 'epilog', 'epilogue', 'kata pengantar', 'prakata', 'pengantar',
    'pendahuluan', 'daftar isi', 'daftar pustaka', 'penutup', 'tentang penulis', 'biodata penulis',
    'profil penulis', 'ucapan terima kasih', 'persembahan', 'glosarium', 'lampiran', 'sinopsis',
    'contents', 'introduction', 'preface', 'foreword', 'acknowledgements', 'bibliography',
    'about the author',
}
_JUNK_AUTHORS = {
    'user', 'admin', 'administrator', 'owner', 'windows user', 'hp', 'asus', 'acer', 'lenovo', 'dell',
    'toshiba', 'pc', 'microsoft', 'unknown', 'author', 'compaq', 'samsung', 'axioo', 'pengguna',
}
_ID_WORDS = {'yang', 'dan', 'di', 'ini', 'itu', 'dengan', 'untuk', 'dari', 'dalam', 'tidak', 'akan', 'pada'}
_EN_WORDS = {'the', 'and', 'of', 'to', 'is', 'in', 'that', 'it', 'was', 'for', 'with', 'on'}


class ConversionError(Exception):
    """Conversion failed; the message is meant for the user."""


@dataclass(eq=False)
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    segs: list  # [(text, bold, italic), ...]
    ocr: bool = False
    col: int = 0

    @property
    def text(self):
        return ''.join(s[0] for s in self.segs)

    @property
    def bold(self):
        total = sum(len(t) for t, _, _ in self.segs)
        return total > 0 and sum(len(t) for t, b, _ in self.segs if b) >= 0.8 * total


@dataclass
class Geom:
    left: float
    right: float
    lead: float
    size: float
    justified: bool = False
    bottom: float = 0  # where body text usually ends on a full page


@dataclass
class Para:
    lines: list
    joins: list = field(default_factory=lambda: [None])  # how each line joins the previous one
    kind: str = 'p'

    @property
    def text(self):
        return ' '.join(l.text.strip() for l in self.lines)


@dataclass
class Book:
    title: str
    author: str
    language: str
    chapters: list  # [(title, html), ...]
    cover: bytes = None
    pages: int = 0
    ocr_pages: int = 0
    empty_pages: int = 0


# ---------- OCR language data ----------

_tessdata_lock = threading.Lock()


def ensure_tessdata(lang, report=None):
    """Return a tessdata folder holding every language in `lang` (e.g. 'ind+eng'),
    downloading missing ones from the official tessdata_fast repository."""
    codes = lang.split('+')
    if not all(re.fullmatch(r'[a-z_]{3,10}', c) for c in codes):
        raise ConversionError(f'Kode bahasa OCR tidak valid: {lang}')
    for folder in (os.environ.get('TESSDATA_PREFIX'), TESSDATA_DIR, WINDOWS_TESSDATA):
        if folder and all(os.path.isfile(os.path.join(folder, f'{c}.traineddata')) for c in codes):
            return folder
    with _tessdata_lock:
        os.makedirs(TESSDATA_DIR, exist_ok=True)
        for code in codes:
            dest = os.path.join(TESSDATA_DIR, f'{code}.traineddata')
            if os.path.isfile(dest):
                continue
            if report:
                report(None, f'Mengunduh data OCR bahasa "{code}" (hanya sekali)...')
            tmp = dest + '.part'
            try:
                with urllib.request.urlopen(TESSDATA_URL.format(code), timeout=60) as resp, \
                        open(tmp, 'wb') as f:
                    shutil.copyfileobj(resp, f)
                os.replace(tmp, dest)
            except OSError as e:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise ConversionError(
                    f'Gagal mengunduh data OCR bahasa "{code}". Periksa koneksi internet, atau unduh '
                    f'{code}.traineddata secara manual ke folder {TESSDATA_DIR}.') from e
    return TESSDATA_DIR


# ---------- Text extraction ----------

def _clean_text(text):
    text = text.translate(_CHAR_MAP)
    if _PUA_RE.search(text):
        # Symbol/Wingdings bullets live in the private use area
        stripped = text.lstrip()
        if stripped and _PUA_RE.match(stripped[0]):
            text = text[:len(text) - len(stripped)] + '•' + stripped[1:]
        text = _PUA_RE.sub('', text)
    return text


def _raw_lines(page_dict, scale=1.0):
    """Lines as [x0, y0, x1, y1, spans]; a span is [text, x0, x1, size, bold, italic]."""
    out = []
    for block in page_dict['blocks']:
        if block['type'] != 0:
            continue
        for line in block['lines']:
            if abs(line['dir'][1]) > 0.2:  # rotated or vertical text, e.g. margin watermarks
                continue
            spans = []
            for s in line['spans']:
                if not s['text']:
                    continue
                font = s['font'].lower()
                bold = bool(s['flags'] & pymupdf.TEXT_FONT_BOLD) or any(h in font for h in _BOLD_HINTS)
                italic = bool(s['flags'] & pymupdf.TEXT_FONT_ITALIC) or 'italic' in font or 'oblique' in font
                spans.append([s['text'], s['bbox'][0] * scale, s['bbox'][2] * scale, s['size'] * scale,
                              bold, italic])
            if spans:
                x0, y0, x1, y1 = line['bbox']
                out.append([x0 * scale, y0 * scale, x1 * scale, y1 * scale, spans])
    return out


def _merge_fragments(raws):
    """Join consecutive pieces of one visual line (MuPDF splits widely spaced justified lines)."""
    out = []
    for r in raws:
        if out:
            p = out[-1]
            height = min(p[3] - p[1], r[3] - r[1])
            if abs(p[1] + p[3] - r[1] - r[3]) / 2 < 0.35 * height and r[0] >= p[2] - 1:
                p[1], p[2], p[3] = min(p[1], r[1]), r[2], max(p[3], r[3])
                p[4].extend(r[4])
                continue
        out.append(r)
    return out


def _weighted_median(pairs):
    pairs = sorted(pairs)
    half = sum(w for _, w in pairs) / 2
    acc = 0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return 0


def _pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def _to_line(raw, pno, ocr):
    x0, y0, x1, y1, spans = raw
    segs, prev = [], None
    for text, sx0, sx1, size, bold, italic in spans:
        text = _clean_text(text)
        if not text:
            continue
        if prev and not prev[0][-1].isspace() and not text[0].isspace() and sx0 - prev[1] > 0.15 * size:
            text = ' ' + text
        segs.append((text, bold, italic))
        prev = (text, sx1)
    if segs:
        segs[0] = (segs[0][0].lstrip(),) + segs[0][1:]
        segs[-1] = (segs[-1][0].rstrip(),) + segs[-1][1:]
        segs = [s for s in segs if s[0]]
    if not segs:
        return None
    size = _weighted_median([(s[3], max(1, len(s[0].strip()))) for s in spans])
    return Line(pno, x0, y0, x1, y1, size, segs, ocr)


def _filter_ocr(raws, height):
    """Drop OCR noise: specks, junk in the margins and stray fragments."""
    words = [s for r in raws for s in r[4] if sum(c.isalpha() for c in s[0]) >= 4]
    if len(words) < 8:
        return [r for r in raws if sum(c.isalnum() for s in r[4] for c in s[0]) >= 4]
    body = statistics.median(s[3] for s in words)
    left, right = _pct([s[1] for s in words], 0.03), _pct([s[2] for s in words], 0.97)
    out = []
    for x0, y0, x1, y1, spans in raws:
        if _SEPARATOR_RE.match(' '.join(s[0] for s in spans).strip()):
            if abs((x0 + x1) / 2 - (left + right) / 2) < 2 * body:
                out.append([x0, y0, x1, y1, spans])
            continue
        kept = [s for s in spans
                if any(c.isalnum() for c in s[0]) and s[3] >= 0.45 * body
                and s[2] >= left - 0.25 * body and s[1] <= right + 0.25 * body]
        clean = []
        for i, s in enumerate(kept):
            if len(kept) > 1 and sum(c.isalnum() for c in s[0]) <= 3:
                gap_left = s[1] - kept[i - 1][2] if i else float('inf')
                gap_right = kept[i + 1][1] - s[2] if i < len(kept) - 1 else float('inf')
                if min(gap_left, gap_right) > 2 * body:
                    continue
            clean.append(s)
        if not clean:
            continue
        lx0, lx1 = clean[0][1], clean[-1][2]
        text = ''.join(s[0] for s in clean)
        alnum = sum(c.isalnum() for c in text)
        centered = abs((lx0 + lx1) / 2 - (left + right) / 2) < 2 * body
        if (alnum <= 5 and centered and max(s[3] for s in clean) < 0.75 * body and 0.1 * height < y0
                and y1 < 0.9 * height and not any(c.isdigit() for c in text) and not _PAGE_NUM_RE.match(text)):
            # a small centered blot inside the text area: "* * *", which Tesseract reads as "Kok"
            out.append([lx0, y0, lx1, y1, [['* * *', lx0, lx1, body] + clean[0][4:]]])
            continue
        if alnum <= 3 and not centered and abs(lx0 - left) > body:
            continue
        out.append([lx0, y0, lx1, y1, clean])
    return out


def _text_lines(page):
    raws = _merge_fragments(_raw_lines(page.get_text('dict', flags=TEXT_FLAGS)))
    raw_text = ''.join(s[0] for r in raws for s in r[4])
    lines = [_to_line(r, page.number, False) for r in raws]
    return [l for l in lines if l], raw_text


def _ocr_lines(page, lang, tessdata):
    dpi = int(min(OCR_DPI, OCR_MAX_SIDE * 72 / max(page.rect.width, page.rect.height)))
    pix = page.get_pixmap(dpi=dpi)  # must stay RGB: MuPDF's OCR returns nothing for grayscale
    ocr_pdf = pix.pdfocr_tobytes(compress=False, language=lang, tessdata=tessdata)
    with pymupdf.open(stream=ocr_pdf, filetype='pdf') as ocr:
        scale = page.rect.width / ocr[0].rect.width
        raws = _raw_lines(ocr[0].get_text('dict', flags=TEXT_FLAGS), scale)
    for r in raws:  # one span per word; spaces are rebuilt from the gaps
        r[4] = [s for s in r[4] if s[0].strip()]
    raws = _filter_ocr(_merge_fragments([r for r in raws if r[4]]), page.rect.height)
    lines = [_to_line(r, page.number, True) for r in raws]
    return [l for l in lines if l]


def _garbled(text):
    chars = [c for c in text if not c.isspace()]
    if len(chars) < 20:
        return False
    bad = sum(1 for c in chars if c == '\ufffd' or '\ue000' <= c <= '\uf8ff' or ord(c) < 32)
    if bad > 0.1 * len(chars):
        return True
    letters = [c for c in text.lower() if 'a' <= c <= 'z']
    if len(letters) >= 200:
        vowels = sum(c in 'aeiou' for c in letters) / len(letters)
        return not 0.2 <= vowels <= 0.6
    return False


def _needs_ocr(page, raw_text):
    if _garbled(raw_text):
        return True
    alnum = sum(c.isalnum() for c in raw_text)
    if alnum >= 200:
        return False
    area = abs(page.rect)
    covered = sum(abs(pymupdf.Rect(info['bbox']) & page.rect) for info in page.get_image_info())
    # a scan may carry a little digital text, e.g. a "downloaded from" stamp
    return area > 0 and covered > (0.3 if alnum < 50 else 0.6) * area


def _try_ocr(doc, pno, lang, tessdata):
    try:
        return _ocr_lines(doc[pno], lang, tessdata)
    except Exception:
        return None  # an unreadable page should not sink the whole book


_worker = {}  # per worker process


def _ocr_worker_init(path, lang, tessdata):
    _worker.update(doc=pymupdf.open(path), lang=lang, tessdata=tessdata)


def _ocr_worker(pno):
    return pno, _try_ocr(_worker['doc'], pno, _worker['lang'], _worker['tessdata'])


def _ocr_pages(path, pnos, lang, tessdata, report):
    """OCR the given pages, in parallel worker processes when possible."""
    results = {}

    def done(pno, lines):
        results[pno] = lines
        report(len(results) / len(pnos), f'OCR halaman {len(results)}/{len(pnos)}...')

    workers = min(len(pnos), max(1, (os.cpu_count() or 2) - 1), 8)
    if workers > 1:
        try:
            with ProcessPoolExecutor(workers, initializer=_ocr_worker_init,
                                     initargs=(path, lang, tessdata)) as pool:
                for pno, lines in pool.map(_ocr_worker, pnos):
                    done(pno, lines)
        except Exception:
            pass  # no worker processes available: OCR whatever is left in this process
    if len(results) < len(pnos):
        with pymupdf.open(path) as doc:
            for pno in pnos:
                if pno not in results:
                    done(pno, _try_ocr(doc, pno, lang, tessdata))
    return results


# ---------- Layout analysis ----------

def _line_key(text):
    text = re.sub(r'\d+', '0', text.lower())
    return re.sub(r'[\W_]+', ' ', text).strip()


def _strip_running_lines(pages, heights, body):
    """Remove running headers/footers, page numbers and watermarks."""
    used = sum(1 for p in pages if p)
    edges = []                     # (line, gap to the body) at the top/bottom edge of a page
    edge_pages = defaultdict(set)  # text key -> pages where it sits at the edge
    anywhere = defaultdict(list)   # text key -> lines with that text anywhere
    for pno, lines in enumerate(pages):
        ordered = sorted(lines, key=lambda l: l.y0)
        n = len(ordered)
        for i, l in enumerate(ordered):
            if i < 2 and l.y1 < 0.15 * heights[pno]:
                edges.append((l, ordered[i + 1].y0 - l.y1 if i + 1 < n else body))
            elif i >= n - 2 and l.y0 > 0.85 * heights[pno]:
                edges.append((l, l.y0 - ordered[i - 1].y1 if i else body))
        for l in lines:
            anywhere[_line_key(l.text)].append(l)
    for l, _ in edges:
        edge_pages[_line_key(l.text)].add(l.page)
    edge_ys = sorted(l.y0 for l, _ in edges)
    min_repeat = 3 if used >= 6 else 2
    drop = set()
    for l, gap in edges:
        text, key = l.text.strip(), _line_key(l.text)
        same_spot = bisect.bisect_right(edge_ys, l.y0 + 3) - bisect.bisect_left(edge_ys, l.y0 - 3)
        letters = [c for c in text if c.isalpha()]
        styled = ((l.size < 0.92 * body and (not l.ocr or len(text) >= 10)) or all(s[2] for s in l.segs)
                  or (len(letters) >= 3 and all(c.isupper() for c in letters)))
        if _PAGE_NUM_RE.match(text) or (key and len(edge_pages[key]) >= min_repeat and l.size < 1.2 * body):
            drop.add(l)
        elif (styled and gap > 0.8 * body and not l.bold and l.size <= 1.05 * body
              and same_spot >= max(3, 0.4 * used)):
            # running head whose text changes, e.g. the chapter title: set apart from the body,
            # styled unlike it, at the same spot on many pages
            drop.add(l)
    for key, lines in anywhere.items():
        # watermarks repeat at the same spot; repeated sentences elsewhere are real content
        if len(key) > 3 and len({l.page for l in lines}) >= max(4, 0.5 * used):
            spot, count = Counter(round(l.y0 / 3) for l in lines).most_common(1)[0]
            if count >= 0.5 * len({l.page for l in lines}):
                drop.update(l for l in lines if abs(round(l.y0 / 3) - spot) <= 1
                            and len(l.text) < 100 and l.size < 1.2 * body)
    for pno, lines in enumerate(pages):
        pages[pno] = [l for l in lines if l not in drop]


def _assign_columns(lines):
    """Detect a two-column page and set line.col to 0/1 (-1 for lines spanning both)."""
    body = [l for l in lines if len(l.text) >= 10]
    if len(body) < 12:
        return
    size = statistics.median(l.size for l in body)
    body = [l for l in body if l.size < 1.15 * size]  # titles may run across the gutter
    lo, hi = min(l.x0 for l in body), max(l.x1 for l in body)
    width = hi - lo
    if width <= 0:
        return
    bins = [0] * 100
    for l in body:
        for b in range(int((l.x0 - lo) / width * 99), int((l.x1 - lo) / width * 99) + 1):
            bins[b] += 1
    limit = max(1, len(body) * 0.05)
    best, b = None, 25
    while b < 75:
        if bins[b] <= limit:
            e = b
            while e < 75 and bins[e] <= limit:
                e += 1
            if not best or e - b > best[1] - best[0]:
                best = (b, e)
            b = e
        b += 1
    if not best:
        return
    gx0, gx1 = lo + best[0] / 99 * width, lo + best[1] / 99 * width
    if sum(l.x1 <= gx1 for l in body) < 5 or sum(l.x0 >= gx0 for l in body) < 5:
        return
    for l in lines:
        l.col = 0 if l.x1 <= gx1 else 1 if l.x0 >= gx0 else -1


def _geometry(lines, fallback):
    body = [l for l in lines if len(l.text) >= 15]
    if len(body) < 4:
        return fallback
    size = statistics.median(l.size for l in body)
    same = [l for l in body if abs(l.size - size) <= 0.15 * size] or body
    left, right = _pct([l.x0 for l in same], 0.1), _pct([l.x1 for l in same], 0.9)
    leads = [b.y1 - a.y1 for a, b in zip(lines, lines[1:])
             if abs(a.size - b.size) <= 0.15 * size and 0.8 * size < b.y1 - a.y1 < 3 * size]
    lead = statistics.median(leads) if len(leads) >= 3 else fallback.lead
    justified = sum(abs(l.x1 - right) < 0.5 * size for l in same) >= 0.5 * len(same)
    return Geom(left, right, lead, size, justified)


def _centered(line, g):
    return (line.x1 - line.x0 < 0.8 * (g.right - g.left)
            and abs((line.x0 + line.x1) / 2 - (g.left + g.right) / 2) < 1.5 * g.size)


def _break_kind(p, l, gp, gl):
    """How line `l` follows line `p`: None (same paragraph), 'br' (line break) or 'p' (new paragraph)."""
    size = gl.size
    if p.ocr or l.ocr:
        # OCR sizes follow the letters (a line like "nam." looks small), so only headings count
        if max(l.size, p.size) >= 1.25 * size and abs(l.size - p.size) > 0.2 * max(l.size, p.size):
            return 'p'
    elif abs(l.size - p.size) > 0.15 * max(l.size, p.size):
        return 'p'
    ptext, ltext = p.text.rstrip(), l.text.lstrip()
    same_flow = p.page == l.page and p.col == l.col and l.y0 > p.y0
    if same_flow and l.y1 - p.y1 > 1.45 * gl.lead:
        return 'p'
    if len(ptext) > 1 and ptext[-1] in '-\u2010' and ptext[-2].isalpha():
        return None
    if _LIST_RE.match(ltext) or _SEPARATOR_RE.match(ltext) or _SEPARATOR_RE.match(ptext):
        return 'p'
    first = ltext.split(' ', 1)[0]
    first_width = (l.x1 - l.x0) * len(first) / max(len(ltext), 1)
    # the line ended early if the next word would still have fitted on it
    ended = (p.x1 + 0.3 * size + first_width < gp.right - 0.2 * size
             or (gp.justified and p.x1 < gp.right - size))
    terminal = bool(_TERMINAL_RE.search(ptext))
    if l.x0 - gl.left > p.x0 - gp.left + 0.6 * size and (ended or terminal):
        return 'p'
    if terminal and p.page != l.page and p.y1 < gp.bottom - gp.lead:
        return 'p'  # the page ended a line early: the next paragraph did not fit any more
    if ended:
        if terminal or p.bold != l.bold or not ltext[:1].islower():
            return 'p'
        return 'br'
    return None


def _is_toc_page(lines):
    if len(lines) < 5:
        return False
    refs = sum(1 for l in lines if re.search(r'\s\d{1,4}$', l.text.strip()) or _LEADER_RE.search(l.text))
    return refs >= 0.4 * len(lines)


def _words(text):
    return re.findall(r'\w+', text.lower())


def _title_match(line_text, title):
    a, b = _words(line_text), _words(title)
    if not a or not b:
        return False
    k = min(len(a), len(b))
    return a[:k] == b[:k] and (k >= 2 or len(a) == 1)


def _heading_like(line, g, bold_body):
    text = line.text.strip()
    return (len(text) <= 80 and not _SENTENCE_END_RE.search(text)
            and (line.size >= 1.15 * g.size or (line.bold and not bold_body) or _centered(line, g)))


def _title_block(lines, i, geom_of, bold_body, limit=3):
    """Indices of the start line plus the heading lines right below it."""
    block, first_gap = [i], None
    while len(block) <= limit and block[-1] + 1 < len(lines):
        prev, nxt = lines[block[-1]], lines[block[-1] + 1]
        gap = nxt.y0 - prev.y1
        min_ratio = 0.7 if nxt.ocr else 0.85  # OCR sizes shrink for words without descenders
        if (not _heading_like(nxt, geom_of(nxt), bold_body) or nxt.size < min_ratio * lines[i].size
                or _CHAPTER_RE.match(nxt.text.strip()) or gap > 3 * max(prev.size, nxt.size)
                or (first_gap is not None and gap > max(1.5 * first_gap, 0.6 * nxt.size))):
            break  # a wider gap starts the text below the title, e.g. "Pasal 1"
        first_gap = gap if first_gap is None else first_gap
        block.append(block[-1] + 1)
    return block


def _outline_starts(doc, pages):
    """Chapter starts from the PDF bookmarks: [(page, line index, title)]."""
    toc = [(lvl, title.strip(), pno - 1) for lvl, title, pno in doc.get_toc()
           if 1 <= pno <= len(pages) and title.strip()]
    level1 = [e for e in toc if e[0] == 1]
    entries = level1 if len(level1) >= 4 else [e for e in toc if e[0] <= 2]
    if len(entries) < 2:
        return []
    starts, last = [], (-1, -1)
    for _, title, pno in entries:
        page = next((p for p in range(pno, min(pno + 3, len(pages))) if pages[p]), None)
        if page is None:
            continue
        idx = next((i for i, l in enumerate(pages[page]) if _title_match(l.text, title)), 0)
        if (page, idx) <= last:
            continue
        last = (page, idx)
        starts.append((page, idx, title))
    return starts


def _heading_starts(pages, geom_of, bold_body):
    """Chapter starts from headings such as 'BAB 1' or large titles: [(page, line index, None)]."""
    hits = []
    for pno, lines in enumerate(pages):
        if _is_toc_page(lines):
            continue
        for i, line in enumerate(lines):
            text = line.text.strip()
            if not text or len(text) > 80 or _LEADER_RE.search(text):
                continue
            g = geom_of(line)
            emphasised = (line.size >= 1.15 * g.size or (line.bold and not bold_body) or _centered(line, g)
                          or (i == 0 and line.x1 - line.x0 < 0.6 * (g.right - g.left)))
            if emphasised and (_CHAPTER_RE.match(text) or text.lower().rstrip(' :.') in _SECTION_WORDS):
                hits.append((pno, i))
            elif pno > 0 and line.size >= 1.4 * g.size and (len(text) > 1 or text.isdigit() or _centered(line, g)):
                hits.append((pno, i))
    if len(hits) < 2:
        return []
    starts, covered = [], set()
    for pno, i in hits:
        if (pno, i) not in covered:
            covered.update((pno, j) for j in _title_block(pages[pno], i, geom_of, bold_body))
            starts.append((pno, i, None))
    return starts


def _drop_hyphen(left, right, vocab):
    """Is the hyphen at the end of `left-` (continued by `right`) a typesetting hyphen?"""
    if not left[-1:].isalpha() or not right[:1].isalpha() or right[0].isupper():
        return False
    lw, rw = left.lower(), right.lower()
    joined, hyphenated = lw + rw, f'{lw}-{rw}'
    if vocab[hyphenated] > vocab[joined]:
        return False
    if vocab[joined]:
        return True
    # Indonesian reduplication: anak-anak, berlari-lari, tolong-menolong
    return not (lw == rw or lw.endswith(rw) or rw.endswith(lw))


def _mend_words(text, vocab):
    """Rejoin words split by stray spaces ("tela h" -> "telah") when the book itself spells them whole."""
    def join(m):
        whole = (m.group(1) + m.group(2)).lower()
        parts = max(vocab[m.group(1).lower()], vocab[m.group(2).lower()])
        return m.group(1) + m.group(2) if vocab[whole] >= 2 and vocab[whole] > parts else m.group(0)
    text = re.sub(r'\b([^\W\d_]{2,}) ([^\W\d_aiAI])\b', join, text)
    return re.sub(r'\b([^\W\d_aiAI]) ([^\W\d_]{2,})\b', join, text)


def _para_segs(para, vocab):
    segs = []
    for line, join in zip(para.lines, para.joins):
        lsegs = [list(s) for s in line.segs]
        if segs:
            last = segs[-1][0]
            if join == 'br':
                segs.append(['\n', False, False])
            elif len(last) > 1 and last[-1] in '-\u2010' and last[-2].isalpha():
                left, right = re.search(r'(\w+)[-\u2010]$', last), re.match(r'\w+', lsegs[0][0])
                if left and right and _drop_hyphen(left.group(1), right.group(0), vocab):
                    segs[-1][0] = last[:-1]
            elif last and last[-1] in '—–' and not last[:-1].endswith(' '):
                pass  # unspaced dash: no space after it either
            else:
                segs[-1][0] = last + ' '
        segs.extend(lsegs)
    merged = []
    for text, bold, italic in segs:
        if text != '\n':
            text = re.sub(r'\s+', ' ', text)
            if not merged or merged[-1][0].endswith((' ', '\n')):
                text = text.lstrip()
            if not text:
                continue
        elif merged:
            merged[-1][0] = merged[-1][0].rstrip(' ')
        if merged and '\n' not in (text, merged[-1][0]) and merged[-1][1:] == [bold, italic]:
            merged[-1][0] += text
        else:
            merged.append([text, bold, italic])
    for seg in merged:
        if seg[0] != '\n':
            seg[0] = _mend_words(seg[0], vocab)
    return merged


def _inline_html(segs, styled=True):
    out = []
    for text, bold, italic in segs:
        if text == '\n':
            out.append('<br/>')
            continue
        html = escape(text, quote=False)
        core = html.strip()
        if styled and core and (italic or bold):
            lead, trail = html[:len(html) - len(html.lstrip())], html[len(html.rstrip()):]
            if italic:
                core = f'<i>{core}</i>'
            if bold:
                core = f'<b>{core}</b>'
            html = lead + core + trail
        out.append(html)
    return re.sub(r'^(?:\s*<br/>)+|(?:<br/>\s*)+$', '', ''.join(out).strip()).strip()


def _classify(para, g, bold_body):
    text = para.text
    if _SEPARATOR_RE.match(text):
        return 'sep'
    if len(text) > 150 or len(para.lines) > 4 or _SENTENCE_END_RE.search(text):
        return 'p'
    ratio = statistics.median(l.size for l in para.lines) / g.size
    if ratio >= 1.3:
        return 'h2'
    if ratio >= 1.2 or (not bold_body and all(l.bold for l in para.lines)):
        return 'h3'
    return 'p'


def _render(paras, vocab, geom_of):
    html = []
    for para in paras:
        if para.kind == 'sep':
            html.append(f'<p style="text-align: center">{escape(para.text, quote=False)}</p>')
            continue
        segs = _para_segs(para, vocab)
        if para.kind in ('h2', 'h3'):
            inner = _inline_html(segs, styled=False)
            if inner:
                html.append(f'<{para.kind}>{inner}</{para.kind}>')
            continue
        inner = _inline_html(segs)
        if not inner:
            continue
        centered = len(para.text) < 200 and all(_centered(l, geom_of(l)) for l in para.lines)
        style = ' style="text-align: center"' if centered else ''
        html.append(f'<p{style}>{inner}</p>')
    return '\n'.join(html)


def _split_long(title, paras, vocab, geom_of):
    parts, current, size = [], [], 0
    for para in paras:
        if current and size + len(para.text) > CHAPTER_MAX_CHARS:
            parts.append(current)
            current, size = [], 0
        current.append(para)
        size += len(para.text)
    parts.append(current)
    return [(title if i == 0 else f'{title} ({i + 1})', _render(part, vocab, geom_of))
            for i, part in enumerate(parts)]


def _assemble(doc, pages, heights):
    all_lines = [l for p in pages for l in p]
    body = _weighted_median([(l.size, len(l.text)) for l in all_lines]) or 10
    bold_body = sum(len(t) for l in all_lines for t, b, _ in l.segs if b) > 0.5 * sum(len(l.text) for l in all_lines)
    _strip_running_lines(pages, heights, body)
    if not any(pages):
        return []

    for lines in pages:
        _assign_columns(lines)
    base = Geom(0, max(l.x1 for l in all_lines), 1.3 * body, body)
    parity = [_geometry([l for p in pages[k::2] for l in p if l.col == 0], base) for k in (0, 1)]
    bottom = _pct([max(l.y1 for l in p) for p in pages if p], 0.95)
    geoms = {}
    for pno, lines in enumerate(pages):
        for col in {l.col for l in lines}:
            g = _geometry([l for l in lines if l.col == col], parity[pno % 2])
            geoms[pno, col] = Geom(g.left, g.right, g.lead, g.size, g.justified, bottom)

    def geom_of(line):
        return geoms[line.page, line.col]

    vocab = Counter(w for l in all_lines
                    for w in re.findall(r'[^\W\d_]+(?:[-\u2010][^\W\d_]+)*', l.text.lower()))
    starts = _outline_starts(doc, pages) or _heading_starts(pages, geom_of, bold_body)

    # chapter titles: bookmark titles, or the heading lines found at the chapter start
    start_titles, skip = {}, set()
    for pno, i, title in starts:
        lines = pages[pno]
        block = _title_block(lines, i, geom_of, bold_body)
        if title is None:
            parts = [lines[j].text.strip() for j in block]
            parts[0] = re.sub(r'^([Bb][Aa][Bb])(?=[0-9IVXLCDM])', r'\1 ', parts[0])
            title = parts[0]
            if len(parts) > 1:
                title += (': ' if _CHAPTER_RE.fullmatch(parts[0]) else ' ') + ' '.join(parts[1:])
            skip.update(lines[j] for j in block)
        else:
            # drop the heading lines that only repeat the bookmark title
            title_words = set(_words(title))
            for j in range(i, min(i + 5, len(lines))):
                words = set(_words(lines[j].text))
                if words and words <= title_words and (j == i or j in block or len(words) <= 6):
                    skip.add(lines[j])
        start_titles[lines[i]] = title

    # without chapters the book is cut every PAGES_PER_SECTION pages (title None = page range)
    chapters = [[FRONT_MATTER_TITLE if starts else None, []]]
    next_section = PAGES_PER_SECTION
    para = prev = None
    for lines in pages:
        toc_page = _is_toc_page(lines)
        for line in lines:
            if line in start_titles:
                chapters.append([start_titles[line], []])
                para = prev = None
            if line in skip:
                continue
            if toc_page:
                # page numbers mean nothing in an EPUB; keep the entries only
                text = re.sub(r'\s*(?:' + _LEADER_RE.pattern + r')?\s*\d{1,4}$', '', line.text.strip())
                line.segs = [(_LEADER_RE.sub(' ', text).strip() or line.text, line.bold, False)]
                kind = 'p'
            else:
                kind = 'p' if para is None else _break_kind(prev, line, geom_of(prev), geom_of(line))
            if kind == 'p':
                if not starts and line.page >= next_section:
                    chapters.append([None, []])
                    next_section = (line.page // PAGES_PER_SECTION + 1) * PAGES_PER_SECTION
                para = Para([line])
                chapters[-1][1].append(para)
            else:
                para.lines.append(line)
                para.joins.append(kind)
            prev = line

    result = []
    for title, paras in chapters:
        if not paras:
            continue
        for para in paras:
            para.kind = _classify(para, geom_of(para.lines[0]), bold_body)
        if title is None:
            first, last = paras[0].lines[0].page + 1, paras[-1].lines[-1].page + 1
            title = f'Halaman {first}' if first == last else f'Halaman {first}–{last}'
        result.extend(_split_long(title, paras, vocab, geom_of))
    return result


# ---------- Public API ----------

def _metadata(doc, title_hint):
    meta = {k: re.sub(r'[\x00-\x1f\x7f]', '', v or '') for k, v in (doc.metadata or {}).items()}
    title = re.sub(r'^microsoft (?:word|powerpoint) - ', '', (meta.get('title') or '').strip(), flags=re.I)
    title = re.sub(r'\.(?:docx?|pdf|odt|rtf|txt)$', '', title, flags=re.I).strip()
    if not title or re.match(r'^[a-z]:\\', title, re.I) or re.fullmatch(
            r'untitled|document\s*\d*|dokumen\s*\d*|judul|title|tanpa judul|[\W_\d]*', title, re.I):
        title = title_hint
    title = re.sub(r'\s+', ' ', title.replace('_', ' ')).strip()
    author = (meta.get('author') or '').strip()
    if author.lower() in _JUNK_AUTHORS or re.fullmatch(r'(?:user|admin|pc)[\s\d_-]*', author, re.I):
        author = ''
    return title, author


def convert_pdf(path, mode='auto', lang='ind', cover=True, title_hint='', progress=None):
    """Convert a PDF file into a Book.

    mode: 'auto' (text layer, OCR for pages without one), 'text' or 'ocr'.
    lang: Tesseract language codes joined with '+', e.g. 'ind' or 'ind+eng'.
    progress: optional callback(fraction or None, message).
    """
    report = progress or (lambda fraction, message: None)
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise ConversionError('File tidak bisa dibuka sebagai PDF.') from e
    with doc:
        if doc.needs_pass:
            raise ConversionError('PDF ini dikunci dengan kata sandi, buka kuncinya dulu.')
        n = doc.page_count
        if n == 0:
            raise ConversionError('PDF ini tidak memiliki halaman.')
        pages, heights, to_ocr = [[] for _ in range(n)], [], []
        for page in doc:
            heights.append(page.cropbox.height)  # text coordinates ignore page rotation
            if mode == 'ocr':
                to_ocr.append(page.number)
                continue
            if page.number % 20 == 0:
                report(0.1 * page.number / n, f'Membaca teks halaman {page.number + 1}/{n}...')
            lines, raw_text = _text_lines(page)
            if mode == 'auto' and _needs_ocr(page, raw_text):
                to_ocr.append(page.number)
            else:
                pages[page.number] = lines

        if to_ocr:
            tessdata = ensure_tessdata(lang, report)
            start = 0.02 if mode == 'ocr' else 0.1
            results = _ocr_pages(path, to_ocr, lang, tessdata,
                                 lambda fraction, message: report(start + (0.88 - start) * fraction, message))
            for pno, lines in results.items():
                pages[pno] = lines or []
                heights[pno] = doc[pno].rect.height

        report(0.9, 'Menyusun paragraf dan bab...')
        chapters = _assemble(doc, pages, heights) if any(pages) else []
        if not chapters:
            if mode == 'text':
                raise ConversionError('Tidak ada teks yang bisa diambil. Sepertinya PDF ini hasil scan, '
                                      'coba metode OCR.')
            raise ConversionError('Tidak ada teks yang terbaca dari PDF ini.')
        title, author = _metadata(doc, title_hint or 'Tanpa Judul')
        words = Counter(w for p in pages for l in p for w in _words(l.text))
        language = 'en' if sum(words[w] for w in _EN_WORDS) > sum(words[w] for w in _ID_WORDS) else 'id'
        cover_jpeg = None
        if cover:
            zoom = COVER_WIDTH / doc[0].rect.width
            cover_jpeg = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes('jpeg', jpg_quality=85)
        return Book(title=title, author=author, language=language, chapters=chapters, cover=cover_jpeg,
                    pages=n, ocr_pages=len(to_ocr), empty_pages=sum(1 for p in pages if not p))
