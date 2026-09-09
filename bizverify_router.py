"""
Bizverify (국세청 사업자등록) — executed on the tunnel gateway.

All data.go.kr keys live here (DATA_GO_KR_API_KEY). Never on EGDesk desktop or customer apps.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

STATUS_URL = "https://api.odcloud.kr/api/nts-businessman/v1/status"
VALIDATE_URL = "https://api.odcloud.kr/api/nts-businessman/v1/validate"
MAX_BATCH = 100

BIZVERIFY_PURPOSE = (
    "Verify Korean business registration via 국세청 (data.go.kr 15081808). "
    "Use for 상호/사업자번호 checks, 휴·폐업, 과세유형. "
    "Do NOT use Bizinfo for company verify — Bizinfo is 지원사업 공고 only. "
    "Korean 상호 names are not unique; always use the 10-digit 사업자번호."
)

BIZVERIFY_TOOLS = [
    {
        "name": "bizverify_status",
        "description": (
            BIZVERIFY_PURPOSE
            + " Batch status lookup (max 100). Returns bNo, status, taxType, endDate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "businessNumbers": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "businessNumber": {"type": "string"},
            },
        },
    },
    {
        "name": "bizverify_validate",
        "description": BIZVERIFY_PURPOSE + " 진위확인 — businessNumber, openDate (YYYYMMDD), ownerName required.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "businessNumber": {"type": "string"},
                "openDate": {"type": "string"},
                "ownerName": {"type": "string"},
                "businessName": {"type": "string"},
                "corpNumber": {"type": "string"},
            },
            "required": ["businessNumber", "openDate", "ownerName"],
        },
    },
    {
        "name": "bizverify_invoice_ok",
        "description": BIZVERIFY_PURPOSE + " Convenience: 계속사업자 + 세금계산서 가능 과세유형.",
        "inputSchema": {
            "type": "object",
            "properties": {"businessNumber": {"type": "string"}},
            "required": ["businessNumber"],
        },
    },
]

bizverify_router = APIRouter(prefix="/bizverify", tags=["Bizverify"])


def _service_key() -> str:
    return (os.getenv("DATA_GO_KR_API_KEY") or os.getenv("DATA_GO_KR_SERVICE_KEY") or "").strip()


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


def normalize_bno(raw: str) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) != 10:
        raise RuntimeError(f"사업자등록번호는 10자리여야 합니다: {raw}")
    return digits


def normalize_bnos(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif value:
        items = [value]
    else:
        items = []
    out = [normalize_bno(v) for v in items]
    if len(out) > MAX_BATCH:
        raise RuntimeError(f"한 번에 최대 {MAX_BATCH}개 사업자번호만 조회할 수 있습니다.")
    return out


def normalize_status_row(raw: dict) -> dict:
    return {
        "bNo": str(raw.get("b_no") or ""),
        "status": str(raw.get("b_stt") or ""),
        "statusCode": str(raw.get("b_stt_cd") or ""),
        "taxType": str(raw.get("tax_type") or ""),
        "taxTypeCode": str(raw.get("tax_type_cd") or ""),
        "endDate": str(raw.get("end_dt") or ""),
        "invoiceApplyDate": str(raw.get("invoice_apply_dt") or ""),
    }


def invoice_eligible(row: dict) -> bool:
    active = row.get("statusCode") == "01" or "계속" in str(row.get("status") or "")
    tax = str(row.get("taxType") or "")
    tax_cd = str(row.get("taxTypeCode") or "")
    tax_ok = tax_cd == "01" or "일반과세" in tax or "간이과세" in tax
    return active and tax_ok


async def odcloud_post(url: str, body: dict) -> Any:
    key = _service_key()
    if not key:
        raise RuntimeError(
            "DATA_GO_KR_API_KEY is not set on tunneling-service. "
            "Apply at https://www.data.go.kr/data/15081808/openapi.do "
            "with 서비스 URL https://tunneling-service.onrender.com/"
        )
    params = {"serviceKey": key, "returnType": "JSON"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, params=params, json=body)
    text = resp.text
    if resp.status_code >= 400:
        raise RuntimeError(f"api.odcloud.kr HTTP {resp.status_code}: {text[:200]}")
    try:
        return resp.json()
    except Exception as exc:
        raise RuntimeError(f"응답 파싱 실패: {text[:200]}") from exc


async def fetch_status(business_numbers: list[str]) -> dict:
    data = await odcloud_post(STATUS_URL, {"b_no": business_numbers})
    if data.get("status_code") and data.get("status_code") != "OK":
        raise RuntimeError(f"국세청 상태조회 실패: {data.get('status_code')} {data.get('msg') or ''}".strip())
    items = [normalize_status_row(row or {}) for row in (data.get("data") or [])]
    return {
        "requestCount": int(data.get("request_cnt") or len(business_numbers)),
        "matchCount": int(data.get("match_cnt") or len(items)),
        "statusCode": str(data.get("status_code") or "OK"),
        "items": items,
    }


async def validate_registration(args: dict) -> Any:
    b_no = normalize_bno(args.get("businessNumber") or "")
    start_dt = re.sub(r"\D", "", str(args.get("openDate") or ""))
    p_nm = str(args.get("ownerName") or "").strip()
    if len(start_dt) != 8:
        raise RuntimeError("openDate는 YYYYMMDD 8자리여야 합니다.")
    if not p_nm:
        raise RuntimeError("ownerName(대표자명)이 필요합니다.")
    biz: dict[str, str] = {"b_no": b_no, "start_dt": start_dt, "p_nm": p_nm}
    if args.get("businessName"):
        biz["b_nm"] = str(args["businessName"]).strip()
    if args.get("corpNumber"):
        biz["corp_no"] = re.sub(r"\D", "", str(args["corpNumber"]))
    data = await odcloud_post(VALIDATE_URL, {"businesses": [biz]})
    if data.get("status_code") and data.get("status_code") != "OK":
        raise RuntimeError(f"국세청 진위확인 실패: {data.get('status_code')} {data.get('msg') or ''}".strip())
    return data


async def execute_tool(name: str, args: dict | None) -> Any:
    args = args or {}
    if name == "bizverify_status":
        bnos = normalize_bnos(args.get("businessNumbers") or args.get("businessNumber"))
        if not bnos:
            raise RuntimeError("businessNumbers or businessNumber is required")
        return await fetch_status(bnos)
    if name == "bizverify_validate":
        return await validate_registration(args)
    if name == "bizverify_invoice_ok":
        bn = str(args.get("businessNumber") or "").strip()
        if not bn:
            raise RuntimeError("businessNumber is required")
        result = await fetch_status([normalize_bno(bn)])
        row = result["items"][0] if result["items"] else None
        if not row:
            return {"businessNumber": normalize_bno(bn), "found": False, "invoiceOk": False}
        return {
            "businessNumber": row["bNo"],
            "found": True,
            "invoiceOk": invoice_eligible(row),
            "status": row["status"],
            "taxType": row["taxType"],
        }
    raise RuntimeError(f"Unknown tool: {name}")


async def handle_bizverify_http(method: str, path: str, body: dict | None) -> JSONResponse:
    clean = path.strip("/")
    if method == "GET" and clean == "tools":
        return JSONResponse(BIZVERIFY_TOOLS)
    if method == "POST" and clean == "tools/call":
        tool = (body or {}).get("tool")
        if not tool:
            return mcp_err('Missing "tool" parameter', 400)
        try:
            return mcp_ok(await execute_tool(str(tool), (body or {}).get("arguments") or {}))
        except Exception as exc:
            print(f"❌ Bizverify tool {tool}: {exc}")
            return mcp_err(str(exc), 500)
    return mcp_err("Bizverify MCP endpoint not found", 404)


@bizverify_router.get("/tools")
async def list_tools():
    if not _service_key():
        return mcp_err("DATA_GO_KR_API_KEY is not set on tunneling-service", 503)
    return JSONResponse(BIZVERIFY_TOOLS)


@bizverify_router.post("/tools/call")
async def call_tool(request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key or not _egdesk_api_key_is_registered(api_key):
        return mcp_err("Missing or invalid X-Api-Key (EGDesk tunnel key required)", 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await handle_bizverify_http("POST", "tools/call", body)


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
        print(f"⚠️ Bizverify root API key lookup failed: {exc}")
        return False
    return False
