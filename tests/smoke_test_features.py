"""Smoke test for the super-admin tier and feature flags (CLAUDE.md rules).

Covers: role defaults, super_admin-only access to /admin/features (normal
admins get 403 and never see the nav link), toggling a feature off 404s
its public pages and drops its nav links and sitemap entries, the data
survives and comes back on re-enabling, and core features have no flag.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_features.py
"""
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_features.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

import app as appmod                                             # noqa: E402
from app import (app, db, FEATURES, FEATURE_DEFAULTS, Campaign,  # noqa: E402
                 FeatureFlag, Milestone, NewsPost, Resource, User)

app.config["TESTING"] = True

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    db.create_all()
    boss = User(email="netbus@example.com")
    boss.set_password("pw123456")
    boss.role = "super_admin"
    db.session.add(boss)
    client_admin = User(email="client@example.com")
    client_admin.set_password("pw123456")
    db.session.add(client_admin)
    for name, _label, _desc, default in FEATURES:
        db.session.add(FeatureFlag(name=name, enabled=default))
    # One row of content per flagged module, so we can prove nothing is lost
    post = NewsPost()
    post.title = "Winter coat appeal"
    post.slug = "winter-coat-appeal"
    post.published_date = date.today()
    post.summary = "Donations of warm coats wanted."
    db.session.add(post)
    db.session.add(Resource(name="Enfield Foodbank", category="Food support"))
    m = Milestone()
    m.year = 2021
    m.title = "Moved into our High Street centre"
    db.session.add(m)
    camp = Campaign()
    camp.title = "Seaside trip"
    camp.slug = "seaside-trip"
    camp.fee_pence = 1500
    db.session.add(camp)
    db.session.commit()

client = app.test_client()

# ---- default role is 'admin' for existing/new users
with app.app_context():
    u = User.query.filter_by(email="client@example.com").first()
    check("new user defaults to role 'admin'", u.role == "admin", repr(u.role))
    check("plain admin is not super admin", u.is_super_admin is False)
    check("super admin flagged", User.query.filter_by(
        email="netbus@example.com").first().is_super_admin is True)

# ---- core features are never flaggable
core = ("home", "about", "events", "gallery", "contact")
check("no flag exists for any core feature",
      not [c for c in core if c in FEATURE_DEFAULTS],
      ", ".join(c for c in core if c in FEATURE_DEFAULTS))

# ---- anonymous access to the settings page redirects to login
for path, method in (("/admin/features", "GET"),
                     ("/admin/features/news/toggle", "POST")):
    r = client.open(path, method=method)
    check("anon %s %s -> login redirect" % (method, path),
          r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
          str(r.status_code))

# ---- a normal client admin gets 403 and never sees the nav link
client.post("/admin/login", data={"email": "client@example.com",
                                  "password": "pw123456"})
r = client.get("/admin")
check("client admin can reach the dashboard", r.status_code == 200,
      str(r.status_code))
check("Settings link hidden from client admin", b"/admin/features" not in r.data)
r = client.get("/admin/features")
check("client admin GET /admin/features -> 403", r.status_code == 403,
      str(r.status_code))
r = client.post("/admin/features/news/toggle")
check("client admin toggle -> 403", r.status_code == 403, str(r.status_code))
with app.app_context():
    check("blocked toggle changed nothing",
          FeatureFlag.query.filter_by(name="news").first().enabled is True)
client.get("/admin/logout")

# ---- super admin sees the page and the nav link
client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": "pw123456"})
r = client.get("/admin")
check("Settings link shown to super admin", b"/admin/features" in r.data)
r = client.get("/admin/features")
check("super admin GET /admin/features -> 200", r.status_code == 200,
      str(r.status_code))
html = r.data.decode("utf-8")
check("every feature listed", all(name in html for name, _l, _d, _de
                                  in FEATURES))
check("core features absent from the settings page",
      "always on" in html and ">home<" not in html)

# ---- "How to add a domain to this site": read-only guidance, and the
# read-only part is the whole point. nginx belongs to root and a broken
# config takes this page down with the site, so the instructions live
# here and the doing lives on SSH — the same boundary the health panel
# draws, said again where somebody might otherwise ask for a button.
import re                                                    # noqa: E402
box = re.search(r'<details class="faq-item admin-help">(.*?)</details>',
                html, re.S)
check("the domain box is on the settings page", box is not None)
if box:
    inside = box.group(1)
    check("it is titled as asked",
          "<summary>How to add a domain to this site</summary>" in inside)
    check("IT IS COLLAPSED — a once-a-year job, not something to scroll "
          "past every visit",
          '<details class="faq-item admin-help">' in html
          and '<details class="faq-item admin-help" open>' not in html)
    check("IT IS READ-ONLY: nothing in it submits anything",
          not any(tag in inside for tag in ("<form", "<button", "<input",
                                            "<select", "<textarea")),
          inside[:200])

    # The paths are this deployment's, not a worked example. Proved by
    # MOVING the constant and seeing the page follow it — a template
    # with the default written into it would pass any assertion that
    # only checked for the default.
    check("it prints this deployment's nginx site file",
          appmod.DEPLOY_NGINX_SITE in inside, appmod.DEPLOY_NGINX_SITE)
    was = appmod.DEPLOY_NGINX_SITE
    appmod.DEPLOY_NGINX_SITE = "/etc/nginx/sites-available/somewhere-else"
    moved = client.get("/admin/features").data.decode("utf-8")
    check("...and follows it when the deployment is somewhere else",
          "/etc/nginx/sites-available/somewhere-else" in moved
          and was not in moved, was)
    appmod.DEPLOY_NGINX_SITE = was

    # All four steps, each by the command that does it.
    check("step 1 — the DNS record, with a way to find the address",
          "dig +short" in inside)
    check("step 2 — editing the nginx site file",
          "sudo nano" in inside and "server_name" in inside)
    check("step 3 — test THEN reload, and only if the test passed",
          "sudo nginx -t &amp;&amp; sudo systemctl reload nginx" in inside)
    check("step 4 — certbot, covering the existing name as well as the new",
          "sudo certbot --nginx -d" in inside)
    # BOTH BRANCHES, because the box has two and only one of them is on
    # screen at a time. A server reached by its IP address is not an
    # edge case here — it is what a server with no domain on it yet
    # looks like, which is precisely who opens these instructions, and
    # telling that person to `dig` an IP address would be nonsense at
    # the one moment the page is most likely to be believed.
    check("read on a bare host, it says that is the address, not a name",
          "which is an address rather than a\n        name" in inside
          or "an address rather than a" in inside, inside[:400])
    check("...and does not tell them to look it up",
          "dig +short localhost" not in inside)
    check("...nor ask certbot for a certificate for it",
          "-d localhost" not in inside, inside[inside.find("certbot"):][:120])

    # A SECOND CLIENT, signed in ON THAT HOST. Two things had to be got
    # right here: the test client builds its environ from `base_url`, so
    # a bare Host header does not change what `request.host` reports —
    # and a session cookie set for one host is not sent to another, so
    # reusing the client above returned the login page, where there is
    # no box at all and every assertion below failed for the wrong
    # reason.
    other = app.test_client()
    other.post("/admin/login", base_url="http://demo.example.org/",
               data={"email": "netbus@example.com", "password": "pw123456"})
    named = other.get("/admin/features",
                      base_url="http://demo.example.org/").data.decode("utf-8")
    check("the second client really is signed in on that host",
          "How to add a domain" in named, named[:160])
    spot = named[named.find("<summary>How to add a domain"):]
    spot = spot[:spot.find("</details>")]
    check("read on a real domain, it looks that name up",
          "dig +short demo.example.org" in spot, spot[:300])
    check("...keeps it on the server_name line",
          "server_name demo.example.org newdomain.org" in spot)
    check("...and asks certbot to cover it as well as the new names",
          "-d demo.example.org -d newdomain.org" in spot,
          spot[spot.find("certbot"):][:160])

    check("it says certbot cannot issue until DNS resolves here",
          "cannot issue anything until DNS resolves" in inside)
    check("it warns to keep mail and website DNS on separate days",
          "different day" in inside and "MX" in inside, inside[:200])

check("is_hostname tells a name from an address",
      appmod.is_hostname("demo.netbus.co.uk") is True
      and appmod.is_hostname("127.0.0.1") is False
      and appmod.is_hostname("::1") is False
      and appmod.is_hostname("localhost") is False
      and appmod.is_hostname("") is False)

# The sentence that has to be on the PAGE rather than folded inside the
# box: somebody looking for a button must meet the answer before they
# have to open anything.
check("the page says plainly that it cannot do this itself",
      "This page cannot add a domain, and deliberately\n  does not try"
      in html or "cannot add a domain" in html)
check("...and says why, in terms of what would break",
      "takes the whole site down" in html and "root" in html)

# ---- every flagged module: public pages work while it is on
LIVE = [("news", "/news", "Winter coat appeal"),
        ("resources", "/resources", "Enfield Foodbank"),
        ("our_journey", "/our-journey", "Moved into our High Street centre"),
        ("membership_form", "/membership", None),
        ("donations", "/donate", None)]
for name, path, marker in LIVE:
    r = client.get(path)
    check("%s on: GET %s -> 200" % (name, path), r.status_code == 200,
          str(r.status_code))
    if marker:
        check("%s on: content shown" % name, marker in r.data.decode("utf-8"))

r = client.get("/collections/seaside-trip")
check("donations on: GET /collections/seaside-trip -> 200",
      r.status_code == 200, str(r.status_code))
home = client.get("/").data.decode("utf-8")
check("news on: homepage strip shows the post", "Winter coat appeal" in home)
check("donations on: homepage strip shows the collection",
      "Seaside trip" in home)
sitemap = client.get("/sitemap.xml").data.decode("utf-8")
check("sitemap lists every flagged page while on",
      all(p in sitemap for p in ("/news", "/resources", "/our-journey",
                                 "/membership", "/collections/seaside-trip")))

# ---- switch every flagged module off
for name, _label, _desc, _default in FEATURES:
    r = client.post("/admin/features/%s/toggle" % name)
    check("super admin toggles %s -> 302" % name, r.status_code == 302,
          str(r.status_code))
with app.app_context():
    check("all flags now off",
          FeatureFlag.query.filter_by(enabled=True).count() == 0)

# ---- disabled features 404 publicly, and their links disappear
for name, path, _marker in LIVE:
    r = client.get(path)
    check("%s off: GET %s -> 404" % (name, path), r.status_code == 404,
          str(r.status_code))
for path in ("/news/winter-coat-appeal", "/collections/seaside-trip",
             "/donate/success", "/donate/cancelled"):
    r = client.get(path)
    check("off: GET %s -> 404" % path, r.status_code == 404, str(r.status_code))

home = client.get("/").data.decode("utf-8")
for path in ("/news", "/resources", "/our-journey", "/membership"):
    check("off: %s nav link hidden" % path, path not in home)
check("off: homepage news strip gone", "Winter coat appeal" not in home)
check("off: homepage collections strip gone", "Seaside trip" not in home)
sitemap = client.get("/sitemap.xml").data.decode("utf-8")
for path in ("/news", "/resources", "/our-journey", "/membership",
             "/collections/seaside-trip"):
    check("off: %s absent from sitemap" % path, path not in sitemap)

# ---- core pages are unaffected by every flag being off
for path in ("/", "/about", "/events", "/gallery", "/contact",
             "/sitemap.xml", "/healthz"):
    r = client.get(path)
    check("core: GET %s still 200 with all flags off" % path,
          r.status_code == 200, str(r.status_code))

# ---- the data itself survived being switched off
with app.app_context():
    check("news data survives", NewsPost.query.count() == 1)
    check("resource data survives", Resource.query.count() == 1)
    check("milestone data survives", Milestone.query.count() == 1)
    check("campaign data survives", Campaign.query.count() == 1)

# ---- switching back on restores the pages exactly as they were
for name, _label, _desc, _default in FEATURES:
    client.post("/admin/features/%s/toggle" % name)
for name, path, marker in LIVE:
    r = client.get(path)
    check("%s re-enabled: GET %s -> 200" % (name, path), r.status_code == 200,
          str(r.status_code))
    if marker:
        check("%s re-enabled: content back" % name,
              marker in r.data.decode("utf-8"))
r = client.get("/news/winter-coat-appeal")
check("re-enabled: article page back", r.status_code == 200, str(r.status_code))
home = client.get("/").data.decode("utf-8")
check("re-enabled: nav links back",
      all(p in home for p in ("/news", "/resources", "/our-journey",
                              "/membership")))

# ---- unknown flag names are not settable
r = client.post("/admin/features/board_hub/toggle")
check("unknown feature name -> 404", r.status_code == 404, str(r.status_code))
with app.app_context():
    check("no stray flag row created",
          FeatureFlag.query.filter_by(name="board_hub").count() == 0)

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
