"""Drive the partner scroller in real Chromium (Playwright).

The markup side of the partner row is covered by
tests/smoke_test_partners.py — two sets, the copy hidden from screen
readers and out of the tab order, the arrows present and `hidden`,
`data-motion` on the row. None of that needs a browser, and none of it
is the part that breaks. What breaks is the behaviour: whether the loop
actually closes, whether a drag steals the click from a partner, whether
the arrows appear for the people they are the affordance for.

So this asserts, against a real engine:

  * the endless loop leaves NO GAP in the visible row — at rest, and
    after each of the three things that used to open one (a drag, a
    wheel, a Tab into the row), at every standard width and several
    partner counts
  * stepping advances exactly one card plus one gap, and settles
  * the row pauses for a pointer over it and for keyboard focus in it
  * dragging scrolls without swallowing the click on a partner, while a
    small wobble still counts as a click
  * the arrows appear only when the row is standing still AND has
    somewhere to go, disable at each end, and work from the keyboard
  * a touch swipe still pans natively, our drag code keeping out of it
  * every mode falls back to a still row under reduced motion

Motion context is per check, from tests/browser_motion.py: MOVING where
the movement itself is what is being measured, STILL for the static and
arrow behaviour — which is the state those checks are about, and which
also makes them deterministic, since nothing is sliding under the
pointer while the check measures.

Three things learned the hard way here, so they are not rediscovered:

  * Playwright's Chromium has OVERLAY scrollbars, so a hidden scrollbar
    frees no layout space. Gutter and pixel-width assertions measure
    nothing. Assert the computed style (`scrollbar-width: none`) and the
    behaviour instead.
  * A smooth `scrollBy` is still gliding when the next line runs.
    Anything that samples a position after an arrow press or a step has
    to WAIT FOR IT TO SETTLE — `settle()` below — not guess a timeout.
  * Click coordinates must come from a card that is actually visible in
    the row. A fixed offset from the row's left edge lands in the gap
    between two cards at some widths, and the click hits nothing.

Run:  python tests/check_partner_marquee.py [--shots DIR]
"""
import gc
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_marquee.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from werkzeug.serving import make_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402

from browser_motion import MOVING, STILL, new_context  # noqa: E402

from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,  # noqa: E402
                 FeatureFlag, PARTNER_MOTION_KEY, PARTNER_STEP_KEY,
                 Partner)

# The same widths as the header check, for the same reason: 900 and 768
# straddle the 899px shed point, and 390 is below the 640px one where the
# arrows move UNDER the row.
WIDTHS = [1440, 1280, 1024, 900, 768, 390]
# Five is the first count that tips into the scroller and, being the
# narrowest set, the tightest case for the loop invariant.
COUNTS = [5, 7, 9]
STEP_SECONDS = 2          # long enough to watch a step land and stop

shots_dir = None
if "--shots" in sys.argv:
    shots_dir = sys.argv[sys.argv.index("--shots") + 1]
    os.makedirs(shots_dir, exist_ok=True)

failures = []
warnings = []
tightest = None      # (spare, room, stride, tag) — the smallest spare seen


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def warn(name, cond, detail=""):
    """Report a margin, not a result.

    Some of what this file measures is headroom rather than behaviour:
    the row still loops and still shows no gap when it runs out, it just
    does it less tidily. Failing the run for that would cry wolf, and
    saying nothing would let it drift to nothing unnoticed — so it is a
    WARNING carrying the numbers, listed again at the end.
    """
    print("%s  %s [%s]" % ("PASS" if cond else "WARN", name, detail))
    if not cond:
        warnings.append("%s [%s]" % (name, detail))


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

server = make_server("127.0.0.1", 5088, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)
BASE = "http://127.0.0.1:5088"


def seed(count):
    """A row of `count` partners, each a real link back at this server.

    Real links matter: the drag checks assert on whether a click opens
    the partner, and a link to the outside world would put a network
    fetch inside the assertion.
    """
    with app.app_context():
        Partner.query.delete()
        for i in range(count):
            db.session.add(Partner(
                name="Partner %d" % i, blurb="Working with EBWA",
                url="%s/?partner=%d" % (BASE, i),
                display_mode="text", sort=i))
        db.session.commit()


def set_motion(mode, seconds=STEP_SECONDS):
    with app.app_context():
        for key, value in ((PARTNER_MOTION_KEY, mode),
                           (PARTNER_STEP_KEY, str(seconds))):
            row = Block.query.filter_by(key=key).first()
            row.value = value
        db.session.commit()


# ---------------------------------------------------------------- JS

# Everything the checks need about the row in one round trip.
MEASURE = """() => {
    const box = document.getElementById('partnerRow');
    if (!box) return null;
    const row = box.closest('.partner-row');
    const track = box.querySelector('.partner-track');
    const set = box.querySelector('.partner-set');
    const gap = parseFloat(getComputedStyle(track).columnGap) || 0;
    const card = box.querySelector('.partner-card');
    const sets = [...box.querySelectorAll('.partner-set')];
    return {
        scrollLeft: box.scrollLeft,
        clientWidth: box.clientWidth,
        scrollWidth: box.scrollWidth,
        setWidth: set.getBoundingClientRect().width,
        setStride: set.getBoundingClientRect().width + gap,
        cardStride: card ? card.getBoundingClientRect().width + gap : 0,
        gap: gap,
        moving: row.classList.contains('is-moving'),
        hasArrows: row.classList.contains('has-arrows'),
        setsInMarkup: sets.length,
        setsShown: sets.filter(
            s => getComputedStyle(s).display !== 'none').length,
        scrollbarWidth: getComputedStyle(box).scrollbarWidth,
        snap: getComputedStyle(box).scrollSnapType,
        arrowsHidden: [...row.querySelectorAll('.partner-arrow')]
            .map(b => b.hidden),
        arrowsDisabled: [...row.querySelectorAll('.partner-arrow')]
            .map(b => b.disabled)
    };
}"""

# The widest run of empty pixels inside the row's visible strip. This is
# the loop's gap, measured as a reader would see it rather than derived
# from the arithmetic that is supposed to prevent it: clip every card to
# the visible box, sort, and walk the edges. A design gap between two
# cards is normal; anything wider is a hole.
WORST_HOLE = """() => {
    const box = document.getElementById('partnerRow');
    const b = box.getBoundingClientRect();
    const spans = [...box.querySelectorAll('.partner-card')]
        .map(c => c.getBoundingClientRect())
        .filter(r => r.right > b.left + 0.5 && r.left < b.right - 0.5)
        .map(r => [Math.max(r.left, b.left), Math.min(r.right, b.right)])
        .sort((p, q) => p[0] - q[0]);
    if (!spans.length) return Math.round(b.width);
    let worst = spans[0][0] - b.left, edge = spans[0][1];
    for (const [l, r] of spans) {
        if (l - edge > worst) worst = l - edge;
        if (r > edge) edge = r;
    }
    if (b.right - edge > worst) worst = b.right - edge;
    return Math.round(worst);
}"""

SCROLL_LEFT = "() => document.getElementById('partnerRow').scrollLeft"


def show_row(page):
    """Bring the row on screen — the drift stops while it is not."""
    page.evaluate("""() => document.querySelector('.partner-row')
        .scrollIntoView({behavior: 'instant', block: 'center'})""")
    page.wait_for_timeout(120)


def settle(page, tries=80, quiet=3):
    """Wait for a glide (smooth scrollBy) to stop, then report where.

    Sampling straight after an arrow press or a step reads a position
    part way through the animation, which is neither the old one nor the
    new one. Poll until it stops changing instead of guessing a wait.
    """
    last, same = None, 0
    for _ in range(tries):
        now = page.evaluate(SCROLL_LEFT)
        if last is not None and abs(now - last) < 0.5:
            same += 1
            if same >= quiet:
                return now
        else:
            same = 0
        last = now
        page.wait_for_timeout(40)
    return last


def worst_hole_over(page, ms=700, every=50):
    """The widest hole seen across a stretch of the row's movement."""
    worst = 0
    for _ in range(max(1, ms // every)):
        worst = max(worst, page.evaluate(WORST_HOLE))
        page.wait_for_timeout(every)
    return worst


def card_point(page, nth=0):
    """Centre of the visible part of the nth most-visible card.

    NOT a fixed offset from the row's left edge: at some widths that
    lands in the gap between two cards and the click hits the row
    itself, which passes a drag check for entirely the wrong reason.

    Nor "the first card wholly inside the strip": a card is 260px and
    the strip is 342px at 390px wide, so for most drift positions no
    card is wholly inside and there would be nothing to aim at. Clip
    each card to the strip, take the widest slice, and aim at the middle
    of THAT — which is on the card wherever the row happens to be.
    """
    return page.evaluate("""(nth) => {
        const box = document.getElementById('partnerRow');
        const b = box.getBoundingClientRect();
        const seen = [...box.querySelectorAll('.partner-card')]
            .map(c => {
                const r = c.getBoundingClientRect();
                const left = Math.max(r.left, b.left + 2);
                const right = Math.min(r.right, b.right - 2);
                return {left: left, right: right, width: right - left,
                        top: r.top, height: r.height};
            })
            .filter(c => c.width > 40)
            .sort((p, q) => q.width - p.width);
        const c = seen[Math.min(nth, seen.length - 1)];
        if (!c) return null;
        return {x: Math.round((c.left + c.right) / 2),
                y: Math.round(c.top + c.height / 2)};
    }""", nth)


def drag(page, point, dx, steps=10):
    """A mouse drag across the row, in steps so pointermove really fires."""
    page.mouse.move(point["x"], point["y"])
    page.mouse.down()
    for i in range(1, steps + 1):
        page.mouse.move(point["x"] + dx * i / steps, point["y"])
        page.wait_for_timeout(10)
    page.mouse.up()


def open_home(browser, width, motion, **options):
    ctx = new_context(browser, width, motion=motion, **options)
    page = ctx.new_page()
    page.goto(BASE + "/", wait_until="load")
    show_row(page)
    return ctx, page


with sync_playwright() as pw:
    browser = pw.chromium.launch()

    # ================================================================
    # A. The loop closes — no gap — whatever has been done to the row.
    #    MOVING: the drift is the thing under test.
    # ================================================================
    set_motion("scroll")
    for count in COUNTS:
        seed(count)
        for width in WIDTHS:
            ctx, page = open_home(browser, width, MOVING)
            tag = "scroll %d partners %dpx" % (count, width)
            m = page.evaluate(MEASURE)
            check("%s: the scroller is the one on the page" % tag,
                  m is not None and m["setsInMarkup"] == 2, str(m))
            if m is None:
                ctx.close()
                continue
            check("%s: the row is marked moving" % tag, m["moving"], str(m))

            # The invariant the endless loop rests on. One set must be
            # wider than the visible strip, or wrapping back by a set
            # leaves the far end of the window past the content — which
            # is the gap. More copies of the set would not fix it: the
            # container can always be scrolled to its own end.
            check("%s: one set is wider than the visible row" % tag,
                  m["setWidth"] >= m["clientWidth"],
                  "set %.0f vs window %.0f" % (m["setWidth"],
                                               m["clientWidth"]))

            # Headroom for the STEP wrap, measured here because the
            # geometry does not depend on the mode and this is where
            # every width and count is covered.
            #
            # Stepping wraps back by a set only once scrollLeft has gone
            # PAST one, and it gets there by scrollBy-ing one stride at
            # a time. So the container needs a whole stride of room past
            # the end of the first set — setWidth - clientWidth against
            # one card plus one gap. Short of it the browser clamps the
            # last step before the wrap into a stub: the row still loops
            # and still shows no gap, so this is a warning, not a
            # failure. CARD WIDTH AND GAP ARE LOAD-BEARING HERE — both
            # are fixed in the stylesheet, and growing either eats this
            # margin. Five partners on the widest viewport is the
            # tightest case, since that is the narrowest set against the
            # widest strip.
            room = m["setWidth"] - m["clientWidth"]
            spare = room - m["cardStride"]
            if tightest is None or spare < tightest[0]:
                tightest = (spare, room, m["cardStride"], tag)
            warn("%s: room to wrap a whole step" % tag,
                 spare >= 0, "%.0fpx of room against a %.0fpx stride, "
                 "%.0fpx spare" % (room, m["cardStride"], spare))

            hole = worst_hole_over(page, 800)
            check("%s: no gap while it drifts" % tag,
                  hole <= m["gap"] + 2, "%dpx hole, gap is %.0fpx"
                  % (hole, m["gap"]))

            # ---- a drag, a wheel and a Tab: the three things that used
            # to add a second offset and tear the loop open.
            point = card_point(page)
            check("%s: a card is visible to aim at" % tag,
                  point is not None, str(point))
            if point:
                drag(page, point, -160)
                hole = worst_hole_over(page, 600)
                check("%s: no gap after a drag" % tag,
                      hole <= m["gap"] + 2, "%dpx hole" % hole)

            page.mouse.move(point["x"], point["y"]) if point else None
            page.mouse.wheel(500, 0)
            page.wait_for_timeout(150)
            hole = worst_hole_over(page, 600)
            check("%s: no gap after a wheel" % tag,
                  hole <= m["gap"] + 2, "%dpx hole" % hole)

            # A Tab into the row scrolls the container natively to bring
            # the focused card into view — the same second offset by
            # another route.
            page.mouse.move(0, 0)
            page.evaluate("""() => {
                const real = document.querySelector(
                    '.partner-set:not([aria-hidden]) .partner-card:last-child');
                if (real) real.focus();
            }""")
            page.wait_for_timeout(200)
            hole = worst_hole_over(page, 600)
            check("%s: no gap after a Tab into the row" % tag,
                  hole <= m["gap"] + 2, "%dpx hole" % hole)

            after = page.evaluate(MEASURE)
            check("%s: never scrolled past its own content" % tag,
                  after["scrollLeft"] <= after["scrollWidth"]
                  - after["clientWidth"] + 1,
                  "%.0f of %.0f" % (after["scrollLeft"],
                                    after["scrollWidth"] - after["clientWidth"]))
            if shots_dir and count == 5:
                page.screenshot(path=os.path.join(
                    shots_dir, "marquee-scroll-%d.png" % width))
            ctx.close()

    # ================================================================
    # B. Stepping: exactly one card plus one gap, then a rest.
    #    MOVING: a step is motion.
    # ================================================================
    set_motion("step", STEP_SECONDS)
    seed(7)
    for width in (1024, 390):
        ctx, page = open_home(browser, width, MOVING)
        tag = "step %dpx" % width
        m = page.evaluate(MEASURE)
        check("%s: the row is marked moving" % tag, m["moving"], str(m))
        check("%s: no arrows while it is stepping" % tag,
              all(m["arrowsHidden"]), str(m["arrowsHidden"]))

        before = settle(page)
        # Wait for the interval to fire rather than assuming when.
        page.wait_for_function(
            "(was) => Math.abs(document.getElementById('partnerRow')"
            ".scrollLeft - was) > 1", arg=before,
            timeout=(STEP_SECONDS + 3) * 1000)
        landed = settle(page)
        moved = landed - before
        check("%s: a step moves one card plus one gap" % tag,
              abs(moved - m["cardStride"]) <= 2,
              "moved %.0f, stride %.0f" % (moved, m["cardStride"]))

        # ...and then stands still until the next one is due.
        resting = page.evaluate(SCROLL_LEFT)
        page.wait_for_timeout(600)
        check("%s: it rests between steps" % tag,
              abs(page.evaluate(SCROLL_LEFT) - resting) <= 1,
              "drifted %.1f" % (page.evaluate(SCROLL_LEFT) - resting))
        check("%s: stepping leaves no gap" % tag,
              page.evaluate(WORST_HOLE) <= m["gap"] + 2,
              "%dpx" % page.evaluate(WORST_HOLE))
        ctx.close()

    # ================================================================
    # C. It pauses for anybody reading. MOVING, continuous.
    # ================================================================
    set_motion("scroll")
    seed(7)
    ctx, page = open_home(browser, 1024, MOVING)
    point = card_point(page)
    page.mouse.move(point["x"], point["y"])
    page.wait_for_timeout(200)
    held = page.evaluate(SCROLL_LEFT)
    page.wait_for_timeout(700)
    check("hover: the row stops for a pointer over it",
          abs(page.evaluate(SCROLL_LEFT) - held) <= 2,
          "moved %.1f" % (page.evaluate(SCROLL_LEFT) - held))

    page.mouse.move(0, 0)
    page.wait_for_timeout(600)
    check("hover: and starts again when the pointer leaves",
          abs(page.evaluate(SCROLL_LEFT) - held) > 2,
          "moved %.1f" % (page.evaluate(SCROLL_LEFT) - held))

    page.evaluate("""() => document.querySelector(
        '.partner-set:not([aria-hidden]) .partner-card').focus()""")
    page.wait_for_timeout(200)
    focused = page.evaluate(SCROLL_LEFT)
    page.wait_for_timeout(700)
    check("focus: the row stops while something in it has focus",
          abs(page.evaluate(SCROLL_LEFT) - focused) <= 2,
          "moved %.1f" % (page.evaluate(SCROLL_LEFT) - focused))
    page.evaluate("() => document.activeElement.blur()")
    page.wait_for_timeout(600)
    check("focus: and starts again when focus leaves",
          abs(page.evaluate(SCROLL_LEFT) - focused) > 2,
          "moved %.1f" % (page.evaluate(SCROLL_LEFT) - focused))
    ctx.close()

    # ================================================================
    # D. Drag to scroll, without stealing the click. STILL: this is not
    #    a motion behaviour, and a row sliding under the pointer would
    #    make the click coordinates a moving target.
    # ================================================================
    set_motion("none")
    seed(7)
    for width in (1024, 390):
        ctx, page = open_home(browser, width, STILL)
        tag = "drag %dpx" % width
        point = card_point(page)
        check("%s: a card is visible to aim at" % tag, point is not None)
        if point is None:
            ctx.close()
            continue

        before = page.evaluate(SCROLL_LEFT)
        try:
            with page.expect_popup(timeout=900) as popped:
                drag(page, point, -180)
            opened = popped.value is not None
        except PWTimeout:
            opened = False
        after = settle(page)
        check("%s: a drag scrolls the row" % tag, after - before > 40,
              "moved %.0f" % (after - before))
        check("%s: a drag does NOT open the partner" % tag, not opened)

        # A hand is never perfectly still: under the threshold it is a
        # click, and the link must open. This is the case that broke
        # when the pointer was captured on pointerdown — the click
        # retargeted to the row and the partner never opened.
        point = card_point(page)
        try:
            with page.expect_popup(timeout=3000) as popped:
                drag(page, point, -3, steps=2)
            popup = popped.value
            opened = True
        except PWTimeout:
            popup, opened = None, False
        check("%s: a small wobble still counts as a click" % tag, opened)
        if popup:
            check("%s: and opens that partner, not the row" % tag,
                  "partner=" in popup.url, popup.url)
            popup.close()
        ctx.close()

    # ================================================================
    # E. The arrows: for a row that is standing still.
    # ================================================================
    seed(7)
    # ---- shown when still, hidden when moving
    for mode, motion, label, want in (
            ("none", MOVING, "no-movement setting", True),
            ("scroll", STILL, "reduced motion over a scrolling row", True),
            ("step", STILL, "reduced motion over a stepping row", True),
            ("scroll", MOVING, "a row that is drifting", False),
            ("step", MOVING, "a row that is stepping", False)):
        set_motion(mode)
        ctx, page = open_home(browser, 1024, motion)
        m = page.evaluate(MEASURE)
        check("arrows: %s %s them" % (label, "gets" if want else "does not get"),
              all(h is not want for h in m["arrowsHidden"]),
              "hidden=%s" % m["arrowsHidden"])
        check("arrows: has-arrows matches, for the scrollbar rules",
              m["hasArrows"] == want, str(m["hasArrows"]))
        # Overlay scrollbars mean a hidden scrollbar frees no space, so
        # there is nothing to measure in pixels — assert the style.
        check("arrows: the scrollbar is hidden exactly when they are shown"
              if want else
              "arrows: a still row that is moving still hides its scrollbar",
              m["scrollbarWidth"] == "none", str(m["scrollbarWidth"]))
        ctx.close()

    # ---- and only when there is somewhere to go
    set_motion("none")
    ctx, page = open_home(browser, 1024, STILL)
    m = page.evaluate(MEASURE)
    check("arrows: shown while the row overflows", not any(m["arrowsHidden"]))
    # Shrink the cards so the whole row fits, and tell the script the
    # layout changed. This is refreshArrows' other branch: nothing to
    # scroll, so nothing to press.
    page.evaluate("""() => {
        const s = document.createElement('style');
        s.id = 'shrink';
        s.textContent =
            '.partner-marquee .partner-card{flex:0 0 20px;width:20px}';
        document.head.appendChild(s);
        window.dispatchEvent(new Event('resize'));
    }""")
    page.wait_for_timeout(150)
    m = page.evaluate(MEASURE)
    check("arrows: gone once there is nothing to scroll",
          all(m["arrowsHidden"]) and not m["hasArrows"],
          "hidden=%s has-arrows=%s" % (m["arrowsHidden"], m["hasArrows"]))
    page.evaluate("""() => {
        document.getElementById('shrink').remove();
        window.dispatchEvent(new Event('resize'));
    }""")
    page.wait_for_timeout(150)
    check("arrows: back when it overflows again",
          not any(page.evaluate(MEASURE)["arrowsHidden"]))

    # ---- disabled at each end
    page.evaluate("() => { document.getElementById('partnerRow')"
                  ".scrollLeft = 0; }")
    page.wait_for_timeout(120)
    m = page.evaluate(MEASURE)
    check("arrows: previous is disabled at the left end",
          m["arrowsDisabled"][0] and not m["arrowsDisabled"][1],
          str(m["arrowsDisabled"]))
    page.evaluate("""() => { const b = document.getElementById('partnerRow');
        b.scrollLeft = b.scrollWidth; }""")
    page.wait_for_timeout(120)
    m = page.evaluate(MEASURE)
    check("arrows: next is disabled at the right end",
          m["arrowsDisabled"][1] and not m["arrowsDisabled"][0],
          str(m["arrowsDisabled"]))

    # ---- one card per press, from the keyboard alone
    page.evaluate("() => { document.getElementById('partnerRow')"
                  ".scrollLeft = 0; }")
    page.wait_for_timeout(120)
    m = page.evaluate(MEASURE)
    page.focus(".partner-arrow-next")
    check("arrows: the next button takes keyboard focus",
          page.evaluate("() => document.activeElement.className")
          .find("partner-arrow-next") >= 0)
    page.keyboard.press("Enter")
    landed = settle(page)
    check("arrows: Enter moves the row one card",
          abs(landed - m["cardStride"]) <= 2,
          "moved %.0f, stride %.0f" % (landed, m["cardStride"]))
    page.focus(".partner-arrow-prev")
    page.keyboard.press("Enter")
    back = settle(page)
    check("arrows: and back again", abs(back) <= 2, "left at %.0f" % back)
    if shots_dir:
        page.screenshot(path=os.path.join(shots_dir, "marquee-arrows.png"))
    ctx.close()

    # ================================================================
    # F. Touch: the browser pans, our drag code keeps out of the way.
    # ================================================================
    set_motion("none")
    ctx, page = open_home(browser, 390, STILL, has_touch=True)
    page.evaluate("""() => {
        const row = document.querySelector('.partner-row');
        window.__sawDragging = false;
        new MutationObserver(() => {
            if (row.classList.contains('is-dragging'))
                window.__sawDragging = true;
        }).observe(row, {attributes: true, attributeFilter: ['class']});
    }""")
    point = card_point(page)
    before = page.evaluate(SCROLL_LEFT)
    cdp = ctx.new_cdp_session(page)
    # Raw touch events, NOT Input.synthesizeScrollGesture: the synthesised
    # gesture goes through the compositor and moves nothing at all in this
    # headless build (measured, both directions). Dispatched touches are
    # handled on the main thread and pan the container as a finger does.
    # The finger travels LEFT, which carries the content left and the
    # scroll offset up.
    cdp.send("Input.dispatchTouchEvent", {
        "type": "touchStart",
        "touchPoints": [{"x": point["x"], "y": point["y"], "id": 1}]})
    for i in range(1, 11):
        cdp.send("Input.dispatchTouchEvent", {
            "type": "touchMove",
            "touchPoints": [{"x": point["x"] - 20 * i,
                             "y": point["y"], "id": 1}]})
        page.wait_for_timeout(16)
    cdp.send("Input.dispatchTouchEvent",
             {"type": "touchEnd", "touchPoints": []})
    after = settle(page)
    check("touch: a swipe still pans the row", after - before > 40,
          "moved %.0f" % (after - before))
    check("touch: the drag handler stays out of it",
          page.evaluate("() => window.__sawDragging") is False)
    ctx.close()

    # ================================================================
    # G. Reduced motion: every mode falls back to a still row.
    # ================================================================
    seed(7)
    for mode in ("scroll", "step", "none"):
        set_motion(mode)
        ctx, page = open_home(browser, 1024, STILL)
        tag = "reduced motion, %s" % mode
        m = page.evaluate(MEASURE)
        check("%s: the row is not marked moving" % tag, not m["moving"])
        check("%s: only one set is rendered" % tag,
              m["setsShown"] == 1, str(m["setsShown"]))
        check("%s: snapping is on, so it rests on a card" % tag,
              "mandatory" in m["snap"], m["snap"])
        check("%s: the arrows are there instead" % tag,
              not any(m["arrowsHidden"]), str(m["arrowsHidden"]))
        at = page.evaluate(SCROLL_LEFT)
        page.wait_for_timeout(900)
        check("%s: and nothing moves on its own" % tag,
              abs(page.evaluate(SCROLL_LEFT) - at) <= 1,
              "moved %.1f" % (page.evaluate(SCROLL_LEFT) - at))
        ctx.close()

    browser.close()

server.shutdown()
server.server_close()
with app.app_context():
    db.session.remove()
    db.engine.dispose()
# Serving the pages threaded means requests ran on worker threads that
# have since ended, and on Windows their SQLite handles can outlive the
# dispose() by a moment. Deleting the db is housekeeping, not a result,
# so retry briefly and say so rather than failing a passing run.
gc.collect()
for suffix in ("", "-wal", "-shm"):
    f = TEST_DB + suffix
    for attempt in range(20):
        if not os.path.isfile(f):
            break
        try:
            os.remove(f)
            break
        except PermissionError:
            time.sleep(0.1)
            gc.collect()
    else:
        print("note: could not remove %s (still open)" % f)

print()
if tightest:
    print("Tightest step-wrap margin: %.0fpx of room against a %.0fpx "
          "stride (%.0fpx spare) at %s." % (tightest[1], tightest[2],
                                            tightest[0], tightest[3]))
if warnings:
    print("WARNED: %d margin(s) worth watching:" % len(warnings))
    for w in warnings:
        print("  -", w)
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
