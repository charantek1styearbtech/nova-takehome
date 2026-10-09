"""Disk state under --state-dir. The process restarts between batches, so everything needed by
a later `report` or `feedback` call lives here as JSON.

Layout inside <state-dir>:
    docs/<safe_doc_id>.json   one file per extracted document (universal schema, see understand.py)
    docs_index.json           {doc_id: received_date} for the as_of filter
    facts.json                feedback-derived facts (standing + one_off), applied by later reports
    closed_keys.json          keys a person closed: stop raising them
    learned.json              {finding_key: {feedback_item_id, batch}} for learned_at tagging
    seen_batches.json         which batch folders we have ingested (idempotence)
"""
from __future__ import annotations

import json
from pathlib import Path


def _read(path: Path, default):
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


# ---- documents --------------------------------------------------------------------------------------------------------
def docs_dir(state_dir: str) -> Path:
    d = Path(state_dir) / "docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(doc_id: str) -> str:
    return doc_id.replace("/", "__").replace("#", "__AT__")


def save_doc(state_dir: str, doc: dict) -> None:
    _write(docs_dir(state_dir) / f"{_safe_name(doc['doc_id'])}.json", doc)


def load_all_docs(state_dir: str, as_of: str | None = None) -> list[dict]:
    """Every stored document, optionally filtered to received <= as_of (YYYY-MM-DD)."""
    out: list[dict] = []
    for f in sorted(docs_dir(state_dir).glob("*.json")):
        doc = _read(f, None)
        if not doc:
            continue
        if as_of is not None:
            received = str(doc.get("received_date") or "9999-12-31")[:10]
            if received > as_of:
                continue
        out.append(doc)
    return out


def load_docs_index(state_dir: str) -> dict:
    return _read(Path(state_dir) / "docs_index.json", {})


def save_docs_index(state_dir: str, index: dict) -> None:
    _write(Path(state_dir) / "docs_index.json", index)


# ---- batches (idempotence) --------------------------------------------------------------------------------------------
def seen_batches(state_dir: str) -> set[str]:
    return set(_read(Path(state_dir) / "seen_batches.json", []))


def mark_batch_seen(state_dir: str, batch: str) -> None:
    seen = seen_batches(state_dir)
    seen.add(batch)
    _write(Path(state_dir) / "seen_batches.json", sorted(seen))


# ---- feedback facts ---------------------------------------------------------------------------------------------------
def load_facts(state_dir: str) -> list[dict]:
    return _read(Path(state_dir) / "facts.json", [])


def save_facts(state_dir: str, facts: list[dict]) -> None:
    _write(Path(state_dir) / "facts.json", facts)


def load_closed_keys(state_dir: str) -> dict:
    return _read(Path(state_dir) / "closed_keys.json", {})


def save_closed_keys(state_dir: str, closed: dict) -> None:
    _write(Path(state_dir) / "closed_keys.json", closed)


def load_learned(state_dir: str) -> dict:
    return _read(Path(state_dir) / "learned.json", {})


def save_learned(state_dir: str, learned: dict) -> None:
    _write(Path(state_dir) / "learned.json", learned)
