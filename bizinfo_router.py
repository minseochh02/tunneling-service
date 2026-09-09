"""
Bizinfo (기업마당 지원사업) — executed on the tunnel gateway.

사용신청 시스템URL is https://tunneling-service.onrender.com/
so 기업마당 must see requests from this host, not from each user's desktop.

Env: TUNNELING_API_KEY (기업마당 crtfcKey on tunneling-service)
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

BIZINFO_ENDPOINT = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"
MAX_PAGE_UNIT = 100
DEFAULT_PAGE_UNIT = 20

CATEGORY_CODES = {
    "finance": "01",
    "tech": "02",
    "talent": "03",
    "export": "04",
    "domestic": "05",
    "startup": "06",
    "management": "07",
    "other": "09",
}

BIZINFO_TOOLS = [
    {
        "name": "bizinfo_search",
        "description": "기업마당에서 정부 지원사업·공고를 조회합니다. query는 제목·개요 클라이언트 필터입니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(CATEGORY_CODES.keys())},
                "region": {"type": "string"},
                "hashtags": {"type": "string"},
                "query": {"type": "string"},
                "display": {"type": "number"},
                "page": {"type": "number"},
            },
        },
    },
    {
        "name": "bizinfo_open",
        "description": "현재 신청 가능한 지원사업만 반환하고 마감일 오름차순 정렬합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(CATEGORY_CODES.keys())},
                "region": {"type": "string"},
                "hashtags": {"type": "string"},
                "query": {"type": "string"},
                "display": {"type": "number"},
                "page": {"type": "number"},
            },
        },
    },
    {
        "name": "bizinfo_get",
        "description": "공고 ID(pblancId)로 한 건을 찾습니다.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
]

bizinfo_router = APIRouter(prefix="/bizinfo", tags=["Bizinfo"])


def _crtfc_key() -> str:
    return (os.getenv("TUNNELING_API_KEY") or os.getenv("BIZINFO_CRTFC_KEY") or "").strip()


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


def resolve_category_code(category: str | None) -> str | None:
    if not category or not str(category).strip():
        return None
    raw = str(category).strip().lower()
    if re.fullmatch(r"\d{2}", raw):
        return raw
    return CATEGORY_CODES.get(raw)


def strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def parse_period(raw: str | None) -> dict:
    period = (raw or "").strip()
    if not period:
        return {"period": "", "openFrom": None, "openUntil": None, "isOpen": None}
    match = re.search(r"(\d{8})\s*[~\-–]\s*(\d{8})", period)
    if not match:
        return {"period": period, "openFrom": None, "openUntil": None, "isOpen": None}
    open_from, open_until = match.group(1), match.group(2)
    today = date.today().strftime("%Y%m%d")
    return {
        "period": period,
        "openFrom": open_from,
        "openUntil": open_until,
        "isOpen": open_until >= today,
    }


def as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def normalize_announcement(raw: dict) -> dict:
    period = parse_period(raw.get("reqstDt") or raw.get("reqstBeginEndDe"))
    tags = [t.strip() for t in str(raw.get("hashTags") or "").split(",") if t.strip()]
    attachments = []
    if raw.get("flpthNm") or raw.get("fileNm"):
        attachments.append({"name": str(raw.get("fileNm") or ""), "url": str(raw.get("flpthNm") or "")})
    if raw.get("printFlpthNm") or raw.get("printFileNm"):
        attachments.append(
            {"name": str(raw.get("printFileNm") or ""), "url": str(raw.get("printFlpthNm") or "")}
        )
    return {
        "id": str(raw.get("pblancId") or raw.get("seq") or ""),
        "title": str(raw.get("pblancNm") or raw.get("title") or ""),
        "agency": str(raw.get("jrsdInsttNm") or raw.get("author") or ""),
        "executor": str(raw.get("excInsttNm") or ""),
        "category": str(raw.get("pldirSportRealmLclasCodeNm") or raw.get("lcategory") or ""),
        "target": str(raw.get("trgetNm") or ""),
        "period": period["period"],
        "openFrom": period["openFrom"],
        "openUntil": period["openUntil"],
        "isOpen": period["isOpen"],
        "summary": strip_html(str(raw.get("bsnsSumryCn") or raw.get("description") or "")),
        "applyUrl": str(raw.get("rceptEngnHmpgUrl") or ""),
        "postUrl": str(raw.get("pblancUrl") or raw.get("link") or ""),
        "tags": tags,
        "attachments": [a for a in attachments if a.get("url")],
    }


async def bizinfo_fetch(params: dict[str, str]) -> Any:
    key = _crtfc_key()
    if not key:
        raise RuntimeError(
            "TUNNELING_API_KEY is not set on tunneling-service. "
            "Apply at https://www.bizinfo.go.kr/apiDetail.do?id=bizinfoApi "
            "with 시스템URL https://tunneling-service.onrender.com/"
        )
    query = {"crtfcKey": key, "dataType": "json"}
    for k, v in params.items():
        if not v or (k == "searchCnt" and v in ("0", "")):
            continue
        query[k] = v
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(BIZINFO_ENDPOINT, params=query)
    if resp.status_code >= 400:
        raise RuntimeError(f"bizinfo.go.kr responded with HTTP {resp.status_code}")
    text = resp.text
    if text.lstrip().startswith("<html") or text.lstrip().startswith("<!"):
        raise RuntimeError("기업마당 API가 오류 페이지를 반환했습니다. crtfcKey와 사용신청을 확인하세요.")
    try:
        return resp.json()
    except Exception as exc:
        raise RuntimeError(f"응답 파싱 실패: {text[:200]}") from exc


def _extract_raw_items(data: Any) -> list:
    """Handle jsonArray as object {item:[...]} or as a direct item list."""
    if data is None:
        return []
    if isinstance(data, list):
        if not data:
            return []
        first = data[0]
        if isinstance(first, dict) and "item" in first:
            out: list = []
            for block in data:
                if isinstance(block, dict):
                    out.extend(as_list(block.get("item")))
            return out
        if isinstance(first, dict) and (
            "pblancId" in first or "seq" in first or "pblancNm" in first
        ):
            return [x for x in data if isinstance(x, dict)]
        return []
    if isinstance(data, dict):
        if "item" in data:
            return as_list(data.get("item"))
        if "jsonArray" in data:
            return _extract_raw_items(data.get("jsonArray"))
    return []


def parse_items(data: Any) -> tuple[int, list[dict]]:
    raw_items = _extract_raw_items(data)
    items = [normalize_announcement(item or {}) for item in raw_items]
    tot = 0
    if raw_items:
        try:
            tot = int(str(raw_items[0].get("totCnt") or len(items)))
        except ValueError:
            tot = len(items)
    return tot or len(items), items


def matches_query(item: dict, query: str | None) -> bool:
    if not query or not query.strip():
        return True
    hay = " ".join(
        [item.get("title") or "", item.get("summary") or "", item.get("agency") or "", item.get("target") or "", " ".join(item.get("tags") or [])]
    ).lower()
    return query.strip().lower() in hay


def build_params(args: dict) -> tuple[int, int, dict[str, str]]:
    page = max(1, int(args.get("page") or 1))
    display = min(MAX_PAGE_UNIT, max(1, int(args.get("display") or DEFAULT_PAGE_UNIT)))
    params: dict[str, str] = {"pageIndex": str(page), "pageUnit": str(display)}
    code = resolve_category_code(args.get("category"))
    if code:
        params["searchLclasId"] = code
    tags = ",".join(t for t in [args.get("hashtags"), args.get("region")] if t)
    if tags:
        params["hashtags"] = tags
    return page, display, params


async def search_announcements(args: dict) -> dict:
    page, display, params = build_params(args)
    total, items = parse_items(await bizinfo_fetch(params))
    query = args.get("query")
    if query:
        items = [i for i in items if matches_query(i, query)]
        total = len(items)
    return {"total": total, "page": page, "display": display, "items": items}


async def list_open(args: dict) -> dict:
    result = await search_announcements(args)
    items = [i for i in result["items"] if i.get("isOpen") is True]
    items.sort(key=lambda i: i.get("openUntil") or "99999999")
    return {**result, "total": len(items), "items": items}


async def get_announcement(announcement_id: str) -> dict | None:
    target = announcement_id.strip()
    if not target:
        raise RuntimeError("공고 id(pblancId)가 필요합니다.")
    for page in range(1, 6):
        result = await search_announcements({"page": page, "display": MAX_PAGE_UNIT})
        found = next((i for i in result["items"] if i.get("id") == target), None)
        if found:
            return found
        if len(result["items"]) < MAX_PAGE_UNIT:
            break
    return None


async def execute_tool(name: str, args: dict | None) -> Any:
    args = args or {}
    if name == "bizinfo_search":
        return await search_announcements(args)
    if name == "bizinfo_open":
        return await list_open(args)
    if name == "bizinfo_get":
        item_id = str(args.get("id") or "").strip()
        if not item_id:
            raise RuntimeError("공고 id(pblancId)가 필요합니다.")
        item = await get_announcement(item_id)
        if not item:
            return {"found": False, "id": item_id}
        return {"found": True, "item": item}
    raise RuntimeError(f"Unknown tool: {name}")


async def handle_bizinfo_http(method: str, path: str, body: dict | None) -> JSONResponse:
    """path is 'tools' or 'tools/call' (no leading slash)."""
    clean = path.strip("/")
    if method == "GET" and clean == "tools":
        return JSONResponse(BIZINFO_TOOLS)
    if method == "POST" and clean == "tools/call":
        tool = (body or {}).get("tool")
        if not tool:
            return mcp_err('Missing "tool" parameter', 400)
        try:
            data = await execute_tool(str(tool), (body or {}).get("arguments") or {})
            return mcp_ok(data)
        except Exception as exc:
            print(f"❌ Bizinfo tool {tool}: {exc}")
            return mcp_err(str(exc), 500)
    return mcp_err("Bizinfo MCP endpoint not found", 404)


@bizinfo_router.get("/tools")
async def list_tools():
    if not _crtfc_key():
        return mcp_err("TUNNELING_API_KEY is not set on tunneling-service", 503)
    return JSONResponse(BIZINFO_TOOLS)


def _egdesk_api_key_is_registered(api_key: str) -> bool:
    """True if this X-Api-Key belongs to any EGDesk tunnel in mcp_servers."""
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
        print(f"⚠️ Bizinfo root API key lookup failed: {exc}")
        return False
    return False


@bizinfo_router.post("/tools/call")
async def call_tool(request: Request):
    api_key = request.headers.get("X-Api-Key")
    if not api_key or not _egdesk_api_key_is_registered(api_key):
        return mcp_err("Missing or invalid X-Api-Key (EGDesk tunnel key required)", 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await handle_bizinfo_http("POST", "tools/call", body)
