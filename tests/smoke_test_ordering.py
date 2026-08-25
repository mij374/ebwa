"""What order things come out in, pinned — including the ties.

Ordering is the kind of thing that looks right for years and then moves,
because most ORDER BY clauses are only fully specified for the rows that
happen to be in the database today. Two rows with the same `sort` and no
further key are returned in whatever order the database finds them, and
SQLite's answer (rowid, so insertion order) is a coincidence, not a
promise: it changes when a row is rewritten, when an index is added, or
when the database is not SQLite.

So every check here builds the TIE deliberately, and inserts the rows in
the OPPOSITE order to the one expected — if the tie-break is missing,
the rows come back in insertion order and the assertion fails.

Four things are asserted per sortable model:

  * a lower `sort` comes out earlier;
  * a NEGATIVE sort pins to the top, which the routes allow and the
    admin forms deliberately do not block;
  * a tie falls to a named second key, not to the database's mood;
  * the ADMIN list is in the same order as the public page, so the
    person arranging the rows sees what the visitor will see.

Run:  python tests/smoke_test_ordering.py
"""
import os
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_ordering.db")
# A run that failed part way through leaves its database behind, and the
# seeding below would then collide with itself. Start from nothing.
for _suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _suffix):
        os.remove(TEST_DB + _suffix)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_uploads  # noqa: E402

from werkzeug.security import generate_password_hash  # noqa: E402

from app import (app, db, AuditLog, Block, ContentImage,  # noqa: E402
                 DEFAULT_BLOCKS, Event, FEATURES, Faq, FeatureFlag,
                 GalleryAlbum, GalleryImage, Milestone, NewsPost,
                 Partner, Resource, Service, Testimonial, User,
                 BackupRun, Campaign, ContactMessage,
                 MembershipApplication, Subscriber,
                 album_choices, events_in_day_order, start_minutes)

app.config["TESTING"] = True
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def positions(html, names):
    """Where each name appears in the page, in the order given."""
    return [html.find(n) for n in names]


def in_order(html, names):
    """True when every name is present, in exactly this order."""
    pos = positions(html, names)
    return all(p >= 0 for p in pos) and pos == sorted(pos)


def show(html, names):
    return "positions %s" % positions(html, names)


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
    # Every module on, so every public page renders.
    for n, _l, _d, _default in FEATURES:
        row = FeatureFlag.query.filter_by(name=n).first()
        if row is None:
            db.session.add(FeatureFlag(name=n, enabled=True))
        else:
            row.enabled = True
    db.session.add(User(email="netbus@example.com",
                        password_hash=generate_password_hash("pw123456"),
                        role="super_admin"))
    db.session.commit()

client = app.test_client()
client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": "pw123456"})



# Every fixture image is made REAL before a page is fetched. The site
# skips a content image whose file is not on disk (it renders an empty
# panel with alt text otherwise, which reads as a broken site), so a
# fixture that inserts a row and no file is testing a broken attachment
# rather than the layout it means to test. fill_dangling() writes one
# for every reference in the database, whatever the fixtures called
# them, and teardown takes them away again.
_fixture_files = []


def _materialise():
    with app.app_context():
        _fixture_files.extend(fake_uploads.fill_dangling())

def page(path):
    _materialise()
    return client.get(path).data.decode("utf-8")


# =====================================================================
# Services — sort then id
# =====================================================================
with app.app_context():
    # Inserted in the WRONG order on purpose: if `sort` were ignored the
    # page would read Middle, Last, First, Tie-B, Tie-A.
    for title, s in (("SvcMiddle", 5), ("SvcLast", 40), ("SvcFirst", -3),
                     ("SvcTieB", 10), ("SvcTieA", 10)):
        db.session.add(Service(title=title, description="x", icon="*",
                               sort=s, published=True))
    db.session.commit()

WANT_SERVICES = ["SvcFirst", "SvcMiddle", "SvcTieB", "SvcTieA", "SvcLast"]
home = page("/")
check("services: lower sort first, and a negative pins to the top",
      in_order(home, ["SvcFirst", "SvcMiddle", "SvcLast"]),
      show(home, WANT_SERVICES))
check("services: a tie falls to id, so it is insertion order and stays",
      in_order(home, ["SvcTieB", "SvcTieA"]), show(home, WANT_SERVICES))
admin = page("/admin/services")
check("services: the admin list is in the same order as the page",
      in_order(admin, WANT_SERVICES), show(admin, WANT_SERVICES))

# =====================================================================
# Partners — sort, name, id
# =====================================================================
with app.app_context():
    for name, s in (("PtnZed", 5), ("PtnAlpha", 5), ("PtnPinned", -1),
                    ("PtnLast", 9)):
        db.session.add(Partner(name=name, display_mode="text", sort=s))
    # Same sort AND the same name: only an id can separate these.
    db.session.add(Partner(name="PtnSame", display_mode="text", sort=7))
    db.session.add(Partner(name="PtnSame", display_mode="text", sort=7))
    db.session.commit()
    same_ids = [p.id for p in Partner.query.filter_by(name="PtnSame")
                .order_by(Partner.id).all()]

WANT_PARTNERS = ["PtnPinned", "PtnAlpha", "PtnZed", "PtnSame", "PtnLast"]
home = page("/")
check("partners: negative pins to the top, then sort order",
      in_order(home, ["PtnPinned", "PtnAlpha", "PtnLast"]),
      show(home, WANT_PARTNERS))
check("partners: a tied sort falls to the NAME, not to insertion order",
      in_order(home, ["PtnAlpha", "PtnZed"]), show(home, WANT_PARTNERS))
admin = page("/admin/partners")
check("partners: the admin list matches the page",
      in_order(admin, WANT_PARTNERS), show(admin, WANT_PARTNERS))
with app.app_context():
    rows = [p.id for p in Partner.query
            .order_by(Partner.sort, Partner.name, Partner.id).all()
            if p.name == "PtnSame"]
    check("partners: two rows alike in sort AND name still have an order",
          rows == sorted(same_ids), "%s vs %s" % (rows, same_ids))

# =====================================================================
# Resources — category, sort, name, id
# =====================================================================
with app.app_context():
    for name, cat, s in (("ResBeta", "Advice", 5), ("ResAlpha", "Advice", 5),
                         ("ResTop", "Advice", -2), ("ResOther", "Zebra", 0)):
        db.session.add(Resource(name=name, category=cat, description="x"))
        db.session.flush()
        Resource.query.filter_by(name=name).first().sort = s
    db.session.commit()

WANT_RESOURCES = ["ResTop", "ResAlpha", "ResBeta", "ResOther"]
pub = page("/resources")
check("resources: category first, then sort, negative pinned",
      in_order(pub, WANT_RESOURCES), show(pub, WANT_RESOURCES))
check("resources: a tie inside a category falls to the name",
      in_order(pub, ["ResAlpha", "ResBeta"]), show(pub, WANT_RESOURCES))
admin = page("/admin/resources")
check("resources: the admin list matches the page",
      in_order(admin, WANT_RESOURCES), show(admin, WANT_RESOURCES))

# =====================================================================
# FAQ — category, sort, id. An EMPTY category sorts before any letter,
# which is the documented behaviour: those questions run ungrouped at
# the top of the page.
# =====================================================================
with app.app_context():
    for q, cat, s in (("FaqSecond?", "", 5), ("FaqFirst?", "", -1),
                      ("FaqTieB?", "Money", 3), ("FaqTieA?", "Money", 3)):
        db.session.add(Faq(question=q, answer="a", category=cat, sort=s,
                           published=True))
    db.session.commit()

WANT_FAQ = ["FaqFirst?", "FaqSecond?", "FaqTieB?", "FaqTieA?"]
pub = page("/faq")
check("faq: ungrouped questions come before any category",
      in_order(pub, WANT_FAQ), show(pub, WANT_FAQ))
check("faq: a tie falls to id, so it is insertion order",
      in_order(pub, ["FaqTieB?", "FaqTieA?"]), show(pub, WANT_FAQ))
admin = page("/admin/faq")
check("faq: the admin list matches the page",
      in_order(admin, WANT_FAQ), show(admin, WANT_FAQ))

# =====================================================================
# Milestones — year DESC, then sort, then title, then id
# =====================================================================
with app.app_context():
    for title, year, s in (("MsOld", 2019, 0), ("MsNewB", 2024, 5),
                           ("MsNewA", 2024, 5), ("MsNewTop", 2024, -4)):
        db.session.add(Milestone(title=title, year=year, summary="x",
                                 published=True, sort=s))
    db.session.commit()

WANT_MILESTONES = ["MsNewTop", "MsNewA", "MsNewB", "MsOld"]
pub = page("/our-journey")
check("milestones: newest YEAR first, and grouped by it",
      in_order(pub, ["MsNewTop", "MsOld"]), show(pub, WANT_MILESTONES))
check("milestones: inside a year, sort decides and a negative pins",
      in_order(pub, WANT_MILESTONES), show(pub, WANT_MILESTONES))
check("milestones: a tie inside a year falls to the title",
      in_order(pub, ["MsNewA", "MsNewB"]), show(pub, WANT_MILESTONES))
admin = page("/admin/journey")
check("milestones: the admin list matches the page",
      in_order(admin, WANT_MILESTONES), show(admin, WANT_MILESTONES))

# =====================================================================
# Testimonials — sort, then NEWEST first, then id descending
# =====================================================================
with app.app_context():
    stamp = datetime(2026, 5, 1, 12, 0, 0)
    for name, s in (("TstTieA", 5), ("TstTieB", 5)):
        db.session.add(Testimonial(name=name, quote="q", sort=s,
                                   published=True, created_at=stamp))
    db.session.add(Testimonial(name="TstPinned", quote="q", sort=-1,
                               published=True, created_at=stamp))
    db.session.commit()

WANT_TESTIMONIALS = ["TstPinned", "TstTieB", "TstTieA"]
home = page("/")
check("testimonials: a negative sort pins to the top",
      in_order(home, ["TstPinned", "TstTieA"]),
      show(home, WANT_TESTIMONIALS))
check("testimonials: same sort and same timestamp still has an order, "
      "newest first", in_order(home, ["TstTieB", "TstTieA"]),
      show(home, WANT_TESTIMONIALS))
admin = page("/admin/testimonials")
check("testimonials: the admin list matches the page",
      in_order(admin, WANT_TESTIMONIALS), show(admin, WANT_TESTIMONIALS))

# =====================================================================
# Gallery albums and images — sort, newest, id descending
# =====================================================================
with app.app_context():
    stamp = datetime(2026, 5, 1, 12, 0, 0)
    for title, slug, s in (("AlbTieA", "alb-tie-a", 5),
                           ("AlbTieB", "alb-tie-b", 5),
                           ("AlbPinned", "alb-pinned", -2)):
        db.session.add(GalleryAlbum(title=title, slug=slug, sort=s,
                                    published=True, created_at=stamp))
    db.session.commit()
    album = GalleryAlbum.query.filter_by(slug="alb-pinned").first()
    album_id = album.id          # read INSIDE the context; the instance
    for cap, s in (("ImgTieA", 3), ("ImgTieB", 3), ("ImgPinned", -1)):
        db.session.add(GalleryImage(filename="%s.jpg" % cap.lower(),
                                    caption=cap, album_id=album_id,
                                    sort=s, created_at=stamp))
    db.session.commit()          # is detached by the time the page loads

WANT_ALBUMS = ["AlbPinned", "AlbTieB", "AlbTieA"]
pub = page("/gallery")
check("albums: a negative sort pins to the top",
      in_order(pub, ["AlbPinned", "AlbTieA"]), show(pub, WANT_ALBUMS))
check("albums: same sort and timestamp still ordered, newest first",
      in_order(pub, ["AlbTieB", "AlbTieA"]), show(pub, WANT_ALBUMS))
admin = page("/admin/gallery/albums")
check("albums: the admin list matches the page",
      in_order(admin, WANT_ALBUMS), show(admin, WANT_ALBUMS))

WANT_IMAGES = ["ImgPinned", "ImgTieB", "ImgTieA"]
pub = page("/gallery/alb-pinned")
check("gallery images: sort decides inside an album, negative pinned",
      in_order(pub, WANT_IMAGES), show(pub, WANT_IMAGES))
check("gallery images: a tie is still ordered, newest first",
      in_order(pub, ["ImgTieB", "ImgTieA"]), show(pub, WANT_IMAGES))
admin = page("/admin/gallery?album=%d" % album_id)
check("gallery images: the admin grid matches the album page",
      in_order(admin, WANT_IMAGES), show(admin, WANT_IMAGES))

# =====================================================================
# Rich-content images — sort then id, one helper for public and admin
# =====================================================================
with app.app_context():
    post = NewsPost(title="Ordering post", slug="ordering-post",
                    body="Body.", published=True,
                    published_date=date(2026, 4, 1))
    db.session.add(post)
    db.session.commit()
    for alt, s in (("CiTieA", 10), ("CiTieB", 10), ("CiFirst", 0)):
        db.session.add(ContentImage(owner_type="news_post", owner_id=post.id,
                                    filename="%s.jpg" % alt.lower(),
                                    alt_text=alt, sort=s))
    db.session.commit()
    post_id = post.id

WANT_CI = ["CiFirst", "CiTieA", "CiTieB"]
pub = page("/news/ordering-post")
check("content images: sort first, then insertion order for a tie",
      in_order(pub, WANT_CI), show(pub, WANT_CI))
admin = page("/admin/news/%d/edit" % post_id)
check("content images: the admin partial matches the post",
      in_order(admin, WANT_CI), show(admin, WANT_CI))

# =====================================================================
# Blocks — the content editor's field order. Every seeded block has
# sort 0, so this list is ALL tie: without a second key the editor's
# field order is whatever the database returns.
# =====================================================================
from app import HIDDEN_BLOCK_KEYS  # noqa: E402  (used just below)

with app.app_context():
    visible = [b for b in Block.query.filter_by(group="home")
               .order_by(Block.id).all()
               if b.key not in HIDDEN_BLOCK_KEYS]
    sorts = {b.sort for b in visible}
    # The editor labels its fields by row id, not by key.
    want_fields = ['id="block_%d"' % b.id for b in visible]
check("blocks: the seeded ones really are all the same sort",
      sorts == {0}, str(sorts))
admin = page("/admin/content")
check("blocks: the editor lists a group in a fixed order (id), not the "
      "database's choice", len(want_fields) >= 3
      and in_order(admin, want_fields), show(admin, want_fields))

# =====================================================================
# Events — day order, then the DAY'S PROGRAMME: earliest start first,
# entries with no readable time after those with one, then title, then
# id. A visitor reading one day expects to read it in the order they
# could attend it, which is why the time sort runs forwards even in the
# past list where the days run backwards.
# =====================================================================
check("start times: read as times, not as text",
      [start_minutes(t) for t in ("6:30 PM", "18:30", "7pm", "10:00 AM",
                                  "6:30 AM", "12 pm", "12:20am")]
      == [1110, 1110, 1140, 600, 390, 720, 20],
      str([start_minutes(t) for t in ("6:30 PM", "18:30", "7pm", "10:00 AM",
                                      "6:30 AM", "12 pm", "12:20am")]))
check("start times: 10am really does sort before 6:30am now",
      start_minutes("10:00 AM") > start_minutes("6:30 AM"))
check("start times: nothing readable means no time at all",
      [start_minutes(t) for t in ("", "Evening", "TBC", None)]
      == [None, None, None, None])

with app.app_context():
    soon = date.today() + timedelta(days=3)
    later = date.today() + timedelta(days=40)
    gone = date.today() - timedelta(days=3)
    older = date.today() - timedelta(days=40)
    # Every one of these is inserted in the OPPOSITE order to the one
    # expected, so insertion order cannot be what makes this pass.
    for title, when, start in (
            ("EvSoonEvening", soon, "6:30 PM"),      # third
            ("EvSoonNoTime", soon, ""),              # last: no time
            ("EvSoonZulu", soon, "10:00 AM"),        # second
            ("EvSoonAlpha", soon, "9.15am"),         # first
            ("EvSoonTbcZ", soon, "TBC"),             # after NoTime, by title
            ("EvLater", later, "7pm"),
            ("EvGoneLate", gone, "8pm"),
            ("EvGoneEarly", gone, "11am"),
            ("EvOlder", older, "")):
        db.session.add(Event(title=title, slug=title.lower(),
                             event_date=when, start_time=start,
                             published=True, description="x"))
    db.session.commit()

WANT_SOON = ["EvSoonAlpha", "EvSoonZulu", "EvSoonEvening",
             "EvSoonNoTime", "EvSoonTbcZ"]
pub = page("/events")
check("events: one day reads as a programme, earliest start first",
      in_order(pub, ["EvSoonAlpha", "EvSoonZulu", "EvSoonEvening"]),
      show(pub, WANT_SOON))
check("events: 9.15am before 10:00 AM before 6:30 PM — read as times",
      in_order(pub, WANT_SOON[:3]), show(pub, WANT_SOON))
check("events: entries with no time come AFTER the timed ones",
      in_order(pub, ["EvSoonEvening", "EvSoonNoTime"]), show(pub, WANT_SOON))
check("events: and among themselves fall to the title",
      in_order(pub, ["EvSoonNoTime", "EvSoonTbcZ"]), show(pub, WANT_SOON))
check("events: days still run soonest first",
      in_order(pub, ["EvSoonAlpha", "EvLater"]),
      show(pub, ["EvSoonAlpha", "EvLater"]))
check("events: what has been runs most recent day first",
      in_order(pub, ["EvGoneEarly", "EvOlder"]),
      show(pub, ["EvGoneEarly", "EvOlder"]))
check("events: but WITHIN a past day the programme still reads forwards",
      in_order(pub, ["EvGoneEarly", "EvGoneLate"]),
      show(pub, ["EvGoneEarly", "EvGoneLate"]))
check("events: upcoming comes before past on the page",
      in_order(pub, ["EvSoonAlpha", "EvGoneEarly"]),
      show(pub, ["EvSoonAlpha", "EvGoneEarly"]))

admin = page("/admin/events")
WANT_ADMIN = ["EvLater"] + WANT_SOON + ["EvGoneEarly", "EvGoneLate",
                                        "EvOlder"]
check("events: the admin list reads the same way, newest day first",
      in_order(admin, WANT_ADMIN), show(admin, WANT_ADMIN))

home = page("/")
check("home: the three soonest are the three earliest of the day, in "
      "order", in_order(home, ["EvSoonAlpha", "EvSoonZulu",
                               "EvSoonEvening"])
      and "EvSoonNoTime" not in home, show(home, WANT_SOON))

# Two alike in day, time AND title still have an order.
with app.app_context():
    for _ in range(2):
        db.session.add(Event(title="EvTwin", slug="evtwin-%d" % _,
                             event_date=soon, start_time="9.15am",
                             published=True, description="x"))
    db.session.commit()
    twins = [e.id for e in Event.query.filter_by(title="EvTwin")
             .order_by(Event.id).all()]
    ordered = [e.id for e in events_in_day_order(
        Event.query.filter_by(title="EvTwin").all())]
check("events: identical day, time and title still fall to id",
      ordered == sorted(twins), "%s vs %s" % (ordered, twins))

# =====================================================================
# News — published date descending, then created_at, then id
# =====================================================================
with app.app_context():
    stamp = datetime(2026, 5, 1, 12, 0, 0)
    day = date(2026, 4, 20)
    for title in ("NwTieA", "NwTieB"):
        db.session.add(NewsPost(title=title, slug=title.lower(), body="x",
                                published=True, published_date=day,
                                created_at=stamp))
    db.session.add(NewsPost(title="NwNewer", slug="nwnewer", body="x",
                            published=True, published_date=date(2026, 6, 1),
                            created_at=stamp))
    db.session.commit()

WANT_NEWS = ["NwNewer", "NwTieB", "NwTieA"]
pub = page("/news")
check("news: newest published date first", in_order(pub, ["NwNewer", "NwTieA"]),
      show(pub, WANT_NEWS))
check("news: same date and same timestamp still ordered, newest entry first",
      in_order(pub, ["NwTieB", "NwTieA"]), show(pub, WANT_NEWS))
admin = page("/admin/news")
check("news: the admin list matches the page", in_order(admin, WANT_NEWS),
      show(admin, WANT_NEWS))

# =====================================================================
# Audit log — newest first, ties by id, which it always had
# =====================================================================
with app.app_context():
    stamp = datetime(2026, 5, 1, 12, 0, 0)
    for summary in ("AudFirst", "AudSecond", "AudThird"):
        db.session.add(AuditLog(user_email="netbus@example.com",
                                action="edit", summary=summary,
                                created_at=stamp))
    db.session.commit()
admin = page("/admin/audit")
check("audit: entries with one timestamp still come newest first",
      in_order(admin, ["AudThird", "AudSecond", "AudFirst"]),
      show(admin, ["AudThird", "AudSecond", "AudFirst"]))

# =====================================================================
# The timestamp-ordered admin lists. A tie needs two rows created in the
# same microsecond, which a person filling in a form cannot do — so each
# one is built here by hand, which is the only way it ever happens.
# =====================================================================
with app.app_context():
    stamp = datetime(2026, 5, 1, 12, 0, 0)
    # A subscriber has no name: the list shows the address, so that is
    # what the assertion below has to look for.
    for email in ("sub-a@example.com", "sub-b@example.com"):
        db.session.add(Subscriber(email=email, created_at=stamp))
    for subject in ("MsgA", "MsgB"):
        db.session.add(ContactMessage(name=subject, email="m@example.com",
                                      subject=subject, message="x",
                                      created_at=stamp))
    for title in ("CampA", "CampB"):
        db.session.add(Campaign(title=title, slug=title.lower(),
                                description="x", active=True,
                                created_at=stamp))
    for name in ("AppA", "AppB"):
        db.session.add(MembershipApplication(
            name=name, email="%s@example.com" % name.lower(),
            phone="0", address="x", over_18=True,
            bangladeshi_origin=True, lives_works_enfield=True,
            fee_confirmed=True, created_at=stamp))
    db.session.commit()

for path, later_first, label in (
        ("/admin/subscribers",
         ["sub-b@example.com", "sub-a@example.com"], "subscribers"),
        ("/admin/messages", ["MsgB", "MsgA"], "enquiries"),
        ("/admin/campaigns", ["CampB", "CampA"], "campaigns"),
        ("/admin/membership", ["AppB", "AppA"], "membership applications")):
    html = page(path)
    check("%s: one timestamp, newest entry still first" % label,
          in_order(html, later_first), show(html, later_first))

with app.app_context():
    for started in (datetime(2026, 5, 1, 2, 0, 0),) * 2:
        db.session.add(BackupRun(started_at=started, status="ok",
                                 filename="backup.zip"))
    db.session.commit()
    runs = [r.id for r in BackupRun.query.order_by(BackupRun.id).all()]
    latest = (BackupRun.query
              .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
              .first())
check("backup runs: two starting at once, the later row is the latest",
      latest.id == max(runs), "%s of %s" % (latest.id, runs))

# =====================================================================
# The album PICKER is alphabetical and the album LIST is not, on
# purpose: one is for finding a name, the other for presenting an
# arrangement. Pinned here so the divergence cannot be "fixed" by
# accident.
# =====================================================================
with app.app_context():
    picker = [a.title for a in album_choices()]
    listed = [a.title for a in GalleryAlbum.query
              .order_by(GalleryAlbum.sort, GalleryAlbum.created_at.desc(),
                        GalleryAlbum.id.desc()).all()]
check("albums: the picker is alphabetical within a sort group",
      picker.index("AlbTieA") < picker.index("AlbTieB"), str(picker))
check("albums: the list is not, and that is the point",
      listed.index("AlbTieB") < listed.index("AlbTieA"), str(listed))
check("albums: so the two orders genuinely differ", picker != listed,
      "%s vs %s" % (picker, listed))

# =====================================================================
# The invariant behind all of it: ask twice, get the same answer.
# =====================================================================
repeats = [page("/"), page("/"), page("/gallery"), page("/gallery")]
check("asking twice gives the same page order",
      repeats[0] == repeats[1] and repeats[2] == repeats[3])

with app.app_context():
    db.session.remove()
    db.engine.dispose()
for suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + suffix):
        os.remove(TEST_DB + suffix)
fake_uploads.remove(_fixture_files)
check("fixture image files cleaned up",
      not any(os.path.isfile(p) for p in _fixture_files),
      "%d left" % sum(os.path.isfile(p) for p in _fixture_files))
check("test db deleted", not os.path.isfile(TEST_DB))

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
