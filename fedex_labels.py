"""Persist label PDFs and idempotent ship results on disk."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LABEL_SUBDIR = "labels"
_IDEM_SUBDIR = "idempotency"


def _root() -> Path:
    raw = (os.getenv("FEDEX_LABEL_DIR") or os.getenv("FEDEX_DATA_DIR") or "/tmp/fedex_data").strip()
    root = Path(raw)
    root.mkdir(parents=True, exist_ok=True)
    (root / _LABEL_SUBDIR).mkdir(parents=True, exist_ok=True)
    (root / _IDEM_SUBDIR).mkdir(parents=True, exist_ok=True)
    return root


def new_label_id() -> str:
    return str(uuid.uuid4())


def save_label_pdf(label_id: str, tenant_id: str, pdf_bytes: bytes) -> Path:
    safe_tenant = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id)[:64]
    path = _root() / _LABEL_SUBDIR / safe_tenant / f"{label_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf_bytes)
    return path


def load_label_pdf(label_id: str, tenant_id: str) -> bytes | None:
    safe_tenant = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id)[:64]
    path = _root() / _LABEL_SUBDIR / safe_tenant / f"{label_id}.pdf"
    if not path.is_file():
        # legacy flat path
        flat = _root() / _LABEL_SUBDIR / f"{label_id}.pdf"
        if flat.is_file():
            return flat.read_bytes()
        return None
    return path.read_bytes()


def _idem_path(tenant_id: str, idempotency_key: str) -> Path:
    safe_tenant = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id)[:64]
    safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in idempotency_key)[:128]
    return _root() / _IDEM_SUBDIR / safe_tenant / f"{safe_key}.json"


def get_idempotent_result(tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
    path = _idem_path(tenant_id, idempotency_key)
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
