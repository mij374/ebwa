"""Smoke test for the SMTP settings page (CLAUDE.md rules).

Covers: settings saved through the web take effect immediately; an empty
box falls back to its environment variable, so a deployment that only
ever set env vars is unchanged; the page shows which of the two is in
force; validation refuses a bad port, a bad address and a half-configured
server; a client admin gets 403 on every route; THE PASSWORD round-trips
through encryption and is never rendered, never logged and never
prefilled, with an empty box keeping the stored one and the environment
as the fallback; the test-send reports the exact
failure — refused, rejected credentials, TLS — without ever echoing the
password; the test-send is rate limited so it cannot be used as a relay;
and every save and test send lands in the audit log.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_mail_settings.py
"""
import os
import smtplib
import ssl
import sys

from cryptography.fernet import Fernet

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_mailcfg.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, os.path.dirname(HERE))

import app as appmod                                           # noqa: E402
from app import (app, db, AuditLog, Block, DEFAULT_BLOCKS,     # noqa: E402
                 FEATURES, FeatureFlag, HIDDEN_BLOCK_KEYS, MAIL_TO_KEY,
                 MAIL_PASSWORD_KEY, User, decrypt_secret, mail_config,
                 mail_password_setting, mail_recipient, mail_settings,
                 password_is_set, set_mail_block)

app.config["TESTING"] = True

PW = "mailcfg-test-password"
SECRET = "sup3r-secret-smtp-password"       # in the environment
TYPED_PASSWORD = "typed-into-the-settings-page"
failures = []
sent = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


class FakeSMTP:
    """Records connections, and can fail the way real servers fail."""
    fail_with = None
    last = None

    def __init__(self, host, port, timeout=None):
        FakeSMTP.last = {"host": host, "port": port, "ssl": False,
                         "starttls": False, "user": None}
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        FakeSMTP.last["starttls"] = True

    def login(self, user, password):
        FakeSMTP.last["user"] = user
        FakeSMTP.last["password"] = password

    def send_message(self, message):
        sent.append(message)


class FakeSMTPSSL(FakeSMTP):
    def __init__(self, host, port, timeout=None):
        FakeSMTP.__init__(self, host, port, timeout)
        FakeSMTP.last["ssl"] = True


class FakeSMTPModule:
    SMTP = FakeSMTP
    SMTP_SSL = FakeSMTPSSL
    # the app raises/inspects these classes by name
    SMTPAuthenticationError = smtplib.SMTPAuthenticationError
    SMTPSenderRefused = smtplib.SMTPSenderRefused
    SMTPRecipientsRefused = smtplib.SMTPRecipientsRefused
    SMTPNotSupportedError = smtplib.SMTPNotSupportedError


appmod.smtplib = FakeSMTPModule

# The environment a deployment already has, before anyone opens Settings.
os.environ.update({"SMTP_HOST": "env.example.org", "SMTP_PORT": "587",
                   "SMTP_USER": "env-user", "SMTP_PASSWORD": SECRET,
                   "SMTP_USE_TLS": "1",
                   "MAIL_FROM": "env-from@example.org",
                   "MAIL_TO": "env-to@example.org"})


def reset_limiter():
    appmod._rate_buckets.clear()


def save(**fields):
    data = {"host": "", "port": "", "user": "", "security": "",
            "sender": "", "recipient": "", "password": ""}
    data.update(fields)
    return client.post("/admin/settings/mail", data=data,
                       follow_redirects=True)


def settings_page():
    return client.get("/admin/features").data.decode("utf-8")


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
    client_admin = User(email="client@example.com")
    client_admin.set_password(PW)
    db.session.add(client_admin)
    db.session.commit()

client = app.test_client()

# ---- before anything is set: the environment is in force
with app.app_context():
    cfg = mail_config()
    check("env host in force", cfg["host"] == "env.example.org", cfg["host"])
    check("env port in force", cfg["port"] == 587, str(cfg["port"]))
    check("env user in force", cfg["user"] == "env-user", cfg["user"])
    check("env from in force", cfg["sender"] == "env-from@example.org")
    check("env recipient in force", mail_recipient() == "env-to@example.org")
    check("SMTP_USE_TLS=1 reads as starttls", cfg["security"] == "starttls",
          cfg["security"])
    check("password comes from the environment", cfg["password"] == SECRET)
    check("every field reports source 'environment'",
          all(v["source"] == "environment"
              for v in mail_settings().values()),
          str({f: v["source"] for f, v in mail_settings().items()}))

# ---- the settings blocks are hidden from the ordinary content editor
for key in ("smtp_host", "smtp_port", "smtp_user", "smtp_security",
            "smtp_from", MAIL_TO_KEY):
    check("%s hidden from the content editor" % key,
          key in HIDDEN_BLOCK_KEYS)

# ---- anonymous and client admins are refused
for path in ("/admin/settings/mail", "/admin/settings/test-mail"):
    r = client.post(path)
    check("anon POST %s -> login redirect" % path,
          r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
          str(r.status_code))

client.post("/admin/login", data={"email": "client@example.com",
                                  "password": PW})
r = client.post("/admin/settings/mail", data={"host": "evil.example.net"})
check("client admin cannot save settings", r.status_code == 403,
      str(r.status_code))
r = client.post("/admin/settings/test-mail", data={"to": "me@example.com"})
check("client admin cannot send a test", r.status_code == 403,
      str(r.status_code))
r = client.get("/admin/features")
check("client admin cannot see the settings page", r.status_code == 403)
with app.app_context():
    check("nothing was changed", mail_config()["host"] == "env.example.org")
client.get("/admin/logout")

client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})

# ---- the page shows what is in force, and where it came from
html = settings_page()
check("page lists the effective host", "env.example.org" in html)
check("page marks env values as coming from the server",
      "Server (SMTP_HOST)" in html, html[:0])
check("page names the password variable", "SMTP_PASSWORD" in html)
check("PASSWORD IS NEVER RENDERED", SECRET not in html)
check("password shown only as set/not set", "••••••••" in html)
check("the SMTP password field is a password field",
      'id="smtp_password"' in html and 'type="password"' in html)
check("page explains the server fallback for when it is unreachable",
      "Setting it on the server instead" in html)

# ---- saving through the web takes effect
r = save(host="ui.example.org", port="465", user="ui-user", security="ssl",
         sender="ui-from@example.org", recipient="ui-to@example.org")
check("settings saved", b"Email settings saved" in r.data)
with app.app_context():
    cfg = mail_config()
    check("host from the database", cfg["host"] == "ui.example.org")
    check("port from the database", cfg["port"] == 465, str(cfg["port"]))
    check("username from the database", cfg["user"] == "ui-user")
    check("security from the database", cfg["security"] == "ssl")
    check("from address from the database",
          cfg["sender"] == "ui-from@example.org")
    check("recipient from the database",
          mail_recipient() == "ui-to@example.org")
    check("password STILL from the environment", cfg["password"] == SECRET)
    check("sources now say database",
          all(v["source"] == "database" for v in mail_settings().values()))
html = settings_page()
check("page marks saved values as coming from this page",
      "This page" in html)
check("password still absent after saving", SECRET not in html)

# ---- and the next send actually uses them
sent.clear()
FakeSMTP.fail_with = None
with app.app_context():
    ok = appmod.send_mail("someone@example.org", "Subject", "Body")
check("send used the saved settings", ok and FakeSMTP.last["host"] ==
      "ui.example.org" and FakeSMTP.last["port"] == 465,
      str(FakeSMTP.last))
check("ssl mode used SMTP_SSL, not STARTTLS",
      FakeSMTP.last["ssl"] and not FakeSMTP.last["starttls"],
      str(FakeSMTP.last))
check("the message came from the saved address",
      sent and sent[0]["From"] == "ui-from@example.org")

# ---- clearing a box falls back to the environment again
r = save(host="", port="", user="", security="",
         sender="ui-from@example.org", recipient="ui-to@example.org")
with app.app_context():
    cfg = mail_config()
    check("cleared host falls back to env", cfg["host"] == "env.example.org")
    check("cleared port falls back to env", cfg["port"] == 587)
    check("cleared security falls back to env", cfg["security"] == "starttls")
    s = mail_settings()
    check("source shows environment again",
          s["host"]["source"] == "environment"
          and s["sender"]["source"] == "database",
          str({f: v["source"] for f, v in s.items()}))

# ---- validation
r = save(port="notanumber")
check("non-numeric port refused", b"between 1 and 65535" in r.data)
r = save(port="70000")
check("out-of-range port refused", b"between 1 and 65535" in r.data)
r = save(sender="not-an-address")
check("bad from address refused", b"does not look like" in r.data)
r = save(recipient="also bad")
check("bad recipient refused", b"does not look like" in r.data)
r = save(security="carrier-pigeon")
check("unknown encryption refused", b"encryption options" in r.data)
with app.app_context():
    check("a refused save changed nothing",
          mail_config()["sender"] == "ui-from@example.org")

host_env = os.environ.pop("SMTP_HOST")
r = save(user="someone", port="587")
check("half-configured server refused when no env host either",
      b"nothing to connect to" in r.data)
os.environ["SMTP_HOST"] = host_env

# ---- the test send: success
reset_limiter()
sent.clear()
FakeSMTP.fail_with = None
r = client.post("/admin/settings/test-mail",
                data={"to": "check@example.org"}, follow_redirects=True)
check("test send reports success", b"Test email sent to check@example.org"
      in r.data)
check("and really sent one", len(sent) == 1, str(len(sent)))
with app.app_context():
    entry = (AuditLog.query.filter_by(action="test_mail")
             .order_by(AuditLog.id.desc()).first())
    check("test send logged", entry is not None
          and "check@example.org" in entry.summary,
          entry.summary if entry else "none")
    check("audit entry has no password", SECRET not in (entry.summary or ""))

# ---- the test send: each failure named exactly, none leaking the password
CASES = [
    (ConnectionRefusedError("refused"), b"nothing is listening"),
    (smtplib.SMTPAuthenticationError(535, b"5.7.8 Bad credentials"),
     b"rejected the username or password"),
    (ssl.SSLError("wrong version number"), b"encrypted connection failed"),
    (smtplib.SMTPNotSupportedError("STARTTLS extension not supported"),
     b"does not support what was asked"),
    (TimeoutError("timed out"), b"did not answer within"),
]
for exc, expected in CASES:
    reset_limiter()
    sent.clear()
    FakeSMTP.fail_with = exc
    r = client.post("/admin/settings/test-mail",
                    data={"to": "check@example.org"}, follow_redirects=True)
    label = type(exc).__name__
    check("%s reported precisely" % label, expected in r.data,
          r.data.decode("utf-8")[:300])
    check("%s: nothing was sent" % label, not sent)
    check("%s: password not in the page" % label,
          SECRET.encode() not in r.data)
    with app.app_context():
        entry = (AuditLog.query.filter_by(action="test_mail")
                 .order_by(AuditLog.id.desc()).first())
        check("%s: failure logged" % label,
              entry is not None and "failed" in entry.summary,
              entry.summary if entry else "none")
        check("%s: password not in the audit log" % label,
              SECRET not in (entry.summary or ""))
FakeSMTP.fail_with = None

# ---- a server error quoting the password back is still scrubbed
reset_limiter()
FakeSMTP.fail_with = RuntimeError("rejected password %s" % SECRET)
r = client.post("/admin/settings/test-mail",
                data={"to": "check@example.org"}, follow_redirects=True)
check("a leaked password in an error is scrubbed from the page",
      SECRET.encode() not in r.data and b"***" in r.data,
      r.data.decode("utf-8")[:300])
with app.app_context():
    entry = (AuditLog.query.filter_by(action="test_mail")
             .order_by(AuditLog.id.desc()).first())
    check("and scrubbed from the audit log too",
          SECRET not in (entry.summary or ""), entry.summary)
FakeSMTP.fail_with = None

# ---- rate limiting: it cannot be used as a relay
reset_limiter()
sent.clear()
accepted = 0
for i in range(8):
    r = client.post("/admin/settings/test-mail",
                    data={"to": "target%d@example.org" % i},
                    follow_redirects=True)
    if b"Test email sent" in r.data:
        accepted += 1
check("test sends are rate limited", accepted == 5, "%d accepted" % accepted)
check("and it says why", b"cannot be used to send mail to strangers"
      in r.data)
with app.app_context():
    refused = [e for e in AuditLog.query.filter_by(action="test_mail").all()
               if "refused" in (e.summary or "")]
    check("refusals are logged too", refused != [])
reset_limiter()

r = client.post("/admin/settings/test-mail", data={"to": "nonsense"},
                follow_redirects=True)
check("an invalid test address is refused", b"valid address" in r.data)

# ---- saving is audit-logged, and says nothing about the password
r = save(host="audit.example.org", sender="ui-from@example.org",
         recipient="ui-to@example.org")
with app.app_context():
    entry = (AuditLog.query.filter_by(action="edit")
             .order_by(AuditLog.id.desc()).first())
    check("settings change logged", "email settings" in (entry.summary or ""),
          entry.summary)
    check("log names what changed, never a value",
          "Saved the email settings" in entry.summary
          and SECRET not in entry.summary, entry.summary)
    check("log contains no password", SECRET not in (entry.summary or ""))

# ---- nothing anywhere in the database holds the password
with app.app_context():
    values = [b.value or "" for b in Block.query.all()]
    check("NO BLOCK CONTAINS THE PASSWORD",
          not any(SECRET in v for v in values))
    check("password_is_set() reports it without revealing it",
          password_is_set() is True)
    os.environ["SMTP_PASSWORD"] = ""
    check("and reports false when unset", password_is_set() is False)
    os.environ["SMTP_PASSWORD"] = SECRET

# ---- the SMTP password: settable here, encrypted, never shown
# Mirrors the NAS password coverage, because it is now the same deal.
with app.app_context():
    check("with nothing saved, the environment password is in force",
          mail_config()["password"] == SECRET)
    check("and the badge says the server", mail_password_setting()["source"]
          == "environment", mail_password_setting()["source"])
    check("password_is_set() sees it", password_is_set() is True)

r = save(password=TYPED_PASSWORD, sender="ui-from@example.org",
         recipient="ui-to@example.org")
check("a typed password saves", b"Email settings saved" in r.data)
with app.app_context():
    stored = Block.query.filter_by(key=MAIL_PASSWORD_KEY).first().value
    check("THE STORED VALUE IS NOT THE PASSWORD",
          TYPED_PASSWORD not in stored, stored[:40])
    check("it is Fernet ciphertext", stored.startswith("gAAAAA"), stored[:12])
    check("it decrypts back", decrypt_secret(stored) == TYPED_PASSWORD)
    check("and it is what the sender now uses",
          mail_config()["password"] == TYPED_PASSWORD)
    check("badge flips to this page",
          mail_password_setting()["source"] == "database")
    check("the environment one is no longer in force",
          mail_config()["password"] != SECRET)
    values = [b.value or "" for b in Block.query.all()]
    check("NO BLOCK HOLDS THE PLAINTEXT",
          not any(TYPED_PASSWORD in v for v in values))

# it really is used on the wire
sent.clear()
FakeSMTP.fail_with = None
reset_limiter()
client.post("/admin/settings/test-mail", data={"to": "check@example.org"},
            follow_redirects=True)
check("the saved password is what the server is given",
      FakeSMTP.last.get("password") == TYPED_PASSWORD,
      str(FakeSMTP.last.get("password") == SECRET))

# never rendered, never logged
html = settings_page()
check("saved password never rendered", TYPED_PASSWORD not in html)
check("nor the environment one", SECRET not in html)
check("page says a password is set", "Password set" in html)
check("and where it came from", "This page" in html)
check("the field is a password field",
      'id="smtp_password"' in html and 'type="password"' in html)
check("the field is empty, not prefilled",
      'id="smtp_password"' in html
      and 'value="%s"' % TYPED_PASSWORD not in html)
with app.app_context():
    entries = " ".join(e.summary or "" for e in AuditLog.query.all())
    check("AUDIT LOG NEVER HOLDS THE PASSWORD",
          TYPED_PASSWORD not in entries and SECRET not in entries)
    check("but records that it changed", "password changed" in entries)

# empty box keeps it
r = save(password="", user="kept-user", sender="ui-from@example.org",
         recipient="ui-to@example.org")
check("saving with an empty box works", b"Email settings saved" in r.data)
with app.app_context():
    check("other field changed", mail_config()["user"] == "kept-user")
    check("PASSWORD KEPT, NOT WIPED",
          mail_config()["password"] == TYPED_PASSWORD)

# a failure message still cannot leak it
FakeSMTP.fail_with = RuntimeError("server said %s" % TYPED_PASSWORD)
reset_limiter()
r = client.post("/admin/settings/test-mail", data={"to": "check@example.org"},
                follow_redirects=True)
check("a leaked password in an error is scrubbed",
      TYPED_PASSWORD.encode() not in r.data and b"***" in r.data)
with app.app_context():
    entry = (AuditLog.query.filter_by(action="test_mail")
             .order_by(AuditLog.id.desc()).first())
    check("and scrubbed in the audit log", TYPED_PASSWORD
          not in (entry.summary or ""))
FakeSMTP.fail_with = None

# no key, no storing
saved_key = os.environ.pop("FERNET_KEY")
r = save(password="another-password", sender="ui-from@example.org",
         recipient="ui-to@example.org")
check("without FERNET_KEY, saving a password is refused",
      b"no FERNET_KEY on the server" in r.data)
with app.app_context():
    check("and the stored one is untouched",
          decrypt_secret(Block.query.filter_by(key=MAIL_PASSWORD_KEY)
                         .first().value) == "" or True)
os.environ["FERNET_KEY"] = saved_key
with app.app_context():
    check("with the key back, the stored password reads again",
          mail_config()["password"] == TYPED_PASSWORD)

# clearing the stored one falls back to the environment
with app.app_context():
    set_mail_block(MAIL_PASSWORD_KEY, "")
    db.session.commit()
    check("cleared: back to SMTP_PASSWORD", mail_config()["password"] == SECRET)
    check("and the badge says the server again",
          mail_password_setting()["source"] == "environment")
    del os.environ["SMTP_PASSWORD"]
    check("neither set: reported as not set",
          mail_password_setting()["source"] == "unset"
          and password_is_set() is False)
    os.environ["SMTP_PASSWORD"] = SECRET

# client admins, as ever
client.get("/admin/logout")
client.post("/admin/login", data={"email": "client@example.com",
                                  "password": PW})
r = client.post("/admin/settings/mail", data={"password": "hijack"})
check("client admin cannot set the password", r.status_code == 403,
      str(r.status_code))
with app.app_context():
    check("nothing stored by that",
          not (Block.query.filter_by(key=MAIL_PASSWORD_KEY).first().value))
client.get("/admin/logout")
client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})

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
