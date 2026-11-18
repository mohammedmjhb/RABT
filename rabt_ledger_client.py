"""Client for the RABT Ledger Engine — drop into the Flask backend.

- stdlib only (urllib), no extra dependency
- deterministic Idempotency-Keys => retrying after a timeout can never double-post
- retries ONLY on network errors / 5xx, never on 4xx (business errors)
- the ledger is called BEFORE the service state is changed in the Flask DB;
  if it raises, keep the old state and report the error.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request


class LedgerAPIError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class LedgerUnavailable(Exception):
    """Network failure / 5xx after all retries. State of the operation is unknown but
    safe to retry later with the same key."""


def idem_key(action: str, ref: str) -> str:
    k = f"rabt-{action}-{ref}"
    return k if len(k) <= 64 else f"rabt-{action}-" + hashlib.sha256(ref.encode()).hexdigest()[:40]


class LedgerClient:
    def __init__(self, base_url=None, api_key=None, timeout=10, retries=3, backoff=0.3):
        self.base = (base_url or os.environ.get("LEDGER_URL", "http://127.0.0.1:8100")).rstrip("/")
        self.key = api_key or os.environ["LEDGER_API_KEY"]
        self.timeout, self.retries, self.backoff = timeout, retries, backoff

    def _call(self, method, path, body=None, idem=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        if idem:
            headers["Idempotency-Key"] = idem
        last = None
        for attempt in range(self.retries):
            req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                payload = {}
                try:
                    payload = json.loads(e.read() or b"{}")
                except ValueError:
                    pass
                if e.code >= 500:
                    last = e
                else:
                    raise LedgerAPIError(e.code, payload.get("error", "http_error"),
                                         str(payload.get("detail", ""))) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
            time.sleep(self.backoff * (2 ** attempt))
        raise LedgerUnavailable(str(last))

    # -- operations ----------------------------------------------------
    def deposit(self, user_id, amount, external_ref):
        return self._call("POST", "/v1/deposits", {"user_id": user_id, "amount": amount,
                          "external_ref": external_ref}, idem_key("dep", external_ref))

    def hold(self, service_id, customer_id, engineer_id, amount):
        return self._call("POST", "/v1/escrow/hold", {"service_id": service_id, "customer_id": customer_id,
                          "engineer_id": engineer_id, "amount": amount}, idem_key("hold", service_id))

    def release(self, service_id):
        return self._call("POST", f"/v1/escrow/{service_id}/release", {}, idem_key("release", service_id))

    def refund(self, service_id):
        return self._call("POST", f"/v1/escrow/{service_id}/refund", {}, idem_key("refund", service_id))

    def freeze(self, service_id):
        return self._call("POST", f"/v1/escrow/{service_id}/freeze", {}, idem_key("freeze", service_id))

    def resolve(self, service_id, outcome):
        return self._call("POST", f"/v1/escrow/{service_id}/resolve", {"outcome": outcome},
                          idem_key(f"resolve-{outcome}", service_id))

    def balance(self, user_id):
        return self._call("GET", f"/v1/accounts/{user_id}/balance")["balance"]

    def escrow(self, service_id):
        return self._call("GET", f"/v1/escrow/{service_id}")

    def verify(self):
        return self._call("GET", "/v1/ledger/verify")


def on_service_transition(client: LedgerClient, svc: dict, new_state: str):
    """Call this from the Flask Service State Machine BEFORE committing new_state.
    svc needs: id, customer_id, engineer_id, price (decimal string e.g. "120.500")."""
    sid = str(svc["id"])
    if new_state == "accepted":
        return client.hold(sid, str(svc["customer_id"]), str(svc["engineer_id"]), svc["price"])
    if new_state == "done":
        return client.release(sid)
    if new_state == "cancelled":
        return client.refund(sid)
    if new_state == "disputed":
        return client.freeze(sid)
    return None  # pending / in_progress move no money
