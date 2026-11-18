"""Convert a Narou vertical PDF to a reflowable, right-to-left EPUB 3."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing, ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from statistics import median
import sys
from tempfile import TemporaryDirectory, TemporaryFile
import uuid
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pdfplumber
from pdfminer.pdffont import PDFCIDFont
from pdfminer.pdfinterp import PDFResourceManager


class _UniJISFont(PDFCIDFont):
    """Keep UTF-16 characters aligned even outside Adobe's Japanese CID map."""

    def __init__(self, manager, spec):
        super().__init__(manager, spec)
        self._unicode_cids = {}

    def decode(self, data):
        try:
            text = data.decode('utf-16-be')
        except UnicodeDecodeError:
            return super().decode(data)
        result = []
        for ch in text:
            if ch not in self._unicode_cids:
                mapped = list(super().decode(ch.encode('utf-16-be')))
                # Negative IDs cannot collide with a real CID. Unmapped
                # characters use the font's default advance.
                self._unicode_cids[ch] = mapped[0] if len(mapped) == 1 else -ord(ch) - 1
            result.append(self._unicode_cids[ch])
        return result

    def to_unichr(self, cid):
        return chr(-cid - 1) if cid < 0 else super().to_unichr(cid)


class _NarouResources(PDFResourceManager):
    def get_font(self, objid, spec):
        if (getattr(spec.get('Subtype'), 'name', '') in ('CIDFontType0', 'CIDFontType2')
                and getattr(spec.get('Encoding'), 'name', '') in ('UniJIS-UTF16-V', 'UniJIS-UTF16-H')):
            return _UniJISFont(self, spec)
        return super().get_font(objid, spec)


XHTML = 'http://www.w3.org/1999/xhtml'
OPF = 'http://www.idpf.org/2007/opf'
DC = 'http://purl.org/dc/elements/1.1/'
NCX = 'http://www.daisy.org/z3986/2005/ncx/'
CONTAINER = 'urn:oasis:names:tc:opendocument:xmlns:container'
ET.register_namespace('', XHTML)


@dataclass
class Paragraph:
    parts: list[tuple[str, str | None]] = field(default_factory=list)
    blank_before: int = 0
    pages: list[int] = field(default_factory=list)

    @property
    def text(self) -> str:
        return ''.join(base for base, _ in self.parts)


@dataclass
class Section:
    title: str
    page: int
    paragraphs: list[Paragraph] = field(default_factory=list)


def _element(parent, tag, text=None, **attrs):
    node = ET.SubElement(parent, tag, attrs)
    if text is not None:
        node.text = text
    return node


def _xml(root) -> bytes:
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _add_parts(node, parts):
    last = node
    plain = []
    for base, annotation in parts:
        if annotation:
            if plain:
                setattr(last, 'text' if last is node else 'tail', ''.join(plain))
                plain.clear()
            last = _element(node, 'ruby', base)
            _element(last, 'rt', annotation)
        else:
            plain.append(base)
    if plain:
        setattr(last, 'text' if last is node else 'tail', ''.join(plain))


def _ruby_runs(chars):
    """Split annotations sharing an x coordinate at vertical whitespace.

    Character origins alone do not identify a ruby: several unrelated words
    may be annotated in the same column. Keep normal glyph spacing together.
    """
    runs = []
    for char in sorted(chars, key=lambda c: c['top']):
        if not runs or char['top'] - runs[-1][-1]['bottom'] > min(
                char['size'], runs[-1][-1]['size']) * .5:
            runs.append([char])
        else:
            runs[-1].append(char)
    return runs


def _column_parts(chars, ruby_groups, column_x, body_size, diagnostics, page_number):
    ordered = sorted(chars, key=lambda c: -c['matrix'][5])
    used = set()
    rubies = {}
    for group in ruby_groups:
        rx = median(c['matrix'][4] for c in group)
        # Vertical ruby sits on the right side of its base column in these PDFs.
        if not (body_size * .3 < abs(column_x - rx) < body_size):
            continue
        low, high = min(c['top'] for c in group), max(c['bottom'] for c in group)
        # A tiny overlap at a glyph boundary should not absorb the next base glyph.
        minimum_overlap = min(body_size, high - low) * .25
        indices = [i for i, c in enumerate(ordered)
                   if min(c['bottom'], high) - max(c['top'], low) > minimum_overlap]
        if not indices:
            center = (low + high) / 2
            indices = [min(range(len(ordered)),
                           key=lambda i: abs((ordered[i]['top'] + ordered[i]['bottom']) / 2 - center))]
            diagnostics['ruby_ambiguous'].append({'page': page_number, 'x': round(rx, 1),
                                                   'reason': 'nearest-character fallback'})
        lo, hi = min(indices), max(indices)
        if any(i in used for i in range(lo, hi + 1)):
            diagnostics['ruby_ambiguous'].append({'page': page_number, 'x': round(rx, 1)})
            # Keep the annotation even when its exact base range is uncertain.
            free = [i for i in range(len(ordered)) if i not in used]
            if not free:
                diagnostics['unassigned_small_text'].append({'page': page_number, 'x': round(rx, 1),
                                                             'count': len(group)})
                continue
            lo = hi = min(free, key=lambda i: abs(ordered[i]['top'] - low))
        used.update(range(lo, hi + 1))
        rubies[lo] = (hi, ''.join(c['text'] for c in sorted(group, key=lambda c: -c['matrix'][5])))
        diagnostics['ruby_count'] += 1
    parts = []
    i = 0
    while i < len(ordered):
        if i in rubies:
            hi, annotation = rubies[i]
            parts.append((''.join(c['text'] for c in ordered[i:hi + 1]), annotation))
            i = hi + 1
        else:
            start = i
            while i < len(ordered) and i not in rubies:
                i += 1
            parts.append((''.join(c['text'] for c in ordered[start:i]), None))
    return parts


def _page_number_group(chars, page_width, page_height, body_size,
                       previous_footer=None, page_number=None):
    """Find a footer number by its bottom position and page sequence."""
    candidates = []
    for char in chars:
        if not char['text'].isdecimal():
            continue
        ordinary_footer = (body_size * .65 <= char['size'] < body_size - .25
                           and char['top'] > page_height - body_size * 5)
        matches_reference = False
        if previous_footer:
            _, _, last_bottom, last_size = previous_footer
            matches_reference = (abs(char['size'] - last_size) <= .25
                                 and abs((page_height - char['top']) - last_bottom)
                                 <= max(body_size, last_size) / 2)
        if ordinary_footer or matches_reference:
            candidates.append(char)
    rows = defaultdict(list)
    for char in candidates:
        rows[(round(char['top'], 1), round(char['size'], 1), char['fontname'])].append(char)
    matches = []
    gap_limit = max(body_size, previous_footer[3]) if previous_footer else body_size
    for row in rows.values():
        ordered = sorted(row, key=lambda c: c['matrix'][4])
        runs = []
        for char in ordered:
            if not runs or not 0 < char['matrix'][4] - runs[-1][-1]['matrix'][4] < gap_limit:
                runs.append([char])
            else:
                runs[-1].append(char)
        for run in runs:
            center = (run[0]['matrix'][4] + run[-1]['matrix'][4]) / 2
            near_center = abs(center - page_width / 2) <= body_size
            sequential = False
            if previous_footer and page_number is not None:
                last_page, last_number, last_bottom, last_size = previous_footer
                sequential = (int(''.join(c['text'] for c in run)) ==
                              last_number + page_number - last_page
                              and abs((page_height - run[0]['top']) - last_bottom)
                              <= max(body_size, last_size) / 2
                              and abs(run[0]['size'] - last_size) <= .25)
            if sequential or (previous_footer is None and near_center):
                matches.append((not sequential, abs(center - page_width / 2), -len(run), run))
    return min(matches, key=lambda item: item[:3])[3] if matches else []


def _footer_reference(page, group):
    return (page.page_number, int(''.join(c['text'] for c in group)),
            page.height - group[0]['top'], group[0]['size'])


def _cover_fields(page):
    lines = [line.strip() for line in (page.extract_text() or '').splitlines() if line.strip()]
    if not lines:
        return '', '', []

    # Cover titles can wrap onto several lines. In these PDFs the author starts
    # where the large title font changes to the smaller cover-text font.
    rows = defaultdict(list)
    for char in page.chars:
        if char['text'].strip():
            rows[round(char['top'], 1)].append(char)
    title_line_count = 1
    if len(rows) == len(lines):
        sizes = [median(char['size'] for char in chars)
                 for _, chars in sorted(rows.items())]
        title_line_count = next((i for i in range(1, len(sizes))
                                 if sizes[i] < sizes[0] * .8), 1)
    title = ''.join(lines[:title_line_count])
    author = lines[title_line_count] if title_line_count < len(lines) else ''
    return title, author, lines[title_line_count + 1:]


def _body_grid(xs, body_size):
    """Find the column grid on a regular page with several body columns."""
    if len(xs) < 4:
        return None
    gaps = [round(a - b, 1) for a, b in zip(xs, xs[1:]) if a - b > body_size]
    if not gaps:
        return None
    pitch, count = Counter(gaps).most_common(1)[0]
    if count < 2 or any(_column_steps(xs[0] - x, pitch) is None for x in xs):
        return None
    return xs[-1], xs[0], pitch


def _column_steps(distance, pitch):
    steps = distance / pitch
    rounded = round(steps)
    if rounded < 0 or abs(steps - rounded) > .15:
        return None
    return rounded


def _boundary_blank(previous, first, grids):
    if previous['page'] + 1 != first['page'] or previous['layout'] != first['layout']:
        return None
    grid = grids.get(first['layout'])
    if grid is None:
        return None
    left, right, pitch = grid
    left_blank = _column_steps(previous['x'] - left, pitch)
    right_blank = _column_steps(right - first['x'], pitch)
    width = right - left
    if (left_blank is None or right_blank is None
            or previous['x'] - left > width + pitch * .15
            or right - first['x'] > width + pitch * .15):
        return None
    return left_blank + right_blank


def _restore_paragraphs(section, pages, grids, diagnostics):
    yield section, None
    for paragraph in section.paragraphs:
        yield section, paragraph
    previous = None
    paragraph = None
    for number, layout, columns, horizontal in pages:
        if horizontal:
            if paragraph is not None:
                yield section, paragraph
            for line in horizontal:
                yield section, Paragraph([(line, None)], pages=[number])
            paragraph = None
            previous = None
            continue
        _, height, body_size = layout
        body_xs = [c['x'] for c in columns if not c['heading']]
        gaps = [a - b for a, b in zip(body_xs, body_xs[1:]) if a - b > body_size]
        grid = grids.get(layout)
        if grid:
            pitch = grid[2]
        elif len(gaps) >= 2:
            pitch = median(sorted(gaps)[:len(gaps) // 2])
        else:
            # A single observed gap could itself span several columns.
            pitch = min(gaps[0], body_size * 1.6) if gaps else body_size * 1.6
        for col in columns:
            if col['heading']:
                if paragraph is not None:
                    yield section, paragraph
                section = Section(col['text'], number)
                yield section, None
                paragraph = None
                previous = None
                continue
            same_page = previous is not None and previous['page'] == number
            blank = 0
            if same_page:
                gap = previous['x'] - col['x']
                blank = max(0, round(gap / pitch) - 1)
                if gap > pitch * 1.15 and (_column_steps(gap, pitch) is None or (blank and not grid)):
                    diagnostics['column_gap_review'].append(
                        {'page': number, 'from_x': previous['x'], 'to_x': col['x']})
            elif previous:
                boundary_blank = _boundary_blank(previous, col, grids)
                if boundary_blank is None:
                    diagnostics['page_boundary_review'].append(number)
                else:
                    blank = boundary_blank
            short_previous_line = (same_page and previous['bottom'] < height * .8
                                   and abs(previous['top'] - col['top']) <= body_size)
            dialogue_break = False
            if previous is not None and col['text'].startswith('「'):
                previous_text = previous['text']
                dialogue_break = previous_text.endswith('」') or (
                    previous['page'] + 1 == number and previous_text.endswith('。'))
            starts = dialogue_break or col['text'].startswith('\u3000') or (
                col['text'].startswith(('「', '『')) and previous is not None
                and previous['bottom'] < height * .8) or short_previous_line
            if previous is None or starts or blank:
                if paragraph is not None:
                    yield section, paragraph
                paragraph = Paragraph(blank_before=blank)
            paragraph.parts.extend(col['parts'])
            if not paragraph.pages or paragraph.pages[-1] != number:
                paragraph.pages.append(number)
            previous = col
    if paragraph is not None:
        yield section, paragraph


def _diagnostics(make_log=list):
    return {'removed_page_numbers': make_log(), 'ruby_count': 0,
            'ruby_ambiguous': make_log(), 'unassigned_small_text': make_log(),
            'page_boundary_review': make_log(), 'column_gap_review': make_log(),
            'skipped_pages': make_log(), 'horizontal_pages': make_log()}


def _save_page(spool, number, layout, columns, horizontal):
    spool.write(json.dumps((number, layout, columns, horizontal), ensure_ascii=False) + '\n')


def _read_pages(spool):
    spool.seek(0)
    for line in spool:
        number, layout, columns, horizontal = json.loads(line)
        if layout is not None:
            layout = tuple(layout)
        for col in columns:
            col['layout'] = layout
            col['page'] = number
            col['parts'] = [tuple(part) for part in col['parts']]
        yield number, layout, columns, horizontal


def _extract_pages(pdf_path, spool, diagnostics, title_override, author_override):
    grid_counts = defaultdict(Counter)
    previous_footer = None
    previous_body_size = None
    vertical_body_count = 0
    with pdfplumber.open(pdf_path) as pdf:
        pdf.rsrcmgr = _NarouResources()
        if not pdf.pages:
            raise ValueError('PDFにページがありません。')
        with closing(pdf.pages[0]) as cover:
            cover_title, cover_author, cover_extra = _cover_fields(cover)
        title = title_override or cover_title
        author = author_override or cover_author
        if not title:
            raise ValueError('表紙からタイトルを取得できません。--title を指定してください。')
        if not author:
            raise ValueError('表紙から著者を取得できません。--author を指定してください。')
        cover_paragraphs = [Paragraph([(author, None)], pages=[1])]
        cover_paragraphs.extend(Paragraph([(line.strip(), None)], pages=[1])
                                for line in cover_extra)
        cover = Section(title, 1, cover_paragraphs)
        # A centered footer on an early page also identifies a shifted first footer.
        for sample_page in pdf.pages[2:6]:
            with closing(sample_page):
                if not sample_page.chars:
                    continue
                sample_size = Counter(round(c['size'], 1) for c in sample_page.chars).most_common(1)[0][0]
                sample_footer = _page_number_group(sample_page.chars, sample_page.width,
                                                    sample_page.height, sample_size)
                if sample_footer:
                    previous_footer = _footer_reference(sample_page, sample_footer)
                    break
        for page in pdf.pages[1:]:
            with closing(page):
                if not page.chars:
                    diagnostics['skipped_pages'].append(page.page_number)
                    continue
                sizes = Counter(round(c['size'], 1) for c in page.chars)
                body_size = sizes.most_common(1)[0][0]
                if previous_body_size and body_size < previous_body_size * .75:
                    prior_columns = Counter(round(c['matrix'][4], 2) for c in page.chars
                                            if abs(c['size'] - previous_body_size) <= .25)
                    if max(prior_columns.values(), default=0) >= 4:
                        body_size = previous_body_size
                previous_body_size = body_size
                # 横書きの奥付を縦書きの本文として扱わない。
                footer = _page_number_group(page.chars, page.width, page.height, body_size,
                                            previous_footer, page.page_number)
                footer_ids = {id(c) for c in footer}
                groups = defaultdict(list)
                small = []
                for c in page.chars:
                    if id(c) in footer_ids:
                        continue
                    if abs(c['size'] - body_size) <= .25:
                        groups[round(c['matrix'][4], 2)].append(c)
                    else:
                        small.append(c)
                if footer:
                    footer_text = ''.join(c['text'] for c in footer)
                    diagnostics['removed_page_numbers'].append(
                        {'page': page.page_number, 'number': footer_text})
                    previous_footer = _footer_reference(page, footer)
                columns = [(x, cs) for x, cs in sorted(groups.items(), reverse=True) if len(cs) >= 2]
                row_counts = Counter(round(c['top'], 1) for cs in groups.values() for c in cs)
                tallest_column = max((len(cs) for _, cs in columns), default=0)
                looks_horizontal = max(row_counts.values(), default=0) > tallest_column * 2
                if tallest_column < 4 or looks_horizontal:
                    lines = [line.strip() for line in (page.extract_text() or '').splitlines() if line.strip()]
                    if lines and lines[-1].isdecimal():
                        number = lines.pop()
                        if not footer or number != footer_text:
                            diagnostics['removed_page_numbers'].append({'page': page.page_number,
                                                                         'number': number})
                    if lines:
                        diagnostics['horizontal_pages'].append(page.page_number)
                        _save_page(spool, page.page_number, None, [], lines)
                    else:
                        diagnostics['skipped_pages'].append(page.page_number)
                    continue
                small_groups = defaultdict(list)
                for c in small:
                    small_groups[round(c['matrix'][4], 1)].append(c)
                # 別々のルビが本文列や親文字の判定に影響しないよう、先に分割する。
                small_groups = {(rx, i): run for rx, chars in small_groups.items()
                                for i, run in enumerate(_ruby_runs(chars))}
                remaining = set(small_groups)
                attached_to = defaultdict(list)
                for key, group in small_groups.items():
                    rx, _ = key
                    low, high = min(c['top'] for c in group), max(c['bottom'] for c in group)
                    candidates = [(-sum(c['top'] < high and c['bottom'] > low for c in cs),
                                   x >= rx, abs(x - rx), x)
                                  for x, cs in columns if body_size * .3 < abs(x - rx) < body_size]
                    if candidates:
                        _, _, _, closest = min(candidates)
                        attached_to[closest].append(group)
                        remaining.discard(key)
                layout = (round(page.width, 1), round(page.height, 1), body_size)
                column_data = []
                for x, cs in columns:
                    attached = attached_to[x]
                    parts = _column_parts(cs, attached, x, body_size, diagnostics, page.page_number)
                    bold = sum('Bold' in c['fontname'] for c in cs) / len(cs) >= .8
                    col_text = ''.join(v for v, _ in parts)
                    if not col_text.strip():
                        continue
                    column_data.append({'x': x, 'parts': parts, 'text': col_text,
                                        'bold': bold,
                                        'top': min(c['top'] for c in cs),
                                        'bottom': max(c['bottom'] for c in cs)})
                for key in sorted(remaining):
                    rx, _ = key
                    diagnostics['unassigned_small_text'].append({'page': page.page_number, 'x': rx,
                                                                 'count': len(small_groups[key])})
                nonbold_xs = [c['x'] for c in column_data if not c['bold']]
                right_body = max(nonbold_xs, default=float('-inf'))
                for col in column_data:
                    col['heading'] = col['bold'] and col['x'] > right_body + body_size * 2
                body_xs = [c['x'] for c in column_data if not c['heading']]
                vertical_body_count += len(body_xs)
                if len(body_xs) == len(column_data):
                    grid = _body_grid(body_xs, body_size)
                    if grid is not None:
                        grid_counts[layout][grid] += 1
                if column_data:
                    _save_page(spool, page.page_number, layout, column_data, [])
    if not vertical_body_count:
        raise ValueError('縦書き本文を抽出できません。画像PDFや異なるレイアウトの可能性があります。')
    # Prefer the most common regular layout and the widest span when tied.
    grids = {layout: max(counts, key=lambda g: (counts[g], g[1] - g[0]))
             for layout, counts in grid_counts.items()}
    return title, author, cover, grids


def extract(pdf_path: Path, title_override=None, author_override=None):
    """段落データを必要とする呼び出し元向けに、抽出結果を全巻分まとめる。"""
    diagnostics = _diagnostics()
    sections = []
    with TemporaryFile(mode='w+t', encoding='utf-8') as spool:
        title, author, cover, grids = _extract_pages(
            pdf_path, spool, diagnostics, title_override, author_override)
        for section, paragraph in _restore_paragraphs(
                cover, _read_pages(spool), grids, diagnostics):
            if paragraph is None:
                sections.append(Section(section.title, section.page))
            else:
                sections[-1].paragraphs.append(paragraph)
    return title, author, sections, diagnostics


_CHAPTER_END = b'</body></html>'


def _chapter_start(section: Section, index: int) -> bytes:
    html = ET.Element(f'{{{XHTML}}}html', {'lang': 'ja', '{http://www.w3.org/XML/1998/namespace}lang': 'ja'})
    head = _element(html, f'{{{XHTML}}}head')
    _element(head, f'{{{XHTML}}}title', section.title)
    _element(head, f'{{{XHTML}}}link', rel='stylesheet', href='style.css', type='text/css')
    body = _element(html, f'{{{XHTML}}}body')
    _element(body, f'{{{XHTML}}}h1', section.title, id=f'section-{index:03d}')
    return _xml(html).removesuffix(_CHAPTER_END)


def _paragraph_xml(paragraph):
    # この断片には、章のルート要素で宣言したXHTMLの名前空間が適用される。
    attrs = {'class': f'scene-break-{paragraph.blank_before}'} if paragraph.blank_before else {}
    p = ET.Element('p', attrs)
    _add_parts(p, paragraph.parts)
    return ET.tostring(p, encoding='utf-8')


def _write_chapters(archive, events):
    sections = []
    blank_counts = {1, 2, 3}
    chapter = None
    with ExitStack() as stack:
        for section, paragraph in events:
            if paragraph is None:
                if chapter is not None:
                    chapter.write(_CHAPTER_END)
                    stack.close()
                sections.append({'title': section.title, 'page': section.page, 'paragraphs': 0})
                index = len(sections)
                chapter = stack.enter_context(archive.open(f'OEBPS/chapter-{index:03d}.xhtml', 'w'))
                chapter.write(_chapter_start(section, index))
                continue
            sections[-1]['paragraphs'] += 1
            if paragraph.blank_before:
                blank_counts.add(paragraph.blank_before)
            if any(base.strip() for base, _ in paragraph.parts):
                chapter.write(_paragraph_xml(paragraph))
        if chapter is not None:
            chapter.write(_CHAPTER_END)
    return sections, blank_counts


def _write_package(archive, title, author, sections, blank_counts):
    blank_styles = ''.join(f'p.scene-break-{n} {{ margin-block-start: {n}.5em; }}\n'
                           for n in sorted(blank_counts))
    book_id = f'urn:uuid:{uuid.uuid4()}'
    container = ET.Element(f'{{{CONTAINER}}}container', version='1.0')
    rootfiles = _element(container, f'{{{CONTAINER}}}rootfiles')
    _element(rootfiles, f'{{{CONTAINER}}}rootfile', **{'full-path': 'OEBPS/package.opf',
                                                      'media-type': 'application/oebps-package+xml'})
    package = ET.Element(f'{{{OPF}}}package', version='3.0', **{'unique-identifier': 'book-id'})
    metadata = _element(package, f'{{{OPF}}}metadata')
    _element(metadata, f'{{{DC}}}identifier', book_id, id='book-id')
    _element(metadata, f'{{{DC}}}title', title)
    _element(metadata, f'{{{DC}}}creator', author)
    _element(metadata, f'{{{DC}}}language', 'ja')
    _element(metadata, f'{{{OPF}}}meta',
             datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
             property='dcterms:modified')
    _element(metadata, f'{{{OPF}}}meta', 'reflowable', property='rendition:layout')
    _element(metadata, f'{{{OPF}}}meta', 'vertical-rl', property='rendition:writing-mode')
    manifest = _element(package, f'{{{OPF}}}manifest')
    _element(manifest, f'{{{OPF}}}item', id='nav', href='nav.xhtml', **{'media-type': 'application/xhtml+xml', 'properties': 'nav'})
    _element(manifest, f'{{{OPF}}}item', id='ncx', href='toc.ncx', **{'media-type': 'application/x-dtbncx+xml'})
    _element(manifest, f'{{{OPF}}}item', id='style', href='style.css', **{'media-type': 'text/css'})
    spine = _element(package, f'{{{OPF}}}spine', toc='ncx', **{'page-progression-direction': 'rtl'})
    nav_html = ET.Element(f'{{{XHTML}}}html', {'lang': 'ja', '{http://www.w3.org/XML/1998/namespace}lang': 'ja'})
    nav_head = _element(nav_html, f'{{{XHTML}}}head')
    _element(nav_head, f'{{{XHTML}}}title', '目次')
    nav_body = _element(nav_html, f'{{{XHTML}}}body')
    nav = _element(nav_body, f'{{{XHTML}}}nav', **{'{http://www.idpf.org/2007/ops}type': 'toc', 'id': 'toc'})
    _element(nav, f'{{{XHTML}}}h1', '目次')
    ol = _element(nav, f'{{{XHTML}}}ol')
    ncx = ET.Element(f'{{{NCX}}}ncx', version='2005-1')
    ncx_head = _element(ncx, f'{{{NCX}}}head')
    _element(ncx_head, f'{{{NCX}}}meta', name='dtb:uid', content=book_id)
    doc_title = _element(ncx, f'{{{NCX}}}docTitle')
    _element(doc_title, f'{{{NCX}}}text', title)
    nav_map = _element(ncx, f'{{{NCX}}}navMap')
    for i, section in enumerate(sections, 1):
        name = f'chapter-{i:03d}.xhtml'
        anchor = f'{name}#section-{i:03d}'
        _element(manifest, f'{{{OPF}}}item', id=f'chapter-{i:03d}', href=name,
                 **{'media-type': 'application/xhtml+xml'})
        _element(spine, f'{{{OPF}}}itemref', idref=f'chapter-{i:03d}')
        li = _element(ol, f'{{{XHTML}}}li')
        _element(li, f'{{{XHTML}}}a', section['title'], href=anchor)
        point = _element(nav_map, f'{{{NCX}}}navPoint', id=f'nav-{i:03d}', playOrder=str(i))
        label = _element(point, f'{{{NCX}}}navLabel')
        _element(label, f'{{{NCX}}}text', section['title'])
        _element(point, f'{{{NCX}}}content', src=anchor)
    contents = {'META-INF/container.xml': _xml(container),
                     'OEBPS/package.opf': _xml(package),
                     'OEBPS/nav.xhtml': _xml(nav_html),
                     'OEBPS/toc.ncx': _xml(ncx),
                     'OEBPS/style.css': (':root { writing-mode: vertical-rl; -epub-writing-mode: vertical-rl; -webkit-writing-mode: vertical-rl; }\n'
                                         'html, body { writing-mode: vertical-rl; -epub-writing-mode: vertical-rl; -webkit-writing-mode: vertical-rl; }\n'
                                         'body { margin: 0; padding: 0; line-height: 1.6; }\n'
                                         'h1 { font-size: 1.25em; margin-block: 0 1.5em; }\n'
                                         'p { margin-block: 0; text-indent: 0; }\n'
                                         + blank_styles +
                                         'rt { font-size: 0.5em; }\n').encode('utf-8')}
    for name, data in contents.items():
        archive.writestr(name, data)


def _write_epub(path, title, author, events):
    with ZipFile(path, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr(ZipInfo('mimetype'), 'application/epub+zip', compress_type=ZIP_STORED)
        sections, blank_counts = _write_chapters(archive, events)
        _write_package(archive, title, author, sections, blank_counts)
    return sections


def build_epub(path: Path, title: str, author: str, sections: list[Section]):
    def events():
        for section in sections:
            yield section, None
            for paragraph in section.paragraphs:
                yield section, paragraph

    _write_epub(path, title, author, events())


class _AnchorCheck:
    """章のXMLツリーを保持せず、目次リンクの移動先を確認する。"""

    def __init__(self, anchors):
        self.remaining = set(anchors)

    def start(self, tag, attrs):
        self.remaining.discard(attrs.get('id'))

    def close(self):
        return self.remaining


def validate_epub(path: Path, expected_sections: int):
    with ZipFile(path) as z:
        corrupt = z.testzip()
        if corrupt:
            raise ValueError(f'EPUB内のZIPデータが破損しています: {corrupt}')
        names = set(z.namelist())
        if z.namelist()[0] != 'mimetype' or z.getinfo('mimetype').compress_type != ZIP_STORED:
            raise ValueError('EPUB mimetype が不正です。')
        package = ET.fromstring(z.read('OEBPS/package.opf'))
        manifest = package.find(f'{{{OPF}}}manifest')
        spine = package.find(f'{{{OPF}}}spine')
        items = {item.get('id'): item.get('href') for item in manifest}
        for href in items.values():
            if 'OEBPS/' + href not in names:
                raise ValueError(f'EPUB内の参照がありません: {href}')
        if len(spine) != expected_sections or spine.get('page-progression-direction') != 'rtl':
            raise ValueError('EPUBの読み順が不正です。')
        if any(item.get('idref') not in items for item in spine):
            raise ValueError('spine に存在しない文書が指定されています。')
        nav = ET.fromstring(z.read('OEBPS/nav.xhtml'))
        links = nav.findall(f'.//{{{XHTML}}}a')
        if len(links) != expected_sections:
            raise ValueError('目次項目数が一致しません。')
        anchors = defaultdict(set)
        for link in links:
            file, anchor = link.get('href').split('#', 1)
            target = 'OEBPS/' + file
            if target not in names:
                raise ValueError(f'目次リンク先がありません: {file}#{anchor}')
            anchors[target].add(anchor)
        ncx = ET.fromstring(z.read('OEBPS/toc.ncx'))
        points = ncx.findall(f'.//{{{NCX}}}navPoint')
        if len(points) != expected_sections:
            raise ValueError('NCX目次項目数が一致しません。')
        if [point.find(f'{{{NCX}}}content').get('src') for point in points] != [link.get('href') for link in links]:
            raise ValueError('EPUB 3とNCXの目次リンクが一致しません。')
        parsed = {'OEBPS/package.opf', 'OEBPS/nav.xhtml', 'OEBPS/toc.ncx'}
        for name in sorted((names - parsed) | anchors.keys()):
            if name not in anchors and not name.endswith(('.opf', '.xhtml', '.ncx', '.xml')):
                continue
            parser = ET.XMLParser(target=_AnchorCheck(anchors.get(name, ())))
            with z.open(name) as member:
                while chunk := member.read(65536):
                    parser.feed(chunk)
            missing = parser.close()
            if missing:
                raise ValueError(f'目次リンク先がありません: {name}#{min(missing)}')


def _protected(path: Path) -> bool:
    return any(part.casefold() == '.dev' for part in path.resolve().parts)


class _DiagnosticLog:
    def __init__(self, stream):
        self.stream = stream

    def append(self, value):
        self.stream.write(json.dumps(value, ensure_ascii=False) + '\n')

    def write_array(self, target):
        self.stream.seek(0)
        target.write('[')
        separator = ''
        for line in self.stream:
            record = json.dumps(json.loads(line), ensure_ascii=False, indent=2)
            target.write(separator + '\n      ' + record.replace('\n', '\n      '))
            separator = ','
        target.write('\n    ]' if separator else ']')


def _write_report(path, summary, diagnostics):
    with path.open('w', encoding='utf-8') as target:
        target.write(json.dumps(summary, ensure_ascii=False, indent=2).removesuffix('\n}'))
        target.write(',\n  "diagnostics": {')
        separator = ''
        for key, value in diagnostics.items():
            target.write(separator + '\n    ' + json.dumps(key) + ': ')
            if isinstance(value, _DiagnosticLog):
                value.write_array(target)
            else:
                target.write(json.dumps(value))
            separator = ','
        target.write('\n  }\n}')


def _convert_one(source: Path, output: Path, report: Path, title_override=None,
                 author_override=None):
    with source.open('rb') as stream:
        source_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    with ExitStack() as stack:
        work = Path(stack.enter_context(TemporaryDirectory(dir=output.parent, prefix='.narou-')))
        spool = stack.enter_context((work / 'pages.jsonl').open('w+', encoding='utf-8'))

        def make_log():
            stream = stack.enter_context(TemporaryFile(mode='w+t', encoding='utf-8', dir=work))
            return _DiagnosticLog(stream)

        diagnostics = _diagnostics(make_log)
        title, author, cover, grids = _extract_pages(
            source, spool, diagnostics, title_override, author_override)
        temporary = work / 'book.epub'
        sections = _write_epub(temporary, title, author, _restore_paragraphs(
            cover, _read_pages(spool), grids, diagnostics))
        spool.close()
        (work / 'pages.jsonl').unlink()
        validate_epub(temporary, len(sections))
        paragraph_count = sum(section['paragraphs'] for section in sections)
        summary = {'title': title, 'author': author, 'source_sha256': source_hash,
                   'sections': sections, 'paragraph_count': paragraph_count}
        report_dir = work
        if report.parent != output.parent:
            report_dir = Path(stack.enter_context(
                TemporaryDirectory(dir=report.parent, prefix='.narou-')))
        temporary_report = report_dir / 'report.json'
        _write_report(temporary_report, summary, diagnostics)
        with source.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != source_hash:
                raise ValueError('変換中に入力PDFが変更されました。')
        temporary_report.replace(report)
        temporary.replace(output)
    print(f'EPUB: {output}\n診断: {report}\n目次: {len(sections)}項目、段落: {paragraph_count}')


def main(argv=None):
    parser = argparse.ArgumentParser(description='小説家になろう縦書きPDFを縦書きEPUBへ変換します。')
    parser.add_argument('pdf', type=Path, help='入力PDF、またはPDFを格納したフォルダ')
    parser.add_argument('output', type=Path, help='出力EPUB、または出力フォルダ')
    parser.add_argument('--title', help='書籍タイトルを上書き')
    parser.add_argument('--author', help='著者名を上書き')
    parser.add_argument('--report', type=Path, help='診断JSONの出力先。既定はEPUBと同名の .report.json')
    parser.add_argument('--overwrite', action='store_true', help='既存の出力とレポートを上書き')
    args = parser.parse_args(argv)
    source = args.pdf.resolve()
    output = args.output.resolve()
    if source.is_dir():
        if args.title or args.report:
            parser.error('フォルダ指定では --title と --report は使用できません。')
        if _protected(output):
            parser.error('.dev 内には出力できません。')
        if output.exists() and not output.is_dir():
            parser.error(f'出力先はフォルダを指定してください: {output}')
        pdfs = sorted((path for path in source.iterdir()
                       if path.is_file() and path.suffix.casefold() == '.pdf'),
                      key=lambda path: (path.name.casefold(), path.name))
        if not pdfs:
            parser.error(f'入力フォルダにPDFがありません: {source}')
        names = [path.stem.casefold() for path in pdfs]
        if len(names) != len(set(names)):
            parser.error('同名の出力になるPDFがあります。入力PDFのファイル名を変更してください。')
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            parser.exit(1, f'出力フォルダの作成に失敗しました: {exc}\n')
        converted = skipped = failed = 0
        for pdf in pdfs:
            epub = output / f'{pdf.stem}.epub'
            report = output / f'{pdf.stem}.report.json'
            if not args.overwrite and (epub.exists() or report.exists()):
                print(f'スキップ（出力が既存）: {pdf.name}')
                skipped += 1
                continue
            try:
                _convert_one(pdf, epub, report, author_override=args.author)
                converted += 1
            except Exception as exc:
                print(f'変換失敗 ({pdf.name}; {type(exc).__name__}): {exc}', file=sys.stderr)
                failed += 1
        print(f'一括処理: 成功 {converted}件、スキップ {skipped}件、失敗 {failed}件')
        if failed:
            raise SystemExit(1)
        return
    report = (args.report or args.output.with_suffix('.report.json')).resolve()
    if not source.is_file():
        parser.error(f'入力PDFがありません: {source}')
    if source.suffix.lower() != '.pdf' or output.suffix.lower() != '.epub':
        parser.error('入力は .pdf、出力は .epub を指定してください。')
    if output == source or report == source or report == output:
        parser.error('入力、EPUB、診断レポートにはそれぞれ別のパスが必要です。')
    if _protected(output) or _protected(report):
        parser.error('.dev 内には出力できません。')
    if not args.overwrite and (output.exists() or report.exists()):
        parser.error('出力または診断レポートが既に存在します。上書きする場合は --overwrite を指定してください。')
    if not output.parent.is_dir() or not report.parent.is_dir():
        parser.error('出力先の親ディレクトリがありません。')
    try:
        _convert_one(source, output, report, args.title, args.author)
    except Exception as exc:
        parser.exit(1, f'変換失敗 ({type(exc).__name__}): {exc}\n')


if __name__ == '__main__':
    main()
