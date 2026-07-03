"""The passphrase-gated approval page for the OAuth ``/authorize`` step.

Registered as an *unauthenticated* custom route on the FastMCP app (via
``@mcp.custom_route``, which the SDK exempts from the resource-server auth
middleware precisely for authorization-flow pages). The user's browser lands here
after ``/authorize`` parks the request; the operator approves by entering the
bearer token as the passphrase. On success we mint the auth code and 302 back to
the client's ``redirect_uri``.

The gate matters because nginx makes ``/authorize`` publicly reachable — without
it, anyone who could load the URL would obtain a token and full DB access.
"""

from __future__ import annotations

import hmac
import html

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .provider import PostgresOAuthProvider

_EXPIRED = (
    "This authorization request has expired or is invalid. "
    "Start the connection again from your client."
)

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center;
  font: 15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  background: #f4f5f7; color: #1c1e21; }
.card { width: min(92vw, 420px); background: #fff; border: 1px solid #e3e5e8;
  border-radius: 12px; padding: 28px 26px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
h1 { font-size: 18px; margin: 0 0 4px; }
p.sub { margin: 0 0 20px; color: #61656b; font-size: 13px; }
label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
input[type=password] { width: 100%; padding: 10px 12px; font-size: 15px;
  border: 1px solid #cdd0d4; border-radius: 8px; background: #fff; color: inherit; }
input[type=password]:focus { outline: 2px solid #3b6ef5; border-color: #3b6ef5; }
button { margin-top: 18px; width: 100%; padding: 11px; font-size: 15px; font-weight: 600;
  color: #fff; background: #2b6cff; border: 0; border-radius: 8px; cursor: pointer; }
button:hover { background: #1f5be0; }
.err { margin: 0 0 16px; padding: 10px 12px; font-size: 13px; border-radius: 8px;
  background: #fdecec; color: #b3261e; border: 1px solid #f6cfcc; }
.foot { margin-top: 18px; font-size: 12px; color: #8a8f96; text-align: center; }
@media (prefers-color-scheme: dark) {
  body { background: #16181c; color: #e6e8eb; }
  .card { background: #1f2226; border-color: #2c3036; box-shadow: none; }
  p.sub { color: #9aa0a6; }
  input[type=password] { background: #14161a; border-color: #3a3f46; }
  .err { background: #3a1e1c; color: #f2b8b5; border-color: #5c2b28; }
  .foot { color: #6b7178; }
}
"""


def _page(*, rid: str = "", error: str = "", fatal: bool = False) -> str:
    err_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
    if fatal:
        form = ""
    else:
        form = (
            '<form method="post" action="/login">'
            f'<input type="hidden" name="rid" value="{html.escape(rid)}">'
            '<label for="passphrase">Authorization passphrase</label>'
            '<input type="password" id="passphrase" name="passphrase" '
            'autocomplete="off" autofocus required>'
            "<button type=\"submit\">Approve connection</button>"
            "</form>"
        )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>mcp-postgres — authorize</title>"
        f"<style>{_STYLE}</style></head><body><main class=card>"
        "<h1>Authorize connection</h1>"
        '<p class="sub">A client is requesting access to this mcp-postgres server. '
        "Enter the server passphrase to approve.</p>"
        f"{err_html}{form}"
        '<div class="foot">The passphrase is this server\'s bearer token.</div>'
        "</main></body></html>"
    )


def register_login_routes(
    mcp, provider: PostgresOAuthProvider, passphrase: str
) -> None:
    """Register the ``/login`` GET/POST route on the FastMCP server."""

    @mcp.custom_route("/login", methods=["GET", "POST"], include_in_schema=False)
    async def login(request: Request) -> Response:
        if request.method == "GET":
            rid = request.query_params.get("rid", "")
            if provider.pending_client(rid) is None:
                return HTMLResponse(_page(error=_EXPIRED, fatal=True), status_code=400)
            return HTMLResponse(_page(rid=rid))

        # POST: verify the passphrase, then mint the code and redirect back.
        form = await request.form()
        rid = str(form.get("rid", ""))
        supplied = str(form.get("passphrase", ""))
        if provider.pending_client(rid) is None:
            return HTMLResponse(_page(error=_EXPIRED, fatal=True), status_code=400)
        if not passphrase or not hmac.compare_digest(supplied, passphrase):
            return HTMLResponse(
                _page(rid=rid, error="Incorrect passphrase — try again."),
                status_code=401,
            )
        redirect_url = provider.complete_authorization(rid)
        if redirect_url is None:
            return HTMLResponse(_page(error=_EXPIRED, fatal=True), status_code=400)
        return RedirectResponse(url=redirect_url, status_code=302)
