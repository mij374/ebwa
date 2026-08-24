"""Saving an unrelated change must never unpublish something.

An unticked checkbox posts nothing at all, so any form that renders its
"Published" box unticked while editing a live item would take that item
off the site the moment somebody fixed a typo in it. The item would
still be listed in the admin, still say it exists, and simply be gone
from the website — the sort of failure a client discovers weeks later.

Reading the templates is not enough to know this is safe, because what
matters is what a BROWSER would submit. So every edit form here is
fetched, its fields are collected exactly as a browser would collect
them — an unticked box contributing nothing, a ticked one contributing
"on" — and posted straight back unchanged. Whatever the item was, it
must still be.

The opposite is asserted too: unticking the box really does unpublish.
A test that only proved things stay published would pass just as well
against a form that had lost its checkbox altogether.

Run:  python tests/smoke_test_publish_state.py
"""
import os
import sys
from datetime import date
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_publish.db")
for _suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _suffix):
        os.remove(TEST_DB + _suffix)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from werkzeug.datastructures import MultiDict  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from app import (app, db, Block, Campaign, DEFAULT_BLOCKS, Event,  # noqa: E402
                 FEATURES, Faq, FeatureFlag, GalleryAlbum, Milestone,
                 NewsPost, Service, Testimonial, User)

app.config["TESTING"] = True
PW = "publish-state-password"
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


class FormFields(HTMLParser):
    """What a browser would submit from the page's main edit form.

    The main form is the one with no `action`: it posts back to the same
    URL. The extra forms on these pages — the layout picker, the video
    box, a delete button — all name an action of their own, so this
    cannot pick one of those up by accident.
    """

    def __init__(self):
        super().__init__()
        self.in_form = False
        self.fields = []
        self._textarea = None
        self._select = None
        self._option = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.in_form = not a.get("action")
            return
        if not self.in_form:
            return
        if tag == "input":
            kind = (a.get("type") or "text").lower()
            name = a.get("name")
            if not name or kind in ("file", "submit", "button", "image"):
                return
            if kind in ("checkbox", "radio"):
                # THE WHOLE POINT: an unticked box submits nothing.
                if "checked" in a:
                    self.fields.append((name, a.get("value", "on")))
            else:
                self.fields.append((name, a.get("value", "")))
        elif tag == "textarea":
            self._textarea = a.get("name")
            self.fields.append((self._textarea, ""))
        elif tag == "select":
            self._select = a.get("name")
        elif tag == "option" and self._select:
            self._option = a.get("value", "")
            if "selected" in a:
                self.fields.append((self._select, self._option))

    def handle_data(self, data):
        if self.in_form and self._textarea and data.strip():
            name, _old = self.fields[-1]
            self.fields[-1] = (name, data)

    def handle_endtag(self, tag):
        if tag == "form" and self.in_form:
            self.in_form = False
        elif tag == "textarea":
            self._textarea = None
        elif tag == "select":
            # A select with nothing marked selected submits its first
            # option, the way a browser does.
            if self._select and not any(n == self._select
                                        for n, _v in self.fields):
                self.fields.append((self._select, self._option or ""))
            self._select = None


def browser_would_post(html):
    """The name/value pairs, as a MultiDict — a form may repeat a name."""
    parser = FormFields()
    parser.feed(html)
    return MultiDict(parser.fields)


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
    live = {
        "event": Event(title="Live event", slug="live-event",
                       event_date=date(2026, 9, 1), description="Words.",
                       published=True),
        "news": NewsPost(title="Live post", slug="live-post", body="Words.",
                         published_date=date(2026, 4, 1), published=True),
        "milestone": Milestone(title="Live milestone", year=2026,
                               summary="Words.", published=True),
        "testimonial": Testimonial(name="Live person", quote="Words.",
                                   published=True),
        "service": Service(title="Live service", description="Words.",
                           icon="*", published=True),
        "faq": Faq(question="Live question?", answer="Words.",
                   published=True),
        "album": GalleryAlbum(title="Live album", slug="live-album",
                              published=True),
        "campaign": Campaign(title="Live campaign", slug="live-campaign",
                             description="Words.", state="closed"),
    }
    db.session.add_all(live.values())
    db.session.commit()
    IDS = {k: v.id for k, v in live.items()}

client = app.test_client()
client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})

# form name -> (edit url, model, the column that decides visibility)
FORMS = {
    "event": ("/admin/events/%d/edit", Event, "published"),
    "news": ("/admin/news/%d/edit", NewsPost, "published"),
    "milestone": ("/admin/journey/%d/edit", Milestone, "published"),
    "testimonial": ("/admin/testimonials/%d/edit", Testimonial, "published"),
    "service": ("/admin/services/%d/edit", Service, "published"),
    "faq": ("/admin/faq/%d/edit", Faq, "published"),
    "album": ("/admin/gallery/albums/%d/edit", GalleryAlbum, "published"),
    # Campaigns are NOT here any more, and their absence is the point:
    # they carry a three-way <select> now, not a tick-box, so the whole
    # failure this file exists for — an unticked box posting nothing,
    # which is indistinguishable from "take it off the site" — cannot
    # happen to them. A select always posts something. They get their
    # own section below instead.
}

for name, (url_pattern, model, column) in FORMS.items():
    url = url_pattern % IDS[name]
    html = client.get(url).data.decode("utf-8")
    fields = browser_would_post(html)

    check("%s: the visibility box is in the form" % name,
          column in fields,
          "browser would post %s" % sorted(fields.keys()))

    # Re-submit exactly what a browser would, with nothing altered.
    client.post(url, data=fields, follow_redirects=True)
    with app.app_context():
        row = db.session.get(model, IDS[name])
        check("%s: SAVING WITHOUT TOUCHING ANYTHING KEEPS IT LIVE" % name,
              getattr(row, column) is True,
              "%s is now %s" % (column, getattr(row, column)))

    # ...and the box still works: drop it, as unticking does.
    off = MultiDict([(n, v) for n, v in fields.items(multi=True)
                     if n != column])
    client.post(url, data=off, follow_redirects=True)
    with app.app_context():
        row = db.session.get(model, IDS[name])
        check("%s: and unticking it really does take it off the site" % name,
              getattr(row, column) is False,
              "%s is still %s" % (column, getattr(row, column)))

    # Now edit the HIDDEN item and check the reverse: it must stay
    # hidden rather than being republished by a save.
    html = client.get(url).data.decode("utf-8")
    fields = browser_would_post(html)
    check("%s: the box is unticked while it is hidden" % name,
          column not in fields, str(sorted(fields.keys())))
    client.post(url, data=fields, follow_redirects=True)
    with app.app_context():
        row = db.session.get(model, IDS[name])
        check("%s: saving a hidden one leaves it hidden" % name,
              getattr(row, column) is False,
              "%s is now %s" % (column, getattr(row, column)))
        # put it back for anything that follows
        setattr(row, column, True)
        db.session.commit()

# ---- campaigns: a select, so the test is different in kind ----------
# The risk here is not a field that vanishes; it is a field whose value
# is not what the row currently holds. A form that offered "Taking
# payments" as the selected option while editing a CLOSED collection
# would reopen it for payment the moment somebody fixed a typo.
url = "/admin/campaigns/%d/edit" % IDS["campaign"]
html = client.get(url).data.decode("utf-8")
fields = browser_would_post(html)
check("campaign: the state field is in the form", "state" in fields,
      "browser would post %s" % sorted(fields.keys()))
check("campaign: the form offers the state the row is actually in",
      fields.get("state") == "closed", str(fields.get("state")))

client.post(url, data=fields, follow_redirects=True)
with app.app_context():
    row = db.session.get(Campaign, IDS["campaign"])
    check("campaign: SAVING WITHOUT TOUCHING ANYTHING KEEPS ITS STATE",
          row.state == "closed", row.state)

for state in ("open", "hidden", "closed"):
    posted = MultiDict([(n, v) for n, v in fields.items(multi=True)
                        if n != "state"])
    posted.add("state", state)
    client.post(url, data=posted, follow_redirects=True)
    with app.app_context():
        row = db.session.get(Campaign, IDS["campaign"])
        check("campaign: the select really does set '%s'" % state,
              row.state == state, row.state)

# A value nobody offered falls back to what the row HAS, never to a
# default — otherwise a hand-made POST could publish a hidden collection.
for junk in ("", "OPEN", "active", "deleted"):
    posted = MultiDict([(n, v) for n, v in fields.items(multi=True)
                        if n != "state"])
    posted.add("state", junk)
    client.post(url, data=posted, follow_redirects=True)
    with app.app_context():
        row = db.session.get(Campaign, IDS["campaign"])
        check("campaign: junk state %r leaves it alone" % junk,
              row.state == "closed", row.state)

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
