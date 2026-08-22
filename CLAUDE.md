# EBWA Community Website + CMS

Flask + SQLite website for the Enfield Bangladesh Welfare Association (EBWA),
a community charity in Ponders End, Enfield. Public site + admin CMS.
Built and maintained by Netbus IT Support.

## Stack

- Python 3 / Flask, Flask-SQLAlchemy, Flask-Login
- SQLite (single file: `instance/ebwa.db`) — do NOT introduce PostgreSQL;
  the app is deliberately SQLite-only. `DATABASE_URL` env var exists as a
  future escape hatch, never as a default.
- Plain Jinja templates + one shared stylesheet (`static/css/style.css`).
  No build step, no npm, no React. Keep it that way.
- Gunicorn behind nginx in production (systemd unit `ebwa`, port 8011).

## Project layout

- `app.py` — the entire application: models, helpers, public routes,
  admin routes, CLI commands. Single-file by design; keep new code in the
  matching commented section (`# ---- models`, `# ---- public`,
  `# ---- admin: <area>`, `# ---- CLI`). Do not split into blueprints
  unless explicitly asked.
- `templates/` — public pages extend `base.html`; admin pages extend
  `admin/base_admin.html`.
- `static/uploads/` — user-uploaded images (UUID filenames). PUBLIC.
  Anything private (e.g. future board minutes) must go in a folder outside
  `static/` and be served through a `@login_required` route with
  `send_from_directory` — never via a static URL.
- `instance/` — the SQLite database. Never committed.

## Code conventions (follow existing patterns exactly)

- JavaScript: `var`, not `const`/`let`. Vanilla JS only, no libraries.
  Small scripts inline in the template's `{% block scripts %}`.
- No Jinja expressions inside inline JS logic — pass values via data
  attributes (see the stat counter in `index.html`).
- Timestamps: naive UTC via `datetime.utcnow` defaults. Never store local
  time; format for display in templates.
- Admin-facing date filters and exports use UK local dates
  (Europe/London); storage remains naive UTC.
- Dates that are calendar dates (events) use `db.Date`, not DateTime.
- New models: integer PK `id`, follow the style of `Event` / `Testimonial`.
  Slugged public content copies the `Event` pattern exactly: `slugify()` +
  `unique_slug()`, `published` boolean, detail route with
  `first_or_404()` filtered on `published=True`.
- unique_slug(model, title, obj_id=None) in app.py is the shared slug
  helper; do not create per-model copies.
- IMPORTANT (learned the hard way): when creating a new row whose fields
  are set from form data, populate the object FIRST and only
  `db.session.add()` it just before `commit()`. Adding an empty object
  early triggers autoflush inserts of half-populated rows during
  intermediate queries (e.g. slug-uniqueness checks).
- Rich content (multiple images + layout presets) is deliberately
  generic: ONE `ContentImage` table keyed by `owner_type`/`owner_id`, one
  admin partial (`admin/_content_images.html`) and one rendering macro
  (`_rich_content.html`). Do not add a photo table or a bespoke gallery
  template to a module — wire it into this instead.
  - To give a content type rich content: add it to `CONTENT_OWNERS`, give
    it a `layout` column (About has no row, so it uses the `about_layout`
    Block and owner_id 0), include the admin partial on its form page,
    and call `rich_content(paragraphs, images, layout)` in its template.
    `owner_admin_url()` needs a case so the admin redirects back sensibly.
  - Go through `images_for()` / `attach_image()` / `delete_content_image()`
    / `delete_images_for()` — never `ContentImage.query.delete()` or a
    bare `delete_upload()`. The delete helper checks whether another row
    or a Block still points at the file before removing it, which is what
    stops the legacy single image being deleted out from under the
    flag-off view.
  - **Every owner's delete route must call `delete_images_for()`** or
    attachments and files are orphaned.
  - Alt text is REQUIRED on upload and cannot be emptied by an edit. An
    image nobody can describe is one a screen reader user simply loses.
    Images are lazy-loaded and every figure carries an aspect-ratio, so
    the page does not jump as they arrive.
  - The three presets (`CONTENT_LAYOUTS`) must stay visually distinct —
    that is the point of offering them. Reading width is capped at 68ch
    in all three. Any transition is already covered by the global
    prefers-reduced-motion rule at the top of the stylesheet.
    - On a page carrying MANY owners the presets are scoped down to
      entry scale rather than page scale (Our Journey). Rendered full
      size they stop being layouts and become unrelated page designs
      stacked, and the structure of the page — there, the year headings
      — disappears into a wall of photographs. Scope with CSS on the
      wrapper; never fork the macro.
  - The "no photo yet" box in the classic preset is OPT-IN, via the
    macro's `placeholder` argument. Only About passes one, because only
    About's copy makes sense to a visitor. Everywhere else a piece of
    content with no photo simply shows its words at full reading width
    (`.rc-classic-top.is-textonly`).
- Image uploads: always use the existing `save_upload()` /
  `delete_upload()` helpers (extension whitelist, UUID rename, 8 MB cap).
  When replacing an image, delete the old file after a successful save.
  - Every upload goes through Pillow on the way in (`process_image()`):
    the EXIF orientation flag is APPLIED and then all EXIF is DROPPED,
    anything wider than `MAX_IMAGE_WIDTH` (1600) is scaled down, and the
    result is re-encoded as a progressive JPEG at quality 82. A 600px
    `<uuid>-thumb.<ext>` is written beside it.
  - **Stripping EXIF is a privacy measure, not just a size one.** A
    photo taken on a phone carries GPS coordinates in its EXIF — often
    a volunteer's or a member's home, or a venue that should not be
    published to the metre. `static/uploads/` is PUBLIC, so anything
    left in the file is published with it. Never add an "keep original
    metadata" option, and never serve an unprocessed upload.
  - Transparency is preserved (a logo stays PNG), and an animated GIF is
    passed through untouched rather than flattened to its first frame.
    An upload that will not decode is refused with a flash — never a
    500, and nothing is written to disk.
  - An image already within the ceiling, carrying no EXIF and in a
    format we would not change is stored byte for byte. Re-encoding it
    could only cost quality.
  - Templates NEVER build an uploads URL by hand: `thumb_url(filename)`
    for cards, grids and admin previews, `upload_url(filename)` for
    detail views and the hero. `thumb_url()` falls back to the full size
    when there is no thumbnail, so an upload from before this existed
    still renders.
  - `flask --app app reprocess-images` optimises and thumbnails
    everything already on disk. It is idempotent and never renames a
    file, so nothing in the database has to change; a file is only
    rewritten when it is too wide, carries EXIF, or would be at least a
    tenth smaller — "any saving at all" would re-encode the same JPEGs
    on every run and degrade them a little each time.
- Configuration: `app.py` loads a `.env` beside it at startup, before
  anything reads `os.environ`, with `override=False` — so a variable
  already in the environment always wins. **This is a local-development
  convenience only.** Production secrets live in `/etc/ebwa/env` (mode
  640, root:www-data), read by systemd's `EnvironmentFile`; the VPS has
  no `.env` and is unaffected by this. `.env` is gitignored;
  `.env.example` is committed and must list EVERY variable the app
  reads — it is the only complete inventory of them, so add to it in the
  same commit that adds a variable.
- Email: everything outbound goes through `send_mail(to, subject, body,
  reply_to=None)`. Membership and ticketing will use it as it stands.
  - **It never raises.** Callers save the visitor's data FIRST and send
    afterwards, so an SMTP server that is down, slow or misconfigured
    cannot cost somebody their enquiry or turn a thank-you page into a
    500. Failures go to the audit log (`mail_failed`) instead, naming
    the recipient and the error — never a credential, never the body of
    what somebody wrote.
  - Every setting resolves the same way, in `mail_settings()`: the Block
    a super admin filled in WINS, the environment variable is the
    fallback. So a deployment that only ever set env vars is unchanged,
    and anything typed on Settings overrides it without a redeploy. Add
    a setting by extending `MAIL_SETTINGS` — field, Block key, env var,
    label — and it appears on the page, in the source table and in
    `test-mail` automatically.
  - The Blocks are all seeded EMPTY and all listed in
    `HIDDEN_BLOCK_KEYS`: empty means "use the environment", and none of
    them belongs in the ordinary content editor.
  - **Both credentials — SMTP and the NAS — are ENCRYPTED AT REST with
    Fernet, the key in `FERNET_KEY`.** (This supersedes an earlier note
    saying SMTP was different and env-only; the split was inconsistent
    and the usability cost was real — changing a mail password meant
    server access.) The rule is the same for any credential the app ever
    stores:
    - ciphertext in the database, key in the environment. The nightly
      backup archive CONTAINS THE DATABASE and is copied to the NAS, so
      an archive must never hold anything that opens anything: with the
      key elsewhere, a stolen archive yields nothing.
    - never rendered — the page shows only whether one is stored and
      which source is in force;
    - an empty box means "keep the current one", so saving an unrelated
      field cannot wipe a credential;
    - never in a flash, an audit summary or an error message; failure
      text goes through `_scrubbed()`;
    - refuse to store one at all when `FERNET_KEY` is absent, and say so
      plainly rather than pretending to keep it.
  - `SMTP_PASSWORD` stays as the FALLBACK when nothing is stored, so a
    deployment that only ever set environment variables is unaffected,
    and the server route still works when the admin is unreachable.
  - `describe_mail_failure()` turns an exception into a sentence an
    admin can act on — refused, credentials rejected, TLS mismatch,
    timeout, name lookup — and `_scrubbed()` removes the password from
    anything that reaches a page or the log, in case a server quotes it
    back. Failure text must always go through both.
  - Encryption is a three-way choice (`SECURITY_MODES`: starttls / ssl /
    none), not a boolean. With no Block set it is derived from the old
    `SMTP_USE_TLS` flag and the port, so existing deployments keep the
    behaviour they had.
  - The "send a test email" button is super-admin only, rate limited
    (`test_mail` scope) because a button that emails a typed address is
    a relay otherwise, and logs every attempt — success, failure and
    refusal — with the recipient.
  - `flask --app app test-mail [address]` proves the configuration
    without waiting for a visitor to discover it is wrong.
  - Notifications set Reply-To to the person who wrote, so hitting reply
    answers them directly. There is deliberately NO auto-reply to the
    enquirer yet: it needs a decision on wording and on what happens
    when it bounces. Worth adding — ask before building it.
- Server health panel (Settings, super admins only) — **READ-ONLY, and
  that is a hard boundary, not a current state of things**:
  - No restart button, no service control, no log tailing, no shell, no
    file browser, no command execution of any kind. Anything that ACTS
    stays on SSH, where it is visible and reversible. If somebody asks
    for a restart button, the answer is no: a website that can restart
    its own server is a website whose next vulnerability restarts the
    server.
  - **No value from a request may reach a shell, a path or a unit name.**
    The JSON endpoint takes no parameters at all, which is the simplest
    way to guarantee it. The two units come from `HEALTH_UNITS`, fixed at
    startup from the environment.
  - The one command it runs is `systemctl is-active --quiet <unit>` with
    a fixed argv list, `shell=False` and a three-second timeout. That is
    the whole exception, and it exists because reading systemd's state
    any other way needs a D-Bus dependency for the same answer.
  - Metrics come from psutil where it is installed, and from `/proc`,
    `os.statvfs` and `shutil.disk_usage` where it is not. **Every metric
    degrades to None with a note rather than raising** — a development
    box has no `/proc` and no systemd, and the panel is not worth a 500
    on the settings page.
  - No speed test, ever: it would mean putting traffic on a client's
    server to produce a number nobody acts on.
  - The auto-refresh is opt-in, every 30 seconds, and rate limited like
    everything else that reads the machine.
- Backups: `run_backup()` writes a database snapshot (through sqlite3's
  own backup API, so it is consistent while the site is serving) plus
  every upload into one timestamped zip in `BACKUP_DIR`, and records a
  `BackupRun` either way — a backup that failed silently is worse than
  none. `prune_backups()` keeps the newest `BACKUP_KEEP`.
  - Two runs must never overlap: they write archives and upload at the
    same time, and the second prunes the first's archive out from under
    it. `backup_in_progress()` is the shared guard — the Settings button
    and both CLI commands all check it and refuse rather than starting a
    second run. It ignores rows older than `BACKUP_STALE_MINUTES`, so a
    process killed mid-backup cannot block every future backup for ever.
    The hourly cap (`BACKUP_MANUAL_PER_HOUR`, beside the other rate-limit
    scopes) is only a brake on a leaned-on button; the guard is the part
    that matters, so keep them both and do not swap the guard for a
    tighter count.
  - Every attempt is audit-logged INCLUDING refusals. "Somebody tried to
    back up and was told no" is exactly what you want to see when asking
    why there is no archive from that afternoon.
  - **Nothing here shells out, and nothing ever will.** No subprocess, no
    command built from anything a request supplied. The web button calls
    the same Python the CLI does. A website that can run commands on its
    own server is one vulnerability away from being a shell.
  - **An archive beside the database is not a backup.** It protects
    against a mistake — a deleted album, a bad edit — not against losing
    the server, so getting a copy onto another machine is part of the
    job and not somebody else's. The app therefore owns the whole of
    it: writing the archive, transferring it to ONE configured SFTP
    destination on the private network, and recording both outcomes on
    the same `BackupRun` (see Offsite transfer below).
    (This supersedes an earlier note saying the app made archives and
    nothing else, and that copying them off was cron's job with rsync or
    scp. The NAS transfer replaced it; that note contradicted the code
    directly beneath it.)
    - The destination's credentials are Fernet-encrypted with the key in
      `FERNET_KEY`, per the shared credential rule above. This is the
      case that made the rule: the archive contains the database, so a
      plaintext password in it would be posted to the very machine it
      opens.
    - The boundary that DID survive: no shelling out for any of it (no
      rsync, no scp, no subprocess), and one destination the admin
      configures — not arbitrary remote endpoints, not a consumer cloud
      account, not an "upload to Dropbox" button. Transfer over paramiko
      to a host on the tailnet, or not at all.
  - `backups/` is gitignored: an archive holds the entire database,
    personal data and all.
- Offsite transfer: after a backup, `upload_backup()` sends the archive
  to the NAS over SFTP (paramiko), across Tailscale. Settings live in
  Blocks like the mail ones; the outcome lives on the SAME `BackupRun`
  row (`transfer_status`, `remote_filename`, `transfer_error`,
  `transfer_attempts`) rather than in a second table — one row answers
  "did we back up, and did it leave the building?", which is the only
  pair of questions anybody asks.
  - The NAS password follows the shared credential rule above:
    Fernet-encrypted with the key in `FERNET_KEY`, never rendered, empty
    box keeps the current one. It is the credential that made the rule —
    a plaintext key to the backup destination, stored in the database,
    would be copied into every archive and then onto the NAS: a key to
    the safe, inside the safe, posted to the offsite copy of the safe.
  - Two attempts, then stop until the next scheduled run. A NAS that is
    switched off must not be hammered, and the settings page promises
    exactly this behaviour — keep them in step.
  - Upload to `<name>.part` and rename into place, so an interrupted
    transfer never leaves something that looks like a complete archive.
  - Remote retention is SEPARATE from local (`sftp_keep` vs
    `BACKUP_KEEP`): the NAS has room for far more history, which is most
    of the point of it.
  - **Scheduling is cron, not a thread.** `run-scheduled-backup` asks the
    BackupRun table whether today's run has happened and does nothing if
    it has; cron calls it every fifteen minutes. Never start a background
    thread for this: gunicorn runs several workers, so a thread in each
    means several backups at once writing the same archive name.
  - `AutoAddPolicy` is used for host keys because the NAS is on a private
    tailnet whose transport is already authenticated and encrypted. That
    is a defensible choice THERE and would not be over the open internet.
- Security visibility: failed logins were always in the audit log; the
  dashboard now shows a count above `FAILED_LOGIN_NOTICE` in 24 hours,
  and `note_failed_login()` can email once when one IP passes
  `ALERT_IP_THRESHOLD` within the hour.
  - The alert is OFF by default, switched on by a super admin, and its
    cooldown is read from the AUDIT LOG rather than memory — gunicorn
    runs several workers, and an attacker must not be able to earn one
    email per worker.
  - The email carries the addresses tried and the IP. It must never carry
    a password, part of one, or anything derived from one: the site does
    not record attempted passwords anywhere, and that is the reason it
    can say so plainly.
  - Alerts have their OWN recipient (`site_security_alert_to`,
    comma-separated), falling back to the enquiries address only until
    somebody sets one. They are different audiences: an enquiry is for
    EBWA, "somebody is working through passwords on your admin" is for
    whoever runs the server. They look identical while both fall back to
    MAIL_TO and stop being identical the day enquiries move to an
    @ebwa.org.uk mailbox — which is exactly when nobody would notice the
    alerts had followed them there. Any future alerting (disk full,
    backup failed, certificate expiring) belongs on this address, not the
    enquiries one.
- Contact form: `ContactMessage` behind the `contact_form` flag. The
  /contact PAGE is core and stays whole with the flag off — address,
  phone and map are what somebody looking for us actually needs.
  - Spam defences are layered and none of them tells a bot which one it
    hit: honeypot, a minimum time-to-submit (`MIN_FORM_SECONDS`), and
    the `contact` rate-limit scope. A caught submission gets the same
    thank-you as a real one.
  - Enquiries are personal data: admin-only, no CSV export, and reading
    the list is audit-logged like an export because that is a view of
    people's names and questions. Status changes and deletions log too,
    naming the person but never quoting the message.
  - The dashboard's unread-enquiry check is the ONE attention item that
    ignores its feature flag. Switching the form off stops new messages
    arriving; it does not answer the ones already sent, and somebody
    waiting for a reply is not a module's content.
- FAQ: `Faq` follows the resources pattern; `category` is OPTIONAL and
  an empty one is not a bug — those questions run ungrouped at the top of
  the page (`""` sorts before any letter). Categories themselves are
  alphabetical; `sort` orders questions within one.
  - The accordions are plain `<details>`/`<summary>`: the browser brings
    the keyboard handling, the ARIA and the open/closed state, and the
    page still works with the script gone. Every question is rendered
    `open` and the inline script collapses all but the first, so a
    visitor without JavaScript reads the answers instead of a column of
    headings that will not open. Do not swap this for buttons and
    `aria-expanded` — it would be more code doing less.
  - The page carries FAQPage JSON-LD built in the ROUTE from the same
    rows the page renders, so the structured data cannot drift from what
    is on screen, and an unpublished question is absent from both. Google
    shows these under a search result, which is the point.
- Gallery albums: `GalleryAlbum` groups `GalleryImage` rows through a
  NULLABLE `album_id`. Rules that matter:
  - **Deleting an album must never delete photographs.** The delete
    route sets their `album_id` to NULL and they carry on under "All
    photos". An album is an arrangement and can be rebuilt in a minute;
    a photograph of somebody's grandmother cannot. The same goes for any
    future grouping — group by nullable key, never by cascade.
  - Unfiled photos are normal, not a broken state: everything from
    before albums existed is unfiled, and `/gallery/all` is what
    guarantees no photo is ever unreachable. Keep that view.
  - `/gallery/all` is a reserved address — the album form refuses the
    slug `all`, or that album would have no page.
  - A hidden album hides its photos everywhere public, including
    `/gallery/all` and the sitemap. Admins still see them.
  - Order is `sort` ascending then newest first, so an admin can pin a
    photograph to the top of an album without renumbering the rest.
- The gallery masonry is the rich-content gallery preset's approach (CSS
  columns, `break-inside: avoid`) with the photographs' REAL aspect
  ratios rather than a staggered pattern, so a portrait phone photo
  stays portrait. `aspect_ratio_of()` reads the ratio from the file
  header and caches it per worker on (mtime, size) — deliberately not a
  database column, so there is nothing to backfill and nothing that can
  drift from the file on disk.
- The lightbox is vanilla JS inline in the template's `{% block scripts %}`,
  like every other script here. It UPGRADES plain links: every photo is
  an `<a href>` to the full-size file, so with JavaScript broken clicking
  one still opens the photograph. Keep it that way — no library, no
  build step, and no `<img src="">` (an empty src re-fetches the page
  itself).
  - Closing on an outside click is decided by what the click LANDED ON —
    "not the photo and not a control" — never by `event.target === box`.
    The figure covers the middle of the overlay and the caption sits
    inside it, so testing for the backdrop element left the gap under a
    wide photo, the space beside a tall one, and the caption looking
    exactly like backdrop but dead to the click.
- Every `<img>` needs its box reserved before the bytes arrive
  (`aspect-ratio` or explicit width/height in the stylesheet), or the
  page reflows as photos load. Check this when adding an image class.
- Editable page text/images live in the `Block` model. To add one, append
  to `DEFAULT_BLOCKS` (group, key, label, kind, default) — `init-db` is
  idempotent and inserts only missing keys. Read in routes with
  `blocks_for(group)`, render with `c.get('key','')`.
  - No visible string on a public page should be hardcoded in a template
    if an admin might reasonably want to change it — headings, labels
    and all. The exceptions are the Bengali `.bn` eyebrow accents (Phase
    2 translations own those) and purely decorative emoji icons.
- Seeded RECORDS (as opposed to blocks) are seeded only into an empty
  table — see `DEFAULT_SERVICES` / `seed_services()`. Blocks are fixed
  slots, so re-inserting a missing key is right; records are things an
  admin may legitimately delete, and a later `init-db` on deploy must
  never resurrect them. Follow that split for any future seeded list.
- Admin forms: plain POST + redirect + `flash(msg, "ok"|"error")`.
  Destructive actions are POST forms with a JS `confirm()`. No AJAX.
- Auth: single `User` model via Flask-Login, `@login_required` on every
  admin route, with optional per-user TOTP 2FA (see the roadmap entry
  for the rules). Tiers live in `User.role` (see `ROLES`) — do not
  create a second user table. A future board-member tier is another value here.
  - Admin sessions expire after `IDLE_SESSION_MINUTES` (20) of
    INACTIVITY: `PERMANENT_SESSION_LIFETIME` + `SESSION_REFRESH_EACH_REQUEST`,
    with `start_admin_session()` (not a bare `login_user()`) at every
    login point so the session is permanent. Flask re-signs the cookie
    on each request, so the clock restarts on activity — do not swap
    this for an absolute timeout. The 2FA hand-off has its own, shorter
    5-minute window (`PENDING_2FA_MAX_AGE`) and must stay under the idle
    window so it is the one that decides.
  - `login_manager.unauthorized_handler` explains WHY the login page
    appeared: a session that timed out says so, a first-time visitor
    gets a plain prompt. `session_expired()` tells them apart by
    re-reading the cookie with the age limit lifted — an anonymous
    visitor can hold a session cookie just from a flash message, so
    "cookie present" alone is not enough.
  - `admin` (default) — EBWA's own admins. Everything they need to run
    the site.
  - `super_admin` — **Netbus only**, never a client login. Adds the
    Users page (accounts) and the Settings page (feature flags). Gate
    those routes with `@super_admin_required` (anonymous → login
    redirect, client admin → 403) and hide the nav link behind
    `{% if current_user.is_super_admin %}`. Promote with
    `flask --app app promote-super-admin`.
  - Account safety rails, enforced in the ROUTE and not just the UI:
    the last super admin can be neither deleted nor demoted, and nobody
    can delete or demote their own account. `is_last_super_admin()` is
    the shared check; it runs BEFORE the self check so the sole super
    admin gets the more useful message. Every action refuses with a
    flash rather than raising. Keep these rails on anything new that
    can change a role or remove an account.
  - Break-glass CLI, for when nobody can log in:
    `reset-admin-password`, `disable-2fa`, `delete-admin` (same
    last-super-admin protection), `promote-super-admin`. Prefer adding
    a CLI command over documenting raw SQL — the README should never
    tell anyone to hand-edit the database.
- Reverse proxy: gunicorn runs behind nginx, so `app.wsgi_app` is
  wrapped in `ProxyFix(x_for=1, x_proto=1, x_host=1)` — exactly one hop.
  Anything reading `request.remote_addr` (audit log, rate limiter)
  depends on it. Do not raise the hop counts unless a real extra proxy
  is added in front, do not switch to trusting a whole forwarded chain,
  and keep gunicorn bound to 127.0.0.1 so the app is unreachable except
  through nginx.
- Audit log: `AuditLog` is APPEND-ONLY. Never write a route, helper or
  CLI command that updates or deletes an entry, and never make recording
  conditional on anything — a log that can be edited or switched off is
  not a log. The `audit_log` feature flag governs only who may READ the
  page (super admins always can).
  - Record with `log_action(action, entity=None, summary="")`. It reads
    `current_user` and `request.remote_addr` itself and commits.
  - Call it AFTER `db.session.commit()`, so the row has its id. For
    deletes, capture `("Model", obj.id)` and the human name BEFORE
    deleting and pass that tuple as `entity`.
  - Anything that creates, edits, deletes or changes the status of a
    record MUST log. So must every export or printable view of personal
    data — that is where data leaves the system.
  - Summaries are British English and readable by a non-technical
    trustee ("Deleted event “Eid Iftar”."). NEVER put a password, a
    password hash, a TOTP secret or a recovery code in a summary; a
    failed login records the attempted email only.
  - Edits record WHICH fields changed, never the values: build a
    `{field: new_value}` dict, call `changed_fields(obj, values)` BEFORE
    `apply_values(obj, values)`, and pass the result to `save_summary()`
    (or `describe_changes()` for anything not named create/edit).
    Appending an uploaded image counts as a changed field. Logging old
    or new values would copy page content and personal data into a
    second table that is never pruned — don't, however useful it looks.
- Feature flags: optional/phased modules are listed in `FEATURES` in
  `app.py` (name, label, description, default) and stored one row per
  name in `FeatureFlag` — `init-db` is idempotent and inserts only
  missing names, exactly like `DEFAULT_BLOCKS`. Guard public routes with
  `@feature_required("name")` (404 when off), pick up the flags in
  templates via the `features` dict from the context processor, and read
  them in route code with `feature_enabled("name")`.
  - Switching a feature off ONLY hides it. Never delete, archive or skip
    writing data because a flag is off; switching it back on must restore
    the pages exactly as they were. The Stripe webhook is deliberately
    NOT flagged, so a payment already in flight still completes.
  - Core features (home, about, events, gallery, contact) are always on
    and must never gain a flag.
  - Admin routes stay reachable when a feature is off (only the nav link
    hides), so content is never stranded. It is a tidiness feature, not
    a security boundary — anything that must actually be protected needs
    an auth check of its own.
  - The ONE exception is listed in `ADMIN_FLAG_GATES`: an admin page
    whose route must enforce its flag, not merely hide its link. Today
    that is `/admin/audit` under `audit_log`. Add to that dict and the
    route must `abort(403)`; `tests/smoke_test_admin_flag_gates.py`
    walks the URL map and proves every entry does, and that every other
    admin page still opens with all flags off.
  - A gate reads the flag with `flag_explicitly_on()`, NOT
    `feature_enabled()`. The latter falls back to the FEATURES default
    when no row exists — right for a module that must work before
    init-db, wrong for a gate: on a database predating the flag it
    opened the audit log to client admins, which is exactly the bug that
    put this rule here. For a gate, "no row" means no.
  - The menu link and the route must read the SAME helper
    (`can_read_audit()`, exposed to templates as `audit_readable`), so
    the two can never disagree about who may see something.
- Dashboard: every module has a card at /admin, so a new one means a
  card in `dashboard_cards()` and nothing else — a module with no card
  is a module the admin forgets exists.
  - A flagged module's card is built inside `if flags["<name>"]`, so
    cards appear and disappear exactly as the nav links do. The same
    goes for its `dashboard_attention()` checks: a module switched off
    must not nag.
  - AGGREGATE ONLY. A count or a total, never a name, an address or an
    amount tied to a person. The one exception is the recent-activity
    list, which is the audit log's own summaries and is super admins
    only. Neither is a view of personal data, which is why the page does
    not `log_action()` — unlike the contributor, Gift Aid and membership
    pages, which are and do log.
  - COUNT QUERIES ONLY: `_count()`, `_published_split()`,
    `_no_photo_count()` and the aggregate sums. Nothing here may load
    rows to `len()` them, and no "needs attention" check may run a query
    per record — the page is the first thing after every login and has
    to stay the same cost as the site fills up.
  - The one red card (`.admin-stat-alert`) is for something actually
    waiting on a human — today, membership applications still at 'new'.
    Keep it to that; a dashboard where everything shouts says nothing.
  - "Needs attention" is hidden entirely when it is empty, and each item
    is one sentence in plain British English plus a link to where the
    fix happens. Anything still holding seeded PLACEHOLDER copy shows
    there, with the legal pages called out as a LAUNCH BLOCKER.
- Form controls have ONE definition, selected by CONTAINER (`.field`,
  `.contact-form`, `.login-card`) rather than by input type. The old list
  named types, `number` was not among them, and the donation amount box
  sat small and square beside rounded ones for months — any type added
  later would have gone the same way. Do not add a per-form copy of the
  padding/radius/border: if a form looks wrong, the shared rule is
  wrong. The footer newsletter is the one deliberate variant (dark
  ground, pill shape to match the button beside it) and still shares the
  height and font size.
  - Only genuine differences belong per field: `input[type=number]` is
    narrower and nothing else. Note that the shared selector's `:not()`
    chain outranks a plain `.field input[type=x]`, so a per-type
    override needs the shared rule to leave that property alone.
- Every public page must be reachable from the nav or the footer.
  `tests/smoke_test_navigation.py` walks the URL map and fails otherwise;
  a page reached from inside a section (`/gallery/all`) is listed there
  with its parent and the link is CHECKED on that page, and the handful
  that nothing links (crawler and Stripe-return URLs) are listed with the
  reason. Legal pages are footer-only, which is conventional — do not
  add them to the header.
- CSS: extend `static/css/style.css` using the existing custom properties
  (`--green`, `--red`, `--paper`, etc.) and class naming style. Design
  identity: Bangladeshi flag bottle green + red circle motif, Bengali
  script accents in section eyebrows (`.eyebrow .bn`).
- Browser-tab icons live in ONE place: `templates/_icons.html`, included
  by every template that owns a `<head>` (`base.html`,
  `admin/base_admin.html`, `admin/login.html`, `admin/login_2fa.html`).
  Add an icon or a `theme-color` there, never in a single template — the
  admin pages went months with no tab icon because the block was only
  ever in the public base. A new standalone page with its own `<head>`
  must include it too.
  - The icons are generated from `ebwa-mark.png` (the simplified lily) —
    NOT `ebwa-logo.png`, the full badge, whose lettering and Bengali
    script are illegible below about 64px. The set is 16x16 and 32x32
    PNGs plus a 180x180 apple-touch-icon (opaque white background, as
    iOS ignores transparency). Regenerate by trimming the mark to its
    alpha bounding box and scaling with LANCZOS; the 16px one gets its
    edge opacity lifted (alpha gamma ~0.7) or the thin strokes wash out.
- Header: the nav must never wrap to a second line. It sheds things in
  order as the viewport narrows — brand strapline at 980px, whole nav to
  the menu button at 899px. Header height is `--header-h`; the open
  mobile menu is positioned from it, so change the variable, not both.
  - The row is GROUPED, not flat: three group triggers ("About us",
    "What's on", "Get involved"), each opening a dropdown, plus exactly
    ONE primary action — the Donate pill (`.nav-donate`, red, flag-gated
    on `donations`). A new page joins an existing group; it does not
    join the row. Ten flat items had filled the row and each addition
    was buying space from something else, which is what the grouping
    ended.
  - What each group MEANS, because that decides where a new page goes:
    - **About us** — who EBWA is and what it offers: About, Our Journey,
      FAQ, and the community resources directory.
    - **What's on** — what is happening: events, news, gallery.
    - **Get involved** — what a VISITOR DOES to support EBWA:
      membership, collections, contact (and Donate beside it).
    Community resources sat under Get involved and was moved: the
    directory is a service EBWA provides to people who may need help, and
    filing it beside volunteering and donating asks something of the
    reader at the moment they are looking for support. Group by what the
    page is FOR, not by which list has room.
  - The footer's quick links carry the same items in the same order as
    the menu. Two lists in different orders is two things to keep in
    step, and one of them drifts.
  - Every group trigger is a real link to a CORE page (`about`,
    `events`, `contact` — none of them flaggable), so a click always
    lands somewhere and a group can never end up empty. Dropdown items
    are flag-gated individually.
  - The dropdowns are pure CSS: `:hover` and `:focus-within` on the
    `.nav-group`, no script anywhere. Two rules make that work and must
    not be undone:
    - the trigger is full header height, so the pointer never crosses a
      gap on its way to the panel;
    - `visibility` is NOT transitioned. Animating it delays the switch
      to `visible`, and a hidden item cannot take focus, so a keyboard
      user tabbing at speed sails straight past the panel. Animate
      opacity and transform only.
  - On a phone the groups do not become nested menus: inside the open
    `.nav-links.open` the panels are `position:static` and always
    visible, each group a heading with its pages beneath it.
  - `tests/check_header_layout.py` is the proof, at the widths in its
    own `WIDTHS` list — 1440/1280/1024/900/768/390, which includes 900
    and 768 either side of the 899px shed point, since that is where a
    header regression would land: one line (measured by item CENTRES, since a trigger is taller
    than the pill), no sideways scroll, panels shut until hovered or
    focused, every destination reachable by Tab alone, and the mobile
    menu listing all of them. Run it after touching the header, the nav
    or the breakpoints.
- Copy/tone: British English. Public-facing text should read warmly and
  plainly — this is a community charity, not a SaaS product.
- Cookies: the site sets exactly two, both first-party and strictly
  necessary — the Flask login session, and `ebwa_notice` recording that
  the footer notice has been read. There is NO analytics, advertising or
  tracking of any kind.
  - The banner is therefore INFORMATIONAL, not a consent mechanism. It
    does not collect, store or represent consent, and dismissing it is
    an acknowledgement only. Never describe it as consent, and never
    treat `ebwa_notice` as a lawful basis for anything.
  - If analytics or any third-party script is ever added, this banner is
    NOT sufficient: PECR requires prior, informed, refusable consent
    before such cookies are set, which means a real consent flow (block
    the script until opted in, offer an equal "reject", store the
    choice). Raise it rather than quietly reusing the banner.
  - Dismissal is a server-set cookie via a plain POST form — no
    localStorage and no inline script, so the CSP stays as tight as it
    is. Keep it that way.

## Database changes

No migration tool. Schema changes are additive:

1. Add the model/column in `app.py`.
2. New TABLES: `flask --app app init-db` (runs `create_all`; only creates
   what's missing, never drops or alters).
3. New COLUMNS on existing tables: provide the manual
   `ALTER TABLE ... ADD COLUMN ...` statement in the change notes, to be
   run with `sqlite3 instance/ebwa.db` before restart.
4. Never write code that drops tables or deletes data.
5. **Append an entry to `DEPLOY.md` in the SAME commit.** That file is
   the only record of what has been applied to which environment; a
   schema change missing from it gets missed on a deploy, and the site
   500s on the first request after the restart. Newest entry at the top,
   with the date, the commit subject, the exact statements (`ALTER
   TABLE ...` and/or `flask --app app init-db`), anything else the
   deploy needs (a `pip install`, an env var, a CLI command someone must
   run afterwards), and unticked boxes for local / demo VPS /
   production.
   - This applies to seed-only changes too — new `DEFAULT_BLOCKS` keys
     or a new seeded list need `init-db` even though no schema moves.
   - A commit cannot contain its own hash, so head the entry
     `## (pending) — <date> — <subject>` and backfill the short hash
     with the next commit. Only the HASH may be backfilled; the entry
     itself goes in with the change that caused it.
   - Tick a box only once it has actually been applied to that
     environment, and commit the tick.
6. `flask --app app check-schema` compares the models against the
   database, names any missing tables or columns, suggests the
   `ALTER TABLE` for each, and exits 1 if anything is missing. Run it at
   the end of every deploy, BEFORE the restart — it turns a missed step
   into a failed check instead of a 500 for every visitor. It is
   read-only. Leftover tables from retired modules are reported but
   never fail it, since this project never drops anything.

## Testing

Smoke-test with Flask's test client (see README history): assert status
codes for every new route, auth redirects (302) for anonymous access to
admin, form create/edit/delete round-trips, and that unpublished content
is absent from public pages and 404s by direct URL. Smoke tests live in
tests/; run them against a scratchpad DATABASE_URL, never
instance/ebwa.db.

Layout changes also have `tests/check_header_layout.py` — a Playwright
run against real Chromium at 1440/1280/1024/900/768/390px asserting the
nav stays on one line, nothing scrolls sideways, and the mobile menu
opens. 900 and 768 straddle the 899px shed point deliberately; keep both
if you change the list.
It needs a browser so it is NOT part of the smoke suite (`smoke_test_*`);
run it by hand after touching the header, nav or breakpoints:
`python tests/check_header_layout.py [--shots DIR]`.

`tests/check_partner_marquee.py` is the other behavioural one, for the
partner scroller — the half of it that only a browser can answer, the
markup half staying in `tests/smoke_test_partners.py`. It drives the row
at all six widths and at 5/7/9 partners and asserts the loop closes with
NO GAP (measured as a reader sees it — cards clipped to the visible
strip, widest empty run found — at rest and after each of a drag, a
wheel and a Tab, the three things that used to open one), that a step is
exactly one card plus one gap and then rests, the pause on hover and
focus, drag-to-scroll not swallowing the click while a sub-threshold
wobble still opens the partner, the arrows appearing only for a still
row with somewhere to go, a touch swipe still panning, and every mode
falling back to still under reduced motion. Run it after touching the
row, its script or `PARTNER_MOTIONS`:
`python tests/check_partner_marquee.py [--shots DIR]`. Three things it
knows that are easy to rediscover the hard way: Playwright's Chromium
has OVERLAY scrollbars, so hiding one frees no layout space and only the
computed `scrollbar-width` means anything; a smooth `scrollBy` is still
gliding on the next line, so positions are read through `settle()`
rather than after a guessed timeout; and click coordinates come from the
widest VISIBLE slice of a card, since a fixed offset lands in the gap
between two cards at some widths and a card is wider than the strip at
390px. Touch is driven with raw `Input.dispatchTouchEvent` over CDP —
`Input.synthesizeScrollGesture` moves nothing at all in this headless
build, in either direction.

Every browser check runs STILL by default — `prefers-reduced-motion:
reduce`, set per context through `tests/browser_motion.py`
(`new_page()` / `new_context()`). Rules for it:

- **A check that scrolls and then measures is only correct in a still
  context.** The stylesheet's first block turns
  `html{scroll-behavior:smooth}` off under reduced motion; with it on,
  `window.scrollTo` ANIMATES, so anything measured before it lands is
  measured against a moving page. That is exactly what made the
  cookie-notice check read a fixed strip at the bottom of the VIEWPORT
  as overlapping a footer still below the fold — at all six widths, and
  worse the taller the page (31px short at 1440, 481px at 390). The site
  was never wrong; the measurement was. The same goes for reading a
  computed opacity or transform mid-transition.
- Still is the DEFAULT and is passed explicitly, never left to
  Chromium's own default, so what the check assumes is written where the
  page is opened.
- **A check testing motion itself asks for `MOVING` at the check** —
  `tests/check_partner_marquee.py`, whose drift, stepping and
  arrow-swapping are the behaviour under test. Inheriting the still
  default there would pass while measuring a row that never moved, and
  under reduced motion the stylesheet also hides the duplicate
  `.partner-set` and turns snapping back on, so the thing being asserted
  is not even on the page. That file mixes both on purpose: MOVING for
  the drift and the steps, STILL for the drag, the arrows and the touch
  pan — which is the state those are about, and which stops the row
  sliding under the pointer while the check measures it.
- It is a CONTEXT option and not a launch flag (`--force-prefers-
  reduced-motion`) on purpose: a launch flag is the whole browser, so
  the motion-enabled checks could not opt out, and the choice would stop
  being visible at the check that depends on it.
- Belt and braces still belong in the check: the cookie-notice
  measurement asks for an instant scroll AND waits for it to land,
  rather than trusting the context to have made that true.

## Deploy (production pattern)

```
cd /opt/ebwa
git pull
# any manual ALTER TABLE statements, then if new tables:
# flask --app app init-db
sudo systemctl restart ebwa
```

Schema step ALWAYS before restart. Backups: nightly
`sqlite3 instance/ebwa.db ".backup ..."` + `static/uploads/` copy.

## Donations & collections module (confirmed scope — build rules)

This is the most sensitive module. Follow these rules exactly:

- Payment provider: Stripe (Checkout or Payment Element). Keys via env
  vars, never committed. Amounts stored in pence (integer), GBP only.
- Two payment kinds: (1) GENERAL DONATION to the charity; (2) EVENT
  COLLECTION — payment toward a specific campaign/trip (e.g. seaside
  trip), with its own page, optional target amount and running total.
- CRITICAL Gift Aid rule (HMRC): Gift Aid applies ONLY to genuine gifts.
  A payment for a place on a trip/event (person receives a benefit) can
  NEVER carry Gift Aid. Model this structurally: an event-collection
  payment has a `fee_amount` (no Gift Aid, ever) plus an optional
  `donation_amount` (voluntary extra on top) — only the donation part may
  carry a Gift Aid declaration. General donations are 100% donation.
  It must be impossible in the data model and UI to attach Gift Aid to a
  fee. Do not "simplify" this.
- Gift Aid declaration capture: full name, house name/number + postcode,
  tick-box with HMRC wording (UK taxpayer, understands responsibility).
  Store against the payment record; records retained (no auto-deletion).
- Admin: per-campaign contributor lists (view/print/CSV export), and a
  "Gift Aid claim export" — CSV in HMRC Charities Online schedule layout,
  date-range filtered, including ONLY donation portions with valid
  declarations.
- Donor personal data is sensitive: admin-only, never in public templates,
  minimal collection, and covered by the DPA — treat like the private
  minutes rule (nothing via static URLs).
- Stripe webhooks must be verified (signature) and idempotent.

## Current state / roadmap

Built: pages + Block CMS, events (slug pages, upcoming/past), gallery,
testimonials, partners, newsletter subscribers (+ CSV export),
sitemap.xml/robots.txt, animated stat counters, WAL mode.

Gallery albums (rules above): `GalleryAlbum` + `GalleryImage.album_id`,
public album cards at /gallery, album pages at /gallery/<slug>, the
everything view at /gallery/all, masonry honouring each photo's real
shape, and a vanilla-JS lightbox (keyboard, swipe, no-JS fallback).
Admin: album CRUD at /admin/gallery/albums plus an album picker and bulk
move on the photo screen. Deploy:
`ALTER TABLE gallery_image ADD COLUMN album_id INTEGER;` then
`flask --app app init-db` for the new table.

Image pipeline (rules above): Pillow-backed `save_upload()` — orientation
applied, EXIF/GPS stripped, capped at 1600px, JPEG q82 — plus 600px
thumbnails, `thumb_url()`/`upload_url()` template helpers and the
`reprocess-images` CLI command. Measured on a homepage carrying eight
photographs straight off a phone: 8,915 KB of images before, 926 KB
after (90% less). Deploy: `pip install -r requirements.txt` for Pillow,
then `flask --app app reprocess-images` once.

Server health panel (rules above): read-only CPU, memory, disk, uptime,
service state, network counters, version and schema state on the
super-admin Settings page, with an opt-in 30-second refresh from
`/admin/settings/health.json`. Deploy: `pip install -r requirements.txt`
for psutil — the panel works without it, with fewer numbers.

NAS transfer (rules above): paramiko SFTP upload of each archive over
Tailscale, settings and an encrypted password on the Settings page,
`run-scheduled-backup` for cron, remote retention, and the outcome
recorded on the same `BackupRun`. Deploy: five `ALTER TABLE`s, `init-db`,
`pip install -r requirements.txt` for paramiko and cryptography, plus
`FERNET_KEY` in the environment.

Backups and security visibility (rules above): `BackupRun` +
`backup-now` CLI + a read-only Settings panel with a "Back up now"
button, failed-sign-in counts on the dashboard and an optional alert
email. Deploy: new table — `flask --app app init-db` — plus optional
`BACKUP_DIR` / `BACKUP_KEEP`. (The off-server copy was a cron job Netbus
set up by hand when this was written; the NAS transfer entry above
replaced it.)

Contact form and mail layer (rules above): `send_mail()` over smtplib,
every setting resolved by `mail_settings()` — the Block a super admin
filled in wins, the environment variable is the fallback — including the
password, which is Fernet-encrypted at rest when set on Settings and
falls back to `SMTP_PASSWORD`. Plus the `test-mail` CLI, the whole of
Settings → Email (host, port, encryption, user, from, recipient, test
send), the form on /contact behind the `contact_form` flag with honeypot
+ timing + rate limiting, and `ContactMessage` with an admin list at
/admin/messages (statuses, unread badge, mailto reply, no export).
Deploy: new table — `flask --app app init-db` — plus `FERNET_KEY`, and
the SMTP environment variables for any setting not typed on Settings.

FAQ module (rules above): `Faq` model, public /faq with accordions and
FAQPage structured data, admin CRUD at /admin/faq, behind the `faq`
feature flag, linked from the nav, the footer and the sitemap. Deploy:
new table only — `flask --app app init-db`.

Built (Phase 1, contract JUL112601, £3,000 — all client-confirmed):

- News & Projects module (Event pattern; homepage "Latest news" max 3).
- Community resources directory (/resources grouped by category; admin
  CRUD with category suggestions).
- "Become a member" form (/membership with honeypot; admin list with
  status workflow new/contacted/approved/declined + CSV export).
  Client-confirmed eligibility (per EBWA constitution): four required
  tick-boxes — 18+, Bangladeshi origin, lives/works in Enfield, fee
  paid/will pay — stored as booleans, all enforced server-side.
  IMPORTANT: Bangladeshi origin is SPECIAL-CATEGORY data (ethnic
  origin): admin-only, excluded from any CSV export unless explicitly
  needed, and covered by the privacy notice on the form.
- Donations & event collections (rules above): /donate general
  donations; /collections/<slug> campaign pages with place fee +
  optional donation, running totals/targets; verified idempotent Stripe
  webhook; admin campaign CRUD + contributor lists (print/CSV); Gift
  Aid claims page with HMRC Charities Online CSV export and
  declarations record-keeping view. Gift Aid rules enforced in UI,
  server AND Payment CHECK constraints.
- Pre-launch hardening: security headers (CSP/nosniff/Referrer-Policy),
  in-memory rate limiting (login/subscribe/donation POSTs), /healthz,
  URL-map test asserting every /admin route requires login.

Built (post-signing variation, Jul 2026):

- Our Journey module (milestones + funding track record, consolidated):
  `Milestone` model (year, title, summary, outcome, optional
  funder_name/amount_pence/funder_url, optional image via save_upload);
  public /our-journey grouped by year descending with a "Funded by
  [name], £X,XXX" line where funder fields are set, intro editable via
  Block group `journey`; admin CRUD at /admin/journey matching the
  events admin; nav links + sitemap entry. Deploy: new table only —
  `flask --app app init-db`.
  IMPORTANT: institutional funders only (councils, trusts, foundations)
  in funder fields; individual donors must NEVER be published here
  without documented consent.
  (An earlier /track-record module (`FundingRecord`) was superseded by
  this and removed; dbs that ran init-db while it existed retain a
  harmless orphan `funding_record` table and `track_record` Block row.)
- Super-admin tier + feature flags (conventions above): `User.role`
  ('admin' | 'super_admin'), `flask --app app promote-super-admin`;
  `FeatureFlag` table seeded from `FEATURES` in `app.py`, which is the
  authoritative list — read it there rather than looking for one here,
  because a list kept in two places drifts (this one named five flags
  for a while after there were nine); super-admin-only Settings page at
  /admin/features with per-feature on/off toggles. Deploy:
  `ALTER TABLE user ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT
  'admin';` then `flask --app app init-db` for the new table.
- Account settings at /admin/account (all roles): change your own
  password — current password verified, `MIN_PASSWORD_LEN` enforced,
  confirmation must match, existing werkzeug hashing. Deliberately
  logged-in only: there is no reset/forgotten-password flow on the
  login page, so a lost password is reset by Netbus on the server.
  No schema change.
- Two-factor authentication (optional, per user, TOTP — RFC 6238, the
  standard 30-second 6-digit codes any authenticator app produces):
  `pyotp` + `qrcode` for the enrolment QR, `totp_secret` /
  `totp_enabled` / `totp_last_counter` on `User`, single-use hashed
  `RecoveryCode` rows. Enrol and disable from /admin/account, both
  requiring a working code; login gains a second step at
  /admin/login/2fa. Deploy: three ALTER TABLEs (below) then
  `flask --app app init-db` for `recovery_code`, plus
  `pip install -r requirements.txt` for the two new packages.
  - `ALTER TABLE user ADD COLUMN totp_secret VARCHAR(64) DEFAULT '';`
  - `ALTER TABLE user ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT 0;`
  - `ALTER TABLE user ADD COLUMN totp_last_counter INTEGER;`
  Rules if you touch this code:
  - The secret is server-side only. It reaches the authenticator app
    through the enrolment QR and nothing else — never put it in a
    session, cookie, hidden field or log line. Between the password and
    the code the signed session holds only the user id and a timestamp.
  - Verify codes with `verify_totp()`, never `pyotp.TOTP.verify()`
    directly: it enforces the ±1 step window AND the single-use replay
    guard (`totp_last_counter`), so an intercepted code cannot be used
    twice while still in window.
  - Recovery codes are hashed like passwords, shown exactly once at
    enrolment, and spent by stamping `used_at`. There is no way to
    redisplay them — a user who runs out turns 2FA off and re-enrols.
  - The code step is rate limited (`totp` scope) because six digits is
    guessable. Do not remove it.
- Super-admin user management at /admin/users (conventions above):
  lists every account with role, 2FA status and created date — never a
  password hash or TOTP secret — with create, reset password, reset
  2FA, change role and delete, each a POST form with a JS `confirm()`.
  Deploy: `ALTER TABLE user ADD COLUMN created_at DATETIME;` (nullable
  by necessity — SQLite refuses a CURRENT_TIMESTAMP default on ADD
  COLUMN, so accounts predating it show "—").
- "What we do" service cards (`Service` model: title, description, icon,
  sort, published) replacing the six hardcoded homepage cards, with
  admin CRUD at /admin/services and a publish toggle. `icon` is a single
  emoji typed into the form — no icon library. Seeded from
  `DEFAULT_SERVICES` into an empty table only. The contact page's
  headings and field labels became Blocks in the same change, so every
  text string in that section is editable. Deploy: new table only —
  `flask --app app init-db`.
- Rich content system (rules above): `ContentImage` + the three layout
  presets, behind the `rich_layouts` flag. **Rollout complete: About,
  News, Events and Our Journey.** Listing and card views deliberately
  keep using the single `image` column, so a post's lead photo is the
  one that appears in a card.
  Deploy: table and columns landed with the About commit (see DEPLOY.md).
  - Our Journey is the one page that renders MANY owners at once, so it
    uses `rich_content_for_many()` (one query for every milestone's
    images) rather than `rich_content_for()` per row, and each entry is
    rendered by the same macro inside a `.journey-entry` frame. The
    presets are scaled down there to an image treatment for one entry —
    see the Our Journey block in the stylesheet for why. A future page
    that lists rich owners should copy this, not add its own queries.
- Partner cards can show a logo: `Partner.display_mode` ('text' |
  'image' | 'both', see `PARTNER_MODES`) plus an optional `logo` upload,
  with admin CRUD moved to the list + form-page pattern so logos can be
  added to existing rows. A logo mode with no logo falls back to text
  (`shows_logo` / `shows_text`), so a half-finished partner never renders
  an empty card. Deploy:
  `ALTER TABLE partner ADD COLUMN logo VARCHAR(255) DEFAULT '';` and
  `ALTER TABLE partner ADD COLUMN display_mode VARCHAR(10) NOT NULL
  DEFAULT 'text';`
  - A NEW partner defaults to 'image'; the column's `server_default`
    stays 'text' so the ALTER above still backfills existing rows with
    the look they had. Two different defaults on purpose — new rows get
    the app's, rows already on disk keep theirs — which is why
    `_suggested_alter()` prefers a server default over the Python one.
    Never "tidy" that by migrating existing partners: a row whose mode
    said logo when it had none would be fine (it falls back), but a row
    that deliberately shows its name and blurb would silently lose them.
  - Every logo renders in an IDENTICAL 200x100 box, `object-fit:
    contain`, on a white tile with a hairline border. The tile is the
    point: without it a transparent PNG floats on the card and a
    white-background JPEG shows as a rectangle, and the wall of partners
    looks like a mistake. Never crop or stretch a partner's mark to fill
    a box — it is somebody's identity, and the admin field says what
    size to supply (400x200 PNG, transparent).
  - More than four partners becomes a scroller (`.partner-marquee`
    inside `.partner-row`), four or fewer stay the static
    `.partner-grid`. Rules for it:
    - **ONE offset moves the row: its own `scrollLeft`.** The drift, the
      stepping, dragging, swiping and the arrows all push that same
      number, and the script wraps it back by exactly one set so the row
      never reaches an end. It used to be a CSS animation translating
      the track while the container scrolled underneath it — two
      offsets, which ADDED UP, so the first drag or Tab into the row
      left the animation running off the end of its content and a gap
      opened in the loop. Measured: the two-offset version lost up to
      4,028px of cards out of the window after a scroll; with one offset
      the worst case is a positive margin at every count and width. Do
      not put a transform back on the track.
      - More copies of the set would NOT have fixed that, which is worth
        knowing before someone tries it: the container can always be
        scrolled to its own end, and that end moves with the content.
        Untouched, two sets were always enough — one set is wider than
        the widest the row can be.
    - It pauses for anybody reading: pointer over it, keyboard focus
      inside it, or a drag in progress. It also stops while the row is
      off screen, so an idle tab is not animating nothing.
    - The loop needs a duplicate set. That copy is decoration:
      `aria-hidden="true"` on the set AND `tabindex="-1"` with empty alt
      on every card inside it, or a screen reader reads the partner list
      twice and Tab walks through phantom links.
    - Cards snap to their START, not their centre, so an arrow press
      moves exactly one card and the row rests with a card against the
      left edge.
    - The logos are deliberately NOT `loading="lazy"`. They are 200x100
      thumbnails that the row brings into view constantly, so lazy
      loading buys nothing and risks a blank tile arriving mid-scroll.
    - How it moves is a SITE setting, not a per-partner one:
      `PARTNER_MOTIONS` (scroll / step / none) plus
      `partners_step_seconds`, both Blocks, both set at Admin →
      Partners and both in `HIDDEN_BLOCK_KEYS`. The mode reaches the
      page as `data-motion` on `.partner-row`, never as Jinja inside the
      script.
    - **Every mode falls back to a still row under
      `prefers-reduced-motion`**, and that is not a fourth option for an
      admin to override. The script checks `matchMedia` before it starts
      anything, and the stylesheet hides the copy set — keep both.
    - **The arrows are the affordance for a row that is standing still**
      (the `none` setting, or anybody with reduced motion). The script
      un-hides them only when the row is static AND has somewhere to go,
      and the scrollbar-hiding rules hang off the classes it sets
      (`has-arrows` / `is-moving`) — so with no JavaScript there are
      neither dead buttons nor a hidden scrollbar, and the row still
      scrolls. They move one card, disable at each end, and carry real
      labels ("Previous partners" / "Next partners"). On a phone they
      sit UNDER the row: beside it they took 84px of a 342px window and
      no card was ever fully in view.
    - Drag-to-scroll (pointer events, no library) is what a mouse user
      has instead of a scrollbar. Two things it must keep doing, both
      learned by breaking them:
      - **Pointer capture only AFTER the drag threshold** (`DRAG_MIN`,
        6px). Capturing on pointerdown retargets the pointer events, and
        the mouseup the browser builds from them, to the row — so the
        click fires on the row instead of the partner and the link never
        opens.
      - **The browser's own drag of a logo or link must be cancelled**
        (`user-select`/`-webkit-user-drag` in the stylesheet plus a
        `dragstart` preventDefault). Otherwise Chromium starts a native
        drag on the first pointermove and takes the rest of the pointer
        stream with it: no pointerup, no click, no scrolling.
      Touch is left alone — the browser pans natively, and the handler
      returns immediately for `pointerType === 'touch'`.
- Legal pages: /privacy and /terms rendering `legal` group Blocks
  (title + multi-paragraph body, split on newlines like `about_body`),
  linked in the footer and listed in the sitemap. Core pages — not
  flaggable. **Seeded with PLACEHOLDER copy: EBWA must supply the real
  privacy notice and terms before launch.** Plus the footer cookie
  notice (rules above). Deploy: `flask --app app init-db` seeds the new
  blocks; no new tables.
- Audit log (rules above): `AuditLog` model + `log_action()`, wired into
  logins (success and failure), logout, password change, 2FA on/off,
  every create/edit/delete/status-change across all content modules,
  every super-admin user-management action, every feature toggle, and
  every export or printable personal-data view. Read-only paginated
  page at /admin/audit with who/action/date filters, gated by the
  `audit_log` flag for client admins only. Deploy: new table only —
  `flask --app app init-db`.
- Dashboard overview at /admin (rules above): the three original KPI
  cards became grouped rows — pages and content, people, donations and
  collections — with a card per module, unpublished counts noted
  underneath, each card a link to its admin page, money via the `pounds`
  filter and the Gift Aid figure taken from `gift_aid_claimable_query()`
  so it can never disagree with the claim page. Above the cards sits a
  "needs attention" panel (applications still 'new', published events
  whose date has passed, PLACEHOLDER copy, campaigns and published
  content with no photo, payments unfinished for over a day), and below
  them the six newest audit entries in UK local time for super admins.
  No schema change.

Each module has a smoke test in tests/ (smoke_test_<module>.py, run
directly with python); seed_demo.py fills a fresh db with demo content.
Phase 1 is code-complete but NOT yet deployed: deploy needs init-db
(new tables), pip install (stripe), Stripe env vars + webhook
registration (see README).

Phase 2 (separately quoted — DO NOT build under Phase 1): Board
Transparency Hub (board-member tier as a third `User.role` value — the
column already exists, private minutes stored outside static/ and served
via authenticated route, public AGM minutes),
Bengali page translations (Bengali twin values for Blocks + small chrome
translation dict, EN | বাংলা toggle), booking system (not specced — do
not build).
