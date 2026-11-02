"""
EGDesk Mobile Phase 2 — MO enrollment proxy on the tunnel gateway.

Phones call https://<gateway>/mobile/enroll/* ; this service forwards to Supabase Edge
Functions with server-side credentials (no Supabase URL or anon key in the APK).

Env (Render / tunneling-service):
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY  — used as Authorization for functions/v1/* (verify_jwt=false)

Optional:
  MOBILE_ENROLL_MO_WEBHOOK_SECRET — if set, POST /mobile/enroll/mo-webhook requires
    header x-mobile-mo-secret to match (in addition to Supabase MO_WEBHOOK_SECRET).
"""

from __future__ import annotations

import os
from typing import Mapping

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

mobile_enroll_router = APIRouter(prefix="/mobile/enroll", tags=["Mobile Enroll"])

FORWARD_HEADERS = (
    "x-mobile-enroll-dev-secret",
    "x-mobile-mo-secret",
)

ACTION_TO_FUNCTION = {
    "start": "mobile-enroll-start",
    "status": "mobile-enroll-status",
    "complete": "mobile-enroll-complete",
    "dev-simulate": "mobile-enroll-dev-simulate",
    "mo-webhook": "mobile-enroll-mo-webhook",
}


def _supabase_url() -> str:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("SUPABASE_URL is not configured")
    return url


def _supabase_service_key() -> str:
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not configured")
    return key


async def _proxy_enroll(request: Request, function_name: str) -> Response:
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "content-type, x-mobile-enroll-dev-secret, x-mobile-mo-secret",
            },
        )

    if request.method != "POST":
        return JSONResponse({"error": "method_not_allowed"}, status_code=405)

    body = await request.body()
    url = f"{_supabase_url()}/functions/v1/{function_name}"
    service_key = _supabase_service_key()

    headers: dict[str, str] = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": request.headers.get("content-type") or "application/json",
    }
    for name in FORWARD_HEADERS:
        value = request.headers.get(name)
        if value:
            headers[name] = value

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            upstream = await client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        print(f"❌ Mobile enroll proxy failed ({function_name}): {exc}")
        return JSONResponse({"error": "gateway_upstream_error"}, status_code=502)

    out_headers: Mapping[str, str] = {}
    content_type = upstream.headers.get("content-type")
    if content_type:
        out_headers = {"Content-Type": content_type}

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=dict(out_headers),
    )


def _route(action: str):
    function_name = ACTION_TO_FUNCTION[action]

    async def handler(request: Request) -> Response:
        if action == "mo-webhook":
            gateway_secret = (os.getenv("MOBILE_ENROLL_MO_WEBHOOK_SECRET") or "").strip()
            if gateway_secret:
                header = request.headers.get("x-mobile-mo-secret") or ""
                if header != gateway_secret:
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await _proxy_enroll(request, function_name)

    return handler


for _action in ACTION_TO_FUNCTION:
    mobile_enroll_router.add_api_route(
        f"/{_action}",
        _route(_action),
        methods=["POST", "OPTIONS"],
        name=f"mobile_enroll_{_action.replace('-', '_')}",
    )


async def dispatch_mobile_enroll_gateway(path: str, request: Request) -> Response | None:
    """
    Handle /mobile/enroll/* when the catch-all route wins matching on PRIMARY_DOMAINS.
    (Starlette can match `/{path:path}` alongside literal API routes.)
    """
    prefix = "mobile/enroll/"
    if not path.startswith(prefix):
        return None

    action = path[len(prefix) :].strip("/").split("/")[0]
    function_name = ACTION_TO_FUNCTION.get(action)
    if not function_name:
        return JSONResponse({"error": "not_found"}, status_code=404)

    if action == "mo-webhook":
        gateway_secret = (os.getenv("MOBILE_ENROLL_MO_WEBHOOK_SECRET") or "").strip()
        if gateway_secret:
            header = request.headers.get("x-mobile-mo-secret") or ""
            if header != gateway_secret:
                return JSONResponse({"error": "unauthorized"}, status_code=401)

    return await _proxy_enroll(request, function_name)
