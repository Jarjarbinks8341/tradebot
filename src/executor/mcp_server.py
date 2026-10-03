"""Local MCP server: lets Claude place LIVE IBKR orders through IB Gateway.

Unlike IBKR's official MCP (which only creates instructions you submit in the
app), orders here go straight to the exchange. Safety lives in `OrderDesk`:
two-step preview → place, limit orders only, whole shares, no margin, hard
per-order/daily caps shared with the hourly executor, fat-finger guard, HALT.

stdio only — never expose this over the network.

    claude mcp add tradebot -s user -- uv run --directory /path/to/tradebot tradebot-mcp
"""

from __future__ import annotations

from fastmcp import FastMCP

from .broker.ibkr import IBKRBroker
from .config import Settings
from .order_desk import OrderDesk, OrderRejected
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
        return {
            "accounts": b.ib.managedAccounts(),
            "port": settings.ib_port,
            "cash_usd": await b.get_cash(),
            "buying_power_usd": await b.get_buying_power(),
            "positions": positions,
            "limits": {
                "max_order_usd": settings.max_order_usd,
                "max_daily_buy_usd": settings.max_daily_usd,
                "max_limit_from_quote_pct": settings.max_limit_from_quote_pct,
                "margin": "never — buys are capped by settled cash",
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
    has seen the preview_order result and explicitly confirmed it in this turn.
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
