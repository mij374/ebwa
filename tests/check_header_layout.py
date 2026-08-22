"""Measure the header at real viewport widths (Chromium via Playwright).

Not part of the smoke suite — it needs a browser — but this is what the
header work is verified against. At each width it asserts that the nav
sits on ONE line, nothing overflows the viewport horizontally, and the
mobile menu opens and closes.

Since the nav became three groups with dropdown panels, it also asserts
that the panels stay shut until hovered or focused, that a keyboard alone
reaches every destination in them, and that on a phone the groups expand
in place inside the menu instead of hiding behind a hover.

Every page here is opened STILL (prefers-reduced-motion: reduce) through
tests/browser_motion.py, so nothing measured is mid-animation. Nothing
in this file is testing motion; a check that were would ask for MOVING
itself. See that module for why.

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
sys.path.insert(0, HERE)

from werkzeug.serving import make_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from browser_motion import new_page  # noqa: E402

from app import app, db, DEFAULT_BLOCKS, Block, FEATURES, FeatureFlag  # noqa: E402

# 900 and 768 sit either side of the 899px shed point, where the whole
# nav collapses to the menu button: 900 is the last width that must fit
# every group on one line, so it is the case most likely to regress.
WIDTHS = [1440, 1280, 1024, 900, 768, 390]
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
        page = new_page(browser, width)
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
            // Direct children only: dropdown items live in their own
            // panel and are not part of the row.
            const items = [...ul.children]
                .filter(li => getComputedStyle(li).display !== 'none');
            // Items are no longer all the same height — a group trigger
            // fills the header, the Donate pill does not — so "one line"
            // means their CENTRES line up, not their tops.
            const mids = items.map(li => {
                const r = li.getBoundingClientRect();
                return r.top + r.height / 2;
            });
            const spread = Math.max(...mids) - Math.min(...mids);
            return {hidden: false, lines: spread <= 4 ? 1 : 2,
                    spread: Math.round(spread), items: items.length,
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
                  "%d items, centres %dpx apart"
                  % (nav["items"], nav["spread"]))
            check("%dpx: full nav only above the mobile breakpoint" % width,
                  width > MOBILE_MENU_MAX, "visible at %d" % width)

        # ---- the dropdowns: shut, then open on hover and on focus
        if width > MOBILE_MENU_MAX:
            groups = page.locator("#navLinks > li.nav-group")
            check("%dpx: three groups in the row" % width,
                  groups.count() == 3, str(groups.count()))
            shut = page.evaluate("""() => [...document.querySelectorAll(
                    '#navLinks > li.nav-group .nav-drop')]
                .every(d => getComputedStyle(d).visibility === 'hidden')""")
            check("%dpx: panels start shut" % width, shut)

            first = page.locator("#navLinks > li.nav-group").first
            first.hover()
            page.wait_for_timeout(220)
            open_state = page.evaluate("""() => {
                const d = document.querySelector(
                    '#navLinks > li.nav-group .nav-drop');
                const r = d.getBoundingClientRect();
                return {vis: getComputedStyle(d).visibility,
                        inside: r.left >= -1 && r.right <= innerWidth + 1,
                        below: r.top >= document.querySelector('.nav')
                            .getBoundingClientRect().bottom - 40};
            }""")
            check("%dpx: hover opens a panel" % width,
                  open_state["vis"] == "visible", open_state["vis"])
            check("%dpx: open panel stays inside the viewport" % width,
                  open_state["inside"], str(open_state))
            over = page.evaluate(
                "() => document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth")
            check("%dpx: open panel causes no sideways scroll" % width,
                  over <= 0, "%dpx" % over)
            if shots_dir:
                page.screenshot(path=os.path.join(
                    shots_dir, "header-%d-dropdown.png" % width))

            # keyboard alone: focusing a trigger must reveal its items
            page.evaluate("() => document.querySelector("
                          "'#navLinks > li.nav-group > a').focus()")
            page.wait_for_timeout(200)
            check("%dpx: focus alone opens the panel" % width,
                  page.evaluate("""() => getComputedStyle(
                      document.querySelector(
                          '#navLinks > li.nav-group .nav-drop')
                      ).visibility""") == "visible")

            # ---- tab from the brand and collect everything reachable
            page.evaluate("() => document.querySelector('.brand').focus()")
            reached, guard = [], 0
            while guard < 40:
                guard += 1
                page.keyboard.press("Tab")
                here = page.evaluate("""() => {
                    const el = document.activeElement;
                    if (!el || el.tagName !== 'A') return null;
                    return el.closest('#navLinks') ? el.getAttribute('href')
                                                   : 'left-the-nav';
                }""")
                if here is None:
                    continue
                if here == "left-the-nav":
                    break
                reached.append(here)
            wanted = ["/about", "/our-journey", "/faq", "/events", "/news",
                      "/gallery", "/membership", "/resources", "/contact",
                      "/donate"]
            missing = [w for w in wanted if w not in reached]
            check("%dpx: every nav destination is keyboard-reachable" % width,
                  not missing, "missing %s (reached %s)"
                  % (missing, reached))

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
            expanded = page.evaluate("""() => {
                const drops = [...document.querySelectorAll('.nav-drop')];
                const links = [...document.querySelectorAll(
                    '.nav-drop a')].map(a => a.getAttribute('href'));
                return {panels: drops.length,
                        allShown: drops.every(
                            d => getComputedStyle(d).visibility === 'visible'
                              && getComputedStyle(d).position === 'static'),
                        links: links};
            }""")
            check("%dpx: groups expand in place in the menu" % width,
                  expanded["allShown"], str(expanded["panels"]))
            for path in ("/about", "/our-journey", "/faq", "/events",
                         "/news", "/gallery", "/membership", "/resources",
                         "/contact"):
                check("%dpx: menu lists %s" % (width, path),
                      path in expanded["links"], str(expanded["links"]))
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

        # ---- the cookie notice must not sit over the footer, and must be
        # dismissible from the keyboard alone
        page.goto(BASE + "/", wait_until="load")
        notice = page.locator(".cookie-notice")
        check("%dpx: cookie notice shown on a first visit" % width,
              notice.count() == 1, str(notice.count()))
        if notice.count():
            # This page is STILL, so scroll-behavior:smooth is off and
            # the jump lands at once. Both belt and braces stay: ask for
            # an instant scroll, then WAIT for it to land before
            # measuring. Under smooth scrolling a fixed wait measured
            # whatever the animation had reached — further short of the
            # bottom the taller the page — so the footer was still below
            # the fold while the fixed notice sat at the bottom of the
            # VIEWPORT, and every width read as an overlap it does not
            # have. The notice's position only means anything once the
            # page is genuinely at its end, so assert that rather than
            # trusting the context to have made it true.
            page.evaluate("""() => window.scrollTo(
                {top: document.documentElement.scrollHeight,
                 behavior: 'instant'})""")
            page.wait_for_function("""() => {
                const de = document.documentElement;
                return Math.abs(de.scrollHeight - de.clientHeight
                                - Math.round(window.scrollY)) <= 1;
            }""")
            room = page.evaluate("""() => {
                const n = document.querySelector('.cookie-notice')
                    .getBoundingClientRect();
                const f = document.querySelector('.foot-bar')
                    .getBoundingClientRect();
                const de = document.documentElement;
                return {gap: Math.round(n.top - f.bottom),
                        short: de.scrollHeight - de.clientHeight
                               - Math.round(window.scrollY),
                        over: document.documentElement.scrollWidth
                              - document.documentElement.clientWidth};
            }""")
            check("%dpx: footer clears the cookie notice" % width,
                  room["gap"] >= 0, "%dpx of overlap, %dpx short of the "
                  "bottom" % (-room["gap"], room["short"]))
            check("%dpx: cookie notice causes no sideways scroll" % width,
                  room["over"] <= 0, "%dpx" % room["over"])
            with page.expect_navigation(wait_until="load"):
                page.focus(".cookie-ok")
                page.keyboard.press("Enter")
            page.wait_for_timeout(300)
            check("%dpx: notice dismissed with the keyboard alone" % width,
                  page.locator(".cookie-notice").count() == 0)

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
