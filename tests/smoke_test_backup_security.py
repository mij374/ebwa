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
import re
import shutil
import sqlite3
import sys
import tempfile
import time
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
                     # The panel's poll reads the same machine the panel
                     # does, so it is gated exactly as the page is.
                     ("/admin/settings/backup.json", "GET"),
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
# Prose checks read the page as a reader does: tags stripped and
# whitespace collapsed, so a phrase that happens to wrap or carry a <b>
# in the middle still matches.
prose = " ".join(re.sub(r"<[^>]+>", " ", page).split())
check("panel says an on-server archive is not a backup",
      "not a backup" in prose)
# The old wording claimed this page could not configure the copy off the
# server, which stopped being true when the NAS transfer landed directly
# beneath it. It now describes what actually happens, and the check
# follows the claim rather than being dropped.
check("panel says archives are sent to the destination below",
      "sent to the SFTP destination set up below" in prose)
check("panel says retention applies at both ends",
      "keeping the newest" in prose and "are kept" in prose
      and "Retention is separate at each end" in prose)
check("panel says the destination password is encrypted",
      "encrypted before it is stored" in prose)
check("panel no longer claims it cannot configure the transfer",
      "cannot configure that" not in prose)
check("panel says plainly when nothing is leaving the server",
      "nothing is leaving this server" in prose)   # transfer off here
check("panel never shows the SMTP password", SECRET_PASSWORD not in page)

# ---- the button works, is logged, and is rate limited
# THE BUTTON STARTS THE WORK AND RETURNS; the archive is written by a
# thread. So "did it work?" is no longer answerable from the response —
# it is answered by the BackupRun row a moment later, which is what the
# panel reads too.
def press_and_wait(seconds=20):
    """Press Back up now, then wait for the run to finish. (row, response)"""
    response = client.post("/admin/settings/backup", follow_redirects=True)
    deadline = time.time() + seconds
    while time.time() < deadline:
        with app.app_context():
            row = (BackupRun.query.filter_by(reason="manual")
                   .order_by(BackupRun.id.desc()).first())
            if row is not None and row.status != "running":
                return row, response
        time.sleep(0.05)
    with app.app_context():
        return ((BackupRun.query.filter_by(reason="manual")
                 .order_by(BackupRun.id.desc()).first()), response)


appmod._rate_buckets.clear()
run, r = press_and_wait()
check("THE BUTTON RETURNS AT ONCE, saying the backup has started",
      b"Backup started" in r.data,
      r.data.decode("utf-8", "replace")[:300])
check("...and does not wait for the archive to be written",
      b"Backup written" not in r.data)
check("the backup then actually runs, in the background",
      run is not None and run.status == "ok",
      run.status + " / " + (run.error or "") if run else "no run")
check("...and writes a real archive",
      run is not None and run.filename
      and os.path.isfile(os.path.join(ARCHIVES, run.filename)),
      run.filename if run else "none")
with app.app_context():
    entry = (AuditLog.query.filter_by(action="backup")
             .order_by(AuditLog.id.desc()).first())
    check("backup logged", entry is not None
          and "Settings page" in entry.summary,
          entry.summary if entry else "none")
LIMIT = appmod.BACKUP_MANUAL_PER_HOUR
check("the limit is a named constant, not a number in the route",
      appmod.RATE_LIMITS["backup"][0] == LIMIT and LIMIT == 10, str(LIMIT))
check("the panel states the real limit",
      "Up to %d an hour" % LIMIT in page, "panel text")

accepted = 1
for _i in range(LIMIT + 2):
    _run, r = press_and_wait()
    if b"Backup started" in r.data:
        accepted += 1
check("the button allows the stated number an hour", accepted == LIMIT,
      "%d accepted, limit %d" % (accepted, LIMIT))
check("and the refusal states the actual limit",
      ("That is %d backups in an hour" % LIMIT).encode() in r.data,
      r.data.decode("utf-8", "replace")[:200])
check("no stale 'twice an hour' wording survives anywhere",
      b"twice an hour" not in r.data
      and "twice an hour" not in page.lower())
with app.app_context():
    refusal = (AuditLog.query.filter_by(action="backup")
               .order_by(AuditLog.id.desc()).first())
    check("a refused backup is logged too, not silently dropped",
          refusal is not None and "Refused" in refusal.summary
          and str(LIMIT) in refusal.summary,
          refusal.summary if refusal else "none")
appmod._rate_buckets.clear()

# ---- THE BUTTON IS ASYNCHRONOUS ---------------------------------------
# The request starts the work and returns; a thread writes the archive.
# What matters is that the panel can say what is happening at every point
# of that, and that a second press is still refused.
print()
print("---- starting a backup without waiting for it")
import threading                                                # noqa: E402
from app import (backup_state, claim_backup_slot,               # noqa: E402
                 start_backup, backup_job, BACKUP_STATE_LABELS,
                 BACKUP_STATE_PILLS, BACKUP_STALE_MINUTES, current_actor)

appmod._rate_buckets.clear()
with app.app_context():
    BackupRun.query.delete()
    db.session.commit()
    check("with no runs at all, the panel says so rather than breaking",
          backup_state()["state"] == "none"
          and backup_state()["busy"] is False)

# THE RESPONSE MUST COME BACK BEFORE THE WORK IS DONE. Timing a request
# would be a flaky assertion, so this proves it structurally: the archive
# is held shut until we let go, and the response has to arrive anyway.
gate = threading.Event()
real_run_backup = appmod.run_backup


def slow_run_backup(reason="manual", run=None):
    gate.wait(20)
    return real_run_backup(reason=reason, run=run)


appmod.run_backup = slow_run_backup
try:
    r = client.post("/admin/settings/backup", follow_redirects=True)
    check("THE PAGE COMES BACK WHILE THE BACKUP IS STILL RUNNING",
          b"Backup started" in r.data,
          r.data.decode("utf-8", "replace")[:200])
    with app.app_context():
        state = backup_state()
        check("...and the panel says it is running", state["state"] == "running"
              and state["busy"] is True, str(state["state"]))
        check("...with the time it started", state["started"] != "")
        check("...and no finish time yet", state["finished"] == "")
        check("...described in words a trustee can read",
              "Writing the archive" in state["detail"], state["detail"])

    # The page itself, mid-run, is what an admin actually sees.
    page = client.get("/admin/features").data.decode("utf-8")
    check("the page shows the running state on load",
          'id="backupState"' in page and 'data-busy="1"' in page)
    check("...with the pill saying Running",
          BACKUP_STATE_LABELS["running"] in page)

    # THE JSON THE PANEL POLLS.
    data = client.get("/admin/settings/backup.json").get_json()
    check("the JSON endpoint reports it running",
          data["state"] == "running" and data["busy"] is True, str(data))
    check("...and hands over the label and colour, not just a code",
          data["label"] == "Running" and data["pill"] == "amber", str(data))

    # A SECOND PRESS WHILE ONE RUNS IS STILL REFUSED.
    r2 = client.post("/admin/settings/backup", follow_redirects=True)
    check("A SECOND BACKUP IS REFUSED WHILE ONE IS RUNNING",
          b"already running" in r2.data,
          r2.data.decode("utf-8", "replace")[:200])
    with app.app_context():
        check("...and no second run was started",
              BackupRun.query.filter_by(status="running").count() == 1,
              str(BackupRun.query.filter_by(status="running").count()))
        entry = (AuditLog.query.filter_by(action="backup")
                 .order_by(AuditLog.id.desc()).first())
        check("...and the refusal is logged",
              entry is not None and "Refused" in entry.summary,
              entry.summary if entry else "none")
finally:
    gate.set()
    appmod.run_backup = real_run_backup

deadline = time.time() + 20
while time.time() < deadline:
    with app.app_context():
        if backup_state()["state"] != "running":
            break
    time.sleep(0.05)

with app.app_context():
    done = backup_state()
check("IT FINISHES ON ITS OWN, with nobody watching",
      done["state"] == "ok", str(done["state"]) + " " + str(done["detail"]))
check("...and the panel then shows a finish time",
      done["finished"] != "" and done["busy"] is False)
check("...and names the archive and its size",
      ".zip" in done["detail"], done["detail"])

data = client.get("/admin/settings/backup.json").get_json()
check("THE POLL SEES THE CHANGE WITHOUT A PAGE REFRESH",
      data["state"] == "ok" and data["busy"] is False, str(data))
check("...so the script knows to stop polling", data["busy"] is False)

# WHO PRESSED IT SURVIVES THE THREAD. A background thread has no
# current_user, and an audit entry reading "anonymous" for something a
# named super admin did is the one fact worth keeping, lost.
with app.app_context():
    entries = (AuditLog.query.filter_by(action="backup")
               .order_by(AuditLog.id.desc()).limit(6).all())
    ran = [e for e in entries if "Ran a backup" in e.summary]
    check("THE COMPLETION IS LOGGED AGAINST THE PERSON WHO PRESSED IT",
          ran and ran[0].user_email == "netbus@example.com",
          ran[0].user_email if ran else "no completion entry")
    started = [e for e in entries if "Started a backup" in e.summary]
    check("...and so is the start", bool(started),
          str([e.summary for e in entries]))

# ---- A FAILURE SHOWS THE REASON ---------------------------------------
print()
print("---- when it fails")
with app.app_context():
    BackupRun.query.delete()
    db.session.commit()
appmod._rate_buckets.clear()


def broken_run_backup(reason="manual", run=None):
    raise RuntimeError("the disk went away")


appmod.run_backup = broken_run_backup
try:
    r = client.post("/admin/settings/backup", follow_redirects=True)
    check("a backup that will fail still starts cheerfully",
          b"Backup started" in r.data)
    deadline = time.time() + 20
    while time.time() < deadline:
        with app.app_context():
            if backup_state()["state"] != "running":
                break
        time.sleep(0.05)
finally:
    appmod.run_backup = real_run_backup

with app.app_context():
    bad = backup_state()
check("AN EXCEPTION IN THE THREAD BECOMES A FAILED RUN, not a stuck one",
      bad["state"] == "failed", str(bad["state"]))
check("...and the panel shows the reason",
      "the disk went away" in bad["detail"], bad["detail"])
data = client.get("/admin/settings/backup.json").get_json()
check("...which the poll reports too, in red",
      data["state"] == "failed" and data["pill"] == "red"
      and "disk went away" in data["detail"], str(data))
with app.app_context():
    entry = (AuditLog.query.filter_by(action="backup")
             .order_by(AuditLog.id.desc()).first())
    check("...and the failure is logged against the person",
          entry is not None and "failed" in entry.summary
          and entry.user_email == "netbus@example.com",
          entry.summary if entry else "none")
    check("a failed run does not go on blocking the next one",
          appmod.backup_in_progress() is None)

# ---- A WORKER KILLED MID-BACKUP ---------------------------------------
# gunicorn is restarted on every deploy, and the thread dies with the
# process, leaving a row saying "running" that nothing is working on.
print()
print("---- a backup interrupted by a restart")
with app.app_context():
    BackupRun.query.delete()
    db.session.add(BackupRun(status="running", reason="manual",
                             started_at=datetime.utcnow()
                             - timedelta(minutes=BACKUP_STALE_MINUTES + 1)))
    db.session.commit()
    stale = backup_state()
check("A ROW LEFT 'RUNNING' BY A RESTART READS AS INTERRUPTED, not running",
      stale["state"] == "interrupted", str(stale["state"]))
check("...and is NOT busy, so the panel stops polling for ever",
      stale["busy"] is False)
check("...and says what happened, in plain words",
      "restarted" in stale["detail"] and "start another" in stale["detail"],
      stale["detail"])
with app.app_context():
    check("...and the guard has let go, so another can be started",
          appmod.backup_in_progress() is None)
appmod._rate_buckets.clear()
_run, r = press_and_wait()
check("...which it can be", b"Backup started" in r.data)

# A row that is running and RECENT is still busy — the stale rule must
# not be so eager that it declares a live backup dead.
with app.app_context():
    BackupRun.query.delete()
    db.session.add(BackupRun(status="running", reason="manual",
                             started_at=datetime.utcnow()
                             - timedelta(minutes=BACKUP_STALE_MINUTES - 1)))
    db.session.commit()
    fresh = backup_state()
check("a backup still inside the stale window is left alone",
      fresh["state"] == "running" and fresh["busy"] is True,
      str(fresh["state"]))

# ---- THE CLAIM IS A CLAIM, not a look ---------------------------------
# Two clicks landing on the two gunicorn workers at the same instant both
# pass the "is one running?" read. Only one may come away with the slot.
print()
print("---- two presses at the same instant")
with app.app_context():
    BackupRun.query.delete()
    db.session.commit()

claimed = []
barrier = threading.Barrier(4)


def claim():
    barrier.wait(10)
    with app.app_context():
        got = claim_backup_slot("manual")
        claimed.append(got.id if got is not None else None)


# The barrier is the size of the racing threads and nothing else — the
# main thread must NOT wait on it, or it is a fifth party to a
# four-party rendezvous and everybody waits for somebody who is late.
racers = [threading.Thread(target=claim) for _ in range(4)]
for t in racers:
    t.start()
for t in racers:
    t.join(20)

check("FOUR SIMULTANEOUS CLAIMS PRODUCE EXACTLY ONE WINNER",
      len([c for c in claimed if c is not None]) == 1, str(claimed))
with app.app_context():
    check("...and exactly one row is left behind",
          BackupRun.query.count() == 1, str(BackupRun.query.count()))
    check("...still saying running, for the winner to work on",
          BackupRun.query.first().status == "running")
    BackupRun.query.delete()
    db.session.commit()

# ---- THE ENDPOINT IS SUPER-ADMIN ONLY ---------------------------------
print()
print("---- who may read it")
anon = app.test_client()
check("an anonymous visitor gets the login page, not the JSON",
      anon.get("/admin/settings/backup.json").status_code == 302)
check("...and cannot start one either",
      anon.post("/admin/settings/backup").status_code == 302)

# It READS and never acts: a GET must not start anything.
with app.app_context():
    before = BackupRun.query.count()
client.get("/admin/settings/backup.json")
client.get("/admin/settings/backup.json")
with app.app_context():
    check("READING THE PANEL STARTS NOTHING",
          BackupRun.query.count() == before, str(BackupRun.query.count()))

# ---- the page tells somebody without JavaScript what to do ------------
page = client.get("/admin/features").data.decode("utf-8")
check("the page says to refresh if it is not updating on its own",
      "refresh it to see where the backup has got to" in page)
check("...and that the panel is driven from the run, not a guess",
      'id="backupState"' in page and 'id="backupPill"' in page)
check("...and explains what an interrupted backup means",
      "Interrupted" in page and str(BACKUP_STALE_MINUTES) in page)
check("the button is a plain POST form, working without the script",
      'action="/admin/settings/backup"' in page and 'method="post"' in page)

# ---- the concurrency guard: the real reason not to start a second run
with app.app_context():
    before = BackupRun.query.count()
    db.session.add(BackupRun(status="running", reason="cli",
                             started_at=datetime.utcnow()))
    db.session.commit()
    check("a running backup is detected", appmod.backup_in_progress()
          is not None)
r = client.post("/admin/settings/backup", follow_redirects=True)
check("a second backup is refused while one is running",
      b"already running" in r.data)
check("and it does not just say it is rate limited",
      b"backups in an hour" not in r.data)
with app.app_context():
    check("no second run was started", BackupRun.query.count() == before + 1,
          str(BackupRun.query.count() - before))
    entry = (AuditLog.query.filter_by(action="backup")
             .order_by(AuditLog.id.desc()).first())
    check("the refusal is logged", entry is not None
          and "still running" in entry.summary,
          entry.summary if entry else "none")
    check("the refusal names no file path or command",
          ARCHIVES not in entry.summary)

# a run abandoned by a crash must not block backups for ever
with app.app_context():
    stuck = (BackupRun.query.filter_by(status="running")
             .order_by(BackupRun.id.desc()).first())
    stuck.started_at = (datetime.utcnow()
                        - timedelta(minutes=appmod.BACKUP_STALE_MINUTES + 5))
    db.session.commit()
    check("a stale 'running' row stops counting",
          appmod.backup_in_progress() is None)
appmod._rate_buckets.clear()
_run, r = press_and_wait()
check("so a backup can still be run after a crash",
      b"Backup started" in r.data)
with app.app_context():
    for row in BackupRun.query.filter_by(status="running").all():
        db.session.delete(row)
    db.session.commit()
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
