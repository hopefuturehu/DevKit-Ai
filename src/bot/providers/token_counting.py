"""Offline DeepSeek V4 input counting; installation is explicit, never in a request."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import OrderedDict
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from tokenizers import Tokenizer

from bot.core.context import TokenEstimator
from bot.core.models import InputTokenEstimate, ModelRequest
from bot.providers._vendor.encoding_dsv4 import encode_messages

REVISION = "60d8d70770c6776ff598c94bb586a859a38244f1"
TOKENIZER_SHA256 = "8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf"
TOKENIZER_URL = (
    f"https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/resolve/{REVISION}/tokenizer.json"
)
MODEL = "deepseek-v4-flash"
# Pro's official tokenizer and encoder are byte-identical to the pinned Flash
# assets (Pro revision b5968e9190ef611bbf34a7229255be88a0e937c1).
MODEL_REVISIONS = {MODEL: REVISION, "deepseek-v4-pro": "b5968e9190ef611bbf34a7229255be88a0e937c1"}


def tokenizer_path() -> Path:
    if configured := os.environ.get("BOT_DEEPSEEK_TOKENIZER_PATH"):
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "bot" / "tokenizers" / MODEL / REVISION / "tokenizer.json"


@lru_cache(maxsize=2)
def load_tokenizer(path: Path) -> Tokenizer:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("DeepSeek tokenizer checksum mismatch")
    tokenizer = Tokenizer.from_str(content.decode("utf-8"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


def install_tokenizer() -> Path:
    """Download only the pinned vocabulary, atomically, without model credentials."""
    path = tokenizer_path()
    try:
        load_tokenizer(path)
        return path
    except (OSError, ValueError):
        pass
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        response = client.get(TOKENIZER_URL)
        response.raise_for_status()
        content = response.content
    if hashlib.sha256(content).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("DeepSeek tokenizer checksum mismatch")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    load_tokenizer.cache_clear()
    load_tokenizer(path)
    return path


def render_deepseek_input(payload: dict[str, Any]) -> str:
    """Use the effective HTTP payload and upstream chat/reasoning serialization."""
    messages = deepcopy(payload["messages"])
    if payload.get("tools"):
        if not messages or messages[0]["role"] != "system":
            messages.insert(0, {"role": "system", "content": ""})
        messages[0]["tools"] = deepcopy(payload["tools"])
    mode = "chat" if payload.get("thinking", {}).get("type") == "disabled" else "thinking"
    return encode_messages(messages, thinking_mode=mode)


class DeepSeekInputCounter:
    def __init__(self) -> None:
        # Store only hashes and counts; never retain historical prompts in this cache.
        self._counts: OrderedDict[str, int] = OrderedDict()
        self._fallback = TokenEstimator()

    def estimate(self, request: ModelRequest, payload: dict[str, Any]) -> InputTokenEstimate:
        # Rendering assumes the documented default, but telemetry must not
        # mistake an omitted parameter for an explicitly enabled request.
        mode = payload.get("thinking", {}).get("type", "provider_default")
        source = f"{request.model}:{MODEL_REVISIONS.get(request.model, REVISION)}"
        try:
            tokenizer = load_tokenizer(tokenizer_path())
            key = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            tokens = self._counts.get(key)
            if tokens is None:
                prompt = render_deepseek_input(payload)
                tokens = len(tokenizer.encode(prompt, add_special_tokens=False).ids)
                self._counts[key] = tokens
                if len(self._counts) > 128:
                    self._counts.popitem(last=False)
            else:
                self._counts.move_to_end(key)
            # Engineering reserve, not a statistical confidence bound. Existing
            # output/protocol reserves are accounted for separately by the caller.
            budget = tokens + max(256, math.ceil(tokens * 0.05))
        except (OSError, ValueError, TypeError, KeyError, AssertionError, NotImplementedError):
            tokens = self._fallback.request(request.messages, request.tools)
            # A missing tokenizer must not silently restore the unsafe old budget.
            # This is only a degraded fallback, not a calibrated tokenizer result.
            budget = tokens + max(2048, math.ceil(tokens * 0.5))
            source = "heuristic:deepseek_tokenizer_unavailable"
        return InputTokenEstimate(
            tokens=tokens, budget_tokens=budget, source=source, effective_thinking=mode
        )
