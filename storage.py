"""Experimental block-encrypted SQLite container for large files.

SQLite's structure, object identifiers and block sizes remain visible; payloads and
manifest metadata are authenticated and encrypted. This format has not been audited.
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
import struct
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

BLOCK_SIZE = 1024 * 1024
SCHEMA_VERSION = b'PAI2'


def _key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode('utf-8'))


def _encrypt(aes: AESGCM, data: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + aes.encrypt(nonce, data, aad)


def _decrypt(aes: AESGCM, data: bytes, aad: bytes) -> bytes:
    return aes.decrypt(data[:12], data[12:], aad)


class EncryptedStore:
    def __init__(self, path, password, create=False):
        self.path = Path(path)
        if create and self.path.exists():
            raise FileExistsError(self.path)
        if not create and not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.db = sqlite3.connect(str(self.path), timeout=0.2)
        try:
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            if create:
                self.db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value BLOB NOT NULL)')
                self.db.execute('CREATE TABLE blocks (object_id BLOB NOT NULL, ordinal INTEGER NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(object_id, ordinal)) WITHOUT ROWID')
                salt = os.urandom(16)
                self.aes = AESGCM(_key(password, salt))
                self.db.executemany('INSERT INTO meta(key, value) VALUES (?, ?)', [
                    ('version', SCHEMA_VERSION), ('salt', salt),
                    ('check', _encrypt(self.aes, b'Portable AI USB', b'PAI2:check')),
                    ('manifest', _encrypt(self.aes, b'{"objects":[]}', b'PAI2:manifest'))])
                self.db.commit()
            else:
                values = dict(self.db.execute('SELECT key, value FROM meta'))
                if values.get('version') != SCHEMA_VERSION or len(values.get('salt', b'')) != 16:
                    raise ValueError('неизвестная версия контейнера')
                self.aes = AESGCM(_key(password, values['salt']))
                if _decrypt(self.aes, values['check'], b'PAI2:check') != b'Portable AI USB':
                    raise ValueError('неверный контейнер')
            self.manifest = self._load_manifest()
        except Exception:
            self.db.close()
            raise

    def _load_manifest(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
        self._manifest_ciphertext = row[0]
        result = json.loads(_decrypt(self.aes, row[0], b'PAI2:manifest'))
        if not isinstance(result, dict) or not isinstance(result.get('objects'), list):
            raise ValueError('некорректный каталог контейнера')
        return result

    def _save_manifest(self, manifest):
        data = json.dumps(manifest, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        encrypted = _encrypt(self.aes, data, b'PAI2:manifest')
        self.db.execute("UPDATE meta SET value=? WHERE key='manifest'", (encrypted,))
        return encrypted

    def _assert_current(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
        if not row or row[0] != self._manifest_ciphertext:
            raise RuntimeError('Контейнер изменён в другой копии программы; откройте его заново')

    @staticmethod
    def _aad(identifier: bytes, number: int) -> bytes:
        return b'PAI2:block:' + identifier + struct.pack('>Q', number)

    def add_stream(self, name: str, stream, kind='file', replace_kind=False):
        if not name or not isinstance(name, str): raise ValueError('имя объекта обязательно')
        try:
            current = stream.tell()
            expected = os.fstat(stream.fileno()).st_size - current
        except (AttributeError, OSError, ValueError):
            expected = None
        if expected is not None and expected + 32 * 1024 * 1024 > shutil.disk_usage(self.path.parent).free:
            raise OSError('Недостаточно свободного места на носителе для файла и служебных данных')
        identifier = os.urandom(16)
        digest = hashlib.sha256()
        size = count = 0
        removed = [x for x in self.manifest['objects'] if replace_kind and x['kind'] == kind]
        candidate = {'objects': [x for x in self.manifest['objects'] if x not in removed]}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._assert_current()
            while True:
                raw = stream.read(BLOCK_SIZE)
                if not raw: break
                if not isinstance(raw, bytes): raise TypeError('поток должен возвращать байты')
                digest.update(raw); size += len(raw)
                self.db.execute('INSERT INTO blocks VALUES (?, ?, ?)',
                                (identifier, count, _encrypt(self.aes, raw, self._aad(identifier, count))))
                count += 1
            entry = {'id': identifier.hex(), 'name': name, 'kind': kind, 'size': size,
                     'blocks': count, 'sha256': digest.hexdigest()}
            candidate['objects'].append(entry)
            for old in removed:
                self.db.execute('DELETE FROM blocks WHERE object_id=?', (bytes.fromhex(old['id']),))
            encrypted = self._save_manifest(candidate)
        self.manifest = candidate
        self._manifest_ciphertext = encrypted
        return entry

    def add_file(self, path, kind='file'):
        path = Path(path)
        with path.open('rb') as stream:
            return self.add_stream(path.name, stream, kind)

    def read_to(self, entry, destination):
        identifier = bytes.fromhex(entry['id'])
        digest = hashlib.sha256(); size = 0; count = 0
        rows = self.db.execute('SELECT ordinal, payload FROM blocks WHERE object_id=? ORDER BY ordinal', (identifier,))
        for ordinal, payload in rows:
            if ordinal != count or ordinal >= entry['blocks']:
                raise ValueError('нарушен порядок блоков')
            raw = _decrypt(self.aes, payload, self._aad(identifier, ordinal))
            digest.update(raw); size += len(raw); destination.write(raw); count += 1
        if count != entry['blocks'] or size != entry['size'] or digest.hexdigest() != entry['sha256']:
            raise ValueError('данные контейнера неполные или повреждены')

    def remove(self, entry):
        identifier = bytes.fromhex(entry['id'])
        candidate = {'objects': [x for x in self.manifest['objects'] if x['id'] != entry['id']]}
        if len(candidate['objects']) == len(self.manifest['objects']): raise KeyError(entry['id'])
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._assert_current()
            self.db.execute('DELETE FROM blocks WHERE object_id=?', (identifier,))
            encrypted = self._save_manifest(candidate)
        self.manifest = candidate
        self._manifest_ciphertext = encrypted

    def close(self):
        self.db.close()

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()
