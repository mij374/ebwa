"""The gallery's upload progress, and the shared busy state, in Chromium.

None of this can be read off the HTML, which is why it is a browser
check rather than another smoke test:

  * THAT THE REQUESTS ARE SEQUENTIAL. The markup says nothing about it.
    Twelve photographs go up one at a time, and this file proves it by
    recording every request and asserting no two of them were in flight
    at the same moment — twelve parallel Pillow encodes on a small VPS
    is the thing being avoided, and only a running browser can show
    that it is.
  * THAT ONE BAD FILE DOES NOT COST THE OTHERS THEIR UPLOAD. Eleven
    photographs, one file that is not a photograph, and the run has to
    finish, name the bad one, and keep the eleven.
  * THAT DOUBLE-CLICKING UPLOAD UPLOADS ONCE. The whole point of a busy
    state is the second press that never happens, so the check counts
    the requests a real double click produced.
  * THAT WITH JAVASCRIPT OFF THE PLAIN FORM STILL WORKS, unchanged: one
    multipart request with every file in it, and the photographs on the
    page afterwards. This is the half that must never be broken by the
    half above it, and the only way to be sure is to switch JavaScript
    off in a real browser and use the form.

Two real screens, per tests/browser_view.py: 1440x900 and 390x740. The
panel grows down the page, so the phone HEIGHT is the case that matters
— a progress bar below the fold is a progress bar nobody is watching.

Run:  python tests/check_upload_progress.py [--shots DIR]
"""
import atexit
import io
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEST_DB = os.path.join(HERE, "test_upload_progress.db")
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _s):
        os.remove(TEST_DB + _s)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from PIL import Image                                     # noqa: E402
from werkzeug.serving import make_server                  # noqa: E402
from werkzeug.security import generate_password_hash      # noqa: E402
from playwright.sync_api import sync_playwright           # noqa: E402

from browser_motion import STILL, new_context             # noqa: E402
from browser_view import height_for                       # noqa: E402

import app as appmod                                      # noqa: E402
from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,  # noqa: E402
                 FeatureFlag, GalleryAlbum, GalleryImage, User)

SHOTS = (sys.argv[sys.argv.index("--shots") + 1]
         if "--shots" in sys.argv else None)
if SHOTS:
    os.makedirs(SHOTS, exist_ok=True)

PORT = 5187
BASE = "http://127.0.0.1:%d" % PORT
PW = "upload-progress-password"
WIDTHS = [1440, 390]

# THE REAL uploads folder, unlike the smoke tests, and deliberately: a
# browser check fetches the thumbnails, and Flask serves /static/uploads
# from static/uploads whatever `save_upload()` has been pointed at. With
# a sandbox every photograph on the page 404s, `naturalWidth` is 0 for
# all of them, and a check asking about their SHAPE quietly measures
# nothing. So the files land where they really land and everything this
# run created is swept up at the end — the same bargain
# check_admin_widths.py makes with its one fixture image.
UPLOAD_DIR = appmod.UPLOAD_DIR
BEFORE = set(os.listdir(UPLOAD_DIR)) if os.path.isdir(UPLOAD_DIR) else set()
FILES = tempfile.mkdtemp(prefix="ebwa-progress-files-")


def sweep_uploads():
    """Remove every file this run put in static/uploads, and only those."""
    if not os.path.isdir(UPLOAD_DIR):
        return 0
    gone = 0
    for name in os.listdir(UPLOAD_DIR):
        if name in BEFORE:
            continue
        try:
            os.remove(os.path.join(UPLOAD_DIR, name))
            gone += 1
        except OSError:
            pass
    return gone


# REGISTERED NOW, not only run at the end. A run that raises part way —
# or is killed by a timeout while it is uploading — otherwise leaves
# several hundred photographs in a folder this check does not own. That
# happened once, deliberately, while proving this file can fail: 286
# files, and nothing to tell them from real uploads but their mtime.
atexit.register(sweep_uploads)

# The advice this file asserts on contains "→", and a Windows console is
# cp1252 by default — printing a failure detail would then die with a
# UnicodeEncodeError and hide the failure it was reporting.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):        # not a real stream, or too old
    pass

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        ("\n        %s" % detail) if detail and not cond
                        else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------- fixtures
# TWELVE MIXED SIZES, one of them portrait, one of them not a photograph
# at all. The sizes vary so the requests take visibly different lengths
# of time — a bar that only ever advances in equal steps has not been
# watched doing anything.
def photo(path, width, height):
    im = Image.new("RGB", (width, height))
    px = im.load()
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            px[x, y] = ((x * 5) % 256, (y * 11) % 256, ((x + y) * 7) % 256)
    im.save(path, "JPEG", quality=85)


SIZES = [(1600, 1067), (2400, 1600), (1200, 800), (800, 1200),   # portrait
         (1024, 768), (1920, 1280), (900, 600), (1400, 933),
         (640, 480), (2000, 1333), (1100, 733)]
CHOSEN = []
for i, (w, h) in enumerate(SIZES):
    name = "photo-%02d-%dx%d.jpg" % (i + 1, w, h)
    photo(os.path.join(FILES, name), w, h)
    CHOSEN.append(os.path.join(FILES, name))
PORTRAIT_NAME = "photo-04-800x1200.jpg"

# The deliberately invalid one. An extension the site does not take, on
# bytes that are not an image either — the shape of a real complaint
# ("my phone gave me a .heic and the website said nothing").
BAD_NAME = "beach.heic"
BAD = os.path.join(FILES, BAD_NAME)
with open(BAD, "wb") as fh:
    fh.write(b"not an image at all" * 200)
# FIFTH OF TWELVE, not last. At the end its failure is reported and then
# navigated away from in the same breath, so nothing can be seen — and
# the claim that matters, that the SEVEN AFTER IT still go up, is never
# put to the test at all.
BAD_AT = 4
CHOSEN.insert(BAD_AT, BAD)
assert len(CHOSEN) == 12 and CHOSEN[BAD_AT] == BAD

# ONE PHOTOGRAPH FAR OVER THE CAP — about 20MB, which is an ordinary
# frame off an ordinary camera and the commonest way this form is going
# to be misused. Blocky noise rather than a gradient, because JPEG
# compresses a gradient down to nothing and the point here is the SIZE.
# Built by resizing a small random image with NEAREST, which is instant;
# generating 20 megapixels a pixel at a time is not.
def oversized(path, width, height, block=6):
    small = Image.frombytes("RGB", (width // block, height // block),
                            os.urandom((width // block) * (height // block) * 3))
    small.resize((width, height), Image.NEAREST).save(path, "JPEG", quality=96)


BIG_NAME = "enormous-20mb.jpg"
BIG = os.path.join(FILES, BIG_NAME)
oversized(BIG, 5200, 3900)
BIG_MB = os.path.getsize(BIG) / (1024.0 * 1024.0)

# Twelve again, with the oversized one FIFTH: the eleven either side of
# it have to go up regardless, which is the whole claim.
BIG_AT = 4
WITH_BIG = [f for f in CHOSEN if f != BAD]
WITH_BIG.insert(BIG_AT, BIG)
WITH_BIG = WITH_BIG[:12]
assert len(WITH_BIG) == 12 and WITH_BIG[BIG_AT] == BIG

# The plain form posts everything in ONE request, and MAX_CONTENT_LENGTH
# is 8MB for the whole of it. So the no-JS run uses a batch that fits —
# what is being proved there is that the plain path still works, and the
# cap is proved separately, below, as its own fact.
SMALL_BATCH = sorted([f for f in CHOSEN if f != BAD],
                     key=os.path.getsize)[:4]
assert sum(os.path.getsize(f) for f in SMALL_BATCH) < 6 * 1024 * 1024

with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        db.session.add(Block(group=group, key=key, label=label, kind=kind,
                             value=value))
    for n, _l, _d, default in FEATURES:
        db.session.add(FeatureFlag(name=n, enabled=default))
    db.session.add(User(email="progress@example.com",
                        password_hash=generate_password_hash(PW),
                        role="super_admin"))
    db.session.add(GalleryAlbum(title="Seaside trip", slug="seaside-trip",
                                published=True))
    db.session.commit()


def photo_count():
    with app.app_context():
        return GalleryImage.query.count()


def clear_photos():
    with app.app_context():
        for img in GalleryImage.query.all():
            db.session.delete(img)
        db.session.commit()


server = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)


class Uploads(object):
    """Every POST to the upload route, with when it began and ended.

    Overlap is the question: if any request started before the one
    before it finished, the run was parallel and this file has failed
    to keep its own promise.
    """

    def __init__(self, page):
        self.spans = {}
        self.order = []
        page.on("request", self._begin)
        page.on("requestfinished", self._end)
        page.on("requestfailed", self._end)

    def _begin(self, request):
        if request.method == "POST" and "/admin/gallery" in request.url:
            self.spans[request] = [time.time(), None]
            self.order.append(request)

    def _end(self, request):
        if request in self.spans:
            self.spans[request][1] = time.time()

    def count(self):
        return len(self.order)

    def overlaps(self):
        done = [self.spans[r] for r in self.order]
        bad = []
        for i in range(1, len(done)):
            began = done[i][0]
            ended = done[i - 1][1]
            if ended is None or began < ended - 0.02:
                bad.append("request %d began before request %d finished"
                           % (i + 1, i))
        return bad


# ONE REAL LOGIN, THEN THE COOKIE. The `login` scope allows five
# attempts in ten minutes and this file needs eight contexts — four in
# the runs above and four more for the oversized ones — so signing in
# each time trips the rate limiter part way through. A rate-limited
# context lands on the LOGIN page, where there is no upload form at all,
# and every assertion after it is measuring the wrong page. This is the
# same trap check_admin_widths.py documents; it needs a different answer
# here, because these contexts differ in whether JavaScript is on and so
# cannot be one context resized.
_session_cookie = []


def sign_in(ctx):
    page = ctx.new_page()
    if _session_cookie:
        ctx.add_cookies(_session_cookie)
        page.goto(BASE + "/admin", wait_until="load")
        return page
    page.goto(BASE + "/admin/login", wait_until="load")
    page.fill("input[name=email]", "progress@example.com")
    page.fill("input[name=password]", PW)
    page.click("button[type=submit]")
    page.wait_for_load_state("load")
    _session_cookie.extend(c for c in ctx.cookies()
                           if c["name"] == "session")
    return page


with sync_playwright() as pw:
    browser = pw.chromium.launch()

    for width in WIDTHS:
        height = height_for(width)
        print()
        print("---- %dx%d, JavaScript on" % (width, height))
        clear_photos()
        ctx = new_context(browser, width, height, motion=STILL)
        page = sign_in(ctx)
        check("%d: signed in" % width,
              page.locator(".admin-side").count() == 1, page.url)

        page.goto(BASE + "/admin/gallery", wait_until="load")
        watch = Uploads(page)
        page.set_input_files("#images", CHOSEN)
        page.select_option("#album_id", label="Seaside trip")
        page.fill("#caption", "Southend, July")

        # The panel is not on the page until there is something to say.
        check("%d: no progress panel before the upload starts" % width,
              page.locator("#uploadProgress").is_hidden())

        # A MARKER ON THE DOCUMENT, because `page.url` cannot answer
        # "have we been redirected yet?" here: it is /admin/gallery
        # before the upload as well as after it. A value on `window` is
        # gone the instant a new page replaces this one, which is the
        # question actually being asked.
        page.evaluate("() => { window.__uploadPage = 1; }")

        # DOUBLE CLICK, which is the thing the busy state exists for.
        # Two presses in quick succession must still be one upload.
        page.click("#uploadButton", click_count=2, delay=40)

        page.wait_for_selector("#uploadProgress:not(.is-hidden)",
                               timeout=10000)
        # EVERYTHING THE PANEL SAYS HAS TO BE READ WHILE IT IS SAYING
        # IT. The run ends by navigating to the gallery, so a locator
        # consulted after the loop is reading the NEXT page — which is
        # how the failed-file list first came back empty on a run that
        # had displayed it perfectly well.
        seen, widths, failed_text, shot = [], [], "", False
        for _ in range(600):
            # The page this is watching ENDS BY NAVIGATING, and a
            # navigation lands in the middle of an evaluate as an
            # "execution context was destroyed" error rather than a
            # value. That is the run finishing, not the check breaking —
            # a crash here would report a broken upload as a broken
            # test, which is exactly what happened when the sequencing
            # was deliberately broken to prove this file can fail.
            try:
                here = page.evaluate(
                    "() => ({on: !!window.__uploadPage,"
                    " text: (document.getElementById('uploadStatus')"
                    "        ||{}).textContent || '',"
                    " bar: ((document.querySelector('.upload-bar i')"
                    "        ||{}).style||{}).width || '',"
                    " fails: (document.getElementById('uploadFails')"
                    "        ||{}).textContent || ''})")
            except Exception:
                break
            if not here["on"]:
                break                      # the redirect has happened
            text = here["text"].strip()
            if text and text not in seen:
                seen.append(text)
            if here["bar"]:
                widths.append(here["bar"])
            if here["fails"].strip():
                failed_text = here["fails"]
                if SHOTS and not shot:
                    shot = True
                    page.screenshot(path=os.path.join(
                        SHOTS, "upload-%d.png" % width))
            page.wait_for_timeout(40)

        counted = [t for t in seen if t.startswith("Uploading photo ")]
        check("%d: the bar counts photographs, not bytes" % width,
              any("of 12" in t for t in counted), str(seen[:4]))
        check("%d: it reached the later files" % width,
              len(counted) >= 4,
              "%d distinct messages: %s" % (len(counted), counted[:6]))
        # A DETERMINATE bar: it has to have been somewhere in the middle,
        # not gone straight from nothing to done.
        middles = [w for w in widths
                   if w.endswith("%") and 5 < float(w[:-1]) < 95]
        check("%d: the bar advanced through the middle" % width,
              len(set(middles)) >= 3,
              "widths seen: %s" % sorted(set(widths))[:8])
        check("%d: the failing file is named on screen, during the run"
              % width,
              BAD_NAME in failed_text, repr(failed_text))
        # beach.heic is what an iPhone hands somebody, so this is the
        # end-to-end proof that the HEIC advice — not a list of
        # extensions — is what appears on the bar.
        check("%d: and it is named with a reason beside it" % width,
              "HEIC format" in failed_text, repr(failed_text))
        check("%d: the reason says an iPhone chose the format" % width,
              "iPhones take" in failed_text, repr(failed_text))
        check("%d: and gives both ways out of it" % width,
              "WhatsApp" in failed_text and "Most Compatible" in failed_text,
              repr(failed_text))
        # THE RUN CARRIED ON PAST IT. The bad file is fifth of twelve, so
        # a run that stopped there would never have reached these.
        check("%d: the photographs after the bad one still went up" % width,
              any("photo 1" in t and "of 12" in t for t in counted),
              "last message seen: %r" % (counted[-1:] or [""])[0])

        page.wait_for_function("() => window.__uploadPage === undefined",
                               timeout=60000)
        page.wait_for_load_state("load")
        page.wait_for_selector(".flash", timeout=15000)

        # ---- what actually happened on the server
        check("%d: one request per file, and only one run" % width,
              watch.count() == 12,
              "%d POSTs for 12 files (a double click would double it)"
              % watch.count())
        clashes = watch.overlaps()
        check("%d: the requests were sequential, never parallel" % width,
              not clashes,
              "%d overlapping: %s%s" % (len(clashes), "; ".join(clashes[:3]),
                                        " ..." if len(clashes) > 3 else ""))
        check("%d: eleven photographs stored, the twelfth refused" % width,
              photo_count() == 11, str(photo_count()))

        # ---- the summary, composed by the server as the page was drawn
        flash = page.locator(".flash").first.inner_text()
        check("%d: the flash counts what was added" % width,
              "11 photos added" in flash, flash)
        check("%d: the flash counts and names the failure" % width,
              "1 failed" in flash and BAD_NAME in flash, flash)
        check("%d: the flash says why, in words" % width,
              "HEIC format" in flash and "Most Compatible" in flash, flash)
        check("%d: it landed on the album it uploaded into" % width,
              "album=" in page.url, page.url)

        # ---- the portrait photograph is still portrait
        # A THUMBNAIL THAT HAS NOT LOADED HAS naturalWidth 0, and every
        # comparison against it is false — so the whole grid read as
        # "no portrait photographs" on a page that had one. Wait for the
        # bytes before asking about the shape.
        page.wait_for_function(
            """() => [...document.querySelectorAll('.admin-gallery-item img')]
                   .every(i => i.complete && i.naturalWidth > 0)""",
            timeout=15000)
        shapes = page.evaluate(
            """() => [...document.querySelectorAll('.admin-gallery-item img')]
                   .map(i => i.naturalWidth + 'x' + i.naturalHeight)""")
        portraits = [s for s in shapes
                     if int(s.split('x')[1]) > int(s.split('x')[0])]
        check("%d: the portrait photograph stayed portrait" % width,
              len(portraits) == 1,
              "%d portrait of %d: %s" % (len(portraits), len(shapes),
                                         sorted(set(shapes))))

        # ---- and the page still does not scroll sideways with it all on
        over = page.evaluate(
            "() => document.documentElement.scrollWidth"
            " - document.documentElement.clientWidth")
        check("%d: no sideways scroll after the upload" % width, over <= 0,
              "%dpx past the viewport" % over)
        ctx.close()

    # ------------------------------------------------ JavaScript switched off
    for width in WIDTHS:
        height = height_for(width)
        print()
        print("---- %dx%d, JavaScript OFF" % (width, height))
        clear_photos()
        ctx = new_context(browser, width, height, motion=STILL,
                          java_script_enabled=False)
        page = sign_in(ctx)
        check("%d no-JS: signed in with the plain form" % width,
              page.locator(".admin-side").count() == 1, page.url)

        page.goto(BASE + "/admin/gallery", wait_until="load")
        watch = Uploads(page)
        page.set_input_files("#images", SMALL_BATCH)
        page.select_option("#album_id", label="Seaside trip")
        check("%d no-JS: the progress panel is never shown" % width,
              page.locator("#uploadProgress").is_hidden())
        page.click("#uploadButton")
        page.wait_for_load_state("load")

        check("%d no-JS: ONE multipart request carried them all" % width,
              watch.count() == 1, "%d POSTs" % watch.count())
        check("%d no-JS: every photograph in the batch was stored" % width,
              photo_count() == len(SMALL_BATCH), str(photo_count()))
        flash = page.locator(".flash").first.inner_text()
        check("%d no-JS: the plain count is flashed" % width,
              "%d image(s) uploaded." % len(SMALL_BATCH) in flash, flash)

        # AND THE CAP THE PER-FILE PATH ROUTES AROUND. MAX_CONTENT_LENGTH
        # is 8MB PER REQUEST, so the same twelve photographs the script
        # puts up one at a time are refused outright as one batch. The
        # cap has not moved; what changed is that meeting it is now a
        # sentence on the form rather than a bare 413 page, so that is
        # what is pinned here.
        clear_photos()
        page.goto(BASE + "/admin/gallery", wait_until="load")
        page.set_input_files("#images", CHOSEN)
        page.click("#uploadButton")
        page.wait_for_load_state("load")
        body = page.locator("body").inner_text()
        check("%d no-JS: twelve at once is still over the 8MB request cap"
              % width, photo_count() == 0, str(photo_count()))
        check("%d no-JS: and it is explained rather than shown as a 413"
              % width,
              "That file is too large" in body
              and "Request Entity Too Large" not in body, body[:160])
        check("%d no-JS: the sentence says the batch is what counts" % width,
              "whole upload" in body, body[:200])
        ctx.close()

    # ------------------------------------------- an upload that is too big
    # A BARE 413 IS THE WORST PAGE ON THE SITE and it is the one shown
    # for the commonest mistake: no heading, no navigation, nothing to
    # do next, and the Back button losing whatever else was typed. Both
    # paths have to explain themselves instead — the form with a flash
    # on the page it came from, the script with a named line on the bar.
    for width in WIDTHS:
        height = height_for(width)
        print()
        print("---- %dx%d, an oversized photograph (%.1fMB)"
              % (width, height, BIG_MB))
        check("%d: the oversized fixture really is oversized" % width,
              18 <= BIG_MB <= 26, "%.1fMB" % BIG_MB)

        # ---- through the script: photo 5 of 12, and the other eleven
        # must not pay for it.
        clear_photos()
        ctx = new_context(browser, width, height, motion=STILL)
        page = sign_in(ctx)
        page.goto(BASE + "/admin/gallery", wait_until="load")
        watch = Uploads(page)
        page.set_input_files("#images", WITH_BIG)
        page.evaluate("() => { window.__uploadPage = 1; }")
        page.click("#uploadButton")

        page.wait_for_selector("#uploadProgress:not(.is-hidden)",
                               timeout=10000)
        failed_text = ""
        for _ in range(900):
            try:
                here = page.evaluate(
                    "() => ({on: !!window.__uploadPage,"
                    " fails: (document.getElementById('uploadFails')"
                    "        ||{}).textContent || ''})")
            except Exception:
                break
            if not here["on"]:
                break
            if here["fails"].strip():
                failed_text = here["fails"]
            page.wait_for_timeout(40)

        check("%d: the oversized photo is named on the bar" % width,
              BIG_NAME in failed_text, repr(failed_text))
        check("%d: and told it was too large, not 'an answer we could "
              "not read'" % width,
              "too large" in failed_text, repr(failed_text))
        check("%d: the limit is in the sentence" % width,
              "MB" in failed_text, repr(failed_text))

        page.wait_for_function("() => window.__uploadPage === undefined",
                               timeout=60000)
        page.wait_for_load_state("load")
        page.wait_for_selector(".flash", timeout=15000)

        check("%d: still one request per file" % width, watch.count() == 12,
              "%d POSTs" % watch.count())
        clashes = watch.overlaps()
        check("%d: still sequential" % width, not clashes,
              "%d overlapping" % len(clashes))
        check("%d: photos 1-4 and 6-12 all stored" % width,
              photo_count() == 11, str(photo_count()))
        flash = page.locator(".flash").first.inner_text()
        check("%d: the summary counts eleven and one" % width,
              "11 photos added" in flash and "1 failed" in flash, flash)
        check("%d: and names the oversized one" % width,
              BIG_NAME in flash and "too large" in flash, flash)
        ctx.close()

        # ---- and through the plain form, with JavaScript switched off,
        # where the bare 413 page is what used to appear.
        clear_photos()
        ctx = new_context(browser, width, height, motion=STILL,
                          java_script_enabled=False)
        page = sign_in(ctx)
        page.goto(BASE + "/admin/gallery", wait_until="load")
        page.set_input_files("#images", [BIG])
        page.click("#uploadButton")
        page.wait_for_load_state("load")

        # THE TEST THAT MATTERS IS THAT THIS IS STILL THE WEBSITE. A 413
        # page has no sidebar, no heading and nowhere to go; asserting
        # only on the message would pass on a page carrying the message
        # and nothing else.
        check("%d no-JS: it is still the admin, not an error page" % width,
              page.locator(".admin-side").count() == 1
              and page.locator(".admin-h1").count() >= 1, page.url)
        body = page.locator("body").inner_text()
        check("%d no-JS: no bare 413 text anywhere on it" % width,
              "Request Entity Too Large" not in body, body[:120])
        check("%d no-JS: the reason is on the page in words" % width,
              "That file is too large" in body,
              page.locator(".flash").first.inner_text()
              if page.locator(".flash").count() else "(no flash at all)")
        check("%d no-JS: it names the limit" % width,
              "MB" in page.locator(".flash").first.inner_text(),
              page.locator(".flash").first.inner_text())
        check("%d no-JS: nothing was stored" % width, photo_count() == 0,
              str(photo_count()))
        # And it landed back on the form, not somewhere else.
        check("%d no-JS: back on the gallery form" % width,
              page.locator("#uploadForm").count() == 1, page.url)
        if SHOTS:
            page.screenshot(path=os.path.join(SHOTS,
                                              "toolarge-%d.png" % width))
        ctx.close()

    # --------------------------------------------------- the shared busy state
    print()
    print("---- busy.js")
    ctx = new_context(browser, 1440, 900, motion=STILL)
    page = ctx.new_page()

    # A public form, so this is not only an admin feature.
    page.goto(BASE + "/contact", wait_until="load")
    button = page.locator(".contact-form button[type=submit]")
    check("busy: the button starts as itself",
          button.inner_text().strip() == "Send message",
          button.inner_text())
    check("busy: it is a live region before anything changes",
          button.get_attribute("aria-live") == "polite",
          str(button.get_attribute("aria-live")))
    page.fill("input[name=name]", "A Visitor")
    page.fill("input[name=email]", "visitor@example.org")
    page.fill("input[name=subject]", "A question")
    page.fill("textarea[name=message]", "When does the weekend school start?")
    # Read the state DURING the submit, before the navigation replaces
    # the page: the submit handler runs first, so this fires after it.
    # WATCHED FROM `window`, not from the form. busy.js listens on the
    # document, so a listener on the form runs first and reads the button
    # exactly as it was — which is a check that can only ever fail. The
    # window is the last stop in the bubble path.
    state = page.evaluate("""() => {
        const form = document.querySelector('.contact-form');
        const btn = form.querySelector('button[type=submit]');
        let seen = null;
        window.addEventListener('submit', function () {
            seen = {text: btn.textContent.trim(),
                    busy: btn.getAttribute('aria-busy'),
                    spinner: !!btn.querySelector('.busy-spin'),
                    cls: btn.className};
        });
        form.requestSubmit(btn);
        return seen;
    }""")
    check("busy: the label becomes the message",
          state and state["text"].startswith("Sending your message"),
          str(state))
    check("busy: aria-busy is set", state and state["busy"] == "true",
          str(state))
    check("busy: a spinner is added", state and state["spinner"], str(state))
    check("busy: the control is marked busy for the stylesheet",
          state and "is-busy" in state["cls"], str(state))
    page.wait_for_load_state("load")

    # A cancelled confirm() must leave the button alone — several admin
    # forms are guarded that way and answering No has to mean nothing.
    page.goto(BASE + "/admin/login", wait_until="load")
    page.fill("input[name=email]", "progress@example.com")
    page.fill("input[name=password]", PW)
    page.click("button[type=submit]")
    page.wait_for_load_state("load")
    page.goto(BASE + "/admin/features", wait_until="load")
    page.on("dialog", lambda d: d.dismiss())
    test_mail = page.locator("form[action$='/test-mail'] button[type=submit]")
    if test_mail.count():
        before = test_mail.inner_text().strip()
        test_mail.click()
        page.wait_for_timeout(300)
        check("busy: answering No to a confirm leaves the button alone",
              test_mail.inner_text().strip() == before
              and test_mail.get_attribute("aria-busy") is None,
              "%r -> %r" % (before, test_mail.inner_text().strip()))
    else:
        check("busy: the test-mail form is on the settings page", False,
              "not found")

    # And a download link, which never navigates, comes back on its own.
    page.goto(BASE + "/admin/subscribers", wait_until="load")
    link = page.locator("a[data-busy]").first
    if link.count():
        was = link.inner_text().strip()
        restore = int(link.get_attribute("data-busy-restore"))
        check("busy: a download link says how long to stay busy",
              restore > 0, str(restore))
        # The download itself is not what is under test, so it is
        # cancelled — but FROM `window`, after busy.js has seen the
        # click. Cancelling it on the link, in the capture phase, sets
        # defaultPrevented first and busy.js then correctly does nothing,
        # which looks exactly like the feature being broken.
        page.evaluate("""() => {
            window.addEventListener('click', function (e) {
                e.preventDefault();
            });
        }""")
        link.click()
        page.wait_for_timeout(120)
        check("busy: the download link goes busy",
              link.get_attribute("aria-busy") == "true",
              link.inner_text())
        page.wait_for_timeout(restore + 400)
        check("busy: and puts itself back, since nothing navigated",
              link.inner_text().strip() == was
              and link.get_attribute("aria-busy") is None,
              "%r -> %r" % (was, link.inner_text().strip()))
    else:
        check("busy: the subscribers export opts in", False, "no link found")

    ctx.close()
    browser.close()

server.shutdown()
server.server_close()
with app.app_context():
    db.session.remove()
    db.engine.dispose()
swept = sweep_uploads()
shutil.rmtree(FILES, ignore_errors=True)
print()
print("swept %d file(s) out of static/uploads" % swept)
left = (set(os.listdir(UPLOAD_DIR)) - BEFORE) if os.path.isdir(UPLOAD_DIR) \
    else set()
check("static/uploads is as it was found", not left, str(sorted(left)[:5]))
for suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + suffix):
        os.remove(TEST_DB + suffix)

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
