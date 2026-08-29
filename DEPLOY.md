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
the whole site. If one of these steps goes wrong and you need to put
the site back, [RESTORE.md](RESTORE.md) is the procedure — including
why a restored database must never keep the old `-wal` beside it.

### Environment key

- **Local** — a developer's `instance/ebwa.db`
- **Demo VPS** — the client-facing preview
- **Production** — `/opt/ebwa` on the live server

**A TICK MEANS VERIFIED APPLIED, NOT INTENDED.** Tick a box only when
you have seen evidence that this entry's own steps ran in that
environment - `check-schema` clean, the statement executed, the page
working - and never because the deploy was planned, scheduled or
believed to have happened. An unticked box is not a claim that the step
is missing; it means nobody has checked. Where a step cannot be checked
from where you are, say so beside the entry rather than ticking it
hopefully.

Local was verified on 10 Aug 2026 by reading the schema out of
`instance/ebwa.db`, and again on 23 Aug for the audit_log indexes.
Demo VPS ticks were established on 24 Aug — see the verification record
below for exactly how, and for what is still unchecked there.
**Production has never been deployed**: it has no host yet.

---

## Verification of the demo VPS - 24 Aug 2026

Every entry in this file had been ticked Local only, which was wrong:
demo.netbus.co.uk has been running the site for some time. This is what
was checked that day, how, and what could NOT be established - so that
the next person reads ticks rather than guesses.

**Method: anonymous HTTPS requests to the public pages, nothing else.**
No shell on the box, no admin login, no POSTs. That bounds what can be
proven, and the bound is the point of the notes below.

**What that proved:**

- The deployed stylesheet is byte-identical to this repository's
  (`?v=d650ab11` matches the local sha256), and the homepage carries
  every marker of the newest front-end work - `setupMarquee`,
  `fitWholeCards`, `data-row="testimonials"`, `foot-donate`,
  `menu-open`, `nav-group-last`, versioned asset URLs. **The demo is
  running code at or after the whole-card fitting commit (7f38b04,
  23 Aug).**
- `/news/<slug>`, `/events/<slug>`, `/collections/<slug>`,
  `/our-journey` and `/about` all return 200. Every one of those loads
  rows carrying `video_url` / `video_thumb`, so **the eight video
  ALTER TABLEs have been applied** - a missing column would 500 the
  page rather than degrade.
- Rich-content markup renders on About and on a news post; partner
  logos, thumbnails (`-thumb`), the cookie notice, the Donate pill and
  the testimonial scroller are all live; `/faq`, `/resources`,
  `/membership`, `/donate`, `/gallery/all`, `/privacy`, `/terms`,
  `/sitemap.xml` and `/healthz` all answer 200.

**What could NOT be established this way, and is therefore NOT ticked:**

1. **The exact deployed commit.** CSS identity places it at or after
   7f38b04; the three commits after that (aadfa0e, 4e69c7c, a8fdd96)
   change templates, tests and the CLI only, and none of them alters
   any publicly visible output. Settle it with
   `git -C /opt/ebwa rev-parse --short HEAD`.
2. **The two `audit_log` indexes.** An index is invisible over HTTP by
   definition - it changes speed, not behaviour. Nothing on the public
   site can tell you whether they exist.
3. **Everything admin-only**: the audit log table, `user.role`,
   `user.created_at`, the TOTP columns, the membership eligibility
   columns, `backup_run` and its five SFTP columns, the contact
   messages table, the SMTP and NAS settings. All are reached only
   behind a login or a POST, and neither was used.
4. **Block seeds.** A missing Block does not break a page - it falls
   back to a default - so no seeded setting can be confirmed from
   outside. `flask --app app init-db` is idempotent and inserts only
   missing keys, so running it once settles every one of these at no
   cost.

**Two commands close all four gaps**, and both are read-only apart from
the second's idempotent seeding:

```bash
git -C /opt/ebwa rev-parse --short HEAD
cd /opt/ebwa && ./venv/bin/flask --app app check-schema
```

`check-schema` now reports missing INDEXES as well as columns, so its
output alone settles points 2 and 3. Paste the result here and the
remaining boxes can be ticked on evidence.

---

## d58d721 — 2026-08-29 — Membership fees and renewals

**TWO NEW TABLES AND ONE NEW BLOCK. No ALTER TABLE.**

```bash
cd /opt/ebwa
git pull
flask --app app init-db          # creates member + membership_payment,
                                 # and seeds the fee-terms block
flask --app app check-schema     # must say 27 tables, all present
sudo systemctl restart ebwa
```

`check-schema` on a database that has not had `init-db` names both
tables and exits 1, so a missed step is a failed check rather than a 500
for every visitor.

### The seventeen existing members — run ONCE, by hand

```bash
flask --app app seed-members
```

**Not part of `init-db`, deliberately.** `init-db` seeds fixtures and is
idempotent by design; these are seventeen real people, and a deploy that
re-ran `init-db` must never resurrect a member somebody deliberately
deleted. The command skips anybody already on the roll, so a second run
adds nothing, and it refuses outright if members already exist unless
you pass `--force`.

**They arrive with no payments and no join date**, and their status
reads **"No payment recorded"** — not lapsed, which would accuse real
members of owing money this system has never seen, and not current,
which would claim they had paid when nobody knows. The dashboard asks
somebody to resolve it. That is the intended state on day one.

### The feature flag is OFF and nothing changes until it is on

`membership_fees` ships **off**. Until a super admin switches it on:
no public payment page, no fee settings, no renewal chasing on the
dashboard, and no "Record a payment" button. The member records, their
payment history, the treasurer's report and the exports are all
reachable either way — a payment already taken is part of the accounts,
not a feature.

### Before switching it on

1. **Set the fee** on Settings → Membership fees (it defaults to £10)
   and check the grace period (30 days, taking it to 30 October).
2. **The terms page must say fees are non-refundable.** The payment form
   says it already, from an editable block; the terms are EBWA's own
   words and the dashboard will nag until they mention refunds.
3. **Stripe must be configured** — the same keys and the same webhook as
   donations. No new Stripe setup: membership uses the existing
   integration, and the webhook now completes membership payments as
   well as donations.

### If it is ever switched back off

Nothing is deleted and nothing is hidden in the admin. A payment already
in flight still completes: the webhook is deliberately not gated on the
flag, because taking somebody's money and recording nothing is worse
than the feature being on for another minute.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## f9164af — 2026-08-28 — "How to add a domain" on Settings

**NO SCHEMA CHANGE, NO NEW PACKAGE, NOTHING TO RUN.** `git pull` and
restart:

```bash
cd /opt/ebwa
git pull
flask --app app check-schema     # unchanged, still clean
sudo systemctl restart ebwa
```

**What changed.** Settings gains a **Domains** section with a collapsed
box, *How to add a domain to this site*: the DNS A record at the
registrar, adding the name to `server_name`, `nginx -t` then reload, and
certbot. It is guidance only — there is no button, no form and nothing
in it that submits anything, for the same reason there is no restart
button on that page: nginx belongs to root, and a bad config takes the
whole site down including the page somebody would then be looking at.

**One optional new environment variable**, `DEPLOY_NGINX_SITE`. Nothing
reads or writes it; it is printed in those instructions so a deployment
prints its own path rather than a guess. It defaults to
`/etc/nginx/sites-available/<DEPLOY_SERVICE>`, which is right for this
install — **set it only if the site file is somewhere else**:

```bash
# in /etc/ebwa/env, only if the default is wrong:
DEPLOY_NGINX_SITE='/etc/nginx/sites-available/ebwa'
```

Check what the box prints after the restart, on Settings → Domains, and
correct the variable if the path shown is not the real one. **A wrong
path here is worse than no path**, because it is printed with the same
confidence as a right one.

**Worth knowing before anybody follows it:** the box tells you to keep
website DNS and mail DNS changes on separate days. That is not
box-ticking — if both move together and mail stops, there are two
candidate causes and no way to tell them apart, and mail failures are
noticed late.

- [x] Local
- [ ] Demo VPS — **check the printed nginx path is the real one** once
      deployed; it cannot be verified from here.
- [ ] Production

---

## c15699a — 2026-08-28 — Tell iPhone users what HEIC is

**NO SCHEMA CHANGE, NO NEW PACKAGE, NOTHING TO RUN.** `git pull` and
restart:

```bash
cd /opt/ebwa
git pull
flask --app app check-schema     # unchanged, still clean
sudo systemctl restart ebwa
```

**What changed.** An iPhone takes photographs in HEIC unless it has been
told otherwise, so this is going to be the commonest upload failure EBWA
meets. What it used to say was *"Image must be one of: gif, jpeg, jpg,
png, webp"* — true, and to a volunteer it says only that they have done
something wrong. It now says what the format is, that their phone chose
it, and how to get a JPEG: share the photo to yourself on WhatsApp or by
email and upload the copy that arrives, or change Settings → Camera →
Formats → Most Compatible on the phone.

The same message appears wherever the upload was made — the flash on the
plain form, and the line under the progress bar in the gallery. A HEIC
renamed to `.jpg` gets it too, since renaming is what somebody tries
next; that version answers the renaming first.

The admin guide has a section on it at **Help → Photographs → “If a photo
from an iPhone is refused”**, which the upload forms link to. **Worth
mentioning to EBWA directly when handing the site over** — it is the one
question they are most likely to ring about.

**NO HEIC LIBRARY WAS ADDED, and that is a decision rather than an
omission.** Reading HEIC needs `pillow-heif`, which needs libheif — a
system package, and a new image codec running on a server that takes card
payments. Converting on the phone is one tap for a volunteer and nothing
at all for the server. If that trade is ever revisited, it is a
`requirements.txt` change AND an `apt install`, and it belongs in its own
entry here.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## adbf656 — 2026-08-28 — An oversized upload explains itself

**NO SCHEMA CHANGE. ONE NGINX SETTING TO CHECK — see below, and check it
even though nothing in this repository can.**

```bash
cd /opt/ebwa
git pull
flask --app app check-schema     # unchanged, still clean
sudo systemctl restart ebwa
```

**What changed.** A file past `MAX_CONTENT_LENGTH` used to produce a bare
`413 Request Entity Too Large` page — no heading, no navigation, nothing
to do next, and the Back button losing whatever else had been typed into
the form. It now flashes *"That file is too large — the limit is 8MB per
photo. If you chose several at once, that limit is for the whole upload,
so add them in smaller batches."* and puts the person back on the form
they were using. All eight forms that take an image, not just the
gallery. In the gallery's progress script an oversized photograph is
reported by name mid-run like any other failure, the rest of the batch
still goes up, and it is counted in the summary.

The limit in that sentence is READ FROM `MAX_CONTENT_LENGTH`. Raising the
cap is one edit and every message follows it.

### nginx: `client_max_body_size` MUST BE ABOVE Flask's limit

**This is the part that cannot be checked from the repository, and the
part that silently undoes the whole change.** nginx enforces its own body
limit BEFORE the request ever reaches gunicorn. If it is the smaller of
the two, nginx answers with its own 413 page — plain white, its version
number on it, no site around it — and the handler added here never runs.
The site would look exactly as it did before this commit, and the code
would be word for word correct.

**nginx's default is `1m`, which is EIGHT TIMES SMALLER than Flask's
8MB.** If the directive is not set at all in the EBWA server block, every
photograph over 1MB is already being refused that way — which is most
photographs off a phone.

Set it in the `server` block for the site, comfortably above the 8MB
Flask enforces, so Flask is always the one that answers:

```nginx
server {
    # ...
    client_max_body_size 12m;   # ABOVE app.py's MAX_CONTENT_LENGTH (8MB)
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

**12m, not 8m.** A request carrying an 8MB file is a little larger than
8MB once the multipart boundaries and field names are counted, so an
exactly-equal limit would have nginx refuse a file Flask would have
accepted — and refuse it with the page this change exists to avoid. The
gap is deliberate headroom, not slack.

**Keep the two in step.** If `MAX_UPLOAD_MB` in `app.py` is ever raised,
raise `client_max_body_size` first and by more. The order matters: raise
Flask's first and the extra allowance does nothing, because nginx is
still refusing at the old number.

`proxy_request_buffering` is on by default and should stay on. It is why
Flask receives the complete request and can answer it properly instead of
the browser seeing a reset connection part way through a 20MB upload.

**How to tell it is right:** sign in, choose a photograph of about 10MB,
press Upload. You should get the sentence above on the gallery page. If
you get a white page saying `413 Request Entity Too Large` with `nginx`
underneath it, the directive is missing or too small.

- [x] Local (no nginx; verified against the app's own limit)
- [ ] Demo VPS — **`client_max_body_size` NOT CHECKED.** Cannot be read
      from here. Check it before assuming this works there.
- [ ] Production

---

## 408a9b1 — 2026-08-28 — Busy states and gallery upload progress

**NO SCHEMA CHANGE, NO NEW BLOCKS, NO NEW PACKAGE.** `git pull` and
restart is the whole of it:

```bash
cd /opt/ebwa
git pull
flask --app app check-schema     # unchanged, still clean
sudo systemctl restart ebwa
```

**One new static file**, `static/js/busy.js`, served from `/static/`
like everything else there. It is linked from both shells with
`asset_version()`, so nginx's `expires 30d` is safe for it exactly as it
is for the stylesheet — no nginx change, and nothing to purge.

**What changed.** Anything that takes a moment now says so. A form or
link opts in with `data-busy="Uploading photos…"`; the button's label
becomes that message, gains a spinner and `aria-busy`, and a second
press does nothing. With JavaScript off, `data-busy` is an attribute
nobody reads and every form behaves exactly as it did.

**The gallery upload now posts one photograph per request** when the
browser can, showing a determinate bar and the filename, then lands back
on the album with one sentence: *"11 photos added, 1 failed: beach.heic
— Image must be one of: gif, jpeg, jpg, png, webp."* The plain
multipart form is untouched and is still what a browser with no
JavaScript uses.

**A SIDE EFFECT WORTH KNOWING ABOUT ON A REAL DEPLOY.**
`MAX_CONTENT_LENGTH` is 8MB **per request**, so a dozen photographs
straight off a phone were refused as one batch — a bare 413 page, no
flash, nothing stored. One file per request goes under the cap, so that
upload now works. The plain form still hits it, unchanged: this makes
the common case work rather than raising the cap.

**Nothing to run afterwards, and nothing to undo.** If the script is
ever a problem on a client's browser, deleting the two `<script>` lines
from `templates/base.html` and `templates/admin/base_admin.html` puts
every form back exactly as it was — that is what "progressive
enhancement" is being claimed here, and it is worth knowing the exit
exists.

**Checks for it:** `python tests/smoke_test_gallery_upload.py` (both
paths through the route) and `python tests/check_upload_progress.py`
(Chromium at 1440x900 and 390x740: twelve photographs one at a time,
sequential, one invalid file named and survived, a double click that
uploads once, and the whole thing with JavaScript switched off). The
browser one writes into the REAL `static/uploads/` — a sandbox would
404 every thumbnail — and sweeps up after itself.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## b30bee9 — 2026-08-26 — Backup schedule in British time

**ONE NEW BLOCK AND A ONE-OFF COMMAND. No schema change.**

```bash
flask --app app init-db
flask --app app migrate-backup-schedule     # see below — run it once
flask --app app check-schema                # unchanged, still clean
sudo systemctl restart ebwa
```

**What changed.** The NAS transfer time was entered and read as UTC while
every other admin-facing time on this site is Europe/London. On a British
site that means a 19:36 schedule fires at 20:36 for seven months of the
year, which reads as the schedule not working. It is now a British time,
like the rest.

**`migrate-backup-schedule` rewrites the stored value** from the UTC time
it used to mean to the British time that is the same moment — so the
backup goes on happening exactly when it happens today, and only the
label changes. Run on BST the displayed time moves forward an hour; run
on GMT nothing moves. It is idempotent: a second run says so and changes
nothing.

**IT IS SAFE TO FORGET, but do not.** An unmigrated value is converted on
the way out too (`schedule_in_uk`), so a deployment that skips the
command still backs up at the right moment and still shows the admin the
right time. What it does NOT do is settle the stored digits, so the
displayed time will shift at the next clock change until somebody either
runs the command or saves the Settings form. Run it.

**The two awkward mornings**, in case anybody is asked: on the day the
clocks go forward an hour does not exist, and a schedule set inside it
runs once, at the moment the clocks change. On the day they go back an
hour happens twice, and it runs once, at the first.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## 1993a37 — 2026-08-26 — Retention setting and the period report

**THREE NEW BLOCKS, NO SCHEMA CHANGE.** `init-db` is idempotent and
inserts only missing keys:

```bash
flask --app app init-db
flask --app app check-schema     # unchanged, still clean
sudo systemctl restart ebwa
```

The Blocks are `stats_raw_days` (hidden from the content editor, like the
other settings), plus `org_name` and `org_charity_number` in a new `org`
group — those two ARE editable content, because they appear on the
report a funder reads.

**SET THE CHARITY NUMBER before anybody produces a report.** It seeds
empty, and while it is empty the report says so on screen — a note that
is deliberately not printed, so a document cannot go out carrying a
message to its own admin. Page content → Organisation.

**How long per-page detail is kept is now a setting**, 30 to 365 days,
seeded at the 62 it was fixed at, so nothing changes on upgrade. It
governs the roll-up only: `aggregate_page_views()` folds anything older
into daily totals and deletes the raw rows. At 200 page views a day that
is about 2.6MB of database at 30 days and 32MB at 365.

**Shortening it takes effect on the next roll-up, not immediately**, and
what it prunes has already been counted into the daily totals — the
figures do not change, only how far back "most visited pages" can look.

**The daily totals themselves have no setting and must not get one.**
They are what the year-on-year comparison is made of, they cost about
70KB a year, and a control that can delete them is a control somebody
will use by accident.

**New page: /admin/visitors/report, open to EVERY admin**, like the
visitor summary — a grant application is EBWA's work. It is a
print-friendly HTML document rather than a generated PDF: no new Python
dependency to install, and the browser's own "Save as PDF" gives
selectable text and the site's real fonts.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-26 — Monthly report and the Visitors page

**THREE NEW BLOCKS, NO SCHEMA CHANGE.** `init-db` is idempotent and
inserts only missing keys:

```bash
flask --app app init-db
flask --app app check-schema     # unchanged, still clean
sudo systemctl restart ebwa
```

**ADD THE CRON LINE**, beside the backup and the page-view roll-up:

```
20 7 * * *  cd /opt/ebwa && ./venv/bin/flask --app app send-monthly-report
```

**Daily, not monthly, and that is deliberate.** A machine that happened
to be off on the first of the month would otherwise skip that month
entirely and nobody would notice until somebody asked for the figures.
The command does nothing on the other thirty days: it checks the audit
log for a report already sent for that month and stops.

**Nothing is emailed until a super admin switches it on.** The three
Blocks seed to off, no recipient and a 2,000 target, so installing this
cannot start posting figures at a board that has not asked for them.
Settings → Visitors carries the switch, the recipient and the target,
plus a "Send one now" button for checking the address.

The recipient falls back to the enquiries address. Set its own as soon
as there is one: a monthly figure is for EBWA's trustees and an enquiry
is for whoever answers the post, and they only look like the same
address until one of them moves.

**New page: /admin/visitors, open to EVERY admin.** EBWA could not see
their own figures before — the panel was on Settings, which is
super-admin only. Nothing on Settings became visible to them; the
summary is a shared partial rendered on both pages.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-26 — Self-hosted fonts

**NO SCHEMA CHANGE.** New static files and a changed stylesheet:

```bash
cd /opt/ebwa && git pull
sudo systemctl restart ebwa
```

`static/fonts/` gains nine `.woff2` files, about 400KB in total. They are
committed, so a `git pull` brings them — but **check they arrived**,
because a missing font file is not an error, it is a page that silently
falls back to a system serif:

```bash
ls /opt/ebwa/static/fonts/*.woff2 | wc -l    # expect 9
```

The `fonts.googleapis.com` links are gone from all four templates that
own a `<head>`, and both font domains are out of the CSP. Nothing else
changes: no new dependency, no environment variable, no cookie.

Worth knowing for the nginx side: the filenames carry a content hash, so
`expires 30d` on /static stays correct — replacing a font changes its
name.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-26 — Visitor statistics

**THREE NEW TABLES AND THREE NEW INDEXES.** No columns move on any
existing table, so `init-db` is the whole schema step:

```bash
flask --app app init-db          # page_view, page_view_daily, visitor_salt
flask --app app check-schema     # must be clean, 6 indexes
sudo systemctl restart ebwa
```

`create_all()` makes the indexes along with the tables, so a fresh
`init-db` needs nothing else. If a database somehow has `page_view`
WITHOUT them — nothing here would do that, but `create_all()` never adds
an index to a table that already exists, so it is worth having the
statements written down:

```sql
CREATE INDEX IF NOT EXISTS ix_pageview_day ON page_view (day);
CREATE INDEX IF NOT EXISTS ix_pageview_day_visitor ON page_view (day, visitor);
CREATE INDEX IF NOT EXISTS ix_pageview_day_path ON page_view (day, path);
```

`check-schema` reports a missing index by name, so it will tell you.

**ADD THE CRON LINE.** Without it nothing breaks and no figure is wrong
— the raw table simply keeps growing, one row per page load, for ever:

```
5 3 * * *  cd /opt/ebwa && ./venv/bin/flask --app app aggregate-pageviews
```

It folds days older than 62 into `page_view_daily` and deletes the raw
rows. Safe to run twice; a day already rolled has nothing left to roll.

**No new environment variables, no `pip install`, no new cookie and no
third-party service.** The privacy and cookie notices need no change —
see the commit message for why that was checked rather than assumed.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-26 — Collections: where the picture sits

**ONE NEW COLUMN**, defaulting to the top, so nothing moves on any
existing collection page.

```bash
sqlite3 instance/ebwa.db <<'SQL'
ALTER TABLE campaign ADD COLUMN image_position VARCHAR(20) NOT NULL DEFAULT 'lead';
SQL
flask --app app check-schema     # must be clean
sudo systemctl restart ebwa
```

Same three places as the video (`IMAGE_POSITIONS`, the same keys), and
it applies only when `show_image_on_page` is ticked — the position is
still stored while the box is unticked, so ticking it back on puts the
picture where it was rather than at the top.

**When both are in the same place the VIDEO goes first** (`MEDIA_ORDER`),
which is what the top of this page has always done.

No `init-db`, no Block, no data migration — the default is the existing
behaviour.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-26 — Collections: keep the cover off the page

**ONE NEW COLUMN**, defaulting to on, so nothing changes for any
existing campaign.

```bash
sqlite3 instance/ebwa.db <<'SQL'
ALTER TABLE campaign ADD COLUMN show_image_on_page BOOLEAN NOT NULL DEFAULT '1';
SQL
flask --app app check-schema     # must be clean
sudo systemctl restart ebwa
```

The campaign image does two jobs: the cover on the collections listing
and the homepage strip, and a picture on the collection's own page. This
column governs only the second. **The cover is not optional and this
switch cannot touch it** — a collection with no card image would be a
hole in the listing.

No `init-db` and no Block. Shipped in the same commit as the fix for the
video position having no effect on this page, which needed no schema
change of its own — the column was already there from
2026-08-25, the template simply never read it.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-25 — Where the video sits

**FOUR NEW COLUMNS AND ONE NEW BLOCK.** The columns all default to
`'lead'`, which is what every video has done until now, so nothing moves
on any existing page.

```bash
sqlite3 instance/ebwa.db <<'SQL'
ALTER TABLE campaign  ADD COLUMN video_position VARCHAR(20) NOT NULL DEFAULT 'lead';
ALTER TABLE event     ADD COLUMN video_position VARCHAR(20) NOT NULL DEFAULT 'lead';
ALTER TABLE milestone ADD COLUMN video_position VARCHAR(20) NOT NULL DEFAULT 'lead';
ALTER TABLE news_post ADD COLUMN video_position VARCHAR(20) NOT NULL DEFAULT 'lead';
SQL
flask --app app init-db          # the About page's Block
flask --app app check-schema     # must be clean
sudo systemctl restart ebwa
```

No data migration this time: the default IS the migration. Every video
that exists is in the lead slot, `'lead'` means the lead slot, and the
column default says so for rows already on disk.

The Block is `about_video_position`, seeded `lead` and listed in
`HIDDEN_BLOCK_KEYS` — About's video settings are edited on the About tab,
not typed into the content editor, exactly like `about_video_url`.

A value the code does not recognise — a hand-edited row, or a form from
an older deploy — renders at the top rather than blank
(`clean_video_position`), so a half-applied deploy cannot make a video
disappear from a page.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-25 — Homepage section order and switches

**Two new Blocks, no schema change.** `init-db` is idempotent and
inserts only missing keys, so this is one command and it is safe to
re-run:

```bash
flask --app app init-db
flask --app app check-schema     # still clean; no table or column moved
sudo systemctl restart ebwa
```

The two keys are `home_section_order` (seeded with the order the site
already had) and `home_sections_hidden` (seeded empty). Both are in
`HIDDEN_BLOCK_KEYS`, so neither appears in the ordinary content editor —
arranging the front page is a design decision made on Settings.

**Nothing changes appearance until a super admin moves something.** The
seeded order is the one that was hardcoded in the template, and nothing
is hidden.

**If the deploy skips `init-db`, the site is still correct.** Both
helpers treat a missing Block exactly as an empty one: the order falls
back to `HOME_SECTIONS` and nothing is hidden. The only cost is that the
Settings panel saves into Blocks it creates itself the first time
somebody presses Save. This is deliberate — a front page that depends
on a seed step having run is a front page that can lose its content to a
forgotten command.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-24 — Collections: three states instead of Active

**ONE NEW COLUMN, AND A DATA MIGRATION THAT MUST RUN WITH IT.** The
`UPDATE` is not optional and not deferrable: without it every existing
collection is hidden, including the ones currently taking money.

```bash
sqlite3 instance/ebwa.db <<'SQL'
ALTER TABLE campaign ADD COLUMN state VARCHAR(20) NOT NULL DEFAULT 'hidden';
UPDATE campaign SET state = CASE WHEN active THEN 'open' ELSE 'hidden' END;
SQL
sudo systemctl restart ebwa
```

Then, before the restart:

```bash
flask --app app check-schema     # must be clean
```

**Why the column default is `hidden` when the app's own default is
`open`.** The two are deliberately different, the same split
`Partner.display_mode` uses. The ALTER is about the rows ALREADY THERE
and the `UPDATE` on the next line is what gives them their real value;
`hidden` is simply the safest thing for them to be for the moment in
between. If the ALTER lands and the UPDATE does not — a killed shell, a
typo, a full disk — the site shows no collections, which is a visible
fault someone fixes in a minute. The other way round, `DEFAULT 'open'`
would put a collection the admin had deliberately hidden back on the
public website, and nobody would necessarily notice. Fail towards the
quiet failure, not the loud publication. A NEW collection made in the
admin still starts at "Taking payments", which is what somebody creating
one means.

The migration maps `active = 1` to **open** and `active = 0` to
**hidden** — not to `closed`. An admin who unticked Active meant to take
the thing off the website; assuming they meant "show this as finished"
would publish pages they had chosen to remove.

`active` is left in place and is now legacy. Nothing reads it; the admin
form keeps it in step with `state` so that a database opened by hand
does not contradict the website. Do not reintroduce it into a query.

No new tables, no `init-db` needed, no new environment variables and no
`pip install`.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-23 — Health panel: swap + failed sign-ins

**No new columns, but TWO NEW INDEXES on `audit_log`**, and this is the
one kind of schema change `check-schema` will NOT catch: it compares
columns, not indexes, and `create_all()` does not add an index to a
table that already exists. So on any database that already has an
`audit_log` table, these must be run by hand:

```bash
sqlite3 instance/ebwa.db <<'SQL'
CREATE INDEX IF NOT EXISTS ix_auditlog_action_created
    ON audit_log (action, created_at);
CREATE INDEX IF NOT EXISTS ix_auditlog_created
    ON audit_log (created_at);
SQL
sudo systemctl restart ebwa
```

`IF NOT EXISTS`, so re-running is harmless and a fresh database created
by `init-db` (which does make them) is unaffected.

**`check-schema` now reports a missing index**, so this is no longer a
step that can be skipped silently — it names both, prints the statements
above and exits 1, exactly as it does for a missing column.

Why: the audit log only ever grows — nothing prunes an append-only log —
and three things now read it on a schedule: the dashboard's failed
sign-in count, the health panel's counts for today/7/30 days, and the
log's own listing. Without the indexes each is a full table scan that
gets slower every day the site is used. The dashboard's existing check
needs no code change: it filters on the same (action, created_at) pair
and simply starts using the index.

Nothing else to do — the panel picks the new cards up on restart.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-23 — Video embedding (YouTube / Vimeo)

**EIGHT NEW COLUMNS and two new Blocks.** Run the statements first, then
`init-db`, then `check-schema`, then restart:

```bash
sqlite3 instance/ebwa.db <<'SQL'
ALTER TABLE campaign  ADD COLUMN video_url   VARCHAR(300) DEFAULT '';
ALTER TABLE campaign  ADD COLUMN video_thumb VARCHAR(255) DEFAULT '';
ALTER TABLE event     ADD COLUMN video_url   VARCHAR(300) DEFAULT '';
ALTER TABLE event     ADD COLUMN video_thumb VARCHAR(255) DEFAULT '';
ALTER TABLE milestone ADD COLUMN video_url   VARCHAR(300) DEFAULT '';
ALTER TABLE milestone ADD COLUMN video_thumb VARCHAR(255) DEFAULT '';
ALTER TABLE news_post ADD COLUMN video_url   VARCHAR(300) DEFAULT '';
ALTER TABLE news_post ADD COLUMN video_thumb VARCHAR(255) DEFAULT '';
SQL
flask --app app init-db        # seeds about_video_url / about_video_thumb
flask --app app check-schema   # must print "Schema is up to date"
sudo systemctl restart ebwa
```

New feature flag `video`, on by default, seeded by the same `init-db`.

**The server now makes an outbound HTTPS request when a video is saved**
— once, to fetch the poster image from `i.ytimg.com` or Vimeo's oEmbed
endpoint — and stores the result in `static/uploads/` like any other
image. If the VPS blocks outbound HTTPS, saving still works: the video
is stored and the page falls back to the content's own photo. Nothing is
fetched when a page is VIEWED.

**No `img-src` change was needed** and none was made: posters are served
from this site. `frame-src` gains `https://www.youtube-nocookie.com` and
`https://player.vimeo.com`, and nothing else.

Nothing appears on the public site until somebody adds a video link, so
this deploy changes no existing page.

- [x] Local
- [x] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-23 — Testimonials: editing, scroller, settings

**No schema change**, but FOUR NEW BLOCKS, so `init-db` must run:

```bash
flask --app app init-db      # seeds the testimonial row's four settings
sudo systemctl restart ebwa
```

| Block | Default | Meaning |
| --- | --- | --- |
| `testimonials_motion` | `none` | `scroll`, `step` or `none` |
| `testimonials_step_seconds` | `4` | seconds between steps, 1–60 |
| `testimonials_step_glide_ms` | `360` | how long one step takes, 300–3000ms |
| `testimonials_drift_speed` | `45` | drift, 10–200 pixels a second |

All four are at **Admin → Testimonials → How the row moves**, and are
SEPARATE from the partner row's — see the commit message for why.

**What changes on the site without anybody touching a setting:** with
four or more published testimonials the homepage section becomes a
horizontal scroller instead of a wrapping grid. It ships STILL, with
arrow buttons, so nothing animates on its own. Three or fewer are
unchanged. The homepage still shows at most six quotes, as it always
has.

Admin URLs moved: adding a testimonial is now `/admin/testimonials/new`
rather than a form on the list page, and `/admin/testimonials/<id>/edit`
is new. Nothing links to the old form.

Until `init-db` runs, `row_motion()` falls back to the same constants in
code, so the row renders correctly either way.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-23 — Partner row speed settings

**No schema change**, but TWO NEW BLOCKS again, so `init-db` must run:

```bash
flask --app app init-db      # seeds the two speed settings below
sudo systemctl restart ebwa
```

| Block | Default | Meaning |
| --- | --- | --- |
| `partners_step_glide_ms` | `360` | how long ONE step takes, 300–3000ms |
| `partners_drift_speed` | `45` | continuous drift, 10–200 pixels a second |

Both are at **Admin → Partners → Speed settings** (a collapsed advanced
section), both hidden from the ordinary content editor, and both can be
put back with the **Reset speeds to defaults** button beside them, which
restores these constants rather than the last saved values.

**Nothing changes on the site until somebody changes a setting**, with
one caveat worth knowing before you look at it: the step used to hand
the browser `behavior: 'smooth'` and accept whatever that engine chose,
which cannot be a setting because it cannot be read or set. It is our
own glide now. Measured with the same detector on the same page, the
browser's own took 253ms of visible movement and ours at the 360ms
default takes 303ms — about 50ms longer on a movement that happens every
few seconds. Set the glide to 300ms if you want it as close to the old
behaviour as it can be measured.

Until `init-db` runs, the homepage falls back to the same two constants
in code, so the row looks exactly as it does now either way.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## (pending) — 2026-08-22 — Partner row movement setting

**No schema change**, but TWO NEW BLOCKS, so `init-db` must run or the
partners admin page saves into rows that do not exist yet (it recreates
them itself, but the homepage falls back to continuous scrolling until
somebody saves):

```bash
flask --app app init-db      # seeds partners_motion + partners_step_seconds
sudo systemctl restart ebwa
```

New settings, both editable at **Admin → Partners → How the row moves**
and both hidden from the ordinary content editor:

| Block | Default | Meaning |
| --- | --- | --- |
| `partners_motion` | `scroll` | `scroll`, `step` or `none` |
| `partners_step_seconds` | `4` | seconds between steps, 1–60 |

Nothing to run afterwards. Anyone whose device asks for reduced motion
sees a still row whichever mode is set.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## ea3ecdc — 2026-08-22 — Server health panel

**No schema change**, no new settings. One new package:

```bash
pip install -r requirements.txt      # adds psutil
sudo systemctl restart ebwa
```

The panel works without psutil — it falls back to `/proc` and
`os.statvfs` — but with it installed the memory, load and network figures
are read the same way on every platform, so install it.

It reads the machine and nothing else: no restart button, no logs, no
commands. The one thing it runs is `systemctl is-active` on the `ebwa`
and `nginx` units, with the names fixed at startup rather than taken from
a request. Nothing here needs sudo, and the service user needs no new
permissions.

Worth a look after deploying: Settings → Server health should show real
figures for memory and load (rather than "not available here"), both
services active, and the schema up to date.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## a4b8783 — 2026-08-22 — Collections listing page

**Nothing to run.** A new public page at `/collections`, plus template
and stylesheet changes — no tables, no columns, no new seeded rows.
Confirmed with `flask --app app check-schema`.

The page lists open collections and is linked from the menu under Get
involved and from the footer, so campaign pages are no longer reachable
only from the homepage strip. It follows the `donations` flag: with that
off the page 404s and both links disappear, as before.

- [x] Local
- [x] Demo VPS
- [ ] Production

---

## 59a07cc — 2026-08-22 — The SMTP password can be set on the Settings page

**No schema change.** One new content Block, seeded empty:

```bash
flask --app app init-db     # seeds smtp_password_enc
```

Nothing changes for an existing install: with no password saved on the
page, `SMTP_PASSWORD` from the environment is still used, and the
Settings page says which of the two is in force.

**It needs `FERNET_KEY`** — the same key the NAS password already uses.
If it is not set, the page refuses to store a password and says so; the
environment variable keeps working meanwhile. Deployments that did the
NAS entry already have this.

The seven-step "change it on the server" box is now a short note: use
Settings → Email, and the server route stays documented for when the
admin is unreachable.

- [x] Local
- [ ] Demo VPS
- [ ] Production

---

## a9d7c7d — 2026-08-21 — Send backups to the NAS over SFTP

Five new columns on `backup_run`, eight new Blocks, two new packages and
one new environment variable.

```bash
pip install -r requirements.txt          # paramiko, cryptography
```

```sql
ALTER TABLE backup_run ADD COLUMN transfer_status VARCHAR(20) DEFAULT 'none';
ALTER TABLE backup_run ADD COLUMN remote_filename VARCHAR(255) DEFAULT '';
ALTER TABLE backup_run ADD COLUMN transfer_error TEXT DEFAULT '';
ALTER TABLE backup_run ADD COLUMN transfer_attempts INTEGER DEFAULT 0;
ALTER TABLE backup_run ADD COLUMN transferred_at DATETIME;
```

```bash
flask --app app init-db                  # seeds the sftp_* Blocks
```

**Prerequisite: Tailscale.** The VPS reaches the NAS over the tailnet, so
Tailscale must be installed and logged in on both machines, and the VPS
must be able to reach the NAS by its tailnet name before any of this can
work. Prove it from the server first:

```bash
tailscale status
sftp ebwa-backup@nas.tailnet-name.ts.net
```

**New environment variable.** The NAS password is stored encrypted, so
`/etc/ebwa/env` needs a key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# then, in /etc/ebwa/env:
FERNET_KEY='the-generated-key'
```

Keep it in the environment file and nowhere else. It is what stops the
NAS password being readable inside the backup archives — which contain
the database, and end up on the NAS.

**The cron line.** The app has no scheduler and must not have one under
gunicorn; this checks every fifteen minutes whether the configured time
has passed without a good run:

```cron
*/15 * * * * cd /opt/ebwa && set -a && . /etc/ebwa/env && set +a && ./venv/bin/flask --app app run-scheduled-backup >> /var/log/ebwa-backup.log 2>&1
```

**It replaces the plain `backup-now` line from the earlier entry
outright — not only when transfers are switched on.** Remove that line
when you add this one.

`run-scheduled-backup` writes the archive whether or not SFTP is
configured; it checks `sftp_ready()` afterwards, purely to decide whether
to send it, and prints "Transfers to the NAS are off, so the archive
stays here" when it is not. So the two lines together are not a belt-and-
braces arrangement, they are **two archives a day**: `backup-now` records
`reason="cli"`, which never satisfies this command's "has today's run
happened?" check (that asks for `reason="scheduled"`), so it takes its
own regardless. Both then compete for the same `BACKUP_KEEP`, halving how
far back the local history reaches, and the `backup-now` one never leaves
the machine.

(This clause used to read "if transfers are switched on", which would
reasonably lead somebody deploying with transfers off to keep both. The
steps recorded below are unchanged; only this guidance is corrected.)

Then, in Settings → Send backups to the NAS: fill in the details, save,
**Test connection**, and finally **Backup now** to prove a whole archive
lands on the NAS.

- [x] Local
- [ ] Demo VPS
- [ ] Production

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
- [x] Demo VPS
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
- [x] Demo VPS
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
- [x] Demo VPS
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
- [x] Demo VPS
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
- [x] Demo VPS
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
- [x] Demo VPS
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
- [x] Demo VPS
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
- [x] Demo VPS
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
- [x] Demo VPS
- [ ] Production

---

## eae8402 — 2026-07-18 — Add Milestones & Track Record module (Variation 2)

New table `milestone`, plus the `journey_intro` block.

```bash
flask --app app init-db
```

- [x] Local
- [x] Demo VPS
- [ ] Production

---

## 32d3da3 — 2026-07-18 — Add Funding Track Record module (Variation 1)

**Superseded by e209fa3 — do not apply to a database that never saw it.**

It created a `funding_record` table and a `track_record_intro` block. If
you are bringing a database forward from before 18 Jul 2026, skip
straight past this entry to eae8402.

- [x] Local (applied at the time; now an orphan, see e209fa3)
- [x] Demo VPS — skip
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
- [x] Demo VPS
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
- [x] Demo VPS
- [ ] Production
