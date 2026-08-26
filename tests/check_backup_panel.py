"""The backup panel has to show what is happening, in a real browser.

The server side is covered by smoke_test_backup_security.py — the button
returns at once, the thread does the work, a second press is refused, an
interrupted run reads as interrupted. None of that answers the question
this file exists for: does the person who pressed the button SEE any of
it, without touching the page again?

That is a browser question and only a browser can answer it:

  * pressing the button leaves the panel saying Running straight away,
    rather than a blank page and a spinner in the tab;
  * the panel turns to Finished ON ITS OWN — and this file proves the
    page was never reloaded, by stamping a value on `window` before it
    waits and asserting the same value is still there afterwards. A
    check that allowed a reload would pass against no script at all;
  * a failure shows the reason rather than a colour;
  * the button cannot be pressed again while one is running;
  * AND WHAT THE PERSON IS TOLD MATCHES WHAT IS ON THE SCREEN, whether
    the backup takes a minute or is over before the page is drawn.

That last one is here because of a bug this file's first version could
not have caught. It proved the running state by holding the archive
shut, so the fast path — what a small site actually does, in well under
a second — was never exercised end to end, and the message was composed
inside the POST before anyone knew which it would be. It read "the panel
below shows how it is getting on" over a panel that already said
Finished. It also pointed "below" at something about 1,900px down a
9,000px page: the redirect lands on #backups, so the panel is on screen
and the flash is far above it, and the two are never in view together.
Hence ONSCREEN — this file reads WHERE things are, not only that they
exist in the document.

The backup itself is slowed down here, not mocked away: the real
`run_backup` still writes a real archive, it is just held for a moment
first, so the running state exists long enough to look at. For the fast
path the handover to the thread is removed instead, so the real claim and
the real run both complete before the view returns.

Run:  python tests/check_backup_panel.py [--shots DIR]
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEST_DB = os.path.join(HERE, "test_backup_panel.db")
SANDBOX = os.path.join(HERE, "backup-panel-sandbox")
ARCHIVES = os.path.join(SANDBOX, "archives")
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _s):
        os.remove(TEST_DB + _s)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
os.environ["BACKUP_DIR"] = ARCHIVES
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from werkzeug.serving import make_server              # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402
from playwright.sync_api import sync_playwright       # noqa: E402

from browser_motion import STILL, new_context         # noqa: E402
from browser_view import VIEWPORTS                    # noqa: E402

import app as appmod                                  # noqa: E402
from app import app, db, User, BackupRun              # noqa: E402
import seed_demo                                      # noqa: E402

SHOTS = (sys.argv[sys.argv.index("--shots") + 1]
         if "--shots" in sys.argv else None)
if SHOTS:
    os.makedirs(SHOTS, exist_ok=True)

PORT = 5189
BASE = "http://127.0.0.1:%d" % PORT
PW = "backup-panel-password"

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        ("\n        %s" % detail) if detail and not cond
                        else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    seed_demo.seed()
    db.session.add(User(email="panel@example.com", role="super_admin",
                        password_hash=generate_password_hash(PW)))
    db.session.commit()

# ---- the backup, slowed but REAL --------------------------------------
# Held shut until the check lets go, so "running" lasts long enough to
# look at. What happens after the gate opens is the real thing: a real
# archive, a real BackupRun, the real panel state.
real_run_backup = appmod.run_backup
gate = threading.Event()
mode = {"how": "slow"}


def instrumented(reason="manual", run=None):
    if mode["how"] == "broken":
        gate.wait(30)
        raise RuntimeError("the backup disk is full")
    gate.wait(30)
    return real_run_backup(reason=reason, run=run)


appmod.run_backup = instrumented

server = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)

# WHAT IS ON SCREEN, not merely in the document. The Settings page is
# about 9,000px tall and the redirect lands on #backups, so the flash sits
# roughly 1,900px above the panel and the two are never in view together.
# That is why each has to be true on its own, and why this reads their
# positions rather than only their text.
ONSCREEN = r"""() => {
  const out = {scrollY: Math.round(window.scrollY)};
  const look = (name, sel) => {
    const el = document.querySelector(sel);
    if (!el) { out[name] = null; return; }
    const r = el.getBoundingClientRect();
    out[name] = {inView: r.bottom > 0 && r.top < window.innerHeight,
                 top: Math.round(r.top),
                 text: (el.textContent || '').replace(/\s+/g, ' ').trim()};
  };
  look('flash', '.flash');
  look('panel', '#backupState');
  return out;
}"""

STATE = """() => {
  const row = document.getElementById('backupState');
  const button = document.getElementById('backupButton');
  if (!row) return null;
  return {
    busy: row.getAttribute('data-busy'),
    state: row.getAttribute('data-state'),
    pill: (document.getElementById('backupPill').textContent || '').trim(),
    detail: (document.getElementById('backupDetail').textContent || '').trim(),
    when: (document.getElementById('backupWhen').textContent || '').trim(),
    disabled: button ? button.disabled : null,
    marker: window.__panelMarker || null
  };
}"""


def wait_for(page, wanted, seconds=40):
    """Wait for the panel to reach a state — WITHOUT touching the page."""
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        last = page.evaluate(STATE)
        if last and last["state"] == wanted:
            return last
        time.sleep(0.2)
    return last


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = new_context(browser, 1280, 800, STILL)
    page = ctx.new_page()
    # The button asks for confirmation; accept it rather than letting a
    # modal dialog wedge the session.
    page.on("dialog", lambda d: d.accept())

    page.goto(BASE + "/admin/login", wait_until="load")
    page.fill("input[name=email]", "panel@example.com")
    page.fill("input[name=password]", PW)
    page.click("button[type=submit]")
    page.wait_for_load_state("load")

    page.goto(BASE + "/admin/features", wait_until="load")
    before = page.evaluate(STATE)
    check("the panel is on the page before anything is pressed",
          before is not None and before["state"] == "none",
          str(before))
    check("...saying it has never run, rather than nothing at all",
          before and before["pill"] == "Never run", str(before))

    # ---- pressing it -------------------------------------------------
    gate.clear()
    mode["how"] = "slow"
    page.click("#backupButton")
    page.wait_for_load_state("load")
    running = page.evaluate(STATE)
    check("PRESSING IT SHOWS RUNNING STRAIGHT AWAY",
          running and running["state"] == "running"
          and running["pill"] == "Running", str(running))
    check("...with the time it started",
          running and "started" in running["when"], str(running))
    check("...and a sentence saying what it is doing",
          running and "Writing the archive" in running["detail"],
          str(running))
    check("...and the page said so rather than hanging until it finished",
          gate.is_set() is False)
    check("A SECOND PRESS IS NOT OFFERED while one is running",
          running and running["disabled"] is True, str(running))
    check("...and the flash says the work carries on without the page",
          "you can leave this page" in page.inner_text("main"))
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, "backup-running.png"),
                        full_page=True)

    # THE PROOF THAT NOTHING RELOADED. A value on `window` survives a
    # repaint and does not survive a navigation, so if it is still there
    # at the end, the panel below it changed by script.
    page.evaluate("() => { window.__panelMarker = 'same-page'; }")

    gate.set()
    done = wait_for(page, "ok")
    check("THE PANEL TURNS TO FINISHED ON ITS OWN",
          done and done["state"] == "ok" and done["pill"] == "Finished",
          str(done))
    check("...WITHOUT THE PAGE HAVING BEEN RELOADED",
          done and done["marker"] == "same-page",
          "the page navigated, so this proves nothing about the poll")
    check("...naming the archive it wrote",
          done and ".zip" in done["detail"], str(done))
    check("...showing when it finished",
          done and "finished" in done["when"], str(done))
    check("...and offering the button again",
          done and done["disabled"] is False, str(done))
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, "backup-finished.png"),
                        full_page=True)

    # The poll must STOP once there is nothing to watch, or an admin who
    # leaves Settings open asks the server a question every two seconds
    # for the rest of the afternoon.
    polls = {"n": 0}

    def count_polls(request):
        if "backup.json" in request.url:
            polls["n"] += 1

    page.on("request", count_polls)
    time.sleep(6)
    check("THE POLL STOPS once the backup is finished",
          polls["n"] == 0, "%d more requests in six seconds" % polls["n"])
    # Detached here: it outlives this section otherwise, and every later
    # navigation goes on running a listener that belongs to one check.
    page.remove_listener("request", count_polls)

    # ---- a failure ---------------------------------------------------
    gate.clear()
    mode["how"] = "broken"
    page.goto(BASE + "/admin/features", wait_until="load")
    page.click("#backupButton")
    page.wait_for_load_state("load")
    check("a backup that will fail still starts visibly",
          (page.evaluate(STATE) or {}).get("state") == "running")
    page.evaluate("() => { window.__panelMarker = 'same-page'; }")
    gate.set()
    failed = wait_for(page, "failed")
    check("A FAILURE SHOWS AS FAILED, on its own",
          failed and failed["state"] == "failed"
          and failed["pill"] == "Failed", str(failed))
    check("...WITHOUT A RELOAD", failed and failed["marker"] == "same-page")
    check("...AND SAYS WHAT WENT WRONG, not just that it did",
          failed and "the backup disk is full" in failed["detail"],
          str(failed))
    check("...and lets somebody try again",
          failed and failed["disabled"] is False, str(failed))
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, "backup-failed.png"),
                        full_page=True)

    # ---- A BACKUP THAT BEATS THE PAGE --------------------------------
    # The checks above prove the RUNNING state by holding the archive
    # shut, which means the fast path — what a small site really does —
    # was never exercised end to end. Here the whole job completes before
    # the view returns, and what the person is told must match what is on
    # the screen in front of them.
    with app.app_context():
        BackupRun.query.delete()
        db.session.commit()
    appmod._rate_buckets.clear()
    gate.set()
    mode["how"] = "slow"          # i.e. not broken; the gate is open

    real_start = appmod.start_backup

    def start_and_finish(reason="manual", actor=None):
        """The real claim and the real run, without the handover."""
        run = appmod.claim_backup_slot(reason)
        if run is None:
            return None
        appmod.log_action("backup",
                          summary="Started a backup from the Settings page.",
                          actor=actor)
        appmod.backup_job(run.id, actor)
        return run

    appmod.start_backup = start_and_finish
    try:
        page.goto(BASE + "/admin/features", wait_until="load")
        page.click("#backupButton")
        page.wait_for_load_state("load")
    finally:
        appmod.start_backup = real_start

    quick = page.evaluate(STATE)
    onscreen = page.evaluate(ONSCREEN)
    check("A BACKUP THAT FINISHES FIRST SHOWS AS FINISHED, not running",
          quick and quick["state"] == "ok" and quick["pill"] == "Finished",
          str(quick))
    check("...and the panel is what the person is looking at",
          onscreen["panel"] and onscreen["panel"]["inView"] is True,
          "the panel is off screen: %s" % str(onscreen.get("panel")))
    check("...naming the archive right there",
          quick and ".zip" in quick["detail"], str(quick))

    # THE MESSAGE. It is far above the panel, so it is read on its own or
    # not at all — either way it must not describe the wrong thing.
    flash_text = (onscreen["flash"] or {}).get("text", "")
    check("THE MESSAGE SAYS IT HAS FINISHED",
          "Backup finished already" in flash_text, flash_text or "no flash")
    check("...and carries the result, rather than pointing elsewhere",
          ".zip" in flash_text, flash_text)
    check("...and does NOT promise a panel the reader cannot see",
          "panel below" not in flash_text, flash_text)
    check("THE MESSAGE AND THE PANEL AGREE",
          ("finished" in flash_text.lower())
          == (quick["state"] == "ok"), "%s vs %s" % (flash_text, quick))
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, "backup-instant.png"),
                        full_page=True)

    # The same question for the SLOW case, from the same code path.
    with app.app_context():
        BackupRun.query.delete()
        db.session.commit()
    appmod._rate_buckets.clear()
    gate.clear()
    page.goto(BASE + "/admin/features", wait_until="load")
    page.click("#backupButton")
    page.wait_for_load_state("load")
    slow_state = page.evaluate(STATE)
    slow_seen = page.evaluate(ONSCREEN)
    slow_flash = (slow_seen["flash"] or {}).get("text", "")
    check("A SLOW BACKUP SAYS IT IS RUNNING",
          "it is running now" in slow_flash, slow_flash or "no flash")
    check("...and the panel agrees, on screen",
          slow_state["state"] == "running"
          and slow_seen["panel"]["inView"] is True, str(slow_seen["panel"]))
    check("...and it does not point at a panel either",
          "panel below" not in slow_flash, slow_flash)
    gate.set()
    done2 = wait_for(page, "ok")
    check("...and it still finishes on its own afterwards",
          done2 and done2["state"] == "ok", str(done2))

    # Revisiting Settings must not repeat the message — it describes a
    # moment, while the panel describes a state.
    page.goto(BASE + "/admin/features", wait_until="load")
    later = page.evaluate(ONSCREEN)
    check("the message is shown once, not on every later visit",
          later["flash"] is None, str(later["flash"]))
    check("...but the panel still shows the result",
          (page.evaluate(STATE) or {}).get("pill") == "Finished")

    # ---- with no script at all ---------------------------------------
    # The button is a plain POST form, so it must still start a backup
    # and the page must still say what happened when it is reloaded.
    with app.app_context():
        BackupRun.query.delete()
        db.session.commit()
    appmod._rate_buckets.clear()
    gate.set()
    mode["how"] = "slow"
    noscript = new_context(browser, 1280, 800, STILL,
                           java_script_enabled=False)
    nopage = noscript.new_page()
    nopage.goto(BASE + "/admin/login", wait_until="load")
    nopage.fill("input[name=email]", "panel@example.com")
    nopage.fill("input[name=password]", PW)
    nopage.click("button[type=submit]")
    nopage.wait_for_load_state("load")
    nopage.goto(BASE + "/admin/features", wait_until="load")
    check("WITH NO JAVASCRIPT, the page says how to see progress",
          "refresh it to see where the backup has got to"
          in nopage.inner_text("main"))
    nopage.click("#backupButton")          # no confirm() without script
    nopage.wait_for_load_state("load")
    text = nopage.inner_text("main")
    check("...the button still starts a backup",
          "Backup started" in text or "Backup finished" in text, text[:200])
    check("...and the panel says which it is, with no script to update it",
          "Running" in text or "Finished" in text)
    deadline = time.time() + 40
    while time.time() < deadline:
        with app.app_context():
            row = (BackupRun.query.order_by(BackupRun.id.desc()).first())
            if row is not None and row.status != "running":
                break
        time.sleep(0.2)
    nopage.reload(wait_until="load")
    check("...and a refresh shows it finished",
          "Finished" in nopage.inner_text("main"),
          nopage.inner_text("main")[:200])
    noscript.close()

    # ---- it fits on a phone ------------------------------------------
    for width, height in VIEWPORTS:
        page.set_viewport_size({"width": width, "height": height})
        page.goto(BASE + "/admin/features", wait_until="load")
        over = page.evaluate("""() => {
          const d = document.documentElement;
          return d.scrollWidth - d.clientWidth;
        }""")
        check("%dx%d: the panel does not drag the page sideways"
              % (width, height), over <= 1, "%dpx over" % over)

    browser.close()

server.shutdown()
appmod.run_backup = real_run_backup
import shutil                                          # noqa: E402
shutil.rmtree(SANDBOX, ignore_errors=True)
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
print("The backup panel shows its state without the page being touched.")
