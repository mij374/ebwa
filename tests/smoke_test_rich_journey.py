"""Smoke test for rich content on Our Journey (CLAUDE.md rules).

Mirrors the News/Events coverage: every preset renders and is
distinguishable, the existing single image acts as the lead and migrates
into ContentImage on first save without being lost, the rich_layouts
flag falls the page back to classic, the admin form carries the manager
once the record exists, and deleting a milestone takes its images and
files with it.

Plus what is particular to this page, which renders EVERY milestone at
once rather than one record on a detail page: entries keep their year
grouping, three presets can sit inside one year, a milestone with no
images renders its words and no placeholder box, funder lines survive,
and the whole page stays a fixed number of queries however many
milestones there are.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_rich_journey.py
"""
import re
import base64
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_rich_journey.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_uploads  # noqa: E402

from sqlalchemy import event as sa_event                        # noqa: E402

from app import (app, db, Block, CONTENT_LAYOUTS, ContentImage,  # noqa: E402
                 DEFAULT_BLOCKS, FEATURES, FeatureFlag, Milestone,
                 UPLOAD_DIR, User, images_for)

app.config["TESTING"] = True

# Uploads are decoded and optimised now, so a test upload has to be a
# real image. This is a 1x1 transparent PNG: small enough to need no
# thumbnail and, having an alpha channel, stored byte for byte as .png.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


PW = "rich-journey-password"
OUTCOME = ("Thirty families came to the first session.\n"
           "By the summer we were running two a week.")

failures = []
made = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def offsite_scripts(html):
    """Every <script src> on the page that is not one of our own files.

    This used to be `"<script src" not in html`, which meant "no
    library" only while every script here was inline. static/js/busy.js
    is linked from both shells now, and it is ours; the claim worth
    keeping is that NOTHING on this page comes off somebody else's
    server. A CDN link still fails, and the failure names it.
    """
    return [src for src in re.findall(r'<script[^>]+src="([^"]+)"', html)
            if not src.startswith("/static/")]




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

def get(path="/our-journey", materialise=True):
    # materialise=False for the query-count section below: writing the
    # fixture files runs its own SELECTs, and counting those as the
    # page's would measure the test harness rather than the page.
    if materialise:
        _materialise()
    return client.get(path).data.decode("utf-8")


def on_disk(filename):
    return os.path.isfile(os.path.join(UPLOAD_DIR, filename))


def set_flag(name, enabled):
    with app.app_context():
        FeatureFlag.query.filter_by(name=name).first().enabled = enabled
        db.session.commit()


def set_layout(obj_id, value):
    with app.app_context():
        db.session.get(Milestone, obj_id).layout = value
        db.session.commit()


def upload(owner_id, alt="Volunteers at the drop-in", name="rich.png",
           sort=0):
    return client.post(
        "/admin/content-images/milestone/%d/add" % owner_id,
        data={"image": (io.BytesIO(TINY_PNG), name),
              "alt_text": alt, "caption": "", "sort": str(sort)},
        content_type="multipart/form-data", follow_redirects=True)


def make_milestone(year, title, summary="", outcome=OUTCOME, image="",
                   funder="", amount=None, published=True):
    m = Milestone()
    m.year, m.title = year, title
    m.summary, m.outcome = summary, outcome
    m.image, m.published = image, published
    m.funder_name, m.amount_pence = funder, amount
    db.session.add(m)
    db.session.commit()
    return m.id


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

    # one year holding three entries, plus a year with a bare entry
    lead_id = make_milestone(2024, "Elderly drop-in moves to twice weekly",
                             summary="Twice a week from September.",
                             image="legacy_journey.png",
                             funder="Enfield Council", amount=750000)
    second_id = make_milestone(2024, "Women's employability course",
                               summary="Twelve women completed it.")
    third_id = make_milestone(2024, "Cricket project reaches 25 young people")
    bare_id = make_milestone(2023, "Bengali school opens on Saturdays",
                             outcome="Thirty children enrolled.")
    draft_id = make_milestone(2022, "Unpublished plan", published=False)

open(os.path.join(UPLOAD_DIR, "legacy_journey.png"), "wb").write(TINY_PNG)
made.append("legacy_journey.png")

client = app.test_client()

# ---- before anything is added: the page is exactly as it was
html = get()
check("/our-journey -> 200", client.get("/our-journey").status_code == 200)
check("year headings still group the page",
      "2024" in html and "2023" in html)
check("entry titles present",
      all(t in html for t in ("Elderly drop-in moves to twice weekly",
                              "Women&#39;s employability course",   # escaped
                              "Bengali school opens on Saturdays")))
check("summary and outcome paragraphs both render",
      "Twice a week from September." in html
      and "Thirty families came to the first session." in html
      and "By the summer we were running two a week." in html)
check("funder line kept", "Enfield Council" in html and "£7,500" in html)
check("single image still used as the lead", "legacy_journey.png" in html)
check("renders through the shared macro", "rc-classic" in html)
check("unpublished milestone stays off the page",
      "Unpublished plan" not in html)
with app.app_context():
    check("no attachments yet", images_for("milestone", lead_id) == [])

# ---- an entry with no images shows its words, and no placeholder box
check("image-less entry keeps its text", "Thirty children enrolled." in html)
check("image-less entry has no placeholder box",
      "img-placeholder" not in html and "Admin → Content" not in html)
check("and takes the full reading width", "is-textonly" in html)

# ---- every preset renders, inside the year structure, and differs
with app.app_context():
    for oid in (lead_id, second_id, third_id):
        for i in range(3):
            img = ContentImage()
            img.owner_type, img.owner_id = "milestone", oid
            img.filename = "m%d_seed%d.png" % (oid, i)
            img.alt_text, img.sort = "Seed %d" % i, i * 10
            db.session.add(img)
    db.session.commit()

markup = {}
for layout in CONTENT_LAYOUTS:
    for oid in (lead_id, second_id, third_id):
        set_layout(oid, layout)
    markup[layout] = get()
    check("journey %s renders" % layout,
          client.get("/our-journey").status_code == 200)
    check("journey %s shows every image" % layout,
          all(("m%d_seed%d.png" % (oid, i)) in markup[layout]
              for oid in (lead_id, second_id, third_id) for i in range(3)))
    check("journey %s lazy-loads them" % layout,
          markup[layout].count('loading="lazy"') == 9,
          str(markup[layout].count('loading="lazy"')))
    check("journey %s reserves space" % layout,
          markup[layout].count("--rc-ratio") == 9)
    check("journey %s keeps the year headings" % layout,
          "journey-year-head" in markup[layout] and "2024" in markup[layout])
    check("journey %s keeps the funder line" % layout,
          "Enfield Council" in markup[layout])
check("classic marked classic", "rc-classic" in markup["classic"])
check("gallery uses the grid", "rc-masonry" in markup["gallery"])
check("alternating interleaves", "rc-alt-row" in markup["alternating"])
check("presets all differ", len({markup[k] for k in CONTENT_LAYOUTS}) == 3)

# ---- three different presets can sit inside one year
set_layout(lead_id, "classic")
set_layout(second_id, "gallery")
set_layout(third_id, "alternating")
mixed = get()
check("mixed year: all three presets on the page",
      all(m in mixed for m in ("rc-classic", "rc-masonry", "rc-alt-row")))
check("mixed year: each entry keeps its own frame",
      mixed.count('class="journey-entry"') == 4,          # 3 in 2024, 1 in 2023
      str(mixed.count('class="journey-entry"')))
check("mixed year: still one 2024 heading", mixed.count(">2024<") == 1)
check("mixed year: entries stay inside their year section",
      mixed.index(">2024<") < mixed.index("Elderly drop-in")
      < mixed.index(">2023<") < mixed.index("Bengali school"))

# ---- the page costs the same however long the charity's history gets
statements = []


def _record(conn, cursor, stmt, params, context, executemany):
    statements.append(stmt)


with app.app_context():
    sa_event.listen(db.engine, "before_cursor_execute", _record)
try:
    _materialise()                      # outside the count, on purpose
    statements[:] = []
    get(materialise=False)
    small = len(statements)
    with app.app_context():
        for i in range(25):
            oid = make_milestone(2010 + i % 5, "Bulk milestone %d" % i,
                                 image="legacy_journey.png")
            db.session.add(ContentImage(
                owner_type="milestone", owner_id=oid,
                filename="bulk%d.png" % i, alt_text="Bulk %d" % i, sort=0))
        db.session.commit()
    _materialise()
    statements[:] = []
    get(materialise=False)
    large = len(statements)
finally:
    with app.app_context():
        sa_event.remove(db.engine, "before_cursor_execute", _record)
check("journey query count does not grow with milestones", small == large,
      "%d -> %d" % (small, large))
check("journey stays a handful of queries", large < 12, str(large))
with app.app_context():
    for m in Milestone.query.filter(Milestone.title.like("Bulk %")).all():
        ContentImage.query.filter_by(owner_type="milestone",
                                     owner_id=m.id).delete()
        db.session.delete(m)
    db.session.commit()

with app.app_context():
    ContentImage.query.delete()
    db.session.commit()

client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})

# ---- admin form: manager once saved, a nudge before that
r = client.get("/admin/journey/new")
check("new form nudges you to save first", b"Save this first" in r.data)
check("new form has no manager", b"Add photo" not in r.data)
r = client.get("/admin/journey/%d/edit" % lead_id)
check("edit form carries the manager", b"Add photo" in r.data)
check("edit form carries the layout picker", b"Save layout" in r.data)

# ---- alt text enforced
r = upload(lead_id, alt="  ")
check("blank alt text refused", b"alt text box" in r.data)
with app.app_context():
    check("nothing attached, and nothing migrated either",
          images_for("milestone", lead_id) == [])

# ---- the single image migrates in on first save, and is not lost
r = upload(lead_id, alt="A new photo", name="new_journey.png", sort=10)
check("upload accepted", b"Image added." in r.data)
with app.app_context():
    rows = images_for("milestone", lead_id)
    check("legacy image migrated in as lead",
          len(rows) == 2 and rows[0].filename == "legacy_journey.png",
          str(rows))
    check("migrated lead has alt text", rows[0].alt_text != "")
    check("image column still set",
          db.session.get(Milestone, lead_id).image == "legacy_journey.png")
    made.append(rows[1].filename)
    added = rows[1].filename
check("file kept on disk", on_disk("legacy_journey.png"))
check("page shows both",
      "legacy_journey.png" in get() and added in get())

# ---- layout can be set through the admin, and lands on this entry only
r = client.post("/admin/content-images/milestone/%d/layout" % lead_id,
                data={"layout": "gallery"}, follow_redirects=True)
check("layout saved from the admin", b"Layout saved." in r.data)
with app.app_context():
    check("layout stored on the model",
          db.session.get(Milestone, lead_id).layout == "gallery")
    check("other milestones untouched",
          db.session.get(Milestone, second_id).layout == "gallery"
          and db.session.get(Milestone, third_id).layout == "alternating")
check("and the page uses it", "rc-masonry" in get())
check("the admin came back to the milestone form",
      b"Save milestone" in client.get("/admin/journey/%d/edit"
                                      % lead_id).data)

# ---- flag off: classic with the single image, manager gone and refused
set_flag("rich_layouts", False)
html = get()
check("falls back to classic with the flag off",
      "rc-classic" in html and "rc-masonry" not in html
      and "rc-alt-row" not in html)
check("shows only the single image",
      "legacy_journey.png" in html and added not in html)
check("image-less entries still fine with the flag off",
      "Thirty children enrolled." in html and "img-placeholder" not in html)
r = client.get("/admin/journey/%d/edit" % lead_id)
check("manager hidden from the admin form", b"Add photo" not in r.data)
check("and no stray nudge either", b"Save this first" not in r.data)
r = upload(lead_id, alt="Should not attach")
check("uploads refused server-side", b"switched off" in r.data)
set_flag("rich_layouts", True)
check("switching back on restores the rich page", "rc-masonry" in get())

# ---- deleting a milestone cascades to its images and files
with app.app_context():
    files = [i.filename for i in images_for("milestone", lead_id)]
check("milestone has images to lose", len(files) == 2, str(files))
client.post("/admin/journey/%d/delete" % lead_id)
with app.app_context():
    check("images gone with the milestone",
          images_for("milestone", lead_id) == [])
check("image files removed from disk",
      not any(on_disk(f) for f in files), str(files))
check("and the entry has left the page",
      "Elderly drop-in moves to twice weekly" not in get())

# ---- what is left still renders
html = get()
check("remaining entries fine", client.get("/our-journey").status_code == 200)
check("year headings survive the deletion",
      "2024" in html and "2023" in html)

# ---- the shared lightbox, reused rather than reimplemented -----------
# The gallery's viewer, included here too. What matters is that it is the
# SAME one: a second implementation is a second place to fix the next
# thing found in it, and the two would drift.
# Its own milestone: by this point in the file the earlier fixtures have
# been edited and deleted by the CRUD sections above, and a page with no
# photographs has nothing to upgrade.
with app.app_context():
    box_m = Milestone(year=2031, title="Lightbox milestone",
                      summary="Words.", published=True)
    db.session.add(box_m)
    db.session.commit()
    db.session.add(ContentImage(owner_type="milestone", owner_id=box_m.id,
                                filename="lightbox_seed.png",
                                alt_text="A photograph", caption="A caption",
                                sort=0))
    db.session.commit()
html = get()
check("journey includes the photo viewer once",
      html.count('id="lightbox"') == 1, str(html.count('id="lightbox"')))
check("...the shared one, keyed on a class rather than the gallery's grid",
      'js-lightbox' in html and 'photoGrid' not in html)
check("...and its script, once",
      html.count("var links") == 1, str(html.count("var links")))
check("every photograph is a real link to the full-size file",
      html.count('<a href="/static/uploads/') >= 1
      and 'aria-label="View ' in html)
check("DEGRADES WITHOUT JS: the link is the photograph itself",
      '<a href="/static/uploads/' in html
      and 'data-caption=' in html)
check("the viewer ships hidden, so there is nothing to tab into "
      "unless it works", 'aria-label="Photo viewer" hidden' in html)
check("keyboard and swipe come with it",
      all(k in html for k in ("ArrowLeft", "ArrowRight", "Escape",
                              "touchstart", "touchend")))
check("no library, still", not offsite_scripts(html),
      str(offsite_scripts(html)))

# The gallery must be unchanged by the extraction — same page, same
# viewer, same fallback.
album = client.get("/gallery/all").data.decode("utf-8")
if 'id="lightbox"' in album:
    check("the gallery still has exactly one viewer",
          album.count('id="lightbox"') == 1)
    check("...and still upgrades plain links",
          'class="photo-masonry js-lightbox"' in album)

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
fake_uploads.remove(_fixture_files)
check("fixture image files cleaned up",
      not any(os.path.isfile(p) for p in _fixture_files),
      "%d left" % sum(os.path.isfile(p) for p in _fixture_files))
check("test db deleted", not os.path.exists(TEST_DB))

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
