"""The three states a collection can be in, and the migration into them.

`active` answered two questions with one tick — is this on the website,
and is it taking money — so closing a finished trip also deleted it from
the site. `state` separates them:

  open    on the site, taking payments
  closed  on the site as a record: final total, contributor count, a
          line saying it has finished, and NO payment form
  hidden  off the public site entirely

The check that matters most here is the last section: a closed
collection must REFUSE A PAYMENT, not merely stop offering one. Hiding
a form is a courtesy to the person reading the page. A stale tab, a
back button and anything posting at the endpoint directly all arrive
at the route regardless, and taking money for a trip that has already
happened is not something to leave resting on a template `{% if %}`.

Run:  python tests/smoke_test_campaign_states.py
"""
import os
import re
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEST_DB = os.path.join(HERE, "test_campaign_states.db")
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _s):
        os.remove(TEST_DB + _s)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, ROOT)

from app import (app, db, User, Campaign, Payment,  # noqa: E402
                 CAMPAIGN_STATES, PUBLIC_CAMPAIGN_STATES)

app.config["TESTING"] = True
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def paid(camp_id, fee, donation, status="complete"):
    p = Payment(campaign_id=camp_id, name="Giver", email="g@example.org",
                fee_pence=fee, donation_pence=donation, status=status,
                stripe_session_id="cs_%d_%d_%s" % (camp_id, fee + donation,
                                                   status))
    db.session.add(p)
    return p


with app.app_context():
    db.create_all()
    u = User(email="t@example.com")
    u.set_password("pw123456")
    db.session.add(u)
    rows = {
        "open": Campaign(title="Seaside trip", slug="seaside-trip",
                         description="A day at the coast.", fee_pence=1500,
                         target_pence=50000, state="open"),
        "closed": Campaign(title="Winter coats", slug="winter-coats",
                           description="Coats for elders.",
                           target_pence=25000, state="closed"),
        "hidden": Campaign(title="Draft appeal", slug="draft-appeal",
                           description="Not ready.", state="hidden"),
    }
    db.session.add_all(rows.values())
    db.session.commit()
    IDS = {k: v.id for k, v in rows.items()}
    # Money against the closed one, so its final figures are real, plus
    # one incomplete payment that must not be counted in either figure.
    paid(IDS["closed"], 0, 10000)
    paid(IDS["closed"], 0, 5000)
    paid(IDS["closed"], 0, 9900, status="pending")
    paid(IDS["open"], 1500, 500)
    db.session.commit()

client = app.test_client()

# ---- the constants themselves --------------------------------------
names = [s[0] for s in CAMPAIGN_STATES]
check("three states, open first (what a new collection gets)",
      names == ["open", "closed", "hidden"], str(names))
check("open and closed are the public pair, in that order",
      PUBLIC_CAMPAIGN_STATES == ("open", "closed"),
      str(PUBLIC_CAMPAIGN_STATES))
check("every state has a label and a description for the admin",
      all(len(s) == 3 and all(s) for s in CAMPAIGN_STATES))

# ---- figures are counts, and only completed money counts ------------
with app.app_context():
    closed = db.session.get(Campaign, IDS["closed"])
    check("raised_pence counts completed payments only",
          closed.raised_pence == 15000, str(closed.raised_pence))
    check("contributor_count counts completed payments only",
          closed.contributor_count == 2, str(closed.contributor_count))
    check("takes_payments is state-derived",
          closed.takes_payments is False
          and db.session.get(Campaign, IDS["open"]).takes_payments is True)
    check("is_public covers open and closed but not hidden",
          db.session.get(Campaign, IDS["open"]).is_public
          and closed.is_public
          and not db.session.get(Campaign, IDS["hidden"]).is_public)

# ---- the listing page ------------------------------------------------
r = client.get("/collections")
body = r.data.decode("utf-8")
check("/collections -> 200", r.status_code == 200, str(r.status_code))
check("the open collection is listed", "Seaside trip" in body)
check("the closed one is listed too", "Winter coats" in body)
check("under a Completed heading", "Completed" in body)
check("the hidden one is nowhere on the page", "Draft appeal" not in body)
check("a completed collection LINKS to its page, it is not a bare name",
      '/collections/winter-coats"' in body)
check("the completed card shows what was raised against the target",
      re.search(r"£150(\.00)?</b>\s*raised of a\s*£250", body) is not None)
check("open cards come before completed ones",
      body.index("Seaside trip") < body.index("Winter coats"))

# ---- the homepage strip: open only -----------------------------------
home = client.get("/").data.decode("utf-8")
check("homepage strip shows the open collection", "Seaside trip" in home)
check("homepage strip does NOT show the completed one",
      "Winter coats" not in home)
check("nor the hidden one", "Draft appeal" not in home)
check("the strip links to the full list", 'href="/collections"' in home
      and "All collections" in home)

# THREE AT MOST, like the event and news strips. Every open collection
# used to be shown, and six put two full rows on the front page.
with app.app_context():
    extra = [Campaign(title="Extra collection %d" % i, slug="extra-%d" % i,
                      description="Words.", state="open") for i in range(5)]
    db.session.add_all(extra)
    db.session.commit()
home = client.get("/").data.decode("utf-8")
strip = home.split("Help us get there", 1)[1].split("</section>", 1)[0]
check("homepage strip shows at most three open collections",
      strip.count('class="event-card"') == 3,
      "%d cards" % strip.count('class="event-card"'))
listing = client.get("/collections").data.decode("utf-8")
check("the collections page still lists all six",
      listing.count('href="/collections/extra-') == 5
      and "Seaside trip" in listing)
with app.app_context():
    for c in Campaign.query.filter(Campaign.slug.like("extra-%")).all():
        db.session.delete(c)
    db.session.commit()

# ---- the sitemap: both public states, never the hidden one -----------
xml = client.get("/sitemap.xml").data.decode("utf-8")
check("open collection in the sitemap", "/collections/seaside-trip" in xml)
check("closed collection in the sitemap too — it has a page",
      "/collections/winter-coats" in xml)
check("hidden collection absent from the sitemap",
      "draft-appeal" not in xml)

# ---- the detail pages ------------------------------------------------
r = client.get("/collections/seaside-trip")
open_body = r.data.decode("utf-8")
check("open detail page -> 200", r.status_code == 200, str(r.status_code))
check("open page offers the payment form",
      'name="donation"' in open_body and "Pay securely" in open_body)

r = client.get("/collections/winter-coats")
closed_body = r.data.decode("utf-8")
check("CLOSED DETAIL PAGE STILL LOADS -> 200", r.status_code == 200,
      str(r.status_code))
check("closed page says plainly that it has finished",
      "This collection has finished" in closed_body)
check("closed page names how many people gave",
      "2 people" in closed_body, "expected the contributor count")
check("closed page shows the final total",
      "£150" in closed_body)
check("CLOSED PAGE HAS NO PAYMENT FORM",
      'name="donation"' not in closed_body
      and "Pay securely" not in closed_body)
check("and no Gift Aid declaration either",
      "gift_aid_declaration" not in closed_body)
check("the form's script goes with the form",
      "syncGiftAid" not in closed_body)
check("closed page points somewhere useful instead",
      "/donate" in closed_body)

r = client.get("/collections/draft-appeal")
check("hidden detail page -> 404", r.status_code == 404, str(r.status_code))

# ---- THE ONE THAT MATTERS: a closed collection refuses a payment -----
before = None
with app.app_context():
    before = Payment.query.count()

fake = SimpleNamespace(id="cs_should_never_exist",
                       url="https://checkout.stripe.test/x")
with patch("app.stripe.checkout.Session.create", return_value=fake) as create:
    r = client.post("/collections/winter-coats", data={
        "donation": "25", "name": "Late Giver", "email": "late@example.org"},
        follow_redirects=True)
check("POST to a closed collection is refused, not processed",
      r.status_code == 200, str(r.status_code))
check("NO STRIPE SESSION WAS EVEN CREATED", create.call_count == 0,
      "stripe called %d time(s)" % create.call_count)
with app.app_context():
    check("no payment row was written",
          Payment.query.count() == before, str(Payment.query.count()))
    check("and certainly not that one",
          Payment.query.filter_by(
              stripe_session_id="cs_should_never_exist").first() is None)
check("the visitor is told why, in plain words",
      b"no longer taking payments" in r.data)

# a hidden one does not even have an endpoint to post at
r = client.post("/collections/draft-appeal", data={
    "donation": "25", "name": "X", "email": "x@example.org"})
check("POST to a hidden collection -> 404", r.status_code == 404,
      str(r.status_code))

# ...while the open one still works, so the guard is not just "refuse all"
with patch("app.stripe.checkout.Session.create",
           return_value=SimpleNamespace(id="cs_open_ok",
                                        url="https://x.test/1")) as create:
    r = client.post("/collections/seaside-trip", data={
        "include_fee": "on", "donation": "5",
        "name": "Real Giver", "email": "real@example.org"})
check("the OPEN collection still takes a payment", r.status_code == 303,
      str(r.status_code))
with app.app_context():
    check("and stored it",
          Payment.query.filter_by(
              stripe_session_id="cs_open_ok").first() is not None)

# ---- admin: every state keeps its records ---------------------------
client.post("/admin/login", data={"email": "t@example.com",
                                  "password": "pw123456"})
for name, cid in IDS.items():
    r = client.get("/admin/campaigns/%d/contributors" % cid)
    check("admin contributor list works for a %s collection" % name,
          r.status_code == 200, str(r.status_code))
    r = client.get("/admin/campaigns/%d/contributors.csv" % cid)
    check("admin contributor CSV works for a %s collection" % name,
          r.status_code == 200 and r.mimetype == "text/csv",
          str(r.status_code))
    r = client.get("/admin/campaigns/%d/edit" % cid)
    check("admin edit form opens for a %s collection" % name,
          r.status_code == 200, str(r.status_code))

r = client.get("/admin/gift-aid")
check("the Gift Aid claim page opens whatever any collection's state is",
      r.status_code == 200, str(r.status_code))
r = client.get("/admin/campaigns")
listing = r.data.decode("utf-8")
check("the admin list names all three states, one word each",
      ">Open<" in listing and ">Closed<" in listing
      and ">Hidden<" in listing)
# The form is where the fuller wording belongs — the list is read, the
# form is chosen from.
form_html = client.get("/admin/campaigns/%d/edit" % IDS["open"]).data.decode()
check("the form still spells out what each state does",
      "Taking payments" in form_html
      and "with the payment form" in form_html
      and "Off the public website entirely" in form_html)
check("the admin list offers View for the two public ones only",
      listing.count('/collections/') == 2, str(listing.count('/collections/')))

# ---- THE MIGRATION, run exactly as DEPLOY.md prints it ---------------
# Not a paraphrase of it: the statements are read OUT of DEPLOY.md, so a
# migration that is edited there and not here, or here and not there,
# fails this test rather than being discovered on the server.
deploy = open(os.path.join(ROOT, "DEPLOY.md"), encoding="utf-8").read()
section = deploy.split("Collections: three states instead of Active", 1)[1]
sql_block = section.split("```bash", 1)[1].split("```", 1)[0]
statements = [ln.strip() for ln in sql_block.splitlines()
              if ln.strip().upper().startswith(("ALTER", "UPDATE"))]
check("DEPLOY.md prints an ALTER and an UPDATE for this change",
      len(statements) == 2
      and statements[0].startswith("ALTER")
      and statements[1].startswith("UPDATE"), str(statements))

OLD_DB = os.path.join(HERE, "test_campaign_migration.db")
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(OLD_DB + _s):
        os.remove(OLD_DB + _s)
con = sqlite3.connect(OLD_DB)
# A campaign table as it was BEFORE this change: no state column.
con.execute("""CREATE TABLE campaign (
    id INTEGER PRIMARY KEY, title VARCHAR(200) NOT NULL,
    slug VARCHAR(220) NOT NULL UNIQUE, description TEXT,
    image VARCHAR(255), target_pence INTEGER, fee_pence INTEGER,
    video_url VARCHAR(300), video_thumb VARCHAR(255),
    active BOOLEAN, created_at DATETIME)""")
con.executemany(
    "INSERT INTO campaign (title, slug, active) VALUES (?, ?, ?)",
    [("Live one", "live-one", 1), ("Another live", "another-live", 1),
     ("Taken down", "taken-down", 0), ("Also down", "also-down", 0)])
con.commit()
for statement in statements:
    con.execute(statement)
con.commit()
got = dict(con.execute("SELECT slug, state FROM campaign").fetchall())
con.close()

check("migration: active=1 becomes 'open'",
      got["live-one"] == "open" and got["another-live"] == "open", str(got))
check("MIGRATION: active=0 BECOMES 'hidden', NOT 'closed'",
      got["taken-down"] == "hidden" and got["also-down"] == "hidden",
      str(got))
check("migration invents no other states",
      set(got.values()) == {"open", "hidden"}, str(set(got.values())))

# The fail-safe: if the ALTER lands and the UPDATE does not, everything
# must be HIDDEN rather than published. The column default is what
# decides that, and it is the opposite of the app's own default.
con = sqlite3.connect(OLD_DB + ".half")
con.execute("""CREATE TABLE campaign (
    id INTEGER PRIMARY KEY, slug VARCHAR(220), active BOOLEAN)""")
con.execute("INSERT INTO campaign (slug, active) VALUES ('was-hidden', 0)")
con.execute("INSERT INTO campaign (slug, active) VALUES ('was-live', 1)")
con.execute(statements[0])          # the ALTER only — UPDATE never runs
con.commit()
half = dict(con.execute("SELECT slug, state FROM campaign").fetchall())
con.close()
check("HALF-APPLIED MIGRATION HIDES EVERYTHING RATHER THAN PUBLISHING IT",
      set(half.values()) == {"hidden"}, str(half))
check("...while a NEW collection made in the admin starts open",
      CAMPAIGN_STATES[0][0] == "open")

for path in (OLD_DB, OLD_DB + ".half"):
    for _s in ("", "-wal", "-shm"):
        if os.path.isfile(path + _s):
            os.remove(path + _s)

# ---- teardown --------------------------------------------------------
with app.app_context():
    db.session.remove()
    db.engine.dispose()
for suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + suffix):
        os.remove(TEST_DB + suffix)
check("test db deleted", not os.path.isfile(TEST_DB))

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
