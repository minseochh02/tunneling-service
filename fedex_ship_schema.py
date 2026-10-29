"""Sheet row models and validation for FedEx Ship."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ShipStatus = Literal["draft", "validated", "shipped", "error", "skipped"]

US_CA_STATE_COUNTRIES = frozenset({"US", "CA"})


class FedExCredentialOverride(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None
    account_number: str | None = None
    use_production: bool | None = None
    api_base: str | None = None
    locale: str | None = None


class PartyAddress(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    phone: str = Field(..., min_length=1, max_length=30)
    email: str | None = None
    address1: str = Field(..., min_length=1, max_length=120)
    address2: str | None = None
    city: str = Field(..., min_length=1, max_length=80)
    state: str | None = None
    postal: str = Field(..., min_length=1, max_length=20)
    country: str = Field(..., min_length=2, max_length=2)

    @field_validator("country")
    @classmethod
    def upper_country(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("state")
    @classmethod
    def upper_state(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        return s.upper() if s else None


class PackageDims(BaseModel):
    weight_lb: float | None = Field(None, gt=0)
    weight_kg: float | None = Field(None, gt=0)
    length_cm: float | None = Field(None, gt=0)
    width_cm: float | None = Field(None, gt=0)
    height_cm: float | None = Field(None, gt=0)
    length_in: float | None = Field(None, gt=0)
    width_in: float | None = Field(None, gt=0)
    height_in: float | None = Field(None, gt=0)
    package_count: int = Field(1, ge=1, le=40)

    @model_validator(mode="after")
    def require_weight(self) -> PackageDims:
        if self.weight_lb is None and self.weight_kg is None:
            raise ValueError("weight_lb or weight_kg is required")
        return self

    def weight_for_fedex(self) -> tuple[str, float]:
        if self.weight_lb is not None:
            return "LB", float(self.weight_lb)
        assert self.weight_kg is not None
        return "KG", float(self.weight_kg)

    def dimensions_for_fedex(self) -> dict[str, Any] | None:
        if self.length_in and self.width_in and self.height_in:
            return {
                "length": int(round(self.length_in)),
                "width": int(round(self.width_in)),
                "height": int(round(self.height_in)),
                "units": "IN",
            }
        if self.length_cm and self.width_cm and self.height_cm:
            return {
                "length": int(round(self.length_cm)),
                "width": int(round(self.width_cm)),
                "height": int(round(self.height_cm)),
                "units": "CM",
            }
        return None


class ShipSheetRow(BaseModel):
    row_id: str = Field(..., min_length=1)
    row_index: int | None = None
    ship_ready: bool = False
    ship_status: ShipStatus | None = None
    fedex_tracking_number: str | None = None
    ship_idempotency_key: str | None = None

    service_type: str | None = None

    recipient: PartyAddress
    shipper: PartyAddress
    package: PackageDims

    @field_validator("fedex_tracking_number")
    @classmethod
    def strip_tracking(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = "".join(str(v).split())
        return s or None

    def idempotency_key(self) -> str:
        return (self.ship_idempotency_key or self.row_id).strip()

    def validate_for_ship(self, allowed_services: set[str] | None = None) -> list[str]:
        errors: list[str] = []
        if not self.ship_ready:
            errors.append("ship_ready must be true")
        if self.fedex_tracking_number:
            errors.append("fedex_tracking_number already set (already shipped)")
        for party, label in ((self.recipient, "recipient"), (self.shipper, "shipper")):
            if party.country in US_CA_STATE_COUNTRIES and not party.state:
                errors.append(f"{label}.state is required for {party.country}")
        if not self.service_type or not str(self.service_type).strip():
            errors.append("service_type is required")
        elif allowed_services is not None:
            st = str(self.service_type).strip()
            if st not in allowed_services:
                errors.append(
                    f"service_type '{st}' is not in allowed services for this lane"
                )
        return errors


class ShipValidateRequest(BaseModel):
    tenant_id: str = "default"
    spreadsheet_id: str | None = None
    rows: list[ShipSheetRow]
    credentials: FedExCredentialOverride | None = None


class ShipBatchRequest(BaseModel):
    tenant_id: str = "default"
    spreadsheet_id: str | None = None
    dry_run: bool = False
    skip_rate_check: bool = False
    rows: list[ShipSheetRow]
    credentials: FedExCredentialOverride | None = None


class RateQuoteRequest(BaseModel):
    tenant_id: str = "default"
    recipient: PartyAddress
    shipper: PartyAddress
    package: PackageDims
    credentials: FedExCredentialOverride | None = None


class RowShipResult(BaseModel):
    row_id: str
    row_index: int | None = None
    ship_status: ShipStatus
    fedex_tracking_number: str | None = None
    fedex_label_id: str | None = None
    fedex_label_url: str | None = None
    fedex_ship_error: str | None = None
    fedex_shipped_at: str | None = None
    service_type: str | None = None
    validation_errors: list[str] = Field(default_factory=list)
    allowed_services: list[str] = Field(default_factory=list)


def row_from_sheet_dict(data: dict[str, Any]) -> ShipSheetRow:
    """Map flat sheet column names to ShipSheetRow."""

    def party(prefix: str) -> dict[str, Any]:
        return {
            "name": data.get(f"{prefix}_name") or data.get(f"{prefix}Name"),
            "phone": data.get(f"{prefix}_phone") or data.get(f"{prefix}Phone"),
            "email": data.get(f"{prefix}_email") or data.get(f"{prefix}Email"),
            "address1": data.get(f"{prefix}_address1") or data.get(f"{prefix}Address1"),
            "address2": data.get(f"{prefix}_address2") or data.get(f"{prefix}Address2"),
            "city": data.get(f"{prefix}_city") or data.get(f"{prefix}City"),
            "state": data.get(f"{prefix}_state") or data.get(f"{prefix}State"),
            "postal": data.get(f"{prefix}_postal") or data.get(f"{prefix}Postal"),
            "country": data.get(f"{prefix}_country") or data.get(f"{prefix}Country"),
        }

    def truthy(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        return str(v).strip().upper() in ("Y", "YES", "TRUE", "1", "✓", "CHECKED")

    pkg: dict[str, Any] = {}
    for key in (
        "weight_lb",
        "weight_kg",
        "length_cm",
        "width_cm",
        "height_cm",
        "length_in",
        "width_in",
        "height_in",
        "package_count",
    ):
        if key in data and data[key] is not None and str(data[key]).strip() != "":
            pkg[key] = data[key]

    return ShipSheetRow(
        row_id=str(data.get("row_id") or data.get("rowId") or ""),
        row_index=data.get("row_index") or data.get("rowIndex"),
        ship_ready=truthy(data.get("ship_ready") or data.get("shipReady")),
        ship_status=data.get("ship_status") or data.get("shipStatus"),
        fedex_tracking_number=data.get("fedex_tracking_number") or data.get("fedexTrackingNumber"),
        ship_idempotency_key=data.get("ship_idempotency_key") or data.get("shipIdempotencyKey"),
        service_type=data.get("service_type") or data.get("serviceType"),
        recipient=PartyAddress(**{k: v for k, v in party("recipient").items() if v is not None}),
        shipper=PartyAddress(**{k: v for k, v in party("shipper").items() if v is not None}),
        package=PackageDims(**pkg),
    )


def parse_service_types_from_rate_response(data: dict[str, Any]) -> set[str]:
    services: set[str] = set()
    output = data.get("output") or {}
    for reply in output.get("rateReplyDetails") or []:
        st = reply.get("serviceType")
        if st:
            services.add(str(st))
    return services
