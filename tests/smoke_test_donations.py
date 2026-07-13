"""Smoke test for donations stage 1: models + general donations.

Stripe is mocked throughout — no network calls, no real keys needed.
The critical assertions are the STRUCTURAL Gift Aid rules from CLAUDE.md:
Gift Aid can never attach to a fee, and general donations are 100%
donation_pence. Both are database CHECK constraints, tested here.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_donations.py
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from sqlalchemy.exc import IntegrityError  # noqa: E402

from app import app, db, Campaign, Payment, parse_pounds  # noqa: E402

app.config["TESTING"] = True

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    db.create_all()

client = app.test_client()

# ---- parse_pounds
check("parse_pounds whole pounds", parse_pounds("25") == 2500)
check("parse_pounds pence", parse_pounds("10.50") == 1050)
check("parse_pounds rejects garbage", parse_pounds("abc") is None)
check("parse_pounds rejects sub-pence", parse_pounds("10.555") is None)
check("parse_pounds rejects missing", parse_pounds(None) is None)

# ---- structural Gift Aid rules (database CHECK constraints)
with app.app_context():
    bad = Payment(campaign_id=None, fee_pence=0, donation_pence=0,
                  gift_aid=True, status="pending")
    db.session.add(bad)
    try:
        db.session.commit()
        violated = False
    except IntegrityError:
        violated = True
        db.session.rollback()
    check("DB refuses Gift Aid without a donation portion", violated)

    c = Campaign(title="Seaside Trip", slug="seaside-trip", fee_pence=1500)
    db.session.add(c)
    db.session.commit()
    bad = Payment(campaign_id=None, fee_pence=1500, donation_pence=0,
                  status="pending")
    db.session.add(bad)
    try:
        db.session.commit()
        violated = False
    except IntegrityError:
        violated = True
        db.session.rollback()
    check("DB refuses a general donation carrying a fee", violated)

    ok = Payment(campaign_id=c.id, fee_pence=1500, donation_pence=500,
                 gift_aid=True, gift_aid_name="A Donor",
                 gift_aid_address="12", gift_aid_postcode="EN3 4EU",
                 status="pending")
    db.session.add(ok)
    db.session.commit()
    check("fee + voluntary donation is allowed", ok.id is not None)
    check("gift_aid_pence covers ONLY the donation, never the fee",
          ok.gift_aid_pence == 500 and ok.total_pence == 2000)
    no_ga = Payment(campaign_id=None, fee_pence=0, donation_pence=1000,
                    status="pending")
    check("gift_aid_pence is 0 without a declaration",
          no_ga.gift_aid_pence == 0)
    db.session.delete(ok)
    db.session.delete(c)
    db.session.commit()

# ---- page rendering
r = client.get("/donate")
check("GET /donate -> 200", r.status_code == 200, str(r.status_code))
check("HMRC declaration wording present",
      b"UK taxpayer" in r.data and b"responsibility to pay any difference"
      in r.data)
check("Gift Aid fields hidden until ticked", b"<div id=\"giftAidFields\" hidden>"
      in r.data)
for path in ("/donate/success", "/donate/cancelled"):
    r = client.get(path)
    check("GET %s -> 200" % path, r.status_code == 200, str(r.status_code))

# ---- POST /donate with Stripe mocked
fake_session = SimpleNamespace(id="cs_test_abc123",
                               url="https://checkout.stripe.test/pay")
with patch("app.stripe.checkout.Session.create",
           return_value=fake_session) as create:
    r = client.post("/donate", data={
        "amount": "25.50", "name": "Amina Chowdhury",
        "email": "Amina@Example.org", "gift_aid": "on",
        "gift_aid_name": "Amina Chowdhury", "gift_aid_address": "42",
        "gift_aid_postcode": "en3 4eu", "gift_aid_declaration": "on"})
check("donate POST -> 303 to Stripe", r.status_code == 303
      and r.headers["Location"] == fake_session.url, str(r.status_code))
check("Stripe called with pence amount",
      create.call_args.kwargs["line_items"][0]["price_data"]["unit_amount"]
      == 2550)
check("Stripe called in GBP",
      create.call_args.kwargs["line_items"][0]["price_data"]["currency"]
      == "gbp")
with app.app_context():
    p = Payment.query.filter_by(stripe_session_id="cs_test_abc123").first()
    check("payment stored pending", p is not None and p.status == "pending")
    check("general donation: 100% donation, no fee",
          p.campaign_id is None and p.fee_pence == 0
          and p.donation_pence == 2550)
    check("gift aid declaration stored", p.gift_aid
          and p.gift_aid_name == "Amina Chowdhury"
          and p.gift_aid_address == "42"
          and p.gift_aid_postcode == "EN3 4EU")
    p_id = p.id

# ---- rejected submissions create no payment and no Stripe call
with patch("app.stripe.checkout.Session.create") as create:
    client.post("/donate", data={"amount": "0.50", "name": "A",
                                 "email": "a@b.c"})
    client.post("/donate", data={"amount": "20000", "name": "A",
                                 "email": "a@b.c"})
    client.post("/donate", data={"amount": "ten", "name": "A",
                                 "email": "a@b.c"})
    client.post("/donate", data={"amount": "10", "name": "", "email": ""})
    # Gift Aid ticked but declaration incomplete must NOT silently drop it
    client.post("/donate", data={"amount": "10", "name": "A",
                                 "email": "a@b.c", "gift_aid": "on",
                                 "gift_aid_name": "A"})
    check("invalid submissions never reach Stripe", create.call_count == 0,
          str(create.call_count))
with app.app_context():
    check("invalid submissions stored nothing",
          Payment.query.count() == 1, str(Payment.query.count()))

# ---- Stripe failure: flash error, store nothing
with patch("app.stripe.checkout.Session.create",
           side_effect=Exception("stripe down")):
    r = client.post("/donate", data={"amount": "10", "name": "A",
                                     "email": "a@b.c"},
                    follow_redirects=True)
check("stripe failure shows friendly error",
      b"start the payment" in r.data)
with app.app_context():
    check("stripe failure stored nothing", Payment.query.count() == 1)

# ---- webhook: verified, idempotent
completed_event = {"type": "checkout.session.completed",
                   "data": {"object": {"id": "cs_test_abc123"}}}

with patch("app.stripe.Webhook.construct_event",
           side_effect=Exception("bad signature")):
    r = client.post("/stripe/webhook", data="{}")
check("unverifiable webhook -> 400", r.status_code == 400,
      str(r.status_code))
with app.app_context():
    check("payment still pending after rejected webhook",
          db.session.get(Payment, p_id).status == "pending")

with patch("app.stripe.Webhook.construct_event",
           return_value=completed_event):
    r = client.post("/stripe/webhook", data="{}")
    check("verified webhook -> 200", r.status_code == 200, str(r.status_code))
    with app.app_context():
        check("payment marked complete",
              db.session.get(Payment, p_id).status == "complete")
    r = client.post("/stripe/webhook", data="{}")   # Stripe replay
    check("replayed webhook -> 200 (idempotent)", r.status_code == 200)
    with app.app_context():
        check("replay left payment complete",
              db.session.get(Payment, p_id).status == "complete")

with patch("app.stripe.Webhook.construct_event",
           return_value={"type": "checkout.session.completed",
                         "data": {"object": {"id": "cs_unknown"}}}):
    r = client.post("/stripe/webhook", data="{}")
check("unknown session webhook -> 200 no-op", r.status_code == 200)

with patch("app.stripe.Webhook.construct_event",
           return_value={"type": "payment_intent.created",
                         "data": {"object": {"id": "cs_test_abc123"}}}):
    r = client.post("/stripe/webhook", data="{}")
check("irrelevant event type -> 200 no-op", r.status_code == 200)

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
