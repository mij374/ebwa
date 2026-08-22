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

from app import (app, db, AuditLog, Block, DEFAULT_BLOCKS,  # noqa: E402
                 FEATURES,
                 FeatureFlag, PARTNER_MODES, PARTNER_MOTIONS,
                 PARTNER_MOTION_KEY, PARTNER_STEP_DEFAULT,
                 PARTNER_STEP_KEY, Partner, UPLOAD_DIR, User,
                 partner_motion)

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
    # A partner created before any of this: no logo, mode 'text', which
    # is what every row held before new partners started defaulting to
    # the logo. Set explicitly, because the Python-side default has since
    # changed and this fixture stands for a row on disk.
    legacy = Partner(name="Enfield Council", url="https://www.enfield.gov.uk",
                     blurb="Our local authority partner",
                     display_mode="text")
    db.session.add(legacy)
    db.session.commit()
    legacy_id = legacy.id

client = app.test_client()

# ---- existing rows keep text, new ones default to the logo
with app.app_context():
    p = db.session.get(Partner, legacy_id)
    check("an existing partner keeps 'text'", p.display_mode == "text",
          repr(p.display_mode))
    fresh = Partner(name="Brand New Partner")
    db.session.add(fresh)
    db.session.flush()
    check("A NEW PARTNER DEFAULTS TO THE LOGO",
          fresh.display_mode == "image", repr(fresh.display_mode))
    check("and with no logo yet it still shows its name, not an empty card",
          fresh.shows_text and not fresh.shows_logo)
    db.session.rollback()
    check("nothing else was migrated",
          {q.display_mode for q in Partner.query.all()} == {"text"},
          str({q.display_mode for q in Partner.query.all()}))
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
form = r.data.decode("utf-8")
check("the new-partner form starts on 'Logo only'",
      re.search(r'<option value="image"\s+selected', form) is not None,
      form[form.find("display_mode"):][:400])
check("and not on the text option",
      re.search(r'<option value="text"\s+selected', form) is None)
check("the upload field says what size to supply",
      "400x200px PNG with a transparent background" in form
      and "fitted automatically" in form)
r = client.get("/admin/partners/%d/edit" % legacy_id)
edit = r.data.decode("utf-8")
check("EDITING AN EXISTING PARTNER KEEPS ITS OWN MODE",
      re.search(r'<option value="text"\s+selected', edit) is not None
      and re.search(r'<option value="image"\s+selected', edit) is None,
      edit[edit.find("display_mode"):][:400])

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

# ---- four or fewer stay a static row; five or more become a scroller
def partner_count(n):
    """Leave exactly n partners in the database."""
    with app.app_context():
        for row in Partner.query.all():
            if row.logo and os.path.isfile(os.path.join(UPLOAD_DIR, row.logo)):
                os.remove(os.path.join(UPLOAD_DIR, row.logo))
            db.session.delete(row)
        db.session.commit()
        for i in range(n):
            db.session.add(Partner(name="Partner %d" % i,
                                   url="https://example.org/%d" % i,
                                   display_mode="text", sort=i))
        db.session.commit()


for n in (1, 4):
    partner_count(n)
    html = home()
    check("%d partners: still the static row" % n,
          'class="partner-grid"' in html and "partner-marquee" not in html)
    check("%d partners: one card each" % n,
          html.count('class="partner-card"') == n,
          str(html.count('class="partner-card"')))

partner_count(5)
html = home()
check("FIVE PARTNERS TIP INTO THE SCROLLER",
      "partner-marquee" in html and 'class="partner-grid"' not in html)
check("the scroller holds two sets", html.count('class="partner-set"') == 2,
      str(html.count('class="partner-set"')))
check("one real card and one copy per partner",
      html.count('class="partner-card"') == 10,
      str(html.count('class="partner-card"')))
check("the copy is hidden from screen readers",
      'class="partner-set" aria-hidden="true"' in html)
check("the copy is out of the tab order",
      html.count('tabindex="-1"') == 5, str(html.count('tabindex="-1"')))
check("the real set is not aria-hidden and not tabindexed",
      html.split('aria-hidden="true"')[0].count('tabindex="-1"') == 0)
check("the count reaches the CSS, so the speed suits the row",
      "--partner-count:5" in html, html[html.find("partner-marquee"):][:120])
check("every partner is named once for a screen reader",
      all(html.count(">Partner %d<" % i) + html.count('"Partner %d"' % i) >= 1
          for i in range(5)))

partner_count(9)
html = home()
check("nine partners: still one scroller, eighteen cards",
      html.count('class="partner-set"') == 2
      and html.count('class="partner-card"') == 18,
      str(html.count('class="partner-card"')))
check("nine partners: the duration follows the count",
      "--partner-count:9" in html)

# back to four: the scroller goes away again
partner_count(4)
check("dropping back to four returns the static row",
      "partner-marquee" not in home())

# ---- how the row moves: one site setting, not a field per partner
partner_count(5)
with app.app_context():
    check("the movement setting starts on continuous scrolling",
          partner_motion() == {"mode": "scroll",
                               "step_seconds": PARTNER_STEP_DEFAULT},
          str(partner_motion()))
html = home()
check("the row carries the mode for the CSS and the script",
      'data-motion="scroll"' in html, html[html.find("partner-marquee"):][:200])
check("and the interval, so no Jinja is needed inside the script",
      'data-step-seconds="%d"' % PARTNER_STEP_DEFAULT in html)

r = client.get("/admin/partners")
page = r.data.decode("utf-8")
check("the admin page offers every movement option",
      all(('value="%s"' % m).encode() in r.data
          for m, _l, _h in PARTNER_MOTIONS), str(r.status_code))
check("the admin page names them in plain English",
      "Continuous smooth scroll" in page and "Step every few seconds" in page
      and "No movement" in page)
check("the admin page says reduced motion overrides the choice",
      "reduce motion" in page)
check("the interval is a field too", 'name="step_seconds"' in page)

r = client.post("/admin/partners/motion",
                data={"motion": "step", "step_seconds": "7"},
                follow_redirects=True)
check("saving the setting works", b"movement saved" in r.data)
with app.app_context():
    check("STEPPING IS STORED AS A BLOCK",
          partner_motion() == {"mode": "step", "step_seconds": 7},
          str(partner_motion()))
    keys = {b.key for b in Block.query.filter_by(group="partners").all()}
    check("both settings live in the partners group",
          keys == {PARTNER_MOTION_KEY, PARTNER_STEP_KEY}, str(keys))
    entry = (AuditLog.query.filter_by(action="edit")
             .order_by(AuditLog.id.desc()).first())
    check("the change is audit-logged in plain words",
          entry is not None and "partner row moves" in (entry.summary or ""),
          entry.summary if entry else "none")
html = home()
check("the row now says step, with the chosen interval",
      'data-motion="step"' in html and 'data-step-seconds="7"' in html)

r = client.post("/admin/partners/motion",
                data={"motion": "none", "step_seconds": "4"},
                follow_redirects=True)
check("no movement can be chosen too", b"movement saved" in r.data
      and 'data-motion="none"' in home())

# rubbish in is refused, and does not damage what is stored
for bad in ({"motion": "spin", "step_seconds": "4"},
            {"motion": "step", "step_seconds": "0"},
            {"motion": "step", "step_seconds": "600"},
            {"motion": "step", "step_seconds": "soon"}):
    r = client.post("/admin/partners/motion", data=bad, follow_redirects=True)
    check("refused: %s" % bad, b"movement saved" not in r.data, str(bad))
with app.app_context():
    check("and the stored setting survived every refusal",
          partner_motion()["mode"] == "none", str(partner_motion()))

# the settings are not loose in the page editor (HIDDEN_BLOCK_KEYS)
r = client.get("/admin/content")
check("the content editor opens", r.status_code == 200, str(r.status_code))
check("the movement settings stay OUT of the content editor",
      PARTNER_MOTION_KEY.encode() not in r.data
      and PARTNER_STEP_KEY.encode() not in r.data, str(r.status_code))
check("(and the editor really does list other blocks)",
      b"Hero headline" in r.data and b'name="block_' in r.data)

# a database that predates the settings still renders
with app.app_context():
    for row in Block.query.filter_by(group="partners").all():
        db.session.delete(row)
    db.session.commit()
    check("with no rows at all it falls back to scrolling",
          partner_motion() == {"mode": "scroll",
                               "step_seconds": PARTNER_STEP_DEFAULT},
          str(partner_motion()))
check("and the homepage still renders", 'data-motion="scroll"' in home())
r = client.post("/admin/partners/motion",
                data={"motion": "step", "step_seconds": "3"},
                follow_redirects=True)
check("saving recreates the missing rows", b"movement saved" in r.data)
with app.app_context():
    check("and they hold the new values",
          partner_motion() == {"mode": "step", "step_seconds": 3},
          str(partner_motion()))

# anonymous visitors cannot change it
r = anon.post("/admin/partners/motion", data={"motion": "none",
                                              "step_seconds": "4"})
check("anon POST /admin/partners/motion -> login redirect",
      r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
      str(r.status_code))
with app.app_context():
    check("and nothing changed", partner_motion()["mode"] == "step")

partner_count(4)

# ---- the Donate button follows the donations flag
set_flag("donations", True)
html = home()
# to </nav>, not the first </ul>: the nav has dropdown lists inside it
nav = html.split('id="navLinks"')[1].split("</nav>")[0]
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
# to </nav>, not the first </ul>: the nav has dropdown lists inside it
nav = html.split('id="navLinks"')[1].split("</nav>")[0]
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
