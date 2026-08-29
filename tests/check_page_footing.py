"""Every public page ends the same distance above the footer.

The membership payment page had none: `<section class="wrap">` puts both
rules on one element and the class wins, so `.wrap{padding:0 24px}`
REPLACED `section{padding:80px 0}` rather than adding to it. The Pay
button sat against the footer, and there was nothing above the eyebrow
either. Every other public page nests them, which is where their 80px
comes from.

So this measures the real gap — the bottom of the last thing in the
page's content to the top of the footer — and asserts the payment pages
match the pages they are supposed to look like. It compares against
SIBLING PAGES rather than a number written in here: a check carrying its
own copy of 80 would pass while disagreeing with the site, and would
have to be edited the day somebody changes the spacing on purpose.

Run:  python tests/check_page_footing.py [--shots DIR]
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEST_DB = os.path.join(HERE, "test_page_footing.db")
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _s):
        os.remove(TEST_DB + _s)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from werkzeug.serving import make_server                  # noqa: E402
from playwright.sync_api import sync_playwright           # noqa: E402

from browser_motion import STILL, new_context             # noqa: E402
from browser_view import VIEWPORTS                        # noqa: E402

from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,  # noqa: E402
                 FeatureFlag, Member)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

SHOTS = (sys.argv[sys.argv.index("--shots") + 1]
         if "--shots" in sys.argv else None)
if SHOTS:
    os.makedirs(SHOTS, exist_ok=True)

PORT = 5195
BASE = "http://127.0.0.1:%d" % PORT

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        ("\n        %s" % detail) if detail and not cond
                        else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        db.session.add(Block(group=group, key=key, label=label, kind=kind,
                             value=value))
    for n, _l, _d, _default in FEATURES:
        db.session.add(FeatureFlag(name=n, enabled=True))
    db.session.add(Member(name="A Member", email="member@example.org"))
    db.session.commit()

server = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)

# The gap a reader actually sees: from the bottom of the last painted
# thing inside <main> to the top of the footer. Measured rather than read
# off a stylesheet, because padding, margins and collapsing all get a
# say and only the rendered page knows the answer.
# LEAVES ONLY — elements with no element children of their own. A
# container's rectangle INCLUDES its own padding, so measuring <section>
# reports its bottom as touching the footer and the gap as nought. The
# first version of this did exactly that and returned 0px for every page
# on the site, passing while measuring nothing: the padding it was
# looking for was inside the box it was measuring.
GAP_JS = """() => {
  const main = document.getElementById('main');
  const foot = document.querySelector('footer');
  if (!main || !foot) return null;
  let lowest = -Infinity;
  main.querySelectorAll('*').forEach(el => {
    if (el.children.length) return;              // not a leaf
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;     // paints nothing
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return;
    lowest = Math.max(lowest, r.bottom);
  });
  if (lowest === -Infinity) return null;
  return Math.round(foot.getBoundingClientRect().top - lowest);
}"""

# Pages built the ordinary way, to take the expected gap FROM rather
# than writing a number in here.
SIBLINGS = ["/donate", "/membership", "/contact"]
UNDER_TEST = ["/membership/pay", "/membership/paid"]

results = {}
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for width, height in VIEWPORTS:
        ctx = new_context(browser, width, height, motion=STILL)
        page = ctx.new_page()
        print()
        print("---- %dx%d" % (width, height))
        gaps = {}
        for url in SIBLINGS + UNDER_TEST:
            page.goto(BASE + url, wait_until="load")
            # The cookie notice is fixed to the bottom of the screen and
            # would be measured as the lowest thing on the page.
            page.evaluate("""() => {
                const n = document.querySelector('.cookie-notice');
                if (n) n.remove();
            }""")
            gaps[url] = page.evaluate(GAP_JS)
            results[(width, url)] = gaps[url]
        expected = min(gaps[u] for u in SIBLINGS)
        # A sibling with no gap at all means the measurement is broken,
        # not that the site is: these three pages are the reference.
        check("%dpx: the reference pages have a measurable gap" % width,
              expected > 20, "smallest sibling gap was %spx" % expected)
        print("        siblings: %s" % ", ".join(
            "%s %spx" % (u, gaps[u]) for u in SIBLINGS))
        for url in UNDER_TEST:
            got = gaps[url]
            check("%dpx %s: has room before the footer" % (width, url),
                  got is not None and got >= expected - 2,
                  "%spx, against %spx on the plainest sibling" % (got, expected))
            # And not a page of empty space either — matching means
            # matching, not "at least".
            check("%dpx %s: and not a gulf of it" % (width, url),
                  got is not None and got <= expected + 40,
                  "%spx, against %spx" % (got, expected))
        if SHOTS:
            page.goto(BASE + "/membership/pay", wait_until="load")
            page.screenshot(path=os.path.join(SHOTS, "pay-%d.png" % width),
                            full_page=True)
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
print("Every page ends the same distance above the footer.")
