"""FedEx Rate + Ship API clients."""

from __future__ import annotations

import base64
import uuid
from datetime import date
from typing import Any

import httpx

from fedex_auth import get_access_token
from fedex_config import FedExCredentials
from fedex_ship_schema import (
    PackageDims,
    PartyAddress,
    ShipSheetRow,
    parse_service_types_from_rate_response,
)


def _party_contact(party: PartyAddress) -> dict[str, Any]:
    contact: dict[str, Any] = {
        "personName": party.name[:70],
        "phoneNumber": party.phone[:15],
    }
    if party.email:
        contact["emailAddress"] = party.email[:80]
    return {"contact": contact}


def _party_address(party: PartyAddress) -> dict[str, Any]:
    street = [party.address1]
    if party.address2:
        street.append(party.address2)
    addr: dict[str, Any] = {
        "streetLines": street[:2],
        "city": party.city,
        "postalCode": party.postal,
        "countryCode": party.country,
    }
    if party.state:
        addr["stateOrProvinceCode"] = party.state
    return addr


def _shipper_block(party: PartyAddress) -> dict[str, Any]:
    return {**_party_contact(party), "address": _party_address(party)}


def _recipient_block(party: PartyAddress) -> dict[str, Any]:
    return {**_party_contact(party), "address": _party_address(party)}


def _package_line_item(pkg: PackageDims) -> dict[str, Any]:
    units, value = pkg.weight_for_fedex()
    item: dict[str, Any] = {
        "weight": {"units": units, "value": round(value, 3)},
    }
    dims = pkg.dimensions_for_fedex()
    if dims:
        item["dimensions"] = dims
    return item


def _requested_shipment_base(
    shipper: PartyAddress,
    recipient: PartyAddress,
    pkg: PackageDims,
    service_type: str | None = None,
) -> dict[str, Any]:
    ship_date = date.today().isoformat()
    body: dict[str, Any] = {
        "shipDatestamp": ship_date,
        "pickupType": "USE_SCHEDULED_PICKUP",
        "packagingType": "YOUR_PACKAGING",
        "shipper": _shipper_block(shipper),
        "recipients": [_recipient_block(recipient)],
        "shippingChargesPayment": {"paymentType": "SENDER"},
        "requestedPackageLineItems": [_package_line_item(pkg)],
    }
    if service_type:
        body["serviceType"] = service_type
    return body


def _fedex_headers(creds: FedExCredentials, token: str, transaction_id: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-locale": creds.locale,
        "x-customer-transaction-id": transaction_id or str(uuid.uuid4()),
    }


async def fetch_rate_services(
    creds: FedExCredentials,
    shipper: PartyAddress,
    recipient: PartyAddress,
    pkg: PackageDims,
) -> tuple[set[str], str | None, dict[str, Any] | None]:
    """Return (service_types, error, raw_response)."""
    if not creds.account_number:
        return set(), "FEDEX_ACCOUNT_NUMBER (or credentials.account_number) is required for rate quotes.", None

    token, err = await get_access_token(creds)
    if err:
        return set(), err, None

    body = {
        "accountNumber": {"value": creds.account_number},
        "requestedShipment": {
            **_requested_shipment_base(shipper, recipient, pkg, service_type=None),
            "rateRequestType": ["ACCOUNT", "LIST"],
        },
    }
    base = creds.base_url()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base}/rate/v1/rates/quotes",
                headers=_fedex_headers(creds, token),
                json=body,
            )
    except httpx.HTTPError as exc:
        return set(), f"{type(exc).__name__}: {exc}", None

    text = resp.text or ""
    if resp.status_code >= 400:
        return set(), f"FedEx rate HTTP {resp.status_code}: {text[:500]}", None

    try:
        data = resp.json()
    except ValueError:
        return set(), "FedEx rate response was not JSON.", None

    return parse_service_types_from_rate_response(data), None, data


async def create_shipment(
    creds: FedExCredentials,
    row: ShipSheetRow,
    transaction_id: str,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """
    Create shipment and return success dict:
      tracking_number, pdf_bytes, service_type, raw
    """
    if not creds.account_number:
        return None, "FEDEX_ACCOUNT_NUMBER (or credentials.account_number) is required to ship.", None
    if not row.service_type:
        return None, "service_type is required", None

    token, err = await get_access_token(creds)
    if err:
        return None, err, None

    body = {
        "labelResponseOptions": "LABEL",
        "accountNumber": {"value": creds.account_number},
        "requestedShipment": {
            **_requested_shipment_base(
                row.shipper,
                row.recipient,
                row.package,
                service_type=str(row.service_type).strip(),
            ),
            "labelSpecification": {
                "imageType": "PDF",
                "labelStockType": "STOCK_4X6",
            },
        },
    }
    base = creds.base_url()
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{base}/ship/v1/shipments",
                headers=_fedex_headers(creds, token, transaction_id),
                json=body,
            )
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}", None

    text = resp.text or ""
    if resp.status_code >= 400:
        return None, f"FedEx ship HTTP {resp.status_code}: {text[:800]}", None

    try:
        data = resp.json()
    except ValueError:
        return None, "FedEx ship response was not JSON.", None

    try:
        output = data.get("output") or {}
        txs = output.get("transactionShipments") or []
        if not txs:
            return None, "FedEx ship response missing transactionShipments.", data
        tx = txs[0]
        tracking = (tx.get("masterTrackingNumber") or "").strip()
        pieces = tx.get("pieceResponses") or []
        pdf_bytes: bytes | None = None
        for piece in pieces:
            for doc in piece.get("packageDocuments") or []:
                if (doc.get("contentType") or "").upper() == "LABEL":
                    encoded = doc.get("encodedLabel") or doc.get("url")
                    if encoded and not str(encoded).startswith("http"):
                        pdf_bytes = base64.b64decode(encoded)
                        break
            if pdf_bytes:
                break
        if not tracking:
            return None, "FedEx ship response missing tracking number.", data
        if not pdf_bytes:
            return None, "FedEx ship response missing label PDF (encodedLabel).", data
        return (
            {
                "tracking_number": tracking,
                "pdf_bytes": pdf_bytes,
                "service_type": str(row.service_type).strip(),
            },
            None,
            data,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"Failed to parse FedEx ship response: {exc}", data
