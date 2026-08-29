"""Membership fees and renewals: the date rules and the money.

The dates are the part worth testing hardest, because every one of them
is a rule somebody stated in a sentence and they only agree with each
other by construction:

  * SEPTEMBER HAS 30 DAYS. The deadline is the 30th, and a date(y, 9, 31)
    written anywhere would raise rather than being wrong quietly.
  * JOIN ON OR AFTER 1 JUNE and you are covered to the FOLLOWING
    September — you do not pay twice in one autumn.
  * The renewal window is 1-30 September, then a grace period, then
    lapsed.
  * A MEMBER WITH NO PAYMENT AT ALL IS `unknown`, NOT `lapsed`. The
    seventeen existing members start that way and calling them lapsed
    would be an accusation the data does not support.
  * A MANUAL STANDING IS NEVER OVERWRITTEN by the passage of time: a
    suspended member stays suspended through as many Septembers as it
    takes, because nothing writes a derived status anywhere.
  * A CASH PAYMENT AND A CARD PAYMENT MOVE THE STATUS IDENTICALLY. The
    method is the only difference, and the test proves it by building
    both and comparing.
  * GIFT AID CANNOT ATTACH. Not "is refused" — there is nowhere to put
    it, and no membership money can reach the Gift Aid claim.

Runs against a throwaway SQLite db, so instance/ebwa.db is never touched.

Run:  python tests/smoke_test_membership_fees.py
"""
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_member_fees.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

import app as appmod                                            # noqa: E402
from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,      # noqa: E402
                 FeatureFlag, Member, MembershipApplication,
                 MembershipPayment, Payment, User,
                 EXISTING_MEMBERS, MEMBER_STATUS_LABELS,
                 derived_member_status, membership_year_for,
                 renewal_deadline, renewal_opens, membership_grace_ends,
                 gift_aid_claimable_query)

app.config["TESTING"] = True

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PW = "member-fees-password"
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


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

# ---------------------------------------------------------- the calendar
check("September has 30 days and the deadline is the 30th",
      renewal_deadline(2026) == date(2026, 9, 30))
check("the window opens on 1 September",
      renewal_opens(2026) == date(2026, 9, 1))
try:
    date(2026, 9, 31)
    thirty_one = True
except ValueError:
    thirty_one = False
check("there is no 31 September to be written by accident", not thirty_one)

# ---- the join-date rule, at its edges
check("join 1 June 2026 -> covered to September 2027",
      membership_year_for(date(2026, 6, 1)) == 2027)
check("join 31 May 2026 -> covered to September 2026 (renews that autumn)",
      membership_year_for(date(2026, 5, 31)) == 2026)
check("join 30 September 2026 -> covered to September 2027",
      membership_year_for(date(2026, 9, 30)) == 2027)
check("paying in the September window buys the FOLLOWING year",
      membership_year_for(date(2026, 9, 15)) == 2027)
check("join 1 January 2026 -> covered to September 2026",
      membership_year_for(date(2026, 1, 1)) == 2026)

# ---- and what that means for somebody who joins in June
with app.app_context():
    june = derived_member_status(membership_year_for(date(2026, 6, 1)),
                                 today=date(2026, 9, 15))
check("A JUNE JOINER IS NOT ASKED TO PAY IN THAT SEPTEMBER",
      june == "current", june)
with app.app_context():
    may = derived_member_status(membership_year_for(date(2026, 5, 31)),
                                today=date(2026, 9, 15))
check("...but a May joiner is", may == "due", may)

# ---- the window, the grace period, and the cliff after it
with app.app_context():
    grace_end = membership_grace_ends(2026)
check("grace runs one month past the deadline",
      grace_end == date(2026, 10, 30), str(grace_end))

CASES = [
    (date(2026, 8, 31), "current", "the day before the window opens"),
    (date(2026, 9, 1), "due", "the day the window opens"),
    (date(2026, 9, 30), "due", "the deadline itself — still covered"),
    (date(2026, 10, 1), "overdue", "the day after the deadline"),
    (date(2026, 10, 30), "overdue", "the last day of grace"),
    (date(2026, 10, 31), "lapsed", "the day after grace ends"),
    (date(2027, 6, 1), "lapsed", "long past"),
]
for day, expected, why in CASES:
    with app.app_context():
        got = derived_member_status(2026, today=day)
    check("%s (%s) -> %s" % (day.isoformat(), why, expected),
          got == expected, got)

# ---------------------------------------------- a member with no payments
with app.app_context():
    nobody = Member(name="No Payments Recorded")
    db.session.add(nobody)
    db.session.commit()
    status = nobody.status
    covered = nobody.covered_to_year
check("A MEMBER WITH NO PAYMENT IS 'unknown', NOT 'lapsed'",
      status == "unknown", status)
check("...and has no covered-to year at all, which is not year nought",
      covered is None, str(covered))
check("...and reads as 'No payment recorded' to a person",
      MEMBER_STATUS_LABELS["unknown"] == "No payment recorded")

# ---------------------------------------------------- manual vs derived
with app.app_context():
    sus = Member(name="Under Review", standing="suspended")
    db.session.add(sus)
    db.session.commit()
    # A payment history that WOULD read as lapsed if anything consulted it
    db.session.add(MembershipPayment(
        member_id=sus.id, amount_pence=1000, period_end_year=2020,
        method="cash", received_on=date(2019, 9, 10), status="complete"))
    db.session.commit()
    db.session.refresh(sus)
    suspended_status = sus.status
    underlying = derived_member_status(sus.covered_to_year,
                                       today=date(2026, 12, 1))
check("A SUSPENDED MEMBER STAYS SUSPENDED, however many Septembers pass",
      suspended_status == "suspended", suspended_status)
check("...even though the payments underneath say lapsed",
      underlying == "lapsed", underlying)
with app.app_context():
    sus = Member.query.filter_by(name="Under Review").first()
    sus.standing = ""
    db.session.commit()
    db.session.refresh(sus)
    reinstated = sus.status
check("...and reinstating hands the answer back to the payments",
      reinstated == "lapsed", reinstated)

with app.app_context():
    left = Member(name="Moved Away", standing="left")
    db.session.add(left)
    db.session.commit()
    left_status = left.status
check("a member who has left stays 'left'", left_status == "left")

# ---- nothing anywhere writes a derived status into a column
check("Member has no column that caches the derived status",
      not any(c.name in ("derived_status", "computed_status")
              for c in Member.__table__.columns)
      and "status" not in [c.name for c in Member.__table__.columns],
      str([c.name for c in Member.__table__.columns]))

# -------------------------------------------- cash and card are the same
client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})
with app.app_context():
    FeatureFlag.query.filter_by(name="membership_fees").first().enabled = True
    db.session.commit()

with app.app_context():
    payer_cash = Member(name="Paid In Cash", email="cash@example.org")
    payer_card = Member(name="Paid By Card", email="card@example.org")
    db.session.add_all([payer_cash, payer_card])
    db.session.commit()
    cash_id, card_id = payer_cash.id, payer_card.id

# the manual path, through the admin form a treasurer actually uses
# TODAY, not a date in September: the route refuses a payment dated in
# the future, which is right — money is received on a day that has
# happened — and the first draft of this test posted one and then read
# an empty list.
PAID_ON = date.today()
r = client.post("/admin/members/%d/payments" % cash_id,
                data={"method": "cash", "amount": "10.00",
                      "received_on": PAID_ON.isoformat(),
                      "period_end_year": "2027",
                      "received_by": "The treasurer"},
                follow_redirects=True)
with app.app_context():
    stored = MembershipPayment.query.filter_by(member_id=cash_id).count()
check("a cash payment is recorded", r.status_code == 200 and stored == 1,
      "%s, %d row(s)" % (r.status_code, stored))

# A future date is refused rather than quietly accepted.
r = client.post("/admin/members/%d/payments" % cash_id,
                data={"method": "cash", "amount": "10.00",
                      "received_on": (date.today()
                                      + timedelta(days=30)).isoformat(),
                      "period_end_year": "2027"},
                follow_redirects=True)
with app.app_context():
    after = MembershipPayment.query.filter_by(member_id=cash_id).count()
check("a payment dated in the future is refused", after == 1, str(after))

# the card path, exactly as the Stripe webhook completes it
with app.app_context():
    db.session.add(MembershipPayment(
        member_id=card_id, amount_pence=1000, period_end_year=2027,
        method="card", received_on=PAID_ON, status="complete",
        stripe_session_id="cs_test_membership_1"))
    db.session.commit()

with app.app_context():
    a = db.session.get(Member, cash_id)
    b = db.session.get(Member, card_id)
    same_status = a.status == b.status
    same_cover = a.covered_to_year == b.covered_to_year
    same_upto = a.paid_up_to == b.paid_up_to
    cash_method = a.payments[0].method
    card_method = b.payments[0].method
check("CASH AND CARD PRODUCE THE SAME STATUS", same_status)
check("...the same covered-to year", same_cover)
check("...the same paid-up-to date", same_upto)
check("...and differ in the method and nothing else",
      cash_method == "cash" and card_method == "card")

# ---- and the manual entry is audit-logged with who typed it
with app.app_context():
    from app import AuditLog
    entry = (AuditLog.query.filter(AuditLog.summary.like("%cash membership%"))
             .order_by(AuditLog.id.desc()).first())
    recorded = MembershipPayment.query.filter_by(member_id=cash_id).first()
check("the manual payment is audit-logged", entry is not None,
      "no audit entry naming a cash membership payment")
check("...naming the admin who entered it",
      entry is not None and entry.user_email == "admin@example.com",
      entry.user_email if entry else "")
check("...and naming who was handed the money",
      entry is not None and "The treasurer" in entry.summary,
      entry.summary if entry else "")
check("...and the row itself records who typed it",
      recorded is not None and recorded.recorded_by == "admin@example.com",
      recorded.recorded_by if recorded else "")

# ------------------------------------------------------------- Gift Aid
check("GIFT AID CANNOT BE PUT ON A MEMBERSHIP PAYMENT — there is no "
      "column for it",
      not any("gift" in c.name for c in MembershipPayment.__table__.columns),
      str([c.name for c in MembershipPayment.__table__.columns]))
with app.app_context():
    try:
        MembershipPayment(member_id=cash_id, amount_pence=1000,
                          period_end_year=2027, method="cash",
                          received_on=date.today(), gift_aid=True)
        took_it = True
    except TypeError:
        took_it = False
check("...and the model refuses the keyword outright", not took_it)

with app.app_context():
    # A real donation WITH Gift Aid, so the claim query is not empty for
    # the wrong reason — an assertion that finds nothing proves nothing.
    db.session.add(Payment(
        campaign_id=None, name="A Donor", email="donor@example.org",
        fee_pence=0, donation_pence=5000, gift_aid=True,
        gift_aid_name="A Donor", gift_aid_address="1 Road",
        gift_aid_postcode="EN1 1AA", status="complete",
        stripe_session_id="cs_test_donation_1"))
    db.session.commit()
    claim = gift_aid_claimable_query().all()
    claim_total = sum(p.gift_aid_pence for p in claim)
check("the Gift Aid claim finds the real donation", len(claim) == 1,
      str(len(claim)))
check("...and no membership money is in it — £50, not £60",
      claim_total == 5000, str(claim_total))

# ------------------------------------------------ the seventeen, seeded
runner = app.test_cli_runner()
result = runner.invoke(args=["seed-members"])
with app.app_context():
    seeded = [m.name for m in Member.query.all()]
    statuses = {m.name: m.status for m in Member.query.all()
                if m.name in EXISTING_MEMBERS}
check("seed-members refuses when members already exist",
      "already" in result.output.lower(), result.output.strip()[:120])
result = runner.invoke(args=["seed-members", "--force"])
with app.app_context():
    seeded = {m.name for m in Member.query.all()}
    statuses = {m.status for m in Member.query.all()
                if m.name in EXISTING_MEMBERS}
check("all seventeen are there", len(EXISTING_MEMBERS) == 17
      and all(n in seeded for n in EXISTING_MEMBERS),
      "%d of %d" % (len([n for n in EXISTING_MEMBERS if n in seeded]),
                    len(EXISTING_MEMBERS)))
check("EVERY ONE OF THE SEVENTEEN STARTS AS 'no payment recorded'",
      statuses == {"unknown"}, str(statuses))
result = runner.invoke(args=["seed-members", "--force"])
with app.app_context():
    again = len([m for m in Member.query.all() if m.name in EXISTING_MEMBERS])
check("running it twice adds nobody twice", again == 17, str(again))

# ------------------------------------- an application becomes a member
with app.app_context():
    appl = MembershipApplication(
        name="An Applicant", email="applicant@example.org",
        phone="020 8804 4006", address="1 Somewhere Road",
        reason="I would like to help.",
        over_18=True, bangladeshi_origin=True,
        lives_works_enfield=True, fee_confirmed=True, status="new")
    db.session.add(appl)
    db.session.commit()
    appl_id = appl.id

r = client.post("/admin/membership/%d/make-member" % appl_id,
                follow_redirects=True)
with app.app_context():
    made = Member.query.filter_by(application_id=appl_id).first()
check("an approved application becomes a member", made is not None)
check("...without anybody retyping the contact details",
      made and made.email == "applicant@example.org"
      and made.phone == "020 8804 4006")
check("...and the declarations come across, ethnic origin included",
      made and made.over_18 and made.bangladeshi_origin
      and made.lives_works_enfield and made.fee_confirmed)
with app.app_context():
    approved = db.session.get(MembershipApplication, appl_id).status
check("...the application is marked approved", approved == "approved",
      approved)
r = client.post("/admin/membership/%d/make-member" % appl_id,
                follow_redirects=True)
with app.app_context():
    made_twice = Member.query.filter_by(application_id=appl_id).count()
check("...and doing it twice does not make a second record",
      made_twice == 1, str(made_twice))

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
