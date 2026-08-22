"""Smoke test for the server health panel (CLAUDE.md rules).

Covers: the panel renders for a super admin and 403s for a client admin
and anonymously; the JSON endpoint enforces the same and is rate limited;
every metric returns something plausible or an honest None — the panel
must survive a machine without psutil, without /proc and without systemd,
which is exactly a Windows development box; and the panel is READ-ONLY:
no form, no button, no link that acts on the server, and the endpoint
takes no parameters at all.

Both paths are exercised: whatever this machine has, and a stand-in
psutil, so the production path is covered from a development box.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_health.py
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_health.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

import app as appmod                                           # noqa: E402
from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,     # noqa: E402
                 FeatureFlag, User, app_version, health_cpu, health_disk,
                 health_memory, health_network, health_services,
                 health_uptime, schema_state, server_health)

app.config["TESTING"] = True

PW = "health-test-password"
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


class FakePsutil:
    """Stands in for psutil, so the production path is covered here too."""

    @staticmethod
    def getloadavg():
        return (2.0, 1.5, 1.1)

    class _Mem:
        total = 8 * 1024 ** 3
        available = 3 * 1024 ** 3
        percent = 62.5

    @staticmethod
    def virtual_memory():
        return FakePsutil._Mem()

    @staticmethod
    def boot_time():
        import time
        return time.time() - 86400 * 3

    class _Net:
        bytes_sent = 1234567890
        bytes_recv = 9876543210

    @staticmethod
    def net_io_counters():
        return FakePsutil._Net()


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

# ---- metrics on THIS machine, whatever it happens to be
with app.app_context():
    health = server_health()

cpu = health["cpu"]
check("cpu reports a core count", isinstance(cpu["cores"], int)
      and cpu["cores"] >= 1, str(cpu["cores"]))
check("load average is three numbers or an honest None",
      cpu["load"] is None or (len(cpu["load"]) == 3
                              and all(isinstance(v, float)
                                      for v in cpu["load"])), str(cpu["load"]))
check("per-core figure accompanies the load",
      (cpu["load"] is None) == (cpu["per_core"] is None), str(cpu))

mem = health["memory"]
check("memory is either complete or honestly empty",
      (mem["total"] and mem["percent"] is not None)
      or (mem["total"] is None and mem["level"] == "unknown"), str(mem))
if mem["total"]:
    check("memory percentage is a percentage", 0 <= mem["percent"] <= 100)
    check("used and available add up to the total",
          abs((mem["used"] + mem["available"]) - mem["total"]) < 1024 ** 2)

disk = health["disk"]
check("disk usage read", disk["total"] and disk["total"] > 0, str(disk))
check("disk percentage is a percentage", 0 <= disk["percent"] <= 100)
check("used and free do not exceed the total",
      disk["used"] + disk["free"] <= disk["total"] * 1.02)
for key in ("database", "uploads", "backups"):
    check("%s size is a number" % key, isinstance(disk[key], int)
          and disk[key] >= 0, str(disk[key]))

up = health["uptime"]
check("the app's own start time is always known",
      up["app_started"] is not None and up["app_seconds"] >= 0)
check("boot time is a datetime or None", up["boot"] is None
      or hasattr(up["boot"], "year"), str(up["boot"]))

check("a service line per unit", len(health["services"]) == 2,
      str(health["services"]))
for svc in health["services"]:
    check("%s reports a state or says why not" % svc["unit"],
          svc["state"] in ("active", "inactive") or svc["note"], str(svc))

net = health["network"]
check("network counters are numbers or None",
      all(v is None or isinstance(v, int) for v in net.values()), str(net))

check("python version reported", re.match(r"^\d+\.\d+\.\d+",
                                          health["python"]) is not None)
check("app version is a short hash or None",
      health["version"] is None or re.match(r"^[0-9a-f]{7}$",
                                            health["version"]),
      str(health["version"]))
check("schema state is reported", health["schema"]["ok"] is True,
      str(health["schema"]))
check("schema counts the tables", health["schema"]["tables"] > 15,
      str(health["schema"]["tables"]))

# ---- the production path, with psutil standing by
real = appmod._psutil
appmod._psutil = lambda: FakePsutil
try:
    with app.app_context():
        withps = server_health()
    check("psutil path: load average read", withps["cpu"]["load"] is not None
          or not hasattr(os, "getloadavg"), str(withps["cpu"]))
    check("psutil path: memory complete",
          withps["memory"]["total"] == 8 * 1024 ** 3
          and withps["memory"]["percent"] == 62.5, str(withps["memory"]))
    check("psutil path: memory level is judged",
          withps["memory"]["level"] == "green", withps["memory"]["level"])
    check("psutil path: network counters read",
          withps["network"]["received"] == 9876543210,
          str(withps["network"]))
    check("psutil path: boot time read",
          withps["uptime"]["boot"] is not None
          and withps["uptime"]["boot_seconds"] > 86000,
          str(withps["uptime"]["boot_seconds"]))

    # thresholds
    FakePsutil._Mem.percent = 85.0
    with app.app_context():
        check("85% memory is amber", health_memory()["level"] == "amber")
    FakePsutil._Mem.percent = 95.0
    with app.app_context():
        check("95% memory is red", health_memory()["level"] == "red")
    FakePsutil._Mem.percent = 62.5
finally:
    appmod._psutil = real

# ---- and with NOTHING available, which is the degradation case
broken = lambda: None                                          # noqa: E731
appmod._psutil = broken
real_open = open
try:
    with app.app_context():
        bare = server_health()
    check("no psutil: still returns every section",
          set(bare) >= {"cpu", "memory", "disk", "uptime", "services",
                        "network", "schema"}, str(sorted(bare)))
    check("no psutil: nothing raised, disk still read",
          bare["disk"]["total"] > 0)
    check("no psutil: the app's uptime is still known",
          bare["uptime"]["app_started"] is not None)
finally:
    appmod._psutil = real

# ---- who may see it
r = client.get("/admin/settings/health.json")
check("anon JSON -> login redirect", r.status_code == 302
      and "/admin/login" in r.headers.get("Location", ""), str(r.status_code))
r = client.get("/admin/features")
check("anon panel -> login redirect", r.status_code == 302)

client.post("/admin/login", data={"email": "client@example.com",
                                  "password": PW})
r = client.get("/admin/settings/health.json")
check("client admin JSON -> 403", r.status_code == 403, str(r.status_code))
r = client.get("/admin/features")
check("client admin panel -> 403", r.status_code == 403, str(r.status_code))
check("client admin sees no server figures anywhere",
      b"Server health" not in client.get("/admin").data)
client.get("/admin/logout")

client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})
page = client.get("/admin/features").data.decode("utf-8")
check("super admin sees the panel", "Server health" in page)
check("panel shows the cards", page.count("health-card") >= 8,
      str(page.count("health-card")))
check("panel names this site's own sizes",
      "Database" in page and "Uploads" in page and "Backups" in page)
check("panel reports the schema", "Schema" in page)
check("panel says no speed test, and why",
      "No speed test" in page)
check("panel offers the 30-second refresh", 'id="healthAuto"' in page)

# ---- READ-ONLY: nothing here acts
panel = page.split("Server health")[1].split("Optional modules")[0]
check("the panel contains no form", "<form" not in panel)
check("the panel contains no button", "<button" not in panel)
check("the panel contains no link at all", "<a " not in panel)
# Words in prose are fine — the panel explains what it deliberately does
# NOT do. What must not exist is a way to act: a handler, a form target,
# or a link that does something.
for marker in ("onclick", "formaction", "method=\"post\"", "action=\"/",
               "systemctl restart", "systemctl stop"):
    check("the panel has no %s" % marker, marker not in panel.lower())

# ---- the JSON endpoint
r = client.get("/admin/settings/health.json")
check("super admin JSON -> 200", r.status_code == 200, str(r.status_code))
data = json.loads(r.data.decode("utf-8"))
check("JSON carries every section",
      set(data) >= {"cpu", "memory", "disk", "uptime", "services",
                    "network", "schema", "checked_at"}, str(sorted(data)))
check("JSON datetimes are strings", isinstance(data["checked_at"], str)
      and isinstance(data["uptime"]["app_started"], str))
check("JSON says which level each card is",
      data["memory"]["level"] in ("green", "amber", "red", "unknown"))
check("JSON contains no secrets",
      "PASSWORD" not in r.data.decode("utf-8").upper()
      or "SMTP_PASSWORD" not in r.data.decode("utf-8"))

# it takes no parameters, so nothing from a request can steer it
r = client.get("/admin/settings/health.json?unit=nginx;rm%20-rf%20/")
check("query parameters change nothing", r.status_code == 200)
after = json.loads(r.data.decode("utf-8"))
check("the same units are reported whatever is asked for",
      [s["unit"] for s in after["services"]]
      == [s["unit"] for s in data["services"]],
      str([s["unit"] for s in after["services"]]))

# ---- rate limited
appmod._rate_buckets.clear()
limit = appmod.RATE_LIMITS["health"][0]
ok_count = 0
for _i in range(limit + 5):
    if client.get("/admin/settings/health.json").status_code == 200:
        ok_count += 1
check("the JSON endpoint is rate limited", ok_count == limit,
      "%d of %d allowed" % (ok_count, limit))
r = client.get("/admin/settings/health.json")
check("and says so in JSON, not HTML", r.status_code == 429
      and b"Too many" in r.data, str(r.status_code))
appmod._rate_buckets.clear()
check("the panel itself still renders when the endpoint is limited",
      client.get("/admin/features").status_code == 200)

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
