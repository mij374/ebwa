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
  - **A row and the file it names are two different things, and they
    can part company.** A content image whose file is not on disk is
    NOT rendered to a visitor: `present_images()` drops it and the
    layout closes up. Signed-in admins get an `.rc-missing` panel in
    its place naming the file, because they are the ones who can put
    the photograph back. What it used to do was render an empty box
    with the alt text in it, which reads as a broken WEBSITE rather
    than as one missing photograph — found live on the demo, one
    ContentImage on /about 404ing.
  - `flask --app app check-uploads` names every database row pointing
    at a file that is not there, across every column that holds one
    (`UPLOAD_REFERENCES` — add to it when a new column holds an upload).
    Read-only, exits 1 when it finds any, and reads the uploads
    directory ONCE rather than stat-ing per row. Run it after moving,
    restoring or syncing `static/uploads/`, the same habit as
    `check-schema` before a restart. It deletes nothing: a file missing
    today may be one somebody is about to restore from a backup.
  - Test fixtures that insert an image row must WRITE A FILE for it —
    `tests/fake_uploads.py`, whose `fill_dangling()` materialises every
    reference in the test database whatever the fixtures called them.
    Four test files were asserting on rows that could never have
    rendered on a real site.
  - Alt text is REQUIRED on upload and cannot be emptied by an edit. An
    image nobody can describe is one a screen reader user simply loses.
    Images are lazy-loaded and every figure carries an aspect-ratio, so
    the page does not jump as they arrive.
  - **In the alternating preset, a row whose WORDS are too short to
    hold up a column becomes a full-width band** — the same
    `is-mediaonly` treatment a spare image with no words already gets,
    with the line above the photograph rather than centred beside it.
    Decided in `interleave_content()` (`row["wide"]`), where the pairing
    is already worked out, against `ALT_MIN_TEXT_CHARS`.
    - The threshold is on the ROW's total text, not one paragraph: two
      short paragraphs together fill the column perfectly well.
    - **130, tuned by eye and not guessed.** 200 was the first number
      and it was too aggressive — it turned ordinary three-line
      paragraphs into bands and would have stripped the left-right
      alternation out of most real content, which is the entire point
      of the preset. One line (55 chars) is marooned in 373px of
      nothing; two (105) reads as a gap; three (150) is a short
      paragraph beside a photo and perfectly fine. The cut belongs
      between two lines and three. The measurements are in the comment
      above the constant.
    - Vertical centring was NOT the fix and was never missing:
      `.rc-alt-row` has had `align-items:center` all along, which is
      what put the emptiness on both sides of a short line instead of
      below it.
    - **Below 920px this changes nothing**, and that is enforced: the
      row is already one column there, so there was no empty half to
      fix — but `is-mediaonly`'s 16/7 band would have cropped the
      photograph to 150px where it had been 274px on a phone. The
      media query puts 5/4 back for `.is-shorttext` only; a row with
      genuinely no words keeps its band, as it always has.
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
- **`send_mail()` takes a STRING OR A LIST of addresses** and
  normalises both through `recipient_header()` — commas or semicolons,
  blanks dropped, duplicates removed, joined with ", " for the header.
  It used to take a string and call `.strip()` on it, so
  `send_monthly_report` handing it a list was an `AttributeError` on
  every send, including the button whose entire job was to prove the
  address worked. Fixed in the helper rather than at the call site:
  every caller has a list of addresses in mind, some hold it as text a
  super admin typed and some as the parsed list, and making each one
  remember which is how this happened.
  - `security_alert_to()` predates it and joined by hand for exactly
    this reason — that path was right by design, not by accident. It
    survives for the places that want the addresses as TEXT (a flash, a
    log line) rather than as something to send to.
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
  - SWAP is shown BESIDE memory, never on its own: 85% of memory with
    2GB of swap behind it and 85% with none are different machines, and
    reading either number alone is how you draw the wrong conclusion. Its
    thresholds are deliberately tighter than memory's (amber at a
    quarter, not 80%) because swap is the overflow — any sustained use
    of it means the machine has already been under pressure and is now
    reading pages back off a disk, which is where a site feels slow for
    reasons the memory figure does not explain. No swap configured is
    reported as "none", which is a normal machine, not "unknown", which
    is a machine we could not ask.
  - FAILED SIGN-INS are counted as ONE TOTAL, not three kinds. A wrong
    password, a wrong two-factor code and an attempt stopped by the rate
    limiter are all recorded under the same `login_failed` action and
    told apart only by the sentence in their summary — counting them
    separately would mean matching on prose written for a trustee to
    read. The number that matters is how many times somebody failed to
    get in; which door they were stopped at is one click away in the log
    the figures link to.
  - Those figures are the one exception to "the panel contains no
    links". The rule is that NOTHING HERE ACTS — no restart, no service
    control, no command — not that the panel may not be navigated from,
    and a count you cannot click through to is a number somebody has to
    go and look up by hand. The test asserts every link in the panel is
    a plain GET to the audit log.
  - Day boundaries are UK LOCAL, per the admin date convention: "today"
    means today as somebody in Enfield counts it, though the column is
    naive UTC.
  - The auto-refresh is opt-in, every 30 seconds, and rate limited like
    everything else that reads the machine.
- Backups: `run_backup()` writes a database snapshot (through sqlite3's
  own backup API, so it is consistent while the site is serving) plus
  every upload into one timestamped zip in `BACKUP_DIR`, and records a
  `BackupRun` either way — a backup that failed silently is worse than
  none. `prune_backups()` keeps the newest `BACKUP_KEEP`.
  - **The Settings button RUNS IT IN A THREAD and returns at once**
    (`start_backup()` → `backup_job()`), and that is a correctness fix as
    much as a comfort one. It used to do the whole job inside the
    request: gunicorn's sync worker heartbeats to the arbiter BETWEEN
    requests, not during one, so a request blocking past `--timeout` (30
    seconds by default, and the unit sets none) has its worker killed
    underneath it — with nginx's 60-second read timeout behind that. At
    6MB the archive beat both; a real uploads folder plus a minute of
    SFTP would not, and what a super admin would get is a 502 from a
    backup that may well have succeeded, plus a `BackupRun` stuck at
    "running".
    - **A thread does NOT occupy a worker.** A sync worker handles one
      request at a time in its main thread; once the view returns it is
      back accepting connections while the thread runs alongside it. The
      cost is real but different: memory in that process, and the GIL
      during the zip's CPU-bound stretches (SFTP and file I/O release
      it). At this site's traffic — which the visitor statistics now
      measure — one of two workers being a little slower for a minute is
      an easy trade against every long backup returning a 502.
    - **A DAEMON thread**, so a deploy is not held open waiting for it.
      gunicorn gives a worker a moment on SIGTERM and then kills it, so a
      non-daemon thread would delay the restart and be killed anyway.
    - This is not a scheduler and must not become one. The NIGHTLY backup
      is still cron calling `run-scheduled-backup` in its own process
      (see below) — the rule against a background thread there stands,
      for a different reason: a thread per worker means several backups
      at once. Here there is exactly one, started by one person pressing
      one button, and guarded.
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
    - **`backup_in_progress()` IS A READ, AND A READ IS NOT A CLAIM.**
      That was tolerable while the button held the browser for the whole
      backup — nobody clicked twice — and stopped being so the moment it
      returned instantly. `claim_backup_slot()` is the claim: it INSERTS
      the row and then settles the race on the rows themselves, lowest id
      among the live "running" rows taking the slot and everybody else
      deleting their row and standing down. Both sides see the same rows
      and reach the same answer, with no lock and nothing left behind if
      a process dies mid-decision. The smoke test races four threads
      through it and requires exactly one winner; put the old read back
      and all four win.
  - **The panel's state is READ OFF THE `BackupRun` ROW** — started,
    finished, status and error are all already there, so there is no
    second idea of "what is happening" to keep in step with the first.
    `backup_state()` is the one place that turns those columns into what
    a person sees, and the page and the JSON poll both use it, so they
    cannot drift into two vocabularies.
    - FOUR states, not three: "running" alone cannot tell a backup in
      progress from one whose process was killed underneath it. Past
      `BACKUP_STALE_MINUTES` a row still saying "running" is reported as
      **interrupted** — not busy, so the panel stops polling, and worded
      so the admin knows nothing was damaged and another can be started.
      The guard already forgave such a row; before this the PANEL did
      not, and would have shown a backup running for days.
  - `/admin/settings/backup.json` is the poll, built like the health
    panel's: super admins only, rate limited, parameterless, and READ-
    ONLY. It starts nothing — the button is the only thing that acts, and
    it is a plain POST form that works with the script absent. The script
    polls only while the server says a run is BUSY and stops the moment
    it is not, so Settings left open is not a question every two seconds
    for the rest of the day.
    - With no script the page says to refresh to see progress, and that
      sentence is in the PAGE and hidden BY the script rather than sitting
      in a `<noscript>`: a script that is present but broken leaves the
      page exactly as delivered, and that is the case a `<noscript>` gets
      wrong.
    - `tests/check_backup_panel.py` is the browser half, and the part
      that makes it worth anything is the reload proof: it stamps a value
      on `window` before waiting and asserts the same value is still
      there when the panel has changed. Without that, a check would pass
      against a page that simply reloaded — or against no script at all.
  - Work that outlives the request needs `current_actor()`, captured IN
    the request and passed to the thread, because there is no
    `current_user` there. An audit entry reading "anonymous" for
    something a named super admin pressed a button to start has lost the
    only fact worth keeping. The start is logged BEFORE the thread is
    spawned: a backup that fails immediately can finish first, which put
    "Started" in the log after "failed".
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
- **An edit form's Published/Active checkbox renders `checked` from the
  row's CURRENT state.** An unticked checkbox posts nothing at all, so a
  form that showed the box unticked while editing a live item would take
  that item off the site the moment somebody fixed a typo in it — still
  listed in the admin, still apparently fine, simply gone from the
  website. All eight forms that have such a box do this correctly
  (events, news, milestones, testimonials, services, FAQ, albums,
  campaigns); resources and partners have no visibility flag at all.
  - Never put that checkbox inside a `{% if %}`. A box that is not
    rendered submits nothing, which is indistinguishable from unticked,
    so hiding it behind a feature flag would silently unpublish
    everything edited while the flag was off.
  - `tests/smoke_test_publish_state.py` proves it the only way that
    means anything: it fetches each edit form, collects the fields A
    BROWSER would submit (an unticked box contributing nothing), posts
    them straight back unchanged, and asserts the item is still live.
    It asserts the reverse too — unticking really does unpublish —
    because a test that only checked one direction would pass against a
    form that had lost its checkbox altogether.
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
- `audit_log` carries two indexes, `(action, created_at)` and
  `(created_at)`, because it only ever grows and three things read it on
  a schedule: the dashboard's failed sign-in count, the health panel's
  today/7/30-day counts, and the log's own listing. **`create_all()`
  does not add an index to a table that already exists**, so a new index
  on an old table is never created by `init-db`: it needs a
  `CREATE INDEX IF NOT EXISTS` in DEPLOY.md, like an `ALTER TABLE`.
  `check-schema` compares the models' indexes against the database and
  names any that are missing with the statement to run - it used to
  compare only columns, which is how this very pair sat missing on a
  database while the check called it up to date.
  - A missing index exits 1 like a missing column, but says plainly that
    the site RUNS without it and is only slower. The severity differs
    and a deployer under pressure should not have to guess.
  - An index the database has and the models do not is left alone, the
    same as an orphan table. That also means SQLite's own
    `sqlite_autoindex_*` entries for unique constraints are ignored
    without having to name them.
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
  ground, pill shape to match the button below it) and still shares the
  height and font size.
  - Its field and button are STACKED, not side by side. Sharing a line
    meant the input gave up whatever the button needed, and the button
    is `--large-text` bold for contrast — which left the field at 133px
    at 1440 and 106px at 1024, about eight characters of an email
    address. Stacked it is 269px and 242px. The button is left-aligned
    to the input's edge and sized to its own word, never stretched: a
    full-width Subscribe is the whole width of the screen once the
    footer stacks, and louder than Donate, which is the action that
    matters.
  - Only genuine differences belong per field: `input[type=number]` is
    narrower and nothing else. Note that the shared selector's `:not()`
    chain outranks a plain `.field input[type=x]`, so a per-type
    override needs the shared rule to leave that property alone.
  - **Every `type="number"` carries a `step` that matches its natural
    increment, and `min`/`max` mirroring whatever the ROUTE enforces.**
    The arrows are the point: a field whose real increment is 50 and
    whose step is 1 has arrows nobody can use, and a field with no `max`
    lets somebody arrow past a bound into a rejection they could have
    been stopped from reaching. `tests/check_number_inputs.py` drives
    every one of them in Chromium — presses the arrow and measures what
    moved, types an off-step value and asks the browser whether it would
    submit — and is the list of what exists.
    - **The step BASE is `min`, not zero.** `min="300" step="50"` makes
      300, 350, 400 ... valid and 360 INVALID, so a form nobody has
      touched refuses to submit with "the two nearest valid values are
      350 and 400". That is why the glide steps by 20 rather than the 50
      its range would suggest: 20 keeps the shipped 360 default and the
      3000 ceiling both on the grid. Check any new step against the
      DEFAULT VALUE, not just the range.
    - **Money keeps `step="0.01"` and gets quick-pick buttons instead.**
      A step of 5 would make the arrows useful and £12.50 unenterable,
      and somebody donating £12.50 matters more than somebody's arrows.
      `.amount-presets` is the shared markup (donate and the collection
      donation both use it, and both drive a tiny inline script). If a
      preset sets a value the page reacts to, call the handler by hand:
      assigning `.value` fires no `input` event, which would have left
      the Gift Aid box disabled on a £25 donation.
    - Every money input therefore carries `class="money"`, which HIDES
      ITS SPINNER (`appearance:textfield` plus the older
      `::-webkit-*-spin-button` spelling). A penny-a-press arrow is a
      control that looks useful and is not, on the one field where a
      wrong number costs somebody money. Nothing else about the field
      changes: still `type="number"`, so the browser still validates the
      range and a phone still opens a numeric keypad, and
      `inputmode="decimal"` asks for the one with a decimal point.
      - The WHEEL is suppressed on those fields, because a focused
        number input changes its value on a scroll and with no spinner
        there is nothing to suggest it might — somebody scrolling the
        page with the amount focused would silently give £24.99 instead
        of £25. The KEYBOARD arrows are deliberately LEFT: they are
        native, they are the only way a keyboard user can nudge a value,
        and there is no misleading control attached to them.
      - Settings fields keep their spinners — the glide's 20ms, the
        drift's 5, ports, retention, sort, year are all increments worth
        a press. `tests/check_number_inputs.py` asserts which fields
        have them and which do not, by computed `appearance`: headless
        Chromium paints no spin button on ANY number input, so clicking
        where the arrows would be proves nothing either way.
    - Sort fields keep `step="1"` and NO `min`: sorting is ascending and
      the route parses a bare `int()`, so a negative is a legitimate way
      to pin something to the top. The 10s in the content-image sort box
      are a suggested STARTING value, not a convention every row
      follows — `step="10"` would reject a hand-typed 15.
- Grid tracks that hold anything unbreakable are `minmax(0,1fr)`, never
  a bare `1fr`: an `fr` track's automatic minimum is MIN-CONTENT, so one
  item that cannot shrink sizes the track wider than its container and
  every cell overflows with it. The footer newsletter input and its
  button, side by side, did exactly that — 3px of sideways scroll on the
  page at 360px (a Galaxy S10) and 43px at 320px, at every breakpoint
  down to one column. `.admin-shell` carries the same guard for the same
  reason.
- **Every ORDER BY ends in a key that cannot tie**, normally `id`. Two
  rows with the same `sort` and nothing after it come back in whatever
  order the database finds them; SQLite's answer is rowid, so insertion
  order, which is a coincidence and not a promise — it moves when a row
  is rewritten, when an index appears, or on any other engine. The
  public page and its admin list must also carry the SAME order, so the
  person arranging the rows sees what the visitor will see.
  - A `sort` column is ascending, so a lower number is earlier and a
    NEGATIVE number pins to the top. That is deliberate and the reason
    the admin forms carry no `min` on a sort field.
  - Match the tie-break to the direction above it: an ascending list
    ends `id` ascending (insertion order), a newest-first list ends `id`
    DESCENDING, or the last two rows of a tie read backwards against the
    rest of the page.
  - Events are the exception that proves it: a day's events are ordered
    by `start_time`, which is FREE TEXT ("6:30 PM", "18:30", "Doors
    6.45") because that is what somebody typing an event writes and it
    prints as typed. No database can sort that — "10:00 AM" precedes
    "6:30 AM" as text — so `events_in_day_order()` sorts in Python,
    reading the first time-like thing in the box (`start_minutes()`).
    Entries with no readable time come AFTER the timed ones, then title,
    then id. The time sort runs FORWARDS even in the past list where the
    days run backwards: a day's programme reads in the order you could
    have attended it, whichever way the days go.
  - The album PICKER (`album_choices()`) is alphabetical while the album
    LIST is sort-then-newest, and that divergence is deliberate: a
    picker is for finding one album by name, a list is for presenting an
    arrangement. Both the comment and a test say so, so it does not get
    "fixed".
  - `tests/smoke_test_ordering.py` pins every one of them, and builds
    each tie on purpose — rows inserted in the opposite order to the one
    expected, so a missing tie-break fails rather than passing by luck
    on SQLite's rowid. Eleven checks in it fail without the keys added
    in the same commit.
- Testimonials follow the list-plus-form-page pattern (`Add`, `Edit`,
  publish toggle, delete) like partners, resources and services.
  **Editing matters more here than elsewhere: a testimonial is somebody
  else's words**, and before the edit route existed a typo in a quote
  could only be fixed by deleting it and typing it again from memory.
  - The quote row is keyboard-reachable through `tabindex="0"` on the
    scroller, because unlike a partner card a quote holds no link and
    there is nothing inside it to Tab to. A focusable scroll container
    is scrolled by the arrow keys natively, and focusing it pauses the
    motion like any other focus in the row.
  - The homepage shows at most `HOME_TESTIMONIALS` (12) quotes. It was
    six, sized for a grid three across and two down; the row scrolls
    now, so that constraint went with the grid.
  - **THERE IS NO PUBLIC PAGE LISTING TESTIMONIALS.** The homepage is
    the only place they appear, so that cap is the difference between
    published and visible — a quote past it is neither on a page nor
    404ing, just absent. If EBWA ever collects more than a dozen, the
    answer is a page of their own (the resources pattern), not a bigger
    number: `tests/smoke_test_navigation.py` would then require it to be
    reachable from the nav or the footer like every other public page.
- Every public page must be reachable from the nav or the footer.
  `tests/smoke_test_navigation.py` walks the URL map and fails otherwise;
  a page reached from inside a section (`/gallery/all`) is listed there
  with its parent and the link is CHECKED on that page, and the handful
  that nothing links (crawler and Stripe-return URLs) are listed with the
  reason. Legal pages are footer-only, which is conventional — do not
  add them to the header.
- **The homepage's plain/tinted bands are decided by POSITION, not by
  section.** `alternating_bands()` (a template global, beside
  `thumb_url`) takes the sections in the order the page emits them with
  whether each is visible, and hands back a class per name; skipped
  sections take no turn. The classes used to be written into the
  template one at a time, which is correct only when every section is on
  the page — and five of the six can vanish, from a feature flag or
  simply from having nothing to show this month. That is 64
  arrangements, one of which was right. Hide a section and the two
  either side come out the same colour, so the page has a seam in it
  where a band silently doubles in height.
  - **The order and the per-section switches are SETTINGS**, two Blocks
    in `HIDDEN_BLOCK_KEYS` (`home_section_order`, `home_sections_hidden`)
    edited on the Settings page, super admins only — arranging the front
    page is a design decision, not day-to-day content work. `HOME_SECTIONS`
    in app.py is the registry: key, the name an admin sees, what it
    holds. Each key is also its partial, `templates/home/_<key>.html`,
    and `home_layout()` returns keys from that tuple and nothing else,
    because the include path is built from them — a template path
    assembled from anything a request could influence is not a thing to
    have on a site that takes card payments.
    - **ADDING A SECTION**: an entry in `HOME_SECTIONS`, a partial, and
      the content in `home()`. It appears at the END for anyone who has
      already saved an order, and is shown by default.
    - Both fallbacks exist to stop the same thing — A SECTION MUST NOT
      DISAPPEAR BECAUSE SOMEBODY EDITED A LIST. An unknown key in the
      setting is ignored; a section missing from the setting is
      APPENDED, never dropped. And the hidden list stores the sections
      that are OFF, not the ones that are on: storing "on" would make a
      section added in a later deploy invisible on every site that had
      ever saved this setting, looking like a bug in the new feature.
    - A missing Block is treated exactly as an empty one, so **a deploy
      that skips `init-db` still renders the right front page** —
      pinned by a test, because a homepage that can lose its content to
      a forgotten command is the failure this feature must not add.
    - Hiding a section here is NOT a feature flag, and the panel says so
      in as many words: this takes a section off the FRONT PAGE, the
      flag removes the module from the whole site. Two controls that
      look alike and do very different things need the difference
      written down where somebody is about to press one.
    - Positions are read as numbers and SORTED, not trusted: duplicates
      and gaps are an arrangement somebody meant, and ties keep the
      order already on screen so nudging one number does not shuffle the
      rest. Visibility is read from `HOME_SECTIONS`, never from the
      form's own keys — an unticked checkbox posts nothing, so a
      form-shaped loop cannot tell "unticked" from "not submitted".
    - The reset restores the CONSTANT, never the last saved value, and
      is audit-logged even when it changes nothing — the same rule as
      the marquee speed reset.
  - Server-side rather than CSS, and the CSS option is worth knowing
    about before someone proposes it again: `:nth-of-type` counts by
    ELEMENT TYPE and ignores class, so it would work only while every
    band is a bare `<section>` sibling and nothing else is; and
    `:nth-child(even of .band)` is too new to rely on here, where a
    Samsung Internet 7 bug report is a thing that has actually happened.
    A helper is also the version a smoke test can exhaust in a blink.
  - `tests/smoke_test_home_bands.py` checks all 64 arrangements TWICE:
    against the helper, and against the rendered page. Both halves are
    needed — a helper that answers correctly and a template that asks
    it the wrong question look identical from the helper's side, and
    only the HTML can say the list is in the page's own order. It also
    renders each section alone (each must come out plain), pins the
    all-visible sequence so the page cannot silently change appearance
    for everybody, and asserts that faq, resources and our_journey move
    no band — they gate pages, not homepage sections.
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
- **The fonts are SELF-HOSTED, and that removed the last third-party
  request the site itself makes.** Linking `fonts.googleapis.com` meant
  every visitor's browser announced their IP address to Google before a
  word of the page was drawn — no cookie, nothing stored, but the site
  tells people there is no tracking of any kind, and a request to a
  third party on every page load is what makes a reader right to be
  sceptical of that. The font hosts are out of the CSP too, and the two
  must stay in step: leaving the domains in the policy would quietly
  re-permit what was removed.
  - **One file per family per SUBSET, not per weight.** All three are
    variable fonts and Google was serving the identical file for every
    weight requested — 30 downloads, 9 distinct files, verified by
    sha256. The `font-weight` ranges in `@font-face` are the real axis
    ranges read out of the files with fontTools, so the browser
    interpolates rather than synthesising a bold.
  - Google's own `unicode-range` lines are kept verbatim. They are what
    stops a visitor reading English ever downloading the Bengali subset
    — 187KB that only somebody reading Bengali needs.
  - **The version token is in the FILENAME** (`name.<sha8>.woff2`),
    because a `url()` inside a static stylesheet is not Jinja and cannot
    carry `asset_version()`. nginx serves /static with `expires 30d`,
    which is only safe while a changed file means a changed URL.
    `python tools/hash-fonts.py` re-stamps them and rewrites the CSS;
    run it after adding or replacing a face.
  - `tests/check_fonts.py` proves both halves in a browser, because
    neither can be read off the templates: that NOTHING leaves this
    server on 14 public and 3 admin pages, and that the Bengali face is
    loaded and usable for the actual eyebrow text at every viewport. A
    missing Bengali face does not error — it falls back to whatever
    serif has the glyphs, or to tofu — so it is measured with
    `document.fonts.check()` and a width, not eyeballed.
  - **THE MAP ON /contact IS STILL A THIRD PARTY**, and the check says
    so on purpose rather than hiding it: Google's iframe loads its own
    fonts from inside itself. Those are requests the MAP makes, not the
    page, and they are the one remaining off-site request on the site.
    If that ever needs to go too, the answer is the video's
    click-to-load treatment, not another allow-list entry.
- **Every reference to one of the site's own static files carries a
  version**: `url_for('static', filename='css/style.css',
  v=asset_version('css/style.css'))`. nginx serves `/static/` with
  `expires 30d`, which is correct ONLY because the URL changes when the
  file does — so keep the 30 days, and never add a static reference
  without the argument.
  - The failure it prevents does not look like a caching problem to
    anybody. A returning visitor's phone holds last month's stylesheet
    for up to a month after a deploy, so the site is broken on their
    device and plainly fine on yours, and the deploy gets blamed. The
    same goes for a client ringing up about the admin looking wrong.
  - `asset_version()` (a `@app.template_global`, beside `thumb_url` /
    `upload_url`) is the first eight hex of the file's sha256, cached
    per worker on `(mtime, size)` exactly like `aspect_ratio_of()`.
    CONTENT and not the mtime, because the token must also HOLD STILL
    between deploys or the 30 days buy nothing: a fresh clone, a rebuilt
    server, an rsync without `-t` or a stray `touch` all restamp files
    whose bytes never moved.
  - A missing file returns None, and Werkzeug drops a None query value —
    so a typo costs the cache busting on that one URL and never a 500.
  - Applies to the stylesheet in all four templates that own a `<head>`
    and to the three icons in `_icons.html`. Body images (the brand
    mark, the footer logo) and the `og:image` are deliberately left
    plain: they change about never, and a moving og:image URL churns the
    social scrapers' caches for nothing.
  - `tests/smoke_test_asset_version.py` holds both halves — the token
    changes when the file changes, and comes back to exactly its old
    value when the content does even with a fresh mtime — plus a source
    scan asserting every `</head>` template versions its stylesheet, so
    the next template to be added cannot quietly miss it.
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
  - The footer ALSO carries a Donate button (`.foot-donate`, `.btn
    .btn-red`), in the first column under the who-we-are text and gated
    on the same `donations` flag as the header pill — switch the flag
    off and both go, or the one left behind is a dead link on a page
    whose flag says there are no donations. It is there because on a
    phone the header pill is folded inside the menu button, so for most
    visitors the footer one is the only Donate actually on the page.
    It is a call to action and deliberately NOT another entry in the
    quick links.
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
  - **The open menu must always be able to reach every item**, and two
    separate things used to stop it on a short screen. Both are fixed;
    keep both.
    - It SCROLLS ITSELF (`max-height` + `overflow-y:auto` +
      `overscroll-behavior:contain`). The panel is absolutely positioned
      inside a sticky header, so it travels with the header and is not
      part of the page's scroll — anything past the bottom of the screen
      could not be reached at all, and scrolling the page moved it not
      one pixel. With all four groups expanded the panel is ~720px, so
      it fits a 900px-tall test window and neither a Galaxy S10 at
      360x740 (phone number lost) nor the same phone with its browser
      chrome showing at 360x640 (Contact us, Donate and the phone lost).
      The `max-height` is declared twice, `vh` then `dvh`: dvh is the
      viewport EXCLUDING the browser chrome, which is the case that
      breaks, and vh is what an older Samsung Internet understands.
    - The cookie notice is a FIXED strip at the bottom of the screen,
      190px tall on a phone, so it sat over the last few items — covered
      is as unreachable as off screen. `body.menu-open .cookie-notice
      {display:none}` lifts it while the menu is open, exactly as
      `body.lightbox-open` already did. It is not dismissed, only not
      painted; it returns when the menu closes.
  - The RIGHTMOST group's panel opens leftwards (`.nav-group-last`). The
    nav is right-aligned, so how far right that group sits depends on
    what else is in the row, and a panel is `visibility:hidden` rather
    than `display:none` — it counts toward the page's scrollWidth
    whether or not anybody has hovered it. With donations switched off,
    and so no Donate pill after it, that was 76px of sideways scroll at
    1024 and 900 on every page with no panel open. Any future group
    added to the end of the row takes the class with it.
  - `tests/check_header_layout.py` is the proof, at the shared
    `VIEWPORTS` from `tests/browser_view.py` — real screens, heights and
    all, including 900 and 768 either side of the 899px shed point,
    since that is where a header regression would land: one line (measured by item CENTRES, since a trigger is taller
    than the pill), no sideways scroll, panels shut until hovered or
    focused, every destination reachable by Tab alone, and the mobile
    menu listing all of them. It also runs two sections the six widths
    cannot cover, because neither is about width: `SHORT_SCREENS`
    (360x740 and 360x640) opens the menu and asserts every item can be
    scrolled to AND tapped — `elementFromPoint`, so anything painted
    over an item fails it too — ending with a real tap on Donate that
    must land on /donate; and a pass with the donations flag OFF,
    asserting no sideways scroll and that the last group's panel still
    opens inside the window. Run it after touching the header, the nav
    or the breakpoints.
- Video (YouTube/Vimeo), behind the `video` flag. Two rules, and both
  are the point of the feature rather than details of it:
  - **Nothing an admin types is stored or rendered as markup.** They
    paste a link; `parse_video_url()` pulls the provider and id out of
    it; what is stored is an address THIS CODE builds
    (`video_watch_url()`). Paste a whole `<iframe>` into the video box
    and the right video is saved with none of the markup. There is no
    path by which admin text becomes HTML on a page, and there must
    never be one — that is how a CMS becomes a way to run scripts on its
    own site.
  - **CLICK TO LOAD, and this is why:** the poster is fetched ONCE when
    the video is saved and stored in `static/uploads/` like any other
    image, so a visitor's browser contacts YouTube or Vimeo only if they
    press play. That keeps the cookie notice honest — the site really
    does set two first-party cookies and contact nobody — and it is what
    avoids needing a PECR consent flow, which the cookies section below
    is explicit would otherwise be required before any third-party
    script or cookie. **A hot-linked thumbnail or an eager iframe would
    break both**: it would make the notice's "no tracking of any kind"
    false and turn a one-line change into a consent-flow project. If
    somebody asks for the video to autoplay or the thumbnail to come
    straight from YouTube, that is the trade being proposed.
  - The player is `youtube-nocookie.com` and Vimeo with `dnt=1`, and
    those two hosts are the only `frame-src` addition. `img-src` was
    NOT widened and must not be: posters are our own files.
  - The poster fetch NEVER RAISES, on the same terms as `send_mail()`:
    the video saves first and a failed fetch falls back to the content's
    own photo, then to a plain play button. A charity's news post must
    not fail to save because YouTube was slow.
  - The field lives in the shared rich-content partial, so News, Events,
    Our Journey and About all get it from one place and one route
    (`admin_video_save`), exactly as the layout picker does. Campaigns
    are not rich owners and carry their own field.
  - **WHERE a video sits is a setting** (`VIDEO_POSITIONS`): at the top
    (`lead`, the default and what every video did before), after the
    text, or at the end after any photographs. Stored as
    `video_position` on each owner, and as the `about_video_position`
    Block for About — the same split `video_url` already uses. It
    reaches the macro on the `video` dict from `video_of()`.
    - An unrecognised value renders at the TOP, never blank
      (`clean_video_position`). A hand-edited row or a form from an
      older deploy must put the video where it has always been, not
      take it off the page.
    - **THREE FIXED POSITIONS, NOT AN ORDER.** If somebody wants two
      videos, or a video BETWEEN two photographs, this is the wrong
      shape and a fourth position is the wrong answer: the right one is
      to make a video another ordered attachment on `ContentImage`
      (a `kind` column plus the video fields, position becoming `sort`).
      That refactor touches every consumer of `images_for()` — the
      macro's lead/strip split, `interleave_content()`,
      `present_images()`, the lightbox links, `delete_images_for()` —
      each of which carries rules learned from bugs, and it needs a data
      migration that must not lose the video EBWA has. It was
      deliberately not done for a positioning question. **A request for
      a SECOND video is the trigger**; `video_position` is then an input
      to that migration rather than wasted work, since it records where
      each video was meant to go.
    - Only a LEAD video takes the classic preset's lead slot. Moved
      anywhere else the slot goes back to the first photograph — a
      video sent down the page must not leave a hole at the top of it.
    - **A PAGE THAT RENDERS `video_player()` ITSELF MUST HONOUR
      `video.position` ITSELF.** The macro does it for the rich-content
      owners; `collection_detail.html` does not use the macro, because
      campaigns are not rich-content owners — no ContentImage
      attachments, no layout preset, one image column and one video. It
      called `video_player()` directly and hardcoded video-then-image,
      so the setting saved and did nothing, on the one content type
      whose page is hand-built. The three places there are: before
      everything, after the description, and below the payment form —
      which on a page whose job is to take a payment is the "find out
      more" slot, and a genuinely different place from "after the
      words".
    - The position tests must assert on the RENDERED PAGE and on ORDER.
      The campaign checks used to assert `c.video_url == ...` on the
      model and, on the page, only that the player and the photograph
      were both PRESENT. Presence is not position, and a field that
      exists with an admin control that saves it looks covered.
    - **NO TEMPLATE ASSEMBLES THE DICT `video_of()` READS.** About has
      no row — its settings are Blocks — so something must build an
      object-shaped thing for it, and that something is `about_video()`
      in app.py, next to the accessors the admin writes through. It used
      to be built in about.html, which meant a template listed another
      module's field names: when `video_position` was added, every model
      got it and that one dict did not, so About saved the setting and
      ignored it. Nothing broke loudly, because an unknown position
      falls back to the lead slot — the safe default doing its job.
      `tests/smoke_test_video.py` now renders all three positions on
      About and asserts the player actually moves, plus a source check
      that about.html names no video fields.
  - **A video LEADS; it never DISPLACES.** It takes the lead slot in
    the macro, where the first image would sit, and every image moves
    down a place rather than being dropped — adding a video never costs
    a photograph its spot. The campaign page got this wrong at first
    (`{% elif camp.image %}`, so the video replaced the picture) and the
    photograph vanished from the page while staying on the card, which
    is exactly the sort of half-there that makes a client think the CMS
    ate their content.
  - The one exception is the picture that is doing duty as the video's
    POSTER — which happens when no still could be fetched from the
    provider. Showing it again underneath is the same photograph twice,
    so it is dropped from the strip. Both halves are pinned by tests.
  - CARD AND COVER CONTEXTS ALWAYS USE THE IMAGE, never the video
    poster: the listing card, the homepage strip and every other
    thumbnail are the item's identity, and a video changes what is ON
    the page, not what the page IS.
  - Pasting an embed code into a BODY field is refused with a message
    pointing at the video box (`body_embed_problem()`). It used to
    render as escaped source code on the live page, which is what
    somebody gets for doing the reasonable thing in a CMS that has no
    video field. Only `<iframe|script|embed|object|video|source>` as
    tags: "5 < 10" and "<3" are ordinary writing and go through.
- Accessibility: every page begins with a skip-to-content link
  (`.skip-link` → `#main`), in BOTH shells — `base.html` and
  `admin/base_admin.html`. Rules that are easy to undo by accident:
  - It is positioned off screen (`left:-9999px`), NOT `display:none`. A
    hidden element cannot take focus, so `display:none` would make the
    link unreachable by exactly the people it exists for.
  - The target carries `tabindex="-1"`. Without it the browser scrolls
    to `#main` but leaves FOCUS where it was, so the next Tab goes
    straight back into the nav that was just skipped — a skip link that
    looks right and does nothing.
  - `admin/login.html` and `admin/login_2fa.html` deliberately have
    none: they are standalone templates with no sidebar, and WCAG's
    bypass-blocks requirement is about repeated blocks of content.
  - `tests/check_accessibility.py` proves all three (first Tab stop,
    visible when focused, focus lands on `#main`) on a public page and
    on the dashboard.
- Colour and contrast, all of it decided once and none of it per element:
  - **`--red` (#E8333F) is the flag red and does not move.** It is
    4.21:1 on white — under AA for small text — so it is kept for the
    places that are not small white-on-red or small red-on-white: the
    eyebrow dot, focus rings, alert borders, the launch-blocker text.
  - **`--red-ink` (#C9202B, 5.64:1) is the red that carries text**, and
    it is the DEFAULT answer, not the exception: both Donate buttons,
    Subscribe, the admin Delete links, the album photo count. It clears
    AA at any size, so nothing has to grow to meet a bar.
    - Growing type to 18.7px bold was tried first, across all of these,
      and reverted. It changes the DESIGN to fix a COLOUR, and every
      element it touched came out louder than it was drawn to be —
      Delete became the loudest thing in every admin row, so the
      destructive action read as the primary one. Reach for the darker
      surface first; the size lever is for something that should be
      loud anyway.
    - **The brand mark is itself painted #C82028**, which is 0.77
      ΔE2000 from `--red-ink` and 8.46 from `--red`. So in the header,
      where the Donate pill sits beside the mark, the pill agrees with
      the logo now and used to be the odd one out. Against `--red` the
      difference is 8.1 ΔE2000 and almost all of it lightness (L* 51.7
      → 43.7), hue and chroma barely moving: the same red, darker, not
      a second colour. Measure before assuming a token is off-brand —
      this one is closer to the artwork than the variable named after
      it.
  - `--large-text` (18.7px) is WCAG's large-text threshold, and only
    means anything **with `font-weight:700` beside it**: 14pt bold or
    18pt regular. 18.7 and not 18.6667 because a rounding that lands
    below the line is a failure. **Exactly one thing uses it** — the
    dashboard's launch blocker, where being loud is the point of the
    sentence.
  - `--ink-muted` (#68746E, 4.87:1) is the quiet line under an admin
    figure. It replaced `--ink-soft` at `opacity:.8`, which computes to
    #6e7b75 and fails AA by 0.09. **Do not dim a token at the point of
    use to make a lighter colour**: the result is invisible to anyone
    reading the palette, nobody chose it, and opacity dims an element's
    TEXT AND ITS BACKGROUND TOGETHER — which is exactly how the footer
    Donate button ended up at 3.98:1, the worst contrast on the public
    site, from `.foot-grid a{opacity:.85}` reaching a button it was
    never meant for. Add a token.
- Heading levels: **every public page's title is an `<h1>`**, and `h1`
  and `h2` deliberately share the page-title size, so promoting a title
  changed the outline and nothing else. Section headings are `h2`, cards
  and entries `h3` — except on the three LISTING pages (events, news,
  collections), where the cards sit directly under the h1 and are `h2`.
  `.event-card h2,.event-card h3` styles both, because the same card is
  an h3 on the homepage (under a section h2) and an h2 on a listing.
  - The footer's four column headings are `h2`, not `h4`. As `h4` after
    an `h2` they skipped a level on 17 pages; they are the top-level
    headings of the contentinfo landmark, which is what an `h2` says.
  - `.hero h1`, `.admin-h1` and `.login-card h1` override the shared
    size; the login card also zeroes the margin, its shell being a grid
    with its own gap.
- `.visually-hidden` is read aloud and never painted — the clip-rect
  pattern, NOT `display:none` or `visibility:hidden`, both of which
  remove an element from the accessibility tree as well as the page,
  which is the opposite of the point. It names the actions column in
  every admin list, where a visible "Actions" would be furniture above a
  column of two links.
- **EVERY wide admin table lives in a `.table-scroll` box.** An admin
  table is wide by nature — a date, a title, a venue, a status and four
  actions — and without the box it drags the whole page sideways on a
  phone: measured at up to 663px past a 390px screen, on nineteen of the
  twenty admin tables. The box scrolls; the page does not.
  - It is `position:relative`, and that is load-bearing rather than
    tidiness. An absolutely positioned descendant is laid out against
    the nearest POSITIONED ancestor, so in a `position:static` box it is
    not clipped by the overflow — it escapes and pushes the document.
    The actions column's `.visually-hidden` "Actions" span is exactly
    that: 1px wide, invisible, sitting at the far right of a 635px
    table, dragging the page 111px past a 390px phone while the table
    itself sat clipped and innocent. Every admin list had it, and no
    amount of wrapping fixed it until the box became a containing block.
  - A box that holds **nothing focusable** also carries `tabindex="0"`
    plus `role="region"` and an `aria-label`: a div with `overflow-x`
    can be reached with a mouse, a trackpad and a finger and by nothing
    else, so its right-hand end is otherwise unreachable by keyboard.
    Seven have it — the dashboard's activity table, the two Settings
    status tables, the audit log, the contributor list and the two Gift
    Aid tables. The rest do NOT, deliberately: every row has a link or a
    button, so tabbing already scrolls the box and a stop of its own
    would be a redundant pause. Any new table needs the same judgement,
    and the focus ring (`.table-scroll:focus-visible`) either way.
- `tests/check_admin_widths.py` walks EVERY admin page at 390 and 360
  and asserts no sideways scroll. Nothing covered the admin at phone
  widths before it — `check_header_layout.py` tests `/admin/login` and
  stops there — which is how nineteen tables came to be doing it at
  once. Run it after touching an admin template or the admin CSS:
  `python tests/check_admin_widths.py [--shots DIR]`.
  - It **logs in ONCE and resizes**, never once per viewport: several
    logins in quick succession trip the `login` rate limiter, and a
    rate-limited context lands on the login page — where there is no
    table, nothing overflows, and the check passes while looking at the
    wrong page. That is not hypothetical; it is how an earlier version
    of this measurement reported two phone widths clean.
  - Every page is paired with a SELECTOR proving that page is on screen,
    asserted before anything is measured. "Nothing overflowed" and
    "there was nothing to overflow" are the same number and completely
    different facts. It earned its place immediately: /admin/faq and
    /admin/resources render their table only `{% if rows %}`, and
    `seed_demo` seeds neither, so both were being measured empty.
  - When something does overflow it names the widest offending element,
    **skipping anything inside a scroll container** — a table in a
    `.table-scroll` box is wider than the viewport by design, and
    reporting it named the innocent party while hiding the real one.
- A checkbox whose `<label>` wraps only an image and the input HAS NO
  ACCESSIBLE NAME. The gallery's bulk-move ticks were read as "checkbox,
  unchecked" once per photograph with nothing to tell them apart, which
  made the feature unusable non-visually. They carry an `aria-label`
  naming the caption, or — where there is none — the album and the
  upload date. A row number would be a name that names nothing, and the
  filename is a UUID.
- `tests/check_accessibility.py` runs axe-core over the public pages and
  the admin at 1280x800 and 390x740, and reports violations by severity
  with public and admin separated. Run it after any markup or colour
  change: `python tests/check_accessibility.py [--strict] [--json FILE]`.
  - It REPORTS and exits 0 by default; `--strict` turns it into a gate
    that fails on critical or serious violations on PUBLIC pages.
  - **Both areas are at ZERO violations, public and admin.** That is the
    baseline to defend: run it after any markup or colour change, and if
    something appears, it appeared in that change.
  - axe-core is fetched on demand into `tests/vendor/` (gitignored):
    half a megabyte of third-party minified JavaScript does not belong
    in a repository with no npm and no build step. It is injected with
    `page.evaluate`, which goes through CDP and so is not subject to the
    site's CSP — a `<script src>` would correctly have been blocked.
  - **It seeds its own fixtures and audits opened states, and both are
    load-bearing.** `seed_demo` leaves every image blank and seeds no
    album and no campaign, so an audit of it alone never sees a
    photograph, a gallery, a video player or a rich-content figure. The
    unlabelled bulk-move checkboxes on /admin/gallery — the one CRITICAL
    finding — appeared only once photos existed. Likewise axe sees the
    DOM as delivered: the mobile menu, a nav dropdown and the lightbox
    are opened and scanned, because a `role="dialog"` is where labelling
    goes wrong and the closed page is the half nobody has trouble with.
  - Every opened state is CONFIRMED to have opened (`confirm()`). A scan
    of a state that silently failed to open is a scan of the ordinary
    page, comes back clean, and is the most misleading result this file
    could produce. It caught one immediately: below 899px there is no
    dropdown to open, since the panels are `position:static` inside the
    menu.
  - Automated rules catch roughly a third of what matters. A clean run
    is a FLOOR, not a pass: reading order, whether alt text says the
    right thing, and whether the site can be operated with a screen
    reader are not answered by any of it.
- Copy/tone: British English. Public-facing text should read warmly and
  plainly — this is a community charity, not a SaaS product.
- Visitor statistics (`PageView`, `PageViewDaily`, `VisitorSalt`),
  super-admin only, on Settings. Counted on this server: **no analytics
  service, no third-party request, no extra cookie**, and the cookie and
  privacy notices are unaffected because nothing stored identifies
  anybody.
  - **WHY A SALTED DAILY HASH RATHER THAN AN IDENTIFIER.** To say "how
    many people" rather than "how many page loads" you must tell two
    loads by one person from two by different people. An IP is personal
    data under the UK GDPR; an IP plus a user agent is close to a
    fingerprint; a cookie would be a new cookie the notice does not
    mention and PECR would want consent for. So:
    `sha256(salt_for_today + ip + user_agent)`. The IP and the user
    agent are used in the request and **never written anywhere**.
    - **The salt is random and is REPLACED every day**, the old one
      overwritten rather than kept. That is what stops the counts being
      joined across days: the same visitor gets an unrelated hash
      tomorrow, and yesterday's hashes cannot be recomputed even with
      the database in hand. A salt DERIVED from the date would be
      reproducible for ever, which is the whole failure this avoids.
    - Said plainly rather than glossed: while today's salt exists,
      somebody holding the database and a specific IP and user agent
      could test whether that combination visited TODAY. That is
      inherent in counting same-day uniqueness at all, and it ends at
      the next rotation.
    - It is one row shared through the database, not per worker:
      gunicorn runs several, and a per-worker salt would count one
      visitor several times.
  - **A "visit" is one person on ONE DAY**, and the panel says so. A
    returning visitor is counted again next week, deliberately — so a
    month's figure is person-days, not different people. Only the
    "today" figure is people. Saying the first half without the second
    would be a half-truth on a page of numbers somebody will quote.
  - Recorded in `teardown_request`, ONE insert, and **it never raises**
    — same rule as `send_mail()`: a statistics table must not turn a
    visit into a 500. `after_request` only stashes the status code,
    which teardown cannot see.
  - Excluded: admin, `/static`, `/healthz`, the sitemap and robots,
    non-GET, non-200, obvious bots by user-agent substring, and
    **anybody signed in** — staff looking at their own site are not an
    audience, and the Settings page must not count itself.
  - How long per-page rows live is a SUPER-ADMIN SETTING,
    `pageview_raw_days()` — 30 to 365 days, `PAGEVIEW_RAW_DAYS` (62) the
    default and the fallback for anything out of range or unparseable;
    `aggregate-pageviews` then folds a day into `PageViewDaily` and
    deletes it, from cron. Both tables are queried and added for any
    range, or the figures would fall off a cliff at the boundary. The
    daily totals hold no paths: per-page figures are only ever shown for
    the current month.
    - It governs the ROLL-UP only, never the totals. Shortening it
      prunes more on the next run and changes no figure — the rows it
      removes were counted into `PageViewDaily` first — it only shortens
      how far back "most visited pages" can look. Measured at 460 bytes
      a raw row including its three indexes: at 200 views a day, 2.6MB
      of database at 30 days, 5.4MB at 62 and 32MB at 365.
    - The field's step is 1 with `min` 30, so the 62-day default is on
      the grid. See the number-input rules above for why that sentence
      is not pedantry.
  - **`PageViewDaily` IS KEPT FOR EVER. Never add a prune to it, and
    never give it a setting either.** A range control on the raw window
    is a tidiness knob; the same control over the totals is a button
    that deletes the year-on-year history, and it would be used by
    accident by somebody trying to save space. The helper text beside
    the raw setting says so in as many words, so the absence reads as a
    decision rather than an oversight. The
    raw rows are the disposable half; the daily totals are the point of
    keeping anything, and the year-on-year figure is the one number that
    cannot be recovered once it is gone — the rows it would be
    recomputed from were deleted two months after they were written.
    Measured: five years is about 350KB, 196 bytes a day including the
    unique index, against 6.5MB for the raw table's own 62-day window at
    200 views a day. The permanent history is about five per cent of the
    working set.
    - The only `.delete()` in the whole stats path is the raw one inside
      `aggregate_page_views()`, and `tests/smoke_test_stats.py` pins it:
      five years of totals, aggregation run four times, all fifteen rows
      still there.
    - "Same month last year" reads from `PageViewDaily` and nowhere
      else, because there are no raw rows that old by definition. The
      test seeds a year-ago month as totals with NO raw rows and asserts
      the card is populated — and that with no history it is absent
      rather than a row of zeros.
  - **The figures are EBWA's, so /admin/visitors is open to EVERY
    admin.** The Settings panel is super-admin only because of what sits
    BESIDE it — the mail server, the NAS password, the state of the
    machine — not because visitor numbers are Netbus's business. A
    charity that cannot see how many people read its own website is
    being counted at rather than for.
    - **Its own page, not the dashboard**, and a card on the dashboard
      linking to it — the pattern every other module follows. The
      dashboard's job is "what needs me today"; a thirty-day chart would
      be the largest thing on it and would compete with the one red card
      that is meant to stand out. The test asserts the card is there and
      the chart is not.
    - `admin/_visitor_summary.html` is ONE partial rendered on both
      pages. Two copies of a chart is two places to fix the next thing
      found in one of them.
  - The monthly report (`send-monthly-report`, from cron DAILY — a
    machine off on the first would otherwise skip the month) is
    **idempotent through the AUDIT LOG**, not a flag: the log already
    records every send, it cannot drift from what happened, and cron may
    run twice without a second email. `--force` overrides it, for
    checking the address.
    - Seeded OFF with no recipient. A site that starts emailing a board
      the moment it is deployed is a site nobody asked for.
    - **The wording is part of the feature.** A board reads it and the
      number gets quoted afterwards without whatever caveat sat three
      paragraphs away — so "a visit is one person on one day" goes on
      the line UNDER the figure it qualifies, and the test asserts the
      distance between them. Where there is no comparison to make it
      says so in words rather than printing a zero.
    - The address is never in the audit summary. Which field changed is
      enough; somebody's email is not.
  - The period report (`/admin/visitors/report`, `period_report()`) is a
    DOCUMENT, and every rule about it follows from that: it goes into
    grant applications, where it is read a year later by somebody with no
    access to the admin. So it carries EBWA's name and charity number,
    the period, the figures, the person-days caveat beside the figure it
    qualifies, the comparison, and the date it was produced.
    - **Print-friendly HTML, not a generated PDF.** reportlab is not in
      `requirements.txt` and would be a server install plus hand-placed
      text; the browser's own "Save as PDF" gives selectable text, the
      site's real fonts (self-hosted, so a print fetches nothing) and a
      page that is readable on screen and by a screen reader first. If a
      real PDF is ever wanted, that is the trade being proposed.
    - `@media print` hides everything that is the ADMIN — sidebar, the
      period picker, flashes, the skip link — and the on-screen note
      about a missing charity number, which must never print: on a
      funding application it reads as the charity not knowing its own
      number.
    - `tests/check_visitor_report.py` is the proof, and it has to be a
      browser check: nothing in the HTML says what comes out of the
      printer. It emulates print media at every shared viewport and
      asserts what is gone, what is left, that no link dragged its URL
      into a sentence, and that the big figures print in dark ink rather
      than the green they are on screen.
    - Open to EVERY admin, like the visitor summary.
  - **The public stylesheet styles BARE `header`, `footer` and
    `section`**, and those rules reach any admin page using one of them
    semantically. `.admin-body :where(header,footer,section)` neutralises
    them, deliberately placed BEFORE the admin rules so anything after it
    still wins (`.admin-attention` keeps its own box). Found the hard
    way: the report's masthead rendered sticky and blurred, its colophon
    as a band of dark green with grey text at 1.79:1, and every section
    carried the public bands' 80px of padding — 160px of white between
    two short paragraphs on the printed page. Reach for a semantic
    element on an admin page and check what the public shell already
    says about it.
  - The chart is **inline SVG built in the template** — no charting
    library, no client-side fetch, nothing new for the CSP. It carries
    an `aria-label` listing every day and figure, because a bar chart is
    an image to a screen reader.
  - The table grows faster than anything else here, so every query is an
    index seek: `(day)`, `(day, visitor)` for the COUNT(DISTINCT), and
    `(day, path)` for the most-visited list.
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
   database, names any missing tables, columns OR INDEXES, suggests the
   `ALTER TABLE` / `CREATE INDEX` for each, and exits 1 if anything is
   missing. Run it at
   the end of every deploy, BEFORE the restart — it turns a missed step
   into a failed check instead of a 500 for every visitor. It is
   read-only. Leftover tables from retired modules are reported but
   never fail it, since this project never drops anything.

## Testing

**A TEST THAT PATCHES THE THING IT IS TESTING PROVES NOTHING.** The
monthly report's tests replaced `send_mail` with a fake that accepted
anything, then asserted the ARGUMENTS handed to it — so a report passing
a list to a function that called `.strip()` on it passed every check
while raising on every real send. Patch at the BOUNDARY instead: the
mail tests fake `smtplib.SMTP` and assert on the `EmailMessage` that
would have gone down the wire, so the recipient handling, the header and
the body are all real code. The same gap let the video position bug
through, and it has one shape: asserting the value rather than the
behaviour.

Smoke-test with Flask's test client (see README history): assert status
codes for every new route, auth redirects (302) for anonymous access to
admin, form create/edit/delete round-trips, and that unpublished content
is absent from public pages and 404s by direct URL. Smoke tests live in
tests/; run them against a scratchpad DATABASE_URL, never
instance/ebwa.db.

Layout changes also have `tests/check_header_layout.py` — a Playwright
run against real Chromium at the shared `VIEWPORTS` (1440x900, 1280x800,
1024x768, 900x700, 768x1024, 390x740, 360x640) asserting the nav stays
on one line, nothing scrolls sideways, and the mobile menu opens and can
be used to its last item. 900 and 768 straddle the 899px shed point
deliberately, and the two phone heights are what is left of those phones
once the browser's own chrome is showing; keep all of them if you change
the list, and add a height with any width.
It needs a browser so it is NOT part of the smoke suite (`smoke_test_*`);
run it by hand after touching the header, nav or breakpoints:
`python tests/check_header_layout.py [--shots DIR]`.

`tests/check_number_inputs.py` is the third: it opens every page with a
number field, presses the arrows and measures the increment, types
off-step and out-of-range values to see what the browser would refuse,
and asserts each `min`/`max` against the range its route enforces. Run
it after adding or changing any number input:
`python tests/check_number_inputs.py`.

`tests/check_marquees.py` is the other behavioural one, for the two
scrolling rows — the half of it that only a browser can answer, the
markup half staying in `tests/smoke_test_partners.py`. It drives the row
at every shared viewport and at 5/7/9 partners (4/6/8 quotes) and asserts the loop closes with
NO GAP (measured as a reader sees it — cards clipped to the visible
strip, widest empty run found — at rest and after each of a drag, a
wheel and a Tab, the three things that used to open one), that a step is
exactly one card plus one gap and then rests, the pause on hover and
focus, drag-to-scroll not swallowing the click while a sub-threshold
wobble still opens the partner, the arrows appearing only for a still
row with somewhere to go, a touch swipe still panning, and every mode
falling back to still under reduced motion.
Its last section (H) is the FAIL-SAFE one, at 360 and 412 — the CSS
widths of the two phones the duplicate-cards bug was reported across. It
breaks the script four different ways (JavaScript disabled entirely, an
exception on the way in, reduced motion, and an offset that refuses to
move) and asserts the same three things every time: one set rendered,
five cards not ten, arrows showing. It also drives a phone-width row
with no `Element.scrollBy` at all, and checks the two ways of moving a
row that has no scrollbar — arrows and a swipe — at 360.
It runs the WHOLE file once per row: `--row partners` or `--row
testimonials`, and with neither it runs itself for both in turn, each
with its own database, server and exit code. Every selector in it is
scoped by `ROW_SEL` and every JS snippet reads `window.__row`, so the
same checks drive whichever row is named. Two things it knows about the
difference between them: a quote card holds no link, so the "a wobble
still opens the card" check is partner-only and the quote row asserts
the row does not shift instead; and a drag has to travel MORE THAN HALF
A STRIDE or scroll-snapping pulls it back, so the drag distance is taken
from the row's own card rather than being the 180px that suited a 260px
partner card.
Run it after touching either row, the shared script or `MOTION_ROWS`:
`python tests/check_marquees.py [--row NAME] [--shots DIR]`. It also reports
the step wrap's headroom (see the partner rules above) as a WARNING
rather than a failure, and prints the tightest margin it saw — headroom
running out makes the row untidy, not broken, and a check that failed
for it would cry wolf while one that said nothing would let it drift to
nothing unnoticed. Three things it
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

Every browser check runs at a REAL SCREEN SIZE — a (width, height) pair
from `tests/browser_view.py`, never a width with whatever height the
harness felt like. **Height matters as much as width for anything that
can grow vertically**, and that is not a small category: an open menu, a
modal, the lightbox, a long form, a page of stacked cards, any fixed
strip anchored to the bottom of the screen. Widths decide what wraps and
what sheds; heights decide what falls off the bottom, and only one of
those was ever being tested.

  - The cost of not doing it, which is why this is a rule: every check
    ran 900px tall, because that was the default in `browser_motion` and
    no caller ever passed one. The open mobile menu is ~720px with its
    groups expanded — comfortable at 900, and taller than any phone. It
    ran off the bottom of a Galaxy S10 with Donate on the lost part, and
    NO existing check could have caught it at any width. That is a
    structural blind spot, not a missed assertion.
  - `height` has NO DEFAULT in `new_page()` / `new_context()`, so a
    check cannot inherit one by accident, and `height_for(width)` raises
    on a width nobody has recorded a screen for rather than guessing.
    Add the device to `VIEWPORTS` (or `EXTRA_HEIGHTS`) and name it.
  - `VIEWPORTS` is the shared list — 1440x900, 1280x800, 1024x768,
    900x700, 768x1024, 390x740, 360x640 — each a real laptop, tablet or
    phone, the phone heights being what is left once the browser's own
    chrome is showing. `PHONES` is the subset for things that grow
    downwards, and includes the S10 at BOTH its heights (740 and 640),
    where the menu overflowed by 58px and 158px respectively.

**"Can a person reach this?" is asked with `elementFromPoint`, never by
comparing a rectangle to the viewport** — `unreachable()` in
`tests/browser_view.py`. A rectangle sitting inside the viewport says
nothing about whether anything is painted over it, and the site has a
FIXED strip at the bottom of every first visit: the cookie notice, 190px
tall on a phone. It sat over the last items of the open menu, Donate
among them, and every tap landed on the notice. Donate measured
top 638, bottom 673 in a 740px viewport — a bounds assertion passes that
happily, and the person cannot tap it.

  - Three things the helper knows, each learned by getting it wrong:
    `pointer-events:none` is SKIPPED rather than failed (the three nav
    group triggers decline taps deliberately); it scrolls to the element
    first, since reaching something by scrolling its panel to it counts;
    and it tests the centre of a LINE BOX rather than of the union
    rectangle, because a link wrapped onto two lines has a rect whose
    centre is in the whitespace beside the text and reports itself
    covered by its own paragraph.
  - Keep plain rectangle assertions for what they are actually about —
    no sideways scroll, a panel fitting the window, a column's width.
    Reachability is the one that needs the browser's own answer.
  - A reachability check that cannot fail is worth nothing, so prove it:
    undo the fix, watch the check name what is covering what, put it
    back. Both the menu and the cookie notice were confirmed that way.

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
  `tests/check_marquees.py`, whose drift, stepping and
  arrow-swapping are the behaviour under test. Inheriting the still
  default there would pass while measuring a row that never moved, and
  under reduced motion the script never adds `.is-moving`, so the
  duplicate `.partner-set` stays hidden and snapping stays on, and the
  thing being asserted is not even on the page. That file mixes both on purpose: MOVING for
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
- **A collection's STATE is three-way (`CAMPAIGN_STATES`), not a
  boolean**, because being on the website and taking money are different
  questions. `active` answered both with one tick, so closing a finished
  trip also deleted it from the site — the day out that ran, that people
  paid for and that a funder may ask about was simply gone, and showing
  it again meant reopening the payment form.
  - `open` on the site taking payments • `closed` on the site as a
    record, with its final total, its contributor count and a line
    saying it has finished • `hidden` off the public site entirely.
    `PUBLIC_CAMPAIGN_STATES` is the pair a visitor can reach, in the
    order /collections renders its two sections.
  - **A CLOSED COLLECTION REFUSES A PAYMENT IN THE ROUTE**, not only by
    hiding the form. Hiding a form is a courtesy to the person reading
    the page; a stale tab, a back button and anything posting at the
    endpoint all still arrive. Taking money for a trip that has already
    happened must not rest on a template `{% if %}`.
  - Filter on the state you MEAN. `closed` is not "not open": the
    Completed section asks for `state == "closed"`, so a hidden
    collection cannot reappear there — which is what `active == False`
    would have become the moment there were three states.
  - Contributor lists, CSV exports and Gift Aid records are available in
    the admin for EVERY state, including hidden. The treasurer needs
    them AFTER a collection ends, which is exactly when it is no longer
    open. Nothing in those queries may filter on state.
  - `active` is LEGACY and nothing reads it. The admin form keeps it in
    step in the one place state is written, so a database opened by hand
    does not contradict the website. Do not put it back in a query.
  - The state control is a `<select>`, which is why the publish-checkbox
    rule above does not apply to campaigns — a select always posts
    something. It has its own trap instead: the selected option must be
    the row's CURRENT state, or editing a closed collection reopens it
    for payment. An unrecognised value falls back to what the row has,
    never to a default, so a hand-made POST cannot publish a hidden one.
  - **NOTHING STOPS A PAYMENT WHEN A TARGET IS REACHED, and that is
    deliberate.** `target_pence` drives the progress bar and nothing
    else; `target_percent` clamps to 100 for display only. For a
    donation, going past the target is a good day. For a trip with a
    fixed number of seats it is not — but a target is an AMOUNT OF
    MONEY and seats are a COUNT OF PLACES, and the two only coincide
    when every payer takes exactly one place at the full fee, which the
    optional extra donation already breaks. Capacity needs its own
    field and its own count of completed fee-paying payments; it is
    Phase 2. Until then, closing a collection is how a trip that has
    filled up stops selling seats, which is now something an admin can
    do without deleting the page.
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
  - **Each Our Journey entry is a BOUNDED CARD**, and that is the fix
    for a real ambiguity rather than decoration. The separator used to
    be a 1px rule, with 26px above a title and 4px below the last thing
    in an entry — so a trailing photograph sat 31px above the NEXT
    entry's funder line and 31px below its own text, and on any preset
    that puts images last it read as belonging to the wrong milestone.
    Everything inside the box is that milestone. Do not go back to a
    rule between entries.
  - The entry-scale image sizes are 400px (classic lead), 380px
    (alternating media), 240px minimum (gallery grid) and 190px minimum
    (classic strip) — up from 340/320/180/116. The constraint they are
    under is not "small", it is **must not read as a page-width band**:
    400px is 38% of the width inside a card at 1440, which is plainly
    part of an entry. The strip was the worst of it at 124px, an icon
    rather than a photograph.
    - On phones the card gives its inset back (14px, not 28px). At 28px
      a side it took 56px out of a 342px column and the photographs came
      out SMALLER than before the card existed, which is the opposite of
      the point.
  - **The photo viewer is ONE implementation, shared** —
    `templates/_lightbox.html` (markup) and `_lightbox_script.html`
    (behaviour), included by the gallery album pages and Our Journey.
    It keys on `.js-lightbox`, so any container of photo links opts in,
    and it collects links across the WHOLE page: on Our Journey that
    means Previous and Next walk every milestone's photographs in
    document order, which is what a reader expects. A second copy would
    be a second place to fix the next thing found in it.
    - Every photo must be a real `<a href>` to the full-size file. The
      script UPGRADES those links, so with JavaScript gone the click
      still opens the photograph. That is why `_figure()` takes `link`
      and why it is a link and not a button.
    - Only Our Journey passes `link=true`. The pages that show one
      photograph at a time have nothing to page through.
  - Our Journey is the one page that renders MANY owners at once, so it
    uses `rich_content_for_many()` (one query for every milestone's
    images) rather than `rich_content_for()` per row, and each entry is
    rendered by the same macro inside a `.journey-entry` frame. The
    presets are scaled down there to an image treatment for one entry —
    see the Our Journey block in the stylesheet for why. A future page
    that lists rich owners should copy this, not add its own queries.
- **`Campaign.image` does two jobs**, and `show_image_on_page` governs
  only one of them: it is the COVER on the collections listing and the
  homepage strip, and a picture on the collection's own page. Unticking
  the box keeps the cover and takes the picture off the detail page —
  useful when that page has a video or photographs of its own. The cover
  is not optional and no setting may make it so; a collection with no
  card image is a hole in the listing.
  - The photo doing duty as the video's POSTER is still never shown
    twice, whatever the box says. Two independent reasons to leave it
    off the page, and both have to be checked.
  - `image_position` puts it in the same three places as the video
    (`IMAGE_POSITIONS` — the same keys, wording that suits a
    photograph). It is hand-threaded into `collection_detail.html`, not
    inherited from the rich-content macro, because campaigns do not use
    that macro; there is nothing to reuse and nothing that comes free.
    - Position is stored even while `show_image_on_page` is unticked, so
      ticking the box back on puts the picture where it was rather than
      at the top.
    - **BOTH IN THE SAME SLOT: THE VIDEO GOES FIRST** (`MEDIA_ORDER`).
      Decided rather than left to template accident — it is what the top
      of this page has always done, so an admin who puts both in one
      place gets the order they already know, and the video is the thing
      somebody came to watch with the photograph reading as a still
      beneath it. One `media_at()` macro renders all three slots, so the
      two can never come out in different orders in different places.
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
  - Enough partners becomes a scroller, fewer stay the static
    `.partner-grid`. **The scroller is SHARED with the testimonial
    row** — one `.marquee` implementation, one `setupMarquee()` run over
    every `.marquee-row` on the page, one stylesheet block. A second
    copy for the second row would be a second place to fix the next
    thing found in it. What differs per row is the CARD
    (`.partner-card`, `.quote-card`) and the threshold
    (`ROW_SCROLLER_MIN`: five logos, four quotes, a quote card being far
    wider). Rules for it:
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
    - **The copy set is HIDDEN BY DEFAULT and revealed only by
      `.is-moving`** — never the other way round. Hiding it used to
      depend on the admin's `none` setting or a `prefers-reduced-motion`
      match, which left every OTHER way of not animating showing the
      same five logos twice: the script blocked, erroring or not run at
      all, or an engine too old to support the media feature. Reported
      from a Galaxy S10 rendering ten cards where a Note 10 rendered
      five. The default state — no JavaScript, no knowledge of what the
      browser supports — must be the real partners and nothing else.
    - `.is-moving` therefore has to MEAN it, because it is what puts the
      copies on screen. Every attempted move reports whether the offset
      actually changed (`partnerMoved()`), and a run of attempts that
      changed nothing calls `partnerStill()`: class off, copies gone,
      arrows back. Anything new that moves the row reports through the
      same pair.
    - Move the row through `partnerScrollBy()`, never `scrollBy()`
      directly. `Element.scrollBy` is Chromium 61 and later — Samsung
      Internet 7 and earlier do not have it — and stepping and the
      arrows were the only two things that used it, so on such a phone
      a stepping row wore `.is-moving` and never moved. The helper falls
      back to the same `scrollLeft` everything else here uses.
    - Cards snap to their START, not their centre, so an arrow press
      moves exactly one card and the row rests with a card against the
      left edge.
    - The logos are deliberately NOT `loading="lazy"`. They are 200x100
      thumbnails that the row brings into view constantly, so lazy
      loading buys nothing and risks a blank tile arriving mid-scroll.
    - How it moves is a SITE setting, not a per-card one:
      `ROW_MOTIONS` (scroll / step / none) plus an interval, all Blocks,
      all in `HIDDEN_BLOCK_KEYS`. The mode reaches the page as
      `data-motion` on the `.marquee-row`, never as Jinja inside the
      script.
    - **Each row has its OWN settings** (`MOTION_ROWS`), saved and reset
      by one shared pair of helpers (`save_row_motion()`,
      `reset_row_speeds()`) behind two thin routes each. Separate and
      not shared because the rows are READ differently: a logo is
      glanced at, a testimonial is somebody's words that a visitor has
      to read, and moving text is harder to read than a moving logo. One
      setting would force a compromise on whichever row lost. Only the
      DEFAULT MODE differs — testimonials ship `none`, a still row with
      arrows — and the bounds and speed defaults are shared constants so
      nothing is decided twice. Adding a third row means an entry in
      `MOTION_ROWS`, a settings form and two thin routes; nothing else.
    - HOW FAST it moves is two more Blocks, `PARTNER_SPEEDS`:
      `partners_step_glide_ms` (how long ONE step takes) and
      `partners_drift_speed` (the continuous drift, in PIXELS A SECOND).
      Same rules — validated in the route, clamped again in
      `partner_motion()`, hidden from the content editor, and reaching
      the page as data attributes.
      - They live behind a collapsed `<details class="admin-advanced">`
        with a plain warning, because they are the settings most likely
        to make the row worse and least likely to be what somebody came
        for. Each field shows the default it is departing from, and the
        **Reset speeds to defaults** button beside them restores the
        CONSTANTS — never the last saved values, or it would only take
        somebody back to their previous experiment. The reset is
        audit-logged even when it changes nothing.
      - Pixels a second, not a lap time: a lap gets longer with every
        partner added, so the same lap time would be a different speed
        on every site. The admin page translates it instead ("about one
        partner card every six seconds").
      - **A step glides on our own clock** (`partnerGlide`), not on
        `behavior: 'smooth'`. The browser's own takes however long that
        engine likes — 253ms of visible movement in Chromium, measured;
        unknowable elsewhere — which cannot be offered as a setting. The
        360ms default is that measurement rounded to what the same probe
        read end to end; it measures 303ms by the same detector, so a
        step is imperceptibly slower than it was and identical across
        engines. 300ms is the closest to the old behaviour if that ever
        matters more.
      - A glide longer than the interval would start the next move
        before finishing the last: refused by the form, capped again in
        `partner_motion()` for a hand-edited row, and guarded a third
        time in the script (`glideFrame`), because a timer and an
        animation are two clocks and the cost of them disagreeing is the
        row moving two ways at once.
      - Anything the VISITOR does — a drag, an arrow — cancels a glide
        in progress (`partnerCancelGlide`). One offset, one mover.
    - **A row AT REST shows whole cards only.** Step mode and the
      no-movement setting are both rest states, and a stopped row with a
      sliced card at its right edge looks like a mistake rather than a
      design. `fitWholeCards()` counts how many whole cards fit at the
      current width and GROWS them to fill it, between
      `--marquee-card-base` (what a card is designed to be, and what it
      stays with no JavaScript) and `--marquee-card-max`.
      - Growing rather than only centring: five 260px partner cards fit
        three across a 1076px strip and leave 260px over, which centred
        is 130px of nothing either side — a row that reads as inset
        rather than full. Grown, the three are 347px and it fills.
      - **The max is a preference, not a rule.** Leftover wider than one
        gap is not empty space: the next card starts a gap after the
        last visible one, so more than a gap of slack shows a slice of
        it — and, split evenly, a slice of the previous card on the left
        too. Measured at 768: one 540px quote card in a 616px strip left
        76px and put 20px of the next card on screen. Where the cap
        would leave more than a gap it gives way and the cards fill.
      - A DRIFTING row is excluded deliberately: a part-card at the edge
        is what tells the eye the row is moving, and there is no rest
        position to tidy.
      - **Order matters**: the arrows are ~104px of the strip, so fit
        AFTER deciding whether they are shown, or the row settles that
        much short with a sliver showing. `relayout()` is
        refreshArrows → fit → refreshArrows for exactly that reason.
      - It also makes the step wrap structural: with n whole cards
        visible out of N, the room past the first set is exactly
        (N - n) strides, so it cannot go negative while any card is off
        screen. The margins below are what that used to be measured as.
    - **The card width and the gap are load-bearing for the step
      wrap**, not just a look. Stepping wraps back by a set only once
      `scrollLeft` has gone PAST one, and it gets there one stride — a
      card plus a gap — at a time, so the row needs a whole stride of
      room beyond the end of the first set (`setWidth` minus the visible
      width, against one stride). Five partners on the widest viewport
      is the tightest case: 300px of room against a 278px stride, 22px
      spare. Widen `.partner-marquee .partner-card` (260px) or
      `--partner-gap` (18px) and that margin goes first — past it the
      browser clamps the last step before the wrap into a stub, and the
      row still loops and still shows no gap, it just arrives untidily.
      `tests/check_marquees.py` measures it at every width and
      count and WARNS with the numbers rather than failing, since a
      stub step is untidy and not broken; it prints the tightest margin
      it saw at the end of every run. Watch that number if either value
      changes.
    - **Every mode falls back to a still row under
      `prefers-reduced-motion`**, and that is not a fourth option for an
      admin to override. The script checks `matchMedia` before it starts
      anything, and the stylesheet overrides the `.is-moving` reveal as
      well — keep both. The reduced-motion rule is no longer what hides
      the copies (they are hidden by default now); it is the guarantee
      that no sequence of classes can put one on screen in an engine
      that does support the feature.
    - **The arrows are the affordance for a row that is standing still**
      (the `none` setting, anybody with reduced motion, and any browser
      that never gets the row moving). They ship VISIBLE in the markup
      and the script hides them when it takes the row over — while it is
      drifting or stepping, or when there is nowhere to scroll to. That
      is the way round it has to be: rendering them `hidden` for the
      script to reveal made the one state most in need of them the state
      without them. The scrollbar-hiding rules still hang off the
      classes the script sets (`has-arrows` / `is-moving`), so with no
      JavaScript the scrollbar is there to scroll with and the buttons
      are the hint that there is more. They move one card, disable at
      each end, and carry real labels ("Previous partners" / "Next
      partners"). On a phone they sit UNDER the row: beside it they took
      84px of a 342px window and no card was ever fully in view.
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

`tests/check_video.py` proves the claim that matters about video, which
cannot be read off the HTML: it records EVERY request the page makes and
asserts that none reaches YouTube, Vimeo or their CDNs before the play
button is pressed — a hot-linked poster, a preconnect or a stray script
would all be invisible in the markup. It also checks the 16:9 box, no
sideways scroll and a fair tap target at every viewport.

Each module has a smoke test in tests/ (smoke_test_<module>.py, run
directly with python); seed_demo.py fills a fresh db with demo content.
Two are not modules: `smoke_test_asset_version.py` holds the
static-asset cache busting, and `smoke_test_ordering.py` holds the
order every list comes out in, ties included.
Phase 1 is code-complete. THE DEMO VPS (demo.netbus.co.uk) IS RUNNING
IT; production has never been deployed and has no host yet. Which
entries are confirmed applied on the demo, and which could not be
checked, is recorded at the top of DEPLOY.md — that file is the record,
not this paragraph. Production still needs init-db (new tables), pip
install (stripe), Stripe env vars + webhook registration (see README).

Phase 2 (separately quoted — DO NOT build under Phase 1): Board
Transparency Hub (board-member tier as a third `User.role` value — the
column already exists, private minutes stored outside static/ and served
via authenticated route, public AGM minutes),
Bengali page translations (Bengali twin values for Blocks + small chrome
translation dict, EN | বাংলা toggle), booking system (not specced — do
not build).
