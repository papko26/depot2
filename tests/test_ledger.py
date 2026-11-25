import json
import os
import unittest

from helpers import TempState

from depot2.ledger import AUTO_REASON, Decisions, Ledger, LedgerError, OverdraftError, Pending
from depot2.store import WriteCheckError, append_jsonl


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState({"builder": 4, "painter": 5})
        self.led = Ledger(self.t.state)

    def tearDown(self):
        self.t.close()

    def test_balance_is_the_sum(self):
        self.led.stake("builder", 3, "r1", "p1")
        self.led.payout("builder", 2, "good point")
        self.assertEqual(self.led.balance("builder"), 4 - 3 + 2)
        self.assertEqual(sum(e["amount"] for e in self.led.entries() if e["character"] == "builder"), 3)

    def test_no_overdraft(self):
        self.led.stake("builder", 4, "r1", "p1")
        with self.assertRaises(OverdraftError):
            self.led.stake("builder", 1, "r1", "p2")
        self.assertEqual(self.led.balance("builder"), 0)
        # Nothing was written by the refused debit.
        self.assertEqual([e["type"] for e in self.led.entries() if e["character"] == "builder"],
                         ["seed", "stake"])

    def test_signs_are_enforced(self):
        with self.assertRaises(LedgerError):
            self.led._append("stake", "builder", 2)
        with self.assertRaises(LedgerError):
            self.led._append("payout", "builder", -2)
        with self.assertRaises(LedgerError):
            self.led.payout("builder", 0, "nothing")

    def test_fine_takes_tokens_never_below_zero(self):
        tx = self.led.fine("builder", 1, "требовал обратного", round_id="r1", point="Q7")
        self.assertEqual((tx["type"], tx["amount"], tx["asked"], tx["round"], tx["point"]), ("fine", -1, 1, "r1", "Q7"))
        self.assertEqual(self.led.balance("builder"), 3)
        tx = self.led.fine("builder", 10, "много")  # only 3 left: the code takes 3
        self.assertEqual((tx["amount"], tx["asked"], tx["balance_after"]), (-3, 10, 0))
        tx = self.led.fine("builder", 1, "пусто")  # nothing left: recorded, takes nothing
        self.assertEqual((tx["amount"], tx["asked"], tx["balance_after"]), (0, 1, 0))
        for bad in (0, -1, True, 1.5):
            with self.assertRaises(LedgerError):
                self.led.fine("builder", bad, "x")
        with self.assertRaises(LedgerError):
            self.led._append("fine", "builder", 2)  # a fine never adds
        self.assertEqual(self.led.balance("builder"), 0)

    def test_a_fine_gates_the_reflection(self):
        from depot2.reflect import changes_since, owner_facts
        self.led.fine("painter", 7, "требовала обратного", round_id="r1", point="Q7")
        owner, _ = changes_since(self.t.state, "painter")
        self.assertEqual([e["type"] for e in owner], ["fine"])
        self.assertIn("штраф -5 (из 7: больше не было) (r1 Q7): требовала обратного", owner_facts(owner, []))
        self.assertEqual(changes_since(self.t.state, "builder")[0], [])

    def test_paid_thinking_is_read_never_written(self):
        self.assertFalse(hasattr(self.led, "think") or hasattr(self.led, "think_refund"))
        for typ, amount in (("think", -1), ("think_refund", 1)):
            with self.assertRaises(LedgerError):
                self.led._append(typ, "painter", amount)
        append_jsonl(self.led.path, {"seq": 6, "id": "tx-000006", "ts": "2015-11-03T10:00:00+02:00",
                                     "type": "think", "character": "painter", "amount": -2, "balance_after": 3,
                                     "round": "r0", "call": "r0/painter-deep", "level": 2})
        self.assertEqual(self.led.balance("painter"), 3)
        self.assertEqual(self.led.stake("painter", 1, "r1", "p1")["balance_after"], 2)

    def test_auto_refund_returns_every_stake_of_the_round(self):
        a = self.led.stake("painter", 2, "r1", "p1")
        b = self.led.stake("painter", 1, "r1", "p2")
        c = self.led.stake("builder", 3, "r1", "p1")
        other = self.led.stake("builder", 1, "r0", "p9")  # another round's stake stays open
        got = self.led.auto_refund_round("r1")
        self.assertEqual([(t["type"], t["ref"], t["amount"], t["point"], t["reason"]) for t in got],
                         [("auto_refund", a["id"], 2, "p1", AUTO_REASON), ("auto_refund", b["id"], 1, "p2", AUTO_REASON),
                          ("auto_refund", c["id"], 3, "p1", AUTO_REASON)])
        self.assertEqual((self.led.balance("painter"), self.led.balance("builder")), (5, 3))
        self.assertEqual([e["id"] for e in self.led.open_stakes()], [other["id"]])
        self.assertEqual(self.led.auto_refund_round("r1"), [])  # no stake is closed twice
        self.assertEqual(len(self.led.auto_refunds("r1")), 3)

    def test_refund_and_forfeit_find_nothing_open_after_an_auto_refund(self):
        self.led.stake("painter", 2, "r1", "p1")
        self.led.auto_refund_round("r1")
        with self.assertRaises(LedgerError) as e:
            self.led.refund_stakes(None, "r1", reason="x")
        self.assertIn("came back by themselves when the round ended", str(e.exception))
        self.assertIn("nothing to refund", str(e.exception))
        with self.assertRaises(LedgerError) as e:
            self.led.forfeit_stakes("painter", "r1", point="p1", reason="x")
        self.assertIn("fine instead: depot2 fine <character|all> <n> --round r1 --point p1", str(e.exception))
        with self.assertRaises(LedgerError) as e:
            self.led.refund_stakes("builder", "r1", reason="x")
        self.assertEqual(str(e.exception), "no open stakes for Строитель in r1")
        self.assertEqual(self.led.balance("painter"), 5)

    def test_an_auto_refund_is_not_an_owner_action(self):
        from depot2.reflect import changes_since
        self.led.stake("painter", 2, "r1", "p1")
        self.led.auto_refund_round("r1")
        self.assertEqual(changes_since(self.t.state, "painter"), ([], []))
        self.led.payout("painter", 1, "толково", round_id="r1", point="p1")
        self.assertEqual([e["type"] for e in changes_since(self.t.state, "painter")[0]], ["payout"])

    def test_refund_open_stakes(self):
        a = self.led.stake("painter", 2, "r1", "p1")
        self.led.stake("painter", 1, "r1", "p2")
        self.led.stake("builder", 1, "r1", "p1")
        got = self.led.refund_stakes(None, "r1", point="p1", reason="owner: all p1 back")
        self.assertEqual(sorted(t["character"] for t in got), ["builder", "painter"])
        self.assertEqual([t["ref"] for t in got if t["character"] == "painter"], [a["id"]])
        self.assertEqual(len(self.led.open_stakes(round_id="r1")), 1)
        with self.assertRaises(LedgerError):
            self.led.refund_stakes("painter", "r1", point="p1")

    def test_forfeit_closes_without_refund(self):
        a = self.led.stake("painter", 3, "r3", "p6")
        self.led.stake("builder", 1, "r3", "p6")
        got = self.led.forfeit_stakes("painter", "r3", point="p6", reason="filters stay")
        self.assertEqual([(t["type"], t["amount"], t["ref"], t["stake"]) for t in got], [("forfeit", 0, a["id"], 3)])
        self.assertEqual(self.led.balance("painter"), 2)  # still spent
        self.assertEqual([e["character"] for e in self.led.open_stakes(round_id="r3")], ["builder"])
        with self.assertRaises(LedgerError):
            self.led.refund_stakes("painter", "r3", point="p6")  # closed: nothing to refund
        with self.assertRaises(LedgerError):
            self.led._append("forfeit", "painter", 1)

    def test_ids_are_full_and_sequential(self):
        tx = self.led.payout("builder", 1, "x")
        self.assertRegex(tx["id"], r"^tx-\d{6}$")
        self.assertEqual(tx["seq"], self.led.last_seq())

    def test_a_corrupt_ledger_is_an_error_not_a_skip(self):
        with open(self.led.path, "a") as f:
            f.write('{"broken": \n')
        with self.assertRaises(ValueError):
            self.led.balances()

    def test_balance_after_is_recorded(self):
        tx = self.led.stake("painter", 2, "r1", "p1")
        self.assertEqual(tx["balance_after"], 3)
        with open(self.led.path) as f:
            last = json.loads(f.read().splitlines()[-1])
        self.assertEqual(last, tx)

    def test_pending_amount_null(self):
        pend = Pending(self.t.state)
        p = pend.add("painter", "dark theme wins the reader 3/3 demo", amount=None, ref="B51")
        self.assertIsNone(p["amount"])
        self.assertEqual([x["id"] for x in pend.open_items()], [p["id"]])
        self.assertEqual(self.led.balance("painter"), 5)  # not a transaction
        tx = self.led.payout("painter", 3, "the dark theme won", settles=p["id"])
        pend.close(p["id"], "paid", tx_id=tx["id"])
        self.assertEqual(pend.open_items(), [])
        with self.assertRaises(LedgerError):
            pend.close(p["id"], "again")

    def test_decisions(self):
        d = Decisions(self.t.state).add("all", "export after the reader", "r3", "p1")
        self.assertEqual(d["id"], "dec-000001")
        with self.assertRaises(LedgerError):
            Decisions(self.t.state).add("nobody", "x")


class WriteCheckTest(unittest.TestCase):
    def test_append_rejects_newlines_in_records(self):
        t = TempState()
        try:
            path = os.path.join(t.dir, "y.jsonl")
            append_jsonl(path, {"text": "a\nb"})  # json escapes \n, so this is one line
            with open(path) as f:
                self.assertEqual(len(f.read().splitlines()), 1)
        finally:
            t.close()

    def test_write_check_error_type(self):
        self.assertTrue(issubclass(WriteCheckError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
