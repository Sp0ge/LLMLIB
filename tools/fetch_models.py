"""Fetch the two fixed public GGUF assets and verify their pinned SHA-256."""
import hashlib
import sys
import tempfile
import urllib.request
from pathlib import Path

# `python tools/fetch_models.py` places only tools/ on sys.path in clean CI jobs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bundled_models import MODEL_FILES, MODEL_SHA256


SOURCES = {
    'llm': 'https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf',
    'embedding': 'https://huggingface.co/hdrrayan/multilingual-e5-small-gguf/resolve/main/multilingual-e5-small-f16.gguf',
}


def fetch(kind, directory):
    destination = Path(directory) / MODEL_FILES[kind]
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(SOURCES[kind], headers={'User-Agent': 'PortableAI-build'})
    digest = hashlib.sha256()
    temporary = None
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.model-', delete=False) as output:
                temporary = Path(output.name)
                while True:
                    block = response.read(4 * 1024 * 1024)
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                output.flush()
        if digest.hexdigest() != MODEL_SHA256[kind]:
            raise ValueError(f'Unexpected SHA-256 for {kind}: {digest.hexdigest()}')
        temporary.replace(destination)
        print(f'{kind}: verified {destination.name}')
    except BaseException:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    target = Path('bundle-models')
    for name in MODEL_FILES:
        fetch(name, target)
