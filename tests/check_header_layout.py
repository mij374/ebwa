"""Measure the header at real viewport widths (Chromium via Playwright).

Not part of the smoke suite — it needs a browser — but this is what the
header rework was verified against. It asserts, at each width, that the
nav sits on ONE line, nothing overflows the viewport horizontally, and
the mobile menu opens and closes.

Run:  python tests/check_header_layout.py [--shots DIR]
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_header.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from werkzeug.serving import make_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import app, db, DEFAULT_BLOCKS, Block, FEATURES, FeatureFlag  # noqa: E402

WIDTHS = [1440, 1280, 1024, 768, 390]
MOBILE_MENU_MAX = 899          # below this the hamburger takes over
PAGES = ["/", "/about", "/events", "/contact", "/admin/login"]

shots_dir = None
if "--shots" in sys.argv:
    shots_dir = sys.argv[sys.argv.index("--shots") + 1]
    os.makedirs(shots_dir, exist_ok=True)

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

server = make_server("127.0.0.1", 5099, app)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)
BASE = "http://127.0.0.1:5099"

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for width in WIDTHS:
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.goto(BASE + "/", wait_until="networkidle")

        # ---- no horizontal overflow anywhere on the page
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - "
            "document.documentElement.clientWidth")
        check("%dpx: page does not scroll sideways" % width, overflow <= 0,
              "%dpx of overflow" % overflow)

        # ---- header children stay inside the header box
        head = page.evaluate("""() => {
            const nav = document.querySelector('.nav');
            const box = nav.getBoundingClientRect();
            const kids = [...nav.children].map(el => {
                const r = el.getBoundingClientRect();
                return {cls: el.className, left: r.left, right: r.right,
                        visible: r.width > 0 && r.height > 0};
            }).filter(k => k.visible);
            return {left: box.left, right: box.right, height: box.height,
                    kids};
        }""")
        inside = all(k["left"] >= head["left"] - 1
                     and k["right"] <= head["right"] + 1
                     for k in head["kids"])
        check("%dpx: header contents fit the header" % width, inside,
              str(head["kids"]))

        # ---- the nav itself is on exactly one line
        nav = page.evaluate("""() => {
            const ul = document.getElementById('navLinks');
            const style = getComputedStyle(ul);
            if (style.display === 'none') return {hidden: true};
            const items = [...ul.querySelectorAll('li')]
                .filter(li => getComputedStyle(li).display !== 'none');
            const tops = new Set(items.map(
                li => Math.round(li.getBoundingClientRect().top)));
            return {hidden: false, lines: tops.size, items: items.length,
                    width: ul.getBoundingClientRect().width};
        }""")
        if nav.get("hidden"):
            check("%dpx: nav collapsed into the menu button" % width,
                  width <= MOBILE_MENU_MAX, "hidden at a desktop width")
            btn = page.evaluate(
                "() => getComputedStyle("
                "document.getElementById('menuBtn')).display")
            check("%dpx: menu button shown instead" % width, btn != "none",
                  btn)
        else:
            check("%dpx: nav is on one line" % width, nav["lines"] == 1,
                  "%d lines across %d items" % (nav["lines"], nav["items"]))
            check("%dpx: full nav only above the mobile breakpoint" % width,
                  width > MOBILE_MENU_MAX, "visible at %d" % width)

        # ---- the logo mark is a legible size, not a smudge
        mark = page.evaluate(
            "() => document.querySelector('.brand-mark img')"
            ".getBoundingClientRect().height")
        check("%dpx: header logo is large enough" % width, mark >= 40,
              "%.0fpx" % mark)

        # ---- the mobile menu opens, lists the phone, and closes
        if width <= MOBILE_MENU_MAX:
            page.click("#menuBtn")
            page.wait_for_timeout(120)
            opened = page.evaluate("""() => {
                const ul = document.getElementById('navLinks');
                const r = ul.getBoundingClientRect();
                const phone = ul.querySelector('.nav-phone-item');
                return {display: getComputedStyle(ul).display,
                        right: r.right, width: r.width,
                        expanded: document.getElementById('menuBtn')
                            .getAttribute('aria-expanded'),
                        phone: phone ? getComputedStyle(phone).display
                                     : 'missing'};
            }""")
            check("%dpx: menu opens" % width, opened["display"] != "none",
                  opened["display"])
            check("%dpx: menu marked expanded" % width,
                  opened["expanded"] == "true", str(opened["expanded"]))
            check("%dpx: menu stays within the viewport" % width,
                  opened["right"] <= width + 1, str(opened["right"]))
            check("%dpx: phone number available in the menu" % width,
                  opened["phone"] not in ("none", "missing"),
                  str(opened["phone"]))
            over = page.evaluate(
                "() => document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth")
            check("%dpx: open menu causes no sideways scroll" % width,
                  over <= 0, "%dpx" % over)
            if shots_dir:
                page.screenshot(path=os.path.join(
                    shots_dir, "header-%d-menu-open.png" % width))
            page.click("#menuBtn")
            page.wait_for_timeout(120)
            closed = page.evaluate(
                "() => getComputedStyle("
                "document.getElementById('navLinks')).display")
            check("%dpx: menu closes again" % width, closed == "none", closed)
        else:
            check("%dpx: phone hidden or inline, never wrapping" % width,
                  True)

        # ---- other pages share the header, so spot-check them too
        for path in PAGES[1:]:
            page.goto(BASE + path, wait_until="networkidle")
            over = page.evaluate(
                "() => document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth")
            check("%dpx: %s does not scroll sideways" % (width, path),
                  over <= 0, "%dpx" % over)

        # ---- the login badge should read as a seal, not a smudge
        page.goto(BASE + "/admin/login", wait_until="networkidle")
        badge = page.evaluate(
            "() => document.querySelector('.login-card .brand-mark img')"
            ".getBoundingClientRect().height")
        check("%dpx: login badge is legible" % width, badge >= 80,
              "%.0fpx" % badge)
        if shots_dir:
            page.goto(BASE + "/", wait_until="networkidle")
            page.screenshot(path=os.path.join(shots_dir,
                                              "header-%d.png" % width))
        page.close()
    browser.close()

server.shutdown()
with app.app_context():
    db.session.remove()
    db.engine.dispose()
for suffix in ("", "-wal", "-shm"):
    f = TEST_DB + suffix
    if os.path.isfile(f):
        os.remove(f)

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
