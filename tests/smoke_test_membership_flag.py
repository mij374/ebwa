"""What the membership_fees flag covers, and what it deliberately does not.

The rule this project already keeps is that switching a feature off
hides a MODULE and never strands CONTENT. Membership is the case where
that line has to be drawn carefully, because a payment somebody has
already made is not a feature — it is a fact about the accounts, and a
treasurer whose year stops adding up because somebody flicked a switch
has been failed by the software.

So, with membership_fees OFF:
  * /membership/pay 404s — nobody is asked for money;
  * the fee settings section is off the Settings page;
  * "Record a payment" is not offered, and the route refuses;
  * the dashboard does not chase anybody about renewals.
And, still with it OFF:
  * the member list, a member's page, add, edit, suspend, left, delete
    and the exports all work exactly as before;
  * payments already recorded are still listed, still counted, still in
    the treasurer's report;
  * THE STRIPE WEBHOOK STILL COMPLETES A PAYMENT IN FLIGHT, because
    somebody mid-checkout has already been charged.

It also pins the four combinations with membership_form, since a state
that only happens by accident is a state nobody has thought about.

Run:  python tests/smoke_test_membership_flag.py
"""
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_member_flag.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,      # noqa: E402
                 FEATURE_DEFAULTS, FeatureFlag, Member,
                 MembershipPayment, User)

app.config["TESTING"] = True

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PW = "member-flag-password"
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def set_flag(name, on):
    with app.app_context():
        FeatureFlag.query.filter_by(name=name).first().enabled = on
        db.session.commit()


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        db.session.add(Block(group=group, key=key, label=label, kind=kind,
                             value=value))
    for n, _l, _d, default in FEATURES:
        db.session.add(FeatureFlag(name=n, enabled=default))
    u = User(email="boss@example.com")
    u.set_password(PW)
    u.role = "super_admin"
    db.session.add(u)
    member = Member(name="A Paid Member", email="paid@example.org")
    db.session.add(member)
    db.session.commit()
    db.session.add(MembershipPayment(
        member_id=member.id, amount_pence=1000, period_end_year=2027,
        method="cash", received_on=date(2026, 1, 5), status="complete"))
    db.session.commit()
    MEMBER_ID = member.id

client = app.test_client()

# ---- IT SHIPS OFF. Nothing changes until somebody decides it should.
check("membership_fees defaults to OFF",
      FEATURE_DEFAULTS["membership_fees"] is False)
with app.app_context():
    seeded = FeatureFlag.query.filter_by(name="membership_fees").first()
check("...and is seeded off", seeded is not None and seeded.enabled is False)

client.post("/admin/login", data={"email": "boss@example.com",
                                  "password": PW})

# ---------------------------------------------------------------- OFF
set_flag("membership_fees", False)

check("off: /membership/pay 404s", client.get("/membership/pay").status_code
      == 404, str(client.get("/membership/pay").status_code))
check("off: the Stripe return page 404s too",
      client.get("/membership/paid").status_code == 404)

settings = client.get("/admin/features").data.decode("utf-8")
check("off: the fee settings section is not on Settings",
      'id="membership"' not in settings)

r = client.post("/admin/members/%d/payments" % MEMBER_ID,
                data={"method": "cash", "amount": "10.00",
                      "received_on": date.today().isoformat(),
                      "period_end_year": "2027"})
check("off: recording a payment is refused by the ROUTE, not just hidden",
      r.status_code == 404, str(r.status_code))

page = client.get("/admin/members/%d" % MEMBER_ID).data.decode("utf-8")
check("off: no Record a payment form is offered",
      "Record payment" not in page)

# ---- AND YET THE RECORDS ARE ALL STILL THERE
check("off: the member list still opens",
      client.get("/admin/members").status_code == 200)
check("off: a member's own page still opens",
      client.get("/admin/members/%d" % MEMBER_ID).status_code == 200)
check("off: the add form still opens",
      client.get("/admin/members/new").status_code == 200)
check("off: the edit form still opens",
      client.get("/admin/members/%d/edit" % MEMBER_ID).status_code == 200)
check("off: the treasurer's report still opens",
      client.get("/admin/members/renewals").status_code == 200)
csv = client.get("/admin/members.csv")
check("off: the export still works", csv.status_code == 200)
check("OFF: A PAYMENT ALREADY TAKEN IS STILL LISTED",
      "£10" in page, page[page.find("Payments"):][:220])
renewals = client.get("/admin/members/renewals").data.decode("utf-8")
check("off: and still counted in the treasurer's totals",
      "£10" in renewals, renewals[renewals.find("collected") - 220:][:280])
check("off: the report says why nobody is being chased",
      "switched off" in renewals)

# ---- the sidebar link is not hidden either: the roll is not a module
admin = client.get("/admin").data.decode("utf-8")
check("off: the Members link stays in the sidebar",
      "/admin/members" in admin)
check("off: the dashboard does not chase anybody about renewals",
      "renew" not in admin.lower() or "not yet renewed" not in admin.lower())

# ---- suspending and leaving still work with fees off
r = client.post("/admin/members/%d/standing" % MEMBER_ID,
                data={"standing": "suspended"}, follow_redirects=True)
with app.app_context():
    standing = db.session.get(Member, MEMBER_ID).standing
check("off: suspending a member still works", standing == "suspended",
      standing)
client.post("/admin/members/%d/standing" % MEMBER_ID, data={"standing": ""})

# ---------------------------------------------------------------- ON
set_flag("membership_fees", True)
check("on: /membership/pay opens",
      client.get("/membership/pay").status_code == 200)
settings = client.get("/admin/features").data.decode("utf-8")
check("on: the fee settings section appears", 'id="membership"' in settings)
page = client.get("/admin/members/%d" % MEMBER_ID).data.decode("utf-8")
check("on: Record a payment is offered", "Record payment" in page)

pay = client.get("/membership/pay").data.decode("utf-8")
check("THE PAYMENT FORM STATES THAT FEES ARE NON-REFUNDABLE",
      "non-refundable" in pay.lower(), pay[:200])
check("...and says what period the money buys", "30 September" in pay)

# ---- the two flags are independent, and all four states are meaningful
for form_on, fees_on, meaning in (
        (True, True, "applications open and members can pay"),
        (True, False, "applications open, nobody asked to pay (the "
                      "default, and today's behaviour)"),
        (False, True, "applications closed, existing members still renew"),
        (False, False, "the whole module off publicly")):
    set_flag("membership_form", form_on)
    set_flag("membership_fees", fees_on)
    apply_code = client.get("/membership").status_code
    pay_code = client.get("/membership/pay").status_code
    check("form=%s fees=%s: %s" % (form_on, fees_on, meaning),
          apply_code == (200 if form_on else 404)
          and pay_code == (200 if fees_on else 404),
          "apply %s, pay %s" % (apply_code, pay_code))
    # The records are reachable in every one of the four.
    check("form=%s fees=%s: the member records are still reachable"
          % (form_on, fees_on),
          client.get("/admin/members").status_code == 200)

# APPLICATIONS ALREADY RECEIVED STILL BECOME MEMBERS with the form off —
# the same rule as the dashboard's unread-enquiry check. Switching the
# form off stops new applications arriving; it does not process the ones
# already sent.
set_flag("membership_form", False)
from app import MembershipApplication                            # noqa: E402
with app.app_context():
    a = MembershipApplication(name="Late Applicant",
                              email="late@example.org",
                              over_18=True, bangladeshi_origin=True,
                              lives_works_enfield=True, fee_confirmed=True)
    db.session.add(a)
    db.session.commit()
    a_id = a.id
r = client.post("/admin/membership/%d/make-member" % a_id,
                follow_redirects=True)
with app.app_context():
    made = Member.query.filter_by(application_id=a_id).first()
check("an application still becomes a member with the form switched off",
      made is not None, str(r.status_code))

# ---- THE WEBHOOK IS NOT GATED. Somebody mid-checkout when the flag went
# off has already been charged; the one outcome worse than the feature
# being on is taking their money and recording nothing.
set_flag("membership_fees", False)
with app.app_context():
    pending = MembershipPayment(
        member_id=MEMBER_ID, amount_pence=1000, period_end_year=2028,
        method="card", received_on=date.today(), status="pending",
        stripe_session_id="cs_test_inflight")
    db.session.add(pending)
    db.session.commit()
    pending_id = pending.id

import app as appmod                                             # noqa: E402


class _FakeEvent(dict):
    pass


original = appmod.stripe.Webhook.construct_event
appmod.stripe.Webhook.construct_event = staticmethod(
    lambda *a, **k: {"type": "checkout.session.completed",
                     "data": {"object": {"id": "cs_test_inflight"}}})
try:
    r = client.post("/stripe/webhook", data=b"{}",
                    headers={"Stripe-Signature": "t=1,v1=x"})
finally:
    appmod.stripe.Webhook.construct_event = original
with app.app_context():
    done = db.session.get(MembershipPayment, pending_id).status
check("A PAYMENT IN FLIGHT IS STILL COMPLETED WITH THE FLAG OFF",
      r.status_code == 200 and done == "complete",
      "%s / %s" % (r.status_code, done))

# ---- teardown
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
