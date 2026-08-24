"""Smoke test for admin-managed service cards + contact blocks.

The important one: the seeded cards must render EXACTLY the markup the
hardcoded homepage produced, so converting them to data changes nothing
a visitor can see. The expected HTML below is copied verbatim from the
template as it stood before the change.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_services.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_services.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, Block, DEFAULT_BLOCKS, DEFAULT_SERVICES,  # noqa: E402
                 FEATURES, FeatureFlag, Service, User, seed_services)

app.config["TESTING"] = True

PW = "services-test-password"

# Verbatim from templates/index.html before this change.
HARDCODED_CARDS = """\
      <div class="card"><div class="card-icon">📚</div><h3>Education &amp; schools</h3><p>Weekend Arabic and Bengali schools, supplementary education and cultural activities.</p></div>
      <div class="card"><div class="card-icon">🤝</div><h3>Elderly drop-in</h3><p>Regular recreational and fitness sessions tackling social isolation for older residents.</p></div>
      <div class="card"><div class="card-icon">💼</div><h3>Training &amp; employment</h3><p>Employability, childcare and volunteering courses for women.</p></div>
      <div class="card"><div class="card-icon">⚖️</div><h3>Legal advice &amp; translation</h3><p>Free advice and translation to navigate social services with confidence.</p></div>
      <div class="card"><div class="card-icon">❤️</div><h3>Health &amp; wellbeing</h3><p>Health awareness campaigns, counselling and wellbeing initiatives for all ages.</p></div>
      <div class="card"><div class="card-icon">🛡️</div><h3>Community safety</h3><p>Working with local authorities and the police on legal awareness and crime prevention.</p></div>
"""

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def cards_html(html):
    """The rendered .cards block, with whitespace between tags collapsed
    so indentation differences don't count as a rendering difference."""
    m = re.search(r'<div class="cards">(.*?)</div>\s*</div>\s*</section>',
                  html, re.S)
    if not m:
        return None
    return re.sub(r">\s+<", "><", m.group(1)).strip()


def home():
    return client.get("/").data.decode("utf-8")


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

client = app.test_client()

# ---- seeding is idempotent, exactly like the init-db convention
with app.app_context():
    first = seed_services()
    db.session.commit()
    check("seeding inserts the six default cards",
          first == len(DEFAULT_SERVICES) == 6, str(first))
    again = seed_services()
    db.session.commit()
    check("re-seeding inserts nothing", again == 0, str(again))
    check("still exactly six cards", Service.query.count() == 6,
          str(Service.query.count()))

# ---- THE POINT: seeded cards render identically to the old hardcoded HTML
rendered = cards_html(home())
check("the cards block was found in the page", rendered is not None)
expected = re.sub(r">\s+<", "><", HARDCODED_CARDS).strip()
check("seeded cards render identically to the hardcoded markup",
      rendered == expected,
      "\n  expected: %s\n  got:      %s" % (expected[:200], (rendered or "")[:200]))

# every emoji and every sentence survived the move into the database
html = home()
for icon, title, description in DEFAULT_SERVICES:
    check("card kept its icon (%s)" % title, icon in html)
    check("card kept its description (%s)" % title, description in html)

# ---- ordering follows sort, not insertion
moved_title = "Elderly drop-in"      # no ampersand, so no escaping to mind
with app.app_context():
    moved = Service.query.filter_by(title=moved_title).first()
    was_sort = moved.sort
    moved.sort = 99
    db.session.commit()
html = home()
check("sort order drives the running order",
      html.index(moved_title) > html.index("Community safety"),
      "moved card is still above the last one")
with app.app_context():
    Service.query.filter_by(title=moved_title).first().sort = was_sort
    db.session.commit()
check("restoring the sort puts it back",
      home().index(moved_title) < home().index("Community safety"))

# ---- admin: anonymous access redirects
for path, method in (("/admin/services", "GET"),
                     ("/admin/services/new", "GET"),
                     ("/admin/services/1/edit", "GET"),
                     ("/admin/services/1/toggle", "POST"),
                     ("/admin/services/1/delete", "POST")):
    r = client.open(path, method=method)
    check("anon %s %s -> login redirect" % (method, path),
          r.status_code == 302
          and "/admin/login" in r.headers.get("Location", ""),
          str(r.status_code))

client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})
r = client.get("/admin/services")
check("authed GET /admin/services -> 200", r.status_code == 200,
      str(r.status_code))
check("admin list shows the seeded cards", b"Elderly drop-in" in r.data)
check("admin nav has the section",
      b"/admin/services" in client.get("/admin").data)

# ---- create round-trip
r = client.post("/admin/services/new", data={
    "title": "Youth football", "description": "Saturday sessions at the park.",
    "icon": "⚽", "sort": "10", "published": "on"})
check("create service -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    svc = Service.query.filter_by(title="Youth football").first()
    svc_id = svc.id if svc else None
    check("service stored", svc is not None)
    check("emoji icon stored as typed", svc and svc.icon == "⚽",
          repr(svc.icon if svc else None))
html = home()
check("new card rendered on the homepage",
      '<div class="card-icon">⚽</div><h3>Youth football</h3>' in
      re.sub(r">\s+<", "><", html))

# ---- validation: a card with no title is refused
r = client.post("/admin/services/new", data={"title": "  ", "icon": "🎈"},
                follow_redirects=True)
check("blank title refused", b"Title is required" in r.data)
with app.app_context():
    check("nothing created for the blank title", Service.query.count() == 7,
          str(Service.query.count()))

# ---- edit round-trip
r = client.post("/admin/services/%d/edit" % svc_id, data={
    "title": "Youth football & cricket", "icon": "⚽",
    "description": "Saturday sessions at the park.", "sort": "10",
    "published": "on"})
check("edit service -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("edit saved", Service.query.get(svc_id).title
          == "Youth football & cricket")
check("ampersand escaped in the rendered card",
      "Youth football &amp; cricket" in home())

# ---- hide / publish round-trip
r = client.post("/admin/services/%d/toggle" % svc_id)
check("toggle -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("card is now hidden",
          Service.query.get(svc_id).published is False)
check("hidden card absent from the homepage",
      "Youth football" not in home())
r = client.get("/admin/services")
check("hidden card still listed in admin", b"Youth football" in r.data)
client.post("/admin/services/%d/toggle" % svc_id)
check("republished card is back", "Youth football" in home())

# ---- delete round-trip
r = client.post("/admin/services/%d/delete" % svc_id)
check("delete -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    check("card gone from the db", db.session.get(Service, svc_id) is None)
check("deleted card absent from the homepage",
      "Youth football" not in home())

# ---- with no cards at all, the section disappears cleanly
with app.app_context():
    Service.query.delete()
    db.session.commit()
html = home()
check("empty section is not rendered", "What we do" not in html)
check("homepage still fine with no cards",
      client.get("/").status_code == 200)
with app.app_context():
    seed_services()
    db.session.commit()
check("restored after re-seeding", "Elderly drop-in" in home())

# ---- contact page: every text string comes from an editable block
r = client.get("/contact")
check("GET /contact -> 200", r.status_code == 200, str(r.status_code))
html = r.data.decode("utf-8")
CONTACT_KEYS = ["contact_eyebrow", "contact_heading", "contact_card_title",
                "contact_label_address", "contact_label_phone",
                "contact_label_hours", "contact_intro", "contact_hours"]
defaults = {key: value for _g, key, _l, _k, value in DEFAULT_BLOCKS}
for key in CONTACT_KEYS:
    check("%s is a seeded block" % key, key in defaults)
    check("%s renders its default" % key, defaults[key] in html,
          repr(defaults[key]))

# editing each block changes the page — nothing is left hardcoded
with app.app_context():
    for i, key in enumerate(CONTACT_KEYS):
        Block.query.filter_by(key=key).first().value = "EDITED%d" % i
    db.session.commit()
html = client.get("/contact").data.decode("utf-8")
for i, key in enumerate(CONTACT_KEYS):
    check("editing %s changes the page" % key, "EDITED%d" % i in html)
# Scope to <main>: the shared footer has its own "Get in touch" heading,
# which is chrome on every page and not part of this section.
# Split on the OPENING TAG NAME, not the whole tag: <main> carries an id
# and a tabindex now (the skip link's target), and matching the literal
# "<main>" made this depend on that element having no attributes.
main = html.split("<main", 1)[1].split("</main>", 1)[0]
for _g, key, _l, _k, value in DEFAULT_BLOCKS:
    if key in CONTACT_KEYS and value:
        check("old hardcoded text for %s is gone" % key, value not in main,
              repr(value))

# the admin content editor exposes them all under the contact group
r = client.get("/admin/content?group=contact")
check("contact blocks editable in admin", r.status_code == 200)
with app.app_context():
    ids = [Block.query.filter_by(key=k).first().id for k in CONTACT_KEYS]
check("every contact block has a field in the editor",
      all(("block_%d" % i).encode() in r.data for i in ids))

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
