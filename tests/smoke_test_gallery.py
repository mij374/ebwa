"""Smoke test for the gallery: albums, filing, lightbox (CLAUDE.md rules).

Covers: album CRUD through the admin; photos uploaded into an album and
moved between albums in bulk; unfiled photos staying reachable under
"All photos"; unpublished albums hidden from the index, the all-photos
view and the sitemap, and 404 by direct URL; DELETING AN ALBUM KEEPING
ITS PHOTOS (they become unfiled — an album is an arrangement, and losing
irreplaceable photographs to one is not a trade worth making); ordering
newest first with `sort` overriding; thumbnails on the grid and full size
in the lightbox links; and the lightbox markup and its no-JS fallback.

Runs against a throwaway SQLite db AND a throwaway uploads folder, so
neither instance/ebwa.db nor static/uploads is touched.

Run:  python tests/smoke_test_gallery.py
"""
import base64
import io
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_gallery.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image                                          # noqa: E402

import app as appmod                                           # noqa: E402
from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,     # noqa: E402
                 FeatureFlag, GalleryAlbum, GalleryImage, User, thumb_name)

app.config["TESTING"] = True

REAL_UPLOAD_DIR = appmod.UPLOAD_DIR
SANDBOX = tempfile.mkdtemp(prefix="ebwa-gallery-")
appmod.UPLOAD_DIR = SANDBOX

PW = "gallery-test-password"
failures = []


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



def get(path):
    return client.get(path).data.decode("utf-8")


def photo_bytes(width, height):
    """A real photograph-shaped image, so aspect ratios are meaningful."""
    im = Image.new("RGB", (width, height))
    px = im.load()
    for y in range(0, height, 3):
        for x in range(0, width, 3):
            px[x, y] = ((x * 5) % 256, (y * 11) % 256, ((x + y) * 7) % 256)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


LANDSCAPE = photo_bytes(1200, 800)
PORTRAIT = photo_bytes(800, 1200)


def upload(raw, filename="photo.jpg", album_id="", caption=""):
    return client.post(
        "/admin/gallery",
        data={"images": (io.BytesIO(raw), filename), "caption": caption,
              "album_id": str(album_id)},
        content_type="multipart/form-data", follow_redirects=True)


def make_album(title, description="", published="on", sort="0"):
    return client.post("/admin/gallery/albums/new",
                       data={"title": title, "description": description,
                             "sort": sort,
                             **({"published": published} if published else {})},
                       follow_redirects=True)


def album_by_title(title):
    with app.app_context():
        return GalleryAlbum.query.filter_by(title=title).first()


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        db.session.add(Block(group=group, key=key, label=label, kind=kind,
                             value=value))
    for n, _l, _d, default in FEATURES:
        db.session.add(FeatureFlag(name=n, enabled=default))
    u = User(email="admin@example.com")
    u.set_password(PW)
    db.session.add(u)
    # two photos from before albums existed: they must stay reachable
    for i, raw in enumerate((LANDSCAPE, PORTRAIT)):
        name = "legacy%d.jpg" % i
        with open(os.path.join(SANDBOX, name), "wb") as fh:
            fh.write(raw)
        db.session.add(GalleryImage(filename=name,
                                    caption="Legacy photo %d" % i))
    db.session.commit()

client = app.test_client()

# ---- anonymous admin access still refused
for path, method in (("/admin/gallery/albums", "GET"),
                     ("/admin/gallery/albums/new", "GET"),
                     ("/admin/gallery/move", "POST")):
    r = client.open(path, method=method)
    check("anon %s %s -> login redirect" % (method, path),
          r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
          str(r.status_code))

client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})

# ---- before any album: the index offers everything, nothing is lost
html = get("/gallery")
check("gallery index -> 200", client.get("/gallery").status_code == 200)
check("all-photos card shown", "/gallery/all" in html)
check("unfiled photos counted", "2 photos" in html, html[html.find("All photos"):][:200])
all_html = get("/gallery/all")
check("unfiled photos reachable under all photos",
      "legacy0.jpg" in all_html or thumb_name("legacy0.jpg") in all_html)

# ---- album CRUD
r = make_album("Seaside trip 2026", "A day at the coast.")
check("album created", b"Album saved." in r.data)
seaside = album_by_title("Seaside trip 2026")
check("album has a slug", seaside and seaside.slug == "seaside-trip-2026",
      seaside.slug if seaside else "none")
check("album published by default", seaside.published is True)
r = client.get("/admin/gallery/albums")
check("album listed in the admin", b"Seaside trip 2026" in r.data)

r = client.post("/admin/gallery/albums/%d/edit" % seaside.id,
                data={"title": "Seaside trip", "description": "A day out.",
                      "sort": "5", "published": "on"},
                follow_redirects=True)
check("album edited", b"Album saved." in r.data)
seaside = album_by_title("Seaside trip")
check("edit stored", seaside and seaside.description == "A day out."
      and seaside.sort == 5)
check("slug follows the title", seaside.slug == "seaside-trip", seaside.slug)

r = make_album("Eid celebration")
eid = album_by_title("Eid celebration")
check("second album created", eid is not None)

# ---- an album may not squat on /gallery/all
r = make_album("All")
squatter = album_by_title("All")
check("an album cannot take the all-photos address",
      squatter.slug != "all", squatter.slug)
check("and still has a usable page",
      client.get("/gallery/%s" % squatter.slug).status_code == 200)
client.post("/admin/gallery/albums/%d/delete" % squatter.id,
            follow_redirects=True)

# ---- uploading into an album
r = upload(LANDSCAPE, "beach.jpg", album_id=seaside.id, caption="On the sand")
check("upload into an album accepted", b"image(s) uploaded" in r.data)
with app.app_context():
    filed = GalleryImage.query.filter_by(album_id=seaside.id).all()
    check("photo filed into the album", len(filed) == 1, str(filed))
    check("caption kept", filed[0].caption == "On the sand")
for raw, name in ((PORTRAIT, "pier.jpg"), (LANDSCAPE, "icecream.jpg")):
    upload(raw, name, album_id=seaside.id)
upload(PORTRAIT, "eid1.jpg", album_id=eid.id)

album_html = get("/gallery/seaside-trip")
check("album page -> 200",
      client.get("/gallery/seaside-trip").status_code == 200)
check("album page shows its photos", album_html.count("photo-item") == 3,
      str(album_html.count("photo-item")))
check("album page does not show another album's photos",
      "eid1" not in album_html)
check("photos carry their own aspect ratio",
      "aspect-ratio: 1200 / 800" in album_html
      and "aspect-ratio: 800 / 1200" in album_html)
check("grid uses thumbnails", "-thumb.jpg" in album_html)
check("links open the full size",
      re.search(r'<a href="[^"]+/uploads/[0-9a-f]{32}\.jpg"', album_html)
      is not None)
check("photos lazy-load", album_html.count('loading="lazy"') == 3)

index = get("/gallery")
check("album card on the index", "Seaside trip" in index
      and "/gallery/seaside-trip" in index)
check("album card shows its count", "3 photos" in index)

# ---- ordering: newest first, sort overrides
with app.app_context():
    rows = GalleryImage.query.filter_by(album_id=seaside.id).all()
    base = datetime.utcnow()
    for i, row in enumerate(rows):          # beach, pier, icecream
        row.created_at = base - timedelta(hours=i)
    db.session.commit()
    names = [r.filename for r in rows]
# From <main> onwards: the head's og:image names the cover first.
order = [m for m in re.findall(r"uploads/([0-9a-f]{32}[^\"']*)",
                               get("/gallery/seaside-trip").split("<main", 1)[1])]
check("newest photo comes first",
      order[0].replace("-thumb", "") == names[0], str(order[:2]))
with app.app_context():
    last = GalleryImage.query.filter_by(filename=names[2]).first()
    last.sort = -10                        # pin the oldest to the top
    db.session.commit()
order = [m for m in re.findall(r"uploads/([0-9a-f]{32}[^\"']*)",
                               get("/gallery/seaside-trip").split("<main", 1)[1])]
check("sort overrides the date",
      order[0].replace("-thumb", "") == names[2], str(order[:2]))

# ---- moving photos in bulk
with app.app_context():
    move_ids = [r.id for r in
                GalleryImage.query.filter_by(album_id=seaside.id).all()[:2]]
r = client.post("/admin/gallery/move",
                data={"photo_ids": [str(i) for i in move_ids],
                      "target_album": str(eid.id)}, follow_redirects=True)
check("bulk move accepted", b"photo(s) moved" in r.data)
with app.app_context():
    check("photos moved into the target album",
          GalleryImage.query.filter_by(album_id=eid.id).count() == 3)
    check("and left the old one",
          GalleryImage.query.filter_by(album_id=seaside.id).count() == 1)

r = client.post("/admin/gallery/move",
                data={"photo_ids": [str(move_ids[0])], "target_album": ""},
                follow_redirects=True)
check("photos can be moved back out to unfiled", b"photo(s) moved" in r.data)
with app.app_context():
    check("photo is unfiled now",
          db.session.get(GalleryImage, move_ids[0]).album_id is None)
r = client.post("/admin/gallery/move", data={"target_album": ""},
                follow_redirects=True)
check("moving nothing is refused politely", b"Tick the photos" in r.data)

# ---- unpublished albums are hidden, and 404 by slug
client.post("/admin/gallery/albums/%d/edit" % eid.id,
            data={"title": "Eid celebration", "sort": "0"},
            follow_redirects=True)          # no `published` = hidden
with app.app_context():
    check("album is now hidden",
          db.session.get(GalleryAlbum, eid.id).published is False)
index = get("/gallery")
check("hidden album absent from the index", "Eid celebration" not in index)
r = client.get("/gallery/eid-celebration")
check("hidden album 404s by slug", r.status_code == 404, str(r.status_code))
all_html = get("/gallery/all")
with app.app_context():
    hidden_names = [r.filename for r in
                    GalleryImage.query.filter_by(album_id=eid.id).all()]
check("hidden album's photos absent from all-photos",
      not any(n in all_html for n in hidden_names), str(hidden_names))
sitemap = get("/sitemap.xml")
check("hidden album absent from the sitemap",
      "/gallery/eid-celebration" not in sitemap)
check("published album listed in the sitemap",
      "/gallery/seaside-trip" in sitemap)
check("all-photos listed in the sitemap", "/gallery/all" in sitemap)
check("admin can still reach a hidden album's photos",
      client.get("/admin/gallery?album=%d" % eid.id).status_code == 200)

client.post("/admin/gallery/albums/%d/edit" % eid.id,
            data={"title": "Eid celebration", "sort": "0", "published": "on"},
            follow_redirects=True)

# ---- deleting an album keeps every photograph
with app.app_context():
    doomed = GalleryAlbum.query.filter_by(title="Eid celebration").first()
    kept = [r.filename for r in
            GalleryImage.query.filter_by(album_id=doomed.id).all()]
    total_before = GalleryImage.query.count()
check("album has photos to lose", len(kept) == 2, str(kept))
r = client.post("/admin/gallery/albums/%d/delete" % doomed.id,
                follow_redirects=True)
check("album deleted", b"Album deleted." in r.data)
with app.app_context():
    check("the album really is gone",
          GalleryAlbum.query.filter_by(title="Eid celebration").first() is None)
    check("NO photographs were deleted with it",
          GalleryImage.query.count() == total_before,
          "%d vs %d" % (GalleryImage.query.count(), total_before))
    check("its photos are unfiled now",
          all(GalleryImage.query.filter_by(filename=n).first().album_id is None
              for n in kept))
check("the files are still on disk",
      all(os.path.isfile(os.path.join(SANDBOX, n)) for n in kept), str(kept))
all_html = get("/gallery/all")
check("and they are reachable again under all photos",
      all(thumb_name(n) in all_html or n in all_html for n in kept))

# ---- lightbox markup, and what happens without JavaScript
album_html = get("/gallery/seaside-trip")
check("lightbox container present", 'id="lightbox"' in album_html)
check("lightbox is a dialog", 'role="dialog"' in album_html
      and 'aria-modal="true"' in album_html)
check("lightbox starts hidden", re.search(r'id="lightbox"[^>]*hidden',
                                          album_html) is not None)
for control in ("lightboxClose", "lightboxPrev", "lightboxNext",
                "lightboxImage", "lightboxCaption", "lightboxCount"):
    check("lightbox has %s" % control, ('id="%s"' % control) in album_html)
check("keyboard handling shipped", "ArrowLeft" in album_html
      and "ArrowRight" in album_html and "Escape" in album_html)
check("swipe handling shipped", "touchstart" in album_html
      and "touchend" in album_html)
check("no JS library loaded",
      not offsite_scripts(album_html)
      and "cdn" not in album_html.lower(),
      str(offsite_scripts(album_html)))
check("script uses var, per house style",
      "var links" in album_html and "const " not in album_html
      and "let " not in album_html)
with app.app_context():
    remaining = GalleryImage.query.filter_by(album_id=seaside.id).count()
check("degrades without JS: every photo is a plain link to the file",
      album_html.count('<a href="/static/uploads/') == remaining,
      "%d links for %d photos"
      % (album_html.count('<a href="/static/uploads/'), remaining))
check("captions travel with the links", "data-caption=" in album_html)

# ---- an empty album renders, with no lightbox to open
make_album("Empty album")
empty_album = album_by_title("Empty album")
html = get("/gallery/empty-album")
check("empty album -> 200",
      client.get("/gallery/empty-album").status_code == 200)
check("empty album says so", "No photos in this album yet." in html)
check("empty album ships no lightbox", 'id="lightbox"' not in html)

# ---- deleting a photo still deletes the file and its thumbnail
with app.app_context():
    victim = GalleryImage.query.filter_by(album_id=seaside.id).first()
    vname, vid = victim.filename, victim.id
client.post("/admin/gallery/%d/delete" % vid, follow_redirects=True)
with app.app_context():
    check("photo gone from the database",
          db.session.get(GalleryImage, vid) is None)
check("file and thumbnail removed",
      not os.path.isfile(os.path.join(SANDBOX, vname))
      and not os.path.isfile(os.path.join(SANDBOX, thumb_name(vname))))

# ---- teardown
with app.app_context():
    db.session.remove()
    db.engine.dispose()
shutil.rmtree(SANDBOX, ignore_errors=True)
appmod.UPLOAD_DIR = REAL_UPLOAD_DIR
check("sandbox uploads folder removed", not os.path.isdir(SANDBOX))
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
