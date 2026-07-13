"""Smoke test for donations stage 3: Gift Aid claim export.

Stripe is mocked throughout. The critical assertions: a mixed
fee+donation payment exports ONLY its donation portion, and a fee-only
payment never appears in the claim at all.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_gift_aid.py
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import app, db, User, Campaign, Payment  # noqa: E402

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
    c = Campaign(title="Seaside Trip", slug="seaside-trip",
                 description="Trip.", fee_pence=1500, active=True)
    db.session.add(c)
    db.session.commit()

client = app.test_client()

# ---- anon access redirects
for path in ("/admin/gift-aid", "/admin/gift-aid.csv",
             "/admin/gift-aid/declarations"):
    r = client.get(path)
    check("anon GET %s -> 302" % path, r.status_code == 302, str(r.status_code))


def pay(session_id, path, data):
    fake = SimpleNamespace(id=session_id, url="https://checkout.stripe.test/x")
    with patch("app.stripe.checkout.Session.create", return_value=fake):
        r = client.post(path, data=data)
    assert r.status_code == 303, "%s -> %s" % (path, r.status_code)


def complete(session_id):
    with patch("app.stripe.Webhook.construct_event",
               return_value={"type": "checkout.session.completed",
                             "data": {"object": {"id": session_id}}}):
        client.post("/stripe/webhook", data="{}")


ga = {"gift_aid": "on", "gift_aid_declaration": "on"}

# 1. fee-only with tampered Gift Aid claim — completed, NEVER claimable
pay("cs_fee_only", "/collections/seaside-trip", dict(ga,
    include_fee="on", donation="", name="Fee Only",
    email="f@example.org", gift_aid_name="Fee Only",
    gift_aid_address="9", gift_aid_postcode="EN9 9ZZ"))
complete("cs_fee_only")

# 2. mixed fee (£15) + donation (£5) with declaration — completed
pay("cs_mixed", "/collections/seaside-trip", dict(ga,
    include_fee="on", donation="5", name="Rahim Uddin",
    email="r@example.org", gift_aid_name="Rahim Uddin",
    gift_aid_address="7", gift_aid_postcode="EN2 2BB"))
complete("cs_mixed")

# 3. donation-only (£20) with declaration — completed
pay("cs_donation", "/collections/seaside-trip", dict(ga,
    donation="20", name="Salma Begum", email="s@example.org",
    gift_aid_name="Salma Begum", gift_aid_address="42",
    gift_aid_postcode="EN3 4EU"))
complete("cs_donation")

# 4. general donation (£25.50) with declaration — completed
pay("cs_general", "/donate", dict(ga,
    amount="25.50", name="Amina Chowdhury", email="a@example.org",
    gift_aid_name="Amina Chowdhury", gift_aid_address="12",
    gift_aid_postcode="EN1 1AA"))
complete("cs_general")

# 5. donation with declaration but payment still PENDING — not claimable
pay("cs_pending", "/collections/seaside-trip", dict(ga,
    donation="10", name="Pending Person", email="p@example.org",
    gift_aid_name="Pending Person", gift_aid_address="3",
    gift_aid_postcode="EN5 5EE"))

# ---- claims page totals: 500 + 2000 + 2550 = 5050 claimable
client.post("/admin/login", data={"email": "test@example.com",
                                  "password": "pw123456"})
html = client.get("/admin/gift-aid").data.decode("utf-8")
check("claims page -> claimable total £50.50", "£50.50" in html)
check("claims page -> 25% reclaim £12.63", "£12.63" in html)
check("mixed payment listed at its donation portion only",
      "Rahim Uddin" in html and "£5</b>" in html)
check("fee-only payer absent from claims page", "Fee Only" not in html)
check("pending declaration absent from claims page",
      "Pending Person" not in html)

# ---- HMRC schedule CSV
csv_data = client.get("/admin/gift-aid.csv").data.decode("utf-8")
# Storage is naive UTC, but all admin-facing Gift Aid dates are UK local
# (Europe/London) — so assertions and filter params use UK dates.
uk_today = datetime.now(ZoneInfo("Europe/London")).date()
today = uk_today.strftime("%d/%m/%y")
check("HMRC header row", csv_data.splitlines()[0] ==
      "Title,First name,Last name,House name or number,Postcode,"
      "Aggregated donations,Donation date,Amount")
check("mixed payment exports ONLY the £5 donation",
      ",Rahim,Uddin,7,EN2 2BB,,%s,5.00" % today in csv_data)
check("fee amount 15.00 never appears anywhere in the claim",
      "15.00" not in csv_data and "20.00" not in csv_data.replace(
          ",20.00", ""))   # £20 donation is fine; £15 fee must not be
check("fee-only payer never appears", "Fee Only" not in csv_data)
check("donation-only row present",
      ",Salma,Begum,42,EN3 4EU,,%s,20.00" % today in csv_data)
check("general donation row present",
      ",Amina,Chowdhury,12,EN1 1AA,,%s,25.50" % today in csv_data)
check("pending payment never appears", "Pending Person" not in csv_data)
check("claim has exactly 3 rows",
      len([l for l in csv_data.splitlines() if l.strip()]) == 4,
      str(len(csv_data.splitlines())))

# ---- date-range filter
tomorrow = (uk_today + timedelta(days=1)).isoformat()
html = client.get("/admin/gift-aid?from=%s" % tomorrow).data.decode("utf-8")
check("future-dated filter shows nothing claimable",
      "£0" in html and "Rahim Uddin" not in html)
csv_data = client.get("/admin/gift-aid.csv?from=%s&to=%s"
                      % (uk_today.isoformat(), uk_today.isoformat())
                      ).data.decode("utf-8")
check("today-to-today filter includes all 3 rows",
      len([l for l in csv_data.splitlines() if l.strip()]) == 4)
csv_data = client.get("/admin/gift-aid.csv?from=%s" % tomorrow
                      ).data.decode("utf-8")
check("future-dated CSV is empty of rows",
      len([l for l in csv_data.splitlines() if l.strip()]) == 1)

# ---- UK local dates at the BST boundary: 23:30 UTC on 31 March is
# 00:30 BST on 1 April, so it belongs to the April claim period.
pay("cs_boundary", "/collections/seaside-trip", dict(ga,
    donation="7", name="Boundary Case", email="bc@example.org",
    gift_aid_name="March Boundary", gift_aid_address="31",
    gift_aid_postcode="EN4 4DD"))
complete("cs_boundary")
with app.app_context():
    p = Payment.query.filter_by(stripe_session_id="cs_boundary").first()
    p.created_at = datetime(2026, 3, 31, 23, 30)   # naive UTC
    db.session.commit()

april_csv = client.get("/admin/gift-aid.csv?from=2026-04-01&to=2026-04-30"
                       ).data.decode("utf-8")
check("23:30 UTC 31 March exports in the April range as 01/04/26",
      ",March,Boundary,31,EN4 4DD,,01/04/26,7.00" in april_csv)
march_csv = client.get("/admin/gift-aid.csv?from=2026-03-01&to=2026-03-31"
                       ).data.decode("utf-8")
check("...and is absent from the March range", "Boundary" not in march_csv)
html = client.get("/admin/gift-aid?from=2026-04-01&to=2026-04-30"
                  ).data.decode("utf-8")
check("claims page shows the UK local date 01 Apr 2026",
      "01 Apr 2026" in html and "March Boundary" in html)

# ---- declarations record-keeping view
html = client.get("/admin/gift-aid/declarations").data.decode("utf-8")
check("declarations view mentions the six-year duty", "six years" in html)
check("declarations view includes pending declarations",
      "Pending Person" in html)
check("declarations view includes completed declarations",
      "Rahim Uddin" in html and "Amina Chowdhury" in html)
check("fee-only tamperer has no declaration on record",
      "Fee Only" not in html)

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
