"""
나라장터 입찰공고정보 — executed on the tunnel gateway.

DATA_GO_KR_API_KEY lives here. Never on EGDesk desktop or customer apps.
Apply for dataset 15129394 on the same data.go.kr account key.
Docs: https://www.data.go.kr/data/15129394/openapi.do
Spec: 조달청_OpenAPI참고자료_나라장터_입찰공고정보서비스_1.2
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

BIDNOTICE_BASE = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"
BIDNOTICE_APPLY_URL = "https://www.data.go.kr/data/15129394/openapi.do"
MAX_ROWS = 100
MAX_LOOKUP_PAGES = 5
MAX_RANGE_DAYS = 31

CATEGORIES = {
    "goods": {
        "label": "물품",
        "list": "getBidPblancListInfoThng",
        "pps": "getBidPblancListInfoThngPPSSrch",
    },
    "construction": {
        "label": "공사",
        "list": "getBidPblancListInfoCnstwk",
        "pps": "getBidPblancListInfoCnstwkPPSSrch",
    },
    "service": {
        "label": "용역",
        "list": "getBidPblancListInfoServc",
        "pps": "getBidPblancListInfoServcPPSSrch",
    },
    "foreign": {
        "label": "외자",
        "list": "getBidPblancListInfoFrgcpt",
        "pps": "getBidPblancListInfoFrgcptPPSSrch",
    },
    "etc": {
        "label": "기타",
        "list": "getBidPblancListInfoEtc",
        "pps": "getBidPblancListInfoEtcPPSSrch",
    },
}

BIDNOTICE_PURPOSE = (
    "나라장터 입찰공고 (data.go.kr 15129394, BidPublicInfoService). "
    "Use for 물품/공사/용역/외자/기타 입찰공고 — 공고명, 발주·수요기관, 마감일시, 추정가격. "
    "This is bid announcements, not awarded contracts (use KONEPS), not 휴·폐업 (Bizverify), "
    "and not 지원사업 (Bizinfo). Vendor 상호 is not a request param."
)

BIDNOTICE_TOOLS = [
    {
        "name": "bidnotice_search",
        "description": (
            BIDNOTICE_PURPOSE
            + " 나라장터검색조건 조회 (getBidPblancListInfo*PPSSrch). "
            "Date range (YYYYMMDD or YYYYMMDDHHMM, max 31 days), 공고명, 공고/수요기관, 공고번호."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["goods", "construction", "service", "foreign", "etc"],
                    "description": "물품/공사/용역/외자/기타. Default goods.",
                },
                "startDate": {
                    "type": "string",
                    "description": "공고게시 시작 YYYYMMDD 또는 YYYYMMDDHHMM (inqryDiv=1)",
                },
                "endDate": {"type": "string", "description": "공고게시 종료 YYYYMMDD 또는 YYYYMMDDHHMM"},
                "title": {"type": "string", "description": "입찰공고명 (일부 일치)"},
                "institutionName": {"type": "string", "description": "공고기관 또는 수요기관명"},
                "institutionDiv": {
                    "type": "string",
                    "description": "1=공고기관(기본), 2=수요기관",
                },
                "regionName": {"type": "string", "description": "참가제한지역명"},
                "industryName": {"type": "string", "description": "업종명"},
                "noticeNo": {"type": "string", "description": "입찰공고번호 — uses list op inqryDiv=2"},
                "openOnly": {
                    "type": "boolean",
                    "description": "true면 입찰마감 전 공고만 (bidClseExcpYn=Y + local filter)",
                },
                "dateField": {
                    "type": "string",
                    "description": "1=공고게시일시(기본), 2=개찰일시",
                },
                "page": {"type": "integer"},
                "display": {"type": "integer"},
            },
        },
    },
    {
        "name": "bidnotice_get",
        "description": (
            BIDNOTICE_PURPOSE
            + " 입찰공고번호로 목록 조회 (getBidPblancListInfo*, inqryDiv=2)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "noticeNo": {"type": "string", "description": "입찰공고번호 (bidNtceNo)"},
                "category": {
                    "type": "string",
                    "enum": ["goods", "construction", "service", "foreign", "etc"],
                },
            },
            "required": ["noticeNo"],
        },
    },
    {
        "name": "bidnotice_lookup",
        "description": (
            BIDNOTICE_PURPOSE
            + " Convenience: scan PPS rows in a date window for 공고명 and/or 수요·공고기관. "
            "Default last 31 days, all categories. Use for B2G bid alerts on a client 기관명."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "입찰공고명 키워드"},
                "institutionName": {"type": "string", "description": "수요기관명 (기본) 또는 공고기관명"},
                "institutionDiv": {
                    "type": "string",
                    "description": "1=공고기관, 2=수요기관(기본)",
                },
                "startDate": {"type": "string", "description": "YYYYMMDD. Default 31일 전"},
                "endDate": {"type": "string", "description": "YYYYMMDD. Default today"},
                "category": {
                    "type": "string",
                    "enum": ["all", "goods", "construction", "service", "foreign", "etc"],
                },
                "openOnly": {"type": "boolean", "description": "마감 전 공고만. Default false"},
                "display": {"type": "integer"},
            },
        },
    },
]

bidnotice_router = APIRouter(prefix="/bidnotice", tags=["BidNotice"])


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
        f"Apply at {BIDNOTICE_APPLY_URL} "
        "with 서비스 URL https://tunneling-service.onrender.com/"
    )


def clamp_rows(value: Any, default: int = 10) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_ROWS))


def clamp_page(value: Any, default: int = 1) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, n)


def digits_only(raw: str | None) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def parse_ymd(raw: str | None) -> date | None:
    text = digits_only(raw)
    if len(text) < 8:
        return None
    try:
        return datetime.strptime(text[:8], "%Y%m%d").date()
    except ValueError:
        return None


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def default_range() -> tuple[date, date]:
    end = date.today()
    return end - timedelta(days=MAX_RANGE_DAYS - 1), end


def ensure_range(start_raw: Any, end_raw: Any) -> tuple[date, date]:
    start = parse_ymd(str(start_raw) if start_raw else "") or default_range()[0]
    end = parse_ymd(str(end_raw) if end_raw else "") or date.today()
    if end < start:
        start, end = end, start
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise RuntimeError(f"조회기간은 최대 {MAX_RANGE_DAYS}일입니다. startDate/endDate를 줄이세요.")
    return start, end


def to_dt_start(raw: Any, fallback: date) -> str:
    text = digits_only(str(raw) if raw else "")
    if len(text) >= 12:
        return text[:12]
    if len(text) >= 8:
        return text[:8] + "0000"
    return ymd(fallback) + "0000"


def to_dt_end(raw: Any, fallback: date) -> str:
    text = digits_only(str(raw) if raw else "")
    if len(text) >= 12:
        return text[:12]
    if len(text) >= 8:
        return text[:8] + "2359"
    return ymd(fallback) + "2359"


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def xml_items(root: ET.Element) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        row = {child.tag: (child.text or "").strip() for child in list(item)}
        if row:
            items.append(row)
    return items


def bidnotice_fault_message(code: str, msg: str) -> str:
    code = (code or "").strip()
    if code in {"12", "NO_OPENAPI_SERVICE_ERROR"} or "NO_OPENAPI_SERVICE" in (msg or ""):
        return (
            "DATA_GO_KR_API_KEY is not approved for 나라장터 입찰공고 (15129394). "
            f"활용신청: {BIDNOTICE_APPLY_URL} — 서비스 URL https://tunneling-service.onrender.com/"
        )
    if code in {"30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}:
        return f"data.go.kr serviceKey is not registered. Apply at {BIDNOTICE_APPLY_URL}"
    return f"나라장터 입찰공고 API 오류 ({code or 'unknown'}): {msg}".strip()


def parse_payload(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise RuntimeError("나라장터 입찰공고 API 빈 응답")
    if raw[0] in "{[":
        data = json.loads(raw)
        fault = (
            data.get("OpenAPI_ServiceResponse", {}).get("cmmMsgHeader")
            if isinstance(data, dict)
            else None
        )
        if fault:
            code = str(fault.get("returnReasonCode") or fault.get("returnAuthMsg") or "")
            msg = str(fault.get("errMsg") or fault.get("returnAuthMsg") or "SERVICE ERROR")
            raise RuntimeError(bidnotice_fault_message(code, msg))
        return data
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"나라장터 입찰공고 API 응답 파싱 실패: {raw[:200]}") from exc
    fault = root.find(".//cmmMsgHeader")
    if fault is not None:
        code = _text(fault.find("returnReasonCode")) or _text(fault.find("returnAuthMsg"))
        msg = _text(fault.find("errMsg")) or _text(fault.find("returnAuthMsg"))
        raise RuntimeError(bidnotice_fault_message(code, msg))
    header = root.find("header") or root.find(".//header")
    body = root.find("body") or root.find(".//body")
    return {
        "response": {
            "header": {
                "resultCode": _text(header.find("resultCode") if header is not None else None),
                "resultMsg": _text(header.find("resultMsg") if header is not None else None),
            },
            "body": {
                "items": {"item": xml_items(root)},
                "numOfRows": _text(body.find("numOfRows") if body is not None else None),
                "pageNo": _text(body.find("pageNo") if body is not None else None),
                "totalCount": _text(body.find("totalCount") if body is not None else None),
            },
        }
    }


def unwrap_items(body: Any) -> list[dict]:
    if not isinstance(body, dict):
        return []
    items = body.get("items")
    if items in (None, ""):
        return []
    if isinstance(items, list):
        raw = items
    elif isinstance(items, dict):
        raw = items.get("item")
        if raw is None:
            raw = []
    else:
        raw = []
    if isinstance(raw, dict):
        raw = [raw]
    return [row for row in raw if isinstance(row, dict)]


def first_value(row: dict, *keys: str) -> str:
    for key in keys:
        if row.get(key) not in (None, ""):
            return str(row[key])
    return ""


def as_int(value: Any) -> int | None:
    raw = str(value or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def parse_close_dt(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            return datetime.strptime(text[:19] if " " in text else text[:14], fmt)
        except ValueError:
            continue
    return None


def is_still_open(close_raw: str) -> bool | None:
    close = parse_close_dt(close_raw)
    if close is None:
        return None
    return close > datetime.now()


def normalize_row(row: dict, category: str) -> dict[str, Any]:
    close = first_value(row, "bidClseDt")
    return {
        "category": category,
        "categoryLabel": CATEGORIES[category]["label"],
        "noticeNo": first_value(row, "bidNtceNo"),
        "noticeOrd": first_value(row, "bidNtceOrd"),
        "title": first_value(row, "bidNtceNm"),
        "kind": first_value(row, "ntceKindNm"),
        "reNotice": first_value(row, "reNtceYn"),
        "registerType": first_value(row, "rgstTyNm"),
        "international": first_value(row, "intrbidYn"),
        "noticeDate": first_value(row, "bidNtceDt"),
        "closeDate": close,
        "openDate": first_value(row, "opengDt"),
        "refNo": first_value(row, "refNo"),
        "noticeInstitution": first_value(row, "ntceInsttNm"),
        "noticeInstitutionCode": first_value(row, "ntceInsttCd"),
        "demandInstitution": first_value(row, "dminsttNm"),
        "demandInstitutionCode": first_value(row, "dminsttCd"),
        "bidMethod": first_value(row, "bidMethdNm"),
        "contractMethod": first_value(row, "cntrctCnclsMthdNm"),
        "awardMethod": first_value(row, "sucsfbidMthdNm"),
        "estimatedPrice": as_int(first_value(row, "presmptPrce")),
        "budgetAmount": as_int(first_value(row, "asignBdgtAmt", "bdgtAmt")),
        "regionLimit": first_value(row, "prtcptLmtRgnNm"),
        "industryName": first_value(row, "indstrytyNm"),
        "industryCode": first_value(row, "indstrytyCd"),
        "detailUrl": first_value(row, "bidNtceDtlUrl"),
        "isOpen": is_still_open(close),
    }


def parse_page(data: dict[str, Any], category: str) -> dict[str, Any]:
    envelope = data.get("response") if isinstance(data.get("response"), dict) else data
    header = envelope.get("header") or {}
    body = envelope.get("body") or {}
    result_code = str(header.get("resultCode") or "")
    result_msg = str(header.get("resultMsg") or "")
    if result_code and result_code not in {"00", "0"}:
        raise RuntimeError(bidnotice_fault_message(result_code, result_msg))
    items = [normalize_row(row, category) for row in unwrap_items(body)]
    return {
        "page": as_int(body.get("pageNo")) or 1,
        "display": as_int(body.get("numOfRows")) or len(items),
        "totalCount": as_int(body.get("totalCount")) or len(items),
        "items": items,
    }


async def bidnotice_get(op: str, extra: dict[str, Any], category: str) -> dict[str, Any]:
    key = _service_key()
    if not key:
        raise missing_key_error()
    params: dict[str, Any] = {
        "ServiceKey": key,
        "serviceKey": key,
        "type": "json",
        "pageNo": extra.get("pageNo", 1),
        "numOfRows": extra.get("numOfRows", 10),
    }
    for name, value in extra.items():
        if name in {"pageNo", "numOfRows"}:
            continue
        if value not in (None, ""):
            params[name] = value
    url = f"{BIDNOTICE_BASE}/{op}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, params=params)
    text = resp.text
    if resp.status_code >= 400:
        raise RuntimeError(f"apis.data.go.kr HTTP {resp.status_code}: {text[:200]}")
    try:
        payload = parse_payload(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"나라장터 입찰공고 API 응답 파싱 실패: {text[:200]}") from exc
    return parse_page(payload, category)


def resolve_category(raw: Any, default: str = "goods") -> str:
    value = str(raw or default).strip().lower()
    aliases = {
        "물품": "goods",
        "공사": "construction",
        "용역": "service",
        "외자": "foreign",
        "기타": "etc",
    }
    value = aliases.get(value, value)
    if value not in CATEGORIES:
        raise RuntimeError(f"category must be one of {', '.join(CATEGORIES)}")
    return value


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


async def get_notice(args: dict[str, Any]) -> dict[str, Any]:
    notice_no = str(args.get("noticeNo") or args.get("bidNtceNo") or "").strip()
    if not notice_no:
        raise RuntimeError("noticeNo is required")
    raw_cat = str(args.get("category") or "").strip().lower()
    cats = [resolve_category(raw_cat)] if raw_cat and raw_cat != "all" else list(CATEGORIES)
    extra = {
        "inqryDiv": "2",
        "bidNtceNo": notice_no,
        "pageNo": 1,
        "numOfRows": 10,
    }
    last: dict[str, Any] = {"page": 1, "display": 10, "totalCount": 0, "items": []}
    for category in cats:
        page = await bidnotice_get(CATEGORIES[category]["list"], extra, category)
        if page.get("items"):
            return page
        last = page
    return last


async def search_notices(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("noticeNo") or args.get("bidNtceNo"):
        return await get_notice(args)
    category = resolve_category(args.get("category"), "goods")
    start, end = ensure_range(args.get("startDate") or args.get("inqryBgnDt"), args.get("endDate") or args.get("inqryEndDt"))
    extra: dict[str, Any] = {
        "pageNo": clamp_page(args.get("page") or args.get("pageNo"), 1),
        "numOfRows": clamp_rows(args.get("display") or args.get("numOfRows"), 10),
        "inqryDiv": str(args.get("dateField") or args.get("inqryDiv") or "1").strip() or "1",
        "inqryBgnDt": to_dt_start(args.get("startDate") or args.get("inqryBgnDt"), start),
        "inqryEndDt": to_dt_end(args.get("endDate") or args.get("inqryEndDt"), end),
    }
    if args.get("title") or args.get("bidNtceNm"):
        extra["bidNtceNm"] = str(args.get("title") or args.get("bidNtceNm")).strip()
    inst = str(args.get("institutionName") or "").strip()
    inst_div = str(args.get("institutionDiv") or "1").strip() or "1"
    if inst:
        if inst_div == "2" or args.get("dminsttNm"):
            extra["dminsttNm"] = inst
        else:
            extra["ntceInsttNm"] = inst
    if args.get("dminsttNm") and "dminsttNm" not in extra:
        extra["dminsttNm"] = str(args.get("dminsttNm")).strip()
    if args.get("ntceInsttNm") and "ntceInsttNm" not in extra:
        extra["ntceInsttNm"] = str(args.get("ntceInsttNm")).strip()
    if args.get("regionName") or args.get("prtcptLmtRgnNm"):
        extra["prtcptLmtRgnNm"] = str(args.get("regionName") or args.get("prtcptLmtRgnNm")).strip()
    if args.get("industryName") or args.get("indstrytyNm"):
        extra["indstrytyNm"] = str(args.get("industryName") or args.get("indstrytyNm")).strip()
    if truthy(args.get("openOnly")):
        extra["bidClseExcpYn"] = "Y"
    page = await bidnotice_get(CATEGORIES[category]["pps"], extra, category)
    if truthy(args.get("openOnly")):
        page["items"] = [row for row in page["items"] if row.get("isOpen") is not False]
    page["query"] = {k: extra[k] for k in extra if k not in {"pageNo", "numOfRows"}}
    return page


async def lookup_notices(args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title") or args.get("bidNtceNm") or "").strip()
    inst = str(args.get("institutionName") or args.get("companyName") or "").strip()
    notice_no = str(args.get("noticeNo") or args.get("bidNtceNo") or "").strip()
    if not title and not inst and not notice_no:
        raise RuntimeError("title, institutionName, or noticeNo is required")
    if notice_no:
        page = await get_notice({"noticeNo": notice_no, "category": args.get("category")})
        items = page.get("items") or []
        return {
            "found": bool(items),
            "matchCount": len(items),
            "scannedCount": len(items),
            "items": items,
        }
    start, end = ensure_range(args.get("startDate"), args.get("endDate"))
    raw_cat = str(args.get("category") or "all").strip().lower()
    cats = list(CATEGORIES) if raw_cat in {"", "all"} else [resolve_category(raw_cat)]
    inst_div = str(args.get("institutionDiv") or "2").strip() or "2"
    matched: list[dict[str, Any]] = []
    scanned = 0
    for category in cats:
        page_no = 1
        while page_no <= MAX_LOOKUP_PAGES:
            page = await search_notices(
                {
                    "category": category,
                    "startDate": ymd(start),
                    "endDate": ymd(end),
                    "title": title,
                    "institutionName": inst,
                    "institutionDiv": inst_div,
                    "openOnly": args.get("openOnly"),
                    "page": page_no,
                    "display": clamp_rows(args.get("display"), 100),
                }
            )
            scanned += len(page["items"])
            matched.extend(page["items"])
            total = page.get("totalCount") or 0
            if page_no * (page.get("display") or 100) >= total:
                break
            page_no += 1
    return {
        "found": bool(matched),
        "matchCount": len(matched),
        "scannedCount": scanned,
        "startDate": ymd(start),
        "endDate": ymd(end),
        "categories": cats,
        "items": matched,
        "hint": None
        if matched
        else "No bid notice in this window matched. Widen dates (max 31 days) or try 수요기관/공고명.",
    }


async def execute_tool(name: str, args: dict | None) -> Any:
    args = args or {}
    if name == "bidnotice_search":
        return await search_notices(args)
    if name == "bidnotice_get":
        return await get_notice(args)
    if name == "bidnotice_lookup":
        return await lookup_notices(args)
    raise RuntimeError(f"Unknown tool: {name}")


async def handle_bidnotice_http(method: str, path: str, body: dict | None) -> JSONResponse:
    clean = path.strip("/")
    if method == "GET" and clean == "tools":
        return JSONResponse(BIDNOTICE_TOOLS)
    if method == "POST" and clean == "tools/call":
        tool = (body or {}).get("tool")
        if not tool:
            return mcp_err('Missing "tool" parameter', 400)
        try:
            return mcp_ok(await execute_tool(str(tool), (body or {}).get("arguments") or {}))
        except Exception as exc:
            print(f"❌ BidNotice tool {tool}: {exc}")
            return mcp_err(str(exc), 500)
    return mcp_err("BidNotice MCP endpoint not found", 404)


@bidnotice_router.get("/tools")
async def list_tools():
    if not _service_key():
        return mcp_err("DATA_GO_KR_API_KEY is not set on tunneling-service", 503)
    return JSONResponse(BIDNOTICE_TOOLS)


@bidnotice_router.post("/tools/call")
async def call_tool(request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key or not _egdesk_api_key_is_registered(api_key):
        return mcp_err("Missing or invalid X-Api-Key (EGDesk tunnel key required)", 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await handle_bidnotice_http("POST", "tools/call", body)


def _egdesk_api_key_is_registered(api_key: str) -> bool:
    url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not service_key or not api_key:
        return False
    try:
        from supabase import create_client

        client = create_client(url, service_key)
        result = client.table("mcp_servers").select("description").limit(500).execute()
        for row in result.data or []:
            try:
                if json.loads(row.get("description") or "{}").get("api_key") == api_key:
                    return True
            except Exception:
                continue
    except Exception as exc:
        print(f"⚠️ BidNotice root API key lookup failed: {exc}")
        return False
    return False
