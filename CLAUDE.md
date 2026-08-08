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
- Image uploads: always use the existing `save_upload()` /
  `delete_upload()` helpers (extension whitelist, UUID rename, 8 MB cap).
  When replacing an image, delete the old file after a successful save.
- Editable page text/images live in the `Block` model. To add one, append
  to `DEFAULT_BLOCKS` (group, key, label, kind, default) — `init-db` is
  idempotent and inserts only missing keys. Read in routes with
  `blocks_for(group)`, render with `c.get('key','')`.
- Admin forms: plain POST + redirect + `flash(msg, "ok"|"error")`.
  Destructive actions are POST forms with a JS `confirm()`. No AJAX.
- Auth: single `User` model via Flask-Login, `@login_required` on every
  admin route. Tiers live in `User.role` (see `ROLES`) — do not create a
  second user table. A future board-member tier is another value here.
  - `admin` (default) — EBWA's own admins. Everything they need to run
    the site.
  - `super_admin` — **Netbus only**, never a client login. Adds the
    Settings page (feature flags) and nothing else so far. Gate those
    routes with `@super_admin_required` (anonymous → login redirect,
    client admin → 403) and hide the nav link behind
    `{% if current_user.is_super_admin %}`. Promote with
    `flask --app app promote-super-admin`.
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
- CSS: extend `static/css/style.css` using the existing custom properties
  (`--green`, `--red`, `--paper`, etc.) and class naming style. Design
  identity: Bangladeshi flag bottle green + red circle motif, Bengali
  script accents in section eyebrows (`.eyebrow .bn`).
- Copy/tone: British English. Public-facing text should read warmly and
  plainly — this is a community charity, not a SaaS product.

## Database changes

No migration tool. Schema changes are additive:

1. Add the model/column in `app.py`.
2. New TABLES: `flask --app app init-db` (runs `create_all`; only creates
   what's missing, never drops or alters).
3. New COLUMNS on existing tables: provide the manual
   `ALTER TABLE ... ADD COLUMN ...` statement in the change notes, to be
   run with `sqlite3 instance/ebwa.db` before restart.
4. Never write code that drops tables or deletes data.

## Testing

Smoke-test with Flask's test client (see README history): assert status
codes for every new route, auth redirects (302) for anonymous access to
admin, form create/edit/delete round-trips, and that unpublished content
is absent from public pages and 404s by direct URL. Smoke tests live in
tests/; run them against a scratchpad DATABASE_URL, never
instance/ebwa.db.

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
