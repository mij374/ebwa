"""Static asset cache busting: the ?v= token on the site's own files.

nginx serves /static/ with `expires 30d`, so the URL is the only thing
that can tell a returning visitor's browser to fetch a new stylesheet.
The whole feature is two properties, and they pull in opposite
directions — which is why both are asserted here rather than just the
first:

  * the token CHANGES when the file changes, or a deploy is invisible to
    anybody who has been to the site before;
  * the token HOLDS STILL when it does not, or every deploy re-downloads
    every asset and the 30 days buy nothing.

The second is why the token is a content hash and not an mtime: a fresh
clone, an rsync without -t or a stray `touch` restamps files whose bytes
never moved.

Run:  python tests/smoke_test_asset_version.py
"""
import io
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_assets.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from werkzeug.security import generate_password_hash  # noqa: E402

from app import (app, db, asset_version, DEFAULT_BLOCKS,  # noqa: E402
                 Block, FEATURES, FeatureFlag, User)

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
    for n, _l, _d, default in FEATURES:
        if not FeatureFlag.query.filter_by(name=n).first():
            db.session.add(FeatureFlag(name=n, enabled=default))
    db.session.commit()

app.config["TESTING"] = True
client = app.test_client()

CSS = "css/style.css"
ICONS = ("img/favicon-32x32.png", "img/favicon-16x16.png",
         "img/apple-touch-icon.png")
css_path = os.path.join(app.static_folder, "css", "style.css")


def versions_in(html, filename):
    """Every ?v= token attached to `filename` in a rendered page."""
    pattern = r"/static/%s\?v=([0-9a-f]+)" % re.escape(filename)
    return re.findall(pattern, html)


# ---- the token itself
first = asset_version(CSS)
check("a static file has a version token", bool(first), str(first))
check("it is a short hex token", bool(re.fullmatch(r"[0-9a-f]{8}", first or "")),
      str(first))
check("and the same file gives the same one twice",
      asset_version(CSS) == first)

# ---- every template that owns a <head> carries it
PAGES = [("/", "the public pages"),
         ("/admin/login", "the login page")]
for url, label in PAGES:
    html = client.get(url).data.decode("utf-8")
    found = versions_in(html, CSS)
    check("%s: the stylesheet is versioned" % label, found == [first],
          str(found))
    for icon in ICONS:
        got = versions_in(html, icon)
        check("%s: %s is versioned" % (label, icon.split("/")[-1]),
              got == [asset_version(icon)], str(got))
    check("%s: no unversioned stylesheet slips through" % label,
          "/static/css/style.css\"" not in html
          and "/static/css/style.css'" not in html)

# The admin chrome is a different base template with ITS OWN <head>, and
# that is exactly how the admin pages went months with no tab icon. So
# log in and look at the real thing.
with app.app_context():
    if not User.query.filter_by(email="netbus@example.com").first():
        db.session.add(User(email="netbus@example.com",
                            password_hash=generate_password_hash("pw123456"),
                            role="super_admin"))
        db.session.commit()
client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": "pw123456"})
html = client.get("/admin").data.decode("utf-8")
check("the admin chrome is versioned too", versions_in(html, CSS) == [first],
      str(versions_in(html, CSS)))
for icon in ICONS:
    got = versions_in(html, icon)
    check("the admin chrome: %s is versioned" % icon.split("/")[-1],
          got == [asset_version(icon)], str(got))
client.get("/admin/logout")

# Both login pages own a <head> of their own as well, and the 2FA one
# needs a half-finished sign-in to reach. Rather than build one, hold
# the rule at the source: EVERY template with a <head> versions its
# stylesheet. That is the check that catches the next template to be
# added, which is how this class of bug arrives.
heads = []
for root, _dirs, names in os.walk(os.path.join(os.path.dirname(HERE),
                                               "templates")):
    for name in names:
        if not name.endswith(".html"):
            continue
        path = os.path.join(root, name)
        text = io.open(path, encoding="utf-8").read()
        # The CLOSING tag: _icons.html mentions "<head>" in its own
        # comment, and it is the include, not a page.
        if "</head>" in text:
            heads.append((os.path.relpath(path, HERE), text))
check("found the templates that own a <head>", len(heads) >= 4,
      str([h[0] for h in heads]))
for name, text in heads:
    link = [ln for ln in text.splitlines() if "css/style.css" in ln]
    check("%s: its stylesheet link is versioned" % os.path.basename(name),
          bool(link) and all("asset_version(" in ln for ln in link),
          str(link))
    check("%s: it includes the shared icons" % os.path.basename(name),
          '_icons.html' in text)

# ---- it changes when the file changes...
backup = css_path + ".versiontest"
shutil.copy2(css_path, backup)
try:
    with open(css_path, "a", encoding="utf-8") as fh:
        fh.write("\n/* cache-busting smoke test */\n")
    changed = asset_version(CSS)
    check("editing the file changes the token", changed != first,
          "%s -> %s" % (first, changed))
    html = client.get("/").data.decode("utf-8")
    check("and the page serves the new one", versions_in(html, CSS) == [changed],
          str(versions_in(html, CSS)))

    # ...and comes back to exactly the old one when the bytes come back,
    # even though the mtime is now different. This is the property an
    # mtime-based token would fail, and the reason for a content hash.
    shutil.copy2(backup, css_path)
    os.utime(css_path, None)          # a deploy that rewrote an unchanged file
    restored = asset_version(CSS)
    check("restoring the content restores the token, new mtime and all",
          restored == first, "%s vs %s" % (restored, first))
finally:
    shutil.copy2(backup, css_path)
    os.remove(backup)

# ---- a missing file must not take a page down
check("a missing file has no token", asset_version("css/not-here.css") is None)
check("and no empty one either", asset_version("") is None)
with app.test_request_context():
    from flask import url_for
    built = url_for("static", filename="css/not-here.css",
                    v=asset_version("css/not-here.css"))
    check("so its URL is simply unversioned, not broken",
          built.endswith("/static/css/not-here.css"), built)

check("the site still renders after all that",
      client.get("/").status_code == 200)

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
