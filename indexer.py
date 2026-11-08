"""Bounded, offline text extraction and an extractive search index."""
from __future__ import annotations
import hashlib
import io
import json
import math
import re
import tarfile
import gzip
import bz2
import lzma
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from collections import Counter
from pathlib import PurePosixPath

MAX_ARCHIVE_MEMBERS = 1000
MAX_UNPACKED = 20 * 1024 * 1024
MAX_MEMBER = 5 * 1024 * 1024
MAX_DEPTH = 2
MAX_TEXT = 2_000_000
TEXT_EXT = {
    '.txt', '.md', '.markdown', '.rst', '.csv', '.tsv', '.json', '.jsonl', '.jsonc', '.json5',
    '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.log', '.env',
    '.py', '.pyi', '.ipynb', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.vue', '.svelte',
    '.java', '.kt', '.kts', '.scala', '.c', '.h', '.cpp', '.hpp', '.cc', '.hh', '.cs', '.vb',
    '.fs', '.fsx', '.go', '.rs', '.swift', '.m', '.mm', '.sql', '.ps1', '.psm1', '.psd1',
    '.bat', '.cmd', '.sh', '.bash', '.zsh', '.fish', '.html', '.htm', '.xhtml', '.css',
    '.scss', '.sass', '.less', '.srt', '.vtt', '.tex', '.make', '.mk', '.makefile', '.dockerfile',
    '.gemfile', '.rakefile', '.podfile', '.justfile', '.gradle', '.properties', '.graphql',
    '.proto', '.lua', '.rb', '.php', '.pl', '.ex', '.exs', '.erl', '.hrl', '.hs', '.lhs',
    '.elm', '.clj', '.cljs', '.edn', '.r', '.jl', '.dart', '.tf', '.hcl', '.asm', '.s',
    '.cbl', '.cob', '.vbs',
}


class _VisibleHTML(HTMLParser):
    """Collect visible text while dropping scripts, styles, and document metadata."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {'script', 'style', 'noscript', 'svg', 'head'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag.lower() in {'script', 'style', 'noscript', 'svg', 'head'} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def _xml_text(raw: bytes) -> str:
    if len(raw) > MAX_MEMBER:
        raise ValueError('XML-элемент превышает лимит')
    root = ET.fromstring(raw)
    return '\n'.join(value.strip() for value in root.itertext() if value and value.strip())


def _zip_text_blocks(name: str, data: bytes, suffix: str) -> tuple[list[dict], list[str]]:
    """Read selected XML/HTML members from ODF and EPUB archives without extracting them."""
    blocks, warnings, used = [], [], 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS or sum(m.file_size for m in members) > MAX_UNPACKED:
                return [], [f'{name}: превышены лимиты контейнера']
            for member in members:
                path = member.filename.replace('\\', '/')
                if member.is_dir():
                    continue
                if path.startswith('/') or '..' in PurePosixPath(path).parts or member.file_size > MAX_MEMBER or member.file_size > 100 * max(member.compress_size, 1):
                    warnings.append(f'{name}!/{path}: небезопасный путь или превышен лимит')
                    continue
                lower = path.lower()
                selected = ((suffix == '.odt' and lower == 'content.xml') or
                            (suffix == '.ods' and lower.startswith('content.xml')) or
                            (suffix == '.odp' and lower == 'content.xml') or
                            (suffix == '.epub' and lower.endswith(('.xhtml', '.html', '.htm'))))
                if not selected:
                    continue
                if used + member.file_size > MAX_UNPACKED:
                    warnings.append(f'{name}: превышен общий лимит чтения контейнера')
                    break
                raw = archive.read(member)
                used += len(raw)
                if suffix == '.epub':
                    parser = _VisibleHTML(); parser.feed(raw.decode('utf-8', 'replace'))
                    text = '\n'.join(parser.parts)
                else:
                    text = _xml_text(raw)
                if text:
                    blocks.append({'path': f'{name}!/{path}', 'locator': 'раздел', 'text': text[:MAX_TEXT]})
    except Exception as exc:
        return [], [f'{name}: ошибка контейнера: {type(exc).__name__}: {exc}']
    return blocks, warnings or ([] if blocks else [f'{name}: текстовые материалы не найдены'])


def _rtf_text(data: bytes) -> str:
    raw = data.decode('latin-1', 'replace')
    raw = re.sub(r"\\'[0-9a-fA-F]{2}", lambda m: bytes([int(m.group(0)[2:], 16)]).decode('cp1252', 'replace'), raw)
    raw = re.sub(r'\\u(-?\d+)\??', lambda m: chr(int(m.group(1)) & 0xffff), raw)
    raw = re.sub(r'\\(par|line|tab)\b ?', lambda m: {'par': '\n', 'line': '\n', 'tab': '\t'}[m.group(1)], raw)
    raw = re.sub(r'\\[a-zA-Z]+-?\d* ?', '', raw)
    raw = re.sub(r'\\[^a-zA-Z]', '', raw)
    return re.sub(r'[{}]', '', raw).strip()


def extract_7z(name: str, data: bytes, depth: int) -> tuple[list[dict], list[str]]:
    if depth >= MAX_DEPTH:
        return [], [f'{name}: превышена глубина архивов']
    try:
        from py7zr import Py7zIO, SevenZipFile, WriterFactory

        class LimitedBuffer(Py7zIO):
            def __init__(self): self.buffer = io.BytesIO()
            def write(self, value):
                if self.buffer.tell() + len(value) > MAX_MEMBER:
                    raise ValueError('элемент 7z превышает лимит')
                return self.buffer.write(value)
            def read(self, size=None): return self.buffer.read(0 if size is None else size)
            def seek(self, offset, whence=0): return self.buffer.seek(offset, whence)
            def flush(self): return self.buffer.flush()
            def size(self): return len(self.buffer.getvalue())

        class LimitedFactory(WriterFactory):
            def __init__(self): self.products = {}
            def create(self, filename):
                product = LimitedBuffer(); self.products[filename] = product; return product

        blocks, warnings, total = [], [], 0
        with SevenZipFile(io.BytesIO(data), mode='r', max_extract_size=MAX_UNPACKED) as archive:
            members = archive.list()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                return [], [f'{name}: превышен лимит элементов архива']
            targets = []
            for member in members:
                path = member.filename.replace('\\', '/')
                parts = PurePosixPath(path).parts
                if member.is_directory:
                    continue
                if (path.startswith('/') or '..' in parts or re.match(r'^[A-Za-z]:', path) or
                        getattr(member, 'is_symlink', False) or getattr(member, 'is_junction', False) or
                        getattr(member, 'is_socket', False)):
                    warnings.append(f'{name}!/{path}: небезопасный путь или ссылка'); continue
                size = member.uncompressed or 0
                compressed = member.compressed or 0
                if size > MAX_MEMBER or size > 100 * max(compressed, 1) or total + size > MAX_UNPACKED:
                    warnings.append(f'{name}!/{path}: превышен лимит размера или сжатия'); continue
                suffix = PurePosixPath(path.lower()).suffix
                if suffix not in TEXT_EXT | {'.pdf', '.docx', '.xlsx', '.pptx', '.odt', '.ods', '.odp', '.epub', '.zip', '.tar', '.tgz', '.gz', '.bz2', '.xz', '.lzma', '.7z'}:
                    continue
                total += size; targets.append(path)
            factory = LimitedFactory()
            if targets:
                archive.extract(targets=targets, factory=factory)
            for path in targets:
                product = factory.products.get(path)
                if product is None:
                    warnings.append(f'{name}!/{path}: не удалось получить содержимое'); continue
                child, child_warnings = extract(f'{name}!/{path}', product.buffer.getvalue(), depth + 1)
                blocks.extend(child); warnings.extend(child_warnings)
        return blocks, warnings or ([] if blocks else [f'{name}: поддерживаемые файлы не найдены'])
    except Exception as exc:
        return [], [f'{name}: ошибка 7z: {type(exc).__name__}: {exc}']


def extract(name: str, data: bytes, depth: int = 0) -> tuple[list[dict], list[str]]:
    basename = PurePosixPath(name.lower().split('!/')[-1]).name
    suffix = PurePosixPath(basename).suffix
    if not suffix and basename in {'makefile', 'dockerfile', 'gemfile', 'rakefile', 'podfile', 'justfile', '.env'}:
        suffix = '.' + basename
    if basename.startswith('.env.'):
        suffix = '.env'
    if suffix in {'.odt', '.ods', '.odp', '.epub'}:
        return _zip_text_blocks(name, data, suffix)
    if suffix == '.zip':
        return extract_zip(name, data, depth)
    if suffix == '.7z':
        return extract_7z(name, data, depth)
    if suffix in {'.tar', '.tgz', '.tbz', '.tbz2', '.txz', '.gz', '.bz2', '.xz', '.lzma'}:
        return extract_tar_or_gzip(name, data, depth)
    try:
        if suffix in {'.docx', '.xlsx', '.pptx'}:
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                members = package.infolist()
                if len(members) > MAX_ARCHIVE_MEMBERS or sum(m.file_size for m in members) > MAX_UNPACKED:
                    raise ValueError('превышены лимиты Office-контейнера')
                if any(m.file_size > MAX_MEMBER or m.file_size > 100 * max(m.compress_size, 1) for m in members):
                    raise ValueError('превышены лимиты сжатия Office-контейнера')
        if suffix in TEXT_EXT:
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                if data.startswith((b'\xff\xfe', b'\xfe\xff')):
                    text = data.decode('utf-16')
                else:
                    return [], [f'{name}: неподдерживаемая кодировка']
            if suffix in {'.html', '.htm', '.xhtml'}:
                parser = _VisibleHTML(); parser.feed(text)
                text = '\n'.join(parser.parts)
            elif suffix == '.ipynb':
                try:
                    notebook = json.loads(text)
                    cells = []
                    for cell in notebook.get('cells', []):
                        source = ''.join(cell.get('source', [])) if isinstance(cell.get('source'), list) else cell.get('source', '')
                        if source: cells.append(f"[{cell.get('cell_type', 'cell')}]\n{source}")
                        for output in cell.get('outputs', []):
                            plain = output.get('text', [])
                            if plain: cells.append('[output]\n' + ''.join(plain if isinstance(plain, list) else [plain]))
                    text = '\n\n'.join(cells)
                except (ValueError, TypeError, AttributeError):
                    return [], [f'{name}: некорректный Jupyter Notebook']
            return [{'path': name, 'locator': 'текст', 'text': text[:MAX_TEXT]}], ([f'{name}: текст сокращён'] if len(text) > MAX_TEXT else [])
        if suffix == '.rtf':
            text = _rtf_text(data)
            return [{'path': name, 'locator': 'документ', 'text': text[:MAX_TEXT]}], ([f'{name}: текст сокращён'] if len(text) > MAX_TEXT else [])
        if suffix == '.pdf':
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            if len(reader.pages) > 500:
                raise ValueError('превышен предел 500 страниц')
            blocks = []
            total = 0
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ''
                remaining = MAX_TEXT - total
                if remaining <= 0:
                    break
                blocks.append({'path': name, 'locator': f'страница {i + 1}', 'text': text[:remaining]})
                total += len(text[:remaining])
            return blocks, ([] if any(x['text'].strip() for x in blocks) else [f'{name}: текстовый слой отсутствует'])
        if suffix == '.docx':
            from docx import Document
            doc = Document(io.BytesIO(data))
            text = '\n'.join(p.text for p in doc.paragraphs)
            for table in doc.tables:
                text += '\n' + '\n'.join('\t'.join(c.text for c in row.cells) for row in table.rows)
            return [{'path': name, 'locator': 'документ', 'text': text[:MAX_TEXT]}], []
        if suffix == '.pptx':
            from pptx import Presentation
            pres = Presentation(io.BytesIO(data))
            blocks = []
            for i, slide in enumerate(pres.slides):
                if i >= 500: break
                text = '\n'.join(shape.text for shape in slide.shapes if shape.has_text_frame)
                blocks.append({'path': name, 'locator': f'слайд {i + 1}', 'text': text[:MAX_TEXT]})
            return blocks, []
        if suffix == '.xlsx':
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
            blocks = []
            try:
                for sheet in wb.worksheets[:100]:
                    lines = []
                    length = 0
                    for i, row in enumerate(sheet.iter_rows(max_col=100, values_only=True)):
                        if i >= 10000: break
                        line = '\t'.join(str(cell) if cell is not None else '' for cell in row).rstrip('\t')
                        lines.append(line); length += len(line)
                        if length >= MAX_TEXT: break
                    blocks.append({'path': name, 'locator': f'лист {sheet.title}', 'text': '\n'.join(lines)[:MAX_TEXT]})
            finally:
                wb.close()
            return blocks, []
        return [], [f'{name}: формат не поддерживается для извлечения текста']
    except Exception as exc:
        return [], [f'{name}: ошибка обработки: {type(exc).__name__}: {exc}']


def extract_zip(name: str, data: bytes, depth: int) -> tuple[list[dict], list[str]]:
    if depth >= MAX_DEPTH:
        return [], [f'{name}: превышена глубина архивов']
    blocks, warnings = [], []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                return [], [f'{name}: превышен лимит элементов архива']
            used = 0
            for item in members:
                path = item.filename.replace('\\', '/')
                parts = PurePosixPath(path).parts
                mode = (item.external_attr >> 16) & 0o170000
                if item.is_dir(): continue
                if path.startswith('/') or '..' in parts or re.match(r'^[A-Za-z]:', path) or mode == 0o120000:
                    warnings.append(f'{name}!/{path}: небезопасный путь или ссылка'); continue
                if item.flag_bits & 1:
                    warnings.append(f'{name}!/{path}: зашифрованный элемент пропущен'); continue
                if item.file_size > MAX_MEMBER or item.file_size > 100 * max(item.compress_size, 1):
                    warnings.append(f'{name}!/{path}: превышен лимит размера или сжатия'); continue
                if used + item.file_size > MAX_UNPACKED:
                    warnings.append(f'{name}: превышен общий лимит распаковки'); break
                with archive.open(item) as stream:
                    content = stream.read(MAX_MEMBER + 1)
                if len(content) > MAX_MEMBER:
                    warnings.append(f'{name}!/{path}: превышен фактический лимит'); continue
                used += len(content)
                sub, sub_warnings = extract(f'{name}!/{path}', content, depth + 1)
                blocks.extend(sub); warnings.extend(sub_warnings)
    except Exception as exc:
        warnings.append(f'{name}: ошибка архива: {type(exc).__name__}: {exc}')
    return blocks, warnings


def extract_tar_or_gzip(name, data, depth):
    if depth >= MAX_DEPTH:
        return [], [f'{name}: превышена глубина архивов']
    if name.lower().endswith('.gz') and not name.lower().endswith('.tar.gz'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                raw = gz.read(MAX_MEMBER + 1)
            if len(raw) > MAX_MEMBER or len(raw) > 100 * max(len(data), 1):
                raise ValueError('превышен лимит распаковки')
            return extract(name + '!/' + PurePosixPath(name[:-3]).name, raw, depth + 1)
        except Exception as exc:
            return [], [f'{name}: ошибка gzip: {exc}']
    if name.lower().endswith(('.bz2', '.xz', '.lzma')) and not name.lower().endswith(('.tar.bz2', '.tar.xz', '.tar.lzma')):
        try:
            if name.lower().endswith('.bz2'):
                decoder = bz2.BZ2Decompressor(); raw = decoder.decompress(data, max_length=MAX_MEMBER + 1)
                finished = decoder.eof
            else:
                decoder = lzma.LZMADecompressor(memlimit=64 * 1024 * 1024)
                raw = decoder.decompress(data, max_length=MAX_MEMBER + 1); finished = decoder.eof
            if len(raw) > MAX_MEMBER or not finished or len(raw) > 100 * max(len(data), 1):
                raise ValueError('превышен лимит распаковки')
            return extract(name.rsplit('.', 1)[0], raw, depth + 1)
        except Exception as exc:
            return [], [f'{name}: ошибка распаковки: {exc}']
    blocks, warnings, used, count = [], [], 0, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
            for member in archive:
                count += 1
                if count > MAX_ARCHIVE_MEMBERS:
                    warnings.append(f'{name}: превышен лимит элементов'); break
                path = member.name.replace('\\', '/')
                if path.startswith('/') or '..' in PurePosixPath(path).parts or re.match(r'^[A-Za-z]:', path):
                    warnings.append(f'{name}!/{path}: небезопасный путь'); continue
                if member.isdir(): continue
                if not member.isfile():
                    warnings.append(f'{name}!/{path}: ссылка или специальный файл пропущены'); continue
                if member.size > MAX_MEMBER or used + member.size > MAX_UNPACKED:
                    warnings.append(f'{name}!/{path}: превышен лимит размера'); continue
                stream = archive.extractfile(member)
                if stream is None: continue
                with stream: raw = stream.read(MAX_MEMBER + 1)
                if len(raw) > MAX_MEMBER:
                    warnings.append(f'{name}!/{path}: превышен фактический лимит'); continue
                used += len(raw)
                if used > 100 * max(len(data), 1):
                    warnings.append(f'{name}: превышен лимит сжатия'); break
                sub, warns = extract(name + '!/' + path, raw, depth + 1)
                blocks.extend(sub); warnings.extend(warns)
    except Exception as exc:
        warnings.append(f'{name}: ошибка архива: {exc}')
    return blocks, warnings


def chunks(blocks: list[dict]) -> list[dict]:
    out = []
    used = 0
    for block in blocks:
        remaining = MAX_TEXT - used
        if remaining <= 0: break
        text_source = block['text'][:remaining]
        used += len(text_source)
        if block['path'].lower().endswith(('.srt', '.vtt')):
            for cue in re.split(r'\r?\n\s*\r?\n', text_source):
                match = re.search(r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->', cue)
                if not match: continue
                hour, minute, second, ms = map(int, match.groups())
                stamp = ((hour * 60 + minute) * 60 + second) * 1000 + ms
                text = cue[match.end():].split('\n', 1)[-1].strip()
                if text:
                    out.append({'path': block['path'], 'locator': 'таймкод ' + match.group(0).split(' ')[0],
                                'text': text[:6000], 'timestamp_ms': stamp})
            continue
        lines = text_source.splitlines()
        for start in range(0, len(lines), 25):
            text = '\n'.join(lines[start:start + 30]).strip()
            if text:
                out.append({'path': block['path'], 'locator': f"{block['locator']}, строки {start + 1}–{min(start + 30, len(lines))}", 'text': text[:6000]})
    return out


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def search(index: list[dict], query: str, limit: int = 5) -> list[dict]:
    """BM25-style lexical retrieval including file paths and exact phrases."""
    raw_terms = re.findall(r'[^\W_]{2,}', query.casefold())
    terms = set(raw_terms)
    if not terms: return []
    def tokens(chunk):
        return re.findall(r'[^\W_]{2,}', (chunk.get('text', '') + ' ' + chunk.get('path', '')).casefold())
    tokenized = [tokens(chunk) for chunk in index]
    document_frequency = Counter(term for words in tokenized for term in set(words))
    average_length = max(1.0, sum(map(len, tokenized)) / max(1, len(tokenized)))
    scored = []
    total = len(index)
    phrase = ' '.join(raw_terms)
    for chunk, words in zip(index, tokenized):
        counts = Counter(words)
        length = len(words)
        score = 0.0
        for term in terms:
            frequency = counts[term]
            if not frequency: continue
            inverse = math.log(1 + (total - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            score += inverse * (frequency * 2.2) / (frequency + 1.2 * (0.25 + 0.75 * length / average_length))
            if term in chunk.get('path', '').casefold(): score += inverse * 1.5
        if phrase and phrase in chunk.get('text', '').casefold(): score += 3.0
        if score > 0: scored.append((score, chunk))
    return [c for _, c in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]]
