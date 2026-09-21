"""Small, layout-specific PDF extraction experiment; not a full converter.

Requires pdfplumber. Page numbers passed on the command line are one-based.
Outputs contain book text and must not be committed.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from statistics import median
import xml.etree.ElementTree as ET
import zipfile

import pdfplumber


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract(pages):
    paragraphs, headings, diagnostics = [], [], []
    previous = None
    started = False
    for page in pages:
        size = Counter(round(c['size'], 1) for c in page.chars).most_common(1)[0][0]
        groups = defaultdict(list)
        excluded = []
        for c in page.chars:
            if abs(c['size'] - size) > 0.2:
                excluded.append(c)
                continue
            groups[round(c['matrix'][4], 2)].append(c)
        columns = []
        for x, chars in sorted(groups.items(), reverse=True):
            chars.sort(key=lambda c: -c['matrix'][5])
            columns.append({'x': x, 'text': ''.join(c['text'] for c in chars),
                            'heading': all('Bold' in c['fontname'] for c in chars),
                            'page': page.page_number})
        xs = [c['x'] for c in columns if not c['heading']]
        pitch = median(a-b for a, b in zip(xs, xs[1:]))
        diagnostics.append({'page': page.page_number, 'body_size': size, 'pitch': pitch,
                            'excluded_characters': [{'text': c['text'], 'size': c['size'],
                                                     'top': c['top']} for c in excluded]})
        for col in columns:
            if col['heading']:
                headings.append(col)
                started = True
                previous = None
                continue
            if not started:
                continue
            gap = (previous['x'] - col['x']) if previous and previous['page'] == col['page'] else None
            blanks = max(0, round(gap/pitch)-1) if gap is not None else 0
            # Indentation marks paragraph starts in this sample. An unindented
            # column continues the preceding paragraph, including across pages.
            new = not paragraphs or col['text'].startswith(('\u3000', '「', '『')) or blanks
            if new:
                paragraphs.append({'text': col['text'], 'blank_before': blanks,
                                   'pages': [col['page']]})
            else:
                paragraphs[-1]['text'] += col['text']
                if col['page'] not in paragraphs[-1]['pages']:
                    paragraphs[-1]['pages'].append(col['page'])
            previous = col
    return paragraphs, headings, diagnostics


def reference_paragraphs(path, heading):
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith(('.html', '.xhtml')):
                continue
            root = ET.fromstring(z.read(name))
            elements = list(root.iter())
            for i, e in enumerate(elements):
                if e.tag.split('}')[-1] in ('h1', 'h2') and ''.join(e.itertext()) == heading:
                    return [''.join(p.itertext()) for p in elements[i+1:]
                            if p.tag.split('}')[-1] == 'p' and ''.join(p.itertext()).strip()]
    raise ValueError('Matching heading not found in reference EPUB')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--reference-epub', type=Path, required=True)
    parser.add_argument('--start-page', type=int, default=4)
    parser.add_argument('--end-page', type=int, default=5)
    parser.add_argument('--paragraphs', type=int, default=20)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if '.dev' in output.parts or output == args.pdf.resolve().parent:
        raise ValueError('Output must be outside .dev and input directory')
    sources = [args.pdf, args.reference_epub]
    hashes = {str(p): digest(p) for p in sources}
    output.mkdir(parents=True, exist_ok=True)
    with pdfplumber.open(args.pdf) as pdf:
        selected = pdf.pages[args.start_page-1:args.end_page]
        paragraphs, headings, diagnostics = extract(selected)
        for page in selected:
            page.to_image(resolution=110).save(output / f'page-{page.page_number}.png')
    result = paragraphs[:args.paragraphs]
    reference = reference_paragraphs(args.reference_epub, headings[0]['text'])[:args.paragraphs]
    diffs = [i for i, (a, b) in enumerate(zip(result, reference), 1) if a['text'] != b]
    blank_after = [i for i, p in enumerate(result) if p['blank_before']]
    report = {'scope': 'Selected pages only; indentation and font heuristics are provisional',
              'headings': headings, 'paragraphs': result, 'diagnostics': diagnostics,
              'comparison': {'count': len(result), 'reference_count': len(reference),
                             'text_mismatch_paragraphs': diffs,
                             'text_match': len(result) == len(reference) == args.paragraphs and not diffs,
                             'blank_after_paragraphs': blank_after},
              'source_hashes': hashes,
              'sources_unchanged': all(digest(p) == hashes[str(p)] for p in sources)}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = [headings[0]['text'], '']
    for p in result:
        lines.extend([''] * p['blank_before'])
        lines.append(p['text'])
    (output / 'extracted.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'comparison': report['comparison'], 'sources_unchanged': report['sources_unchanged'],
                      'diagnostics': diagnostics}, ensure_ascii=False, indent=2))
    if not report['comparison']['text_match'] or not report['sources_unchanged']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
