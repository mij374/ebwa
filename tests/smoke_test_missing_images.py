"""A content image whose FILE IS GONE must not render as a hole.

A ContentImage row and the file it names are two different things, and
they can part company: a file deleted by hand, an uploads folder
restored from an incomplete copy, an rsync that missed one. What a
visitor saw then was an empty panel with the alt text sitting in it,
which reads as a broken website rather than as a missing photograph —
and nothing anywhere said so. Found on the demo VPS: one row on /about
pointing at a file that 404s.

So: visitors get the layout closed up around it, signed-in admins get a
notice naming the file, and `flask --app app check-uploads` finds them
all without anybody having to look at every page.

Run:  python tests/smoke_test_missing_images.py
"""
import os
import shutil
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEST_DB = os.path.join(HERE, "test_missing_images.db")
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _s):
        os.remove(TEST_DB + _s)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, ROOT)

from werkzeug.security import generate_password_hash    # noqa: E402
from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,  # noqa: E402
                 FeatureFlag, ContentImage, Milestone, NewsPost, User,
                 UPLOAD_DIR, dangling_uploads, present_images,
                 upload_on_disk)

app.config["TESTING"] = True
failures = []

REAL = "missing-images-real.png"
GONE = "missing-images-gone.png"
PW = "missing-images-password"


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


shutil.copyfile(os.path.join(ROOT, "static", "img", "ebwa-logo.png"),
                os.path.join(UPLOAD_DIR, REAL))
assert not os.path.isfile(os.path.join(UPLOAD_DIR, GONE))

with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
    for n, _l, _d, _default in FEATURES:
        if not FeatureFlag.query.filter_by(name=n).first():
            db.session.add(FeatureFlag(name=n, enabled=True))
    db.session.add(User(email="admin@example.com",
                        password_hash=generate_password_hash(PW),
                        role="super_admin"))
    # About: one good photograph and one whose file is not there.
    db.session.add(ContentImage(owner_type="about", owner_id=0,
                                filename=REAL, alt_text="A real photograph",
                                caption="Real", sort=0))
    db.session.add(ContentImage(owner_type="about", owner_id=0,
                                filename=GONE,
                                alt_text="Enfield Bangladesh Welfare "
                                         "Association", sort=1))
    post = NewsPost(title="A post", slug="a-post", body="Words.",
                    published_date=date.today(), published=True)
    milestone = Milestone(title="A milestone", year=2026, summary="Words.",
                          published=True, image=GONE)
    db.session.add_all([post, milestone])
    db.session.commit()
    db.session.add(ContentImage(owner_type="news", owner_id=post.id,
                                filename=GONE, alt_text="Also gone", sort=0))
    db.session.commit()

# ---- the primitives ---------------------------------------------------
check("upload_on_disk finds a file that is there", upload_on_disk(REAL))
check("...and does not find one that is not", not upload_on_disk(GONE))
check("...and is not fooled by an empty filename", not upload_on_disk(""))


class Fake:
    def __init__(self, filename):
        self.filename = filename


kept, missing = present_images([Fake(REAL), Fake(GONE), Fake(REAL)])
check("present_images keeps the ones with files",
      [f.filename for f in kept] == [REAL, REAL])
check("...and hands back the ones without, rather than dropping them "
      "silently", [f.filename for f in missing] == [GONE])

# ---- what a VISITOR sees ----------------------------------------------
client = app.test_client()


def main_of(path):
    html = client.get(path).data.decode("utf-8")
    return html.split("<main", 1)[1].split("</main>", 1)[0]


about = main_of("/about")
check("/about still loads", "<figure" in about)
check("A VISITOR NEVER SEES THE BROKEN IMAGE",
      GONE not in about, "the missing filename is in the page")
check("...nor its alt text sitting in an empty panel",
      "Enfield Bangladesh Welfare Association" not in about)
check("...nor the admin notice", "rc-missing" not in about)
check("the good photograph is still there", REAL in about)
check("the layout closed up: one figure, not two",
      about.count("<figure") == 1, str(about.count("<figure")))

news = main_of("/news/a-post")
check("a news post with only a missing image renders no figure at all",
      GONE not in news and "rc-missing" not in news)
journey = main_of("/our-journey")
check("Our Journey drops a milestone's missing image too",
      GONE not in journey and "rc-missing" not in journey)

# ---- what an ADMIN sees -----------------------------------------------
client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})
about = main_of("/about")
check("a signed-in admin IS told about it", "rc-missing" in about)
check("...by name, so they know which file to put back", GONE in about)
check("...but still never as a broken <img>",
      '<img src="/static/uploads/%s"' % GONE not in about)
check("...and the good photograph is unaffected", REAL in about)
check("...and the notice says it is admin-only",
      "Only signed-in admins see this notice" in about)
check("both figures are present for the admin",
      about.count("<figure") == 2, str(about.count("<figure")))

# The public page must go back to being clean the moment they log out.
client.get("/admin/logout")
check("after logging out the page is a visitor's again",
      "rc-missing" not in main_of("/about"))

# ---- the CLI check ----------------------------------------------------
with app.app_context():
    found = dangling_uploads()
names = {(m, f) for m, _c, _i, _l, f in found}
check("check-uploads finds the About one",
      ("ContentImage", GONE) in names, str(names))
check("...and the news one, and the milestone's own image column",
      sum(1 for m, f in names if f == GONE) >= 2
      and ("Milestone", GONE) in names, str(names))
check("...and reports nothing about the file that IS there",
      all(f != REAL for _m, f in names), str(names))
check("...and does not mistake ordinary Block text for a filename",
      not any(m == "Block" for m, _f in names), str(names))

with app.app_context():
    for row in ContentImage.query.filter_by(filename=GONE).all():
        db.session.delete(row)
    Milestone.query.first().image = ""
    db.session.commit()
    check("with the references gone, so is the warning",
          dangling_uploads() == [], str(dangling_uploads()))

# ---- teardown ---------------------------------------------------------
with app.app_context():
    db.session.remove()
    db.engine.dispose()
for suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + suffix):
        os.remove(TEST_DB + suffix)
os.remove(os.path.join(UPLOAD_DIR, REAL))
check("fixtures cleaned up",
      not os.path.isfile(os.path.join(UPLOAD_DIR, REAL))
      and not os.path.isfile(TEST_DB))

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
