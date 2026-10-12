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


TUNNEL_GATEWAY_HOSTS = frozenset(
    {
        "tunneling-service.onrender.com",
        "egdesk.cloud",
        "www.egdesk.cloud",
    }
)


def normalize_hostname(host: str) -> str:
    return (host or "").lower().split(":")[0]


def is_tunnel_gateway_hostname(hostname: str) -> bool:
    h = normalize_hostname(hostname)
    if h in TUNNEL_GATEWAY_HOSTS:
        return True
    return h.endswith(".egdesk.cloud")


def extract_project_from_coding_path(path: str) -> str | None:
    """Extract project name from p/{project_name}/... (no leading slash)."""
    normalized = (path or "").lstrip("/")
    if normalized.startswith("p/"):
        parts = normalized.split("/", 2)
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return None


def extract_project_after_tunnel_strip(path: str, tunnel_id: str | None = None) -> str | None:
    """Project from path after optional /t/{tunnel_id}/ strip (custom-domain + tunnel URLs)."""
    normalized = strip_tunnel_path_prefix((path or "").lstrip("/"), tunnel_id)
    return extract_project_from_coding_path(normalized)


def resolve_custom_domain_forward_path(
    path: str,
    mapped_project: str | None,
    tunnel_id: str | None = None,
) -> tuple[str, str | None]:
    """
    Custom-domain forward path + project for access checks.

    When the browser uses a full tunnel URL on a custom host
    (e.g. solgemtoken.com/t/vicky-cha4/p/Sheetbot/api/...), honor /p/Sheetbot/
    instead of replacing it with domain_project_map (often a different default app).
    """
    mapped = (mapped_project or "").strip() or None
    explicit = extract_project_after_tunnel_strip(path, tunnel_id)

    if explicit and mapped and explicit.lower() != mapped.lower():
        normalized = strip_tunnel_path_prefix((path or "").lstrip("/"), tunnel_id)
        return normalized, explicit

    if mapped:
        return inject_custom_domain_project_path(path, mapped, tunnel_id), mapped

    if explicit:
        normalized = strip_tunnel_path_prefix((path or "").lstrip("/"), tunnel_id)
        return normalized, explicit

    return (path or "").lstrip("/"), None


def _canonical_host_sort_key(host: str) -> tuple[int, str]:
    is_www = 1 if host.startswith("www.") else 0
    bare = host[4:] if host.startswith("www.") else host
    return (is_www, bare)


def resolve_effective_project_name(
    path: str,
    request_host: str | None,
    domain_project_map: dict | None,
    project_override: str | None = None,
) -> str | None:
    if project_override and str(project_override).strip():
        return str(project_override).strip()
    if domain_project_map and isinstance(domain_project_map, dict):
        host = normalize_hostname(request_host or "")
        for map_host, proj in domain_project_map.items():
            if normalize_hostname(str(map_host)) == host:
                mapped = str(proj).strip()
                if mapped:
                    return mapped
    return extract_project_from_coding_path(path)


def preferred_canonical_origin_for_project(
    domain_project_map: dict | None,
    project: str | None,
) -> str | None:
    """Canonical https origin for domains mapped to this project (apex preferred over www)."""
    if not project or not domain_project_map or not isinstance(domain_project_map, dict):
        return None
    project_key = str(project).strip()
    if not project_key:
        return None
    project_lower = project_key.lower()
    hosts = [
        str(h).strip().lower()
        for h, mapped in domain_project_map.items()
        if str(h).strip() and str(mapped).strip().lower() == project_lower
    ]
    if not hosts:
        return None
    hosts.sort(key=_canonical_host_sort_key)
    return f"https://{hosts[0]}"


def is_www_alternate_of_canonical(request_host: str, canonical_origin: str) -> bool:
    try:
        canonical_host = normalize_hostname(canonical_origin.split("://", 1)[-1].split("/")[0])
        if canonical_host.startswith("www."):
            return False
        bare = canonical_host[4:] if canonical_host.startswith("www.") else canonical_host
        return normalize_hostname(request_host) == f"www.{bare}"
    except Exception:
        return False


def should_inject_canonical_redirect_headers(
    request_host: str,
    canonical_origin: str | None,
) -> bool:
    """
    Only hint Next.js to redirect for custom-domain alias hosts (e.g. www → apex).
    Never inject on shared tunnel gateway hosts (/t/.../p/... dev URLs).
    """
    if not canonical_origin:
        return False
    host = normalize_hostname(request_host)
    if is_tunnel_gateway_hostname(host):
        return False
    try:
        canonical_host = normalize_hostname(canonical_origin.split("://", 1)[-1].split("/")[0])
    except Exception:
        return False
    if host == canonical_host:
        return False
    return is_www_alternate_of_canonical(host, canonical_origin)


def inject_canonical_visitor_origin_headers(
    path: str,
    request_host: str,
    domain_project_map: dict | None,
    project_override: str | None = None,
) -> dict[str, str]:
    """
    Gateway source of truth for canonical visitor redirects (Next.js plugin only acts on these).

    1. Resolve project from custom Host → domain_project_map, else p/{name}/… in path.
    2. Canonical origin only from domains mapped to that project (never tunnel-wide).
    3. Emit redirect hints only for custom-host alias cases (www → apex), not on shared
       tunnel gateway hosts (/t/…/p/… preview URLs).
    """
    project = resolve_effective_project_name(
        path, request_host, domain_project_map, project_override
    )
    canonical = preferred_canonical_origin_for_project(domain_project_map, project)
    if not canonical:
        return {}
    if not should_inject_canonical_redirect_headers(request_host, canonical):
        return {}
    return {
        "x-egdesk-canonical-visitor-origin": canonical,
        "x-egdesk-canonical-redirect-enabled": "1",
    }


# Back-compat alias for tests / older imports
build_canonical_visitor_forward_headers = inject_canonical_visitor_origin_headers


def preferred_canonical_origin_from_domain_map(domain_project_map: dict | None) -> str | None:
    """
    Pick a canonical https origin from domain_project_map keys (apex preferred over www).
    Deprecated for per-request injection — use preferred_canonical_origin_for_project.
    """
    if not domain_project_map or not isinstance(domain_project_map, dict):
        return None
    hosts = [str(h).strip().lower() for h in domain_project_map.keys() if str(h).strip()]
    if not hosts:
        return None

    hosts.sort(key=_canonical_host_sort_key)
    chosen = hosts[0]
    return f"https://{chosen}"
