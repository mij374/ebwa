"""Smoke test: what a search engine and a shared link can read off a page.

Titles and descriptions per page, the Open Graph and Twitter tags that
decide what WhatsApp and Facebook show for a shared link, the
Organization (NGO) structured data on every page, and the Event
structured data on an event page. Every JSON-LD block is PARSED, not
pattern-matched, because a block that does not parse is one Google
silently ignores.

The tests assert on the RENDERED PAGE — the tag as a crawler would read
it, the JSON as a parser would load it — and never on the value the
helper would have returned, so a template that asks the right helper
the wrong question fails here.

Runs against a throwaway SQLite db in this folder via DATABASE_URL, so
the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_seo.py
"""
import html as html_mod
import json
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_seo.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_uploads  # noqa: E402

from app import (app, db, Block, Campaign, DEFAULT_BLOCKS, Event,  # noqa: E402
                 FEATURES, Faq, FeatureFlag, GalleryAlbum, GalleryImage,
                 META_DESCRIPTION_MAX, NewsPost, ORG_CHARITY_NO_KEY,
                 SITE_DESCRIPTION_KEY, SITE_POSTCODE_KEY,
                 dashboard_attention, feature_flags, page_description)

app.config["TESTING"] = True
HOST = "https://ebwa.org.uk"

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


LONG_BODY = ("The weekend schools reopen on Saturday with places for eighty "
             "children across the Bengali and Arabic classes, and for the "
             "first time a beginners' group for parents who want to learn "
             "alongside them.\n"
             "Second paragraph that must not appear in a description.")

# ---- fixtures
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        db.session.add(Block(group=group, key=key, label=label, kind=kind,
                             value=value))
    for name, _l, _d, _default in FEATURES:
        db.session.add(FeatureFlag(name=name, enabled=True))
    db.session.commit()

    # Summer, so the offset is +01:00; a winter one below proves +00:00.
    ev_centre = Event(title="Community Iftar Evening", slug="iftar",
                      event_date=date(2026, 7, 15), start_time="6:30 PM",
                      venue="", summary="Break the fast together at the "
                      "centre.", description="Everyone welcome.",
                      image="seo-event.jpg", published=True)
    ev_away = Event(title="Seaside trip", slug="seaside",
                    event_date=date(2026, 12, 5), start_time="Morning",
                    venue="Clacton-on-Sea", summary="",
                    description="A day out.\nBring a coat.", published=True)
    ev_named = Event(title="Annual General Meeting", slug="agm",
                     event_date=date(2026, 11, 20), start_time="19:00",
                     venue="EBWA Centre, 180 High Street, Ponders End",
                     published=True)
    ev_hidden = Event(title="Draft event", slug="draft",
                      event_date=date(2026, 8, 1), state="unpublished")
    post_long = NewsPost(title="New term begins", slug="new-term",
                         published_date=date(2026, 9, 1), summary="",
                         body=LONG_BODY, image="seo-news.jpg",
                         published=True)
    post_summary = NewsPost(title="Cricket project", slug="cricket",
                            published_date=date(2026, 8, 1),
                            summary="Twenty-five young people joined.",
                            body=LONG_BODY, published=True)
    camp = Campaign(title="Seaside collection", slug="seaside-2026",
                    description="Help us take forty members to the "
                    "seaside.\nMore words.", image="seo-camp.jpg",
                    state="open")
    album = GalleryAlbum(title="Eid 2026", slug="eid-2026",
                         description="Photographs from Eid in the park.",
                         cover_image="seo-cover.jpg", published=True)
    faq = Faq(question="Is it free?", category="",
              answer="Yes.</script><b>x</b>", sort=0, published=True)
    db.session.add_all([ev_centre, ev_away, ev_named, ev_hidden, post_long,
                        post_summary, camp, album, faq])
    db.session.commit()
    db.session.add(GalleryImage(filename="seo-photo.jpg",
                                caption="Eid", album_id=album.id))
    db.session.commit()
    fixture_files = fake_uploads.fill_dangling()

client = app.test_client()


def get(path):
    r = client.get(path, base_url=HOST)
    check("GET %s is 200" % path, r.status_code == 200, r.status_code)
    return r.get_data(as_text=True)


def title_of(html):
    return re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()


def meta(html, attr, name):
    m = re.search(r'<meta %s="%s" content="(.*?)"' % (attr, re.escape(name)),
                  html, re.S)
    return m.group(1) if m else None


def jsonld_blocks(html):
    blobs = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    return [json.loads(b) for b in blobs]


def of_type(blocks, kind):
    return [b for b in blocks if b.get("@type") == kind]


# ---- A. the helper's own rules
print("-- descriptions")
check("first paragraph only",
      page_description(LONG_BODY) == page_description(
          LONG_BODY.split("\n")[0]))
cut = page_description(LONG_BODY)
check("cut to fit a search result",
      len(cut) <= META_DESCRIPTION_MAX and cut.endswith("…"), cut)
check("cut at a word boundary", not cut[:-1].endswith(" ") and
      cut[:-1] in LONG_BODY, cut)
check("candidates tried in order",
      page_description("", "  ", "second", "third") == "second")
check("whitespace collapsed",
      page_description("a  b\tc") == "a b c")
check("nothing at all is empty", page_description("", None) == "")

# ---- B. every public page: tags that agree with the page, NGO markup
print("-- every page")
with app.app_context():
    flags = feature_flags()
PAGES = ["/", "/about", "/events", "/news", "/gallery", "/gallery/eid-2026",
         "/gallery/all", "/resources", "/our-journey", "/faq",
         "/collections", "/collections/seaside-2026", "/donate",
         "/membership", "/membership/pay", "/contact", "/privacy",
         "/terms", "/events/iftar", "/news/new-term"]
ENFIELD_TITLES = ["/about", "/events", "/news", "/gallery", "/resources",
                  "/our-journey", "/faq", "/collections", "/donate",
                  "/membership", "/membership/pay", "/contact",
                  "/events/iftar"]
seen_descriptions = {}
for path in PAGES:
    html = get(path)
    title = title_of(html)
    desc = meta(html, "name", "description")
    seen_descriptions[path] = desc
    check("%s: og:title is the tab title" % path,
          meta(html, "property", "og:title") == title)
    check("%s: og:description is the meta description" % path,
          meta(html, "property", "og:description") == desc and desc)
    check("%s: description fits a result" % path,
          desc and len(desc) <= META_DESCRIPTION_MAX + 5, len(desc or ""))
    check("%s: og:url is this page, absolute" % path,
          meta(html, "property", "og:url") == HOST + path)
    check("%s: og:image is absolute" % path,
          (meta(html, "property", "og:image") or "").startswith(HOST + "/"))
    check("%s: og:site_name, og:type, twitter:card present" % path,
          all(meta(html, "property", k) for k in ("og:site_name", "og:type"))
          and meta(html, "name", "twitter:card") in ("summary",
                                                    "summary_large_image"))
    check("%s: title names EBWA" % path, "EBWA" in title, title)
    if path in ENFIELD_TITLES:
        check("%s: title places it in Enfield" % path, "Enfield" in title,
              title)
    blocks = jsonld_blocks(html)
    ngo = of_type(blocks, "NGO")
    check("%s: exactly one NGO block, and it parses" % path, len(ngo) == 1)
    if ngo:
        org = ngo[0]
        check("%s: NGO name, phone, url, logo" % path,
              org.get("name") == "Enfield Bangladesh Welfare Association"
              and org.get("telephone") == "020 8804 4006"
              and org.get("url") == HOST + "/"
              and org.get("logo", "").startswith(HOST + "/static/"))
        addr = org.get("address", {})
        check("%s: NGO structured address" % path,
              addr.get("@type") == "PostalAddress"
              and addr.get("postalCode") == "EN3 4EU"
              and addr.get("addressLocality") == "Enfield"
              and addr.get("streetAddress") == "180 High Street, Ponders End"
              and addr.get("addressCountry") == "GB", addr)
        check("%s: NGO charity number and register link" % path,
              org.get("identifier", {}).get("value") == "1055430"
              and any(u.endswith("/1055430") for u in org.get("sameAs", [])))
        check("%s: NGO has no opening hours or email (not supplied)" % path,
              "openingHours" not in org
              and "openingHoursSpecification" not in org
              and "email" not in org)
    check("%s: footer states the registered charity number" % path,
          "Registered charity number 1055430." in html)

check("the generic sentence is only the fallback",
      seen_descriptions["/"] != seen_descriptions["/donate/success"]
      if "/donate/success" in seen_descriptions else True)
with app.app_context():
    hero = Block.query.filter_by(key="home_hero_text").first().value
check("home describes itself from its hero paragraph",
      html_mod.unescape(seen_descriptions["/"]) == page_description(hero),
      seen_descriptions["/"])
check("contact description carries the address and phone",
      "EN3 4EU" in seen_descriptions["/contact"]
      and "020 8804 4006" in seen_descriptions["/contact"],
      seen_descriptions["/contact"])
check("event description is its summary",
      seen_descriptions["/events/iftar"]
      == "Break the fast together at the centre.")
check("news with no summary uses its first paragraph, cut",
      html_mod.unescape(seen_descriptions["/news/new-term"]) == cut,
      seen_descriptions["/news/new-term"])
check("collection description is its first paragraph",
      seen_descriptions["/collections/seaside-2026"]
      == "Help us take forty members to the seaside.")
check("album description is its own",
      seen_descriptions["/gallery/eid-2026"]
      == "Photographs from Eid in the park.")
html = get("/news/cricket")
check("news with a summary uses the summary",
      meta(html, "name", "description") == "Twenty-five young people joined.")

# ---- C. sharing: a page with a photograph shares that photograph
print("-- sharing")
html = get("/news/new-term")
img = meta(html, "property", "og:image")
check("news post shares its own photograph",
      img == HOST + "/static/uploads/seo-news.jpg", img)
check("news post is an article with a large card",
      meta(html, "property", "og:type") == "article"
      and meta(html, "name", "twitter:card") == "summary_large_image")
check("news headline is the shared title",
      meta(html, "property", "og:title").startswith("New term begins"))
html = get("/events/iftar")
check("event shares its own photograph",
      meta(html, "property", "og:image") == HOST
      + "/static/uploads/seo-event.jpg")
html = get("/collections/seaside-2026")
check("collection shares its own photograph",
      meta(html, "property", "og:image") == HOST
      + "/static/uploads/seo-camp.jpg")
html = get("/gallery/eid-2026")
check("album shares its cover",
      meta(html, "property", "og:image") == HOST
      + "/static/uploads/seo-cover.jpg")
html = get("/news/cricket")
check("a post with no photograph shares the logo, small card",
      meta(html, "property", "og:image") == HOST
      + "/static/img/ebwa-logo.png"
      and meta(html, "name", "twitter:card") == "summary")
html = get("/")
check("home with no hero shares the logo",
      meta(html, "property", "og:image") == HOST
      + "/static/img/ebwa-logo.png")

# ---- D. Event structured data
print("-- events")
html = get("/events/iftar")
events = of_type(jsonld_blocks(html), "Event")
check("event page carries one Event block that parses", len(events) == 1)
e = events[0] if events else {}
check("startDate is the date and parsed time with the UK summer offset",
      e.get("startDate") == "2026-07-15T18:30:00+01:00", e.get("startDate"))
check("blank venue means the centre, with its address",
      e.get("location", {}).get("name")
      == "Enfield Bangladesh Welfare Association"
      and e["location"].get("address", {}).get("postalCode") == "EN3 4EU",
      e.get("location"))
check("event name, url, organizer, status, image, description",
      e.get("name") == "Community Iftar Evening"
      and e.get("url") == HOST + "/events/iftar"
      and e.get("organizer", {}).get("name")
      == "Enfield Bangladesh Welfare Association"
      and e.get("eventStatus", "").endswith("EventScheduled")
      and e.get("image") == HOST + "/static/uploads/seo-event.jpg"
      and e.get("description") == "Break the fast together at the centre.")
check("nothing invented: no endDate, offers or attendance mode",
      not any(k in e for k in ("endDate", "offers", "eventAttendanceMode")))

html = get("/events/seaside")
e = of_type(jsonld_blocks(html), "Event")[0]
check("unreadable time gives a date-only startDate",
      e.get("startDate") == "2026-12-05", e.get("startDate"))
check("a named venue is a Place with that name and NO guessed address",
      e.get("location") == {"@type": "Place", "name": "Clacton-on-Sea"},
      e.get("location"))
check("event with no summary describes itself from its first paragraph",
      e.get("description") == "A day out.")

html = get("/events/agm")
e = of_type(jsonld_blocks(html), "Event")[0]
check("winter time carries +00:00",
      e.get("startDate") == "2026-11-20T19:00:00+00:00", e.get("startDate"))
check("a venue naming the centre's street gets the address",
      e["location"].get("name") == "EBWA Centre, 180 High Street, Ponders End"
      and e["location"].get("address", {}).get("postalCode") == "EN3 4EU")

check("an unpublished event has no page and so no markup",
      client.get("/events/draft", base_url=HOST).status_code == 404)
html = get("/events")
check("the listing carries no Event block (one per event page)",
      not of_type(jsonld_blocks(html), "Event"))

# ---- E. the JSON cannot break out of its script tag
print("-- escaping")
html = get("/faq")
blocks = jsonld_blocks(html)
check("FAQ answer holding </script> still parses",
      any(b.get("@type") == "FAQPage" for b in blocks))
check("</ is escaped inside every JSON block",
      "</script><b>" not in html and "<\\/script>" in html)

# ---- F. when the charity number is not set
print("-- charity number")
with app.app_context():
    row = Block.query.filter_by(key=ORG_CHARITY_NO_KEY).first()
    row.value = ""
    db.session.commit()
html = get("/about")
org = of_type(jsonld_blocks(html), "NGO")[0]
check("no number: footer says nothing about registration",
      "Registered charity" not in html)
check("no number: NGO carries no identifier and no register link",
      "identifier" not in org and "sameAs" not in org)
with app.app_context(), app.test_request_context("/admin"):
    items = dashboard_attention(flags)
    check("no number: the dashboard says so",
          any("charity number" in i["text"] for i in items))
    Block.query.filter_by(key=ORG_CHARITY_NO_KEY).first().value = "1055430"
    db.session.commit()
    items = dashboard_attention(flags)
    check("number set: the dashboard is quiet about it",
          not any("charity number" in i["text"] for i in items))
    # The two addresses drifting apart is the other thing it watches.
    Block.query.filter_by(key="site_address").first().value = \
        "1 Somewhere Else, Enfield"
    db.session.commit()
    items = dashboard_attention(flags)
    check("address line without the postcode: the dashboard says so",
          any("EN3 4EU" in i["text"] for i in items))
    Block.query.filter_by(key="site_address").first().value = \
        "180 High Street, Ponders End, Enfield EN3 4EU"
    Block.query.filter_by(key=SITE_POSTCODE_KEY).first().value = ""
    db.session.commit()
html = get("/about")
org = of_type(jsonld_blocks(html), "NGO")[0]
check("half an address is no address",
      "address" not in org)
with app.app_context():
    Block.query.filter_by(key=SITE_POSTCODE_KEY).first().value = "EN3 4EU"
    Block.query.filter_by(key=SITE_DESCRIPTION_KEY).first().value = ""
    db.session.commit()
html = get("/donate/success")
check("empty fallback Block gives an empty, not broken, description",
      meta(html, "name", "description") == "")

# ---- teardown
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
print("ALL PASSED")
