"""Custom-domain path helpers (no FastAPI imports — safe for unit tests)."""


def strip_tunnel_path_prefix(path: str, tunnel_id: str | None = None) -> str:
    """
    Strip /t/{tunnel_id}/ prefix from paths baked with tunnel basePath.
    Path arrives without a leading slash (middleware lstrip).
    """
    normalized = (path or "").lstrip("/")
    if not normalized.startswith("t/"):
        return normalized

    parts = normalized.split("/", 2)
    if len(parts) < 2:
        return normalized

    if tunnel_id and parts[1] != tunnel_id:
        return normalized

    return parts[2] if len(parts) > 2 else ""


def visitor_gateway_path(path: str, tunnel_id: str | None = None) -> str | None:
    """
    Path the gateway should handle itself for visitor OAuth.

    Custom-domain hosts already identify the tunnel, but clients sometimes copy the
    shared-host prefix and send /t/{id}/visitor-auth/callback/{pendingId}. Strip that
    (and an optional /p/{project}/) so the request is not forwarded to Next.js.
    """
    normalized = strip_tunnel_path_prefix((path or "").lstrip("/"), tunnel_id)
    if normalized.startswith("t/"):
        normalized = strip_tunnel_path_prefix(normalized, None)
    if normalized.startswith("p/"):
        parts = normalized.split("/", 2)
        normalized = parts[2] if len(parts) > 2 else ""

    if normalized == "visitor-auth/callback" or normalized.startswith("visitor-auth/callback/"):
        return normalized
    if normalized in ("visitor-auth/tools/call", "visitor-google/tools/call"):
        return normalized
    return None


def inject_custom_domain_project_path(path: str, project_name: str, tunnel_id: str | None = None) -> str:
    """
    Prefix a bare custom-domain path with p/{project}/ for the tunnel client.

    Rules (Step 7 — implementationREADME.md):
    - Do not double-prefix when path already targets this project.
    - Replace /p/{other}/... with /p/{project}/... (copied tunnel links on custom host).
    - Strip /t/{tunnel_id}/ when asset URLs carry tunnel basePath.
    """
    project = (project_name or "").strip()
    if not project:
        return (path or "").lstrip("/")

    normalized = strip_tunnel_path_prefix((path or "").lstrip("/"), tunnel_id)

    if normalized == f"p/{project}" or normalized.startswith(f"p/{project}/"):
        return normalized

    if normalized.startswith("p/"):
        parts = normalized.split("/", 2)
        rest = parts[2] if len(parts) > 2 else ""
        return f"p/{project}/{rest}" if rest else f"p/{project}"

    return f"p/{project}/{normalized}" if normalized else f"p/{project}"
