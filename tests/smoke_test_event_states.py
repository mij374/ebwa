"""Smoke test: an event is published, cancelled or unpublished.

The state that matters is CANCELLED: the event keeps its page and its
address, says prominently that it is not going ahead, is listed under
upcoming or past by its date, is left off the homepage strip, and tells
Google EventCancelled. Unpublishing was the only way to say "off"
before, and it handed a visitor holding the poster a 404.

Everything is asserted on the RENDERED page or the parsed JSON-LD, and
the past-date cases are here because the automatic past-event split is
the thing a new state is most likely to fight with.

Runs against a throwaway SQLite db in this folder via DATABASE_URL, so
the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_event_states.py
"""
import json
import os
import re
import sys
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_event_states.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, Block, DEFAULT_BLOCKS, Event,  # noqa: E402
                 EVENT_STATES, FEATURES, FeatureFlag, PUBLIC_EVENT_STATES,
                 User, dashboard_attention, feature_flags)

app.config["TESTING"] = True
HOST = "https://ebwa.org.uk"
PW = "event-state-test-password"

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


TODAY = date.today()
FUTURE = TODAY + timedelta(days=20)
PAST = TODAY - timedelta(days=20)

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
    db.session.add(User(email="netbus@example.com",
                        password_hash=generate_password_hash(PW),
                        role="super_admin"))
    rows = {
        # The slug the admin route would derive from the title, since
        # the round trip below saves through that route.
        "live": Event(title="Eid in the Park", slug="eid-in-the-park",
                      event_date=FUTURE, start_time="2pm",
                      summary="Bring the family.", state="published"),
        "cancelled": Event(title="Seaside Trip", slug="seaside",
                           event_date=FUTURE, start_time="9:00 AM",
                           venue="Clacton-on-Sea",
                           summary="Coach leaves from the centre.",
                           cancel_note="The coach company let us down — "
                                       "we will rebook for the spring.",
                           state="cancelled"),
        "draft": Event(title="Draft Event", slug="draft",
                       event_date=FUTURE, state="unpublished"),
        "past_live": Event(title="Iftar 2026", slug="iftar",
                           event_date=PAST, state="published"),
        "past_cancelled": Event(title="Cancelled Bazaar", slug="bazaar",
                                event_date=PAST, state="cancelled"),
        "past_draft": Event(title="Old Draft", slug="old-draft",
                            event_date=PAST, state="unpublished"),
    }
    db.session.add_all(rows.values())
    db.session.commit()
    IDS = {k: v.id for k, v in rows.items()}
    flags = feature_flags()

client = app.test_client()


def get(path, ok=True):
    r = client.get(path, base_url=HOST)
    if ok:
        check("GET %s is 200" % path, r.status_code == 200, r.status_code)
    return r.status_code, r.get_data(as_text=True)


def main_of(html):
    return html.split("<main", 1)[1].split("</main>", 1)[0]


def event_blocks(html):
    out = []
    for blob in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', html,
            re.S):
        data = json.loads(blob)
        if data.get("@type") == "Event":
            out.append(data)
    return out


def card_for(body, slug):
    """The listing card linking to this slug, or ''."""
    m = re.search(r'<a class="event-card[^"]*" href="/events/%s">(.*?)</a>'
                  % re.escape(slug), body, re.S)
    return m.group(0) if m else ""


# ---- A. the three states are what the constants say
print("-- constants")
check("three states, published first",
      [s for s, _l, _d in EVENT_STATES]
      == ["published", "cancelled", "unpublished"])
check("two of them public", PUBLIC_EVENT_STATES == ("published", "cancelled"))
with app.app_context():
    for key, want in (("live", True), ("cancelled", True), ("draft", False)):
        row = db.session.get(Event, IDS[key])
        check("legacy published flag follows state (%s)" % key,
              row.published is want, str(row.published))
    check("is_public / is_cancelled read the state",
          db.session.get(Event, IDS["cancelled"]).is_public
          and db.session.get(Event, IDS["cancelled"]).is_cancelled
          and not db.session.get(Event, IDS["draft"]).is_public
          and not db.session.get(Event, IDS["live"]).is_cancelled)

# ---- B. the listing: upcoming and past, by date, cancelled marked
print("-- listing")
_, html = get("/events")
body = main_of(html)
up, past = body.split("Past events", 1)
check("published upcoming event is listed under upcoming",
      card_for(up, "eid-in-the-park") != "")
check("cancelled upcoming event is listed under upcoming too",
      card_for(up, "seaside") != "")
check("...marked cancelled on its card",
      "badge-cancelled" in card_for(up, "seaside")
      and "is-cancelled" in card_for(up, "seaside"))
check("published card is NOT marked cancelled",
      "cancelled" not in card_for(up, "eid-in-the-park").lower())
check("unpublished event is not listed anywhere",
      "draft" not in body.lower())
check("past published event is under past events",
      card_for(past, "iftar") != "")
check("past CANCELLED event is under past events, still marked",
      "badge-cancelled" in card_for(past, "bazaar"))
check("past unpublished event is not listed", "old-draft" not in body)
check("cancelled event does not move section because it is cancelled",
      card_for(past, "seaside") == "" and card_for(up, "bazaar") == "")

# ---- C. the homepage promotes published events only
print("-- homepage")
_, html = get("/")
body = main_of(html)
check("homepage strip carries the published upcoming event",
      'href="/events/eid-in-the-park"' in body)
check("homepage strip does NOT carry the cancelled one",
      'href="/events/seaside"' not in body)
check("nor the draft", 'href="/events/draft"' not in body)

# ---- D. the page itself
print("-- the cancelled page")
status, html = get("/events/seaside")
body = main_of(html)
check("cancelled event keeps its page at its address", status == 200)
check("the notice is prominent — before the date and venue",
      "event-cancelled" in body
      and body.index("event-cancelled") < body.index("event-meta"))
check("the notice says it is not going ahead",
      "This event has been cancelled" in body
      and "not going ahead" in body)
check("the admin's note is shown",
      "The coach company let us down" in body)
check("the notice names the date it was due",
      FUTURE.strftime("%A %d %B") in body)
check("the tab title says cancelled first",
      re.search(r"<title>Cancelled: Seaside Trip", html) is not None)
check("the shared title says cancelled first",
      'og:title" content="Cancelled: Seaside Trip' in html)
check("the description says cancelled first",
      'name="description" content="This event has been cancelled. Coach'
      in html)
check("the meta badge says cancelled",
      'class="badge-cancelled">Cancelled' in body)

status, html = get("/events/eid-in-the-park")
body = main_of(html)
check("a published event carries none of it",
      "event-cancelled" not in body and "badge-cancelled" not in body
      and "Cancelled" not in html.split("</head>")[0])

status, html = get("/events/draft", ok=False)
check("an unpublished event is a 404", status == 404, status)
status, html = get("/events/old-draft", ok=False)
check("...past or future", status == 404, status)

status, html = get("/events/bazaar")
body = main_of(html)
check("a past cancelled event keeps its page too", status == 200)
check("and says it did not go ahead as planned",
      "not going ahead" in body and "as planned" in body)
check("with the past badge beside the cancelled one",
      "badge-past" in body and "badge-cancelled" in body)
check("no note, no empty paragraph",
      "<p></p>" not in body)

# ---- E. structured data
print("-- structured data")
_, html = get("/events/seaside")
e = event_blocks(html)
check("cancelled page carries one Event block", len(e) == 1)
e = e[0] if e else {}
check("eventStatus is EventCancelled",
      e.get("eventStatus") == "https://schema.org/EventCancelled",
      e.get("eventStatus"))
check("startDate is KEPT, not blanked",
      e.get("startDate", "").startswith(FUTURE.isoformat()),
      e.get("startDate"))
check("name and location are unchanged",
      e.get("name") == "Seaside Trip"
      and e.get("location", {}).get("name") == "Clacton-on-Sea")
_, html = get("/events/eid-in-the-park")
e = event_blocks(html)[0]
check("a published event is EventScheduled",
      e.get("eventStatus") == "https://schema.org/EventScheduled")
_, html = get("/events/bazaar")
e = event_blocks(html)[0]
check("a past cancelled event is EventCancelled with its past date",
      e.get("eventStatus") == "https://schema.org/EventCancelled"
      and e.get("startDate") == PAST.isoformat())

# ---- F. sitemap
print("-- sitemap")
_, xml = get("/sitemap.xml")
check("sitemap lists the cancelled event", HOST + "/events/seaside" in xml)
check("and the past cancelled one", HOST + "/events/bazaar" in xml)
check("but not the unpublished ones",
      "/events/draft" not in xml and "/events/old-draft" not in xml)

# ---- G. the admin: list pill, form select, dashboard figures
print("-- admin")
# Same host as every other request here, or the session cookie set by
# the login is for a different site and the admin pages redirect.
client.post("/admin/login", base_url=HOST,
            data={"email": "netbus@example.com", "password": PW})
_, html = get("/admin/events")
row = re.search(r"<tr>.*?/events/seaside.*?</tr>", html, re.S).group(0)
check("admin list shows a red Cancelled pill",
      'pill pill-red">Cancelled' in row)
row = re.search(r"<tr>.*?/events/draft.*?</tr>", html, re.S).group(0)
check("and Unpublished for a draft", "Unpublished" in row)
row = re.search(r"<tr>.*?/events/bazaar.*?</tr>", html, re.S).group(0)
check("a past cancelled event reads Cancelled, not Past",
      "Cancelled" in row and ">Past<" not in row)

_, html = get("/admin/events/%d/edit" % IDS["cancelled"])
state_select = re.search(r'<select id="state".*?</select>', html, re.S)
check("form offers the three states",
      state_select is not None
      and state_select.group(0).count('<option value="') == 3
      and 'value="cancelled"' in state_select.group(0))
check("the selected option is the row's current state",
      re.search(r'value="cancelled"\s+selected', html) is not None)
check("the note is in the form",
      'name="cancel_note"' in html and "coach company" in html)
_, html = get("/admin/events/new")
check("a new event defaults to published",
      re.search(r'value="published"\s+selected', html) is not None)

_, html = get("/admin")
check("dashboard counts cancelled events (upcoming and past alike)",
      "2 cancelled" in html)
with app.app_context(), app.test_request_context("/admin"):
    items = dashboard_attention(flags)
    past_note = [i for i in items if "now past" in i["text"]]
    check("past-event nag counts published AND cancelled, not drafts",
          past_note and past_note[0]["text"].startswith("2 events now past"),
          past_note[0]["text"] if past_note else "no item")

# ---- H. cancelling from the form keeps the page, un-cancelling restores
print("-- round trip")
url = "/admin/events/%d/edit" % IDS["live"]
client.post(url, base_url=HOST, data={"title": "Eid in the Park", "event_date":
                       FUTURE.isoformat(), "start_time": "2pm",
                       "state": "cancelled",
                       "cancel_note": "Weather."}, follow_redirects=True)
status, html = get("/events/eid-in-the-park")
check("cancelled from the form: page still there, notice on it",
      status == 200 and "Weather." in html)
_, html = get("/")
check("...and gone from the homepage", 'href="/events/eid-in-the-park"' not in
      main_of(html))
client.post(url, base_url=HOST, data={"title": "Eid in the Park", "event_date":
                       FUTURE.isoformat(), "start_time": "2pm",
                       "state": "published",
                       "cancel_note": "Weather."}, follow_redirects=True)
_, html = get("/events/eid-in-the-park")
check("published again: notice gone, note kept for next time",
      "event-cancelled" not in main_of(html))
with app.app_context():
    check("...the note is still on the row",
          db.session.get(Event, IDS["live"]).cancel_note == "Weather.")
_, html = get("/")
check("...and back on the homepage", 'href="/events/eid-in-the-park"' in main_of(html))

# ---- teardown
with app.app_context():
    db.session.remove()
    db.engine.dispose()
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
