import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class BuildToolTest(unittest.TestCase):
    def test_model_fetcher_imports_from_outside_repository(self):
        script = Path(__file__).resolve().parents[1] / 'tools' / 'fetch_models.py'
        code = f"import runpy; runpy.run_path({str(script)!r}, run_name='library_only')"
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([sys.executable, '-c', code], cwd=directory, check=True)


if __name__ == '__main__':
    unittest.main()
