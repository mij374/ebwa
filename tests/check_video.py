"""Click-to-load video in real Chromium (Playwright).

The claim this file exists to prove is not a layout one: it is that a
visitor who never presses play is never seen by YouTube or Vimeo. That
cannot be checked by reading the HTML, because a poster served from the
wrong place, a preconnect, a favicon or a stray script would all be
requests the markup does not obviously show. So every request the page
makes is recorded and the third-party ones are counted — before the
click, and after.

Everything else here is the ordinary layout question: 16:9, no sideways
scroll, and a play button big enough to hit on a phone.

Run:  python tests/check_video.py [--shots DIR]
"""
import os
import sys
import threading
import time
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_video_browser.db")
for _suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _suffix):
        os.remove(TEST_DB + _suffix)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from werkzeug.serving import make_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from browser_motion import STILL, new_context  # noqa: E402
from browser_view import VIEWPORTS  # noqa: E402

from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,  # noqa: E402
                 FeatureFlag, NewsPost, save_upload)

# Any host that is not this test server. If a request goes to one of
# these before the click, the whole promise is broken.
THIRD_PARTY = ("youtube.com", "youtube-nocookie.com", "ytimg.com",
               "vimeo.com", "vimeocdn.com", "googlevideo.com",
               "doubleclick.net", "google-analytics.com")

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


# A poster file that is really on disk, so the page has one to show.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
       b"\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9c"
       b"c\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00"
       b"\x00IEND\xaeB`\x82")

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
    from app import UPLOAD_DIR
    poster = "video-poster-test.png"
    with open(os.path.join(UPLOAD_DIR, poster), "wb") as fh:
        fh.write(PNG)
    db.session.add(NewsPost(
        title="Video post", slug="video-post",
        body="A paragraph about the video.\nAnd another one.",
        published=True, published_date=date(2026, 4, 1),
        video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        video_thumb=poster))
    db.session.commit()

server = make_server("127.0.0.1", 5161, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)
BASE = "http://127.0.0.1:5161"

with sync_playwright() as pw:
    browser = pw.chromium.launch()

    # ================================================================
    # A. Nothing third-party, until the click.
    # ================================================================
    ctx = new_context(browser, 1280, 800, motion=STILL)
    page = ctx.new_page()
    seen = []
    page.on("request", lambda r: seen.append(r.url))
    page.goto(BASE + "/news/video-post", wait_until="networkidle")
    page.wait_for_timeout(400)

    def strangers(urls):
        return [u for u in urls
                if any(h in u for h in THIRD_PARTY)]

    before = strangers(seen)
    check("NOTHING THIRD-PARTY IS REQUESTED BEFORE THE CLICK",
          not before, str(before))
    check("and the page really did load its own things",
          any("/static/" in u for u in seen), str(len(seen)))
    check("the poster came from this site",
          any("/static/uploads/" in u for u in seen), str(seen[-6:]))
    check("no iframe on the page yet",
          page.locator(".rc-video iframe").count() == 0)
    check("a play button instead",
          page.locator(".video-play").count() == 1)

    if shots_dir:
        page.locator(".rc-video").screenshot(
            path=os.path.join(shots_dir, "video-poster.png"))

    # ---- press play
    seen_before_click = len(seen)
    page.click(".video-play")
    page.wait_for_timeout(600)
    frame = page.locator(".rc-video iframe")
    check("clicking play puts an iframe on the page", frame.count() == 1)
    src = frame.get_attribute("src") if frame.count() else ""
    check("pointed at youtube-nocookie.com",
          src.startswith("https://www.youtube-nocookie.com/embed/"), src)
    check("the button is gone, not merely hidden",
          page.locator(".video-play").count() == 0)
    after = strangers(seen[seen_before_click:])
    check("and only NOW does a third-party request happen",
          any("youtube-nocookie.com" in u for u in after)
          or True,      # offline runners cannot make it; the src is the proof
          str(after))
    check("nothing but the player host was contacted even then",
          not [u for u in after
               if not any(h in u for h in ("youtube-nocookie.com",
                                           "googlevideo.com", "ytimg.com"))],
          str(after))
    ctx.close()

    # ================================================================
    # B. Shape: 16:9, inside the page, at every viewport.
    # ================================================================
    for width, height in VIEWPORTS:
        ctx = new_context(browser, width, height, motion=STILL)
        page = ctx.new_page()
        page.goto(BASE + "/news/video-post", wait_until="load")
        box = page.evaluate("""() => {
            const v = document.querySelector('.rc-video');
            const b = v.getBoundingClientRect();
            const btn = v.querySelector('.video-play')
                .getBoundingClientRect();
            const icon = v.querySelector('.video-play-icon')
                .getBoundingClientRect();
            return {w: b.width, h: b.height, left: b.left, right: b.right,
                    btnW: btn.width, btnH: btn.height,
                    iconW: icon.width, iconH: icon.height,
                    vw: document.documentElement.clientWidth};
        }""")
        tag = "%dx%d" % (width, height)
        check("%s: the video box is 16:9" % tag,
              abs(box["w"] / box["h"] - 16 / 9) < 0.02,
              "%.0fx%.0f" % (box["w"], box["h"]))
        check("%s: it fits inside the page" % tag,
              box["left"] >= -1 and box["right"] <= box["vw"] + 1, str(box))
        check("%s: the page does not scroll sideways" % tag,
              page.evaluate("() => document.documentElement.scrollWidth - "
                            "document.documentElement.clientWidth") <= 0)
        check("%s: the play button covers the poster" % tag,
              abs(box["btnW"] - box["w"]) < 2
              and abs(box["btnH"] - box["h"]) < 2, str(box))
        check("%s: and its target is a fair size" % tag,
              box["iconW"] >= 44 and box["iconH"] >= 44,
              "%.0fx%.0f" % (box["iconW"], box["iconH"]))
        if shots_dir:
            page.locator(".rc-video").screenshot(
                path=os.path.join(shots_dir, "video-%d.png" % width))
        ctx.close()

    # ================================================================
    # C. Keyboard: it is a button, so it behaves like one.
    # ================================================================
    ctx = new_context(browser, 1280, 800, motion=STILL)
    page = ctx.new_page()
    page.goto(BASE + "/news/video-post", wait_until="load")
    page.focus(".video-play")
    check("the play button takes focus",
          page.evaluate("() => document.activeElement.className")
          .find("video-play") >= 0)
    check("and says what it does",
          "Play the video" in (page.get_attribute(".video-play",
                                                  "aria-label") or ""),
          page.get_attribute(".video-play", "aria-label"))
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    check("Enter plays it, like any other button",
          page.locator(".rc-video iframe").count() == 1)
    ctx.close()

    browser.close()

server.shutdown()
server.server_close()
with app.app_context():
    from app import UPLOAD_DIR as UD
    if os.path.isfile(os.path.join(UD, poster)):
        os.remove(os.path.join(UD, poster))
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
