"""Smoke test for the rich-content system (CLAUDE.md rules).

Covers: all three presets render and are distinguishable, images attach /
reorder / delete with the files removed from disk, deleting an owner
cascades to its images, alt text is enforced on upload and on edit, the
rich_layouts flag falls the page back to classic with its single image,
the legacy about_image migrates into ContentImage on first save without
being lost, and About renders its existing content unchanged before any
images are added.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_content.py
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_content.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, Block, CONTENT_LAYOUTS, ContentImage,  # noqa: E402
                 DEFAULT_BLOCKS, FEATURES, FeatureFlag, NewsPost, UPLOAD_DIR,
                 User, images_for, interleave_content, layout_for)

app.config["TESTING"] = True

PW = "content-test-password"
BODY = ("EBWA was founded to relieve hardship in Enfield.\n"
        "We run weekend schools and an elderly drop-in.\n"
        "Everyone is welcome at the centre.\n"
        "Come and find us on the High Street.")

failures = []
made = []          # uploaded filenames, cleaned up at the end


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def about():
    return client.get("/about").data.decode("utf-8")


def set_flag(name, enabled):
    with app.app_context():
        FeatureFlag.query.filter_by(name=name).first().enabled = enabled
        db.session.commit()


def set_layout_block(value):
    with app.app_context():
        Block.query.filter_by(key="about_layout").first().value = value
        db.session.commit()


def upload(alt="A volunteer serving lunch", caption="", sort=0,
           owner="about", owner_id=0, name="photo.png"):
    return client.post(
        "/admin/content-images/%s/%d/add" % (owner, owner_id),
        data={"image": (io.BytesIO(b"fake-png-bytes"), name),
              "alt_text": alt, "caption": caption, "sort": str(sort)},
        content_type="multipart/form-data", follow_redirects=True)


def on_disk(filename):
    return os.path.isfile(os.path.join(UPLOAD_DIR, filename))


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
    db.session.commit()
    Block.query.filter_by(key="about_body").first().value = BODY
    Block.query.filter_by(key="about_image").first().value = "legacy.png"
    db.session.commit()
open(os.path.join(UPLOAD_DIR, "legacy.png"), "wb").write(b"legacy-bytes")
made.append("legacy.png")

client = app.test_client()

# ---- before anything is added, About is exactly what it always was
html = about()
check("GET /about -> 200", client.get("/about").status_code == 200)
check("existing title still shown", "About EBWA" in html)
for para in BODY.split("\n"):
    check("existing paragraph kept: %s" % para[:28], para in html)
check("each paragraph is its own <p>", html.count("<p>") >= 4,
      str(html.count("<p>")))
check("the existing single image is still the lead", "legacy.png" in html)
check("reading width is capped", "rc-text" in html)
check("defaults to the classic preset", "rc-classic" in html)
with app.app_context():
    check("with no attachments at all", ContentImage.query.count() == 0)

# ---- the three presets are distinguishable
with app.app_context():
    for i in range(4):
        img = ContentImage()
        img.owner_type, img.owner_id = "about", 0
        img.filename = "seed%d.png" % i
        img.alt_text = "Seeded photo %d" % i
        img.caption = "Caption %d" % i
        img.sort = i * 10
        db.session.add(img)
    db.session.commit()

markup = {}
for layout in CONTENT_LAYOUTS:
    set_layout_block(layout)
    markup[layout] = about()
    check("%s renders" % layout, client.get("/about").status_code == 200)
    check("%s shows every image" % layout,
          all(("seed%d.png" % i) in markup[layout] for i in range(4)))
    check("%s lazy-loads every image" % layout,
          markup[layout].count('loading="lazy"') == 4,
          str(markup[layout].count('loading="lazy"')))
    check("%s reserves space with aspect-ratio" % layout,
          markup[layout].count("--rc-ratio") == 4)
    check("%s keeps the alt text" % layout,
          'alt="Seeded photo 0"' in markup[layout])
    check("%s keeps the body text" % layout,
          "weekend schools" in markup[layout])

check("classic is marked as classic", "rc-classic" in markup["classic"])
check("gallery uses the masonry grid", "rc-masonry" in markup["gallery"])
check("alternating interleaves rows", "rc-alt-row" in markup["alternating"])
check("alternating flips alternate rows",
      "is-flipped" in markup["alternating"])
check("classic puts the extra images in a strip",
      "rc-strip" in markup["classic"])
check("the three presets differ from each other",
      len({markup[k] for k in CONTENT_LAYOUTS}) == 3)
check("gallery has no classic-only parts",
      "rc-strip" not in markup["gallery"]
      and "rc-classic" not in markup["gallery"])
check("classic has no gallery-only parts",
      "rc-masonry" not in markup["classic"])

# interleaving never loses a paragraph or an image
rows = interleave_content(["a", "b", "c", "d", "e"], ["i1", "i2"])
check("interleave keeps every paragraph",
      sorted(p for r in rows for p in r["paragraphs"])
      == ["a", "b", "c", "d", "e"], str(rows))
check("interleave keeps every image",
      [r["image"] for r in rows if r["image"]] == ["i1", "i2"], str(rows))
check("interleave copes with no images",
      interleave_content(["a"], []) == [{"paragraphs": ["a"], "image": None}])
with app.app_context():
    ContentImage.query.delete()
    db.session.commit()

# ---- admin: anonymous access refused
anon = app.test_client()
for path in ("/admin/content-images/about/0/add",
             "/admin/content-images/about/0/layout",
             "/admin/content-images/1/save",
             "/admin/content-images/1/delete"):
    r = anon.post(path)
    check("anon POST %s -> login redirect" % path,
          r.status_code == 302
          and "/admin/login" in r.headers.get("Location", ""),
          str(r.status_code))

client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})
r = client.get("/admin/content?group=about")
check("the About tab shows the manager", b"Add photo" in r.data)
check("and the layout picker", b"Save layout" in r.data)
check("about_layout is not a raw text box",
      b'name="block_' in r.data and b"Page layout" in r.data
      and b'value="classic"' not in r.data.split(b"Page layout")[0])

# ---- alt text is required
r = upload(alt="   ")
check("blank alt text refused", b"alt text box" in r.data)
with app.app_context():
    check("nothing attached without alt text", ContentImage.query.count() == 0)

# ---- the legacy about_image migrates in on first save
r = upload(alt="Volunteers at the drop-in", caption="Tuesday lunch", sort=10)
check("upload accepted", b"Image added." in r.data)
with app.app_context():
    rows = images_for("about")
    check("legacy image migrated in as the lead",
          len(rows) == 2 and rows[0].filename == "legacy.png", str(rows))
    check("the migrated lead has alt text", rows[0].alt_text != "")
    check("the about_image block still points at it",
          Block.query.filter_by(key="about_image").first().value == "legacy.png")
    new_file = rows[1].filename
    made.append(new_file)
    check("the new upload was stored", rows[1].caption == "Tuesday lunch")
check("new file written to disk", on_disk(new_file))
check("both images now render", "legacy.png" in about() and new_file in about())

# ---- reordering by sort number
with app.app_context():
    legacy_row, new_row = images_for("about")
    legacy_id, new_id = legacy_row.id, new_row.id
r = client.post("/admin/content-images/%d/save" % new_id,
                data={"alt_text": "Volunteers at the drop-in",
                      "caption": "Tuesday lunch", "sort": "-5"},
                follow_redirects=True)
check("reorder saved", b"Image updated." in r.data)
with app.app_context():
    order = [i.filename for i in images_for("about")]
    check("sort order changed the running order", order[0] == new_file,
          str(order))
html = about()
check("the page follows the new order",
      html.index(new_file) < html.index("legacy.png"))
r = client.post("/admin/content-images/%d/save" % new_id,
                data={"alt_text": "  ", "sort": "0"}, follow_redirects=True)
check("alt text cannot be emptied by an edit",
      b"cannot be emptied" in r.data)
with app.app_context():
    check("and the old alt text survived",
          db.session.get(ContentImage, new_id).alt_text != "")

# ---- deleting an attachment removes the file...
r = client.post("/admin/content-images/%d/delete" % new_id,
                follow_redirects=True)
check("image removed", b"Image removed." in r.data)
with app.app_context():
    check("row gone", db.session.get(ContentImage, new_id) is None)
check("file deleted from disk", not on_disk(new_file))

# ...but not one that a Block still points at
r = client.post("/admin/content-images/%d/delete" % legacy_id,
                follow_redirects=True)
with app.app_context():
    check("migrated lead detached", images_for("about") == [])
check("shared file kept, because the about_image block still uses it",
      on_disk("legacy.png"))
check("so the page still shows it", "legacy.png" in about())

# ---- deleting an owner cascades to its images
client.post("/admin/news/new", data={"title": "Winter appeal",
                                     "published_date": "2026-08-01",
                                     "published": "on"})
with app.app_context():
    post_id = NewsPost.query.filter_by(title="Winter appeal").first().id
upload(alt="Coats donated by the community", owner="news_post",
       owner_id=post_id, name="news1.png")
upload(alt="Sorting the donations", owner="news_post", owner_id=post_id,
       name="news2.png")
with app.app_context():
    news_images = images_for("news_post", post_id)
    news_files = [i.filename for i in news_images]
    made.extend(news_files)
    check("images attached to the news post", len(news_images) == 2)
check("their files are on disk", all(on_disk(f) for f in news_files))
client.post("/admin/news/%d/delete" % post_id)
with app.app_context():
    check("deleting the owner removed its images",
          images_for("news_post", post_id) == [])
check("and their files", not any(on_disk(f) for f in news_files))

# ---- an unknown owner 404s rather than attaching orphans
r = client.post("/admin/content-images/news_post/99999/add",
                data={"image": (io.BytesIO(b"x"), "x.png"),
                      "alt_text": "Nope"},
                content_type="multipart/form-data")
check("unknown owner -> 404", r.status_code == 404, str(r.status_code))
r = client.post("/admin/content-images/wardrobe/1/add",
                data={"image": (io.BytesIO(b"x"), "x.png"),
                      "alt_text": "Nope"},
                content_type="multipart/form-data")
check("unknown owner type -> 404", r.status_code == 404, str(r.status_code))

# ---- an unknown layout is refused
r = client.post("/admin/content-images/about/0/layout",
                data={"layout": "carousel"}, follow_redirects=True)
check("unknown layout refused", b"Unknown layout" in r.data)
with app.app_context():
    check("layout unchanged", layout_for("about") in CONTENT_LAYOUTS)

# ---- flag off: classic, single image, manager hidden and refused
set_layout_block("gallery")
upload(alt="A second photo", sort=20, name="second.png")
with app.app_context():
    stored = [i.filename for i in images_for("about")]
    made.extend(stored)
    second_file = stored[-1]          # save_upload renames to a UUID
check("the second photo is on disk", on_disk(second_file))
set_flag("rich_layouts", False)
html = about()
check("flag off falls back to classic", "rc-classic" in html
      and "rc-masonry" not in html, html[:200])
check("flag off shows the single legacy image", "legacy.png" in html)
check("flag off ignores the extra attachments",
      second_file not in html and html.count('loading="lazy"') <= 1)
r = client.get("/admin/content?group=about")
check("manager hidden from admin with the flag off",
      b"Add photo" not in r.data and b"Save layout" not in r.data)
r = client.post("/admin/content-images/about/0/layout",
                data={"layout": "gallery"}, follow_redirects=True)
check("and refused server-side too", b"switched off" in r.data)
r = upload(alt="Should not attach", name="nope.png")
check("uploads refused with the flag off", b"switched off" in r.data)

set_flag("rich_layouts", True)
check("switching the flag back on restores the rich page",
      "rc-masonry" in about())
check("nothing was lost while it was off", second_file in about())

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
