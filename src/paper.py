"""Paper portfolio. PAPER ONLY -- there are no real-money code paths here.

The v1 "live trader" does not exist in this package; when it is built it
will be a separate, audited module. Nothing in this file can move funds.

Rules (from spec):
  - size_pct is a fraction of CURRENT equity (auto-scales with wins/losses)
  - max 0.20 per position (high-conviction tier), max 0.80 deployed at once
  - cash = starting_balance - open notionals + realized P&L of closed positions
"""

MAX_SIZE_PCT = 0.20      # high-conviction tier ceiling
MAX_DEPLOYED_PCT = 0.80  # dry powder + gas reserve


class PaperPortfolio:
    def __init__(self, database, starting_balance: float):
        self.db = database
        self.starting_balance = float(starting_balance)

    # ---- accounting ----
    def _open_rows(self):
        return self.db.conn.execute(
            "SELECT p.*, t.chain, t.address, t.symbol FROM positions p "
            "JOIN tokens t ON t.id = p.token_id "
            "WHERE p.mode='paper' AND p.status='open'"
        ).fetchall()

    def cash(self) -> float:
        open_notional = sum(r["notional"] for r in self._open_rows())
        closed_pnl = self.db.conn.execute(
            "SELECT COALESCE(SUM(pnl),0) s FROM positions "
            "WHERE mode='paper' AND status='closed'"
        ).fetchone()["s"]
        partial_pnl = self.db.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) s FROM positions "
            "WHERE mode='paper' AND status='open'"
        ).fetchone()["s"]
        return self.starting_balance - open_notional + closed_pnl + partial_pnl

    def equity(self, mark_prices: dict) -> float:
        """mark_prices: {token_id: current_price}. Cash + unrealized."""
        total = self.cash()
        for r in self._open_rows():
            px = mark_prices.get(r["token_id"], r["entry_price"])
            units = r["notional"] / r["entry_price"] if r["entry_price"] else 0
            total += units * px
        return total

    # ---- trading ----
    def open_position(self, token_id, size_pct, entry_price, ts=None,
                      reasoning="", score_breakdown=None) -> int:
        """Open a paper position. Returns position id. Raises on rule breach."""
        import time
        ts = int(ts if ts is not None else time.time())
        size_pct = float(size_pct)
        entry_price = float(entry_price)
        if not (0 < size_pct <= MAX_SIZE_PCT):
            raise ValueError(f"size_pct {size_pct} outside (0, {MAX_SIZE_PCT}]")
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")
        eq = self.equity({})
        notional = eq * size_pct
        deployed = sum(r["notional"] for r in self._open_rows())
        if (deployed + notional) / eq > MAX_DEPLOYED_PCT:
            raise ValueError("would exceed 80% max deployed")
        if notional > self.cash():
            raise ValueError("insufficient paper cash")
        cur = self.db.conn.execute(
            "INSERT INTO positions(mode, token_id, entry_price, size_pct,"
            " notional, opened_ts, status) VALUES('paper', ?, ?, ?, ?, ?, 'open')",
            (token_id, entry_price, size_pct, notional, ts),
        )
        pid = cur.lastrowid
        self.db.conn.commit()
        self.db.log_decision(
            "paper_open", token_id=token_id,
            score_breakdown={"size_pct": size_pct, "entry_price": entry_price,
                             "notional": notional,
                             **(score_breakdown or {})},
            reasoning=reasoning, tx_hash=None, ts=ts,
        )
        return pid

    def close_position(self, position_id, exit_price, ts=None,
                       reason="") -> float:
        """Close a paper position. Returns realized P&L."""
        import time
        ts = int(ts if ts is not None else time.time())
        row = self.db.conn.execute(
            "SELECT * FROM positions WHERE id=? AND status='open'",
            (position_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"no open position {position_id}")
        units = row["notional"] / row["entry_price"]
        pnl = units * float(exit_price) - row["notional"]
        self.db.conn.execute(
            "UPDATE positions SET status='closed', exit_price=?, closed_ts=?,"
            " pnl=?, close_reason=? WHERE id=?",
            (float(exit_price), ts, pnl, reason, position_id),
        )
        self.db.conn.commit()
        self.db.log_decision(
            "paper_close", token_id=row["token_id"],
            score_breakdown={"exit_price": float(exit_price), "pnl": pnl,
                             "pnl_pct": pnl / row["notional"]},
            reasoning=reason or "manual close", tx_hash=None, ts=ts,
        )
        return pnl

    def close_partial(self, position_id, fraction, exit_price, ts=None,
                      reason="") -> float:
        """Close a fraction (0,1) of a paper position. Returns realized P&L
        on the closed fraction; the position stays open with reduced
        notional. PAPER ONLY."""
        import time
        ts = int(ts if ts is not None else time.time())
        fraction = float(fraction)
        if not (0 < fraction < 1):
            raise ValueError(f"fraction {fraction} must be in (0, 1)")
        row = self.db.conn.execute(
            "SELECT * FROM positions WHERE id=? AND status='open'",
            (position_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"no open position {position_id}")
        closed_notional = row["notional"] * fraction
        units_closed = closed_notional / row["entry_price"]
        pnl = units_closed * float(exit_price) - closed_notional
        self.db.conn.execute(
            "UPDATE positions SET notional = notional * (1 - ?),"
            " realized_pnl = realized_pnl + ? WHERE id=?",
            (fraction, pnl, position_id),
        )
        self.db.conn.commit()
        self.db.log_decision(
            "paper_partial_close", token_id=row["token_id"],
            score_breakdown={"fraction": fraction,
                             "exit_price": float(exit_price), "pnl": pnl,
                             "pnl_pct": pnl / closed_notional,
                             "remaining_notional": row["notional"] * (1 - fraction)},
            reasoning=reason or "partial close", tx_hash=None, ts=ts,
        )
        return pnl

    def open_positions(self):
        return [dict(r) for r in self._open_rows()]

    def mark_to_market(self, mark_prices: dict):
        """Open positions with unrealized P&L at given marks."""
        out = []
        for r in self._open_rows():
            px = mark_prices.get(r["token_id"], r["entry_price"])
            units = r["notional"] / r["entry_price"] if r["entry_price"] else 0
            upnl = units * px - r["notional"]
            d = dict(r)
            d["mark_price"] = px
            d["unrealized_pnl"] = upnl
            d["unrealized_pct"] = upnl / r["notional"] if r["notional"] else 0
            out.append(d)
        return out
