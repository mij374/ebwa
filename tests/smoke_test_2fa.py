"""Smoke test for optional TOTP two-factor authentication (CLAUDE.md rules).

Covers: enrolment needs a valid code, login with 2FA on requires the code
before any session exists, wrong/expired/replayed codes are rejected,
recovery codes work exactly once, users without 2FA log in normally, and
disabling needs a code.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_2fa.py
"""
import os
import re
import sys
import time

import pyotp

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_2fa.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, RecoveryCode, User, _rate_buckets,  # noqa: E402
                 RECOVERY_CODE_COUNT)

app.config["TESTING"] = True

PW = "correct-horse-battery"

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def user(email):
    return User.query.filter_by(email=email).first()


def code_now(secret, offset=0):
    """A code for the 30-second step `offset` steps from now."""
    totp = pyotp.TOTP(secret)
    return totp.at(int(time.time()) + offset * totp.interval)


def wait_for_next_code(email):
    """Stand in for the user waiting for their app to roll to the next
    code. Codes are single-use, so without this the test would be
    replaying the one it just spent — which is exactly what the replay
    check below asserts is refused."""
    with app.app_context():
        user(email).totp_last_counter = None
        db.session.commit()


with app.app_context():
    db.create_all()
    for email in ("with2fa@example.com", "plain@example.com"):
        u = User(email=email)
        u.set_password(PW)
        db.session.add(u)
    db.session.commit()

client = app.test_client()

# ---- a user without 2FA logs straight in (nothing changed for them)
r = client.post("/admin/login", data={"email": "plain@example.com",
                                      "password": PW})
check("no 2FA: login goes straight to the dashboard",
      r.status_code == 302 and "/admin" in r.headers.get("Location", "")
      and "2fa" not in r.headers.get("Location", ""),
      r.headers.get("Location", ""))
check("no 2FA: session really created",
      client.get("/admin").status_code == 200)
r = client.get("/admin/account")
check("no 2FA: account page offers to set it up",
      b"/admin/account/2fa/enable" in r.data)
client.get("/admin/logout")

# ---- the second-step URL is useless without a live hand-off
for method in ("GET", "POST"):
    r = client.open("/admin/login/2fa", method=method)
    check("anon %s /admin/login/2fa -> login redirect" % method,
          r.status_code == 302
          and "/admin/login" in r.headers.get("Location", ""),
          "%s -> %s" % (r.status_code, r.headers.get("Location", "")))

# ---- enrolment: anonymous access is refused
r = client.get("/admin/account/2fa/enable")
check("anon GET /admin/account/2fa/enable -> login redirect",
      r.status_code == 302
      and "/admin/login" in r.headers.get("Location", ""), str(r.status_code))

# ---- enrolment page issues a secret and a QR, but does not enable yet
_rate_buckets.clear()
client.post("/admin/login", data={"email": "with2fa@example.com",
                                  "password": PW})
r = client.get("/admin/account/2fa/enable")
check("GET enrolment page -> 200", r.status_code == 200, str(r.status_code))
html = r.data.decode("utf-8")
check("QR rendered as an inline data URI",
      'src="data:image/svg+xml;base64,' in html)
with app.app_context():
    secret = user("with2fa@example.com").totp_secret
    check("secret stored server-side on the user", bool(secret), repr(secret))
    check("not enabled until a code confirms it",
          user("with2fa@example.com").totp_enabled is False)
check("secret shown on the enrolment page for manual entry", secret in html)

# ---- enrolment rejects a wrong code
r = client.post("/admin/account/2fa/enable", data={"code": "000000"},
                follow_redirects=True)
check("enrolment with a wrong code -> error", b"code was not right" in r.data)
with app.app_context():
    check("wrong code did not enable 2FA",
          user("with2fa@example.com").totp_enabled is False)
    check("no recovery codes issued yet", RecoveryCode.query.count() == 0)

# ---- enrolment with a valid code turns it on and shows recovery codes once
r = client.post("/admin/account/2fa/enable",
                data={"code": code_now(secret)})
check("enrolment with a valid code -> 200 codes page", r.status_code == 200,
      str(r.status_code))
html = r.data.decode("utf-8")
codes = re.findall(r"<li>([a-z0-9]{4}-[a-z0-9]{4})</li>", html)
check("recovery codes shown once", len(codes) == RECOVERY_CODE_COUNT,
      "%d shown" % len(codes))
with app.app_context():
    check("2FA now enabled", user("with2fa@example.com").totp_enabled is True)
    check("recovery codes stored hashed, not in clear",
          RecoveryCode.query.count() == RECOVERY_CODE_COUNT
          and all(c not in rc.code_hash
                  for rc in RecoveryCode.query.all() for c in codes))
r = client.get("/admin/account")
check("account page reports 2FA on with codes left",
      b"Turn off two-factor" in r.data
      and str(RECOVERY_CODE_COUNT).encode() in r.data)
client.get("/admin/logout")

# ---- login now stops at the code step, with no session created
wait_for_next_code("with2fa@example.com")
_rate_buckets.clear()
r = client.post("/admin/login", data={"email": "with2fa@example.com",
                                      "password": PW})
check("correct password -> redirected to the code step",
      r.status_code == 302 and "/admin/login/2fa" in r.headers.get("Location", ""),
      r.headers.get("Location", ""))
r = client.get("/admin")
check("no session yet after only the password",
      r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
      str(r.status_code))
r = client.get("/admin/login/2fa")
check("code step renders", r.status_code == 200 and b"code" in r.data.lower(),
      str(r.status_code))
check("secret is never sent to the browser at login",
      secret.encode() not in r.data)

# ---- a wrong code is rejected and still creates no session
r = client.post("/admin/login/2fa", data={"code": "000000"})
check("wrong code at login -> stays on the form", r.status_code == 200,
      str(r.status_code))
check("wrong code -> error shown", b"code was not right" in r.data)
check("wrong code created no session",
      client.get("/admin").status_code == 302)

# ---- an expired (out-of-window) code is rejected
r = client.post("/admin/login/2fa", data={"code": code_now(secret, -5)})
check("expired code rejected", r.status_code == 200
      and client.get("/admin").status_code == 302)

# ---- the current code logs in
valid = code_now(secret)
r = client.post("/admin/login/2fa", data={"code": valid})
check("valid code -> logged in",
      r.status_code == 302 and "/admin" in r.headers.get("Location", ""),
      r.headers.get("Location", ""))
check("session really created", client.get("/admin").status_code == 200)
client.get("/admin/logout")

# ---- the same code cannot be replayed
_rate_buckets.clear()
client.post("/admin/login", data={"email": "with2fa@example.com",
                                  "password": PW})
r = client.post("/admin/login/2fa", data={"code": valid})
check("a used code cannot be replayed", r.status_code == 200
      and client.get("/admin").status_code == 302)

# ---- a recovery code works, exactly once
r = client.post("/admin/login/2fa", data={"code": codes[0]})
check("recovery code logs in", r.status_code == 302,
      str(r.status_code))
check("recovery-code session created",
      client.get("/admin").status_code == 200)
with app.app_context():
    used = RecoveryCode.query.filter(RecoveryCode.used_at.isnot(None)).count()
    check("exactly one recovery code marked used", used == 1, str(used))
r = client.get("/admin/account")
check("account page shows one fewer recovery code",
      str(RECOVERY_CODE_COUNT - 1).encode() in r.data)
client.get("/admin/logout")

_rate_buckets.clear()
client.post("/admin/login", data={"email": "with2fa@example.com",
                                  "password": PW})
r = client.post("/admin/login/2fa", data={"code": codes[0]})
check("the same recovery code cannot be used twice", r.status_code == 200
      and client.get("/admin").status_code == 302)
# a different one still works, and formatting/spacing is forgiven
r = client.post("/admin/login/2fa",
                data={"code": " " + codes[1].upper().replace("-", " ") + " "})
check("a second recovery code still works (any spacing/case)",
      r.status_code == 302 and client.get("/admin").status_code == 200,
      str(r.status_code))

# ---- disabling needs a code
r = client.post("/admin/account/2fa/disable", data={"code": "000000"},
                follow_redirects=True)
check("disable with a wrong code refused", b"still on" in r.data)
with app.app_context():
    check("still enabled after a refused disable",
          user("with2fa@example.com").totp_enabled is True)

wait_for_next_code("with2fa@example.com")
r = client.post("/admin/account/2fa/disable", data={"code": code_now(secret)},
                follow_redirects=True)
check("disable with a valid code works", b"now off" in r.data)
with app.app_context():
    u = user("with2fa@example.com")
    check("2FA off and secret cleared",
          u.totp_enabled is False and u.totp_secret == "")
    check("recovery codes removed with it",
          RecoveryCode.query.filter_by(user_id=u.id).count() == 0)
client.get("/admin/logout")

# ---- with 2FA off, login is one step again
_rate_buckets.clear()
r = client.post("/admin/login", data={"email": "with2fa@example.com",
                                      "password": PW})
check("after disabling, login is one step again",
      r.status_code == 302 and "2fa" not in r.headers.get("Location", "")
      and client.get("/admin").status_code == 200,
      r.headers.get("Location", ""))
client.get("/admin/logout")

# ---- a wrong password never reaches the code step
_rate_buckets.clear()
r = client.post("/admin/login", data={"email": "with2fa@example.com",
                                      "password": "wrong"})
check("wrong password -> no hand-off to the code step",
      r.status_code == 200 and b"Incorrect email or password" in r.data)

# ---- code attempts are rate limited (a 6-digit code is guessable)
with app.app_context():
    u = user("plain@example.com")
    u.totp_secret = pyotp.random_base32()
    u.totp_enabled = True
    db.session.commit()
    plain_secret = u.totp_secret
_rate_buckets.clear()
env = {"REMOTE_ADDR": "203.0.113.77"}
brute = app.test_client()
brute.post("/admin/login", data={"email": "plain@example.com",
                                 "password": PW}, environ_overrides=env)
for _ in range(10):
    brute.post("/admin/login/2fa", data={"code": "000000"},
               environ_overrides=env)
r = brute.post("/admin/login/2fa", data={"code": code_now(plain_secret)},
               environ_overrides=env, follow_redirects=True)
check("11th code attempt blocked even when the code is correct",
      b"Too many codes tried" in r.data)
check("blocked attempt created no session",
      brute.get("/admin", environ_overrides=env).status_code == 302)
_rate_buckets.clear()

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
