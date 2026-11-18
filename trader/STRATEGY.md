# Strategy (from 2026-11-18)

Buy and hold a broad US index ETF: SPYM (SPDR Portfolio S&P 500, 0.02% fee).

Why: over 10 years, most of SPY's gain came overnight (close to open +142%) vs
intraday (+37%). Day trading flattens every close and gives that up; the ORB
backtest showed no edge, and IBKR's ~$0.35 minimum per order costs ~0.9% per
round trip on a $75 position. Holding pays one commission.

Rules: hold; add money when deposited (whole shares of SPYM); no stops, no day
trading. The hourly day-trading scheduled task is disabled. tools/paper_trader.py
is kept for reference only. Live orders are drafted as IBKR instructions; the
user submits them.
