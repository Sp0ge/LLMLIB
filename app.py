"""Offline encrypted document browser: development build, no AI model bundled."""
import base64
import hmac
import html
import io
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt, QUrl, QThread, Signal, QTimer
from PySide6.QtGui import QImage, QImageReader, QPixmap, QTextCharFormat, QColor, QTextCursor
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QInputDialog,
    QLabel, QListWidget, QMainWindow, QMessageBox, QPushButton, QSplitter, QTextBrowser,
    QLineEdit, QSlider, QVBoxLayout, QWidget)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from indexer import chunks, extract, fingerprint, search
from vault import add_file, load, save
from storage import EncryptedStore
from ai import LocalModel, hybrid_search
from updater import CURRENT_VERSION, MANIFEST_URL, check_update, stage_asset, prepare_replacement, launch_update_helper


def storage_dir():
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable).resolve()
        if sys.platform == 'darwin' and executable.parent.name == 'MacOS' and executable.parent.parent.name == 'Contents' and executable.parent.parent.parent.suffix == '.app':
            return executable.parent.parent.parent.parent
        return executable.parent
    return None


def bundled_runtime():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    path = root / 'runtime' / ('llama-server.exe' if sys.platform == 'win32' else 'llama-server')
    return str(path) if path.is_file() else None


def password_prompt(parent, title):
    value, ok = QInputDialog.getText(parent, 'Мастер-пароль', title, QLineEdit.EchoMode.Password)
    return value if ok else None


class Viewer(QDialog):
    def __init__(self, title, text, needle='', parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 540)
        layout = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setPlainText(text)
        layout.addWidget(browser)
        if needle:
            words = [w for w in needle.split() if len(w) > 1]
            for word in words[:8]:
                cursor = browser.document().find(word)
                count = 0
                while not cursor.isNull() and count < 100:
                    fmt = QTextCharFormat(); fmt.setBackground(QColor('#fff09e'))
                    cursor.mergeCharFormat(fmt)
                    cursor = browser.document().find(word, cursor)
                    count += 1
        self.browser = browser


class VideoViewer(QDialog):
    def __init__(self, name, data, parent=None, start_ms=0):
        super().__init__(parent)
        self.setWindowTitle(name); self.resize(800, 550)
        layout = QVBoxLayout(self)
        video = QVideoWidget(); layout.addWidget(video, 1)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self); self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(video)
        self.buffer = QBuffer(self)
        self.buffer.setData(QByteArray(data)); self.buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        if start_ms:
            def seek(status):
                if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
                    self.player.setPosition(start_ms)
                    self.player.mediaStatusChanged.disconnect(seek)
            self.player.mediaStatusChanged.connect(seek)
        self.player.setSourceDevice(self.buffer, QUrl.fromLocalFile(name))
        slider = QSlider(Qt.Orientation.Horizontal); layout.addWidget(slider)
        self.player.durationChanged.connect(lambda value: slider.setRange(0, value))
        self.player.positionChanged.connect(slider.setValue)
        slider.sliderMoved.connect(self.player.setPosition)
        controls = QHBoxLayout(); layout.addLayout(controls)
        for label, callback in [('Воспроизвести', self.player.play), ('Пауза', self.player.pause)]:
            button = QPushButton(label); button.clicked.connect(callback); controls.addWidget(button)
        self.player.errorOccurred.connect(lambda *args: QMessageBox.warning(self, 'Видео', self.player.errorString()))

    def closeEvent(self, event):
        self.player.stop(); self.buffer.close(); super().closeEvent(event)


class AudioViewer(QDialog):
    def __init__(self, name, data, parent=None):
        super().__init__(parent)
        self.setWindowTitle(name); self.resize(520, 150)
        layout = QVBoxLayout(self); layout.addWidget(QLabel(name))
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self); self.player.setAudioOutput(self.audio)
        self.buffer = QBuffer(self); self.buffer.setData(QByteArray(data)); self.buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.player.setSourceDevice(self.buffer, QUrl.fromLocalFile(name))
        slider = QSlider(Qt.Orientation.Horizontal); layout.addWidget(slider)
        self.player.durationChanged.connect(lambda value: slider.setRange(0, value))
        self.player.positionChanged.connect(slider.setValue)
        slider.sliderMoved.connect(self.player.setPosition)
        controls = QHBoxLayout(); layout.addLayout(controls)
        for label, callback in [('Воспроизвести', self.player.play), ('Пауза', self.player.pause)]:
            button = QPushButton(label); button.clicked.connect(callback); controls.addWidget(button)
        self.player.errorOccurred.connect(lambda *args: QMessageBox.warning(self, 'Аудио', self.player.errorString()))

    def closeEvent(self, event):
        self.player.stop(); self.buffer.close(); super().closeEvent(event)


class ImageViewer(QDialog):
    def __init__(self, name, data, parent=None):
        super().__init__(parent)
        self.setWindowTitle(name); self.resize(800, 600)
        buffer = QBuffer(self); buffer.setData(QByteArray(data)); buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer); size = reader.size()
        if not size.isValid() or size.width() * size.height() > 40_000_000:
            raise ValueError('изображение повреждено или превышает лимит разрешения')
        image = reader.read()
        if image.isNull(): raise ValueError('не удалось прочитать изображение')
        layout = QVBoxLayout(self); label = QLabel()
        label.setPixmap(QPixmap.fromImage(image).scaled(760, 550, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(label)


class Worker(QThread):
    completed = Signal(object, object)

    def __init__(self, function, parent=None):
        super().__init__(parent); self.function = function

    def run(self):
        try: self.completed.emit(self.function(), None)
        except Exception as exc: self.completed.emit(None, str(exc))


class App(QMainWindow):
    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        self.disk = directory / 'disk.data'
        self.cache_path = directory / 'data.cache'
        self.password = None
        self.store = None
        self.data = {'version': 1, 'files': [], 'index': {}, 'warnings': {}}
        self.cache = {'messages': []}
        self.results = []
        self.last_query = ''
        self.runtime_path = None
        self.viewers = []
        self.worker = None
        self.update_worker = None
        self.setWindowTitle(f'Portable AI USB — beta {CURRENT_VERSION}')
        self.resize(1050, 700)
        main = QWidget(); self.setCentralWidget(main)
        layout = QVBoxLayout(main)
        self.status = QLabel('Локальный режим. Импортируйте две модели и выберите движок в настройках.')
        self.status.setWordWrap(True); layout.addWidget(self.status)
        bar = QHBoxLayout(); layout.addLayout(bar)
        for label, action in [('Добавить файлы', self.import_files), ('Удалить', self.delete_file),
                              ('Переиндексация', self.reindex), ('Обновить модели', self.models),
                              ('Настройки CPU', self.performance), ('Очистить историю', self.clear_history),
                              ('Обновить программу', self.updates),
                              ('Полный сброс', self.reset)]:
            button = QPushButton(label); button.clicked.connect(lambda checked=False, fn=action: fn()); bar.addWidget(button)
        split = QSplitter(); layout.addWidget(split, 1)
        left = QWidget(); left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel('Файлы'))
        self.files = QListWidget(); left_layout.addWidget(self.files)
        self.files.itemDoubleClicked.connect(self.open_file)
        split.addWidget(left)
        right = QWidget(); right_layout = QVBoxLayout(right)
        self.chat = QTextBrowser(); self.chat.setOpenExternalLinks(False); right_layout.addWidget(self.chat, 1)
        self.sources = QListWidget(); self.sources.itemDoubleClicked.connect(self.open_source)
        right_layout.addWidget(QLabel('Найденные источники — двойной щелчок открывает окно'))
        right_layout.addWidget(self.sources)
        row = QHBoxLayout(); right_layout.addLayout(row)
        self.query = QLineEdit(); self.query.setPlaceholderText('Введите вопрос или слова для поиска')
        self.query.returnPressed.connect(self.ask); row.addWidget(self.query, 1)
        button = QPushButton('Найти'); button.clicked.connect(self.ask); row.addWidget(button)
        split.addWidget(right); split.setSizes([250, 800])

    def start(self):
        import shutil

        if not self.directory.is_dir():
            QMessageBox.critical(self, 'Ошибка', 'Каталог не существует.')
            return False

        creating = not self.disk.exists()

        if creating and self.cache_path.exists():
            QMessageBox.critical(
                self,
                'Ошибка',
                'Найден data.cache без disk.data. Ничего не изменено.'
            )
            return False

        if creating:
            allowed = {'PortableAI.exe', 'PortableAI.app', 'PortableAI'}
            hidden_system = {'System Volume Information', '$RECYCLE.BIN'}
            unexpected = []

            for p in self.directory.iterdir():
                is_hidden = p.name.startswith('.') or p.name in hidden_system

                if sys.platform == 'win32':
                    try:
                        import ctypes
                        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(p))
                        if attributes != 0xFFFFFFFF:
                            is_hidden = is_hidden or bool(attributes & 0x2)
                    except Exception:
                        pass

                if p.name in allowed:
                    continue

                if is_hidden:
                    try:
                        if p.is_dir() and not p.is_symlink():
                            shutil.rmtree(p)
                        else:
                            p.unlink()
                    except OSError as exc:
                        QMessageBox.critical(
                            self,
                            'Ошибка очистки',
                            f'Не удалось удалить скрытый файл или каталог:\n'
                            f'{p.name}\n\n{exc}'
                        )
                        return False
                else:
                    unexpected.append(p.name)

            if unexpected:
                QMessageBox.warning(
                    self,
                    'Каталог не пуст',
                    'Уберите посторонние видимые файлы до настройки:\n'
                    + '\n'.join(unexpected[:20])
                )
                return False

        pw = password_prompt(
            self,
            'Создайте пароль (минимум 12 символов)'
            if creating else
            'Введите пароль для открытия'
        )

        if pw is None:
            return False

        if creating:
            if len(pw) < 12 or pw != password_prompt(self, 'Повторите пароль'):
                QMessageBox.warning(
                    self,
                    'Ошибка',
                    'Пароли не совпадают или слишком короткие.'
                )
                return False

            try:
                self.store = EncryptedStore(self.disk, pw, create=True)
                save(self.cache_path, pw, self.cache)
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    'Ошибка создания',
                    str(exc)
                )
                return False

        else:
            try:
                self.store = EncryptedStore(self.disk, pw)
                self.data = self.load_index()

                if not self.cache_path.exists():
                    save(self.cache_path, pw, self.cache)
                else:
                    try:
                        cache = load(self.cache_path, pw)

                        if not isinstance(cache, dict):
                            raise ValueError('Неверная структура контейнера')

                        self.cache = cache

                    except Exception as cache_error:
                        answer = QMessageBox.question(
                            self,
                            'Кэш истории повреждён',
                            'Файлы и индекс в disk.data открыты. '
                            'Создать новый пустой data.cache?\n'
                            'История чата будет потеряна.\n\n'
                            + str(cache_error)
                        )

                        if answer != QMessageBox.StandardButton.Yes:
                            self.store.close()
                            self.store = None
                            return False

                        save(self.cache_path, pw, self.cache)

            except Exception as exc:
                if self.store is not None:
                    self.store.close()
                    self.store = None

                QMessageBox.critical(
                    self,
                    'Ошибка открытия',
                    'Пароль неверен, контейнер отсутствует или повреждён.\n'
                    + str(exc)
                )
                return False

        self.password = pw

        self.runtime_path = (
            bundled_runtime()
            or self.cache.get('runtime_path')
        )

        llm_present = self.model_entry('llm') is not None
        embedding_present = self.model_entry('embedding') is not None

        self.status.setText(
            'Офлайн-режим'
            ' · языковая модель: '
            + ('готова' if llm_present else 'не добавлена')
            + ' · семантическая модель: '
            + ('готова' if embedding_present else 'не добавлена')
        )

        self.sync_files()
        self.refresh()
        self.greet()

        QTimer.singleShot(0, self.summarize)

        if getattr(sys, 'frozen', False):
            QTimer.singleShot(
                3000,
                lambda: self.updates(automatic=True)
            )

        return True

    def admin(self):
        pw = password_prompt(self, 'Введите мастер-пароль для изменения данных')
        return pw is not None and hmac.compare_digest(pw.encode(), self.password.encode())

    def commit(self, data, after=None):
        try:
            index_data = {'index': data.get('index', {}), 'warnings': data.get('warnings', {})}
            encoded = json.dumps(index_data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        except Exception as exc:
            QMessageBox.critical(self, 'Ошибка сохранения', str(exc)); return False
        disk, password = self.disk, self.password
        def write_index():
            with EncryptedStore(disk, password) as store:
                store.add_stream('index.json', io.BytesIO(encoded), kind='index', replace_kind=True)
        def done(_, error):
            if error:
                QMessageBox.critical(self, 'Ошибка сохранения', error)
                if after: after(False)
                return
            self.reload_store()
            if after: after(True)
        self.run_task(write_index, done, 'Индекс сохраняется в зашифрованный disk.data…')
        return True

    def sync_files(self):
        self.data['files'] = [entry for entry in self.store.manifest['objects'] if entry['kind'] == 'file']

    def load_index(self):
        data = {'files': [], 'index': {}, 'warnings': {}}
        matches = [x for x in self.store.manifest['objects'] if x['kind'] == 'index']
        if matches:
            output = io.BytesIO(); self.store.read_to(matches[-1], output)
            data.update(json.loads(output.getvalue()))
        return data

    def refresh(self):
        self.files.clear()
        for entry in self.data['files']: self.files.addItem(entry['name'])

    def greet(self):
        for item in self.cache.get('messages', [])[-20:]:
            self.chat.append('Вы: ' + html.escape(item.get('question', '')))
            self.chat.append(html.escape(item.get('answer', '')))
        n = len(self.data['files']); indexed = sum(len(x) for x in self.data.get('index', {}).values())
        self.chat.append(f'На носителе файлов: {n}. Проиндексировано фрагментов: {indexed}. Что вы хотите найти?')
        if not self.model_entry('llm'):
            self.chat.append('Языковая модель ещё не загружена.')

    def summarize(self):
        if not self.store or not self.runtime_path or not self.model_entry('llm') or (self.worker and self.worker.isRunning()): return
        live = {item['id'] for item in self.data['files']}
        hits = [group[0] for key, group in self.data.get('index', {}).items() if key in live and group][:3]
        if not hits: return
        runtime, disk, password = self.runtime_path, self.disk, self.password
        model = self.model_entry('llm')
        threads = max(1, min(int(self.cache.get('threads', 2)), 4))
        def run():
            with tempfile.TemporaryDirectory(prefix='portableai-model-') as folder, EncryptedStore(disk, password) as store:
                path = Path(folder) / 'llm.gguf'
                with path.open('wb') as output: store.read_to(model, output)
                with LocalModel(runtime, path, threads=threads) as engine:
                    return engine.answer('Кратко опиши, какая информация есть в источниках.', hits)
        def done(answer, error):
            if error:
                self.status.setText('Сводка недоступна: ' + error)
            elif answer:
                self.chat.append('Сводка по источникам: ' + html.escape(answer))
                self.chat.append('Что вы хотели бы посмотреть?')
        self.run_task(run, done, 'Расшифровывается модель и подготавливается сводка…')

    def import_files(self):
        if not self.admin(): return
        names, _ = QFileDialog.getOpenFileNames(self, 'Выберите файлы')
        if not names: return
        disk, password = self.disk, self.password
        def import_batch():
            with EncryptedStore(disk, password) as store:
                imported, errors = 0, []
                for name in names:
                    try:
                        store.add_file(name); imported += 1
                    except Exception as exc:
                        errors.append(Path(name).name + ': ' + str(exc))
                return imported, errors
        def done(result, error):
            self.reload_store()
            if error:
                QMessageBox.critical(self, 'Ошибка импорта', error); return
            imported, errors = result
            if errors: QMessageBox.warning(self, 'Часть файлов пропущена', '\n'.join(errors[:20]))
            if imported: self.reindex(auth=False, force=False)
        self.run_task(import_batch, done, 'Файлы шифруются и копируются в disk.data…')

    def reload_store(self):
        """Refresh the UI connection after a worker has committed storage changes."""
        if self.store is not None:
            self.store.close()
        self.store = EncryptedStore(self.disk, self.password)
        self.data = self.load_index()
        self.sync_files(); self.refresh()

    def delete_file(self):
        if not self.admin(): return
        row = self.files.currentRow()
        if row < 0: return
        if QMessageBox.question(self, 'Удаление', 'Удалить выбранный файл из контейнера?') != QMessageBox.StandardButton.Yes: return
        entry = self.data['files'][row]
        disk, password = self.disk, self.password
        def remove_one():
            with EncryptedStore(disk, password) as store:
                store.remove(entry)
        def done(_, error):
            if error:
                QMessageBox.critical(self, 'Ошибка удаления', error); return
            self.reload_store(); self.reindex(auth=False, force=False)
        self.run_task(remove_one, done, 'Файл удаляется из зашифрованного контейнера…')

    def reindex(self, auth=True, force=True):
        if auth and not self.admin(): return
        records = [dict(entry) for entry in self.data['files']]
        old = self.data.get('index', {})
        old_warnings = self.data.get('warnings', {})
        base = {key: value for key, value in self.data.items() if key not in {'index', 'warnings', 'files'}}
        disk, password = self.disk, self.password
        model = self.model_entry('embedding')
        runtime = self.runtime_path
        threads = max(1, min(int(self.cache.get('threads', 2)), 4))
        embed_version = model['sha256'] + ':e5-v2-600' if model else None

        def calculate():
            result, warnings = {}, {}
            with EncryptedStore(disk, password) as store:
                for record in records:
                    key = record['id']
                    if not force and key in old:
                        result[key] = old[key]; warnings[key] = old_warnings.get(key, [])
                        continue
                    if record['size'] > 10 * 1024 * 1024:
                        result[key] = []; warnings[key] = [record['name'] + ': файл больше лимита текстового анализатора (10 МБ)']; continue
                    output = io.BytesIO(); store.read_to(record, output)
                    blocks, warn = extract(record['name'], output.getvalue())
                    result[key], warnings[key] = chunks(blocks), warn
                    for i, chunk in enumerate(result[key]): chunk['id'] = key + ':' + str(i)
                if model and runtime and any(group for group in result.values()):
                    with tempfile.TemporaryDirectory(prefix='portableai-model-') as folder:
                        model_path = Path(folder) / 'embedding.gguf'
                        with model_path.open('wb') as output: store.read_to(model, output)
                        with LocalModel(runtime, model_path, embeddings=True, threads=threads) as engine:
                            for group in result.values():
                                for chunk in group:
                                    if chunk.get('embedding_version') != embed_version:
                                        chunk['embedding'] = engine.embed('passage: ' + chunk['text'][:600])
                                        chunk['embedding_version'] = embed_version
            return {**base, 'files': records, 'index': result, 'warnings': warnings}

        def done(candidate, error):
            if error:
                QMessageBox.critical(self, 'Ошибка индексации', error); return
            self.finish_index(candidate)
        self.run_task(calculate, done, 'Извлекается текст и строится индекс…')

    def finish_index(self, candidate):
        def saved(ok):
            if not ok: return
            total = sum(len(v) for v in candidate['index'].values())
            info = [w for group in candidate['warnings'].values() for w in group]
            QMessageBox.information(self, 'Индексация', f'Фрагментов: {total}. Предупреждений: {len(info)}\n' + '\n'.join(info[:20]))
        self.commit(candidate, saved)

    def run_task(self, function, callback, label):
        self.centralWidget().setEnabled(False); self.status.setText(label)
        self.worker = Worker(function, self)
        def done(result, error):
            self.centralWidget().setEnabled(True); self.status.setText('Локальный режим')
            callback(result, error)
        self.worker.completed.connect(done)
        self.worker.start()

    def ask(self):
        query = self.query.text().strip()
        if not query: return
        if len(query) > 1000:
            QMessageBox.information(self, 'Слишком длинный запрос', 'Введите запрос короче 1 000 символов.')
            return
        self.last_query = query
        self.query.clear(); self.chat.append('Вы: ' + html.escape(query))
        live_ids = {entry['id'] for entry in self.data['files']}
        all_chunks = [c for key, group in self.data.get('index', {}).items() if key in live_ids for c in group]
        embedding_model = self.model_entry('embedding')
        if embedding_model:
            version = embedding_model['sha256'] + ':e5-v2-600'
            all_chunks = [c if c.get('embedding_version') == version else {k: v for k, v in c.items() if k != 'embedding'} for c in all_chunks]
        lexical = search(all_chunks, query)
        if not self.runtime_path or not (self.model_entry('embedding') or self.model_entry('llm')):
            self.finish_answer(query, lexical, None, None); return
        runtime, disk, password = self.runtime_path, self.disk, self.password
        embedding_entry, llm_entry = self.model_entry('embedding'), self.model_entry('llm')
        history = list(self.cache.get('messages', []))
        threads = max(1, min(int(self.cache.get('threads', 2)), 4))
        def calculate():
            hits, answer = lexical, None
            with tempfile.TemporaryDirectory(prefix='portableai-model-') as folder, EncryptedStore(disk, password) as store:
                if embedding_entry:
                    path = Path(folder) / 'embedding.gguf'
                    with path.open('wb') as output: store.read_to(embedding_entry, output)
                    with LocalModel(runtime, path, embeddings=True, threads=threads) as engine:
                        hits = hybrid_search(all_chunks, engine.embed('query: ' + query), lexical)
                    path.unlink(missing_ok=True)
                if llm_entry and hits:
                    path = Path(folder) / 'llm.gguf'
                    with path.open('wb') as output: store.read_to(llm_entry, output)
                    with LocalModel(runtime, path, threads=threads) as engine:
                        answer = engine.answer(query, hits, history)
                    path.unlink(missing_ok=True)
            return hits, answer
        def done(result, error):
            hits, answer = result if result else (lexical, None)
            self.finish_answer(query, hits, answer, error)
        self.run_task(calculate, done, 'Расшифровываются модели и обрабатывается запрос…')

    def finish_answer(self, query, hits, answer, error):
        self.results = hits
        if error: QMessageBox.warning(self, 'ИИ недоступен', error)
        self.sources.clear()
        for hit in self.results: self.sources.addItem(hit['path'] + ' — ' + hit['locator'])
        if self.results and any(word in query.casefold() for word in ('покажи', 'открой', 'посмотреть')):
            self.sources.setCurrentRow(0); self.open_source()
        if answer is None:
            answer = 'Найдены фрагменты. Откройте источник двойным щелчком.' if self.results else 'Совпадений не найдено. Часть файлов могла быть пропущена при индексации.'
        self.chat.append(html.escape(answer))
        context_snapshot = [{'path': hit.get('path', '')[:300], 'locator': hit.get('locator', '')[:200],
                             'text': hit.get('text', '')[:600]} for hit in hits[:3]]
        candidate = {**self.cache, 'messages': (self.cache.get('messages', []) + [
            {'question': query[:4000], 'answer': answer[:4000], 'sources': context_snapshot}])[-100:]}
        try:
            save(self.cache_path, self.password, candidate); self.cache = candidate
        except Exception as exc:
            QMessageBox.warning(self, 'Кэш не сохранён', str(exc))

    def clear_history(self):
        if not self.admin(): return
        if not self.cache.get('messages'):
            QMessageBox.information(self, 'История чата', 'История уже пуста.'); return
        if QMessageBox.question(self, 'Очистить историю', 'Удалить зашифрованную историю чата из data.cache?') != QMessageBox.StandardButton.Yes:
            return
        self.cache['messages'] = []
        try:
            save(self.cache_path, self.password, self.cache)
            self.chat.clear(); self.greet()
            QMessageBox.information(self, 'История чата', 'История удалена из data.cache.')
        except Exception as exc:
            QMessageBox.warning(self, 'История чата', 'Не удалось сохранить кэш: ' + str(exc))

    def performance(self):
        if not self.admin(): return
        options = ['1', '2', '3', '4']
        current = str(max(1, min(int(self.cache.get('threads', 2)), 4)))
        value, ok = QInputDialog.getItem(self, 'Производительность',
            'Потоков CPU для локальной модели (меньше потоков — ниже нагрузка):', options, options.index(current), False)
        if not ok: return
        self.cache['threads'] = int(value)
        try:
            save(self.cache_path, self.password, self.cache)
            QMessageBox.information(self, 'Производительность', f'Настроено потоков CPU: {value}.')
        except Exception as exc:
            QMessageBox.warning(self, 'Производительность', 'Не удалось сохранить настройки: ' + str(exc))

    def open_source(self):
        row = self.sources.currentRow()
        if row < 0: return
        hit = self.results[row]; self.show_viewer(hit['path'] + ' — ' + hit['locator'], hit['text'], self.last_query)
        if 'timestamp_ms' in hit and '!/' not in hit['path']:
            stem = Path(hit['path']).stem
            for entry in self.data['files']:
                if Path(entry['name']).stem == stem and Path(entry['name']).suffix.lower() in {'.mp4', '.mov', '.m4v', '.avi', '.mkv'}:
                    self.open_video(entry, hit['timestamp_ms']); break

    def show_viewer(self, title, content, needle=''):
        dialog = Viewer(title, content, needle, self); self.viewers.append(dialog)
        dialog.finished.connect(lambda: self.viewers.remove(dialog) if dialog in self.viewers else None)
        dialog.show()

    def open_file(self):
        row = self.files.currentRow()
        if row < 0: return
        entry = self.data['files'][row]
        suffix = Path(entry['name']).suffix.lower()
        if suffix in {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm', '.mpeg', '.mpg'}:
            self.open_video(entry); return
        if suffix in {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac', '.opus', '.wma'}:
            self.open_audio(entry); return
        if entry['size'] > 100 * 1024 * 1024:
            QMessageBox.warning(self, 'Просмотр', 'Предпросмотр файлов более 100 МБ пока недоступен.'); return
        output = io.BytesIO(); self.store.read_to(entry, output); raw = output.getvalue()
        if Path(entry['name']).suffix.lower() in {'.png', '.jpg', '.jpeg', '.bmp', '.webp', '.gif', '.tif', '.tiff', '.ico'}:
            try:
                dialog = ImageViewer(entry['name'], raw, self); self.viewers.append(dialog)
                dialog.finished.connect(lambda: self.viewers.remove(dialog) if dialog in self.viewers else None)
                dialog.show()
            except ValueError as exc: QMessageBox.warning(self, 'Изображение', str(exc))
            return
        blocks, warnings = extract(entry['name'], raw)
        text = '\n\n'.join(b['locator'] + '\n' + b['text'] for b in blocks)
        self.show_viewer(entry['name'], text or '\n'.join(warnings) or 'Предпросмотр недоступен.')

    def open_video(self, entry, start_ms=0):
        if entry['size'] > 100 * 1024 * 1024:
            QMessageBox.warning(self, 'Видео', 'Просмотр видео более 100 МБ пока недоступен.'); return
        output = io.BytesIO(); self.store.read_to(entry, output)
        dialog = VideoViewer(entry['name'], output.getvalue(), self, start_ms); self.viewers.append(dialog)
        dialog.finished.connect(lambda: self.viewers.remove(dialog) if dialog in self.viewers else None)
        dialog.show()

    def open_audio(self, entry):
        if entry['size'] > 100 * 1024 * 1024:
            QMessageBox.warning(self, 'Аудио', 'Просмотр файлов более 100 МБ пока недоступен.'); return
        output = io.BytesIO(); self.store.read_to(entry, output)
        dialog = AudioViewer(entry['name'], output.getvalue(), self); self.viewers.append(dialog)
        dialog.finished.connect(lambda: self.viewers.remove(dialog) if dialog in self.viewers else None)
        dialog.show()

    def models(self):
        if not self.admin(): return
        kinds = {'Языковая модель (LLM)': 'llm', 'Модель эмбеддингов': 'embedding'}
        label, ok = QInputDialog.getItem(self, 'Модели ИИ', 'Какую модель заменить?', list(kinds), 0, False)
        if not ok: return
        kind = kinds[label]
        path, _ = QFileDialog.getOpenFileName(self, 'Выберите локальный файл модели', filter='Модель GGUF (*.gguf)')
        if not path: return
        try:
            with open(path, 'rb') as source:
                if source.read(4) != b'GGUF': raise ValueError('Файл не является GGUF')
            disk, password = self.disk, self.password
            def import_model():
                with EncryptedStore(disk, password) as store, open(path, 'rb') as source:
                    store.add_stream(Path(path).name, source, kind=kind, replace_kind=True)
            def done(_, error):
                if error:
                    QMessageBox.critical(self, 'Ошибка импорта модели', error); return
                self.reload_store()
                if kind == 'embedding':
                    for group in self.data.get('index', {}).values():
                        for chunk in group:
                            chunk.pop('embedding', None); chunk.pop('embedding_version', None)
                runtime = bundled_runtime() or self.cache.get('runtime_path')
                if not runtime or not Path(runtime).is_file():
                    runtime, _ = QFileDialog.getOpenFileName(self, 'Выберите локальный llama-server')
                if not runtime: return
                self.runtime_path = runtime
                self.cache['runtime_path'] = runtime
                try: save(self.cache_path, self.password, self.cache)
                except Exception as exc: QMessageBox.warning(self, 'Не сохранён путь к движку', str(exc))
                if kind == 'embedding':
                    self.commit(self.data, lambda ok: self.reindex(auth=False, force=True) if ok else None)
                else:
                    QMessageBox.information(self, 'Модели', f'Модель «{label}» импортирована. Вторую можно оставить без изменений.')
            self.run_task(import_model, done, 'Модель проверяется и шифруется в disk.data…')
        except Exception as exc:
            QMessageBox.critical(self, 'Ошибка импорта модели', str(exc))

    def model_entry(self, kind):
        entries = [entry for entry in self.store.manifest['objects'] if entry['kind'] == kind]
        return entries[-1] if entries else None

    def updates(self, automatic=False):
        if not automatic and not self.admin(): return
        if self.update_worker and self.update_worker.isRunning(): return
        machine = platform.machine().lower()
        platform_name = ('windows-x64' if sys.platform == 'win32' and machine in ('amd64', 'x86_64') else
                         'macos-arm64' if sys.platform == 'darwin' and machine in ('arm64', 'aarch64') else
                         'macos-x64' if sys.platform == 'darwin' and machine in ('x86_64', 'amd64') else None)
        if not platform_name:
            if not automatic: QMessageBox.warning(self, 'Обновление', 'Для этой платформы обновление пока не предусмотрено.')
            return
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
        try: public_key = bytes.fromhex((root / 'update-public-key.hex').read_text().strip())
        except (OSError, ValueError) as exc:
            if not automatic: QMessageBox.warning(self, 'Обновление', 'Отсутствует открытый ключ обновлений: ' + str(exc))
            return
        def check():
            result = check_update(MANIFEST_URL, public_key, CURRENT_VERSION, platform_name)
            if result and getattr(sys, 'frozen', False):
                target = self.application_target()
                staged = stage_asset(result['asset'], target.parent)
                try: result['replacement'] = str(prepare_replacement(staged, target))
                finally: staged.unlink(missing_ok=True)
            return result
        self.update_worker = Worker(check, self)
        def checked(result, error):
            if error:
                if not automatic: QMessageBox.warning(self, 'Обновление', 'Не удалось проверить обновления: ' + error)
            elif result:
                if result.get('replacement'):
                    self.pending_update = result
                    self.apply_pending_update()
                else:
                    QMessageBox.information(self, 'Обновление', 'Доступна подписанная версия ' + result['version'] + '. Для установки требуется собранное приложение.')
            elif not automatic:
                QMessageBox.information(self, 'Обновление', 'Новых версий нет.')
        self.update_worker.completed.connect(checked); self.update_worker.start()

    def application_target(self):
        exe = Path(sys.executable).resolve()
        if sys.platform == 'darwin' and exe.parent.name == 'MacOS' and exe.parent.parent.name == 'Contents':
            return exe.parent.parent.parent
        return exe

    def apply_pending_update(self):
        if self.worker and self.worker.isRunning():
            QTimer.singleShot(1000, self.apply_pending_update); return
        try:
            self.update_worker.wait()
            launch_update_helper(self.application_target(), self.pending_update['replacement'])
            self.close()
        except Exception as exc:
            QMessageBox.warning(self, 'Обновление', 'Не удалось установить обновление: ' + str(exc))

    def closeEvent(self, event):
        if self.update_worker is not None and self.update_worker.isRunning():
            QMessageBox.warning(self, 'Проверка обновлений', 'Дождитесь завершения проверки обновлений.')
            event.ignore(); return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, 'Выполняется операция', 'Дождитесь завершения работы модели.')
            event.ignore(); return
        if self.store is not None:
            self.store.close(); self.store = None
        super().closeEvent(event)

    def reset(self):
        if not self.admin(): return
        phrase, ok = QInputDialog.getText(self, 'Полный сброс', 'Будут удалены disk.data и data.cache. Введите УДАЛИТЬ:')
        if not ok or phrase != 'УДАЛИТЬ': return
        try:
            self.store.close(); self.store = None
            self.disk.unlink(); self.cache_path.unlink()
        except OSError as exc:
            QMessageBox.critical(self, 'Ошибка сброса', str(exc)); return
        QMessageBox.information(self, 'Сброс', 'Контейнеры удалены. Приложение будет закрыто.')
        self.close()


if __name__ == '__main__':
    if len(sys.argv) == 6 and sys.argv[1] == '--apply-update':
        from updater import helper_main
        helper_main(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]); sys.exit(0)
    directory = storage_dir()
    app = QApplication(sys.argv[:1])
    if directory is None:
        QMessageBox.critical(None, 'Запуск', 'Укажите пустой тестовый каталог аргументом командной строки.')
        sys.exit(1)
    window = App(directory)
    if window.start(): window.show(); sys.exit(app.exec())
    sys.exit(1)
