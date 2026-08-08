"""Smoke test for super-admin user management (CLAUDE.md rules).

Covers: every route refuses anonymous users and normal admins, the five
actions round-trip, the safety rails (last super admin, self-delete,
self-demote, duplicate email, short password) all refuse with a flash
rather than crashing, a user whose 2FA was reset can log in with a
password alone and re-enrol, and no password hash or TOTP secret ever
reaches the page.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_users.py
"""
import os
import sys
import time

import pyotp

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_users.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, MIN_PASSWORD_LEN, RecoveryCode,  # noqa: E402
                 User, _rate_buckets, make_recovery_codes)

app.config["TESTING"] = True

PW = "original-password"
NEW_PW = "brand-new-password"

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def user(email):
    return User.query.filter_by(email=email).first()


def make_user(email, role="admin", with_2fa=False):
    with app.app_context():
        u = User(email=email)
        u.role = role
        u.set_password(PW)
        if with_2fa:
            u.totp_secret = pyotp.random_base32()
            u.totp_enabled = True
        db.session.add(u)
        db.session.commit()
        if with_2fa:
            make_recovery_codes(u)
        return u.id


def login(client, email, password=PW):
    _rate_buckets.clear()
    return client.post("/admin/login", data={"email": email,
                                             "password": password})


with app.app_context():
    db.create_all()

boss_id = make_user("boss@example.com", "super_admin")
plain_id = make_user("plain@example.com")
victim_id = make_user("victim@example.com", with_2fa=True)

ACTIONS = [                       # (path, method) for every route
    ("/admin/users", "GET"),
    ("/admin/users/new", "POST"),
    ("/admin/users/%d/password" % plain_id, "POST"),
    ("/admin/users/%d/reset-2fa" % plain_id, "POST"),
    ("/admin/users/%d/role" % plain_id, "POST"),
    ("/admin/users/%d/delete" % plain_id, "POST"),
]

# ---- anonymous: every route redirects to the login page
anon = app.test_client()
for path, method in ACTIONS:
    r = anon.open(path, method=method)
    check("anon %s %s -> login redirect" % (method, path),
          r.status_code == 302
          and "/admin/login" in r.headers.get("Location", ""),
          "%s -> %s" % (r.status_code, r.headers.get("Location", "")))

# ---- normal admin: 403 on every route, and no nav link
client = app.test_client()
login(client, "plain@example.com")
check("normal admin reaches the dashboard",
      client.get("/admin").status_code == 200)
check("Users link hidden from a normal admin",
      b"/admin/users" not in client.get("/admin").data)
for path, method in ACTIONS:
    r = client.open(path, method=method)
    check("normal admin %s %s -> 403" % (method, path), r.status_code == 403,
          str(r.status_code))
with app.app_context():
    check("nothing changed by the blocked attempts",
          User.query.count() == 3 and user("plain@example.com").role == "admin"
          and user("victim@example.com").totp_enabled is True)
client.get("/admin/logout")

# ---- super admin: the page lists everyone, without secrets
boss = app.test_client()
login(boss, "boss@example.com")
r = boss.get("/admin")
check("Users link shown to a super admin", b"/admin/users" in r.data)
r = boss.get("/admin/users")
check("super admin GET /admin/users -> 200", r.status_code == 200,
      str(r.status_code))
html = r.data.decode("utf-8")
check("every account listed",
      all(e in html for e in ("boss@example.com", "plain@example.com",
                              "victim@example.com")))
check("roles shown", "super admin" in html and "admin" in html)
check("2FA status shown", ">On<" in html and ">Off<" in html)
with app.app_context():
    hashes = [u.password_hash for u in User.query.all()]
    secrets_ = [u.totp_secret for u in User.query.all() if u.totp_secret]
check("no password hash in the page HTML",
      not any(h in html for h in hashes))
check("no TOTP secret in the page HTML",
      not any(s in html for s in secrets_))

# ---- create round-trip
r = boss.post("/admin/users/new", data={"email": "  NEW@Example.com ",
                                        "password": "a-fine-password",
                                        "role": "admin"})
check("create -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    u = user("new@example.com")
    check("email normalised to lowercase", u is not None)
    check("password stored hashed, and works",
          u and u.check_password("a-fine-password")
          and u.password_hash != "a-fine-password")
    check("role applied", u and u.role == "admin")
    check("created_at stamped", u and u.created_at is not None)

# ---- duplicate email rejected
r = boss.post("/admin/users/new", data={"email": "new@example.com",
                                        "password": "another-password",
                                        "role": "admin"},
              follow_redirects=True)
check("duplicate email refused", b"already an account" in r.data)
with app.app_context():
    check("duplicate created nothing",
          User.query.filter_by(email="new@example.com").count() == 1)

# ---- short password rejected
short = "a" * (MIN_PASSWORD_LEN - 1)
r = boss.post("/admin/users/new", data={"email": "short@example.com",
                                        "password": short, "role": "admin"},
              follow_redirects=True)
check("short password refused on create",
      ("at least %d characters" % MIN_PASSWORD_LEN).encode() in r.data)
with app.app_context():
    check("short password created nothing", user("short@example.com") is None)

# ---- unknown role rejected
r = boss.post("/admin/users/new", data={"email": "sneaky@example.com",
                                        "password": "a-fine-password",
                                        "role": "root"},
              follow_redirects=True)
check("unknown role refused on create", b"Unknown role" in r.data)
with app.app_context():
    check("unknown role created nothing", user("sneaky@example.com") is None)

# ---- reset password round-trip
with app.app_context():
    before = user("plain@example.com").password_hash
r = boss.post("/admin/users/%d/password" % plain_id,
              data={"password": short, "confirm_password": short},
              follow_redirects=True)
check("short password refused on reset",
      ("at least %d characters" % MIN_PASSWORD_LEN).encode() in r.data)
r = boss.post("/admin/users/%d/password" % plain_id,
              data={"password": NEW_PW, "confirm_password": NEW_PW + "x"},
              follow_redirects=True)
check("mismatched reset refused", b"do not match" in r.data)
with app.app_context():
    check("refused resets left the hash alone",
          user("plain@example.com").password_hash == before)
r = boss.post("/admin/users/%d/password" % plain_id,
              data={"password": NEW_PW, "confirm_password": NEW_PW})
check("reset password -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    u = user("plain@example.com")
    check("new password works and old one does not",
          u.check_password(NEW_PW) and not u.check_password(PW))
other = app.test_client()
r = login(other, "plain@example.com", NEW_PW)
check("user can log in with the reset password",
      r.status_code == 302 and other.get("/admin").status_code == 200)
other.get("/admin/logout")

# ---- reset 2FA round-trip: they log in with a password alone and re-enrol
with app.app_context():
    check("victim starts with 2FA and recovery codes",
          user("victim@example.com").totp_enabled is True
          and RecoveryCode.query.filter_by(user_id=victim_id).count() > 0)
r = boss.post("/admin/users/%d/reset-2fa" % victim_id)
check("reset 2FA -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    u = user("victim@example.com")
    check("2FA fully cleared",
          u.totp_enabled is False and u.totp_secret == ""
          and u.totp_last_counter is None)
    check("their recovery codes deleted",
          RecoveryCode.query.filter_by(user_id=victim_id).count() == 0)
freed = app.test_client()
r = login(freed, "victim@example.com")
check("password alone now logs them in",
      r.status_code == 302 and "2fa" not in r.headers.get("Location", "")
      and freed.get("/admin").status_code == 200,
      r.headers.get("Location", ""))
r = freed.get("/admin/account/2fa/enable")
check("they can start enrolling again", r.status_code == 200,
      str(r.status_code))
with app.app_context():
    fresh_secret = user("victim@example.com").totp_secret
r = freed.post("/admin/account/2fa/enable",
               data={"code": pyotp.TOTP(fresh_secret).at(int(time.time()))})
check("re-enrolment completes", r.status_code == 200)
with app.app_context():
    check("2FA back on with a brand-new secret",
          user("victim@example.com").totp_enabled is True
          and fresh_secret != "")
freed.get("/admin/logout")
r = boss.post("/admin/users/%d/reset-2fa" % plain_id, follow_redirects=True)
check("reset 2FA on a user without it -> clear error",
      b"does not have two-factor" in r.data)

# ---- change role round-trip
r = boss.post("/admin/users/%d/role" % plain_id, data={"role": "super_admin"})
check("promote -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("promoted", user("plain@example.com").role == "super_admin")
r = boss.post("/admin/users/%d/role" % plain_id, data={"role": "admin"},
              follow_redirects=True)
with app.app_context():
    check("demoted again", user("plain@example.com").role == "admin")
r = boss.post("/admin/users/%d/role" % plain_id, data={"role": "wizard"},
              follow_redirects=True)
check("unknown role refused on change", b"Unknown role" in r.data)
with app.app_context():
    check("unknown role left it alone",
          user("plain@example.com").role == "admin")

# ---- unknown user id 404s rather than crashing
r = boss.post("/admin/users/999999/role", data={"role": "admin"})
check("unknown user id -> 404", r.status_code == 404, str(r.status_code))

# ---- safety rail: the last super admin cannot be deleted or demoted
# boss is currently the only one, and on the web that branch is only
# reachable by the sole super admin targeting themselves.
with app.app_context():
    check("set up: boss is the only super admin",
          User.query.filter_by(role="super_admin").count() == 1)
r = boss.post("/admin/users/%d/delete" % boss_id, follow_redirects=True)
check("last super admin cannot be deleted", b"only super admin left" in r.data)
with app.app_context():
    check("last super admin survived", user("boss@example.com") is not None)
r = boss.post("/admin/users/%d/role" % boss_id, data={"role": "admin"},
              follow_redirects=True)
check("last super admin cannot be demoted", b"only super admin left" in r.data)
with app.app_context():
    check("last super admin keeps the role",
          user("boss@example.com").role == "super_admin")

# ---- safety rail: no self-delete or self-demote, even with a spare
r = boss.post("/admin/users/%d/role" % plain_id, data={"role": "super_admin"})
with app.app_context():
    check("set up: two super admins now",
          User.query.filter_by(role="super_admin").count() == 2)
r = boss.post("/admin/users/%d/delete" % boss_id, follow_redirects=True)
check("self-deletion refused", b"delete your own account" in r.data)
with app.app_context():
    check("own account still there", user("boss@example.com") is not None)
r = boss.post("/admin/users/%d/role" % boss_id, data={"role": "admin"},
              follow_redirects=True)
check("self-demotion refused", b"change your own role" in r.data)
with app.app_context():
    check("still a super admin after the self-demote attempt",
          user("boss@example.com").role == "super_admin")

# ---- a super admin who is not the last one can be demoted, then deleted
helper = app.test_client()
login(helper, "plain@example.com", NEW_PW)
check("promoted user now reaches the users page",
      helper.get("/admin/users").status_code == 200)
r = helper.post("/admin/users/%d/role" % boss_id, data={"role": "admin"},
                follow_redirects=True)
check("demoting a non-last super admin is allowed", b"is now admin" in r.data)
with app.app_context():
    check("boss demoted", user("boss@example.com").role == "admin")
check("demoted user loses the users page",
      boss.get("/admin/users").status_code == 403)
r = helper.post("/admin/users/%d/delete" % boss_id, follow_redirects=True)
check("deleting a plain admin is allowed", b"Account deleted" in r.data)
with app.app_context():
    check("boss gone", user("boss@example.com") is None)
    check("their recovery codes went too",
          RecoveryCode.query.filter_by(user_id=boss_id).count() == 0)

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
