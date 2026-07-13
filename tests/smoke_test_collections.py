"""Smoke test for donations stage 2: event collection pages.

Stripe is mocked throughout. The critical scenario is form tampering:
a fee-only payment submitted WITH gift_aid ticked and a full declaration
must still be stored gift_aid=False — the fee can never carry Gift Aid.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_collections.py
"""
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

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
    db.session.commit()

client = app.test_client()

# ---- anon admin access redirects
for path in ("/admin/campaigns", "/admin/campaigns/new",
             "/admin/campaigns/1/contributors",
             "/admin/campaigns/1/contributors.csv"):
    r = client.get(path)
    check("anon GET %s -> 302" % path, r.status_code == 302, str(r.status_code))

# ---- admin creates campaigns
client.post("/admin/login", data={"email": "test@example.com",
                                  "password": "pw123456"})
r = client.post("/admin/campaigns/new", data={
    "title": "Summer Seaside Trip", "description": "A day at the seaside.\n\n"
    "Coach, beach and fish and chips.", "fee": "15", "target": "500",
    "active": "on"})
check("create campaign -> 302", r.status_code == 302, str(r.status_code))
client.post("/admin/campaigns/new", data={
    "title": "Winter Coat Fund", "description": "Coats for elders.",
    "target": "250"})   # no fee, INACTIVE (checkbox absent)
with app.app_context():
    camp = Campaign.query.filter_by(slug="summer-seaside-trip").first()
    check("campaign created with slug", camp is not None)
    check("amounts stored in pence", camp.fee_pence == 1500
          and camp.target_pence == 50000)
    camp_id = camp.id
    inactive = Campaign.query.filter_by(slug="winter-coat-fund").first()
    inactive_id = inactive.id

# ---- public page
r = client.get("/collections/summer-seaside-trip")
check("active collection page -> 200", r.status_code == 200,
      str(r.status_code))
check("fee shown", "£15".encode() in r.data)
check("explicit Gift Aid note shown",
      b"Gift Aid applies to your optional donation only" in r.data)
check("HMRC wording present", b"responsibility to pay any difference"
      in r.data)
r = client.get("/collections/winter-coat-fund")
check("inactive collection -> 404", r.status_code == 404, str(r.status_code))
r = client.get("/collections/nope")
check("unknown slug -> 404", r.status_code == 404, str(r.status_code))
r = client.get("/sitemap.xml")
check("active collection in sitemap, inactive absent",
      b"/collections/summer-seaside-trip" in r.data
      and b"winter-coat-fund" not in r.data)

# ---- TAMPER TEST: fee only, but form claims Gift Aid with full declaration
fake = SimpleNamespace(id="cs_fee_only", url="https://checkout.stripe.test/1")
with patch("app.stripe.checkout.Session.create", return_value=fake) as create:
    r = client.post("/collections/summer-seaside-trip", data={
        "include_fee": "on", "donation": "",
        "name": "Tamper Test", "email": "t@example.org",
        "gift_aid": "on", "gift_aid_name": "Tamper Test",
        "gift_aid_address": "1", "gift_aid_postcode": "EN1 1AA",
        "gift_aid_declaration": "on"})
check("fee-only POST -> 303", r.status_code == 303, str(r.status_code))
with app.app_context():
    p = Payment.query.filter_by(stripe_session_id="cs_fee_only").first()
    check("TAMPERED fee-only payment stored gift_aid=False",
          p is not None and p.gift_aid is False)
    check("tampered declaration fields not stored",
          p.gift_aid_name == "" and p.gift_aid_postcode == "")
    check("fee-only amounts correct", p.fee_pence == 1500
          and p.donation_pence == 0 and p.gift_aid_pence == 0)
    check("payment linked to campaign", p.campaign_id == camp_id)

# ---- donation-only path (place unticked)
fake = SimpleNamespace(id="cs_donation", url="https://checkout.stripe.test/2")
with patch("app.stripe.checkout.Session.create", return_value=fake) as create:
    r = client.post("/collections/summer-seaside-trip", data={
        "donation": "20", "name": "Donor Only", "email": "d@example.org",
        "gift_aid": "on", "gift_aid_name": "Donor Only",
        "gift_aid_address": "42", "gift_aid_postcode": "en3 4eu",
        "gift_aid_declaration": "on"})
check("donation-only POST -> 303", r.status_code == 303, str(r.status_code))
check("donation-only sends one Stripe line item",
      len(create.call_args.kwargs["line_items"]) == 1)
with app.app_context():
    p = Payment.query.filter_by(stripe_session_id="cs_donation").first()
    check("donation-only: fee 0, donation 2000, gift aid on donation",
          p.fee_pence == 0 and p.donation_pence == 2000
          and p.gift_aid and p.gift_aid_pence == 2000)
    check("postcode stored uppercase", p.gift_aid_postcode == "EN3 4EU")

# ---- fee + donation path
fake = SimpleNamespace(id="cs_both", url="https://checkout.stripe.test/3")
with patch("app.stripe.checkout.Session.create", return_value=fake) as create:
    r = client.post("/collections/summer-seaside-trip", data={
        "include_fee": "on", "donation": "5",
        "name": "Both Payer", "email": "b@example.org",
        "gift_aid": "on", "gift_aid_name": "Both Payer",
        "gift_aid_address": "7", "gift_aid_postcode": "EN2 2BB",
        "gift_aid_declaration": "on"})
check("fee+donation POST -> 303", r.status_code == 303, str(r.status_code))
items = create.call_args.kwargs["line_items"]
check("fee and donation are separate Stripe line items",
      len(items) == 2
      and items[0]["price_data"]["unit_amount"] == 1500
      and items[1]["price_data"]["unit_amount"] == 500)
with app.app_context():
    p = Payment.query.filter_by(stripe_session_id="cs_both").first()
    check("fee+donation: gift aid covers ONLY the donation",
          p.fee_pence == 1500 and p.donation_pence == 500
          and p.gift_aid and p.gift_aid_pence == 500)

# ---- nothing selected is rejected
with patch("app.stripe.checkout.Session.create") as create:
    client.post("/collections/summer-seaside-trip", data={
        "donation": "", "name": "Nobody", "email": "n@example.org"})
    check("empty submission never reaches Stripe", create.call_count == 0)
with app.app_context():
    check("empty submission stored nothing",
          Payment.query.filter_by(name="Nobody").count() == 0)

# ---- running total counts only completed payments
with app.app_context():
    check("raised is 0 while payments pending",
          db.session.get(Campaign, camp_id).raised_pence == 0)
for sid in ("cs_fee_only", "cs_donation", "cs_both"):
    with patch("app.stripe.Webhook.construct_event",
               return_value={"type": "checkout.session.completed",
                             "data": {"object": {"id": sid}}}):
        client.post("/stripe/webhook", data="{}")
with app.app_context():
    camp = db.session.get(Campaign, camp_id)
    # 1500 + 2000 + (1500 + 500) = 5500
    check("running total sums completed payments",
          camp.raised_pence == 5500, str(camp.raised_pence))
    check("progress toward £500 target", camp.target_percent == 11,
          str(camp.target_percent))
r = client.get("/collections/summer-seaside-trip")
check("running total on public page", "£55".encode() in r.data
      and "£500".encode() in r.data)

# ---- homepage: active campaign card, inactive absent
html = client.get("/").data.decode("utf-8")
check("homepage shows active collection",
      "Summer Seaside Trip" in html
      and "/collections/summer-seaside-trip" in html)
check("homepage shows progress", "£55" in html and "£500" in html)
check("inactive collection absent from homepage",
      "Winter Coat Fund" not in html)

# ---- contributor personal data never on public pages
for path in ("/collections/summer-seaside-trip", "/"):
    r = client.get(path)
    check("payer names absent from %s" % path,
          b"Donor Only" not in r.data and b"Tamper Test" not in r.data)

# ---- contributor list + CSV (authed)
r = client.get("/admin/campaigns/%d/contributors" % camp_id)
check("contributor list -> 200 with payers", r.status_code == 200
      and b"Donor Only" in r.data and b"Both Payer" in r.data)
csv_data = client.get("/admin/campaigns/%d/contributors.csv"
                      % camp_id).data.decode("utf-8")
check("csv header", csv_data.startswith(
    "date,name,email,fee_gbp,donation_gbp,total_gbp,gift_aid,status"))
check("csv fee+donation row correct",
      "Both Payer,b@example.org,15.00,5.00,20.00,yes,complete" in csv_data)
check("csv fee-only row has gift_aid no",
      "Tamper Test,t@example.org,15.00,0.00,15.00,no,complete" in csv_data)

# ---- contributor dates are UK local: 23:30 UTC on 31 March is
# 00:30 BST on 1 April
with app.app_context():
    p = Payment.query.filter_by(stripe_session_id="cs_fee_only").first()
    p.created_at = datetime(2026, 3, 31, 23, 30)   # naive UTC
    db.session.commit()
r = client.get("/admin/campaigns/%d/contributors" % camp_id)
check("contributor list shows UK local date at the BST boundary",
      b"01 Apr 2026" in r.data)
csv_data = client.get("/admin/campaigns/%d/contributors.csv"
                      % camp_id).data.decode("utf-8")
check("contributor CSV shows UK local date at the BST boundary",
      "2026-04-01,Tamper Test" in csv_data)

# ---- campaign edit keeps slug; delete rules
client.post("/admin/campaigns/%d/edit" % camp_id, data={
    "title": "Summer Seaside Trip", "description": "Updated.",
    "fee": "15", "target": "600", "active": "on"})
with app.app_context():
    camp = db.session.get(Campaign, camp_id)
    check("edit round-trip", camp.target_pence == 60000
          and camp.description == "Updated.")
    check("slug stable on edit", camp.slug == "summer-seaside-trip")
r = client.post("/admin/campaigns/%d/delete" % camp_id,
                follow_redirects=True)
with app.app_context():
    check("delete refused while payments exist",
          db.session.get(Campaign, camp_id) is not None)
check("refusal explains deactivating instead", b"active" in r.data)
r = client.post("/admin/campaigns/%d/delete" % inactive_id)
with app.app_context():
    check("delete works for campaign without payments",
          db.session.get(Campaign, inactive_id) is None)

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
