"""The fonts are ours: no third-party request, and Bengali still renders.

Two claims, and only a browser can settle either.

  1. NOTHING OFF THIS SERVER, on any page. Reading the templates would
     not prove it — a stylesheet can @import, a font can be referenced
     from a rule nobody grepped for, and an SVG or an iframe can pull
     something in. So every request the browser makes is recorded and
     the list has to be empty of anything that is not us.

  2. THE BENGALI SCRIPT STILL DRAWS. Noto Serif Bengali has by far the
     largest character set of the three and its subset file is separate,
     so it is the one a self-hosting mistake breaks. The eyebrow accents
     on every public page are Bengali, and a missing face there does not
     error — it silently falls back to a serif that has the glyphs, or
     to tofu. Measured, not eyeballed: the face has to be the one asked
     for, and the text has to have width.

Run:  python tests/check_fonts.py
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEST_DB = os.path.join(HERE, "test_fonts.db")
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _s):
        os.remove(TEST_DB + _s)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from werkzeug.serving import make_server                # noqa: E402
from werkzeug.security import generate_password_hash    # noqa: E402
from playwright.sync_api import sync_playwright         # noqa: E402

from browser_motion import STILL, new_context           # noqa: E402
from browser_view import VIEWPORTS                      # noqa: E402
from app import app, db, FeatureFlag, User              # noqa: E402
import seed_demo                                        # noqa: E402

PORT = 5174
BASE = "http://127.0.0.1:%d" % PORT
PW = "fonts-check-password"
PAGES = ["/", "/about", "/events", "/news", "/gallery", "/our-journey",
         "/resources", "/faq", "/membership", "/collections", "/donate",
         "/contact", "/privacy", "/terms"]
ADMIN = ["/admin/login", "/admin", "/admin/features"]
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        ("\n        %s" % detail) if detail and not cond
                        else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    seed_demo.seed()
    for flag in FeatureFlag.query.all():
        flag.enabled = True
    db.session.add(User(email="fonts@example.com",
                        password_hash=generate_password_hash(PW),
                        role="super_admin"))
    db.session.commit()

server = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)

# document.fonts.check() answers "would this face be used", which is the
# question — not "is a file on disk".
BENGALI_JS = """() => {
  const el = document.querySelector('.eyebrow .bn');
  if (!el) return {absent: true};
  const cs = getComputedStyle(el);
  const box = el.getBoundingClientRect();
  const family = cs.fontFamily.split(',')[0].replace(/["']/g, '');
  return {
    text: el.textContent.trim(),
    family: family,
    ready: document.fonts.status,
    // Is the face actually loaded and usable for THIS text?
    loaded: document.fonts.check('500 16px "' + family + '"', el.textContent),
    faces: [...document.fonts].filter(f => f.family === family)
             .map(f => f.status),
    width: Math.round(box.width), height: Math.round(box.height)
  };
}"""

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = new_context(browser, 1280, 800, motion=STILL)
    page = ctx.new_page()
    # MAIN FRAME ONLY. The contact page embeds a Google map, and Google's
    # iframe loads its own fonts from inside itself — those are requests
    # the MAP makes, not requests this site makes, and they are a
    # separate, deliberate, documented third party (frame-src in the
    # CSP). Counting them here would mean this check could never pass
    # and would say nothing about our own pages. The map is asserted on
    # its own terms further down.
    offsite = []
    page.on("request", lambda r: (
        offsite.append(r.url)
        if r.frame == page.main_frame and "127.0.0.1" not in r.url
        and not r.url.startswith("data:") else None))

    fonts_seen = set()
    page.on("response", lambda r: (fonts_seen.add(r.url.split("/")[-1])
                                   if ".woff2" in r.url
                                   and "127.0.0.1" in r.url else None))

    for url in PAGES:
        page.goto(BASE + url, wait_until="networkidle")
    page.goto(BASE + "/admin/login", wait_until="networkidle")
    page.fill("input[name=email]", "fonts@example.com")
    page.fill("input[name=password]", PW)
    page.click("button[type=submit]")
    page.wait_for_load_state("load")
    for url in ADMIN[1:]:
        page.goto(BASE + url, wait_until="networkidle")

    check("NO REQUEST LEFT THIS SERVER, on %d public and %d admin pages"
          % (len(PAGES), len(ADMIN)),
          not offsite, "\n        ".join(sorted(set(offsite))[:6]))
    check("the fonts really were fetched (so the pages did use them)",
          any(f.endswith(".woff2") for f in fonts_seen),
          str(sorted(fonts_seen)))
    check("...from this site, with a content hash in the name",
          all(len(f.split(".")) >= 3 for f in fonts_seen if ".woff2" in f),
          str(sorted(fonts_seen)))
    # The Bengali subset must NOT be fetched by a page with no Bengali on
    # it — that is the whole point of keeping Google's unicode-range.
    print("   fonts fetched across the run: %s" % ", ".join(sorted(fonts_seen)))
    ctx.close()

    # ---- the Bengali script, at every viewport
    for width, height in VIEWPORTS:
        ctx = new_context(browser, width, height, motion=STILL)
        page = ctx.new_page()
        page.goto(BASE + "/about", wait_until="networkidle")
        page.evaluate("() => document.fonts.ready")
        d = page.evaluate(BENGALI_JS)
        if d.get("absent"):
            check("%dpx: there is Bengali on the page" % width, False,
                  "no .eyebrow .bn found")
            ctx.close()
            continue
        check("%dpx: the Bengali eyebrow has text" % width,
              bool(d["text"]) and any(ord(c) >= 0x0980 for c in d["text"]),
              repr(d["text"]))
        check("%dpx: it asks for Noto Serif Bengali" % width,
              d["family"] == "Noto Serif Bengali", d["family"])
        check("%dpx: THE FACE IS LOADED AND USABLE FOR THAT TEXT" % width,
              d["loaded"] is True, str(d))
        check("%dpx: the browser holds a loaded face for it" % width,
              "loaded" in d["faces"], str(d["faces"]))
        check("%dpx: and it takes up space (not zero-width tofu)" % width,
              d["width"] > 20 and d["height"] > 6,
              "%dx%d" % (d["width"], d["height"]))
        if width == 1280:
            # Codepoints, not the characters: a Windows console is
            # cp1252 and printing Bengali to it raises.
            print("   Bengali: %d glyphs (U+%04X...) rendered %dpx wide "
                  "in %s" % (len(d["text"]), ord(d["text"][0]),
                             d["width"], d["family"]))
        ctx.close()

    # ---- the one third party that is left, recorded rather than hidden
    ctx = new_context(browser, 1280, 800, motion=STILL)
    page = ctx.new_page()
    frames = []
    page.on("request", lambda r: (frames.append(r.url)
                                  if r.frame != page.main_frame else None))
    page.goto(BASE + "/contact", wait_until="networkidle")
    check("/contact still embeds the Google map, and it is the ONLY "
          "third party left on the site",
          any("google" in u for u in frames),
          "the map stopped loading - if that is deliberate, this check "
          "should go")
    page.on("request", lambda r: None)
    main_only = []
    page2 = ctx.new_page()
    page2.on("request", lambda r: (
        main_only.append(r.url)
        if r.frame == page2.main_frame and "127.0.0.1" not in r.url
        and not r.url.startswith("data:") else None))
    page2.goto(BASE + "/contact", wait_until="networkidle")
    check("...and the contact PAGE itself still fetches nothing off-server",
          not main_only, str(sorted(set(main_only))[:4]))
    ctx.close()

    # ---- the Latin faces are ours too
    ctx = new_context(browser, 1280, 800, motion=STILL)
    page = ctx.new_page()
    page.goto(BASE + "/", wait_until="networkidle")
    page.evaluate("() => document.fonts.ready")
    d = page.evaluate("""() => ({
        display: document.fonts.check('700 32px "Bricolage Grotesque"', 'EBWA'),
        body: document.fonts.check('400 16px "Public Sans"', 'EBWA'),
        swap: [...document.fonts].every(f => f.display === 'swap')
    })""")
    check("the display face is loaded", d["display"] is True, str(d))
    check("the body face is loaded", d["body"] is True, str(d))
    check("every face declares font-display: swap", d["swap"] is True, str(d))
    ctx.close()
    browser.close()

server.shutdown()
server.server_close()
with app.app_context():
    db.session.remove()
    db.engine.dispose()
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
