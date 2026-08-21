"""Smoke test for changed-field lists in audit summaries (CLAUDE.md rules).

An edit records WHICH fields changed and nothing about their contents.
This test checks the list is accurate across every module with an edit
path, that an edit touching nothing says so, and — the part that matters
for data protection — that no submitted value ever reaches the summary.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_audit_changes.py
"""
import base64
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_audit_changes.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, AuditLog, Block, Campaign, Event,  # noqa: E402
                 FEATURES, FeatureFlag, MembershipApplication, Milestone,
                 NewsPost, Resource, Testimonial, User, UPLOAD_DIR)

app.config["TESTING"] = True

# Uploads are decoded and optimised now, so a test upload has to be a
# real image. This is a 1x1 transparent PNG: small enough to need no
# thumbnail and, having an alpha channel, stored byte for byte as .png.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


PW = "audit-changes-password"

# Distinctive values, so any leak into a summary is unmistakable.
V = {
    "title": "Eid Iftar 2026",
    "new_title": "Eid Iftar 2026 (rescheduled)",
    "description": "SECRETDESCRIPTION community meal in the main hall",
    "venue": "SECRETVENUE Ponders End",
    "body": "SECRETBODY the appeal raised a great deal",
    "phone": "020 8804 SECRETPHONE",
    "outcome": "SECRETOUTCOME lasting change",
    "blurb": "SECRETBLURB",
    "block_text": "SECRETBLOCKTEXT hero headline",
}
LEAK_MARKERS = [v for k, v in V.items() if "SECRET" in v]

failures = []
uploaded = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def last_summary(action="edit"):
    with app.app_context():
        e = (AuditLog.query.filter_by(action=action)
             .order_by(AuditLog.id.desc()).first())
        return e.summary if e else ""


def changed_in(summary):
    """The field names listed in a summary, as a set."""
    if "no fields changed" in summary:
        return set()
    if "changed: " not in summary:
        return None
    tail = summary.split("changed: ", 1)[1].rstrip(").")
    return set(n.strip() for n in tail.split(","))


def expect(name, summary, fields):
    got = changed_in(summary)
    check(name, got == set(fields),
          "expected %s, got %s — %r" % (sorted(fields),
                                        sorted(got) if got is not None
                                        else None, summary))


with app.app_context():
    db.create_all()
    for n, _l, _d, default in FEATURES:
        db.session.add(FeatureFlag(name=n, enabled=default))
    u = User(email="admin@example.com")
    u.set_password(PW)
    db.session.add(u)
    db.session.add(Block(group="home", key="home_hero_title",
                         label="Hero headline", kind="text", value="Welcome"))
    db.session.add(Block(group="home", key="home_hero_image",
                         label="Hero photo", kind="image", value=""))
    db.session.add(Testimonial(name="Fatima", quote="Lovely.", published=True))
    m = MembershipApplication(name="Rina Begum", email="rina@example.org",
                              over_18=True, bangladeshi_origin=True,
                              lives_works_enfield=True, fee_confirmed=True)
    db.session.add(m)
    db.session.commit()
    m_id, t_id = m.id, Testimonial.query.first().id

client = app.test_client()
client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})

# ---- events -------------------------------------------------------
client.post("/admin/events/new", data={
    "title": V["title"], "event_date": "2026-09-01",
    "venue": V["venue"], "description": V["description"],
    "start_time": "6:30 PM", "summary": "Community meal",
    "published": "on"})
check("create says created, not changed",
      "Created event" in last_summary("create")
      and "changed" not in last_summary("create"), last_summary("create"))
with app.app_context():
    ev_id = Event.query.first().id

# an identical resubmission changes nothing
client.post("/admin/events/%d/edit" % ev_id, data={
    "title": V["title"], "event_date": "2026-09-01",
    "venue": V["venue"], "description": V["description"],
    "start_time": "6:30 PM", "summary": "Community meal",
    "published": "on"})
check("resubmitting the same values reports no change",
      "no fields changed" in last_summary(), last_summary())
check("no-change wording does not claim an edit happened",
      "changed:" not in last_summary())

# three fields at once — the example from the brief
client.post("/admin/events/%d/edit" % ev_id, data={
    "title": V["new_title"], "event_date": "2026-09-05",
    "venue": V["venue"], "description": "A different SECRETDESCRIPTION plan",
    "start_time": "6:30 PM", "summary": "Community meal",
    "published": "on"})
expect("event: three changed fields listed", last_summary(),
       ["title", "event_date", "description"])
check("summary keeps the record title for context",
      V["new_title"] in last_summary(), last_summary())

# a single field, and a boolean
client.post("/admin/events/%d/edit" % ev_id, data={
    "title": V["new_title"], "event_date": "2026-09-05",
    "venue": "Elsewhere", "description": "A different SECRETDESCRIPTION plan",
    "start_time": "6:30 PM", "summary": "Community meal",
    "published": "on"})
expect("event: single changed field", last_summary(), ["venue"])
client.post("/admin/events/%d/edit" % ev_id, data={
    "title": V["new_title"], "event_date": "2026-09-05",
    "venue": "Elsewhere", "description": "A different SECRETDESCRIPTION plan",
    "start_time": "6:30 PM", "summary": "Community meal"})
expect("event: unticking published is caught", last_summary(), ["published"])

# a replaced image counts as a changed field
client.post("/admin/events/%d/edit" % ev_id, data={
    "title": V["new_title"], "event_date": "2026-09-05",
    "venue": "Elsewhere", "description": "A different SECRETDESCRIPTION plan",
    "start_time": "6:30 PM", "summary": "Community meal",
    "image": (io.BytesIO(TINY_PNG), "photo.png")},
    content_type="multipart/form-data")
expect("event: uploading an image is listed", last_summary(), ["image"])
with app.app_context():
    uploaded.append(Event.query.get(ev_id).image)

# ---- news ---------------------------------------------------------
client.post("/admin/news/new", data={
    "title": "Winter coat appeal", "published_date": "2026-08-01",
    "summary": "Warm coats wanted", "body": V["body"], "published": "on"})
with app.app_context():
    post_id = NewsPost.query.first().id
client.post("/admin/news/%d/edit" % post_id, data={
    "title": "Winter coat appeal", "published_date": "2026-08-02",
    "summary": "Warm coats wanted", "body": V["body"], "published": "on"})
expect("news: date change listed", last_summary(), ["published_date"])
client.post("/admin/news/%d/edit" % post_id, data={
    "title": "Winter coat appeal 2", "published_date": "2026-08-02",
    "summary": "Warm coats wanted", "body": "SECRETBODY rewritten",
    "published": "on"})
expect("news: title and body listed", last_summary(), ["title", "body"])

# ---- resources ----------------------------------------------------
client.post("/admin/resources/new", data={
    "name": "Enfield Foodbank", "category": "Food support",
    "phone": V["phone"], "description": "Parcels", "sort": "0"})
with app.app_context():
    res_id = Resource.query.first().id
client.post("/admin/resources/%d/edit" % res_id, data={
    "name": "Enfield Foodbank", "category": "Food support",
    "phone": V["phone"], "description": "Parcels", "sort": "0"})
check("resource: no-op edit reports no change",
      "no fields changed" in last_summary(), last_summary())
client.post("/admin/resources/%d/edit" % res_id, data={
    "name": "Enfield Foodbank", "category": "Advice",
    "phone": "020 0000 0000", "description": "Parcels", "sort": "3"})
expect("resource: category, phone and sort listed", last_summary(),
       ["category", "phone", "sort"])

# ---- milestones ---------------------------------------------------
client.post("/admin/journey/new", data={
    "title": "Opened the centre", "year": "2021",
    "outcome": V["outcome"], "published": "on", "sort": "0"})
with app.app_context():
    ms_id = Milestone.query.first().id
client.post("/admin/journey/%d/edit" % ms_id, data={
    "title": "Opened the centre", "year": "2022", "amount": "3000",
    "funder_name": "Enfield Council", "outcome": V["outcome"],
    "published": "on", "sort": "0"})
expect("milestone: year, funder and amount listed", last_summary(),
       ["year", "funder_name", "amount_pence"])

# ---- campaigns ----------------------------------------------------
client.post("/admin/campaigns/new", data={
    "title": "Seaside trip", "description": "A day out", "fee": "15",
    "active": "on"})
with app.app_context():
    camp_id = Campaign.query.first().id
client.post("/admin/campaigns/%d/edit" % camp_id, data={
    "title": "Seaside trip", "description": "A day out", "fee": "15",
    "target": "2000", "active": "on"})
expect("campaign: target listed", last_summary(), ["target_pence"])
client.post("/admin/campaigns/%d/edit" % camp_id, data={
    "title": "Seaside trip", "description": "A day out", "fee": "15",
    "target": "2000"})
expect("campaign: deactivating is caught", last_summary(), ["active"])

# ---- blocks (page content) ----------------------------------------
with app.app_context():
    text_block = Block.query.filter_by(key="home_hero_title").first()
    image_block = Block.query.filter_by(key="home_hero_image").first()
    text_id, image_id = text_block.id, image_block.id
client.post("/admin/content?group=home",
            data={"block_%d" % text_id: "Welcome"})
check("blocks: saving unchanged text reports no change",
      "no fields changed" in last_summary(), last_summary())
client.post("/admin/content?group=home",
            data={"block_%d" % text_id: V["block_text"]})
expect("blocks: the changed block key is listed", last_summary(),
       ["home_hero_title"])
client.post("/admin/content?group=home", data={
    "block_%d" % text_id: V["block_text"],
    "block_%d" % image_id: (io.BytesIO(TINY_PNG), "hero.png")},
    content_type="multipart/form-data")
expect("blocks: an uploaded image block is listed", last_summary(),
       ["home_hero_image"])
with app.app_context():
    uploaded.append(Block.query.get(image_id).value)

# ---- membership status --------------------------------------------
client.post("/admin/membership/%d/status" % m_id, data={"status": "approved"})
expect("membership: status change listed",
       last_summary("status_change"), ["status"])
client.post("/admin/membership/%d/status" % m_id, data={"status": "approved"})
check("membership: re-submitting the same status reports no change",
      "no fields changed" in last_summary("status_change"),
      last_summary("status_change"))

# ---- testimonial toggle -------------------------------------------
client.post("/admin/testimonials/%d/toggle" % t_id)
expect("testimonial: toggle lists published",
       last_summary("status_change"), ["published"])

# ---- THE RULE: no submitted value ever reaches a summary -----------
with app.app_context():
    all_summaries = " ".join(e.summary or "" for e in AuditLog.query.all())
for marker in LEAK_MARKERS:
    check("no field value leaked into any summary (%s)" % marker[:14],
          marker not in all_summaries)
check("no free-text description leaked", "SECRET" not in all_summaries,
      all_summaries[:300])
with app.app_context():
    edits = AuditLog.query.filter_by(action="edit").all()
    check("every edit summary carries a changed-field clause",
          all("changed:" in e.summary or "no fields changed" in e.summary
              for e in edits), str([e.summary for e in edits][:3]))
    check("a good number of edits were recorded", len(edits) >= 10,
          str(len(edits)))

# ---- teardown ------------------------------------------------------
for name in uploaded:
    path = os.path.join(UPLOAD_DIR, name) if name else ""
    if path and os.path.isfile(path):
        os.remove(path)
check("uploaded test images cleaned up",
      not any(os.path.isfile(os.path.join(UPLOAD_DIR, n))
              for n in uploaded if n))
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
