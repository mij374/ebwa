"""Smoke test for the admin dashboard cards.

The dashboard grew a card per module, so the things worth pinning down
are: every number is right, flagged modules' cards appear and disappear
with the flag exactly as the nav does, money is rendered in pounds and
agrees with the Gift Aid claim page, and nothing personal (a donor's
name, an applicant's address) ever reaches the page.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_dashboard.py
"""
import os
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_dashboard.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, Campaign, Event, FEATURES, FeatureFlag,  # noqa: E402
                 GalleryImage, MembershipApplication, Milestone, NewsPost,
                 Partner, Payment, Resource, Service, Subscriber,
                 Testimonial, User, dashboard_counts)

app.config["TESTING"] = True

PW = "dashboard-test-password"
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def page():
    return client.get("/admin").data.decode("utf-8")


def set_flag(name, enabled):
    with app.app_context():
        FeatureFlag.query.filter_by(name=name).update({"enabled": enabled})
        db.session.commit()


with app.app_context():
    db.create_all()
    boss = User(email="netbus@example.com")
    boss.set_password(PW)
    boss.role = "super_admin"
    db.session.add(boss)
    for name, _label, _desc, default in FEATURES:
        db.session.add(FeatureFlag(name=name, enabled=default))

    # ---- events: 2 upcoming published, 1 past, 1 unpublished (4 total)
    for offset, published in ((7, True), (30, True), (-30, True), (14, False)):
        ev = Event()
        ev.title = "Event %d" % offset
        ev.slug = "event-%d" % offset
        ev.event_date = date.today() + timedelta(days=offset)
        ev.published = published
        db.session.add(ev)

    # ---- news: 2 published, 1 draft
    for n, published in ((1, True), (2, True), (3, False)):
        post = NewsPost()
        post.title = "Post %d" % n
        post.slug = "post-%d" % n
        post.published_date = date.today()
        post.published = published
        db.session.add(post)

    for n in range(5):
        db.session.add(GalleryImage(filename="p%d.jpg" % n))
    for n in range(3):
        db.session.add(Service(title="Service %d" % n, published=n < 2))
    for n in range(3):
        db.session.add(Testimonial(name="Person %d" % n, quote="Lovely.",
                                   published=n < 1))
    db.session.add(Partner(name="Enfield Council", logo="logo.png",
                           display_mode="both"))
    db.session.add(Partner(name="A trust"))               # text only
    db.session.add(Partner(name="Half done", display_mode="image"))  # no logo
    db.session.add(Resource(name="Foodbank", category="Food support"))
    db.session.add(Resource(name="Advice line", category="Food support"))
    db.session.add(Resource(name="GP surgery", category="Health"))

    for year, funder, published in ((2021, "Enfield Council", True),
                                    (2022, "", True), (2023, "", False)):
        m = Milestone()
        m.year = year
        m.title = "Milestone %d" % year
        m.funder_name = funder
        m.published = published
        db.session.add(m)

    for n in range(4):
        db.session.add(Subscriber(email="sub%d@example.com" % n))

    for n, status in enumerate(("new", "new", "approved", "declined")):
        m = MembershipApplication()
        m.name = "Applicant %d" % n
        m.email = "applicant%d@example.com" % n
        m.address = "%d Ponders End Road, EN3" % (n + 1)
        m.status = status
        db.session.add(m)

    trip = Campaign()
    trip.title = "Seaside trip"
    trip.slug = "seaside-trip"
    trip.fee_pence = 1500
    db.session.add(trip)
    closed = Campaign()
    closed.title = "Winter coats"
    closed.slug = "winter-coats"
    closed.active = False
    db.session.add(closed)
    db.session.commit()

    # ---- payments. Totals below are worked out by hand, not from the code:
    #   complete this year : 1500+500 (fee+gift) + 2000 (general, Gift Aid)
    #                        + 1000 (general, no declaration)   = £50
    #   plus a complete one from a previous year: 4000           = £40
    #   the pending one must not count at all.
    #   Gift Aid claimable = 500 + 2000 = £25 -> reclaim £6.25
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
        p = Payment()
        p.campaign_id = camp.id if camp else None
        p.name = "Donor Nasrin"
        p.email = "donor@example.com"
        p.fee_pence = fee
        p.donation_pence = donation
        p.status = status
        if ga:
            p.gift_aid = True
            p.gift_aid_name = "Donor Nasrin"
            p.gift_aid_address = "12"
            p.gift_aid_postcode = "EN3 4AB"
        if created:
            p.created_at = created
        db.session.add(p)
    db.session.commit()

client = app.test_client()

# ---- the page is admin-only, like every other admin route
check("dashboard needs a login", client.get("/admin").status_code == 302)

client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})
check("logged in", client.get("/admin").status_code == 200)

# ---- the counts themselves
with app.app_context():
    c = dashboard_counts()

expected = {
    "events": 4, "upcoming": 2, "events_draft": 1,
    "news": 2, "news_draft": 1,
    "gallery": 5,
    "services": 2, "services_hidden": 1,
    "testimonials": 1, "testimonials_hidden": 2,
    "partners": 3, "partners_logo": 1,
    "resources": 3, "resource_categories": 2,
    "milestones": 2, "milestones_draft": 1, "milestones_funded": 1,
    "subscribers": 4,
    "members": 4, "members_new": 2, "members_approved": 1,
    "campaigns": 2, "campaigns_active": 1,
    "raised_pence": 9000, "raised_year_pence": 5000,
    "gift_aid_pence": 2500, "gift_aid_reclaim_pence": 625,
}
for key, want in expected.items():
    check("count %s == %s" % (key, want), c.get(key) == want,
          "got %r" % (c.get(key),))

# ---- a half-finished partner (image mode, no logo) is not counted as
# having one, matching Partner.shows_logo on the public page
check("logoless 'image' partner not counted as having a logo",
      c["partners_logo"] == 1)

# ---- money renders in pounds, and agrees with the Gift Aid claim page
html = page()
for want in ("£50", "£90", "£6.25", "£25"):
    check("dashboard shows %s" % want, want in html)

ga = client.get("/admin/gift-aid").data.decode("utf-8")
check("Gift Aid reclaim matches the claim page", "£6.25" in ga)

# ---- every card links somewhere useful
for url in ("/admin/events", "/admin/news", "/admin/gallery",
            "/admin/services", "/admin/testimonials", "/admin/partners",
            "/admin/resources", "/admin/journey", "/admin/subscribers",
            "/admin/membership", "/admin/campaigns", "/admin/gift-aid"):
    check("links to %s" % url, url in html)

# ---- nothing personal on the page: aggregates only
for leak in ("Donor Nasrin", "donor@example.com", "Applicant 0",
             "applicant0@example.com", "Ponders End Road", "EN3 4AB"):
    check("no personal data on the dashboard: %s" % leak, leak not in html)

# ---- applications waiting get the alert treatment; none waiting does not
check("waiting applications are flagged", "admin-stat-alert" in html)
with app.app_context():
    MembershipApplication.query.filter_by(status="new").update(
        {"status": "contacted"})
    db.session.commit()
html = page()
check("no alert once nothing is waiting", "admin-stat-alert" not in html)
check("and it says so", "nothing waiting" in html)

# ---- cards follow the feature flags, exactly as the nav does
FLAG_CARDS = {
    "news": "news &amp; projects",
    "resources": "community resources",
    "our_journey": "journey milestones",
    "membership_form": "membership applications",
    "donations": "Gift Aid to claim",
}
for flag, label in FLAG_CARDS.items():
    check("%s card shows while on" % flag, label in page())
    set_flag(flag, False)
    check("%s card hides when off" % flag, label not in page())
    set_flag(flag, True)
    check("%s card comes back" % flag, label in page())

# ---- with donations off, the dashboard asks for no payment figures at all
set_flag("donations", False)
with app.app_context():
    off = dashboard_counts()
check("no money counted while donations are off",
      "raised_pence" not in off and "gift_aid_pence" not in off)
check("content counts still there", off["events"] == 4)
set_flag("donations", True)

# ---- an empty site renders rather than dividing by zero anywhere
with app.app_context():
    for model in (Event, NewsPost, GalleryImage, Service, Testimonial,
                  Partner, Resource, Milestone, Subscriber,
                  MembershipApplication, Payment, Campaign):
        model.query.delete()
    db.session.commit()
r = client.get("/admin")
check("empty site still renders", r.status_code == 200)
check("empty site shows zeroes", "£0" in r.data.decode("utf-8"))

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
