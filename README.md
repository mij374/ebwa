# EBWA Community Website + CMS

Flask + SQLite site for the Enfield Bangladesh Welfare Association.
Public site with editable content, plus an admin area for managing
page text/images, events (each with its own page) and a photo gallery.

## Features

- **Public pages:** Home, About, Events (list + auto-generated detail pages), Gallery, Contact (with map)
- **Admin area** (`/admin`, login required):
  - **Page content** — edit text blocks and swap images per page, grouped by tab
  - **Events** — create/edit/delete events with date, time, venue, summary, full description, photo, published/draft toggle. Past events auto-move to "Past events"
  - **Gallery** — multi-file photo upload with captions, delete
- SQLite database (single file: `instance/ebwa.db`) — backup = copy one file
- Uploaded images stored in `static/uploads/` with UUID filenames
- Follows CLAUDE.md conventions: `var` not `const/let`, naive UTC storage on timestamps

## Local setup

```bash
cd ebwa-cms
python3 -m venv venv && source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
flask --app app init-db
flask --app app create-admin
flask --app app run --debug
```

Site: http://127.0.0.1:5000 — Admin: http://127.0.0.1:5000/admin

## Two-factor authentication (optional, per person)

On the **Account** page each admin can turn on two-factor authentication:
scan the QR code with any authenticator app (Google Authenticator,
Microsoft Authenticator, Authy), type the six-digit code it shows to
confirm, and from then on logging in takes two steps — password, then
code. It's per person: turning it on for yourself doesn't affect anyone
else, and it stays off until someone chooses it.

Enrolment hands out **ten recovery codes**, shown once and never again
(only hashed copies are kept). Each works once, in place of a code, so a
lost phone isn't a lockout. If someone runs out, they turn 2FA off from
the Account page and set it up again for a fresh set.

If an admin loses both their phone and their recovery codes, a super
admin clears 2FA for them from **Users** (below), or Netbus does it on
the server with `flask --app app disable-2fa`.

## Changing your admin password

Log in, then **Account** in the sidebar (`/admin/account`). You need your
current password, and the new one must be at least 10 characters.

There is deliberately no "forgotten password" link on the login page. If
someone is locked out, a super admin resets it from **Users** (below), or
Netbus does it on the server with `flask --app app reset-admin-password`.

## Admin roles and feature flags

Admins are `role = 'admin'` by default — that's the EBWA team. Netbus
staff are `role = 'super_admin'`, promoted after the account exists:

```bash
flask --app app promote-super-admin      # prompts for the user's email
```

Super admins also get a **Users** page at `/admin/users` listing every
account with its role, whether two-factor authentication is on, and when
it was created. Passwords and 2FA secrets are never shown — only reset.
From there you can create an account, reset someone's password, clear
their 2FA, change their role, or delete them. Two rules are enforced by
the server, not just hidden in the UI: **the last super admin can't be
deleted or demoted**, and **you can't delete or demote yourself** —
otherwise nobody would be left who could put it right.

If everything goes wrong and nobody can log in at all, Netbus has the
same actions on the server (each prompts for the details):

```bash
cd /opt/ebwa && source venv/bin/activate
flask --app app create-admin              # new account
flask --app app promote-super-admin       # make one a super admin
flask --app app reset-admin-password      # set a new password
flask --app app disable-2fa               # clear someone's 2FA
flask --app app delete-admin              # remove an account
```

Super admins get a **Settings** page at `/admin/features` listing the
optional modules (News & projects, Community resources, Our Journey,
Become a member, Donations & collections) with an on/off toggle each.
Normal admins never see the link and get a 403 if they try the URL.

Switching a module off hides its public pages (they 404), its menu links
and its admin section — **nothing is deleted**, and switching it back on
restores everything exactly as it was. Core pages (home, about, events,
gallery, contact) have no flag and are always on.

To add a flag, append to `FEATURES` in `app.py` (name, label,
description, default), guard the public route with
`@feature_required("name")`, wrap the nav link in
`{% if features.name %}`, and run `flask --app app init-db` again (it
only inserts missing names).

## Adding/changing editable content blocks

Content blocks are seeded in `DEFAULT_BLOCKS` in `app.py`
(group, key, label, kind, default). Add a row, run `flask --app app init-db`
again (it only inserts missing keys), then reference it in a template with
`{{ c.get('your_key','') }}` or, for images,
`{{ url_for('static', filename='uploads/' + c['your_key']) }}`.

## VPS deployment (Ubuntu, same pattern as Netsoft)

```bash
# on the VPS
sudo mkdir -p /opt/ebwa && sudo chown $USER /opt/ebwa
# upload project via FileZilla or git, then:
cd /opt/ebwa
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_hex(32))")
flask --app app init-db
flask --app app create-admin
```

### Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | yes | Flask session signing — long random value |
| `STRIPE_SECRET_KEY` | for donations | Stripe secret key (`sk_live_...`), from the Stripe dashboard |
| `STRIPE_WEBHOOK_SECRET` | for donations | Signing secret (`whsec_...`) for the webhook endpoint |
| `DATABASE_URL` | no | Escape hatch only — the app is deliberately SQLite-only |

Never commit keys. After deploying, register the webhook endpoint in the
Stripe dashboard: `https://ebwa.org.uk/stripe/webhook`, listening for
`checkout.session.completed` — the signing secret it shows you is
`STRIPE_WEBHOOK_SECRET`. Payments stay "pending" until the webhook
confirms them.

`/etc/systemd/system/ebwa.service`:

```ini
[Unit]
Description=EBWA website
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/ebwa
Environment="SECRET_KEY=PUT-A-LONG-RANDOM-VALUE-HERE"
Environment="STRIPE_SECRET_KEY=sk_live_PUT-REAL-KEY-HERE"
Environment="STRIPE_WEBHOOK_SECRET=whsec_PUT-REAL-SECRET-HERE"
ExecStart=/opt/ebwa/venv/bin/gunicorn -w 2 -b 127.0.0.1:8011 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo chown -R www-data:www-data /opt/ebwa/instance /opt/ebwa/static/uploads
sudo systemctl enable --now ebwa
```

nginx server block (then certbot for HTTPS):

```nginx
server {
    server_name ebwa.org.uk www.ebwa.org.uk;
    client_max_body_size 10M;

    location /static/ {
        alias /opt/ebwa/static/;
        expires 30d;
    }
    location / {
        proxy_pass http://127.0.0.1:8011;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Upgrading an existing deployment

Schema changes are additive and always run **before** the restart:

```bash
cd /opt/ebwa
git pull
source venv/bin/activate
pip install -r requirements.txt      # pyotp + qrcode are new
# one-off column additions, on databases created before they existed:
sqlite3 instance/ebwa.db "ALTER TABLE user ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'admin';"
sqlite3 instance/ebwa.db "ALTER TABLE user ADD COLUMN totp_secret VARCHAR(64) DEFAULT '';"
sqlite3 instance/ebwa.db "ALTER TABLE user ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT 0;"
sqlite3 instance/ebwa.db "ALTER TABLE user ADD COLUMN totp_last_counter INTEGER;"
sqlite3 instance/ebwa.db "ALTER TABLE user ADD COLUMN created_at DATETIME;"
flask --app app init-db      # creates feature_flag + recovery_code
sudo systemctl restart ebwa
```

`created_at` has to be nullable — SQLite won't accept a
`CURRENT_TIMESTAMP` default on an added column — so accounts that
predate it show "—" in the Users list. New accounts are stamped.

Each `ALTER TABLE` is a one-off — re-running one errors with "duplicate
column name", which is harmless. Existing admins keep working: they all
become `role = 'admin'` with two-factor authentication off. Promote the
Netbus account afterwards with `flask --app app promote-super-admin`.

The server clock matters for 2FA — codes are time-based, so keep NTP
running (Ubuntu does by default; check with `timedatectl`).

## Backups

Everything lives in two places:

- `instance/ebwa.db` — the database
- `static/uploads/` — all images

A nightly cron zipping both (and optionally sending to Telegram, same as the
RustDesk server) covers full disaster recovery:

```bash
0 3 * * * cd /opt/ebwa && zip -qr /opt/backups/ebwa-$(date +\%F).zip instance static/uploads
```

## Notes / possible next steps

- Donations/volunteer section, contact form with email, event RSVP,
  multi-admin roles, and Bengali page translations are all easy additions.
- To move to PostgreSQL later: set `DATABASE_URL` env var — no code changes.
