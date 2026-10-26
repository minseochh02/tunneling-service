"""
UNI-PASS 화물통관진행정보 test lookup.

Dataset: https://www.data.go.kr/data/15126268/openapi.do
The call goes to UNI-PASS directly. The auth key (crkyCn) stays on this
gateway. Prefer UNIPASS_API_KEY; DATA_GO_KR_API_KEY is the fallback already
set on Render.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from xml.etree import ElementTree

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

CARGO_URL = (
    "https://unipass.customs.go.kr:38010/ext/rest/"
    "cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
)
APPLY_URL = "https://www.data.go.kr/data/15126268/openapi.do"

CARGO_FIELDS = (
    "csclPrgsStts",
    "prgsStts",
    "prgsStCd",
    "cargMtNo",
    "mblNo",
    "hblNo",
    "blPtNm",
    "etprDt",
    "etprCstm",
    "dsprNm",
    "ldprNm",
    "shipNm",
    "shipNatNm",
    "shcoFlco",
    "pckGcnt",
    "pckUt",
    "ttwg",
    "wghtUt",
    "prnm",
    "cntrNo",
    "cntrGcnt",
    "cargTp",
    "frwrSgn",
)

DETAIL_FIELDS = (
    "cargTrcnRelaBsopTpcd",
    "prcsDttm",
    "shedNm",
    "rlbrCn",
    "rlbrDttm",
    "dclrNo",
    "wght",
    "wghtUt",
    "pckGcnt",
    "pckUt",
)

unipass_router = APIRouter(prefix="/unipass", tags=["Unipass"])


def _crky() -> tuple[str, str]:
    for name in (
        "UNIPASS_API_KEY",
        "UNIPASS_CRKY_CN",
        "DATA_GO_KR_API_KEY",
        "DATA_GO_KR_SERVICE_KEY",
    ):
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


def _children(text: str, tag: str) -> list[dict[str, str]]:
    root = ElementTree.fromstring(text)
    rows: list[dict[str, str]] = []
    for child in root.iter():
        if _local(child.tag) != tag:
            continue
        row = {
            _local(node.tag): (node.text or "").strip()
            for node in list(child)
            if (node.text or "").strip()
        }
        if row:
            rows.append(row)
    return rows


def _pick(row: dict[str, str], fields: tuple[str, ...]) -> dict[str, str]:
    return {key: row[key] for key in fields if row.get(key)}


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
        cargos = _children(body, "cargCsclPrgsInfoQryVo")
        details = _children(body, "cargCsclPrgsInfoDtlQryVo")
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
        "cargos": [_pick(row, CARGO_FIELDS) for row in cargos],
        "details": [_pick(row, DETAIL_FIELDS) for row in details[:12]],
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

    key, source = _crky()
    if not key:
        return JSONResponse(
            status_code=503,
            content={
                "queryable": False,
                "error": (
                    "No UNI-PASS key on tunneling-service. "
                    "Set UNIPASS_API_KEY (crkyCn from UNI-PASS OpenAPI 사용관리) "
                    f"or DATA_GO_KR_API_KEY. Apply: {APPLY_URL}"
                ),
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
                "UNI-PASS rejected the key on Render. "
                "화물통관진행정보 uses crkyCn from UNI-PASS My메뉴 > OpenAPI 사용관리, "
                "which is separate from the data.go.kr serviceKey used by 국세청/나라장터. "
                f"Set UNIPASS_API_KEY. Portal: {APPLY_URL}"
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
        "details": match.get("details") or [],
        "attempts": attempts,
    }
