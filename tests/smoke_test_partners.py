"""Smoke test for partner display modes and the header Donate button.

Covers: existing partners keep the text-only look until a logo is added,
each of the three modes renders as specified, a logo-ish mode with no
logo falls back to text rather than an empty card, logos are replaced and
cleaned up on the usual save_upload/delete_upload terms, and the Donate
button follows the donations feature flag.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_partners.py
"""
import base64
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_partners.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,  # noqa: E402
                 FeatureFlag, PARTNER_MODES, Partner, UPLOAD_DIR, User)

app.config["TESTING"] = True

# Uploads are decoded and optimised now, so a test upload has to be a
# real image. This is a 1x1 transparent PNG: small enough to need no
# thumbnail and, having an alpha channel, stored byte for byte as .png.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


PW = "partners-test-password"

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def home():
    return client.get("/").data.decode("utf-8")


def card_for(html, name):
    """The rendered partner card containing `name`, whitespace collapsed."""
    flat = re.sub(r">\s+<", "><", html)
    cards = re.findall(r'<(?:a|div) class="partner-card".*?</(?:a|div)>', flat)
    for c in cards:
        if name in c:
            return c
    return ""


def set_flag(name, enabled):
    with app.app_context():
        FeatureFlag.query.filter_by(name=name).first().enabled = enabled
        db.session.commit()


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
    # A partner created before this change: no logo, no explicit mode.
    legacy = Partner(name="Enfield Council", url="https://www.enfield.gov.uk",
                     blurb="Our local authority partner")
    db.session.add(legacy)
    db.session.commit()
    legacy_id = legacy.id

client = app.test_client()

# ---- existing rows default to text, and look exactly as before
with app.app_context():
    p = db.session.get(Partner, legacy_id)
    check("existing partner defaults to 'text'", p.display_mode == "text",
          repr(p.display_mode))
    check("existing partner has no logo", not p.logo, repr(p.logo))
card = card_for(home(), "Enfield Council")
check("text-only card shows the name",
      "<b>Enfield Council</b>" in card, card)
check("text-only card shows the blurb",
      "Our local authority partner" in card, card)
check("text-only card has no image", "partner-logo" not in card, card)
check("text-only card is still a link to the partner",
      'href="https://www.enfield.gov.uk"' in card, card)

client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})

# ---- admin: anonymous access to the new routes redirects
anon = app.test_client()
for path, method in (("/admin/partners", "GET"),
                     ("/admin/partners/new", "GET"),
                     ("/admin/partners/%d/edit" % legacy_id, "GET"),
                     ("/admin/partners/%d/delete" % legacy_id, "POST")):
    r = anon.open(path, method=method)
    check("anon %s %s -> login redirect" % (method, path),
          r.status_code == 302
          and "/admin/login" in r.headers.get("Location", ""),
          str(r.status_code))

r = client.get("/admin/partners")
check("authed GET /admin/partners -> 200", r.status_code == 200,
      str(r.status_code))
r = client.get("/admin/partners/new")
check("the form offers every display mode", r.status_code == 200
      and all(('value="%s"' % m).encode() in r.data for m in PARTNER_MODES),
      str(r.status_code))
check("the form takes a file upload",
      b'enctype="multipart/form-data"' in r.data and b'name="logo"' in r.data)

# ---- create with a logo, mode 'image'
r = client.post("/admin/partners/new", data={
    "name": "Trust For London", "url": "https://trustforlondon.org.uk",
    "blurb": "Funds our advice work", "display_mode": "image", "sort": "1",
    "logo": (io.BytesIO(TINY_PNG), "tfl.png")},
    content_type="multipart/form-data")
check("create partner -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    tfl = Partner.query.filter_by(name="Trust For London").first()
    tfl_id, tfl_logo = tfl.id, tfl.logo
    check("logo stored", bool(tfl_logo), repr(tfl_logo))
    check("mode stored", tfl.display_mode == "image", tfl.display_mode)
logo_path = os.path.join(UPLOAD_DIR, tfl_logo)
check("logo file written to uploads", os.path.isfile(logo_path))

card = card_for(home(), "trustforlondon")
check("image mode renders the logo", tfl_logo in card, card)
check("image mode hides the name text",
      "<b>Trust For London</b>" not in card, card)
check("image mode keeps the name as alt text",
      'alt="Trust For London"' in card, card)
check("image mode still links to the partner",
      'href="https://trustforlondon.org.uk"' in card, card)

# ---- switch it to 'both'
r = client.post("/admin/partners/%d/edit" % tfl_id, data={
    "name": "Trust For London", "url": "https://trustforlondon.org.uk",
    "blurb": "Funds our advice work", "display_mode": "both", "sort": "1"})
check("edit partner -> 302", r.status_code == 302, str(r.status_code))
card = card_for(home(), "trustforlondon")
check("both mode renders the logo", tfl_logo in card, card)
check("both mode also renders the name", "<b>Trust For London</b>" in card,
      card)
check("both mode renders the blurb", "Funds our advice work" in card, card)
check("logo comes before the name",
      card.index("partner-logo") < card.index("<b>Trust"), card)

# ---- a logo mode with no logo falls back to text, not an empty card
r = client.post("/admin/partners/%d/edit" % legacy_id, data={
    "name": "Enfield Council", "url": "https://www.enfield.gov.uk",
    "blurb": "Our local authority partner", "display_mode": "image",
    "sort": "0"})
check("mode change saved -> 302", r.status_code == 302, str(r.status_code))
card = card_for(home(), "Enfield Council")
check("image mode without a logo still shows the name",
      "<b>Enfield Council</b>" in card, card)
check("and renders no broken image", "partner-logo" not in card, card)
r = client.get("/admin/partners")
check("admin list flags the missing logo", b"no logo yet" in r.data)

# ---- an unknown mode is refused
r = client.post("/admin/partners/%d/edit" % legacy_id, data={
    "name": "Enfield Council", "display_mode": "carousel"},
    follow_redirects=True)
check("unknown display mode refused", b"Unknown display option" in r.data)
with app.app_context():
    check("unknown mode did not stick",
          db.session.get(Partner, legacy_id).display_mode == "image")

# ---- replacing a logo deletes the old file
r = client.post("/admin/partners/%d/edit" % tfl_id, data={
    "name": "Trust For London", "url": "https://trustforlondon.org.uk",
    "blurb": "Funds our advice work", "display_mode": "both", "sort": "1",
    "logo": (io.BytesIO(TINY_PNG), "tfl2.png")},
    content_type="multipart/form-data")
with app.app_context():
    new_logo = db.session.get(Partner, tfl_id).logo
check("logo replaced", new_logo and new_logo != tfl_logo, repr(new_logo))
check("old logo file deleted", not os.path.isfile(logo_path))
new_path = os.path.join(UPLOAD_DIR, new_logo)
check("new logo file written", os.path.isfile(new_path))

# ---- deleting a partner takes its logo file with it
r = client.post("/admin/partners/%d/delete" % tfl_id)
check("delete partner -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("partner gone", db.session.get(Partner, tfl_id) is None)
check("logo file cleaned up with the partner", not os.path.isfile(new_path))
check("deleted partner absent from the homepage",
      "Trust For London" not in home())

# ---- the Donate button follows the donations flag
set_flag("donations", True)
html = home()
nav = html.split('id="navLinks"')[1].split("</ul>")[0]
check("Donate button in the header nav when donations are on",
      "nav-donate" in nav, nav[:400])
check("Donate button links to /donate", '/donate"' in nav, nav[:400])
check("Donate button is styled as the primary action",
      'class="nav-donate"' in nav, nav[:400])
check("Donate appears on every public page",
      "nav-donate" in client.get("/about").data.decode("utf-8")
      and "nav-donate" in client.get("/contact").data.decode("utf-8"))

set_flag("donations", False)
html = home()
nav = html.split('id="navLinks"')[1].split("</ul>")[0]
check("Donate button gone when donations are off", "nav-donate" not in nav,
      nav[:400])
check("no dead link to /donate in the nav", '/donate"' not in nav, nav[:400])
check("and /donate itself 404s, as before",
      client.get("/donate").status_code == 404)
set_flag("donations", True)
check("Donate button returns with the flag", "nav-donate" in home())

# ---- teardown: delete the throwaway db (incl. WAL sidecars)
with app.app_context():
    for p in Partner.query.all():
        if p.logo and os.path.isfile(os.path.join(UPLOAD_DIR, p.logo)):
            os.remove(os.path.join(UPLOAD_DIR, p.logo))
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
