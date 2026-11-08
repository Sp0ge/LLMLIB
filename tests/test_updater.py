import base64
import json
import os
import unittest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from updater import validate_asset, verify_manifest, version_tuple


class UpdaterTest(unittest.TestCase):
    def test_signed_manifest_and_tampering(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        raw = json.dumps({'version':'1.2.3', 'assets':{}}).encode()
        envelope = {'payload':base64.b64encode(raw).decode(), 'signature':base64.b64encode(key.sign(raw)).decode()}
        self.assertEqual(verify_manifest(envelope, public)['version'], '1.2.3')
        envelope['payload'] = base64.b64encode(b'{"version":"9.9.9","assets":{}}').decode()
        with self.assertRaises(InvalidSignature): verify_manifest(envelope, public)

    def test_asset_policy(self):
        valid = {'url':'https://github.com/Sp0ge/LLMLIB/releases/download/v1/a.exe','sha256':'a'*64,'size':5}
        validate_asset(valid)
        for invalid in ['http://github.com/file', 'https://evil.example/file', 'https://github.com@evil.example/file']:
            with self.assertRaises(ValueError): validate_asset({**valid, 'url':invalid})
        with self.assertRaises(ValueError): version_tuple('1.2.3; rm')

    def test_signed_remote_manifest(self):
        import io
        from unittest.mock import patch
        from updater import check_update
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        asset = {'url': 'https://github.com/Sp0ge/LLMLIB/releases/download/v1/a.exe', 'sha256': 'a' * 64, 'size': 5}
        payload = json.dumps({'version': '1.2.3', 'assets': {'windows-x64': asset}}).encode()
        data = json.dumps({'payload':base64.b64encode(payload).decode(), 'signature':base64.b64encode(key.sign(payload)).decode()}).encode()
        class Opener:
            def open(self, request, timeout): return io.BytesIO(data)
        with patch('updater.urllib.request.build_opener', return_value=Opener()):
            result = check_update('https://github.com/Sp0ge/LLMLIB/releases/latest/download/portableai-update.json', public, '1.0.0', 'windows-x64')
            self.assertEqual(result['version'], '1.2.3')


if __name__ == '__main__': unittest.main()

class InstallerTest(unittest.TestCase):
    def test_replace_only_launcher_and_reject_arbitrary_paths(self):
        import tempfile
        from pathlib import Path
        from updater import install_replacement
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'PortableAI.exe'; target.write_bytes(b'old')
            candidate = root / '.portableai-update-new'; candidate.write_bytes(b'new')
            backup = install_replacement(target, candidate)
            self.assertEqual(target.read_bytes(), b'new')
            self.assertEqual(backup.read_bytes(), b'old')
            other = root / 'disk.data'; other.write_bytes(b'data')
            with self.assertRaises(ValueError): install_replacement(other, backup)
            self.assertEqual(other.read_bytes(), b'data')

    def test_failed_install_restores_launcher(self):
        import tempfile
        import os
        from pathlib import Path
        from unittest.mock import patch
        from updater import install_replacement
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / 'PortableAI.exe'; target.write_bytes(b'old')
            candidate = root / '.portableai-update-new'; candidate.write_bytes(b'new')
            original = os.replace
            def fail_once(src, dst):
                if Path(src) == candidate: raise OSError('disconnected')
                return original(src, dst)
            with patch('updater.os.replace', side_effect=fail_once):
                with self.assertRaises(OSError): install_replacement(target, candidate)
            self.assertEqual(target.read_bytes(), b'old')

    def test_reject_traversal_in_package(self):
        import tempfile, zipfile
        from pathlib import Path
        from updater import prepare_replacement
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / 'PortableAI.app'; target.mkdir()
            staged = root / 'release.zip'
            with zipfile.ZipFile(staged, 'w') as archive:
                archive.writestr('PortableAI.app/../../disk.data', 'bad')
            with self.assertRaises(ValueError): prepare_replacement(staged, target)
            self.assertFalse((root.parent / 'disk.data').exists())

    def test_safe_app_symlink_is_restored_and_escape_is_rejected(self):
        import stat
        import tempfile
        import zipfile
        from pathlib import Path
        from unittest.mock import patch
        from updater import prepare_replacement

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / 'PortableAI.app'; target.mkdir()
            staged = root / 'safe.zip'
            with zipfile.ZipFile(staged, 'w') as archive:
                archive.writestr('PortableAI.app/Contents/MacOS/PortableAI', b'app')
                archive.writestr('PortableAI.app/Contents/Frameworks/lib.dylib', b'library')
                link = zipfile.ZipInfo('PortableAI.app/Contents/Resources/lib.dylib')
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, '../Frameworks/lib.dylib')
            with patch('updater.sys.platform', 'linux'):
                replacement = prepare_replacement(staged, target)
            try:
                restored = replacement / 'Contents/Resources/lib.dylib'
                self.assertTrue(restored.is_symlink())
                self.assertEqual(restored.readlink().as_posix(), '../Frameworks/lib.dylib')
            finally:
                import shutil
                shutil.rmtree(replacement)

            unsafe = root / 'unsafe.zip'
            with zipfile.ZipFile(unsafe, 'w') as archive:
                link = zipfile.ZipInfo('PortableAI.app/Contents/Resources/escape')
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, '../../../disk.data')
            with self.assertRaises(ValueError): prepare_replacement(unsafe, target)

    def test_helper_argument_order(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from updater import launch_update_helper
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / 'PortableAI.exe'; target.write_bytes(b'old')
            replacement = root / '.portableai-update-test'; replacement.write_bytes(b'new')
            helper = root / 'helper'; helper.mkdir()
            with patch('updater.tempfile.mkdtemp', return_value=str(helper)), patch('subprocess.Popen') as process:
                launch_update_helper(target, replacement)
                args = process.call_args.args[0]
                self.assertEqual(args[1], '--apply-update')
                self.assertEqual(args[2], str(os.getpid()))
                self.assertEqual(args[3:], [str(target), str(replacement)])

    def test_helper_applies_update_without_touching_user_data(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from updater import helper_main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / 'PortableAI.exe'; target.write_bytes(b'old')
            replacement = root / '.portableai-update-test'; replacement.write_bytes(b'new')
            data = root / 'disk.data'; data.write_bytes(b'user data')
            with patch('updater.wait_for_process') as wait, patch('subprocess.Popen'):
                backup = helper_main(123, target, replacement)
            wait.assert_called_once_with(123)
            self.assertEqual(target.read_bytes(), b'new')
            self.assertEqual(data.read_bytes(), b'user data')
            self.assertEqual(backup.read_bytes(), b'old')
