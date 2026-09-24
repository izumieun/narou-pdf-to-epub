from contextlib import redirect_stdout
from io import StringIO
import tempfile
from pathlib import Path
from unittest import TestCase, main, mock
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from narou_epub import _column_parts, build_epub, extract, main as convert_main, validate_epub, XHTML


def chars(text, x, *, bold=False, size=14, top=20):
    return [{'text': value, 'matrix': (1, 0, 0, 1, x, 150 - i * size),
             'top': top + i * size, 'bottom': top + (i + 1) * size,
             'x0': x, 'size': size,
             'fontname': 'Novel-Bold' if bold else 'Novel-Regular'}
            for i, value in enumerate(text)]


def horizontal_chars(text, center, *, size=12, top=180):
    step = size * .62
    start = center - step * (len(text) - 1) / 2
    return [{'text': value, 'matrix': (1, 0, 0, 1, start + i * step, 20),
             'top': top, 'bottom': top + size, 'x0': start + i * step,
             'size': size, 'fontname': 'Novel-Regular'}
            for i, value in enumerate(text)]


class FakePage:
    def __init__(self, number, columns, footer=None, cover=None):
        self.page_number = number
        self.height = 200
        self.width = 300
        self.chars = [c for column in columns for c in column]
        if footer is not None:
            self.chars += horizontal_chars(footer, self.width / 2)
        self.cover = cover

    def extract_text(self):
        return self.cover or ''


class FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class ConverterTests(TestCase):
    def test_reading_order_headings_blanks_page_boundary_and_digits(self):
        pages = [FakePage(1, [], cover='題名\n著者\n出版社'),
                 FakePage(2, [chars('第１話', 200, bold=True),
                              chars('　今日は123456', 160), chars('続く', 140),
                              chars('　次あいうえおかきくけ', 100), chars('太字本文', 80, bold=True)], footer='1'),
                 FakePage(3, [chars('　翌日ですあいうえお', 160), chars('４５', 140)], footer='2')]
        with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
            title, author, sections, diagnostics = extract(Path('sample.pdf'))
        self.assertEqual((title, author), ('題名', '著者'))
        self.assertEqual([s.title for s in sections], ['題名', '第１話'])
        self.assertEqual([p.text for p in sections[1].paragraphs],
                         ['　今日は123456続く', '　次あいうえおかきくけ太字本文', '　翌日ですあいうえお４５'])
        self.assertEqual(sections[1].paragraphs[1].blank_before, 1)
        self.assertEqual([x['number'] for x in diagnostics['removed_page_numbers']], ['1', '2'])

    def test_short_aligned_columns_keep_list_line_breaks(self):
        entries = ['項目一：説明文一', '項目二：説明文二',
                   '項目三：説明文三', '項目四：説明文四']
        list_page = FakePage(2, [chars('人物一覧', 260, bold=True),
                                 *[chars(entry, 200 - i * 20, top=80)
                                   for i, entry in enumerate(entries)]])
        list_page.height = 600
        prose_page = FakePage(3, [chars('　' + '文章' * 17, 200, top=20),
                                  chars('続き', 180, top=20)])
        prose_page.height = 600
        pages = [FakePage(1, [], cover='題名\n著者'), list_page, prose_page]
        with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
            title, author, sections, _ = extract(Path('sample.pdf'))
        self.assertEqual([p.text for p in sections[1].paragraphs[:4]], entries)
        self.assertEqual(sections[1].paragraphs[4].text, '　' + '文章' * 17 + '続き')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'book.epub'
            build_epub(target, title, author, sections)
            with ZipFile(target) as archive:
                chapter = ET.fromstring(archive.read('OEBPS/chapter-002.xhtml'))
                lines = chapter.findall(f'.//{{{XHTML}}}p')
                self.assertEqual([''.join(line.itertext()) for line in lines[:4]], entries)

    def test_ruby_attaches_to_base_text(self):
        base = chars('親文字', 100)
        ruby = chars('おや', 111, size=7, top=20)
        diagnostics = {'ruby_count': 0, 'ruby_ambiguous': [], 'unassigned_small_text': []}
        parts = _column_parts(base, [ruby], 100, 14, diagnostics, 1)
        self.assertEqual(''.join(p[0] for p in parts), '親文字')
        self.assertEqual(diagnostics['ruby_count'], 1)
        self.assertTrue(any(annotation == 'おや' for _, annotation in parts))

    def test_ruby_on_wrapped_column_prefers_its_base_and_excludes_next_glyph(self):
        # The preceding column is fractionally closer to the ruby at the same height.
        ruby = chars('るび', 111.34, size=7, top=48.66)
        pages = [FakePage(1, [], cover='題名\n著者'),
                 FakePage(2, [chars('あいうえお', 122.60), chars('甲乙丙丁戊己庚', 100), ruby])]
        with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
            title, author, sections, diagnostics = extract(Path('sample.pdf'))
        parts = sections[0].paragraphs[-1].parts
        self.assertIn(('丙', 'るび'), parts)
        self.assertEqual([base for base, annotation in parts if annotation], ['丙'])
        self.assertEqual(diagnostics['ruby_count'], 1)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'book.epub'
            build_epub(target, title, author, sections)
            with ZipFile(target) as archive:
                chapter = ET.fromstring(archive.read('OEBPS/chapter-001.xhtml'))
                ruby_nodes = chapter.findall(f'.//{{{XHTML}}}ruby')
                self.assertEqual([(node.text, node.findtext(f'{{{XHTML}}}rt'))
                                  for node in ruby_nodes], [('丙', 'るび')])

    def test_page_number_is_removed_when_small_body_dots_reach_footer_area(self):
        body = chars('　これは検証用の文です。点を残します。', 100)
        page = FakePage(2, [body], footer='3661')
        page.chars += chars('・・・', 111, size=7, top=166)
        pages = [FakePage(1, [], cover='題名\n著者'), page]
        with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
            _, _, sections, diagnostics = extract(Path('sample.pdf'))
        annotations = [annotation for section in sections for paragraph in section.paragraphs
                       for _, annotation in paragraph.parts if annotation]
        self.assertFalse(any(annotation.isdecimal() for annotation in annotations))
        self.assertEqual(diagnostics['removed_page_numbers'], [{'page': 2, 'number': '3661'}])

    def test_shifted_page_numbers_on_chapter_openings_are_not_ruby(self):
        for number in ('3710', '3724'):
            with self.subTest(number=number):
                physical_page = int(number) + 1
                prior = FakePage(physical_page - 1, [chars('　前のページの本文です。', 100)],
                                 footer=str(int(number) - 1))
                opening = FakePage(physical_page,
                                   [chars('「……」仮の見本文が続きます。', 100)])
                opening.chars += horizontal_chars(number, opening.width / 2 - 19)
                pages = [FakePage(1, [], cover='題名\n著者'), prior, opening]
                with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
                    _, _, sections, diagnostics = extract(Path('sample.pdf'))
                annotations = [annotation for section in sections for paragraph in section.paragraphs
                               for _, annotation in paragraph.parts if annotation]
                self.assertFalse(any(annotation.isdecimal() for annotation in annotations))
                self.assertEqual(diagnostics['removed_page_numbers'],
                                 [{'page': physical_page - 1, 'number': str(int(number) - 1)},
                                  {'page': physical_page, 'number': number}])

    def test_first_shifted_footer_uses_following_page_as_reference(self):
        first = FakePage(2, [chars('　本文が始まります。', 100)])
        first.chars += horizontal_chars('1', first.width / 2 - 30)
        second = FakePage(3, [chars('　次のページです。', 100)], footer='2')
        pages = [FakePage(1, [], cover='題名\n著者'), first, second]
        with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
            _, _, _, diagnostics = extract(Path('sample.pdf'))
        self.assertEqual(diagnostics['removed_page_numbers'],
                         [{'page': 2, 'number': '1'}, {'page': 3, 'number': '2'}])

    def test_sparse_page_uses_footer_sequence_when_ruby_is_most_common_size(self):
        prior = FakePage(5844, [chars('　前のページです。', 100)], footer='5843')
        sparse = FakePage(5845, [chars('本文です。続', 100),
                                 chars('ふりがながたくさん', 111, size=7)], footer='5844')
        pages = [FakePage(1, [], cover='題名\n著者'), prior, sparse]
        with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
            _, _, sections, diagnostics = extract(Path('sample.pdf'))
        self.assertIn({'page': 5845, 'number': '5844'},
                      diagnostics['removed_page_numbers'])
        self.assertIn('本文です。続', ''.join(p.text for section in sections
                                           for p in section.paragraphs))

    def test_footer_is_removed_when_it_matches_body_font_size(self):
        prior = FakePage(2, [chars('　本文です。', 100)], footer='1')
        final = FakePage(3, [chars('　後書きです。', 100, size=12)], footer='2')
        pages = [FakePage(1, [], cover='題名\n著者'), prior, final]
        with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
            _, _, sections, diagnostics = extract(Path('sample.pdf'))
        self.assertIn({'page': 3, 'number': '2'}, diagnostics['removed_page_numbers'])
        self.assertNotIn('2', ''.join(paragraph.text for section in sections
                                      for paragraph in section.paragraphs))

    def test_horizontal_colophon_does_not_turn_url_digits_into_ruby(self):
        prior = FakePage(2, [chars('　本文です。', 100)], footer='1')
        rows = [horizontal_chars('0123456789ABCDEFGHIJ', 150, top=20 + i * 15)
                for i in range(4)]
        colophon = FakePage(3, rows, footer='2', cover='出版情報\nURL: example.invalid/book42')
        pages = [FakePage(1, [], cover='題名\n著者'), prior, colophon]
        with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
            _, _, sections, diagnostics = extract(Path('sample.pdf'))
        self.assertIn(3, diagnostics['horizontal_pages'])
        self.assertIn('URL: example.invalid/book42', [p.text for p in sections[-1].paragraphs])
        self.assertEqual(diagnostics['ruby_count'], 0)

    def test_epub_navigation_and_spine(self):
        pages = [FakePage(1, [], cover='題名\n著者'),
                 FakePage(2, [chars('第１話', 200, bold=True), chars('　本文です', 160)])]
        with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
            title, author, sections, _ = extract(Path('sample.pdf'))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'book.epub'
            build_epub(target, title, author, sections)
            validate_epub(target, len(sections))
            with ZipFile(target) as z:
                nav = ET.fromstring(z.read('OEBPS/nav.xhtml'))
                self.assertEqual(len(nav.findall(f'.//{{{XHTML}}}a')), 2)
                self.assertIn(b'writing-mode: vertical-rl', z.read('OEBPS/style.css'))

    def test_cover_only_pdf_is_not_successful(self):
        pages = [FakePage(1, [], cover='題名\n著者'), FakePage(2, [])]
        with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
            with self.assertRaisesRegex(ValueError, '縦書き本文を抽出できません'):
                extract(Path('sample.pdf'))

    def test_batch_converts_pdfs_and_skips_existing_outputs(self):
        pages = [FakePage(1, [], cover='題名\n著者'),
                 FakePage(2, [chars('　本文です', 160)])]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input'
            target = Path(directory) / 'output'
            source.mkdir()
            (source / 'a.pdf').write_bytes(b'first')
            (source / 'b.PDF').write_bytes(b'second')
            (source / 'note.txt').write_text('ignore', encoding='utf-8')
            with mock.patch('narou_epub.pdfplumber.open', side_effect=lambda _: FakePdf(pages)):
                with redirect_stdout(StringIO()):
                    convert_main([str(source), str(target)])
            self.assertEqual(sorted(p.name for p in target.iterdir()),
                             ['a.epub', 'a.report.json', 'b.epub', 'b.report.json'])
            for name in ('a', 'b'):
                validate_epub(target / f'{name}.epub', 1)
            with mock.patch('narou_epub.pdfplumber.open', side_effect=AssertionError('converted again')):
                with redirect_stdout(StringIO()) as output:
                    convert_main([str(source), str(target)])
            self.assertIn('スキップ 2件', output.getvalue())
            updated_pages = [FakePage(1, [], cover='題名\n著者'),
                             FakePage(2, [chars('　変更本文', 160)])]
            with mock.patch('narou_epub.pdfplumber.open',
                            side_effect=lambda _: FakePdf(updated_pages)) as opened:
                with redirect_stdout(StringIO()):
                    convert_main([str(source), str(target), '--overwrite'])
            self.assertEqual(opened.call_count, 2)
            with ZipFile(target / 'a.epub') as archive:
                self.assertIn('変更本文', archive.read('OEBPS/chapter-001.xhtml').decode('utf-8'))

    def test_single_pdf_cli_still_accepts_an_epub_path(self):
        pages = [FakePage(1, [], cover='題名\n著者'),
                 FakePage(2, [chars('　本文です', 160)])]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'book.pdf'
            target = Path(directory) / 'renamed.epub'
            source.write_bytes(b'sample')
            with mock.patch('narou_epub.pdfplumber.open', return_value=FakePdf(pages)):
                with redirect_stdout(StringIO()):
                    convert_main([str(source), str(target)])
            validate_epub(target, 1)
            self.assertTrue(target.with_suffix('.report.json').is_file())

    def test_batch_failure_does_not_prevent_next_pdf(self):
        pages = [FakePage(1, [], cover='題名\n著者'),
                 FakePage(2, [chars('　本文です', 160)])]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input'
            target = Path(directory) / 'output'
            source.mkdir()
            (source / 'a.pdf').write_bytes(b'invalid')
            (source / 'b.pdf').write_bytes(b'valid')

            def open_pdf(path):
                if path.name == 'a.pdf':
                    raise ValueError('invalid PDF')
                return FakePdf(pages)

            with mock.patch('narou_epub.pdfplumber.open', side_effect=open_pdf):
                with redirect_stdout(StringIO()) as output:
                    with mock.patch('sys.stderr', new_callable=StringIO) as errors:
                        with self.assertRaises(SystemExit) as failure:
                            convert_main([str(source), str(target)])
            self.assertEqual(failure.exception.code, 1)
            self.assertIn('a.pdf', errors.getvalue())
            self.assertIn('成功 1件', output.getvalue())
            validate_epub(target / 'b.epub', 1)
            self.assertFalse((target / 'a.epub').exists())


if __name__ == '__main__':
    main()
