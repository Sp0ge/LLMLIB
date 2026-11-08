"""Create a signed release manifest from previously built release assets."""
import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization

parser = argparse.ArgumentParser()
parser.add_argument('--key', required=True)
parser.add_argument('--version', required=True)
parser.add_argument('--repo', default='Sp0ge/LLMLIB')
parser.add_argument('--windows', required=True)
parser.add_argument('--macos-arm64', required=True)
parser.add_argument('--macos-x64', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from updater import version_tuple
version_tuple(args.version)
tag = args.version if args.version.startswith('v') else 'v' + args.version
assets = {}
for name, filename in [('windows-x64', args.windows), ('macos-arm64', args.macos_arm64), ('macos-x64', args.macos_x64)]:
    path = Path(filename); digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
    if not path.name or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for c in path.name):
        raise SystemExit('Unsafe asset filename')
    assets[name] = {'url': f'https://github.com/{args.repo}/releases/download/{tag}/{path.name}',
                    'size': path.stat().st_size, 'sha256': digest.hexdigest()}
payload = json.dumps({'version': tag, 'assets': assets}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
key = serialization.load_pem_private_key(Path(args.key).read_bytes(), password=None)
envelope = {'payload': base64.b64encode(payload).decode('ascii'), 'signature': base64.b64encode(key.sign(payload)).decode('ascii')}
Path(args.output).write_text(json.dumps(envelope, separators=(',', ':')), encoding='utf-8')
