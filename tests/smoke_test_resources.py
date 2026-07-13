"""Smoke test for the community resources directory (CLAUDE.md testing rules).

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_resources.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import app, db, User, Resource  # noqa: E402

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

# ---- public route status (empty directory)
r = client.get("/resources")
check("GET /resources -> 200 (empty)", r.status_code == 200, str(r.status_code))
r = client.get("/sitemap.xml")
check("/resources in sitemap", b"/resources" in r.data)

# ---- anonymous access to admin redirects (302)
for path in ("/admin/resources", "/admin/resources/new"):
    r = client.get(path)
    check("anon GET %s -> 302" % path, r.status_code == 302, str(r.status_code))

# ---- login
r = client.post("/admin/login", data={"email": "test@example.com",
                                      "password": "pw123456"})
check("login -> 302", r.status_code == 302, str(r.status_code))
r = client.get("/admin/resources")
check("authed GET /admin/resources -> 200", r.status_code == 200,
      str(r.status_code))

# ---- create round-trip (two categories, out of alphabetical entry order)
r = client.post("/admin/resources/new", data={
    "name": "Citizens Advice Enfield", "category": "Legal & advice",
    "description": "Free advice on benefits, housing and debt.",
    "phone": "0808 278 7834", "url": "https://example.org", "sort": "0"})
check("create resource -> 302", r.status_code == 302, str(r.status_code))
client.post("/admin/resources/new", data={
    "name": "Enfield Council Customer Services", "category": "Council services",
    "description": "General council enquiries.",
    "phone": "020 8379 1000", "url": "", "sort": "0"})
client.post("/admin/resources/new", data={
    "name": "Enfield Law Centre", "category": "Legal & advice",
    "description": "", "phone": "", "url": "", "sort": "1"})
with app.app_context():
    check("3 resources in db", Resource.query.count() == 3,
          str(Resource.query.count()))
    res_id = Resource.query.filter_by(
        name="Citizens Advice Enfield").first().id

# ---- required fields enforced
client.post("/admin/resources/new", data={"name": "No Category Given",
                                          "category": ""})
with app.app_context():
    check("missing category not created",
          Resource.query.filter_by(name="No Category Given").count() == 0)

# ---- public page: grouping, phone and website links
html = client.get("/resources").data.decode("utf-8")
check("resource name shown", "Citizens Advice Enfield" in html)
check("phone visible", "0808 278 7834" in html)
check("tel: link present", 'href="tel:08082787834"' in html)
check("website link present", 'href="https://example.org"' in html)
check("both category headings shown",
      "Legal &amp; advice" in html and "Council services" in html)
check("categories in alphabetical order",
      html.find("Council services") < html.find("Legal &amp; advice"))
legal_pos = html.find("Legal &amp; advice")
check("resources grouped under their category",
      html.find("Enfield Council Customer Services") < legal_pos
      < html.find("Citizens Advice Enfield")
      < html.find("Enfield Law Centre"))

# ---- category suggestions in the admin form
html = client.get("/admin/resources/new").data.decode("utf-8")
check("form suggests existing categories",
      '<option value="Legal &amp; advice">' in html
      and '<option value="Council services">' in html)

# ---- edit round-trip
r = client.post("/admin/resources/%d/edit" % res_id, data={
    "name": "Citizens Advice Enfield", "category": "Legal & advice",
    "description": "Updated description.", "phone": "0800 144 8848",
    "url": "https://example.org", "sort": "2"})
check("edit resource -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    res = db.session.get(Resource, res_id)
    check("edit saved", res.description == "Updated description."
          and res.phone == "0800 144 8848" and res.sort == 2)

# ---- delete round-trip
r = client.post("/admin/resources/%d/delete" % res_id)
check("delete resource -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("resource gone from db", db.session.get(Resource, res_id) is None)
html = client.get("/resources").data.decode("utf-8")
check("deleted resource absent from public page",
      "Citizens Advice Enfield" not in html)

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
