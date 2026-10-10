"""Local MCP server: lets Claude place LIVE IBKR orders through IB Gateway.

Unlike IBKR's official MCP (which only creates instructions you submit in the
app), orders here go straight to the exchange. Safety lives in `OrderDesk`:
two-step preview → place, limit orders only, whole shares, no margin, hard
per-order/daily caps shared with the hourly executor, fat-finger guard, HALT.
Options: cash-secured puts and long calls only (plus closing them).

stdio only — never expose this over the network.

    claude mcp add tradebot -s user -- uv run --directory /path/to/tradebot tradebot-mcp
"""

from __future__ import annotations

from datetime import datetime

from fastmcp import FastMCP

from .broker.base import OptionContract
from .broker.ibkr import IBKRBroker
from .config import Settings
from .gates import ET
from .order_desk import OrderDesk, OrderRejected, parse_expiry
from .store import Store

settings = Settings.load()
store = Store(settings.state_db)


def _broker() -> IBKRBroker:
    return IBKRBroker(settings.ib_host, settings.ib_port, settings.ib_mcp_client_id)


desk = OrderDesk(settings, store, _broker)
mcp = FastMCP("tradebot")


@mcp.tool()
async def get_account() -> dict:
    """Cash, buying power, positions and the live-trading limits in force."""
    b = _broker()
    await b.connect()
    try:
        positions = await b._positions_map()
        options = await b.get_option_positions()
        return {
            "accounts": b.ib.managedAccounts(),
            "port": settings.ib_port,
            "cash_usd": await b.get_cash(),
            "put_collateral_committed_usd": await b.get_committed_put_collateral(),
            "free_cash_usd": await b.get_free_cash(),
            "buying_power_usd": await b.get_buying_power(),
            "positions": positions,
            "option_positions": {c.label: qty for c, qty in options.items()},
            "limits": {
                "max_order_usd": settings.max_order_usd,
                "max_daily_buy_usd": settings.max_daily_usd,
                "max_limit_from_quote_pct": settings.max_limit_from_quote_pct,
                "max_put_collateral_usd": settings.max_put_collateral_usd,
                "min_long_call_dte": settings.min_long_call_dte,
                "max_option_limit_from_mid_pct": settings.max_option_limit_from_mid_pct,
                "margin": "never — buys and put collateral are capped by settled cash",
            },
            "halted": settings.halt_file.exists(),
        }
    finally:
        await b.disconnect()


@mcp.tool()
async def get_quote(ticker: str) -> dict:
    """Last price for a US stock/ETF (may be 15-min delayed without a data subscription)."""
    b = _broker()
    await b.connect()
    try:
        return {"ticker": ticker.upper(), "price": await b.get_quote(ticker)}
    finally:
        await b.disconnect()


@mcp.tool()
async def get_option_chain(
    ticker: str,
    expiry: str = "",
    min_dte: int = 0,
    max_dte: int = 1000,
    strike_range_pct: float = 20.0,
) -> dict:
    """Listed expiries (with days to expiry) and strikes within ±strike_range_pct of the
    stock price. Pass expiry (YYYY-MM-DD) to get the strikes actually listed for that
    expiry — without it, strikes are the union across all expiries."""
    b = _broker()
    await b.connect()
    try:
        spot = await b.get_quote(ticker)
        expiries, strikes = await b.get_option_chain(ticker)
        if expiry:
            try:
                expiry = parse_expiry(expiry)
            except OrderRejected as e:
                return {"rejected": str(e)}
            strikes = await b.get_option_strikes(ticker, expiry)
    finally:
        await b.disconnect()
    today = datetime.now(ET).date()
    lo, hi = spot * (1 - strike_range_pct / 100), spot * (1 + strike_range_pct / 100)
    exp = []
    for e in expiries:
        dte = (datetime.strptime(e, "%Y%m%d").date() - today).days
        if min_dte <= dte <= max_dte and (not expiry or e == expiry):
            exp.append({"expiry": e, "dte": dte})
    return {
        "ticker": ticker.upper(),
        "price": spot,
        "expiries": exp,
        "strikes": [k for k in strikes if lo <= k <= hi],
        "strikes_for": expiry or "all expiries (union) — pass expiry for exact strikes",
    }


@mcp.tool()
async def get_option_quote(ticker: str, expiry: str, strike: float, right: str) -> dict:
    """Bid/ask/last/close, delta, IV and underlying price for one option.
    expiry: YYYY-MM-DD or YYYYMMDD. right: C or P."""
    try:
        c = OptionContract(ticker.upper(), parse_expiry(expiry), float(strike),
                           right.strip().upper()[0])
    except OrderRejected as e:
        return {"rejected": str(e)}
    b = _broker()
    await b.connect()
    try:
        q = await b.get_option_quote(c)
    except ValueError as e:
        return {"error": str(e)}
    finally:
        await b.disconnect()
    reference, source = q.reference()
    return {
        "contract": c.label,
        "bid": q.bid, "ask": q.ask, "last": q.last, "close": q.close,
        "reference": reference, "reference_source": source,
        "delta": q.delta, "iv": q.iv, "underlying": q.underlying,
    }


@mcp.tool()
async def preview_option_order(
    ticker: str,
    expiry: str,
    strike: float,
    right: str,
    side: str,
    qty: int,
    limit_price: float,
    tif: str = "DAY",
) -> dict:
    """Step 1 of 2 for options. Validates a LIMIT option order; places nothing.

    Allowed: SELL P (cash-secured put, open), BUY C (call, open, min days-to-expiry
    applies), BUY P (close a short put), SELL C (close a long call). Naked calls and
    long puts are refused. qty is in contracts (100 shares each); limit_price is per
    share. expiry: YYYY-MM-DD or YYYYMMDD. tif: DAY|GTC.

    Show the preview (contract, strategy, premium, assignment cash / max loss,
    breakeven, quote) to the user and wait for an explicit "yes" before place_order.
    """
    try:
        return await desk.preview_option(
            ticker, expiry, strike, right, side, qty, limit_price, tif
        )
    except OrderRejected as e:
        return {"rejected": str(e)}
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def preview_order(
    ticker: str, side: str, qty: int, limit_price: float, tif: str = "DAY"
) -> dict:
    """Step 1 of 2. Validate a LIMIT order against all safety checks; places nothing.

    Show the returned preview (ticker, side, qty, limit, notional) to the user and
    wait for their explicit "yes" before calling place_order. side: BUY|SELL.
    tif: DAY|GTC. Whole shares only.
    """
    try:
        return await desk.preview(ticker, side, qty, limit_price, tif)
    except OrderRejected as e:
        return {"rejected": str(e)}


@mcp.tool()
async def place_order(preview_id: str) -> dict:
    """Step 2 of 2. Sends a REAL order with real money. Only call after the user
    has seen the preview_order / preview_option_order result and explicitly
    confirmed it in this turn.
    The preview_id is single-use and expires after 5 minutes.
    """
    try:
        return await desk.place(preview_id)
    except OrderRejected as e:
        return {"rejected": str(e)}


@mcp.tool()
async def list_open_orders() -> list[dict]:
    """All working orders on the account."""
    b = _broker()
    await b.connect()
    try:
        trades = await b.ib.reqAllOpenOrdersAsync()
        return [
            {
                "order_id": t.order.orderId,
                "perm_id": t.order.permId,
                "ticker": t.contract.symbol,
                "sec_type": t.contract.secType,
                "contract": t.contract.localSymbol or t.contract.symbol,
                "side": t.order.action,
                "qty": float(t.order.totalQuantity),
                "limit_price": t.order.lmtPrice,
                "tif": t.order.tif,
                "status": t.orderStatus.status,
                "filled": float(t.orderStatus.filled),
            }
            for t in trades
        ]
    finally:
        await b.disconnect()


@mcp.tool()
async def cancel_order(perm_id: int) -> dict:
    """Cancel a working order by its perm_id (from list_open_orders)."""
    b = _broker()
    await b.connect()
    try:
        trades = await b.ib.reqAllOpenOrdersAsync()
        trade = next((t for t in trades if t.order.permId == perm_id), None)
        if trade is None:
            return {"error": f"no open order with perm_id {perm_id}"}
        b.ib.cancelOrder(trade.order)
        return {"cancel_requested": perm_id, "ticker": trade.contract.symbol}
    finally:
        await b.disconnect()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
