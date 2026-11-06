"""Persist label PDFs and idempotent ship results on disk."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LABEL_SUBDIR = "labels"
_IDEM_SUBDIR = "idempotency"
_SAFE_LABEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _root() -> Path:
    raw = (os.getenv("FEDEX_LABEL_DIR") or os.getenv("FEDEX_DATA_DIR") or "/tmp/fedex_data").strip()
    root = Path(raw).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / _LABEL_SUBDIR).mkdir(parents=True, exist_ok=True)
    (root / _IDEM_SUBDIR).mkdir(parents=True, exist_ok=True)
    return root


def _sanitize_label_id(label_id: str | None) -> str | None:
    """Validate label_id to prevent path traversal."""
    cleaned = (label_id or "").strip()
    if not cleaned or not _SAFE_LABEL_ID_RE.fullmatch(cleaned):
        return None
    return cleaned


def _ensure_within_root(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Resolved path escapes label storage root")
    return resolved


def new_label_id() -> str:
    return str(uuid.uuid4())


def save_label_pdf(label_id: str, tenant_id: str, pdf_bytes: bytes) -> Path:
    safe_label = _sanitize_label_id(label_id)
    if not safe_label:
        raise ValueError("Invalid label_id")
    root = _root()
    safe_tenant = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id)[:64] or "default"
    path = _ensure_within_root(root / _LABEL_SUBDIR / safe_tenant / f"{safe_label}.pdf", root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf_bytes)
    return path


def load_label_pdf(label_id: str, tenant_id: str) -> bytes | None:
    safe_label = _sanitize_label_id(label_id)
    if not safe_label:
        return None
    root = _root()
    safe_tenant = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id)[:64] or "default"
    try:
        path = _ensure_within_root(root / _LABEL_SUBDIR / safe_tenant / f"{safe_label}.pdf", root)
        if not path.is_file():
            # legacy flat path
            flat = _ensure_within_root(root / _LABEL_SUBDIR / f"{safe_label}.pdf", root)
            if flat.is_file():
                return flat.read_bytes()
            return None
        return path.read_bytes()
    except ValueError:
        return None


def _idem_path(tenant_id: str, idempotency_key: str) -> Path:
    root = _root()
    safe_tenant = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id)[:64] or "default"
    safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in idempotency_key)[:128]
    if not safe_key:
        raise ValueError("Invalid idempotency_key")
    return _ensure_within_root(root / _IDEM_SUBDIR / safe_tenant / f"{safe_key}.json", root)


def get_idempotent_result(tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
    try:
        path = _idem_path(tenant_id, idempotency_key)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_idempotent_result(tenant_id: str, idempotency_key: str, result: dict[str, Any]) -> None:
    path = _idem_path(tenant_id, idempotency_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**result, "saved_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
