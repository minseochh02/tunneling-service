"""FedEx Ship — validate, rate, batch ship, label PDF."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from fedex_config import credentials_from_payload
from fedex_labels import (
    get_idempotent_result,
    load_label_pdf,
    new_label_id,
    save_idempotent_result,
    save_label_pdf,
)
from fedex_ship_schema import (
    RateQuoteRequest,
    RowShipResult,
    ShipBatchRequest,
    ShipValidateRequest,
    ShipSheetRow,
)
from fedex_ship_service import create_shipment, fetch_rate_services

fedex_ship_router = APIRouter(prefix="/fedex", tags=["FedEx Ship"])


def _public_base(request: Request) -> str:
    explicit = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    return str(request.base_url).rstrip("/")


def _label_url(request: Request, label_id: str) -> str:
    return f"{_public_base(request)}/fedex/labels/{label_id}"


def _resolve_creds(tenant_id: str, override: Any):
    creds = credentials_from_payload(
        tenant_id,
        override.model_dump(exclude_none=True) if override else None,
    )
    if not creds:
        return None, "Set FEDEX_CLIENT_ID and FEDEX_CLIENT_SECRET (or pass credentials in request)."
    return creds, None


@fedex_ship_router.post("/ship/validate")
async def ship_validate(body: ShipValidateRequest):
    """Validate sheet rows without calling FedEx Ship."""
    results: list[dict[str, Any]] = []
    for row in body.rows:
        errors = row.validate_for_ship(allowed_services=None)
        status = "validated" if not errors else "error"
        results.append(
            RowShipResult(
                row_id=row.row_id,
                row_index=row.row_index,
                ship_status=status,
                validation_errors=errors,
                fedex_ship_error="; ".join(errors) if errors else None,
            ).model_dump()
        )
    return {"ok": True, "results": results}


@fedex_ship_router.post("/ship/rate")
async def ship_rate(body: RateQuoteRequest, request: Request):
    """Return service types available for origin/destination/weight."""
    creds, err = _resolve_creds(body.tenant_id, body.credentials)
    if err:
        return JSONResponse(status_code=503, content={"error": err})

    services, rate_err, raw = await fetch_rate_services(
        creds,
        body.shipper,
        body.recipient,
        body.package,
    )
    if rate_err:
        return JSONResponse(status_code=502, content={"error": rate_err})

    return {
        "ok": True,
        "apiBase": creds.base_url(),
        "allowed_services": sorted(services),
        "count": len(services),
        "raw": raw if os.getenv("FEDEX_DEBUG") else None,
    }


@fedex_ship_router.post("/ship/batch")
async def ship_batch(body: ShipBatchRequest, request: Request):
    """Validate and ship many rows; one PDF label per row."""
    creds, err = _resolve_creds(body.tenant_id, body.credentials)
    if err:
        return JSONResponse(status_code=503, content={"error": err})

    tenant_id = body.tenant_id or "default"
    results: list[dict[str, Any]] = []

    for row in body.rows:
        base_result = RowShipResult(
            row_id=row.row_id,
            row_index=row.row_index,
            ship_status="error",
        )

        if row.fedex_tracking_number:
            results.append(
                RowShipResult(
                    row_id=row.row_id,
                    row_index=row.row_index,
                    ship_status="skipped",
                    fedex_tracking_number=row.fedex_tracking_number,
                    fedex_ship_error="already_shipped",
                ).model_dump()
            )
            continue

        idem_key = row.idempotency_key()
        prior = get_idempotent_result(tenant_id, idem_key)
        if prior and prior.get("fedex_tracking_number"):
            label_id = prior.get("fedex_label_id")
            results.append(
                RowShipResult(
                    row_id=row.row_id,
                    row_index=row.row_index,
                    ship_status="shipped",
                    fedex_tracking_number=prior.get("fedex_tracking_number"),
                    fedex_label_id=label_id,
                    fedex_label_url=_label_url(request, label_id) if label_id else None,
                    fedex_shipped_at=prior.get("fedex_shipped_at"),
                    service_type=prior.get("service_type"),
                ).model_dump()
            )
            continue

        allowed: set[str] | None = None
        if not body.skip_rate_check:
            allowed, rate_err, _ = await fetch_rate_services(
                creds,
                row.shipper,
                row.recipient,
                row.package,
            )
            if rate_err:
                results.append(
                    RowShipResult(
                        row_id=row.row_id,
                        row_index=row.row_index,
                        ship_status="error",
                        fedex_ship_error=rate_err,
                    ).model_dump()
                )
                continue
            base_result.allowed_services = sorted(allowed)

        validation_errors = row.validate_for_ship(allowed_services=allowed)
        if validation_errors:
            results.append(
                RowShipResult(
                    row_id=row.row_id,
                    row_index=row.row_index,
                    ship_status="error",
                    validation_errors=validation_errors,
                    fedex_ship_error="; ".join(validation_errors),
                    allowed_services=sorted(allowed) if allowed else [],
                ).model_dump()
            )
            continue

        if body.dry_run:
            results.append(
                RowShipResult(
                    row_id=row.row_id,
                    row_index=row.row_index,
                    ship_status="validated",
                    service_type=row.service_type,
                    allowed_services=sorted(allowed) if allowed else [],
                ).model_dump()
            )
            continue

        ship_data, ship_err, _raw = await create_shipment(creds, row, idem_key)
        if ship_err or not ship_data:
            results.append(
                RowShipResult(
                    row_id=row.row_id,
                    row_index=row.row_index,
                    ship_status="error",
                    fedex_ship_error=ship_err or "unknown ship error",
                    allowed_services=sorted(allowed) if allowed else [],
                ).model_dump()
            )
            continue

        label_id = new_label_id()
        save_label_pdf(label_id, tenant_id, ship_data["pdf_bytes"])
        shipped_at = datetime.now(timezone.utc).isoformat()
        tracking = ship_data["tracking_number"]
        label_url = _label_url(request, label_id)

        save_idempotent_result(
            tenant_id,
            idem_key,
            {
                "fedex_tracking_number": tracking,
                "fedex_label_id": label_id,
                "fedex_shipped_at": shipped_at,
                "service_type": ship_data.get("service_type"),
            },
        )

        print(f"📦 FedEx ship {tracking} row={row.row_id} label={label_id}")
        results.append(
            RowShipResult(
                row_id=row.row_id,
                row_index=row.row_index,
                ship_status="shipped",
                fedex_tracking_number=tracking,
                fedex_label_id=label_id,
                fedex_label_url=label_url,
                fedex_shipped_at=shipped_at,
                service_type=ship_data.get("service_type"),
                allowed_services=sorted(allowed) if allowed else [],
            ).model_dump()
        )

    return {
        "ok": True,
        "spreadsheet_id": body.spreadsheet_id,
        "tenant_id": tenant_id,
        "apiBase": creds.base_url(),
        "results": results,
    }


@fedex_ship_router.get("/labels/{label_id}")
async def get_label_pdf(
    label_id: str,
    request: Request,
    tenant_id: str = Query("default"),
    download: bool = Query(False, description="If true, force attachment download"),
):
    """Serve stored 4x6 label PDF (inline view or download)."""
    pdf = load_label_pdf(label_id, tenant_id)
    if pdf is None:
        return JSONResponse(status_code=404, content={"error": "Label not found."})

    disposition = "attachment" if download else "inline"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="fedex-label-{label_id}.pdf"',
        },
    )
