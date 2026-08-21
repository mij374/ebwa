"""Smoke test for the admin dashboard overview (/admin).

Covers: anonymous access redirects; the page renders for a client admin
and a super admin; every card counts correctly and links to its section;
modules whose feature flag is off are absent from the cards, the quick
actions and the checks; the "needs attention" panel is hidden when there
is nothing to say and each condition raises it on its own; money renders
in pounds and the Gift Aid figure agrees with the claim page; nothing
personal (a donor's name, an applicant's address) ever reaches the page;
recent activity is super-admin only, newest first, capped at six and
shown in UK local time; and the whole page stays a fixed number of
queries however much content the site holds (no N+1).

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_dashboard.py
"""
import os
import re
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_dashboard.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from sqlalchemy import event as sa_event                        # noqa: E402

from app import (app, db, FEATURES, AuditLog, Block, Campaign,  # noqa: E402
                 ContentImage, Event, FeatureFlag, GalleryImage,
                 MembershipApplication, Milestone, NewsPost, Partner,
                 Payment, RECENT_ACTIVITY, Resource, Service, Subscriber,
                 Testimonial, User, utc_as_uk)

app.config["TESTING"] = True

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def card(html, label):
    """(url, headline value, extra text) for one card, or None.

    The value is an int for a count and the rendered string for money.
    """
    m = re.search(r'<a class="admin-stat([^"]*)" href="([^"]+)">\s*'
                  r'<b>([^<]+)</b>\s*<span>%s</span>(.*?)</a>'
                  % re.escape(label), html, re.S)
    if not m:
        return None
    value = m.group(3).strip()
    return (m.group(2), int(value) if value.isdigit() else value,
            m.group(1) + m.group(4))


def dashboard(cl):
    r = cl.get("/admin")
    return r.status_code, r.data.decode("utf-8")


def set_flag(name, enabled):
    with app.app_context():
        row = FeatureFlag.query.filter_by(name=name).first()
        row.enabled = enabled
        db.session.commit()


def add(obj, **fields):
    """Populate first, add last — autoflush of half-built rows bites."""
    for k, v in fields.items():
        setattr(obj, k, v)
    db.session.add(obj)
    return obj


TODAY = date.today()
FUTURE = TODAY + timedelta(days=30)
PAST = TODAY - timedelta(days=30)

# ---- a site with content, and deliberately nothing needing attention
with app.app_context():
    db.create_all()
    for name, _label, _desc, default in FEATURES:
        db.session.add(FeatureFlag(name=name, enabled=default))

    boss = User(email="netbus@example.com")
    boss.set_password("pw123456")
    boss.role = "super_admin"
    db.session.add(boss)
    client_admin = User(email="client@example.com")
    client_admin.set_password("pw123456")
    db.session.add(client_admin)

    # Two published events (both upcoming, both with a photo) + one draft
    for i in range(2):
        add(Event(), title="Eid party %d" % i, slug="eid-party-%d" % i,
            event_date=FUTURE, image="e%d.jpg" % i, published=True)
    add(Event(), title="Draft event", slug="draft-event",
        event_date=FUTURE, image="d.jpg", published=False)
    # Three news posts, one a draft
    for i in range(3):
        add(NewsPost(), title="News %d" % i, slug="news-%d" % i,
            published_date=TODAY, image="n%d.jpg" % i, published=i < 2)
    add(Milestone(), year=2021, title="Opened the centre", image="m.jpg",
        published=True)
    add(Resource(), name="Enfield Foodbank", category="Food support")
    add(Service(), title="Elderly drop-in", published=True)
    add(Service(), title="Hidden card", published=False)
    add(Partner(), name="Enfield Council")
    add(Testimonial(), name="Rahim", quote="Wonderful people.", published=True)
    db.session.add(GalleryImage(filename="g.jpg"))
    db.session.add(Subscriber(email="someone@example.com"))
    add(MembershipApplication(), name="Nadia", email="n@example.com",
        status="contacted")
    add(Campaign(), title="Seaside trip", slug="seaside-trip",
        fee_pence=1500, image="c.jpg", active=True)
    db.session.commit()

client = app.test_client()

# ---- anonymous
r = client.get("/admin")
check("anon GET /admin -> login redirect",
      r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
      str(r.status_code))

# ---- client admin sees the overview
client.post("/admin/login", data={"email": "client@example.com",
                                  "password": "pw123456"})
status, html = dashboard(client)
check("client admin GET /admin -> 200", status == 200, str(status))

EXPECTED = [("Events", 3, "/admin/events"),
            ("News &amp; projects", 3, "/admin/news"),
            ("Journey milestones", 1, "/admin/journey"),
            ("Community resources", 1, "/admin/resources"),
            ("“What we do” cards", 2, "/admin/services"),
            ("Partners", 1, "/admin/partners"),
            ("Testimonials", 1, "/admin/testimonials"),
            ("Gallery photos", 1, "/admin/gallery"),
            ("Newsletter subscribers", 1, "/admin/subscribers"),
            ("Membership applications", 1, "/admin/membership"),
            ("Collections open", 1, "/admin/campaigns"),
            ("Raised this year", "£0", "/admin/campaigns"),
            ("Gift Aid to claim", "£0", "/admin/gift-aid")]
for label, count, url in EXPECTED:
    c = card(html, label)
    check("card: %s" % label, c is not None)
    if c:
        check("card %s reads %s" % (label, count), c[1] == count,
              str(c[1]))
        check("card %s links to %s" % (label, url), c[0] == url, c[0])

check("events card splits upcoming and past",
      "2 upcoming · 0 past" in card(html, "Events")[2])
check("events card shows its unpublished one",
      "1 unpublished" in card(html, "Events")[2])
check("news card shows its unpublished one",
      "1 unpublished" in card(html, "News &amp; projects")[2])
check("collections card shows the total",
      "1 in total" in card(html, "Collections open")[2])
check("card with nothing unpublished says nothing about it",
      "unpublished" not in card(html, "Partners")[2])
check("cards are grouped", all(h in html for h in
                               ("Pages and content", "People",
                                "Donations and collections")))
check("membership card is not shouting while nothing waits",
      "admin-stat-alert" not in html)

# ---- quick actions
for label, url in (("+ New event", "/admin/events/new"),
                   ("+ New news post", "/admin/news/new"),
                   ("+ New milestone", "/admin/journey/new"),
                   ("Edit page content", "/admin/content"),
                   ("Manage gallery", "/admin/gallery")):
    check("quick action: %s" % label, label in html and url in html)

# ---- nothing to act on: the panel is not there at all
check("attention panel hidden when there is nothing to show",
      "Needs attention" not in html)

# ---- recent activity is super-admin only
check("client admin sees no recent activity", "Recent activity" not in html)
check("client admin gets no audit-log summary link",
      "View the full audit log" not in html)
client.get("/admin/logout")

# ---- each attention condition, one at a time
def attention_case(name, setup, teardown, expect):
    """Apply a condition, assert the panel appears saying `expect`,
    then undo it and assert the panel goes away again."""
    with app.app_context():
        setup()
        db.session.commit()
    _s, page = dashboard(client)
    check("attention: %s raises the panel" % name,
          "Needs attention" in page and expect in page,
          "missing %r" % expect)
    with app.app_context():
        teardown()
        db.session.commit()
    _s, page = dashboard(client)
    check("attention: %s clears again" % name, "Needs attention" not in page)


client.post("/admin/login", data={"email": "client@example.com",
                                  "password": "pw123456"})

attention_case(
    "new membership application",
    lambda: add(MembershipApplication(), name="Karim",
                email="k@example.com", status="new"),
    lambda: [db.session.delete(m) for m in MembershipApplication.query
             .filter_by(status="new")],
    "1 new membership application waiting")

attention_case(
    "published event now in the past",
    lambda: add(Event(), title="Last year's mela", slug="last-mela",
                event_date=PAST, image="p.jpg", published=True),
    lambda: [db.session.delete(e) for e in Event.query
             .filter_by(slug="last-mela")],
    "1 event now past")

attention_case(
    "placeholder legal copy",
    lambda: db.session.add(Block(key="privacy_body", label="Privacy text",
                                 kind="text", group="legal",
                                 value="PLACEHOLDER — replace me.")),
    lambda: [db.session.delete(b) for b in Block.query.all()],
    "LAUNCH BLOCKER")

attention_case(
    "placeholder copy outside the legal pages",
    lambda: db.session.add(Block(key="home_hero_text", label="Hero",
                                 kind="text", group="home",
                                 value="PLACEHOLDER — replace me.")),
    lambda: [db.session.delete(b) for b in Block.query.all()],
    "The home section still has 1 block of placeholder text.")

attention_case(
    "campaign with no image",
    lambda: add(Campaign(), title="Winter appeal", slug="winter-appeal",
                image="", active=True),
    lambda: [db.session.delete(c) for c in Campaign.query
             .filter_by(slug="winter-appeal")],
    "1 collection with no photo")

attention_case(
    "published news post with no photo",
    lambda: add(NewsPost(), title="Photo-less", slug="photo-less",
                published_date=TODAY, image="", published=True),
    lambda: [db.session.delete(p) for p in NewsPost.query
             .filter_by(slug="photo-less")],
    "1 news post published with no photo")

attention_case(
    "stale unfinished payment",
    lambda: add(Payment(), donation_pence=2500, status="pending",
                created_at=datetime.utcnow() - timedelta(days=2)),
    lambda: [db.session.delete(p) for p in Payment.query.all()],
    "1 payment still unfinished after a day")

# ---- a rich-content attachment counts as a photo (no false positive)
with app.app_context():
    post = add(NewsPost(), title="Attachment only", slug="attachment-only",
               published_date=TODAY, image="", published=True)
    db.session.commit()
    db.session.add(ContentImage(owner_type="news_post", owner_id=post.id,
                                filename="a.jpg", alt_text="A photo"))
    db.session.commit()
_s, html = dashboard(client)
check("attached rich-content image counts as a photo",
      "Needs attention" not in html)
with app.app_context():
    for img in ContentImage.query.all():
        db.session.delete(img)
    db.session.commit()
_s, html = dashboard(client)
check("removing the attachment raises the missing-photo item",
      "1 news post published with no photo" in html)
with app.app_context():
    for p in NewsPost.query.filter_by(slug="attachment-only"):
        db.session.delete(p)
    db.session.commit()

# ---- an unpublished draft with no photo is not nagged about
with app.app_context():
    add(NewsPost(), title="Quiet draft", slug="quiet-draft",
        published_date=TODAY, image="", published=False)
    db.session.commit()
_s, html = dashboard(client)
check("draft with no photo does not raise the panel",
      "Needs attention" not in html)

# ---- switching a module off hides its card, quick action and warnings
with app.app_context():
    add(MembershipApplication(), name="Suraiya", email="s@example.com",
        status="new")
    add(Campaign(), title="No photo appeal", slug="no-photo-appeal", image="")
    add(Payment(), donation_pence=500, status="pending",
        created_at=datetime.utcnow() - timedelta(days=3))
    db.session.commit()
_s, html = dashboard(client)
check("membership warning shown while the flag is on",
      "new membership application" in html)
check("collection warning shown while the flag is on",
      "collection with no photo" in html)

for name in ("news", "resources", "our_journey", "membership_form",
             "donations"):
    set_flag(name, False)
_s, html = dashboard(client)
for label in ("News &amp; projects", "Journey milestones",
              "Community resources", "Membership applications",
              "Collections open", "Raised this year", "Gift Aid to claim"):
    check("flag off: %s card absent" % label, card(html, label) is None)
check("flag off: news quick action absent", "+ New news post" not in html)
check("flag off: milestone quick action absent",
      "+ New milestone" not in html)
check("flag off: membership warning absent",
      "membership application" not in html)
check("flag off: collection warning absent", "with no photo" not in html)
check("flag off: the donations group heading goes too",
      "Donations and collections" not in html)
check("flag off: unfinished payment warning absent",
      "still unfinished" not in html)
check("flag off: core cards still there", card(html, "Events") is not None)
check("flag off: nothing left needing attention",
      "Needs attention" not in html)

for name in ("news", "resources", "our_journey", "membership_form",
             "donations"):
    set_flag(name, True)
_s, html = dashboard(client)
check("flags back on: cards return",
      all(card(html, l) for l in ("News &amp; projects", "Journey milestones",
                                  "Community resources",
                                  "Membership applications",
                                  "Collections open", "Gift Aid to claim")))
check("flags back on: warnings return", "Needs attention" in html)
client.get("/admin/logout")

# ---- recent activity, super admins only
client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": "pw123456"})
with app.app_context():
    base = datetime.utcnow()
    for i in range(8):
        db.session.add(AuditLog(user_email="netbus@example.com",
                                action="edit", summary="Audit entry %d." % i,
                                created_at=base + timedelta(seconds=i)))
    db.session.commit()
    newest = (AuditLog.query.order_by(AuditLog.created_at.desc(),
                                      AuditLog.id.desc()).first())
    uk = utc_as_uk(newest.created_at).strftime("%d %b %Y, %H:%M")
    raw = newest.created_at.strftime("%d %b %Y, %H:%M")

status, html = dashboard(client)
check("super admin GET /admin -> 200", status == 200, str(status))
check("super admin sees recent activity", "Recent activity" in html)
check("recent activity links to the full log",
      "View the full audit log" in html and "/admin/audit" in html)
check("recent activity shows the actor", "netbus@example.com" in html)
check("recent activity shows the newest entries",
      all(("Audit entry %d." % i) in html for i in range(2, 8)))
check("recent activity is capped at %d" % RECENT_ACTIVITY,
      "Audit entry 0." not in html and "Audit entry 1." not in html)
check("recent activity uses UK local time", uk in html, uk)
check("recent activity is not raw UTC", raw == uk or raw not in html, raw)

# ---- money: the totals are worked out here by hand, not from the code.
#   complete this year : 1500+500 (fee+gift) + 2000 (general, Gift Aid)
#                        + 1000 (general, no declaration)   = £50
#   plus a complete one from a previous year: 4000           = £40
#   the pending one must not count at all.
#   Gift Aid claimable = 500 + 2000 = £25 -> reclaim £6.25
with app.app_context():
    Payment.query.delete()          # start the money maths from nothing
    trip = Campaign.query.filter_by(slug="seaside-trip").first()
    last_year = datetime.utcnow().replace(year=datetime.utcnow().year - 1)
    rows = [
        # campaign, fee, donation, gift_aid, status, created_at
        (trip, 1500, 500, True, "complete", None),
        (None, 0, 2000, True, "complete", None),
        (None, 0, 1000, False, "complete", None),
        (None, 0, 4000, False, "complete", last_year),
        (trip, 1500, 0, False, "pending", None),
    ]
    for camp, fee, donation, ga, status, created in rows:
        pay = Payment()
        pay.campaign_id = camp.id if camp else None
        pay.name = "Donor Nasrin"
        pay.email = "donor@example.com"
        pay.fee_pence = fee
        pay.donation_pence = donation
        pay.status = status
        if ga:
            pay.gift_aid = True
            pay.gift_aid_name = "Donor Nasrin"
            pay.gift_aid_address = "12"
            pay.gift_aid_postcode = "EN3 4AB"
        if created:
            pay.created_at = created
        db.session.add(pay)
    db.session.commit()

_s, html = dashboard(client)
check("raised this year is right", card(html, "Raised this year")[1] == "£50")
check("raised since the site opened is right",
      "£90 since the site opened" in card(html, "Raised this year")[2])
check("Gift Aid to claim is right",
      card(html, "Gift Aid to claim")[1] == "£6.25")
check("Gift Aid card shows what it is claimed on",
      "on £25 of eligible donations" in card(html, "Gift Aid to claim")[2])
ga = client.get("/admin/gift-aid").data.decode("utf-8")
check("Gift Aid reclaim matches the claim page", "£6.25" in ga)
check("the fresh pending payment is not called stale",
      "still unfinished" not in html)

# ---- aggregates only: nothing personal reaches the page
with app.app_context():
    add(MembershipApplication(), name="Applicant Nadia",
        email="applicant@example.com", address="12 Ponders End Road, EN3",
        status="new")
    db.session.commit()
_s, html = dashboard(client)
check("membership card shouts while an application waits",
      "admin-stat-alert" in card(html, "Membership applications")[2])
for leak in ("Donor Nasrin", "donor@example.com", "Applicant Nadia",
             "applicant@example.com", "Ponders End Road", "EN3 4AB"):
    check("no personal data on the dashboard: %s" % leak, leak not in html)

# ---- an empty site still renders rather than dividing by zero anywhere
with app.app_context():
    for model in (Payment, Campaign, Event, NewsPost, GalleryImage, Service,
                  Testimonial, Partner, Resource, Milestone, Subscriber,
                  MembershipApplication):
        model.query.delete()
    db.session.commit()
status, html = dashboard(client)
check("empty site still renders", status == 200, str(status))
check("empty site shows zeroes", "£0" in html)
check("empty site has nothing needing attention",
      "Needs attention" not in html)

# ---- the page does not get slower as content grows (no N+1)
statements = []


def _record(conn, cursor, stmt, params, context, executemany):
    statements.append(stmt)


with app.app_context():
    sa_event.listen(db.engine, "before_cursor_execute", _record)
try:
    statements[:] = []
    dashboard(client)
    small = len(statements)
    with app.app_context():
        for i in range(30):
            add(Event(), title="Bulk event %d" % i, slug="bulk-event-%d" % i,
                event_date=FUTURE, image="b.jpg", published=True)
            add(NewsPost(), title="Bulk news %d" % i,
                slug="bulk-news-%d" % i, published_date=TODAY,
                image="b.jpg", published=True)
            add(Milestone(), year=2020, title="Bulk milestone %d" % i,
                image="b.jpg", published=True)
            db.session.add(GalleryImage(filename="b%d.jpg" % i))
        db.session.commit()
    statements[:] = []
    dashboard(client)
    large = len(statements)
finally:
    with app.app_context():
        sa_event.remove(db.engine, "before_cursor_execute", _record)
check("dashboard query count does not grow with content",
      small == large, "%d -> %d" % (small, large))
check("dashboard stays a modest number of queries", large < 40, str(large))

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
