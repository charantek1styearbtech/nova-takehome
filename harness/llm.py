"""JSON-only LLM client: NOVA_LLM_* (OpenAI-compatible) for local dev, ANTHROPIC_* for the grader."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


def _load_dotenv() -> None:
    """Read <repo>/.env for local dev (gitignored). Never overrides a real environment variable.
    The grader doesn't ship .env, so its ANTHROPIC_* variables are used there instead."""
    for candidate in (Path(__file__).resolve().parent.parent / ".env",
                      Path.cwd() / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
        break


_load_dotenv()


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return ""


OPENAI_KEY = _env("NOVA_LLM_API_KEY", "OPENAI_API_KEY")
OPENAI_BASE = _env("NOVA_LLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE")
OPENAI_MODEL = _env("NOVA_LLM_MODEL", "OPENAI_MODEL")
ANTHROPIC_KEY = _env("ANTHROPIC_API_KEY")
ANTHROPIC_BASE = _env("ANTHROPIC_BASE_URL")


def mode() -> str:
    """'anthropic' inside the grading container (its key wins when both exist), 'openai' for our
    dev key, 'none' if no key."""
    if ANTHROPIC_KEY:
        return "anthropic"
    if OPENAI_KEY:
        return "openai"
    return "none"


def _extract_json(text: str):
    """Pull the first JSON object/array out of a model response (handles ```json fences)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"model did not return parseable JSON: {text[:300]!r}")


def _post_openai(payload: dict) -> str:
    import urllib.request
    base = OPENAI_BASE.rstrip("/") if OPENAI_BASE else "https://api.openai.com/v1"
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _post_anthropic(payload: dict) -> str:
    import urllib.request
    base = ANTHROPIC_BASE.rstrip("/") if ANTHROPIC_BASE else "https://api.anthropic.com"
    req = urllib.request.Request(
        f"{base}/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(b.get("text", "") for b in data.get("content", []))


def ask_json(system: str, user: str, max_tokens: int = 4000, temperature: float = 0.0, retries: int = 3):
    """Ask the model for JSON, return the parsed object. Raises if no key or unparseable."""
    m = mode()
    if m == "none":
        raise RuntimeError("no LLM key: set NOVA_LLM_API_KEY (dev) or ANTHROPIC_API_KEY (grader)")
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            if m == "openai":
                content = _post_openai({
                    "model": OPENAI_MODEL or "gpt-4o-mini",
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                })
            else:
                content = _post_anthropic({
                    "model": "claude-sonnet-4-5",
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                })
            return _extract_json(content)
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"LLM call failed after {retries} tries: {last_err}")


def ask_text(system: str, user: str, max_tokens: int = 4000, temperature: float = 0.0) -> str:
    """Like ask_json but returns raw text (used for OCR of scans)."""
    m = mode()
    if m == "none":
        raise RuntimeError("no LLM key: set NOVA_LLM_API_KEY (dev) or ANTHROPIC_API_KEY (grader)")
    if m == "openai":
        return _post_openai({
            "model": OPENAI_MODEL or "gpt-4o-mini",
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        })
    return _post_anthropic({
        "model": "claude-sonnet-4-5",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    })

