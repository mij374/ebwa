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
                 video_embed_url, video_watch_url, VIDEO_POSITIONS,
                 VIDEO_POSITION_DEFAULT, VIDEO_POSITION_KEYS,
                 clean_video_position, video_of, video_position_for,
                 ContentImage, IMAGE_POSITIONS, clean_media_position)

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
# A VIDEO LEADS, IT DOES NOT DISPLACE. The photograph is the item's
# identity — it is on the card, the listing and the homepage strip — so
# adding a video must never take it off the page. This is the bug that
# was reported on collections: the video replaced the picture.
# =====================================================================
with app.app_context():
    c = db.session.get(Campaign, CAMP_ID)
    # Active again: the edits above posted no `active` checkbox, which
    # is how an unticked box reads, and an inactive campaign has no
    # public page to look at.
    c.active = True
    c.image = "campaign-photo.jpg"
    # The edit-form posts above send no show_image_on_page box, which is
    # how an unticked one reads — so the picture is off the page by the
    # time we get here. Put it back: this section is about the video not
    # displacing the photograph, not about the switch.
    c.show_image_on_page = True
    c.video_url = "https://vimeo.com/123456789"
    c.video_thumb = "campaign-poster.jpg"
    db.session.commit()
page = client.get("/collections/video-campaign").data.decode("utf-8")
check("campaign: the player is there", 'class="video-play"' in page)
check("CAMPAIGN: AND THE PHOTOGRAPH IS STILL THERE",
      "campaign-photo.jpg" in page, page[page.find("event-detail"):][:400])
check("campaign: with the fetched still as the poster",
      "campaign-poster.jpg" in page)

# ...but not the same picture twice, which is what happens when no
# still could be fetched and the photo is doing duty as the poster.
with app.app_context():
    db.session.get(Campaign, CAMP_ID).video_thumb = ""
    db.session.commit()
page = client.get("/collections/video-campaign").data.decode("utf-8")
check("campaign: with no fetched still, the photo becomes the poster",
      'class="video-play"' in page and "campaign-photo.jpg" in page)
check("campaign: and is not ALSO shown underneath",
      page.count("campaign-photo.jpg") == 1,
      str(page.count("campaign-photo.jpg")))

# The card contexts never had the bug, and must not gain it.
with app.app_context():
    db.session.get(Campaign, CAMP_ID).video_thumb = "campaign-poster.jpg"
    db.session.commit()
listing = client.get("/collections").data.decode("utf-8")
check("campaign: the LISTING CARD still uses the photograph",
      "campaign-photo.jpg" in listing)
check("campaign: and not the video poster",
      "campaign-poster.jpg" not in listing)

# ---- the same rule inside the rich-content macro
with app.app_context():
    p = db.session.get(NewsPost, POST_ID)
    p.image = "news-photo.jpg"
    p.video_url = "https://www.youtube.com/watch?v=%s" % YT
    p.video_thumb = "news-poster.jpg"
    db.session.commit()
page = client.get("/news/video-post").data.decode("utf-8")
check("news: the video leads and the poster is the fetched still",
      'class="video-play"' in page and "news-poster.jpg" in page)
with app.app_context():
    db.session.get(NewsPost, POST_ID).video_thumb = ""
    db.session.commit()
page = client.get("/news/video-post").data.decode("utf-8")
check("news: with no still, the post's own photo becomes the poster",
      "news-photo.jpg" in page)
check("news: and is not repeated in the strip below",
      page.count("news-photo.jpg") == 1,
      str(page.count("news-photo.jpg")))

# =====================================================================
# Our Journey: the milestone video, end to end
# =====================================================================
with app.app_context():
    m = db.session.get(Milestone, MS_ID)
    m.video_url = ""
    m.video_thumb = ""
    db.session.commit()
form = client.get("/admin/journey/%d/edit" % MS_ID).data.decode("utf-8")
check("MILESTONE: the video box IS on the admin form",
      'name="video_url"' in form)
check("milestone: posting to the shared route saves it",
      b"Video saved" in client.post(
          "/admin/milestone/%d/video" % MS_ID,
          data={"video_url": "https://youtu.be/%s" % YT},
          follow_redirects=True).data)
with app.app_context():
    m = db.session.get(Milestone, MS_ID)
    check("milestone: stored canonically",
          m.video_url == "https://www.youtube.com/watch?v=%s" % YT,
          m.video_url)
page = client.get("/our-journey").data.decode("utf-8")
check("milestone: the player is on Our Journey", 'class="video-play"' in page)
# A NEW milestone has nothing to hang a video off yet, the same as its
# photos and its layout — so the form says so rather than offering a box
# that cannot work.
new_form = client.get("/admin/journey/new").data.decode("utf-8")
check("milestone: a new one has no video box, like its photos",
      'name="video_url"' not in new_form)
check("milestone: and the hint says to save first, naming the video",
      "Save this first" in " ".join(new_form.split())
      and "add a video" in " ".join(new_form.split()),
      " ".join(new_form.split())[:0])

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

# ---- WHERE the video sits ---------------------------------------------
# Three fixed positions, defaulting to the top so nothing moved for
# content that already had a video. An arbitrary order would mean making
# a video another attachment on ContentImage — a much larger change, and
# the note in CLAUDE.md says when that becomes the right one.
print()
print("---- video position")
check("three positions, the top one first and default",
      [k for k, _l, _d in VIDEO_POSITIONS]
      == ["lead", "after_text", "end"]
      and VIDEO_POSITION_DEFAULT == "lead",
      str(VIDEO_POSITION_KEYS))
check("every position has a label and an explanation",
      all(len(p) == 3 and all(p) for p in VIDEO_POSITIONS))
for junk in ("", "top", "LEAD", "middle", None):
    check("an unrecognised position %r falls back to the top" % junk,
          clean_video_position(junk) == "lead")

with app.app_context():
    post = NewsPost.query.first()
    check("a post that has never been touched is at the top",
          video_position_for("news_post", post.id) == "lead",
          video_position_for("news_post", post.id))
    check("...and video_of says so, which is what the macro reads",
          (video_of(post) or {}).get("position") == "lead")
    post.video_position = "end"
    db.session.commit()
    check("the column is what video_of reports",
          (video_of(post) or {}).get("position") == "end")
    post.video_position = "nonsense"      # a hand-edited row
    db.session.commit()
    check("a nonsense value in the database renders at the top, not blank",
          (video_of(post) or {}).get("position") == "lead")
    post.video_position = "lead"
    db.session.commit()

# The rendered page: the player must move, and no photograph may be lost
# doing it. Three positions against the classic preset, which is the one
# where the lead slot changes hands.
with app.app_context():
    pos_post = NewsPost(title="Positioned", slug="positioned",
                        body="First paragraph." + chr(10) * 2
                             + "Second paragraph.",
                        published=True, published_date=date.today(),
                        layout="classic", video_url="https://www.youtube.com/watch?v=%s" % YT,
                        video_thumb="poster-fixture.png")
    db.session.add(pos_post)
    db.session.commit()
    for i in range(2):
        db.session.add(ContentImage(owner_type="news_post",
                                    owner_id=pos_post.id,
                                    filename="photo%d.png" % i,
                                    alt_text="Photo %d" % i, sort=i))
    db.session.commit()
    pos_id = pos_post.id

seen = {}
for position in VIDEO_POSITION_KEYS:
    with app.app_context():
        db.session.get(NewsPost, pos_id).video_position = position
        db.session.commit()
    html = client.get("/news/positioned").data.decode("utf-8")
    main = html.split("<main", 1)[1].split("</main>", 1)[0]
    seen[position] = main
    check("%s: exactly one player" % position,
          main.count('class="video-play"') == 1,
          str(main.count('class="video-play"')))
    check("%s: BOTH photographs are still on the page" % position,
          main.count("photo0.png") >= 1 and main.count("photo1.png") >= 1,
          "a video must never cost a photograph its place")

def at(html, needle):
    return html.index(needle)

check("lead: the player comes before the photographs",
      at(seen["lead"], "video-play") < at(seen["lead"], "photo0.png"))
check("end: the player comes after every photograph",
      at(seen["end"], "video-play") > at(seen["end"], "photo1.png"))
check("after_text: between the two",
      at(seen["lead"], "video-play")
      < at(seen["after_text"], "video-play")
      < at(seen["end"], "video-play"))
check("moving it changes the page",
      seen["lead"] != seen["after_text"] != seen["end"])

# The admin form offers it, and saving it sticks.
client.post("/admin/login", data={"email": "netbus@example.com", "password": PW})
form = client.get("/admin/news/%d/edit" % pos_id).data.decode("utf-8")
check("the admin form offers the position",
      'name="video_position"' in form
      and all(label in form for _k, label, _d in VIDEO_POSITIONS))
# The owner_type in the URL is the one CONTENT_OWNERS uses — news_post,
# not news. Posting to the wrong one 404s, which an assertion that only
# looks at the stored value cannot tell from a save that did nothing.
SAVE = "/admin/news_post/%d/video" % pos_id
r = client.post(SAVE, data={"video_url": "https://www.youtube.com/watch?v=%s" % YT,
                            "video_position": "after_text"},
                follow_redirects=False)
check("the save route accepts the form", r.status_code == 302,
      str(r.status_code))
with app.app_context():
    check("saving the form stores it",
          video_position_for("news_post", pos_id) == "after_text",
          video_position_for("news_post", pos_id))
r = client.post(SAVE, data={"video_url": "https://www.youtube.com/watch?v=%s" % YT,
                            "video_position": "made up"},
                follow_redirects=True)
with app.app_context():
    check("a position nobody offered goes to the top rather than sticking",
          video_position_for("news_post", pos_id) == "lead",
          video_position_for("news_post", pos_id))
with app.app_context():
    last = AuditLog.query.order_by(AuditLog.id.desc()).first()
    check("moving the video is audit-logged, naming where it went",
          "video" in (last.summary or "").lower()
          and "position" in (last.summary or "").lower(), last.summary)

# ---- COLLECTIONS HONOUR IT TOO -----------------------------------------
# This page does NOT use the rich-content macro: campaigns are not
# rich-content owners, so it renders the player itself and has to honour
# the position by hand. It did not, and the position tests here only
# ever rendered a news post — the campaign checks asserted on the MODEL
# (c.video_url == ...) and, on the page, only that the player and the
# photograph were both PRESENT. Presence is not position. So the field
# existed, the admin select saved it, and the page ignored it.
#
# Everything below asserts on the RENDERED PAGE and on ORDER.
print()
print("---- collections, which render the player themselves")
with app.app_context():
    c = db.session.get(Campaign, CAMP_ID)
    c.state = "open"
    c.image = "campaign-photo.jpg"
    c.show_image_on_page = True
    c.video_url = "https://vimeo.com/123456789"
    c.video_thumb = "campaign-poster.jpg"
    c.description = "First para." + chr(10) * 2 + "Second para."
    db.session.commit()


def collection_main():
    html = client.get("/collections/video-campaign").data.decode("utf-8")
    return html.split("<main", 1)[1].split("</main>", 1)[0]


def set_campaign(**fields):
    with app.app_context():
        c = db.session.get(Campaign, CAMP_ID)
        for k, v in fields.items():
            setattr(c, k, v)
        db.session.commit()


camp_seen = {}
for position in VIDEO_POSITION_KEYS:
    set_campaign(video_position=position)
    main = collection_main()
    camp_seen[position] = main
    check("collection/%s: exactly one player" % position,
          main.count('class="video-play"') == 1,
          str(main.count('class="video-play"')))
    check("collection/%s: the photograph is still there" % position,
          "campaign-photo.jpg" in main)
    check("collection/%s: the description is still there" % position,
          "Second para." in main)

check("COLLECTIONS ACTUALLY MOVE THE VIDEO, they do not just store it",
      camp_seen["lead"].index("video-play")
      < camp_seen["after_text"].index("video-play")
      < camp_seen["end"].index("video-play"),
      "the collection page rendered the video in the same place for "
      "every setting")
check("collection/lead: above the photograph",
      camp_seen["lead"].index("video-play")
      < camp_seen["lead"].index("campaign-photo.jpg"))
check("collection/after_text: below the description",
      camp_seen["after_text"].index("video-play")
      > camp_seen["after_text"].index("Second para."))
check("collection/after_text: and above the payment form",
      camp_seen["after_text"].index("video-play")
      < camp_seen["after_text"].index("Pay securely"))
check("collection/end: below the payment form",
      camp_seen["end"].index("video-play")
      > camp_seen["end"].index("Pay securely"))

# ---- the cover and the page picture are two different jobs
set_campaign(video_position="lead", show_image_on_page=False)
main = collection_main()
check("unticking it takes the picture OFF the collection page",
      "campaign-photo.jpg" not in main)
check("...but the player is untouched", main.count('class="video-play"') == 1)
listing = client.get("/collections").data.decode("utf-8")
check("...AND THE COVER IS UNTOUCHED on the listing",
      "campaign-photo.jpg" in listing)
home = client.get("/").data.decode("utf-8")
check("...and on the homepage strip",
      "campaign-photo.jpg" in home or "Video campaign" not in home)

set_campaign(show_image_on_page=True)
check("ticking it back puts the picture back",
      "campaign-photo.jpg" in collection_main())

# With the image off, the video still moves through all three places.
set_campaign(show_image_on_page=False)
noimg = {}
for position in VIDEO_POSITION_KEYS:
    set_campaign(video_position=position)
    noimg[position] = collection_main()
    check("no page image, %s: still exactly one player" % position,
          noimg[position].count('class="video-play"') == 1)
check("no page image: the video still moves",
      noimg["lead"].index("video-play")
      < noimg["after_text"].index("video-play")
      < noimg["end"].index("video-play"))

# The poster rule still wins: a photo doing duty as the poster is not
# shown twice, whatever the checkbox says.
set_campaign(show_image_on_page=True, video_thumb="", video_position="lead")
main = collection_main()
check("a photo serving as the video's poster is not repeated below it",
      main.count("campaign-photo.jpg") == 1,
      str(main.count("campaign-photo.jpg")))

# The admin form offers the switch and round-trips it.
form = client.get("/admin/campaigns/%d/edit" % CAMP_ID).data.decode("utf-8")
check("the campaign form offers the switch",
      'name="show_image_on_page"' in form)
check("...ticked, because this campaign shows its image",
      'name="show_image_on_page"' in form
      and "checked" in form.split('name="show_image_on_page"', 1)[1][:40])
client.post("/admin/campaigns/%d/edit" % CAMP_ID,
            data={"title": "Video campaign", "description": "Words.",
                  "target": "", "fee": "", "state": "open",
                  "video_url": "https://vimeo.com/123456789",
                  "video_position": "lead"},
            follow_redirects=True)
with app.app_context():
    check("an unticked box really does turn it off",
          db.session.get(Campaign, CAMP_ID).show_image_on_page is False)
form = client.get("/admin/campaigns/%d/edit" % CAMP_ID).data.decode("utf-8")
check("...and the form then shows it unticked",
      "checked" not in form.split('name="show_image_on_page"', 1)[1][:40])
client.post("/admin/campaigns/%d/edit" % CAMP_ID,
            data={"title": "Video campaign", "description": "Words.",
                  "target": "", "fee": "", "state": "open",
                  "show_image_on_page": "on",
                  "video_url": "https://vimeo.com/123456789",
                  "video_position": "lead"},
            follow_redirects=True)
with app.app_context():
    check("and ticking it turns it back on",
          db.session.get(Campaign, CAMP_ID).show_image_on_page is True)

# ---- THE PICTURE MOVES TOO, AND THE TWO NEVER COLLIDE -----------------
# Every combination, asserted on the RENDERED PAGE and on ORDER — the
# gap that let the video bug through was a test that read the stored
# value and a page that ignored it.
print()
print("---- collection image position")
import itertools as _it     # noqa: E402

MARK = {"VIDEO": 'class="video-play"', "image": "campaign-photo.jpg",
        "text": "Second para.", "form": "Pay securely"}


def order_of(main):
    """The order the page actually renders them in."""
    found = [(main.index(needle), name)
             for name, needle in MARK.items() if needle in main]
    return [name for _i, name in sorted(found)]


set_campaign(show_image_on_page=True, video_thumb="campaign-poster.jpg",
             description="First para." + chr(10) * 2 + "Second para.")
grid = {}
for vpos, ipos in _it.product(VIDEO_POSITION_KEYS, VIDEO_POSITION_KEYS):
    set_campaign(video_position=vpos, image_position=ipos)
    main = collection_main()
    grid[(vpos, ipos)] = order_of(main)
    tag = "video %s / image %s" % (vpos, ipos)
    check("%s: exactly one player" % tag,
          main.count('class="video-play"') == 1)
    check("%s: exactly one page picture" % tag,
          main.count("campaign-photo.jpg") == 1,
          str(main.count("campaign-photo.jpg")))
    seq = grid[(vpos, ipos)]
    # each lands where it was told
    for name, pos in (("VIDEO", vpos), ("image", ipos)):
        i = seq.index(name)
        if pos == "lead":
            check("%s: %s is above the text" % (tag, name),
                  i < seq.index("text"), str(seq))
        elif pos == "after_text":
            check("%s: %s is below the text" % (tag, name),
                  i > seq.index("text"), str(seq))
            check("%s: %s is above the form" % (tag, name),
                  i < seq.index("form"), str(seq))
        else:
            check("%s: %s is below the form" % (tag, name),
                  i > seq.index("form"), str(seq))
    if vpos == ipos:
        check("%s: SAME SLOT -> the video comes first" % tag,
              seq.index("VIDEO") < seq.index("image"), str(seq))

check("all nine combinations render both, and no two are the same layout "
      "unless they should be",
      len({tuple(v) for v in grid.values()}) >= 6,
      str(sorted({tuple(v) for v in grid.values()})))

# Hidden beats position: an unticked box means no picture, wherever it
# was told to go.
set_campaign(show_image_on_page=False)
for ipos in VIDEO_POSITION_KEYS:
    set_campaign(image_position=ipos, video_position="lead")
    main = collection_main()
    check("hidden + image_position %s: no picture on the page" % ipos,
          "campaign-photo.jpg" not in main)
    check("hidden + image_position %s: the player is untouched" % ipos,
          main.count('class="video-play"') == 1)
check("...and the cover is still on the listing",
      "campaign-photo.jpg" in client.get("/collections").data.decode("utf-8"))

# The poster rule beats BOTH: a photo doing duty as the still is never
# shown twice, wherever the picture was told to sit.
set_campaign(show_image_on_page=True, video_thumb="")
for ipos in VIDEO_POSITION_KEYS:
    set_campaign(image_position=ipos, video_position="lead")
    main = collection_main()
    check("poster duty + image_position %s: shown once, not twice" % ipos,
          main.count("campaign-photo.jpg") == 1,
          str(main.count("campaign-photo.jpg")))
set_campaign(video_thumb="campaign-poster.jpg")

# The form offers it and round-trips it, including an unticked box.
form = client.get("/admin/campaigns/%d/edit" % CAMP_ID).data.decode("utf-8")
check("the campaign form offers the image position",
      'name="image_position"' in form
      and all(label in form for _k, label, _d in IMAGE_POSITIONS))
client.post("/admin/campaigns/%d/edit" % CAMP_ID,
            data={"title": "Video campaign", "description": "Words.",
                  "target": "", "fee": "", "state": "open",
                  "show_image_on_page": "on", "image_position": "end",
                  "video_url": "https://vimeo.com/123456789",
                  "video_position": "lead"},
            follow_redirects=True)
with app.app_context():
    check("saving stores the image position",
          db.session.get(Campaign, CAMP_ID).image_position == "end",
          db.session.get(Campaign, CAMP_ID).image_position)
client.post("/admin/campaigns/%d/edit" % CAMP_ID,
            data={"title": "Video campaign", "description": "Words.",
                  "target": "", "fee": "", "state": "open",
                  "image_position": "made up",
                  "video_url": "https://vimeo.com/123456789",
                  "video_position": "lead"},
            follow_redirects=True)
with app.app_context():
    c = db.session.get(Campaign, CAMP_ID)
    check("a position nobody offered goes back to the top",
          c.image_position == "lead", c.image_position)
    check("...and unticking the box did not forget the position",
          c.show_image_on_page is False)

# ---- ABOUT HONOURS THE POSITION TOO ------------------------------------
# About stores its settings in Blocks rather than columns, so it is a
# separate path from the four models — and it is the path that broke:
# the dict video_of() needs was hand-built in about.html, which listed
# the field names and was not updated when video_position was added. The
# setting saved correctly and the page ignored it, silently, because the
# unknown-position fallback is "put it at the top".
print()
print("---- About, whose video settings are Blocks")


def set_about_block(key, value):
    with app.app_context():
        block = Block.query.filter_by(key=key).first()
        if block is None:
            block = Block(group="about", key=key, label=key, kind="text")
            db.session.add(block)
        block.value = value
        db.session.commit()


set_about_block("about_video_url", "https://www.youtube.com/watch?v=%s" % YT)
set_about_block("about_video_thumb", "about-poster.png")
set_about_block("about_layout", "classic")
set_about_block("about_body", "First para." + chr(10) * 2 + "Second para.")
with app.app_context():
    for i in range(2):
        db.session.add(ContentImage(owner_type="about", owner_id=0,
                                    filename="aboutphoto%d.png" % i,
                                    alt_text="About photo %d" % i, sort=i))
    db.session.commit()

about_seen = {}
for position in VIDEO_POSITION_KEYS:
    set_about_block("about_video_position", position)
    with app.app_context():
        check("About stores %s" % position,
              video_position_for("about") == position,
              video_position_for("about"))
    main = (client.get("/about").data.decode("utf-8")
            .split("<main", 1)[1].split("</main>", 1)[0])
    about_seen[position] = main
    check("About/%s: exactly one player" % position,
          main.count('class="video-play"') == 1)
    check("About/%s: both photographs still there" % position,
          "aboutphoto0.png" in main and "aboutphoto1.png" in main)

check("ABOUT ACTUALLY MOVES THE VIDEO, it does not just save the setting",
      about_seen["lead"].index("video-play")
      < about_seen["after_text"].index("video-play")
      < about_seen["end"].index("video-play"),
      "About rendered the video in the same place for every setting")
check("About/lead: before the photographs",
      about_seen["lead"].index("video-play")
      < about_seen["lead"].index("aboutphoto0.png"))
check("About/end: after every photograph",
      about_seen["end"].index("video-play")
      > about_seen["end"].rindex("aboutphoto1.png"))

# The root of it: no template may hand-build the dict video_of() reads.
# One that does has to know another module's field names, and will be
# the thing nobody updates.
about_src = open(os.path.join(os.path.dirname(HERE), "templates",
                              "about.html"), encoding="utf-8").read()
check("about.html does not assemble the video dict itself",
      "video_url" not in about_src and "video_thumb" not in about_src,
      "about.html is listing video field names again")

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
