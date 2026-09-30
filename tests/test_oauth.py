import asyncio
import base64
import hashlib
import threading
import time

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.testclient import TestClient

from claude_connector_kit import LoginRefused, Provider, Store, add_login, auth_settings
from claude_connector_kit.login import RateLimiter
from claude_connector_kit.oauth import MAX_LOGIN_FAILURES
from claude_connector_kit.signin import sign_in

URL = "https://kit.example.test"
SCOPES = ["read"]


def run(coro):
    return asyncio.run(coro)


def make_provider(path):
    creds = iter(["1-aaaa", "1-bbbb", "1-cccc", "1-dddd"])

    def check_login(login, password):
        if password != "right":
            raise LoginRefused("bad password")
        return next(creds)

    store = Store(str(path))
    return Provider(store, URL, check_login, SCOPES), store


@pytest.fixture
def provider(tmp_path):
    return make_provider(tmp_path / "s.sqlite3")


@pytest.fixture
def client(provider):
    p, _ = provider
    info = OAuthClientInformationFull(client_id="c1", client_secret="s1",
                                      redirect_uris=[AnyUrl("https://client.test/cb")])
    run(p.register_client(info))
    return info


def params(challenge="chal"):
    return AuthorizationParams(state="xyz", scopes=SCOPES, code_challenge=challenge,
                               redirect_uri=AnyUrl("https://client.test/cb"),
                               redirect_uri_provided_explicitly=True, resource=None)


def signed_in(p, client):
    req = run(p.authorize(client, params())).split("req=")[1]
    code = p.complete_login(req, "paul@x.test", "right").split("code=")[1].split("&")[0]
    return run(p.exchange_authorization_code(client, run(p.load_authorization_code(client, code))))


def test_authorize_returns_the_login_page_url(provider, client):
    p, _ = provider
    assert run(p.authorize(client, params())).startswith(URL + "/login?req=")


def test_full_code_exchange_binds_the_subject_and_keeps_the_session(provider, client):
    p, store = provider
    req = run(p.authorize(client, params())).split("req=")[1]
    redirect = p.complete_login(req, "Paul@x.test", "right")
    assert redirect.startswith("https://client.test/cb?") and "state=xyz" in redirect
    code = redirect.split("code=")[1].split("&")[0]
    assert store.session("paul@x.test") == "1-aaaa"
    loaded = run(p.load_authorization_code(client, code))
    assert loaded is not None and loaded.subject == "paul@x.test"
    token = run(p.exchange_authorization_code(client, loaded))
    access = run(p.load_access_token(token.access_token))
    assert access is not None and access.subject == "paul@x.test"
    assert run(p.load_authorization_code(client, code)) is None   # single use


def test_a_bad_password_is_refused_and_mints_nothing(provider, client):
    p, store = provider
    req = run(p.authorize(client, params())).split("req=")[1]
    with pytest.raises(LoginRefused):
        p.complete_login(req, "paul@x.test", "wrong")
    assert store.session("paul@x.test") is None


def test_refresh_rotates_and_revoke_removes(provider, client):
    p, _ = provider
    token = signed_in(p, client)
    refresh = run(p.load_refresh_token(client, token.refresh_token))
    rotated = run(p.exchange_refresh_token(client, refresh, SCOPES))
    assert rotated.access_token != token.access_token
    assert run(p.load_refresh_token(client, token.refresh_token)) is None
    run(p.revoke_token(run(p.load_access_token(rotated.access_token))))
    assert run(p.load_access_token(rotated.access_token)) is None


def test_a_stale_login_request_cannot_be_completed(provider, client):
    p, store = provider
    req = run(p.authorize(client, params())).split("req=")[1]
    row = store.get("pending", req)
    row["expires_at"] = 0
    store.put("pending", req, row)
    with pytest.raises(Exception):
        p.complete_login(req, "paul@x.test", "right")


def test_repeated_wrong_passwords_burn_the_pending_request(provider, client):
    p, store = provider
    req = run(p.authorize(client, params())).split("req=")[1]
    for _ in range(MAX_LOGIN_FAILURES):
        with pytest.raises(LoginRefused):
            p.complete_login(req, "paul@x.test", "wrong")
    assert store.get("pending", req) is None
    with pytest.raises(Exception):
        p.complete_login(req, "paul@x.test", "right")


def test_revoke_ends_the_session(provider, client):
    p, store = provider
    token = signed_in(p, client)
    run(p.revoke_token(run(p.load_access_token(token.access_token))))
    assert store.session("paul@x.test") is None


def test_gc_sweeps_expired_rows(provider, client):
    p, store = provider
    req = run(p.authorize(client, params())).split("req=")[1]
    row = store.get("pending", req)
    row["expires_at"] = 0
    store.put("pending", req, row)
    store._last_gc = 0
    store.gc(interval=0)
    assert store.get("pending", req) is None


def test_ending_a_session_ends_its_grants_for_good(provider, client):
    p, store = provider
    token = signed_in(p, client)
    assert store.end_session("paul@x.test", "1-aaaa") is True
    fresh = signed_in(p, client)
    assert run(p.load_access_token(fresh.access_token)) is not None
    assert run(p.load_access_token(token.access_token)) is None
    assert run(p.load_refresh_token(client, token.refresh_token)) is None


def test_ending_a_refused_credential_keeps_a_fresh_sign_in(provider, client):
    p, store = provider
    signed_in(p, client)
    fresh = signed_in(p, client)
    assert store.end_session("paul@x.test", "1-aaaa") is False
    assert run(p.load_access_token(fresh.access_token)) is not None


def test_a_code_exchanged_after_its_session_ended_yields_no_working_grant(provider, client):
    p, store = provider
    req = run(p.authorize(client, params())).split("req=")[1]
    code = p.complete_login(req, "paul@x.test", "right").split("code=")[1].split("&")[0]
    assert store.end_session("paul@x.test") is True
    tok = run(p.exchange_authorization_code(client, run(p.load_authorization_code(client, code))))
    assert run(p.load_access_token(tok.access_token)) is None
    assert run(p.load_refresh_token(client, tok.refresh_token)) is None


def test_a_refresh_exchanged_after_its_session_ended_yields_no_working_grant(provider, client):
    # the session ends between the token handler's load and its exchange
    p, store = provider
    token = signed_in(p, client)
    loaded = run(p.load_refresh_token(client, token.refresh_token))
    assert store.end_session("paul@x.test", "1-aaaa") is True
    rotated = run(p.exchange_refresh_token(client, loaded, []))
    assert run(p.load_access_token(rotated.access_token)) is None
    assert run(p.load_refresh_token(client, rotated.refresh_token)) is None


def test_a_grant_seen_without_a_session_is_deleted_not_suspended(provider, client):
    p, store = provider
    token = signed_in(p, client)
    store.delete("sessions", "paul@x.test")   # the session gone, the grant rows left
    assert run(p.load_access_token(token.access_token)) is None
    assert run(p.load_refresh_token(client, token.refresh_token)) is None
    signed_in(p, client)
    assert run(p.load_access_token(token.access_token)) is None
    assert run(p.load_refresh_token(client, token.refresh_token)) is None


def test_revoking_one_grant_ends_the_others_that_share_its_session(provider, client):
    p, store = provider
    first = signed_in(p, client)
    second = signed_in(p, client)
    run(p.revoke_token(run(p.load_access_token(second.access_token))))
    signed_in(p, client)   # a later sign-in does not bring the first grant back
    assert run(p.load_refresh_token(client, first.refresh_token)) is None


def test_the_pending_client_is_shown_for_consent(provider, client):
    p, _ = provider
    req = run(p.authorize(client, params())).split("req=")[1]
    assert p.pending_client(req).client_id == client.client_id
    assert p.pending_client("nope") is None


def test_parallel_reads_of_the_session_are_right(provider, client):
    p, store = provider
    signed_in(p, client)
    stop = time.time() + 0.3
    errors = []

    def reader():
        while time.time() < stop:
            try:
                assert store.session("paul@x.test") == "1-aaaa"
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# --------------------------------------------------------------- over HTTP

VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


def app(tmp_path, fixed_login=None):
    p, store = make_provider(tmp_path / "s.sqlite3")
    server = MCPServer("kit", auth_server_provider=p, auth=auth_settings(URL, SCOPES))

    @server.tool()
    def ping() -> str:
        return "pong"
    add_login(server, p, what="the kit", fixed_login=fixed_login)
    return TestClient(server.streamable_http_app(transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False))), p


def register(http, auth_method="none"):
    reg = http.post("/register", json={
        "redirect_uris": ["https://client.test/cb"], "token_endpoint_auth_method": auth_method,
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert reg.status_code == 201, reg.text
    return OAuthClientInformationFull.model_validate(reg.json())


def test_the_login_page_signs_in_one_person_by_password_alone(tmp_path):
    http, p = app(tmp_path, fixed_login="owner")
    info = register(http)
    req = run(p.authorize(info, params(CHALLENGE))).split("req=")[1]
    page = http.get("/login", params={"req": req})
    assert page.status_code == 200 and 'name="login"' not in page.text and "the kit" in page.text
    wrong = http.post("/login", data={"req": req, "password": "wrong"}, follow_redirects=False)
    assert wrong.status_code == 401 and "password was not accepted" in wrong.text
    ok = http.post("/login", data={"req": req, "password": "right"}, follow_redirects=False)
    assert ok.status_code == 302 and ok.headers["location"].startswith("https://client.test/cb?")
    code = ok.headers["location"].split("code=")[1].split("&")[0]
    token = http.post("/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER,
                                      "redirect_uri": "https://client.test/cb", "client_id": info.client_id})
    assert token.status_code == 200, token.text
    assert run(p.load_access_token(token.json()["access_token"])).subject == "owner"


def test_an_expired_login_link_says_so(tmp_path):
    http, _ = app(tmp_path)
    got = http.get("/login", params={"req": "nope"})
    assert got.status_code == 400 and "expired" in got.text


def test_sign_in_gets_a_token_that_opens_mcp_and_a_wrong_password_does_not(tmp_path):
    http, _ = app(tmp_path, fixed_login="owner")
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    headers = {"accept": "application/json, text/event-stream"}
    with http:   # the lifespan starts the transport's task group
        http.follow_redirects = False
        assert http.post("/mcp", json=body, headers=headers).status_code == 401
        with pytest.raises(RuntimeError, match="login"):
            sign_in(URL, "wrong", http=http)
        token = sign_in(URL + "/mcp", "right", http=http)
        got = http.post("/mcp", json=body, headers={**headers, "authorization": "Bearer " + token})
        assert got.status_code == 200, got.text


@pytest.mark.parametrize("auth_method", ["none", "client_secret_basic", "client_secret_post"])
def test_every_registered_client_can_revoke_its_own_token(tmp_path, auth_method):
    http, p = app(tmp_path)
    info = register(http, auth_method)
    req = run(p.authorize(info, params(CHALLENGE))).split("req=")[1]
    code = p.complete_login(req, "paul@x.test", "right").split("code=")[1].split("&")[0]
    form = {"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER,
            "redirect_uri": "https://client.test/cb", "client_id": info.client_id}
    auth = None
    if auth_method == "client_secret_post":
        form["client_secret"] = info.client_secret
    elif auth_method == "client_secret_basic":
        auth = (info.client_id, info.client_secret)
    token = http.post("/token", data=form, auth=auth).json()
    form = {"token": token["refresh_token"], "client_id": info.client_id}
    if auth_method == "client_secret_post":
        form["client_secret"] = info.client_secret
    assert http.post("/revoke", data=form, auth=auth).status_code == 200
    assert p.store.get("refresh", token["refresh_token"]) is None
    assert p.store.session("paul@x.test") is None


def test_a_secret_client_still_cannot_revoke_without_its_secret(tmp_path):
    http, p = app(tmp_path)
    info = register(http, "client_secret_post")
    req = run(p.authorize(info, params(CHALLENGE))).split("req=")[1]
    code = p.complete_login(req, "paul@x.test", "right").split("code=")[1].split("&")[0]
    token = http.post("/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER,
        "redirect_uri": "https://client.test/cb", "client_id": info.client_id,
        "client_secret": info.client_secret}).json()
    got = http.post("/revoke", data={"token": token["refresh_token"], "client_id": info.client_id})
    assert got.status_code == 401
    assert p.store.get("refresh", token["refresh_token"]) is not None


def test_the_metadata_names_public_clients_for_token_and_revoke(tmp_path):
    http, _ = app(tmp_path)
    meta = http.get("/.well-known/oauth-authorization-server").json()
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    assert "none" in meta["revocation_endpoint_auth_methods_supported"]
    assert "client_secret_post" in meta["revocation_endpoint_auth_methods_supported"]


def test_the_login_page_cannot_be_framed(tmp_path):
    http, p = app(tmp_path, fixed_login="owner")
    info = register(http)
    req = run(p.authorize(info, params(CHALLENGE))).split("req=")[1]
    page = http.get("/login", params={"req": req})
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


def test_wrong_passwords_from_many_addresses_share_one_budget(tmp_path):
    http, p = app(tmp_path, fixed_login="owner")
    info = register(http)
    codes = []
    for n in range(31):   # each from its own address, so only the shared budget can stop them
        req = run(p.authorize(info, params(CHALLENGE))).split("req=")[1]
        got = http.post("/login", data={"req": req, "password": "wrong"}, headers={"x-real-ip": f"2001:db8::{n:x}"},
                        follow_redirects=False)
        codes.append(got.status_code)
    assert codes[:30] == [401] * 30 and codes[30] == 429
    req = run(p.authorize(info, params(CHALLENGE))).split("req=")[1]
    got = http.post("/login", data={"req": req, "password": "right"}, headers={"x-real-ip": "192.0.2.1"},
                    follow_redirects=False)
    assert got.status_code == 429   # spent for the hour: sign-in waits, grants already made work on


def test_with_many_logins_the_limits_are_per_address_and_per_address_and_login(tmp_path):
    http, p = app(tmp_path)
    info = register(http)

    def attempt(ip, login, password="wrong"):
        req = run(p.authorize(info, params(CHALLENGE))).split("req=")[1]
        return http.post("/login", data={"req": req, "login": login, "password": password},
                         headers={"x-real-ip": ip}, follow_redirects=False).status_code
    assert [attempt("192.0.2.1", "paul@x.test") for _ in range(7)] == [401] * 6 + [429]
    assert attempt("192.0.2.1", "sam@x.test") == 401   # another login from the same address
    assert attempt("192.0.2.2", "paul@x.test", "right") == 302   # no budget shared across addresses
    assert [attempt(f"2001:db8::{n:x}", "paul@x.test") for n in range(31)] == [401] * 31


def test_the_limiter_blocks_past_the_limit_then_recovers_after_the_window():
    rl = RateLimiter(limit=3, window=1)
    assert all(rl.allow("k") for _ in range(3))
    assert not rl.allow("k") and rl.blocked("k")
    time.sleep(1.05)
    assert not rl.blocked("k") and rl.allow("k")


def test_the_limiter_sweeps_expired_keys():
    rl = RateLimiter(limit=5, window=1)
    rl.allow("a")
    rl._hits["a"] = [time.time() - 10]
    rl._last_sweep = 0
    rl.allow("b")
    assert "a" not in rl._hits


def test_the_limiter_caps_its_table_against_a_spray_of_fresh_keys():
    rl = RateLimiter(limit=2, window=300, max_keys=10)
    admitted = sum(1 for i in range(100) if rl.allow(f"login-{i}"))
    assert len(rl._hits) <= 10 and admitted <= 10
