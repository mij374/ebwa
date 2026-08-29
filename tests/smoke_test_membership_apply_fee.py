"""The fee is taken when somebody APPLIES, and what happens to it after.

The money now arrives before there is a member for it to belong to, so
this file is mostly about one question: can a single £10 ever end up
counted twice, counted when it should not be, or lost?

  * IT HANGS ON THE APPLICATION and approval sets `member_id` on the SAME
    row. There is never a second payment record for one payment, so no
    pair of rows can disagree about whether somebody paid.
  * THE APPLICATION IS SAVED BEFORE STRIPE. Somebody who closes the tab
    on the payment page has still applied, and shows as unpaid.
  * A DECLINED APPLICATION'S FEE IS NEVER MEMBERSHIP INCOME, before or
    after it is refunded, and never gives anybody cover.
  * THE FLAG FALLBACK: with membership_fees off the form behaves exactly
    as it did before there was a fee — no payment, same thank-you, and
    certainly not a 404.

Stripe is faked at the boundary: `stripe.checkout.Session.create` is
replaced so no network call happens, and the webhook is driven with the
signature check stubbed. What is real is every row this app writes.

Run:  python tests/smoke_test_membership_apply_fee.py
"""
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_apply_fee.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

import app as appmod                                            # noqa: E402
from app import (app, db, AuditLog, Block, DEFAULT_BLOCKS,      # noqa: E402
                 FEATURES, FeatureFlag, Member, MembershipApplication,
                 MembershipPayment, User, membership_income_query,
                 membership_period_now, renewal_deadline)

app.config["TESTING"] = True

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PW = "apply-fee-password"
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


# ---- Stripe, faked at the boundary and nowhere else
SESSIONS = []


class _Session(object):
    def __init__(self, n):
        self.id = "cs_apply_%d" % n
        self.url = "https://checkout.stripe.test/%s" % self.id


def fake_create(**kwargs):
    SESSIONS.append(kwargs)
    return _Session(len(SESSIONS))


appmod.stripe.checkout.Session.create = staticmethod(fake_create)


def complete(session_id):
    """Drive the real webhook for a session, as Stripe would."""
    original = appmod.stripe.Webhook.construct_event
    appmod.stripe.Webhook.construct_event = staticmethod(
        lambda *a, **k: {"type": "checkout.session.completed",
                         "data": {"object": {"id": session_id}}})
    try:
        return client.post("/stripe/webhook", data=b"{}",
                           headers={"Stripe-Signature": "t=1,v1=x"})
    finally:
        appmod.stripe.Webhook.construct_event = original


APPLICANT = {"name": "A New Applicant", "email": "applicant@example.org",
             "phone": "020 8804 4006", "address": "1 Somewhere Road",
             "reason": "I would like to help.",
             "over_18": "on", "bangladeshi_origin": "on",
             "lives_works_enfield": "on", "fee_confirmed": "on"}


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        db.session.add(Block(group=group, key=key, label=label, kind=kind,
                             value=value))
    for n, _l, _d, default in FEATURES:
        db.session.add(FeatureFlag(name=n, enabled=default))
    u = User(email="admin@example.com")
    u.set_password(PW)
    u.role = "super_admin"
    db.session.add(u)
    db.session.commit()

client = app.test_client()

# ------------------------------------------------ the flag fallback first
# With fees OFF the form is the form it has always been. It must not 404
# and it must not ask anybody for money.
set_flag("membership_fees", False)
r = client.get("/membership")
check("fees off: the application form still opens", r.status_code == 200,
      str(r.status_code))
page = r.data.decode("utf-8")
check("fees off: it does not mention a fee to pay now",
      "payable now" not in page)
r = client.post("/membership", data=dict(APPLICANT, email="nofee@example.org"),
                follow_redirects=True)
with app.app_context():
    a = MembershipApplication.query.filter_by(email="nofee@example.org").first()
    saved = a is not None
    unpaid = saved and not a.payments
check("fees off: the application is still saved", saved)
check("fees off: with no payment attached", unpaid)
check("fees off: and the old thank-you is shown",
      # Jinja escapes the apostrophe, so match on what is on the page.
      "received your application" in r.data.decode("utf-8"))
check("fees off: nothing was sent to Stripe", not SESSIONS, str(len(SESSIONS)))

# ---------------------------------------------------- fees on: apply + pay
set_flag("membership_fees", True)
page = client.get("/membership").data.decode("utf-8")
check("fees on: the form says what the fee is", "£10" in page, page[:200])
check("fees on: and says it is payable now", "payable now" in page)
check("THE FORM DOES NOT CLAIM MORE THAN THE POLICY — it says a declined "
      "application is refunded",
      "not approve your application" in page and "refund" in page.lower(),
      page[page.find("apply-fee"):][:400])

r = client.post("/membership", data=APPLICANT)
check("applying redirects to Stripe", r.status_code == 303,
      str(r.status_code))
check("...with the fee, in pence, in the line item",
      SESSIONS and SESSIONS[-1]["line_items"][0]["price_data"]["unit_amount"]
      == 1000, str(SESSIONS[-1]["line_items"][0] if SESSIONS else None))

with app.app_context():
    appl = MembershipApplication.query.filter_by(
        email="applicant@example.org").first()
    APPL_ID = appl.id
    appl_name = appl.name
    pays = [(p.status, p.application_id, p.member_id, p.period_end_year)
            for p in appl.payments]
check("THE APPLICATION IS SAVED BEFORE STRIPE, not after",
      appl_name == "A New Applicant")
check("...with a pending payment against it",
      len(pays) == 1 and pays[0][0] == "pending", str(pays))
check("...attached to the application and to no member",
      pays and pays[0][1] == APPL_ID and pays[0][2] is None, str(pays))
check("...for the period the join-date rule gives",
      pays and pays[0][3] == membership_period_now(), str(pays))

# A pending payment is not income and buys no cover.
with app.app_context():
    income = membership_income_query().all()
check("an unpaid application is not membership income", income == [],
      str(income))

# ---- and the admin can see they have not paid
client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})
listing = client.get("/admin/membership").data.decode("utf-8")
check("the admin list shows the fee as not paid", "Not paid" in listing,
      listing[listing.find("A New Applicant"):][:400])

# ---- Stripe confirms it
complete("cs_apply_1")
with app.app_context():
    status = MembershipPayment.query.filter_by(
        application_id=APPL_ID).first().status
check("the webhook completes the application's payment",
      status == "complete", status)
listing = client.get("/admin/membership").data.decode("utf-8")
check("...and the admin list now shows it paid", "£10 paid" in listing,
      listing[listing.find("A New Applicant"):][:400])

# --------------------------------------------- approval carries the money
r = client.post("/admin/membership/%d/make-member" % APPL_ID,
                follow_redirects=True)
with app.app_context():
    member = Member.query.filter_by(application_id=APPL_ID).first()
    made = member is not None
    m_id = member.id if made else None
    m_status = member.status if made else ""
    m_cover = member.covered_to_year if made else None
    m_upto = member.paid_up_to if made else None
    rows = [(p.member_id, p.application_id) for p in
            MembershipPayment.query.filter_by(application_id=APPL_ID).all()]
check("approving creates the member", made)
check("THE PAYMENT CARRIES ACROSS — one row, now on the member",
      len(rows) == 1 and rows[0][0] == m_id, str(rows))
check("...and it still remembers the application it came from",
      rows and rows[0][1] == APPL_ID, str(rows))
check("...so the member is paid up without anybody re-entering anything",
      m_status == "current" and m_cover == membership_period_now(),
      "%s / %s" % (m_status, m_cover))
check("...to the date the 1-June rule gives",
      m_upto == renewal_deadline(membership_period_now()), str(m_upto))
with app.app_context():
    income = [p.amount_pence for p in membership_income_query().all()]
check("and it is membership income now that it belongs to a member",
      income == [1000], str(income))

# ------------------------------------------------- decline, and the refund
with app.app_context():
    turned = MembershipApplication(
        name="Turned Down", email="turned@example.org",
        over_18=True, bangladeshi_origin=True,
        lives_works_enfield=True, fee_confirmed=True)
    db.session.add(turned)
    db.session.commit()
    db.session.add(MembershipPayment(
        application_id=turned.id, amount_pence=1000,
        period_end_year=membership_period_now(), method="card",
        received_on=date.today(), status="complete",
        stripe_session_id="cs_apply_declined"))
    db.session.commit()
    TURNED_ID = turned.id

with app.app_context():
    before = [p.amount_pence for p in membership_income_query().all()]
check("a paid application is NOT income while it is undecided",
      before == [1000], str(before))

r = client.post("/admin/membership/%d/decline" % TURNED_ID,
                data={"reason": "Lives outside the borough."},
                follow_redirects=True)
with app.app_context():
    turned = db.session.get(MembershipApplication, TURNED_ID)
    t_status, t_by = turned.status, turned.decided_by
    t_at, t_note = turned.decided_at, turned.decision_note
    pay = MembershipPayment.query.filter_by(
        application_id=TURNED_ID).first()
    PAY_ID, pay_refund = pay.id, pay.refund_status
check("declining records the decision", t_status == "declined", t_status)
check("...who made it", t_by == "admin@example.com", t_by)
check("...when", t_at is not None)
check("...and why, in their own words",
      t_note == "Lives outside the borough.", t_note)
check("THE FEE IS MARKED AS OWED BACK", pay_refund == "due", pay_refund)
check("...and the admin is told to refund it",
      "owed back" in r.data.decode("utf-8"))

with app.app_context():
    income = [p.amount_pence for p in membership_income_query().all()]
check("A DECLINED APPLICATION'S FEE IS NOT MEMBERSHIP INCOME",
      income == [1000], "%s — the £10 owed back is still being counted"
                        % income)

# ---- it is on the dashboard, as the thing that generates a complaint
dash = client.get("/admin").data.decode("utf-8")
check("the dashboard says the money has not gone back",
      "not yet returned" in dash, dash[dash.find("attention"):][:400])

# ---- recording the refund
r = client.post("/admin/membership/payments/%d/refunded" % PAY_ID,
                data={"refunded_on": date.today().isoformat(),
                      "refund_note": "Refunded in Stripe"},
                follow_redirects=True)
with app.app_context():
    pay = MembershipPayment.query.filter_by(
        application_id=TURNED_ID).first()
    r_status, r_on = pay.refund_status, pay.refunded_on
    r_by, r_note = pay.refunded_by, pay.refund_note
check("recording the refund marks it refunded", r_status == "refunded",
      r_status)
check("...with the date it was made", r_on == date.today(), str(r_on))
check("...and who actioned it", r_by == "admin@example.com", r_by)
check("...and the note", r_note == "Refunded in Stripe", r_note)
dash = client.get("/admin").data.decode("utf-8")
check("the dashboard stops asking once it is recorded",
      "not yet returned" not in dash)

with app.app_context():
    income = [p.amount_pence for p in membership_income_query().all()]
check("a refunded fee is still not income", income == [1000], str(income))

# ...and never gives anybody cover
with app.app_context():
    orphan = Member(name="Should Not Be Covered")
    db.session.add(orphan)
    db.session.commit()
    refunded = MembershipPayment.query.filter_by(
        application_id=TURNED_ID).first()
    refunded.member_id = orphan.id     # as if somebody wired it up by hand
    db.session.commit()
    db.session.refresh(orphan)
    covered = orphan.covered_to_year
check("A REFUNDED PAYMENT BUYS NO COVER, even attached to a member",
      covered is None, str(covered))

# ---- money moving is audit-logged, both halves
with app.app_context():
    logs = [e.summary for e in AuditLog.query.all()]
check("the decline is audit-logged with the amount owed",
      any("Declined the membership application from Turned Down" in x
          and "£10" in x for x in logs), str(logs[-4:]))
check("the refund is audit-logged with the date and the person",
      any("Recorded a refund of £10" in x for x in logs), str(logs[-4:]))
with app.app_context():
    entry = (AuditLog.query.filter(AuditLog.summary.like("%Recorded a refund%"))
             .first())
check("...against the admin who recorded it",
      entry is not None and entry.user_email == "admin@example.com",
      entry.user_email if entry else "")

# ---- a declined application cannot then be made into a member
r = client.post("/admin/membership/%d/make-member" % TURNED_ID,
                follow_redirects=True)
with app.app_context():
    made_from_declined = (Member.query
                          .filter_by(application_id=TURNED_ID).count())
check("a declined applicant is not quietly made a member anyway",
      made_from_declined == 0,
      "%d member(s) created from a declined application"
      % made_from_declined)

# ---- and declining somebody who is already a member is refused
r = client.post("/admin/membership/%d/decline" % APPL_ID,
                data={"reason": "changed our minds"},
                follow_redirects=True)
with app.app_context():
    still = db.session.get(MembershipApplication, APPL_ID).status
check("an application already made into a member cannot be declined",
      still == "approved", still)

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
