"""Smoke test for testimonials: editing, the scroller, and its settings.

The editing half matters more here than on most of these forms. A
testimonial is somebody else's words, and before this there was no edit
route at all — a typo in a quote could only be fixed by deleting it and
typing it again from memory.

The rest covers the row: four or more quotes become a scroller sharing
the partner row's marquee, fewer stay a grid, and the movement settings
are the partner ones over again but SEPARATE, so the quotes can stand
still while the logos drift.

Run:  python tests/smoke_test_testimonials.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_testimonials.db")
for _suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _suffix):
        os.remove(TEST_DB + _suffix)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from werkzeug.security import generate_password_hash  # noqa: E402

from app import (app, db, AuditLog, Block, DEFAULT_BLOCKS,  # noqa: E402
                 FEATURES, FeatureFlag, MOTION_ROWS,
                 PARTNER_DRIFT_DEFAULT, PARTNER_GLIDE_DEFAULT,
                 HOME_TESTIMONIALS, ROW_SCROLLER_MIN, Testimonial,
                 User, row_motion)

app.config["TESTING"] = True
PW = "testimonials-test-password"
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
    db.session.add(User(email="netbus@example.com",
                        password_hash=generate_password_hash(PW),
                        role="super_admin"))
    db.session.commit()

client = app.test_client()
anon = app.test_client()
client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})


def home():
    return client.get("/").data.decode("utf-8")


def count(n):
    """Exactly `n` published testimonials, named so they can be found."""
    with app.app_context():
        Testimonial.query.delete()
        for i in range(n):
            db.session.add(Testimonial(name="Person %d" % i, role="Member",
                                       quote="Quote number %d." % i,
                                       published=True, sort=i))
        db.session.commit()


# =====================================================================
# Editing — the reason this module was reopened
# =====================================================================
r = client.post("/admin/testimonials/new",
                data={"name": "Fatima Rahman", "role": "Parent",
                      "quote": "The wekend school changed my daughter.",
                      "sort": "0", "published": "on"},
                follow_redirects=True)
check("a testimonial can be added from its own form page",
      b"Testimonial saved" in r.data)
with app.app_context():
    t = Testimonial.query.filter_by(name="Fatima Rahman").first()
    check("and is stored with everything typed",
          t is not None and t.role == "Parent" and t.published, str(t))
    t_id = t.id

page = client.get("/admin/testimonials").data.decode("utf-8")
check("the list offers an Edit link, which it never used to",
      "/admin/testimonials/%d/edit" % t_id in page)

form = client.get("/admin/testimonials/%d/edit" % t_id).data.decode("utf-8")
check("the edit form opens with the quote already in it",
      "The wekend school changed my daughter." in form)
check("and with the name and role filled in",
      'value="Fatima Rahman"' in form and 'value="Parent"' in form)

r = client.post("/admin/testimonials/%d/edit" % t_id,
                data={"name": "Fatima Rahman", "role": "Parent",
                      "quote": "The weekend school changed my daughter.",
                      "sort": "0", "published": "on"},
                follow_redirects=True)
check("EDITING FIXES THE TYPO WITHOUT DELETING THE QUOTE",
      b"Testimonial saved" in r.data)
with app.app_context():
    t = db.session.get(Testimonial, t_id)
    check("the corrected words are stored",
          t.quote == "The weekend school changed my daughter.", t.quote)
    check("and it is the SAME row, not a retyped replacement",
          Testimonial.query.count() == 1 and t.id == t_id)
    entry = AuditLog.query.order_by(AuditLog.id.desc()).first()
    check("the edit is audit-logged, naming the field and not the words",
          entry.action == "edit" and "quote" in (entry.summary or "")
          and "weekend school" not in (entry.summary or ""),
          entry.summary)

r = client.post("/admin/testimonials/new",
                data={"name": "", "quote": "No name given."},
                follow_redirects=True)
check("a testimonial with no name is refused",
      b"Name and quote are required" in r.data)
r = client.post("/admin/testimonials/new",
                data={"name": "Someone", "quote": ""},
                follow_redirects=True)
check("and so is one with no words", b"Name and quote are required" in r.data)
check("neither left a row behind",
      client.get("/admin/testimonials").data.count(b"/edit") == 1,
      str(client.get("/admin/testimonials").data.count(b"/edit")))

r = client.get("/admin/testimonials/9999/edit")
check("editing a testimonial that is not there 404s", r.status_code == 404)
r = anon.get("/admin/testimonials/%d/edit" % t_id)
check("anon edit -> login redirect",
      r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
      str(r.status_code))

# =====================================================================
# The row: a grid until there are enough quotes, then a scroller
# =====================================================================
MIN = ROW_SCROLLER_MIN["testimonials"]
check("the threshold is lower than the partners' one, quote cards being "
      "wider", MIN < ROW_SCROLLER_MIN["partners"],
      "%d vs %d" % (MIN, ROW_SCROLLER_MIN["partners"]))

for n in range(1, MIN):
    count(n)
    html = home()
    check("%d testimonial(s): a plain grid" % n,
          'class="quote-grid"' in html and 'id="quoteRow"' not in html)
    check("%d testimonial(s): one card each" % n,
          html.count('class="quote-card"') == n,
          str(html.count('class="quote-card"')))

count(MIN)
html = home()
check("%d TESTIMONIALS TIP INTO THE SCROLLER" % MIN,
      'id="quoteRow"' in html and 'class="quote-grid"' not in html)
check("the scroller holds two sets, the copy for the loop",
      html.count('class="marquee-set"') == 2,
      str(html.count('class="marquee-set"')))
check("one real card and one copy per quote",
      html.count('class="quote-card"') == MIN * 2,
      str(html.count('class="quote-card"')))
check("the copy is hidden from screen readers",
      'class="marquee-set" aria-hidden="true"' in html)
check("every quote is named once for a screen reader",
      all(html.count(">Person %d<" % i) >= 1 for i in range(MIN)))
check("the row is reachable from a keyboard, having no links inside it",
      'id="quoteRow" tabindex="0"' in html)
check("it carries arrows, labelled for this row",
      'aria-label="Previous testimonials"' in html
      and 'aria-label="Next testimonials"' in html)
check("and they are NOT hidden, so a browser that never runs the script "
      "has something to push", "hidden>" not in html)

# The homepage cap, raised from six to twelve now the row scrolls: six
# was the size of a grid three across and two down, and there is no
# grid any more. It still matters, because the homepage is the ONLY
# place testimonials appear — a quote past the cap is published and
# invisible — so the number is asserted rather than assumed.
count(8)
html = home()
check("eight testimonials: all eight shown, plus their copies",
      html.count('class="marquee-set"') == 2
      and html.count('class="quote-card"') == 16,
      str(html.count('class="quote-card"')))
count(HOME_TESTIMONIALS + 3)
html = home()
check("more than the cap: the homepage stops at %d" % HOME_TESTIMONIALS,
      html.count('class="quote-card"') == HOME_TESTIMONIALS * 2,
      str(html.count('class="quote-card"')))
check("and the ones past it are absent, not hidden",
      ">Person %d<" % (HOME_TESTIMONIALS + 2) not in html)
check("the admin list shows every one of them, cap or no cap",
      client.get("/admin/testimonials").data.count(b"/edit")
      == HOME_TESTIMONIALS + 3,
      str(client.get("/admin/testimonials").data.count(b"/edit")))


# =====================================================================
# The notice about quotes past the cap. Silent invisibility is the whole
# problem: an admin sees a testimonial saved, listed and marked
# Published, and never sees it on the site. So the threshold is asserted
# in BOTH directions — the notice must not be page furniture either.
# =====================================================================
def admin_list():
    return client.get("/admin/testimonials").data.decode("utf-8")


def flat(html):
    """Whitespace collapsed: the notice wraps across several source
    lines, and a contiguous-string search would be testing the
    indentation rather than the words."""
    return " ".join(html.split())


NOTICE = "published but not visible anywhere"

count(HOME_TESTIMONIALS - 1)
check("under the cap: no notice", NOTICE not in admin_list())
count(HOME_TESTIMONIALS)
check("exactly at the cap: still no notice, nothing is being hidden",
      NOTICE not in admin_list())

count(HOME_TESTIMONIALS + 1)
page = admin_list()
check("ONE PAST THE CAP: the notice appears", NOTICE in page)
check("and names both real numbers",
      "You have %d published testimonials" % (HOME_TESTIMONIALS + 1)
      in flat(page)
      and "the homepage shows the first %d" % HOME_TESTIMONIALS
      in flat(page),
      flat(page)[flat(page).find("You have"):][:120])
check("with the one hidden quote counted, and read as singular",
      "The other 1 is" in flat(page),
      page[page.find("The other"):][:80])

count(HOME_TESTIMONIALS + 3)
page = admin_list()
check("three past the cap: the count follows, and reads as plural",
      "You have %d published testimonials" % (HOME_TESTIMONIALS + 3)
      in flat(page)
      and "The other 3 are" in flat(page),
      page[page.find("The other"):][:80])
check("it says WHY they are invisible, not just that they are",
      "no page listing all" in flat(page))
check("and how to promote one, which is the sort field",
      "sort order" in flat(page) and "lower sort number" in flat(page))
check("it uses the same notice box as the dashboard",
      'class="admin-attention"' in page)

# HIDDEN testimonials do not count towards the cap, because they are not
# on the homepage in the first place — a page full of hidden quotes must
# not raise a notice about quotes that are not published.
with app.app_context():
    for t in Testimonial.query.order_by(Testimonial.id).limit(4).all():
        t.published = False
    db.session.commit()
check("hiding enough of them takes the notice away again",
      NOTICE not in admin_list())
with app.app_context():
    published = Testimonial.query.filter_by(published=True).count()
    check("(and that really did leave fewer published than the cap)",
          published <= HOME_TESTIMONIALS, str(published))

# =====================================================================
# Movement settings — the partners' again, but separate
# =====================================================================
count(MIN)
with app.app_context():
    m = row_motion("testimonials")
    check("THE QUOTE ROW STARTS STILL, unlike the partner row",
          m["mode"] == "none", str(m))
    check("but with the same speeds as the partners",
          m["glide_ms"] == PARTNER_GLIDE_DEFAULT
          and m["drift_speed"] == PARTNER_DRIFT_DEFAULT, str(m))

page = client.get("/admin/testimonials").data.decode("utf-8")
check("the settings are on the testimonials page",
      'action="/admin/testimonials/motion"' in page)
check("with the same advanced section and warning",
      "admin-advanced" in page and "only change them if you are sure" in page)
check("and the reset button",
      'action="/admin/testimonials/motion/reset"' in page)
check("it says the settings are separate from the partners'",
      "separate from" in page.replace("\n", " ").replace("  ", " "))

r = client.post("/admin/testimonials/motion",
                data={"motion": "scroll", "step_seconds": "8",
                      "glide_ms": "600", "drift_speed": "30"},
                follow_redirects=True)
check("saving the movement works", b"row movement saved" in r.data)
with app.app_context():
    m = row_motion("testimonials")
    check("the quote row's settings changed",
          (m["mode"], m["step_seconds"], m["glide_ms"], m["drift_speed"])
          == ("scroll", 8, 600, 30), str(m))
    p = row_motion("partners")
    check("AND THE PARTNER ROW DID NOT MOVE WITH IT",
          (p["mode"], p["step_seconds"], p["glide_ms"], p["drift_speed"])
          == ("scroll", 4, PARTNER_GLIDE_DEFAULT, PARTNER_DRIFT_DEFAULT),
          str(p))
html = home()
check("and the row carries them for the script",
      'data-motion="scroll"' in html.split('data-row="testimonials"')[1][:200]
      and 'data-glide-ms="600"' in html.split('data-row="testimonials"')[1][:300])

for bad, why in (({"motion": "spin", "step_seconds": "4"}, "unknown mode"),
                 ({"motion": "step", "step_seconds": "0"}, "interval too low"),
                 ({"motion": "step", "step_seconds": "4",
                   "glide_ms": "50"}, "glide too short"),
                 ({"motion": "step", "step_seconds": "4",
                   "drift_speed": "900"}, "drift too fast"),
                 ({"motion": "step", "step_seconds": "1",
                   "glide_ms": "3000"}, "a step longer than the wait")):
    r = client.post("/admin/testimonials/motion", data=bad,
                    follow_redirects=True)
    check("refused: %s" % why, b"row movement saved" not in r.data, str(bad))
with app.app_context():
    m = row_motion("testimonials")
    check("and every refusal left the settings alone",
          (m["mode"], m["glide_ms"], m["drift_speed"]) == ("scroll", 600, 30),
          str(m))

r = client.post("/admin/testimonials/motion/reset", follow_redirects=True)
check("reset works", b"put back to the defaults" in r.data)
with app.app_context():
    m = row_motion("testimonials")
    check("the speeds are back to the shipped constants",
          m["glide_ms"] == PARTNER_GLIDE_DEFAULT
          and m["drift_speed"] == PARTNER_DRIFT_DEFAULT, str(m))
    check("and what the row DOES was left alone",
          m["mode"] == "scroll" and m["step_seconds"] == 8, str(m))
    entry = AuditLog.query.order_by(AuditLog.id.desc()).first()
    check("the reset is audit-logged, naming this row",
          "testimonial row speeds" in (entry.summary or ""), entry.summary)

r = anon.post("/admin/testimonials/motion", data={"motion": "none"})
check("anon POST motion -> login redirect",
      r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""))
r = anon.post("/admin/testimonials/motion/reset")
check("anon POST reset -> login redirect",
      r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""))

# the settings are not loose in the page editor
r = client.get("/admin/content")
check("the movement settings stay OUT of the content editor",
      all(MOTION_ROWS["testimonials"][k].encode() not in r.data
          for k in ("mode_key", "step_key", "glide_key", "drift_key")))

# a database predating the settings still renders
with app.app_context():
    for row in Block.query.filter_by(group="testimonials").all():
        db.session.delete(row)
    db.session.commit()
    m = row_motion("testimonials")
    check("with no rows at all it falls back to a still row",
          m["mode"] == "none" and m["glide_ms"] == PARTNER_GLIDE_DEFAULT,
          str(m))
check("and the homepage still renders", 'data-row="testimonials"' in home())

# ---- teardown
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
