"""Smoke test for the Funding Track Record module (CLAUDE.md testing rules).

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_track_record.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_track_record.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import app, db, User, Block, FundingRecord  # noqa: E402

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
    db.session.add(Block(group="track_record", key="track_record_intro",
                         label="Intro text", kind="text",
                         value="Intro sentence about our funders."))
    db.session.commit()

client = app.test_client()

# ---- public route status + editable intro block
r = client.get("/track-record")
check("GET /track-record -> 200", r.status_code == 200, str(r.status_code))
check("intro block rendered", b"Intro sentence about our funders." in r.data)
r = client.get("/")
check("Track Record nav link on public site", b"/track-record" in r.data)
r = client.get("/sitemap.xml")
check("/track-record in sitemap", b"/track-record" in r.data)

# ---- anonymous access to admin redirects (302)
for path in ("/admin/track-record", "/admin/track-record/new"):
    r = client.get(path)
    check("anon GET %s -> 302" % path, r.status_code == 302, str(r.status_code))

# ---- login
r = client.post("/admin/login", data={"email": "test@example.com",
                                      "password": "pw123456"})
check("login -> 302", r.status_code == 302, str(r.status_code))
r = client.get("/admin/track-record")
check("authed GET /admin/track-record -> 200", r.status_code == 200,
      str(r.status_code))

# ---- create round-trip: disclosed amount
r = client.post("/admin/track-record/new", data={
    "funder_name": "Enfield Council", "project_title": "Elderly Lunch Club",
    "year": "2025", "amount": "3000", "summary": "Weekly lunch club",
    "outcome": "Over 100 elderly residents supported.",
    "funder_url": "https://www.enfield.gov.uk", "sort": "0",
    "published": "on"})
check("create record -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    rec = FundingRecord.query.filter_by(funder_name="Enfield Council").first()
    rec_id = rec.id if rec else None
    check("amount stored in pence", rec and rec.amount_pence == 300000,
          repr(rec.amount_pence if rec else None))

html = client.get("/track-record").data.decode("utf-8")
check("funder shown", "Enfield Council" in html)
check("project shown", "Elderly Lunch Club" in html)
check("amount rendered as £3,000", "£3,000" in html)
check("outcome shown", "Over 100 elderly residents supported." in html)
check("year heading shown", ">2025<" in html)

# ---- create round-trip: undisclosed amount (nullable)
r = client.post("/admin/track-record/new", data={
    "funder_name": "National Lottery", "project_title": "Cricket Project",
    "year": "2023", "summary": "Youth cricket", "outcome": "25 young people.",
    "published": "on"})
check("create record without amount -> 302", r.status_code == 302,
      str(r.status_code))
with app.app_context():
    rec2 = FundingRecord.query.filter_by(funder_name="National Lottery").first()
    check("amount stored as NULL", rec2 and rec2.amount_pence is None)

html = client.get("/track-record").data.decode("utf-8")
check("no-amount record listed", "Cricket Project" in html)
check("no £ shown next to undisclosed amount",
      "National Lottery ·" not in html and "National Lottery Â·" not in html)

# ---- grouped by year descending
check("years grouped newest first", html.find(">2025<") < html.find(">2023<"),
      "2025 at %d, 2023 at %d" % (html.find(">2025<"), html.find(">2023<")))

# ---- validation: bad year / bad amount create nothing
client.post("/admin/track-record/new", data={
    "funder_name": "Bad Year Trust", "project_title": "X", "year": "banana"})
client.post("/admin/track-record/new", data={
    "funder_name": "Bad Amount Trust", "project_title": "X", "year": "2024",
    "amount": "lots"})
with app.app_context():
    check("invalid year rejected",
          FundingRecord.query.filter_by(funder_name="Bad Year Trust").count() == 0)
    check("invalid amount rejected",
          FundingRecord.query.filter_by(funder_name="Bad Amount Trust").count() == 0)

# ---- edit round-trip
r = client.post("/admin/track-record/%d/edit" % rec_id, data={
    "funder_name": "Enfield Council", "project_title": "Elderly Lunch Club",
    "year": "2025", "amount": "3500.50", "summary": "Weekly lunch club",
    "outcome": "Updated outcome text.", "published": "on"})
check("edit record -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    rec = db.session.get(FundingRecord, rec_id)
    check("edit saved", rec.outcome == "Updated outcome text."
          and rec.amount_pence == 350050)
html = client.get("/track-record").data.decode("utf-8")
check("edited amount rendered as £3,500.50", "£3,500.50" in html)

# ---- unpublished record hidden from public page and sitemap unaffected
client.post("/admin/track-record/new", data={
    "funder_name": "Secret Foundation", "project_title": "Draft Grant",
    "year": "2024"})
r = client.get("/track-record")
check("draft absent from /track-record", b"Secret Foundation" not in r.data)
r = client.get("/admin/track-record")
check("draft listed in admin", b"Secret Foundation" in r.data)

# ---- delete round-trip
r = client.post("/admin/track-record/%d/delete" % rec_id)
check("delete record -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("record gone from db", db.session.get(FundingRecord, rec_id) is None)
r = client.get("/track-record")
check("deleted record absent", b"Enfield Council" not in r.data)

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
