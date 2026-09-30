"""Sign a script in to a connector the way a Claude client does, without a browser.

A smoke test or a cron job needs a token from the same OAuth server a person
signs in through. `sign_in` registers a public client, runs /authorize with
PKCE, posts the password to the connector's own login page (login.py), and
trades the code for a token. It never revokes: a revoke ends the person's
session, and with it every other client's grant.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urljoin, urlparse

import httpx2

REDIRECT = "http://localhost/signed-in"


def sign_in(url: str, password: str, login: str | None = None, http: httpx2.Client | None = None) -> str:
    """An access token for the connector at `url` (its origin, or its /mcp URL). `login` is for a
    connector whose login page asks for one. Raises RuntimeError naming the step that failed."""
    base = "{0.scheme}://{0.netloc}".format(urlparse(url))
    own = http is None
    http = http or httpx2.Client(follow_redirects=False, timeout=30)
    try:
        reg = http.post(urljoin(base, "/register"), json={
            "client_name": "claude-connector-kit sign_in", "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"]})
        _expect(reg, 201, "register")
        client_id = reg.json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(8)
        auth = http.get(urljoin(base, "/authorize"), params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        _expect(auth, 302, "authorize")
        req = parse_qs(urlparse(auth.headers["location"]).query)["req"][0]
        form = {"req": req, "password": password, **({"login": login} if login else {})}
        signed = http.post(urljoin(base, "/login"), data=form)
        _expect(signed, 302, "login (a wrong password answers 401)")
        back = parse_qs(urlparse(signed.headers["location"]).query)
        if back.get("state") != [state]:
            raise RuntimeError("sign-in: the login page answered with another request's state")
        token = http.post(urljoin(base, "/token"), data={
            "grant_type": "authorization_code", "code": back["code"][0], "code_verifier": verifier,
            "redirect_uri": REDIRECT, "client_id": client_id})
        _expect(token, 200, "token")
        return token.json()["access_token"]
    finally:
        if own:
            http.close()


def _expect(response: httpx2.Response, status: int, step: str) -> None:
    if response.status_code != status:
        raise RuntimeError(f"sign-in: {step} answered {response.status_code}: {response.text[:200]}")
