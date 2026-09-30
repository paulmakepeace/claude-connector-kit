"""The login page the OAuth authorize step sends the browser to, rate limited."""

from __future__ import annotations

import html
import logging
import threading
import time
from urllib.parse import urlparse

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from claude_connector_kit.oauth import LoginRefused, Provider

log = logging.getLogger("claude_connector_kit")


class RateLimiter:
    """A tiny fixed-window limiter for the unauthenticated login page: at most
    `limit` attempts per key per `window` seconds. The internet can reach /login,
    so without this it is an open door to guess every password. Keys are swept
    when they expire (and capped) so a caller rotating the key part cannot grow
    the table without bound."""

    def __init__(self, limit: int = 10, window: int = 300, max_keys: int = 4096):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < self.window and len(self._hits) < self.max_keys:
            return
        self._last_sweep = now
        self._hits = {k: live for k, ts in self._hits.items()
                      if (live := [t for t in ts if now - t < self.window])}

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            self._sweep(now)
            # At the cap, a key never seen is refused rather than admitted, so a
            # spray across fresh keys cannot both grow the table and pass.
            if key not in self._hits and len(self._hits) >= self.max_keys:
                return False
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


def client_ip(request: Request) -> str:
    """The real client IP. Behind an nginx front door every request carries the
    proxy's own address as request.client, so trust the X-Real-IP the vhost sets;
    fall back to the socket peer for a direct or loopback call."""
    return (request.headers.get("x-real-ip")
            or (request.client.host if request.client else "?"))


def add_login(server: MCPServer, provider: Provider, *, what: str,
              refused: tuple[type[BaseException], ...] = (LoginRefused,),
              fixed_login: str | None = None) -> None:
    """Serve /login on `server`. `what` names the thing being opened, as in "Sign in
    to <what>". check_login raising one of `refused` is a wrong login or password;
    anything else is a failure of its own. With `fixed_login` the form asks only for
    the password and signs in as that login, for a connector with one person."""
    # Two windows: a total per-IP cap (bounds spray and check_login volume from one
    # source) and a tighter per-IP-and-login cap (bounds targeted guessing).
    by_ip = RateLimiter(limit=30, window=300)
    by_login = RateLimiter(limit=6, window=300)

    def relying_party(req: str) -> tuple[str, str]:
        """The client name and redirect host to show, so the person sees who they are
        authorizing and where the grant will go before they enter a password."""
        client = provider.pending_client(req)
        found = provider.pending(req)
        name = (client.client_name if client and client.client_name else "an application")
        redirect = str(found[1].redirect_uri) if found else ""
        host = urlparse(redirect).netloc or "an unknown location"
        return name, host

    def form(req: str, error: str = "") -> str:
        name, host = relying_party(req)
        note = f'<p class="err">{html.escape(error)}</p>' if error else ""
        login_field = "" if fixed_login else \
            '<label>Login<input name="login" autocomplete="username" autofocus></label>'
        focus = " autofocus" if fixed_login else ""
        return _page(what, f"""
          <h1>Sign in to {html.escape(what)}</h1>
          <p class="rp"><b>{html.escape(name)}</b> is asking to access {html.escape(what)} at
          <b>{html.escape(host)}</b>. Sign in only if you started this, and check that location
          is one you expect.</p>
          {note}
          <form method="post" action="/login">
            <input type="hidden" name="req" value="{html.escape(req)}">
            {login_field}
            <label>Password<input name="password" type="password" autocomplete="current-password"{focus}></label>
            <button type="submit">Sign in and allow</button>
          </form>
        """)

    @server.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request) -> Response:
        if request.method == "GET":
            req = request.query_params.get("req", "")
            if provider.pending(req) is None:
                return HTMLResponse(_page(what, "This sign-in link has expired. Start again from your client."),
                                    status_code=400)
            return HTMLResponse(form(req))
        data = await request.form()
        req = str(data.get("req", ""))
        login_name = fixed_login or str(data.get("login", "")).strip()
        password = str(data.get("password", ""))
        ip = client_ip(request)
        if not by_ip.allow(ip) or not by_login.allow(f"{ip}|{login_name}"):
            return HTMLResponse(_page(what, "Too many attempts. Wait a few minutes and start again."),
                                status_code=429)
        try:
            redirect = await anyio.to_thread.run_sync(provider.complete_login, req, login_name, password)
        except refused:
            return HTMLResponse(form(req, "That password was not accepted." if fixed_login
                                     else "That login or password was not accepted."), status_code=401)
        except Exception:
            log.warning("login failed for a non-credential reason", exc_info=True)
            return HTMLResponse(_page(what, "Sign-in failed. Start again from your client."), status_code=400)
        return RedirectResponse(redirect, status_code=302)


def _page(what: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(what)} sign-in</title>
    <style>
      body{{font:16px/1.5 system-ui,sans-serif;max-width:22rem;margin:4rem auto;padding:0 1rem}}
      label{{display:block;margin:1rem 0}} input{{width:100%;padding:.5rem;font:inherit}}
      button{{padding:.6rem 1.2rem;font:inherit}} .err{{color:#b00}}
      .rp{{background:#f4f4f4;padding:.75rem 1rem;border-radius:.4rem;font-size:.9rem}}
    </style></head><body>{body}</body></html>"""
