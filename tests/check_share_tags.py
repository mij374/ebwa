"""Browser check: the share tags are in the head and nothing leaks out.

The template test (`smoke_test_seo.py`) reads the HTML as text. This
asks Chromium what it MADE of it, which is the half only a browser can
answer: a stray expression in `<head>` is not an error to a parser, it
is a signal to close the head early — the text and every tag after it
land at the top of the body. That is how a photograph's URL came to be
printed above the hero on the demo, with every og: tag present and the
smoke test green.

At every shared viewport, on the five pages that carry their own
photograph (home, About, an event, a news post, a collection):

  - every og:/twitter: meta and the JSON-LD script are children of
    document.head, by the browser's own reckoning;
  - og:image is that page's own photograph;
  - nothing visible on the page contains a URL;
  - the page does not scroll sideways.

Run:  python tests/check_share_tags.py
"""
import os
import sys
import threading
import time
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_share.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from werkzeug.serving import make_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import fake_uploads  # noqa: E402
from browser_motion import new_page  # noqa: E402
from browser_view import VIEWPORTS  # noqa: E402

from app import (app, db, Block, Campaign, DEFAULT_BLOCKS, Event,  # noqa: E402
                 FEATURES, FeatureFlag, NewsPost)

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        db.session.add(Block(group=group, key=key, label=label, kind=kind,
                             value=value))
    for n, _l, _d, _default in FEATURES:
        db.session.add(FeatureFlag(name=n, enabled=True))
    Block.query.filter_by(key="home_hero_image").first().value = \
        "share-hero.jpg"
    Block.query.filter_by(key="about_image").first().value = \
        "share-about.jpg"
    db.session.add_all([
        Event(title="Eid in the Park", slug="eid-in-the-park",
              event_date=date.today() + timedelta(days=10),
              summary="Bring the family.", image="share-event.jpg"),
        NewsPost(title="New term begins", slug="new-term",
                 published_date=date.today(), summary="Places open.",
                 body="Words.", image="share-news.jpg"),
        Campaign(title="Seaside collection", slug="seaside",
                 description="Help us get there.", image="share-camp.jpg",
                 state="open"),
    ])
    db.session.commit()
    fixture_files = fake_uploads.fill_dangling()

PAGES = [("/", "share-hero.jpg"), ("/about", "share-about.jpg"),
         ("/events/eid-in-the-park", "share-event.jpg"),
         ("/news/new-term", "share-news.jpg"),
         ("/collections/seaside", "share-camp.jpg")]

server = make_server("127.0.0.1", 5098, app)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)
BASE = "http://127.0.0.1:5098"

PROBE = """() => {
    const inHead = sel => [...document.querySelectorAll(sel)]
        .map(el => document.head.contains(el));
    const og = document.querySelector('meta[property="og:image"]');
    return {
        metas: inHead('meta[property^="og:"], meta[name^="twitter:"]'),
        ld: inHead('script[type="application/ld+json"]'),
        ogImage: og ? og.content : null,
        text: document.body.innerText,
        overflow: document.documentElement.scrollWidth
                  - document.documentElement.clientWidth,
        bodyStartsWith: (document.body.textContent || '').trim().slice(0, 60),
    };
}"""

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for width, height in VIEWPORTS:
        page = new_page(browser, width, height)
        for path, photo in PAGES:
            label = "%dx%d %s" % (width, height, path)
            page.goto(BASE + path, wait_until="networkidle")
            got = page.evaluate(PROBE)
            check("%s: every share tag is in the head" % label,
                  got["metas"] and all(got["metas"]),
                  "%d of %d in head" % (sum(got["metas"]),
                                        len(got["metas"])))
            check("%s: the JSON-LD is in the head" % label,
                  got["ld"] and all(got["ld"]))
            check("%s: og:image is this page's own photograph" % label,
                  got["ogImage"] and got["ogImage"].endswith(
                      photo.replace(".jpg", "-thumb.jpg"))
                  or (got["ogImage"] or "").endswith(photo),
                  got["ogImage"])
            check("%s: no URL is visible on the page" % label,
                  "/static/" not in got["text"] and "://" not in got["text"],
                  got["bodyStartsWith"])
            check("%s: no sideways scroll" % label, got["overflow"] <= 0,
                  "%dpx" % got["overflow"])
        page.close()
    browser.close()

server.shutdown()
with app.app_context():
    db.session.remove()
    db.engine.dispose()
fake_uploads.remove(fixture_files)
for suffix in ("", "-wal", "-shm"):
    f = TEST_DB + suffix
    if os.path.isfile(f):
        os.remove(f)

if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
