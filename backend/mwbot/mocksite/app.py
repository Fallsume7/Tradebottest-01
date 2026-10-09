"""FastAPI server for the mock trading site (``python -m mwbot.mocksite``)."""

from __future__ import annotations

import html
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mwbot.mocksite.exchange import MockExchange

CSS = """
body{font-family:system-ui,sans-serif;margin:0;background:#f4f6f8;color:#1b2430}
header{background:#12304f;color:#fff;padding:12px 24px;display:flex;gap:24px;align-items:center}
header a{color:#cfe4ff;text-decoration:none} main{max-width:880px;margin:24px auto;padding:0 16px}
.card{background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:16px;box-shadow:0 1px 3px #0002}
table{width:100%;border-collapse:collapse} th,td{padding:6px 8px;border-bottom:1px solid #e3e8ee;text-align:left}
input,select,button{font:inherit;padding:6px 10px;margin:4px 0} button{cursor:pointer}
.error{background:#fde8e8;color:#8a1c1c;padding:10px;border-radius:6px;margin-bottom:12px}
.ok{background:#e6f6ec;color:#14532d;padding:10px;border-radius:6px;margin-bottom:12px}
.banner{background:#fff3cd;padding:6px 24px;font-size:13px}
"""


def page(title: str, body: str, nav: bool = True) -> HTMLResponse:
    links = (
        '<a href="/portfolio" id="nav-portfolio">Portfolio</a><a href="/trade" id="nav-trade">Trade</a>'
        '<a href="/orders" id="nav-orders">Orders</a><a href="/logout" id="nav-logout">Log out</a>'
        if nav
        else ""
    )
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body><div class='banner'>MOCK virtual trading site for software tests only - not a real venue</div>"
        f"<header><strong>MockTrade</strong>{links}</header><main>{body}</main></body></html>"
    )


def money(x: float) -> str:
    return f"${x:,.2f}"


def create_mock_app(exchange: MockExchange | None = None) -> FastAPI:
    ex = exchange or MockExchange()
    app = FastAPI(title="Mock virtual trading site", docs_url=None, redoc_url=None)
    app.state.exchange = ex

    def token(request: Request) -> str | None:
        return request.cookies.get("session")

    def guard(request: Request) -> Response | None:
        if not ex.valid(token(request)):
            return RedirectResponse("/login?expired=1", status_code=303)
        return None

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        return RedirectResponse("/portfolio" if ex.valid(token(request)) else "/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(expired: int = 0, error: str = "") -> Response:
        msg = ""
        if expired:
            msg = "<div class='error' id='login-error'>Your session has expired. Please log in again.</div>"
        elif error:
            msg = f"<div class='error' id='login-error'>{html.escape(error)}</div>"
        return page(
            "Log in",
            f"""<div class='card'><h2>Log in</h2>{msg}
            <form method='post' action='/login'>
            <div><label>Username <input id='username' name='username' autocomplete='off'></label></div>
            <div><label>Password <input id='password' name='password' type='password'></label></div>
            <button id='login-btn' type='submit'>Log in</button></form></div>""",
            nav=False,
        )

    @app.post("/login")
    def login(username: str = Form(""), password: str = Form("")) -> Response:
        tok = ex.login(username, password)
        if tok is None:
            return RedirectResponse("/login?error=Invalid+username+or+password", status_code=303)
        r = RedirectResponse("/portfolio", status_code=303)
        r.set_cookie("session", tok, httponly=True, samesite="lax")
        return r

    @app.get("/logout")
    def logout(request: Request) -> Response:
        ex.logout(token(request))
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie("session")
        return r

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio(request: Request) -> Response:
        if (g := guard(request)) is not None:
            return g
        snap = ex.snapshot()
        rows = ""
        for sym, h in sorted(ex.holdings.items()):
            avg = h["cost"] / h["qty"]
            rows += (
                f"<tr class='holding' data-symbol='{sym}' data-qty='{int(h['qty'])}' data-avg='{avg:.4f}'>"
                f"<td>{sym}</td><td class='qty'>{int(h['qty'])}</td><td>{money(avg)}</td>"
                f"<td>{money(ex.prices[sym])}</td><td>{money(h['qty'] * ex.prices[sym])}</td></tr>"
            )
        if not rows:
            rows = "<tr><td colspan='5' id='no-holdings'>You have no holdings.</td></tr>"
        return page(
            "Portfolio",
            f"""<div class='card'><h2>Portfolio</h2>
            <p>Cash: <strong id='cash-balance' data-value='{snap["cash"]:.2f}'>{money(snap["cash"])}</strong></p>
            <p>Buying power: <strong id='buying-power' data-value='{snap["buying_power"]:.2f}'>{money(snap["buying_power"])}</strong></p>
            <p>Portfolio value: <strong id='portfolio-value'>{money(snap["total"])}</strong></p></div>
            <div class='card'><table id='holdings'><thead><tr><th>Symbol</th><th>Shares</th><th>Avg cost</th><th>Last</th><th>Value</th></tr></thead>
            <tbody>{rows}</tbody></table></div>""",
        )

    @app.get("/trade", response_class=HTMLResponse)
    def trade(request: Request, symbol: str = "", error: str = "") -> Response:
        if (g := guard(request)) is not None:
            return g
        err = f"<div class='error' id='error-banner'>{html.escape(error)}</div>" if error else ""
        quote_html = ""
        if symbol:
            px = ex.quote(symbol)
            if px is None:
                err = f"<div class='error' id='error-banner'>Unknown ticker {html.escape(symbol.upper())}.</div>"
            else:
                sym = symbol.upper()
                quote_html = f"""<div class='card' id='quote-card'><h3>{sym}</h3>
                  <p>Last price: <strong id='quote-price' data-value='{px:.4f}'>{money(px)}</strong></p>
                  <form method='post' action='/trade/preview'>
                  <input type='hidden' name='symbol' value='{sym}'>
                  <div><label>Action <select id='side' name='side'><option value='buy'>Buy</option><option value='sell'>Sell</option></select></label></div>
                  <div><label>Shares <input id='quantity' name='quantity' type='number' min='1' value='1'></label></div>
                  <div><label>Order type <select id='order-type' name='order_type'><option value='market'>Market</option><option value='limit'>Limit</option></select></label></div>
                  <div><label>Limit price <input id='limit-price' name='limit_price' type='number' step='0.01'></label></div>
                  <button id='preview-btn' type='submit'>Preview order</button></form></div>"""
        return page(
            "Trade",
            f"""<div class='card'><h2>Trade</h2>{err}
            <form method='get' action='/trade'><label>Ticker <input id='symbol-input' name='symbol' value='{html.escape(symbol)}' autocomplete='off'></label>
            <button id='search-btn' type='submit'>Search</button></form></div>{quote_html}""",
        )

    @app.post("/trade/preview")
    def preview(
        request: Request,
        symbol: str = Form(""),
        side: str = Form("buy"),
        quantity: int = Form(0),
        order_type: str = Form("market"),
        limit_price: str = Form(""),
    ) -> Response:
        if (g := guard(request)) is not None:
            return g
        lp = float(limit_price) if limit_price.strip() else None
        err = ex.validate(symbol, side, quantity, order_type, lp)
        if err:
            return RedirectResponse(
                f"/trade?symbol={quote_plus(symbol)}&error={quote_plus(err)}", status_code=303
            )
        px = ex.quote(symbol) or 0.0
        est = quantity * (lp if order_type == "limit" and lp else px)
        return page(
            "Confirm order",
            f"""<div class='card' id='confirm-card'><h2>Confirm your order</h2>
            <p id='confirm-summary'>{side.upper()} {quantity} {symbol.upper()} ({order_type}{f" @ {lp:.2f}" if order_type == "limit" and lp else ""})</p>
            <p>Estimated {"cost" if side == "buy" else "proceeds"}: <strong id='confirm-estimate' data-value='{est:.2f}'>{money(est)}</strong></p>
            <form method='post' action='/trade/confirm'>
            <input type='hidden' name='symbol' value='{symbol.upper()}'><input type='hidden' name='side' value='{side}'>
            <input type='hidden' name='quantity' value='{quantity}'><input type='hidden' name='order_type' value='{order_type}'>
            <input type='hidden' name='limit_price' value='{limit_price}'>
            <button id='confirm-btn' type='submit'>Place order</button>
            <a href='/trade?symbol={symbol.upper()}' id='cancel-btn'>Cancel</a></form></div>""",
        )

    @app.post("/trade/confirm")
    def confirm(
        request: Request,
        symbol: str = Form(""),
        side: str = Form("buy"),
        quantity: int = Form(0),
        order_type: str = Form("market"),
        limit_price: str = Form(""),
    ) -> Response:
        if (g := guard(request)) is not None:
            return g
        lp = float(limit_price) if limit_price.strip() else None
        o = ex.place(symbol, side, quantity, order_type, lp)
        return RedirectResponse(f"/orders?placed={o.id}", status_code=303)

    @app.get("/orders", response_class=HTMLResponse)
    def orders(request: Request, placed: str = "") -> Response:
        if (g := guard(request)) is not None:
            return g
        ex.snapshot()  # advance matching
        banner = ""
        if placed and placed in ex.orders:
            o = ex.orders[placed]
            if o.status == "rejected":
                banner = f"<div class='error' id='order-error' data-order-id='{o.id}'>Order {o.id} was rejected: {html.escape(o.message)}</div>"
            else:
                banner = f"<div class='ok' id='order-confirmation' data-order-id='{o.id}'>Order {o.id} received ({o.status}).</div>"

        def row(o: object, pending: bool) -> str:
            from mwbot.mocksite.exchange import MockOrder

            assert isinstance(o, MockOrder)
            cancel = (
                (
                    f"<form method='post' action='/orders/{o.id}/cancel' style='display:inline'>"
                    f"<button class='cancel-btn' type='submit'>Cancel</button></form>"
                )
                if pending
                else ""
            )
            return (
                f"<tr class='order' data-order-id='{o.id}' data-status='{o.status}' data-symbol='{o.symbol}' "
                f"data-side='{o.side}' data-qty='{o.quantity}' data-filled='{o.filled_quantity}' "
                f"data-price='{o.fill_price if o.fill_price is not None else ''}'>"
                f"<td>{o.id}</td><td>{o.side.upper()}</td><td>{o.symbol}</td><td>{o.quantity}</td><td>{o.order_type}</td>"
                f"<td class='status'>{o.status}</td><td>{html.escape(o.message)}</td><td>{cancel}</td></tr>"
            )

        allo = sorted(ex.orders.values(), key=lambda x: x.id, reverse=True)
        pend = (
            "".join(row(o, True) for o in allo if o.status == "pending")
            or "<tr><td colspan='8'>No pending orders</td></tr>"
        )
        done = (
            "".join(row(o, False) for o in allo if o.status != "pending")
            or "<tr><td colspan='8'>No completed orders</td></tr>"
        )
        head = "<thead><tr><th>ID</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Type</th><th>Status</th><th>Note</th><th></th></tr></thead>"
        return page(
            "Orders",
            f"""{banner}<div class='card'><h2>Pending orders</h2><table id='pending-orders'>{head}<tbody>{pend}</tbody></table></div>
            <div class='card'><h2>Completed orders</h2><table id='completed-orders'>{head}<tbody>{done}</tbody></table></div>""",
        )

    @app.post("/orders/{order_id}/cancel")
    def cancel(request: Request, order_id: str) -> Response:
        if (g := guard(request)) is not None:
            return g
        ex.cancel(order_id)
        return RedirectResponse("/orders", status_code=303)

    # ---- test-control endpoints (not part of the "site")
    @app.post("/admin/config")
    async def admin_config(request: Request) -> JSONResponse:
        body = await request.json()
        c = ex.config
        if "exec_delay_seconds" in body:
            c.exec_delay_seconds = float(body["exec_delay_seconds"])
        if "fail_next_order" in body:
            c.fail_next_order = bool(body["fail_next_order"])
        if "market_open" in body:
            c.market_open = bool(body["market_open"])
        if "reject_symbols" in body:
            c.reject_symbols = {s.upper() for s in body["reject_symbols"]}
        if "expire_session_after_orders" in body:
            c.expire_session_after_orders = body["expire_session_after_orders"]
        if body.get("expire_sessions"):
            ex.expire_all_sessions()
        return JSONResponse({"ok": True})

    @app.post("/admin/prices")
    async def admin_prices(request: Request) -> JSONResponse:
        ex.set_prices(await request.json())
        return JSONResponse({"ok": True})

    @app.post("/admin/reset")
    def admin_reset() -> JSONResponse:
        ex.reset()
        return JSONResponse({"ok": True})

    @app.get("/admin/state")
    def admin_state() -> JSONResponse:
        return JSONResponse(
            {
                "snapshot": ex.snapshot(),
                "holdings": ex.holdings,
                "orders": {k: v.__dict__ for k, v in ex.orders.items()},
            }
        )

    return app
