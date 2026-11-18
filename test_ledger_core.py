import os
import sqlite3
import tempfile
import threading
import unittest

from ledger_core import (IdempotencyConflict, InsufficientFunds, InvalidState, Ledger,
                         NotFound, ValidationError, parse_amount)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "ledger.db")
        self.L = Ledger(self.path)  # 10% commission
        self.L.deposit("cust1", "100.000", "dep-1", "bank-ref-1")

    def tearDown(self):
        self.dir.cleanup()

    def test_release_splits_90_10(self):
        self.L.hold("svc1", "cust1", "eng1", "100.000", "k-hold")
        self.assertEqual(self.L.balance("cust1"), "0.000")
        self.L.release("svc1", "k-rel")
        self.assertEqual(self.L.balance("eng1"), "90.000")
        e = self.L.get_escrow("svc1")
        self.assertEqual((e["state"], e["commission"]), ("RELEASED", "10.000"))
        self.assertTrue(self.L.verify_chain()["ok"])

    def test_rounding_keeps_books_balanced(self):
        self.L.deposit("cust2", "0.005", "dep-2", "r2")
        self.L.hold("svc2", "cust2", "eng1", "0.005", "k2")
        self.L.release("svc2", "k2r")
        self.assertEqual(self.L.get_escrow("svc2")["commission"], "0.001")
        self.assertEqual(self.L.balance("eng1"), "0.004")
        self.assertTrue(self.L.verify_chain()["ok"])

    def test_insufficient_funds(self):
        with self.assertRaises(InsufficientFunds):
            self.L.hold("svc3", "cust1", "eng1", "100.001", "k3")
        with self.assertRaises(NotFound):
            self.L.get_escrow("svc3")  # rolled back completely
        self.assertEqual(self.L.balance("cust1"), "100.000")

    def test_double_release_and_wrong_state(self):
        self.L.hold("svc4", "cust1", "eng1", "10.000", "k4")
        self.L.release("svc4", "k4r")
        with self.assertRaises(InvalidState):
            self.L.release("svc4", "k4r2")
        with self.assertRaises(InvalidState):
            self.L.refund("svc4", "k4f")

    def test_idempotent_replay_posts_once(self):
        a = self.L.hold("svc5", "cust1", "eng1", "10.000", "k5")
        b = self.L.hold("svc5", "cust1", "eng1", "10.000", "k5")
        self.assertFalse(a["replayed"])
        self.assertTrue(b["replayed"])
        self.assertEqual(a["seq"], b["seq"])
        self.assertEqual(self.L.balance("cust1"), "90.000")

    def test_idempotency_key_reuse_with_other_payload(self):
        self.L.hold("svc6", "cust1", "eng1", "10.000", "k6")
        with self.assertRaises(IdempotencyConflict):
            self.L.hold("svc6", "cust1", "eng1", "20.000", "k6")

    def test_refund(self):
        self.L.hold("svc7", "cust1", "eng1", "40.000", "k7")
        self.L.refund("svc7", "k7r")
        self.assertEqual(self.L.balance("cust1"), "100.000")
        self.assertEqual(self.L.get_escrow("svc7")["state"], "REFUNDED")

    def test_freeze_then_resolve(self):
        self.L.hold("svc8", "cust1", "eng1", "50.000", "k8")
        self.L.freeze("svc8", "k8f")
        with self.assertRaises(InvalidState):
            self.L.release("svc8", "k8x")      # cannot bypass dispute
        with self.assertRaises(InvalidState):
            self.L.refund("svc8", "k8y")
        self.L.resolve("svc8", "refund", "k8z")
        self.assertEqual(self.L.balance("cust1"), "100.000")
        self.assertTrue(self.L.verify_chain()["ok"])

    def test_resolve_requires_frozen(self):
        self.L.hold("svc9", "cust1", "eng1", "5.000", "k9")
        with self.assertRaises(InvalidState):
            self.L.resolve("svc9", "release", "k9r")

    def test_duplicate_hold_for_same_service(self):
        self.L.hold("svc10", "cust1", "eng1", "5.000", "k10")
        with self.assertRaises(InvalidState):
            self.L.hold("svc10", "cust1", "eng1", "5.000", "k10b")

    def test_amount_validation(self):
        for bad in ("0", "-1", "1.0001", "abc", "NaN", "Infinity", "1e30", ""):
            with self.assertRaises(ValidationError, msg=bad):
                parse_amount(bad)
        with self.assertRaises(ValidationError):
            parse_amount(12.5)  # floats rejected
        self.assertEqual(parse_amount("12.5"), 12500)

    def test_id_validation_blocks_injection_shapes(self):
        with self.assertRaises(ValidationError):
            self.L.deposit("x'; DROP TABLE accounts;--", "1.000", "ki", "r")
        self.assertTrue(self.L.verify_chain()["ok"])

    def test_same_external_ref_cannot_credit_twice(self):
        with self.assertRaises(IdempotencyConflict):
            self.L.deposit("cust1", "100.000", "dep-other-key", "bank-ref-1")
        self.assertEqual(self.L.balance("cust1"), "100.000")
        self.assertTrue(self.L.deposit("cust1", "100.000", "dep-1", "bank-ref-1")["replayed"])

    def test_verify_during_concurrent_writes_has_no_false_alarm(self):
        stop, bad = threading.Event(), []

        def writer():
            i = 0
            while not stop.is_set():
                i += 1
                self.L.deposit("cust9", "1.000", f"w-{i}", f"wr-{i}")

        th = threading.Thread(target=writer); th.start()
        for _ in range(20):
            if not (r := self.L.verify_chain())["ok"]:
                bad.append(r)
        stop.set(); th.join()
        self.assertEqual(bad, [])

    def test_verify_reads_one_consistent_snapshot(self):
        writer, fired = self.L, []

        class Spy(Ledger):
            def _connect(s):
                c = super()._connect()

                def cb(stmt):
                    if "FROM entries" in stmt and not fired:
                        fired.append(1)       # a deposit lands in the middle of verification
                        writer.deposit("cust-snap", "7.000", "snap-1", "snap-ref")
                c.set_trace_callback(cb)
                return c

        res = Spy(self.path).verify_chain()
        self.assertEqual(fired, [1])
        self.assertTrue(res["ok"], res)

    def test_lock_timeout_surfaces_original_error(self):
        fast = Ledger(self.path, busy_timeout=0.2)
        blocker = sqlite3.connect(self.path, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")           # another writer holds the lock
        try:
            with self.assertRaises(sqlite3.OperationalError) as cm:
                fast.deposit("cust1", "1.000", "lock-1", "lock-ref")
            self.assertIn("locked", str(cm.exception))   # not 'no transaction is active'
        finally:
            blocker.execute("ROLLBACK"); blocker.close()

    def test_tamper_is_detected(self):
        self.L.hold("svc11", "cust1", "eng1", "10.000", "k11")
        con = sqlite3.connect(self.path)
        con.execute("UPDATE entries SET amount = amount + 1 WHERE id = (SELECT MAX(id) FROM entries)")
        con.commit(); con.close()
        self.assertFalse(self.L.verify_chain()["ok"])

    def test_concurrent_holds_only_one_wins(self):
        results = []

        def worker(i):
            try:
                self.L.hold("svc-race", "cust1", "eng1", "60.000", f"race-{i}")
                results.append("ok")
            except Exception as e:  # noqa: BLE001
                results.append(type(e).__name__)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(self.L.balance("cust1"), "40.000")
        self.assertTrue(self.L.verify_chain()["ok"])


if __name__ == "__main__":
    unittest.main()
