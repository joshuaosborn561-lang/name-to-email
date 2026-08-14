"""Open OAuth 2.1 metadata + DCR so Cursor/Claude connectors can register.

No login: authorize immediately issues a code and token issues a bearer.
The API itself does not require the bearer.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from base64 import urlsafe_b64encode
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

router = APIRouter()

_codes: dict[str, dict[str, Any]] = {}
_clients: dict[str, dict[str, Any]] = {}


def public_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def _resource_url(request: Request, suffix: str = "") -> str:
    base = public_base(request)
    return f"{base}{suffix}" if suffix else base


def _as_metadata(request: Request) -> dict[str, Any]:
    base = public_base(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "revocation_endpoint": f"{base}/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp", "openid"],
        "service_documentation": f"{base}/docs",
    }


def _pr_metadata(request: Request, resource_path: str = "") -> dict[str, Any]:
    base = public_base(request)
    resource = f"{base}{resource_path}" if resource_path else base
    return {
        "resource": resource,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    }


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/{rest:path}")
@router.get("/.well-known/openid-configuration")
async def oauth_authorization_server(request: Request) -> dict[str, Any]:
    return _as_metadata(request)


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(request: Request) -> dict[str, Any]:
    return _pr_metadata(request, "")


@router.get("/.well-known/oauth-protected-resource/{rest:path}")
async def oauth_protected_resource_path(request: Request, rest: str) -> dict[str, Any]:
    path = f"/{rest}".rstrip("/")
    return _pr_metadata(request, path)


@router.post("/register")
async def register(request: Request) -> JSONResponse:
    body: dict[str, Any] = {}
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
        else:
            form = await request.form()
            body = {str(k): v for k, v in form.items()}
    except Exception:
        body = {}
    client_id = str(uuid.uuid4())
    record = {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": body.get("redirect_uris") or [],
        "token_endpoint_auth_method": "none",
        "grant_types": body.get("grant_types")
        or ["authorization_code", "refresh_token"],
        "response_types": body.get("response_types") or ["code"],
        "client_name": body.get("client_name") or "mcp-client",
        "scope": body.get("scope") or "mcp",
    }
    _clients[client_id] = record
    return JSONResponse(record, status_code=201)


@router.get("/authorize")
@router.post("/authorize")
async def authorize(
    request: Request,
    response_type: str = Query(default="code"),
    client_id: str = Query(default=""),
    redirect_uri: str = Query(default=""),
    state: str | None = Query(default=None),
    code_challenge: str | None = Query(default=None),
    code_challenge_method: str | None = Query(default="S256"),
    scope: str | None = Query(default=None),
) -> RedirectResponse:
    if request.method == "POST":
        form = await request.form()
        response_type = str(form.get("response_type") or response_type)
        client_id = str(form.get("client_id") or client_id)
        redirect_uri = str(form.get("redirect_uri") or redirect_uri)
        state = str(form.get("state")) if form.get("state") is not None else state
        code_challenge = str(form.get("code_challenge")) if form.get("code_challenge") else code_challenge
        code_challenge_method = str(form.get("code_challenge_method") or code_challenge_method)
    if response_type != "code":
        raise HTTPException(status_code=400, detail="response_type must be code")
    if not redirect_uri:
        raise HTTPException(status_code=400, detail="redirect_uri required")
    code = secrets.token_urlsafe(32)
    _codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method or "S256",
        "scope": scope or "mcp",
        "exp": time.time() + 600,
    }
    params = {"code": code}
    if state is not None:
        params["state"] = state
    sep = "&" if urlparse(redirect_uri).query else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


def _pkce_ok(verifier: str, challenge: str, method: str) -> bool:
    if not challenge:
        return True
    if method == "plain":
        return secrets.compare_digest(verifier, challenge)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(computed, challenge)


@router.post("/token")
async def token(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    data: dict[str, Any] = {}
    try:
        if "application/json" in content_type:
            parsed = await request.json()
            if isinstance(parsed, dict):
                data = parsed
        else:
            form = await request.form()
            data = {str(k): v for k, v in form.items()}
    except Exception:
        data = {}
    grant_type = str(data.get("grant_type") or "")
    code = str(data.get("code") or "")
    code_verifier = str(data.get("code_verifier") or "")
    refresh_token = str(data.get("refresh_token") or "")
    if grant_type in {"refresh_token", ""} and refresh_token:
        return {
            "access_token": secrets.token_urlsafe(32),
            "token_type": "Bearer",
            "expires_in": 86400 * 30,
            "refresh_token": secrets.token_urlsafe(32),
            "scope": "mcp",
        }
    if grant_type and grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="unsupported grant_type")
    record = _codes.pop(code, None)
    if record is None or record["exp"] < time.time():
        # Still issue a token so a flaky client can proceed; this server is open.
        return {
            "access_token": secrets.token_urlsafe(32),
            "token_type": "Bearer",
            "expires_in": 86400 * 30,
            "refresh_token": secrets.token_urlsafe(32),
            "scope": "mcp",
        }
    if record.get("code_challenge") and code_verifier:
        if not _pkce_ok(code_verifier, record["code_challenge"], record.get("code_challenge_method") or "S256"):
            raise HTTPException(status_code=400, detail="invalid code_verifier")
    return {
        "access_token": secrets.token_urlsafe(32),
        "token_type": "Bearer",
        "expires_in": 86400 * 30,
        "refresh_token": secrets.token_urlsafe(32),
        "scope": record.get("scope") or "mcp",
    }


@router.post("/revoke")
async def revoke() -> JSONResponse:
    return JSONResponse({}, status_code=200)
