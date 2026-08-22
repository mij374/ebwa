"""The motion context every browser check runs in.

The stylesheet's first rule block turns `html{scroll-behavior:smooth}`
off under `prefers-reduced-motion: reduce`, along with every animation
and transition on the site. That makes the reduced-motion context the
one a measurement can be trusted in:

  * with smooth scrolling ON, `window.scrollTo` ANIMATES, so anything
    measured before it lands is measured against a page that is still
    moving. That is what made the cookie-notice check read a fixed strip
    at the bottom of the VIEWPORT as overlapping a footer still below
    the fold, at all six widths.
  * with transitions ON, an element's computed opacity or transform is
    whatever the animation had reached when the check looked, so a
    check has to guess at a wait long enough to have finished.

So STILL is the default for every browser check, passed through the
helpers below rather than left to Chromium's default.

MOVING is for the handful of checks that are testing the MOTION itself —
the partners marquee, whose drift, stepping and arrow-swapping are the
behaviour under test. Those must ask for it AT THE CHECK, because the
distinction is the point: a marquee check inheriting a still context
would pass while measuring a row that never moved.

Set per context and not with a launch flag (`--force-prefers-reduced-
motion`) deliberately: a launch flag is the whole browser, so the
motion-enabled checks could not opt out of it, and the choice would stop
being visible at the check that depends on it.
"""

STILL = "reduce"            # the default: nothing animates, nothing eases
MOVING = "no-preference"    # explicit, for checks measuring motion itself


def new_page(browser, width, height=900, motion=STILL, **options):
    """A page at `width`, still unless a check asks for MOVING."""
    return browser.new_page(viewport={"width": width, "height": height},
                            reduced_motion=motion, **options)


def new_context(browser, width, height=900, motion=STILL, **options):
    """A context at `width`, still unless a check asks for MOVING.

    `options` goes straight to Playwright, for the checks that need
    something else of the context as well — `has_touch=True` for the
    swipe check, say. The motion argument stays separate and explicit;
    it is the one every check has to think about.
    """
    return browser.new_context(viewport={"width": width, "height": height},
                               reduced_motion=motion, **options)
