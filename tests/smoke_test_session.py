"""Smoke test for idle admin session expiry (CLAUDE.md rules).

Covers: the 20-minute idle window is configured and the session is
permanent, continued activity keeps someone logged in indefinitely, an
idle session expires and the next request lands on the login page with a
clear flash, a visitor who never logged in gets the ordinary message
instead, and the 5-minute 2FA hand-off window is untouched.

Expiry is exercised by shortening PERMANENT_SESSION_LIFETIME for a few
seconds rather than waiting twenty minutes; the real value is asserted
separately.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_session.py
"""
import os
import sys
import time
from datetime import timedelta

import pyotp

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_session.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, IDLE_SESSION_MINUTES, PENDING_2FA_MAX_AGE,  # noqa: E402
                 Subscriber, User, _rate_buckets)

app.config["TESTING"] = True

PW = "session-test-password"
REAL_LIFETIME = app.config["PERMANENT_SESSION_LIFETIME"]

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def login(client, email="admin@example.com", password=PW):
    _rate_buckets.clear()
    return client.post("/admin/login", data={"email": email,
                                             "password": password})


def short_lifetime(seconds):
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(seconds=seconds)


with app.app_context():
    db.create_all()
    u = User(email="admin@example.com")
    u.set_password(PW)
    db.session.add(u)
    tf = User(email="twofactor@example.com")
    tf.set_password(PW)
    tf.totp_secret = pyotp.random_base32()
    tf.totp_enabled = True
    db.session.add(tf)
    db.session.commit()
    tf_secret = tf.totp_secret

# ---- the configured window is 20 minutes, idle-based
check("idle window is 20 minutes", IDLE_SESSION_MINUTES == 20,
      str(IDLE_SESSION_MINUTES))
check("PERMANENT_SESSION_LIFETIME matches",
      REAL_LIFETIME == timedelta(minutes=20), str(REAL_LIFETIME))
check("session cookie refreshed on every request (idle, not absolute)",
      app.config["SESSION_REFRESH_EACH_REQUEST"] is True)

# ---- logging in creates a permanent (expiring) session cookie
client = app.test_client()
r = login(client)
check("login -> 302", r.status_code == 302, str(r.status_code))
set_cookie = r.headers.get("Set-Cookie", "")
check("session cookie is permanent, so it carries an expiry",
      "Expires=" in set_cookie or "Max-Age=" in set_cookie, set_cookie[:120])
check("logged in", client.get("/admin").status_code == 200)

# ---- the cookie is re-issued on each request, restarting the clock
r = client.get("/admin")
check("session cookie re-sent on an ordinary request",
      "session=" in r.headers.get("Set-Cookie", ""),
      r.headers.get("Set-Cookie", "")[:120])
client.get("/admin/logout")

# ---- continued activity does NOT log you out, even past the window
# A 3s window with a request every second: the total far exceeds the
# window, but no single gap does.
short_lifetime(3)
active = app.test_client()
login(active)
started = time.time()
kept = True
for _ in range(6):                 # 6 x 1s = 6s across a 3s window
    time.sleep(1.0)
    if active.get("/admin").status_code != 200:
        kept = False
        break
elapsed = time.time() - started
check("active session survives well past the idle window",
      kept and elapsed > 5, "still logged in after %.1fs" % elapsed)
check("and is still logged in at the end",
      active.get("/admin").status_code == 200)

# ---- going idle expires it, with a clear flash on the login page
short_lifetime(1)
idle = app.test_client()
login(idle)
check("logged in before going idle", idle.get("/admin").status_code == 200)
time.sleep(3)                      # comfortably past the 1s window
r = idle.get("/admin")
check("idle session -> 302", r.status_code == 302, str(r.status_code))
check("idle session redirects to the login page",
      "/admin/login" in r.headers.get("Location", ""),
      r.headers.get("Location", ""))
r = idle.get("/admin", follow_redirects=True)
check("expiry explains itself on the login page",
      b"Your session has expired, please log in again." in r.data,
      r.data.decode("utf-8")[:400])
check("it is not a bare redirect", b"flash" in r.data)
check("the expired session really is logged out",
      idle.get("/admin").status_code == 302)

# a POST to an admin route expires the same way
idle2 = app.test_client()
login(idle2)
time.sleep(3)
r = idle2.post("/admin/testimonials/new", data={"name": "X", "quote": "Y"},
               follow_redirects=True)
check("an expired POST also lands on the login page with the message",
      b"Your session has expired" in r.data)

# ---- someone who never logged in gets the ordinary message
stranger = app.test_client()
r = stranger.get("/admin", follow_redirects=True)
check("a first-time visitor is not told their session expired",
      b"Your session has expired" not in r.data)
check("but is still told to log in", b"Please log in to continue." in r.data)

# an anonymous visitor who picked up a session cookie from a flash
# message must not be told their session expired either
passerby = app.test_client()
passerby.post("/subscribe", data={"email": "hello@example.org"},
              follow_redirects=True)
with app.app_context():
    check("the flash really did happen",
          Subscriber.query.filter_by(email="hello@example.org").count() == 1)
r = passerby.get("/admin", follow_redirects=True)
check("a flash-only session cookie is not mistaken for an expiry",
      b"Your session has expired" not in r.data
      and b"Please log in to continue." in r.data)

# ---- logging out is not reported as an expiry
out = app.test_client()
login(out)
out.get("/admin/logout")
r = out.get("/admin", follow_redirects=True)
check("logging out is not reported as an expiry",
      b"Your session has expired" not in r.data)

# ---- the 2FA hand-off window is untouched by any of this
check("2FA hand-off window is still 5 minutes",
      PENDING_2FA_MAX_AGE == 300, str(PENDING_2FA_MAX_AGE))
check("hand-off is shorter than the idle window, so it decides first",
      PENDING_2FA_MAX_AGE < REAL_LIFETIME.total_seconds(),
      "%ds vs %ds" % (PENDING_2FA_MAX_AGE, REAL_LIFETIME.total_seconds()))

app.config["PERMANENT_SESSION_LIFETIME"] = REAL_LIFETIME
tfa = app.test_client()
r = login(tfa, "twofactor@example.com")
check("2FA login still stops at the code step",
      r.status_code == 302 and "/admin/login/2fa" in r.headers.get("Location", ""),
      r.headers.get("Location", ""))
check("the code step still renders",
      tfa.get("/admin/login/2fa").status_code == 200)
r = tfa.post("/admin/login/2fa",
             data={"code": pyotp.TOTP(tf_secret).at(int(time.time()))})
check("2FA login completes", r.status_code == 302, str(r.status_code))
check("and lands in an admin session", tfa.get("/admin").status_code == 200)
check("2FA session is permanent too",
      "Expires=" in tfa.get("/admin").headers.get("Set-Cookie", "")
      or "Max-Age=" in tfa.get("/admin").headers.get("Set-Cookie", ""))
tfa.get("/admin/logout")

# a 2FA session goes idle on the same terms
short_lifetime(1)
tfa2 = app.test_client()
login(tfa2, "twofactor@example.com")
with app.app_context():
    User.query.filter_by(email="twofactor@example.com").first() \
        .totp_last_counter = None
    db.session.commit()
tfa2.post("/admin/login/2fa",
          data={"code": pyotp.TOTP(tf_secret).at(int(time.time()))})
check("2FA user is logged in", tfa2.get("/admin").status_code == 200)
time.sleep(3)
r = tfa2.get("/admin", follow_redirects=True)
check("a 2FA session expires on the same terms",
      b"Your session has expired" in r.data)

app.config["PERMANENT_SESSION_LIFETIME"] = REAL_LIFETIME

# ---- public pages are unaffected by any of this
for path in ("/", "/about", "/events", "/contact", "/privacy"):
    r = app.test_client().get(path)
    check("public GET %s unaffected" % path, r.status_code == 200,
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
