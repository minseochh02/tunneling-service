"""
UNI-PASS lookups. Each service has its own crkyCn on this gateway.

Env names:
  UNIPASS_CARGO_KEY   화물통관진행정보조회 (API001). UNIPASS_API_KEY still works.
  UNIPASS_FXRT_KEY    관세환율정보조회 (API012)
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from xml.etree import ElementTree

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

UNIPASS_BASE = "https://unipass.customs.go.kr:38010/ext/rest/"
CARGO_URL = UNIPASS_BASE + "cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
FXRT_URL = UNIPASS_BASE + "trifFxrtInfoQry/retrieveTrifFxrtInfo"

# First name is the one to set on Render. Later names are legacy aliases.
SERVICE_KEYS = {
    "cargo": ("UNIPASS_CARGO_KEY", "UNIPASS_API_KEY", "UNIPASS_CRKY_CN"),
    "fxrt": ("UNIPASS_FXRT_KEY",),
}

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
