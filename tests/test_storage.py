import io
import os
import tempfile
import unittest
from pathlib import Path
from cryptography.exceptions import InvalidTag
from storage import EncryptedStore, BLOCK_SIZE


class StorageTest(unittest.TestCase):
    def test_second_writer_cannot_overwrite_new_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'disk.data'
            with EncryptedStore(path, 'a good password', create=True) as first:
                with EncryptedStore(path, 'a good password') as second:
                    first.add_stream('a', io.BytesIO(b'first'))
                    with self.assertRaises(RuntimeError): second.add_stream('b', io.BytesIO(b'second'))
            with EncryptedStore(path, 'a good password') as check:
                self.assertEqual([o['name'] for o in check.manifest['objects']], ['a'])

    def test_large_stream_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'disk.data'
            data = (b'secret data\n' * (BLOCK_SIZE // 10)) + os.urandom(200)
            with EncryptedStore(path, 'correct password', create=True) as store:
                item = store.add_stream('secret-name.txt', io.BytesIO(data))
                out = io.BytesIO(); store.read_to(item, out)
                self.assertEqual(out.getvalue(), data)
            contents = path.read_bytes()
            self.assertNotIn(b'secret-name.txt', contents)
            self.assertNotIn(b'secret data', contents)
            with self.assertRaises(InvalidTag): EncryptedStore(path, 'incorrect password')
            with EncryptedStore(path, 'correct password') as store:
                out = io.BytesIO(); store.read_to(store.manifest['objects'][0], out)
                self.assertEqual(out.getvalue(), data)
                store.remove(store.manifest['objects'][0])
            with EncryptedStore(path, 'correct password') as store:
                self.assertEqual(store.manifest['objects'], [])

    def test_missing_or_changed_block_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with EncryptedStore(Path(tmp) / 'disk.data', 'a good password', create=True) as store:
                entry = store.add_stream('x', io.BytesIO(b'a' * (BLOCK_SIZE + 1)))
                store.db.execute('DELETE FROM blocks WHERE object_id=? AND ordinal=1', (bytes.fromhex(entry['id']),))
                store.db.commit()
                with self.assertRaises(ValueError): store.read_to(entry, io.BytesIO())

    def test_failed_stream_rolls_back(self):
        class Broken:
            def __init__(self): self.count = 0
            def read(self, _):
                self.count += 1
                if self.count == 1: return b'text'
                raise OSError('disconnected')
        with tempfile.TemporaryDirectory() as tmp:
            with EncryptedStore(Path(tmp) / 'disk.data', 'a good password', create=True) as store:
                with self.assertRaises(OSError): store.add_stream('x', Broken())
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0], 0)
                self.assertEqual(store.manifest['objects'], [])


if __name__ == '__main__': unittest.main()
