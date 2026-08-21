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

## 963f7fb — 2026-08-21 — A separate recipient for security alerts

**No schema change** — confirmed with `flask --app app check-schema`. One
new content Block, seeded empty:

```bash
flask --app app init-db     # seeds site_security_alert_to
```

Empty means alerts follow the enquiries address, so nothing changes for
an existing install until somebody sets one. **Set one before enquiries
move to an @ebwa.org.uk mailbox**, or the alerts will follow them there
and the people who need to see them will not.

Settings → Security → *Send security alerts to* takes one address or
several separated by commas, and *Send a test alert* proves it.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 2b42358 — 2026-08-21 — Backups, failed-sign-in visibility, bigger login badge

New table:

```bash
flask --app app init-db     # creates backup_run, seeds the alert Block
```

Two optional environment variables, both with working defaults:

```
BACKUP_DIR=/opt/ebwa/backups     # default: backups/ beside the app
BACKUP_KEEP=7                    # archives to keep
```

Then set the nightly jobs up — the app writes archives, it does NOT copy
them anywhere:

```cron
15 2 * * * cd /opt/ebwa && set -a && . /etc/ebwa/env && set +a && ./venv/bin/flask --app app backup-now >> /var/log/ebwa-backup.log 2>&1
45 2 * * * rsync -az --delete -e 'ssh -i /root/.ssh/ebwa-backup' /opt/ebwa/backups/ backup@nas.example.net:/volume1/backups/ebwa/ >> /var/log/ebwa-offsite.log 2>&1
```

**The second line is the one that makes it a backup.** Without it there
is a copy of the database sitting on the same disk as the database.

Check afterwards that `BACKUP_DIR` is writable by the service user, and
that the first archive actually opens:

```bash
sudo -u www-data cd /opt/ebwa && ./venv/bin/flask --app app backup-now
unzip -l /opt/ebwa/backups/ebwa-backup-*.zip | head
```

Failed-sign-in alert emails are off until a super admin ticks the box on
Settings → Security; they use the SMTP settings already configured.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 859171b — 2026-08-21 — SMTP settings on the Settings page

**No schema change.** Six new content Blocks, all seeded empty:

```bash
flask --app app init-db     # seeds smtp_host/port/user/security/from
```

Empty means "use the environment variable", so **an existing deployment
behaves exactly as it did** — the variables set in the last release stay
in force until a super admin types something into Settings → Email. The
page shows, per setting, whether the value in force came from the page or
from the server.

`SMTP_PASSWORD` stays in the environment. It is not stored in the
database, not rendered, and has no input on the page; the page shows only
whether it is set. There is nothing to migrate.

`init-db` is not strictly required — the rows are created on first save,
and a missing row already falls back to the environment — but running it
keeps a fresh install and an upgraded one identical.

After deploying, prove email from the Settings page ("Send a test email",
five an hour) or from the command line:

```bash
flask --app app test-mail            # now prints where each value came from
```

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 75d95e4 — 2026-08-21 — Contact form and outgoing email

New table, no `ALTER TABLE`, plus the first environment variables for
email.

```bash
flask --app app init-db     # creates contact_message, seeds the flag
                            # and the site_mail_to block
```

Set these in the service environment (systemd unit or the env file it
reads) — NOT in the database, and never in the repository:

```
SMTP_HOST=smtp.example.net
SMTP_PORT=587               # 465 uses implicit TLS instead of STARTTLS
SMTP_USER=...               # omit for an unauthenticated relay
SMTP_PASSWORD=...
SMTP_USE_TLS=1
MAIL_FROM=website@ebwa.org.uk
MAIL_TO=enquiries@ebwa.org.uk
```

Then prove it, BEFORE announcing the form:

```bash
flask --app app test-mail            # sends to MAIL_TO
flask --app app test-mail you@example.com
```

Until SMTP is configured the form still works and still saves every
enquiry — they are read at /admin/messages — but nothing is emailed, and
each attempt is recorded in the audit log. A super admin can change the
recipient afterwards under Settings without a redeploy.

The seeded privacy-notice placeholder now mentions contact enquiries.
`init-db` only inserts MISSING blocks, so an environment that already has
the old text keeps it — update it by hand in Page content → legal (it is
placeholder copy EBWA has to replace before launch anyway).

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 4c97233 — 2026-08-21 — FAQ module

New table, no `ALTER TABLE`.

```bash
flask --app app init-db     # creates faq, seeds the `faq` feature flag
```

`init-db` also inserts the new `faq` feature flag row, on by default. The
page starts empty and says so until EBWA writes some questions; the nav
and footer links appear as soon as the flag is on, whether or not there
are questions yet, so add a few before the deploy is announced.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## f5d65e5 — 2026-08-21 — Gallery albums

One new table and one new column.

```sql
ALTER TABLE gallery_image ADD COLUMN album_id INTEGER;
```

```bash
flask --app app init-db     # creates gallery_album
```

`album_id` is nullable and every existing photo stays NULL — unfiled.
Nothing disappears: /gallery/all lists every photo that is not in a
hidden album, and the gallery index links to it. Until someone creates
an album, the public gallery shows that one "All photos" card.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## f453007 — 2026-08-21 — Optimise uploaded images

**No schema change** — confirmed with `flask --app app check-schema`. Two
things to run, in this order:

```bash
pip install -r requirements.txt      # adds Pillow
flask --app app reprocess-images     # optimises what is already uploaded
```

`reprocess-images` rewrites oversized files in place, strips their EXIF
(GPS included) and writes a `<name>-thumb.<ext>` beside each one. It
never renames a file, so nothing in the database changes, and it is safe
to re-run — a second pass reports "0 optimised". It can be run before or
after the restart; until it has run, pages simply serve the full-size
files as they do today.

**Back up `static/uploads/` first.** The originals are re-encoded in
place, so the pre-optimisation files only exist in your backup.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 9c1265f — 2026-08-21 — Apply rich content to Our Journey

**Nothing to run.** `milestone.layout` and `content_image` have both
existed since the rich-content commit (7921130), so this is routes,
templates and stylesheet only — confirmed with
`flask --app app check-schema` ("Schema is up to date").

Every milestone keeps its single image until someone opens the new
manager on it, at which point that image becomes the lead attachment and
the `image` column keeps its value. Nothing on the public page changes
for a milestone nobody edits.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## dee0a38 + b7896d0 — 2026-08-21 — Rework the admin dashboard into an overview

**Nothing to run.** Routes, one template and stylesheet rules only — no
new tables, no new columns and no new seeded blocks. Confirmed with
`flask --app app check-schema` ("Schema is up to date"). Covers both
dashboard commits — `dee0a38` and `b7896d0` — which were merged together
on `main`.

The dashboard now counts existing content and reads the existing feature
flags, Block values, payments and audit log, so it works against any
database that is already up to date with the entries below.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## ed71f89 — 2026-08-10 — Apply rich content to News and Events

**Nothing to run.** Both models already had their `layout` column and
`content_image` already exists, so this is templates and routes only —
confirmed with `flask --app app check-schema`.

Each post and event keeps its single image until someone opens the new
manager on it, at which point that image becomes the lead attachment and
the `image` column keeps its value for the listing cards.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 7921130 — 2026-08-10 — Add the rich-content system, applied to About

New `content_image` table, plus a `layout` column on the three content
models that will use the presets next.

```sql
ALTER TABLE event ADD COLUMN layout VARCHAR(20) NOT NULL DEFAULT 'classic';
ALTER TABLE milestone ADD COLUMN layout VARCHAR(20) NOT NULL DEFAULT 'classic';
ALTER TABLE news_post ADD COLUMN layout VARCHAR(20) NOT NULL DEFAULT 'classic';
```

```bash
flask --app app init-db     # creates content_image, seeds about_layout + the flag
```

Only About renders the presets so far; the three columns default to
`classic` and are inert until news, events and Our Journey are wired up.
The existing single About image is untouched on disk and keeps working —
it migrates into `content_image` the first time someone uses the image
manager. Nothing changes on the public site until then.

- [x] Local
- [ ] Demo VPS
- [ ] Production

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
