"""Build the GUI with an already compiled, CPU-only llama-server."""
import os
import platform
import subprocess
import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit('Usage: python package.py PATH_TO_LLAMA_SERVER')
server = Path(sys.argv[1]).resolve()
if not server.is_file(): raise SystemExit('llama-server was not found: ' + str(server))
separator = ';' if os.name == 'nt' else ':'
command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
           '--name', 'PortableAI', '--add-binary', str(server) + separator + 'runtime',
           '--add-data', 'update-public-key.hex' + separator + '.', '--collect-all', 'py7zr', 'app.py']
subprocess.run(command, check=True)
