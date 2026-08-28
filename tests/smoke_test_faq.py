"""Smoke test for the FAQ module (CLAUDE.md rules).

Covers: the public page groups by category with ungrouped questions
first; unpublished questions are hidden from the page AND from the
structured data; multi-paragraph answers render as paragraphs; the
accordions are plain <details> shipped OPEN so the page still reads with
no JavaScript; the FAQPage JSON-LD is valid JSON and matches what is on
the page; admin CRUD round-trips with audit entries; the feature flag
404s the page and drops every link to it; and anonymous access to the
admin redirects.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_faq.py
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_faq.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, AuditLog, Block, DEFAULT_BLOCKS, Faq,   # noqa: E402
                 FEATURES, FEATURE_DEFAULTS, FeatureFlag, User)

app.config["TESTING"] = True

PW = "faq-test-password"
TWO_PARA = ("Yes — the drop-in is open to everyone in Enfield.\n"
            "Membership helps us fund it, but nobody is turned away.")
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



def get(path="/faq"):
    return client.get(path).data.decode("utf-8")


def set_flag(name, enabled):
    with app.app_context():
        FeatureFlag.query.filter_by(name=name).first().enabled = enabled
        db.session.commit()


def ld_json(html):
    """The FAQPage block, parsed."""
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>',
                  html, re.S)
    return json.loads(m.group(1)) if m else None


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

# ---- the flag exists and defaults on
check("faq is a feature flag", "faq" in FEATURE_DEFAULTS)
check("and defaults to on", FEATURE_DEFAULTS["faq"] is True)

# ---- empty state renders rather than 500ing
check("empty FAQ page -> 200", client.get("/faq").status_code == 200)
check("empty page says so", "being written up" in get())
check("no structured data with nothing to say", ld_json(get()) is None)

# ---- anonymous admin access refused
for path, method in (("/admin/faq", "GET"), ("/admin/faq/new", "GET"),
                     ("/admin/faq/1/delete", "POST")):
    r = client.open(path, method=method)
    check("anon %s %s -> login redirect" % (method, path),
          r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
          str(r.status_code))

client.post("/admin/login", data={"email": "admin@example.com",
                                  "password": PW})

# ---- create round-trips
r = client.post("/admin/faq/new", data={
    "question": "Do I have to be a member to come to the drop-in?",
    "answer": TWO_PARA, "category": "Membership", "sort": "0",
    "published": "on"})
check("create -> 302", r.status_code == 302, str(r.status_code))
with app.app_context():
    row = Faq.query.filter_by(category="Membership").first()
    check("question stored", row is not None and row.published is True)
    check("answer stored whole", row.answer == TWO_PARA)
    first_id = row.id

for data in (
    {"question": "Where are you?", "answer": "180 High Street, Ponders End.",
     "category": "Visiting", "sort": "0", "published": "on"},
    {"question": "How much is membership?",
     "answer": "It is a small annual fee — call us for this year's figure.",
     "category": "Membership", "sort": "-5", "published": "on"},
    {"question": "Can I volunteer?", "answer": "Yes, please do.",
     "category": "", "sort": "0", "published": "on"},
    {"question": "Is this one ready yet?", "answer": "No, still a draft.",
     "category": "Membership", "sort": "0"},        # no published tick
):
    client.post("/admin/faq/new", data=data)

html = get()
check("published questions shown",
      all(q in html for q in ("Do I have to be a member",
                              "How much is membership?", "Where are you?",
                              "Can I volunteer?")))
check("UNPUBLISHED question hidden", "Is this one ready yet?" not in html)
check("and its answer with it", "still a draft" not in html)

# ---- grouping and ordering
check("category headings shown",
      "Membership" in html and "Visiting" in html)
check("ungrouped question comes first",
      html.index("Can I volunteer?") < html.index(">Membership<"),
      "ungrouped at %d, Membership at %d"
      % (html.index("Can I volunteer?"), html.index(">Membership<")))
check("sort orders within a category",
      html.index("How much is membership?")
      < html.index("Do I have to be a member"))
check("multi-paragraph answer split into paragraphs",
      "<p>Yes — the drop-in is open to everyone in Enfield.</p>" in html
      and "<p>Membership helps us fund it, but nobody is turned away.</p>"
      in html)

# ---- accordions: plain details/summary, shipped open
check("uses details/summary", "<details" in html and "<summary>" in html)
check("no accordion library",
      not offsite_scripts(html) and "cdn" not in html.lower(),
      str(offsite_scripts(html)))
check("every question ships open, so no-JS readers see the answers",
      html.count("<details class=\"faq-item\" open>") == html.count("<details"),
      "%d open of %d" % (html.count("<details class=\"faq-item\" open>"),
                         html.count("<details")))
check("script collapses them once it runs", "removeAttribute('open')" in html)
check("script is house style", "var items" in html and "const " not in html)

# ---- structured data
data = ld_json(html)
check("FAQPage JSON-LD present and valid JSON", data is not None)
check("typed as FAQPage", data["@type"] == "FAQPage")
check("one entry per published question", len(data["mainEntity"]) == 4,
      str(len(data["mainEntity"])))
check("entries are Questions with accepted answers",
      all(e["@type"] == "Question"
          and e["acceptedAnswer"]["@type"] == "Answer"
          for e in data["mainEntity"]))
questions = [e["name"] for e in data["mainEntity"]]
check("structured data matches the page",
      "Where are you?" in questions and "Can I volunteer?" in questions)
check("UNPUBLISHED question absent from structured data",
      "Is this one ready yet?" not in questions, str(questions))
answer = [e for e in data["mainEntity"]
          if e["name"].startswith("Do I have")][0]["acceptedAnswer"]["text"]
check("both paragraphs reach the structured data",
      "everyone in Enfield" in answer and "nobody is turned away" in answer)

# ---- links in the chrome and the sitemap
check("nav link shown", '/faq"' in html)
check("footer link shown", html.count('href="/faq"') >= 2,
      str(html.count('href="/faq"')))
check("in the sitemap", "/faq" in get("/sitemap.xml"))

# ---- edit and delete
r = client.post("/admin/faq/%d/edit" % first_id, data={
    "question": "Do I have to be a member to visit?", "answer": TWO_PARA,
    "category": "Membership", "sort": "3", "published": "on"},
    follow_redirects=True)
check("edit saved", b"Question saved." in r.data)
with app.app_context():
    row = db.session.get(Faq, first_id)
    check("edit stored", row.question == "Do I have to be a member to visit?"
          and row.sort == 3)
check("edit shows on the page", "Do I have to be a member to visit?" in get())

r = client.get("/admin/faq")
check("admin list shows every question, drafts included",
      b"Is this one ready yet?" in r.data and b"Draft" in r.data)
r = client.get("/admin/faq/%d/edit" % first_id)
check("edit form loads", r.status_code == 200
      and b"Do I have to be a member to visit?" in r.data)
r = client.post("/admin/faq/new", data={"question": "", "answer": ""},
                follow_redirects=True)
check("empty question refused", b"are both required" in r.data)

r = client.post("/admin/faq/%d/delete" % first_id, follow_redirects=True)
check("delete works", b"Question deleted." in r.data)
with app.app_context():
    check("row gone", db.session.get(Faq, first_id) is None)
check("gone from the page", "Do I have to be a member to visit?" not in get())

# ---- every change is in the audit log, none of it with answer text
with app.app_context():
    entries = [e for e in AuditLog.query.all() if e.entity_type == "Faq"
               or "question" in (e.summary or "")]
    check("create, edit and delete all logged",
          {"create", "edit", "delete"} <= {e.action for e in entries},
          str(sorted({e.action for e in entries})))
    check("summaries name the question, never the answer text",
          not any("nobody is turned away" in (e.summary or "")
                  for e in entries))

# ---- the flag hides the module entirely
set_flag("faq", False)
r = client.get("/faq")
check("flag off: /faq -> 404", r.status_code == 404, str(r.status_code))
home = get("/")
check("flag off: nav link gone", 'href="/faq"' not in home)
check("flag off: absent from the sitemap", "/faq" not in get("/sitemap.xml"))
r = client.get("/admin/faq")
check("flag off: admin page still reachable, so content is not stranded",
      r.status_code == 200, str(r.status_code))
with app.app_context():
    check("flag off: nothing deleted", Faq.query.count() == 4)
set_flag("faq", True)
check("flag back on: page returns", client.get("/faq").status_code == 200)
check("flag back on: questions are all still there",
      "Can I volunteer?" in get())

# ---- teardown
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
