import json, os, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from ledger_core import Ledger, LedgerError
from rabt_ledger_client import (LedgerAPIError, LedgerClient, LedgerUnavailable, idem_key,
                                on_service_transition)

STATE = {"fail_next": 0, "calls": 0}


def make_handler(L):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, obj):
            b = json.dumps(obj).encode(); self.send_response(code)
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def _do(self):
            STATE["calls"] += 1
            if self.headers.get("Authorization") != "Bearer testkey":
                return self._send(401, {"detail": "unauthorized"})
            if STATE["fail_next"] > 0:
                STATE["fail_next"] -= 1; return self._send(503, {})
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}"); k = self.headers.get("Idempotency-Key")
            p = self.path.strip("/").split("/")
            try:
                if p[1:] == ["escrow", "hold"]:
                    r = L.hold(body["service_id"], body["customer_id"], body["engineer_id"], body["amount"], k)
                elif p[1] == "escrow" and p[3] == "release": r = L.release(p[2], k)
                elif p[1] == "escrow" and p[3] == "refund": r = L.refund(p[2], k)
                elif p[1] == "escrow" and p[3] == "freeze": r = L.freeze(p[2], k)
                else: return self._send(404, {})
                self._send(200, r)
            except LedgerError as e:
                self._send(409, {"error": e.code, "detail": str(e)})
        do_POST = _do
    return H


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.L = Ledger(os.path.join(self.d.name, "l.db"))
        self.L.deposit("c1", "100.000", "dep-1", "ref-1")
        self.srv = HTTPServer(("127.0.0.1", 0), make_handler(self.L))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.c = LedgerClient(f"http://127.0.0.1:{self.srv.server_port}", "testkey", timeout=3, backoff=0.01)
        STATE.update(fail_next=0, calls=0)

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.d.cleanup()

    def test_full_flow_via_state_machine(self):
        svc = {"id": "s1", "customer_id": "c1", "engineer_id": "e1", "price": "100.000"}
        on_service_transition(self.c, svc, "accepted")
        on_service_transition(self.c, svc, "in_progress")   # no money
        on_service_transition(self.c, svc, "done")
        self.assertEqual(self.L.balance("e1"), "90.000")

    def test_retry_after_5xx_does_not_double_post(self):
        STATE["fail_next"] = 2
        self.c.hold("s2", "c1", "e1", "10.000")
        self.assertEqual(self.L.balance("c1"), "90.000")
        self.c.hold("s2", "c1", "e1", "10.000")           # replay
        self.assertEqual(self.L.balance("c1"), "90.000")

    def test_business_error_is_not_retried(self):
        with self.assertRaises(LedgerAPIError) as cm:
            self.c.hold("s3", "c1", "e1", "999.000")
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "insufficient_funds"))
        self.assertEqual(STATE["calls"], 1)

    def test_unavailable_after_retries(self):
        STATE["fail_next"] = 99
        with self.assertRaises(LedgerUnavailable):
            self.c.hold("s4", "c1", "e1", "1.000")
        self.assertEqual(STATE["calls"], 3)

    def test_wrong_key_rejected(self):
        bad = LedgerClient(self.c.base, "wrong", timeout=3, backoff=0.01)
        with self.assertRaises(LedgerAPIError) as cm:
            bad.hold("s5", "c1", "e1", "1.000")
        self.assertEqual(cm.exception.status, 401)

    def test_idem_key_length_and_charset(self):
        k = idem_key("hold", "x" * 64)
        self.assertLessEqual(len(k), 64)
        self.assertGreaterEqual(len(idem_key("dep", "a")), 8)


if __name__ == "__main__":
    unittest.main()
