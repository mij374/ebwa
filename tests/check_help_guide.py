"""The admin guide has to survive being printed, and being read aloud.

It is a document that people will save as a PDF and hand round, so the
things that go wrong with it are not the things that go wrong with an
admin screen:

  * A SCREENSHOT WIDER THAN THE PAPER. The images are captured at up to
    916px; A4's printable width is about 17cm. A percentage width looks
    fine on screen and runs off the page in print, and nobody finds out
    until it is printed. Measured here under print emulation, in
    centimetres.
  * Alt text that says "screenshot" and nothing else — useless read
    aloud, and useless in black and white where a colour cannot be the
    distinguishing feature.
  * A table of contents whose links do not go anywhere, which is the
    usual way an in-page contents list rots.

Run:  python tests/check_help_guide.py [--shots DIR]
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEST_DB = os.path.join(HERE, "test_help_guide.db")
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _s):
        os.remove(TEST_DB + _s)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from werkzeug.serving import make_server              # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402
from playwright.sync_api import sync_playwright       # noqa: E402

from browser_motion import STILL, new_context         # noqa: E402
from browser_view import VIEWPORTS                    # noqa: E402

from app import app, db, User                         # noqa: E402
import seed_demo                                      # noqa: E402

SHOTS = (sys.argv[sys.argv.index("--shots") + 1]
         if "--shots" in sys.argv else None)
if SHOTS:
    os.makedirs(SHOTS, exist_ok=True)

PORT = 5197
BASE = "http://127.0.0.1:%d" % PORT
PW = "help-guide-password"
# A4 less a 2cm margin each side, in CSS centimetres.
A4_PRINTABLE_CM = 17.0

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        ("\n        %s" % detail) if detail and not cond
                        else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    seed_demo.seed()
    db.session.add(User(email="reader@example.com", role="admin",
                        password_hash=generate_password_hash(PW)))
    db.session.commit()

server = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)

IMAGES = """() => Array.from(document.querySelectorAll('.guide-shot img'))
  .map(img => ({
    src: img.getAttribute('src').split('/').pop(),
    alt: img.getAttribute('alt') || '',
    loaded: img.complete && img.naturalWidth > 0,
    natural: img.naturalWidth,
    shownPx: Math.round(img.getBoundingClientRect().width),
    caption: (img.parentElement.querySelector('figcaption')
              || {}).textContent || ''
  }))"""

# 1cm is 37.7952755906 CSS pixels, by definition.
PX_PER_CM = 37.7952755906

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = new_context(browser, 1280, 800, STILL)
    page = ctx.new_page()
    page.goto(BASE + "/admin/login", wait_until="load")
    page.fill("input[name=email]", "reader@example.com")
    page.fill("input[name=password]", PW)
    page.click("button[type=submit]")
    page.wait_for_load_state("load")

    page.goto(BASE + "/admin/help", wait_until="load")
    check("an ordinary admin can open the guide",
          page.locator(".guide-toc").count() == 1, page.url)
    check("...and it is reachable from the sidebar",
          page.locator(".admin-side a[href='/admin/help']").count() == 1,
          "no Help link in the sidebar")

    # ---- THE PICTURES EXIST AND SAY SOMETHING
    page.wait_for_timeout(600)
    page.evaluate("""() => new Promise(done => {
      const imgs = Array.from(document.querySelectorAll('.guide-shot img'));
      imgs.forEach(i => { i.loading = 'eager'; });
      let left = imgs.filter(i => !i.complete).length;
      if (!left) return done();
      imgs.forEach(i => i.complete || i.addEventListener('load',
        () => { if (--left <= 0) done(); }));
      setTimeout(done, 4000);
    })""")
    images = page.evaluate(IMAGES)
    check("the guide carries screenshots", len(images) >= 15,
          "%d image(s)" % len(images))

    missing = [i["src"] for i in images if not i["loaded"]]
    check("EVERY SCREENSHOT ACTUALLY LOADS", not missing,
          "missing files: %s" % ", ".join(missing))

    # Alt text is what a screen reader reads and what a printed copy
    # falls back to. "Screenshot of the events page" is neither.
    weak = [i["src"] for i in images
            if len(i["alt"]) < 40
            or i["alt"].lower().startswith(("screenshot", "image", "a screen"
                                            "shot"))]
    check("...WITH ALT TEXT DESCRIBING WHAT IS ON THE SCREEN",
          not weak, "thin or generic alt text: %s" % ", ".join(weak))
    check("...and every one has a caption saying what to look at",
          all(len(i["caption"].strip()) > 25 for i in images),
          str([i["src"] for i in images
               if len(i["caption"].strip()) <= 25]))
    check("...and the alt text is not just the caption again",
          all(i["alt"].strip() != i["caption"].strip() for i in images))

    # ---- THE CONTENTS LIST GOES SOMEWHERE
    links = page.evaluate("""() =>
      Array.from(document.querySelectorAll('.guide-toc a'))
        .map(a => a.getAttribute('href'))""")
    check("the contents list has an entry per section", len(links) >= 12,
          "%d entries" % len(links))
    dead = [h for h in links
            if page.locator(h.replace("#", "#")).count() == 0]
    check("EVERY CONTENTS LINK LANDS ON A SECTION", not dead,
          "nothing with these ids: %s" % ", ".join(dead))
    ids = page.evaluate("""() =>
      Array.from(document.querySelectorAll('.guide-section'))
        .map(s => s.id)""")
    check("...and every section is listed in the contents",
          all(("#" + i) in links for i in ids if i),
          str([i for i in ids if ("#" + i) not in links]))

    # ---- ON SCREEN, AT EVERY SIZE
    for width, height in VIEWPORTS:
        page.set_viewport_size({"width": width, "height": height})
        page.goto(BASE + "/admin/help", wait_until="load")
        over = page.evaluate("""() => {
          const d = document.documentElement;
          return d.scrollWidth - d.clientWidth;
        }""")
        check("%dx%d: the guide does not scroll sideways" % (width, height),
              over <= 1, "%dpx over" % over)
        wide = page.evaluate("""() => {
          const main = document.querySelector('.admin-main');
          const room = main.getBoundingClientRect().width;
          return Array.from(document.querySelectorAll('.guide-shot img'))
            .filter(i => i.getBoundingClientRect().width > room + 1).length;
        }""")
        check("%dx%d: no screenshot is wider than the column"
              % (width, height), wide == 0, "%d too wide" % wide)

    # ---- AND ON PAPER
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(BASE + "/admin/help", wait_until="load")
    page.emulate_media(media="print")
    page.wait_for_timeout(300)
    printed = page.evaluate("""(perCm) => {
      const out = {tooWide: [], widest: 0};
      document.querySelectorAll('.guide-shot img').forEach(img => {
        const cm = img.getBoundingClientRect().width / perCm;
        out.widest = Math.max(out.widest, cm);
        if (cm > %f) out.tooWide.push(
          img.getAttribute('src').split('/').pop() + ' at ' +
          cm.toFixed(1) + 'cm');
      });
      const gone = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return true;
        const r = el.getBoundingClientRect();
        return getComputedStyle(el).display === 'none' || r.height === 0;
      };
      out.sidebarGone = gone('.admin-side');
      out.printButtonGone = gone('.guide-print-hint');
      out.tocThere = !gone('.guide-toc');
      out.sectionsThere = document.querySelectorAll(
        '.guide-section').length;
      return out;
    }""" % A4_PRINTABLE_CM, PX_PER_CM)

    check("PRINTED, NO SCREENSHOT IS WIDER THAN THE PAPER",
          not printed["tooWide"],
          "wider than %.0fcm: %s" % (A4_PRINTABLE_CM,
                                     "; ".join(printed["tooWide"])))
    print("      widest printed screenshot: %.1fcm (paper allows %.0fcm)"
          % (printed["widest"], A4_PRINTABLE_CM))
    check("...the admin sidebar is not printed", printed["sidebarGone"])
    check("...nor the Print button, which cannot be pressed on paper",
          printed["printButtonGone"])
    check("...but the contents list is", printed["tocThere"])
    check("...and every section is still there",
          printed["sectionsThere"] >= 12,
          "%d sections" % printed["sectionsThere"])

    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, "help-print.png"),
                        full_page=True)
    page.emulate_media(media="screen")
    browser.close()

server.shutdown()
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _s):
        try:
            os.remove(TEST_DB + _s)
        except OSError:
            pass

print()
if failures:
    print("%d check(s) failed:" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("The guide reads and prints correctly.")
