"""RABT Ledger Core — double-entry ledger + escrow + SHA-256 hash chain.

Design rules
- Money is stored as INTEGER minor units (1 LYD = 1000 dirham). No floats.
- Every posting is balanced (sum DEBIT == sum CREDIT) and atomic with the
  escrow state change that caused it.
- Balance of an account = sum(CREDIT) - sum(DEBIT).
- Each transaction is chained: hash = SHA256(prev_hash + canonical payload).
- Every mutating call needs an idempotency key; replays return the original
  result and never post twice.
- All SQL is parameterised. No user input is ever concatenated into SQL.

Storage here is SQLite (stdlib). Moving to PostgreSQL = swap `_connect`,
`?` -> `%s`, and `BEGIN IMMEDIATE` -> `SELECT ... FOR UPDATE` on the chain head.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

GENESIS = "0" * 64
MINOR = 1000  # LYD has 3 decimals
SYS_EXTERNAL, SYS_ESCROW, SYS_REVENUE = "SYS:EXTERNAL", "SYS:ESCROW", "SYS:REVENUE"
MAX_AMOUNT = 10**12  # sanity cap in minor units

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id TEXT PRIMARY KEY, allow_negative INTEGER NOT NULL DEFAULT 0,
  balance INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS transactions(
  seq INTEGER PRIMARY KEY, idem_key TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
  kind TEXT NOT NULL, ref TEXT NOT NULL, created_at TEXT NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seq INTEGER NOT NULL REFERENCES transactions(seq),
  account_id TEXT NOT NULL REFERENCES accounts(id),
  direction TEXT NOT NULL CHECK(direction IN ('DEBIT','CREDIT')),
  amount INTEGER NOT NULL CHECK(amount > 0));
CREATE TABLE IF NOT EXISTS escrow(
  service_id TEXT PRIMARY KEY, customer TEXT NOT NULL, engineer TEXT NOT NULL,
  amount INTEGER NOT NULL, commission INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK(state IN ('HELD','RELEASED','REFUNDED','FROZEN')),
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_entries_seq ON entries(seq);
CREATE UNIQUE INDEX IF NOT EXISTS ux_deposit_ref ON transactions(ref) WHERE kind='DEPOSIT';
"""


class LedgerError(Exception):
    code = "ledger_error"


class ValidationError(LedgerError):
    code = "validation_error"


class NotFound(LedgerError):
    code = "not_found"


class InsufficientFunds(LedgerError):
    code = "insufficient_funds"


class InvalidState(LedgerError):
    code = "invalid_state"


class IdempotencyConflict(LedgerError):
    code = "idempotency_conflict"


def parse_amount(value: str) -> int:
    """'12.500' -> 12500. Rejects floats, negatives, zero, >3 decimals."""
    if not isinstance(value, str):
        raise ValidationError("amount must be a decimal string")
    try:
        d = Decimal(value.strip())
    except InvalidOperation:
        raise ValidationError("invalid amount")
    if not d.is_finite() or d <= 0:
        raise ValidationError("amount must be positive")
    scaled = d * MINOR
    if scaled != scaled.to_integral_value():
        raise ValidationError("amount has more than 3 decimal places")
    minor = int(scaled)
    if minor > MAX_AMOUNT:
        raise ValidationError("amount too large")
    return minor


def fmt_amount(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    m = abs(minor)
    return f"{sign}{m // MINOR}.{m % MINOR:03d}"


def _check_id(name: str, value: str) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 64) or not all(
        c.isalnum() or c in "-_." for c in value
    ):
        raise ValidationError(f"invalid {name}")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash(prev: str, seq: int, kind: str, ref: str, idem: str, ts: str, entries) -> str:
    payload = json.dumps(
        {"prev": prev, "seq": seq, "kind": kind, "ref": ref, "idem": idem, "ts": ts,
         "entries": [[a, d, n] for a, d, n in entries]},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class Ledger:
    def __init__(self, db_path: str, commission_bps: int = 1000, busy_timeout: float = 30.0):
        if not 0 <= commission_bps <= 10000:
            raise ValueError("commission_bps out of range")
        self.db_path, self.bps, self.busy = db_path, commission_bps, busy_timeout
        with self._connect() as c:
            c.executescript(SCHEMA)
            for acc, neg in ((SYS_EXTERNAL, 1), (SYS_ESCROW, 0), (SYS_REVENUE, 0)):
                c.execute("INSERT OR IGNORE INTO accounts(id, allow_negative) VALUES(?,?)", (acc, neg))

    # -- infrastructure -------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=self.busy, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.row_factory = sqlite3.Row
        return c

    @contextmanager
    def _tx(self):
        """One serialised write transaction (also serialises the hash chain)."""
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def _replay(self, c, idem: str, fp: str):
        row = c.execute("SELECT seq, hash, fingerprint FROM transactions WHERE idem_key=?", (idem,)).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fp:
            raise IdempotencyConflict("idempotency key reused with a different request")
        return {"seq": row["seq"], "hash": row["hash"], "replayed": True}

    def _post(self, c, idem, fp, kind, ref, lines):
        """lines: list of (account_id, 'DEBIT'|'CREDIT', amount). Must balance."""
        deb = sum(n for _, d, n in lines if d == "DEBIT")
        cre = sum(n for _, d, n in lines if d == "CREDIT")
        if deb != cre or deb == 0:
            raise ValidationError("unbalanced posting")
        net: dict[str, int] = {}
        for acc, d, n in lines:
            c.execute("INSERT OR IGNORE INTO accounts(id) VALUES(?)", (acc,))
            net[acc] = net.get(acc, 0) + (n if d == "CREDIT" else -n)
        for acc, delta in net.items():
            row = c.execute("SELECT balance, allow_negative FROM accounts WHERE id=?", (acc,)).fetchone()
            if row["balance"] + delta < 0 and not row["allow_negative"]:
                raise InsufficientFunds(f"insufficient funds in {acc}")
            c.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (delta, acc))
        head = c.execute("SELECT seq, hash FROM transactions ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (head["seq"] + 1, head["hash"]) if head else (1, GENESIS)
        ts = _now()
        ordered = sorted(lines, key=lambda x: (x[0], x[1], x[2]))
        h = _hash(prev, seq, kind, ref, idem, ts, ordered)
        c.execute("INSERT INTO transactions(seq,idem_key,fingerprint,kind,ref,created_at,prev_hash,hash) "
                  "VALUES(?,?,?,?,?,?,?,?)", (seq, idem, fp, kind, ref, ts, prev, h))
        c.executemany("INSERT INTO entries(seq,account_id,direction,amount) VALUES(?,?,?,?)",
                      [(seq, a, d, n) for a, d, n in ordered])
        return {"seq": seq, "hash": h, "replayed": False}

    @staticmethod
    def _fp(*parts) -> str:
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()

    # -- operations -----------------------------------------------------
    def deposit(self, user_id: str, amount: str, idem_key: str, external_ref: str):
        _check_id("user_id", user_id); _check_id("idem_key", idem_key); _check_id("external_ref", external_ref)
        n = parse_amount(amount)
        fp = self._fp("deposit", user_id, n, external_ref)
        with self._tx() as c:
            if (r := self._replay(c, idem_key, fp)):
                return r
            if c.execute("SELECT 1 FROM transactions WHERE kind='DEPOSIT' AND ref=?", (external_ref,)).fetchone():
                raise IdempotencyConflict("external_ref already credited")
            return self._post(c, idem_key, fp, "DEPOSIT", external_ref,
                              [(SYS_EXTERNAL, "DEBIT", n), (f"W:{user_id}", "CREDIT", n)])

    def hold(self, service_id: str, customer: str, engineer: str, amount: str, idem_key: str):
        for k, v in (("service_id", service_id), ("customer", customer), ("engineer", engineer), ("idem_key", idem_key)):
            _check_id(k, v)
        if customer == engineer:
            raise ValidationError("customer and engineer must differ")
        n = parse_amount(amount)
        fp = self._fp("hold", service_id, customer, engineer, n)
        with self._tx() as c:
            if (r := self._replay(c, idem_key, fp)):
                return r
            if c.execute("SELECT 1 FROM escrow WHERE service_id=?", (service_id,)).fetchone():
                raise InvalidState("escrow already exists for this service")
            res = self._post(c, idem_key, fp, "ESCROW_HOLD", service_id,
                             [(f"W:{customer}", "DEBIT", n), (SYS_ESCROW, "CREDIT", n)])
            c.execute("INSERT INTO escrow(service_id,customer,engineer,amount,state,updated_at) VALUES(?,?,?,?,?,?)",
                      (service_id, customer, engineer, n, "HELD", _now()))
            return res

    def _escrow(self, c, service_id: str, allowed: tuple[str, ...]):
        e = c.execute("SELECT * FROM escrow WHERE service_id=?", (service_id,)).fetchone()
        if e is None:
            raise NotFound("escrow not found")
        if e["state"] not in allowed:
            raise InvalidState(f"escrow is {e['state']}, expected one of {allowed}")
        return e

    def _settle(self, service_id, idem_key, action, allowed, to_state):
        _check_id("service_id", service_id); _check_id("idem_key", idem_key)
        fp = self._fp(action, service_id)
        with self._tx() as c:
            if (r := self._replay(c, idem_key, fp)):
                return r
            e = self._escrow(c, service_id, allowed)
            amt = e["amount"]
            if to_state == "RELEASED":
                fee = (amt * self.bps + 5000) // 10000  # half-up, integer math
                lines = [(SYS_ESCROW, "DEBIT", amt), (f"W:{e['engineer']}", "CREDIT", amt - fee)]
                if fee:
                    lines.append((SYS_REVENUE, "CREDIT", fee))
                kind = "ESCROW_RELEASE"
            else:
                fee, kind = 0, "ESCROW_REFUND"
                lines = [(SYS_ESCROW, "DEBIT", amt), (f"W:{e['customer']}", "CREDIT", amt)]
            res = self._post(c, idem_key, fp, kind, service_id, lines)
            c.execute("UPDATE escrow SET state=?, commission=?, updated_at=? WHERE service_id=?",
                      (to_state, fee, _now(), service_id))
            return res

    def release(self, service_id, idem_key):
        return self._settle(service_id, idem_key, "release", ("HELD",), "RELEASED")

    def refund(self, service_id, idem_key):
        return self._settle(service_id, idem_key, "refund", ("HELD",), "REFUNDED")

    def freeze(self, service_id: str, idem_key: str):
        _check_id("service_id", service_id); _check_id("idem_key", idem_key)
        fp = self._fp("freeze", service_id)
        with self._tx() as c:
            if (r := self._replay(c, idem_key, fp)):
                return r
            self._escrow(c, service_id, ("HELD",))
            c.execute("UPDATE escrow SET state='FROZEN', updated_at=? WHERE service_id=?", (_now(), service_id))
            # Freeze moves no money, but it is still chained so the audit trail is complete.
            head = c.execute("SELECT seq, hash FROM transactions ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (head["seq"] + 1, head["hash"]) if head else (1, GENESIS)
            ts = _now()
            h = _hash(prev, seq, "ESCROW_FREEZE", service_id, idem_key, ts, [])
            c.execute("INSERT INTO transactions(seq,idem_key,fingerprint,kind,ref,created_at,prev_hash,hash) "
                      "VALUES(?,?,?,?,?,?,?,?)", (seq, idem_key, fp, "ESCROW_FREEZE", service_id, ts, prev, h))
            return {"seq": seq, "hash": h, "replayed": False}

    def resolve(self, service_id: str, outcome: str, idem_key: str):
        """Dispute resolution: FROZEN -> RELEASED (to engineer) or REFUNDED (to customer)."""
        if outcome not in ("release", "refund"):
            raise ValidationError("outcome must be 'release' or 'refund'")
        to = "RELEASED" if outcome == "release" else "REFUNDED"
        return self._settle(service_id, idem_key, f"resolve-{outcome}", ("FROZEN",), to)

    # -- reads ----------------------------------------------------------
    def balance(self, user_id: str) -> str:
        _check_id("user_id", user_id)
        with self._connect() as c:
            row = c.execute("SELECT balance FROM accounts WHERE id=?", (f"W:{user_id}",)).fetchone()
            return fmt_amount(row["balance"] if row else 0)

    def get_escrow(self, service_id: str) -> dict:
        _check_id("service_id", service_id)
        with self._connect() as c:
            e = c.execute("SELECT * FROM escrow WHERE service_id=?", (service_id,)).fetchone()
            if e is None:
                raise NotFound("escrow not found")
            return {"service_id": e["service_id"], "customer": e["customer"], "engineer": e["engineer"],
                    "amount": fmt_amount(e["amount"]), "commission": fmt_amount(e["commission"]),
                    "state": e["state"]}

    def verify_chain(self) -> dict:
        """Recompute every hash, every posting balance, and every account balance."""
        problems, prev, expected = [], GENESIS, {}
        with self._connect() as c:
            c.execute("BEGIN")  # read snapshot: concurrent writers cannot cause false alarms
            for t in c.execute("SELECT * FROM transactions ORDER BY seq"):
                ents = [(r["account_id"], r["direction"], r["amount"]) for r in c.execute(
                    "SELECT account_id,direction,amount FROM entries WHERE seq=? ORDER BY account_id,direction,amount",
                    (t["seq"],))]
                if t["prev_hash"] != prev:
                    problems.append(f"seq {t['seq']}: broken link")
                if _hash(t["prev_hash"], t["seq"], t["kind"], t["ref"], t["idem_key"], t["created_at"], ents) != t["hash"]:
                    problems.append(f"seq {t['seq']}: hash mismatch")
                if sum(n for _, d, n in ents if d == "DEBIT") != sum(n for _, d, n in ents if d == "CREDIT"):
                    problems.append(f"seq {t['seq']}: unbalanced")
                for a, d, n in ents:
                    expected[a] = expected.get(a, 0) + (n if d == "CREDIT" else -n)
                prev = t["hash"]
            for r in c.execute("SELECT id, balance FROM accounts"):
                if r["balance"] != expected.get(r["id"], 0):
                    problems.append(f"account {r['id']}: cached balance != entries")
            total = c.execute("SELECT COALESCE(SUM(balance),0) s FROM accounts").fetchone()["s"]
            if total != 0:
                problems.append("global balance is not zero")
            c.execute("COMMIT")
        return {"ok": not problems, "problems": problems, "head": prev}
