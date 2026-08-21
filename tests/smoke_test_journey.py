"""Smoke test for the Milestones & Track Record module (CLAUDE.md rules).

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.
Uploaded test images land in static/uploads and are cleaned up too.

Run:  python tests/smoke_test_journey.py
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_journey.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import app, db, User, Block, Milestone, UPLOAD_DIR  # noqa: E402

app.config["TESTING"] = True

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    db.create_all()
    u = User(email="test@example.com")
    u.set_password("pw123456")
    db.session.add(u)
    db.session.add(Block(group="journey", key="journey_intro",
                         label="Intro text", kind="text",
                         value="Intro sentence about our journey."))
    db.session.commit()

client = app.test_client()

# ---- public route status + editable intro block
r = client.get("/our-journey")
check("GET /our-journey -> 200", r.status_code == 200, str(r.status_code))
check("intro block rendered", b"Intro sentence about our journey." in r.data)
r = client.get("/")
check("Our Journey nav link on public site", b"/our-journey" in r.data)
r = client.get("/sitemap.xml")
check("/our-journey in sitemap", b"/our-journey" in r.data)

# ---- anonymous access to admin redirects (302)
for path in ("/admin/journey", "/admin/journey/new"):
    r = client.get(path)
    check("anon GET %s -> 302" % path, r.status_code == 302, str(r.status_code))

# ---- login
r = client.post("/admin/login", data={"email": "test@example.com",
                                      "password": "pw123456"})
check("login -> 302", r.status_code == 302, str(r.status_code))
r = client.get("/admin/journey")
check("authed GET /admin/journey -> 200", r.status_code == 200,
      str(r.status_code))

# ---- create round-trip: funded milestone with image
r = client.post("/admin/journey/new", data={
    "title": "Elderly Lunch Club launched", "year": "2025",
    "summary": "Weekly lunch club", "outcome": "100+ elderly supported.",
    "funder_name": "Enfield Council", "amount": "3000",
    "funder_url": "https://www.enfield.gov.uk", "sort": "0",
    "published": "on",
    "image": (io.BytesIO(b"fake-png-bytes"), "photo.png")},
    content_type="multipart/form-data")
check("create milestone -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    m = Milestone.query.filter_by(title="Elderly Lunch Club launched").first()
    m_id = m.id if m else None
    m_image = m.image if m else ""
    check("amount stored in pence", m and m.amount_pence == 300000,
          repr(m.amount_pence if m else None))
    check("image stored", bool(m_image), repr(m_image))
image_path = os.path.join(UPLOAD_DIR, m_image) if m_image else ""
check("image file saved to uploads", image_path and os.path.isfile(image_path))

html = client.get("/our-journey").data.decode("utf-8")
check("milestone shown", "Elderly Lunch Club launched" in html)
check("funded-by line with amount",
      "Funded by" in html and "Enfield Council" in html and "£3,000" in html)
check("funder link present", "https://www.enfield.gov.uk" in html)
check("outcome shown", "100+ elderly supported." in html)
check("image rendered", m_image in html)
check("year heading shown", ">2025<" in html)

# ---- create round-trip: plain milestone (no funder, no amount, no image)
r = client.post("/admin/journey/new", data={
    "title": "First community gathering", "year": "2023",
    "summary": "Where it all began.", "published": "on"})
check("create plain milestone -> 302", r.status_code == 302,
      str(r.status_code))
with app.app_context():
    m2 = Milestone.query.filter_by(title="First community gathering").first()
    check("no funder / NULL amount stored",
          m2 and m2.funder_name == "" and m2.amount_pence is None)

html = client.get("/our-journey").data.decode("utf-8")
check("plain milestone listed", "First community gathering" in html)
# Milestones render as timeline entries now, not cards: take the entry
# this title sits in and check the funder line is absent from it.
plain_entry = (html.split("First community gathering")[0]
               .rsplit('class="journey-entry"', 1)[-1])
check("no funded-by line on plain milestone", "Funded by" not in plain_entry)

# ---- grouped by year descending
check("years grouped newest first", html.find(">2025<") < html.find(">2023<"),
      "2025 at %d, 2023 at %d" % (html.find(">2025<"), html.find(">2023<")))

# ---- validation: bad year / bad amount create nothing
client.post("/admin/journey/new", data={"title": "Bad Year", "year": "banana"})
client.post("/admin/journey/new", data={"title": "Bad Amount", "year": "2024",
                                        "amount": "lots"})
with app.app_context():
    check("invalid year rejected",
          Milestone.query.filter_by(title="Bad Year").count() == 0)
    check("invalid amount rejected",
          Milestone.query.filter_by(title="Bad Amount").count() == 0)

# ---- edit round-trip, replacing the image (old file deleted)
r = client.post("/admin/journey/%d/edit" % m_id, data={
    "title": "Elderly Lunch Club launched", "year": "2025",
    "summary": "Weekly lunch club", "outcome": "Updated outcome text.",
    "funder_name": "Enfield Council", "amount": "3500.50", "published": "on",
    "image": (io.BytesIO(b"new-png-bytes"), "photo2.png")},
    content_type="multipart/form-data")
check("edit milestone -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    m = db.session.get(Milestone, m_id)
    new_image = m.image
    check("edit saved", m.outcome == "Updated outcome text."
          and m.amount_pence == 350050)
    check("image replaced", new_image and new_image != m_image, repr(new_image))
check("old image file deleted", not os.path.isfile(image_path))
new_image_path = os.path.join(UPLOAD_DIR, new_image)
check("new image file saved", os.path.isfile(new_image_path))
html = client.get("/our-journey").data.decode("utf-8")
check("edited amount rendered as £3,500.50", "£3,500.50" in html)

# ---- unpublished milestone hidden from public page
client.post("/admin/journey/new", data={"title": "Secret Milestone",
                                        "year": "2024"})
r = client.get("/our-journey")
check("draft absent from /our-journey", b"Secret Milestone" not in r.data)
r = client.get("/admin/journey")
check("draft listed in admin", b"Secret Milestone" in r.data)

# ---- delete round-trip (image file cleaned up too)
r = client.post("/admin/journey/%d/delete" % m_id)
check("delete milestone -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("milestone gone from db", db.session.get(Milestone, m_id) is None)
check("image file deleted with milestone", not os.path.isfile(new_image_path))
r = client.get("/our-journey")
check("deleted milestone absent", b"Elderly Lunch Club launched" not in r.data)

# ---- teardown: delete the throwaway db (incl. WAL sidecars)
with app.app_context():
    db.session.remove()
    db.engine.dispose()
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
