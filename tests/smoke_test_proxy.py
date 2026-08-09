"""Smoke test for the reverse-proxy fix (CLAUDE.md rules).

Behind nginx every request arrives from 127.0.0.1. ProxyFix makes
request.remote_addr the real caller, which matters twice over: the audit
log records who actually did something, and the rate limiter gives each
client its own bucket instead of pooling the whole internet into one.

Covers: exactly one hop is trusted (a forged X-Forwarded-For prefix is
ignored), the audit log stores the real IP, the rate limiter keys on it,
X-Forwarded-Proto gives https URLs, and nothing changes when the header
is absent.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_proxy.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_proxy.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

from app import app, db, AuditLog, User, _rate_buckets  # noqa: E402

app.config["TESTING"] = True

PW = "proxy-test-password"
PROXY = {"REMOTE_ADDR": "127.0.0.1"}      # what nginx looks like to gunicorn
REAL = "203.0.113.45"                     # the actual visitor
OTHER = "198.51.100.7"                    # a different visitor

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def via_proxy(forwarded_for=None, proto=None, host=None):
    """A WSGI environ as nginx would hand it to gunicorn."""
    env = dict(PROXY)
    if forwarded_for:
        env["HTTP_X_FORWARDED_FOR"] = forwarded_for
    if proto:
        env["HTTP_X_FORWARDED_PROTO"] = proto
    if host:
        env["HTTP_X_FORWARDED_HOST"] = host
    return env


def failed_login(client, env, email="nobody@example.com"):
    _rate_buckets.clear()
    return client.post("/admin/login",
                       data={"email": email, "password": "wrong"},
                       environ_overrides=env)


def last_ip():
    with app.app_context():
        e = (AuditLog.query.filter_by(action="login_failed")
             .order_by(AuditLog.id.desc()).first())
        return e.ip if e else None


with app.app_context():
    db.create_all()
    u = User(email="admin@example.com")
    u.set_password(PW)
    db.session.add(u)
    db.session.commit()

client = app.test_client()

# ---- the middleware is installed, and trusts exactly one hop
check("ProxyFix wraps the WSGI app", isinstance(app.wsgi_app, ProxyFix))
check("trusts one hop for X-Forwarded-For", app.wsgi_app.x_for == 1,
      str(app.wsgi_app.x_for))
check("trusts one hop for X-Forwarded-Proto", app.wsgi_app.x_proto == 1,
      str(app.wsgi_app.x_proto))
check("trusts one hop for X-Forwarded-Host", app.wsgi_app.x_host == 1,
      str(app.wsgi_app.x_host))
check("does not trust forwarded port or prefix",
      app.wsgi_app.x_port == 0 and app.wsgi_app.x_prefix == 0)

# ---- the audit log records the real client, not the proxy
failed_login(client, via_proxy(REAL))
check("audit log records the forwarded client IP", last_ip() == REAL,
      repr(last_ip()))
check("audit log no longer records the proxy", last_ip() != "127.0.0.1")

# ---- a forged chain does not win: only nginx's own entry is trusted
# (nginx appends the real IP, so the caller's forgery sits to the left)
failed_login(client, via_proxy("9.9.9.9, %s" % REAL))
check("forged X-Forwarded-For prefix ignored", last_ip() == REAL,
      repr(last_ip()))
failed_login(client, via_proxy("10.0.0.1, 172.16.0.1, %s" % OTHER))
check("only the last hop is trusted in a longer chain", last_ip() == OTHER,
      repr(last_ip()))

# ---- with no forwarded header nothing changes (direct/local requests)
failed_login(client, {"REMOTE_ADDR": "192.0.2.99"})
check("no X-Forwarded-For leaves REMOTE_ADDR alone",
      last_ip() == "192.0.2.99", repr(last_ip()))

# ---- THE SECURITY FIX: rate limiting is per real client, not shared
# Five failures from one visitor must not lock out a different visitor
# arriving through the same proxy.
_rate_buckets.clear()
for _ in range(5):
    client.post("/admin/login",
                data={"email": "admin@example.com", "password": "wrong"},
                environ_overrides=via_proxy(REAL))
r = client.post("/admin/login",
                data={"email": "admin@example.com", "password": PW},
                environ_overrides=via_proxy(REAL))
check("6th attempt from the same client is blocked",
      r.status_code == 200 and b"Too many login attempts" in r.data)
check("bucket is keyed on the real IP, not the proxy",
      any(key[1] == REAL for key in _rate_buckets),
      str(sorted(str(k) for k in _rate_buckets)))
check("no bucket was keyed on 127.0.0.1",
      not any(key[1] == "127.0.0.1" for key in _rate_buckets),
      str(sorted(str(k) for k in _rate_buckets)))

other_client = app.test_client()
r = other_client.post("/admin/login",
                      data={"email": "admin@example.com", "password": PW},
                      environ_overrides=via_proxy(OTHER))
check("a different client through the same proxy is NOT locked out",
      r.status_code == 302 and "/admin" in r.headers.get("Location", ""),
      "%s -> %s" % (r.status_code, r.headers.get("Location", "")))
check("that login really worked",
      other_client.get("/admin", environ_overrides=via_proxy(OTHER)
                       ).status_code == 200)
other_client.get("/admin/logout", environ_overrides=via_proxy(OTHER))

# a forged header must not let one client escape its own bucket
_rate_buckets.clear()
for _ in range(5):
    client.post("/admin/login",
                data={"email": "admin@example.com", "password": "wrong"},
                environ_overrides=via_proxy("1.2.3.4, %s" % REAL))
r = client.post("/admin/login",
                data={"email": "admin@example.com", "password": PW},
                environ_overrides=via_proxy("5.6.7.8, %s" % REAL))
check("spoofing the header does not buy a fresh rate-limit bucket",
      r.status_code == 200 and b"Too many login attempts" in r.data)
_rate_buckets.clear()

# ---- X-Forwarded-Proto makes url_for generate https
r = client.get("/sitemap.xml", environ_overrides=via_proxy(REAL, proto="https",
                                                           host="ebwa.org.uk"))
body = r.data.decode("utf-8")
check("sitemap URLs use https behind a TLS proxy",
      "https://ebwa.org.uk/" in body, body[:200])
check("no http:// URLs leaked into the sitemap",
      "http://ebwa.org.uk" not in body.replace("https://ebwa.org.uk", ""))

r = client.get("/robots.txt", environ_overrides=via_proxy(REAL, proto="https",
                                                          host="ebwa.org.uk"))
check("robots.txt sitemap line uses https",
      b"https://ebwa.org.uk/sitemap.xml" in r.data, r.data.decode("utf-8"))

# without the proto header we should still be plain http (no false https)
r = client.get("/robots.txt", environ_overrides=via_proxy(REAL))
check("no X-Forwarded-Proto leaves the scheme as http",
      b"http://" in r.data and b"https://" not in r.data)

# ---- ordinary pages still work through the proxy
for path in ("/", "/about", "/events", "/healthz"):
    r = client.get(path, environ_overrides=via_proxy(REAL, proto="https"))
    check("GET %s through the proxy -> 200" % path, r.status_code == 200,
          str(r.status_code))

# ---- teardown: delete the throwaway db (incl. WAL sidecars)
with app.app_context():
    db.session.remove()
    db.engine.dispose()
for suffix in ("", "-wal", "-shm"):
    f = TEST_DB + suffix
    if os.path.isfile(f):
        os.remove(f)
check("test db deleted", not os.path.exists(TEST_DB))

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
