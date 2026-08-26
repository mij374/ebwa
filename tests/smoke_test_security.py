"""Pre-launch security smoke test (CLAUDE.md testing rules).

Covers: security headers, rate limiting on login/subscribe/donation
endpoints, debug off when imported (the gunicorn path), /healthz, and an
introspection sweep of the URL map asserting every /admin route (except
the login page itself) redirects anonymous users to the login page.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_security.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import app, db, User, Subscriber, _rate_buckets  # noqa: E402

app.config["TESTING"] = True

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    db.create_all()
    u = User(email="test@example.com")
    u.set_password("pw123456")
    db.session.add(u)
    db.session.commit()

client = app.test_client()

# ---- debug must be off on import (how gunicorn loads app:app)
check("app.debug is False on import", app.debug is False)

# ---- health check
r = client.get("/healthz")
check("GET /healthz -> 200 ok", r.status_code == 200 and r.data == b"ok")

# ---- security headers on public and admin responses
for path in ("/", "/donate", "/admin/login"):
    r = client.get(path)
    csp = r.headers.get("Content-Security-Policy", "")
    check("%s has CSP" % path, "default-src 'self'" in csp)
    # The fonts are ours now, and the policy has to say so — leaving the
    # domains in after removing the links would quietly re-permit the
    # thing that was removed.
    check("%s CSP no longer allows a font host" % path,
          "fonts.googleapis.com" not in csp
          and "fonts.gstatic.com" not in csp, csp)
    check("%s CSP says fonts come from here" % path,
          "font-src 'self';" in csp, csp)
    check("%s CSP says styles come from here" % path,
          "style-src 'self' 'unsafe-inline';" in csp, csp)
    check("%s CSP allows the maps embed" % path,
          "frame-src https://www.google.com" in csp)
    check("%s has nosniff" % path,
          r.headers.get("X-Content-Type-Options") == "nosniff")
    check("%s has Referrer-Policy" % path,
          r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin")

# ---- URL-map introspection: every /admin route requires login
admin_rules = [rule for rule in app.url_map.iter_rules()
               if rule.rule.startswith("/admin")
               and rule.endpoint != "admin_login"]
check("introspection found a plausible number of admin routes",
      len(admin_rules) >= 25, str(len(admin_rules)))
anon = app.test_client()   # fresh client, definitely not logged in
for rule in admin_rules:
    url = re.sub(r"<[^>]+>", "1", rule.rule)
    method = "GET" if "GET" in rule.methods else "POST"
    r = anon.open(url, method=method)
    ok = r.status_code == 302 and "/admin/login" in r.headers.get("Location", "")
    check("anon %s %s -> login redirect" % (method, rule.rule), ok,
          "%s -> %s" % (r.status_code, r.headers.get("Location", "")))

# ---- rate limiting: login (5 per window per IP)
_rate_buckets.clear()
env = {"REMOTE_ADDR": "203.0.113.10"}
for _ in range(5):
    client.post("/admin/login", data={"email": "test@example.com",
                                      "password": "wrong"},
                environ_overrides=env)
r = client.post("/admin/login", data={"email": "test@example.com",
                                      "password": "pw123456"},
                environ_overrides=env)
check("6th login attempt blocked even with correct password",
      r.status_code == 200 and b"Too many login attempts" in r.data)
r = client.get("/admin", environ_overrides=env)
check("blocked attempt did not log the user in", r.status_code == 302)
r = client.post("/admin/login", data={"email": "test@example.com",
                                      "password": "pw123456"},
                environ_overrides={"REMOTE_ADDR": "203.0.113.99"})
check("different IP can still log in", r.status_code == 302
      and "/admin" in r.headers.get("Location", ""))
client.get("/admin/logout", environ_overrides={"REMOTE_ADDR": "203.0.113.99"})

# ---- rate limiting: subscribe (5 per window per IP)
_rate_buckets.clear()
env = {"REMOTE_ADDR": "203.0.113.20"}
for i in range(5):
    client.post("/subscribe", data={"email": "s%d@example.org" % i},
                environ_overrides=env)
client.post("/subscribe", data={"email": "blocked@example.org"},
            environ_overrides=env)
with app.app_context():
    check("6th subscribe blocked",
          Subscriber.query.filter_by(email="blocked@example.org").count() == 0)
    check("first five subscribes stored", Subscriber.query.count() == 5)

# ---- rate limiting: donation endpoints (10 per window per IP, shared)
_rate_buckets.clear()
env = {"REMOTE_ADDR": "203.0.113.30"}
from unittest.mock import patch  # noqa: E402
with patch("app.stripe.checkout.Session.create") as create:
    for _ in range(10):
        client.post("/donate", data={"amount": "bad"}, environ_overrides=env)
    r = client.post("/donate", data={"amount": "10", "name": "A",
                                     "email": "a@b.c"},
                    environ_overrides=env, follow_redirects=True)
    check("11th donation attempt blocked before Stripe",
          create.call_count == 0 and b"Too many attempts" in r.data)
_rate_buckets.clear()

# ---- no behaviour change to public pages
for path in ("/", "/about", "/events", "/news", "/resources", "/gallery",
             "/membership", "/donate", "/contact", "/sitemap.xml"):
    r = client.get(path)
    check("GET %s still 200" % path, r.status_code == 200,
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
