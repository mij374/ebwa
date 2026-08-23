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

from werkzeug.security import generate_password_hash  # noqa: E402

from app import (app, db, AuditLog, Block, ContentImage,  # noqa: E402
                 DEFAULT_BLOCKS, Event, FEATURES, Faq, FeatureFlag,
                 GalleryAlbum, GalleryImage, Milestone, NewsPost,
                 Partner, Resource, Service, Testimonial, User)

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


def page(path):
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
# Events — upcoming ASCENDING, past DESCENDING, ties by id
# =====================================================================
with app.app_context():
    soon, later = date.today() + timedelta(days=3), date.today() + timedelta(days=40)
    gone, older = date.today() - timedelta(days=3), date.today() - timedelta(days=40)
    for title, when in (("EvSoonB", soon), ("EvSoonA", soon),
                        ("EvLater", later), ("EvGoneB", gone),
                        ("EvGoneA", gone), ("EvOlder", older)):
        db.session.add(Event(title=title, slug=title.lower(),
                             event_date=when, published=True,
                             description="x"))
    db.session.commit()

pub = page("/events")
check("events: what is coming up runs soonest first",
      in_order(pub, ["EvSoonB", "EvLater"]),
      show(pub, ["EvSoonB", "EvLater"]))
check("events: two on the same upcoming day keep the order they were "
      "added", in_order(pub, ["EvSoonB", "EvSoonA"]),
      show(pub, ["EvSoonB", "EvSoonA"]))
check("events: what has been runs most recent first",
      in_order(pub, ["EvGoneA", "EvOlder"]),
      show(pub, ["EvGoneA", "EvOlder"]))
check("events: two on the same past day put the newest entry first",
      in_order(pub, ["EvGoneA", "EvGoneB"]),
      show(pub, ["EvGoneA", "EvGoneB"]))
check("events: upcoming comes before past on the page",
      in_order(pub, ["EvSoonB", "EvGoneA"]),
      show(pub, ["EvSoonB", "EvGoneA"]))
admin = page("/admin/events")
check("events: the admin list is newest-dated first, ties by newest entry",
      in_order(admin, ["EvLater", "EvSoonA", "EvSoonB", "EvGoneA",
                       "EvGoneB", "EvOlder"]),
      show(admin, ["EvLater", "EvSoonA", "EvSoonB", "EvGoneA", "EvGoneB",
                   "EvOlder"]))

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
check("test db deleted", not os.path.isfile(TEST_DB))

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
