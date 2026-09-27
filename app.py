"""
Ski Valet — Flask + PostgreSQL backend (Railway)

The two front-ends (static/index.html and static/admin.html) keep their data
model in memory and sync it through /api/sync. PostgreSQL is the single source
of truth: nothing is kept in the browser's localStorage anymore, and the admin
password and staff PIN only ever exist here, as hashes.

Sync protocol (see static/sync.js)
  * Every row carries a `rev` taken from one global sequence (set by a trigger
    on insert/update). Deletes leave a tombstone with its own rev.
  * GET  /api/sync?since=<rev>&epoch=<e>  -> everything that changed after <rev>
  * POST /api/sync {since, epoch, upserts, deletes, snapshot?}
      Each changed row is sent with the `_rev` the client last saw. If the row
      changed on the server meanwhile (another device), the whole push is
      rejected with 409 and the client reloads. Writes are serialised with an
      advisory lock, so rev order == commit order and polling never misses rows.
  * `epoch` changes on bulk operations (restore, import, clear history); a
    client with an old epoch gets a full reload.
"""
import datetime as dt
import json
import math
import os
import re
import secrets
import time
from contextlib import contextmanager
from functools import wraps
from urllib.parse import urlparse

import psycopg2
import psycopg2.errors
import psycopg2.extras
import psycopg2.pool
from flask import Flask, jsonify, request, send_from_directory, session
from psycopg2.extras import Json
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set (add a PostgreSQL service on Railway and reference it)")

COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"      # set 0 only for local http
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "5000"))      # history rows sent on a full load
MAX_SNAPSHOTS = 15
LOGIN_MAX_FAILS = 10                                              # per IP, per 10 minutes
WRITE_LOCK = 734001                                               # pg advisory lock key

# Built-in admin used on the very first start when ADMIN_PASSWORD is not set.
# It is the same default login the single-file version had.
LEGACY_ADMIN_HASH = ("pbkdf2:sha256:260000$IrBfOswUM7KwcPJZ$"
                     "20a131a6402fb718d4188a339933595ad0c17330e4b56f66961a904840cfb047")

# --------------------------------------------------------------------------
# Data model — one entry per column the apps use. Unknown fields a client
# sends are kept in the row's `extra` jsonb, so nothing is lost.
# --------------------------------------------------------------------------
SCHEMA = {
    "rooms": [("room_number", "text"), ("guest_name", "text"), ("departure", "date"), ("status", "text"),
              ("created_at", "ts"), ("updated_at", "ts")],
    "storages": [("storage_number", "text"), ("section", "text"), ("status", "text"), ("room_id", "ref"),
                 ("created_at", "ts"), ("updated_at", "ts")],
    "cards": [("card_code", "text"), ("room_id", "ref"), ("storage_id", "ref"), ("status", "text"),
              ("last_room_id", "bigint"), ("last_storage_id", "bigint"), ("last_scanned_at", "ts"),
              ("released_at", "ts"), ("created_at", "ts"), ("updated_at", "ts")],
    "equipment": [("storage_id", "ref"), ("category", "text"), ("description", "text"), ("quantity", "int"),
                  ("size", "text"), ("type", "text"), ("status", "text"), ("created_at", "ts"), ("updated_at", "ts")],
    "users": [("name", "text"), ("email", "text"), ("role", "text"), ("shared", "text"), ("archived", "bool"),
              ("retired", "bool"), ("created_at", "ts"), ("updated_at", "ts")],
    "history": [("user_id", "bigint"), ("action", "text"), ("room_id", "bigint"), ("storage_id", "bigint"),
                ("key_card_id", "bigint"), ("equipment_id", "bigint"), ("timestamp", "ts"), ("notes", "text"),
                ("prev", "text"), ("next", "text")],
}
TABLES = ["rooms", "storages", "cards", "equipment", "users", "history"]   # insert order (FKs)
HALL = ["rooms", "storages", "cards", "equipment"]
REFS = {("storages", "room_id"): "rooms", ("cards", "room_id"): "rooms",
        ("cards", "storage_id"): "storages", ("equipment", "storage_id"): "storages"}
UNIQUE = {"cards": "card_code", "storages": "storage_number"}
COLTYPE = {t: dict(cols) for t, cols in SCHEMA.items()}
# never stored from client input: secrets and derived fields
IGNORED_KEYS = {"id", "_rev", "rev", "extra", "username", "password_hash", "pin", "pin_hash", "pin_set", "auth_ver"}
SQLTYPE = {"ts": "timestamptz", "date": "date", "int": "integer", "bigint": "bigint", "ref": "bigint",
           "text": "text", "bool": "boolean NOT NULL DEFAULT false"}
MAX_SAFE_ID = 2 ** 53 - 1


# ---------------- value conversion (JSON <-> SQL) ----------------
def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", v):
        return float(v) if "." in v else int(v)
    return None


def to_int(v):
    n = _num(v)
    return int(n) if n is not None else None


def to_ts(v):
    n = _num(v)
    if n is not None:
        try:
            return dt.datetime.fromtimestamp(n / 1000, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str) and v.strip():
        try:
            d = dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


def to_date(v):
    if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v.strip()):
        try:
            return dt.date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def to_text(v):
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def to_bool(v):
    return bool(v) and v not in ("false", "0", 0)


IN = {"ts": to_ts, "date": to_date, "int": to_int, "bigint": to_int, "ref": to_int, "text": to_text, "bool": to_bool}
OUT = {
    "ts": lambda v: int(round(v.timestamp() * 1000)) if v else None,
    "date": lambda v: v.isoformat() if v else None,
    "bool": bool,
}


def valid_id(v):
    n = to_int(v)
    return n if n is not None and 0 < n <= MAX_SAFE_ID else None


def new_id():
    return int(time.time() * 1000) * 1000 + secrets.randbelow(1000)


def row_in(t, o):
    vals = {c: IN[ty](o.get(c)) for c, ty in SCHEMA[t]}
    extra = {k: v for k, v in o.items() if k not in COLTYPE[t] and k not in IGNORED_KEYS}
    return vals, extra


def row_out(t, r, admin=False, with_rev=True):
    o = dict(r.get("extra") or {})
    o["id"] = r["id"]
    for c, ty in SCHEMA[t]:
        v = r[c]
        o[c] = OUT[ty](v) if ty in OUT else v
    if t == "users":
        if r["shared"] == "staff":
            o["pin_set"] = bool(r["pin_hash"])
        if r["shared"] == "admin" and admin:
            o["username"] = r["username"]
    if with_rev:
        o["_rev"] = r["rev"]
    return o


# ---------------- database ----------------
POOL = psycopg2.pool.ThreadedConnectionPool(
    1, int(os.environ.get("DB_POOL_MAX", "8")), DATABASE_URL,
    cursor_factory=psycopg2.extras.RealDictCursor)


def _checkout():
    for _ in range(3):
        conn = POOL.getconn()
        try:
            if not conn.closed:
                conn.rollback()
                with conn.cursor() as c:          # connections dropped while idle get replaced
                    c.execute("SELECT 1")
                conn.rollback()
                return conn
        except psycopg2.Error:
            pass
        POOL.putconn(conn, close=True)
    return POOL.getconn()


@contextmanager
def tx(repeatable=False):
    conn = _checkout()
    broken = False
    try:
        conn.set_session(isolation_level="REPEATABLE READ" if repeatable else "READ COMMITTED",
                         readonly=repeatable)
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        raise
    except BaseException:
        try:
            conn.rollback()
        except psycopg2.Error:
            broken = True
        raise
    finally:
        POOL.putconn(conn, close=broken or bool(conn.closed))


def write_lock(cur):
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))


def ddl():
    out = [
        "CREATE SEQUENCE IF NOT EXISTS rev_seq",
        "CREATE TABLE IF NOT EXISTS meta (key text PRIMARY KEY, value text NOT NULL)",
        """CREATE TABLE IF NOT EXISTS tombstones (tbl text NOT NULL, id bigint NOT NULL, rev bigint NOT NULL,
             PRIMARY KEY (tbl, id))""",
        "CREATE INDEX IF NOT EXISTS tombstones_rev ON tombstones (rev)",
        """CREATE TABLE IF NOT EXISTS snapshots (id serial PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
             label text NOT NULL, by_name text, data jsonb NOT NULL)""",
        "CREATE TABLE IF NOT EXISTS login_failures (ip text NOT NULL, at timestamptz NOT NULL DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS login_failures_ip_at ON login_failures (ip, at)",
        """CREATE OR REPLACE FUNCTION sv_bump_rev() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN NEW.rev := nextval('rev_seq'); RETURN NEW; END $$""",
        """CREATE OR REPLACE FUNCTION sv_tombstone() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             INSERT INTO tombstones (tbl, id, rev) VALUES (TG_TABLE_NAME, OLD.id, nextval('rev_seq'))
             ON CONFLICT (tbl, id) DO UPDATE SET rev = EXCLUDED.rev;
             RETURN OLD;
           END $$""",
    ]
    for t in TABLES:
        cols = ["id bigint PRIMARY KEY", "rev bigint NOT NULL DEFAULT 0"]
        cols += [f'"{c}" {SQLTYPE[ty]}' for c, ty in SCHEMA[t]]
        cols.append("extra jsonb NOT NULL DEFAULT '{}'::jsonb")
        if t == "users":
            cols += ["username text", "password_hash text", "pin_hash text", "auth_ver integer NOT NULL DEFAULT 1"]
        for (tt, c), ref in REFS.items():
            if tt == t:
                cols.append(f'FOREIGN KEY ("{c}") REFERENCES {ref} (id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED')
        if t in UNIQUE:
            cols.append(f'UNIQUE ("{UNIQUE[t]}") DEFERRABLE INITIALLY DEFERRED')
        out.append(f"CREATE TABLE IF NOT EXISTS {t} ({', '.join(cols)})")
        out.append(f"CREATE INDEX IF NOT EXISTS {t}_rev ON {t} (rev)")
    out.append('CREATE INDEX IF NOT EXISTS history_ts ON history ("timestamp" DESC, id DESC)')
    out.append("CREATE UNIQUE INDEX IF NOT EXISTS users_shared ON users (shared) WHERE shared IS NOT NULL")
    return out


def init_db():
    with tx() as cur:
        write_lock(cur)
        for stmt in ddl():
            cur.execute(stmt)
        for t in TABLES:                                  # triggers (created once)
            for name, sql in ((f"{t}_rev_trg", f"BEFORE INSERT OR UPDATE ON {t} FOR EACH ROW EXECUTE FUNCTION sv_bump_rev()"),
                              (f"{t}_del_trg", f"AFTER DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION sv_tombstone()")):
                cur.execute("SELECT 1 FROM pg_trigger WHERE tgname = %s", (name,))
                if not cur.fetchone():
                    cur.execute(f"CREATE TRIGGER {name} {sql}")
        cur.execute("INSERT INTO meta (key, value) VALUES ('epoch', '1') ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO meta (key, value) VALUES ('secret_key', %s) ON CONFLICT DO NOTHING",
                    (secrets.token_hex(32),))
        now = dt.datetime.now(dt.timezone.utc)
        cur.execute("SELECT id FROM users WHERE shared = 'admin'")
        if not cur.fetchone():
            pw = os.environ.get("ADMIN_PASSWORD")
            cur.execute("""INSERT INTO users (id, name, email, role, shared, created_at, updated_at, username, password_hash)
                           VALUES (%s, 'Admin', '', 'admin', 'admin', %s, %s, %s, %s)""",
                        (new_id(), now, now, os.environ.get("ADMIN_USERNAME", "Admin"),
                         generate_password_hash(pw) if pw else LEGACY_ADMIN_HASH))
        cur.execute("SELECT id FROM users WHERE shared = 'staff'")
        if not cur.fetchone():
            pin = os.environ.get("STAFF_PIN", "")
            cur.execute("""INSERT INTO users (id, name, email, role, shared, created_at, updated_at, pin_hash)
                           VALUES (%s, 'Staff', '', 'staff', 'staff', %s, %s, %s)""",
                        (new_id() + 1, now, now, generate_password_hash(pin) if re.fullmatch(r"\d{4}", pin) else None))
        cur.execute("SELECT value FROM meta WHERE key = 'secret_key'")
        return cur.fetchone()["value"]


def meta_get(cur, key):
    cur.execute("SELECT value FROM meta WHERE key = %s", (key,))
    r = cur.fetchone()
    return r["value"] if r else None


def bump_epoch(cur):
    cur.execute("UPDATE meta SET value = (value::bigint + 1)::text WHERE key = 'epoch'")
    cur.execute("TRUNCATE tombstones")


def insert_rows(cur, t, rows, mode="upsert"):
    """rows: list of (id, vals dict, extra dict). mode: upsert | ignore | insert"""
    if not rows:
        return
    cols = [c for c, _ in SCHEMA[t]]
    names = ", ".join(f'"{c}"' for c in cols)
    sql = f"INSERT INTO {t} (id, {names}, extra) VALUES %s"
    if mode == "upsert":
        sql += " ON CONFLICT (id) DO UPDATE SET " + ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols) \
               + ", extra = EXCLUDED.extra"
    elif mode == "ignore":
        sql += " ON CONFLICT (id) DO NOTHING"
    data = [(rid, *[vals[c] for c in cols], Json(extra)) for rid, vals, extra in rows]
    psycopg2.extras.execute_values(cur, sql, data, page_size=500)


def log_server(cur, user_id, action, notes=""):
    insert_rows(cur, "history", [(new_id(), {c: None for c, _ in SCHEMA["history"]} | {
        "user_id": user_id, "action": action, "notes": notes, "timestamp": dt.datetime.now(dt.timezone.utc)}, {})])


def pull(cur, since, client_epoch, admin):
    epoch = meta_get(cur, "epoch")
    full = since <= 0 or str(client_epoch) != epoch
    changes, deleted = {}, {}
    for t in TABLES:
        if full and t == "history":
            cur.execute('SELECT * FROM history ORDER BY "timestamp" DESC NULLS LAST, id DESC LIMIT %s', (HISTORY_LIMIT,))
        elif full:
            cur.execute(f"SELECT * FROM {t}")
        else:
            cur.execute(f"SELECT * FROM {t} WHERE rev > %s", (since,))
        rows = [row_out(t, r, admin) for r in cur.fetchall()]
        if rows or full:
            changes[t] = rows
    if not full:
        cur.execute("SELECT tbl, id FROM tombstones WHERE rev > %s", (since,))
        for r in cur.fetchall():
            if r["tbl"] in COLTYPE and not any(x["id"] == r["id"] for x in changes.get(r["tbl"], [])):
                deleted.setdefault(r["tbl"], []).append(r["id"])
    parts = ", ".join(f"(SELECT COALESCE(max(rev), 0) FROM {t})" for t in TABLES + ["tombstones"])
    cur.execute(f"SELECT GREATEST({parts}) AS rev")
    return {"epoch": epoch, "rev": cur.fetchone()["rev"], "full": full, "changes": changes, "deleted": deleted}


def dump(cur, tables, admin=False):
    out = {}
    for t in tables:
        order = '"timestamp" DESC, id' if t == "history" else "id"
        cur.execute(f"SELECT * FROM {t} ORDER BY {order}")
        out[t] = [row_out(t, r, admin, with_rev=False) for r in cur.fetchall()]
    return out


def take_snapshot(cur, label, by):
    cur.execute("INSERT INTO snapshots (label, by_name, data) VALUES (%s, %s, %s)",
                (str(label)[:200], by, Json(dump(cur, HALL + ["users"]))))
    cur.execute("DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT %s)",
                (MAX_SNAPSHOTS,))


# ---------------- app ----------------
app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or init_db(),
    SESSION_COOKIE_NAME="skivalet",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=SESSION_HOURS),
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
)
if os.environ.get("SECRET_KEY"):
    init_db()


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


@app.errorhandler(ApiError)
def _api_error(e):
    return jsonify(error=e.message, **e.extra), e.status


@app.errorhandler(psycopg2.IntegrityError)
def _integrity(e):
    msg = "Duplicate value" if isinstance(e, psycopg2.errors.UniqueViolation) else "Broken link between records"
    detail = (getattr(e, "diag", None) and e.diag.message_detail) or ""
    return jsonify(error=f"{msg}. {detail}".strip(), reason="integrity"), 409


@app.before_request
def _csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        if not request.is_json:
            raise ApiError(415, "JSON body required")
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            raise ApiError(403, "Cross-site request blocked")


@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def body():
    return request.get_json(silent=True) or {}


def load_user(cur):
    uid = session.get("uid")
    if not uid:
        return None
    cur.execute("SELECT * FROM users WHERE id = %s", (uid,))
    u = cur.fetchone()
    if not u or u["archived"] or u["retired"]:
        return None
    if u["shared"] == "admin" and session.get("ver") != u["auth_ver"]:
        return None
    return u


def is_admin(u):
    return bool(u) and u["shared"] == "admin"


def auth(admin=False, repeatable=False):
    """Opens a transaction, loads the signed-in user, passes (cur, user)."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            with tx(repeatable=repeatable) as cur:
                u = load_user(cur)
                if not u:
                    session.clear()
                    raise ApiError(401, "Not signed in")
                if admin and not is_admin(u):
                    raise ApiError(403, "Admins only")
                return fn(cur, u, *a, **kw)
        return wrapper
    return deco


def client_ip():
    return request.remote_addr or "?"


def throttle(cur):
    cur.execute("SELECT count(*) AS n FROM login_failures WHERE ip = %s AND at > now() - interval '10 minutes'",
                (client_ip(),))
    if cur.fetchone()["n"] >= LOGIN_MAX_FAILS:
        raise ApiError(429, "Too many wrong attempts. Wait a few minutes.")


def record_fail(cur):
    cur.execute("INSERT INTO login_failures (ip) VALUES (%s)", (client_ip(),))
    cur.execute("DELETE FROM login_failures WHERE at < now() - interval '1 day'")


def start_session(u):
    session.clear()
    session.permanent = True
    session["uid"] = u["id"]
    if u["shared"] == "admin":
        session["ver"] = u["auth_ver"]


# ---------------- pages ----------------
def page(name):
    resp = send_from_directory(STATIC, name, max_age=0)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
@app.get("/index.html")
def index_page():
    return page("index.html")


@app.get("/admin")
@app.get("/admin.html")
def admin_page():
    return page("admin.html")


@app.get("/sync.js")
def sync_js():
    return page("sync.js")


@app.get("/healthz")
def healthz():
    with tx() as cur:
        cur.execute("SELECT 1")
    return "ok"


# ---------------- auth ----------------
@app.get("/api/me")
def me():
    with tx() as cur:
        u = load_user(cur)
        cur.execute("SELECT pin_hash FROM users WHERE shared = 'staff'")
        st = cur.fetchone()
        return jsonify(user=row_out("users", u, is_admin(u)) if u else None,
                       staff_pin_set=bool(st and st["pin_hash"]))


@app.post("/api/login/pin")
def login_pin():
    pin = str(body().get("pin") or "")
    with tx() as cur:
        throttle(cur)
        cur.execute("SELECT * FROM users WHERE shared = 'staff'")
        st = cur.fetchone()
        if not st or not st["pin_hash"]:
            raise ApiError(400, "The staff PIN is not set yet. An admin has to set it under Access.")
        if not re.fullmatch(r"\d{4}", pin) or not check_password_hash(st["pin_hash"], pin):
            record_fail(cur)
            return jsonify(error="Wrong PIN"), 400
        cur.execute("""SELECT id, name FROM users WHERE shared IS NULL AND NOT archived AND NOT retired
                       ORDER BY lower(name)""")
        members = cur.fetchall()
        if not members:                                   # no names yet -> generic "Staff"
            start_session(st)
            return jsonify(user=row_out("users", st))
        session.clear()
        session["pin_ok"] = time.time()
        return jsonify(members=members)


@app.post("/api/login/member")
def login_member():
    if time.time() - float(session.get("pin_ok") or 0) > 300:
        session.clear()
        raise ApiError(400, "Enter the staff PIN again")
    mid = valid_id(body().get("id"))
    with tx() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s AND shared IS NULL AND NOT archived AND NOT retired", (mid,))
        u = cur.fetchone()
        if not u:
            raise ApiError(400, "That name is no longer available")
        start_session(u)
        return jsonify(user=row_out("users", u))


@app.post("/api/login/admin")
def login_admin():
    b = body()
    username, password = str(b.get("username") or "").strip(), str(b.get("password") or "")
    with tx() as cur:
        throttle(cur)
        cur.execute("SELECT * FROM users WHERE shared = 'admin'")
        a = cur.fetchone()
        ok = a and a["password_hash"] and username.lower() == str(a["username"] or "").lower() \
            and check_password_hash(a["password_hash"], password)
        if not ok:
            record_fail(cur)
            return jsonify(error="Wrong username or password"), 400
        start_session(a)
        return jsonify(user=row_out("users", a, admin=True))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/admin/pin")
@auth(admin=True)
def set_pin(cur, u):
    pin = str(body().get("pin") or "")
    if not re.fullmatch(r"\d{4}", pin):
        raise ApiError(400, "The PIN must be exactly 4 digits")
    write_lock(cur)
    cur.execute("UPDATE users SET pin_hash = %s, updated_at = now() WHERE shared = 'staff'",
                (generate_password_hash(pin),))
    return jsonify(ok=True)


@app.post("/api/admin/login")
@auth(admin=True)
def set_admin_login(cur, u):
    b = body()
    current, username = str(b.get("current") or ""), str(b.get("username") or "").strip()
    p1, p2 = str(b.get("password") or ""), str(b.get("password2") or "")
    if not check_password_hash(u["password_hash"], current):
        raise ApiError(400, "Current password is wrong")
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", username):
        raise ApiError(400, "Username: 3–32 letters, numbers, . _ -")
    if len(p1) < 8:
        raise ApiError(400, "Password must be at least 8 characters")
    if p1 != p2:
        raise ApiError(400, "The passwords do not match")
    write_lock(cur)
    cur.execute("""UPDATE users SET username = %s, password_hash = %s, auth_ver = auth_ver + 1, updated_at = now()
                   WHERE id = %s RETURNING auth_ver""", (username, generate_password_hash(p1), u["id"]))
    session["ver"] = cur.fetchone()["auth_ver"]          # other admin sessions are signed out
    return jsonify(ok=True)


# ---------------- sync ----------------
@app.get("/api/sync")
@auth(repeatable=True)
def sync_get(cur, u):
    since = to_int(request.args.get("since")) or 0
    return jsonify(pull(cur, since, request.args.get("epoch"), is_admin(u)))


@app.post("/api/sync")
@auth()
def sync_post(cur, u):
    b = body()
    since = to_int(b.get("since")) or 0
    ups, dels = b.get("upserts") or {}, b.get("deletes") or {}
    admin = is_admin(u)
    if not isinstance(ups, dict) or not isinstance(dels, dict):
        raise ApiError(400, "Bad sync payload")
    if not admin and (ups.get("users") or dels.get("users")):
        raise ApiError(403, "Only the admin can change staff members")

    write_lock(cur)
    if str(b.get("epoch")) != meta_get(cur, "epoch"):
        raise ApiError(409, "Data was replaced on the server", reason="epoch")
    if admin and b.get("snapshot"):
        take_snapshot(cur, b["snapshot"], u["name"])

    # 1. validate + detect changes made by other devices since this client read them
    plan, conflicts = {}, []
    for t in TABLES:
        rows = [r for r in (ups.get(t) or []) if isinstance(r, dict) and valid_id(r.get("id"))]
        drops = [d for d in (dels.get(t) or []) if isinstance(d, dict) and valid_id(d.get("id"))]
        if t == "history":
            drops = []                                    # history is append-only through sync
        plan[t] = (rows, drops)
        ids = [valid_id(r["id"]) for r in rows] + [valid_id(d["id"]) for d in drops]
        if not ids or t == "history":
            continue
        cur.execute(f"SELECT id, rev, {'shared, role' if t == 'users' else 'NULL AS shared'} FROM {t} "
                    "WHERE id = ANY(%s) FOR UPDATE", (ids,))
        have = {r["id"]: r for r in cur.fetchall()}
        for r in rows:
            base, cur_row = to_int(r.get("_rev")), have.get(valid_id(r["id"]))
            if (base is None and cur_row) or (base is not None and (not cur_row or cur_row["rev"] != base)):
                conflicts.append(f"{t}#{r['id']}")
        for d in drops:
            base, cur_row = to_int(d.get("_rev")), have.get(valid_id(d["id"]))
            if cur_row and base is not None and cur_row["rev"] != base:
                conflicts.append(f"{t}#{d['id']}")
        plan[t] = (rows, drops, have)
    if conflicts:
        raise ApiError(409, "Another device changed the same records", reason="conflict", ids=conflicts[:20])

    # 2. apply: upserts parents-first, deletes children-first (FKs are deferred anyway)
    for t in TABLES:
        rows = plan[t][0]
        have = plan[t][2] if len(plan[t]) > 2 else {}
        prepared = []
        for r in rows:
            rid = valid_id(r["id"])
            vals, extra = row_in(t, r)
            if t == "users":
                old = have.get(rid)
                if old and old["shared"]:                 # shared accounts: only name/email editable
                    vals.update(shared=old["shared"], role=old["role"], archived=False, retired=False)
                else:
                    vals.update(shared=None, role="staff")
            if t == "history":
                vals["user_id"] = u["id"]                 # history always records the signed-in account
                if vals["timestamp"] is None:
                    vals["timestamp"] = dt.datetime.now(dt.timezone.utc)
            prepared.append((rid, vals, extra))
        insert_rows(cur, t, prepared, "ignore" if t == "history" else "upsert")
    for t in reversed(TABLES):
        drops = plan[t][1]
        ids = [valid_id(d["id"]) for d in drops]
        if not ids:
            continue
        if t == "users":                                   # shared accounts cannot be deleted
            cur.execute("UPDATE users SET updated_at = updated_at WHERE id = ANY(%s) AND shared IS NOT NULL", (ids,))
            cur.execute("DELETE FROM users WHERE id = ANY(%s) AND shared IS NULL", (ids,))
        else:
            cur.execute(f"DELETE FROM {t} WHERE id = ANY(%s)", (ids,))
    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")           # surface FK / unique problems as 409 now
    return jsonify(pull(cur, since, b.get("epoch"), admin))


# ---------------- admin data tools ----------------
def check_collections(data, need_all=False):
    if not isinstance(data, dict):
        raise ApiError(400, "Top level must be an object")
    for t in TABLES:
        v = data.get(t, [])
        if not isinstance(v, list):
            raise ApiError(400, f'"{t}" must be an array')
        for x in v:
            if not isinstance(x, dict) or not valid_id(x.get("id")):
                raise ApiError(400, f"Every {t} record needs a numeric id")


def replace_all(cur, data, keep_history, actor, label, notes):
    check_collections(data)
    write_lock(cur)
    take_snapshot(cur, label, actor["name"])
    cur.execute("SELECT id, shared FROM users WHERE shared IS NOT NULL")
    shared_now = {r["shared"]: r["id"] for r in cur.fetchall()}
    cur.execute("TRUNCATE equipment, cards, storages, rooms")
    cur.execute("DELETE FROM users WHERE shared IS NULL")

    def uniq(rows):
        seen = {}
        for r in rows:
            seen[valid_id(r["id"])] = r
        return seen

    idmap, users = {}, []
    for rid, r in uniq(data.get("users", [])).items():
        if r.get("shared") in shared_now:
            idmap[rid] = shared_now[r["shared"]]
        elif rid not in shared_now.values():
            vals, extra = row_in("users", r)
            vals.update(shared=None, role="staff")
            users.append((rid, vals, extra))
    insert_rows(cur, "users", users, "insert")

    present = {}
    for t in HALL:
        rows = uniq(data.get(t, []))
        present[t] = set(rows)
        prepared = []
        for rid, r in rows.items():
            vals, extra = row_in(t, r)
            for (tt, c), ref in REFS.items():             # drop links to records that are not in the file
                if tt == t and vals[c] is not None and vals[c] not in present.get(ref, set()):
                    vals[c] = None
            prepared.append((rid, vals, extra))
        insert_rows(cur, t, prepared, "insert")
    if not keep_history:
        cur.execute("TRUNCATE history")
        hist = []
        for rid, r in uniq(data.get("history", [])).items():
            vals, extra = row_in("history", r)
            vals["user_id"] = idmap.get(vals["user_id"], vals["user_id"])
            hist.append((rid, vals, extra))
        insert_rows(cur, "history", hist, "insert")
    log_server(cur, actor["id"], "[Admin] " + label, notes)
    bump_epoch(cur)


@app.get("/api/admin/export")
@auth(admin=True, repeatable=True)
def export(cur, u):
    return jsonify(skivalet_backup=1, exported_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                   data=dump(cur, TABLES))


@app.post("/api/admin/replace")
@auth(admin=True)
def replace(cur, u):
    b = body()
    data = b.get("data")
    if isinstance(data, dict) and data.get("skivalet_backup") and isinstance(data.get("data"), dict):
        data = data["data"]
    replace_all(cur, data, bool(b.get("keep_history")), u, str(b.get("label") or "Data replaced"),
                str(b.get("notes") or ""))
    return jsonify(ok=True)


@app.post("/api/admin/clear-history")
@auth(admin=True)
def clear_history(cur, u):
    write_lock(cur)
    cur.execute("TRUNCATE history")
    log_server(cur, u["id"], "[Admin] History cleared")
    bump_epoch(cur)
    return jsonify(ok=True)


@app.get("/api/admin/snapshots")
@auth(admin=True)
def snapshots(cur, u):
    cur.execute("SELECT id, label, by_name, created_at FROM snapshots ORDER BY id DESC")
    return jsonify(snapshots=[{"id": r["id"], "label": r["label"], "by": r["by_name"],
                               "ts": int(r["created_at"].timestamp() * 1000)} for r in cur.fetchall()])


@app.get("/api/admin/snapshots/<int:sid>")
@auth(admin=True)
def snapshot_get(cur, u, sid):
    cur.execute("SELECT data FROM snapshots WHERE id = %s", (sid,))
    r = cur.fetchone()
    if not r:
        raise ApiError(404, "Snapshot not found")
    return jsonify(r["data"])


@app.post("/api/admin/snapshots/<int:sid>/restore")
@auth(admin=True)
def snapshot_restore(cur, u, sid):
    cur.execute("SELECT label, data FROM snapshots WHERE id = %s", (sid,))
    r = cur.fetchone()
    if not r:
        raise ApiError(404, "Snapshot not found")
    replace_all(cur, r["data"], True, u, "Snapshot restored", r["label"])
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
