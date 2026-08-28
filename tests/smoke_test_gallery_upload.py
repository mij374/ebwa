"""The gallery upload route, both ways into it (CLAUDE.md rules).

There are two paths through `/admin/gallery` POST and the point of this
file is that BOTH of them work, because one of them is the fallback for
the other:

  * THE PLAIN FORM — every chosen file in one multipart request, which
    is what a browser with no JavaScript posts and what the page has
    always done. It must keep flashing its count and redirecting.
  * ONE FILE PER REQUEST — what the progress script posts so it can
    draw a determinate bar, asking for JSON back. A photograph the
    server cannot read is reported against its own filename, the run
    carries on, and nothing already stored is discarded.

And the sentence a person actually reads is composed by neither of
those: it comes off a tally in the session when the gallery is next
DRAWN, because no single request in a run of twelve knows the totals.
That is the same rule as `backup_started_flash()`, and it is checked
here — including that the summary NAMES the file that failed, since
"1 failed" with no name is a message nobody can act on.

Runs against a throwaway SQLite db AND a throwaway uploads folder, so
neither instance/ebwa.db nor static/uploads is touched.

Run:  python tests/smoke_test_gallery_upload.py
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_gallery_upload.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image                                          # noqa: E402

import app as appmod                                           # noqa: E402
from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,     # noqa: E402
                 FeatureFlag, GalleryAlbum, GalleryImage, AuditLog, User,
                 upload_limit_mb, upload_limit_message)

app.config["TESTING"] = True

REAL_UPLOAD_DIR = appmod.UPLOAD_DIR
SANDBOX = tempfile.mkdtemp(prefix="ebwa-upload-")
appmod.UPLOAD_DIR = SANDBOX

PW = "upload-test-password"
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def photo_bytes(width, height):
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
# Named like a photograph, and not one. This is the file the progress
# bar has to survive: the run must finish and it must be NAMED.
NOT_AN_IMAGE = b"this is not an image, whatever the extension says" * 20


def photo_count():
    with app.app_context():
        return GalleryImage.query.count()


def flashes(html):
    """Just the flash messages, not the whole page.

    Searching the raw HTML for the sentence is not the same check: the
    upload form carries that exact sentence in its `data-too-large`
    attribute for the progress script to use, so "is it on the page?"
    is TRUE on every gallery page whether or not anything went wrong.
    Both of those assertions passed with the 413 handler switched off.
    """
    return " | ".join(re.findall(
        r'<div class="flash flash-\w+">(.*?)</div>', html, re.S))


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
    album = GalleryAlbum(title="Seaside trip", slug="seaside-trip",
                         published=True)
    db.session.add(album)
    db.session.commit()
    ALBUM_ID = album.id

client = app.test_client()

# ---- the route is still behind a login, whichever way it is posted
r = client.post("/admin/gallery",
                data={"images": (io.BytesIO(LANDSCAPE), "anon.jpg")},
                content_type="multipart/form-data")
check("anonymous upload -> login redirect",
      r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
      str(r.status_code))
r = client.post("/admin/gallery",
                data={"images": (io.BytesIO(LANDSCAPE), "anon.jpg"),
                      "ajax": "1"},
                content_type="multipart/form-data")
check("anonymous JSON upload -> login redirect too",
      r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
      str(r.status_code))
check("nothing stored while signed out", photo_count() == 0)

client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})

# ---------------------------------------------------------------- plain form
# The path that must never change: several files, one request, a count
# flashed on the page it redirects to.
r = client.post("/admin/gallery",
                data={"images": [(io.BytesIO(LANDSCAPE), "one.jpg"),
                                 (io.BytesIO(PORTRAIT), "two.jpg"),
                                 (io.BytesIO(LANDSCAPE), "three.jpg")],
                      "album_id": str(ALBUM_ID), "caption": "Day out"},
                content_type="multipart/form-data", follow_redirects=True)
check("multi-file POST -> 200", r.status_code == 200, str(r.status_code))
check("multi-file POST stored all three", photo_count() == 3,
      str(photo_count()))
check("multi-file POST flashes its count",
      "3 image(s) uploaded." in r.data.decode("utf-8"))
with app.app_context():
    filed = GalleryImage.query.filter_by(album_id=ALBUM_ID).count()
    caption = GalleryImage.query.first().caption
check("multi-file POST filed them in the album", filed == 3, str(filed))
check("multi-file POST kept the caption", caption == "Day out", caption)

# A single file through the SAME field, which is what a browser posts
# when only one is chosen — nothing about the plain path assumes several.
r = client.post("/admin/gallery",
                data={"images": (io.BytesIO(PORTRAIT), "single.jpg")},
                content_type="multipart/form-data", follow_redirects=True)
check("single file under `images` stored", photo_count() == 4,
      str(photo_count()))
check("single file flashes a count of one",
      "1 image(s) uploaded." in r.data.decode("utf-8"))

# And under `image`, so a single-file POST from anywhere else works
# without a second route to keep in step.
r = client.post("/admin/gallery",
                data={"image": (io.BytesIO(LANDSCAPE), "other-field.jpg")},
                content_type="multipart/form-data", follow_redirects=True)
check("single file under `image` stored", photo_count() == 5,
      str(photo_count()))

# A file that is not an image is refused, flashed, and costs the request
# nothing else.
before = photo_count()
r = client.post("/admin/gallery",
                data={"images": [(io.BytesIO(LANDSCAPE), "good.jpg"),
                                 (io.BytesIO(NOT_AN_IMAGE), "beach.jpg")]},
                content_type="multipart/form-data", follow_redirects=True)
body = r.data.decode("utf-8")
check("plain path stores the good file and refuses the bad one",
      photo_count() == before + 1, "%d -> %d" % (before, photo_count()))
check("plain path says why the bad one was refused",
      "could not be read as an image" in body)

# ------------------------------------------------------- one file per request
# What the progress script does. Each request answers in JSON and draws
# no page, so the counts have to travel to the next GET by themselves.
def send_one(raw, filename, album_id="", caption=""):
    r = client.post("/admin/gallery",
                    data={"images": (io.BytesIO(raw), filename),
                          "album_id": str(album_id), "caption": caption,
                          "ajax": "1"},
                    content_type="multipart/form-data")
    return r, json.loads(r.data.decode("utf-8"))


before = photo_count()
r, answer = send_one(LANDSCAPE, "ajax-one.jpg", ALBUM_ID, "From the script")
check("per-file upload -> 200 JSON", r.status_code == 200
      and r.headers.get("Content-Type", "").startswith("application/json"),
      "%s %s" % (r.status_code, r.headers.get("Content-Type")))
check("per-file upload reports one added",
      answer == {"added": 1, "failed": []}, str(answer))
check("per-file upload stored the row", photo_count() == before + 1)
with app.app_context():
    newest = GalleryImage.query.order_by(GalleryImage.id.desc()).first()
check("per-file upload carried the album", newest.album_id == ALBUM_ID)
check("per-file upload carried the caption",
      newest.caption == "From the script", newest.caption)
check("per-file upload wrote the file to disk",
      os.path.isfile(os.path.join(SANDBOX, newest.filename)), newest.filename)

# It does NOT redirect: a redirect is a page, and the script is drawing
# a bar rather than following one.
check("per-file upload does not redirect", r.status_code == 200,
      str(r.status_code))

# The failure that matters. It must be reported with the FILENAME and a
# reason, and it must not stop what comes after it.
before = photo_count()
r, answer = send_one(NOT_AN_IMAGE, "beach.heic")
check("bad file reports nothing added", answer.get("added") == 0, str(answer))
check("bad file is named in the answer",
      answer.get("failed") and answer["failed"][0]["name"] == "beach.heic",
      str(answer))
check("bad file says why — an extension we do not take",
      "Image must be one of" in answer["failed"][0]["error"], str(answer))
check("bad file stored nothing", photo_count() == before, str(photo_count()))

# The other kind: the right extension on bytes Pillow cannot decode. A
# different reason, and the one somebody is least able to guess at.
r, answer = send_one(NOT_AN_IMAGE, "looks-fine.jpg")
check("undecodable bytes are refused with their own reason",
      answer.get("added") == 0 and "could not be read as an image"
      in answer["failed"][0]["error"], str(answer))
check("undecodable bytes stored nothing", photo_count() == before,
      str(photo_count()))

r, answer = send_one(PORTRAIT, "after-the-bad-one.jpg")
check("the run carries on after a failure", answer.get("added") == 1,
      str(answer))
check("and the earlier photos are still there", photo_count() == before + 1,
      str(photo_count()))

# A request with no file at all is answered rather than crashed — a
# stale tab or a hand-made POST must not be a 500.
r = client.post("/admin/gallery", data={"ajax": "1"},
                content_type="multipart/form-data")
answer = json.loads(r.data.decode("utf-8"))
check("an empty per-file POST is answered, not raised",
      r.status_code == 200 and answer.get("added") == 0, str(answer))

# ------------------------------------------------------- the summary sentence
# Composed WHEN THE PAGE IS DRAWN, off the tally those requests left
# behind — no request in the run knew the totals.
body = client.get("/admin/gallery").data.decode("utf-8")
check("the next page summarises the whole run",
      "2 photos added" in body, body[body.find("flash"):][:300])
check("the summary counts both failures", "2 failed" in body,
      body[body.find("flash"):][:300])
check("the summary NAMES the file that failed", "beach.heic" in body,
      body[body.find("flash"):][:300])
check("a failed file makes it an error flash", 'flash-error' in body)

# And it is spent: a reload is not the same news a second time.
body = client.get("/admin/gallery").data.decode("utf-8")
check("the summary is not repeated on the next load",
      "beach.heic" not in body)

# One photograph on its own reads as one photograph.
send_one(LANDSCAPE, "just-the-one.jpg")
body = client.get("/admin/gallery").data.decode("utf-8")
check("one photo is singular", "1 photo added." in body,
      body[body.find("flash"):][:200])
check("and with nothing wrong it is an ok flash",
      'flash-ok' in body and 'flash-error' not in body)

# ---- the audit log records the uploads, per request, naming the album
with app.app_context():
    entries = AuditLog.query.filter_by(action="create").all()
    summaries = [e.summary for e in entries]
check("every upload is audit-logged",
      len([s for s in summaries if "gallery image" in s]) >= 6,
      str(summaries[:3]))
check("an album upload names the album",
      any("Seaside trip" in s for s in summaries), str(summaries[:3]))

# ------------------------------------------------------- too big to accept
# Werkzeug refuses a request past MAX_CONTENT_LENGTH before any route
# runs. Without a handler that is a bare "413 Request Entity Too Large"
# page — no heading, no navigation, nothing to do next — for what is
# probably the commonest mistake anybody makes on this form.
LIMIT = app.config["MAX_CONTENT_LENGTH"]
OVERSIZE = b"x" * (LIMIT + 1024)
check("the fixture really is over the limit", len(OVERSIZE) > LIMIT,
      "%d vs %d" % (len(OVERSIZE), LIMIT))

before = photo_count()
r = client.post("/admin/gallery",
                data={"images": (io.BytesIO(OVERSIZE), "enormous.jpg")},
                content_type="multipart/form-data",
                headers={"Referer": "http://localhost/admin/gallery?album=2"})
check("an oversized upload redirects rather than showing a 413 page",
      r.status_code == 302, str(r.status_code))
check("and it goes back to the form it came from",
      r.headers.get("Location", "").startswith("/admin/gallery"),
      r.headers.get("Location"))
check("nothing was stored from it", photo_count() == before)

body = client.get("/admin/gallery").data.decode("utf-8")
check("the reason is flashed in words",
      "That file is too large" in flashes(body), flashes(body)[:200])
check("the flash names the limit",
      "%dMB" % upload_limit_mb() in flashes(body), flashes(body)[:200])
check("the flash is an error, not a cheerful notice", "flash-error" in body)

# THE LIMIT IS READ, NOT WRITTEN OUT AGAIN. Move the config and the
# sentence has to move with it, or there is a second copy of the number
# somewhere waiting to go stale.
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
check("the message follows the config", "20MB" in upload_limit_message(),
      upload_limit_message())
app.config["MAX_CONTENT_LENGTH"] = LIMIT
check("and back again", "%dMB" % (LIMIT // (1024 * 1024))
      in upload_limit_message(), upload_limit_message())

# ---- the same thing through the progress script's path
# IT MUST NOT BE A REDIRECT. XMLHttpRequest follows one silently, so the
# script would read a 200 and a page of HTML where a 413 had happened,
# and report the one failure it can explain best as an answer it could
# not read.
r = client.post("/admin/gallery",
                data={"images": (io.BytesIO(OVERSIZE), "enormous.jpg"),
                      "ajax": "1"},
                content_type="multipart/form-data",
                headers={"X-Requested-With": "XMLHttpRequest"})
check("the script's path gets a 413, not a redirect", r.status_code == 413,
      "%s -> %s" % (r.status_code, r.headers.get("Location")))
check("and it is JSON it can read",
      r.headers.get("Content-Type", "").startswith("application/json"),
      r.headers.get("Content-Type"))
answer = json.loads(r.data.decode("utf-8"))
check("the script is told the same sentence",
      answer.get("failed") and "too large" in answer["failed"][0]["error"],
      str(answer))
check("still nothing stored", photo_count() == before)

# ---- a batch where ONE photo is oversized: the rest must still go up.
# This is the case the whole per-file design exists for.
clear = photo_count()
sent, refused = 0, []
for i in range(1, 13):
    raw, name = (LANDSCAPE, "batch-%02d.jpg" % i)
    if i == 5:
        raw, name = OVERSIZE, "batch-05-enormous.jpg"
    r = client.post("/admin/gallery",
                    data={"images": (io.BytesIO(raw), name), "ajax": "1"},
                    content_type="multipart/form-data",
                    headers={"X-Requested-With": "XMLHttpRequest"})
    if r.status_code == 413:
        refused.append(name)
    else:
        sent += json.loads(r.data.decode("utf-8")).get("added", 0)
check("photo 5 of 12 was the only one refused", refused ==
      ["batch-05-enormous.jpg"], str(refused))
check("the other eleven all stored", sent == 11, str(sent))
check("and they are really on the table", photo_count() == clear + 11,
      "%d -> %d" % (clear, photo_count()))

# ---- AND A 413 MUST REACH THE SUMMARY. The route never ran for that
# file, so nothing recorded it — the run came out as a cheerful
# "11 photos added." with the twelfth simply absent from the count.
# note_gallery_upload() promises every failure is COUNTED even when it
# cannot be named; this is the path that broke that promise.
clear = photo_count()
client.get("/admin/gallery")                       # spend anything pending
for i in (1, 2):
    client.post("/admin/gallery",
                data={"images": (io.BytesIO(LANDSCAPE), "fine-%d.jpg" % i),
                      "ajax": "1"},
                content_type="multipart/form-data",
                headers={"X-Requested-With": "XMLHttpRequest"})
client.post("/admin/gallery",
            data={"images": (io.BytesIO(OVERSIZE), "enormous.jpg"),
                  "ajax": "1"},
            content_type="multipart/form-data",
            headers={"X-Requested-With": "XMLHttpRequest",
                     "X-Upload-Name": "enormous.jpg"})
body = client.get("/admin/gallery").data.decode("utf-8")
check("a 413 is counted in the summary, not dropped from it",
      "2 photos added" in flashes(body) and "1 failed" in flashes(body),
      flashes(body)[:240])
check("and the header lets it be NAMED",
      "enormous.jpg" in flashes(body), flashes(body)[:240])
check("the two good ones were still stored", photo_count() == clear + 2,
      "%d -> %d" % (clear, photo_count()))

# With no header there is nothing to name it with, and it says so rather
# than inventing a filename or going quiet.
client.post("/admin/gallery",
            data={"images": (io.BytesIO(OVERSIZE), "enormous.jpg"),
                  "ajax": "1"},
            content_type="multipart/form-data",
            headers={"X-Requested-With": "XMLHttpRequest"})
body = client.get("/admin/gallery").data.decode("utf-8")
check("an unnamed 413 is still counted", "1 failed" in flashes(body),
      flashes(body)[:240])

# ---- ALL EIGHT FORMS THAT TAKE AN IMAGE, not just this one. The
# handler is registered on the app rather than on the route, and the
# only way to say that is to post an oversized file at each of them.
UPLOAD_FORMS = [
    "/admin/content",
    "/admin/gallery",
    "/admin/gallery/albums/new",
    "/admin/events/new",
    "/admin/news/new",
    "/admin/journey/new",
    "/admin/partners/new",
    "/admin/campaigns/new",
    "/admin/content-images/about/0/add",
]
for url in UPLOAD_FORMS:
    r = client.post(url,
                    data={"images": (io.BytesIO(OVERSIZE), "enormous.jpg"),
                          "image": (io.BytesIO(OVERSIZE), "enormous.jpg")},
                    content_type="multipart/form-data",
                    headers={"Referer": "http://localhost" + url})
    page = client.get(url if url.endswith("new") or url == "/admin/content"
                      else "/admin/gallery").data.decode("utf-8")
    check("%s: oversized upload is explained, not a 413 page" % url,
          r.status_code == 302
          and "That file is too large" in flashes(page),
          "%s; flash said: %r" % (r.status_code, flashes(page)[:120]))

# ---- the referrer is not a way out of the site.
# A form on somebody else's page can post at ours perfectly well, and
# the Referer it sends is theirs. Redirecting to it unchecked is an open
# redirect that fires on a signed-in admin.
r = client.post("/admin/gallery",
                data={"images": (io.BytesIO(OVERSIZE), "enormous.jpg")},
                content_type="multipart/form-data",
                headers={"Referer": "https://example.invalid/collect"})
where = r.headers.get("Location", "")
check("an off-site Referer is not followed",
      "example.invalid" not in where and where.startswith("/"), where)
client.get("/admin/gallery")          # spend the flash

# ---------------------------------------------------------------- the markup
body = client.get("/admin/gallery").data.decode("utf-8")
check("the plain multipart form is still there",
      'enctype="multipart/form-data"' in body and 'name="images"' in body
      and "multiple" in body)
check("the form still posts to the gallery with no action of its own",
      'id="uploadForm"' in body and 'action=' not in
      body[body.find('id="uploadForm"') - 200:body.find('id="uploadForm"')])
check("the progress panel is present and hidden until the script has news",
      'id="uploadProgress"' in body and 'upload-progress is-hidden' in body)
check("the bar is a labelled progressbar",
      'role="progressbar"' in body and 'aria-valuenow="0"' in body
      and 'aria-label="Upload progress"' in body)
check("the status line is a live region",
      'id="uploadStatus"' in body and 'aria-live="polite"' in body)
check("the upload button opts into the busy state",
      'data-busy="Uploading photos' in body)
check("the finish URL is an attribute, not Jinja inside the script",
      'data-done-url="/admin/gallery"' in body)

# ---- busy.js is loaded by both shells, versioned like every other asset
admin_head = client.get("/admin").data.decode("utf-8")
public_head = client.get("/").data.decode("utf-8")
for name, page in (("admin", admin_head), ("public", public_head)):
    check("%s shell loads busy.js" % name, "js/busy.js" in page)
    at = page.find("js/busy.js")
    check("%s shell versions busy.js" % name, "?v=" in page[at:at + 40],
          page[at:at + 60])
    check("%s shell defers busy.js" % name, "defer" in page[at:at + 90],
          page[at:at + 90])

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
