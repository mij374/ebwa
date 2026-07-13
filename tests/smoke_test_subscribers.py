"""Smoke test for newsletter subscribers (CLAUDE.md testing rules).

The module predates the tests/ convention; this suite covers the
subscribe round-trip, admin list/CSV (with UK-local date display at the
BST boundary) and delete.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_subscribers.py
"""
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import app, db, User, Subscriber  # noqa: E402

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

# ---- subscribe round-trip
r = client.post("/subscribe", data={"email": "Reader@Example.org"},
                follow_redirects=True)
check("subscribe -> confirmation flash", b"Thank you for subscribing" in r.data)
with app.app_context():
    s = Subscriber.query.first()
    check("subscriber stored lowercased",
          s is not None and s.email == "reader@example.org")
    s_id = s.id
r = client.post("/subscribe", data={"email": "reader@example.org"},
                follow_redirects=True)
check("duplicate handled politely", b"already subscribed" in r.data)
client.post("/subscribe", data={"email": "not-an-email"})
with app.app_context():
    check("invalid email not stored", Subscriber.query.count() == 1)

# ---- anonymous admin access redirects
for path in ("/admin/subscribers", "/admin/subscribers.csv"):
    r = client.get(path)
    check("anon GET %s -> 302" % path, r.status_code == 302, str(r.status_code))

# ---- admin dates are UK local: 23:30 UTC on 31 March is 00:30 BST 1 April
with app.app_context():
    db.session.get(Subscriber, s_id).created_at = \
        datetime(2026, 3, 31, 23, 30)   # naive UTC
    db.session.commit()
client.post("/admin/login", data={"email": "test@example.com",
                                  "password": "pw123456"})
r = client.get("/admin/subscribers")
check("subscriber list shows UK local date at the BST boundary",
      r.status_code == 200 and b"01 Apr 2026" in r.data)
csv_data = client.get("/admin/subscribers.csv").data.decode("utf-8")
check("subscriber CSV shows UK local date at the BST boundary",
      "reader@example.org,2026-04-01" in csv_data)

# ---- delete round-trip
r = client.post("/admin/subscribers/%d/delete" % s_id)
check("delete -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("subscriber gone from db",
          db.session.get(Subscriber, s_id) is None)

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
