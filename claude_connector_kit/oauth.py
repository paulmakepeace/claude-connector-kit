"""A connector as its own OAuth 2.1 authorization server, over SQLite.

The MCP SDK supplies the endpoints (metadata, dynamic registration, authorize,
token, revoke, PKCE, the bearer check on /mcp); this module supplies what those
endpoints store and the one step they cannot know: who the person is. The
authorize step sends the browser to the connector's login page (login.py), the
page calls `check_login(login, password)`, and the credential that returns is
kept as the subject's session. A grant lives only as long as its subject's
session, so ending the session (a revoke, or the backend refusing the
credential) ends every grant that acts through it.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from mcp.server.auth import routes as auth_routes
from mcp.server.auth.handlers import revoke as revoke_handler
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken
from pydantic import AnyHttpUrl


# /register accepts a public client (token_endpoint_auth_method "none") and /token serves
# it on PKCE alone, but the SDK's /revoke form requires a client_secret field, which a
# public client has none of and a client_secret_basic client sends in the header instead.
# The client is authenticated before the form is read, so the field only has to be
# optional here, and the metadata names "none" so it describes what the server accepts.
class RevocationRequest(revoke_handler.RevocationRequest):
    client_secret: str | None = None


revoke_handler.RevocationRequest = RevocationRequest
_sdk_build_metadata = auth_routes.build_metadata


def build_metadata(*args: Any, **kwargs: Any) -> OAuthMetadata:
    metadata = _sdk_build_metadata(*args, **kwargs)
    for field in ("token_endpoint_auth_methods_supported", "revocation_endpoint_auth_methods_supported"):
        methods = getattr(metadata, field)
        if methods is not None and "none" not in methods:
            methods.append("none")
    return metadata


auth_routes.build_metadata = build_metadata

ACCESS_TTL = 60 * 60
REFRESH_TTL = 90 * 24 * 60 * 60
CODE_TTL = 10 * 60
LOGIN_TTL = 15 * 60
MAX_LOGIN_FAILURES = 5  # one pending request cannot drive more than this many guesses


class LoginRefused(Exception):
    """check_login's answer to a login or password that is not accepted."""


class Store:
    """One SQLite file, JSON rows. Small by construction: a handful of clients, one
    grant per device, one session per person. Tokens are kept as issued, not hashed:
    the file sits on the connector's own volume beside data the tokens open anyway.
    The chmod below covers the WAL files only when they exist at open; the volume's
    owner-only directory is what keeps them private."""

    # A sessions row may be a live backend credential, so the file and its WAL
    # sidecars can hold real secrets: keep it owner-only.
    TABLES = ("clients", "pending", "codes", "access", "refresh", "sessions")
    # Tables whose rows carry an expires_at and can be swept once past it.
    EXPIRING = ("pending", "codes", "access", "refresh")

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection serves the event loop and the tool worker threads. Two threads
        # running statements on it at once see empty reads of rows that exist, malformed
        # values and InterfaceErrors, so every use of it holds this lock.
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        for table in self.TABLES:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {table} (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        for suffix in ("", "-wal", "-shm"):
            with suppress(OSError):
                os.chmod(f"{path}{suffix}", 0o600)
        self._last_gc = 0

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(f"SELECT v FROM {table} WHERE k = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, table: str, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self.db.execute(f"INSERT OR REPLACE INTO {table} (k, v) VALUES (?, ?)",
                            (key, json.dumps(value)))

    def delete(self, table: str, key: str) -> None:
        with self._lock:
            self.db.execute(f"DELETE FROM {table} WHERE k = ?", (key,))

    def gc(self, interval: int = 300) -> None:
        """Sweep every expired row. Throttled so a burst of writes triggers it at most
        once per interval; without it the expiring tables grow without bound under
        anonymous /authorize and /register traffic."""
        now = int(time.time())
        if now - self._last_gc < interval:
            return
        self._last_gc = now
        with self._lock:
            for table in self.EXPIRING:
                self.db.execute(
                    f"DELETE FROM {table} WHERE json_extract(v, '$.expires_at') < ?", (now,))

    # Sessions: the credential check_login returned for a subject, replaced at every sign-in.
    def session(self, subject: str) -> str | None:
        row = self.get("sessions", subject.lower())
        return row["credential"] if row else None

    def set_session(self, subject: str, credential: str) -> None:
        self.put("sessions", subject.lower(), {"credential": credential, "at": int(time.time())})

    def end_session(self, subject: str, credential: str | None = None) -> bool:
        """Drop the subject's session and every grant that acts through it, and say
        whether a session was dropped. With `credential`, only while the session still
        holds it, so a late refusal of an old credential cannot end a fresh sign-in."""
        with self._lock:
            if credential is None:
                cur = self.db.execute("DELETE FROM sessions WHERE k = ?", (subject.lower(),))
            else:
                cur = self.db.execute(
                    "DELETE FROM sessions WHERE k = ? AND json_extract(v, '$.credential') = ?",
                    (subject.lower(), credential))
            if cur.rowcount == 0:
                return False
            for table in ("access", "refresh"):
                self.db.execute(f"DELETE FROM {table} WHERE json_extract(v, '$.subject') = ?",
                                (subject.lower(),))
            return True


def _now() -> int:
    return int(time.time())


class Provider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, store: Store, public_url: str, check_login: Callable[[str, str], str],
                 scopes: list[str]):
        """`check_login(login, password)` returns the session's credential, or raises
        LoginRefused for a login or password that is not accepted."""
        self.store = store
        self.public_url = public_url.rstrip("/")
        self.check_login = check_login
        self.scopes = list(scopes)

    # ------------------------------------------------------------- clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self.store.get("clients", client_id)
        return OAuthClientInformationFull.model_validate(row) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Open to any redirect URI, as a client's own is not known in advance; the login
        # page names the host a grant will go to before it takes a password. Client rows
        # are a few hundred bytes and are not swept.
        self.store.put("clients", client_info.client_id, client_info.model_dump(mode="json"))

    # ----------------------------------------------------------- authorize
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self.store.gc()
        req = secrets.token_urlsafe(24)
        self.store.put("pending", req, {
            "client_id": client.client_id,
            "params": params.model_dump(mode="json"),
            "expires_at": _now() + LOGIN_TTL,
            "failures": 0,
        })
        return f"{self.public_url}/login?req={req}"

    def pending(self, req: str) -> tuple[str, AuthorizationParams] | None:
        row = self.store.get("pending", req)
        if not row or row["expires_at"] < _now():
            return None
        return row["client_id"], AuthorizationParams.model_validate(row["params"])

    def pending_client(self, req: str) -> OAuthClientInformationFull | None:
        """The client behind a pending request, for the consent screen. None if the
        request is unknown, expired, or names a client that no longer exists."""
        found = self.pending(req)
        if found is None:
            return None
        row = self.store.get("clients", found[0])
        return OAuthClientInformationFull.model_validate(row) if row else None

    def complete_login(self, req: str, login: str, password: str) -> str:
        """The login form's POST: check the login, keep the session, mint the
        authorization code, and return where to send the browser. A wrong password
        counts against the request and burns it after a few tries, so one pending
        request is not an unbounded guessing oracle."""
        row = self.store.get("pending", req)
        if row is None or row["expires_at"] < _now():
            raise AuthorizeError("invalid_request", "this login request has expired; start again")
        client_id = row["client_id"]
        params = AuthorizationParams.model_validate(row["params"])
        try:
            credential = self.check_login(login, password)
        except Exception:
            row["failures"] = row.get("failures", 0) + 1
            if row["failures"] >= MAX_LOGIN_FAILURES:
                self.store.delete("pending", req)
            else:
                self.store.put("pending", req, row)
            raise
        self.store.set_session(login, credential)
        code = secrets.token_urlsafe(32)
        self.store.put("codes", code, AuthorizationCode(
            code=code, scopes=params.scopes or self.scopes, expires_at=_now() + CODE_TTL,
            client_id=client_id, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource, subject=login.lower(),
        ).model_dump(mode="json"))
        self.store.delete("pending", req)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        row = self.store.get("codes", authorization_code)
        if not row or row["client_id"] != client.client_id or row["expires_at"] < _now():
            return None
        return AuthorizationCode.model_validate(row)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        self.store.delete("codes", authorization_code.code)
        return self._issue(client.client_id, authorization_code.scopes,
                           authorization_code.subject, authorization_code.resource)

    # -------------------------------------------------------------- tokens
    def _issue(self, client_id: str, scopes: list[str], subject: str | None,
               resource: str | None) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self.store.put("access", access, AccessToken(
            token=access, client_id=client_id, scopes=scopes, expires_at=_now() + ACCESS_TTL,
            resource=resource, subject=subject).model_dump(mode="json"))
        self.store.put("refresh", refresh, RefreshToken(
            token=refresh, client_id=client_id, scopes=scopes, expires_at=_now() + REFRESH_TTL,
            resource=resource, subject=subject).model_dump(mode="json"))
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes),
                          refresh_token=refresh)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        row = self.store.get("refresh", refresh_token)
        if not row or row["client_id"] != client.client_id:
            return None
        if row.get("expires_at") and row["expires_at"] < _now():
            self.store.delete("refresh", refresh_token)
            return None
        if not self._has_session("refresh", refresh_token, row):
            return None
        return RefreshToken.model_validate(row)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        if any(s not in refresh_token.scopes for s in scopes):
            raise TokenError("invalid_scope")
        self.store.delete("refresh", refresh_token.token)
        return self._issue(client.client_id, scopes or refresh_token.scopes,
                           refresh_token.subject, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.get("access", token)
        if not row:
            return None
        if row.get("expires_at") and row["expires_at"] < _now():
            self.store.delete("access", token)
            return None
        if not self._has_session("access", token, row):
            return None
        return AccessToken.model_validate(row)

    def _has_session(self, table: str, key: str, grant: dict[str, Any]) -> bool:
        """A grant acts through its subject's session, so it lives only as long as
        that session does. Without one it is deleted, the bearer check answers 401, and
        the client signs in again, which stores a fresh session and a fresh grant."""
        subject = grant.get("subject")
        if subject and self.store.session(subject) is not None:
            return True
        self.store.delete(table, key)
        return False

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.delete("access", token.token)
        self.store.delete("refresh", token.token)
        # Revoking a grant ends the subject's session too, so a revoke actually ends
        # their access; every other grant of theirs acts through it and ends with it.
        if token.subject:
            self.store.end_session(token.subject)


def auth_settings(public_url: str, scopes: list[str]) -> AuthSettings:
    """The SDK's settings for a connector that is its own authorization server at
    `public_url`: open dynamic registration, which claude.ai's connector uses, and
    revocation."""
    return AuthSettings(
        issuer_url=AnyHttpUrl(public_url),
        resource_server_url=AnyHttpUrl(public_url),
        required_scopes=scopes,
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=scopes,
                                                               default_scopes=scopes),
        revocation_options=RevocationOptions(enabled=True),
        validate_token_resource=False,
    )
