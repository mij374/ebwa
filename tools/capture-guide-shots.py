"""Capture the screenshots used by the admin guide at /admin/help.

CAPTURED, NOT DRAWN. Every picture in the guide comes from this script
driving the real admin in a real browser, so when a screen changes the
guide can be brought back into line by re-running it rather than by
somebody noticing that a screenshot has quietly gone out of date. That
is the whole point of it being a script: a hand-made image is correct
once.

It is re-runnable and self-contained:

  * a scratch database of its own, thrown away at the end — it will
    refuse to run against instance/ebwa.db, because a guide illustrated
    with real enquiries would publish them;
  * demo content only, and that is ASSERTED rather than assumed: every
    name, address and message the camera could see is checked against
    the fixtures this file created;
  * a desktop viewport, and each shot CROPPED to the thing being
    described. A full-page screenshot of a long form is unreadable at
    the width it is displayed, so the shots are regions: a form's top
    half, the photo manager, one tick-box.

Run:  python tools/capture-guide-shots.py [--only NAME] [--keep]

Output: static/img/guide/<name>.png, overwritten in place.
"""
import os
import shutil
import sys
import threading
import time
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "static", "img", "guide")
SCRATCH = os.path.join(HERE, "guide-shots-scratch")
TEST_DB = os.path.join(SCRATCH, "guide.db")
UPLOADS = os.path.join(SCRATCH, "uploads")
ARCHIVES = os.path.join(SCRATCH, "archives")

ONLY = (sys.argv[sys.argv.index("--only") + 1]
        if "--only" in sys.argv else None)
KEEP = "--keep" in sys.argv

shutil.rmtree(SCRATCH, ignore_errors=True)
os.makedirs(UPLOADS)
os.makedirs(ARCHIVES)
os.makedirs(OUT, exist_ok=True)

# THE GUARD THAT MATTERS. Everything below writes rows and takes
# pictures of them; pointed at the real database it would photograph
# real people's enquiries and put them in a document.
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
os.environ["BACKUP_DIR"] = ARCHIVES
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from PIL import Image, ImageDraw                       # noqa: E402
from werkzeug.serving import make_server               # noqa: E402
from werkzeug.security import generate_password_hash   # noqa: E402
from playwright.sync_api import sync_playwright        # noqa: E402

from browser_motion import STILL, new_context          # noqa: E402

import app as appmod                                   # noqa: E402
from app import (app, db, User, Block, Campaign, ContactMessage,  # noqa: E402
                 Event, FeatureFlag, GalleryAlbum, GalleryImage,
                 MembershipApplication, PageView, PageViewDaily,
                 Testimonial, ContentImage, NewsPost, AuditLog,
                 CAMPAIGN_STATES, slugify, unique_slug)
import seed_demo                                       # noqa: E402

if "instance" in os.environ["DATABASE_URL"]:
    raise SystemExit("Refusing to run against the real database.")

PORT = 5195
BASE = "http://127.0.0.1:%d" % PORT
PW = "guide-capture-password"
ADMIN_EMAIL = "guide@example.invalid"
WIDTH, HEIGHT = 1440, 900

# ---------------------------------------------------------------- fixtures
# EVERY PERSON NAMED IN A SCREENSHOT IS ON THIS LIST, and the check after
# seeding proves nothing else got in. `.invalid` is reserved by RFC 2606
# and can never be a real address.
DEMO_PEOPLE = [
    ("Ayesha Rahman", "ayesha.rahman@example.invalid"),
    ("Tariq Hussain", "tariq.hussain@example.invalid"),
    ("Nasrin Begum", "nasrin.begum@example.invalid"),
    ("Imran Chowdhury", "imran.chowdhury@example.invalid"),
]
DEMO_ENQUIRIES = [
    ("Ayesha Rahman", "ayesha.rahman@example.invalid",
     "Weekend Bengali school",
     "What ages does the Saturday class take, and are there places "
     "left?"),
    ("Tariq Hussain", "tariq.hussain@example.invalid",
     "Hiring the hall",
     "Could I hire the hall on a Sunday afternoon in March?"),
    ("Nasrin Begum", "nasrin.begum@example.invalid",
     "Volunteering",
     "I would like to help with the lunch club on Tuesdays."),
]
DEMO_ALBUMS = [
    ("Eid at the centre", "Photographs from our Eid celebration in the "
                          "main hall."),
    ("Summer day out 2026", "The coach trip to Southend."),
    ("Weekend school", "The Saturday Bengali and Arabic classes."),
]

failures = []
captured = []
skipped = []


def note(message):
    print("   %s" % message)


def make_photo(path, label, colour):
    """A recognisable placeholder so a gallery screenshot has photos in it.

    Drawn rather than shipped: a real photograph of real people is
    exactly what must not end up in a document like this.
    """
    img = Image.new("RGB", (900, 600), colour)
    draw = ImageDraw.Draw(img)
    for i in range(0, 900, 60):
        draw.line([(i, 0), (i - 300, 600)], fill=(255, 255, 255, 40), width=2)
    draw.rectangle([40, 40, 860, 560], outline=(255, 255, 255), width=3)
    draw.text((70, 500), label, fill=(255, 255, 255))
    img.save(path, "JPEG", quality=85)
    thumb = img.copy()
    thumb.thumbnail((600, 600))
    stem, ext = os.path.splitext(path)
    thumb.save("%s-thumb%s" % (stem, ext), "JPEG", quality=85)


COLOURS = [(31, 90, 68), (140, 62, 52), (52, 74, 122), (120, 96, 40),
           (78, 52, 96), (36, 92, 96)]

with app.app_context():
    appmod.UPLOAD_DIR = UPLOADS
    seed_demo.seed()
    for flag in FeatureFlag.query.all():
        flag.enabled = True

    db.session.add(User(email=ADMIN_EMAIL, role="super_admin",
                        password_hash=generate_password_hash(PW)))

    # Photographs, an album each, plus a few unfiled so the "All photos"
    # idea the guide explains is visible.
    photos = []
    for i in range(9):
        name = "guide-photo-%d.jpg" % i
        make_photo(os.path.join(UPLOADS, name), "DEMO PHOTOGRAPH",
                   COLOURS[i % len(COLOURS)])
        photos.append(name)

    for i, (title, blurb) in enumerate(DEMO_ALBUMS):
        album = GalleryAlbum(title=title, description=blurb,
                             slug=unique_slug(GalleryAlbum, title),
                             cover_image=photos[i * 3], sort=i, published=True)
        db.session.add(album)
        db.session.flush()
        for j in range(3):
            db.session.add(GalleryImage(
                filename=photos[i * 3 + j], album_id=album.id,
                caption="%s — demo photograph %d" % (title, j + 1),
                sort=j))
    db.session.commit()

    # A lead photo on the first few events and news posts, so the forms
    # and lists in the guide are not full of empty boxes.
    for i, ev in enumerate(Event.query.order_by(Event.id).limit(4).all()):
        ev.image = photos[i % len(photos)]
    for i, post in enumerate(NewsPost.query.order_by(NewsPost.id)
                             .limit(3).all()):
        post.image = photos[(i + 2) % len(photos)]

    # A collection, for the state select and the money side of the guide.
    camp = Campaign(
        title="Seaside trip to Southend",
        slug=unique_slug(Campaign, "Seaside trip to Southend"),
        description="The coach leaves Ponders End at 9am and returns by "
                    "7pm. The fee covers the coach and the packed lunch; "
                    "anything you add on top is a donation to EBWA.",
        fee_pence=1200, target_pence=200000, state="open", active=True,
        image=photos[4], show_image_on_page=True)
    db.session.add(camp)

    # Enquiries — demo only, and unread so the dashboard has something to
    # point at.
    for i, (name, email, subject, message) in enumerate(DEMO_ENQUIRIES):
        db.session.add(ContactMessage(
            name=name, email=email, subject=subject, message=message,
            status="new" if i < 2 else "read",
            created_at=datetime.utcnow() - timedelta(hours=6 * i + 2)))

    # A membership application, which is what puts the one red card on
    # the dashboard.
    db.session.add(MembershipApplication(
        name="Imran Chowdhury", email="imran.chowdhury@example.invalid",
        phone="020 7946 0000", address="14 Example Road, Enfield EN3 4XX",
        over_18=True, bangladeshi_origin=True, lives_works_enfield=True,
        fee_confirmed=True, status="new",
        reason="I have lived in Enfield for twenty years and would like "
               "to support the association.",
        created_at=datetime.utcnow() - timedelta(days=1)))

    # More than a dozen published quotes, so the testimonials page shows
    # the notice the guide explains.
    for i in range(14 - Testimonial.query.filter_by(published=True).count()):
        db.session.add(Testimonial(
            name="Demo Member %d" % (i + 1), role="Member",
            quote="A short demonstration quote so the list is long enough "
                  "to show what happens past twelve.",
            published=True, sort=50 + i))

    # An event whose date has passed, for the attention panel.
    old = Event.query.order_by(Event.id).first()
    if old is not None:
        old.event_date = date.today() - timedelta(days=9)
        old.published = True

    # Visitor figures, so the chart is a chart rather than a flat line.
    today = date.today()
    for back in range(45):
        day = today - timedelta(days=back)
        views = 40 + (back * 7) % 55
        for v in range(min(views // 8, 9)):
            db.session.add(PageView(day=day, path="/about" if v % 3 else "/",
                                    visitor="demo-%d-%d" % (back, v)))
        db.session.add(PageViewDaily(day=day - timedelta(days=60),
                                     views=views, visitors=views // 4))
    db.session.commit()

    # ---- THE ASSERTION. Not "the database was empty when we started" —
    # this looks at what is actually in it now, in every table a
    # screenshot could show a person's details from.
    allowed = {name for name, _ in DEMO_PEOPLE}
    allowed |= {"Demo Member %d" % i for i in range(1, 30)}
    real = []
    for row in ContactMessage.query.all():
        if row.name not in allowed or not row.email.endswith(".invalid"):
            real.append("enquiry from %s <%s>" % (row.name, row.email))
    for row in MembershipApplication.query.all():
        if row.name not in allowed or not row.email.endswith(".invalid"):
            real.append("membership application from %s" % row.name)
    for row in Testimonial.query.all():
        if not row.name:
            real.append("a testimonial with no name")
    if appmod.Payment.query.count():
        real.append("%d payment(s) — donor details must never be "
                    "photographed" % appmod.Payment.query.count())
    if real:
        raise SystemExit("REFUSING TO CAPTURE: the database holds what "
                         "looks like real data:\n  - " + "\n  - ".join(real))
    print("Fixtures: %d photographs, %d albums, %d enquiries, "
          "%d testimonials — all demo, checked."
          % (len(photos), len(DEMO_ALBUMS), ContactMessage.query.count(),
             Testimonial.query.count()))

server = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.8)

with app.app_context():
    CAMP_ID = Campaign.query.first().id
    EVENT_ID = Event.query.order_by(Event.id.desc()).first().id
    NEWS_ID = NewsPost.query.order_by(NewsPost.id.desc()).first().id


# ------------------------------------------------------------------ shots
# name, url, and how to frame it. `region` is a CSS selector to crop to;
# `after` is a selector whose top edge starts the crop (for "the section
# further down the page"); `height` caps how much is taken, because the
# useful part of a long form is its first screenful.
SHOTS = [
    dict(name="sign-in", url="/admin/login", region=".login-card", pad=28,
         wait=".login-card"),
    dict(name="dashboard-attention", url="/admin",
         region=".admin-attention", pad=18, wait=".admin-attention"),
    dict(name="dashboard-cards", url="/admin", after=".admin-stats-head",
         height=430, wait=".admin-stat"),
    dict(name="events-list", url="/admin/events", region=".table-scroll",
         pad=14, height=520, wait=".admin-table"),
    dict(name="event-form-top", url="/admin/events/%d/edit" % EVENT_ID,
         region="form.admin-form", pad=14, height=560, wait="#title"),
    dict(name="event-form-published", url="/admin/events/%d/edit" % EVENT_ID,
         region="input[name=published]", up=".field", pad=26, padx=30,
         wait="input[name=published]"),
    dict(name="event-form-photos", url="/admin/events/%d/edit" % EVENT_ID,
         after="h1.admin-h1", text="Page layout", back=18, height=560,
         wait="#layout"),
    dict(name="event-form-photo-list", url="/admin/events/%d/edit" % EVENT_ID,
         after="h1.admin-h1", text="Photos", back=18, height=600,
         wait="#layout"),
    dict(name="news-form", url="/admin/news/%d/edit" % NEWS_ID,
         region="form.admin-form", pad=14, height=560, wait="#title"),
    dict(name="gallery-albums", url="/admin/gallery/albums",
         region=".table-scroll", pad=14, height=470, wait=".admin-table"),
    dict(name="gallery-photos", url="/admin/gallery",
         region=".admin-gallery-grid", pad=14, height=520,
         wait=".admin-gallery-grid"),
    dict(name="page-content-tabs", url="/admin/content", region=".tab-row",
         pad=20, height=260, wait=".tab-row"),
    dict(name="collection-state", url="/admin/campaigns/%d/edit" % CAMP_ID,
         region="#state", up=".field", pad=20, padx=26, wait="#state"),
    dict(name="collection-form", url="/admin/campaigns/%d/edit" % CAMP_ID,
         region="form.admin-form", pad=14, height=560, wait="#title"),
    dict(name="testimonials-notice", url="/admin/testimonials",
         region=".admin-attention", pad=16, wait=".admin-attention"),
    dict(name="sort-field", url="/admin/testimonials/new", region="#sort",
         up=".field", pad=20, padx=26, wait="#sort"),
    dict(name="enquiries-list", url="/admin/messages",
         region=".table-scroll", pad=14, height=430, wait=".admin-table"),
    dict(name="visitors-chart", url="/admin/visitors",
         region=".stats-chart", pad=18, height=430, wait=".stats-chart"),
    dict(name="visitors-figures", url="/admin/visitors",
         region=".stat-cards", pad=16, height=260, wait=".stat-cards"),
    dict(name="help-sidebar", url="/admin", region=".admin-side", pad=0,
         height=760, wait=".admin-side"),
]

# A section is anchored on its HEADING, which means finding an element
# by its words rather than by a selector — these admin pages give their
# section headings no ids, and adding them only so this script can aim at
# them would be the tail wagging the dog.
BOX = """([sel, text, up]) => {
  let el = null;
  if (text) {
    el = Array.from(document.querySelectorAll(sel))
         .find(n => (n.textContent || '').trim() === text) || null;
  } else {
    el = document.querySelector(sel);
  }
  if (!el) return null;
  if (up) { el = el.closest(up) || el; }
  const r = el.getBoundingClientRect();
  return {x: r.left + window.scrollX, y: r.top + window.scrollY,
          w: r.width, h: r.height};
}"""

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = new_context(browser, WIDTH, HEIGHT, STILL)
    page = ctx.new_page()

    # The sign-in shot has to be taken signed OUT, so it goes first.
    for shot in SHOTS:
        name = shot["name"]
        if ONLY and ONLY != name:
            continue
        if name != "sign-in" and "logged_in" not in globals():
            page.goto(BASE + "/admin/login", wait_until="load")
            page.fill("input[name=email]", ADMIN_EMAIL)
            page.fill("input[name=password]", PW)
            page.click("button[type=submit]")
            page.wait_for_load_state("load")
            globals()["logged_in"] = True

        # One window for every shot. `.admin-main` is capped at 900px, so
        # a wider one buys nothing: a wide admin table scrolls inside its
        # own box at every window size, which is what an admin really
        # sees and therefore what the guide should show.
        want = (WIDTH, HEIGHT)
        page.goto(BASE + shot["url"], wait_until="load")
        try:
            page.wait_for_selector(shot["wait"], timeout=6000)
        except Exception:
            message = "%s — %s not found on %s" % (name, shot["wait"],
                                                   shot["url"])
            if shot.get("optional"):
                skipped.append(message)
            else:
                failures.append(message)
            print("SKIP  %s" % message)
            continue

        # Let webfonts settle, or the first shot of a run has different
        # metrics from the rest.
        page.wait_for_timeout(220)

        target = shot.get("region") or shot.get("after")
        box = page.evaluate(BOX, [target, shot.get("text"),
                                 shot.get("up")])
        if box is None:
            message = "%s — could not measure %s" % (name, target)
            (skipped if shot.get("optional") else failures).append(message)
            print("SKIP  %s" % message)
            continue

        pad = shot.get("pad", 14)
        padx = shot.get("padx", pad)
        if shot.get("after"):
            top = max(0, box["y"] - shot.get("back", 0))
            # Crop to the content column. Taking the full window would
            # spend a third of the picture on the dark sidebar and the
            # empty margin beside the form.
            column = page.evaluate(BOX, [".admin-main", None, None])
            left = max(0, (column["x"] if column else 0) - 8)
            width = min((column["w"] if column else want[0]) + 16,
                        want[0] - left)
        else:
            top = max(0, box["y"] - pad)
            left = max(0, box["x"] - padx)
            width = min(box["w"] + padx * 2, want[0] - left)
        height = shot.get("height", box["h"] + pad * 2)
        height = min(height, page.evaluate(
            "() => document.documentElement.scrollHeight") - top)

        path = os.path.join(OUT, "%s.png" % name)
        # full_page, so `clip` is in PAGE coordinates. Without it the clip
        # is measured against the 900px viewport and anything below the
        # fold — the photo manager, most of a long form — is "outside the
        # resulting image".
        page.screenshot(path=path, full_page=True,
                        clip={"x": left, "y": top,
                              "width": max(width, 120),
                              "height": max(height, 80)})
        size = os.path.getsize(path)
        captured.append((name, int(width), int(height), size))
        print("OK    %-22s %4dx%-4d  %6.1f KB"
              % (name, width, height, size / 1024.0))

    browser.close()

server.shutdown()
# DISPOSE THE ENGINE, not just the session. SQLite keeps the file open
# through the connection pool, and on Windows an open handle makes the
# directory undeletable — which left the scratch database sitting in
# tools/ after every run, waiting to be committed by accident.
with app.app_context():
    db.session.remove()
    db.engine.dispose()
if not KEEP:
    for _ in range(25):
        try:
            shutil.rmtree(SCRATCH)
            break
        except OSError:
            time.sleep(0.2)
    if os.path.isdir(SCRATCH):
        print("Note: could not remove %s — it is gitignored, so this is "
              "untidy rather than a problem." % SCRATCH)

print()
print("Captured %d screenshot(s) into static/img/guide/." % len(captured))
if skipped:
    print("Skipped %d optional shot(s):" % len(skipped))
    for message in skipped:
        print("  - %s" % message)
if failures:
    print("FAILED %d shot(s):" % len(failures))
    for message in failures:
        print("  - %s" % message)
    sys.exit(1)
