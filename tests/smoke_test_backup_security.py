"""Smoke test for backups and security visibility (CLAUDE.md rules).

Covers: backup-now writes a RESTORABLE archive — the test opens the zip,
reads the database out of it and queries it — and records a BackupRun;
retention prunes to the newest N and re-running is safe; the archive
carries every upload; a failure is recorded rather than swallowed; the
Settings panel is super-admin only and shows a client admin no paths at
all; the failed-sign-in count appears on the dashboard only above the
threshold; and the alert email fires only above ITS threshold, respects
its cooldown, and contains the addresses tried and the IP but nothing
resembling a password; and that alerts go to the SECURITY address when
one is set, fall back to the enquiries address when it is not, accept
several addresses, refuse rubbish, and never reach anyone but a super
admin.

Runs against a throwaway SQLite db, uploads folder and backup folder, so
nothing real is touched. Deletes all three afterwards.

Run:  python tests/smoke_test_backup_security.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_backup.db")
SANDBOX = tempfile.mkdtemp(prefix="ebwa-backup-test-")
UPLOADS = os.path.join(SANDBOX, "uploads")
ARCHIVES = os.path.join(SANDBOX, "archives")
os.makedirs(UPLOADS)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
os.environ["BACKUP_DIR"] = ARCHIVES
os.environ["BACKUP_KEEP"] = "3"
sys.path.insert(0, os.path.dirname(HERE))

import app as appmod                                           # noqa: E402
from app import (app, db, ALERT_IP_THRESHOLD, AuditLog, Block,  # noqa: E402
                 BackupRun, ContactMessage, DEFAULT_BLOCKS, FEATURES,
                 FAILED_LOGIN_NOTICE, FeatureFlag, SECURITY_ALERT_KEY,
                 SECURITY_ALERT_TO_KEY, User, backup_status,
                 note_failed_login, prune_backups, run_backup,
                 security_alert_setting, security_alert_to,
                 security_alerts_on)

app.config["TESTING"] = True
appmod.UPLOAD_DIR = UPLOADS

PW = "backup-test-password"
SECRET_PASSWORD = "hunter2-never-log-me"
failures = []
sent = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        sent.append(message)


class FakeSMTPModule:
    SMTP = FakeSMTP
    SMTP_SSL = FakeSMTP
    import smtplib as _real
    SMTPAuthenticationError = _real.SMTPAuthenticationError
    SMTPSenderRefused = _real.SMTPSenderRefused
    SMTPRecipientsRefused = _real.SMTPRecipientsRefused
    SMTPNotSupportedError = _real.SMTPNotSupportedError


appmod.smtplib = FakeSMTPModule
os.environ.update({"SMTP_HOST": "smtp.example.org",
                   "SMTP_PASSWORD": SECRET_PASSWORD,
                   "MAIL_FROM": "website@example.org",
                   "MAIL_TO": "netbus@example.org"})


def archives():
    return sorted(f for f in os.listdir(ARCHIVES) if f.endswith(".zip"))


def set_alerts(on):
    with app.app_context():
        block = Block.query.filter_by(key=SECURITY_ALERT_KEY).first()
        block.value = "1" if on else ""
        db.session.commit()


def add_failures(count, ip="203.0.113.9", email="target@example.org",
                 minutes_ago=0):
    """Failed-login audit rows, as the login route would write them."""
    with app.app_context():
        for _i in range(count):
            db.session.add(AuditLog(
                action="login_failed", user_email=email, ip=ip,
                summary="Attempted email: %s" % email,
                created_at=datetime.utcnow()
                - timedelta(minutes=minutes_ago)))
        db.session.commit()


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
    # something identifiable to find again inside the archive
    db.session.add(ContactMessage(name="Ayesha Rahman",
                                  email="ayesha@example.com",
                                  message="Findable in the backup.",
                                  status="new"))
    db.session.commit()

for i in range(4):
    with open(os.path.join(UPLOADS, "photo%d.jpg" % i), "wb") as fh:
        fh.write(b"not-really-a-jpeg-%d" % i)

client = app.test_client()

# ---- a backup that can actually be restored
with app.app_context():
    run = run_backup(reason="cli")
    check("backup reports success", run.status == "ok", run.error or "")
    check("BackupRun recorded", run.id is not None and run.filename)
    check("run has a finish time", run.finished_at is not None)
    check("size recorded", run.size_bytes > 0, str(run.size_bytes))
    check("file count covers the db and every upload",
          run.file_count == 5, str(run.file_count))
    archive_name = run.filename

check("archive on disk", archive_name in archives(), str(archives()))
path = os.path.join(ARCHIVES, archive_name)
with zipfile.ZipFile(path) as zf:
    names = zf.namelist()
    check("archive holds the database", "database/ebwa.db" in names)
    check("archive holds the uploads",
          sum(1 for n in names if n.startswith("uploads/")) == 4, str(names))
    check("archive explains itself", "README.txt" in names)
    readme = zf.read("README.txt").decode("utf-8")
    check("README says an on-server copy is not a backup",
          "SOMEWHERE ELSE" in readme)
    restored = os.path.join(SANDBOX, "restored.db")
    with open(restored, "wb") as fh:
        fh.write(zf.read("database/ebwa.db"))

conn = sqlite3.connect(restored)
rows = conn.execute("SELECT name, message FROM contact_message").fetchall()
conn.close()
check("THE RESTORED DATABASE OPENS AND HOLDS THE DATA",
      rows and rows[0][0] == "Ayesha Rahman", str(rows))

# ---- running it again is safe, and history accumulates
with app.app_context():
    before_runs = BackupRun.query.count()
    run2 = run_backup(reason="cli")
    check("second run also succeeds", run2.status == "ok", run2.error or "")
    check("history keeps both", BackupRun.query.count() == before_runs + 1)
check("two archives now", len(archives()) == 2, str(archives()))

# ---- retention keeps the newest N
for i in range(4):
    stamp = "ebwa-backup-2020010%d-000000.zip" % (i + 1)
    with zipfile.ZipFile(os.path.join(ARCHIVES, stamp), "w") as zf:
        zf.writestr("README.txt", "old")
check("six archives before pruning", len(archives()) == 6, str(archives()))
with app.app_context():
    removed = prune_backups()          # BACKUP_KEEP=3 from the environment
check("pruning removed the extras", removed == 3, str(removed))
kept = archives()
check("exactly three kept", len(kept) == 3, str(kept))
# Six archives, keep three: both of today's and the newest of the old
# ones. Sorting by name is sorting by timestamp, which is the point of
# the naming.
check("and they are the NEWEST three",
      kept == sorted(archives() + [])[-3:]
      and "ebwa-backup-20200104-000000.zip" in kept
      and "ebwa-backup-20200101-000000.zip" not in kept, str(kept))
with app.app_context():
    check("pruning again does nothing", prune_backups() == 0)
    check("pruning never empties the folder", len(archives()) == 3)

# ---- a failure is recorded, not swallowed
with app.app_context():
    real_dir = appmod.BACKUP_DIR
    appmod.BACKUP_DIR = os.path.join(SANDBOX, "nope", "\0bad")
    bad = run_backup(reason="manual")
    appmod.BACKUP_DIR = real_dir
    check("a failed backup is marked failed", bad.status == "failed")
    check("and says why", bool(bad.error), bad.error)
    check("and still finished", bad.finished_at is not None)

# ---- the panel: super admin only, no paths for anyone else
client.post("/admin/login", data={"email": "client@example.com",
                                  "password": PW})
r = client.get("/admin/features")
check("client admin cannot see Settings at all", r.status_code == 403,
      str(r.status_code))
body = r.data.decode("utf-8")
check("client admin is shown NO backup directory", ARCHIVES not in body)
check("client admin is shown no upload path", UPLOADS not in body)
for path, method in (("/admin/settings/backup", "POST"),
                     ("/admin/settings/security-alerts", "POST")):
    r = client.open(path, method=method)
    check("client admin refused %s" % path, r.status_code == 403,
          str(r.status_code))
with app.app_context():
    check("no backup was run by that", BackupRun.query.filter_by(
        reason="manual").count() == 1)          # only the failure above
client.get("/admin/logout")

client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})
r = client.get("/admin/features")
page = r.data.decode("utf-8")
check("super admin sees the panel", r.status_code == 200
      and "Back up now" in page)
check("panel shows the directory", ARCHIVES in page)
check("panel shows retention", "keeping the newest 3" in page)
check("panel shows the last backup size", "Last backup" in page)
check("panel shows disk free", "Free disk space" in page)
check("panel says an on-server archive is not a backup",
      "not a backup" in page)
check("panel says this page cannot configure the copy off the box",
      "cannot configure that" in page)
check("panel never shows the SMTP password", SECRET_PASSWORD not in page)

# ---- the button works, is logged, and is rate limited
appmod._rate_buckets.clear()
r = client.post("/admin/settings/backup", follow_redirects=True)
check("backup from the page succeeds", b"Backup written" in r.data)
with app.app_context():
    entry = (AuditLog.query.filter_by(action="backup")
             .order_by(AuditLog.id.desc()).first())
    check("backup logged", entry is not None
          and "Settings page" in entry.summary,
          entry.summary if entry else "none")
accepted = 1
for _i in range(3):
    r = client.post("/admin/settings/backup", follow_redirects=True)
    if b"Backup written" in r.data:
        accepted += 1
check("the button is rate limited", accepted == 2, "%d accepted" % accepted)
check("and says why", b"limited to twice an hour" in r.data)
appmod._rate_buckets.clear()

# ---- failed sign-ins on the dashboard, only above the threshold
with app.app_context():
    AuditLog.query.filter_by(action="login_failed").delete()
    db.session.commit()
dash = client.get("/admin").data.decode("utf-8")
check("no failed-login item when there are none",
      "failed sign-in attempts" not in dash)
add_failures(FAILED_LOGIN_NOTICE)          # exactly at the threshold
dash = client.get("/admin").data.decode("utf-8")
check("still quiet AT the threshold", "failed sign-in attempts" not in dash)
add_failures(1)
dash = client.get("/admin").data.decode("utf-8")
check("appears once above the threshold",
      "failed sign-in attempts in the last 24 hours" in dash, dash[:0])
check("and links to the filtered audit log",
      "/admin/audit?action=login_failed" in dash)
with app.app_context():
    AuditLog.query.filter_by(action="login_failed").delete()
    db.session.commit()
add_failures(8, minutes_ago=60 * 30)       # older than the window
dash = client.get("/admin").data.decode("utf-8")
check("old failures do not count", "failed sign-in attempts" not in dash)

# ---- the alert email
with app.app_context():
    AuditLog.query.delete()
    db.session.commit()
sent.clear()
set_alerts(False)
add_failures(ALERT_IP_THRESHOLD + 2)
with app.app_context():
    note_failed_login("target@example.org", "203.0.113.9")
check("nothing is emailed while alerts are off", not sent)
with app.app_context():
    check("and the setting really is off", security_alerts_on() is False)

set_alerts(True)
with app.app_context():
    AuditLog.query.filter_by(action="login_failed").delete()
    db.session.commit()
add_failures(ALERT_IP_THRESHOLD - 1)
with app.app_context():
    note_failed_login("target@example.org", "203.0.113.9")
check("below the threshold, still nothing", not sent)

add_failures(3)                            # now over it
with app.app_context():
    note_failed_login("target@example.org", "203.0.113.9")
check("above the threshold, one email", len(sent) == 1, str(len(sent)))
alert = sent[0]
body = alert.get_content()
check("alert names the IP", "203.0.113.9" in body)
check("alert names the address tried", "target@example.org" in body)
check("alert counts the attempts", "failed sign-in attempts" in body)
check("ALERT CONTAINS NO PASSWORD MATERIAL",
      SECRET_PASSWORD not in body and "password:" not in body.lower()
      and "hunter2" not in body)
check("alert links to the audit log", "/admin/audit" in body)
with app.app_context():
    logged = (AuditLog.query.filter_by(action="security_alert")
              .order_by(AuditLog.id.desc()).first())
    check("the alert is logged", logged is not None)
    check("alert log has no password", SECRET_PASSWORD not in
          (logged.summary or ""))

# ---- the cooldown stops a flood
add_failures(20)
for _i in range(5):
    with app.app_context():
        note_failed_login("target@example.org", "203.0.113.9")
check("COOLDOWN: still only one email", len(sent) == 1, str(len(sent)))
with app.app_context():
    # age the alert out of the cooldown window
    entry = (AuditLog.query.filter_by(action="security_alert")
             .order_by(AuditLog.id.desc()).first())
    entry.created_at = datetime.utcnow() - timedelta(minutes=61)
    db.session.commit()
    note_failed_login("target@example.org", "203.0.113.9")
check("after the cooldown, one more is allowed", len(sent) == 2,
      str(len(sent)))

# ---- a different IP is counted separately
with app.app_context():
    AuditLog.query.filter_by(action="login_failed").delete()
    db.session.commit()
sent.clear()
add_failures(ALERT_IP_THRESHOLD + 2, ip="198.51.100.7")
with app.app_context():
    note_failed_login("someone@example.org", "203.0.113.9")
check("an IP with no failures does not trigger an alert", not sent)

# ---- the security alert address: its own setting, not the enquiries one
with app.app_context():
    check("with nothing set, alerts follow the enquiries address",
          security_alert_to() == "netbus@example.org", security_alert_to())
    check("and the badge says so",
          security_alert_setting()["source"] == "fallback",
          security_alert_setting()["source"])

client.get("/admin/logout")
appmod._rate_buckets.clear()
client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})
r = client.post("/admin/settings/security-alerts",
                data={"enabled": "on", "alert_to": "it@netbus.co.uk"},
                follow_redirects=True)
check("security address saved", b"Saved. Alerts are on" in r.data)
with app.app_context():
    check("stored in its own Block",
          Block.query.filter_by(key=SECURITY_ALERT_TO_KEY).first().value
          == "it@netbus.co.uk")
    check("in force now", security_alert_to() == "it@netbus.co.uk")
    check("badge says it came from this page",
          security_alert_setting()["source"] == "database")

# an alert now goes THERE, not to the enquiries address
sent.clear()
with app.app_context():
    AuditLog.query.delete()
    db.session.commit()
add_failures(ALERT_IP_THRESHOLD + 2)
with app.app_context():
    note_failed_login("target@example.org", "203.0.113.9")
check("alert sent", len(sent) == 1, str(len(sent)))
check("ALERT GOES TO THE SECURITY ADDRESS",
      sent and sent[0]["To"] == "it@netbus.co.uk",
      sent[0]["To"] if sent else "nothing sent")
check("and NOT to the enquiries address",
      "netbus@example.org" not in str(sent[0]["To"]))

# several recipients
r = client.post("/admin/settings/security-alerts",
                data={"enabled": "on",
                      "alert_to": "it@netbus.co.uk, oncall@netbus.co.uk"},
                follow_redirects=True)
check("several addresses accepted", b"Saved. Alerts are on" in r.data)
with app.app_context():
    check("both kept", security_alert_setting()["recipients"]
          == ["it@netbus.co.uk", "oncall@netbus.co.uk"],
          str(security_alert_setting()["recipients"]))
sent.clear()
with app.app_context():
    AuditLog.query.filter_by(action="security_alert").delete()
    db.session.commit()
    note_failed_login("target@example.org", "203.0.113.9")
check("the alert addresses both",
      sent and "it@netbus.co.uk" in sent[0]["To"]
      and "oncall@netbus.co.uk" in sent[0]["To"],
      sent[0]["To"] if sent else "nothing sent")

# rubbish is refused, and does not overwrite what was there
r = client.post("/admin/settings/security-alerts",
                data={"enabled": "on",
                      "alert_to": "it@netbus.co.uk, nonsense"},
                follow_redirects=True)
check("an invalid address in the list is refused",
      b"do not look like email addresses" in r.data)
with app.app_context():
    check("the good setting survived the bad save",
          security_alert_setting()["recipients"]
          == ["it@netbus.co.uk", "oncall@netbus.co.uk"])
r = client.post("/admin/settings/security-alerts",
                data={"enabled": "on", "alert_to": "no-at-sign.example.com"},
                follow_redirects=True)
check("a single bad address is refused too",
      b"do not look like email addresses" in r.data)

# clearing it falls back again
r = client.post("/admin/settings/security-alerts",
                data={"enabled": "on", "alert_to": ""},
                follow_redirects=True)
with app.app_context():
    check("cleared: back to the enquiries address",
          security_alert_to() == "netbus@example.org", security_alert_to())
    check("and the badge says it is a fallback",
          security_alert_setting()["source"] == "fallback")

# the test-alert button
client.post("/admin/settings/security-alerts",
            data={"enabled": "on", "alert_to": "it@netbus.co.uk"},
            follow_redirects=True)
sent.clear()
appmod._rate_buckets.clear()
r = client.post("/admin/settings/test-alert", follow_redirects=True)
check("test alert sent", b"Test alert sent to it@netbus.co.uk" in r.data)
check("it went to the security address",
      sent and sent[0]["To"] == "it@netbus.co.uk",
      sent[0]["To"] if sent else "nothing sent")
check("the test alert carries no password",
      SECRET_PASSWORD not in sent[0].get_content())
with app.app_context():
    entry = (AuditLog.query.filter_by(action="test_mail")
             .order_by(AuditLog.id.desc()).first())
    check("test alert logged", entry is not None
          and "test security alert" in entry.summary,
          entry.summary if entry else "none")
accepted = 1
for _i in range(6):
    r = client.post("/admin/settings/test-alert", follow_redirects=True)
    if b"Test alert sent" in r.data:
        accepted += 1
check("test alerts are rate limited", accepted == 5, "%d accepted" % accepted)
appmod._rate_buckets.clear()

# the page shows it; a client admin sees neither page nor address
page = client.get("/admin/features").data.decode("utf-8")
check("panel shows the security address", "it@netbus.co.uk" in page)
check("panel labels where it came from", "This page" in page)
check("the checkbox no longer says 'me'",
      "Email an alert when sign-ins keep failing" in page
      and "Email me when sign-ins" not in page)
check("panel names the recipients in the helper text",
      "Alerts currently go to" in page)
client.get("/admin/logout")

client.post("/admin/login", data={"email": "client@example.com",
                                  "password": PW})
r = client.get("/admin/features")
check("client admin still refused the page", r.status_code == 403)
check("THE SECURITY ADDRESS IS NOT RENDERED TO THEM",
      b"it@netbus.co.uk" not in r.data)
for path in ("/admin/settings/security-alerts", "/admin/settings/test-alert"):
    r = client.post(path, data={"enabled": "on",
                                "alert_to": "attacker@example.com"})
    check("client admin refused %s" % path, r.status_code == 403,
          str(r.status_code))
with app.app_context():
    check("and nothing was changed", security_alert_to() == "it@netbus.co.uk")
r = client.get("/admin")
check("address not leaked onto the dashboard either",
      b"it@netbus.co.uk" not in r.data)
client.get("/admin/logout")

# ---- teardown

with app.app_context():
    db.session.remove()
    db.engine.dispose()
shutil.rmtree(SANDBOX, ignore_errors=True)
check("sandbox removed", not os.path.isdir(SANDBOX))
# Windows releases file handles lazily, and this test opens the database
# with raw sqlite3 as well as through SQLAlchemy, so give it a moment
# rather than failing the run over housekeeping.
import gc                                                      # noqa: E402
import time as _time                                           # noqa: E402
gc.collect()
for _attempt in range(10):
    stuck = False
    for suffix in ("", "-wal", "-shm"):
        f = TEST_DB + suffix
        if os.path.isfile(f):
            try:
                os.remove(f)
            except OSError:
                stuck = True
    if not stuck:
        break
    _time.sleep(0.2)
check("test db deleted", not os.path.exists(TEST_DB))

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
