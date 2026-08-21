"""Smoke test for the image pipeline (CLAUDE.md rules).

Covers: an oversized upload is stored downscaled with a thumbnail beside
it; an image already small enough is left alone; EXIF is stripped and the
orientation flag applied first, so a portrait phone photo is not stored
sideways and its GPS tags do not survive; a non-image and a corrupt image
are refused with a flash rather than a 500; delete removes the file AND
its thumbnail; templates render thumbnails on cards and full size on
detail views; and reprocess-images is idempotent.

Runs against a throwaway SQLite db AND a throwaway uploads folder, so
neither instance/ebwa.db nor static/uploads is touched.

Run:  python tests/smoke_test_uploads.py
"""
import io
import os
import shutil
import sys
import tempfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_uploads.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image                                          # noqa: E402
from werkzeug.datastructures import FileStorage                # noqa: E402

import app as appmod                                           # noqa: E402
from app import (app, db, Block, DEFAULT_BLOCKS, Event,        # noqa: E402
                 FEATURES, FeatureFlag, GalleryImage, MAX_IMAGE_WIDTH,
                 NewsPost, THUMB_WIDTH, User, thumb_name)

app.config["TESTING"] = True

# A scratch uploads folder: the pipeline writes real files, and the real
# one holds the developer's demo photos.
REAL_UPLOAD_DIR = appmod.UPLOAD_DIR
SANDBOX = tempfile.mkdtemp(prefix="ebwa-uploads-")
appmod.UPLOAD_DIR = SANDBOX

PW = "uploads-test-password"
# The JPEG APP1 marker every EXIF block starts with, GPS included.
EXIF_MARKER = b"Exif" + bytes(2)
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def photo(width, height, mode="RGB", fmt="JPEG", exif=None):
    """Bytes of a plausible photograph — noisy enough not to compress to
    nothing, which is what makes the size assertions mean anything."""
    im = Image.new(mode, (width, height))
    px = im.load()
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            px[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256) \
                if mode == "RGB" else ((x * 7) % 256, (y * 13) % 256,
                                       ((x + y) * 3) % 256, 255)
    buf = io.BytesIO()
    if exif is not None:
        im.save(buf, fmt, exif=exif)
    else:
        im.save(buf, fmt)
    return buf.getvalue()


def exif_with_orientation(value):
    """EXIF as a phone writes it: an orientation flag and camera details.

    GPS coordinates live in a sub-IFD of this same EXIF segment, and
    Pillow's writer will not serialise one, so the test proves the point
    a stronger way: it asserts the stored file carries no EXIF segment at
    all. No segment, no GPS.
    """
    ex = Image.Exif()
    ex[274] = value                       # Orientation
    ex[271] = "TestPhone"                 # Make
    ex[272] = "TestPhone 14 Pro"          # Model
    return ex.tobytes()


def sandbox_files():
    return sorted(os.listdir(SANDBOX))


def size_of(name):
    return os.path.getsize(os.path.join(SANDBOX, name))


def open_stored(name):
    """Decode a stored file from bytes, so no handle is left open on it —
    Windows will not let the app delete a file the test still holds."""
    with open(os.path.join(SANDBOX, name), "rb") as fh:
        return Image.open(io.BytesIO(fh.read()))


def upload_via_admin(raw, filename):
    """Upload through a real admin form, so the flash path is exercised."""
    return client.post(
        "/admin/gallery",
        data={"images": (io.BytesIO(raw), filename), "caption": "Test"},
        content_type="multipart/form-data", follow_redirects=True)


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
    db.session.commit()

client = app.test_client()
client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})

# ---- an oversized photo is downscaled, and gets a thumbnail
big = photo(3000, 2000)
r = upload_via_admin(big, "holiday.jpg")
check("oversized upload accepted", b"image(s) uploaded" in r.data,
      r.data[:160].decode("utf-8", "replace"))
with app.app_context():
    stored = GalleryImage.query.order_by(GalleryImage.id.desc()).first()
    name = stored.filename
check("stored with a uuid name and jpg extension",
      len(name.split(".")[0]) == 32 and name.endswith(".jpg"), name)
im = open_stored(name)
check("downscaled to the ceiling", im.width == MAX_IMAGE_WIDTH,
      "%dx%d" % im.size)
check("aspect ratio preserved", abs(im.width / im.height - 1.5) < 0.01,
      "%dx%d" % im.size)
check("stored file is smaller than the upload",
      size_of(name) < len(big), "%d vs %d" % (size_of(name), len(big)))
thumb = thumb_name(name)
check("thumbnail written beside it", os.path.isfile(
    os.path.join(SANDBOX, thumb)), str(sandbox_files()))
timg = open_stored(thumb)
check("thumbnail is 600px wide", timg.width == THUMB_WIDTH, str(timg.size))
check("thumbnail is much smaller than the full size",
      size_of(thumb) < size_of(name) / 2,
      "%d vs %d" % (size_of(thumb), size_of(name)))

# ---- an image already small enough is left exactly as it was
small = photo(500, 400)
r = upload_via_admin(small, "small.jpg")
with app.app_context():
    small_name = GalleryImage.query.order_by(
        GalleryImage.id.desc()).first().filename
check("small image kept byte for byte", size_of(small_name) == len(small),
      "%d vs %d" % (size_of(small_name), len(small)))
check("small image not resized", open_stored(small_name).size == (500, 400),
      str(open_stored(small_name).size))
check("no thumbnail for an image already thumbnail-sized",
      not os.path.isfile(os.path.join(SANDBOX, thumb_name(small_name))))
with app.test_request_context():
    check("and the template falls back to the original",
          appmod.thumb_url(small_name).endswith(small_name),
          appmod.thumb_url(small_name))

# ---- EXIF is stripped, and the orientation flag applied first
#      Orientation 6 = "rotate 90° clockwise to display" — a portrait
#      photo off a phone, stored landscape with a flag.
sideways = photo(1200, 800, exif=exif_with_orientation(6))
check("the fixture really carries EXIF",
      bool(Image.open(io.BytesIO(sideways)).getexif())
      and EXIF_MARKER in sideways)
r = upload_via_admin(sideways, "portrait.jpg")
with app.app_context():
    rot_name = GalleryImage.query.order_by(
        GalleryImage.id.desc()).first().filename
rot = open_stored(rot_name)
check("orientation applied — stored portrait, not sideways",
      rot.height > rot.width, "%dx%d" % rot.size)
check("EXIF gone from the stored file", not rot.getexif(),
      str(dict(rot.getexif())))
stored_bytes = open(os.path.join(SANDBOX, rot_name), "rb").read()
check("no EXIF segment at all — so no GPS either",
      EXIF_MARKER not in stored_bytes
      and b"TestPhone" not in stored_bytes)

# ---- transparency survives: a logo is not flattened onto black
logo = photo(900, 300, mode="RGBA", fmt="PNG")
r = upload_via_admin(logo, "logo.png")
with app.app_context():
    logo_name = GalleryImage.query.order_by(
        GalleryImage.id.desc()).first().filename
check("image with alpha stays a png", logo_name.endswith(".png"), logo_name)
check("and keeps its alpha channel",
      open_stored(logo_name).mode in ("RGBA", "LA", "P"),
      open_stored(logo_name).mode)

# ---- rubbish in, flash out — never a 500
before = len(sandbox_files())
r = upload_via_admin(b"this is definitely not an image", "notes.jpg")
check("non-image refused with 200 + flash", r.status_code == 200
      and b"could not be read as an image" in r.data, str(r.status_code))
check("nothing written for it", len(sandbox_files()) == before)
truncated = big[:len(big) // 3]
r = upload_via_admin(truncated, "corrupt.jpg")
check("corrupt image refused with 200 + flash", r.status_code == 200
      and b"could not be read as an image" in r.data, str(r.status_code))
check("nothing written for that either", len(sandbox_files()) == before)
r = upload_via_admin(b"", "empty.jpg")
check("empty file refused", r.status_code == 200, str(r.status_code))
r = upload_via_admin(big, "photo.tiff")
check("disallowed extension still refused",
      b"Image must be one of" in r.data)
check("still nothing written", len(sandbox_files()) == before)

# ---- deleting an upload takes its thumbnail with it
check("the pair is on disk before deleting",
      os.path.isfile(os.path.join(SANDBOX, name))
      and os.path.isfile(os.path.join(SANDBOX, thumb)))
with app.app_context():
    gid = GalleryImage.query.filter_by(filename=name).first().id
client.post("/admin/gallery/%d/delete" % gid)
check("full-size file removed", not os.path.isfile(
    os.path.join(SANDBOX, name)))
check("thumbnail removed too", not os.path.isfile(
    os.path.join(SANDBOX, thumb)), str(sandbox_files()))

# ---- templates: thumbnails on cards, full size on detail views
with app.app_context():
    ev = Event()
    ev.title, ev.slug = "Summer fete", "summer-fete"
    ev.event_date = date.today()
    ev.description = "A day in the park."
    ev.published = True
    db.session.add(ev)
    post = NewsPost()
    post.title, post.slug = "New minibus", "new-minibus"
    post.published_date = date.today()
    post.body = "It arrived on Tuesday."
    post.published = True
    db.session.add(post)
    db.session.commit()
    # give both the big photo, which has a thumbnail
    with app.test_request_context():
        card = appmod.save_upload(
            FileStorage(stream=io.BytesIO(big), filename="card.jpg"))
    ev.image = post.image = card
    db.session.commit()
    ev_id = ev.id
card_thumb = thumb_name(card)
listing = client.get("/events").data.decode("utf-8")
check("listing card uses the thumbnail",
      card_thumb in listing and card not in listing.replace(card_thumb, ""))
detail = client.get("/events/summer-fete").data.decode("utf-8")
check("detail view uses the full size",
      card in detail.replace(card_thumb, ""))
home = client.get("/").data.decode("utf-8")
check("homepage strips use thumbnails", card_thumb in home)
check("admin form preview uses the thumbnail",
      card_thumb in client.get("/admin/events/%d/edit"
                               % ev_id).data.decode("utf-8"))

# ---- reprocess-images: fixes legacy files, then does nothing
legacy_big = photo(2400, 1600, exif=exif_with_orientation(1))
legacy_name = "legacy_upload.jpg"
open(os.path.join(SANDBOX, legacy_name), "wb").write(legacy_big)
runner = app.test_cli_runner()
res = runner.invoke(args=["reprocess-images"])
check("reprocess ran cleanly", res.exit_code == 0, res.output[-300:])
check("reprocess reports what it did", "optimised" in res.output, res.output)
check("legacy file downscaled",
      open_stored(legacy_name).width == MAX_IMAGE_WIDTH,
      str(open_stored(legacy_name).size))
check("legacy file lost its EXIF", not open_stored(legacy_name).getexif())
check("legacy file kept its name", os.path.isfile(
    os.path.join(SANDBOX, legacy_name)))
check("legacy file gained a thumbnail", os.path.isfile(
    os.path.join(SANDBOX, thumb_name(legacy_name))))

sizes = {f: size_of(f) for f in sandbox_files()}
res = runner.invoke(args=["reprocess-images"])
check("second run is a no-op", "0 optimised" in res.output
      and "0 thumbnail(s) created" in res.output, res.output)
check("second run changed no bytes",
      {f: size_of(f) for f in sandbox_files()} == sizes)
res = runner.invoke(args=["reprocess-images"])
check("third run too", "0 optimised" in res.output, res.output)

# ---- an unreadable file in the folder is reported, not fatal
open(os.path.join(SANDBOX, "broken.jpg"), "wb").write(b"not an image")
res = runner.invoke(args=["reprocess-images"])
check("unreadable file survives reprocess", res.exit_code == 0)
check("and is reported", "unreadable" in res.output, res.output[-200:])
check("and is left alone", os.path.isfile(os.path.join(SANDBOX, "broken.jpg")))

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
