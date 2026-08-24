"""Accessibility audit with axe-core, in the existing Chromium harness.

This is an AUDIT, not a gate. It reports what axe finds, grouped by
severity, and exits 0 whatever it finds — the point of the first run is
to know the size of the problem, and a check that fails on its own
first run tells you nothing you can act on in order. Pass --strict once
the public pages are clean to turn it into a gate that fails on
critical and serious violations.

Public pages and admin pages are reported SEPARATELY and in that order.
They are not equally important: a violation on /donate is between EBWA
and somebody trying to give them money, while one on an admin form is
between Netbus and a colleague. Both are worth fixing; only one of them
is urgent.

axe-core is fetched on demand into tests/vendor/ (gitignored) rather
than vendored: it is half a megabyte of third-party minified JavaScript,
this project has no npm and no build step, and nothing else here needs
it. It is injected with page.evaluate, which runs through CDP and is
therefore not subject to the site's Content-Security-Policy — the same
CSP a <script src> would have been blocked by, correctly.

What axe cannot tell you, and this file therefore does not claim:
whether the reading order makes sense, whether alt text says the right
thing, whether a colour that passes contrast is legible to a real
person, or whether the site can be operated with a screen reader.
Automated checks find perhaps a third of what matters. A clean run here
is a floor, not a pass.

Run:  python tests/check_accessibility.py [--strict] [--json FILE]
"""
import json
import os
import sys
import shutil
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_a11y.db")
for _suffix in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _suffix):
        os.remove(TEST_DB + _suffix)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from werkzeug.serving import make_server  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from browser_motion import STILL, new_context  # noqa: E402

from app import (app, db, User, FeatureFlag, Event, NewsPost,  # noqa: E402
                 GalleryAlbum, GalleryImage, Campaign, Milestone,
                 ContentImage, UPLOAD_DIR)
import seed_demo  # noqa: E402

AXE_VERSION = "4.10.2"
AXE_PATH = os.path.join(HERE, "vendor", "axe.min.js")
AXE_URL = ("https://cdn.jsdelivr.net/npm/axe-core@%s/axe.min.js"
           % AXE_VERSION)

STRICT = "--strict" in sys.argv
JSON_OUT = (sys.argv[sys.argv.index("--json") + 1]
            if "--json" in sys.argv else None)

# One desktop and one phone. Not every viewport: axe's findings are
# overwhelmingly structural and repeat identically at every width, and a
# report nobody reads to the end is not a report. The layout checks in
# the other files are what cover the widths.
SIZES = [("desktop", 1280, 800), ("phone", 390, 740)]

# Listing pages plus one DETAIL page of each kind, resolved from the
# seeded rows below. The detail pages are where the rich-content macro,
# the video player and the lightbox live, and none of them appears on a
# listing — auditing only the listings would have missed most of the
# markup this site actually generates.
PUBLIC = ["/", "/about", "/events", "/news", "/gallery", "/gallery/all",
          "/our-journey", "/resources", "/faq", "/membership",
          "/collections", "/donate", "/contact", "/privacy", "/terms"]

# Admin lists AND one form of each shape: the forms are where labels,
# fieldsets and required markers are, so a list-only audit would say
# nothing about the part an admin actually types into.
ADMIN = ["/admin/login", "/admin", "/admin/content", "/admin/events",
         "/admin/events/new", "/admin/news", "/admin/news/new",
         "/admin/gallery", "/admin/gallery/albums",
         "/admin/gallery/albums/new", "/admin/testimonials",
         "/admin/testimonials/new", "/admin/partners",
         "/admin/partners/new", "/admin/faq", "/admin/faq/new",
         "/admin/resources", "/admin/resources/new", "/admin/journey",
         "/admin/journey/new", "/admin/services", "/admin/services/new",
         "/admin/messages", "/admin/membership", "/admin/campaigns",
         "/admin/campaigns/new", "/admin/gift-aid",
         "/admin/gift-aid/declarations", "/admin/subscribers",
         "/admin/users", "/admin/features", "/admin/audit",
         "/admin/account"]

SEVERITIES = ["critical", "serious", "moderate", "minor"]
PW = "a11y-audit-password"

# Written into static/uploads for the run and deleted at the end. The
# name says what they are so that a run killed half way leaves something
# obviously disposable rather than a mystery file among real photographs.
FIXTURE_IMAGE = "a11y-audit-fixture.png"
FIXTURE_FILES = [FIXTURE_IMAGE, "a11y-audit-fixture-thumb.png"]


def axe_source():
    """axe-core, fetched once into a gitignored folder."""
    if os.path.isfile(AXE_PATH):
        return open(AXE_PATH, encoding="utf-8").read()
    os.makedirs(os.path.dirname(AXE_PATH), exist_ok=True)
    print("Fetching axe-core %s ..." % AXE_VERSION)
    try:
        with urllib.request.urlopen(AXE_URL, timeout=30) as resp:
            data = resp.read().decode("utf-8")
    except Exception as err:
        sys.exit("Could not fetch axe-core (%s: %s).\n"
                 "This check needs it once; put axe.min.js in %s by hand "
                 "if this machine has no internet."
                 % (type(err).__name__, err, os.path.dirname(AXE_PATH)))
    open(AXE_PATH, "w", encoding="utf-8").write(data)
    return data


AXE = axe_source()

with app.app_context():
    seed_demo.seed()
    # EVERY feature flag on. A page behind an off flag 404s, and a 404 is
    # not a clean audit — it is an unaudited page. The client's own site
    # may have some of these off; that only makes this the wider net.
    for flag in FeatureFlag.query.all():
        flag.enabled = True
    if not User.query.filter_by(email="a11y@example.com").first():
        db.session.add(User(email="a11y@example.com",
                            password_hash=generate_password_hash(PW),
                            role="super_admin"))
    db.session.commit()

    # seed_demo leaves every image blank and seeds no album and no
    # campaign, so an audit of it alone would never see a photograph, a
    # gallery, a lightbox, a rich-content figure or a video player —
    # exactly the markup most likely to have an accessibility problem.
    # These fixtures put one of each on the page. They are torn down
    # with the database at the end of the run.
    for name in FIXTURE_FILES:
        shutil.copyfile(os.path.join(app.root_path, "static", "img",
                                     "ebwa-logo.png"),
                        os.path.join(UPLOAD_DIR, name))

    event, post = Event.query.first(), NewsPost.query.first()
    milestone = Milestone.query.first()
    album = GalleryAlbum(title="Eid at the centre", slug="eid-at-the-centre",
                         description="Photographs from this year's Eid.",
                         cover_image=FIXTURE_IMAGE, published=True)
    db.session.add(album)
    db.session.commit()
    for i, caption in enumerate(["Volunteers serving lunch", "", "The hall"]):
        db.session.add(GalleryImage(filename=FIXTURE_IMAGE, caption=caption,
                                    album_id=album.id, sort=i))
    # One unfiled photo as well: /gallery/all is what guarantees no photo
    # is unreachable, so it should be audited carrying something.
    db.session.add(GalleryImage(filename=FIXTURE_IMAGE, caption="Our centre"))

    campaign = Campaign(title="Seaside trip 2026", slug="seaside-trip-2026",
                        description="A day at the coast for our elders.",
                        image=FIXTURE_IMAGE, target_pence=250000,
                        fee_pence=1500, state="open",
                        video_url="https://www.youtube.com/watch?v=aqz-KE-bpKQ",
                        video_thumb=FIXTURE_IMAGE)
    db.session.add(campaign)

    # A video and a rich-content figure on the two owners that carry
    # them, so the player and the macro are both audited in place.
    if post:
        post.image = FIXTURE_IMAGE
        post.video_url = "https://vimeo.com/76979871"
        post.video_thumb = FIXTURE_IMAGE
        db.session.add(ContentImage(owner_type="news", owner_id=post.id,
                                    filename=FIXTURE_IMAGE,
                                    alt_text="Children at the weekend school",
                                    caption="Our weekend school", sort=0))
    if event:
        event.image = FIXTURE_IMAGE
    if milestone:
        milestone.image = FIXTURE_IMAGE
    db.session.commit()

    if event:
        PUBLIC.append("/events/%s" % event.slug)
    if post:
        PUBLIC.append("/news/%s" % post.slug)
    ALBUM_SLUG = album.slug
    PUBLIC.append("/gallery/%s" % album.slug)
    PUBLIC.append("/collections/%s" % campaign.slug)

server = make_server("127.0.0.1", 5181, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)
BASE = "http://127.0.0.1:5181"

# WCAG 2.1 AA is the bar a UK charity site is judged against, plus axe's
# own best-practice rules reported separately so they cannot drown it.
RUN_AXE = """() => new Promise(resolve => {
    axe.run(document, {
        resultTypes: ['violations'],
        runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a',
                                        'wcag21aa', 'best-practice']}
    }).then(r => resolve(r.violations.map(v => ({
        id: v.id, impact: v.impact, help: v.help,
        tags: v.tags,
        nodes: v.nodes.length,
        sample: (v.nodes[0] && v.nodes[0].target
                 ? v.nodes[0].target.join(' ') : '').slice(0, 90),
        // Every node, with axe's own one-line explanation of WHY it
        // failed — the contrast ratio it measured, the heading level it
        // found. The console report stays a summary; --json is where you
        // go to fix something, and a selector with no measurement beside
        // it means opening devtools for each of eighty-five elements.
        detail: v.nodes.slice(0, 40).map(n => ({
            target: (n.target || []).join(' '),
            html: (n.html || '').slice(0, 120),
            why: (n.failureSummary || '').replace(/\s+/g, ' ').slice(0, 220)
        }))
    }))));
})"""

findings = {}          # (area, page, size) -> [violation, ...]
errors = []
skip_results = []      # (page, is first tab stop, visible, text, landed on)


def scan(page, area, label, size_name):
    """Run axe against whatever is on the page RIGHT NOW."""
    try:
        page.evaluate(AXE)
        violations = page.evaluate(RUN_AXE)
    except Exception as err:
        errors.append((area, label, size_name, "axe: %s" % type(err).__name__))
        return
    findings[(area, label, size_name)] = violations


def audit(page, url, area, size_name):
    try:
        page.goto(BASE + url, wait_until="networkidle", timeout=20000)
    except Exception as err:
        errors.append((area, url, size_name, "load: %s" % type(err).__name__))
        return
    scan(page, area, url, size_name)


def confirm(page, selector, what, size_name):
    """Record a state that did not actually open.

    A scan of a state that never happened is a scan of the ordinary
    page, and it comes back clean — the most misleading result this
    file could produce. So the state is asserted, and a failure is
    reported beside the pages that could not be audited at all.
    """
    if page.locator(selector).count() == 0:
        errors.append(("public", what, size_name,
                       "state did not open - scan below is NOT that state"))


def check_skip_link(page, url, label):
    """The skip link is the one thing this pass ADDED, so it is proved.

    Three things have to be true and only the first is visible in the
    markup: Tab from a fresh page reaches it before anything else, it
    becomes visible when it does (an off-screen link nobody can see is
    no use to a sighted keyboard user), and activating it puts FOCUS on
    the target rather than merely scrolling to it — the mistake that
    makes a skip link look right and do nothing, because the next Tab
    goes straight back into the nav it just skipped.
    """
    page.goto(BASE + url, wait_until="networkidle")
    page.keyboard.press("Tab")
    state = page.evaluate("""() => {
        const a = document.activeElement;
        const box = a.getBoundingClientRect();
        return {focused: a.className, href: a.getAttribute('href') || '',
                text: (a.textContent || '').trim(),
                onScreen: box.left >= 0 && box.top >= 0
                          && box.width > 0 && box.height > 0};
    }""")
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    landed = page.evaluate("() => document.activeElement.id")
    skip_results.append((label, state["focused"] == "skip-link",
                         state["onScreen"], state["text"], landed))


def audit_states(page, size_name, album_slug):
    """The states axe cannot reach on its own.

    axe sees the DOM as delivered. Everything this site opens — the
    mobile menu, a nav dropdown, the lightbox, an FAQ answer — exists
    only after somebody has acted, and a dialog is precisely where
    focus handling and labelling go wrong. Auditing the closed page
    only would report on the half of the site nobody has trouble with.
    """
    # The open mobile menu. Phone only; above 899px there is no button.
    if size_name == "phone":
        page.goto(BASE + "/", wait_until="networkidle")
        if page.locator("#menuBtn").is_visible():
            page.click("#menuBtn")
            page.wait_for_timeout(150)
            confirm(page, "#navLinks.open", "mobile menu open", size_name)
            scan(page, "public", "/ (mobile menu open)", size_name)

    # A nav dropdown, which is CSS-only and opens on hover or focus.
    # Desktop only: below 899px the whole nav is behind the menu button
    # and the panels are position:static inside it, always visible — so
    # there is no dropdown state to open, and the mobile-menu scan above
    # has already covered those items.
    page.goto(BASE + "/", wait_until="networkidle")
    trigger = page.locator(".nav-group > a").first
    if size_name != "phone" and trigger.count():
        try:
            trigger.focus()
            page.wait_for_timeout(150)
            confirm(page, ".nav-group:focus-within .nav-drop",
                    "nav dropdown open", size_name)
            scan(page, "public", "/ (nav dropdown open)", size_name)
        except Exception:
            pass

    # The lightbox: a role="dialog" with aria-modal, which is the single
    # most exacting piece of markup on the public site.
    page.goto(BASE + "/gallery/%s" % album_slug, wait_until="networkidle")
    photo = page.locator(".photo-item a").first
    if photo.count():
        photo.click()
        page.wait_for_timeout(250)
        confirm(page, "body.lightbox-open #lightbox:not([hidden])",
                "lightbox open", size_name)
        scan(page, "public", "/gallery (lightbox open)", size_name)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for size_name, width, height in SIZES:
        ctx = new_context(browser, width, height, motion=STILL)
        page = ctx.new_page()
        for url in PUBLIC:
            audit(page, url, "public", size_name)
        # Admin needs a session; the login page is audited above as a
        # public page because that is what it is to a visitor.
        page.goto(BASE + "/admin/login", wait_until="load")
        page.fill("input[name=email]", "a11y@example.com")
        page.fill("input[name=password]", PW)
        page.click("button[type=submit]")
        page.wait_for_load_state("load")
        for url in ADMIN:
            audit(page, url, "admin", size_name)
        if size_name == "desktop":
            # /admin, not /admin/login: the login page is a standalone
            # template with no sidebar and nothing to skip, so WCAG's
            # bypass-blocks requirement does not apply to it. The
            # dashboard is where the fifteen sidebar links are.
            check_skip_link(page, "/admin", "admin (/admin)")
        page.goto(BASE + "/admin/logout", wait_until="load")
        if size_name == "desktop":
            check_skip_link(page, "/", "public (/)")
        audit_states(page, size_name, ALBUM_SLUG)
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
for name in FIXTURE_FILES:
    path = os.path.join(UPLOAD_DIR, name)
    if os.path.isfile(path):
        os.remove(path)


# ---------------------------------------------------------------- report
def collect(area):
    """{rule id: {impact, help, pages:set, nodes:int, wcag:bool}}"""
    out = {}
    for (a, url, _size), violations in findings.items():
        if a != area:
            continue
        for v in violations:
            row = out.setdefault(v["id"], {
                "impact": v["impact"] or "minor", "help": v["help"],
                "pages": set(), "nodes": 0, "sample": v["sample"],
                "wcag": any(t.startswith("wcag") for t in v["tags"])})
            row["pages"].add(url)
            row["nodes"] += v["nodes"]
    return out


def report(area, urls):
    rules = collect(area)
    pages = len({u for (a, u, _s) in findings if a == area})
    print()
    print("=" * 74)
    print("%s PAGES - %d audited at %d sizes"
          % (area.upper(), pages, len(SIZES)))
    print("=" * 74)
    if not rules:
        print("  No violations found by axe-core.")
        return rules
    for sev in SEVERITIES:
        hits = {k: v for k, v in rules.items() if v["impact"] == sev}
        if not hits:
            continue
        print("\n  %s (%d rule%s)"
              % (sev.upper(), len(hits), "" if len(hits) == 1 else "s"))
        for rule_id, row in sorted(hits.items(),
                                   key=lambda kv: -len(kv[1]["pages"])):
            scope = ("every page" if len(row["pages"]) == pages
                     else "%d of %d pages" % (len(row["pages"]), pages))
            print("    %-28s %s" % (rule_id, row["help"][:60]))
            print("      %-26s %s, %d element%s%s"
                  % ("", scope, row["nodes"],
                     "" if row["nodes"] == 1 else "s",
                     "" if row["wcag"] else "  [best-practice, not WCAG]"))
            if row["sample"]:
                print("      %-26s first: %s" % ("", row["sample"]))
            worst = sorted(row["pages"])[:4]
            print("      %-26s on: %s%s"
                  % ("", ", ".join(worst),
                     " ..." if len(row["pages"]) > 4 else ""))
    return rules


print()
print("=" * 74)
print("SKIP-TO-CONTENT LINK")
print("=" * 74)
for label, first_stop, visible, text, landed in skip_results:
    ok = first_stop and visible and landed == "main"
    print("  %-24s %s" % (label, "works" if ok else "PROBLEM"))
    print("      first Tab stop: %-5s  visible when focused: %-5s"
          % (first_stop, visible))
    print("      says %-18r  focus after Enter: #%s"
          % (text, landed or "(nothing - focus was left behind)"))

public_rules = report("public", PUBLIC)
admin_rules = report("admin", ADMIN)

print()
print("=" * 74)
print("SUMMARY")
print("=" * 74)
for area, rules in (("public", public_rules), ("admin", admin_rules)):
    counts = {s: len([1 for r in rules.values() if r["impact"] == s])
              for s in SEVERITIES}
    wcag = len([1 for r in rules.values() if r["wcag"]])
    print("  %-8s %s   (%d of %d are WCAG rules, the rest best-practice)"
          % (area,
             "  ".join("%s %d" % (s, counts[s]) for s in SEVERITIES),
             wcag, len(rules)))
if errors:
    print("\n  PAGES THAT COULD NOT BE AUDITED (%d):" % len(errors))
    for area, url, size, why in errors:
        print("    %-8s %-22s %-8s %s" % (area, url, size, why))

print("\n  axe-core %s, WCAG 2.0/2.1 A and AA plus best-practice."
      % AXE_VERSION)
print("  Automated rules catch roughly a third of what matters. This is"
      " a floor,")
print("  not a pass: reading order, whether alt text says the right"
      " thing, and")
print("  whether the site can actually be used with a screen reader are"
      " not")
print("  answered by any of it.")

if JSON_OUT:
    with open(JSON_OUT, "w", encoding="utf-8") as fh:
        json.dump({"%s %s %s" % k: v for k, v in findings.items()}, fh,
                  indent=1)
    print("\n  Full results written to %s" % JSON_OUT)

if STRICT:
    blocking = [k for k, v in public_rules.items()
                if v["impact"] in ("critical", "serious")]
    if blocking:
        print("\n--strict: %d critical/serious rule(s) on public pages."
              % len(blocking))
        raise SystemExit(1)
print("\nAudit complete (reporting only; pass --strict to make it a gate).")
