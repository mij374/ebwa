"""Smoke test for the contact form and the mail layer (CLAUDE.md rules).

Covers: a valid submission is saved AND emailed, with the enquirer as
Reply-To and no auto-reply to them; SMTP being down still saves the
enquiry and records the failure in the audit log, because a visitor's
question must not be lost to somebody else's mail server; the honeypot,
the minimum time-to-submit and the rate limiter all refuse without
telling a bot which one caught it; validation errors are useful; the
admin list is auth-gated, logs that personal data was viewed, and has no
CSV export; status changes and deletions are audit-logged; the recipient
address can be overridden from Settings by a super admin and by nobody
else; and switching the `contact_form` flag off hides the form while the
address, phone and map stay on the page.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_contact.py
"""
import os
import smtplib
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_contact.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

import app as appmod                                           # noqa: E402
from app import (app, db, AuditLog, Block, ContactMessage,     # noqa: E402
                 DEFAULT_BLOCKS, FEATURES, FeatureFlag, MAIL_TO_KEY,
                 MIN_FORM_SECONDS, User, mail_recipient)

app.config["TESTING"] = True

PW = "contact-test-password"
failures = []
sent = []            # every message the fake SMTP server accepted


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


class FakeSMTP:
    """Stands in for smtplib.SMTP, recording what it was asked to send."""
    fail_with = None                 # set to an exception to simulate a
    # broken server

    def __init__(self, host, port, timeout=None):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in_as = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in_as = user

    def send_message(self, message):
        sent.append(message)


class FakeSMTPModule:
    SMTP = FakeSMTP
    SMTP_SSL = FakeSMTP
    # The app names these classes when it works out WHY a send failed, so
    # a stand-in that lacks them is not a faithful stand-in.
    SMTPAuthenticationError = smtplib.SMTPAuthenticationError
    SMTPSenderRefused = smtplib.SMTPSenderRefused
    SMTPRecipientsRefused = smtplib.SMTPRecipientsRefused
    SMTPNotSupportedError = smtplib.SMTPNotSupportedError


appmod.smtplib = FakeSMTPModule
os.environ.update({"SMTP_HOST": "smtp.example.org", "SMTP_PORT": "587",
                   "SMTP_USER": "postbox", "SMTP_PASSWORD": "hunter2",
                   "SMTP_USE_TLS": "1",
                   "MAIL_FROM": "website@example.org",
                   "MAIL_TO": "enquiries@example.org"})


def get(path="/contact"):
    return client.get(path).data.decode("utf-8")


def set_flag(name, enabled):
    with app.app_context():
        FeatureFlag.query.filter_by(name=name).first().enabled = enabled
        db.session.commit()


def submit(name="Ayesha Rahman", email="ayesha@example.com", phone="",
           subject="Bengali classes", message="Do you run Saturday classes?",
           website="", age=MIN_FORM_SECONDS + 1):
    """Post the form as a person would, `age` seconds after loading it."""
    return client.post("/contact", data={
        "name": name, "email": email, "phone": phone, "subject": subject,
        "message": message, "website": website,
        "started": str(int(time.time()) - age)}, follow_redirects=True)


def reset_limiter():
    appmod._rate_buckets.clear()


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
    plain = User(email="client@example.com")
    plain.set_password(PW)
    db.session.add(plain)
    db.session.commit()

client = app.test_client()

# ---- the flag exists, and the page shows the form
check("contact_form is a feature flag",
      "contact_form" in appmod.FEATURE_DEFAULTS)
html = get()
check("contact page -> 200", client.get("/contact").status_code == 200)
check("form shown", 'name="message"' in html and "Send us a message" in html)
check("honeypot present but hidden", 'name="website"' in html
      and "hp-field" in html)
check("time-to-submit stamp present", 'name="started"' in html)
check("privacy line under the form", "privacy notice" in html
      and 'href="/privacy"' in html)
check("details still on the page too",
      "180 High Street" in html and "020 8804 4006" in html)

# ---- a real submission: saved, emailed, replies go to the enquirer
r = submit()
check("submission accepted", b"your message is with us" in r.data)
with app.app_context():
    msg = ContactMessage.query.first()
    check("enquiry saved", msg is not None)
    check("fields stored", msg.name == "Ayesha Rahman"
          and msg.email == "ayesha@example.com"
          and msg.subject == "Bengali classes"
          and msg.message == "Do you run Saturday classes?")
    check("status starts as new", msg.status == "new")
    check("ip recorded", msg.ip == "127.0.0.1", msg.ip)
check("exactly one email sent", len(sent) == 1, str(len(sent)))
mail = sent[0]
check("email went to the configured recipient",
      mail["To"] == "enquiries@example.org", mail["To"])
check("sent from MAIL_FROM", mail["From"] == "website@example.org")
check("Reply-To is the enquirer", mail["Reply-To"] == "ayesha@example.com",
      str(mail["Reply-To"]))
body = mail.get_content()
check("email carries the message", "Do you run Saturday classes?" in body)
check("email names the sender", "Ayesha Rahman" in body)
check("NO auto-reply to the enquirer",
      not any(m["To"] == "ayesha@example.com" for m in sent))

# ---- SMTP down: the enquiry survives, the failure is recorded
sent.clear()
FakeSMTP.fail_with = ConnectionRefusedError("connection refused")
reset_limiter()
r = submit(name="Karim Uddin", email="karim@example.com",
           message="Is the drop-in open on Fridays?")
check("visitor still thanked when email fails",
      b"your message is with us" in r.data)
check("nothing was sent", not sent)
with app.app_context():
    saved = ContactMessage.query.filter_by(name="Karim Uddin").first()
    check("ENQUIRY IS STILL SAVED", saved is not None)
    entry = (AuditLog.query.filter_by(action="mail_failed")
             .order_by(AuditLog.id.desc()).first())
    check("failure recorded in the audit log", entry is not None)
    if entry:
        check("audit says what failed and where it went",
              "enquiries@example.org" in entry.summary
              and "nothing is listening" in entry.summary, entry.summary)
        check("audit reassures that the message is kept",
              "saved" in entry.summary, entry.summary)
        check("audit leaks NO credentials",
              "hunter2" not in entry.summary
              and "postbox" not in entry.summary, entry.summary)
        check("audit leaks NO message text",
              "drop-in open on Fridays" not in entry.summary, entry.summary)
FakeSMTP.fail_with = None

# ---- unconfigured server: still saved, still logged, never a 500
sent.clear()
host, os.environ["SMTP_HOST"] = os.environ["SMTP_HOST"], ""
reset_limiter()
r = submit(name="Nadia Begum", email="nadia@example.com",
           message="Can I hire the hall?")
check("no SMTP configured: submission still works", r.status_code == 200
      and b"your message is with us" in r.data)
with app.app_context():
    check("no SMTP configured: enquiry still saved",
          ContactMessage.query.filter_by(name="Nadia Begum").first()
          is not None)
    check("no SMTP configured: logged as not configured",
          "not configured" in (AuditLog.query.filter_by(action="mail_failed")
                               .order_by(AuditLog.id.desc()).first().summary))
os.environ["SMTP_HOST"] = host

# ---- the spam traps
sent.clear()
reset_limiter()
before = None
with app.app_context():
    before = ContactMessage.query.count()
r = submit(name="Spam Bot", email="bot@example.com", website="http://spam")
check("honeypot: bot is thanked, like everyone else",
      b"your message is with us" in r.data)
with app.app_context():
    check("honeypot: nothing saved", ContactMessage.query.count() == before)
check("honeypot: nothing emailed", not sent)

reset_limiter()
r = submit(name="Too Fast", email="fast@example.com", age=0)
check("too quick: thanked, so a bot learns nothing",
      b"your message is with us" in r.data)
with app.app_context():
    check("too quick: nothing saved", ContactMessage.query.count() == before)
reset_limiter()
r = submit(name="Just Right", email="ok@example.com",
           age=MIN_FORM_SECONDS + 1)
with app.app_context():
    check("a human pace is accepted",
          ContactMessage.query.count() == before + 1)

# ---- validation
reset_limiter()
r = submit(name="", email="someone@example.com")
check("missing name refused", b"name and a valid email" in r.data)
r = submit(name="Someone", email="not-an-email")
check("bad email refused", b"name and a valid email" in r.data)
r = submit(name="Someone", email="someone@example.com", message="")
check("empty message refused", b"tell us how we can help" in r.data)
with app.app_context():
    check("none of those were saved",
          ContactMessage.query.count() == before + 1)

# ---- rate limiting
reset_limiter()
accepted = 0
for i in range(8):
    r = submit(name="Repeat %d" % i, email="repeat@example.com",
               message="Message %d" % i)
    if b"your message is with us" in r.data:
        accepted += 1
check("rate limiter stops a flood", accepted == 5, "%d accepted" % accepted)
check("and says why, kindly", b"give us a little time to reply" in r.data)
reset_limiter()

# ---- admin: auth-gated
for path, method in (("/admin/messages", "GET"),
                     ("/admin/messages/1/status", "POST"),
                     ("/admin/messages/1/delete", "POST")):
    r = client.open(path, method=method)
    check("anon %s %s -> login redirect" % (method, path),
          r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
          str(r.status_code))

client.post("/admin/login", data={"email": "client@example.com",
                                  "password": PW})
r = client.get("/admin/messages")
check("admin list -> 200", r.status_code == 200)
listing = r.data.decode("utf-8")
check("list shows an enquiry", "Ayesha Rahman" in listing)
check("unread rows are marked", "msg-unread" in listing)
check("reply link is a mailto to the enquirer",
      "mailto:ayesha@example.com" in listing)
check("reply quotes the enquiry",
      "you%20wrote" in listing or "you+wrote" in listing, listing[:0])
check("NO csv export offered", ".csv" not in listing)
check("nav badge counts unread", 'class="nav-badge"' in listing)
with app.app_context():
    viewed = (AuditLog.query.filter_by(action="view")
              .order_by(AuditLog.id.desc()).first())
    check("viewing personal data is logged", viewed is not None
          and "enquiry list" in (viewed.summary or ""),
          viewed.summary if viewed else "none")

# ---- there is genuinely no export route
check("no export endpoint exists",
      not any("messages" in rule.rule and "csv" in rule.rule
              for rule in app.url_map.iter_rules()))

# ---- status changes and deletion are logged
with app.app_context():
    target = ContactMessage.query.filter_by(name="Ayesha Rahman").first()
    target_id = target.id
r = client.post("/admin/messages/%d/status" % target_id,
                data={"status": "replied"}, follow_redirects=True)
check("status change accepted", b"Marked as replied." in r.data)
with app.app_context():
    check("status stored",
          db.session.get(ContactMessage, target_id).status == "replied")
    entry = (AuditLog.query.filter_by(action="status")
             .order_by(AuditLog.id.desc()).first())
    check("status change logged", entry is not None
          and "replied" in entry.summary, entry.summary if entry else "none")
    check("status log does not quote the message",
          "Saturday classes" not in (entry.summary or ""))
r = client.post("/admin/messages/%d/status" % target_id,
                data={"status": "banana"}, follow_redirects=True)
check("unknown status refused", b"Unknown status." in r.data)

r = client.get("/admin/messages?status=new")
check("filtering works", r.status_code == 200
      and b"Ayesha Rahman" not in r.data)

r = client.post("/admin/messages/%d/delete" % target_id,
                follow_redirects=True)
check("delete works", b"Message deleted." in r.data)
with app.app_context():
    check("row gone", db.session.get(ContactMessage, target_id) is None)
    entry = (AuditLog.query.filter_by(action="delete")
             .order_by(AuditLog.id.desc()).first())
    check("deletion logged", "enquiry from Ayesha Rahman" in entry.summary,
          entry.summary)

# ---- the recipient address: super admins only
r = client.post("/admin/settings/mail",
                data={"recipient": "hijack@example.com"})
check("a client admin cannot change the recipient", r.status_code == 403,
      str(r.status_code))
with app.app_context():
    check("recipient unchanged", mail_recipient() == "enquiries@example.org")
client.get("/admin/logout")

client.post("/admin/login", data={"email": "netbus@example.com",
                                  "password": PW})
r = client.get("/admin/features")
check("settings page shows the recipient", b"enquiries@example.org" in r.data)
check("settings page never shows the password", b"hunter2" not in r.data)
check("settings page says credentials live on the server",
      b"SMTP_PASSWORD" in r.data and b"not editable here" in r.data)
r = client.post("/admin/settings/mail",
                data={"recipient": "info@ebwa.org.uk"},
                follow_redirects=True)
check("super admin can change the recipient",
      b"Email settings saved" in r.data)
with app.app_context():
    check("override stored in a Block",
          Block.query.filter_by(key=MAIL_TO_KEY).first().value
          == "info@ebwa.org.uk")
    check("mail_recipient prefers it", mail_recipient() == "info@ebwa.org.uk")
sent.clear()
reset_limiter()
submit(name="After Switch", email="after@example.com",
       message="Testing the new address.")
check("the next enquiry goes to the new address",
      sent and sent[0]["To"] == "info@ebwa.org.uk",
      sent[0]["To"] if sent else "nothing sent")
r = client.post("/admin/settings/mail", data={"recipient": "nonsense"},
                follow_redirects=True)
check("an invalid address is refused", b"does not look like" in r.data)
r = client.post("/admin/settings/mail", data={"recipient": ""},
                follow_redirects=True)
with app.app_context():
    check("clearing it falls back to the environment",
          mail_recipient() == "enquiries@example.org")

# ---- the flag: form gone, contact page intact
set_flag("contact_form", False)
html = get()
check("flag off: page still works", client.get("/contact").status_code == 200)
check("flag off: form gone", 'name="message"' not in html)
check("flag off: address and phone still there",
      "180 High Street" in html and "020 8804 4006" in html)
check("flag off: map still there", "google.com/maps" in html)
reset_limiter()
r = client.post("/contact", data={"name": "Sneaky", "email": "s@example.com",
                                  "message": "Posting anyway",
                                  "started": "0"})
check("flag off: posting the form 404s", r.status_code == 404,
      str(r.status_code))
with app.app_context():
    check("flag off: nothing saved",
          ContactMessage.query.filter_by(name="Sneaky").first() is None)
    check("flag off: existing enquiries are untouched",
          ContactMessage.query.count() > 0)
check("flag off: the admin page still opens, so nothing is stranded",
      client.get("/admin/messages").status_code == 200)
set_flag("contact_form", True)
check("flag on again: the form is back", 'name="message"' in get())

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
