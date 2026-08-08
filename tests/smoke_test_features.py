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
