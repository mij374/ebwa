"""Every flag-gated admin ROUTE enforces its flag (CLAUDE.md rules).

Hiding a menu link is not access control. This walks the URL map rather
than the templates, and for each feature flag switched off it asserts, as
a client admin:

  * every route in ADMIN_FLAG_GATES returns 403 — the audit log today;
  * every OTHER admin route stays reachable, because switching a module
    off must never strand its content (that is the documented design, so
    it is tested as deliberate rather than left to chance);
  * super admins keep access to the gated routes either way.

It also covers the way this went wrong: a flag with NO row at all. The
general fallback is "use the FEATURES default", which is right for a
module that must work before init-db and wrong for a gate that decides
who may read something — so a gated route with a missing row must deny.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_admin_flag_gates.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_gates.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, ADMIN_FLAG_GATES, Block, DEFAULT_BLOCKS,  # noqa: E402
                 FEATURES, FEATURE_DEFAULTS, FeatureFlag, User,
                 can_read_audit, flag_explicitly_on)

app.config["TESTING"] = True

PW = "gates-test-password"
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def set_flag(name, enabled):
    with app.app_context():
        row = FeatureFlag.query.filter_by(name=name).first()
        if row:
            row.enabled = enabled
        else:
            db.session.add(FeatureFlag(name=name, enabled=enabled))
        db.session.commit()


def drop_flag(name):
    """Remove the row entirely — a database that predates the flag."""
    with app.app_context():
        FeatureFlag.query.filter_by(name=name).delete()
        db.session.commit()


def admin_get_routes():
    """Every GET-able /admin page, as URLs, from the URL MAP itself.

    Reading the map rather than a list means a page added later is
    covered without anybody remembering to add it here.
    """
    out = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/admin") or "GET" not in rule.methods:
            continue
        if rule.arguments:            # needs an id; covered by its module
            continue
        # The sign-in flow is not a content page: those redirect when
        # you are already signed in, which says nothing about flags.
        if rule.endpoint in ("admin_login", "admin_login_2fa",
                             "admin_logout"):
            continue
        out.append((rule.endpoint, rule.rule))
    return sorted(set(out))


with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        db.session.add(Block(group=group, key=key, label=label, kind=kind,
                             value=value))
    for n, _l, _d, default in FEATURES:
        db.session.add(FeatureFlag(name=n, enabled=default))
    boss = User(email="netbus@example.com")
    boss.set_password(PW)
    boss.role = "super_admin"
    db.session.add(boss)
    hand = User(email="client@example.com")
    hand.set_password(PW)
    db.session.add(hand)
    db.session.commit()

client = app.test_client()
ROUTES = admin_get_routes()
check("the URL map yielded admin pages", len(ROUTES) > 10, str(len(ROUTES)))
known = {endpoint for endpoint, _rule in ROUTES}
check("the registry names only known endpoints",
      set(ADMIN_FLAG_GATES) <= known,
      str(set(ADMIN_FLAG_GATES) - known))
check("every gate names a real flag",
      all(flag in FEATURE_DEFAULTS for flag in ADMIN_FLAG_GATES.values()),
      str(ADMIN_FLAG_GATES))

client.post("/admin/login", data={"email": "client@example.com",
                                  "password": PW})

# ---- with every flag ON, a client admin can reach the ordinary pages
for endpoint, rule in ROUTES:
    if endpoint in ("admin_features", "admin_users"):
        continue                      # super-admin only, by design
    status = client.get(rule).status_code
    check("flags on: %s reachable" % rule, status == 200, str(status))

# ---- one flag at a time, off
for flag in sorted(FEATURE_DEFAULTS):
    set_flag(flag, False)
    gated = [e for e, f in ADMIN_FLAG_GATES.items() if f == flag]
    for endpoint, rule in ROUTES:
        if endpoint in ("admin_features", "admin_users"):
            continue
        status = client.get(rule).status_code
        if endpoint in gated:
            check("%s off: %s -> 403 for a client admin" % (flag, rule),
                  status == 403, str(status))
        else:
            # Not stranded: the module's admin page still opens so its
            # content can be reached and edited.
            check("%s off: %s still reachable" % (flag, rule),
                  status == 200, str(status))
    set_flag(flag, True)

# ---- everything off at once, which is how a site gets misread
for flag in FEATURE_DEFAULTS:
    set_flag(flag, False)
for endpoint, rule in ROUTES:
    if endpoint in ("admin_features", "admin_users"):
        continue
    status = client.get(rule).status_code
    expected = 403 if endpoint in ADMIN_FLAG_GATES else 200
    check("all flags off: %s -> %d" % (rule, expected), status == expected,
          str(status))

# ---- the gated pages are hidden from the menu as well as refused
dash = client.get("/admin").data.decode("utf-8")
for endpoint, rule in ROUTES:
    if endpoint in ADMIN_FLAG_GATES:
        check("all flags off: no menu link to %s" % rule,
              'href="%s"' % rule not in dash)

# ---- a super admin keeps every gated page
client.get("/admin/logout")
client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})
for endpoint, rule in ROUTES:
    if endpoint in ADMIN_FLAG_GATES:
        check("all flags off: super admin still reaches %s" % rule,
              client.get(rule).status_code == 200)
dash = client.get("/admin").data.decode("utf-8")
for endpoint, rule in ROUTES:
    if endpoint in ADMIN_FLAG_GATES:
        check("all flags off: super admin still sees the link to %s" % rule,
              'href="%s"' % rule in dash)
client.get("/admin/logout")
for flag in FEATURE_DEFAULTS:
    set_flag(flag, True)

# ---- THE BUG: a flag with no row at all must not open a gate
client.post("/admin/login", data={"email": "client@example.com",
                                  "password": PW})
for endpoint, rule in ROUTES:
    if endpoint not in ADMIN_FLAG_GATES:
        continue
    flag = ADMIN_FLAG_GATES[endpoint]
    drop_flag(flag)
    with app.app_context():
        check("no row for %s: not treated as explicitly on" % flag,
              flag_explicitly_on(flag) is False)
    status = client.get(rule).status_code
    check("no row for %s: %s -> 403, not open by default" % (flag, rule),
          status == 403, str(status))
    dash = client.get("/admin").data.decode("utf-8")
    check("no row for %s: link hidden too" % flag,
          'href="%s"' % rule not in dash)
    set_flag(flag, True)
    check("row restored: %s reachable again" % rule,
          client.get(rule).status_code == 200)

# ---- and the helper agrees with the route, for both roles
with app.app_context():
    drop_flag("audit_log")
client.get("/admin/logout")
client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})
r = client.get("/admin/audit")
check("no row: a super admin is unaffected", r.status_code == 200,
      str(r.status_code))
client.get("/admin/logout")
r = client.get("/admin/audit")
check("anonymous still just gets the login page",
      r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
      str(r.status_code))

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
