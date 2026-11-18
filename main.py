"""BTC race bot: rare-event jump / dip trader on 1-minute cbBTC bars.

Strategy (found by offline backtesting on the frozen dev window, then
verified with the kit's local grader):

  The tape is a near random walk with 1-minute volatility around 7 bps
  and a full round trip costing about 26 bps in fees, slippage and
  impact, so ordinary trend following and mean reversion both lose
  money here.  What survives is rare, large, fast moves: a sharp
  1-2 minute jump is usually the start of a short continuation leg
  (a large trade working through a thin pool), and a violent 15-minute
  washout tends to bounce.  The bot therefore sits in cash almost all
  of the time and only fires on three rare triggers, always full size
  (100% of cash), with a fixed take-profit, stop-loss and max hold:

  * jump2: 2-bar close-to-close return above +30 bps -> TP +120 bps,
    SL -120 bps, max hold 480 bars.
  * jump1: 1-bar return above +28 bps -> TP +80 bps, SL -120 bps,
    max hold 240 bars.
  * dip: 15-bar return below -140 bps (a washout) -> TP +120 bps,
    SL -120 bps, max hold 60 bars.

  Exits are judged on closes against the close of the bar on which
  the entry was decided (the same convention the offline backtest
  used).  Orders fill at the next bar's open; the bot never trades in
  the last 45 scored bars, and sells out if still holding with 5
  scored bars left, so it is never caught by the end-of-window
  flatten at a bad print.

  Deterministic: no clock, no network, no randomness, no file reads;
  every decision is a pure function of the bars seen so far.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
BPS = 10_000
MAX_SLIPPAGE_BPS = 10_000  # fills are at the next open; never reject on a gap

# Entry triggers, in basis points of close-to-close return.
JUMP2_LOOKBACK = 2
JUMP2_MIN_BPS = 30
JUMP1_LOOKBACK = 1
JUMP1_MIN_BPS = 28
DIP_LOOKBACK = 15
DIP_MAX_BPS = -140

# Per-mode exits: (take-profit bps, stop-loss bps, max hold in bars).
EXITS = {
    "jump2": (120, 120, 480),
    "jump1": (80, 120, 240),
    "dip": (120, 120, 60),
}

# No new entries in the final 45 scored bars; force-sell with 5 left.
TAIL_NO_ENTRY_BARS = 45
TAIL_FORCE_SELL_BARS = 5


def bps_change(now, then):
    """(now / then - 1) in basis points, integer math, truncating."""
    if then <= 0:
        return 0
    return (now - then) * BPS // then


class Bot:
    def __init__(self):
        self.closes = []          # close per batch, in price units (int)
        self.entry_close = None   # close of the bar the entry was decided on
        self.entry_mode = None    # "jump2" / "jump1" / "dip"
        self.held_bars = 0        # bars since the entry decision
        self.pending = None       # (mode, close) of a buy just sent, unconfirmed
        self.warmup_bars = 0
        self.scored_bars = 1440   # the window length; refined from hello if known

    def on_hello(self, state):
        self.warmup_bars = state.warmup_bars
        # Total batches = warmup + scored window; the harness always sends
        # 1440 scored bars for this competition.
        self.scored_bars = 1440

    def scored_index(self, state):
        """Index of this batch within the scored window (0-based)."""
        return state.seq - self.warmup_bars

    def on_batch(self, state, batch):
        close = state.close(SYMBOL)
        if close is None:
            return []
        self.closes.append(close)

        holding = SYMBOL in state.positions and state.positions[SYMBOL].qty > 0

        # Confirm or drop a pending buy from the previous batch.
        if self.pending is not None:
            if holding:
                self.entry_mode, self.entry_close = self.pending
                self.held_bars = 1
            self.pending = None

        if not holding:
            self.entry_close = None
            self.entry_mode = None
            self.held_bars = 0
        elif self.entry_close is not None:
            self.held_bars += 1

        idx = self.scored_index(state)

        # --- exits ------------------------------------------------------
        if holding and self.entry_close is not None:
            tp, sl, max_hold = EXITS[self.entry_mode]
            move = bps_change(close, self.entry_close)
            if move >= tp or move <= -sl or self.held_bars >= max_hold:
                return [{
                    "side": "sell",
                    "symbol": SYMBOL,
                    "qty": state.positions[SYMBOL].qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
            if idx >= self.scored_bars - TAIL_FORCE_SELL_BARS:
                return [{
                    "side": "sell",
                    "symbol": SYMBOL,
                    "qty": state.positions[SYMBOL].qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
            return []

        # --- entries ----------------------------------------------------
        if holding or state.warming_up:
            return []
        if idx < DIP_LOOKBACK:
            return []
        if idx >= self.scored_bars - TAIL_NO_ENTRY_BARS:
            return []

        closes = self.closes
        mode = None
        # NOTE: bps_change() truncates (floor division), so a true move of
        # e.g. +30.4 bps reads as 30; the triggers are >= to match the
        # offline backtest, which compared the untruncated move with >.
        if len(closes) > JUMP2_LOOKBACK and bps_change(
            closes[-1], closes[-1 - JUMP2_LOOKBACK]
        ) >= JUMP2_MIN_BPS:
            mode = "jump2"
        elif len(closes) > JUMP1_LOOKBACK and bps_change(
            closes[-1], closes[-1 - JUMP1_LOOKBACK]
        ) >= JUMP1_MIN_BPS:
            mode = "jump1"
        elif len(closes) > DIP_LOOKBACK and bps_change(
            closes[-1], closes[-1 - DIP_LOOKBACK]
        ) < DIP_MAX_BPS:
            mode = "dip"
        if mode is None:
            return []

        room = state.weight_room(SYMBOL)
        spend = state.cash if state.cash < room else room
        if spend <= 0:
            return []
        min_notional = int(state.hello["fees"]["min_notional_quote_units"])
        if spend < min_notional:
            return []
        self.pending = (mode, close)
        return [{
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": spend,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]


def main():
    bot = Bot()
    makefun_bars.run(bot.on_batch, on_hello=bot.on_hello)


if __name__ == "__main__":
    main()
