"""SQLite transactions for durable spending reservations and fenced stage execution.

Money is stored in integer microdollars. Decimal inputs are rounded UP so reservations
cannot understate costs. Connections are short lived; network work never holds a lock.
"""

from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


class Conflict(Exception):
    pass


class BudgetExceeded(Exception):
    pass


def micro(amount) -> int:
    value = Decimal(str(amount))
    if not value.is_finite() or value < 0:
        raise ValueError("Money must be finite and nonnegative")
    return int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def uid() -> str:
    return uuid.uuid4().hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS studies (
 id TEXT PRIMARY KEY, market TEXT NOT NULL, limit_micro INTEGER NOT NULL CHECK(limit_micro>0));
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES studies(id),
 config_json TEXT NOT NULL, config_hash TEXT NOT NULL,
 limit_micro INTEGER NOT NULL CHECK(limit_micro>0), status TEXT NOT NULL,
 control_version INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pricing (
 id TEXT PRIMARY KEY, body_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS usage (
 request_id TEXT PRIMARY KEY, logical_request_id TEXT NOT NULL,
 run_id TEXT NOT NULL REFERENCES runs(id), provider TEXT NOT NULL, model TEXT,
 endpoint TEXT NOT NULL, agent TEXT NOT NULL, purpose TEXT NOT NULL,
 pricing_id TEXT NOT NULL REFERENCES pricing(id), upper_micro INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('reserved','unknown','settled','released')),
 calculated_micro INTEGER, billed_micro INTEGER, metrics_json TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS usage_run ON usage(run_id);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
 kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS stages (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 scope_id TEXT NOT NULL, name TEXT NOT NULL, input_hash TEXT NOT NULL,
 status TEXT NOT NULL, token TEXT, expires REAL, attempts INTEGER NOT NULL DEFAULT 0,
 result_json TEXT, UNIQUE(run_id,scope_id,name,input_hash));
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Unsupported database schema")
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def create_study(self, market: str, budget) -> str:
        identifier = uid()
        with self.transaction() as db:
            db.execute("INSERT INTO studies VALUES (?,?,?)", (identifier, market, micro(budget)))
        return identifier

    def create_run(self, study_id: str, config) -> str:
        identifier = uid()
        with self.transaction() as db:
            study = db.execute("SELECT * FROM studies WHERE id=?", (study_id,)).fetchone()
            if not study or study["market"] != config.market:
                raise ValueError("Run must match study market")
            db.execute("INSERT INTO runs(id,study_id,config_json,config_hash,limit_micro,status,created_at) "
                       "VALUES (?,?,?,?,?,'selecting',?)", (identifier, study_id, config.snapshot(),
                        config.digest(), micro(config.limits.run_cost_usd), time.time()))
        return identifier

    def register_pricing(self, body: dict) -> str:
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
        identifier = hashlib.sha256(raw.encode()).hexdigest()
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO pricing VALUES (?,?,?)", (identifier, raw, time.time()))
        return identifier

    @staticmethod
    def _used(db, where, args):
        return db.execute("SELECT COALESCE(SUM(CASE WHEN u.state='settled' THEN u.calculated_micro "
                          "WHEN u.state IN ('reserved','unknown') THEN u.upper_micro ELSE 0 END),0) "
                          "FROM usage u JOIN runs r ON r.id=u.run_id WHERE " + where, args).fetchone()[0]

    def reserve(self, *, run_id, request_id, logical_request_id, provider, model,
                endpoint, agent, purpose, pricing_id, upper):
        amount = micro(upper)
        if purpose not in {"development", "eval", "live"}:
            raise ValueError("Invalid usage purpose")
        blocked = False
        now = time.time()
        with self.transaction() as db:
            old = db.execute("SELECT * FROM usage WHERE request_id=?", (request_id,)).fetchone()
            if old:
                expected = (run_id, logical_request_id, provider, model, endpoint, agent, purpose, pricing_id, amount)
                actual = tuple(old[k] for k in ("run_id", "logical_request_id", "provider", "model", "endpoint", "agent", "purpose", "pricing_id", "upper_micro"))
                if actual != expected:
                    raise Conflict("Request ID reused with a different reservation")
                return False  # already accounted: caller MUST NOT send again
            run = db.execute("SELECT r.*, s.limit_micro AS study_limit FROM runs r JOIN studies s "
                             "ON s.id=r.study_id WHERE r.id=?", (run_id,)).fetchone()
            if not run or run["status"] not in {"selecting", "running", "queued"}:
                raise Conflict("Run is not accepting new requests")
            limits = json.loads(run["config_json"])["limits"]
            if provider == "openai":
                request_limit = limits["provider_requests"]
                request_count = db.execute("SELECT count(*) FROM usage WHERE run_id=? AND provider='openai'",
                                           (run_id,)).fetchone()[0]
            elif provider == "tavily" and endpoint in {"search", "extract"}:
                request_limit = limits["search_requests" if endpoint == "search" else "document_fetches"]
                request_count = db.execute("SELECT count(*) FROM usage WHERE run_id=? AND provider='tavily' AND endpoint=?",
                                           (run_id, endpoint)).fetchone()[0]
            else:
                raise ValueError("Unsupported provider/endpoint accounting policy")
            blocked = (self._used(db, "r.id=?", (run_id,)) + amount > run["limit_micro"] or
                       self._used(db, "r.study_id=?", (run["study_id"],)) + amount > run["study_limit"] or
                       request_count >= request_limit)
            if blocked:
                db.execute("UPDATE runs SET status='paused',control_version=control_version+1 WHERE id=?", (run_id,))
                self._event(db, run_id, "budget_paused", {"request_id": request_id, "requested_micro": amount})
            else:
                db.execute("INSERT INTO usage(request_id,logical_request_id,run_id,provider,model,endpoint,agent,purpose,"
                           "pricing_id,upper_micro,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'reserved',?,?)",
                           (request_id, logical_request_id, run_id, provider, model, endpoint, agent, purpose,
                            pricing_id, amount, now, now))
                self._event(db, run_id, "budget_reserved", {"request_id": request_id, "upper_micro": amount})
        if blocked:
            raise BudgetExceeded("Run or study budget cannot cover the next request")
        return True

    @staticmethod
    def _event(db, run_id, kind, payload):
        db.execute("INSERT INTO events(run_id,kind,payload_json,created_at) VALUES (?,?,?,?)",
                   (run_id, kind, json.dumps(payload, sort_keys=True), time.time()))

    def settle(self, request_id: str, calculated, metrics: dict, billed=None):
        # Strict metric allowlist: never persist HTTP headers or raw secret-bearing payloads.
        allowed = {"input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "credits",
                   "billable_units", "duration_ms", "status", "error_kind", "provider_request_id"}
        if set(metrics) - allowed:
            raise ValueError("Unrecognized usage metric")
        for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "duration_ms"):
            value = metrics.get(key)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Usage counters must be nonnegative integers or null")
        for subset, total in (("cached_input_tokens", "input_tokens"), ("reasoning_tokens", "output_tokens")):
            if metrics.get(subset) is not None and metrics.get(total) is not None and metrics[subset] > metrics[total]:
                raise ValueError("Usage subcategory exceeds its parent")
        amount, billed_amount = micro(calculated), None if billed is None else micro(billed)
        encoded = json.dumps(metrics, sort_keys=True)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM usage WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise KeyError(request_id)
            if row["state"] == "released":
                raise Conflict("Released reservation requires explicit reconciliation")
            if row["state"] == "settled" and (row["calculated_micro"], row["metrics_json"], row["billed_micro"]) == (amount, encoded, billed_amount):
                return
            self._event(db, row["run_id"], "usage_settled" if row["state"] != "settled" else "usage_corrected",
                        {"request_id": request_id, "previous_micro": row["calculated_micro"], "calculated_micro": amount})
            db.execute("UPDATE usage SET state='settled',calculated_micro=?,billed_micro=?,metrics_json=?,updated_at=? "
                       "WHERE request_id=?", (amount, billed_amount, encoded, time.time(), request_id))
            if amount > row["upper_micro"]:
                db.execute("UPDATE runs SET status='paused',control_version=control_version+1 WHERE id=?", (row["run_id"],))
                self._event(db, row["run_id"], "reservation_underestimated", {"request_id": request_id})

    def mark_unknown(self, request_id, error_kind="unknown_usage"):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM usage WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise KeyError(request_id)
            if row["state"] == "reserved":
                db.execute("UPDATE usage SET state='unknown',metrics_json=?,updated_at=? WHERE request_id=?",
                           (json.dumps({"status": "unknown", "error_kind": error_kind}), time.time(), request_id))
                self._event(db, row["run_id"], "usage_unknown", {"request_id": request_id, "error_kind": error_kind})

    def pause_run(self, run_id, reason):
        with self.transaction() as db:
            changed = db.execute("UPDATE runs SET status='paused',control_version=control_version+1 "
                                 "WHERE id=? AND status IN ('selecting','queued','running')", (run_id,)).rowcount
            if changed:
                self._event(db, run_id, "run_paused", {"reason": reason})

    def release(self, request_id, reason):
        if not reason.strip():
            raise ValueError("Documented non-billing reason required")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM usage WHERE request_id=?", (request_id,)).fetchone()
            if not row or row["state"] == "settled":
                raise Conflict("Cannot release a settled or missing request")
            db.execute("UPDATE usage SET state='released',updated_at=? WHERE request_id=?", (time.time(), request_id))
            self._event(db, row["run_id"], "usage_released", {"request_id": request_id, "reason": reason})

    def summary(self, run_id):
        with self.connection() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError(run_id)
            rows = db.execute("SELECT * FROM usage WHERE run_id=? ORDER BY created_at,request_id", (run_id,)).fetchall()
            totals = {"calculated_micro": 0, "reserved_micro": 0, "unknown_micro": 0,
                      "billed_micro": 0, "billed_unknown_count": 0}
            for row in rows:
                if row["state"] == "settled":
                    totals["calculated_micro"] += row["calculated_micro"]
                elif row["state"] in {"reserved", "unknown"}:
                    totals[f"{row['state']}_micro"] += row["upper_micro"]
                if row["billed_micro"] is not None:
                    totals["billed_micro"] += row["billed_micro"]
                elif row["state"] != "released":
                    totals["billed_unknown_count"] += 1
            used = totals["calculated_micro"] + totals["reserved_micro"] + totals["unknown_micro"]
            return {"run_id": run_id, "limit_micro": run["limit_micro"], "remaining_micro": run["limit_micro"] - used,
                    **totals, "requests": [dict(r) for r in rows]}

    def enqueue(self, run_id, scope_id, name, input_hash):
        with self.transaction() as db:
            identifier = uid()
            db.execute("INSERT INTO stages(id,run_id,scope_id,name,input_hash,status) VALUES (?,?,?,?,?,'pending') "
                       "ON CONFLICT(run_id,scope_id,name,input_hash) DO NOTHING",
                       (identifier, run_id, scope_id, name, input_hash))
            return db.execute("SELECT id FROM stages WHERE run_id=? AND scope_id=? AND name=? AND input_hash=?",
                              (run_id, scope_id, name, input_hash)).fetchone()[0]

    def claim(self, lease_seconds=150, now=None):
        if lease_seconds <= 0:
            raise ValueError("Lease must be positive")
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT s.* FROM stages s JOIN runs r ON s.run_id=r.id "
                             "WHERE r.status IN ('queued','running','selecting') AND "
                             "(s.status='pending' OR (s.status='running' AND s.expires<=?)) ORDER BY s.rowid LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            token = uid()
            db.execute("UPDATE stages SET status='running',token=?,expires=?,attempts=attempts+1 WHERE id=?",
                       (token, now + lease_seconds, row["id"]))
            return {**dict(row), "token": token, "expires": now + lease_seconds}

    def finish(self, stage_id, token, result, next_stage=None, now=None):
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT * FROM stages WHERE id=? AND token=? AND status='running' AND expires>?",
                             (stage_id, token, now)).fetchone()
            if not row:
                raise Conflict("Expired or superseded worker lease")
            db.execute("UPDATE stages SET status='succeeded',result_json=?,token=NULL,expires=NULL WHERE id=?",
                       (json.dumps(result), stage_id))
            if next_stage:
                name, input_hash = next_stage
                db.execute("INSERT INTO stages(id,run_id,scope_id,name,input_hash,status) VALUES (?,?,?,?,?,'pending') "
                           "ON CONFLICT(run_id,scope_id,name,input_hash) DO NOTHING",
                           (uid(), row["run_id"], row["scope_id"], name, input_hash))
            self._event(db, row["run_id"], "stage_succeeded", {"stage_id": stage_id})
