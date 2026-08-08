"""Smoke test for the admin account page / password change (CLAUDE.md rules).

Covers: anonymous access redirects, a correct current password changes
the stored hash (old password stops working, new one logs in), a wrong
current password is rejected, a too-short password is rejected, a
mismatched confirmation is rejected, and the Account link is in the
sidebar for every admin role.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_account.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_account.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import app, db, MIN_PASSWORD_LEN, User, _rate_buckets  # noqa: E402

app.config["TESTING"] = True

FIRST = "first-password"
SECOND = "second-password"

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def hash_of(email):
    with app.app_context():
        return User.query.filter_by(email=email).first().password_hash


with app.app_context():
    db.create_all()
    u = User(email="admin@example.com")
    u.set_password(FIRST)
    db.session.add(u)
    boss = User(email="netbus@example.com")
    boss.set_password(FIRST)
    boss.role = "super_admin"
    db.session.add(boss)
    db.session.commit()

client = app.test_client()

# ---- anonymous access redirects to the login page
for method in ("GET", "POST"):
    r = client.open("/admin/account", method=method)
    check("anon %s /admin/account -> login redirect" % method,
          r.status_code == 302
          and "/admin/login" in r.headers.get("Location", ""),
          "%s -> %s" % (r.status_code, r.headers.get("Location", "")))

# ---- the change form is not on the login page (logged-in users only)
r = client.get("/admin/login")
check("login page has no password-change form",
      b"current_password" not in r.data and b"new_password" not in r.data)

# ---- login, then the page renders with the Account nav link
r = client.post("/admin/login", data={"email": "admin@example.com",
                                      "password": FIRST})
check("login -> 302", r.status_code == 302, str(r.status_code))
r = client.get("/admin/account")
check("authed GET /admin/account -> 200", r.status_code == 200,
      str(r.status_code))
html = r.data.decode("utf-8")
check("all three password fields present",
      all(f in html for f in ("current_password", "new_password",
                              "confirm_password")))
check("Account link in the sidebar for a normal admin",
      "/admin/account" in client.get("/admin").data.decode("utf-8"))

before = hash_of("admin@example.com")

# ---- wrong current password rejected
r = client.post("/admin/account", data={"current_password": "not-my-password",
                                        "new_password": SECOND,
                                        "confirm_password": SECOND},
                follow_redirects=True)
check("wrong current password -> error shown",
      b"current password is not correct" in r.data)
check("wrong current password left the hash alone",
      hash_of("admin@example.com") == before)

# ---- mismatched confirmation rejected
r = client.post("/admin/account", data={"current_password": FIRST,
                                        "new_password": SECOND,
                                        "confirm_password": SECOND + "x"},
                follow_redirects=True)
check("mismatched confirmation -> error shown", b"do not match" in r.data)
check("mismatch left the hash alone",
      hash_of("admin@example.com") == before)

# ---- too-short new password rejected
short = "a" * (MIN_PASSWORD_LEN - 1)
r = client.post("/admin/account", data={"current_password": FIRST,
                                        "new_password": short,
                                        "confirm_password": short},
                follow_redirects=True)
check("short password -> error shown",
      ("at least %d characters" % MIN_PASSWORD_LEN).encode() in r.data)
check("short password left the hash alone",
      hash_of("admin@example.com") == before)

# ---- correct current password changes it
r = client.post("/admin/account", data={"current_password": FIRST,
                                        "new_password": SECOND,
                                        "confirm_password": SECOND})
check("valid change -> 302", r.status_code == 302, str(r.status_code))
r = client.get("/admin/account")
check("success flashed", b"password has been changed" in r.data)
after = hash_of("admin@example.com")
check("stored hash changed", after != before)
check("password is still hashed, not stored raw",
      SECOND not in after and after.count("$") >= 2, after[:20])
with app.app_context():
    u = User.query.filter_by(email="admin@example.com").first()
    check("new password verifies", u.check_password(SECOND))
    check("old password no longer verifies", not u.check_password(FIRST))

# ---- the change sticks across a fresh login
client.get("/admin/logout")
_rate_buckets.clear()
fresh = app.test_client()
r = fresh.post("/admin/login", data={"email": "admin@example.com",
                                     "password": FIRST})
check("old password cannot log in", r.status_code == 200
      and b"Incorrect email or password" in r.data)
r = fresh.post("/admin/login", data={"email": "admin@example.com",
                                     "password": SECOND})
check("new password logs in", r.status_code == 302
      and "/admin" in r.headers.get("Location", ""))
fresh.get("/admin/logout")

# ---- a super admin gets the same page and link (all roles, not just one)
_rate_buckets.clear()
boss_client = app.test_client()
boss_client.post("/admin/login", data={"email": "netbus@example.com",
                                       "password": FIRST})
check("Account link in the sidebar for a super admin",
      "/admin/account" in boss_client.get("/admin").data.decode("utf-8"))
r = boss_client.post("/admin/account", data={"current_password": FIRST,
                                             "new_password": SECOND,
                                             "confirm_password": SECOND})
check("super admin can change their password too", r.status_code == 302,
      str(r.status_code))
with app.app_context():
    check("super admin's new password verifies",
          User.query.filter_by(email="netbus@example.com")
          .first().check_password(SECOND))
    check("changing one user's password left the other alone",
          User.query.filter_by(email="admin@example.com")
          .first().check_password(SECOND))

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
