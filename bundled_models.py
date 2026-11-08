"""Fixed, offline model assets included in every PortableAI build."""
from pathlib import Path
import sys


MODEL_FILES = {
    'llm': 'qwen2.5-0.5b-instruct-q4_k_m.gguf',
    'embedding': 'multilingual-e5-small-f16.gguf',
}

MODEL_SHA256 = {
    'llm': '74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db',
    'embedding': '6a5b2dfb958f98c851028f8c08424458249359ea921314be378f8b82088dcac1',
}


def model_path(kind):
    filename = MODEL_FILES[kind]
    executable_root = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    candidates = (
        executable_root / 'models' / filename,
        executable_root.parent / 'Resources' / 'models' / filename,
        Path(__file__).parent / 'bundle-models' / filename,
    )
    return next((path for path in candidates if path.is_file()), None)


def model_fingerprint(kind):
    """Stable model version used to invalidate incompatible stored vectors."""
    return MODEL_SHA256[kind]
