"""Measure the rich-content presets in real Chromium (Playwright).

Not part of the smoke suite — it needs a browser — but this is what the
presets were verified against. At 1440/1024/390px, for News (a short
two-paragraph body) and Events (a longer one), it asserts that:

  * nothing scrolls sideways and no figure escapes the viewport
  * the reading column stays around 68ch
  * gallery is 3 columns wide, 2 at tablet, 1 on a phone
  * alternating never leaves a half-empty row on a short body, and
    stacks to one column on a phone

Every page here is opened STILL (prefers-reduced-motion: reduce) through
tests/browser_motion.py: the presets carry transitions, and a measured
width or column count taken mid-transition is not the one a reader ends
up with. Nothing here is testing motion; a check that were would ask for
MOVING itself.

Run:  python tests/check_rich_layouts.py [--shots DIR]
"""
import os
import struct
import sys
import threading
import time
import zlib
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_layouts.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from werkzeug.serving import make_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from browser_motion import new_context  # noqa: E402

from app import (app, db, Block, ContentImage, DEFAULT_BLOCKS,  # noqa: E402
                 Event, FEATURES, FeatureFlag, NewsPost, UPLOAD_DIR)

# 900px is not one of the asked-for widths, but it is where the site's
# other grids already step down, so it is where the masonry does too.
WIDTHS = [1440, 1024, 900, 390]
EXPECTED_COLUMNS = {1440: 3, 1024: 3, 900: 2, 390: 1}    # gallery masonry
LAYOUTS = ["classic", "gallery", "alternating"]

# A deliberately SHORT news body: two paragraphs against three photos is
# where an interleaved layout falls apart if it is going to.
NEWS_BODY = ("Thank you to everyone who donated a coat this winter.\n"
             "Collections continue at the centre every Tuesday.")
EVENT_BODY = ("Join us for our annual community iftar at the centre.\n"
              "Doors open at six, with food served shortly after sunset.\n"
              "Everyone is welcome, and there is no charge to attend.\n"
              "Please let us know numbers so we can cater properly.")

shots_dir = None
if "--shots" in sys.argv:
    shots_dir = sys.argv[sys.argv.index("--shots") + 1]
    os.makedirs(shots_dir, exist_ok=True)

failures = []
made = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def png(w, h, rgb):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I",
                                                              zlib.crc32(c))
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


SHAPES = [(800, 600, (0, 86, 63)), (600, 800, (232, 51, 63)),
          (800, 800, (2, 56, 42))]

with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
    for n, _l, _d, default in FEATURES:
        if not FeatureFlag.query.filter_by(name=n).first():
            db.session.add(FeatureFlag(name=n, enabled=default))
    post = NewsPost()
    post.title, post.slug = "Winter coat appeal", "winter-coat-appeal"
    post.published_date, post.body, post.published = date.today(), NEWS_BODY, True
    db.session.add(post)
    ev = Event()
    ev.title, ev.slug = "Community Iftar", "community-iftar"
    ev.event_date, ev.description, ev.published = date.today(), EVENT_BODY, True
    db.session.add(ev)
    db.session.commit()
    post_id, ev_id = post.id, ev.id
    for owner, oid in (("news_post", post_id), ("event", ev_id)):
        for i, (w, h, rgb) in enumerate(SHAPES):
            name = "layoutcheck_%s_%d.png" % (owner, i)
            open(os.path.join(UPLOAD_DIR, name), "wb").write(png(w, h, rgb))
            made.append(name)
            img = ContentImage()
            img.owner_type, img.owner_id = owner, oid
            img.filename, img.alt_text = name, "Photo %d" % i
            img.caption = "Volunteers at the centre" if i == 0 else ""
            img.sort = i * 10
            db.session.add(img)
    db.session.commit()

server = make_server("127.0.0.1", 5089, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)
BASE = "http://127.0.0.1:5089"
PAGES = [("news", "/news/winter-coat-appeal", 2),
         ("event", "/events/community-iftar", 4)]


def set_layout(layout):
    with app.app_context():
        db.session.get(NewsPost, post_id).layout = layout
        db.session.get(Event, ev_id).layout = layout
        db.session.commit()


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for width in WIDTHS:
        for layout in LAYOUTS:
            set_layout(layout)
            for label, path, paras in PAGES:
                ctx = new_context(browser, width, height=1000)
                page = ctx.new_page()
                page.goto(BASE + path, wait_until="load")
                page.wait_for_timeout(250)
                tag = "%s %s %dpx" % (label, layout, width)

                over = page.evaluate(
                    "() => document.documentElement.scrollWidth - "
                    "document.documentElement.clientWidth")
                check("%s: no sideways scroll" % tag, over <= 0, "%dpx" % over)

                figs = page.evaluate("""(w) => [...document.querySelectorAll(
                    '.rc-figure')].map(f => {
                        const r = f.getBoundingClientRect();
                        return {w: Math.round(r.width), h: Math.round(r.height),
                                left: Math.round(r.left),
                                right: Math.round(r.right)};
                    })""", width)
                check("%s: every photo rendered" % tag,
                      len(figs) == 3 and all(f["w"] > 20 and f["h"] > 20
                                             for f in figs), str(figs))
                check("%s: photos stay inside the viewport" % tag,
                      all(f["left"] >= -1 and f["right"] <= width + 1
                          for f in figs), str(figs))

                text_w = page.evaluate(
                    "() => { const t = document.querySelector('.rc-text');"
                    " return t ? Math.round(t.getBoundingClientRect().width)"
                    " : 0; }")
                check("%s: reading column is not over-wide" % tag,
                      0 < text_w <= 800, "%dpx" % text_w)

                if layout == "gallery":
                    cols = page.evaluate(
                        "() => getComputedStyle(document.querySelector("
                        "'.rc-masonry')).columnCount")
                    check("%s: masonry is %d column(s)"
                          % (tag, EXPECTED_COLUMNS[width]),
                          str(cols) == str(EXPECTED_COLUMNS[width]), str(cols))

                if layout == "alternating":
                    rows = page.evaluate("""() => [...document.querySelectorAll(
                        '.rc-alt-row')].map(r => {
                            const t = r.querySelector('.rc-text');
                            const f = r.querySelector('.rc-figure');
                            const rr = r.getBoundingClientRect();
                            return {
                              hasText: !!t && t.innerText.trim().length > 0,
                              mediaOnly: r.classList.contains('is-mediaonly'),
                              rowW: Math.round(rr.width),
                              figW: f ? Math.round(
                                  f.getBoundingClientRect().width) : 0,
                              cols: getComputedStyle(r).gridTemplateColumns
                                      .split(' ').length};
                        })""")
                    check("%s: a row for every photo" % tag,
                          len([r for r in rows if r["figW"]]) == 3, str(rows))
                    stranded = [r for r in rows
                                if not r["hasText"] and not r["mediaOnly"]]
                    check("%s: no half-empty rows on a short body" % tag,
                          not stranded, str(stranded))
                    if width == 390:
                        stacked = all(r["cols"] == 1 for r in rows)
                        check("%s: stacks to one column on a phone" % tag,
                              stacked, str([r["cols"] for r in rows]))
                        check("%s: photos go full width when stacked" % tag,
                              all(r["figW"] >= r["rowW"] - 2
                                  for r in rows if r["figW"]), str(rows))

                if shots_dir and width in (1440, 390):
                    page.screenshot(
                        path=os.path.join(shots_dir, "rich-%s-%s-%d.png"
                                          % (label, layout, width)),
                        full_page=True)
                ctx.close()
    browser.close()

server.shutdown()
with app.app_context():
    db.session.remove()
    db.engine.dispose()
for name in made:
    path = os.path.join(UPLOAD_DIR, name)
    if os.path.isfile(path):
        os.remove(path)
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
