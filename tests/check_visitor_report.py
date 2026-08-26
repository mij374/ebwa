"""The period report has to come out of the printer as a DOCUMENT.

It goes into grant applications, so the claim that matters is not what
it looks like on the admin screen — it is what a funder receives after
somebody presses Print and saves a PDF. Nothing in the HTML can answer
that: whether the sidebar is gone, whether the date picker printed
itself, whether a link left "(https://...)" in the middle of a sentence,
whether the figures survived the switch to print colours. All of that
lives in `@media print`, and only a browser asked to emulate print
media will tell you.

So this file opens the page at every shared viewport, switches Chromium
to print media, and asserts:

  * the ADMIN is gone — sidebar, the period picker, flashes, skip link
    and the on-screen note about a missing charity number, which must
    never reach a funder;
  * the DOCUMENT is still there — the association's name, the period,
    both figures, the caveat and the produced-on line;
  * it does not run off the side of the paper, at any width;
  * the figures are printed in ink dark enough to read: the big numbers
    are near-black rather than the green they are on screen.

The on-screen half is covered elsewhere — check_admin_widths.py measures
sideways scroll on the phones and check_accessibility.py audits contrast
and headings. This file is only about the printed artefact.

Run:  python tests/check_visitor_report.py [--shots DIR]
"""
import os
import sys
import threading
import time
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEST_DB = os.path.join(HERE, "test_visitor_report.db")
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

from app import (app, db, User, Block, PageView, PageViewDaily,  # noqa: E402
                 ORG_NAME_KEY, ORG_CHARITY_NO_KEY)
import seed_demo                                      # noqa: E402

SHOTS = (sys.argv[sys.argv.index("--shots") + 1]
         if "--shots" in sys.argv else None)
if SHOTS:
    os.makedirs(SHOTS, exist_ok=True)

PORT = 5187
BASE = "http://127.0.0.1:%d" % PORT
PW = "report-print-password"
ORG = "Enfield Bangladesh Welfare Association"

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        ("\n        %s" % detail) if detail and not cond
                        else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------- fixtures
# A period with real figures in it: an empty report would print a tidy
# page of zeroes and prove nothing about the parts that carry numbers.
START = date(2026, 5, 1)
END = date(2026, 5, 31)
with app.app_context():
    seed_demo.seed()
    db.session.add(User(email="report@example.com", role="super_admin",
                        password_hash=generate_password_hash(PW)))
    for key, value in ((ORG_NAME_KEY, ORG),
                       (ORG_CHARITY_NO_KEY, "1123456")):
        block = Block.query.filter_by(key=key).first()
        if block is not None:
            block.value = value
    for i in range(31):
        db.session.add(PageViewDaily(day=START + timedelta(days=i),
                                     views=40 + i, visitors=11))
        db.session.add(PageViewDaily(day=START - timedelta(days=i + 1),
                                     views=20, visitors=6))
    for i in range(5):
        db.session.add(PageView(day=date.today(), path="/about",
                                visitor="v%d" % i))
    db.session.commit()

server = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)

URL = "%s/admin/visitors/report?from=%s&to=%s" % (BASE, START, END)

# Painted, in print media, is not the same question as present in the
# HTML: the @media print block hides by `display:none`, so this asks the
# browser what it would actually put on the paper.
SHOWN = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return getComputedStyle(el).display !== 'none' && r.width > 0
         && r.height > 0;
}"""

OVERFLOW = """() => {
  const d = document.documentElement;
  const wide = [];
  document.querySelectorAll('body *').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > d.clientWidth + 1)
      wide.push([el.tagName + '.' + (el.className || ''),
                 Math.round(r.right - d.clientWidth)]);
  });
  wide.sort((a, b) => b[1] - a[1]);
  return {scroll: d.scrollWidth - d.clientWidth, worst: wide.slice(0, 3)};
}"""

INK = """() => {
  const el = document.querySelector('.report-figure b');
  if (!el) return null;
  const m = getComputedStyle(el).color.match(/\\d+/g).map(Number);
  // Relative luminance, so "dark enough to print" is one number rather
  // than three thresholds.
  const f = c => { c /= 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(m[0]) + 0.7152 * f(m[1]) + 0.0722 * f(m[2]);
}"""

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    # ONE login, then resize per viewport — five logins in ten minutes
    # trip the rate limiter, and a rate-limited context lands on the
    # login page, where nothing overflows and every check passes while
    # measuring the wrong page. (Same trap as check_admin_widths.py.)
    ctx = new_context(browser, *VIEWPORTS[0], motion=STILL)
    page = ctx.new_page()
    page.goto(BASE + "/admin/login", wait_until="load")
    page.fill("input[name=email]", "report@example.com")
    page.fill("input[name=password]", PW)
    page.click("button[type=submit]")
    page.wait_for_load_state("load")

    for width, height in VIEWPORTS:
        tag = "%dx%d" % (width, height)
        page.set_viewport_size({"width": width, "height": height})
        page.goto(URL, wait_until="load")

        # On screen first, so a failure below is known to be about print
        # and not about the page having failed to load at all.
        page.emulate_media(media="screen")
        check("%s: the page is actually the report" % tag,
              page.evaluate(SHOWN, ".report-doc") is True,
              "landed on %s" % page.url)
        check("%s: the period picker is there on screen" % tag,
              page.evaluate(SHOWN, ".report-tools") is True)

        page.emulate_media(media="print")

        # ---- what must NOT be printed
        for sel, what in ((".admin-side", "the admin sidebar"),
                          (".report-tools", "the period picker"),
                          (".skip-link", "the skip link")):
            check("%s: %s is not printed" % (tag, what),
                  page.evaluate(SHOWN, sel) is not True,
                  "%s is on the paper" % sel)

        # ---- what MUST be printed
        for sel, what in ((".report-doc", "the document"),
                          (".report-head h2", "the association's name"),
                          (".report-period-line", "the period"),
                          (".report-figures", "the figures"),
                          (".report-caveat", "the caveat"),
                          (".report-compare", "the comparison"),
                          (".report-foot", "the produced-on line")):
            check("%s: %s is printed" % (tag, what),
                  page.evaluate(SHOWN, sel) is True,
                  "%s is missing from the paper" % sel)

        text = page.inner_text(".report-doc")
        check("%s: it names the association on the paper" % tag,
              ORG in text)
        check("%s: and the charity number" % tag, "1123456" in text)
        check("%s: and the period it covers" % tag,
              "01 May 2026" in text and "31 May 2026" in text, text[:160])
        check("%s: and the produced-on date" % tag, "Produced" in text)
        check("%s: THE CAVEAT SITS WITH THE FIGURES" % tag,
              "one person on one day" in text
              and text.index("one person on one day") > text.index("visits"),
              "the caveat is not below the figures it qualifies")

        # A printed link that drags its URL into the sentence turns a
        # record into a screenshot of a web page.
        check("%s: no printed URLs" % tag,
              "http://" not in text and "https://" not in text,
              [line for line in text.splitlines() if "http" in line])

        over = page.evaluate(OVERFLOW)
        check("%s: nothing runs off the side of the paper" % tag,
              over["scroll"] <= 1 and not over["worst"], str(over))

        ink = page.evaluate(INK)
        check("%s: the figures print in dark ink, not screen green" % tag,
              ink is not None and ink < 0.1,
              "luminance %.3f — too light to read on paper"
              % (ink if ink is not None else -1))

        if SHOTS:
            page.screenshot(path=os.path.join(SHOTS, "report-print-%s.png"
                                              % tag), full_page=True)
        page.emulate_media(media="screen")

    # ---- the note about a missing charity number is SCREEN ONLY.
    # It is a message to the admin. On a funding application it would
    # read as the charity not knowing its own number.
    with app.app_context():
        block = Block.query.filter_by(key=ORG_CHARITY_NO_KEY).first()
        block.value = ""
        db.session.commit()
    page.set_viewport_size({"width": VIEWPORTS[0][0],
                            "height": VIEWPORTS[0][1]})
    page.goto(URL, wait_until="load")
    page.emulate_media(media="screen")
    check("with no charity number, the admin is told on screen",
          page.evaluate(SHOWN, ".report-missing") is True)
    page.emulate_media(media="print")
    check("...AND THAT NOTE IS NEVER PRINTED",
          page.evaluate(SHOWN, ".report-missing") is not True,
          "a note to the admin would go out on the funding application")
    check("...and the document still prints without it",
          page.evaluate(SHOWN, ".report-doc") is True)

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
print("The report prints as a document at all %d viewports."
      % len(VIEWPORTS))
