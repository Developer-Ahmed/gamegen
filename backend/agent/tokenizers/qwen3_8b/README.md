# Qwen3-8B Tokenizer

This directory contains the tokenizer used by the local `qwen3:8b` model so the
agent can count the exact tokens in Ollama's rendered prompt before inference.

- Source: https://huggingface.co/Qwen/Qwen3-8B/blob/main/tokenizer.json
- SHA-256: `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`
- Upstream license: Apache-2.0

`LocalOllamaChatModel` verifies the checksum before loading this file. If the
runtime model changes, replace this tokenizer and update the pinned checksum at
the same time; token accounting is model-specific.
