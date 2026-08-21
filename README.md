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

### Outgoing email (optional locally, required in production)

The contact form emails enquiries to EBWA. Set these in the environment —
never in the database, never in this repository:

| Variable | What it is |
| --- | --- |
| `SMTP_HOST` | Mail server hostname |
| `SMTP_PORT` | 587 for STARTTLS (default), 465 for implicit TLS |
| `SMTP_USER` | Username, if the server needs one |
| `SMTP_PASSWORD` | Password for that username |
| `SMTP_USE_TLS` | `1` (default) or `0` |
| `MAIL_FROM` | The address email is sent from |
| `MAIL_TO` | Where enquiries go, unless overridden in Settings |

Backups use two more:

| Variable | What it is |
| --- | --- |
| `BACKUP_DIR` | Where archives are written (default: `backups/` beside the app) |
| `BACKUP_KEEP` | How many archives to keep (default: 7) |
| `FERNET_KEY` | Encryption key for the stored NAS password (see below) |

Generate the key once, and keep it out of the archives:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put it in `/etc/ebwa/env` as `FERNET_KEY='...'`. **Losing it means the
stored NAS password can no longer be read** — type a new one into
Settings and save. Changing it has the same effect.

Check it works before anyone relies on it:

```bash
flask --app app test-mail                 # sends to MAIL_TO
flask --app app test-mail you@example.com
```

With none of this set the form still works and still saves every enquiry
— they are just not emailed, and each attempt is noted in the audit log.

Everything except the password can also be set through the web, under
**Settings → Email** (Netbus only). A value typed there overrides the
environment variable; leaving a box empty falls back to it, and the page
shows which of the two is in force for every setting. The password is
read from `SMTP_PASSWORD` only: it is never stored in the database, never
shown, and there is no box to type it into.

## The dashboard

`/admin` is the first page you land on after logging in, and it shows a
card for every part of the site, grouped into three rows:

- **Pages and content** — upcoming events, news and projects, gallery
  photos, "What we do" cards, testimonials, partners, community
  resources and journey milestones. The small line under each number
  tells you what is not live yet ("2 unpublished", "1 hidden"), so a
  draft you meant to publish does not sit there unnoticed.
- **People** — newsletter subscribers, and membership applications.
  Applications still marked *new* get their own card, outlined in red
  whenever any are waiting for a reply.
- **Donations and collections** — raised this year (and since the site
  opened), how many collections are open, and the Gift Aid you can
  claim. That last figure counts only donation portions with a complete
  declaration, so it always matches the Gift Aid claim page exactly.

Every card is a link to the page that manages it. Cards for optional
modules disappear when a super admin switches that feature off in
Settings, exactly as the menu links do.

Above the cards, a **Needs attention** panel appears when something is
waiting: membership applications nobody has answered, events that have
now passed but are still published, pages still holding the placeholder
privacy notice or terms (called out as a launch blocker), collections or
published pages with no photo, and payments left unfinished for more
than a day. Each line links to where the fix happens, and the whole
panel disappears when there is nothing to say.

Below the cards, super admins see the six most recent entries from the
audit log, in UK local time, with a link to the full page.

The dashboard deliberately shows numbers only — never a donor's name, an
applicant's address or any other personal detail. For those you go to
the relevant page, which records that you looked in the audit log.

## Audit log

**Audit log** in the sidebar (`/admin/audit`) shows what has been done in
the admin area and by whom: logins and failed login attempts, logouts,
password changes, two-factor being turned on or off, every item created,
edited, deleted or re-published across the whole site, every account
change a super admin makes, every feature switched on or off, and every
export or printable list of personal data. Newest first, with filters by
person, action and date range, and times shown in UK local time.

The page is read-only and the log is permanent. There is no button — and
no hidden URL — that edits or deletes an entry, which is the point of
having one. A failed login records the email that was tried and nothing
else; passwords, 2FA secrets and recovery codes are never written to it.

Netbus can hide the page from EBWA's own admins with the **Audit log**
feature flag in Settings. That only affects who can see the page:
recording carries on regardless, and super admins can always read it.

## Staying signed in

The admin area signs you out after **20 minutes of doing nothing** — a
sensible precaution on a shared or public computer. It's idle time, not
total time: as long as you keep working, the clock keeps resetting, so a
long afternoon of editing never interrupts you.

If it does expire, the next click returns you to the login page with a
note saying the session timed out, rather than dumping you there with no
explanation. Anything you'd typed into a form but not saved is lost, so
save long pieces of writing as you go.

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
Become a member, Donations & collections, Audit log visibility) with an
on/off toggle each. Normal admins never see the link and get a 403 if
they try the URL.

Switching a module off hides its public pages (they 404), its menu links
and its admin section — **nothing is deleted**, and switching it back on
restores everything exactly as it was. Core pages (home, about, events,
gallery, contact) have no flag and are always on.

To add a flag, append to `FEATURES` in `app.py` (name, label,
description, default), guard the public route with
`@feature_required("name")`, wrap the nav link in
`{% if features.name %}`, and run `flask --app app init-db` again (it
only inserts missing names).

## Legal pages and the cookie notice

`/privacy` and `/terms` are ordinary editable pages — **Page content →
legal** in the admin. Leave a blank line between paragraphs; each one
becomes its own paragraph on the page. Both are linked in the footer and
listed in the sitemap.

> **Before launch:** both ship with placeholder text that says so on the
> page. EBWA needs to supply its own privacy notice and terms — Netbus
> can't write them on the charity's behalf. This is a launch blocker.

A notice appears along the bottom of the page on someone's first visit,
explaining the cookies and linking to the privacy page. Clicking (or
tabbing to and pressing Enter on) **OK** records that it has been read
in a first-party cookie and it does not come back.

The site sets exactly two cookies, both strictly necessary: the admin
login session, and the one remembering the notice has been read. There
is no analytics, advertising or tracking of any kind, so the notice is
**informational, not a consent request** — nothing on the site waits on
it. If analytics is ever added, that changes: the law then requires a
proper consent flow, not this banner. Raise it with Netbus first.

## Page layouts and photos

Available on the **About** page (Page content → About), and on every
**news post**, **event** and **Our Journey milestone** — open one for
editing and the same two panels appear below the usual form. A brand-new post or event has to be
saved once before its photos can be added, since there is nothing to
attach them to until then.

**Page layout** picks how the words and photos are arranged:

| Layout | What it looks like |
| --- | --- |
| **Classic** | A lead photo beside the text, any others in a strip underneath |
| **Gallery** | The text first, then the photos in a staggered grid |
| **Alternating** | Text and photos side by side, swapping sides down the page |

Try one, save, and look at the page — you can change back at any time,
and no photos are lost either way. Gallery and Alternating need photos to
show their shape; with none, the page falls back to Classic.

**Photos** adds as many images as you like. The one with the lowest sort
number is the lead image; change the numbers to reorder. Every photo
needs **alt text** — a short description of what is happening in it, read
aloud to people using a screen reader. The form won't accept a photo
without it, and won't let you empty it later. Captions are optional and
appear under the photo on the page.

The single photo an item already had keeps working and becomes the lead
image the first time you use the manager, so nothing is lost. Listing
pages and homepage cards always show that lead photo only — the extra
photos appear on the full page.

Netbus can hide both panels with the **Rich page layouts** flag in
Settings; with it off the page renders the classic layout with that one
photo, exactly as it did before.

## Security alerts

**Settings → Security** shows how many sign-ins have failed in the last
24 hours and can email an alert when one address keeps trying: more than
ten failures within an hour sends one email, and then nothing for an hour
however long it goes on.

Alerts have their own recipient — **Send security alerts to** — because
they are not for the same person as a contact enquiry. An enquiry is for
EBWA; "somebody is trying passwords against your admin" is for whoever
looks after the server. Put Netbus (or several addresses, separated by
commas) in that box. Leave it empty and alerts follow the enquiries
address, which is fine until that becomes an @ebwa.org.uk mailbox — at
which point the alerts would quietly follow it there.

**Send a test alert** proves the route without waiting for a real attack.
Alerts name the addresses that were tried and the IP they came from, and
never a password: the site does not record attempted passwords at all.

## Backups

The website can write a backup archive of itself: a consistent snapshot
of the database plus every uploaded photo, zipped into `BACKUP_DIR` with
a timestamped name. It keeps the newest `BACKUP_KEEP` archives (7 by
default) and deletes older ones.

By hand, on the server:

```bash
cd /opt/ebwa && ./venv/bin/flask --app app backup-now
```

Nightly, as a cron job for the service user:

```cron
15 2 * * * cd /opt/ebwa && set -a && . /etc/ebwa/env && set +a && ./venv/bin/flask --app app backup-now >> /var/log/ebwa-backup.log 2>&1
```

Or from the website: **Settings → Backups → Back up now** (Netbus only,
twice an hour at most). The same page shows when the last backup ran, how
big it was, how many archives are kept, the database and uploads sizes,
and how much disk is free.

### Sending backups to the NAS

**Settings → Send backups to the NAS** copies each archive to the NAS
over SFTP, reached across Tailscale. Fill in the address, port, username,
password, the folder on the NAS, the time of day to send (in **UTC**) and
how many archives to keep there, then use **Test connection** — it
connects, checks the folder exists, and writes and deletes a small file
to prove it can actually be written to.

The password is encrypted before it is stored and never shown again. Once
one is saved the field shows "Password set"; leaving it empty keeps the
one already there. Changing it needs nothing on the server, unlike the
mail password — the difference is that a backup archive contains the
database, so a readable NAS password inside it would travel to the NAS
along with everything it protects.

If a transfer fails it is tried once more and then left until the next
day's run, rather than a machine hammering a NAS that is switched off.
The archive stays on the server meanwhile, and Settings shows what
happened.

Two things this needs on the server:

- **Tailscale running on both ends**, with the VPS able to reach the NAS
  by its tailnet name;
- **a cron job**, because the website has no scheduler of its own and
  must not grow one — it runs under several worker processes, and a timer
  in each would mean several backups at once:

```cron
*/15 * * * * cd /opt/ebwa && set -a && . /etc/ebwa/env && set +a && ./venv/bin/flask --app app run-scheduled-backup >> /var/log/ebwa-backup.log 2>&1
```

That runs every fifteen minutes and does nothing at all until the
configured time has passed without a good run, so it is safe to leave
running.

### An archive on the server is not a backup

It protects you from a mistake — a deleted album, a bad edit, a failed
upgrade. It does **not** protect you from losing the server, which is the
thing backups are for. A copy has to leave the machine.

That copying is a **server-side job, not something this website does or
can configure**. The site has no business holding a key to another
machine, and nothing in the admin runs commands anybody typed into it.
Set it up alongside the backup cron — for example, pushing the archives
to a NAS or another host every night:

```cron
45 2 * * * rsync -az --delete -e 'ssh -i /root/.ssh/ebwa-backup -o StrictHostKeyChecking=yes' /opt/ebwa/backups/ backup@nas.example.net:/volume1/backups/ebwa/ >> /var/log/ebwa-offsite.log 2>&1
```

Points worth keeping:

- the ssh key is on the server, in root's keyring — never in the
  database, never in this repository, never in the admin;
- run the copy *after* the backup job, not at the same time;
- check the far end actually has files, and that they open. An untested
  backup is a rumour;
- keep more copies at the far end than on the server — the whole point
  is surviving the server.

## Changing the SMTP password

The mail password is the one setting that is **not** editable in the
website's Settings page. Everything in the database is included in the
nightly backups, and a live mail password does not belong in a backup —
so it stays in a file on the server. (The same steps are shown, with this
deployment's real paths, in the collapsible box under the password on
Settings → Email.)

1. Connect to the server over SSH.
2. Open the environment file:

   ```bash
   sudo nano /etc/ebwa/env
   ```

3. Edit or add the line below. No spaces around the equals sign, and
   single quotes around the value — the quotes protect `$`, `#`, `!` and
   spaces:

   ```bash
   SMTP_PASSWORD='new-password-here'
   ```

4. Save and exit: **Ctrl+O**, **Enter**, **Ctrl+X**.
5. Check the file still parses. One malformed line stops the service
   reading *any* of its settings:

   ```bash
   sudo -u www-data bash -c 'set -a; source /etc/ebwa/env; set +a; echo "env OK"'
   ```

6. Restart the service so it picks up the change:

   ```bash
   sudo systemctl restart ebwa
   ```

7. Confirm it works:

   ```bash
   sudo -u www-data bash -c 'cd /opt/ebwa && set -a && source /etc/ebwa/env && set +a && ./venv/bin/flask --app app test-mail your@address.com'
   ```

   Or use **Send a test email** on Settings → Email once the service has
   restarted.

Leave the file's permissions as `root:www-data` and `640` — readable by
the service, by nobody else:

```bash
sudo chown root:www-data /etc/ebwa/env
sudo chmod 640 /etc/ebwa/env
```

## Enquiries from the contact form

The contact page has a form under the address and map. When somebody
sends a message two things happen: it is saved here, and it is emailed to
EBWA. Replying to that email goes straight back to the person who wrote —
their address is set as the reply address, so you never have to copy it
out.

**Enquiries** in the sidebar lists everything that has come in, newest
first, with a red count beside the menu item for anything nobody has read
yet. Each one can be marked **read** or **replied**, and **Reply** opens
your own email programme with their message quoted, so your answer comes
from your mailbox and sits in your Sent items like any other email.

There is deliberately no download button. These are people's names,
addresses and questions: read them here, reply, and delete an enquiry
once it is dealt with. Every time the list is opened, a status is changed
or a message is deleted, it is recorded in the audit log.

If email is not set up on the server, or the mail server is having a bad
day, **the enquiry is still saved** — you will see it in this list, and
the audit log will show that the email could not be sent. Nobody's
question is lost because of a mail problem.

Netbus can change where enquiries are emailed under **Settings** — useful
when EBWA moves to its own @ebwa.org.uk address. The mail server password
is not editable through the website, by anyone.

*Not built yet, worth considering:* an automatic "thanks, we've got your
message" reply to the sender. Ask Netbus if you would like it.

## Frequently asked questions

**FAQ** in the sidebar manages the questions on the public /faq page.
Each one has a question, an answer (blank lines make new paragraphs), an
optional category and a sort number. Questions with no category appear
at the top of the page; the rest are grouped under their category
heading, alphabetically, with your sort numbers ordering the questions
inside each group.

Write the question the way somebody would say it out loud — "Do I have
to be a member to come to the drop-in?" rather than "Membership
eligibility". That is what people type into Google, and the page is set
up so search engines can show your answers directly in their results.

Untick **Published** to keep a question out of sight while you work on
it. Netbus can hide the whole page with the **Frequently asked
questions** flag in Settings; nothing is deleted, and switching it back
on restores the page exactly as it was.

## The photo gallery

The gallery is organised into **albums** — an event, a trip, a year.
Visitors see a card for each album with its cover photo and how many
photographs are in it, and clicking one opens that album. Clicking any
photograph opens it full size: arrow keys or the on-screen arrows move
through the album, Escape closes it, and on a phone you can swipe.

**Albums** (Gallery → Manage albums) work like any other section: title,
short description, an optional cover photo (with none, the album shows
its most recent photograph), a sort number and a Published tick. Unticking
Published hides the whole album from the website; the photos stay safely
in the admin.

**Photos** (Gallery) are uploaded with an album chosen from the dropdown,
or left unfiled. To reorganise later, tick the photos you want and use
**Move ticked photos to** — that is the quickest way to sort a big
upload afterwards. The tabs across the top filter the screen to one
album, or to everything still unfiled.

**Deleting an album never deletes photographs.** They become unfiled and
stay on the site under "All photos", where you can put them in another
album whenever you like. Deleting an individual photo, from the photo
screen, does remove it for good.

Photos appear newest first. If you want one to lead an album regardless
of when it was taken, that is what the sort numbers are for — a lower
number comes first.

## What happens to a photo when you upload it

You can upload straight off a phone — the site does the tidying up.
Every photo is:

- **turned the right way up.** Phones record "this was shot in
  portrait" as a hidden flag rather than rotating the picture; the site
  applies that flag, so photos don't end up sideways.
- **stripped of hidden data.** A phone photo carries the camera model,
  the date and, usually, **the GPS coordinates of where it was taken**.
  Uploaded photos are public, so all of that is removed before the file
  is saved. Nobody can read a member's street off a photograph on the
  website.
- **shrunk to a sensible size.** A 4,000-pixel, 5 MB photo is scaled to
  1,600 pixels wide and re-saved — indistinguishable on screen, a
  fraction of the size.
- **given a small copy** for listing cards and grids, so a page of
  thumbnails doesn't download a page of full-size photographs. On a
  homepage carrying eight phone photos this took the images from about
  8.9 MB to 0.9 MB — the difference between a slow page and an instant
  one on a phone signal.

Nothing is asked of you: upload the photo you have. If a file isn't a
photo, or is damaged, you'll get a message saying so rather than a
broken page.

## Partner logos

**Partners** in the admin sidebar. Each partner card can show the
organisation's name and description (the default), its logo on its own,
or the logo with the name and description underneath — pick one under
"What the card shows" and upload a logo on the same form.

Existing partners keep the name-and-description look until you upload a
logo, and if you pick a logo option without uploading one the card
quietly falls back to the text so nothing looks broken. The admin list
flags those with "Name (no logo yet)".

## Editing the homepage "What we do" cards

**What we do** in the admin sidebar (`/admin/services`) manages the six
service cards on the homepage. Add, edit, reorder (lower sort number
first), hide or delete them. The icon is a single emoji typed straight
into the form — on Windows press <kbd>Windows key</kbd> + <kbd>.</kbd>
for the emoji picker. Hiding a card keeps it in the admin list but takes
it off the site; with no cards at all the section disappears cleanly.

The six original cards are seeded on first `init-db`. Once the table has
anything in it, later deploys leave it alone — so edited and deleted
cards stay that way.

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

Secrets live in one environment file rather than in the unit, so they can
be changed without editing systemd and so the file's permissions are the
only thing guarding them. `/etc/ebwa/env`:

```bash
sudo mkdir -p /etc/ebwa
sudo tee /etc/ebwa/env >/dev/null <<'EOF'
SECRET_KEY='PUT-A-LONG-RANDOM-VALUE-HERE'
STRIPE_SECRET_KEY='sk_live_PUT-REAL-KEY-HERE'
STRIPE_WEBHOOK_SECRET='whsec_PUT-REAL-SECRET-HERE'
SMTP_HOST='smtp.example.net'
SMTP_PORT='587'
SMTP_USER='website@ebwa.org.uk'
SMTP_PASSWORD='PUT-THE-MAIL-PASSWORD-HERE'
MAIL_FROM='website@ebwa.org.uk'
MAIL_TO='enquiries@ebwa.org.uk'
EOF
sudo chown root:www-data /etc/ebwa/env
sudo chmod 640 /etc/ebwa/env
```

Single quotes around every value: they protect `$`, `#`, `!` and spaces.
`640 root:www-data` means the service can read the file and nobody else
can.

`/etc/systemd/system/ebwa.service`:

```ini
[Unit]
Description=EBWA website
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/ebwa
EnvironmentFile=/etc/ebwa/env
ExecStart=/opt/ebwa/venv/bin/gunicorn -w 2 -b 127.0.0.1:8011 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

If a deployment puts these elsewhere, set `DEPLOY_ENV_FILE`,
`DEPLOY_PATH`, `DEPLOY_SERVICE` and `DEPLOY_USER` too — the instructions
the admin shows on the Settings page are built from them, so they follow
the real paths instead of these.

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
        proxy_set_header X-Forwarded-Host $host;
    }
}
```

**These four headers are load-bearing.** The app trusts exactly one proxy
hop (`ProxyFix(..., x_for=1, x_proto=1, x_host=1)` in `app.py`) so that
the audit log records the real visitor and each visitor gets their own
rate-limit bucket. Without them every request looks like it came from
127.0.0.1, and the whole internet shares one bucket.

`$proxy_add_x_forwarded_for` appends the real client to whatever the
caller sent, and the app reads the **last** entry — so a visitor who
forges `X-Forwarded-For` can't fake their IP or dodge the rate limit.
Two things follow: gunicorn must stay bound to `127.0.0.1` so nobody can
reach it directly, and if another proxy (a CDN, say) is ever put in
front, the hop counts in `app.py` must go up to match.

## Upgrading an existing deployment

**[DEPLOY.md](DEPLOY.md) is the checklist** — every schema change, newest
first, with the exact statements and a tick-box per environment. Work
upwards from the oldest unticked entry, then:

```bash
cd /opt/ebwa
git pull
source venv/bin/activate
pip install -r requirements.txt          # if an entry says so
sqlite3 instance/ebwa.db ".backup instance/ebwa-before-upgrade.db"
# ... the ALTER TABLE statements from each unticked entry, oldest first ...
flask --app app init-db                  # if an entry says so
flask --app app check-schema             # must print "Schema is up to date"
sudo systemctl restart ebwa
```

Schema changes are additive and always run **before** the restart — the
app queries the new columns the moment it starts.

`check-schema` is what stops a missed step becoming an outage. It
compares the models against the live database and exits non-zero if
anything is missing, listing the tables and columns and suggesting the
`ALTER TABLE` for each:

```
MISSING COLUMNS (2):
  - partner.logo
  - partner.display_mode
  Fix (check each against DEPLOY.md first):
    ALTER TABLE partner ADD COLUMN logo VARCHAR(255) DEFAULT '';
    ALTER TABLE partner ADD COLUMN display_mode VARCHAR(10) NOT NULL DEFAULT 'text';

Schema is BEHIND the code. Apply the above BEFORE restarting the app.
```

**Run it at the end of every deploy, before the restart.** If it doesn't
say "Schema is up to date", don't restart — fix it first. It only reads
the database, so it's safe to run whenever you want to know where a
server stands. Tables left behind by retired modules are listed as
expected and don't count as a failure.

Each `ALTER TABLE` is a one-off; re-running one errors with "duplicate
column name", which is harmless. `init-db` only ever creates what's
missing. On a brand-new database none of the ALTERs apply — `init-db`
builds everything at its current shape.

Existing admins keep working: they all become `role = 'admin'` with
two-factor authentication off. Promote the Netbus account afterwards with
`flask --app app promote-super-admin`, then tick the boxes in DEPLOY.md
and commit them.

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
