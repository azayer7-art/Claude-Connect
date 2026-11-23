# Handoff for a new session (written 2026-11-23)

Read trader/STRATEGY.md first. State as of this note:

- IBKR (live): 1 SPYM share (avg cost $87.15) + ~$12 cash. Plan: SPYM 70% / VXUS 30%,
  whole shares, one order per deposit, buy the fund furthest below target.
  User plans a $300 deposit by end of Nov 2026, then $100/month.
- The user approved autonomous order placement, but IBKR UK does not offer autonomous
  connector trading yet (flow stays REVIEW_AND_APPROVE). So: draft BUY instructions,
  the user approves them in IBKR. A weekday 13:17 ET scheduled task in the original
  session ("Weekly IBKR buy draft (SPYM/VXUS)") drafts buys whenever cash covers a share.
- Live trading limits: buy SPYM/VXUS with new cash; sell only by rule (drift >10 pts
  from target), never because of a down day; no single stocks, options or crypto.
- Day trading stays PAPER ONLY until `python3 tools/paper_trader.py summary` says
  PASSES (30+ trades, positive net profit per trade after fees, beats holding SPYM).
  Then live with $5 max loss per trade, $20 per day.
- Scheduled task "Paper day-trading test (no live orders)" fires hourly 9:33-15:33 ET
  on weekdays into the ORIGINAL session; move or recreate it if this session replaces it.
- Never commit personal data (the repo is public).
