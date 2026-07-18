"""Smoke test for seed_demo.py per-section idempotence.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_seed_demo.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_seed_demo.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import (app, db, Block, Event, Milestone, NewsPost,  # noqa: E402
                 Partner, Testimonial)
import seed_demo  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    # ---- first run on a fresh db seeds every section
    seed_demo.seed()
    check("events seeded", Event.query.count() == len(seed_demo.EVENTS),
          str(Event.query.count()))
    check("news seeded", NewsPost.query.count() == len(seed_demo.NEWS))
    check("testimonials seeded",
          Testimonial.query.count() == len(seed_demo.TESTIMONIALS))
    check("partners seeded", Partner.query.count() == len(seed_demo.PARTNERS))
    check("milestones seeded",
          Milestone.query.count() == len(seed_demo.MILESTONES))
    hero = Block.query.filter_by(key="home_hero_title").first()
    check("demo copy filled into blocks",
          hero and hero.value == seed_demo.BLOCK_VALUES["home_hero_title"])

    # ---- simulate an already-seeded db that lacks a newly added section:
    # delete only milestones, edit a block like an admin would, re-run
    Milestone.query.delete()
    hero.value = "Custom edited headline"
    db.session.commit()
    event_ids = sorted(e.id for e in Event.query.all())

    seed_demo.seed()
    check("milestones re-seeded",
          Milestone.query.count() == len(seed_demo.MILESTONES),
          str(Milestone.query.count()))
    check("event count unchanged",
          Event.query.count() == len(seed_demo.EVENTS),
          str(Event.query.count()))
    check("event rows untouched",
          sorted(e.id for e in Event.query.all()) == event_ids)
    check("news count unchanged",
          NewsPost.query.count() == len(seed_demo.NEWS))
    check("testimonial count unchanged",
          Testimonial.query.count() == len(seed_demo.TESTIMONIALS))
    check("partner count unchanged",
          Partner.query.count() == len(seed_demo.PARTNERS))
    check("edited block not overwritten",
          Block.query.filter_by(key="home_hero_title").first().value
          == "Custom edited headline")

# ---- teardown: delete the throwaway db (incl. WAL sidecars)
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
