"""The homepage's plain/tinted bands must follow WHAT IS RENDERED.

Six sections can each be absent — a feature flag off, or simply nothing
to show this month — which is 64 arrangements. The classes used to be
written into the template one at a time, so the sequence was right for
exactly one of those 64: hide a section and the two either side come out
the same colour, and the page has a seam in it where a band quietly
doubles in height.

This file checks all 64 twice over. First against the helper, which is a
pure function and can be exhausted in a blink. Then against the ACTUAL
RENDERED PAGE for all 64, because a helper that returns the right answer
and a template that asks it the wrong question look identical from the
helper's side — the list of sections in the template has to be in the
same order the page emits them, and only the rendered HTML can say so.

Run:  python tests/smoke_test_home_bands.py
"""
import itertools
import os
import re
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_home_bands.db")
for _s in ("", "-wal", "-shm"):
    if os.path.isfile(TEST_DB + _s):
        os.remove(TEST_DB + _s)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, Block, DEFAULT_BLOCKS, FEATURES,  # noqa: E402
                 FeatureFlag, Service, Event, NewsPost, Campaign,
                 Testimonial, Partner, alternating_bands)

app.config["TESTING"] = True
failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# The order the homepage emits them in. Must match the list in
# templates/index.html — Part B is what proves it does.
ORDER = ["services", "events", "news", "collections", "testimonials",
         "partners"]

# Part A passes None for "hidden"; Part B only ever sees a real section.
NONE_IS_A_FAULT = False


def band_faults(bands):
    """Everything wrong with one arrangement, as readable sentences.

    In Part A a None means "this section is hidden" and is skipped. In
    Part B `rendered_bands` never yields None for a hidden section (it
    is not in the HTML at all), so a None there means a section with no
    class attribute — named below rather than treated as plain.
    """
    faults = ["section %d has no band class at all" % (i + 1)
              for i, b in enumerate(bands) if b is None and NONE_IS_A_FAULT]
    visible = [b for b in bands if b is not None]
    for i in range(1, len(visible)):
        if visible[i] == visible[i - 1]:
            faults.append("sections %d and %d are both %s"
                          % (i, i + 1, visible[i] or "plain"))
    if visible and visible[0] != "":
        faults.append("the first visible section is %s, not plain"
                      % visible[0])
    return faults


# ---- Part A: the helper, over all 64 arrangements --------------------
print("---- the helper, exhaustively")
worst = None
for combo in itertools.product([True, False], repeat=len(ORDER)):
    got = alternating_bands(list(zip(ORDER, combo)))
    bands = [got[n] if on else None for n, on in zip(ORDER, combo)]
    faults = band_faults(bands)
    if faults and worst is None:
        worst = (combo, faults)
check("all 64 arrangements alternate correctly", worst is None,
      str(worst))
check("a hidden section gets no band at all",
      alternating_bands([("a", False)])["a"] == "")
check("an unlisted section is plain rather than shifting the rest",
      alternating_bands([("a", True)]).get("nope", "") == "")
# The one arrangement that used to be right must still be right, or the
# page has silently changed appearance for everybody.
allon = alternating_bands([(n, True) for n in ORDER])
check("with everything visible the bands are unchanged from before",
      [allon[n] for n in ORDER]
      == ["", "tinted", "", "tinted", "", "tinted"],
      str([allon[n] for n in ORDER]))


# ---- Part B: the rendered page, over all 64 --------------------------
with app.app_context():
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
    for n, _l, _d, default in FEATURES:
        if not FeatureFlag.query.filter_by(name=n).first():
            db.session.add(FeatureFlag(name=n, enabled=default))
    db.session.commit()

client = app.test_client()

MAKERS = {
    "services": lambda: Service(title="Advice", description="Words.",
                                icon="*", sort=0, published=True),
    "events": lambda: Event(title="Coffee morning", slug="coffee-morning",
                            event_date=date.today() + timedelta(days=7),
                            description="Words.", published=True),
    "news": lambda: NewsPost(title="A post", slug="a-post", body="Words.",
                             published_date=date.today(), published=True),
    "collections": lambda: Campaign(title="Trip", slug="trip",
                                    description="Words.", state="open"),
    "testimonials": lambda: Testimonial(name="A person", quote="Words.",
                                        published=True),
    "partners": lambda: Partner(name="A partner", sort=0,
                                display_mode="text"),
}
MODELS = [Service, Event, NewsPost, Campaign, Testimonial, Partner]

# Only the two flags that gate a homepage section. The other three are
# checked at the end, where the point is that they change nothing here.
FLAG_FOR = {"news": "news", "collections": "donations"}

# Matches a <section> WITH OR WITHOUT a class attribute. Matching only
# `class="..."` would silently skip a section that had been given no
# band at all — which is the very mistake this file is here to catch.
SECTION_RE = re.compile(r'<section(?:\s+class="([^"]*)")?[^>]*>')


def main_html():
    """Just <main>. The nav and the footer carry some of the same words —
    "News &amp; projects" is a dropdown link as well as a section
    heading — so a marker looked for in the whole page finds the menu
    and reports a hidden section as still on the page."""
    html = client.get("/").data.decode("utf-8")
    return html.split("<main", 1)[1].split("</main>", 1)[0]


def rendered_bands():
    """The band of each rendered section, in document order, hero aside.

    None means the section carries no class attribute, so it is not in
    the band system at all — reported as its own fault rather than being
    quietly counted as plain.
    """
    main = main_html()
    # finditer, NOT findall: for an optional group that did not
    # participate findall gives "" and finditer gives None, and the
    # difference here is "plain section" against "section with no class
    # attribute at all" — the second being the mistake worth catching.
    return [m.group(1) for m in SECTION_RE.finditer(main)
            if m.group(1) != "hero"]


def set_content(present):
    with app.app_context():
        for model in MODELS:
            for row in model.query.all():
                db.session.delete(row)
        for flag in FeatureFlag.query.all():
            flag.enabled = True
        for name, on in zip(ORDER, present):
            if on:
                db.session.add(MAKERS[name]())
        db.session.commit()


print()
print("---- the rendered page, all 64 arrangements")
NONE_IS_A_FAULT = True
bad = []
for combo in itertools.product([True, False], repeat=len(ORDER)):
    set_content(combo)
    got = rendered_bands()
    expected_count = sum(combo)
    if len(got) != expected_count:
        bad.append((combo, "rendered %d sections, expected %d"
                    % (len(got), expected_count)))
        continue
    faults = band_faults(got)
    if faults:
        bad.append((combo, "; ".join(faults)))
check("all 64 rendered arrangements alternate correctly", not bad,
      "%d wrong, first: %s" % (len(bad), bad[0]) if bad else "")

set_content([True] * len(ORDER))
check("everything visible: six sections in the expected order",
      rendered_bands() == ["", "tinted", "", "tinted", "", "tinted"],
      str(rendered_bands()))

# The template's list must be in the SAME ORDER the page emits sections.
# Rendering one section at a time makes a wrong order impossible to miss:
# each must come out plain, being the only one on the page.
for i, name in enumerate(ORDER):
    only = [j == i for j in range(len(ORDER))]
    set_content(only)
    got = rendered_bands()
    check("%s alone renders one plain section" % name,
          got == [""], str(got))


# ---- Part C: the feature flags ---------------------------------------
print()
print("---- feature flags")


def set_flags(**states):
    with app.app_context():
        for flag in FeatureFlag.query.all():
            flag.enabled = states.get(flag.name, True)
        db.session.commit()


set_content([True] * len(ORDER))
for label, off in (("news off", {"news": False}),
                   ("donations off", {"donations": False}),
                   ("news and donations off",
                    {"news": False, "donations": False})):
    set_flags(**off)
    got = rendered_bands()
    check("%s: still alternates" % label, not band_faults(got), str(got))
    check("%s: the right number of sections" % label,
          len(got) == 6 - len(off), str(got))

# The other three gate pages, not homepage sections: switching them off
# must not move a single band. If one of them ever grows a homepage
# section, this check fails and points at the list in index.html.
set_flags()
baseline = rendered_bands()
for label, off in (("faq off", {"faq": False}),
                   ("resources off", {"resources": False}),
                   ("our_journey off", {"our_journey": False}),
                   ("faq, resources and our_journey off",
                    {"faq": False, "resources": False,
                     "our_journey": False})):
    set_flags(**off)
    check("%s: the homepage bands are untouched" % label,
          rendered_bands() == baseline, str(rendered_bands()))

# Everything off at once, content and flags both.
set_flags(**{name: False for name, _l, _d, _x in FEATURES})
set_content([False] * len(ORDER))
got = rendered_bands()
check("nothing to show at all: no sections, and no crash", got == [],
      str(got))

# ---- Part D: a configurable order --------------------------------------
print()
print("---- the order setting")
import itertools as _it     # noqa: E402  (kept local to this section)
from app import (home_section_order, home_hidden_sections,  # noqa: E402
                 HOME_SECTION_KEYS, HOME_ORDER_KEY, HOME_HIDDEN_KEY,
                 HOME_ORDER_DEFAULT)


def set_setting(key, value):
    with app.app_context():
        block = Block.query.filter_by(key=key).first()
        if block is None:
            block = Block(group="home", key=key, label=key, kind="text")
            db.session.add(block)
        block.value = value
        db.session.commit()


def resolved():
    with app.app_context():
        return home_section_order()


set_flags()
set_content([True] * len(ORDER))
set_setting(HOME_ORDER_KEY, HOME_ORDER_DEFAULT)
set_setting(HOME_HIDDEN_KEY, "")
check("the shipped setting resolves to the shipped order",
      resolved() == list(HOME_SECTION_KEYS), str(resolved()))
check("and renders exactly as it did before the setting existed",
      rendered_bands() == ["", "tinted", "", "tinted", "", "tinted"],
      str(rendered_bands()))

# THE TWO FALLBACKS. Both are about the same failure: a section must not
# disappear because somebody edited a list.
set_setting(HOME_ORDER_KEY, "partners,nonsense,services")
check("a key that is not a section is ignored",
      "nonsense" not in resolved(), str(resolved()))
check("SECTIONS MISSING FROM THE SETTING ARE APPENDED, NOT DROPPED",
      sorted(resolved()) == sorted(HOME_SECTION_KEYS), str(resolved()))
check("and the named ones keep the order they were given",
      resolved()[:2] == ["partners", "services"], str(resolved()))
check("nothing renders twice",
      len(resolved()) == len(set(resolved())), str(resolved()))
set_setting(HOME_ORDER_KEY, "services,services,events")
check("a key repeated in the setting appears once",
      resolved().count("services") == 1, str(resolved()))
set_setting(HOME_ORDER_KEY, "")
check("an empty setting is the shipped order",
      resolved() == list(HOME_SECTION_KEYS), str(resolved()))
set_setting(HOME_ORDER_KEY, "   ,  ,")
check("a setting of nothing but separators is too",
      resolved() == list(HOME_SECTION_KEYS), str(resolved()))

# A DATABASE WHERE THE BLOCKS DO NOT EXIST AT ALL — a deploy that pulled
# the code and forgot `init-db`. DEPLOY.md promises the front page is
# still correct in that state, so it is checked rather than assumed: a
# homepage that loses its content to a forgotten command is exactly the
# failure this setting must not introduce.
with app.app_context():
    for key in (HOME_ORDER_KEY, HOME_HIDDEN_KEY):
        row = Block.query.filter_by(key=key).first()
        if row:
            db.session.delete(row)
    db.session.commit()
    check("with the settings Blocks ABSENT the order is the shipped one",
          home_section_order() == list(HOME_SECTION_KEYS),
          str(home_section_order()))
    check("...and nothing is hidden",
          home_hidden_sections() == set(), str(home_hidden_sections()))
set_content([True] * len(ORDER))
check("...and the page renders exactly as it always did",
      rendered_bands() == ["", "tinted", "", "tinted", "", "tinted"],
      str(rendered_bands()))
# Saving from the Settings page must then CREATE them rather than fail.
# (The panel's own route is exercised in Part F; this is the helper.)
set_setting(HOME_ORDER_KEY, HOME_ORDER_DEFAULT)
set_setting(HOME_HIDDEN_KEY, "")
check("a settings save creates a Block that was never seeded",
      resolved() == list(HOME_SECTION_KEYS), str(resolved()))

# The bands must recompute from whatever order is set. Six orders, and
# for each of them all 64 content arrangements — the same exhaustion as
# Part B, now with the order moving underneath it.
ORDERS = [
    list(HOME_SECTION_KEYS),
    list(reversed(HOME_SECTION_KEYS)),
    ["partners", "services", "testimonials", "events", "collections", "news"],
    ["testimonials", "news", "partners", "collections", "services", "events"],
    ["events", "services"],                      # the rest get appended
    ["collections"],                             # ...and again
]
bad_order = []
for custom in ORDERS:
    set_setting(HOME_ORDER_KEY, ",".join(custom))
    expected = resolved()
    for combo in _it.product([True, False], repeat=len(ORDER)):
        set_content(combo)
        got = rendered_bands()
        if len(got) != sum(combo):
            bad_order.append((custom, combo, "rendered %d, expected %d"
                              % (len(got), sum(combo))))
            continue
        faults = band_faults(got)
        if faults:
            bad_order.append((custom, combo, "; ".join(faults)))
check("all 64 arrangements alternate under each of 6 custom orders",
      not bad_order,
      "%d wrong, first: %s" % (len(bad_order), bad_order[0])
      if bad_order else "")

# The page really is emitting them in the configured order, not merely
# alternating. Each section's own heading proves which partial ran.
MARKERS = {
    "services": "Support for every generation",
    "events": "Upcoming events",
    "news": "News &amp; projects",
    "collections": "Help us get there",
    "testimonials": "What our community says",
    "partners": "Working together for Enfield",
}
set_content([True] * len(ORDER))
for custom in ORDERS[:4]:
    set_setting(HOME_ORDER_KEY, ",".join(custom))
    want = resolved()
    main = main_html()
    seen = sorted(want, key=lambda k: main.index(MARKERS[k]))
    check("order %s renders in that order" % ",".join(custom[:3]),
          seen == want, "got %s" % seen)

# ---- Part E: hiding a section from the homepage only -------------------
print()
print("---- the per-section switches")
set_setting(HOME_ORDER_KEY, HOME_ORDER_DEFAULT)
set_content([True] * len(ORDER))
for hidden in ([], ["news"], ["events", "testimonials"],
               ["services", "news", "partners"],
               list(HOME_SECTION_KEYS)):
    set_setting(HOME_HIDDEN_KEY, ",".join(hidden))
    got = rendered_bands()
    check("hiding %s: the right sections render"
          % (", ".join(hidden) or "nothing"),
          len(got) == len(HOME_SECTION_KEYS) - len(hidden), str(got))
    check("hiding %s: and the bands still alternate"
          % (", ".join(hidden) or "nothing"),
          not band_faults(got), str(got))
    main = main_html()
    check("hiding %s: none of them is on the page"
          % (", ".join(hidden) or "nothing"),
          all(MARKERS[k] not in main for k in hidden))

set_setting(HOME_HIDDEN_KEY, "news,nonsense")
with app.app_context():
    check("an unknown key in the hidden list is ignored",
          home_hidden_sections() == {"news"},
          str(home_hidden_sections()))

# HIDING IS NOT A FEATURE FLAG. The section goes from the front page and
# nothing else moves: the module's own page still answers, and it is
# still in the sitemap.
set_setting(HOME_HIDDEN_KEY, "news")
check("a section hidden from the homepage keeps its own page",
      client.get("/news").status_code == 200)
check("...and its sitemap entry",
      b"/news" in client.get("/sitemap.xml").data)
set_setting(HOME_HIDDEN_KEY, "")

# ---- Part F: the admin controls ----------------------------------------
print()
print("---- the Settings panel")
from werkzeug.security import generate_password_hash    # noqa: E402
from app import User, AuditLog                          # noqa: E402

PW = "home-sections-password"
with app.app_context():
    db.session.add(User(email="netbus@example.com",
                        password_hash=generate_password_hash(PW),
                        role="super_admin"))
    db.session.add(User(email="client@example.com",
                        password_hash=generate_password_hash(PW),
                        role="admin"))
    db.session.commit()

SAVE = "/admin/home-sections"
RESET = "/admin/home-sections/reset"

anon = app.test_client()
for path in (SAVE, RESET):
    check("anonymous POST %s redirects to login" % path,
          anon.post(path).status_code == 302)

client_admin = app.test_client()
client_admin.post("/admin/login", data={"email": "client@example.com",
                                        "password": PW})
for path in (SAVE, RESET):
    check("a client admin gets 403 from %s" % path,
          client_admin.post(path).status_code == 403,
          str(client_admin.post(path).status_code))

boss = app.test_client()
boss.post("/admin/login", data={"email": "netbus@example.com",
                                "password": PW})
check("a super admin can see the panel",
      b"Homepage sections" in boss.get("/admin/features").data)


def positions(order, hidden=()):
    """The form a browser would post for this arrangement."""
    data = {"pos_%s" % k: str(i + 1) for i, k in enumerate(order)}
    for k in order:
        if k not in hidden:
            data["show_%s" % k] = "on"
    return data


set_setting(HOME_ORDER_KEY, HOME_ORDER_DEFAULT)
set_setting(HOME_HIDDEN_KEY, "")
wanted = ["partners", "news", "services", "collections", "events",
          "testimonials"]
boss.post(SAVE, data=positions(wanted))
check("saving a new order applies it", resolved() == wanted, str(resolved()))
check("and the page renders in it",
      [k for k in resolved()] == wanted)

boss.post(SAVE, data=positions(wanted, hidden=["news", "events"]))
with app.app_context():
    check("unticking a section hides it from the homepage",
          home_hidden_sections() == {"news", "events"},
          str(home_hidden_sections()))
check("and it really leaves the page", MARKERS["news"] not in main_html())

# Two sections given the same number is an arrangement, not an error:
# the tie keeps the order they are already in.
current = resolved()
tied = {"pos_%s" % k: "1" for k in current}
for k in current:
    tied["show_%s" % k] = "on"
boss.post(SAVE, data=tied)
check("every position the same leaves the order as it was",
      resolved() == current, str(resolved()))

for bad_data, why in (
        ({"pos_services": "nought"}, "not a number"),
        ({"pos_services": "0"}, "below the range"),
        ({"pos_services": "99"}, "above the range"),
        ({"pos_services": ""}, "empty")):
    before = resolved()
    data = positions(before)
    data.update(bad_data)
    r = boss.post(SAVE, data=data, follow_redirects=True)
    check("a position that is %s is refused" % why,
          resolved() == before, str(resolved()))
    check("...and says so" % (), b"whole number" in r.data
          or b"between 1 and" in r.data)

boss.post(SAVE, data=positions(["partners", "services", "news", "events",
                                "collections", "testimonials"],
                               hidden=["partners"]))
r = boss.post(RESET, follow_redirects=True)
check("reset puts the shipped order back",
      resolved() == list(HOME_SECTION_KEYS), str(resolved()))
with app.app_context():
    check("and shows every section again",
          home_hidden_sections() == set(), str(home_hidden_sections()))

with app.app_context():
    entries = (AuditLog.query.order_by(AuditLog.id.desc()).limit(12).all())
    summaries = [e.summary for e in entries]
check("the reset is audit-logged",
      any("Reset the homepage sections" in x for x in summaries),
      str(summaries[:3]))
check("a save is audit-logged, naming the arrangement in plain language",
      any("Changed the homepage sections" in x and "What we do" in x
          for x in summaries), str(summaries[:3]))
check("the log never prints a raw section key",
      not any("collections,testimonials" in x for x in summaries))

# Pressing reset when nothing needs resetting is still recorded.
before_n = len(summaries)
boss.post(RESET)
with app.app_context():
    last = (AuditLog.query.order_by(AuditLog.id.desc()).first().summary)
check("a reset that changes nothing is logged too",
      "Reset the homepage sections" in last and "nothing changed" in last,
      last)

# ---- teardown --------------------------------------------------------
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
