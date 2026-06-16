"""
Stock market skill for kenzy-llm.
"""

from __future__ import annotations

import asyncio
import logging

from kenzy.llm.skills import skill  # type: ignore[import]

log = logging.getLogger(__name__)

_FIELDS = [
    "longName",
    "previousClose",
    "open",
    "dayLow",
    "dayHigh",
    "fiftyTwoWeekLow",
    "fiftyTwoWeekHigh",
    "twoHundredDayAverage",
    "regularMarketPrice",
    "marketState",
    "quoteType",
    "symbol",
    "currency",
]


def _fetch(ticker: str) -> dict:
    import yfinance  # type: ignore[import-untyped]
    data = yfinance.Ticker(ticker.upper())
    info = data.info
    return {k: info.get(k) for k in _FIELDS}


@skill
async def get_stock_info(tickers: list[str]) -> str:
    """Get current price and market data for one or more stock ticker symbols.

    Use when the user asks about a stock price, stock quote, or market data for
    a company — e.g. "what's Apple's stock price?", "how is Tesla doing?",
    "look up MSFT and GOOGL".

    tickers: list of ticker symbols, e.g. ["AAPL", "TSLA"]
    """
    if not tickers:
        return "No tickers provided."

    loop = asyncio.get_event_loop()
    results: list[str] = []

    for ticker in tickers:
        try:
            info = await loop.run_in_executor(None, _fetch, ticker)
            symbol       = info.get("symbol")       or ticker.upper()
            name         = info.get("longName")      or symbol
            price        = info.get("regularMarketPrice") or info.get("open")
            currency     = info.get("currency", "USD")
            market_state = info.get("marketState", "")
            prev_close   = info.get("previousClose")
            day_low      = info.get("dayLow")
            day_high     = info.get("dayHigh")
            week52_low   = info.get("fiftyTwoWeekLow")
            week52_high  = info.get("fiftyTwoWeekHigh")
            avg200       = info.get("twoHundredDayAverage")

            change_str = ""
            if price is not None and prev_close:
                change = price - prev_close
                pct    = (change / prev_close) * 100
                sign   = "+" if change >= 0 else ""
                change_str = f"  {sign}{change:.2f} ({sign}{pct:.2f}%)"

            lines = [f"{name} ({symbol})"]
            if price is not None:
                lines.append(f"  Price: {price:.2f} {currency}{change_str}  [{market_state}]")
            if day_low is not None and day_high is not None:
                lines.append(f"  Day range: {day_low:.2f} – {day_high:.2f}")
            if week52_low is not None and week52_high is not None:
                lines.append(f"  52-week range: {week52_low:.2f} – {week52_high:.2f}")
            if avg200 is not None:
                lines.append(f"  200-day avg: {avg200:.2f}")

            results.append("\n".join(lines))
        except Exception as exc:
            log.warning("Failed to fetch ticker %s: %s", ticker, exc)
            results.append(f"{ticker.upper()}: data unavailable ({exc})")

    return "\n\n".join(results)
