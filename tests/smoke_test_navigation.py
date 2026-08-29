"""Every public page is reachable (CLAUDE.md rules).

A page nobody can find is the failure mode this exists to prevent: the
collections pages sat on the site for weeks linked only from a homepage
strip that showed three of them.

It walks the URL MAP, not the templates, so a page added later is covered
whether or not anybody remembers this file. Every public,
non-parameterised GET route must be either:

  * linked from the main navigation or the footer — the chrome on every
    page; or
  * listed in REACHED_FROM with the page that links it, and the link is
    then CHECKED to be on that page, not merely asserted to exist; or
  * listed in NOT_LINKED with the reason — machine endpoints and the
    pages Stripe returns people to, which nobody navigates to.

Anything else fails, which is what makes adding an unlinked page
impossible to do quietly.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_navigation.py
"""
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_nav.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, Block, Campaign, DEFAULT_BLOCKS, Event,  # noqa: E402
                 FEATURES, FEATURE_DEFAULTS, Faq, FeatureFlag,
                 GalleryAlbum, GalleryImage, Milestone, NewsPost, Resource)

app.config["TESTING"] = True

# Pages reached from within a section rather than from the chrome. The
# parent is FETCHED and the link checked, so this is a statement about
# where the link is, not permission to have none.
REACHED_FROM = {
    "/gallery/all": "/gallery",
}

# Pages nothing links to on purpose.
NOT_LINKED = {
    "/healthz": "monitoring endpoint, not a page",
    "/robots.txt": "for crawlers",
    "/sitemap.xml": "for crawlers — and it lists the pages itself",
    "/donate/success": "Stripe returns people here after paying",
    "/donate/cancelled": "Stripe returns people here if they back out",
    "/membership/paid": "Stripe returns people here after paying a "
                        "membership fee",
    "/membership/applied": "Stripe returns people here after paying the "
                           "fee with a membership application",
}

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def public_routes():
    out = []
    for rule in app.url_map.iter_rules():
        if rule.arguments or "GET" not in rule.methods:
            continue
        if rule.rule.startswith(("/admin", "/static")):
            continue
        out.append(rule.rule)
    return sorted(set(out))


def links_in(html):
    """Every internal href on a page."""
    return set(re.findall(r'href="(/[^"#?]*)', html))


def chrome_links(html):
    """The whole header and footer — what appears on every page.

    The header, not just the nav list: the brand logo links home, and a
    link is a link wherever it sits in the furniture.
    """
    head = html.split("<header")[1].split("</header>")[0] \
        if "<header" in html else ""
    foot = html.split("<footer")[1] if "<footer" in html else ""
    return links_in(head) | links_in(foot)


def get(path):
    return client.get(path).data.decode("utf-8")


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        db.session.add(Block(group=group, key=key, label=label, kind=kind,
                             value=value))
    for n, _l, _d, default in FEATURES:
        # ON, not `default`: this file asks whether every public page
        # can be REACHED, so every flagged page has to be on the site
        # while it looks. membership_fees ships off, and a page that is
        # 404ing cannot fail a reachability check — it would pass by
        # being absent, which is the one result worth nothing here.
        db.session.add(FeatureFlag(name=n, enabled=True))
    # one row per module, so every listing has something to show
    ev = Event()
    ev.title, ev.slug = "Community iftar", "community-iftar"
    ev.event_date, ev.published = date.today(), True
    db.session.add(ev)
    post = NewsPost()
    post.title, post.slug = "Minibus arrives", "minibus-arrives"
    post.published_date, post.published = date.today(), True
    db.session.add(post)
    db.session.add(Resource(name="Foodbank", category="Food"))
    m = Milestone()
    m.year, m.title, m.published = 2024, "Opened the centre", True
    db.session.add(m)
    db.session.add(Faq(question="Can I volunteer?", answer="Yes.",
                       published=True))
    album = GalleryAlbum(title="Eid 2026", slug="eid-2026", published=True)
    db.session.add(album)
    db.session.add(GalleryImage(filename="photo.jpg"))
    camp = Campaign()
    camp.title, camp.slug = "Seaside trip", "seaside-trip"
    camp.fee_pence, camp.active = 1500, True
    db.session.add(camp)
    db.session.commit()

client = app.test_client()

home = get("/")
CHROME = chrome_links(home)
ROUTES = public_routes()
check("the URL map yielded public pages", len(ROUTES) >= 15, str(len(ROUTES)))
check("the chrome has links in it", len(CHROME) >= 10, str(sorted(CHROME)))

# ---- the point of the file
for path in ROUTES:
    if path in CHROME:
        check("%s is in the menu or footer" % path, True)
    elif path in REACHED_FROM:
        parent = REACHED_FROM[path]
        check("%s is linked from %s" % (path, parent),
              path in links_in(get(parent)),
              "not found on %s" % parent)
    elif path in NOT_LINKED:
        check("%s is deliberately unlinked (%s)" % (path, NOT_LINKED[path]),
              True)
    else:
        check("%s IS REACHABLE FROM SOMEWHERE" % path, False,
              "not in the menu or footer, and not listed as an exception "
              "— link it, or say why not in this test")

# ---- the exception lists must not rot
for path in list(REACHED_FROM) + list(NOT_LINKED):
    check("exception %s is still a real route" % path, path in ROUTES,
          "listed as an exception but no longer exists")
    check("exception %s is not ALSO in the chrome" % path,
          path not in CHROME, "linked after all — remove the exception")

# ---- the collections pages, which is what started this
check("collections listing exists", "/collections" in ROUTES)
check("collections is in the menu", "/collections" in CHROME,
      str(sorted(CHROME)))
listing = get("/collections")
check("it lists the open collection", "Seaside trip" in listing)
check("and links to the campaign page",
      "/collections/seaside-trip" in listing)
check("campaign page opens",
      client.get("/collections/seaside-trip").status_code == 200)
# Counted on the OPENING of the class attribute, not the whole of it:
# the last group also carries .nav-group-last (its panel opens leftwards
# so it cannot hang off the window), and an exact-string count read that
# as one group fewer.
check("it is under Get involved, not a new top-level item",
      home.count('class="nav-group') == 3,
      str(home.count('class="nav-group')))

# ---- legal pages are footer-only, which is conventional and correct
foot = home.split("<footer")[1]
nav = home.split("<header")[1].split("</header>")[0]
for path in ("/privacy", "/terms"):
    check("%s is in the footer" % path, path in links_in(foot))
    check("%s is NOT in the header, by design" % path,
          path not in links_in(nav))

# ---- a page whose module is switched off should not be linked either
with app.app_context():
    FeatureFlag.query.filter_by(name="donations").first().enabled = False
    db.session.commit()
home_off = get("/")
check("flag off: collections drops out of the chrome",
      "/collections" not in chrome_links(home_off))
check("flag off: the listing 404s", client.get("/collections").status_code
      == 404)
check("flag off: it leaves the sitemap",
      "/collections" not in get("/sitemap.xml"))
with app.app_context():
    FeatureFlag.query.filter_by(name="donations").first().enabled = True
    db.session.commit()

# ---- every page the chrome links to must actually work
for path in sorted(CHROME):
    if path.startswith("/admin"):
        continue
    status = client.get(path).status_code
    check("chrome link %s works" % path, status == 200, str(status))

# ---- and the sitemap should agree with what is reachable
sitemap = get("/sitemap.xml")
for path in ROUTES:
    if path in NOT_LINKED or path in REACHED_FROM:
        continue
    check("%s is in the sitemap" % path, path in sitemap,
          "reachable but not offered to search engines")

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
