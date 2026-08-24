"""Every number input, driven the way a person drives one (Playwright).

A `step` is not decoration: it decides what the arrows do AND what the
browser will let through. The two interact in a way that is easy to get
wrong — the step BASE is the `min` attribute, so `min="300" step="50"`
makes 300, 350, 400 ... valid and 360 invalid, and a form nobody has
touched refuses to submit. That trap is asserted below rather than
remembered.

So for each field this presses the up arrow and measures what moved,
types an off-step value and asks the browser whether it would submit,
and checks that min/max match the range the ROUTE enforces — an arrow
that runs past a server bound produces a rejection the visitor could
have been stopped from reaching.

Money fields are the exception to all of it: they must accept £12.50, so
they step by a penny, so their spinner arrows are a control that looks
useful and is not. The arrows are hidden there and the quick-pick
buttons take their place — so this also asserts that hiding them cost
nothing, which is the whole question: the range is still validated, the
value is still a number, and a phone still gets the numeric keypad.

Run:  python tests/check_number_inputs.py
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_numbers.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from werkzeug.serving import make_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from browser_motion import STILL, new_context  # noqa: E402
from browser_view import VIEWPORTS, height_for  # noqa: E402

from app import (app, db, Block, Campaign, DEFAULT_BLOCKS,  # noqa: E402
                 FEATURES, FeatureFlag, PARTNER_DRIFT_DEFAULT,
                 PARTNER_GLIDE_DEFAULT, Partner, User)

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    db.create_all()
    for g, k, l, kd, v in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=k).first():
            db.session.add(Block(group=g, key=k, label=l, kind=kd, value=v))
    for n, _l, _d, d in FEATURES:
        if not FeatureFlag.query.filter_by(name=n).first():
            db.session.add(FeatureFlag(name=n, enabled=d))
    db.session.add(User(email="netbus@example.com",
                        password_hash=generate_password_hash("pw123456"),
                        role="super_admin"))
    for i in range(5):
        db.session.add(Partner(name="Partner %d" % i, display_mode="text",
                               sort=i))
    # state="open" explicitly: this fixture exists to render the payment
    # form, and a closed or hidden collection has no form to measure.
    db.session.add(Campaign(title="Seaside trip", slug="seaside-trip",
                            description="A day out.", state="open",
                            fee_pence=1200, target_pence=200000))
    db.session.commit()

server = make_server("127.0.0.1", 5121, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)
BASE = "http://127.0.0.1:5121"

# What the browser thinks of a field: the attributes it will act on, and
# whether it would let the form through as it stands.
STATE = """(sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    return {type: el.type, value: el.value,
            min: el.getAttribute('min'), max: el.getAttribute('max'),
            step: el.getAttribute('step'),
            valid: el.checkValidity(),
            message: el.validationMessage};
}"""


def state(page, sel):
    return page.evaluate(STATE, sel)


def arrow_moves(page, sel, presses=1):
    """What one press of the up arrow actually does to the value."""
    before = float(page.evaluate("(s) => document.querySelector(s).value",
                                 sel) or 0)
    page.focus(sel)
    for _ in range(presses):
        page.keyboard.press("ArrowUp")
    after = float(page.evaluate("(s) => document.querySelector(s).value",
                                sel) or 0)
    return round(after - before, 2)


SPINNER = """(sel) => {
    const el = document.querySelector(sel);
    return {appearance: getComputedStyle(el).appearance,
            type: el.type, step: el.getAttribute('step')};
}"""


def spinner(page, sel):
    """Whether this field would be painted with spin buttons.

    Measured as the input's computed `appearance`, and it has to be:
    HEADLESS CHROMIUM DRAWS NO SPIN BUTTON ON ANY NUMBER INPUT, so
    clicking where the arrows would be changes nothing whether they are
    suppressed or not — a behavioural check here would pass for both and
    prove neither (measured: a plain number input and a suppressed one
    both ignored a click at their top-right corner).

    What does differ, and is exactly what the stylesheet sets, is
    `appearance`: `auto` on a field that gets spin buttons, `textfield`
    on one that does not. That is the standard property for this — the
    ::-webkit-*-spin-button rules beside it are the older spelling, kept
    for WebKit versions that only understand that one.
    """
    return page.evaluate(SPINNER, sel)


SPUN, UNSPUN = "auto", "textfield"


def would_accept(page, sel, typed):
    """Type a value and ask the browser whether it would submit it."""
    page.fill(sel, "")
    page.fill(sel, typed)
    return page.evaluate(STATE, sel)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = new_context(browser, 1280, height_for(1280), motion=STILL)
    page = ctx.new_page()

    # ---------------------------------------------------------- money
    page.goto(BASE + "/donate", wait_until="load")
    s = state(page, "#amount")
    check("donate: the amount is a number field", s["type"] == "number",
          str(s))
    check("donate: it steps by a penny, so £12.50 can be typed",
          s["step"] == "0.01", str(s["step"]))
    check("donate: min 1 and max 10000 mirror the route's £1-£10,000",
          s["min"] == "1" and s["max"] == "10000", str(s))
    check("donate: a decimal amount is accepted",
          would_accept(page, "#amount", "12.50")["valid"])
    check("donate: 12.5 is not rejected as off-step",
          "step" not in would_accept(page, "#amount", "12.50")["message"],
          would_accept(page, "#amount", "12.50")["message"])
    zero = would_accept(page, "#amount", "0")
    check("donate: zero is refused before it reaches the server",
          not zero["valid"], str(zero))
    check("donate: and the browser says what the smallest is",
          "1" in zero["message"], zero["message"])
    neg = would_accept(page, "#amount", "-20")
    check("donate: a negative amount is refused too", not neg["valid"],
          str(neg))
    over = would_accept(page, "#amount", "10001")
    check("donate: over £10,000 is refused, as the route would",
          not over["valid"], str(over))
    # ---- the spinner is gone, and nothing went with it
    sp = spinner(page, "#amount")
    check("donate: the penny-at-a-time spinner is not drawn",
          sp["appearance"] == UNSPUN, str(sp))
    check("donate: but it is still a number field, so the range still "
          "validates", sp["type"] == "number", str(sp))
    check("donate: and a phone still gets the numeric keypad",
          # type=number is what asks for it; inputMode is left alone so
          # the browser can offer the decimal key it knows this needs.
          sp["type"] == "number", str(sp))
    check("donate: hiding it did not disturb validation",
          would_accept(page, "#amount", "12.50")["valid"]
          and not would_accept(page, "#amount", "0")["valid"]
          and not would_accept(page, "#amount", "-5")["valid"]
          and not would_accept(page, "#amount", "10001")["valid"])
    # The keyboard still steps, deliberately: it is a native affordance
    # with no misleading control attached to it, and taking it away
    # would remove the only way a keyboard user has to nudge a value.
    page.fill("#amount", "25")
    check("donate: arrow keys still nudge the value for keyboard users",
          arrow_moves(page, "#amount") == 0.01,
          str(arrow_moves(page, "#amount")))
    # The wheel does NOT, because that one is silent and unasked for.
    page.fill("#amount", "25")
    page.focus("#amount")
    page.mouse.move(*page.evaluate("""() => {
        const r = document.getElementById('amount').getBoundingClientRect();
        return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
    }"""))
    page.mouse.wheel(0, -240)
    page.wait_for_timeout(120)
    check("donate: a wheel scroll over the focused field changes nothing",
          page.evaluate("() => document.getElementById('amount').value")
          == "25",
          page.evaluate("() => document.getElementById('amount').value"))

    # The quick picks are what this field has instead of useful arrows.
    page.click(".amount-presets [data-amount='25']")
    check("donate: a quick pick fills the amount",
          page.evaluate("() => document.getElementById('amount').value")
          == "25")
    picked = page.evaluate("""() => {
        const on = [...document.querySelectorAll('.amount-presets [data-amount]')]
            .filter(b => b.classList.contains('is-picked'));
        return {count: on.length,
                which: on.map(b => b.getAttribute('data-amount')),
                pressed: on.map(b => b.getAttribute('aria-pressed'))};
    }""")
    check("donate: and the picked button says so, visibly and to a "
          "screen reader",
          picked["count"] == 1 and picked["which"] == ["25"]
          and picked["pressed"] == ["true"], str(picked))
    page.fill("#amount", "40")
    check("donate: typing an amount clears the picked button",
          page.evaluate("""() => document.querySelectorAll(
              '.amount-presets .is-picked').length""") == 0)
    # It has to LOOK like the main control, next to a field that reads
    # as the way round it rather than the way in.
    look = page.evaluate("""() => {
        const b = document.querySelector('.amount-presets [data-amount]');
        const i = document.getElementById('amount');
        const br = b.getBoundingClientRect();
        return {h: br.height, w: br.width,
                above: br.bottom <= i.getBoundingClientRect().top + 1,
                placeholder: i.getAttribute('placeholder')};
    }""")
    check("donate: the quick picks are a fair size to tap",
          look["h"] >= 40 and look["w"] >= 60, str(look))
    check("donate: they come before the box, not after it", look["above"],
          str(look))
    check("donate: and the box reads as the alternative",
          "other" in (look["placeholder"] or "").lower(),
          str(look["placeholder"]))

    page.goto(BASE + "/collections/seaside-trip", wait_until="load")
    s = state(page, "#donation")
    check("collection: the extra donation steps by a penny too",
          s["step"] == "0.01", str(s["step"]))
    check("collection: min 0 — the extra donation is optional",
          s["min"] == "0" and s["max"] == "10000", str(s))
    check("collection: £7.25 is accepted",
          would_accept(page, "#donation", "7.25")["valid"])
    page.fill("#donation", "")
    page.click(".amount-presets [data-amount='25']")
    check("collection: a quick pick fills it",
          page.evaluate("() => document.getElementById('donation').value")
          == "25")
    check("collection: and unlocks the Gift Aid tick-box with it",
          page.evaluate("() => !document.getElementById('giftAidToggle')"
                        ".disabled"))
    sp = spinner(page, "#donation")
    check("collection: the spinner is not drawn here either",
          sp["appearance"] == UNSPUN, str(sp))
    check("collection: still a number field, still validating",
          sp["type"] == "number"
          and would_accept(page, "#donation", "7.25")["valid"]
          and not would_accept(page, "#donation", "-1")["valid"]
          and not would_accept(page, "#donation", "10001")["valid"])
    page.fill("#donation", "")
    page.click(".amount-presets [data-amount='10']")
    check("collection: the picked button shows there too",
          page.evaluate("""() => {
              const on = document.querySelectorAll(
                  '.amount-presets .is-picked');
              return on.length === 1
                  && on[0].getAttribute('data-amount') === '10';
          }"""))

    # ---------------------------------------------------------- admin
    page.goto(BASE + "/admin/login", wait_until="load")
    page.fill("input[name=email]", "netbus@example.com")
    page.fill("input[name=password]", "pw123456")
    page.click("button[type=submit]")
    page.wait_for_load_state("load")

    page.goto(BASE + "/admin/partners", wait_until="load")
    page.click(".admin-advanced summary")
    page.wait_for_timeout(120)

    s = state(page, "#step_seconds")
    check("interval: still steps by 1, which is right for seconds",
          s["step"] in (None, "1"), str(s["step"]))
    check("interval: 1-60 mirrors the route", s["min"] == "1"
          and s["max"] == "60", str(s))
    check("interval: one arrow press is one second",
          arrow_moves(page, "#step_seconds") == 1)

    s = state(page, "#glide_ms")
    check("glide: steps by 20ms, not 1", s["step"] == "20", str(s["step"]))
    check("glide: 300-3000 mirrors the route",
          s["min"] == "300" and s["max"] == "3000", str(s))
    check("glide: one arrow press moves 20ms",
          arrow_moves(page, "#glide_ms") == 20)
    # The trap this file exists for: the step base is `min`, so a step of
    # 50 would put the shipped 360 default off the grid and the browser
    # would refuse to submit a form nobody had touched.
    page.evaluate("() => document.getElementById('glide_ms')"
                  ".setAttribute('step', '50')")
    would_be = would_accept(page, "#glide_ms", str(PARTNER_GLIDE_DEFAULT))
    check("glide: a step of 50 WOULD have made the default invalid",
          not would_be["valid"], str(would_be))
    page.evaluate("() => document.getElementById('glide_ms')"
                  ".setAttribute('step', '20')")
    back = would_accept(page, "#glide_ms", str(PARTNER_GLIDE_DEFAULT))
    check("glide: with 20 the default is on the grid", back["valid"],
          str(back))
    check("glide: and the ceiling is on it too",
          would_accept(page, "#glide_ms", "3000")["valid"])
    over = would_accept(page, "#glide_ms", "3200")
    check("glide: past the ceiling is refused here, not by the server",
          not over["valid"], str(over))

    check("glide: and it KEEPS its spinner, which is worth 20ms a press",
          spinner(page, "#glide_ms")["appearance"] == SPUN,
          str(spinner(page, "#glide_ms")))

    s = state(page, "#drift_speed")
    check("drift: steps by 5 pixels a second", s["step"] == "5",
          str(s["step"]))
    check("drift: 10-200 mirrors the route",
          s["min"] == "10" and s["max"] == "200", str(s))
    check("drift: one arrow press moves 5",
          arrow_moves(page, "#drift_speed") == 5)
    check("drift: the default sits on the grid",
          would_accept(page, "#drift_speed",
                       str(PARTNER_DRIFT_DEFAULT))["valid"])
    check("drift: and so does the ceiling",
          would_accept(page, "#drift_speed", "200")["valid"])
    check("drift: it keeps its spinner too",
          spinner(page, "#drift_speed")["appearance"] == SPUN)
    check("interval: and so does the interval",
          spinner(page, "#step_seconds")["appearance"] == SPUN)

    # ---- sort orders: 1 is right, and NOTHING may block a negative
    for path, label in (("/admin/services/new", "service"),
                        ("/admin/partners/new", "partner"),
                        ("/admin/resources/new", "resource"),
                        ("/admin/journey/new", "milestone")):
        page.goto(BASE + path, wait_until="load")
        s = state(page, "#sort")
        check("%s sort: steps by 1" % label, s["step"] in (None, "1"),
              str(s["step"]))
        check("%s sort: one arrow press is one place" % label,
              arrow_moves(page, "#sort") == 1)
        # int() with no floor server-side, and the row sorts ascending,
        # so -1 is a legitimate way to pin something to the top.
        check("%s sort: a negative is still allowed through" % label,
              would_accept(page, "#sort", "-1")["valid"], str(s))
        check("%s sort: keeps its spinner — one place a press is useful"
              % label,
              spinner(page, "#sort")["appearance"] == SPUN)

    page.goto(BASE + "/admin/journey/new", wait_until="load")
    s = state(page, "#year")
    check("milestone year: steps by 1", s["step"] in (None, "1"),
          str(s["step"]))
    check("milestone year: 1900-2100 bounds it sensibly",
          s["min"] == "1900" and s["max"] == "2100", str(s))

    # ---- campaign money
    page.goto(BASE + "/admin/campaigns/new", wait_until="load")
    for field in ("#fee", "#target"):
        s = state(page, field)
        check("campaign %s: no penny-at-a-time spinner" % field,
              spinner(page, field)["appearance"] == UNSPUN,
              str(spinner(page, field)))
        check("campaign %s: a penny a step, so £7.50 is typeable" % field,
              s["step"] == "0.01", str(s["step"]))
        check("campaign %s: accepts a decimal" % field,
              would_accept(page, field, "7.50")["valid"])
        check("campaign %s: refuses nothing-at-all amounts" % field,
              not would_accept(page, field, "0")["valid"])

    # ---- the settings page: three fields that were text
    page.goto(BASE + "/admin/features", wait_until="load")
    for sel, low, high, label in (("#port", "1", "65535", "SMTP port"),
                                  ("#sftp_port", "1", "65535", "NAS port"),
                                  ("#sftp_keep", "1", "9999",
                                   "NAS retention")):
        s = state(page, sel)
        check("%s: is a number field now" % label, s["type"] == "number",
              str(s))
        check("%s: steps by 1" % label, s["step"] == "1", str(s["step"]))
        check("%s: %s-%s mirrors the route" % (label, low, high),
              s["min"] == low and s["max"] == high, str(s))
        check("%s: one arrow press moves 1" % label,
              arrow_moves(page, sel) == 1)
        check("%s: zero is refused before the round trip" % label,
              not would_accept(page, sel, "0")["valid"])
        check("%s: a value past the ceiling is refused too" % label,
              not would_accept(page, sel, str(int(high) + 1))["valid"])
        check("%s: keeps its spinner" % label,
              spinner(page, sel)["appearance"] == SPUN)

    # ---- the homepage section positions
    # A position among a fixed set of six, not a sort weight — so unlike
    # the sort fields elsewhere it DOES carry a min: there is no first
    # place before the first, and a negative would be a value the route
    # only has to reject because the field let somebody reach it.
    for sel, label in (("#pos_services", "position of What we do"),
                       ("#pos_partners", "position of Our partners")):
        s = state(page, sel)
        check("%s: is a number field" % label, s["type"] == "number", str(s))
        check("%s: steps by 1 — one place at a time" % label,
              s["step"] == "1", str(s["step"]))
        check("%s: 1 to 6, mirroring the route" % label,
              s["min"] == "1" and s["max"] == "6", str(s))
        # The up arrow moves one place — except on the section already in
        # LAST place, where the browser refuses to go past the max. That
        # refusal is the better check of the two: it proves the ceiling
        # is enforced by the control and not only written in an
        # attribute.
        at_ceiling = state(page, sel)["value"] == s["max"]
        check("%s: %s" % (label, "the up arrow will not go past the last "
                          "place" if at_ceiling
                          else "one arrow press moves one place"),
              arrow_moves(page, sel) == (0 if at_ceiling else 1))
        check("%s: there is no position 0" % label,
              not would_accept(page, sel, "0")["valid"])
        check("%s: nor a seventh place among six" % label,
              not would_accept(page, sel, "7")["valid"])
        check("%s: keeps its spinner — a press is a place" % label,
              spinner(page, sel)["appearance"] == SPUN)

    # ---- and the validation messages are sentences, not codes
    page.goto(BASE + "/donate", wait_until="load")
    msg = would_accept(page, "#amount", "0")["message"]
    check("the browser's own message reads like English",
          msg and msg[0].isupper() and len(msg) > 12, msg)
    print("  (donate, value 0) browser says: %s" % msg)

    ctx.close()

    # ================================================================
    # The money fields at every screen, phones included. The spinner and
    # the validation do not depend on width, but the quick-pick row that
    # REPLACES the spinner does: it is the control now, so it has to be
    # reachable and a fair size on the screen most donations come from.
    # ================================================================
    for width, height in VIEWPORTS:
        ctx = new_context(browser, width, height, motion=STILL)
        page = ctx.new_page()
        for path, sel, label in (("/donate", "#amount", "donate"),
                                 ("/collections/seaside-trip", "#donation",
                                  "collection")):
            page.goto(BASE + path, wait_until="load")
            tag = "%s %dx%d" % (label, width, height)
            sp = spinner(page, sel)
            check("%s: no spinner" % tag, sp["appearance"] == UNSPUN,
                  str(sp))
            check("%s: still a number field with a decimal keypad" % tag,
                  sp["type"] == "number"
                  and page.evaluate("(s) => document.querySelector(s)"
                                    ".inputMode", sel) == "decimal",
                  str(sp))
            check("%s: £12.50 still accepted" % tag,
                  would_accept(page, sel, "12.50")["valid"])
            check("%s: a negative still refused" % tag,
                  not would_accept(page, sel, "-5")["valid"])
            check("%s: over the ceiling still refused" % tag,
                  not would_accept(page, sel, "10001")["valid"])
            picks = page.evaluate("""() => [...document.querySelectorAll(
                    '.amount-presets [data-amount]')].map(b => {
                const r = b.getBoundingClientRect();
                return {w: Math.round(r.width), h: Math.round(r.height),
                        left: Math.round(r.left),
                        right: Math.round(r.right)};
            })""")
            check("%s: every quick pick is a fair target" % tag,
                  picks and all(b["h"] >= 40 and b["w"] >= 55 for b in picks),
                  str(picks))
            check("%s: and none of them runs off the side" % tag,
                  all(b["left"] >= 0 and b["right"] <= width for b in picks),
                  str(picks))
            check("%s: the page does not scroll sideways" % tag,
                  page.evaluate(
                      "() => document.documentElement.scrollWidth - "
                      "document.documentElement.clientWidth") <= 0)
            # Tapping one still works at this size, and still shows which.
            page.click(".amount-presets [data-amount]")
            check("%s: tapping a quick pick fills and marks it" % tag,
                  page.evaluate("(s) => document.querySelector(s).value", sel)
                  != ""
                  and page.evaluate("""() => document.querySelectorAll(
                      '.amount-presets .is-picked').length""") == 1)
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
