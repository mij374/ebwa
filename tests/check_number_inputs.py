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
from browser_view import height_for  # noqa: E402

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
    db.session.add(Campaign(title="Seaside trip", slug="seaside-trip",
                            description="A day out.", active=True,
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
    # The quick picks are what this field has instead of useful arrows.
    page.click(".amount-presets [data-amount='25']")
    check("donate: a quick pick fills the amount",
          page.evaluate("() => document.getElementById('amount').value")
          == "25")

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

    # ---- and the validation messages are sentences, not codes
    page.goto(BASE + "/donate", wait_until="load")
    msg = would_accept(page, "#amount", "0")["message"]
    check("the browser's own message reads like English",
          msg and msg[0].isupper() and len(msg) > 12, msg)
    print("  (donate, value 0) browser says: %s" % msg)

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
