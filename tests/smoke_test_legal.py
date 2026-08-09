"""Smoke test for the legal pages and the cookie notice (CLAUDE.md rules).

Covers: /privacy and /terms render their editable Blocks with multiple
paragraphs, both are linked in the footer and listed in the sitemap, the
notice appears on a first visit and stops appearing once dismissed, the
dismissal is a plain server-set first-party cookie needing no JavaScript,
and the redirect after dismissal cannot be pushed off-site.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_legal.py
"""
import os
import sys

from markupsafe import escape

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_legal.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, Block, COOKIE_NOTICE_NAME,  # noqa: E402
                 DEFAULT_BLOCKS, FEATURES, FeatureFlag, User)

app.config["TESTING"] = True

PW = "legal-test-password"
LEGAL_KEYS = ["privacy_title", "privacy_body", "terms_title", "terms_body"]

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def cookie_names(client):
    return {c.key for c in client._cookies.values()} \
        if hasattr(client, "_cookies") else set()


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
    for n, _l, _d, default in FEATURES:
        if not FeatureFlag.query.filter_by(name=n).first():
            db.session.add(FeatureFlag(name=n, enabled=default))
    u = User(email="admin@example.com")
    u.set_password(PW)
    db.session.add(u)
    db.session.commit()

defaults = {key: value for _g, key, _l, _k, value in DEFAULT_BLOCKS}
client = app.test_client()

# ---- the blocks are seeded in their own group
for key in LEGAL_KEYS:
    check("%s is a seeded block" % key, key in defaults)
with app.app_context():
    group = {b.key: b.group for b in Block.query.all()}
check("legal blocks live in the 'legal' group",
      all(group.get(k) == "legal" for k in LEGAL_KEYS),
      str({k: group.get(k) for k in LEGAL_KEYS}))

# ---- both pages render their block content
for path, title_key, body_key in (("/privacy", "privacy_title",
                                   "privacy_body"),
                                  ("/terms", "terms_title", "terms_body")):
    r = client.get(path)
    check("GET %s -> 200" % path, r.status_code == 200, str(r.status_code))
    html = r.data.decode("utf-8")
    check("%s shows its title" % path, defaults[title_key] in html)
    # Jinja escapes the copy, so compare against the escaped form
    first_para = str(escape(defaults[body_key].split("\n")[0]))
    check("%s shows its body" % path, first_para in html, first_para[:80])
    check("%s makes clear the wording is a placeholder" % path,
          "PLACEHOLDER" in html)
    paragraphs = defaults[body_key].split("\n")
    check("%s renders each paragraph separately" % path,
          html.count("<p>") >= len([p for p in paragraphs if p.strip()]),
          "%d <p> for %d paragraphs" % (html.count("<p>"), len(paragraphs)))

# ---- editing the blocks changes the pages (nothing hardcoded)
with app.app_context():
    for i, key in enumerate(LEGAL_KEYS):
        Block.query.filter_by(key=key).first().value = \
            "EDITED%d line one\nEDITED%d line two" % (i, i)
    db.session.commit()
privacy = client.get("/privacy").data.decode("utf-8")
terms = client.get("/terms").data.decode("utf-8")
check("editing privacy_title changes the page", "EDITED0" in privacy)
check("editing privacy_body changes the page", "EDITED1" in privacy)
check("editing terms_title changes the page", "EDITED2" in terms)
check("editing terms_body changes the page", "EDITED3" in terms)
check("multi-paragraph body splits into paragraphs",
      "EDITED1 line one</p>" in privacy and "EDITED1 line two</p>" in privacy)
with app.app_context():
    for key in LEGAL_KEYS:
        Block.query.filter_by(key=key).first().value = defaults[key]
    db.session.commit()

# ---- both are editable through the existing content admin
client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})
r = client.get("/admin/content?group=legal")
check("legal group listed in the content editor", r.status_code == 200,
      str(r.status_code))
with app.app_context():
    ids = [Block.query.filter_by(key=k).first().id for k in LEGAL_KEYS]
check("every legal block has a field in the editor",
      all(("block_%d" % i).encode() in r.data for i in ids))
client.get("/admin/logout")

# ---- footer links and sitemap
home = client.get("/").data.decode("utf-8")
check("footer links to /privacy", 'href="/privacy"' in home)
check("footer links to /terms", 'href="/terms"' in home)
sitemap = client.get("/sitemap.xml").data.decode("utf-8")
check("/privacy in the sitemap", "/privacy</loc>" in sitemap, sitemap[:200])
check("/terms in the sitemap", "/terms</loc>" in sitemap)

# ---- the notice appears on a first visit
fresh = app.test_client()
r = fresh.get("/")
html = r.data.decode("utf-8")
check("notice shown on a first visit", 'class="cookie-notice"' in html)
check("notice links to the privacy page",
      'cookie-notice' in html and '/privacy' in html)
check("notice is informational, not a consent gate",
      "tracking" in html and "consent" not in html.split("cookie-notice")[1][:600],
      "wording implies consent collection")
check("notice does not block the page",
      'role="region"' in html and 'role="dialog"' not in html)
check("notice needs no JavaScript",
      "onclick" not in html.split("cookie-notice")[1][:600])
check("page still renders its content behind the notice",
      "Empowering communities" in html or "hero" in html)
check("body flagged so the footer clears the strip",
      'class="has-notice"' in html)

# ---- dismissing it sets a first-party cookie and bounces back
r = fresh.post("/cookie-notice/dismiss", data={"next": "/about"})
check("dismiss -> 302", r.status_code == 302, str(r.status_code))
check("dismiss returns to the page you were on",
      r.headers.get("Location", "").endswith("/about"),
      r.headers.get("Location", ""))
set_cookie = r.headers.get("Set-Cookie", "")
check("dismissal sets the notice cookie",
      COOKIE_NOTICE_NAME in set_cookie, set_cookie)
check("cookie is HttpOnly (no script needs it)",
      "HttpOnly" in set_cookie, set_cookie)
check("cookie is SameSite=Lax", "SameSite=Lax" in set_cookie, set_cookie)
check("cookie is scoped to the whole site", "Path=/" in set_cookie,
      set_cookie)
check("cookie is not a session cookie", "Expires=" in set_cookie
      or "Max-Age=" in set_cookie, set_cookie)

# ---- and it stays dismissed
for path in ("/", "/about", "/privacy", "/contact"):
    r = fresh.get(path)
    check("notice gone from %s after dismissal" % path,
          b'class="cookie-notice"' not in r.data, str(r.status_code))
check("body class dropped once dismissed",
      b'class="has-notice"' not in fresh.get("/").data)

# a different visitor still sees it
other = app.test_client()
check("a new visitor still sees the notice",
      b'class="cookie-notice"' in other.get("/").data)

# ---- the redirect cannot be pushed off-site
for bad in ("https://evil.example.com/", "//evil.example.com/",
            "javascript:alert(1)", ""):
    r = other.post("/cookie-notice/dismiss", data={"next": bad})
    loc = r.headers.get("Location", "")
    check("refuses to redirect to %r" % bad,
          "evil.example.com" not in loc and "javascript" not in loc,
          loc)

# ---- the admin area is unaffected by the notice
admin = app.test_client()
admin.post("/admin/login", data={"email": "admin@example.com",
                                 "password": PW})
r = admin.get("/admin")
check("no notice in the admin area",
      b'class="cookie-notice"' not in r.data)

# ---- legal pages are core: no feature flag can switch them off
with app.app_context():
    for f in FeatureFlag.query.all():
        f.enabled = False
    db.session.commit()
for path in ("/privacy", "/terms"):
    check("%s still 200 with every feature off" % path,
          client.get(path).status_code == 200)
sitemap = client.get("/sitemap.xml").data.decode("utf-8")
check("legal pages stay in the sitemap with features off",
      "/privacy</loc>" in sitemap and "/terms</loc>" in sitemap)

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
