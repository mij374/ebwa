"""Drive the scrolling rows in real Chromium (Playwright).

TWO rows share one marquee — the partners, and the testimonials above
them — so this file drives whichever row ROW names, and the run does
both in turn. A parallel file for the second row would have drifted from
this one inside a month, and the point of the shared implementation is
that a fix to the loop or the arrows lands on both.

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

Run:  python tests/check_marquees.py [--row NAME] [--shots DIR]
"""
import gc
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_marquee_%s.db"
                       % (sys.argv[sys.argv.index("--row") + 1]
                          if "--row" in sys.argv else "all"))
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from werkzeug.serving import make_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402

from browser_motion import MOVING, STILL, new_context  # noqa: E402
from browser_view import (FALLBACK_PHONES, VIEWPORTS,  # noqa: E402
                          height_for, unreachable)

from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,  # noqa: E402
                 FeatureFlag, MOTION_ROWS, PARTNER_DRIFT_DEFAULT,
                 PARTNER_GLIDE_DEFAULT,
                 Partner, Testimonial)

# The same screens as the header check, from tests/browser_view.py, for
# the same reason: 900 and 768 straddle the 899px shed point, and the
# phone sizes are below the 640px one where the arrows move UNDER the
# row. Heights come with them — the row itself does not grow downwards,
# but the arrows below it and the cookie notice below THEM do, and a
# check that measured only widths could not see either.
# The first count that tips a row into the scroller is the tightest
# case for the loop invariant, because it is the narrowest set. Five
# partners; four quotes, a quote card being far wider than a logo tile.
COUNTS = [5, 7, 9]
QUOTE_COUNTS = [4, 6, 8]
STEP_SECONDS = 2          # long enough to watch a step land and stop
# Section H's screens: the two phones the duplicate-cards bug was
# reported across — a Galaxy S10 showing ten cards where a Note 10
# showed five — named in tests/browser_view.py with their real heights.
FALLBACK_WIDTHS = [w for w, _h in FALLBACK_PHONES]

# ---- the row under test.
# One row per run, chosen with --row, and with no --row the file runs
# itself once for each: the checks below are a linear script against one
# database and one server, and re-entering them for a second row would
# mean threading the whole file through a function for no gain. A
# subprocess each gives every row a clean database, a clean server and
# its own exit code.
ROWS = {
    "partners": {"counts": COUNTS, "min": 5, "seed": "partners"},
    "testimonials": {"counts": QUOTE_COUNTS, "min": 4,
                     "seed": "testimonials"},
}
if "--row" in sys.argv:
    ROW_NAME = sys.argv[sys.argv.index("--row") + 1]
else:
    import subprocess
    print("Two rows share this marquee; running both.\n")
    worst = 0
    for _name in ROWS:
        print("=" * 70)
        print("ROW: %s" % _name)
        print("=" * 70)
        worst = max(worst, subprocess.call(
            [sys.executable, os.path.abspath(__file__), "--row", _name]
            + [a for a in sys.argv[1:]]))
    sys.exit(worst)
ROW = ROWS[ROW_NAME]
ROW_SEL = '[data-row="%s"]' % ROW_NAME    # scopes every selector below
ROW_BOX = ROW_SEL + " .marquee"
COUNTS = ROW["counts"]

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

PORT = 5088 if ROW_NAME == "partners" else 5089
server = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)
BASE = "http://127.0.0.1:%d" % PORT


def seed(count):
    """`count` cards in the row under test.

    Partner cards carry a real link back at this server, because the
    drag checks assert on whether a click opens the partner and a link
    to the outside world would put a network fetch inside an assertion.
    A quote card has no link — the drag checks skip that half for the
    testimonial row, which is noted where they do.
    """
    with app.app_context():
        Partner.query.delete()
        Testimonial.query.delete()
        for i in range(count):
            if ROW["seed"] == "partners":
                db.session.add(Partner(
                    name="Partner %d" % i, blurb="Working with EBWA",
                    url="%s/?partner=%d" % (BASE, i),
                    display_mode="text", sort=i))
            else:
                db.session.add(Testimonial(
                    name="Person %d" % i, role="Member",
                    quote="Quote number %d about what EBWA does." % i,
                    published=True, sort=i))
        db.session.commit()


def set_motion(mode, seconds=STEP_SECONDS, glide=PARTNER_GLIDE_DEFAULT,
               drift=PARTNER_DRIFT_DEFAULT):
    """Set every movement setting, speeds included.

    The speeds default to the shipped constants, so every check written
    before they existed still runs against the row exactly as it was.
    """
    with app.app_context():
        conf = MOTION_ROWS[ROW_NAME]
        for key, value in ((conf["mode_key"], mode),
                           (conf["step_key"], str(seconds)),
                           (conf["glide_key"], str(glide)),
                           (conf["drift_key"], str(drift))):
            row = Block.query.filter_by(key=key).first()
            row.value = value
        db.session.commit()


# ---------------------------------------------------------------- JS

# Everything the checks need about the row in one round trip.
MEASURE = """() => {
    const box = document.querySelector(window.__row);
    if (!box) return null;
    const row = box.closest('.marquee-row');
    const track = box.querySelector('.marquee-track');
    const set = box.querySelector('.marquee-set');
    const gap = parseFloat(getComputedStyle(track).columnGap) || 0;
    const card = box.querySelector('.marquee-set > *');
    const sets = [...box.querySelectorAll('.marquee-set')];
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
        // Cards actually laid out on the page — a card inside a
        // display:none set has no client rects at all. This is the
        // number a visitor counts, which is the whole point of H.
        cardsRendered: [...box.querySelectorAll('.marquee-set > *')]
            .filter(c => c.getClientRects().length > 0).length,
        scrollbarWidth: getComputedStyle(box).scrollbarWidth,
        snap: getComputedStyle(box).scrollSnapType,
        arrowsHidden: [...row.querySelectorAll('.marquee-arrow')]
            .map(b => b.hidden),
        arrowsDisabled: [...row.querySelectorAll('.marquee-arrow')]
            .map(b => b.disabled)
    };
}"""

# The widest run of empty pixels inside the row's visible strip. This is
# the loop's gap, measured as a reader would see it rather than derived
# from the arithmetic that is supposed to prevent it: clip every card to
# the visible box, sort, and walk the edges. A design gap between two
# cards is normal; anything wider is a hole.
WORST_HOLE = """() => {
    const box = document.querySelector(window.__row);
    const b = box.getBoundingClientRect();
    const spans = [...box.querySelectorAll('.marquee-set > *')]
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

SCROLL_LEFT = "() => document.querySelector(window.__row).scrollLeft"


def show_row(page):
    """Bring the row under test on screen — the drift stops while it is
    not. Tolerates there being no row at all: below the threshold the
    section is a plain grid, and one check opens the page in that state
    deliberately."""
    page.evaluate("""(sel) => {
        const row = document.querySelector(sel);
        if (row) row.scrollIntoView({behavior: 'instant', block: 'center'});
    }""", ROW_SEL)
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
        const box = document.querySelector(window.__row);
        const b = box.getBoundingClientRect();
        const seen = [...box.querySelectorAll('.marquee-set > *')]
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
    """The home page at `width`, on the screen that width really is.

    The height is looked up rather than defaulted — see
    tests/browser_view.py. Every check here used to run 900px tall,
    which is taller than any phone this row is scrolled on.
    """
    ctx = new_context(browser, width, height_for(width), motion=motion,
                      **options)
    # Every JS snippet in this file says document.querySelector(
    # window.__row); this is where that is answered, once per context.
    ctx.add_init_script("window.__row = '%s';" % ROW_BOX)
    ctx.add_init_script("window.__row = '%s';" % ROW_BOX)
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
        for width, _height in VIEWPORTS:
            ctx, page = open_home(browser, width, MOVING)
            tag = "scroll %d %s %dpx" % (count, ROW_NAME, width)
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
            # The last REAL card, or — for a row whose cards are not
            # focusable, like quotes — the scroller itself, which
            # carries tabindex for exactly this reason.
            page.evaluate("""(sel) => {
                const real = document.querySelector(
                    sel + ' .marquee-set:not([aria-hidden]) > *:last-child');
                const target = (real && real.tabIndex >= 0) ? real
                    : document.querySelector(window.__row);
                target.focus();
            }""", ROW_SEL)
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
            "(was) => Math.abs(document.querySelector(window.__row)"
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

    page.evaluate("""(sel) => {
        const real = document.querySelector(
            sel + ' .marquee-set:not([aria-hidden]) > *');
        ((real && real.tabIndex >= 0) ? real
            : document.querySelector(window.__row)).focus();
    }""", ROW_SEL)
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

        # Far enough to survive the SNAP. The row rests on a card
        # start, so a drag shorter than half a stride is pulled back
        # where it came from — which is snapping working, not the drag
        # failing. 180px was fine for a 278px partner stride and lands
        # short of half a 378px quote stride, so take it from the row.
        before = page.evaluate(SCROLL_LEFT)
        pull = -int(page.evaluate(MEASURE)["cardStride"] * 0.8) or -180
        try:
            with page.expect_popup(timeout=900) as popped:
                drag(page, point, pull)
            opened = popped.value is not None
        except PWTimeout:
            opened = False
        after = settle(page)
        check("%s: a drag scrolls the row" % tag, after - before > 40,
              "moved %.0f after a %dpx pull" % (after - before, pull))
        check("%s: a drag does NOT open the card" % tag, not opened)

        # A hand is never perfectly still: under the threshold it is a
        # click, and the link must open. This is the case that broke
        # when the pointer was captured on pointerdown — the click
        # retargeted to the row and the partner never opened.
        # Only a row whose cards are LINKS can be asked this: the
        # thing being tested is that the click reaches the card rather
        # than being swallowed by the row, and a quote card has nothing
        # to click through to. The rest of the drag behaviour above
        # applies to both rows and is checked for both.
        if ROW_NAME == "partners":
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
        else:
            # ...but a wobble must still not RUN AWAY with the row.
            point = card_point(page)
            steady = page.evaluate(SCROLL_LEFT)
            drag(page, point, -3, steps=2)
            page.wait_for_timeout(200)
            check("%s: a small wobble does not shift the row" % tag,
                  abs(page.evaluate(SCROLL_LEFT) - steady) <= 2,
                  "moved %.1f" % (page.evaluate(SCROLL_LEFT) - steady))
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
            '.marquee .marquee-set > *{flex:0 0 20px;width:20px}';
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
    page.evaluate("() => { document.querySelector(window.__row)"
                  ".scrollLeft = 0; }")
    page.wait_for_timeout(120)
    m = page.evaluate(MEASURE)
    check("arrows: previous is disabled at the left end",
          m["arrowsDisabled"][0] and not m["arrowsDisabled"][1],
          str(m["arrowsDisabled"]))
    page.evaluate("""() => { const b = document.querySelector(window.__row);
        b.scrollLeft = b.scrollWidth; }""")
    page.wait_for_timeout(120)
    m = page.evaluate(MEASURE)
    check("arrows: next is disabled at the right end",
          m["arrowsDisabled"][1] and not m["arrowsDisabled"][0],
          str(m["arrowsDisabled"]))

    # ---- one card per press, from the keyboard alone
    page.evaluate("() => { document.querySelector(window.__row)"
                  ".scrollLeft = 0; }")
    page.wait_for_timeout(120)
    m = page.evaluate(MEASURE)
    page.focus(ROW_SEL + ' .marquee-arrow-next')
    check("arrows: the next button takes keyboard focus",
          page.evaluate("() => document.activeElement.className")
          .find("marquee-arrow-next") >= 0)
    page.keyboard.press("Enter")
    landed = settle(page)
    check("arrows: Enter moves the row one card",
          abs(landed - m["cardStride"]) <= 2,
          "moved %.0f, stride %.0f" % (landed, m["cardStride"]))
    page.focus(ROW_SEL + ' .marquee-arrow-prev')
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
        const row = document.querySelector('.marquee-row');
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

    # ================================================================
    # H. FAIL SAFE: whatever stops the row animating, what is left is
    #    the REAL cards and the arrows — never the aria-hidden copies.
    #
    # Reported from a Galaxy S10: ten cards, static, where a Note 10
    # showed five and drifted. Hiding the copy set used to depend on the
    # admin's `none` setting or on a prefers-reduced-motion match, so
    # every OTHER way of not animating — the script blocked, erroring,
    # not run, or an engine too old to move the row the way the script
    # asked — left the same five logos on the page twice.
    #
    # So each case below breaks the script in a different way and asserts
    # the same three things: one set, five cards, arrows showing.
    # ================================================================
    # ---- below the threshold there is no scroller at all
    seed(ROW["min"] - 1)
    ctx, page = open_home(browser, 1024, STILL)
    check("%d %s: a plain grid, no scroller" % (ROW["min"] - 1, ROW_NAME),
          page.evaluate("() => !document.querySelector(window.__row)"))
    ctx.close()
    seed(ROW["min"])
    ctx, page = open_home(browser, 1024, STILL)
    check("%d %s: and at the threshold it becomes one" % (ROW["min"], ROW_NAME),
          page.evaluate("() => !!document.querySelector(window.__row)"))
    ctx.close()

    seed(5)
    for width in FALLBACK_WIDTHS:

        # (a) No JavaScript at all. Nothing adds a class, nothing hides
        #     anything: this is purely what the server sent plus the
        #     stylesheet, which is the state the fix has to be built in.
        set_motion("scroll")
        ctx, page = open_home(browser, width, STILL,
                              java_script_enabled=False)
        tag = "%dpx, no JavaScript" % width
        m = page.evaluate(MEASURE)
        check("%s: one set rendered" % tag, m["setsShown"] == 1,
              str(m["setsShown"]))
        check("%s: five cards, not ten" % tag, m["cardsRendered"] == 5,
              str(m["cardsRendered"]))
        check("%s: the arrows are there" % tag,
              not any(m["arrowsHidden"]), str(m["arrowsHidden"]))
        check("%s: the row is not claiming to move" % tag, not m["moving"])
        check("%s: and its scrollbar is still there to scroll with" % tag,
              m["scrollbarWidth"] != "none", m["scrollbarWidth"])
        check("%s: the page does not scroll sideways" % tag,
              page.evaluate("() => document.documentElement.scrollWidth - "
                            "document.documentElement.clientWidth") <= 0)
        if shots_dir:
            page.screenshot(path=os.path.join(
                shots_dir, "fallback-nojs-%d.png" % width))
        ctx.close()

        # (b) The script runs and throws on the way in. Different cause,
        #     same requirement — and this one leaves the page's other
        #     scripts running, so it is not just "no JS" again.
        ctx = new_context(browser, width, height_for(width),
                          motion=MOVING)
        # matchMedia, because the partner block calls it on its fifth
        # line — and because MEASURE below does not, so the check can
        # still read the page it has just broken.
        ctx.add_init_script("""
            window.matchMedia = function () {
                throw new TypeError('no matchMedia here');
            };""")
        ctx.add_init_script("window.__row = '%s';" % ROW_BOX)
        page = ctx.new_page()
        page.goto(BASE + "/", wait_until="load")
        show_row(page)
        tag = "%dpx, the script throws" % width
        m = page.evaluate(MEASURE)
        check("%s: one set rendered" % tag, m["setsShown"] == 1,
              str(m["setsShown"]))
        check("%s: five cards, not ten" % tag, m["cardsRendered"] == 5,
              str(m["cardsRendered"]))
        check("%s: the arrows are there" % tag,
              not any(m["arrowsHidden"]), str(m["arrowsHidden"]))
        ctx.close()

        # (c) Reduced motion, which the site has always handled — here at
        #     the phone widths, and now holding without the media query
        #     being the thing that hides the copies.
        ctx, page = open_home(browser, width, STILL)
        tag = "%dpx, reduced motion" % width
        m = page.evaluate(MEASURE)
        check("%s: one set rendered" % tag, m["setsShown"] == 1,
              str(m["setsShown"]))
        check("%s: five cards, not ten" % tag, m["cardsRendered"] == 5,
              str(m["cardsRendered"]))
        check("%s: the arrows are there" % tag,
              not any(m["arrowsHidden"]), str(m["arrowsHidden"]))
        ctx.close()

        # (d) The row says it is moving but the offset will not move —
        #     a clamp, a bug, an engine doing something else. .is-moving
        #     is what reveals the copies, so the script has to notice and
        #     take it back off rather than show them for a motion that is
        #     not happening.
        set_motion("scroll")
        ctx = new_context(browser, width, height_for(width),
                          motion=MOVING)
        ctx.add_init_script("""
            Object.defineProperty(Element.prototype, 'scrollLeft', {
                get: function () { return 0; },
                set: function () {},
                configurable: true});""")
        ctx.add_init_script("window.__row = '%s';" % ROW_BOX)
        page = ctx.new_page()
        page.goto(BASE + "/", wait_until="load")
        show_row(page)
        page.wait_for_timeout(1500)      # 30 still frames is half a second
        tag = "%dpx, a drift that cannot move" % width
        m = page.evaluate(MEASURE)
        check("%s: the row stops claiming to move" % tag, not m["moving"])
        check("%s: one set rendered" % tag, m["setsShown"] == 1,
              str(m["setsShown"]))
        check("%s: five cards, not ten" % tag, m["cardsRendered"] == 5,
              str(m["cardsRendered"]))
        check("%s: and the arrows come back" % tag,
              not any(m["arrowsHidden"]), str(m["arrowsHidden"]))
        ctx.close()

    # (e) An engine without Element.scrollBy — Chromium below 61, which
    #     is Samsung Internet 7 and earlier. Stepping and the arrows were
    #     the only two things that went through it, so on such a phone a
    #     stepping row wore .is-moving and never moved. It should step
    #     anyway now, on the offset every other part of this uses.
    set_motion("step")
    ctx = new_context(browser, 360, height_for(360), motion=MOVING)
    ctx.add_init_script("delete Element.prototype.scrollBy;")
    ctx.add_init_script("window.__row = '%s';" % ROW_BOX)
    page = ctx.new_page()
    page.goto(BASE + "/", wait_until="load")
    show_row(page)
    m = page.evaluate(MEASURE)
    check("no Element.scrollBy: the row has no scrollBy to call",
          page.evaluate(
              "() => !document.querySelector(window.__row).scrollBy"))
    page.wait_for_timeout(STEP_SECONDS * 1000 + 400)
    landed = settle(page)
    check("no Element.scrollBy: it steps one card anyway",
          abs(landed - m["cardStride"]) <= 3,
          "moved %.0f, stride %.0f" % (landed, m["cardStride"]))
    check("no Element.scrollBy: so it is genuinely moving, copies and all",
          page.evaluate(MEASURE)["setsShown"] == 2)
    ctx.close()

    # (f) The phone itself: at 360 five partners overflow, so the two
    #     ways of moving a row without a scrollbar both have to work.
    set_motion("none")
    ctx, page = open_home(browser, 360, STILL, has_touch=True)
    m = page.evaluate(MEASURE)
    check("360px: five partners overflow the row",
          m["scrollWidth"] > m["clientWidth"] + 1,
          "%d vs %d" % (m["scrollWidth"], m["clientWidth"]))
    check("360px: so the arrows are showing", not any(m["arrowsHidden"]),
          str(m["arrowsHidden"]))
    check("360px: only the real cards are in that overflow",
          m["cardsRendered"] == 5, str(m["cardsRendered"]))
    arrow = page.locator(ROW_SEL + ' .marquee-arrow-next').bounding_box()
    row = page.locator(ROW_SEL + ' .marquee').bounding_box()
    check("360px: the arrows sit under the row, not beside it",
          arrow["y"] >= row["y"] + row["height"] - 2,
          "arrow at %.0f, row ends %.0f" % (arrow["y"],
                                            row["y"] + row["height"]))
    check("360px: and are a fair size to hit",
          arrow["width"] >= 40 and arrow["height"] >= 40,
          "%.0fx%.0f" % (arrow["width"], arrow["height"]))
    # Not the same as "not hidden": on a phone the arrows sit under the
    # row, which is where the cookie notice is too. Ask the browser what
    # is actually at each button rather than trusting the rectangle.
    blocked = unreachable(page, ".marquee-arrow", scroll=False)
    check("360px: and nothing is painted over them", not blocked,
          str(blocked))
    page.click(ROW_SEL + ' .marquee-arrow-next')
    landed = settle(page)
    check("360px: the next arrow moves the row one card",
          abs(landed - m["cardStride"]) <= 3,
          "moved %.0f, stride %.0f" % (landed, m["cardStride"]))
    point = card_point(page)
    before = page.evaluate(SCROLL_LEFT)
    cdp = ctx.new_cdp_session(page)
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
    check("360px: and a swipe still pans it", after - before > 40,
          "moved %.0f" % (after - before))
    if shots_dir:
        page.screenshot(path=os.path.join(shots_dir, "fallback-360.png"))
    ctx.close()

    # ================================================================
    # I. The two SPEED settings. Both ship at the value the row already
    #    used, so the first thing asserted is that a default-configured
    #    row behaves as it did before they existed — a setting that
    #    changes the site the day it is added is not a setting, it is a
    #    redesign.
    # ================================================================
    seed(5)

    def time_one_step(page):
        """How long one step takes, from first movement to settled."""
        return page.evaluate("""() => new Promise(resolve => {
            const box = document.querySelector(window.__row);
            const from = box.scrollLeft;
            let t0 = 0, last = from, still = 0;
            const started = performance.now();
            function tick(now) {
                const at = box.scrollLeft;
                if (!t0 && Math.abs(at - from) > 0.5) t0 = now;
                if (t0) {
                    if (Math.abs(at - last) < 0.05) {
                        if (++still > 3) {
                            resolve({ms: Math.round(now - t0 - 48),
                                     moved: Math.round(at - from)});
                            return;
                        }
                    } else { still = 0; }
                }
                last = at;
                if (now - started > 12000) {
                    resolve({ms: -1, moved: Math.round(at - from)});
                    return;
                }
                requestAnimationFrame(tick);
            }
            requestAnimationFrame(tick);
        })""")

    def drift_over(page, ms=1200):
        """Pixels the row drifts in `ms` of wall clock."""
        return page.evaluate("""(ms) => new Promise(resolve => {
            const box = document.querySelector(window.__row);
            const from = box.scrollLeft;
            const t0 = performance.now();
            setTimeout(() => resolve({
                moved: box.scrollLeft - from,
                seconds: (performance.now() - t0) / 1000}), ms);
        })""", ms)

    # ---- the default step is the ~360ms the browser's own smooth
    # scroll was taking, measured at 344-374ms before any of this.
    set_motion("step", seconds=4)
    ctx, page = open_home(browser, 1024, MOVING)
    took = time_one_step(page)
    check("default glide: a step still takes about %dms"
          % PARTNER_GLIDE_DEFAULT,
          abs(took["ms"] - PARTNER_GLIDE_DEFAULT) <= 160,
          "%dms, moved %dpx" % (took["ms"], took["moved"]))
    check("default glide: and it is a whole card",
          abs(took["moved"] - page.evaluate(MEASURE)["cardStride"]) <= 3,
          str(took))
    ctx.close()

    # ---- the default drift is the 45px a second it always was
    set_motion("scroll")
    ctx, page = open_home(browser, 1024, MOVING)
    d = drift_over(page)
    rate = d["moved"] / d["seconds"]
    check("default drift: still about %d pixels a second"
          % PARTNER_DRIFT_DEFAULT, abs(rate - PARTNER_DRIFT_DEFAULT) <= 12,
          "%.0f px/s over %.2fs" % (rate, d["seconds"]))
    ctx.close()

    # ---- changed values take effect, and proportionally
    set_motion("step", seconds=4, glide=1200)
    ctx, page = open_home(browser, 1024, MOVING)
    slow = time_one_step(page)
    check("a longer glide really is longer",
          abs(slow["ms"] - 1200) <= 220, "%dms" % slow["ms"])
    check("and still lands on exactly one card",
          abs(slow["moved"] - page.evaluate(MEASURE)["cardStride"]) <= 3,
          str(slow))
    ctx.close()

    set_motion("scroll", drift=90)
    ctx, page = open_home(browser, 1024, MOVING)
    d = drift_over(page)
    fast = d["moved"] / d["seconds"]
    check("double the drift setting is double the speed",
          abs(fast - 90) <= 20, "%.0f px/s" % fast)
    ctx.close()

    # ---- reduced motion still overrides both, whatever they are set to
    set_motion("scroll", drift=200)
    ctx, page = open_home(browser, 1024, STILL)
    at = page.evaluate(SCROLL_LEFT)
    page.wait_for_timeout(900)
    check("reduced motion still stops the fastest drift there is",
          abs(page.evaluate(SCROLL_LEFT) - at) <= 1,
          "moved %.1f" % (page.evaluate(SCROLL_LEFT) - at))
    check("and still gets the arrows instead",
          not any(page.evaluate(MEASURE)["arrowsHidden"]))
    ctx.close()
    set_motion("step", seconds=1, glide=3000)
    ctx, page = open_home(browser, 1024, STILL)
    at = page.evaluate(SCROLL_LEFT)
    page.wait_for_timeout(1200)
    check("reduced motion stops a glide longer than its own interval too",
          abs(page.evaluate(SCROLL_LEFT) - at) <= 1,
          "moved %.1f" % (page.evaluate(SCROLL_LEFT) - at))
    ctx.close()

    # ---- an impossible pair cannot overlap two movements. The form
    # refuses it; this is the row rendering one anyway, because a
    # database can hold anything a hand edit puts in it.
    set_motion("step", seconds=1, glide=3000)
    ctx, page = open_home(browser, 1024, MOVING)
    capped = page.evaluate(
        "() => document.querySelector('.marquee-row')"
        ".getAttribute('data-glide-ms')")
    check("a glide longer than the interval is capped to it",
          capped == "1000", str(capped))
    # Watch it for several intervals: with two movements overlapping the
    # row would jump backwards, so assert it only ever goes forwards.
    backwards = page.evaluate("""() => new Promise(resolve => {
        const box = document.querySelector(window.__row);
        let last = box.scrollLeft, worst = 0, wrapped = 0;
        const t0 = performance.now();
        function tick(now) {
            const at = box.scrollLeft;
            const d = at - last;
            // A wrap back by one set is the loop, not a stumble.
            if (d < -50) { wrapped++; } else if (d < worst) { worst = d; }
            last = at;
            if (now - t0 > 4000) { resolve({worst: worst, wrapped: wrapped}); return; }
            requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);
    })""")
    check("and the row never stumbles backwards while stepping",
          backwards["worst"] > -2, str(backwards))
    ctx.close()

    set_motion("scroll")      # leave the row as the site ships it

    # ================================================================
    # J. AT REST, NO CARD IS SLICED.
    #
    # A stopped row showing two thirds of a card at its right edge looks
    # like a mistake rather than a design. Step mode and the
    # no-movement setting are both rest states, so both have to settle
    # on a boundary; a DRIFTING row is excluded, because a part-card at
    # the edge is what says it is moving.
    #
    # "Sliced" is measured as a reader sees it: any card whose box
    # crosses either edge of the strip while any part of it is inside.
    # ================================================================
    SLICED = """() => {
        const box = document.querySelector(window.__row);
        const b = box.getBoundingClientRect();
        const cut = [];
        let whole = 0;
        for (const c of box.querySelectorAll('.marquee-set > *')) {
            const r = c.getBoundingClientRect();
            const inside = r.right > b.left + 0.5 && r.left < b.right - 0.5;
            if (!inside) continue;
            if (r.left < b.left - 0.5 || r.right > b.right + 0.5) {
                cut.push(Math.round(Math.min(r.right, b.right)
                                    - Math.max(r.left, b.left)));
            } else {
                whole++;
            }
        }
        const cs = getComputedStyle(box);
        return {sliced: cut, whole: whole,
                pad: Math.round(parseFloat(cs.paddingLeft) || 0),
                strip: Math.round(b.width),
                card: Math.round(box.querySelector('.marquee-set > *')
                                 .getBoundingClientRect().width)};
    }"""

    for count in ROW["counts"]:
        seed(count)
        for mode, motion, label in (("step", MOVING, "stepping"),
                                    ("none", MOVING, "no-movement"),
                                    ("scroll", STILL, "reduced motion")):
            set_motion(mode, seconds=STEP_SECONDS)
            for width, height in VIEWPORTS:
                ctx = new_context(browser, width, height, motion=motion)
                ctx.add_init_script("window.__row = '%s';" % ROW_BOX)
                page = ctx.new_page()
                page.goto(BASE + "/", wait_until="load")
                show_row(page)
                tag = "rest %s %d %s %dx%d" % (label, count, ROW_NAME,
                                               width, height)
                if mode == "step":
                    # Let one step land, so this is a rest position the
                    # row actually arrives at rather than its first one.
                    page.wait_for_timeout(STEP_SECONDS * 1000 + 500)
                    settle(page)
                at = page.evaluate(SLICED)
                check("%s: no card is sliced" % tag, not at["sliced"],
                      "%s of %spx card, strip %s, pad %s"
                      % (at["sliced"], at["card"], at["strip"], at["pad"]))
                check("%s: and at least one whole card shows" % tag,
                      at["whole"] >= 1, str(at))
                if mode == "step":
                    # The step wrap needs a whole stride of room past
                    # the end of the first set. Fitting makes that
                    # structural rather than a measured margin: with n
                    # whole cards visible out of N, the room is exactly
                    # (N - n) strides, so it cannot go negative while
                    # any card is off screen. Asserted, not assumed.
                    room = page.evaluate("""() => {
                        const box = document.querySelector(window.__row);
                        const set = box.querySelector('.marquee-set');
                        const cs = getComputedStyle(box);
                        const gap = parseFloat(getComputedStyle(
                            box.querySelector('.marquee-track')).columnGap) || 0;
                        const card = box.querySelector('.marquee-set > *')
                            .getBoundingClientRect().width;
                        const pad = (parseFloat(cs.paddingLeft) || 0)
                                  + (parseFloat(cs.paddingRight) || 0);
                        return {room: set.getBoundingClientRect().width
                                      - (box.clientWidth - pad),
                                stride: card + gap};
                    }""")
                    check("%s: a whole stride of room for the wrap" % tag,
                          room["room"] >= room["stride"] - 1
                          or room["room"] <= 1,
                          "%.0f of room against a %.0f stride"
                          % (room["room"], room["stride"]))
                # The row must FILL its width rather than sit in a pool
                # of nothing: whatever the growth could not absorb is
                # split evenly, and half of it is the most a side gets.
                check("%s: the leftover is split evenly, not left at the "
                      "end" % tag,
                      at["pad"] * 2 <= at["strip"] - at["whole"] * at["card"]
                      + 2, str(at))
                ctx.close()

    # ---- an arrow press lands on a boundary too
    set_motion("none")
    seed(ROW["counts"][-1])
    for width, height in ((1440, 900), (1024, 768), (390, 740), (360, 640)):
        ctx = new_context(browser, width, height, motion=STILL)
        ctx.add_init_script("window.__row = '%s';" % ROW_BOX)
        page = ctx.new_page()
        page.goto(BASE + "/", wait_until="load")
        show_row(page)
        tag = "rest arrows %s %dx%d" % (ROW_NAME, width, height)
        for press in range(3):
            page.click(ROW_SEL + ' .marquee-arrow-next')
            settle(page)
            at = page.evaluate(SLICED)
            check("%s: press %d lands on a boundary" % (tag, press + 1),
                  not at["sliced"], "%s" % at)
        ctx.close()

    # ---- and a DRIFTING row is left alone, on purpose
    set_motion("scroll")
    ctx, page = open_home(browser, 1024, MOVING)
    drift = page.evaluate("""() => {
        const box = document.querySelector(window.__row);
        const cs = getComputedStyle(box);
        return {pad: Math.round(parseFloat(cs.paddingLeft) || 0),
                card: Math.round(box.querySelector('.marquee-set > *')
                                 .getBoundingClientRect().width),
                base: parseFloat(getComputedStyle(
                    box.closest('.marquee-row'))
                    .getPropertyValue('--marquee-card-base'))};
    }""")
    check("drifting: the card keeps its designed width",
          abs(drift["card"] - drift["base"]) <= 1, str(drift))
    check("drifting: and no padding is added to line it up",
          drift["pad"] == 0, str(drift))
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
