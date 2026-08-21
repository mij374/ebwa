"""Smoke test for rich content on News and Events (CLAUDE.md rules).

Mirrors the About coverage: every preset renders and is distinguishable,
the existing single image acts as the lead and migrates into
ContentImage on first save without being lost, listing cards keep using
the lead image only, the rich_layouts flag falls both pages back to
classic, the admin form carries the manager once the record exists, and
deleting a post or event takes its images and files with it.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_rich_news_events.py
"""
import base64
import io
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_rich_ne.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, Block, CONTENT_LAYOUTS, ContentImage,  # noqa: E402
                 DEFAULT_BLOCKS, Event, FEATURES, FeatureFlag, NewsPost,
                 UPLOAD_DIR, User, images_for)

app.config["TESTING"] = True

# Uploads are decoded and optimised now, so a test upload has to be a
# real image. This is a 1x1 transparent PNG: small enough to need no
# thumbnail and, having an alpha channel, stored byte for byte as .png.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


PW = "rich-ne-password"
NEWS_BODY = "Coats were donated all winter.\nCollections continue Tuesdays."
EVENT_BODY = ("Join us for the community iftar.\nDoors open at six.\n"
              "Everyone is welcome.")

failures = []
made = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def get(path):
    return client.get(path).data.decode("utf-8")


def on_disk(filename):
    return os.path.isfile(os.path.join(UPLOAD_DIR, filename))


def set_flag(name, enabled):
    with app.app_context():
        FeatureFlag.query.filter_by(name=name).first().enabled = enabled
        db.session.commit()


def set_layout(model, obj_id, value):
    with app.app_context():
        db.session.get(model, obj_id).layout = value
        db.session.commit()


def upload(owner, owner_id, alt="Volunteers sorting donations",
           name="rich.png", sort=0):
    return client.post(
        "/admin/content-images/%s/%d/add" % (owner, owner_id),
        data={"image": (io.BytesIO(TINY_PNG), name),
              "alt_text": alt, "caption": "", "sort": str(sort)},
        content_type="multipart/form-data", follow_redirects=True)


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
    for n, _l, _d, default in FEATURES:
        if not FeatureFlag.query.filter_by(name=n).first():
            db.session.add(FeatureFlag(name=n, enabled=default))
    u = User(email="admin@example.com")
    u.set_password(PW)
    db.session.add(u)
    post = NewsPost()
    post.title, post.slug = "Winter coat appeal", "winter-coat-appeal"
    post.published_date = date.today()
    post.body, post.image, post.published = NEWS_BODY, "legacy_news.png", True
    db.session.add(post)
    ev = Event()
    ev.title, ev.slug = "Community Iftar", "community-iftar"
    ev.event_date = date.today()
    ev.description, ev.image, ev.published = EVENT_BODY, "legacy_ev.png", True
    db.session.add(ev)
    db.session.commit()
    post_id, ev_id = post.id, ev.id
for name in ("legacy_news.png", "legacy_ev.png"):
    open(os.path.join(UPLOAD_DIR, name), "wb").write(TINY_PNG)
    made.append(name)

client = app.test_client()

TARGETS = [("news", NewsPost, post_id, "/news/winter-coat-appeal", "/news",
            "news_post", "legacy_news.png", NEWS_BODY),
           ("event", Event, ev_id, "/events/community-iftar", "/events",
            "event", "legacy_ev.png", EVENT_BODY)]

# ---- before anything is added: unchanged, with the single image as lead
for label, model, oid, detail, listing, owner, legacy, body in TARGETS:
    html = get(detail)
    check("%s detail -> 200" % label, client.get(detail).status_code == 200)
    for para in body.split("\n"):
        check("%s keeps paragraph: %s" % (label, para[:24]), para in html)
    check("%s still shows its single image" % label, legacy in html)
    check("%s renders through the shared macro" % label, "rc-" in html)
    check("%s defaults to classic" % label, "rc-classic" in html)
    with app.app_context():
        check("%s has no attachments yet" % label,
              images_for(owner, oid) == [])

# ---- every preset renders and is distinguishable
with app.app_context():
    for owner, oid in (("news_post", post_id), ("event", ev_id)):
        for i in range(3):
            img = ContentImage()
            img.owner_type, img.owner_id = owner, oid
            img.filename = "%s_seed%d.png" % (owner, i)
            img.alt_text, img.sort = "Seed %d" % i, i * 10
            db.session.add(img)
    db.session.commit()

for label, model, oid, detail, listing, owner, legacy, body in TARGETS:
    markup = {}
    for layout in CONTENT_LAYOUTS:
        set_layout(model, oid, layout)
        markup[layout] = get(detail)
        check("%s %s renders" % (label, layout),
              client.get(detail).status_code == 200)
        check("%s %s shows every image" % (label, layout),
              all(("%s_seed%d.png" % (owner, i)) in markup[layout]
                  for i in range(3)))
        check("%s %s lazy-loads them" % (label, layout),
              markup[layout].count('loading="lazy"') == 3,
              str(markup[layout].count('loading="lazy"')))
        check("%s %s reserves space" % (label, layout),
              markup[layout].count("--rc-ratio") == 3)
        check("%s %s keeps the body" % (label, layout),
              body.split("\n")[0] in markup[layout])
    check("%s classic marked classic" % label, "rc-classic" in markup["classic"])
    check("%s gallery uses masonry" % label, "rc-masonry" in markup["gallery"])
    check("%s alternating interleaves" % label,
          "rc-alt-row" in markup["alternating"])
    check("%s presets all differ" % label,
          len({markup[k] for k in CONTENT_LAYOUTS}) == 3)

    # ---- listing cards still use the lead image only
    cards = get(listing)
    check("%s listing shows no attachments" % label,
          ("%s_seed0.png" % owner) not in cards, cards[:200])
    check("%s listing still uses the single image" % label, legacy in cards)
    home = get("/")
    if label == "news":
        check("homepage news strip also unchanged",
              "news_post_seed0.png" not in home and legacy in home)
    else:
        check("homepage events strip also unchanged",
              "event_seed0.png" not in home)

with app.app_context():
    ContentImage.query.delete()
    db.session.commit()

client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})

# ---- admin form: manager once saved, a nudge before that
for label, path_new, path_edit in (
        ("news", "/admin/news/new", "/admin/news/%d/edit" % post_id),
        ("event", "/admin/events/new", "/admin/events/%d/edit" % ev_id)):
    r = client.get(path_new)
    check("%s new form nudges you to save first" % label,
          b"Save this first" in r.data)
    check("%s new form has no manager" % label, b"Add photo" not in r.data)
    r = client.get(path_edit)
    check("%s edit form carries the manager" % label, b"Add photo" in r.data)
    check("%s edit form carries the layout picker" % label,
          b"Save layout" in r.data)

# ---- alt text enforced
r = upload("news_post", post_id, alt="  ")
check("blank alt text refused", b"alt text box" in r.data)
with app.app_context():
    check("nothing attached, and nothing migrated either",
          images_for("news_post", post_id) == [])

# ---- the single image migrates in on first save, and is not lost
for label, model, oid, detail, listing, owner, legacy, body in TARGETS:
    r = upload(owner, oid, alt="A new photo for %s" % label,
               name="new_%s.png" % label, sort=10)
    check("%s upload accepted" % label, b"Image added." in r.data)
    with app.app_context():
        rows = images_for(owner, oid)
        check("%s legacy image migrated in as lead" % label,
              len(rows) == 2 and rows[0].filename == legacy, str(rows))
        check("%s migrated lead has alt text" % label, rows[0].alt_text != "")
        obj = db.session.get(model, oid)
        check("%s image column still set" % label, obj.image == legacy)
        made.append(rows[1].filename)
    check("%s file kept on disk" % label, on_disk(legacy))
    check("%s detail shows both" % label,
          legacy in get(detail) and rows[1].filename in get(detail))
    check("%s listing still shows only the lead" % label,
          rows[1].filename not in get(listing))

# ---- layout can be set through the admin
r = client.post("/admin/content-images/news_post/%d/layout" % post_id,
                data={"layout": "gallery"}, follow_redirects=True)
check("layout saved from the admin", b"Layout saved." in r.data)
with app.app_context():
    check("layout stored on the model",
          db.session.get(NewsPost, post_id).layout == "gallery")
check("and the page uses it", "rc-masonry" in get("/news/winter-coat-appeal"))

# ---- flag off: classic with the single image, manager gone and refused
set_flag("rich_layouts", False)
for label, model, oid, detail, listing, owner, legacy, body in TARGETS:
    html = get(detail)
    check("%s falls back to classic with the flag off" % label,
          "rc-classic" in html and "rc-masonry" not in html)
    check("%s shows only its single image" % label,
          legacy in html and html.count('loading="lazy"') == 1,
          str(html.count('loading="lazy"')))
r = client.get("/admin/news/%d/edit" % post_id)
check("manager hidden from the admin form", b"Add photo" not in r.data)
check("and no stray nudge either", b"Save this first" not in r.data)
r = upload("event", ev_id, alt="Should not attach")
check("uploads refused server-side", b"switched off" in r.data)
set_flag("rich_layouts", True)
check("switching back on restores the rich page",
      "rc-masonry" in get("/news/winter-coat-appeal"))

# ---- deleting an owner cascades
for label, model, oid, path in (("news", NewsPost, post_id,
                                 "/admin/news/%d/delete" % post_id),
                                ("event", Event, ev_id,
                                 "/admin/events/%d/delete" % ev_id)):
    owner = "news_post" if label == "news" else "event"
    with app.app_context():
        files = [i.filename for i in images_for(owner, oid)]
    check("%s has images to lose" % label, len(files) == 2, str(files))
    client.post(path)
    with app.app_context():
        check("%s images gone with the owner" % label,
              images_for(owner, oid) == [])
    # the legacy file is shared with the model's own image column, which
    # went with the record, so both should now be gone
    check("%s image files removed from disk" % label,
          not any(on_disk(f) for f in files), str(files))

# ---- teardown
with app.app_context():
    for img in ContentImage.query.all():
        made.append(img.filename)
    db.session.remove()
    db.engine.dispose()
for name in set(made):
    path = os.path.join(UPLOAD_DIR, name)
    if os.path.isfile(path):
        os.remove(path)
check("uploaded test files cleaned up",
      not any(os.path.isfile(os.path.join(UPLOAD_DIR, n)) for n in set(made)))
for suffix in ("", "-wal", "-shm"):
    f = TEST_DB + suffix
    if os.path.isfile(f):
        os.remove(f)
check("test db deleted", not os.path.exists(TEST_DB))

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
