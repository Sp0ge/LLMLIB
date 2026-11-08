"""Build the GUI with an already compiled, CPU-only llama-server."""
import os
import hashlib
import shutil
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit('Usage: python package.py PATH_TO_LLAMA_SERVER')
server = Path(sys.argv[1]).resolve()
if not server.is_file(): raise SystemExit('llama-server was not found: ' + str(server))
from bundled_models import MODEL_FILES, MODEL_SHA256
models = Path(os.environ.get('PORTABLEAI_MODEL_DIR', 'bundle-models')).resolve()
for kind, filename in MODEL_FILES.items():
    model = models / filename
    if not model.is_file():
        raise SystemExit(f'Required bundled model is missing: {model}')
    digest = hashlib.sha256()
    with model.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != MODEL_SHA256[kind]:
        raise SystemExit(f'Unexpected SHA-256 for bundled model: {model}')
separator = ';' if os.name == 'nt' else ':'
mode = '--onefile' if os.name == 'nt' else '--onedir'
runtime_name = 'llama-server.exe' if os.name == 'nt' else 'llama-server'
with tempfile.TemporaryDirectory(prefix='portableai-build-') as staging:
    staged_server = Path(staging) / runtime_name
    shutil.copy2(server, staged_server)
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', mode, '--windowed',
               '--name', 'PortableAI', '--add-binary', str(staged_server) + separator + 'runtime',
               '--add-data', str(models) + separator + 'models',
               '--add-data', 'update-public-key.hex' + separator + '.',
               '--add-data', 'THIRD_PARTY_NOTICES.md' + separator + '.',
               '--collect-all', 'py7zr', 'app.py']
    if sys.platform == 'darwin':
        command.extend(['--osx-bundle-identifier', 'com.sp0ge.portableai'])
    subprocess.run(command, check=True)
if sys.platform == 'darwin':
    from updater import CURRENT_VERSION
    info = Path('dist/PortableAI.app/Contents/Info.plist')
    with info.open('rb') as stream:
        metadata = plistlib.load(stream)
    metadata['CFBundleShortVersionString'] = CURRENT_VERSION
    metadata['CFBundleVersion'] = CURRENT_VERSION
    with info.open('wb') as stream:
        plistlib.dump(metadata, stream, sort_keys=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', '--timestamp=none',
                    'dist/PortableAI.app'], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict',
                    'dist/PortableAI.app'], check=True)
