import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from app import App, ImageViewer
from vault import add_file, load


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_create_index_search_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            folder = Path(directory)
            source = Path(other) / 'info.txt'
            source.write_text('Текст о планете Марс', encoding='utf-8')
            with patch('app.password_prompt', side_effect=['длинный пароль', 'длинный пароль']):
                window = App(folder)
                self.assertTrue(window.start())
            window.store.add_file(source)
            window.sync_files(); window.refresh()
            with patch.object(QMessageBox, 'information'):
                window.reindex(auth=False)
                self.wait_for_idle(window)
            window.query.setText('Марс')
            window.ask()
            self.assertEqual(window.sources.count(), 1)
            self.assertIn('Марс', str(load(folder / 'data.cache', 'длинный пароль')))
            window.close()
            with patch('app.password_prompt', return_value='длинный пароль'):
                restored = App(folder)
                self.assertTrue(restored.start())
            self.assertEqual(len(restored.data['files']), 1)
            self.assertGreater(len(restored.data['index']), 0)
            restored.close()

    def test_initial_setup_preserves_hidden_dot_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            metadata = folder / '.DS_Store'
            metadata.write_bytes(b'macOS metadata')
            with patch('app.password_prompt', side_effect=['длинный пароль', 'длинный пароль']):
                window = App(folder)
                self.assertTrue(window.start())
            self.assertEqual(metadata.read_bytes(), b'macOS metadata')
            window.close()

    def wait_for_idle(self, window, timeout_ms=20000):
        loop = QEventLoop()
        poll = QTimer(); poll.setInterval(10)
        def check():
            if window.centralWidget().isEnabled() and (window.worker is None or not window.worker.isRunning()):
                loop.quit()
        poll.timeout.connect(check); poll.start()
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        poll.stop()
        self.assertTrue(window.centralWidget().isEnabled(), 'background operation did not finish')

    def test_image_viewer_validates_image(self):
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice
        from PySide6.QtGui import QImage
        data = QByteArray(); buffer = QBuffer(data); buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image = QImage(4, 4, QImage.Format.Format_RGB32); image.fill(0); image.save(buffer, 'PNG'); buffer.close()
        viewer = ImageViewer('test.png', bytes(data)); self.assertEqual(viewer.windowTitle(), 'test.png'); viewer.close()
        with self.assertRaises(ValueError): ImageViewer('bad.png', b'not a png')


if __name__ == '__main__': unittest.main()
