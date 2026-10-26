"""
UNI-PASS lookups. Each service has its own crkyCn on this gateway.

Env names:
  UNIPASS_CARGO_KEY   화물통관진행정보조회 (API001). UNIPASS_API_KEY still works.
  UNIPASS_FXRT_KEY    관세환율정보조회 (API012)
  UNIPASS_IMP_PAY_KEY 수입 제세 납부여부 및 납부일자
  UNIPASS_IMP_PAY_PATH optional path after /ext/rest/ (see OpenAPI 연계가이드 API043)
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any
from xml.etree import ElementTree

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

_DEFAULT_UNIPASS_BASE = "https://unipass.customs.go.kr:38010/ext/rest/"
UNIPASS_BASE = (os.getenv("UNIPASS_BASE_URL") or _DEFAULT_UNIPASS_BASE).strip()
if not UNIPASS_BASE.endswith("/"):
    UNIPASS_BASE += "/"

_DEFAULT_IMP_PAY_PATH = "impPayYnDtQry/retrieveImpPayYnDt"


def _rest_url(path: str) -> str:
    path = path.strip().lstrip("/")
    return UNIPASS_BASE + path


def _imp_pay_url() -> str:
    path = (os.getenv("UNIPASS_IMP_PAY_PATH") or _DEFAULT_IMP_PAY_PATH).strip().lstrip("/")
    return _rest_url(path)


CARGO_URL = _rest_url("cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo")
FXRT_URL = _rest_url("trifFxrtInfoQry/retrieveTrifFxrtInfo")
IMP_PAY_URL = _imp_pay_url()

# First name is the one to set on Render. Later names are legacy aliases.
SERVICE_KEYS = {
    "cargo": ("UNIPASS_CARGO_KEY", "UNIPASS_API_KEY", "UNIPASS_CRKY_CN"),
    "fxrt": ("UNIPASS_FXRT_KEY",),
    "imp_pay": ("UNIPASS_IMP_PAY_KEY",),
}

IMP_PAY_MARKERS = ("payYn", "pymnYn", "impPayYn", "payDt", "pymnDt", "rcptDt", "impDclrNo")

unipass_router = APIRouter(prefix="/unipass", tags=["Unipass"])


def _service_key(service: str) -> tuple[str, str]:
    for name in SERVICE_KEYS[service]:
        raw = (os.getenv(name) or "").strip()
        if raw:
            return raw, name
    return "", ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_xml(text: str) -> dict[str, Any]:
    root = ElementTree.fromstring(text)
    out: dict[str, Any] = {}
    for child in list(root):
        name = _local(child.tag)
        value = (child.text or "").strip()
        if name in out:
            prev = out[name]
            out[name] = prev + [value] if isinstance(prev, list) else [prev, value]
        else:
            out[name] = value
    return out


def _records(text: str, tag: str) -> tuple[list[dict[str, str]], list[str]]:
    """Return every direct child. Empty tags are listed once so we can see the full schema."""
    root = ElementTree.fromstring(text)
    rows: list[dict[str, str]] = []
    empty: list[str] = []
    for child in root.iter():
        if _local(child.tag) != tag:
            continue
        row: dict[str, str] = {}
        for node in list(child):
            name = _local(node.tag)
            value = (node.text or "").strip()
            if value:
                row[name] = value
            elif name not in empty:
                empty.append(name)
        if row:
            rows.append(row)
    return rows, empty


def _rows_with_field(text: str, field: str) -> list[dict[str, str]]:
    """Collect elements that carry `field` (UNI-PASS VO tag names vary by API)."""
    root = ElementTree.fromstring(text)
    rows: list[dict[str, str]] = []
    for elem in root.iter():
        row = {
            _local(node.tag): (node.text or "").strip()
            for node in list(elem)
            if (node.text or "").strip()
        }
        if field in row:
            rows.append(row)
    return rows


def _auth_failed(notice: str, count: int | None) -> bool:
    if count == -1:
        return True
    text = notice or ""
    return "인증키" in text or "crkyCn" in text


def _normalize_imp_dclr_no(raw: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(raw or ""))


def _looks_like_imp_dclr_no(value: str) -> bool:
    return bool(re.fullmatch(r"\d{13}M", value))


def _imp_dclr_from_cargo_details(details: list[dict[str, str]]) -> str | None:
    candidates: list[str] = []
    for row in details:
        for key in ("dclrNo", "rlbrBssNo"):
            value = _normalize_imp_dclr_no(row.get(key) or "")
            if _looks_like_imp_dclr_no(value):
                candidates.append(value)
    return candidates[0] if candidates else None


async def _unipass_get(url: str, params: dict[str, str]) -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.get(url, params=params)
    return resp.status_code, resp.text or ""


def _imp_pay_records(text: str) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []
    keys_seen: set[tuple[tuple[str, str], ...]] = set()
    for marker in IMP_PAY_MARKERS:
        for row in _rows_with_field(text, marker):
            sig = tuple(sorted(row.items()))
            if sig in keys_seen:
                continue
            keys_seen.add(sig)
            seen.append(row)
    return seen


def _count(raw: Any) -> int | None:
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


async def _query(kind: str, bl: str, year: str, key: str) -> dict[str, Any]:
    params: dict[str, str] = {"crkyCn": key}
    if kind == "carg":
        params["cargMtNo"] = bl
    elif kind == "mbl":
        params["mblNo"] = bl
        params["blYy"] = year
    else:
        params["hblNo"] = bl
        params["blYy"] = year

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.get(CARGO_URL, params=params)
    except httpx.HTTPError as exc:
        return {
            "kind": kind,
            "year": year or None,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    body = resp.text or ""
    if resp.status_code >= 400:
        return {
            "kind": kind,
            "year": year or None,
            "ok": False,
            "httpStatus": resp.status_code,
            "error": body[:300],
        }

    try:
        parsed = _parse_xml(body)
        cargos, cargo_empty = _records(body, "cargCsclPrgsInfoQryVo")
        details, detail_empty = _records(body, "cargCsclPrgsInfoDtlQryVo")
    except ElementTree.ParseError:
        return {
            "kind": kind,
            "year": year or None,
            "ok": False,
            "error": f"XML parse failed: {body[:300]}",
        }

    notice = parsed.get("ntceInfo") or ""
    if isinstance(notice, list):
        notice = " ".join(str(part) for part in notice if part)
    count = _count(parsed.get("tCnt"))
    return {
        "kind": kind,
        "year": year or None,
        "ok": True,
        "count": count,
        "notice": notice,
        "authFailed": _auth_failed(str(notice), count),
        "cargos": cargos,
        "cargoEmpty": cargo_empty,
        "details": details,
        "detailEmpty": detail_empty,
    }


def _years(year: str | None) -> list[str]:
    if year:
        return [year]
    current = datetime.now().year
    return [str(current - offset) for offset in range(0, 3)]


@unipass_router.get("/cargo")
async def cargo_lookup(
    bl: str = Query(..., description="B/L 번호 또는 화물관리번호"),
    year: str | None = Query(None, description="B/L 연도 YYYY. 생략하면 최근 3년"),
    kind: str = Query("auto", description="auto | hbl | mbl | carg"),
):
    """Test whether 관세청 화물통관진행정보 can resolve this B/L."""
    bl_no = "".join(bl.split())
    if not bl_no:
        return JSONResponse(status_code=400, content={"error": "bl is required"})

    key, source = _service_key("cargo")
    if not key:
        return JSONResponse(
            status_code=503,
            content={
                "queryable": False,
                "error": "Set UNIPASS_CARGO_KEY on tunneling-service (화물통관진행정보 crkyCn).",
            },
        )

    kinds = ["hbl", "mbl"] if kind == "auto" else [kind]
    if any(item not in {"hbl", "mbl", "carg"} for item in kinds):
        return JSONResponse(status_code=400, content={"error": "kind must be auto, hbl, mbl, or carg"})

    years = [""] if kinds == ["carg"] else _years(year)
    attempts: list[dict[str, Any]] = []
    match: dict[str, Any] | None = None
    auth_failed = False

    for query_year in years:
        for query_kind in kinds:
            result = await _query(query_kind, bl_no, query_year, key)
            summary = {
                "kind": result.get("kind"),
                "year": result.get("year"),
                "ok": result.get("ok"),
                "count": result.get("count"),
                "notice": result.get("notice") or result.get("error"),
                "authFailed": result.get("authFailed", False),
            }
            attempts.append(summary)
            print(
                f"🚢 UNI-PASS cargo {query_kind} {bl_no} {query_year or '-'} "
                f"count={result.get('count')} auth={result.get('authFailed', False)}"
            )
            if result.get("authFailed"):
                auth_failed = True
                break
            count = result.get("count")
            if result.get("ok") and isinstance(count, int) and count > 0:
                match = result
                break
        if match or auth_failed:
            break

    if auth_failed:
        return {
            "bl": bl_no,
            "queryable": False,
            "reason": "auth",
            "keySource": source,
            "message": (
                "UNI-PASS rejected this key. "
                "Set UNIPASS_CARGO_KEY to the 화물통관진행정보 crkyCn."
            ),
            "attempts": attempts,
        }

    if not match:
        return {
            "bl": bl_no,
            "queryable": False,
            "reason": "not_found",
            "keySource": source,
            "message": "인증은 통과했지만 이 B/L은 최근 3년 수입화물에서 조회되지 않았습니다.",
            "attempts": attempts,
        }

    return {
        "bl": bl_no,
        "queryable": True,
        "keySource": source,
        "matched": {"kind": match.get("kind"), "year": match.get("year")},
        "count": match.get("count"),
        "notice": match.get("notice") or "",
        "cargos": match.get("cargos") or [],
        "cargoEmpty": match.get("cargoEmpty") or [],
        "details": match.get("details") or [],
        "detailEmpty": match.get("detailEmpty") or [],
        "attempts": attempts,
    }


@unipass_router.get("/fxrt")
async def exchange_rate(
    date: str = Query(..., description="조회년월일 YYYYMMDD"),
    imex: str = Query("2", description="1 수출, 2 수입"),
    curr: str | None = Query(None, description="통화부호. 예: USD. 생략하면 전체"),
):
    """관세환율정보조회 (API012). Uses UNIPASS_FXRT_KEY."""
    qry = "".join(ch for ch in date if ch.isdigit())
    if len(qry) != 8:
        return JSONResponse(status_code=400, content={"error": "date must be YYYYMMDD"})
    if imex not in {"1", "2"}:
        return JSONResponse(status_code=400, content={"error": "imex must be 1 (export) or 2 (import)"})

    key, source = _service_key("fxrt")
    if not key:
        return JSONResponse(
            status_code=503,
            content={"error": "Set UNIPASS_FXRT_KEY on tunneling-service (관세환율정보 crkyCn)."},
        )

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.get(
                FXRT_URL,
                params={"crkyCn": key, "qryYymmDd": qry, "imexTp": imex},
            )
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={"error": f"{type(exc).__name__}: {exc}"})

    body = resp.text or ""
    if resp.status_code >= 400:
        return JSONResponse(status_code=502, content={"error": body[:300], "httpStatus": resp.status_code})

    try:
        parsed = _parse_xml(body)
        rates = _rows_with_field(body, "currSgn")
        empty: list[str] = []
    except ElementTree.ParseError:
        return JSONResponse(status_code=502, content={"error": f"XML parse failed: {body[:300]}"})

    notice = parsed.get("ntceInfo") or ""
    if isinstance(notice, list):
        notice = " ".join(str(part) for part in notice if part)
    count = _count(parsed.get("tCnt"))
    if _auth_failed(str(notice), count):
        return {
            "queryable": False,
            "reason": "auth",
            "keySource": source,
            "notice": notice,
            "message": "Set UNIPASS_FXRT_KEY to the 관세환율정보 crkyCn. It is not the cargo key.",
        }

    wanted = (curr or "").strip().upper()
    if wanted:
        rates = [row for row in rates if str(row.get("currSgn") or "").upper() == wanted]

    print(f"💱 UNI-PASS fxrt {qry} imex={imex} curr={wanted or '*'} count={count}")
    return {
        "queryable": bool(rates),
        "keySource": source,
        "date": qry,
        "imex": imex,
        "count": count,
        "notice": notice,
        "rates": rates,
        "empty": empty,
    }


@unipass_router.get("/imp-pay")
async def import_tax_payment(
    impDclrNo: str | None = Query(None, description="수입신고번호 (예: 2317626122128M)"),
    bl: str | None = Query(None, description="House B/L — cargo에서 신고번호 추출"),
    year: str | None = Query(None, description="B/L 연도 YYYY"),
):
    """수입 제세 납부여부 및 납부일자. Uses UNIPASS_IMP_PAY_KEY."""
    dclr = _normalize_imp_dclr_no(impDclrNo or "")
    resolved_from: str | None = None

    if not dclr and bl:
        bl_no = "".join(bl.split())
        cargo_key, _ = _service_key("cargo")
        if not cargo_key:
            return JSONResponse(
                status_code=503,
                content={"error": "Set UNIPASS_CARGO_KEY to resolve impDclrNo from B/L."},
            )
        cargo = await _query("hbl", bl_no, (year or str(datetime.now().year)), cargo_key)
        if cargo.get("authFailed"):
            return JSONResponse(status_code=502, content={"error": "Cargo key rejected while resolving B/L."})
        dclr = _imp_dclr_from_cargo_details(cargo.get("details") or []) or ""
        if dclr:
            resolved_from = "cargo"

    if not dclr:
        return JSONResponse(
            status_code=400,
            content={"error": "impDclrNo is required (or pass bl= with a resolvable import declaration)."},
        )

    key, source = _service_key("imp_pay")
    if not key:
        return JSONResponse(
            status_code=503,
            content={
                "error": "Set UNIPASS_IMP_PAY_KEY on tunneling-service (수입 제세 납부여부 crkyCn).",
            },
        )

    imp_pay_url = _imp_pay_url()
    try:
        status, body = await _unipass_get(imp_pay_url, {"crkyCn": key, "impDclrNo": dclr})
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={"error": f"{type(exc).__name__}: {exc}"})

    if status == 404:
        return JSONResponse(
            status_code=502,
            content={
                "error": "UNI-PASS returned HTTP 404 for the imp-pay REST path.",
                "httpStatus": status,
                "unipassPath": imp_pay_url.replace(UNIPASS_BASE, "").split("?")[0],
                "hint": (
                    "Copy the sample URI from OpenAPI 연계가이드 (API043 수입 제세 납부여부·납부일자) "
                    "or UNI-PASS My > OpenAPI 사용관리 > 상세, then set UNIPASS_IMP_PAY_PATH on Render "
                    "(path only, e.g. someQry/retrieveSome)."
                ),
            },
        )

    if status >= 400:
        return JSONResponse(
            status_code=502,
            content={"error": body[:300], "httpStatus": status, "unipassPath": imp_pay_url.replace(UNIPASS_BASE, "").split("?")[0]},
        )

    try:
        parsed = _parse_xml(body)
        records = _imp_pay_records(body)
    except ElementTree.ParseError:
        return JSONResponse(status_code=502, content={"error": f"XML parse failed: {body[:300]}"})

    notice = parsed.get("ntceInfo") or ""
    if isinstance(notice, list):
        notice = " ".join(str(part) for part in notice if part)
    count = _count(parsed.get("tCnt"))
    if _auth_failed(str(notice), count):
        return {
            "queryable": False,
            "reason": "auth",
            "impDclrNo": dclr,
            "keySource": source,
            "notice": notice,
            "message": "Set UNIPASS_IMP_PAY_KEY to the 수입 제세 납부여부 crkyCn.",
        }

    print(f"💰 UNI-PASS imp-pay {dclr} count={count} records={len(records)}")
    return {
        "queryable": bool(records) or count not in (None, 0, -1),
        "impDclrNo": dclr,
        "resolvedFrom": resolved_from,
        "keySource": source,
        "count": count,
        "notice": notice,
        "records": records,
        "parsed": {k: v for k, v in parsed.items() if k not in {"ntceInfo", "tCnt"}},
    }
