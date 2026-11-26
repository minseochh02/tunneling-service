"""
SMPP (중소벤처기업부 기술개발제품 인증현황) — executed on the tunnel gateway.

DATA_GO_KR_API_KEY lives here (datasets 3033913 + 3049999). Desktop calls via callPublicDataGateway only.
Swagger: https://infuser.odcloud.kr/oas/docs?namespace=3033913/v1
Supplement: https://infuser.odcloud.kr/oas/docs?namespace=3049999/v1 (KHIDI 보건신기술 NET)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway_security import egdesk_api_key_is_registered

SMPP_APPLY_URL = "https://www.data.go.kr/data/3033913/openapi.do"
KHIDI_NET_APPLY_URL = "https://www.data.go.kr/data/3049999/openapi.do"
MAX_PER_PAGE = 100
LOOKUP_MAX_PAGES = 160
KHIDI_MAX_PAGES = 20

SMPP_RESOURCES = [
    {
        "resource_id": "uddi:469c72f1-8125-4751-bb08-d3115d8e3039",
        "snapshot_label": "중소벤처기업부_기술개발제품 인증현황_20260901",
    },
    {
        "resource_id": "uddi:834e8428-51b3-420b-9fd4-aaee942e4916",
        "snapshot_label": "중소벤처기업부_기술개발제품 인증현황_20250512",
    },
]

# KHIDI 보건신기술(NET) — often present here when MSS 3033913 CSV lags (e.g. 티이바이오스).
KHIDI_NET_RESOURCES = [
    {
        "resource_id": "uddi:e2a4a21b-3c4d-4262-83ef-947e0f57f58d",
        "snapshot_label": "한국보건산업진흥원_보건신기술 인증 결과_20231222",
    },
    {
        "resource_id": "uddi:1e396da5-3cff-4851-8cbe-c32cd3a45e7a",
        "snapshot_label": "한국보건산업진흥원_보건신기술 인증 결과_20221031",
    },
]

CATEGORY_HINTS: dict[str, list[str]] = {
    "performance": ["성능인증"],
    "excellent_procurement": ["우수조달물품"],
    "nep": ["NEP", "신제품인증"],
    "gs": ["GS인증", "GS"],
    "net": ["NET", "신기술"],
    "excellent_procurement_joint_mark": ["우수조달 공동상표"],
    "water_industry_excellent": ["물산업 우수제품"],
    "innovation": ["혁신제품", "우수연구개발혁신제품", "혁신시제품", "기타혁신제품"],
    "green_tech": ["녹색기술"],
    "industry_convergence_new_product": ["산업융합 신제품"],
    "industry_convergence_item": ["산업융합품목"],
    "demand_driven": ["구매조건부", "민관공동투자", "성과공유", "수요처 지정형"],
    "disaster_safety": ["재난안전"],
}

SMPP_PURPOSE = (
    "중소벤처기업부 기술개발제품 인증현황 (SMPP / data.go.kr 3033913) "
    "plus KHIDI 보건신기술(NET) supplement (3049999) on lookup when 업체명 is given. "
    "Static CSV via api.odcloud.kr — not live smpp.go.kr. "
    "Company name matched client-side (server cond[업체명::LIKE] is unreliable on 3033913)."
)

SMPP_TOOLS = [
    {
        "name": "smpp_search",
        "description": SMPP_PURPOSE + " Filters: companyName, businessNumber, productName, certNumber, certCategory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "companyName": {"type": "string"},
                "businessNumber": {"type": "string"},
                "productName": {"type": "string"},
                "certNumber": {"type": "string"},
                "certCategory": {"type": "string"},
                "certCategoryLabel": {"type": "string"},
                "netDomain": {"type": "string"},
                "page": {"type": "integer"},
                "display": {"type": "integer"},
            },
        },
    },
    {
        "name": "smpp_lookup",
        "description": SMPP_PURPOSE
        + " Lookup by 업체명 or 사업자등록번호. 업체명 also queries KHIDI 3049999 (보건 NET).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "companyName": {"type": "string"},
                "businessNumber": {"type": "string"},
                "certCategory": {"type": "string"},
                "display": {"type": "integer"},
            },
        },
    },
]

smpp_router = APIRouter(prefix="/smpp", tags=["SMPP"])


def _service_key() -> str:
    raw = (os.getenv("DATA_GO_KR_API_KEY") or os.getenv("DATA_GO_KR_SERVICE_KEY") or "").strip()
    if "%" in raw:
        return unquote(raw)
    return raw


def mcp_ok(data: Any) -> JSONResponse:
    return JSONResponse(
        {
            "success": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}]
            },
        }
    )


def mcp_err(message: str, status: int = 500) -> JSONResponse:
    return JSONResponse({"success": False, "error": message}, status_code=status)


def missing_key_error() -> RuntimeError:
    return RuntimeError(
        "DATA_GO_KR_API_KEY is not set on tunneling-service. "
        f"Apply for dataset 3033913 at {SMPP_APPLY_URL}"
    )


def normalize_bno(raw: Any) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def normalize_company_label(raw: str) -> str:
    s = re.sub(r"\s+", "", str(raw or "").strip())
    s = re.sub(r"^(주식회사|\(주\)|㈜)", "", s)
    return s.lower()


def normalize_row(row: dict) -> dict[str, str]:
    return {
        "certCategory": str(row.get("인증구분") or "").strip(),
        "certNumber": str(row.get("인증번호") or "").strip(),
        "productName": str(row.get("인증제품명") or "").strip(),
        "companyName": str(row.get("업체명") or "").strip(),
        "businessNumber": normalize_bno(row.get("사업자등록번호")),
        "representative": str(row.get("대표자") or "").strip(),
        "certifiedAt": str(row.get("인증일자") or "").strip(),
        "expiresAt": str(row.get("만료일자") or "").strip(),
        "sourceDataset": "3033913",
    }


def normalize_khidi_row(row: dict) -> dict[str, str]:
    domain = str(row.get("분야") or "").strip()
    cert_label = "보건신기술(NET)"
    if domain:
        cert_label = f"{cert_label} · {domain}"
    seq_no = row.get("연번") if row.get("연번") is not None else row.get("No")
    return {
        "certCategory": cert_label,
        "certNumber": str(seq_no or "").strip(),
        "productName": str(row.get("기술명") or row.get("기술명(국문명)") or "").strip(),
        "companyName": str(row.get("업체") or row.get("기업명") or "").strip(),
        "businessNumber": "",
        "representative": str(row.get("이름") or "").strip(),
        "certifiedAt": str(row.get("인증일") or "").strip(),
        "expiresAt": str(row.get("인증만료일") or "").strip(),
        "certificationTerm": str(row.get("인증기간") or "").strip(),
        "netDomain": domain,
        "sourceDataset": "3049999",
    }


def company_name_variants(raw: str) -> list[str]:
    base = str(raw or "").strip()
    if not base:
        return []
    core = re.sub(r"^(주식회사|\(주\)|㈜)\s*", "", base).strip()
    out = {base, core, f"(주){core}", f"주식회사 {core}", f"㈜{core}"}
    return [x for x in out if x]


def row_matches_company(variants: list[str], company_name: str) -> bool:
    row_norm = normalize_company_label(company_name)
    if not row_norm:
        return False
    for v in variants:
        n = normalize_company_label(v)
        if not n:
            continue
        if n in row_norm or row_norm in n:
            return True
    return False


def build_server_cond(args: dict) -> dict[str, str]:
    cond: dict[str, str] = {}
    bn = normalize_bno(args.get("businessNumber"))
    if bn:
        cond["cond[사업자등록번호::EQ]"] = bn
    product = str(args.get("productName") or "").strip()
    if product:
        cond["cond[인증제품명::LIKE]"] = product
    cert_no = str(args.get("certNumber") or "").strip()
    if cert_no:
        cond["cond[인증번호::EQ]"] = cert_no
    label = str(args.get("certCategoryLabel") or "").strip()
    category_id = str(args.get("certCategory") or "").strip()
    if label:
        cond["cond[인증구분::LIKE]"] = label
    elif category_id and category_id in CATEGORY_HINTS:
        cond["cond[인증구분::LIKE]"] = CATEGORY_HINTS[category_id][0]
    return cond


def post_filter_rows(rows: list[dict], args: dict) -> list[dict]:
    out = rows
    company = str(args.get("companyName") or "").strip()
    if company:
        variants = company_name_variants(company)
        out = [r for r in out if row_matches_company(variants, r.get("companyName") or "")]
    category_id = str(args.get("certCategory") or "").strip()
    net_domain = str(args.get("netDomain") or "").strip()
    if category_id and category_id in CATEGORY_HINTS:
        hints = CATEGORY_HINTS[category_id]
        out = [r for r in out if any(h in (r.get("certCategory") or "") for h in hints)]
    if net_domain:
        out = [r for r in out if net_domain in (r.get("certCategory") or "")]
    return out


def post_filter_khidi_rows(rows: list[dict], args: dict) -> list[dict]:
    out = rows
    company = str(args.get("companyName") or "").strip()
    if company:
        variants = company_name_variants(company)
        out = [r for r in out if row_matches_company(variants, r.get("companyName") or "")]
    product = str(args.get("productName") or "").strip()
    if product:
        out = [r for r in out if product in (r.get("productName") or "")]
    category_id = str(args.get("certCategory") or "").strip()
    net_domain = str(args.get("netDomain") or "").strip()
    if category_id and category_id != "net":
        return []
    if net_domain:
        out = [r for r in out if net_domain in (r.get("netDomain") or "")]
    return out


def khidi_fault_message(status: int, text: str) -> str:
    if status == 401:
        return (
            "DATA_GO_KR_API_KEY is not approved for 보건신기술(NET) (3049999). "
            f"Apply at {KHIDI_NET_APPLY_URL}"
        )
    if status == 403 or "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR" in text:
        return "data.go.kr daily quota exceeded for dataset 3049999."
    return f"api.odcloud.kr HTTP {status}: {text[:200]}"


def should_query_khidi_net(args: dict) -> bool:
    company = str(args.get("companyName") or "").strip()
    if not company:
        return False
    category_id = str(args.get("certCategory") or "").strip()
    if category_id and category_id != "net":
        return False
    label = str(args.get("certCategoryLabel") or "").strip()
    if label and "NET" not in label and "보건" not in label:
        return False
    return True


def odcloud_fault_message(status: int, text: str) -> str:
    if status == 401:
        return (
            "DATA_GO_KR_API_KEY is not approved for 기술개발제품 인증현황 (3033913). "
            f"Apply at {SMPP_APPLY_URL}"
        )
    if status == 403 or "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR" in text:
        return "data.go.kr daily quota exceeded for dataset 3033913."
    return f"api.odcloud.kr HTTP {status}: {text[:200]}"


async def fetch_raw_page(
    args: dict,
    resource_id: str,
    snapshot_label: str,
) -> dict[str, Any]:
    key = _service_key()
    if not key:
        raise missing_key_error()

    page = max(1, int(args.get("page") or 1))
    per_page = min(MAX_PER_PAGE, max(1, int(args.get("display") or 20)))
    url = f"https://api.odcloud.kr/api/3033913/v1/{resource_id}"
    params: dict[str, Any] = {
        "serviceKey": key,
        "returnType": "JSON",
        "page": page,
        "perPage": per_page,
        **build_server_cond(args),
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, params=params)
    text = resp.text
    if resp.status_code >= 400:
        raise RuntimeError(odcloud_fault_message(resp.status_code, text))

    try:
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError(f"응답 파싱 실패: {text[:200]}") from exc

    raw_rows = payload.get("data") or []
    rows = [normalize_row(row or {}) for row in raw_rows]

    return {
        "page": int(payload.get("page") or page),
        "perPage": int(payload.get("perPage") or per_page),
        "totalCount": int(payload.get("totalCount") or 0),
        "matchCount": int(payload.get("matchCount") or 0),
        "currentCount": int(payload.get("currentCount") or len(raw_rows)),
        "rows": rows,
        "resource_id": resource_id,
        "snapshot_label": snapshot_label,
    }


async def fetch_khidi_page(
    args: dict,
    resource_id: str,
    snapshot_label: str,
) -> dict[str, Any]:
    key = _service_key()
    if not key:
        raise missing_key_error()

    page = max(1, int(args.get("page") or 1))
    per_page = min(MAX_PER_PAGE, max(1, int(args.get("display") or 100)))
    url = f"https://api.odcloud.kr/api/3049999/v1/{resource_id}"
    params: dict[str, Any] = {
        "serviceKey": key,
        "returnType": "JSON",
        "page": page,
        "perPage": per_page,
    }
    company = str(args.get("companyName") or "").strip()
    if company:
        core = re.sub(r"^(주식회사|\(주\)|㈜)\s*", "", company).strip()
        if len(core) >= 2:
            params["cond[업체::LIKE]"] = core

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, params=params)
    text = resp.text
    if resp.status_code >= 400:
        raise RuntimeError(khidi_fault_message(resp.status_code, text))

    try:
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError(f"KHIDI 응답 파싱 실패: {text[:200]}") from exc

    raw_rows = payload.get("data") or []
    rows = [normalize_khidi_row(row or {}) for row in raw_rows]

    return {
        "page": int(payload.get("page") or page),
        "perPage": int(payload.get("perPage") or per_page),
        "totalCount": int(payload.get("totalCount") or 0),
        "matchCount": int(payload.get("matchCount") or 0),
        "currentCount": int(payload.get("currentCount") or len(raw_rows)),
        "rows": rows,
        "resource_id": resource_id,
        "snapshot_label": snapshot_label,
    }


async def search_khidi_net(args: dict) -> tuple[list[dict], dict[str, Any] | None]:
    scan_args = {**args, "display": MAX_PER_PAGE}
    collected: list[dict] = []
    last_raw: dict[str, Any] | None = None
    pages_scanned = 0

    for resource in KHIDI_NET_RESOURCES:
        resource_hits: list[dict] = []
        for page in range(1, KHIDI_MAX_PAGES + 1):
            pages_scanned += 1
            raw = await fetch_khidi_page({**scan_args, "page": page}, resource["resource_id"], resource["snapshot_label"])
            last_raw = raw
            resource_hits.extend(post_filter_khidi_rows(raw["rows"], args))
            if raw["currentCount"] < MAX_PER_PAGE:
                break
        if resource_hits:
            collected = resource_hits
            break
        if collected:
            break

    if not collected and last_raw:
        for page in range(1, KHIDI_MAX_PAGES + 1):
            raw = await fetch_khidi_page(
                {**scan_args, "page": page, "companyName": None},
                KHIDI_NET_RESOURCES[0]["resource_id"],
                KHIDI_NET_RESOURCES[0]["snapshot_label"],
            )
            last_raw = raw
            pages_scanned += 1
            hits = post_filter_khidi_rows(raw["rows"], args)
            if hits:
                collected = hits
                break
            if raw["currentCount"] < MAX_PER_PAGE:
                break

    meta = {
        "datasetId": "3049999",
        "datasetSnapshot": (last_raw or {}).get("snapshot_label"),
        "resourceUddi": (last_raw or {}).get("resource_id"),
        "pagesScanned": pages_scanned,
        "swaggerUrl": "https://infuser.odcloud.kr/oas/docs?namespace=3049999/v1",
        "returnedCount": len(collected),
    }
    return collected, meta if last_raw else None


def merge_khidi_items(mss: dict[str, Any], khidi_items: list[dict], khidi_meta: dict[str, Any] | None) -> dict[str, Any]:
    if not khidi_items:
        return mss

    existing = list(mss.get("items") or [])
    seen = {
        (
            (i.get("productName") or "").strip(),
            (i.get("companyName") or "").strip(),
            (i.get("expiresAt") or "").strip(),
            (i.get("sourceDataset") or "3033913"),
        )
        for i in existing
    }
    added = 0
    for row in khidi_items:
        key = (
            (row.get("productName") or "").strip(),
            (row.get("companyName") or "").strip(),
            (row.get("expiresAt") or "").strip(),
            "3049999",
        )
        if key in seen:
            continue
        existing.append(row)
        seen.add(key)
        added += 1

    mss["items"] = existing
    mss["returnedCount"] = len(existing)
    if mss.get("notFoundInSnapshot") and added:
        mss.pop("notFoundInSnapshot", None)
    mss["khidiNetSupplement"] = {
        **(khidi_meta or {}),
        "mergedCount": added,
        "note": (
            "KHIDI 보건신기술(NET) from data.go.kr 3049999 — may appear here when absent from MSS 3033913 CSV."
        ),
    }
    return mss


async def enrich_with_khidi_net(result: dict[str, Any], args: dict) -> dict[str, Any]:
    if not should_query_khidi_net(args):
        return result
    try:
        khidi_items, khidi_meta = await search_khidi_net(args)
    except Exception as exc:
        print(f"⚠️ KHIDI NET supplement skipped: {exc}")
        result["khidiNetSupplement"] = {"datasetId": "3049999", "error": str(exc)}
        return result
    return merge_khidi_items(result, khidi_items, khidi_meta)


def wrap_result(raw: dict, args: dict, items: list[dict], extra: dict | None = None) -> dict[str, Any]:
    base = {
        "ok": True,
        "source": "api.odcloud.kr",
        "datasetId": "3033913",
        "datasetSnapshot": raw["snapshot_label"],
        "resourceUddi": raw["resource_id"],
        "page": raw["page"],
        "perPage": raw["perPage"],
        "totalCount": raw["totalCount"],
        "matchCount": raw["matchCount"],
        "currentCount": raw["currentCount"],
        "returnedCount": len(items),
        "items": items,
        "filters": build_server_cond(args),
        "dataLagNotice": (
            "Static CSV snapshot — renewals may lag. Same-day: www.smpp.go.kr or KHIDI (보건 NET)."
        ),
        "realtimeAlternatives": [
            "www.smpp.go.kr → 정보조회 → 기술개발제품 현황",
            "KHIDI 보건신기술 인증 플랫폼 (보건 NET)",
        ],
    }
    company = str(args.get("companyName") or "").strip()
    if company:
        base["companyNameClientFilter"] = company_name_variants(company)
    if extra:
        base.update(extra)
    return base


async def scan_for_company(args: dict, max_pages: int = LOOKUP_MAX_PAGES) -> dict[str, Any]:
    company = str(args.get("companyName") or "").strip()
    scan_args = {**args, "companyName": None, "display": MAX_PER_PAGE}
    last_raw: dict[str, Any] | None = None
    pages_scanned = 0

    for resource in SMPP_RESOURCES:
        for page in range(1, max_pages + 1):
            pages_scanned += 1
            raw = await fetch_raw_page({**scan_args, "page": page}, resource["resource_id"], resource["snapshot_label"])
            last_raw = raw
            hits = post_filter_rows(raw["rows"], {**args, "companyName": company})
            if hits:
                return wrap_result(
                    raw,
                    args,
                    hits,
                    {
                        "searchMode": "client_scan",
                        "pagesScanned": page,
                        "companyNameRequested": company,
                    },
                )
            if raw["currentCount"] < MAX_PER_PAGE:
                break

    empty = last_raw or {
        "page": 1,
        "perPage": MAX_PER_PAGE,
        "totalCount": 0,
        "matchCount": 0,
        "currentCount": 0,
        "rows": [],
        "resource_id": SMPP_RESOURCES[0]["resource_id"],
        "snapshot_label": SMPP_RESOURCES[0]["snapshot_label"],
    }
    return wrap_result(
        empty,
        args,
        [],
        {
            "searchMode": "client_scan",
            "pagesScanned": pages_scanned,
            "companyNameRequested": company,
            "notFoundInSnapshot": True,
        },
    )


async def _search_certifications_mss(args: dict) -> dict[str, Any]:
    company = str(args.get("companyName") or "").strip()
    company_only = (
        company
        and not normalize_bno(args.get("businessNumber"))
        and not str(args.get("productName") or "").strip()
        and not str(args.get("certNumber") or "").strip()
        and not str(args.get("certCategory") or "").strip()
        and not str(args.get("certCategoryLabel") or "").strip()
    )

    if company_only:
        return await scan_for_company(args)

    for resource in SMPP_RESOURCES:
        raw = await fetch_raw_page(args, resource["resource_id"], resource["snapshot_label"])
        items = post_filter_rows(raw["rows"], args)
        if items:
            return wrap_result(raw, args, items)
        if not company and (raw["matchCount"] > 0 or raw["rows"]):
            return wrap_result(raw, args, items)

    if company:
        return await scan_for_company(args)

    raw = await fetch_raw_page(args, SMPP_RESOURCES[0]["resource_id"], SMPP_RESOURCES[0]["snapshot_label"])
    return wrap_result(raw, args, post_filter_rows(raw["rows"], args))


async def search_certifications(args: dict) -> dict[str, Any]:
    result = await _search_certifications_mss(args)
    if should_query_khidi_net(args):
        result = await enrich_with_khidi_net(result, args)
    return result


async def lookup_company(args: dict) -> dict[str, Any]:
    company = str(args.get("companyName") or "").strip()
    bn = normalize_bno(args.get("businessNumber"))
    if not company and not bn:
        raise RuntimeError("companyName or businessNumber is required")
    result = await _search_certifications_mss({**args, "page": 1})
    if company:
        result = await enrich_with_khidi_net(result, args)
    elif bn:
        result["khidiNetSupplement"] = {
            "datasetId": "3049999",
            "skipped": True,
            "reason": "3049999 export has no 사업자등록번호 — pass companyName for KHIDI NET supplement.",
        }
    return result


def has_predicate(args: dict) -> bool:
    if str(args.get("companyName") or "").strip():
        return True
    return len(build_server_cond(args)) > 0


async def execute_tool(name: str, args: dict | None) -> Any:
    args = args or {}
    if name == "smpp_search":
        if not has_predicate(args):
            raise RuntimeError(
                "At least one filter is required: companyName, businessNumber, productName, "
                "certNumber, certCategoryLabel, or certCategory"
            )
        return await search_certifications(args)
    if name == "smpp_lookup":
        return await lookup_company(args)
    raise RuntimeError(f"Unknown tool: {name}")


async def handle_smpp_http(method: str, path: str, body: dict | None) -> JSONResponse:
    clean = path.strip("/")
    if method == "GET" and clean == "tools":
        return JSONResponse(SMPP_TOOLS)
    if method == "POST" and clean == "tools/call":
        tool = (body or {}).get("tool")
        if not tool:
            return mcp_err('Missing "tool" parameter', 400)
        try:
            return mcp_ok(await execute_tool(str(tool), (body or {}).get("arguments") or {}))
        except Exception as exc:
            print(f"❌ SMPP tool {tool}: {exc}")
            return mcp_err(str(exc), 500)
    return mcp_err("SMPP MCP endpoint not found", 404)


@smpp_router.get("/tools")
async def list_tools():
    if not _service_key():
        return mcp_err("DATA_GO_KR_API_KEY is not set on tunneling-service", 503)
    return JSONResponse(SMPP_TOOLS)


@smpp_router.post("/tools/call")
async def call_tool(request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key or not egdesk_api_key_is_registered(api_key):
        return mcp_err("Missing or invalid X-Api-Key (EGDesk tunnel key required)", 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await handle_smpp_http("POST", "tools/call", body)