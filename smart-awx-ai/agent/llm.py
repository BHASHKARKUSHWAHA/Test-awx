"""Local LLM client (spec sections 13, 14, 36, 53).

Talks to Ollama on localhost over HTTP with the standard library only.  No
external AI API is ever contacted: there is no code path in this file that can
reach anything other than ``OLLAMA_URL``.

The model is used for two things and nothing else:

* choosing which declared tool answers a request (``plan``);
* turning tool output into prose (``summarise``).

It is never the source of a fact about the machine.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
PRIMARY_MODEL = os.environ.get("AI_MODEL", "qwen2.5:3b-instruct-q4_K_M")
FALLBACK_MODELS = [m for m in (os.environ.get("AI_MODEL_FALLBACKS", "") or "").split(",") if m.strip()]
NUM_CTX = int(os.environ.get("AI_NUM_CTX", "4096"))
KEEP_ALIVE = os.environ.get("AI_KEEP_ALIVE", "5m")
REQUEST_TIMEOUT = int(os.environ.get("AI_TIMEOUT", "180"))


class LLMUnavailable(RuntimeError):
    """Raised when no local model can answer; the agent then degrades, not lies."""


@dataclass
class LLMReply:
    text: str
    model: str
    duration_ms: int
    eval_count: int = 0


class OllamaClient:
    def __init__(self, url: str = OLLAMA_URL, model: str = PRIMARY_MODEL, fallbacks: Optional[List[str]] = None) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.fallbacks = [m.strip() for m in (fallbacks if fallbacks is not None else FALLBACK_MODELS) if m.strip()]
        self._active_model = model
        self._guard = threading.Lock()

    # -- plumbing -------------------------------------------------------
    def _post(self, path: str, body: Dict[str, Any], timeout: int, stream: bool = False):
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        return urllib.request.urlopen(request, timeout=timeout)

    def available_models(self) -> List[str]:
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            return [m.get("name", "") for m in payload.get("models", [])]
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return []

    def health(self) -> Dict[str, Any]:
        models = self.available_models()
        reachable = bool(models) or self._ping()
        return {
            "reachable": reachable,
            "url": self.url,
            "configured_model": self.model,
            "active_model": self._active_model,
            "models_present": models,
            "model_ready": self.model in models,
            "fallbacks": self.fallbacks,
        }

    def _ping(self) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/api/version", timeout=5) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def candidates(self) -> List[str]:
        present = self.available_models()
        ordered = [self.model] + [m for m in self.fallbacks if m != self.model]
        usable = [m for m in ordered if m in present]
        # If none of the configured names are present but something is, use it:
        # a working small model beats refusing to answer.
        if not usable and present:
            usable = present[:1]
        return usable

    # -- generation -----------------------------------------------------
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.1,
        timeout: Optional[int] = None,
        json_mode: bool = False,
        max_tokens: int = 1024,
    ) -> LLMReply:
        last_error: Optional[Exception] = None
        for model in self.candidates() or [self.model]:
            body: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "keep_alive": KEEP_ALIVE,
                "options": {"temperature": temperature, "num_ctx": NUM_CTX, "num_predict": max_tokens},
            }
            if json_mode:
                body["format"] = "json"
            started = time.time()
            try:
                with self._post("/api/chat", body, timeout or REQUEST_TIMEOUT) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
                text = (payload.get("message") or {}).get("content", "")
                with self._guard:
                    self._active_model = model
                return LLMReply(text=text.strip(), model=model, duration_ms=int((time.time() - started) * 1000), eval_count=payload.get("eval_count", 0))
            except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
                last_error = exc
                continue
        raise LLMUnavailable("no local model could answer (%s): %s" % (self.url, last_error))

    def chat_stream(self, messages: List[Dict[str, str]], temperature: float = 0.2, max_tokens: int = 1024) -> Iterator[str]:
        model = (self.candidates() or [self.model])[0]
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": temperature, "num_ctx": NUM_CTX, "num_predict": max_tokens},
        }
        try:
            with self._post("/api/chat", body, REQUEST_TIMEOUT, stream=True) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError:
                        continue
                    piece = (chunk.get("message") or {}).get("content", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise LLMUnavailable("streaming from %s failed: %s" % (self.url, exc))


_client: Optional[OllamaClient] = None


def client() -> OllamaClient:
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client


def reset_client() -> None:
    global _client
    _client = None


# --------------------------------------------------------------------------
# JSON helpers - small models wrap JSON in prose and code fences
# --------------------------------------------------------------------------
_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def extract_json(text: str) -> Optional[Any]:
    """Pull the first JSON object or array out of a model response."""
    if not text:
        return None
    candidates: List[str] = []
    match = _FENCE.search(text)
    if match:
        candidates.append(match.group(1).strip())
    stripped = text.strip()
    candidates.append(stripped)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None
