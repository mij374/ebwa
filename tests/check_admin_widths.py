"""No admin page may drag itself sideways on a phone.

The public side has had this covered for a long time —
check_header_layout.py asserts it at every viewport — but nothing
covered the ADMIN at phone widths, and /admin/campaigns was pushing the
whole page 353px sideways at 390 and 383px at 360. An admin table is
wide by nature (a date, a title, a venue, a status and four actions),
and the answer already existed in the stylesheet: .table-scroll, which
lets the table scroll inside its own box instead of taking the page
with it.

Two things this file does that are easy to get wrong, both learned by
getting them wrong:

  * IT LOGS IN ONCE AND RESIZES, rather than logging in per viewport.
    Seven logins in quick succession trip the `login` rate limiter, and
    a rate-limited context lands on the login page — where there is no
    table, nothing overflows, and the check passes while looking at the
    wrong page entirely.
  * IT ASSERTS THE PAGE IS ACTUALLY THERE at each width before
    measuring. A redirect to the login page, a 404 or a 403 all render
    something narrow. "Nothing overflowed" and "there was nothing to
    overflow" are the same measurement and completely different facts.

Every page is measured twice where it has anything collapsed: as
delivered, and again with every <details> forced open. What one hides is
the likeliest thing on an admin page to be too wide — long shell
commands, wide number fields — and measuring only the delivered page
measures it with all of that absent.

When something does overflow it names the widest offending element, so
the next person is not left bisecting a template.

Run:  python tests/check_admin_widths.py [--shots DIR]
"""
import os
import shutil
import sys
import threading
import time
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEST_DB = os.path.join(HERE, "test_admin_widths.db")
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _s):
        os.remove(TEST_DB + _s)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from werkzeug.serving import make_server            # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402
from playwright.sync_api import sync_playwright     # noqa: E402

from browser_motion import STILL, new_context       # noqa: E402
from browser_view import PHONES                     # noqa: E402

from app import (app, db, User, Campaign, Payment, GalleryAlbum,  # noqa: E402
                 GalleryImage, ContactMessage, MembershipApplication,
                 Member, MembershipPayment,
                 Subscriber, AuditLog, FeatureFlag, Faq,
                 Resource, UPLOAD_DIR)
import seed_demo                                    # noqa: E402

SHOTS = (sys.argv[sys.argv.index("--shots") + 1]
         if "--shots" in sys.argv else None)
if SHOTS:
    os.makedirs(SHOTS, exist_ok=True)

PORT = 5185
BASE = "http://127.0.0.1:%d" % PORT
PW = "admin-widths-password"
FIXTURE_IMAGE = "admin-widths-fixture.png"

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        ("\n        %s" % detail) if detail and not cond
                        else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------- fixtures
# Every list page needs ROWS, or it renders its empty state and the wide
# table this file exists to measure is never on the page at all.
with app.app_context():
    seed_demo.seed()
    for flag in FeatureFlag.query.all():
        flag.enabled = True
    shutil.copyfile(os.path.join(app.root_path, "static", "img",
                                 "ebwa-logo.png"),
                    os.path.join(UPLOAD_DIR, FIXTURE_IMAGE))
    db.session.add(User(email="widths@example.com",
                        password_hash=generate_password_hash(PW),
                        role="super_admin"))
    album = GalleryAlbum(title="Eid at the centre", slug="eid-at-the-centre",
                         description="Photographs from this year.",
                         cover_image=FIXTURE_IMAGE, published=True)
    db.session.add(album)
    db.session.commit()
    for i in range(3):
        db.session.add(GalleryImage(filename=FIXTURE_IMAGE,
                                    caption="Volunteers serving lunch",
                                    album_id=album.id, sort=i))
    camp = Campaign(title="Summer seaside trip to Southend",
                    slug="summer-seaside-trip",
                    description="A day at the coast for our elders.",
                    image=FIXTURE_IMAGE, fee_pence=1500,
                    target_pence=50000, state="open")
    closed = Campaign(title="Winter coat fund", slug="winter-coat-fund",
                      description="Coats for elders.", target_pence=25000,
                      state="closed")
    db.session.add_all([camp, closed])
    db.session.commit()
    for i in range(3):
        db.session.add(Payment(
            campaign_id=camp.id,
            name="A Contributor With A Long Name %d" % i,
            email="somebody.with.a.long.address%d@example.org" % i,
            fee_pence=1500, donation_pence=2500, gift_aid=True,
            gift_aid_name="A Contributor With A Long Name %d" % i,
            gift_aid_address="114 Somewhere Road", gift_aid_postcode="EN3 4EU",
            status="complete", stripe_session_id="cs_widths_%d" % i))
    db.session.add(ContactMessage(
        name="Somebody Enquiring", email="enquirer@example.org",
        subject="A question about the weekend school",
        message="Could you tell me what time the Saturday class starts?"))
    db.session.add(MembershipApplication(
        name="An Applicant With A Long Name", email="applicant@example.org",
        phone="020 8804 4006", address="114 Somewhere Road, Enfield",
        over_18=True, bangladeshi_origin=True, lives_works_enfield=True,
        fee_confirmed=True, status="new"))
    db.session.add(Subscriber(email="a.long.subscriber.address@example.org"))
    # A member with a payment, so the member pages carry their widest
    # tables rather than their empty states.
    member = Member(name="A Member With A Fairly Long Name",
                    email="a.long.member.address@example.org",
                    phone="020 8804 4006",
                    address="114 Somewhere Road, Enfield, EN3 4EU",
                    joined_on=date(2024, 6, 1))
    db.session.add(member)
    db.session.commit()
    db.session.add(MembershipPayment(
        member_id=member.id, amount_pence=1000, period_end_year=2027,
        method="bank_transfer", received_on=date(2026, 1, 5),
        status="complete", received_by="The treasurer",
        recorded_by="somebody.with.a.long.address@example.org"))
    db.session.add(Member(name="Somebody With No Payments"))
    # seed_demo seeds no FAQ and no resources, and both list pages render
    # their table only `{% if rows %}` — so without these two the check
    # measured an empty state and reported no overflow. That is exactly
    # what the marker assertion above exists to catch, and it did.
    db.session.add(Faq(question="What time does the weekend school start?",
                       answer="Saturday mornings at ten, in the main hall.",
                       category="Weekend school", sort=0, published=True))
    db.session.add(Resource(name="Enfield Citizens Advice",
                            category="Advice and benefits",
                            description="Free, confidential advice on "
                                        "benefits, debt and housing.",
                            phone="0808 278 7844",
                            url="https://example.org/advice", sort=0))
    for i in range(4):
        db.session.add(AuditLog(
            user_email="somebody.with.a.long.address@example.org",
            action="edit", entity_type="Event", entity_id=i,
            summary="Edited event “Annual General Meeting 2026” "
                    "(date, venue, description).",
            ip="192.168.1.100",
            created_at=datetime.utcnow() - timedelta(hours=i)))
    db.session.commit()
    CAMP_ID = camp.id
    ALBUM_ID = album.id
    MEMBER_ID = member.id

# Every admin page, with a selector proving THIS page is the one on
# screen. A redirect to the login page or a 404 renders something narrow
# and would otherwise sail through.
PAGES = [
    ("/admin", ".admin-stat"),
    ("/admin/content", ".admin-form"),
    ("/admin/services", ".admin-table"),
    ("/admin/services/new", ".admin-form"),
    ("/admin/events", ".admin-table"),
    ("/admin/events/new", ".admin-form"),
    ("/admin/news", ".admin-table"),
    ("/admin/news/new", ".admin-form"),
    ("/admin/gallery", ".admin-gallery-grid"),
    ("/admin/gallery/albums", ".admin-table"),
    ("/admin/gallery/albums/new", ".admin-form"),
    ("/admin/testimonials", ".admin-table"),
    ("/admin/testimonials/new", ".admin-form"),
    ("/admin/partners", ".admin-table"),
    ("/admin/partners/new", ".admin-form"),
    ("/admin/faq", ".admin-table"),
    ("/admin/faq/new", ".admin-form"),
    ("/admin/resources", ".admin-table"),
    ("/admin/resources/new", ".admin-form"),
    ("/admin/journey", ".admin-table"),
    ("/admin/journey/new", ".admin-form"),
    ("/admin/messages", ".admin-table"),
    ("/admin/membership", ".admin-table"),
    ("/admin/members", ".admin-table"),
    ("/admin/members/new", ".admin-form"),
    ("/admin/members/renewals", ".report-doc"),
    ("/admin/campaigns", ".admin-table"),
    ("/admin/campaigns/new", ".admin-form"),
    ("/admin/campaigns/%d/contributors" % CAMP_ID, ".admin-table"),
    ("/admin/members/%d" % MEMBER_ID, ".admin-table"),
    ("/admin/members/%d/edit" % MEMBER_ID, ".admin-form"),
    ("/admin/gift-aid", ".admin-form, .admin-table"),
    ("/admin/gift-aid/declarations", ".admin-table"),
    ("/admin/subscribers", ".admin-table"),
    ("/admin/users", ".admin-table"),
    ("/admin/features", ".admin-table"),
    ("/admin/audit", ".admin-table"),
    ("/admin/account", ".admin-form"),
    ("/admin/help", ".guide-toc"),
    ("/admin/visitors", ".stats-chart"),
    ("/admin/visitors/report", ".report-doc"),
]

server = make_server("127.0.0.1", PORT, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)

# Anything sticking out past the document's own width, widest first, so a
# failure names the element to fix rather than just the page.
#
# ELEMENTS INSIDE A SCROLL CONTAINER ARE SKIPPED. A table in a
# .table-scroll box is WIDER than the viewport by design — that is the
# whole point of the box — and its rectangle says so, but it is clipped
# and drags nothing with it. Reporting it named the innocent party and
# hid the guilty one: on /admin/events the fix was already working and
# the check was still pointing at the table.
OVERFLOW_JS = """() => {
  const docW = document.documentElement.clientWidth;
  const over = document.documentElement.scrollWidth - docW;
  const clipped = el => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') return true;
    }
    return false;
  };
  const guilty = [];
  if (over > 0) {
    document.querySelectorAll('body *').forEach(el => {
      const b = el.getBoundingClientRect();
      if (b.width && b.right > docW + 1 && !clipped(el)) {
        guilty.push({
          tag: el.tagName.toLowerCase(),
          cls: (el.getAttribute('class') || '').slice(0, 34),
          w: Math.round(b.width), right: Math.round(b.right),
          past: Math.round(b.right - docW)
        });
      }
    });
    guilty.sort((a, b) => b.past - a.past);
  }
  return {over, docW, guilty: guilty.slice(0, 3)};
}"""

results = {}
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    # ONE login, then resize — see the note at the top of this file.
    ctx = new_context(browser, PHONES[0][0], PHONES[0][1], motion=STILL)
    page = ctx.new_page()
    page.goto(BASE + "/admin/login", wait_until="load")
    page.fill("input[name=email]", "widths@example.com")
    page.fill("input[name=password]", PW)
    page.click("button[type=submit]")
    page.wait_for_load_state("load")
    check("logged in once, before any measuring",
          page.locator(".admin-side").count() == 1, page.url)

    for width, height in PHONES:
        page.set_viewport_size({"width": width, "height": height})
        print()
        print("---- %dx%d" % (width, height))
        for url, marker in PAGES:
            page.goto(BASE + url, wait_until="networkidle")
            # Is this really the page we asked for? A rate-limited or
            # logged-out context renders a narrow login card that would
            # otherwise pass every width assertion below.
            present = page.locator(marker).count() > 0
            check("%dpx %s: the page is actually on screen" % (width, url),
                  present, "expected %s, landed on %s" % (marker, page.url))
            if not present:
                continue
            d = page.evaluate(OVERFLOW_JS)
            results[(width, url)] = d["over"]
            detail = ""
            if d["over"] > 0:
                detail = "%dpx past a %dpx viewport; widest: %s" % (
                    d["over"], d["docW"],
                    ", ".join("%s.%s %dpx wide, %dpx past"
                              % (g["tag"], g["cls"], g["w"], g["past"])
                              for g in d["guilty"]) or "(nothing measurable)")
            check("%dpx %s: no sideways scroll" % (width, url),
                  d["over"] <= 0, detail)
            if SHOTS and d["over"] > 0:
                page.screenshot(
                    path=os.path.join(SHOTS, "overflow-%d-%s.png"
                                      % (width, url.strip("/").replace("/", "-"))),
                    full_page=True)

            # AND AGAIN WITH EVERYTHING OPEN. What a <details> hides is
            # the likeliest thing on an admin page to be too wide — long
            # shell commands in the domain instructions, number fields
            # in the marquee speeds — and a page measured as delivered is
            # measured with all of it absent. Only pages that have one
            # are measured twice.
            opened = page.evaluate("""() => {
                const all = [...document.querySelectorAll('details')];
                all.forEach(d => { d.open = true; });
                return all.length;
            }""")
            if opened:
                d = page.evaluate(OVERFLOW_JS)
                results[(width, url + " (opened)")] = d["over"]
                detail = ""
                if d["over"] > 0:
                    detail = "%dpx past a %dpx viewport; widest: %s" % (
                        d["over"], d["docW"],
                        ", ".join("%s.%s %dpx wide, %dpx past"
                                  % (g["tag"], g["cls"], g["w"], g["past"])
                                  for g in d["guilty"]) or "(nothing measurable)")
                check("%dpx %s: no sideways scroll with its %d <details> "
                      "open" % (width, url, opened), d["over"] <= 0, detail)
                if SHOTS and d["over"] > 0:
                    page.screenshot(
                        path=os.path.join(
                            SHOTS, "overflow-open-%d-%s.png"
                            % (width, url.strip("/").replace("/", "-"))),
                        full_page=True)
    ctx.close()
    browser.close()

server.shutdown()
server.server_close()
with app.app_context():
    db.session.remove()
    db.engine.dispose()
for suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + suffix):
        os.remove(TEST_DB + suffix)
path = os.path.join(UPLOAD_DIR, FIXTURE_IMAGE)
if os.path.isfile(path):
    os.remove(path)

print()
worst = sorted(((v, k) for k, v in results.items() if v > 0), reverse=True)
if worst:
    print("Pages that scroll sideways, widest first:")
    for over, (width, url) in worst:
        print("  %4dpx at %dpx  %s" % (over, width, url))
else:
    widths = sorted({w for w, _h in PHONES}, reverse=True)
    print("No admin page scrolls sideways: %d pages at %s."
          % (len(PAGES), " or ".join("%dpx" % w for w in widths)))

print()
if failures:
    print("FAILED: %d check(s):" % len(failures))
    for f in failures[:20]:
        print("  -", f)
    if len(failures) > 20:
        print("  ... and %d more" % (len(failures) - 20))
    sys.exit(1)
print("All checks passed.")
