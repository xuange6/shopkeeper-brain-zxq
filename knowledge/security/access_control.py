"""Signed principal context and storage-level ACL filter construction.

The query body is deliberately not allowed to declare its own tenant, roles, or
ACL grants.  An authenticated gateway can mint a short-lived HMAC token for the
API.  Retrieval nodes consume the verified internal context and apply the same
filter before vector/entity/chunk lookup.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import re
import time
from typing import Any, Iterable, Mapping, Sequence


ACCESS_TOKEN_AUDIENCE = "shopkeeper-brain"
DEFAULT_TENANT = "public"
VISIBILITY_VALUES = {"public", "tenant", "private"}
MAX_ACL_READERS = 128


class AccessContextError(ValueError):
    """Raised when an externally supplied principal context is not trustworthy."""


@dataclass(frozen=True)
class AccessContext:
    subject_id: str = "anonymous"
    tenant_id: str = DEFAULT_TENANT
    roles: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    authenticated: bool = False
    authn_method: str = "anonymous"

    @classmethod
    def public(cls, tenant_id: str = DEFAULT_TENANT) -> "AccessContext":
        return cls(tenant_id=_clean_scalar(tenant_id, DEFAULT_TENANT))

    @classmethod
    def from_state(cls, value: Any) -> "AccessContext":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls.public()
        return cls(
            subject_id=_clean_scalar(value.get("subject_id"), "anonymous"),
            tenant_id=_clean_scalar(value.get("tenant_id"), DEFAULT_TENANT),
            roles=_clean_values(value.get("roles")),
            groups=_clean_values(value.get("groups")),
            authenticated=bool(value.get("authenticated")),
            authn_method=_clean_scalar(value.get("authn_method"), "internal"),
        )

    def to_state(self) -> dict[str, Any]:
        return asdict(self)

    def reader_tokens(self) -> tuple[str, ...]:
        if not self.authenticated:
            return ()
        values = [f"subject:{self.subject_id}"]
        values.extend(f"role:{role}" for role in self.roles)
        values.extend(f"group:{group}" for group in self.groups)
        return tuple(dict.fromkeys(values))

    def audit_summary(self) -> dict[str, Any]:
        subject_hash = hashlib.sha256(self.subject_id.encode("utf-8")).hexdigest()[:12]
        return {
            "version": "retrieval-acl-v1",
            "tenant_id": self.tenant_id,
            "authenticated": self.authenticated,
            "subject_hash": subject_hash,
            "role_count": len(self.roles),
            "group_count": len(self.groups),
            "authn_method": self.authn_method,
        }


def scope_session_id(
    session_id: str,
    context: AccessContext | Mapping[str, Any] | None,
) -> str:
    """Derive a storage-only conversation key bound to tenant and subject."""

    raw_session_id = _clean_scalar(session_id)
    if not raw_session_id:
        raise AccessContextError("session id is required")
    principal = AccessContext.from_state(context)
    material = "\x1f".join(
        (principal.tenant_id, principal.subject_id, raw_session_id)
    ).encode("utf-8")
    return f"session_acl_v1_{hashlib.sha256(material).hexdigest()}"


def verify_access_context_token(
    token: str,
    secret: str,
    *,
    now: int | None = None,
) -> AccessContext:
    """Verify ``base64url(payload).base64url(hmac_sha256)`` and return context."""

    if len(secret.encode("utf-8")) < 32:
        raise AccessContextError("access context verifier is not securely configured")
    try:
        payload_part, signature_part = token.strip().split(".", 1)
    except ValueError as exc:
        raise AccessContextError("malformed access context token") from exc

    expected = hmac.new(
        secret.encode("utf-8"), payload_part.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        provided = _b64url_decode(signature_part)
    except (ValueError, UnicodeError) as exc:
        raise AccessContextError("malformed access context signature") from exc
    if not hmac.compare_digest(expected, provided):
        raise AccessContextError("invalid access context signature")

    try:
        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise AccessContextError("malformed access context payload") from exc
    if not isinstance(payload, dict):
        raise AccessContextError("access context payload must be an object")

    current = int(time.time() if now is None else now)
    try:
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AccessContextError("access context expiration is required") from exc
    if expires_at < current:
        raise AccessContextError("access context token expired")
    if expires_at > current + 3600:
        raise AccessContextError("access context lifetime exceeds one hour")
    if payload.get("aud") != ACCESS_TOKEN_AUDIENCE:
        raise AccessContextError("invalid access context audience")

    subject_id = _clean_scalar(payload.get("sub"))
    tenant_id = _clean_scalar(payload.get("tenant"))
    if not subject_id or not tenant_id:
        raise AccessContextError("access context subject and tenant are required")
    return AccessContext(
        subject_id=subject_id,
        tenant_id=tenant_id,
        roles=_clean_values(payload.get("roles")),
        groups=_clean_values(payload.get("groups")),
        authenticated=True,
        authn_method="hmac-sha256-v1",
    )


def sign_access_context_token(
    claims: Mapping[str, Any], secret: str
) -> str:
    """Create a token for trusted gateway integrations and deterministic tests."""

    if len(secret.encode("utf-8")) < 32:
        raise AccessContextError("access context signer requires at least 32 secret bytes")
    payload = dict(claims)
    payload.setdefault("aud", ACCESS_TOKEN_AUDIENCE)
    payload_part = _b64url_encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    signature = hmac.new(
        secret.encode("utf-8"), payload_part.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{payload_part}.{_b64url_encode(signature)}"


def normalize_access_metadata(
    *,
    tenant_id: str = DEFAULT_TENANT,
    visibility: str = "public",
    acl_readers: Iterable[str] | None = None,
) -> dict[str, Any]:
    tenant = _clean_scalar(tenant_id, DEFAULT_TENANT)
    normalized_visibility = _clean_scalar(visibility, "public").lower()
    if normalized_visibility not in VISIBILITY_VALUES:
        raise AccessContextError(f"unsupported visibility: {visibility}")
    readers = _clean_values(acl_readers)[:MAX_ACL_READERS]
    if normalized_visibility == "private" and not readers:
        raise AccessContextError("private content requires at least one ACL reader")
    return {
        "tenant_id": tenant,
        "visibility": normalized_visibility,
        "acl_readers": list(readers),
    }


def build_milvus_access_filter(context: AccessContext | Mapping[str, Any] | None) -> str:
    """Return a fail-closed Milvus predicate for one verified principal."""

    principal = AccessContext.from_state(context)
    tenant = json.dumps(principal.tenant_id, ensure_ascii=False)
    clauses = ['visibility == "public"']
    if principal.authenticated:
        clauses.append('visibility == "tenant"')
        tokens = principal.reader_tokens()
        if tokens:
            encoded = ", ".join(json.dumps(value, ensure_ascii=False) for value in tokens)
            clauses.append(
                f'(visibility == "private" and '
                f"array_contains_any(acl_readers, [{encoded}]))"
            )
    return f"tenant_id == {tenant} and ({' or '.join(clauses)})"


def combine_milvus_filters(*filters: str | None) -> str | None:
    active = [f"({value})" for value in filters if value and value.strip()]
    return " and ".join(active) if active else None


def build_milvus_item_filter(item_names: Iterable[str] | None) -> str | None:
    names = [str(name).strip() for name in (item_names or []) if str(name).strip()]
    if not names:
        return None
    encoded = ", ".join(json.dumps(name, ensure_ascii=False) for name in names)
    return f"item_name in [{encoded}]"


def _clean_scalar(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if len(text) > 128 or any(ord(character) < 32 for character in text):
        raise AccessContextError("invalid access context scalar")
    return text


def _clean_values(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence):
        raise AccessContextError("access context list field must be an array")
    cleaned = [_clean_scalar(value) for value in values]
    return tuple(dict.fromkeys(value for value in cleaned if value))[:MAX_ACL_READERS]


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid base64url encoding")
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64url encoding") from exc
    # Base64's unused trailing bits can otherwise admit multiple textual
    # encodings for the same signature bytes. Tokens must use the one canonical
    # unpadded representation produced by the signer.
    if _b64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url encoding")
    return decoded
