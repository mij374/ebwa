"""Smoke test for the check-schema CLI command (CLAUDE.md rules).

Covers: a current database passes and exits 0, a missing table and a
missing column are each reported and exit 1, the suggested ALTER matches
the statement recorded in DEPLOY.md, and a leftover table from a retired
module is reported without being treated as a failure.

Runs against a throwaway SQLite db in this folder via DATABASE_URL,
so the real instance/ebwa.db is never touched. Deletes the db afterwards.

Run:  python tests/smoke_test_check_schema.py
"""
import os
import re
import sys

from click.testing import CliRunner

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_ebwa_check_schema.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB.replace("\\", "/")
sys.path.insert(0, os.path.dirname(HERE))

from app import app, db  # noqa: E402

app.config["TESTING"] = True

failures = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        (" [%s]" % detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run_check():
    return CliRunner().invoke(app.cli, ["check-schema"])


def sql(statement):
    with app.app_context():
        db.session.execute(db.text(statement))
        db.session.commit()


with app.app_context():
    db.create_all()

# ---- a current database passes
r = run_check()
check("current database exits 0", r.exit_code == 0, str(r.exit_code))
check("and says so", "Schema is up to date" in r.output, r.output.strip())
check("no false alarms", "MISSING" not in r.output, r.output.strip())

# ---- a missing table is caught
sql("DROP TABLE service")
r = run_check()
check("missing table exits 1", r.exit_code == 1, str(r.exit_code))
check("missing table named", "service" in r.output and "MISSING TABLES" in
      r.output, r.output.strip())
check("points at init-db as the fix", "init-db" in r.output)
check("warns before the restart", "BEFORE" in r.output)
with app.app_context():
    db.create_all()
check("recreating it clears the error", run_check().exit_code == 0)

# ---- a missing column is caught, with a usable ALTER
sql("DROP TABLE partner")
sql("CREATE TABLE partner (id INTEGER NOT NULL PRIMARY KEY, "
    "name VARCHAR(160) NOT NULL, url VARCHAR(300), blurb VARCHAR(300), "
    "sort INTEGER)")     # the pre-b092725 shape: no logo, no display_mode
r = run_check()
check("missing column exits 1", r.exit_code == 1, str(r.exit_code))
check("missing columns named",
      "partner.logo" in r.output and "partner.display_mode" in r.output,
      r.output.strip())
check("MISSING COLUMNS section shown", "MISSING COLUMNS" in r.output)

suggested = set(re.findall(r"^\s+(ALTER TABLE .*?;)$", r.output, re.M))
check("an ALTER is suggested for each", len(suggested) == 2, str(suggested))
deploy_md = open(os.path.join(os.path.dirname(HERE), "DEPLOY.md"),
                 encoding="utf-8").read()
documented = set(re.findall(r"^(ALTER TABLE partner .*?;)$", deploy_md, re.M))
check("suggestions match the statements recorded in DEPLOY.md",
      suggested == documented,
      "suggested %s vs documented %s" % (sorted(suggested),
                                         sorted(documented)))
check("suggestions are told to be checked against DEPLOY.md",
      "DEPLOY.md" in r.output)

# and the suggestions actually work
for statement in sorted(suggested):
    sql(statement.rstrip(";"))
r = run_check()
check("applying the suggestions fixes it", r.exit_code == 0, r.output.strip())

# ---- a leftover table from a retired module is noted, not failed
sql("CREATE TABLE funding_record (id INTEGER NOT NULL PRIMARY KEY, "
    "funder_name VARCHAR(160))")
r = run_check()
check("leftover table does not fail the check", r.exit_code == 0,
      str(r.exit_code))
check("leftover table is mentioned", "funding_record" in r.output,
      r.output.strip())
check("and explained as expected", "never drops tables" in r.output)
check("CLI output stays ASCII (Windows consoles mangle the rest)",
      r.output.isascii(), repr([c for c in r.output if not c.isascii()][:5]))
sql("DROP TABLE funding_record")

# ---- both kinds at once are reported together
sql("DROP TABLE audit_log")
sql("DROP TABLE partner")
sql("CREATE TABLE partner (id INTEGER NOT NULL PRIMARY KEY, "
    "name VARCHAR(160) NOT NULL)")
r = run_check()
check("tables and columns reported together",
      "MISSING TABLES" in r.output and "MISSING COLUMNS" in r.output,
      r.output.strip())
check("still exits 1", r.exit_code == 1, str(r.exit_code))

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
