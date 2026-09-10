"""
NPS (국민연금 가입 사업장) — executed on the tunnel gateway.

DATA_GO_KR_API_KEY lives here. Never on EGDesk desktop or customer apps.
Apply for dataset 3046071 with the same data.go.kr account key used by Bizverify.
Docs: https://www.data.go.kr/data/3046071/openapi.do
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

NPS_BASE = "https://apis.data.go.kr/B552015/NpsBplcInfoInqireServiceV2"
NPS_SEARCH = f"{NPS_BASE}/getBassInfoSearchV2"
NPS_DETAIL = f"{NPS_BASE}/getDetailInfoSearchV2"
NPS_TREND = f"{NPS_BASE}/getPdAcctoSttusInfoSearchV2"
NPS_APPLY_URL = "https://www.data.go.kr/data/3046071/openapi.do"
MAX_ROWS = 100

NPS_PURPOSE = (
    "국민연금 가입 사업장 내역 (data.go.kr 3046071, V2). "
    "Use for 가입자 수·고용 규모·월별 취득/상실 추이. "
    "Not a company registry and not 휴·폐업 verify — use Bizverify for 사업자등록 상태. "
    "Open coverage: 가입자 3인 이상 법인; 개인은 2025.7 이후 10인 이상. "
    "사업자번호는 앞 6자리만 공개. seq is a monthly snapshot id, not a stable workplace key."
)

NPS_TOOLS = [
    {
        "name": "nps_search",
        "description": (
            NPS_PURPOSE
            + " 사업장 기본정보 조회 (getBassInfoSearchV2). "
            "Search by 사업장명 and/or 사업자번호 앞 6자리. Returns monthly snapshot rows (seq + dataCrtYm)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workplaceName": {
                    "type": "string",
                    "description": "사업장명 (상호). 예: 삼성전자",
                },
                "businessNumber": {
                    "type": "string",
                    "description": "사업자등록번호 (하이픈 허용). 앞 6자리만 사용.",
                },
                "sidoCode": {
                    "type": "string",
                    "description": "법정동 광역시도 코드 2자리. 11서울 26부산 41경기",
                },
                "sigunguCode": {
                    "type": "string",
                    "description": "법정동 시군구 코드",
                },
                "emdCode": {
                    "type": "string",
                    "description": "법정동 읍면동 코드",
                },
                "page": {"type": "integer", "description": "페이지 번호 (기본 1)"},
                "display": {
                    "type": "integer",
                    "description": f"페이지당 건수 (기본 10, 최대 {MAX_ROWS})",
                },
            },
        },
    },
    {
        "name": "nps_detail",
        "description": (
            NPS_PURPOSE
            + " 사업장 상세정보 조회 (getDetailInfoSearchV2). "
            "Requires seq from nps_search. Returns 가입자수, 당월고지금액, 업종."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "seq": {"type": "string", "description": "nps_search 결과의 seq"},
                "page": {"type": "integer"},
                "display": {"type": "integer"},
            },
            "required": ["seq"],
        },
    },
    {
        "name": "nps_trend",
        "description": (
            NPS_PURPOSE
            + " 기간별 현황 (getPdAcctoSttusInfoSearchV2) — 가입자 추이. "
            "Requires seq from nps_search. Monthly 취득/상실/가입자수."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "seq": {"type": "string", "description": "nps_search 결과의 seq"},
                "page": {"type": "integer"},
                "display": {"type": "integer"},
            },
            "required": ["seq"],
        },
    },
    {
        "name": "nps_lookup",
        "description": (
            NPS_PURPOSE
            + " Convenience: search by 상호 (+ optional 사업자번호), pick the latest month, "
            "then return detail + 가입자 추이. Multiple candidates are returned without guessing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workplaceName": {"type": "string", "description": "사업장명 (필수)"},
                "businessNumber": {
                    "type": "string",
                    "description": "사업자등록번호 (하이픈 허용). 앞 6자리 필터.",
                },
                "sidoCode": {"type": "string"},
                "display": {"type": "integer"},
            },
            "required": ["workplaceName"],
        },
    },
]

nps_router = APIRouter(prefix="/nps", tags=["NPS"])


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
        f"Apply at {NPS_APPLY_URL} "
        "with 서비스 URL https://tunneling-service.onrender.com/"
    )


def normalize_bn_prefix(raw: str | None) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return ""
    if len(digits) < 6:
        raise RuntimeError(f"사업자등록번호는 최소 6자리여야 합니다: {raw}")
    return digits[:6]


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


def parse_nps_payload(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise RuntimeError("국민연금 API 빈 응답")

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
            raise RuntimeError(nps_fault_message(code, msg))
        return data

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"국민연금 API 응답 파싱 실패: {raw[:200]}") from exc

    fault = root.find(".//cmmMsgHeader") or root.find(".//returnAuthMsg")
    if fault is not None:
        if fault.tag == "returnAuthMsg":
            raise RuntimeError(nps_fault_message("", _text(fault)))
        code = _text(fault.find("returnReasonCode")) or _text(fault.find("returnAuthMsg"))
        msg = _text(fault.find("errMsg")) or _text(fault.find("returnAuthMsg"))
        raise RuntimeError(nps_fault_message(code, msg))

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


def nps_fault_message(code: str, msg: str) -> str:
    code = (code or "").strip()
    if code in {"12", "NO_OPENAPI_SERVICE_ERROR"} or "NO_OPENAPI_SERVICE" in (msg or ""):
        return (
            "DATA_GO_KR_API_KEY is not approved for 국민연금 가입 사업장 내역 (3046071). "
            f"활용신청: {NPS_APPLY_URL} — 서비스 URL https://tunneling-service.onrender.com/"
        )
    if code in {"30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}:
        return f"data.go.kr serviceKey is not registered. Apply at {NPS_APPLY_URL}"
    return f"국민연금 API 오류 ({code or 'unknown'}): {msg}".strip()


def unwrap_items(body: Any) -> list[dict]:
    if not isinstance(body, dict):
        return []
    items = body.get("items")
    if items is None or items == "":
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


def status_label(code: str) -> str:
    return {"1": "등록", "2": "탈퇴"}.get(code, code)


def form_label(code: str) -> str:
    return {"1": "법인", "2": "개인"}.get(code, code)


def normalize_row(row: dict) -> dict[str, Any]:
    status_code = first_value(row, "wkplJnngStcd", "wkpl_jnng_stcd")
    form_code = first_value(row, "wkplStylDvcd", "wkpl_styl_dvcd")
    return {
        "seq": first_value(row, "seq"),
        "workplaceName": first_value(row, "wkplNm", "wkpl_nm"),
        "businessNumberPrefix": first_value(row, "bzowrRgstNo", "bzowr_rgst_no"),
        "address": first_value(row, "wkplRoadNmDtlAddr", "wkpl_road_nm_dtl_addr"),
        "status": status_label(status_code),
        "statusCode": status_code,
        "form": form_label(form_code),
        "formCode": form_code,
        "dataMonth": first_value(row, "dataCrtYm", "data_crt_ym"),
        "subscriberCount": as_int(first_value(row, "jnngpCnt", "jnngp_cnt")),
        "monthlyNoticeAmount": as_int(first_value(row, "crrmmNtcAmt", "crrmm_ntc_amt")),
        "newJoiners": as_int(first_value(row, "nwAcqzrCnt", "nw_acqzr_cnt")),
        "leavers": as_int(first_value(row, "lssJnngpCnt", "lss_jnngp_cnt")),
        "industryCode": first_value(row, "wkplIntpCd", "wkpl_intp_cd"),
        "industryName": first_value(row, "vldtVlKrnNm", "vldt_vl_krn_nm"),
        "establishedDate": first_value(row, "adptDt", "adpt_dt"),
        "closedDate": first_value(row, "scsnDt", "scsn_dt"),
        "sidoCode": first_value(row, "ldongAddrMgplDgCd", "ldong_addr_mgpl_dg_cd"),
        "sigunguCode": first_value(row, "ldongAddrMgplSgguCd", "ldong_addr_mgpl_sggu_cd"),
        "emdCode": first_value(row, "ldongAddrMgplSgguEmdCd", "ldong_addr_mgpl_sggu_emd_cd"),
    }


def parse_page(data: dict[str, Any]) -> dict[str, Any]:
    envelope = data.get("response") if isinstance(data.get("response"), dict) else data
    header = envelope.get("header") or {}
    body = envelope.get("body") or {}
    result_code = str(header.get("resultCode") or "")
    result_msg = str(header.get("resultMsg") or "")
    if result_code and result_code not in {"00", "0"}:
        raise RuntimeError(nps_fault_message(result_code, result_msg))
    items = [normalize_row(row) for row in unwrap_items(body)]
    return {
        "page": as_int(body.get("pageNo")) or 1,
        "display": as_int(body.get("numOfRows")) or len(items),
        "totalCount": as_int(body.get("totalCount")) or len(items),
        "items": items,
    }


async def nps_get(url: str, extra: dict[str, Any]) -> dict[str, Any]:
    key = _service_key()
    if not key:
        raise missing_key_error()
    params: dict[str, Any] = {
        "serviceKey": key,
        "resultType": "json",
        "pageNo": extra.get("pageNo", 1),
        "numOfRows": extra.get("numOfRows", 10),
    }
    for key_name, value in extra.items():
        if key_name in {"pageNo", "numOfRows"}:
            continue
        if value not in (None, ""):
            params[key_name] = value
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, params=params)
    text = resp.text
    if resp.status_code >= 400:
        raise RuntimeError(f"apis.data.go.kr HTTP {resp.status_code}: {text[:200]}")
    try:
        payload = parse_nps_payload(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"국민연금 API 응답 파싱 실패: {text[:200]}") from exc
    return parse_page(payload)


def paging_args(args: dict[str, Any], default_rows: int = 10) -> dict[str, int]:
    return {
        "pageNo": clamp_page(args.get("page") or args.get("pageNo"), 1),
        "numOfRows": clamp_rows(args.get("display") or args.get("numOfRows"), default_rows),
    }


async def search_workplaces(args: dict[str, Any]) -> dict[str, Any]:
    extra = paging_args(args, 10)
    name = str(args.get("workplaceName") or args.get("wkplNm") or "").strip()
    bn = normalize_bn_prefix(args.get("businessNumber") or args.get("bzowrRgstNo") or "")
    if name:
        extra["wkplNm"] = name
    if bn:
        extra["bzowrRgstNo"] = bn
    sido = str(args.get("sidoCode") or args.get("ldongAddrMgplDgCd") or "").strip()
    sigungu = str(args.get("sigunguCode") or args.get("ldongAddrMgplSgguCd") or "").strip()
    emd = str(args.get("emdCode") or args.get("ldongAddrMgplSgguEmdCd") or "").strip()
    if sido:
        extra["ldongAddrMgplDgCd"] = sido
    if sigungu:
        extra["ldongAddrMgplSgguCd"] = sigungu
    if emd:
        extra["ldongAddrMgplSgguEmdCd"] = emd
    if not name and not bn and not sido:
        raise RuntimeError("workplaceName, businessNumber, or sidoCode is required")
    return await nps_get(NPS_SEARCH, extra)


async def fetch_by_seq(url: str, args: dict[str, Any]) -> dict[str, Any]:
    seq = str(args.get("seq") or "").strip()
    if not seq:
        raise RuntimeError("seq is required (from nps_search)")
    extra = paging_args(args, 20)
    extra["seq"] = seq
    return await nps_get(url, extra)


def workplace_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("workplaceName") or "").strip(),
        str(row.get("businessNumberPrefix") or "").strip(),
        str(row.get("address") or "").strip(),
    )


def latest_per_workplace(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in items:
        key = workplace_key(row)
        prev = latest.get(key)
        if prev is None or str(row.get("dataMonth") or "") > str(prev.get("dataMonth") or ""):
            latest[key] = row
    return sorted(latest.values(), key=lambda r: str(r.get("dataMonth") or ""), reverse=True)


async def lookup_workplace(args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("workplaceName") or "").strip()
    if not name:
        raise RuntimeError("workplaceName is required")
    search_args = {
        "workplaceName": name,
        "businessNumber": args.get("businessNumber"),
        "sidoCode": args.get("sidoCode"),
        "page": 1,
        "display": args.get("display") or 100,
    }
    search = await search_workplaces(search_args)
    candidates = latest_per_workplace(search["items"])
    result: dict[str, Any] = {
        "found": False,
        "selected": None,
        "detail": None,
        "trend": None,
        "candidateCount": len(candidates),
        "candidates": candidates,
        "searchTotalCount": search.get("totalCount"),
    }
    if len(candidates) != 1:
        if candidates:
            result["hint"] = (
                "Multiple workplaces matched. Pass businessNumber or call nps_detail/nps_trend with a seq."
            )
        else:
            result["hint"] = (
                "No open NPS workplace matched. Coverage is 3+ 가입자 법인 (개인은 2025.7 이후 10인 이상)."
            )
        return result

    selected = candidates[0]
    seq = str(selected.get("seq") or "")
    detail = await fetch_by_seq(NPS_DETAIL, {"seq": seq, "display": 20})
    trend = await fetch_by_seq(NPS_TREND, {"seq": seq, "display": 100})
    result.update(
        {
            "found": True,
            "selected": selected,
            "detail": detail,
            "trend": trend,
        }
    )
    return result


async def execute_tool(name: str, args: dict | None) -> Any:
    args = args or {}
    if name == "nps_search":
        return await search_workplaces(args)
    if name == "nps_detail":
        return await fetch_by_seq(NPS_DETAIL, args)
    if name == "nps_trend":
        return await fetch_by_seq(NPS_TREND, args)
    if name == "nps_lookup":
        return await lookup_workplace(args)
    raise RuntimeError(f"Unknown tool: {name}")


async def handle_nps_http(method: str, path: str, body: dict | None) -> JSONResponse:
    clean = path.strip("/")
    if method == "GET" and clean == "tools":
        return JSONResponse(NPS_TOOLS)
    if method == "POST" and clean == "tools/call":
        tool = (body or {}).get("tool")
        if not tool:
            return mcp_err('Missing "tool" parameter', 400)
        try:
            return mcp_ok(await execute_tool(str(tool), (body or {}).get("arguments") or {}))
        except Exception as exc:
            print(f"❌ NPS tool {tool}: {exc}")
            return mcp_err(str(exc), 500)
    return mcp_err("NPS MCP endpoint not found", 404)


@nps_router.get("/tools")
async def list_tools():
    if not _service_key():
        return mcp_err("DATA_GO_KR_API_KEY is not set on tunneling-service", 503)
    return JSONResponse(NPS_TOOLS)


@nps_router.post("/tools/call")
async def call_tool(request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key or not _egdesk_api_key_is_registered(api_key):
        return mcp_err("Missing or invalid X-Api-Key (EGDesk tunnel key required)", 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await handle_nps_http("POST", "tools/call", body)


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
        print(f"⚠️ NPS root API key lookup failed: {exc}")
        return False
    return False
