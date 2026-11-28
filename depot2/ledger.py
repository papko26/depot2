"""The team token ledger: append-only transactions; a balance is the sum of its amounts.

The code never lets a balance go below zero, and never closes the same stake twice. Pending conditional awards
(amount unknown) live in pending.jsonl, not in the ledger.
"""

from . import characters
from .store import append_jsonl, now_iso, read_jsonl

DEBITS = ("stake", "brainstorm_fee", "order", "lock")
CREDITS = ("seed", "auto_refund", "refund", "payout", "brainstorm_refund", "lock_refund")
CLOSERS = ("forfeit",)  # amount 0: the stake's tokens stay spent
FINES = ("fine",)  # amount <= 0: clamped at the balance, so a fine may take nothing
SETTLES = ("order_settle",)  # amount >= 0: burn returns nothing
STAKE_CLOSERS = ("auto_refund", "refund", "forfeit")  # each names the stake it closes (ref)
# The owner's actions; they gate the reflection. auto_refund, brainstorm_fee, brainstorm_refund,
# order, lock and lock_refund are not among them (an owner's unlock is a decision, decisions.jsonl).
OWNER_TYPES = ("payout", "refund", "forfeit", "fine", "order_settle")
SETTLE_OUTCOMES = ("moved", "burn", "refund")
LEGACY = ("think", "think_refund")  # read, never written
AUTO_REASON = "auto: stakes come back when the round ends"


class OverdraftError(ValueError):
    pass


class LedgerError(ValueError):
    pass


class Ledger:
    def __init__(self, state) -> None:
        self.state = state
        self.path = state.ledger_path

    def entries(self):
        return read_jsonl(self.path)

    def balance(self, cid, entries=None):
        entries = self.entries() if entries is None else entries
        return sum(e["amount"] for e in entries if e["character"] == cid)

    def balances(self, entries=None):
        entries = self.entries() if entries is None else entries
        return {cid: self.balance(cid, entries) for cid in characters.IDS}

    def last_seq(self, entries=None):
        entries = self.entries() if entries is None else entries
        return max([e["seq"] for e in entries] or [0])

    def get(self, tx_id, entries=None):
        for e in self.entries() if entries is None else entries:
            if e["id"] == tx_id:
                return e
        raise LedgerError(f"no transaction {tx_id}")

    def _append(self, typ, cid, amount, **fields):
        if cid not in characters.IDS:
            raise LedgerError(f"unknown character {cid!r}")
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise LedgerError("amount must be an int")
        if typ in CLOSERS:
            if amount != 0:
                raise LedgerError("%s has amount 0, got %d" % (typ, amount))  # noqa: UP031
        elif typ in FINES:
            if amount > 0:
                raise LedgerError("%s needs an amount <= 0, got %d" % (typ, amount))  # noqa: UP031
        elif typ in SETTLES:
            if amount < 0:
                raise LedgerError("%s needs an amount >= 0, got %d" % (typ, amount))  # noqa: UP031
        elif typ not in DEBITS + CREDITS:
            raise LedgerError(f"unknown transaction type {typ!r}")
        elif (typ in DEBITS and amount >= 0) or (typ in CREDITS and amount <= 0):
            raise LedgerError(
                "%s needs a %s amount, got %d"  # noqa: UP031
                % (typ, "negative" if typ in DEBITS else "positive", amount)
            )
        entries = self.entries()
        before = self.balance(cid, entries)
        if before + amount < 0:
            raise OverdraftError(
                "%s has %d, cannot take %d (%s)"  # noqa: UP031
                % (characters.name(cid), before, -amount, typ)
            )
        seq = self.last_seq(entries) + 1
        rec = {
            "seq": seq,
            "id": "tx-%06d" % seq,  # noqa: UP031
            "ts": now_iso(),
            "type": typ,
            "character": cid,
            "amount": amount,
            "balance_after": before + amount,
        }
        for k, v in fields.items():
            if v is not None:
                rec[k] = v
        append_jsonl(self.path, rec)
        self.state.touch(self.path)
        # The write is checked: the new balance must be what we computed.
        after = self.balance(cid)
        if after != before + amount:
            raise LedgerError("balance check failed for %s: %d != %d" % (cid, after, before + amount))  # noqa: UP031
        return rec

    # --- operations -------------------------------------------------------------------------

    def seed(self, cid, amount, reason):
        return self._append("seed", cid, int(amount), reason=reason)

    def stake(self, cid, amount, round_id, point, reason=None):
        if amount <= 0:
            raise LedgerError("a stake is at least 1")
        return self._append("stake", cid, -int(amount), round=round_id, point=point, reason=reason)

    def payout(self, cid, amount, reason, round_id=None, point=None, settles=None):
        if amount <= 0:
            raise LedgerError("a payout is at least 1")
        return self._append("payout", cid, int(amount), reason=reason, round=round_id, point=point, settles=settles)

    def fine(self, cid, amount, reason, round_id=None, point=None):
        """The owner's fine of `amount` tokens. The balance never goes below zero: the code takes
        what is there, up to `amount`. The transaction keeps the fine imposed (`asked`); what was
        taken is -tx["amount"].
        """
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
            raise LedgerError("a fine is at least 1")
        taken = min(amount, max(0, self.balance(cid)))
        return self._append("fine", cid, -taken, asked=amount, reason=reason, round=round_id, point=point)

    def brainstorm_fee(self, cid, amount, round_id, brainstorm_id, role, reason=None):
        if amount < 1:
            raise LedgerError("a brainstorm fee is at least 1")
        return self._append(
            "brainstorm_fee", cid, -int(amount), round=round_id, brainstorm=brainstorm_id, role=role, reason=reason
        )

    def brainstorm_refund(self, fee_tx, reason, round_id=None):
        if fee_tx["type"] != "brainstorm_fee":
            raise LedgerError("{} is not a brainstorm fee".format(fee_tx["id"]))
        if any(e["type"] == "brainstorm_refund" and e.get("ref") == fee_tx["id"] for e in self.entries()):
            raise LedgerError("{} already refunded".format(fee_tx["id"]))
        return self._append(
            "brainstorm_refund",
            fee_tx["character"],
            -fee_tx["amount"],
            ref=fee_tx["id"],
            brainstorm=fee_tx.get("brainstorm"),
            round=round_id,
            reason=reason,
        )

    def order(self, cid, price, round_id, order_id, reason=None):
        if price < 1:
            raise LedgerError("an order costs at least 1")
        return self._append("order", cid, -int(price), round=round_id, order=order_id, reason=reason)

    def order_settle(self, order_tx, outcome, bonus, reason):
        """The owner settles an order: moved = the price back + bonus, refund = the price back, burn = 0."""
        if order_tx["type"] != "order":
            raise LedgerError("{} is not an order".format(order_tx["id"]))
        if outcome not in SETTLE_OUTCOMES:
            raise LedgerError(f"an order settles as one of {SETTLE_OUTCOMES}")
        if any(e["type"] == "order_settle" and e.get("ref") == order_tx["id"] for e in self.entries()):
            raise LedgerError("{} already settled".format(order_tx.get("order", order_tx["id"])))
        price = -order_tx["amount"]
        amount = {"moved": price + int(bonus), "refund": price, "burn": 0}[outcome]
        return self._append(
            "order_settle",
            order_tx["character"],
            amount,
            ref=order_tx["id"],
            order=order_tx.get("order"),
            round=order_tx.get("round"),
            outcome=outcome,
            reason=reason,
        )

    def lock(self, cid, price, round_id, object_id, reason=None):
        if price < 1:
            raise LedgerError("a lock costs at least 1")
        return self._append("lock", cid, -int(price), round=round_id, object=object_id, reason=reason)

    def lock_refund(self, lock_tx, reason):
        if lock_tx["type"] != "lock":
            raise LedgerError("{} is not a lock".format(lock_tx["id"]))
        if any(e["type"] == "lock_refund" and e.get("ref") == lock_tx["id"] for e in self.entries()):
            raise LedgerError("{} already refunded".format(lock_tx["id"]))
        return self._append(
            "lock_refund",
            lock_tx["character"],
            -lock_tx["amount"],
            ref=lock_tx["id"],
            object=lock_tx.get("object"),
            reason=reason,
        )

    def open_stakes(self, cid=None, round_id=None, point=None, entries=None):
        entries = self.entries() if entries is None else entries
        closed = {e.get("ref") for e in entries if e["type"] in STAKE_CLOSERS}
        out = []
        for e in entries:
            if e["type"] != "stake" or e["id"] in closed:
                continue
            if cid and e["character"] != cid:
                continue
            if round_id and e.get("round") != round_id:
                continue
            if point and e.get("point") != point:
                continue
            out.append(e)
        return out

    def auto_refunds(self, round_id, entries=None):
        entries = self.entries() if entries is None else entries
        return [e for e in entries if e["type"] == "auto_refund" and e.get("round") == round_id]

    def _nothing_open(self, cid, round_id, point, forfeit=False):
        """The plain error when refund or forfeit finds nothing open."""
        what = "{} in {}{}".format(characters.name(cid) if cid else "anyone", round_id, " " + point if point else "")
        entries = self.entries()
        back = [
            e
            for e in self.auto_refunds(round_id, entries)
            if (not cid or e["character"] == cid) and (not point or e.get("point") == point)
        ]
        if not back:
            return LedgerError(f"no open stakes for {what}")
        msg = (
            "nothing open for %s: its stakes came back by themselves when the round ended "  # noqa: UP031
            "(%d auto refunds, %s … %s), so the owner has nothing to %s"
            % (what, len(back), back[0]["id"], back[-1]["id"], "keep" if forfeit else "refund")
        )
        if forfeit:
            msg += "; to take tokens, fine instead: depot2 fine <character|all> <n> --round {}{} --reason …".format(
                round_id, " --point " + point if point else ""
            )
        return LedgerError(msg)

    def auto_refund_round(self, round_id, reason=None):
        """Return every open stake of a round, by the code: the stakes come back when the round
        ends. One auto_refund per stake, naming it (ref). Not an owner action. [] when nothing is open.
        """
        return [
            self._append(
                "auto_refund",
                s["character"],
                -s["amount"],
                ref=s["id"],
                round=s.get("round"),
                point=s.get("point"),
                reason=reason or AUTO_REASON,
            )
            for s in self.open_stakes(round_id=round_id)
        ]

    def refund_stakes(self, cid, round_id, point=None, reason=None):
        """The owner refunds the open stakes of one character (or every one, cid=None) on a round/point."""
        stakes = self.open_stakes(cid=cid, round_id=round_id, point=point)
        if not stakes:
            raise self._nothing_open(cid, round_id, point)
        return [
            self._append(
                "refund",
                s["character"],
                -s["amount"],
                ref=s["id"],
                round=s.get("round"),
                point=s.get("point"),
                reason=reason,
            )
            for s in stakes
        ]

    def forfeit_stakes(self, cid, round_id, point=None, reason=None):
        """Close the open stakes of one character (or every one) without returning them."""
        stakes = self.open_stakes(cid=cid, round_id=round_id, point=point)
        if not stakes:
            raise self._nothing_open(cid, round_id, point, forfeit=True)
        return [
            self._append(
                "forfeit",
                s["character"],
                0,
                ref=s["id"],
                stake=-s["amount"],
                round=s.get("round"),
                point=s.get("point"),
                reason=reason,
            )
            for s in stakes
        ]


class Pending:
    """Conditional awards whose amount is not known yet (amount null). Open, then closed."""

    def __init__(self, state) -> None:
        self.state = state
        self.path = state.pending_path

    def records(self):
        return read_jsonl(self.path)

    def open_items(self):
        recs = self.records()
        closed = {r["id"] for r in recs if r["op"] == "close"}
        return [r for r in recs if r["op"] == "open" and r["id"] not in closed]

    def add(self, cid, condition, amount=None, ref=None):
        recs = self.records()
        n = len({r["id"] for r in recs}) + 1
        rec = {
            "id": "pend-%04d" % n,  # noqa: UP031
            "op": "open",
            "ts": now_iso(),
            "character": cid,
            "amount": amount,
            "condition": condition,
        }
        if ref:
            rec["ref"] = ref
        append_jsonl(self.path, rec)
        self.state.touch(self.path)
        return rec

    def close(self, pend_id, note, tx_id=None):
        if pend_id not in {r["id"] for r in self.open_items()}:
            raise LedgerError(f"{pend_id} is not an open pending item")
        rec = {"id": pend_id, "op": "close", "ts": now_iso(), "note": note}
        if tx_id:
            rec["tx"] = tx_id
        append_jsonl(self.path, rec)
        self.state.touch(self.path)
        return rec


class Decisions:
    """The owner's decisions about a character (or 'all'). They gate the reflection too."""

    def __init__(self, state) -> None:
        self.state = state
        self.path = state.decisions_path

    def records(self):
        return read_jsonl(self.path)

    def last_seq(self):
        return max([r["seq"] for r in self.records()] or [0])

    def add(self, who, text, round_id=None, point=None):
        if who != "all" and who not in characters.IDS:
            raise LedgerError(f"unknown character {who!r}")
        seq = self.last_seq() + 1
        rec = {"seq": seq, "id": "dec-%06d" % seq, "ts": now_iso(), "character": who, "text": text}  # noqa: UP031
        if round_id:
            rec["round"] = round_id
        if point:
            rec["point"] = point
        append_jsonl(self.path, rec)
        self.state.touch(self.path)
        return rec
