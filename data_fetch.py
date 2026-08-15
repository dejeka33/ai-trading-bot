"""
Modul pro stahování tržních dat a stavu účtu z Alpaca API.
Pozn.: Tento skript je navržený pro běh v GitHub Actions (nebo jinde s reálným
přístupem k internetu) - v cloudovém sandboxu Cowork nefunguje kvůli síťovému
omezení (Alpaca API není na allowlistu).
"""
import os
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


def get_clients():
    # .strip() ošetřuje případnou neviditelnou mezeru/odřádkování navíc,
    # které se snadno omylem zkopíruje spolu s hodnotou do GitHub Secrets.
    key = os.environ["ALPACA_API_KEY_ID"].strip()
    secret = os.environ["ALPACA_API_SECRET_KEY"].strip()
    paper = os.environ.get("ALPACA_PAPER", "true").strip().lower() == "true"

    trading_client = TradingClient(key, secret, paper=paper)
    stock_data_client = StockHistoricalDataClient(key, secret)
    crypto_data_client = CryptoHistoricalDataClient(key, secret)
    return trading_client, stock_data_client, crypto_data_client


def get_account_snapshot(trading_client):
    account = trading_client.get_account()
    positions = trading_client.get_all_positions()
    return {
        "cash": float(account.cash),
        "portfolio_value": float(account.portfolio_value),
        "buying_power": float(account.buying_power),
        "positions": [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
            for p in positions
        ],
    }


def get_recent_bars(stock_client, crypto_client, stock_symbols, crypto_symbols, lookback_days=14):
    end = datetime.utcnow()
    start = end - timedelta(days=lookback_days)
    result = {}

    if stock_symbols:
        req = StockBarsRequest(
            symbol_or_symbols=stock_symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,  # bezplatný feed - SIP vyžaduje placené předplatné u Alpaca
        )
        bars = stock_client.get_stock_bars(req)
        for symbol in stock_symbols:
            if symbol in bars.data:
                result[symbol] = [
                    {
                        "t": b.timestamp.isoformat(),
                        "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume,
                    }
                    for b in bars.data[symbol]
                ]

    if crypto_symbols:
        req = CryptoBarsRequest(
            symbol_or_symbols=crypto_symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars = crypto_client.get_crypto_bars(req)
        for symbol in crypto_symbols:
            if symbol in bars.data:
                result[symbol] = [
                    {
                        "t": b.timestamp.isoformat(),
                        "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume,
                    }
                    for b in bars.data[symbol]
                ]

    return result
