"""
KONEPS / 나라장터 계약정보 — executed on the tunnel gateway.

DATA_GO_KR_API_KEY lives here. Never on EGDesk desktop or customer apps.
Apply for dataset 15129427 on the same data.go.kr account key.
Docs: https://www.data.go.kr/data/15129427/openapi.do
Spec: 조달청_OpenAPI참고자료_나라장터_계약정보서비스_1.0
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

KONEPS_BASE = "https://apis.data.go.kr/1230000/ao/CntrctInfoService"
KONEPS_APPLY_URL = "https://www.data.go.kr/data/15129427/openapi.do"
MAX_ROWS = 100
MAX_LOOKUP_PAGES = 5
MAX_RANGE_DAYS = 31

CATEGORIES = {
    "goods": {
        "label": "물품",
        "list": "getCntrctInfoListThng",
        "pps": "getCntrctInfoListThngPPSSrch",
    },
    "construction": {
        "label": "공사",
        "list": "getCntrctInfoListCnstwk",
        "pps": "getCntrctInfoListCnstwkPPSSrch",
    },
    "service": {
        "label": "용역",
        "list": "getCntrctInfoListServc",
        "pps": "getCntrctInfoListServcPPSSrch",
    },
    "foreign": {
        "label": "외자",
        "list": "getCntrctInfoListFrgcpt",
        "pps": "getCntrctInfoListFrgcptPPSSrch",
    },
}

KONEPS_PURPOSE = (
    "나라장터 계약정보 (data.go.kr 15129427, CntrctInfoService). "
    "Use for 체결된 공공계약 — 물품/공사/용역/외자. "
    "This is awarded-contract history, not 입찰공고 and not 휴·폐업 (Bizverify) or 지원사업 (Bizinfo). "
    "Vendor name/사업자번호 is inside corpList; PPS search filters by 기관명·품명·공고번호·계약일자, not 상호."
)

KONEPS_TOOLS = [
    {
        "name": "koneps_search",
        "description": (
            KONEPS_PURPOSE
            + " 나라장터검색조건 조회 (getCntrctInfoList*PPSSrch). "
            "Date range (YYYYMMDD, max 31 days) or 확정계약번호/요청번호/공고번호."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["goods", "construction", "service", "foreign"],
                    "description": "물품/공사/용역/외자. Default goods.",
                },
                "startDate": {"type": "string", "description": "계약체결 시작 YYYYMMDD (inqryDiv=1)"},
                "endDate": {"type": "string", "description": "계약체결 종료 YYYYMMDD"},
                "institutionName": {"type": "string", "description": "계약기관 또는 수요기관명"},
                "institutionDiv": {
                    "type": "string",
                    "description": "1=계약기관(기본), 2=수요기관",
                },
                "productName": {"type": "string", "description": "품명"},
                "method": {
                    "type": "string",
                    "description": "1일반경쟁 2제한경쟁 3지명경쟁 4수의계약",
                },
                "noticeNo": {"type": "string", "description": "공고번호 — sets inqryDiv=4"},
                "contractNo": {"type": "string", "description": "확정계약번호 — sets inqryDiv=2"},
                "requestNo": {"type": "string", "description": "요청번호 — sets inqryDiv=3"},
                "page": {"type": "integer"},
                "display": {"type": "integer"},
            },
        },
    },
    {
        "name": "koneps_get",
        "description": (
            KONEPS_PURPOSE
            + " 통합계약번호로 목록 조회 (getCntrctInfoList*, inqryDiv=2)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "untyCntrctNo": {"type": "string", "description": "통합계약번호"},
                "category": {
                    "type": "string",
                    "enum": ["goods", "construction", "service", "foreign"],
                },
            },
            "required": ["untyCntrctNo"],
        },
    },
    {
        "name": "koneps_lookup",
        "description": (
            KONEPS_PURPOSE
            + " Convenience: scan PPS rows in a date window and keep contracts whose corpList "
            "matches 상호 and/or 사업자번호. Default last 31 days, all categories."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "companyName": {"type": "string", "description": "업체명 (상호)"},
                "businessNumber": {"type": "string", "description": "사업자등록번호 (하이픈 허용)"},
                "startDate": {"type": "string", "description": "YYYYMMDD. Default 31일 전"},
                "endDate": {"type": "string", "description": "YYYYMMDD. Default today"},
                "category": {
                    "type": "string",
                    "enum": ["all", "goods", "construction", "service", "foreign"],
                },
                "display": {"type": "integer"},
            },
        },
    },
]

koneps_router = APIRouter(prefix="/koneps", tags=["KONEPS"])


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
        f"Apply at {KONEPS_APPLY_URL} "
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


def koneps_fault_message(code: str, msg: str) -> str:
    code = (code or "").strip()
    if code in {"12", "NO_OPENAPI_SERVICE_ERROR"} or "NO_OPENAPI_SERVICE" in (msg or ""):
        return (
            "DATA_GO_KR_API_KEY is not approved for 나라장터 계약정보 (15129427). "
            f"활용신청: {KONEPS_APPLY_URL} — 서비스 URL https://tunneling-service.onrender.com/"
        )
    if code in {"30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}:
        return f"data.go.kr serviceKey is not registered. Apply at {KONEPS_APPLY_URL}"
    return f"나라장터 API 오류 ({code or 'unknown'}): {msg}".strip()


def parse_payload(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise RuntimeError("나라장터 API 빈 응답")
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
            raise RuntimeError(koneps_fault_message(code, msg))
        return data
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"나라장터 API 응답 파싱 실패: {raw[:200]}") from exc
    fault = root.find(".//cmmMsgHeader")
    if fault is not None:
        code = _text(fault.find("returnReasonCode")) or _text(fault.find("returnAuthMsg"))
        msg = _text(fault.find("errMsg")) or _text(fault.find("returnAuthMsg"))
        raise RuntimeError(koneps_fault_message(code, msg))
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


def parse_caret_groups(raw: str) -> list[list[str]]:
    groups = re.findall(r"\[([^\]]+)\]", str(raw or ""))
    if not groups and raw:
        groups = [str(raw)]
    return [part.split("^") for part in groups]


def parse_corps(raw: str) -> list[dict[str, str]]:
    out = []
    for parts in parse_caret_groups(raw):
        out.append(
            {
                "seq": parts[0] if len(parts) > 0 else "",
                "role": parts[1] if len(parts) > 1 else "",
                "jointType": parts[2] if len(parts) > 2 else "",
                "name": parts[3] if len(parts) > 3 else "",
                "representative": parts[4] if len(parts) > 4 else "",
                "nationality": parts[5] if len(parts) > 5 else "",
                "share": parts[6] if len(parts) > 6 else "",
                "creditor": parts[7] if len(parts) > 7 else "",
                "contact": parts[8] if len(parts) > 8 else "",
                "businessNumber": parts[9] if len(parts) > 9 else "",
            }
        )
    return out


def parse_agencies(raw: str) -> list[dict[str, str]]:
    out = []
    for parts in parse_caret_groups(raw):
        out.append(
            {
                "seq": parts[0] if len(parts) > 0 else "",
                "code": parts[1] if len(parts) > 1 else "",
                "name": parts[2] if len(parts) > 2 else "",
                "jurisdiction": parts[3] if len(parts) > 3 else "",
                "dept": parts[4] if len(parts) > 4 else "",
                "officer": parts[5] if len(parts) > 5 else "",
                "phone": parts[6] if len(parts) > 6 else "",
            }
        )
    return out


def normalize_row(row: dict, category: str) -> dict[str, Any]:
    corps = parse_corps(first_value(row, "corpList"))
    return {
        "category": category,
        "categoryLabel": CATEGORIES[category]["label"],
        "untyCntrctNo": first_value(row, "untyCntrctNo"),
        "businessType": first_value(row, "bsnsDivNm"),
        "contractNo": first_value(row, "dcsnCntrctNo"),
        "contractRefNo": first_value(row, "cntrctRefNo"),
        "title": first_value(row, "cntrctNm"),
        "signedDate": first_value(row, "cntrctCnclsDate", "cntrctDate"),
        "period": first_value(row, "cntrctPrd"),
        "method": first_value(row, "cntrctCnclsMthdNm"),
        "totalAmount": as_int(first_value(row, "totCntrctAmt")),
        "amount": as_int(first_value(row, "thtmCntrctAmt")),
        "noticeNo": first_value(row, "ntceNo"),
        "requestNo": first_value(row, "reqNo"),
        "institutionName": first_value(row, "cntrctInsttNm"),
        "institutionCode": first_value(row, "cntrctInsttCd"),
        "detailUrl": first_value(row, "cntrctDtlInfoUrl"),
        "infoUrl": first_value(row, "cntrctInfoUrl"),
        "jointContract": first_value(row, "cmmnCntrctYn"),
        "longTerm": first_value(row, "lngtrmCtnuDivNm"),
        "law": first_value(row, "baseLawNm"),
        "basis": first_value(row, "baseDtls"),
        "registeredAt": first_value(row, "rgstDt"),
        "changedAt": first_value(row, "chgDt"),
        "procurementClass": first_value(row, "pubPrcrmntClsfcNm"),
        "corps": corps,
        "agencies": parse_agencies(first_value(row, "dminsttList")),
    }


def parse_page(data: dict[str, Any], category: str) -> dict[str, Any]:
    envelope = data.get("response") if isinstance(data.get("response"), dict) else data
    header = envelope.get("header") or {}
    body = envelope.get("body") or {}
    result_code = str(header.get("resultCode") or "")
    result_msg = str(header.get("resultMsg") or "")
    if result_code and result_code not in {"00", "0"}:
        raise RuntimeError(koneps_fault_message(result_code, result_msg))
    items = [normalize_row(row, category) for row in unwrap_items(body)]
    return {
        "page": as_int(body.get("pageNo")) or 1,
        "display": as_int(body.get("numOfRows")) or len(items),
        "totalCount": as_int(body.get("totalCount")) or len(items),
        "items": items,
    }


async def koneps_get(op: str, extra: dict[str, Any], category: str) -> dict[str, Any]:
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
    url = f"{KONEPS_BASE}/{op}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, params=params)
    text = resp.text
    if resp.status_code >= 400:
        raise RuntimeError(f"apis.data.go.kr HTTP {resp.status_code}: {text[:200]}")
    try:
        payload = parse_payload(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"나라장터 API 응답 파싱 실패: {text[:200]}") from exc
    return parse_page(payload, category)


def resolve_category(raw: Any, default: str = "goods") -> str:
    value = str(raw or default).strip().lower()
    aliases = {"물품": "goods", "공사": "construction", "용역": "service", "외자": "foreign"}
    value = aliases.get(value, value)
    if value not in CATEGORIES:
        raise RuntimeError(f"category must be one of {', '.join(CATEGORIES)}")
    return value


async def search_contracts(args: dict[str, Any]) -> dict[str, Any]:
    category = resolve_category(args.get("category"), "goods")
    extra: dict[str, Any] = {
        "pageNo": clamp_page(args.get("page") or args.get("pageNo"), 1),
        "numOfRows": clamp_rows(args.get("display") or args.get("numOfRows"), 10),
    }
    if args.get("noticeNo") or args.get("ntceNo"):
        extra["inqryDiv"] = "4"
        extra["ntceNo"] = str(args.get("noticeNo") or args.get("ntceNo")).strip()
    elif args.get("contractNo") or args.get("dcsnCntrctNo"):
        extra["inqryDiv"] = "2"
        extra["dcsnCntrctNo"] = str(args.get("contractNo") or args.get("dcsnCntrctNo")).strip()
    elif args.get("requestNo") or args.get("reqNo"):
        extra["inqryDiv"] = "3"
        extra["reqNo"] = str(args.get("requestNo") or args.get("reqNo")).strip()
    else:
        start, end = ensure_range(args.get("startDate") or args.get("inqryBgnDate"), args.get("endDate") or args.get("inqryEndDate"))
        extra["inqryDiv"] = "1"
        extra["inqryBgnDate"] = ymd(start)
        extra["inqryEndDate"] = ymd(end)
    if args.get("institutionName") or args.get("insttNm"):
        extra["insttNm"] = str(args.get("institutionName") or args.get("insttNm")).strip()
    if args.get("institutionDiv") or args.get("insttDivCd"):
        extra["insttDivCd"] = str(args.get("institutionDiv") or args.get("insttDivCd")).strip()
    if args.get("productName") or args.get("prdctClsfcNoNm"):
        extra["prdctClsfcNoNm"] = str(args.get("productName") or args.get("prdctClsfcNoNm")).strip()
    if args.get("method") or args.get("cntrctMthdCd"):
        extra["cntrctMthdCd"] = str(args.get("method") or args.get("cntrctMthdCd")).strip()
    page = await koneps_get(CATEGORIES[category]["pps"], extra, category)
    page["query"] = {k: extra[k] for k in extra if k not in {"pageNo", "numOfRows"}}
    return page


async def get_contract(args: dict[str, Any]) -> dict[str, Any]:
    unty = str(args.get("untyCntrctNo") or "").strip()
    if not unty:
        raise RuntimeError("untyCntrctNo is required")
    category = resolve_category(args.get("category"), "goods")
    extra = {
        "inqryDiv": "2",
        "untyCntrctNo": unty,
        "pageNo": 1,
        "numOfRows": 10,
    }
    return await koneps_get(CATEGORIES[category]["list"], extra, category)


def corp_matches(corps: list[dict[str, str]], name: str, bn: str) -> list[dict[str, str]]:
    hits = []
    seen: set[tuple[str, str]] = set()
    for corp in corps:
        corp_bn = digits_only(corp.get("businessNumber"))
        corp_name = str(corp.get("name") or "")
        bn_ok = bool(bn) and (bn in corp_bn or corp_bn.startswith(bn[:6]) if len(bn) >= 6 else bn in corp_bn)
        name_ok = bool(name) and name in corp_name
        if bn and name:
            matched = bn_ok
        elif bn:
            matched = bn_ok
        else:
            matched = name_ok
        if not matched:
            continue
        key = (corp_name, corp_bn)
        if key in seen:
            continue
        seen.add(key)
        hits.append(corp)
    return hits


async def lookup_vendor(args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("companyName") or args.get("workplaceName") or "").strip()
    bn = digits_only(args.get("businessNumber"))
    if not name and len(bn) < 6:
        raise RuntimeError("companyName or businessNumber (min 6 digits) is required")
    start, end = ensure_range(args.get("startDate"), args.get("endDate"))
    raw_cat = str(args.get("category") or "all").strip().lower()
    cats = list(CATEGORIES) if raw_cat in {"", "all"} else [resolve_category(raw_cat)]
    matched: list[dict[str, Any]] = []
    scanned = 0
    for category in cats:
        page_no = 1
        while page_no <= MAX_LOOKUP_PAGES:
            page = await search_contracts(
                {
                    "category": category,
                    "startDate": ymd(start),
                    "endDate": ymd(end),
                    "page": page_no,
                    "display": clamp_rows(args.get("display"), 100),
                }
            )
            scanned += len(page["items"])
            for item in page["items"]:
                hits = corp_matches(item.get("corps") or [], name, bn)
                if hits:
                    row = dict(item)
                    row["matchedCorps"] = hits
                    matched.append(row)
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
        else "No awarded contract in this window matched that vendor. Widen dates (max 31 days per call) or check 지사/상호.",
    }


async def execute_tool(name: str, args: dict | None) -> Any:
    args = args or {}
    if name == "koneps_search":
        return await search_contracts(args)
    if name == "koneps_get":
        return await get_contract(args)
    if name == "koneps_lookup":
        return await lookup_vendor(args)
    raise RuntimeError(f"Unknown tool: {name}")


async def handle_koneps_http(method: str, path: str, body: dict | None) -> JSONResponse:
    clean = path.strip("/")
    if method == "GET" and clean == "tools":
        return JSONResponse(KONEPS_TOOLS)
    if method == "POST" and clean == "tools/call":
        tool = (body or {}).get("tool")
        if not tool:
            return mcp_err('Missing "tool" parameter', 400)
        try:
            return mcp_ok(await execute_tool(str(tool), (body or {}).get("arguments") or {}))
        except Exception as exc:
            print(f"❌ KONEPS tool {tool}: {exc}")
            return mcp_err(str(exc), 500)
    return mcp_err("KONEPS MCP endpoint not found", 404)


@koneps_router.get("/tools")
async def list_tools():
    if not _service_key():
        return mcp_err("DATA_GO_KR_API_KEY is not set on tunneling-service", 503)
    return JSONResponse(KONEPS_TOOLS)


@koneps_router.post("/tools/call")
async def call_tool(request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key or not _egdesk_api_key_is_registered(api_key):
        return mcp_err("Missing or invalid X-Api-Key (EGDesk tunnel key required)", 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await handle_koneps_http("POST", "tools/call", body)


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
        print(f"⚠️ KONEPS root API key lookup failed: {exc}")
        return False
    return False
