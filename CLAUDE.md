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
  the menu button at 899px — so a new nav item means re-checking those
  steps (and probably a shorter label) rather than letting the row grow.
  Header height is `--header-h`; the open mobile menu is positioned from
  it, so change the variable, not both.
  - The row is full at 1280px. It carries the nav and exactly ONE
    primary action, the Donate pill (`.nav-donate`, red, flag-gated on
    `donations`). The phone number was moved out to make room and now
    lives in the mobile menu, the footer and /contact. Anything new
    competing for that row has to displace something — run
    `tests/check_header_layout.py` before assuming it fits.
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
run against real Chromium at 1440/1280/1024/768/390px asserting the nav
stays on one line, nothing scrolls sideways, and the mobile menu opens.
It needs a browser so it is NOT part of the smoke suite (`smoke_test_*`);
run it by hand after touching the header, nav or breakpoints:
`python tests/check_header_layout.py [--shots DIR]`.

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
  `FeatureFlag` table seeded from `FEATURES` (news, resources,
  our_journey, membership_form, donations); super-admin-only Settings
  page at /admin/features with per-feature on/off toggles. Deploy:
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
