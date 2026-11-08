"""Signed release manifests: validation and bounded staging, never self-execution."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_MANIFEST = 128 * 1024
MAX_PACKAGE = 1024 * 1024 * 1024
ALLOWED_HOSTS = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
CURRENT_VERSION = '0.2.2'
MANIFEST_URL = 'https://github.com/Sp0ge/LLMLIB/releases/latest/download/portableai-update.json'


def version_tuple(value):
    match = re.fullmatch(r'v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})', value)
    if not match: raise ValueError('некорректная версия')
    return tuple(map(int, match.groups()))


def verify_manifest(envelope, trusted_public_key):
    if not isinstance(envelope, dict) or set(envelope) != {'payload', 'signature'}:
        raise ValueError('неверный формат обновления')
    payload = base64.b64decode(envelope['payload'], validate=True)
    signature = base64.b64decode(envelope['signature'], validate=True)
    if len(payload) > MAX_MANIFEST: raise ValueError('манифест слишком велик')
    Ed25519PublicKey.from_public_bytes(trusted_public_key).verify(signature, payload)
    result = json.loads(payload.decode('utf-8'))
    version_tuple(result['version'])
    if not isinstance(result.get('assets'), dict): raise ValueError('нет перечня файлов')
    return result


def validate_asset(asset):
    if not isinstance(asset, dict): raise ValueError('неверное описание файла')
    url = asset.get('url', ''); digest = asset.get('sha256', ''); size = asset.get('size')
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('неразрешённый адрес обновления')
    if not re.fullmatch(r'[0-9a-f]{64}', digest): raise ValueError('неверная контрольная сумма')
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > MAX_PACKAGE: raise ValueError('неверный размер обновления')
    return url, digest, size


def _set_hidden(path):
    if os.name != 'nt':
        return
    import ctypes
    kernel = ctypes.windll.kernel32
    kernel.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    kernel.GetFileAttributesW.restype = ctypes.c_uint32
    kernel.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    path = str(path)
    attributes = kernel.GetFileAttributesW(path)
    if attributes != 0xFFFFFFFF:
        kernel.SetFileAttributesW(path, attributes | 0x2)


def _clear_hidden(path):
    if os.name != 'nt':
        return
    import ctypes
    kernel = ctypes.windll.kernel32
    kernel.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    kernel.GetFileAttributesW.restype = ctypes.c_uint32
    kernel.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    path = str(path)
    attributes = kernel.GetFileAttributesW(path)
    if attributes != 0xFFFFFFFF and attributes & 0x2:
        kernel.SetFileAttributesW(path, attributes & ~0x2)


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError('неразрешённое перенаправление обновления')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def stage_asset(asset, directory):
    url, expected, size = validate_asset(asset)
    directory = Path(directory)
    opener = urllib.request.build_opener(RestrictedRedirect())
    temporary = None
    try:
        with opener.open(urllib.request.Request(url, headers={'User-Agent': 'PortableAI-Updater'}), timeout=30) as response:
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.update-', delete=False) as out:
                temporary = Path(out.name); digest = hashlib.sha256(); count = 0
                while True:
                    block = response.read(1024 * 1024)
                    if not block: break
                    count += len(block)
                    if count > size: raise ValueError('файл превышает заявленный размер')
                    digest.update(block); out.write(block)
                out.flush(); os.fsync(out.fileno())
        if count != size or digest.hexdigest() != expected: raise ValueError('обновление повреждено')
        return temporary
    except BaseException:
        if temporary: temporary.unlink(missing_ok=True)
        raise


def check_update(manifest_url, public_key, current_version, platform_name):
    validate_asset({'url': manifest_url, 'sha256': '0' * 64, 'size': 1})
    opener = urllib.request.build_opener(RestrictedRedirect())
    request = urllib.request.Request(manifest_url, headers={'User-Agent': 'PortableAI-Updater'})
    with opener.open(request, timeout=10) as response:
        raw = response.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST: raise ValueError('манифест слишком велик')
    manifest = verify_manifest(json.loads(raw), public_key)
    if version_tuple(manifest['version']) <= version_tuple(current_version): return None
    asset = manifest['assets'].get(platform_name)
    if not asset: raise ValueError('для этой системы нет обновления')
    validate_asset(asset)
    return {'version': manifest['version'], 'asset': asset}


def install_replacement(target, replacement):
    """Replace only the launcher, keeping a rollback copy until replacement succeeds."""
    target, replacement = Path(target), Path(replacement)
    if target.name not in {'PortableAI.exe', 'PortableAI.app'} or target.parent.resolve() != replacement.parent.resolve():
        raise ValueError('неверное расположение приложения')
    if not replacement.name.startswith('.portableai-update-') or not replacement.exists() or not target.exists():
        raise ValueError('неверный подготовленный пакет')
    backup = target.parent / ('.portableai-backup-' + os.urandom(8).hex())
    _clear_hidden(replacement)
    os.replace(target, backup)
    _set_hidden(backup)
    try:
        os.replace(replacement, target)
    except BaseException:
        _clear_hidden(backup)
        os.replace(backup, target)
        raise
    return backup


def prepare_replacement(staged, target):
    """Prepare an authenticated asset beside the application without touching data files."""
    import shutil
    import stat
    import zipfile
    staged, target = Path(staged), Path(target)
    if target.name not in {'PortableAI.exe', 'PortableAI.app'}:
        raise ValueError('неподдерживаемый путь приложения')
    result = target.parent / ('.portableai-update-' + os.urandom(8).hex())
    if target.name.endswith('.exe'):
        shutil.copyfile(staged, result)
        return result
    result.mkdir(mode=0o700)
    total = 0
    try:
        with zipfile.ZipFile(staged) as archive:
            infos = archive.infolist()
            if len(infos) > 50000: raise ValueError('слишком много файлов в пакете')
            links, extracted = {}, set()
            for info in infos:
                from pathlib import PurePosixPath
                raw = info.filename.replace('\\', '/')
                parts = PurePosixPath(raw).parts
                if not parts or parts[0] != 'PortableAI.app' or '..' in parts or raw.startswith('/'):
                    raise ValueError('небезопасный путь в пакете')
                relative = PurePosixPath(*parts[1:]).as_posix()
                if relative in {'', '.'}:
                    continue
                if relative in extracted or relative in links:
                    raise ValueError('повторяющийся путь в пакете')
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                is_link = file_type == stat.S_IFLNK
                is_directory = info.is_dir() or file_type == stat.S_IFDIR
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
                    raise ValueError('специальные файлы в пакете запрещены')
                total += info.file_size
                if total > 2 * MAX_PACKAGE or info.file_size > MAX_PACKAGE:
                    raise ValueError('слишком большой распакованный пакет')
                # Drop the wrapper directory; result itself is the replacement .app.
                destination = result.joinpath(*PurePosixPath(relative).parts)
                if is_link:
                    with archive.open(info) as source:
                        link_target = source.read(4097).decode('utf-8')
                    if len(link_target) > 4096 or '\x00' in link_target or link_target.startswith('/') or '\\' in link_target:
                        raise ValueError('небезопасная цель ссылки в пакете')
                    resolved = os.path.normpath(os.path.join(os.path.dirname(relative), link_target)).replace('\\', '/')
                    if resolved in {'.', '..'} or resolved.startswith('../'):
                        raise ValueError('ссылка выходит за каталог приложения')
                    links[relative] = link_target
                    continue
                extracted.add(relative)
                if is_directory: destination.mkdir(parents=True, exist_ok=True); continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                permissions = mode & 0o777
                if permissions: destination.chmod(permissions & 0o755)
            if any(path.startswith(link + '/') for path in links for link in links):
                raise ValueError('каталог приложения пересекается с символической ссылкой')
            if any(path.startswith(link + '/') for path in extracted for link in links):
                raise ValueError('каталог приложения пересекается с символической ссылкой')
            for relative, link_target in links.items():
                destination = result.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(link_target, destination)
        if not (result / 'Contents/MacOS/PortableAI').is_file(): raise ValueError('исполняемый файл приложения отсутствует')
        if sys.platform == 'darwin':
            import subprocess
            subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(result)],
                           check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result
    except BaseException:
        shutil.rmtree(result, ignore_errors=True)
        raise


def wait_for_process(pid, timeout=90):
    import time
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87: return
            raise OSError(error, 'не удалось проверить процесс')
        try:
            if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
                raise TimeoutError('предыдущий процесс не завершился')
        finally: kernel.CloseHandle(handle)
    else:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try: os.kill(pid, 0)
            except ProcessLookupError: return
            time.sleep(0.2)
        raise TimeoutError('предыдущий процесс не завершился')


def helper_main(pid, target, replacement):
    import shutil
    import subprocess
    wait_for_process(int(pid))
    target = Path(target)
    backup = install_replacement(target, replacement)
    try:
        executable = target / 'Contents/MacOS/PortableAI' if target.suffix == '.app' else target
        subprocess.Popen([str(executable)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except BaseException:
        failed = target.parent / ('.portableai-failed-' + os.urandom(8).hex())
        os.replace(target, failed); _clear_hidden(backup); os.replace(backup, target)
        raise
    # Backup is retained for recovery if the new application fails after launch.
    return backup


def launch_update_helper(target, replacement):
    import shutil
    import subprocess
    target, replacement = Path(target), Path(replacement)
    helper_dir = Path(tempfile.mkdtemp(prefix='portableai-helper-'))
    if target.suffix == '.app':
        helper = helper_dir / 'PortableAI.app'
        shutil.copytree(replacement, helper)
        executable = helper / 'Contents/MacOS/PortableAI'
    else:
        executable = helper_dir / 'PortableAI.exe'
        shutil.copyfile(replacement, executable)
    subprocess.Popen([str(executable), '--apply-update', str(os.getpid()), str(target), str(replacement)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
