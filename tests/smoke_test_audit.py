"""Smoke test for the append-only audit log (CLAUDE.md rules).

Covers: mutations across several modules record entries, a failed login
records the attempted email and no password material, exports record
their parameters, anonymous access redirects, the audit_log flag gates
the page for normal admins but never for super admins and never stops
recording, entries survive the flag being toggled, and a URL-map sweep
asserts no route exists that could edit or delete an entry.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_audit.py
"""
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_audit.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, AuditLog, Campaign, FEATURES,  # noqa: E402
                 FeatureFlag, MembershipApplication, Payment, Subscriber,
                 User, _rate_buckets)

app.config["TESTING"] = True

PW = "audit-test-password"

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def entries(**kw):
    with app.app_context():
        return AuditLog.query.filter_by(**kw).all()


def summaries(action):
    return [e.summary for e in entries(action=action)]


def audit_count():
    with app.app_context():
        return AuditLog.query.count()


def set_flag(name, enabled):
    with app.app_context():
        f = FeatureFlag.query.filter_by(name=name).first()
        f.enabled = enabled
        db.session.commit()


with app.app_context():
    db.create_all()
    for name, _l, _d, default in FEATURES:
        db.session.add(FeatureFlag(name=name, enabled=default))
    boss = User(email="boss@example.com")
    boss.set_password(PW)
    boss.role = "super_admin"
    db.session.add(boss)
    plain = User(email="plain@example.com")
    plain.set_password(PW)
    db.session.add(plain)
    db.session.add(Subscriber(email="friend@example.org"))
    m = MembershipApplication(name="Rina Begum", email="rina@example.org",
                              over_18=True, bangladeshi_origin=True,
                              lives_works_enfield=True, fee_confirmed=True)
    db.session.add(m)
    camp = Campaign()
    camp.title = "Seaside trip"
    camp.slug = "seaside-trip"
    db.session.add(camp)
    db.session.commit()
    camp_id = camp.id
    m_id = m.id
    p = Payment()
    p.campaign_id = camp_id
    p.name = "Donor"
    p.email = "donor@example.org"
    p.fee_pence = 0
    p.donation_pence = 2000
    p.gift_aid = True
    p.gift_aid_name = "A Donor"
    p.gift_aid_address = "12"
    p.gift_aid_postcode = "EN3 4EU"
    p.status = "complete"
    db.session.add(p)
    db.session.commit()

client = app.test_client()

# ---- a failed login is recorded, with the email but nothing else
_rate_buckets.clear()
r = client.post("/admin/login", data={"email": "Boss@example.com",
                                      "password": "not-the-password"})
check("failed login stays on the form", r.status_code == 200)
failed = entries(action="login_failed")
check("failed login recorded", len(failed) == 1, str(len(failed)))
if failed:
    e = failed[0]
    check("attempted email recorded", "boss@example.com" in e.summary,
          e.summary)
    check("failed login has no user_id", e.user_id is None)
    check("failed login attributed to anonymous", e.user_email == "anonymous")
    check("IP recorded", e.ip is not None)
with app.app_context():
    row = " ".join(str(v) for v in vars(AuditLog.query.filter_by(
        action="login_failed").first()).values())
    hashes = [u.password_hash for u in User.query.all()]
check("no password material anywhere in the row",
      "not-the-password" not in row and not any(h in row for h in hashes))

# ---- a successful login and logout are recorded
_rate_buckets.clear()
client.post("/admin/login", data={"email": "boss@example.com",
                                  "password": PW})
check("login recorded", len(entries(action="login")) == 1)
check("login attributed to the user",
      entries(action="login")[0].user_email == "boss@example.com")

# ---- mutations across several modules each record an entry
client.post("/admin/events/new", data={"title": "Eid Community Iftar",
                                       "event_date": "2026-09-01",
                                       "published": "on"})
client.post("/admin/news/new", data={"title": "Winter coat appeal",
                                     "published_date": "2026-08-01",
                                     "published": "on"})
client.post("/admin/resources/new", data={"name": "Enfield Foodbank",
                                          "category": "Food support"})
client.post("/admin/journey/new", data={"title": "Opened the centre",
                                        "year": "2021", "published": "on"})
client.post("/admin/testimonials/new", data={"name": "Fatima", "quote": "Lovely.",
                                         "published": "on"})
client.post("/admin/partners/new", data={"name": "Enfield Council"})
client.post("/admin/content?group=home", data={})
created = summaries("create")
check("event creation recorded",
      any("Eid Community Iftar" in s for s in created), str(created))
check("news creation recorded",
      any("Winter coat appeal" in s for s in created))
check("resource creation recorded",
      any("Enfield Foodbank" in s for s in created))
check("milestone creation recorded",
      any("Opened the centre" in s for s in created))
check("testimonial creation recorded", any("Fatima" in s for s in created))
check("partner creation recorded",
      any("Enfield Council" in s for s in created))
check("block save recorded",
      any("page content" in s for s in summaries("edit")))
with app.app_context():
    ev = AuditLog.query.filter(AuditLog.entity_type == "Event").first()
    check("entity type and id recorded",
          ev is not None and ev.entity_id is not None)

# ---- an edit, a status change and a delete are each recorded distinctly
with app.app_context():
    from app import Event, Testimonial
    ev_id = Event.query.first().id
    t_id = Testimonial.query.first().id
client.post("/admin/events/%d/edit" % ev_id,
            data={"title": "Eid Community Iftar 2026",
                  "event_date": "2026-09-02", "published": "on"})
check("edit recorded separately from create",
      any("Edited event" in s for s in summaries("edit")))
client.post("/admin/testimonials/%d/toggle" % t_id)
check("status change recorded",
      any("is now hidden" in s for s in summaries("status_change")),
      str(summaries("status_change")))
client.post("/admin/events/%d/delete" % ev_id)
deleted = summaries("delete")
check("delete recorded with the title of the gone row",
      any("Eid Community Iftar 2026" in s for s in deleted), str(deleted))
with app.app_context():
    d = AuditLog.query.filter_by(action="delete").first()
    check("delete keeps the entity id of the removed row",
          d.entity_type == "Event" and d.entity_id == ev_id)

# ---- membership status change and deletion (personal data)
client.post("/admin/membership/%d/status" % m_id, data={"status": "approved"})
check("membership status change recorded",
      any("Rina Begum" in s and "approved" in s
          for s in summaries("status_change")))

# ---- exports are recorded with their parameters
client.get("/admin/subscribers.csv")
client.get("/admin/membership.csv")
client.get("/admin/campaigns/%d/contributors.csv" % camp_id)
client.get("/admin/campaigns/%d/contributors" % camp_id)
client.get("/admin/gift-aid?from=2026-01-01&to=2026-12-31")
client.get("/admin/gift-aid.csv?from=2026-01-01&to=2026-12-31")
client.get("/admin/gift-aid/declarations")
exports = summaries("export")
check("subscriber export recorded",
      any("subscriber list" in s for s in exports), str(exports))
check("membership export recorded",
      any("membership applications" in s.lower() for s in exports))
check("contributor CSV export recorded",
      any("contributor list" in s and "CSV" in s for s in exports))
check("printable contributor list recorded",
      any("printable contributor list" in s for s in exports))
check("gift aid claim export records the date range",
      any("01 Jan 2026 to 31 Dec 2026" in s for s in exports), str(exports))
check("gift aid claim export records the totals",
      any("£20" in s and "£5" in s for s in exports), str(exports))
check("gift aid declarations view recorded",
      any("declaration records" in s for s in exports))

# ---- feature toggles are recorded, including the audit_log flag itself
client.post("/admin/features/news/toggle")
client.post("/admin/features/audit_log/toggle")
toggles = summaries("feature_toggle")
check("feature toggle recorded", any("News" in s for s in toggles),
      str(toggles))
check("toggling the audit_log flag is itself recorded",
      any("Audit log" in s for s in toggles), str(toggles))

# ---- recording continues while the audit_log flag is off
set_flag("audit_log", False)
before = audit_count()
client.post("/admin/partners/new", data={"name": "Recorded While Off"})
check("mutations are still recorded with the flag off",
      audit_count() > before)
check("that entry is really there",
      any("Recorded While Off" in s for s in summaries("create")))

# ---- super admin sees the page whether the flag is on or off
r = client.get("/admin/audit")
check("super admin sees the log with the flag off", r.status_code == 200,
      str(r.status_code))
check("super admin keeps the nav link with the flag off",
      b"/admin/audit" in client.get("/admin").data)
set_flag("audit_log", True)
r = client.get("/admin/audit")
check("super admin sees the log with the flag on", r.status_code == 200,
      str(r.status_code))
html = r.data.decode("utf-8")
check("entries listed newest first",
      html.find("Recorded While Off") < html.find("Attempted email"),
      "off at %d, failed login at %d" % (html.find("Recorded While Off"),
                                         html.find("Attempted email")))
check("UK-local timestamps shown", date.today().strftime("%d %b %Y") in html)

# ---- filters narrow the list
r = client.get("/admin/audit?action=login_failed")
html = r.data.decode("utf-8")
check("action filter applied",
      "Attempted email" in html and "Recorded While Off" not in html)
r = client.get("/admin/audit?user=anonymous")
check("user filter applied",
      b"Attempted email" in r.data and b"Recorded While Off" not in r.data)
r = client.get("/admin/audit?from=2000-01-01&to=2000-01-02")
check("date range filter applied", b"Nothing recorded" in r.data)
r = client.get("/admin/audit?page=99999")
check("out-of-range page does not crash", r.status_code == 200,
      str(r.status_code))
r = client.get("/admin/audit?page=banana")
check("bad page value does not crash", r.status_code == 200,
      str(r.status_code))

# ---- entries survive the flag going off and on
total_before = audit_count()
set_flag("audit_log", False)
set_flag("audit_log", True)
check("entries survive the flag being toggled off and on",
      audit_count() == total_before, "%d -> %d" % (total_before, audit_count()))
r = client.get("/admin/audit")
check("the oldest entry is still readable", b"Attempted email" in r.data)
client.get("/admin/logout")
check("logout recorded", len(entries(action="logout")) == 1)

# ---- a normal admin only sees the page when the flag is on
_rate_buckets.clear()
normal = app.test_client()
normal.post("/admin/login", data={"email": "plain@example.com",
                                  "password": PW})
r = normal.get("/admin/audit")
check("normal admin sees the log while the flag is on", r.status_code == 200,
      str(r.status_code))
check("normal admin has the nav link while the flag is on",
      b"/admin/audit" in normal.get("/admin").data)
set_flag("audit_log", False)
r = normal.get("/admin/audit")
check("normal admin gets 403 with the flag off", r.status_code == 403,
      str(r.status_code))
check("normal admin loses the nav link with the flag off",
      b"/admin/audit" not in normal.get("/admin").data)
set_flag("audit_log", True)

# ---- anonymous access redirects to login
anon = app.test_client()
r = anon.get("/admin/audit")
check("anon GET /admin/audit -> login redirect",
      r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
      str(r.status_code))

# ---- URL-map sweep: nothing can edit or delete an audit entry
audit_rules = [rule for rule in app.url_map.iter_rules()
               if "audit" in rule.rule or "audit" in (rule.endpoint or "")]
check("the audit log has exactly one route", len(audit_rules) == 1,
      str([r.rule for r in audit_rules]))
for rule in audit_rules:
    methods = rule.methods - {"HEAD", "OPTIONS"}
    check("%s is read-only (%s)" % (rule.rule, ",".join(sorted(methods))),
          methods == {"GET"}, ",".join(sorted(methods)))
writable = [r.rule for r in app.url_map.iter_rules()
            if ("audit" in r.rule or "audit" in (r.endpoint or ""))
            and ({"POST", "PUT", "PATCH", "DELETE"} & r.methods)]
check("no route writes to the audit log", not writable, str(writable))
check("no delete/edit endpoint name mentions audit",
      not [r.endpoint for r in app.url_map.iter_rules()
           if "audit" in (r.endpoint or "")
           and ("delete" in r.endpoint or "edit" in r.endpoint)])

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
