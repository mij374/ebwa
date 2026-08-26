"""Smoke test for the NAS transfer (CLAUDE.md rules).

Covers: settings save and take effect; the NAS password round-trips
through Fernet and appears NOWHERE in plaintext — not in the database,
not in rendered HTML, not in a flash, not in the audit log; an empty
password box keeps the stored one instead of wiping it; the upload path
works against a mocked SFTP server, renaming a .part into place; a
failure records the reason on the same BackupRun and is retried exactly
once; remote retention keeps the newest N on the NAS; the connection test
reports precise failures and proves the folder is writable with a probe
file; run-scheduled-backup is idempotent within a day and respects the
two-attempt rule; and a client admin gets 403 everywhere.

Runs against a throwaway db, uploads folder and backup folder, with
paramiko replaced by a fake server. Nothing real is touched.

Run:  python tests/smoke_test_sftp_backup.py
"""
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_sftp.db")
SANDBOX = tempfile.mkdtemp(prefix="ebwa-sftp-test-")
UPLOADS = os.path.join(SANDBOX, "uploads")
ARCHIVES = os.path.join(SANDBOX, "archives")
REMOTE = os.path.join(SANDBOX, "nas")
os.makedirs(UPLOADS)
os.makedirs(REMOTE)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
os.environ["BACKUP_DIR"] = ARCHIVES
os.environ["BACKUP_KEEP"] = "5"
sys.path.insert(0, os.path.dirname(HERE))

from cryptography.fernet import Fernet                         # noqa: E402

KEY = Fernet.generate_key().decode()
os.environ["FERNET_KEY"] = KEY

import app as appmod                                           # noqa: E402
from app import (app, db, AuditLog, BackupRun, Block,          # noqa: E402
                 DEFAULT_BLOCKS, FEATURES, FeatureFlag, SFTP_KEYS,
                 SFTP_MAX_ATTEMPTS, User, decrypt_secret, prune_remote_backups,
                 run_backup, scheduled_run_due, sftp_password, sftp_ready,
                 sftp_settings, test_sftp, transfer_with_retry,
                 upload_backup)

app.config["TESTING"] = True
appmod.UPLOAD_DIR = UPLOADS

PW = "sftp-test-password"
NAS_PASSWORD = "nas-secret-do-not-leak"
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------- fake NAS
class FakeSFTP:
    """A pretend SFTP server backed by a folder on this machine."""
    fail_on_put = None          # an exception to raise instead of storing
    puts = []                   # every remote name written
    renames = []
    removed = []

    def __init__(self, root):
        self.root = root

    def _local(self, remote):
        return os.path.join(self.root, os.path.basename(remote))

    def stat(self, path):
        if not os.path.isdir(self.root):
            raise IOError("no such folder")
        return os.stat(self.root)

    def file(self, remote, mode="r"):
        return open(self._local(remote), mode)

    def put(self, local, remote):
        if FakeSFTP.fail_on_put:
            raise FakeSFTP.fail_on_put
        shutil.copyfile(local, self._local(remote))
        FakeSFTP.puts.append(os.path.basename(remote))

    def rename(self, old, new):
        os.replace(self._local(old), self._local(new))
        FakeSFTP.renames.append((os.path.basename(old),
                                 os.path.basename(new)))

    def remove(self, remote):
        path = self._local(remote)
        if not os.path.isfile(path):
            raise IOError("no such file")
        os.remove(path)
        FakeSFTP.removed.append(os.path.basename(remote))

    def listdir(self, path):
        return os.listdir(self.root)

    def get_channel(self):
        return self

    def settimeout(self, value):
        pass

    def close(self):
        pass


class FakeSSHClient:
    connect_error = None
    last_connect = None

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, host, **kwargs):
        FakeSSHClient.last_connect = dict(kwargs, host=host)
        if FakeSSHClient.connect_error:
            raise FakeSSHClient.connect_error

    def open_sftp(self):
        return FakeSFTP(REMOTE)

    def close(self):
        pass


class FakeParamiko:
    SSHClient = FakeSSHClient

    class AutoAddPolicy:
        pass

    import paramiko as _real
    AuthenticationException = _real.AuthenticationException
    BadHostKeyException = _real.BadHostKeyException


sys.modules["paramiko"] = FakeParamiko


def nas_files():
    return sorted(os.listdir(REMOTE))


def save_settings(**overrides):
    data = {"enabled": "on", "host": "nas.tailnet.ts.net", "port": "22",
            "user": "ebwa-backup", "path": "/volume1/backups/ebwa",
            "schedule": "02:30", "keep": "3", "password": NAS_PASSWORD}
    data.update(overrides)
    if data.get("enabled") is None:
        data.pop("enabled")
    return client.post("/admin/settings/sftp", data=data,
                       follow_redirects=True)


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

with open(os.path.join(UPLOADS, "photo.jpg"), "wb") as fh:
    fh.write(b"pretend-photo")

client = app.test_client()

# ---- off until it is configured
with app.app_context():
    check("transfers start switched off", sftp_ready() is False)
    check("no password stored yet",
          sftp_settings()["password_set"] is False)
    check("the key is present for these tests",
          sftp_settings()["key_present"] is True)

# ---- client admins cannot touch any of it
client.post("/admin/login", data={"email": "client@example.com",
                                  "password": PW})
for path in ("/admin/settings/sftp", "/admin/settings/sftp/test"):
    r = client.post(path, data={"host": "evil.example.net"})
    check("client admin refused %s" % path, r.status_code == 403,
          str(r.status_code))
with app.app_context():
    check("nothing was saved", sftp_settings()["host"] == "")
client.get("/admin/logout")

client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})

# ---- saving, and the password going in encrypted
r = save_settings()
check("settings saved", b"NAS backup settings saved" in r.data)
with app.app_context():
    cfg = sftp_settings()
    check("host stored", cfg["host"] == "nas.tailnet.ts.net")
    check("port stored", cfg["port"] == 22)
    check("user stored", cfg["user"] == "ebwa-backup")
    check("path stored", cfg["path"] == "/volume1/backups/ebwa")
    check("schedule stored", cfg["schedule"] == "02:30")
    check("remote retention stored", cfg["keep"] == 3)
    check("enabled", cfg["enabled"] is True and sftp_ready() is True)
    check("password reported as set", cfg["password_set"] is True)

    stored = Block.query.filter_by(key=SFTP_KEYS["password"]).first().value
    check("THE STORED VALUE IS NOT THE PASSWORD", NAS_PASSWORD not in stored,
          stored[:40])
    check("it is Fernet ciphertext", stored.startswith("gAAAAA"), stored[:12])
    check("and it decrypts back", decrypt_secret(stored) == NAS_PASSWORD)
    check("sftp_password() returns the plaintext for use",
          sftp_password() == NAS_PASSWORD)

# nowhere in the database at all
with app.app_context():
    values = [b.value or "" for b in Block.query.all()]
    check("NO BLOCK HOLDS THE PLAINTEXT",
          not any(NAS_PASSWORD in v for v in values))

page = client.get("/admin/features").data.decode("utf-8")
check("password never rendered", NAS_PASSWORD not in page)
# With transfer ON the Backups panel must describe the whole journey:
# written here, sent there, retention at each end. The off state is
# covered in smoke_test_backup_security.py.
prose = " ".join(re.sub(r"<[^>]+>", " ", page).split())
check("panel says archives are sent to the destination",
      "sent to the SFTP destination set up below" in prose)
check("panel states both retentions", "keeping the newest" in prose
      and "the newest 3 are kept" in prose)
check("panel does not warn that nothing leaves the server",
      "nothing is leaving this server" not in prose)
check("panel says the stored password is encrypted",
      "encrypted before it is stored" in prose)
check("page says a password is set", "Password set" in page)
check("password field is a password field",
      'type="password"' in page and 'name="password"' in page)
with app.app_context():
    entries = " ".join(e.summary or "" for e in AuditLog.query.all())
    check("audit log never holds the password", NAS_PASSWORD not in entries)
    check("but records that it changed", "password changed" in entries)

# ---- an empty password box keeps the stored one
r = save_settings(password="", user="ebwa-backup2")
check("saved without touching the password", b"settings saved" in r.data)
with app.app_context():
    check("other field changed", sftp_settings()["user"] == "ebwa-backup2")
    check("PASSWORD KEPT, NOT WIPED", sftp_password() == NAS_PASSWORD)
save_settings(password="")          # restore the username
with app.app_context():
    check("still ready", sftp_ready() is True)

# ---- validation
for bad, expected in ((("port", "notanumber"), b"between 1 and 65535"),
                      (("port", "99999"), b"between 1 and 65535"),
                      (("schedule", "25:00"), b"look like 02:30"),
                      (("schedule", "2.30"), b"look like 02:30"),
                      (("keep", "0"), b"at least one archive"),
                      (("path", "volume1/backups"), b"start with a slash")):
    field, value = bad
    r = save_settings(**{field: value})
    check("refuses %s=%s" % (field, value), expected in r.data,
          r.data.decode("utf-8")[:200])
with app.app_context():
    check("a refused save changed nothing",
          sftp_settings()["schedule"] == "02:30"
          and sftp_settings()["port"] == 22)

# ---- the connection test
FakeSSHClient.connect_error = None
appmod._rate_buckets.clear()
r = client.post("/admin/settings/sftp/test", follow_redirects=True)
check("connection test succeeds", b"Connection test" in r.data
      and b"wrote a test file" in r.data)
check("the probe file was cleaned up",
      not any(n.startswith("ebwa-write-test") for n in nas_files()),
      str(nas_files()))
with app.app_context():
    entry = (AuditLog.query.filter_by(action="sftp_test")
             .order_by(AuditLog.id.desc()).first())
    check("test logged", entry is not None and "succeeded" in entry.summary)
    check("test log has no password", NAS_PASSWORD not in (entry.summary or ""))

import paramiko as _paramiko                                   # noqa: E402
CASES = [
    (_paramiko.AuthenticationException("nope"), b"rejected the username"),
    (TimeoutError("timed out"), b"did not answer within"),
    (ConnectionRefusedError("refused"), b"nothing is listening"),
]
for exc, expected in CASES:
    appmod._rate_buckets.clear()
    FakeSSHClient.connect_error = exc
    r = client.post("/admin/settings/sftp/test", follow_redirects=True)
    check("%s reported precisely" % type(exc).__name__, expected in r.data,
          r.data.decode("utf-8")[:200])
    check("%s: no password in the page" % type(exc).__name__,
          NAS_PASSWORD.encode() not in r.data)
FakeSSHClient.connect_error = None

# a folder that is not there
appmod._rate_buckets.clear()
moved = REMOTE + "-away"
os.rename(REMOTE, moved)
r = client.post("/admin/settings/sftp/test", follow_redirects=True)
check("a missing remote folder is reported",
      b"does not exist on the NAS" in r.data,
      r.data.decode("utf-8")[:200])
os.rename(moved, REMOTE)

appmod._rate_buckets.clear()
for _i in range(7):
    r = client.post("/admin/settings/sftp/test", follow_redirects=True)
check("connection tests are rate limited",
      b"enough connection tests" in r.data)
appmod._rate_buckets.clear()

# ---- the upload itself
with app.app_context():
    run = run_backup(reason="manual")
    check("a backup to send", run.status == "ok", run.error or "")
    ok = upload_backup(run)
    check("upload reports success", ok is True)
    check("transfer status recorded", run.transfer_status == "ok")
    check("remote filename recorded", run.remote_filename == run.filename)
    check("transfer time recorded", run.transferred_at is not None)
    check("one attempt was enough", run.transfer_attempts == 1)
    first_archive = run.filename
check("THE ARCHIVE IS ON THE NAS", first_archive in nas_files(),
      str(nas_files()))
check("it was uploaded as .part first, then renamed",
      FakeSFTP.puts and FakeSFTP.puts[-1].endswith(".part")
      and FakeSFTP.renames and FakeSFTP.renames[-1][1] == first_archive,
      str(FakeSFTP.puts[-1:]) + str(FakeSFTP.renames[-1:]))
check("no .part left behind",
      not any(n.endswith(".part") for n in nas_files()), str(nas_files()))
local_size = os.path.getsize(os.path.join(ARCHIVES, first_archive))
check("the copy is the same size as the original",
      os.path.getsize(os.path.join(REMOTE, first_archive)) == local_size)

# ---- a failure is recorded on the same run, and retried once
FakeSFTP.fail_on_put = IOError("disk full on the NAS")
with app.app_context():
    run2 = run_backup(reason="manual")
    sent = transfer_with_retry(run2)
    check("a failing transfer reports failure", sent is False)
    check("recorded on the SAME BackupRun", run2.transfer_status == "failed")
    check("with the reason", "disk full" in (run2.transfer_error or ""),
          run2.transfer_error)
    check("tried exactly twice, then stopped",
          run2.transfer_attempts == SFTP_MAX_ATTEMPTS,
          str(run2.transfer_attempts))
    check("the backup itself is still good", run2.status == "ok")
    entry = (AuditLog.query.filter_by(action="backup")
             .order_by(AuditLog.id.desc()).first())
    check("the failure is in the audit log",
          "could not be sent to the NAS" in (entry.summary or ""),
          entry.summary if entry else "none")
    check("audit entry has no password",
          NAS_PASSWORD not in (entry.summary or ""))
check("the archive stayed on the server",
      os.path.isfile(os.path.join(ARCHIVES, run2.filename)))
FakeSFTP.fail_on_put = None

# ---- remote retention
for i in range(4):
    old_name = "ebwa-backup-2020010%d-000000.zip" % (i + 1)
    with open(os.path.join(REMOTE, old_name), "wb") as fh:
        fh.write(b"old archive")
with app.app_context():
    before = len([n for n in nas_files() if n.endswith(".zip")])
    removed = prune_remote_backups()
after = [n for n in nas_files() if n.endswith(".zip")]
check("remote retention removed the extras", removed == before - 3,
      "%d removed of %d" % (removed, before))
check("keeps the configured 3 on the NAS", len(after) == 3, str(after))
check("and keeps the NEWEST three", after == sorted(after)[-3:], str(after))
check("local retention is untouched by that",
      len([n for n in os.listdir(ARCHIVES) if n.endswith(".zip")]) >= 2)

# ---- the scheduled command
with app.app_context():
    BackupRun.query.delete()
    db.session.commit()
    due, why = scheduled_run_due(now=datetime.utcnow().replace(hour=1,
                                                              minute=0))
    check("not due before the configured time", due is False, why)
    due, why = scheduled_run_due(now=datetime.utcnow().replace(hour=3,
                                                              minute=0))
    check("due once the time has passed", due is True, why)

# THE CLI READS THE REAL CLOCK, so the schedule has to be a time that
# has certainly passed however early in the day this test is run. With
# the 02:30 default it passed all afternoon and failed between midnight
# and half past two, which is a test that reports on the hour rather
# than on the code.
with app.app_context():
    for key, value in (("schedule", "00:01"), ("schedule_tz", "uk")):
        row = Block.query.filter_by(key=SFTP_KEYS[key]).first()
        if row is None:
            row = Block(group="site", key=SFTP_KEYS[key], label=key,
                        kind="text")
            db.session.add(row)
        row.value = value
    db.session.commit()
runner = app.test_cli_runner()
res = runner.invoke(args=["run-scheduled-backup"])
check("scheduled run works", res.exit_code == 0, res.output[-300:])
check("it backed up and sent", "Sent as" in res.output, res.output[-300:])
with app.app_context():
    runs = BackupRun.query.filter_by(reason="scheduled").all()
    check("one scheduled run recorded", len(runs) == 1, str(len(runs)))
    check("marked as transferred", runs[0].transfer_status == "ok")

res = runner.invoke(args=["run-scheduled-backup"])
check("RUNNING IT AGAIN DOES NOTHING", "Nothing to do" in res.output,
      res.output[-200:])
with app.app_context():
    check("still only one scheduled run",
          BackupRun.query.filter_by(reason="scheduled").count() == 1)
res = runner.invoke(args=["run-scheduled-backup"])
check("and again", "Nothing to do" in res.output)

# a scheduled backup whose transfer failed is retried, then left alone
FakeSFTP.fail_on_put = IOError("NAS unplugged")
with app.app_context():
    BackupRun.query.delete()
    db.session.commit()
res = runner.invoke(args=["run-scheduled-backup"])
check("a failed transfer exits non-zero", res.exit_code == 1,
      res.output[-200:])
with app.app_context():
    run = BackupRun.query.filter_by(reason="scheduled").first()
    check("it tried twice", run.transfer_attempts == SFTP_MAX_ATTEMPTS,
          str(run.transfer_attempts))
    due, why = scheduled_run_due()
    check("and then leaves it until tomorrow", due is False, why)
res = runner.invoke(args=["run-scheduled-backup"])
check("so a later cron tick does nothing", "Nothing to do" in res.output,
      res.output[-200:])
FakeSFTP.fail_on_put = None

# ---- switching it off stops transfers, keeps local backups
save_settings(enabled=None, password="")
with app.app_context():
    check("transfers off", sftp_ready() is False)
    before_nas = len(nas_files())
    run = run_backup(reason="manual")
    check("a local backup still works", run.status == "ok")
    check("but nothing is sent", upload_backup(run) is False)
    check("and it is not marked as transferred",
          run.transfer_status == "none")
check("the NAS is untouched", len(nas_files()) == before_nas)

# ---- THE SCHEDULE IS A BRITISH TIME -----------------------------------
# It used to be read as UTC while being typed by somebody in Enfield, so
# a 19:36 schedule ran at 20:36 their time for seven months of the year
# and read as the schedule simply not working.
print()
print("---- the backup schedule, in UK local time")
from datetime import date                                      # noqa: E402
from app import (uk_wall_as_utc, uk_clock_change,              # noqa: E402
                 schedule_parts, schedule_in_uk, scheduled_run_due,
                 SFTP_KEYS, SFTP_DEFAULT_SCHEDULE, utc_as_uk)


def set_sftp(key, value):
    with app.app_context():
        b = Block.query.filter_by(key=SFTP_KEYS[key]).first()
        if b is None:
            b = Block(group="site", key=SFTP_KEYS[key], label=key,
                      kind="text")
            db.session.add(b)
        b.value = value
        db.session.commit()


# ---- an ordinary day, each side of the clock change
for when, wall, expect_utc, why in (
        (date(2026, 6, 1), "02:30", "01:30", "in summer, BST is an hour "
                                             "ahead of UTC"),
        (date(2026, 1, 15), "02:30", "02:30", "in winter, GMT is UTC"),
        (date(2026, 7, 4), "19:36", "18:36", "the evening time from the "
                                             "report")):
    hour, minute = schedule_parts(wall)
    got = uk_wall_as_utc(when, hour, minute)
    check("%s at %s British time is %s UTC — %s"
          % (when, wall, expect_utc, why),
          got.strftime("%H:%M") == expect_utc, got.strftime("%H:%M"))
    check("...and reads back as the time that was typed",
          utc_as_uk(got).strftime("%H:%M") == wall,
          utc_as_uk(got).strftime("%H:%M"))

# ---- SPRING: the hour that does not happen
# At 01:00 GMT the clocks go to 02:00 BST. Nothing between 01:00 and
# 01:59 exists that morning.
SPRING = date(2027, 3, 28)
check("the clocks are found to change that morning, without hard-coding "
      "the date", uk_clock_change(SPRING) == datetime(2027, 3, 28, 1, 0),
      str(uk_clock_change(SPRING)))
check("an ordinary day has no clock change",
      uk_clock_change(date(2026, 6, 1)) is None)

for wall in ("01:00", "01:30", "01:59"):
    hour, minute = schedule_parts(wall)
    got = uk_wall_as_utc(SPRING, hour, minute)
    check("SPRING: %s does not exist, so it runs at the change itself"
          % wall, got == datetime(2027, 3, 28, 1, 0), str(got))
    check("...which is %s British time, the first moment the hour exists"
          % "02:00", utc_as_uk(got).strftime("%H:%M") == "02:00",
          utc_as_uk(got).strftime("%H:%M"))

check("SPRING: a time before the gap is untouched",
      uk_wall_as_utc(SPRING, 0, 30) == datetime(2027, 3, 28, 0, 30),
      str(uk_wall_as_utc(SPRING, 0, 30)))
check("SPRING: a time after the gap is BST, so an hour back in UTC",
      uk_wall_as_utc(SPRING, 2, 30) == datetime(2027, 3, 28, 1, 30),
      str(uk_wall_as_utc(SPRING, 2, 30)))

# IT RUNS ONCE on that morning, not never and not twice.
set_sftp("schedule", "01:30")
set_sftp("schedule_tz", "uk")
with app.app_context():
    BackupRun.query.delete()
    db.session.commit()
    before = datetime(2027, 3, 28, 0, 45)     # 00:45 GMT, before the gap
    due, why = scheduled_run_due(now=before)
    check("SPRING: not due before the clocks change", due is False, why)
    at_change = datetime(2027, 3, 28, 1, 0)   # 02:00 BST
    due, why = scheduled_run_due(now=at_change)
    check("SPRING: DUE the moment the hour exists", due is True, why)
    # Once it has run, it does not run again that day.
    db.session.add(BackupRun(reason="scheduled", status="ok",
                             transfer_status="ok",
                             started_at=at_change))
    db.session.commit()
    due, why = scheduled_run_due(now=datetime(2027, 3, 28, 3, 0))
    check("SPRING: and does NOT run a second time later that morning",
          due is False, why)

# ---- AUTUMN: the hour that happens twice
# At 02:00 BST the clocks go back to 01:00 GMT, so 01:00-01:59 comes
# round again an hour later.
AUTUMN = date(2026, 10, 25)
check("the autumn change is found too",
      uk_clock_change(AUTUMN) == datetime(2026, 10, 25, 1, 0),
      str(uk_clock_change(AUTUMN)))
got = uk_wall_as_utc(AUTUMN, 1, 30)
check("AUTUMN: 01:30 uses the FIRST of the two, which is BST",
      got == datetime(2026, 10, 25, 0, 30), str(got))

set_sftp("schedule", "01:30")
with app.app_context():
    BackupRun.query.delete()
    db.session.commit()
    first = datetime(2026, 10, 25, 0, 30)     # 01:30 BST
    second = datetime(2026, 10, 25, 1, 30)    # 01:30 GMT, the repeat
    check("AUTUMN: both really are 01:30 British time",
          utc_as_uk(first).strftime("%H:%M") == "01:30"
          and utc_as_uk(second).strftime("%H:%M") == "01:30",
          "%s / %s" % (utc_as_uk(first), utc_as_uk(second)))
    due, why = scheduled_run_due(now=datetime(2026, 10, 25, 0, 15))
    check("AUTUMN: not due before the first 01:30", due is False, why)
    due, why = scheduled_run_due(now=first)
    check("AUTUMN: due at the first 01:30", due is True, why)
    db.session.add(BackupRun(reason="scheduled", status="ok",
                             transfer_status="ok", started_at=first))
    db.session.commit()
    due, why = scheduled_run_due(now=second)
    check("AUTUMN: NOT DUE AGAIN at the second 01:30 — once, not twice",
          due is False, why)
    due, why = scheduled_run_due(now=datetime(2026, 10, 25, 23, 0))
    check("AUTUMN: and not again later that day", due is False, why)

# ---- the UK date, not the UTC one
# Between midnight and 01:00 BST the two disagree, and using the UTC date
# would ask whether YESTERDAY's run had happened.
set_sftp("schedule", "23:30")
with app.app_context():
    BackupRun.query.delete()
    db.session.commit()
    # 00:15 UTC on 2 July is 01:15 BST on 2 July; the 23:30 run belongs
    # to the 1st and has already been done.
    db.session.add(BackupRun(reason="scheduled", status="ok",
                             transfer_status="ok",
                             started_at=datetime(2026, 7, 1, 22, 30)))
    db.session.commit()
    due, why = scheduled_run_due(now=datetime(2026, 7, 2, 0, 15))
    check("after midnight BST it does not re-run yesterday's backup",
          due is False, why)

# ---- MIGRATING AN EXISTING UTC VALUE
print()
print("---- the stored value, which used to be UTC")
check("an unmarked value is read as UTC and converted, in summer",
      schedule_in_uk("19:36", "", on=date(2026, 7, 4)) == "20:36",
      schedule_in_uk("19:36", "", on=date(2026, 7, 4)))
check("...and is unchanged in winter, when the two agree",
      schedule_in_uk("19:36", "", on=date(2026, 1, 4)) == "19:36",
      schedule_in_uk("19:36", "", on=date(2026, 1, 4)))
check("A VALUE ALREADY MARKED uk IS LEFT ALONE",
      schedule_in_uk("19:36", "uk", on=date(2026, 7, 4)) == "19:36")
check("...so migrating twice cannot shift it twice",
      schedule_in_uk(schedule_in_uk("19:36", "", on=date(2026, 7, 4)),
                     "uk", on=date(2026, 7, 4)) == "20:36")

# The whole point: the backup goes on happening at the moment it happens
# now, rather than moving an hour without anybody being told.
set_sftp("schedule", "19:36")
set_sftp("schedule_tz", "")
with app.app_context():
    cfg = sftp_settings()
check("AN UNMIGRATED SITE STILL SHOWS THE TIME ITS BACKUP HAPPENS",
      cfg["schedule"] == schedule_in_uk("19:36", ""), cfg["schedule"])
check("...and the page can say it has not been settled yet",
      cfg["schedule_migrated"] is False)

runner = app.test_cli_runner()
out = runner.invoke(args=["migrate-backup-schedule"]).output
check("the migration command reports what it did",
      "British time" in out or "GMT today" in out, out.strip()[:160])
with app.app_context():
    cfg = sftp_settings()
    stored = Block.query.filter_by(key=SFTP_KEYS["schedule"]).first().value
check("...and stores a UK local time from then on",
      cfg["schedule_migrated"] is True and stored == cfg["schedule"],
      "%s / %s" % (stored, cfg["schedule"]))
check("...leaving the schedule where the admin's backup already ran",
      stored == schedule_in_uk("19:36", ""), stored)
out2 = runner.invoke(args=["migrate-backup-schedule"]).output
check("RUNNING IT TWICE DOES NOT SHIFT IT AGAIN",
      "Already done" in out2, out2.strip()[:120])
with app.app_context():
    check("...and the value is untouched",
          Block.query.filter_by(
              key=SFTP_KEYS["schedule"]).first().value == stored)

# ---- the page says British time, nowhere says UTC
set_sftp("schedule_tz", "uk")
set_sftp("schedule", "02:30")
panel = client.get("/admin/features").data.decode("utf-8")
check("the field is labelled British time",
      "Daily transfer time (British time)" in panel)
check("...and no longer claims to be UTC",
      "Daily transfer time (UTC)" not in panel and "02:30 UTC" not in panel)
check("...and explains both clock-change mornings",
      "clocks go forward" in panel and "clocks go back" in panel
      and "as soon as the hour exists" in panel)
# Saving through the form is the other way a legacy value gets settled,
# so it has to set the marker as well as the time.
set_sftp("schedule_tz", "")
with app.app_context():
    stored_password_first = sftp_settings()["password_set"]
saved = client.post("/admin/settings/sftp", data={
    "host": "nas.example.org", "port": "22", "user": "backup",
    "path": "/volume1/backups/ebwa", "schedule": "03:15", "keep": "14",
    "password": "", "enabled": ""}, follow_redirects=True)
check("the settings form accepts a British time",
      saved.status_code == 200 and b"must look like" not in saved.data,
      saved.data.decode("utf-8", "replace")[:200])
with app.app_context():
    cfg = sftp_settings()
check("SAVING A TIME RECORDS THAT IT IS A BRITISH ONE",
      cfg["schedule"] == "03:15" and cfg["schedule_migrated"] is True,
      "%s / %s" % (cfg["schedule"], cfg["schedule_migrated"]))
check("...so it is not converted a second time on the next read",
      schedule_in_uk(cfg["schedule"], "uk", on=date(2026, 7, 4)) == "03:15")

# The last-run time was already shown in UK local, through the same
# uk_datetime filter as every other admin timestamp. Pinned so it stays
# that way rather than being assumed.
with app.app_context():
    BackupRun.query.delete()
    db.session.add(BackupRun(reason="manual", status="ok",
                             filename="x.zip", size_bytes=10, file_count=1,
                             started_at=datetime(2026, 7, 4, 18, 36),
                             finished_at=datetime(2026, 7, 4, 18, 37)))
    db.session.commit()
panel = client.get("/admin/features").data.decode("utf-8")
check("THE LAST BACKUP IS SHOWN IN BRITISH TIME, not UTC",
      "19:36" in panel and "04 Jul 2026, 18:36" not in panel,
      "expected 19:36 (BST) for an 18:36 UTC row")

# ---- teardown
with app.app_context():
    db.session.remove()
    db.engine.dispose()
shutil.rmtree(SANDBOX, ignore_errors=True)
check("sandbox removed", not os.path.isdir(SANDBOX))
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
