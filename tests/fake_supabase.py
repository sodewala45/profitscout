"""Tiny in-memory stand-in for the supabase-py client, enough for the API tests.
No network: every table is a list of dicts."""
from __future__ import annotations

import copy
import fnmatch
import uuid
from datetime import datetime
from types import SimpleNamespace


class Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


def _cmp_val(v):
    return (v is None, v)


class Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters, self.orders = [], []
        self.lo, self.hi, self.lim = None, None, None
        self.op, self.payload, self.on_conflict = "select", None, None
        self.cols, self.count = "*", None
        self._not = False

    # --- builders ---
    def select(self, cols="*", count=None):
        self.cols, self.count = cols, count
        return self

    def _f(self, fn):
        if self._not:
            self._not = False
            self.filters.append(lambda r, fn=fn: not fn(r))
        else:
            self.filters.append(fn)
        return self

    @property
    def not_(self):
        self._not = True
        return self

    def eq(self, c, v):
        return self._f(lambda r: str(r.get(c)) == str(v) if not isinstance(v, bool) else r.get(c) is v)

    def gte(self, c, v):
        return self._f(lambda r: r.get(c) is not None and r.get(c) >= v)

    def lte(self, c, v):
        return self._f(lambda r: r.get(c) is not None and r.get(c) <= v)

    def lt(self, c, v):
        def fn(r):
            x = r.get(c)
            if x is None:
                return False
            if isinstance(x, (int, float)) and isinstance(v, (int, float)):
                return x < v
            return str(x) < str(v)
        return self._f(fn)

    def ilike(self, c, pat):
        # Postgres ILIKE: % = any run, _ = one char, backslash escapes the next character.
        import re as _re
        rx, i = "", 0
        while i < len(pat):
            ch = pat[i]
            if ch == "\\" and i + 1 < len(pat):
                rx += _re.escape(pat[i + 1]); i += 2
                continue
            rx += ".*" if ch == "%" else "." if ch == "_" else _re.escape(ch)
            i += 1
        cre = _re.compile(rx + r"\Z", _re.I | _re.S)
        return self._f(lambda r: bool(cre.match(str(r.get(c) or ""))))

    def is_(self, c, v):
        want = {"null": None, "true": True, "false": False}[str(v)]
        return self._f(lambda r: r.get(c) is want)

    def in_(self, c, vals):
        vals = {str(v) for v in vals}
        return self._f(lambda r: str(r.get(c)) in vals)

    def or_(self, expr):
        """PostgREST or=(...) with eq / gte / is.null / not.is.null parts and nested and(...)."""
        def split(e):
            parts, depth, cur = [], 0, ""
            for ch in e:
                if ch == "," and depth == 0:
                    parts.append(cur); cur = ""
                    continue
                depth += ch == "("
                depth -= ch == ")"
                cur += ch
            return parts + ([cur] if cur else [])

        def test(part, r):
            if part.startswith("and(") and part.endswith(")"):
                return all(test(p, r) for p in split(part[4:-1]))
            col, rest = part.split(".", 1)
            if rest == "is.null":
                return r.get(col) is None
            if rest == "not.is.null":
                return r.get(col) is not None
            op, val = rest.split(".", 1)
            if op == "gte":
                return r.get(col) is not None and float(r.get(col)) >= float(val)
            if op == "eq":
                return str(r.get(col)) == val
            raise NotImplementedError(part)

        return self._f(lambda r: any(test(p, r) for p in split(expr)))

    def order(self, c, desc=False, nullsfirst=None):
        self.orders.append((c, desc))
        return self

    def range(self, a, b):
        self.lo, self.hi = a, b
        return self

    def limit(self, n):
        self.lim = n
        return self

    def single(self):
        self.lim = 1
        self._single = True
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def upsert(self, rows, on_conflict=None):
        self.op, self.payload, self.on_conflict = "upsert", rows, on_conflict
        return self

    def update(self, vals):
        self.op, self.payload = "update", vals
        return self

    def delete(self):
        self.op = "delete"
        return self

    # --- run ---
    def execute(self):
        if self.table in self.db.fail_tables:
            raise Exception(self.db.fail_tables[self.table])
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "insert":
            new = self.payload if isinstance(self.payload, list) else [self.payload]
            for r in new:
                if self.table == "webhook_events" and any(x["id"] == r["id"] for x in rows):
                    raise Exception("duplicate key value violates unique constraint (23505)")
                r = dict(r)
                r.setdefault("id", len(rows) + 1)
                rows.append(r)
            return Result(copy.deepcopy(new))
        if self.op == "upsert":
            new = self.payload if isinstance(self.payload, list) else [self.payload]
            keys = [k.strip() for k in (self.on_conflict or "id").split(",")]
            for r in new:
                cur = next((x for x in rows if all(x.get(k) == r.get(k) for k in keys)), None)
                if cur is not None:
                    cur.update(copy.deepcopy(r))
                else:
                    rows.append(copy.deepcopy(r))
            return Result(copy.deepcopy(new))
        match = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in match:
                r.update(self.payload)
            return Result(copy.deepcopy(match))
        if self.op == "delete":
            for r in match:
                rows.remove(r)
            return Result(copy.deepcopy(match))
        for c, desc in reversed(self.orders):
            match.sort(key=lambda r: _cmp_val(r.get(c)), reverse=desc)
        total = len(match)
        if self.lo is not None:
            match = match[self.lo:self.hi + 1]
        if self.lim is not None:
            match = match[:self.lim]
        if self.db.max_rows is not None:          # PostgREST max-rows: silently caps every response
            match = match[:self.db.max_rows]
        if self.cols not in ("*", None):
            keep = [c.strip() for c in self.cols.split(",")]
            self.db.selected_columns.append((self.table, tuple(keep)))
            match = [{k: r.get(k) for k in keep if k in r} for r in match]
        data = copy.deepcopy(match)
        if getattr(self, "_single", False):
            if not data:
                raise Exception("PGRST116 JSON object requested, multiple (or no) rows returned")
            data = data[0]
        return Result(data, total if self.count else None)


class RPC:
    def __init__(self, db, name, params):
        self.db, self.name, self.params = db, name, params

    def execute(self):
        self.db.rpc_calls.append((self.name, dict(self.params)))
        fn = getattr(self.db, "rpc_" + self.name, None)
        if fn is None:
            raise Exception(f"function {self.name} does not exist")
        return Result(fn(**self.params))


class FakeUser(SimpleNamespace):
    pass


class FakeAuth:
    def __init__(self, db):
        self.db = db
        self.admin = SimpleNamespace(get_user_by_id=self._by_id)

    def get_user(self, token):
        self.db.auth_calls += 1
        u = self.db.tokens.get(token)
        if not u:
            raise Exception("invalid JWT")
        return SimpleNamespace(user=u)

    def _by_id(self, uid):
        for u in self.db.tokens.values():
            if u.id == uid:
                return SimpleNamespace(user=u)
        raise Exception("User not found")


class FakeDB:
    def __init__(self):
        self.reset()

    def reset(self):
        self.tables = {}
        self.tokens = {}
        self.fail_tables = {}
        self.rpc_calls = []
        self.selected_columns = []
        self.auth_calls = 0
        self.consumed = []
        self.max_rows = None

    def add_user(self, token, email, confirmed=True, plan="free", uid=None, users_row=True):
        u = FakeUser(id=uid or str(uuid.uuid4()), email=email,
                     email_confirmed_at="2026-01-01T00:00:00Z" if confirmed else None,
                     confirmed_at="2026-01-01T00:00:00Z" if confirmed else None)
        self.tokens[token] = u
        if users_row:
            self.tables.setdefault("users", []).append({"user_email": email, "plan": plan})
        return u

    # --- SQL functions ---
    def rpc_get_credit_balance(self, p_email):
        return {"plan": "free", "credits_total": 5, "credits_used": 0, "credits_remaining": 5}

    def rpc_consume_credit(self, p_email, p_product_id, p_platform, p_mode):
        self.consumed.append((p_email, p_product_id, p_platform))
        return {"allowed": True, "charged": True, "credits_total": 5, "credits_used": 1}

    def rpc_refund_credit(self, p_email, p_product_id, p_platform):
        return None

    def rpc_start_credit_period(self, p_email):
        return {}

    def rpc_upsert_subscription(self, p_id, p_user_email, p_user_id, p_variant_id, p_status, p_updated_at):
        subs = self.tables.setdefault("subscriptions", [])
        ts = datetime.fromisoformat(p_updated_at.replace("Z", "+00:00")) if p_updated_at else datetime.now()
        cur = next((s for s in subs if s["ls_subscription_id"] == p_id), None)
        if cur and cur["updated_at"] and ts < cur["updated_at"]:
            return False
        row = {"ls_subscription_id": p_id, "user_email": (p_user_email or "").lower(), "user_id": p_user_id,
               "variant_id": p_variant_id, "status": (p_status or "").lower(), "updated_at": ts}
        if cur:
            cur.update(row)
        else:
            subs.append(row)
        return True


class FakeClient:
    def __init__(self, db):
        self.db = db
        self.auth = FakeAuth(db)
        self.storage = SimpleNamespace()

    def table(self, name):
        return Query(self.db, name)

    def rpc(self, name, params):
        return RPC(self.db, name, params)


DB = FakeDB()


def create_client(*a, **k):
    return FakeClient(DB)
