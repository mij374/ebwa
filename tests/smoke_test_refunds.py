"""Refunds on donations and collections, and what they must stop counting.

A refund issued in the Stripe dashboard used to be invisible here: the
campaign total, the contributor list and the Gift Aid claim all went on
counting it. The last of those is the serious one — CLAIMING GIFT AID ON
A REFUNDED DONATION IS CLAIMING MONEY HMRC IS OWED BACK, and the charity
repays it when somebody notices.

  * `charge.refunded` and `payment_intent.refunded` both mark the
    payment, because which one arrives depends on the account's API
    version and they carry the same fact in different shapes.
  * A refunded payment is out of the campaign total, out of the
    contributor count and out of the Gift Aid claim — but STILL VISIBLE
    in the admin, marked, because money that was taken and given back is
    a thing the accounts have to be able to show.
  * PARTIAL REFUNDS ARE HANDLED FOR TOTALS AND EXCLUDED FROM GIFT AID.
    Totals use the net, which is arithmetic. Gift Aid cannot be
    apportioned: nothing in a Stripe refund says whether the money came
    out of the place fee or out of the donation, and the two ways of
    guessing wrong are not equal — guess low and EBWA loses some Gift
    Aid, guess high and it owes HMRC. So a part-refunded donation leaves
    the claim entirely and is listed on the claim page as left out, for
    a person to decide about.
  * The webhook NEVER issues a refund. Nothing in this codebase calls
    Stripe's refund API.

Run:  python tests/smoke_test_refunds.py
"""
import os
import sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_refunds.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

import app as appmod                                            # noqa: E402
from app import (app, db, AuditLog, Block, Campaign,            # noqa: E402
                 DEFAULT_BLOCKS, FEATURES, FeatureFlag, Payment, User,
                 gift_aid_claimable_query, gift_aid_excluded_by_refund,
                 record_refund, refund_status_for)

app.config["TESTING"] = True

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PW = "refund-test-password"
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def fire(event_type, obj):
    """Drive the real webhook with the signature check stubbed."""
    original = appmod.stripe.Webhook.construct_event
    appmod.stripe.Webhook.construct_event = staticmethod(
        lambda *a, **k: {"type": event_type, "data": {"object": obj}})
    try:
        return client.post("/stripe/webhook", data=b"{}",
                           headers={"Stripe-Signature": "t=1,v1=x"})
    finally:
        appmod.stripe.Webhook.construct_event = original


def add_payment(**kw):
    with app.app_context():
        p = Payment(**kw)
        db.session.add(p)
        db.session.commit()
        return p.id


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        db.session.add(Block(group=group, key=key, label=label, kind=kind,
                             value=value))
    for n, _l, _d, _default in FEATURES:
        db.session.add(FeatureFlag(name=n, enabled=True))
    u = User(email="admin@example.com")
    u.set_password(PW)
    u.role = "super_admin"
    db.session.add(u)
    camp = Campaign(title="Seaside trip", slug="seaside-trip",
                    description="A day at the coast.", fee_pence=1500,
                    target_pence=50000, state="open")
    db.session.add(camp)
    db.session.commit()
    CAMP = camp.id

client = app.test_client()
client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})

# Three payments into one campaign: two that stay, one to refund.
KEEP = add_payment(campaign_id=CAMP, name="Stays Put",
                   email="stays@example.org", fee_pence=1500,
                   donation_pence=2500, gift_aid=True,
                   gift_aid_name="Stays Put", gift_aid_address="1 Road",
                   gift_aid_postcode="EN1 1AA", status="complete",
                   stripe_session_id="cs_keep",
                   stripe_payment_intent="pi_keep")
GONE = add_payment(campaign_id=CAMP, name="Gets Refunded",
                   email="gone@example.org", fee_pence=1500,
                   donation_pence=3500, gift_aid=True,
                   gift_aid_name="Gets Refunded", gift_aid_address="2 Road",
                   gift_aid_postcode="EN2 2BB", status="complete",
                   stripe_session_id="cs_gone",
                   stripe_payment_intent="pi_gone")
PART = add_payment(campaign_id=CAMP, name="Partly Refunded",
                   email="part@example.org", fee_pence=1500,
                   donation_pence=2500, gift_aid=True,
                   gift_aid_name="Partly Refunded", gift_aid_address="3 Road",
                   gift_aid_postcode="EN3 3CC", status="complete",
                   stripe_session_id="cs_part",
                   stripe_payment_intent="pi_part")

with app.app_context():
    before_total = db.session.get(Campaign, CAMP).raised_pence
    before_count = db.session.get(Campaign, CAMP).contributor_count
    before_claim = sum(p.gift_aid_pence
                       for p in gift_aid_claimable_query().all())
check("to start with, all three are counted", before_total == 13000,
      str(before_total))
check("...all three are contributors", before_count == 3, str(before_count))
check("...and all three donations are claimable", before_claim == 8500,
      str(before_claim))

# ------------------------------------------------- charge.refunded, in full
r = fire("charge.refunded", {"id": "ch_gone", "payment_intent": "pi_gone",
                             "amount": 5000, "amount_refunded": 5000})
with app.app_context():
    p = db.session.get(Payment, GONE)
    status, refunded, net = p.refund_status, p.refunded_pence, p.net_pence
    ga = p.gift_aid_pence
check("charge.refunded is accepted", r.status_code == 200)
check("...and marks the payment refunded", status == "refunded", status)
check("...with the amount that came back", refunded == 5000, str(refunded))
check("...so its net is nothing", net == 0, str(net))
check("ITS GIFT AID GOES TO ZERO ON THE MODEL ITSELF", ga == 0, str(ga))

with app.app_context():
    total = db.session.get(Campaign, CAMP).raised_pence
    count = db.session.get(Campaign, CAMP).contributor_count
    claim = sum(p.gift_aid_pence for p in gift_aid_claimable_query().all())
    claim_names = [p.name for p in gift_aid_claimable_query().all()]
check("A REFUNDED PAYMENT IS OUT OF THE CAMPAIGN TOTAL",
      total == 8000, "%s — the refunded £50 is still counted" % total)
check("...and is no longer a contributor", count == 2, str(count))
check("A REFUNDED DONATION IS OUT OF THE GIFT AID CLAIM",
      claim == 5000 and "Gets Refunded" not in claim_names,
      "%s / %s" % (claim, claim_names))

# ...but it is still on the page, marked, rather than vanishing
page = client.get("/admin/campaigns/%d/contributors" % CAMP).data.decode("utf-8")
check("IT IS STILL VISIBLE IN THE ADMIN", "Gets Refunded" in page)
check("...marked as refunded", "Refunded" in page)
check("...showing what came back",
      "less £50 refunded" in page or "£50 refunded" in page,
      page[page.find("Gets Refunded"):][:400])

# ---- a replay changes nothing and does not log twice
with app.app_context():
    logs_before = AuditLog.query.count()
fire("charge.refunded", {"id": "ch_gone", "payment_intent": "pi_gone",
                         "amount": 5000, "amount_refunded": 5000})
with app.app_context():
    logs_after = AuditLog.query.count()
    still = db.session.get(Campaign, CAMP).raised_pence
check("a replayed refund is a no-op", logs_after == logs_before
      and still == 8000, "%d -> %d, total %s"
      % (logs_before, logs_after, still))

# ------------------------------------- payment_intent.refunded, same fact
r = fire("payment_intent.refunded",
         {"id": "pi_part", "amount": 4000, "amount_refunded": 1000})
with app.app_context():
    p = db.session.get(Payment, PART)
    pstatus, prefunded, pnet = p.refund_status, p.refunded_pence, p.net_pence
    pga = p.gift_aid_pence
check("payment_intent.refunded is understood too", r.status_code == 200)
check("A PART REFUND IS CALLED PARTIAL, not refunded",
      pstatus == "partial", pstatus)
check("...with the amount that came back", prefunded == 1000, str(prefunded))
check("...and a net of what is left", pnet == 3000, str(pnet))
check("PART REFUNDED MEANS NO GIFT AID AT ALL — the split cannot be "
      "guessed at", pga == 0, str(pga))

with app.app_context():
    total = db.session.get(Campaign, CAMP).raised_pence
    count = db.session.get(Campaign, CAMP).contributor_count
    claim = sum(p.gift_aid_pence for p in gift_aid_claimable_query().all())
check("the campaign total is net of the part refund", total == 7000,
      str(total))
check("...but they are still a contributor, having given something",
      count == 2, str(count))
check("...and the claim is only the untouched donation", claim == 2500,
      str(claim))

# ---- and the claim page SAYS what it left out
gift = client.get("/admin/gift-aid").data.decode("utf-8")
check("the claim page lists what refunds took out of it",
      "Left out of this claim" in gift)
check("...naming them", "Gets Refunded" in gift and "Partly Refunded" in gift)
check("...and saying why a partial one cannot be split",
      "Nothing in a refund says" in gift or "came back" in gift,
      gift[gift.find("Left out"):][:400])
with app.app_context():
    left = [p.name for p in gift_aid_excluded_by_refund().all()]
check("the excluded query finds both", sorted(left) ==
      ["Gets Refunded", "Partly Refunded"], str(left))

# ---- and the HMRC CSV carries neither
csv_out = client.get("/admin/gift-aid.csv").data.decode("utf-8")
check("THE HMRC EXPORT CONTAINS NO REFUNDED DONATION",
      "Gets Refunded" not in csv_out and "Partly Refunded" not in csv_out,
      csv_out[:400])
check("...but does contain the one that stands",
      "Stays,Put" in csv_out,
      "the HMRC schema splits the name across two columns")

# ---------------------------------------- a refund we cannot match to a row
with app.app_context():
    before_logs = AuditLog.query.count()
r = fire("charge.refunded", {"id": "ch_unknown",
                             "payment_intent": "pi_not_ours",
                             "amount": 999, "amount_refunded": 999})
with app.app_context():
    said = [e.summary for e in AuditLog.query.all()][-1]
check("an unmatched refund is not silently dropped",
      r.status_code == 200 and "does not recognise" in said, said)

# ------------------------------------------------- recording one by hand
# For a refund made before the payment intent was being stored, or one
# the webhook missed. THIS DOES NOT REFUND ANYBODY.
OLD = add_payment(campaign_id=CAMP, name="Refunded Long Ago",
                  email="old@example.org", fee_pence=1500,
                  donation_pence=1500, gift_aid=True,
                  gift_aid_name="Refunded Long Ago",
                  gift_aid_address="4 Road", gift_aid_postcode="EN4 4DD",
                  status="complete", stripe_session_id="cs_old")
r = client.post("/admin/payments/%d/refunded" % OLD,
                data={"amount": "30.00",
                      "refunded_on": date.today().isoformat(),
                      "refund_note": "Refunded in Stripe last year"},
                follow_redirects=True)
with app.app_context():
    p = db.session.get(Payment, OLD)
    ostatus, orefunded, oby = p.refund_status, p.refunded_pence, p.refunded_by
    onote = p.refund_note
check("a refund can be recorded by hand", ostatus == "refunded", ostatus)
check("...for the amount given", orefunded == 3000, str(orefunded))
check("...naming the admin who recorded it", oby == "admin@example.com", oby)
check("...and their note", onote == "Refunded in Stripe last year", onote)
with app.app_context():
    said = [e.summary for e in AuditLog.query.all()
            if "Recorded a refund" in e.summary]
check("...and it is audit-logged like every other money action",
      said and "£30" in said[-1], str(said))

# A refund bigger than the payment is refused rather than making every
# net figure on the site wrong.
BIG = add_payment(campaign_id=CAMP, name="Too Much", email="big@example.org",
                  fee_pence=1000, donation_pence=0, status="complete",
                  stripe_session_id="cs_big")
r = client.post("/admin/payments/%d/refunded" % BIG,
                data={"amount": "50.00",
                      "refunded_on": date.today().isoformat()},
                follow_redirects=True)
with app.app_context():
    bstatus = db.session.get(Payment, BIG).refund_status
check("a refund larger than the payment is refused", bstatus == "none",
      bstatus)
check("...and says so", "more than the" in r.data.decode("utf-8"))

# A future date is refused, as everywhere else money is recorded.
r = client.post("/admin/payments/%d/refunded" % BIG,
                data={"amount": "10.00", "refunded_on": "2099-01-01"},
                follow_redirects=True)
with app.app_context():
    bstatus = db.session.get(Payment, BIG).refund_status
check("a refund dated in the future is refused", bstatus == "none", bstatus)

# ---------------------------------------------- the shared mechanism itself
check("refund_status_for: nothing", refund_status_for(0, 1000) == "none")
check("refund_status_for: some", refund_status_for(400, 1000) == "partial")
check("refund_status_for: all", refund_status_for(1000, 1000) == "refunded")
check("refund_status_for: more than all is still just refunded",
      refund_status_for(5000, 1000) == "refunded")

# ONE implementation writes both tables — the membership refund and this
# one are the same function, not two that look alike.
from app import MembershipPayment                                # noqa: E402
with app.app_context():
    mp = MembershipPayment(amount_pence=1000, period_end_year=2027,
                           method="card", received_on=date.today(),
                           status="complete")
    db.session.add(mp)
    db.session.commit()
    got = record_refund(mp, 1000, mp.amount_pence, on=date.today(),
                        by="somebody", note="x")
    db.session.commit()
    mstatus, mpence = mp.refund_status, mp.refunded_pence
check("record_refund writes a membership payment the same way",
      got == "refunded" and mstatus == "refunded" and mpence == 1000,
      "%s / %s / %s" % (got, mstatus, mpence))
check("...and clamps an over-large amount rather than storing it",
      record_refund(mp, 99999, mp.amount_pence) == "refunded"
      and mp.refunded_pence == 1000, str(mp.refunded_pence))

# ---- NOTHING here ever issues a refund
src = open(os.path.join(os.path.dirname(HERE), "app.py"),
           encoding="utf-8").read()
check("THE APP NEVER ISSUES A REFUND — no call to Stripe's refund API",
      "Refund.create" not in src and "refunds.create" not in src
      and "stripe.Refund" not in src)

# ------------------------------------------------------------- teardown
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
