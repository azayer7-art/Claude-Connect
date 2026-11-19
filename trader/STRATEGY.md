# Strategy (from 2026-11-18)

Buy and hold a broad US index ETF: SPYM (SPDR Portfolio S&P 500, 0.02% fee).

Why: over 10 years, most of SPY's gain came overnight (close to open +142%) vs
intraday (+37%). Day trading flattens every close and gives that up; the ORB
backtest showed no edge, and IBKR's ~$0.35 minimum per order costs ~0.9% per
round trip on a $75 position. Holding pays one commission.

Rules: hold; add money when deposited (whole shares of SPYM); no stops, no day
trading with real money. Day trading continues on PAPER only (hourly check-in),
as a test: it goes live only if 30+ paper trades show a clearly positive average
R after fees. Live orders are drafted as IBKR instructions; the
user submits them.

## How any strategy is judged (applies to every bot, idea, ad or reel)

1. Profit per trade AFTER fees is the measure. Win rate decides nothing:
   "81% wins" says nothing about profit.
2. Look at the size of the losses, not how often they happen. One big loss can
   erase dozens of small wins; check largest loss and average loss vs average win.
3. Costs count. Commissions ($0.35 minimum per order at IBKR Tiered) and spreads
   turn small edges negative. Paper results are recorded net of both commissions.
4. Always compare against simply holding SPYM over the same period. If a strategy
   cannot beat that, it is not worth the effort or the risk.
5. Nothing gets real money until it passes 1-4 on 30+ paper trades.
   `python3 tools/paper_trader.py summary` prints this scorecard and the verdict.

Default for this account: small size, long horizon, so holding beats active trading.
