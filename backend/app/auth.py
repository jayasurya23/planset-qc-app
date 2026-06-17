"""Signed-in engineer identity from Azure Container Apps EasyAuth headers.

In production the Container Apps built-in auth (Microsoft Entra) sidecar sits in
front of the app and injects the user's identity as request headers — and, since
it strips any client-supplied copies, those headers are trustworthy. This module
turns them into a small ``{email, user_id, name}`` dict via the ``current_user``
FastAPI dependency.

Locally (uvicorn + Vite, no sidecar) the headers are absent, so we fall back to
the ``DEV_USER_EMAIL`` env var so development still works. The legacy free-text
``engineer_name`` form field remains as a last-resort display fallback handled by
the caller — it is NOT authoritative.
"""
from __future__ import annotations

import base64
import binascii
import json
import os

from fastapi import Header

# Claim types Entra/EasyAuth may use inside the base64 X-MS-CLIENT-PRINCIPAL blob.
_EMAIL_CLAIM_TYPES = (
    "preferred_username",
    "upn",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
    "email",
    "emails",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
)
_NAME_CLAIM_TYPES = (
    "name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
)
_OID_CLAIM_TYPES = (
    "http://schemas.microsoft.com/identity/claims/objectidentifier",
    "oid",
)


def decode_principal(b64: str | None) -> dict:
    """Decode the base64-JSON X-MS-CLIENT-PRINCIPAL blob; {} on anything off."""
    if not b64:
        return {}
    try:
        raw = base64.b64decode(b64)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return {}


def _claims_map(principal: dict) -> dict[str, str]:
    """Flatten the principal's claim list to {typ: first-val}."""
    out: dict[str, str] = {}
    claims = principal.get("claims")
    if isinstance(claims, list):
        for c in claims:
            if (
                isinstance(c, dict)
                and c.get("typ")
                and c.get("val") is not None
                and c["typ"] not in out
            ):
                out[c["typ"]] = str(c["val"])
    return out


def _first(claims: dict[str, str], types: tuple[str, ...]) -> str | None:
    for t in types:
        v = (claims.get(t) or "").strip()
        if v:
            return v
    return None


def resolve_identity(
    principal_name: str | None,
    principal_id: str | None,
    principal_blob: str | None,
) -> dict:
    """Pure resolver (unit-testable): headers -> {email, user_id, name}."""
    claims = _claims_map(decode_principal(principal_blob))
    email = (principal_name or "").strip() or _first(claims, _EMAIL_CLAIM_TYPES)
    if not email:
        email = (os.getenv("DEV_USER_EMAIL") or "").strip() or None
    user_id = (principal_id or "").strip() or _first(claims, _OID_CLAIM_TYPES)
    name = _first(claims, _NAME_CLAIM_TYPES) or email
    return {"email": email, "user_id": user_id, "name": name}


def current_user(
    x_ms_client_principal_name: str | None = Header(None),
    x_ms_client_principal_id: str | None = Header(None),
    x_ms_client_principal: str | None = Header(None),
) -> dict:
    """FastAPI dependency: the signed-in engineer (or dev fallback)."""
    return resolve_identity(
        x_ms_client_principal_name,
        x_ms_client_principal_id,
        x_ms_client_principal,
    )
