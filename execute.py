"""Provedení schválených obchodů přes Alpaca API."""
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce


def execute_trades(trading_client, trades):
    """
    Provede seznam obchodů. Vrací seznam výsledků (úspěch/chyba u každého obchodu) -
    jeden neúspěšný obchod nezastaví ostatní.
    """
    results = []
    for t in trades:
        try:
            side = OrderSide.BUY if t["side"] == "buy" else OrderSide.SELL

            if t.get("order_type") == "limit" and t.get("limit_price"):
                order_req = LimitOrderRequest(
                    symbol=t["symbol"],
                    qty=t["qty"],
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=t["limit_price"],
                )
            else:
                order_req = MarketOrderRequest(
                    symbol=t["symbol"],
                    qty=t["qty"],
                    side=side,
                    time_in_force=TimeInForce.DAY,
                )

            order = trading_client.submit_order(order_req)
            results.append({
                "symbol": t["symbol"],
                "side": t["side"],
                "qty": t["qty"],
                "status": "submitted",
                "order_id": str(order.id),
                "reasoning": t.get("reasoning", ""),
            })
        except Exception as e:
            results.append({
                "symbol": t.get("symbol"),
                "side": t.get("side"),
                "qty": t.get("qty"),
                "status": "failed",
                "error": str(e),
                "reasoning": t.get("reasoning", ""),
            })
    return results
