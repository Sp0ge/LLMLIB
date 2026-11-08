import tempfile
import unittest
from pathlib import Path

from cryptography.exceptions import InvalidTag
from vault import add_file, load, save


class VaultTest(unittest.TestCase):
    def test_round_trip_and_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / 'sample.txt'
            sample.write_text('Секретный текст', encoding='utf-8')
            entries = []
            add_file(entries, sample)
            for name, value in [('disk.data', entries), ('data.cache', {'messages': ['Секретный текст']})]:
                target = root / name
                save(target, 'пароль достаточно длинный', value)
                self.assertEqual(load(target, 'пароль достаточно длинный'), value)
                self.assertNotIn('Секретный текст'.encode(), target.read_bytes())
                with self.assertRaises(InvalidTag):
                    load(target, 'wrong password')
                data = bytearray(target.read_bytes())
                data[-1] ^= 1
                target.write_bytes(data)
                with self.assertRaises(InvalidTag):
                    load(target, 'пароль достаточно длинный')


if __name__ == '__main__':
    unittest.main()
