"""Smoke test for the "Become a member" form (CLAUDE.md testing rules).

Personal data module: the key assertions are that anonymous access to the
admin list and CSV export redirects (302), and that applicant details never
appear on public pages.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_membership.py
"""
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import app, db, User, MembershipApplication  # noqa: E402

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

# ---- public form page
r = client.get("/membership")
check("GET /membership -> 200", r.status_code == 200, str(r.status_code))
check("honeypot field present", b'name="website"' in r.data)
r = client.get("/sitemap.xml")
check("/membership in sitemap", b"/membership" in r.data)

# ---- personal data: anonymous admin access redirects
for path in ("/admin/membership", "/admin/membership.csv"):
    r = client.get(path)
    check("anon GET %s -> 302" % path, r.status_code == 302, str(r.status_code))
r = client.post("/admin/membership/1/status", data={"status": "approved"})
check("anon status update -> 302", r.status_code == 302, str(r.status_code))

# ---- submit an application (all four eligibility ticks required)
TICKS = {"over_18": "on", "bangladeshi_origin": "on",
         "lives_works_enfield": "on", "fee_confirmed": "on"}
r = client.post("/membership", data=dict(TICKS,
    name="Shirin Akhter", email="Shirin@Example.org",
    phone="07700 900123", address="12 Test Road, Enfield EN3 4XX",
    reason="I volunteer locally, and I love the drop-in."),
    follow_redirects=True)
check("submit with all four ticks -> confirmation flash",
      b"received your application" in r.data)
with app.app_context():
    m = MembershipApplication.query.filter_by(name="Shirin Akhter").first()
    m_id = m.id if m else None
    check("application stored", m is not None)
    check("email lowercased", m.email == "shirin@example.org", m.email)
    check("status defaults to new", m.status == "new", m.status)
    check("all four eligibility booleans stored True",
          m.over_18 and m.bangladeshi_origin and m.lives_works_enfield
          and m.fee_confirmed)

# ---- each missing tick rejects the submission server-side
for missing in TICKS:
    partial = {k: v for k, v in TICKS.items() if k != missing}
    r = client.post("/membership", data=dict(partial,
        name="Missing Tick", email="mt@example.org"),
        follow_redirects=True)
    check("submission without %s rejected" % missing,
          b"all four membership declarations" in r.data)
with app.app_context():
    check("no partial-tick application stored",
          MembershipApplication.query.filter_by(name="Missing Tick")
          .count() == 0)

# ---- honeypot: pretend success, store nothing
r = client.post("/membership", data={
    "name": "Spam Bot", "email": "bot@spam.example",
    "website": "http://spam.example"}, follow_redirects=True)
check("honeypot gets normal confirmation",
      b"received your application" in r.data)
with app.app_context():
    check("honeypot submission not stored",
          MembershipApplication.query.filter_by(name="Spam Bot").count() == 0)

# ---- validation
client.post("/membership", data={"name": "", "email": "no-at-sign"})
with app.app_context():
    check("invalid submission not stored",
          MembershipApplication.query.count() == 1,
          str(MembershipApplication.query.count()))

# ---- personal data never on public pages
for path in ("/membership", "/", "/news", "/resources"):
    r = client.get(path)
    check("applicant data absent from %s" % path,
          b"Shirin Akhter" not in r.data and b"07700 900123" not in r.data)

# ---- admin list + status workflow
client.post("/admin/login", data={"email": "test@example.com",
                                  "password": "pw123456"})
r = client.get("/admin/membership")
check("authed list -> 200 with applicant", r.status_code == 200
      and b"Shirin Akhter" in r.data)
r = client.post("/admin/membership/%d/status" % m_id,
                data={"status": "contacted"})
check("status update -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("status updated",
          db.session.get(MembershipApplication, m_id).status == "contacted")
client.post("/admin/membership/%d/status" % m_id, data={"status": "hacked"})
with app.app_context():
    check("unknown status rejected",
          db.session.get(MembershipApplication, m_id).status == "contacted")

# ---- CSV export (fields with commas must be quoted)
client.post("/admin/membership/%d/status" % m_id, data={"status": "approved"})
csv_data = client.get("/admin/membership.csv").data.decode("utf-8")
check("csv has header row", csv_data.startswith(
    "name,email,phone,address,reason,status,applied_on"))
check("csv row quoted correctly",
      '"12 Test Road, Enfield EN3 4XX"' in csv_data
      and "approved" in csv_data)
check("special-category origin data excluded from CSV",
      "bangladeshi" not in csv_data.lower())

# ---- admin dates are UK local: 23:30 UTC on 31 March is 00:30 BST 1 April
with app.app_context():
    db.session.get(MembershipApplication, m_id).created_at = \
        datetime(2026, 3, 31, 23, 30)   # naive UTC
    db.session.commit()
r = client.get("/admin/membership")
check("membership list shows UK local date at the BST boundary",
      b"01 Apr 2026" in r.data)
csv_data = client.get("/admin/membership.csv").data.decode("utf-8")
check("membership CSV shows UK local date at the BST boundary",
      ",approved,2026-04-01" in csv_data)

# ---- delete round-trip
r = client.post("/admin/membership/%d/delete" % m_id)
check("delete -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("application gone from db",
          db.session.get(MembershipApplication, m_id) is None)

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
