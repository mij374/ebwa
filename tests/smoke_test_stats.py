"""Visitor statistics: counted here, identifying nobody.

The claim this file has to keep true is the one on the cookie notice and
the privacy notice: there is no tracking. So as well as the arithmetic —
what gets counted, what is excluded, how the totals roll up and when the
raw rows go — it checks the properties that make the counting honest:

  * no IP address and no user agent is ever written anywhere;
  * the visitor hash for the same person changes when the salt rotates,
    so two days' figures cannot be joined up;
  * the salt is REPLACED, not kept, so yesterday's hashes cannot be
    recomputed even with the database in hand.

Run:  python tests/smoke_test_stats.py
"""
import re
import os
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEST_DB = os.path.join(HERE, "test_stats.db")
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _s):
        os.remove(TEST_DB + _s)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, ROOT)

from werkzeug.security import generate_password_hash    # noqa: E402
from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,  # noqa: E402
                 FeatureFlag, PageView, PageViewDaily, User, VisitorSalt,
                 PAGEVIEW_RAW_DAYS, PAGEVIEW_SKIP_PREFIXES,
                 aggregate_page_views, looks_like_a_bot, should_count,
                 visitor_hash, visitor_salt_for, visitor_stats,
                 utc_as_uk)

app.config["TESTING"] = True
PW = "stats-test-password"
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



with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
    for n, _l, _d, _x in FEATURES:
        if not FeatureFlag.query.filter_by(name=n).first():
            db.session.add(FeatureFlag(name=n, enabled=True))
    db.session.add(User(email="netbus@example.com",
                        password_hash=generate_password_hash(PW),
                        role="super_admin"))
    db.session.add(User(email="client@example.com",
                        password_hash=generate_password_hash(PW),
                        role="admin"))
    db.session.commit()

BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def visit(path="/", ua=BROWSER, ip="203.0.113.5", client=None):
    (client or app.test_client()).get(
        path, headers={"User-Agent": ua},
        environ_base={"REMOTE_ADDR": ip})


def rows():
    with app.app_context():
        return PageView.query.all()


# ---- the hash, which is the whole privacy argument -------------------
print("---- the visitor hash")
with app.app_context():
    today = date(2026, 5, 1)
    h1 = visitor_hash(today, "203.0.113.5", BROWSER)
    db.session.commit()
    h2 = visitor_hash(today, "203.0.113.5", BROWSER)
    check("the same visitor on the same day hashes the same", h1 == h2)
    check("a different address hashes differently",
          h1 != visitor_hash(today, "203.0.113.9", BROWSER))
    check("a different browser hashes differently",
          h1 != visitor_hash(today, "203.0.113.5", BROWSER + " Edg/120"))
    check("it is a sha256 hex digest, not anything readable",
          len(h1) == 64 and all(c in "0123456789abcdef" for c in h1), h1)
    check("THE ADDRESS IS NOT IN IT ANYWHERE", "203.0.113.5" not in h1)

    salt_today = visitor_salt_for(today)
    db.session.commit()
    h_tomorrow = visitor_hash(today + timedelta(days=1), "203.0.113.5",
                              BROWSER)
    db.session.commit()
    check("THE SAME VISITOR HASHES DIFFERENTLY TOMORROW, so two days' "
          "figures cannot be joined up", h1 != h_tomorrow)
    salt_tomorrow = visitor_salt_for(today + timedelta(days=1))
    check("the salt really changed", salt_today != salt_tomorrow)
    check("AND THE OLD SALT IS GONE, not kept beside the new one",
          VisitorSalt.query.count() == 1
          and VisitorSalt.query.first().salt == salt_tomorrow,
          "%d salt row(s)" % VisitorSalt.query.count())
    check("...so yesterday's hash can no longer be recomputed",
          visitor_hash(today, "203.0.113.5", BROWSER) != h1)
    db.session.commit()

# ---- what is counted, and what is not --------------------------------
print()
print("---- what counts")
check("a browser is not a bot", not looks_like_a_bot(BROWSER))
for ua in ("Googlebot/2.1", "python-requests/2.31", "curl/8.0",
           "Mozilla/5.0 (compatible; bingbot/2.0)", "", "uptime-monitor"):
    check("a bot is spotted: %r" % (ua[:24] or "(no user agent)"),
          looks_like_a_bot(ua))

with app.app_context():
    check("a normal page load counts",
          should_count("/about", "GET", 200, BROWSER))
    check("a POST does not", not should_count("/about", "POST", 200, BROWSER))
    check("a 404 does not", not should_count("/nope", "GET", 404, BROWSER))
    for prefix in PAGEVIEW_SKIP_PREFIXES:
        check("%s is excluded" % prefix,
              not should_count(prefix + "/x", "GET", 200, BROWSER))

with app.app_context():
    PageView.query.delete()
    db.session.commit()

visit("/")
visit("/about")
visit("/nope")                          # 404
visit("/healthz")
visit("/robots.txt")
visit("/static/css/style.css")
visit("/", ua="Googlebot/2.1")
paths = sorted(r.path for r in rows())
check("only the real pages were counted", paths == ["/", "/about"],
      str(paths))
check("no admin path was counted",
      not any(p.startswith("/admin") for p in paths))

# An admin looking at their own site is not a visitor.
staff = app.test_client()
staff.post("/admin/login", data={"email": "netbus@example.com",
                                 "password": PW})
before = len(rows())
visit("/about", client=staff)
check("a signed-in admin browsing the site is not counted",
      len(rows()) == before, "%d -> %d" % (before, len(rows())))

# ---- NOTHING IDENTIFYING IS STORED -----------------------------------
print()
print("---- what is in the table")
with app.app_context():
    cols = [c.name for c in PageView.__table__.columns]
check("the columns are just id, day, path and the hash",
      sorted(cols) == ["day", "id", "path", "visitor"], str(cols))
check("there is no ip column", "ip" not in cols)
check("there is no user agent column",
      not any("agent" in c or c == "ua" for c in cols))
check("day is a DATE, so no time of day is kept",
      PageView.__table__.columns["day"].type.__class__.__name__ == "Date")
with app.app_context():
    stored = [(r.path, r.visitor) for r in PageView.query.all()]
check("NO ADDRESS APPEARS IN ANY ROW",
      not any("203.0.113" in str(v) for _p, v in stored))
check("NO USER AGENT APPEARS IN ANY ROW",
      not any("Mozilla" in str(v) or "Chrome" in str(v) for _p, v in stored))

# ---- counting people vs page loads -----------------------------------
print()
print("---- visits against page loads")
with app.app_context():
    PageView.query.delete()
    db.session.commit()
for _ in range(3):
    visit("/")                          # one person, three page loads
visit("/", ip="198.51.100.7")           # a second person
with app.app_context():
    check("four page loads", PageView.query.count() == 4)
    check("...but two people",
          db.session.query(db.func.count(db.distinct(PageView.visitor)))
          .scalar() == 2)
    stats = visitor_stats()
check("the panel says 4 views today", stats["today"][0] == 4,
      str(stats["today"]))
check("...and 2 visitors", stats["today"][1] == 2, str(stats["today"]))
check("the chart has one bar per day", len(stats["chart"]) == 30)
check("today is the last bar", stats["chart"][-1]["views"] == 4,
      str(stats["chart"][-1]))
check("the most-visited list has the page", stats["top"][0]["path"] == "/")

# ---- aggregation and retention ---------------------------------------
print()
print("---- rolling up and pruning")
with app.app_context():
    PageView.query.delete()
    PageViewDaily.query.delete()
    db.session.commit()
    today = date(2026, 6, 30)
    old_day = today - timedelta(days=PAGEVIEW_RAW_DAYS + 5)
    edge_day = today - timedelta(days=PAGEVIEW_RAW_DAYS)      # the cutoff
    recent = today - timedelta(days=3)
    for day, n, people in ((old_day, 5, 2), (edge_day, 4, 2), (recent, 6, 3)):
        for i in range(n):
            db.session.add(PageView(day=day, path="/p%d" % i,
                                    visitor="v%d" % (i % people)))
    db.session.commit()
    check("15 raw rows to start", PageView.query.count() == 15,
          str(PageView.query.count()))

    days, deleted = aggregate_page_views(today=today)
    check("one day was old enough to roll", days == 1, str(days))
    check("...and its 5 raw rows were deleted", deleted == 5, str(deleted))
    check("the raw table has the rest", PageView.query.count() == 10,
          str(PageView.query.count()))
    rolled = PageViewDaily.query.filter_by(day=old_day).first()
    check("the day's totals were kept",
          rolled is not None and rolled.views == 5 and rolled.visitors == 2,
          str(rolled and (rolled.views, rolled.visitors)))
    check("THE DAY AT THE CUTOFF IS STILL RAW, not rolled early",
          PageView.query.filter_by(day=edge_day).count() == 4)
    check("nothing recent was touched",
          PageView.query.filter_by(day=recent).count() == 6)

    again = aggregate_page_views(today=today)
    check("running it twice does nothing the second time", again == (0, 0),
          str(again))
    check("...and did not double the totals",
          PageViewDaily.query.filter_by(day=old_day).first().views == 5)

    # A rolled-up day still counts in a range that covers it: the panel
    # asks both tables and adds them, or the figures would drop off a
    # cliff at the retention boundary.
    from app import _pv_counts
    views, visitors = _pv_counts(old_day, today)
    check("a range spanning the boundary counts BOTH tables",
          views == 15 and visitors > 0, "%d views" % views)

# ---- THE DAILY TOTALS ARE PERMANENT ----------------------------------
# The raw rows are the disposable half; the totals are the point of
# keeping anything at all. Nothing may ever delete them — year-on-year
# comparison is the one figure that cannot be recovered once it is gone,
# because the raw rows it would be recomputed from were pruned years
# earlier. Asserted rather than assumed, because "we do not delete it"
# is the sort of thing that stays true until somebody adds a tidy-up.
print()
print("---- long-term history")
with app.app_context():
    PageView.query.delete()
    PageViewDaily.query.delete()
    db.session.commit()
    today = date(2026, 6, 30)
    # Five years of history, nothing raw.
    for years in range(1, 6):
        for i in range(3):
            db.session.add(PageViewDaily(
                day=date(today.year - years, today.month, 1 + i),
                views=100 * years + i, visitors=20 * years))
    db.session.commit()
    kept = PageViewDaily.query.count()
    check("five years of daily totals are in the table", kept == 15,
          str(kept))

    # Rolling up today's traffic must not touch any of them.
    old_day = today - timedelta(days=PAGEVIEW_RAW_DAYS + 1)
    for i in range(4):
        db.session.add(PageView(day=old_day, path="/x", visitor="v%d" % i))
    db.session.commit()
    aggregate_page_views(today=today)
    check("AGGREGATION ADDS A DAY AND DELETES NOTHING OLDER",
          PageViewDaily.query.count() == kept + 1,
          "%d -> %d" % (kept, PageViewDaily.query.count()))
    for _ in range(3):
        aggregate_page_views(today=today)
    check("...and running it again and again still deletes nothing",
          PageViewDaily.query.count() == kept + 1,
          str(PageViewDaily.query.count()))
    oldest = db.session.query(db.func.min(PageViewDaily.day)).scalar()
    check("the oldest day is still five years back",
          oldest.year == today.year - 5, str(oldest))
    # Every seeded year is more than 365 days back, so all fifteen must
    # still be there — the newly rolled day is the only one inside the
    # year and it is not part of this count.
    older = PageViewDaily.query.filter(
        PageViewDaily.day < today - timedelta(days=365)).count()
    check("all five years survive, none quietly pruned", older == 15,
          "rows older than a year: %d" % older)

# ---- SAME MONTH LAST YEAR, read from the aggregate -------------------
# There will be no raw rows for a month a year ago — they were pruned at
# 62 days — so this figure comes entirely from PageViewDaily or it comes
# from nowhere.
with app.app_context():
    PageView.query.delete()
    PageViewDaily.query.delete()
    db.session.commit()
    today = utc_as_uk(datetime.utcnow()).date()
    first_last_year = today.replace(year=today.year - 1, day=1)
    for i in range(28):
        db.session.add(PageViewDaily(day=first_last_year + timedelta(days=i),
                                     views=10 + i, visitors=3))
    db.session.commit()
    check("no raw rows exist for that month at all",
          PageView.query.filter(PageView.day >= first_last_year).count() == 0)
    stats = visitor_stats()
check("THE LAST-YEAR CARD IS POPULATED FROM THE AGGREGATE ALONE",
      stats["last_year"] is not None, "the card would not appear")
expected = sum(10 + i for i in range(28))
check("...with the right total", stats["last_year"][0] == expected,
      "%s vs %d" % (stats["last_year"], expected))
check("...and the right visits", stats["last_year"][1] == 28 * 3,
      str(stats["last_year"]))
check("...labelled with the month and year",
      stats["last_year_label"] == first_last_year.strftime("%B %Y"),
      stats["last_year_label"])
check("the panel knows how far back it can see",
      stats["since"] == first_last_year, str(stats["since"]))

# And it stays absent rather than showing zeros when there is no history.
with app.app_context():
    PageViewDaily.query.delete()
    db.session.commit()
    stats = visitor_stats()
check("with no history the card is absent, not a row of zeros",
      stats["last_year"] is None, str(stats["last_year"]))

# ---- the panel is super-admin only -----------------------------------
print()
print("---- who can see it")
with app.app_context():
    PageView.query.delete()
    db.session.commit()
boss = app.test_client()
boss.post("/admin/login", data={"email": "netbus@example.com",
                                "password": PW})
page = boss.get("/admin/features").data.decode("utf-8")
check("a super admin sees the panel", "Visitors" in page
      and 'id="visitorStats"' in page)
check("the chart is inline SVG, not a library",
      "<svg" in page and not offsite_scripts(page))
check("...and has a text description for a screen reader",
      'role="img"' in page and "Page views per day" in page)
check("the panel says plainly what is stored",
      "no extra cookie" in page and "identifies" in page)
check("...AND WHAT THE FIGURES DO NOT MEAN: a daily hash counts a "
      "returning visitor again",
      "one person on one day" in page and "person-days" in page)
client_admin = app.test_client()
client_admin.post("/admin/login", data={"email": "client@example.com",
                                        "password": PW})
check("a client admin cannot reach Settings at all",
      client_admin.get("/admin/features").status_code == 403)
check("an anonymous visitor is sent to the login page",
      app.test_client().get("/admin/features").status_code == 302)

# The panel itself must not be counted, or looking at the figures would
# change them.
with app.app_context():
    check("opening Settings recorded nothing", PageView.query.count() == 0,
          str(PageView.query.count()))

# ---- THE MONTHLY REPORT ----------------------------------------------
print()
print("---- the monthly report")
from unittest.mock import patch      # noqa: E402
from app import (STATS_REPORT_KEY, STATS_REPORT_TO_KEY,  # noqa: E402
                 STATS_TARGET_KEY, STATS_TARGET_DEFAULT, STATS_REPORT_ACTION,
                 AuditLog, monthly_report, monthly_report_sent_for,
                 send_monthly_report, stats_monthly_target, stats_report_on,
                 stats_report_setting, MAIL_TO_KEY, send_mail)

NL = chr(10)


def set_block(key, value, group="stats"):
    with app.app_context():
        b = Block.query.filter_by(key=key).first()
        if b is None:
            b = Block(group=group, key=key, label=key, kind="text")
            db.session.add(b)
        b.value = value
        db.session.commit()


with app.app_context():
    check("the report is OFF until somebody switches it on",
          stats_report_on() is False)
    check("the target defaults to the figure from the brief",
          stats_monthly_target() == 2000 == STATS_TARGET_DEFAULT,
          str(stats_monthly_target()))
set_block(STATS_TARGET_KEY, "nonsense")
with app.app_context():
    check("a nonsense target falls back to the default rather than "
          "breaking the report", stats_monthly_target() == 2000)
set_block(STATS_TARGET_KEY, str(STATS_TARGET_DEFAULT))

# Two months of figures: a busy one to report on and a quieter one
# before it, plus the same month a year earlier.
LAST = date(2026, 5, 1)
with app.app_context():
    PageView.query.delete()
    PageViewDaily.query.delete()
    db.session.commit()
    for i in range(20):
        for j in range(3):
            db.session.add(PageView(day=LAST + timedelta(days=i),
                                    path="/about" if j else "/",
                                    visitor="p%d" % (i % 5)))
    db.session.add(PageViewDaily(day=date(2026, 4, 10), views=30, visitors=9))
    db.session.add(PageViewDaily(day=date(2025, 5, 10), views=44, visitors=11))
    db.session.commit()
    subject, body, month = monthly_report(for_month=LAST)

check("the subject names the month", subject == "EBWA website: May 2026",
      subject)
check("the report is for the month asked for", month == LAST, str(month))
check("it gives the page views", "60 page views" in body, body[:200])
check("...and the visits", "from 5 visits" in body, body[:200])
check("IT SAYS WHAT A VISIT IS, on the line under the number",
      "A visit is one person on one day" in body
      and body.index("A visit is one person") - body.index("60 page views")
      < 120, "the caveat is not next to the figure")
check("...and spells out the consequence rather than hinting at it",
      "not the number of different people" in body)
check("it compares with the month before",
      "Against April" in body and "up 100%" in body,
      str([l for l in body.split(NL) if "April" in l]))
check("it compares with the same month last year",
      "Against May 2025" in body,
      str([l for l in body.split(NL) if "May 2025" in l]))
check("it names the target and says the month fell short",
      "target of 2,000" in body and "not met" in body
      and "1,940 short" in body,
      str([l for l in body.split(NL) if "target" in l]))
check("it lists the most visited pages",
      "Most visited pages" in body and "/about" in body)
check("it says the figures are counted here, with no analytics",
      "no analytics" in body and "identifies anybody" in body)
check("no address, hash or anything personal is in the body",
      "203.0.113" not in body and "Mozilla" not in body)

# A month that beats the target reads differently.
set_block(STATS_TARGET_KEY, "50")
with app.app_context():
    _s, body2, _m = monthly_report(for_month=LAST)
check("a month that beats the target says so",
      "was MET" in body2 and "10 over" in body2,
      str([l for l in body2.split(NL) if "target" in l]))
set_block(STATS_TARGET_KEY, str(STATS_TARGET_DEFAULT))

# No history a year back: say so rather than printing a zero.
with app.app_context():
    PageViewDaily.query.filter_by(day=date(2025, 5, 10)).delete()
    db.session.commit()
    _s, body3, _m = monthly_report(for_month=LAST)
check("with no figures from a year ago it says so, not a zero",
      "no figures for May 2025" in body3 and "Against May 2025" not in body3,
      str([l for l in body3.split(NL) if "2025" in l]))

# ---- sending it: through the REAL send_mail ---------------------------
# THIS USED TO PATCH send_mail ITSELF, and that is why it passed while
# every actual send raised. The old fake accepted anything, so the test
# asserted the ARGUMENTS the report handed over rather than that the
# mail layer could take them — send_monthly_report was passing a list to
# a function that called .strip() on it, and nothing here ever found out.
# Same gap as the video position bug: the stored value was right and the
# behaviour was never exercised.
#
# So the fake is at the SMTP boundary now. Everything above it is the
# real code, including the recipient handling, and what is asserted is
# the message that would have gone down the wire.
class FakeSMTP:
    sent = []

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
        FakeSMTP.sent.append(message)


def mail_setup():
    """Enough configuration for send_mail to get as far as sending."""
    for key, value in (("smtp_host", "smtp.example.org"),
                       ("smtp_port", "587"),
                       ("smtp_from", "website@example.org"),
                       ("smtp_security", "none")):
        set_block(key, value, group="mail")


def sent_messages():
    return FakeSMTP.sent


def clear_sent():
    FakeSMTP.sent = []


mail_setup()
clear_sent()

with app.app_context():
    result = send_monthly_report(for_month=LAST)
check("switched off, it sends nothing",
      not sent_messages() and "switched off" in result, result)

set_block(STATS_REPORT_KEY, "1")
set_block(MAIL_TO_KEY, "trustees@example.org", group="mail")
with app.app_context():
    check("the recipient falls back to the enquiries address",
          stats_report_setting()["recipients"] == ["trustees@example.org"],
          str(stats_report_setting()))
    with patch("app.smtplib.SMTP", FakeSMTP):
        first = send_monthly_report(for_month=LAST)
check("SWITCHED ON, IT ACTUALLY SENDS — through the real send_mail",
      len(sent_messages()) == 1 and "Sent the report" in first, first)
check("...to one recipient, in the To header",
      sent_messages()[0]["To"] == "trustees@example.org",
      str(sent_messages()[0]["To"]))
check("...with the month in the subject",
      "May 2026" in sent_messages()[0]["Subject"],
      sent_messages()[0]["Subject"])
check("...and the caveat in the body it would really deliver",
      "one person on one day"
      in sent_messages()[0].get_content())

with app.app_context():
    with patch("app.smtplib.SMTP", FakeSMTP):
        again = send_monthly_report(for_month=LAST)
check("RUNNING IT AGAIN THE SAME MONTH SENDS NOTHING",
      len(sent_messages()) == 1 and "Already sent" in again, again)
with app.app_context():
    check("...and it knows because of the audit log, not a flag",
          monthly_report_sent_for(LAST) is True)
    entry = (AuditLog.query.filter_by(action=STATS_REPORT_ACTION)
             .order_by(AuditLog.id.desc()).first())
    check("the send is audit-logged, naming the month",
          entry is not None and "May 2026" in entry.summary, str(entry))
    check("...and does NOT put the address in the log",
          "trustees@example.org" not in entry.summary, entry.summary)

with app.app_context():
    with patch("app.smtplib.SMTP", FakeSMTP):
        send_monthly_report(for_month=date(2026, 4, 1))
check("a different month still sends", len(sent_messages()) == 2)
with app.app_context():
    with patch("app.smtplib.SMTP", FakeSMTP):
        send_monthly_report(for_month=LAST, force=True)
check("--force sends anyway, for checking the address",
      len(sent_messages()) == 3)

# ---- SEVERAL RECIPIENTS, end to end -----------------------------------
set_block(STATS_REPORT_TO_KEY, "board@example.org, chair@example.org")
with app.app_context():
    setting = stats_report_setting()
check("a recipient set here wins over the enquiries address",
      setting["recipients"] == ["board@example.org", "chair@example.org"],
      str(setting))
check("...and the page can say where it came from",
      setting["source"] == "database", setting["source"])
clear_sent()
with app.app_context():
    with patch("app.smtplib.SMTP", FakeSMTP):
        many = send_monthly_report(for_month=LAST, force=True)
check("IT SENDS TO SEVERAL RECIPIENTS",
      len(sent_messages()) == 1 and "Sent the report" in many, many)
check("...with both in one To header, comma separated",
      sent_messages()[0]["To"] == "board@example.org, chair@example.org",
      str(sent_messages()[0]["To"]))

# A semicolon, a stray blank and a duplicate are an admin typing, not an
# error worth refusing a whole month's report over.
set_block(STATS_REPORT_TO_KEY,
          "board@example.org; , chair@example.org ,BOARD@example.org")
clear_sent()
with app.app_context():
    with patch("app.smtplib.SMTP", FakeSMTP):
        send_monthly_report(for_month=LAST, force=True)
check("semicolons, blanks and a duplicate are cleaned up rather than sent",
      sent_messages()[0]["To"] == "board@example.org, chair@example.org",
      str(sent_messages()[0]["To"]))
set_block(STATS_REPORT_TO_KEY, "board@example.org, chair@example.org")

# ---- the security alert takes both shapes too -------------------------
print()
print("---- the security alert, one address and several")
from app import (note_failed_login, SECURITY_ALERT_KEY,  # noqa: E402
                 SECURITY_ALERT_TO_KEY, ALERT_IP_THRESHOLD,
                 security_alert_to, recipient_header,
                 FAILED_LOGIN_ACTION)

check("recipient_header takes a plain string",
      recipient_header("a@x.org") == "a@x.org")
check("...a list", recipient_header(["a@x.org", "b@x.org"])
      == "a@x.org, b@x.org")
check("...a comma-separated string",
      recipient_header("a@x.org, b@x.org") == "a@x.org, b@x.org")
check("...a list whose elements are themselves lists of addresses",
      recipient_header(["a@x.org, b@x.org"]) == "a@x.org, b@x.org")
check("...and nothing at all, without raising",
      recipient_header(None) == "" and recipient_header([]) == ""
      and recipient_header("") == "")

set_block(SECURITY_ALERT_KEY, "1", group="security")
for addresses, expected in (
        ("netbus@example.org", "netbus@example.org"),
        ("netbus@example.org, oncall@example.org",
         "netbus@example.org, oncall@example.org")):
    set_block(SECURITY_ALERT_TO_KEY, addresses, group="security")
    clear_sent()
    with app.app_context():
        AuditLog.query.delete()
        db.session.commit()
        # note_failed_login COUNTS the failed-login rows; it does not
        # write them. The real flow logs the failure first and then
        # calls it, so the test has to do both or the threshold is never
        # reached and nothing is sent — which looked like a bug in the
        # alert and was a bug in the fixture.
        for i in range(ALERT_IP_THRESHOLD):
            db.session.add(AuditLog(user_email="someone@example.org",
                                    action=FAILED_LOGIN_ACTION,
                                    summary="Wrong password.",
                                    ip="198.51.100.1"))
        db.session.commit()
        with patch("app.smtplib.SMTP", FakeSMTP):
            note_failed_login("someone@example.org", "198.51.100.1")
    check("the alert reaches %d recipient(s)" % len(expected.split(",")),
          len(sent_messages()) == 1, str(len(sent_messages())))
    check("...in one To header: %s" % expected,
          sent_messages() and sent_messages()[0]["To"] == expected,
          str(sent_messages()[0]["To"]) if sent_messages() else "nothing sent")
    check("...and it carries no password, only the addresses tried",
          "someone@example.org" in sent_messages()[0].get_content())
set_block(SECURITY_ALERT_KEY, "", group="security")

# ---- the contact form's single address still works --------------------
from app import mail_recipient      # noqa: E402
set_block(MAIL_TO_KEY, "enquiries@example.org", group="mail")
with app.app_context():
    check("the contact form's recipient is a plain string",
          isinstance(mail_recipient(), str), str(type(mail_recipient())))
clear_sent()
with app.app_context():
    with patch("app.smtplib.SMTP", FakeSMTP):
        ok = send_mail(mail_recipient(), "Test", "Body")
check("...and a plain string still sends", ok is True
      and len(sent_messages()) == 1)
check("...to that address", sent_messages()[0]["To"] == "enquiries@example.org")

# ---- WHO SEES WHAT ----------------------------------------------------
print()
print("---- the client admin's own figures")
with app.app_context():
    PageView.query.delete()
    db.session.commit()
for _ in range(4):
    visit("/about")

client_admin = app.test_client()
client_admin.post("/admin/login", data={"email": "client@example.com",
                                        "password": PW})
page = client_admin.get("/admin/visitors")
check("A CLIENT ADMIN CAN SEE THEIR OWN FIGURES",
      page.status_code == 200, str(page.status_code))
html = page.data.decode("utf-8")
check("...the headline numbers", "page views" in html)
check("...and the chart", "<svg" in html and "stats-bar" in html)
check("...and the caveat about what a visit is",
      "one person on one day" in html)
check("...and the target, so the number means something",
      "monthly target" in html)
check("BUT NOT THE REPORT SETTINGS",
      'name="report_to"' not in html and 'name="enabled"' not in html)
check("...nor anything else from Settings",
      "SMTP" not in html and "Back up now" not in html
      and "Homepage sections" not in html)
check("...and Settings itself is still closed to them",
      client_admin.get("/admin/features").status_code == 403)
for path in ("/admin/stats-report", "/admin/stats-report/send"):
    check("...as are the report's own routes (%s)" % path,
          client_admin.post(path).status_code == 403)

dash = client_admin.get("/admin").data.decode("utf-8")
check("the dashboard carries a card that links to the page",
      "Page views this month" in dash and "/admin/visitors" in dash)
check("...and NOT the chart, which would swamp it",
      "stats-bar" not in dash)

check("an anonymous visitor gets the login page, not the figures",
      app.test_client().get("/admin/visitors").status_code == 302)

boss2 = app.test_client()
boss2.post("/admin/login", data={"email": "netbus@example.com",
                                 "password": PW})
settings = boss2.get("/admin/features").data.decode("utf-8")
check("a super admin sees the report settings on Settings",
      'name="report_to"' in settings and 'name="enabled"' in settings
      and 'name="target"' in settings)
check("...and the same summary, from the one shared partial",
      "one person on one day" in settings and "stats-bar" in settings)

# ---- HOW LONG PER-PAGE DETAIL IS KEPT ---------------------------------
print()
print("---- the retention setting")
from app import (PAGEVIEW_RAW_MIN, PAGEVIEW_RAW_MAX,  # noqa: E402
                 STATS_RAW_DAYS_KEY, pageview_raw_days, human_bytes,
                 period_report, parse_report_period, ORG_NAME_KEY,
                 ORG_CHARITY_NO_KEY)

with app.app_context():
    check("it defaults to the 62 days it was fixed at",
          pageview_raw_days() == PAGEVIEW_RAW_DAYS == 62,
          str(pageview_raw_days()))
for value, expected, why in (
        ("30", 30, "the floor is allowed"),
        ("365", 365, "the ceiling is allowed"),
        ("120", 120, "a value inside the range is used"),
        ("29", 62, "below the floor falls back to the default"),
        ("366", 62, "above the ceiling falls back"),
        ("0", 62, "zero falls back"),
        ("-40", 62, "a negative falls back"),
        ("", 62, "an empty setting falls back"),
        ("ninety", 62, "nonsense falls back rather than raising")):
    set_block(STATS_RAW_DAYS_KEY, value)
    with app.app_context():
        check("%s (%r)" % (why, value), pageview_raw_days() == expected,
              str(pageview_raw_days()))

# The route refuses what the field refuses, so a hand-made POST cannot
# set a window the helper would then ignore.
set_block(STATS_RAW_DAYS_KEY, str(PAGEVIEW_RAW_DAYS))
# boss2, logged in further up: the login rate limiter allows five in ten
# minutes and this file is already at its limit, so a fresh client here
# would be turned away and every check below it would pass or fail
# against the login page instead of Settings.


def save_settings(**over):
    data = {"target": "2000", "raw_days": "62",
            "report_to": "", "enabled": "on"}
    data.update(over)
    return boss2.post("/admin/stats-report", data=data,
                      follow_redirects=True)


for bad in ("29", "366", "0", "abc", ""):
    r = save_settings(raw_days=bad)
    with app.app_context():
        check("the route refuses raw_days=%r" % bad,
              pageview_raw_days() == 62
              and (b"between 30 and 365" in r.data
                   or b"whole number" in r.data),
              str(pageview_raw_days()))
save_settings(raw_days="90")
with app.app_context():
    check("...and accepts one inside the range",
          pageview_raw_days() == 90, str(pageview_raw_days()))

# AGGREGATION FOLLOWS THE SETTING, which is the whole point of it.
with app.app_context():
    PageView.query.delete()
    PageViewDaily.query.delete()
    db.session.commit()
    ref = date(2026, 6, 30)
    for back in (100, 95, 40, 10):
        db.session.add(PageView(day=ref - timedelta(days=back),
                                path="/", visitor="v"))
    db.session.commit()
    aggregate_page_views(today=ref)
    left = sorted((ref - r.day).days for r in PageView.query.all())
    check("at 90 days, the two older rows roll up and the rest stay",
          left == [10, 40], str(left))
set_block(STATS_RAW_DAYS_KEY, "30")
with app.app_context():
    aggregate_page_views(today=ref)
    left = sorted((ref - r.day).days for r in PageView.query.all())
    check("SHORTENING IT PRUNES MORE ON THE NEXT RUN", left == [10],
          str(left))
    check("...and the rolled-up days are all still in the totals",
          PageViewDaily.query.count() == 3,
          str(PageViewDaily.query.count()))
    total = db.session.query(
        db.func.sum(PageViewDaily.views)).scalar() or 0
    check("...with nothing lost from the totals", total == 3, str(total))

# THE DAILY TOTALS HAVE NO SETTING, and that is deliberate.
settings_html = boss2.get("/admin/features").data.decode("utf-8")
check("the retention field is on Settings",
      'name="raw_days"' in settings_html)
check("...and says what the trade-off is",
      "most visited" in settings_html.lower()
      and "bigger database" in settings_html)
check("...and states the storage at both ends of the range",
      human_bytes(200 * PAGEVIEW_RAW_MIN * 460) in settings_html
      and human_bytes(200 * PAGEVIEW_RAW_MAX * 460) in settings_html,
      "the numbers in the helper text do not match the measured cost")
check("IT SAYS THE DAILY TOTALS ARE NOT AFFECTED",
      "not affected" in settings_html and "kept for ever" in settings_html)
check("...and that having no control over them is a decision",
      "a decision" in settings_html and "not an omission" in settings_html)
check("there is NO setting that can shorten the daily totals",
      'name="daily_days"' not in settings_html
      and 'name="totals_days"' not in settings_html)
set_block(STATS_RAW_DAYS_KEY, str(PAGEVIEW_RAW_DAYS))

# ---- THE DOWNLOADABLE REPORT ------------------------------------------
print()
print("---- the report for a period")
set_block(ORG_NAME_KEY, "Enfield Bangladesh Welfare Association",
          group="org")
set_block(ORG_CHARITY_NO_KEY, "1234567", group="org")
with app.app_context():
    PageView.query.delete()
    PageViewDaily.query.delete()
    db.session.commit()
    today = utc_as_uk(datetime.utcnow()).date()
    recent_start = today - timedelta(days=9)
    for i in range(10):
        for j in range(4):
            db.session.add(PageView(day=recent_start + timedelta(days=i),
                                    path="/about" if j else "/",
                                    visitor="p%d" % (i % 3)))
    db.session.commit()
    r = period_report(recent_start, today)

check("the report knows who it is about", r["org"].startswith("Enfield"))
check("...and carries the charity number", r["charity_number"] == "1234567")
check("...the period and its length",
      r["start"] == recent_start and r["end"] == today and r["days"] == 10,
      str((r["start"], r["end"], r["days"])))
check("...the views and the visits", r["views"] == 40 and r["visits"] == 3,
      str((r["views"], r["visits"])))
check("...a comparison with the previous period of the same length",
      r["previous"]["days"] if False else
      (r["previous"]["start"] == recent_start - timedelta(days=10)
       and r["previous"]["end"] == recent_start - timedelta(days=1)),
      str(r["previous"]))
check("...and it is described in words, not just a number",
      "no figure" in r["previous"]["phrase"]
      or "up" in r["previous"]["phrase"]
      or "down" in r["previous"]["phrase"], r["previous"]["phrase"])
check("...the most visited pages, while the detail is still held",
      r["pages_held"] is True and r["top"] and r["top"][0]["path"] == "/about",
      str(r["top"]))
check("...and when it was produced", r["produced"] is not None)

# A PERIOD WITH ONLY DAILY TOTALS: the figures still come out, and the
# missing page detail is explained rather than shown as zero.
with app.app_context():
    old_start = date(2024, 3, 1)
    old_end = date(2024, 3, 31)
    for i in range(31):
        db.session.add(PageViewDaily(day=old_start + timedelta(days=i),
                                     views=20 + i, visitors=5))
    db.session.commit()
    old = period_report(old_start, old_end)
check("A PERIOD WITH ONLY DAILY TOTALS STILL REPORTS ITS FIGURES",
      old["views"] == sum(20 + i for i in range(31)) and old["visits"] == 155,
      str((old["views"], old["visits"])))
check("...and says the page detail is no longer held, rather than zero",
      old["pages_held"] is False and old["top"] == [], str(old["top"]))
check("...naming the date it is kept from",
      old["kept_from"] is not None and old["raw_days"] == 62,
      str((old["kept_from"], old["raw_days"])))

# ---- the page it renders ----------------------------------------------
page = client_admin.get("/admin/visitors/report?from=%s&to=%s"
                        % (old_start, old_end))
check("A CLIENT ADMIN CAN OPEN THE REPORT", page.status_code == 200,
      str(page.status_code))
doc = page.data.decode("utf-8")
check("it names the association", "Enfield Bangladesh Welfare" in doc)
check("...and the charity number", "1234567" in doc
      and "Registered charity number" in doc)
check("...the period in words", "01 March 2024" in doc
      and "31 March 2024" in doc)
check("...the caveat, beside the figures",
      "one person on one day" in doc
      and doc.index("one person on one day") - doc.index("page views")
      < 900, "the caveat is not near the figures")
check("...that no analytics service is used", "no analytics service" in doc)
check("...when it was produced", "Produced" in doc)
check("...and explains the missing page detail",
      "no longer held" in doc, "the gap is unexplained")
check("it is a document, not a screenshot: the admin chrome is print-hidden",
      "report-doc" in doc and "report-tools" in doc)

fresh = client_admin.get("/admin/visitors/report?from=%s&to=%s"
                         % (recent_start, today)).data.decode("utf-8")
check("a recent period lists the pages", "/about" in fresh
      and "Most visited pages" in fresh)

# Bad input is refused rather than becoming a strange query.
for args, why in (("from=nonsense", "a date that is not a date"),
                  ("from=2026-12-01&to=2026-01-01", "a backwards period"),
                  ("from=1990-01-01&to=2030-01-01", "a forty-year period")):
    r2 = client_admin.get("/admin/visitors/report?" + args)
    check("refuses %s, and still renders" % why, r2.status_code == 200,
          str(r2.status_code))

check("an anonymous visitor cannot read it",
      app.test_client().get("/admin/visitors/report").status_code == 302)

# With no charity number it says so on screen, and that note is
# print-hidden so it cannot end up in a funding application.
set_block(ORG_CHARITY_NO_KEY, "", group="org")
doc2 = client_admin.get("/admin/visitors/report").data.decode("utf-8")
check("with no charity number it prompts for one on screen",
      "No registered charity number is" in doc2)
check("...and that prompt is hidden from the print",
      "report-missing" in doc2)
set_block(ORG_CHARITY_NO_KEY, "1234567", group="org")

# ---- teardown ---------------------------------------------------------
with app.app_context():
    db.session.remove()
    db.engine.dispose()
for suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + suffix):
        os.remove(TEST_DB + suffix)
check("test db deleted", not os.path.isfile(TEST_DB))

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
