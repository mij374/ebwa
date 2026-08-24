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


def rendered_bands():
    """The band of each rendered section, in document order, hero aside.

    None means the section carries no class attribute, so it is not in
    the band system at all — reported as its own fault rather than being
    quietly counted as plain.
    """
    html = client.get("/").data.decode("utf-8")
    main = html.split("<main", 1)[1].split("</main>", 1)[0]
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
