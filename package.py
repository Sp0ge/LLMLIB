"""Build the GUI with an already compiled, CPU-only llama-server."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit('Usage: python package.py PATH_TO_LLAMA_SERVER')
server = Path(sys.argv[1]).resolve()
if not server.is_file(): raise SystemExit('llama-server was not found: ' + str(server))
separator = ';' if os.name == 'nt' else ':'
mode = '--onefile' if os.name == 'nt' else '--onedir'
command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', mode, '--windowed',
           '--name', 'PortableAI', '--add-binary', str(server) + separator + 'runtime',
           '--add-data', 'update-public-key.hex' + separator + '.', '--collect-all', 'py7zr', 'app.py']
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
