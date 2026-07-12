"""Smoke test for the News & Projects module (CLAUDE.md testing rules).

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_news.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import app, db, User, Event, NewsPost  # noqa: E402

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
    db.session.commit()

client = app.test_client()

# ---- public route statuses
for path in ("/", "/about", "/events", "/news", "/gallery", "/contact",
             "/sitemap.xml", "/robots.txt"):
    r = client.get(path)
    check("GET %s -> 200" % path, r.status_code == 200, str(r.status_code))

# ---- anonymous access to admin redirects (302)
for path in ("/admin", "/admin/news", "/admin/news/new", "/admin/events"):
    r = client.get(path)
    check("anon GET %s -> 302" % path, r.status_code == 302, str(r.status_code))

# ---- login
r = client.post("/admin/login", data={"email": "test@example.com",
                                      "password": "pw123456"})
check("login -> 302", r.status_code == 302, str(r.status_code))
r = client.get("/admin/news")
check("authed GET /admin/news -> 200", r.status_code == 200, str(r.status_code))

# ---- create round-trip
r = client.post("/admin/news/new", data={
    "title": "Cricket Project Update", "published_date": "2026-07-01",
    "summary": "A short summary line", "body": "Para one.\n\nPara two.",
    "published": "on"})
check("create post -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    p = NewsPost.query.filter_by(title="Cricket Project Update").first()
    post_id = p.id if p else None
    post_slug = p.slug if p else None
check("post created with slug", post_slug == "cricket-project-update",
      repr(post_slug))

r = client.get("/news")
check("published post listed on /news", b"Cricket Project Update" in r.data)
r = client.get("/news/cricket-project-update")
check("detail page -> 200", r.status_code == 200, str(r.status_code))
check("body split into 2 paragraphs", r.data.count(b"<p>Para") == 2,
      str(r.data.count(b"<p>Para")))

# ---- edit round-trip
r = client.post("/admin/news/%d/edit" % post_id, data={
    "title": "Cricket Project Update", "published_date": "2026-07-02",
    "summary": "Updated summary", "body": "Changed body.", "published": "on"})
check("edit post -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    p = db.session.get(NewsPost, post_id)
    check("edit saved", p.summary == "Updated summary"
          and p.published_date.isoformat() == "2026-07-02")
    check("slug stable on edit", p.slug == "cricket-project-update", p.slug)

# ---- draft hidden publicly, 404 by slug
client.post("/admin/news/new", data={
    "title": "Secret Draft", "published_date": "2026-07-03", "body": "hidden"})
r = client.get("/news")
check("draft absent from /news", b"Secret Draft" not in r.data)
r = client.get("/news/secret-draft")
check("draft detail -> 404", r.status_code == 404, str(r.status_code))
r = client.get("/")
check("draft absent from homepage", b"Secret Draft" not in r.data)
r = client.get("/sitemap.xml")
check("draft absent from sitemap", b"secret-draft" not in r.data)
check("published post in sitemap", b"/news/cricket-project-update" in r.data)

# ---- homepage cap of 3 (newest first)
for i, d in enumerate(("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04")):
    client.post("/admin/news/new", data={
        "title": "Homecap Post %d" % i, "published_date": d,
        "body": "b", "published": "on"})
html = client.get("/").data.decode("utf-8")
check("homepage shows the 3 newest posts",
      "Cricket Project Update" in html and "Homecap Post 3" in html
      and "Homecap Post 2" in html)
check("older posts absent from homepage",
      "Homecap Post 1" not in html and "Homecap Post 0" not in html)
check("homepage has exactly 3 news detail links", html.count("/news/") == 3,
      str(html.count("/news/")))
check("all 5 published posts on /news",
      client.get("/news").data.decode("utf-8").count("/news/") == 5)

# ---- delete round-trip
r = client.post("/admin/news/%d/delete" % post_id)
check("delete post -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("post gone from db", db.session.get(NewsPost, post_id) is None)
r = client.get("/news/cricket-project-update")
check("deleted post detail -> 404", r.status_code == 404, str(r.status_code))

# ---- event slug generation still works after the unique_slug refactor
client.post("/admin/events/new", data={
    "title": "Eid Party", "event_date": "2026-08-01", "published": "on"})
client.post("/admin/events/new", data={
    "title": "Eid Party", "event_date": "2026-08-02", "published": "on"})
with app.app_context():
    slugs = [e.slug for e in Event.query.order_by(Event.id).all()]
    ev_id = Event.query.order_by(Event.id).first().id
check("duplicate event titles -> suffixed slug",
      slugs == ["eid-party", "eid-party-2"], repr(slugs))
client.post("/admin/events/%d/edit" % ev_id, data={
    "title": "Eid Party", "event_date": "2026-08-01", "published": "on"})
with app.app_context():
    check("event slug stable on edit",
          db.session.get(Event, ev_id).slug == "eid-party")
r = client.get("/events/eid-party")
check("event detail -> 200", r.status_code == 200, str(r.status_code))

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
