"""Experimental encrypted container. Not an audited production format."""
import base64
import json
import os
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b'PAI1'
MAX_FILE = 10 * 1024 * 1024
MAX_VAULT = 50 * 1024 * 1024


def derive(password, salt):
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode('utf-8'))


def save(path, password, records):
    path = Path(path)
    payload = json.dumps(records, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(payload) > MAX_VAULT:
        raise ValueError('Превышен лимит контейнера 50 МБ')
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    encrypted = AESGCM(derive(password, salt)).encrypt(nonce, payload, header)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.portableai-', delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(header + encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp and tmp.exists():
            tmp.unlink()


def load(path, password):
    blob = Path(path).read_bytes()
    if len(blob) < 48 or blob[:4] != MAGIC or len(blob) > MAX_VAULT + 48:
        raise ValueError('Неверный формат или слишком большой контейнер')
    salt, nonce = blob[4:20], blob[20:32]
    result = AESGCM(derive(password, salt)).decrypt(nonce, blob[32:], blob[:32])
    return json.loads(result.decode('utf-8'))


def add_file(records, source):
    source = Path(source)
    if source.stat().st_size > MAX_FILE:
        raise ValueError('Файл больше 10 МБ')
    data = source.read_bytes()
    if len(data) > MAX_FILE:
        raise ValueError('Файл больше 10 МБ')
    records.append({'name': source.name, 'content': base64.b64encode(data).decode('ascii')})
