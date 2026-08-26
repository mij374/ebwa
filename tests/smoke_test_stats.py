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
      "<svg" in page and "<script src" not in page)
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
