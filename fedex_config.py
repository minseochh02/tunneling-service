"""FedEx credentials — platform env defaults with per-request tenant override."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

_DEFAULT_SANDBOX = "https://apis-sandbox.fedex.com"
_PRODUCTION = "https://apis.fedex.com"


@dataclass(frozen=True)
class FedExCredentials:
    tenant_id: str
    client_id: str
    client_secret: str
    account_number: str
    use_production: bool
    api_base: str | None = None
    locale: str = "en_US"

    def cache_key(self) -> str:
        return f"{self.tenant_id}:{self.client_id}"

    def base_url(self) -> str:
        if self.api_base:
            return self.api_base.rstrip("/")
        return _PRODUCTION if self.use_production else _DEFAULT_SANDBOX


def _env_bool(name: str) -> bool:
    return (os.getenv(name) or "").lower() in ("1", "true", "yes")


def credentials_from_env(tenant_id: str = "default") -> FedExCredentials | None:
    client_id = (os.getenv("FEDEX_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("FEDEX_CLIENT_SECRET") or "").strip()
    account_number = (os.getenv("FEDEX_ACCOUNT_NUMBER") or "").strip()
    if not client_id or not client_secret:
        return None
    return FedExCredentials(
        tenant_id=tenant_id or "default",
        client_id=client_id,
        client_secret=client_secret,
        account_number=account_number,
        use_production=_env_bool("FEDEX_USE_PRODUCTION"),
        api_base=(os.getenv("FEDEX_API_BASE_URL") or "").strip() or None,
        locale=(os.getenv("FEDEX_LOCALE") or "en_US").strip() or "en_US",
    )


def credentials_from_payload(
    tenant_id: str,
    override: dict[str, Any] | None,
) -> FedExCredentials | None:
    """Resolve creds: request override first, else platform env for tenant_id."""
    tid = (tenant_id or "default").strip() or "default"
    if override:
        client_id = str(override.get("client_id") or override.get("clientId") or "").strip()
        client_secret = str(
            override.get("client_secret") or override.get("clientSecret") or ""
        ).strip()
        account_number = str(
            override.get("account_number") or override.get("accountNumber") or ""
        ).strip()
        if client_id and client_secret:
            use_prod = override.get("use_production")
            if use_prod is None:
                use_prod = override.get("useProduction")
            production = bool(use_prod) if use_prod is not None else _env_bool("FEDEX_USE_PRODUCTION")
            api_base = str(override.get("api_base") or override.get("apiBase") or "").strip() or None
            locale = str(override.get("locale") or os.getenv("FEDEX_LOCALE") or "en_US").strip()
            return FedExCredentials(
                tenant_id=tid,
                client_id=client_id,
                client_secret=client_secret,
                account_number=account_number,
                use_production=production,
                api_base=api_base,
                locale=locale or "en_US",
            )
    return credentials_from_env(tid)
