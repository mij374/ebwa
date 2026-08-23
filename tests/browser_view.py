"""What the browser checks look at: real screen sizes, and reachability.

Two things live here because the same bug taught both of them.

VIEWPORTS — every browser check ran at 900px TALL, because that was the
default height in browser_motion and nobody ever passed one. A width
without a height is not a screen, and a whole class of bug is invisible
without one: the open mobile menu is ~720px with its groups expanded, so
it fits 900 comfortably and fits no phone at all. It ran off the bottom
of a Galaxy S10 with Donate on the lost part, and no check could have
caught it. Every size below is a real device or window — the height
belongs to the width, never made up to go with it.

`unreachable()` — asking whether a person can use something by comparing
its rectangle to the viewport answers a different question. An element
can be entirely "in view" and still untappable because something is
painted over it: the cookie notice is a FIXED strip 190px tall on a
phone, and it sat over the last items of the open menu, Donate among
them. Every tap landed on the notice. `elementFromPoint` asks the
browser what is actually at that spot, which is the question.

Use `unreachable()` for anything a visitor has to reach. Keep plain
rectangle assertions for what they are genuinely about — whether the
page scrolls sideways, whether a panel fits the window, how wide a
column is.
"""

# (width, height) pairs, each a real screen rather than a width with a
# convenient height stuck on it:
#   1440x900   a laptop, the common desktop case
#   1280x800   a smaller laptop
#   1024x768   a small window, and the classic tablet landscape
#   900x700    a short desktop window — 900 straddles the 899px nav shed
#              point, and the short height is where a tall thing shows
#   768x1024   tablet portrait, which is where the 768 width came from
#   390x740    iPhone 12/13/14 once the browser's chrome is showing
#   360x640    Galaxy S10, ditto — the phone the menu bug was found on
VIEWPORTS = [
    (1440, 900),
    (1280, 800),
    (1024, 768),
    (900, 700),
    (768, 1024),
    (390, 740),
    (360, 640),
]

# Widths alone, for the few places that genuinely index by width (the
# gallery masonry's expected column count, say). Deriving it here rather
# than keeping a second list is the point: they cannot drift apart.
WIDTHS = [w for w, _h in VIEWPORTS]

# The phone sizes, for checks about things that grow DOWN the screen.
# 360 appears at both its heights on purpose: 740 is a Galaxy S10 with
# the browser chrome hidden and 640 is the same phone with it showing,
# and the menu overflowed at both — by 58px and 158px.
PHONES = [(390, 740), (360, 740), (360, 640)]

# The two phones the partner row's duplicate-cards bug was reported
# across: a Galaxy S10 showing ten cards where a Note 10 showed five.
FALLBACK_PHONES = [(360, 640), (412, 740)]

# Widths a check uses that are not in VIEWPORTS still get a REAL height,
# not a convenient one: 412 is a Galaxy Note 10 with its chrome showing.
EXTRA_HEIGHTS = {412: 740}


def height_for(width):
    """The height the real screen at this width actually has.

    Raises rather than guessing. A width nobody has written a height for
    is a width nobody has thought about, and quietly handing back 900
    is how every check ended up on a screen taller than any phone.
    """
    for w, h in VIEWPORTS:
        if w == width:
            return h
    if width in EXTRA_HEIGHTS:
        return EXTRA_HEIGHTS[width]
    raise KeyError(
        "no real screen height recorded for %dpx wide — add it to "
        "VIEWPORTS or EXTRA_HEIGHTS in tests/browser_view.py, naming the "
        "device, rather than passing a height at the call site" % width)


# One element: is it where a finger could land on it?
#
#   * `scroll` first, because something inside its own scrolling box (the
#     open mobile menu) is reachable by scrolling to it, and that counts;
#   * pointer-events:none is SKIPPED, not failed. The three nav group
#     triggers decline pointer events deliberately — each group's page is
#     listed underneath it — so a tap falling through them is correct;
#   * the point tested is the centre of a LINE BOX, not of the union
#     rectangle. A link that wraps onto two lines has a bounding rect
#     spanning both, and that rect's centre lands in the whitespace
#     beside the text, where the parent paragraph is the hit target —
#     the privacy link inside the cookie notice reported itself covered
#     by its own <p> at five widths that way. getClientRects() gives the
#     line boxes a finger actually aims at; a block element has exactly
#     one and nothing changes for it;
#   * an element counts as reachable if ANY of its line boxes is: half a
#     wrapped link below the fold still leaves the other half tappable.
REACHABLE_JS = """([sel, scroll]) => {
    const bad = [];
    for (const el of document.querySelectorAll(sel)) {
        const cs = getComputedStyle(el);
        if (cs.pointerEvents === 'none') continue;
        if (cs.display === 'none' || cs.visibility === 'hidden') {
            bad.push((el.getAttribute('href') || el.textContent.trim()
                      || el.tagName) + ' (not rendered)');
            continue;
        }
        if (scroll) el.scrollIntoView({block: 'center', behavior: 'instant'});
        const name = (el.getAttribute('href') || el.getAttribute('aria-label')
                      || el.textContent.trim().slice(0, 20) || el.tagName)
                     .replace(/[^\\x20-\\x7e]/g, '?');
        const boxes = [...el.getClientRects()]
            .filter(b => b.width >= 1 && b.height >= 1);
        if (!boxes.length) {
            bad.push(name + ' (no box)');
            continue;
        }
        const shown = boxes.filter(b => b.top >= 0 && b.bottom <= innerHeight
                                     && b.left >= 0 && b.right <= innerWidth);
        if (!shown.length) {
            bad.push(name + ' (off screen)');
            continue;
        }
        shown.sort((a, b) => b.width * b.height - a.width * a.height);
        const r = shown[0];
        const x = Math.round(r.left + r.width / 2);
        const y = Math.round(r.top + r.height / 2);
        const hit = document.elementFromPoint(x, y);
        if (!hit || !(hit === el || el.contains(hit))) {
            const over = hit ? (hit.className && hit.className.toString
                                ? hit.className.toString().trim()
                                : '') || hit.tagName
                             : 'nothing';
            bad.push(name + ' (covered by ' + over + ')');
        }
    }
    return bad;
}"""


def unreachable(page, selector, scroll=True):
    """Everything matching `selector` that a person could not tap.

    Returns a list of short descriptions — empty when all is well, so it
    reads as `check("...", not unreachable(page, sel))` and prints what
    was in the way when it is not.
    """
    return page.evaluate(REACHABLE_JS, [selector, scroll])
