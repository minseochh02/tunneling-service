"""FedEx ship row validation tests."""

from fedex_ship_schema import PackageDims, PartyAddress, ShipSheetRow


def _sample_row(**kwargs) -> ShipSheetRow:
    base = {
        "row_id": "r1",
        "ship_ready": True,
        "service_type": "FEDEX_GROUND",
        "recipient": PartyAddress(
            name="Jane",
            phone="5551234567",
            address1="10 Main St",
            city="Memphis",
            state="TN",
            postal="38118",
            country="US",
        ),
        "shipper": PartyAddress(
            name="Acme",
            phone="5559876543",
            address1="1 Shipper Rd",
            city="Memphis",
            state="TN",
            postal="38116",
            country="US",
        ),
        "package": PackageDims(weight_lb=2.5),
    }
    base.update(kwargs)
    return ShipSheetRow(**base)


def test_validate_ok():
    row = _sample_row()
    assert row.validate_for_ship({"FEDEX_GROUND"}) == []


def test_skip_when_tracking_set():
    row = _sample_row(fedex_tracking_number="123456789012")
    errs = row.validate_for_ship()
    assert any("already" in e for e in errs)


def test_service_not_allowed():
    row = _sample_row(service_type="PRIORITY_OVERNIGHT")
    errs = row.validate_for_ship({"FEDEX_GROUND"})
    assert any("not in allowed" in e for e in errs)


def test_us_requires_state():
    row = _sample_row(
        recipient=PartyAddress(
            name="Jane",
            phone="555",
            address1="10 Main",
            city="LA",
            postal="90001",
            country="US",
        )
    )
    errs = row.validate_for_ship()
    assert any("recipient.state" in e for e in errs)
