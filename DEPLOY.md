# Deploy checklist — schema changes

Every commit that changes the database schema has an entry below, newest
first. Tick each environment as you apply it, and commit the tick.

There is no migration tool: schema changes are additive and applied by
hand (see CLAUDE.md → Database changes). This file is the record of what
still needs applying where.

## How to use this

**Fresh database?** None of the `ALTER TABLE` statements below apply.
`flask --app app init-db` creates every table at its current shape and
seeds the default content. Tick everything and move on.

**Existing database?** Work **upwards from the oldest unticked entry**,
in order. Then, once:

```bash
cd /opt/ebwa
git pull
source venv/bin/activate
pip install -r requirements.txt          # only if an entry says so
sqlite3 instance/ebwa.db ".backup instance/ebwa-before-upgrade.db"
# ... the ALTER TABLE statements from each unticked entry, oldest first ...
flask --app app init-db                  # if any entry says so
flask --app app check-schema             # must print "Schema is up to date"
sudo systemctl restart ebwa
```

**The schema step always comes before the restart.** The app queries the
new columns and tables as soon as it starts; restarting first means an
error page for every visitor until you catch up.

`check-schema` is the safety net for exactly that: it compares the models
against the database and exits non-zero if anything is missing, naming
the tables and columns and suggesting the `ALTER TABLE` for each. **If it
does not print "Schema is up to date", do not restart** — fix it first,
and you have caught the problem instead of your visitors. It reads the
database and changes nothing, so it is safe to run any time.

Re-running an `ALTER TABLE` that has already been applied fails with
`duplicate column name` and changes nothing — annoying, not dangerous.
`init-db` is idempotent: it only ever creates what is missing and seeds
what is absent, and never drops or alters anything.

Back up before you start. `instance/ebwa.db` and `static/uploads/` are
the whole site.

### Environment key

- **Local** — a developer's `instance/ebwa.db`
- **Demo VPS** — the client-facing preview
- **Production** — `/opt/ebwa` on the live server

Local was verified on 10 Aug 2026 by reading the schema out of
`instance/ebwa.db`. Demo and production are unticked because Phase 1 has
not been deployed yet — tick them as you go.

---

## b092725 — 2026-08-09 — Add partner logos and a header Donate button

Two new columns on `partner`. No new tables.

```sql
ALTER TABLE partner ADD COLUMN logo VARCHAR(255) DEFAULT '';
ALTER TABLE partner ADD COLUMN display_mode VARCHAR(10) NOT NULL DEFAULT 'text';
```

Existing partners come out as `display_mode = 'text'` with no logo, which
renders exactly as they did before.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 6857b2b — 2026-08-09 — Add editable legal pages and a cookie notice

No schema change. Four new content blocks (`privacy_title`,
`privacy_body`, `terms_title`, `terms_body`) need seeding.

```bash
flask --app app init-db
```

Afterwards: **EBWA must replace the placeholder privacy notice and terms
before launch.** Both say PLACEHOLDER on the page until they do.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 9b7cd52 — 2026-08-09 — Make the service cards and contact details editable

New table `service`, plus six new `contact` blocks.

```bash
flask --app app init-db
```

That creates `service`, seeds the six "What we do" cards **into an empty
table only**, and inserts the new contact blocks. On a database that
already has service cards, nothing is touched — deleted or edited cards
are never resurrected.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 7480a77 — 2026-08-09 — Add append-only audit log

New table `audit_log`, plus the new `audit_log` feature flag.

```bash
flask --app app init-db
```

The app writes to `audit_log` on nearly every admin action, so this one
genuinely cannot wait until after the restart.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 760f42e — 2026-08-08 — Add super-admin user management page

One new column on `user`.

```sql
ALTER TABLE user ADD COLUMN created_at DATETIME;
```

Nullable by necessity: SQLite refuses a `CURRENT_TIMESTAMP` default on
`ADD COLUMN`, so accounts that predate this show "—" in the Users list.
New accounts are stamped.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 5161a39 — 2026-08-08 — Add optional TOTP two-factor authentication

Three new columns on `user`, plus a new `recovery_code` table, plus two
new Python packages.

```bash
pip install -r requirements.txt          # pyotp, qrcode
```

```sql
ALTER TABLE user ADD COLUMN totp_secret VARCHAR(64) DEFAULT '';
ALTER TABLE user ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE user ADD COLUMN totp_last_counter INTEGER;
```

```bash
flask --app app init-db                  # creates recovery_code
```

Existing admins come out with 2FA off and log in exactly as before. TOTP
is time-based, so check the server clock is synced (`timedatectl`).

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## cb9be0e — 2026-08-08 — Add super-admin tier and feature-flag settings

One new column on `user`, plus a new `feature_flag` table.

```sql
ALTER TABLE user ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'admin';
```

```bash
flask --app app init-db                  # creates and seeds feature_flag
```

Every existing admin becomes `role = 'admin'`. Promote the Netbus account
afterwards with `flask --app app promote-super-admin`, or nobody can
reach the Settings and Users pages.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## e209fa3 — 2026-07-18 — Consolidate track record into Our Journey

**Nothing to run.** The `FundingRecord` model was removed in favour of
`Milestone`.

A database that ran `init-db` while 32d3da3 was checked out keeps an
orphan `funding_record` table and a `track_record_intro` block row. Both
are harmless and are deliberately left alone — this project never drops
tables or deletes data.

- [x] Local (orphan `funding_record` table present, as expected)
- [ ] Demo VPS
- [ ] Production

---

## eae8402 — 2026-07-18 — Add Milestones & Track Record module (Variation 2)

New table `milestone`, plus the `journey_intro` block.

```bash
flask --app app init-db
```

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 32d3da3 — 2026-07-18 — Add Funding Track Record module (Variation 1)

**Superseded by e209fa3 — do not apply to a database that never saw it.**

It created a `funding_record` table and a `track_record_intro` block. If
you are bringing a database forward from before 18 Jul 2026, skip
straight past this entry to eae8402.

- [x] Local (applied at the time; now an orphan, see e209fa3)
- [ ] Demo VPS — skip
- [ ] Production — skip

---

## 1f03519 — 2026-07-15 — Add membership eligibility declarations

Four new columns on `membership_application`.

```sql
ALTER TABLE membership_application ADD COLUMN over_18 BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE membership_application ADD COLUMN bangladeshi_origin BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE membership_application ADD COLUMN lives_works_enfield BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE membership_application ADD COLUMN fee_confirmed BOOLEAN NOT NULL DEFAULT 0;
```

Applications submitted before this show all four as unticked, which is
accurate: they were never asked. `bangladeshi_origin` is special-category
data (ethnic origin) — admin-only, and kept out of CSV exports.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 470b0a6 — 2026-07-13 — Add resources, membership, donations modules

Four new tables — `resource`, `membership_application`, `campaign`,
`payment` — plus new content blocks and a new Python package.

```bash
pip install -r requirements.txt          # stripe
flask --app app init-db
```

`payment` carries CHECK constraints enforcing the HMRC Gift Aid rule, so
it must be created by `init-db` rather than by hand.

Also needed before donations work: `STRIPE_SECRET_KEY` and
`STRIPE_WEBHOOK_SECRET` in the systemd unit, and the webhook endpoint
registered in the Stripe dashboard (see README).

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 45428a6 — 2026-07-12 — Add News & Projects module + smoke tests

**Baseline.** The first commit carrying `app.py`, creating `user`,
`block`, `event`, `news_post`, `gallery_image`, `testimonial`, `partner`
and `subscriber`.

```bash
flask --app app init-db
flask --app app create-admin
```

- [x] Local
- [ ] Demo VPS
- [ ] Production
