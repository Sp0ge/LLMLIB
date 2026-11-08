import io
import unittest
import zipfile
import tarfile
from pathlib import Path
import tempfile
from indexer import chunks, extract, search


class IndexerTest(unittest.TestCase):
    def test_text_and_search(self):
        blocks, warnings = extract('документ.txt', 'Первая строка\nВторая строка про космос'.encode())
        self.assertEqual(warnings, [])
        hits = search(chunks(blocks), 'космос')
        self.assertEqual(len(hits), 1)
        self.assertIn('строки 1–2', hits[0]['locator'])
        self.assertEqual(search(chunks(blocks), 'неизвестно'), [])

    def test_search_finds_descriptive_filename(self):
        records = [
            {'id': '1', 'path': 'архив/трудовой_договор.pdf', 'text': 'Документ подписан сторонами.'},
            {'id': '2', 'path': 'архив/список_покупок.txt', 'text': 'Молоко и хлеб.'},
        ]
        self.assertEqual(search(records, 'трудовой договор')[0]['id'], '1')

    def test_zip_disallows_traversal_and_reads_safe_entry(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('../escape.txt', 'опасно')
            archive.writestr('folder/info.txt', 'нормальный текст')
        blocks, warnings = extract('archive.zip', buffer.getvalue())
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['path'], 'archive.zip!/folder/info.txt')
        self.assertTrue(any('небезопасный' in w for w in warnings))

    def test_zip_ratio_limit(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('bomb.txt', 'A' * 500_000)
        blocks, warnings = extract('bomb.zip', buffer.getvalue())
        self.assertFalse(blocks)
        self.assertTrue(warnings)

    def test_unknown_file_reported(self):
        blocks, warnings = extract('unknown.bin', b'123')
        self.assertEqual(blocks, [])
        self.assertTrue(warnings)

    def test_tar_skips_links_and_unsafe_paths(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            contents = b'safe data'
            info = tarfile.TarInfo('safe.txt'); info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
            link = tarfile.TarInfo('link'); link.type = tarfile.SYMTYPE; link.linkname = '../../outside'
            archive.addfile(link)
            bad = tarfile.TarInfo('../bad.txt'); bad.size = len(contents)
            archive.addfile(bad, io.BytesIO(contents))
        blocks, warnings = extract('data.tgz', buffer.getvalue())
        self.assertEqual(len(blocks), 1)
        self.assertGreaterEqual(len(warnings), 2)

    def test_subtitle_timecode(self):
        source = b'1\n00:01:23,450 --> 00:01:26,000\nHello Mars\n\n'
        blocks, warnings = extract('video.srt', source)
        self.assertEqual(warnings, [])
        result = chunks(blocks)
        self.assertEqual(result[0]['timestamp_ms'], 83450)
        self.assertIn('Hello Mars', result[0]['text'])

    def test_office_formats(self):
        from docx import Document
        from pptx import Presentation
        from openpyxl import Workbook
        doc = Document(); doc.add_paragraph('Документ')
        slides = Presentation(); slide = slides.slides.add_slide(slides.slide_layouts[1]); slide.shapes.title.text = 'Презентация'
        book = Workbook(); book.active['A1'] = 'Таблица'
        for name, obj, expected in [('a.docx', doc, 'Документ'), ('a.pptx', slides, 'Презентация'), ('a.xlsx', book, 'Таблица')]:
            with self.subTest(name=name):
                data = io.BytesIO(); obj.save(data)
                blocks, warnings = extract(name, data.getvalue())
                self.assertEqual(warnings, [])
                self.assertIn(expected, '\n'.join(block['text'] for block in blocks))

    def test_7z_reads_supported_files_without_extracting_to_disk(self):
        import py7zr
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'notes.txt'
            source.write_text('Текст из безопасного архива', encoding='utf-8')
            archive_data = io.BytesIO()
            with py7zr.SevenZipFile(archive_data, 'w') as archive:
                archive.write(source, 'folder/notes.txt')
            blocks, warnings = extract('notes.7z', archive_data.getvalue())
        self.assertFalse(warnings)
        self.assertIn('Текст из безопасного архива', '\n'.join(block['text'] for block in blocks))

    def test_html_and_rtf_ignore_markup_and_keep_text(self):
        html_blocks, _ = extract('page.html', b'<html><head>Hidden title</head><body>Hello<script>secret()</script></body></html>')
        rtf_blocks, _ = extract('notes.rtf', br'{\rtf1\ansi Hello\par world}')
        self.assertIn('Hello', html_blocks[0]['text'])
        self.assertNotIn('secret', html_blocks[0]['text'])
        self.assertIn('Hello\nworld', rtf_blocks[0]['text'])


if __name__ == '__main__': unittest.main()
