"""Smoke test for video embedding: parsing, refusals, and click-to-load.

The whole feature rests on two rules, and both are asserted here rather
than trusted:

  * NOTHING an admin types is stored or rendered as markup. They paste a
    link; the provider and id are pulled out of it; what is stored is an
    address this code builds. Paste an <iframe> into a body field and it
    is refused with a message pointing at the video box.
  * NOTHING THIRD-PARTY IS CONTACTED UNTIL A VISITOR PRESSES PLAY. The
    poster comes from our own uploads folder, and the player URL sits in
    a data attribute until a click. That is what keeps the cookie
    notice's "no tracking" claim true without a consent flow.

The poster fetch is not exercised against the real YouTube here — a
smoke test must not need the internet — so it is stubbed both ways:
returning a filename, and failing, which is the case that must still
save the video.

Run:  python tests/smoke_test_video.py
"""
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_video.db")
for _suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _suffix):
        os.remove(TEST_DB + _suffix)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from werkzeug.security import generate_password_hash  # noqa: E402

import app as appmod  # noqa: E402
from app import (app, db, AuditLog, Block, Campaign, CSP,  # noqa: E402
                 DEFAULT_BLOCKS, Event, FEATURES, FeatureFlag, Milestone,
                 NewsPost, User, body_embed_problem, parse_video_url,
                 video_embed_url, video_watch_url)

app.config["TESTING"] = True
PW = "video-test-password"
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
    for n, _l, _d, default in FEATURES:
        if not FeatureFlag.query.filter_by(name=n).first():
            db.session.add(FeatureFlag(name=n, enabled=default))
    db.session.add(User(email="netbus@example.com",
                        password_hash=generate_password_hash(PW),
                        role="super_admin"))
    post = NewsPost(title="Video post", slug="video-post", body="Words.",
                    published=True, published_date=date(2026, 4, 1))
    ev = Event(title="Video event", slug="video-event",
               event_date=date(2026, 9, 1), description="Words.",
               published=True)
    ms = Milestone(title="Video milestone", year=2026, summary="Words.",
                   published=True)
    camp = Campaign(title="Video campaign", slug="video-campaign",
                    description="Words.", active=True)
    db.session.add_all([post, ev, ms, camp])
    db.session.commit()
    POST_ID, EV_ID, MS_ID, CAMP_ID = post.id, ev.id, ms.id, camp.id

client = app.test_client()
anon = app.test_client()
client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})

# The poster fetch is the only part that would touch the network. Stub
# it, so this test never needs the internet and both outcomes can be
# exercised deliberately.
FETCHED = []


def fake_fetch(provider, video_id):
    FETCHED.append((provider, video_id))
    return "poster-%s.jpg" % video_id if FETCH_WORKS[0] else ""


FETCH_WORKS = [True]
appmod.fetch_video_poster = fake_fetch

YT = "dQw4w9WgXcQ"

# =====================================================================
# Parsing: every form a person might paste
# =====================================================================
GOOD = [
    ("https://www.youtube.com/watch?v=%s" % YT, "youtube", YT),
    ("http://youtube.com/watch?v=%s" % YT, "youtube", YT),
    ("https://m.youtube.com/watch?feature=share&v=%s&t=42s" % YT,
     "youtube", YT),
    ("https://youtu.be/%s" % YT, "youtube", YT),
    ("https://youtu.be/%s?si=AbCdEf" % YT, "youtube", YT),
    ("https://www.youtube.com/embed/%s" % YT, "youtube", YT),
    ("https://www.youtube-nocookie.com/embed/%s" % YT, "youtube", YT),
    ("https://www.youtube.com/shorts/%s" % YT, "youtube", YT),
    ("  https://www.youtube.com/watch?v=%s  " % YT, "youtube", YT),
    ("https://vimeo.com/123456789", "vimeo", "123456789"),
    ("https://vimeo.com/123456789/9a8b7c6d5e", "vimeo", "123456789"),
    ("https://player.vimeo.com/video/123456789", "vimeo", "123456789"),
    ("https://vimeo.com/channels/staffpicks/123456789", "vimeo", "123456789"),
    ("https://vimeo.com/groups/shortfilms/videos/123456789",
     "vimeo", "123456789"),
]
for raw, provider, ident in GOOD:
    got = parse_video_url(raw)
    check("parses %s" % raw[:52],
          got == {"provider": provider, "id": ident}, str(got))

BAD = ["", "   ", "not a url", "https://example.com/video.mp4",
       "https://dailymotion.com/video/x7abcd",
       "https://vimeo.com/notanumber",
       "https://facebook.com/watch?v=123456789012",
       "javascript:alert(1)",
       "https://www.youtube.com/watch?v=short"]
for raw in BAD:
    check("refuses %r" % raw[:40], parse_video_url(raw) is None,
          str(parse_video_url(raw)))

# =====================================================================
# The URLs we build — no-cookie and do-not-track, never the plain host
# =====================================================================
yt_embed = video_embed_url("youtube", YT)
vm_embed = video_embed_url("vimeo", "123456789")
check("YOUTUBE EMBEDS THROUGH YOUTUBE-NOCOOKIE.COM",
      yt_embed.startswith("https://www.youtube-nocookie.com/embed/"),
      yt_embed)
check("and never through youtube.com itself",
      "//www.youtube.com" not in yt_embed, yt_embed)
check("VIMEO EMBEDS WITH dnt=1", "dnt=1" in vm_embed, vm_embed)
check("through the player host", vm_embed.startswith(
    "https://player.vimeo.com/video/"), vm_embed)
check("the watch links are the ordinary ones, for the admin",
      video_watch_url("youtube", YT).startswith("https://www.youtube.com")
      and video_watch_url("vimeo", "1") == "https://vimeo.com/1")

# =====================================================================
# The CSP allows those two hosts and nothing else new
# =====================================================================
frame = [p.strip() for p in CSP.split(";") if p.strip().startswith("frame-src")][0]
check("CSP allows the two player hosts",
      "https://www.youtube-nocookie.com" in frame
      and "https://player.vimeo.com" in frame, frame)
check("and still the map, which was already there",
      "https://www.google.com" in frame, frame)
check("plain youtube.com is NOT allowed to frame",
      " https://www.youtube.com" not in frame, frame)
img = [p.strip() for p in CSP.split(";") if p.strip().startswith("img-src")][0]
check("IMG-SRC IS UNCHANGED — posters are our own files",
      img == "img-src 'self' data:", img)

# =====================================================================
# Saving a video: what is stored is OUR url, never what was pasted
# =====================================================================
r = client.post("/admin/news_post/%d/video" % POST_ID,
                data={"video_url": "https://youtu.be/%s?si=xyz" % YT},
                follow_redirects=True)
check("a video saves from the shared partial", b"Video saved" in r.data)
with app.app_context():
    p = db.session.get(NewsPost, POST_ID)
    check("STORED AS OUR CANONICAL URL, not the pasted one",
          p.video_url == "https://www.youtube.com/watch?v=%s" % YT,
          p.video_url)
    check("with the poster filename stored beside it",
          p.video_thumb == "poster-%s.jpg" % YT, p.video_thumb)
    entry = AuditLog.query.order_by(AuditLog.id.desc()).first()
    check("the change is audit-logged", "video" in (entry.summary or ""),
          entry.summary)

r = client.post("/admin/news_post/%d/video" % POST_ID,
                data={"video_url": '<iframe src="https://www.youtube.com/'
                                   'embed/%s"></iframe>' % YT},
                follow_redirects=True)
with app.app_context():
    p = db.session.get(NewsPost, POST_ID)
    check("PASTING AN IFRAME IN THE VIDEO BOX STORES NO MARKUP",
          "<" not in p.video_url and ">" not in p.video_url, p.video_url)
    check("and still finds the right video",
          p.video_url.endswith(YT), p.video_url)

r = client.post("/admin/news_post/%d/video" % POST_ID,
                data={"video_url": "https://example.com/clip.mp4"},
                follow_redirects=True)
check("a link that is neither provider is refused",
      b"does not look like a YouTube or Vimeo link" in r.data)
with app.app_context():
    check("and the video already there is untouched",
          db.session.get(NewsPost, POST_ID).video_url.endswith(YT))

r = client.post("/admin/news_post/%d/video" % POST_ID,
                data={"video_url": ""}, follow_redirects=True)
with app.app_context():
    p = db.session.get(NewsPost, POST_ID)
    check("clearing the box removes the video",
          p.video_url == "" and p.video_thumb == "", str(p.video_url))

# a fetch that fails must still save the video
FETCH_WORKS[0] = False
client.post("/admin/news_post/%d/video" % POST_ID,
            data={"video_url": "https://vimeo.com/123456789"},
            follow_redirects=True)
with app.app_context():
    p = db.session.get(NewsPost, POST_ID)
    check("A POSTER THAT CANNOT BE FETCHED DOES NOT LOSE THE VIDEO",
          p.video_url == "https://vimeo.com/123456789" and p.video_thumb == "",
          "%s / %s" % (p.video_url, p.video_thumb))
FETCH_WORKS[0] = True

# =====================================================================
# The public page: a poster, no third-party anything, until a click
# =====================================================================
client.post("/admin/news_post/%d/video" % POST_ID,
            data={"video_url": "https://www.youtube.com/watch?v=%s" % YT},
            follow_redirects=True)
page = client.get("/news/video-post").data.decode("utf-8")
check("the page carries a play button", 'class="video-play"' in page)
check("with the poster served from THIS site",
      '/static/uploads/poster-%s.jpg' % YT in page, page[:0])
check("NO IFRAME IS IN THE DELIVERED HTML", "<iframe" not in page)
check("no request to youtube-nocookie can happen before a click: the "
      "player URL is only in a data attribute",
      'data-embed="https://www.youtube-nocookie.com/embed/%s' % YT in page)
check("nothing else on the page points at a third party",
      "i.ytimg.com" not in page and "player.vimeo.com" not in page
      and "youtube.com/embed" not in page.replace("youtube-nocookie.com", ""))
check("and there is a no-script way to watch it",
      "<noscript>" in page and "https://www.youtube.com/watch?v=%s" % YT
      in page)

# =====================================================================
# Pasting an embed code into a BODY field — the failure this replaces
# =====================================================================
check("body_embed_problem spots an iframe",
      body_embed_problem("Hello <iframe src='x'></iframe>") == "iframe")
check("and a script", body_embed_problem("<script>alert(1)</script>")
      == "script")
check("and an object/embed", body_embed_problem("<embed src='x'>")
      == "embed")
check("but leaves ordinary writing alone",
      body_embed_problem("Five < ten, and 3<4 too. <3") is None)
check("and an empty field", body_embed_problem("", None) is None)

for path, data, label in (
        ("/admin/news/%d/edit" % POST_ID,
         {"title": "Video post", "published_date": "2026-04-01",
          "summary": "", "body": 'See <iframe src="https://www.youtube.com/'
                                 'embed/x"></iframe>', "published": "on"},
         "news body"),
        ("/admin/events/%d/edit" % EV_ID,
         {"title": "Video event", "event_date": "2026-09-01",
          "description": '<iframe src="x"></iframe>', "summary": ""},
         "event description"),
        ("/admin/journey/%d/edit" % MS_ID,
         {"title": "Video milestone", "year": "2026",
          "summary": "<script>x</script>", "outcome": "", "amount": ""},
         "milestone summary"),
        ("/admin/campaigns/%d/edit" % CAMP_ID,
         {"title": "Video campaign", "description": '<iframe src="x"></iframe>',
          "target": "", "fee": ""},
         "campaign description")):
    r = client.post(path, data=data, follow_redirects=True)
    check("%s: an embed code is refused" % label,
          b"looks like a" in r.data and b"embed code pasted" in r.data)
    check("%s: and the message points at the video box" % label,
          b"Use the Video box" in r.data)

with app.app_context():
    check("none of that markup reached the database",
          "<iframe" not in (db.session.get(NewsPost, POST_ID).body or "")
          and "<iframe" not in (db.session.get(Event, EV_ID).description or "")
          and "<script" not in (db.session.get(Milestone, MS_ID).summary or "")
          and "<iframe" not in
          (db.session.get(Campaign, CAMP_ID).description or ""))

# =====================================================================
# The campaign's own field, which is not the shared partial
# =====================================================================
r = client.post("/admin/campaigns/%d/edit" % CAMP_ID,
                data={"title": "Video campaign", "description": "Words.",
                      "target": "", "fee": "",
                      "video_url": "https://vimeo.com/987654321"},
                follow_redirects=True)
with app.app_context():
    c = db.session.get(Campaign, CAMP_ID)
    check("a campaign stores its video too",
          c.video_url == "https://vimeo.com/987654321", c.video_url)
r = client.post("/admin/campaigns/%d/edit" % CAMP_ID,
                data={"title": "Video campaign", "description": "Words.",
                      "target": "", "fee": "", "video_url": "nonsense"},
                follow_redirects=True)
check("and refuses a link that is neither provider",
      b"does not look like a YouTube or Vimeo link" in r.data)

# =====================================================================
# The feature flag
# =====================================================================
with app.app_context():
    FeatureFlag.query.filter_by(name="video").first().enabled = False
    db.session.commit()
page = client.get("/news/video-post").data.decode("utf-8")
check("FLAG OFF: no player on the public page",
      'class="video-play"' not in page)
check("flag off: and no player URL either",
      "youtube-nocookie" not in page)
form = client.get("/admin/news/%d/edit" % POST_ID).data.decode("utf-8")
check("flag off: the admin box is gone", 'name="video_url"' not in form)
r = client.post("/admin/news_post/%d/video" % POST_ID,
                data={"video_url": "https://youtu.be/%s" % YT},
                follow_redirects=True)
check("flag off: and the route refuses to save",
      b"Video saved" not in r.data)
with app.app_context():
    FeatureFlag.query.filter_by(name="video").first().enabled = True
    db.session.commit()
check("flag back on: the player returns",
      'class="video-play"' in client.get("/news/video-post")
      .data.decode("utf-8"))

# =====================================================================
# Access
# =====================================================================
r = anon.post("/admin/news_post/%d/video" % POST_ID,
              data={"video_url": "https://youtu.be/%s" % YT})
check("anon POST video -> login redirect",
      r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
      str(r.status_code))

# ---- teardown
with app.app_context():
    db.session.remove()
    db.engine.dispose()
for suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + suffix):
        os.remove(TEST_DB + suffix)
check("test db deleted", not os.path.isfile(TEST_DB))

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
