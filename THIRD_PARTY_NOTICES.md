# Third-party model notices

PortableAI bundles the following fixed model files for offline use:

- **Qwen2.5-0.5B-Instruct GGUF, Q4_K_M** — converted GGUF from `Qwen/Qwen2.5-0.5B-Instruct-GGUF`, based on the Qwen2.5-0.5B-Instruct model. The upstream model is licensed under Apache License 2.0. Copyright and license terms are provided by the upstream project. See <https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct> and <https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF>.
- **multilingual-e5-small GGUF, F16** — conversion of `intfloat/multilingual-e5-small`, distributed by `hdrrayan/multilingual-e5-small-gguf`. The model is licensed under MIT License. See <https://huggingface.co/intfloat/multilingual-e5-small> and <https://huggingface.co/hdrrayan/multilingual-e5-small-gguf>.

The exact model binaries are checked against SHA-256 digests in `bundled_models.py` during packaging. Their upstream licenses remain applicable to the bundled files.
