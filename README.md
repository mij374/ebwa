# EBWA Community Website + CMS

Flask + SQLite site for the Enfield Bangladesh Welfare Association.
Public site with editable content, plus an admin area for managing
page text/images, events (each with its own page) and a photo gallery.

## Features

**Public pages.** Home, About, Our Journey, Events (list plus a page per
event), News & Projects, Gallery with albums, Community resources, FAQ,
Membership, Collections and Donate, Contact (with a map), and the legal
pages. Which of these exist depends on the feature flags — see
[Admin roles and feature flags](#admin-roles-and-feature-flags).

**Admin area** (`/admin`, login required) — a page per module, all
reached from the sidebar:

- **Page content** — every editable string on the site, grouped by tab
- **What we do**, **Events**, **News & Projects**, **Gallery**
  (photos and albums), **Testimonials**, **Partners**, **FAQ**,
  **Resources**, **Our Journey** — create, edit, delete, publish
- **Enquiries** and **Membership** — personal data, admin-only, logged
- **Subscribers**, **Collections**, **Gift Aid** — with CSV exports
  where they are useful and deliberately not where they are not
- **Visitors** — how many people read the site, counted on this server
- **Account** (your password and 2FA) and **Help** (the written guide)
- Super admins only: **Users**, **Settings**, **Audit log**

**Underneath.** SQLite (`instance/ebwa.db`) in WAL mode; uploads in
`static/uploads/` under UUID filenames, resized and stripped of EXIF on
the way in; self-hosted fonts, so the site makes no third-party request
of its own. It follows the conventions in CLAUDE.md — `var` not
`const`/`let`, naive UTC in the database and UK local on screen.

> **"Backup = copy one file" is not true**, though this file used to say
> so. WAL mode means the database is up to three files (`ebwa.db`,
> `-wal`, `-shm`), and a backup is worth nothing without
> `static/uploads/` beside it. Use `backup-now` — see
> [Backups](#backups) — and [RESTORE.md](RESTORE.md) to put it back.

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

### Local settings: .env

Copy the example and fill in what you need — the file is gitignored:

```bash
cp .env.example .env
```

`.env.example` lists **every** variable the app reads, with a note on
each, so it doubles as the list of what there is to configure. Anything
left out simply keeps its default: the site runs with none of it set,
with email, payments and NAS transfers switched off accordingly.

**.env is for local development only.** It is read at startup if it
exists, does nothing when it does not, and never overrides a variable
that is already set — so it changes nothing on the server. Production
secrets live in `/etc/ebwa/env`, owned `root:www-data` and mode `640`,
which systemd reads via `EnvironmentFile`. Never copy a live key into a
`.env`, and never put a `.env` on the VPS.

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

All of it can also be set through the web, under **Settings → Email**
(Netbus only). A value typed there overrides the environment variable;
leaving a box empty falls back to it, and the page shows which of the two
is in force for every setting.

The password included — it is encrypted before being stored (which is
what `FERNET_KEY` is for) and never shown again: the page says only
whether one is set and where it came from. Leave the box empty to keep
the current one. `SMTP_PASSWORD` remains the fallback when nothing has
been saved, so nothing changes for an existing install and there is still
a way in when the admin itself is unreachable.

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

Super admins get a **Settings** page at `/admin/features`, which carries
an on/off toggle for every optional module — plus the email settings, the
backups and NAS transfer, security alerts, visitor statistics and the
order of the homepage sections. Normal admins never see the link and get
a 403 if they try the URL.

**`FEATURES` in `app.py` is the list of flags.** It is not repeated here
on purpose: a list kept in two places drifts, and this one has drifted
twice — it named six modules for a while after there were ten. Open the
Settings page, or read `FEATURES`.

Switching a module off hides its **public pages** (they 404) and its
**menu links** — nothing is deleted, and switching it back on restores
everything exactly as it was. Core pages (home, about, events, gallery,
contact) have no flag and are always on.

**Its admin pages stay reachable**, and that is deliberate: a flag is a
tidiness feature, not a security boundary, and content must never be
stranded behind one. An admin who switches News off can still open
`/admin/news` and get at what is there.

The single exception is listed in `ADMIN_FLAG_GATES`: the **audit log**,
whose admin route enforces its own flag and returns 403, because that
flag governs who may *read* the log rather than whether the module is on
show. Anything that genuinely needs protecting needs an auth check of its
own — `@super_admin_required` — not a flag.

To add a flag, append to `FEATURES` in `app.py` (name, label,
description, default), guard the public route with
`@feature_required("name")`, wrap the nav link in
`{% if features.name %}`, and run `flask --app app init-db` again (it
only inserts missing names). A flag that must gate an *admin* route as
well goes in `ADMIN_FLAG_GATES` — and reads it with
`flag_explicitly_on()`, not `feature_enabled()`, so that "no row yet"
means no rather than falling through to the default.

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
editing and the same three panels appear below the usual form: **Page
layout**, **Video** and **Photos**. A brand-new post or event has to be
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

**Video** takes an ordinary YouTube or Vimeo link — the one from the
address bar, not an embed code; paste an embed and the site takes just
the video out of it. The page shows a still image with a play button and
**only contacts YouTube or Vimeo if a visitor presses play**, which is
what keeps the cookie notice's "no tracking of any kind" true.

**Where the video sits is a setting** — at the top (the default, and what
a video has always done), after the text, or at the end after any
photographs. A video never displaces a photograph: it takes the lead
slot and everything else moves down one.

Collections work the same way but are on their own path — they are not
rich-content owners, so they have one image and one video with the same
three positions, plus a tick-box for whether the image appears on the
collection page as well as on its card. With both in the same slot the
video comes first.

Netbus can hide the layout and photo panels with the **Rich page
layouts** flag in Settings; with it off the page renders the classic
layout with that one photo, exactly as it did before.

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

> **Something already broken?** [RESTORE.md](RESTORE.md) is the procedure
> for putting the site back — data loss with the server intact, and a
> full rebuild from nothing. Read that rather than this section.


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
ten an hour at most, and never two at once — it refuses while another run
is still going, including one started by the cron job above). Both the
runs and the refusals appear in the audit log. The same page shows when
the last backup ran, how big it was, how many archives are kept, the
database and uploads sizes, and how much disk is free.

The hourly limit is `BACKUP_MANUAL_PER_HOUR` in `app.py`, next to the
other rate-limit scopes, if it ever needs changing.

### Sending backups to the NAS

**Settings → Send backups to the NAS** copies each archive to the NAS
over SFTP, reached across Tailscale. Fill in the address, port, username,
password, the folder on the NAS, the time of day to send (**British
time**, like every other time on the site) and how many archives to keep
there, then use **Test connection** — it
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

**The app does that itself**, over SFTP to the NAS — see [Sending backups
to the NAS](#sending-backups-to-the-nas) above. Writing the archive and
getting it off the server are one job on one `BackupRun` row, so the
Settings panel answers both halves of the only question anybody asks:
did we back up, and did it leave the building?

> An earlier version of this section said the copying was "a server-side
> job, not something this website does or can configure", and gave an
> rsync cron line to set up by hand. **That stopped being true when the
> NAS transfer landed** — the app now owns the whole of it. If you find
> that rsync line in a crontab on a server, it is a leftover: it would
> duplicate the transfer the app is already doing, and it can be removed.

What did survive from that older design, because it was right for reasons
that have not changed:

- **no shelling out.** No rsync, no scp, no subprocess, and no command
  built from anything typed into the admin. The transfer is paramiko
  talking SFTP from inside the app.
- **one destination the admin configures**, not arbitrary remote hosts
  and not a consumer cloud account.
- **the credential never sits in the archive.** The NAS password is
  encrypted with `FERNET_KEY`, which lives in the environment and not in
  the database — so the archive that gets posted to the NAS does not
  contain the key to the NAS.
- **more copies at the far end than on the server.** Remote retention
  (`sftp_keep`, 14 by default) is deliberately separate from local
  (`BACKUP_KEEP`, 7) — the whole point is surviving the server, and the
  NAS has room for far more history.
- **check the far end actually has files, and that they open.** An
  untested backup is a rumour. **Settings → Backups** shows the last
  transfer and what it was called at the other end; to prove an archive
  is readable rather than merely present, see *Reading an archive without
  restoring it* in [RESTORE.md](RESTORE.md).

## Changing the SMTP password

Normally: **Settings → Email**, type the new password, Save. It is
encrypted before it is stored, so this needs no server access — and the
backups stay safe to hold, because the key that decrypts it lives in the
environment and not in the archive.

The steps below are the fallback for when the website itself is
unreachable, or nobody can sign in. A password saved on the Settings page
takes precedence over the one in this file.

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

## Visitor statistics

**Visitors** in the sidebar shows how many people are reading the site.
Open to **every admin** — the figures are EBWA's own, and a charity that
cannot see how many people read its website is being counted at rather
than for. Headline totals, a thirty-day bar chart and the most-visited
pages this month.

It is counted **on this server**. No Google Analytics, no third-party
request, no extra cookie, and nothing stored that identifies anybody — so
the cookie and privacy notices stay true. Telling two visits apart uses a
salted daily hash of the IP and user agent; neither is written anywhere,
and the salt is replaced every day, so the counts cannot be joined up
across days even by somebody holding the database. CLAUDE.md explains why
that was chosen over storing an identifier.

**A "visit" is one person on one day.** Somebody returning next week is
counted again, so a month's figure is person-days rather than a count of
different people. Only "today" is people. The page says so on the line
under the figure, because the number gets quoted without its caveat.

- **Per-page detail** is kept for 62 days by default, then folded into
  daily totals and pruned. A super admin can set that anywhere from 30 to
  365 days on Settings; the trade-off and the disk cost at each end are
  in the helper text beside it.
- **The daily totals are kept for ever**, and have no setting. They are
  what year-on-year comparison is made of, and a control that can delete
  them is one somebody would use by accident.
- **Report for a period** (`/admin/visitors/report`) produces a document
  for grant applications — EBWA's name and charity number, the period,
  the figures, the comparison with last year and the date it was
  produced. Print-friendly HTML rather than a generated PDF: use the
  browser's Print and save it.
- **A monthly email** to the board can be switched on by a super admin,
  off by default with no recipient. It is sent by cron
  (`send-monthly-report`) and is idempotent through the audit log, so
  running it twice in a month sends once.

Set EBWA's name and charity number in **Page content → Org**, or the
period report goes out without them. It says so on screen while they are
missing, and that note is deliberately not printed — a funding
application should not carry a message to its own author.

## The written guide at /admin/help

**Help** in the sidebar is the manual for running the site — every admin
role, with a contents list and a print stylesheet so it saves as a PDF.
It covers each module, the four rules that come up everywhere (Published,
sort order, delete being final, what happens to an uploaded photo) and a
"if something looks wrong" list.

Its screenshots are **captured, not drawn**:

```bash
python tools/capture-guide-shots.py
```

That signs in to the real admin in a browser against a scratch database
of demo content and photographs each screen into `static/img/guide/`.
**Re-run it when a screen changes** rather than editing an image by hand
— a hand-made screenshot is right once and then quietly wrong. It refuses
to run against `instance/ebwa.db` and checks its own fixtures, so no real
enquiry or donor detail can end up in the guide.

## Fonts

The three faces — Bricolage Grotesque, Public Sans and Noto Serif Bengali
— are **self-hosted** from `static/fonts/`, and that removed the last
third-party request the site makes of its own accord. Linking Google's
CSS meant every visitor announced their IP address to Google before a
word was drawn, which sits badly with a site that tells people it does
not track them.

Two things to know if you touch them:

- **The version is in the filename** (`name.<sha8>.woff2`), because a
  `url()` inside a static stylesheet cannot call `asset_version()` and
  nginx serves `/static/` with `expires 30d`. After adding or replacing a
  face, run `python tools/hash-fonts.py` to re-stamp the names and
  rewrite the CSS.
- **The font hosts are out of the CSP**, and the two must stay in step —
  leaving the domains in the policy would quietly re-permit what was
  removed. `tests/check_fonts.py` proves in a browser that nothing leaves
  this server, and that the Bengali face actually loads for the eyebrow
  text.

The map on /contact is still a third party: Google's iframe loads its own
fonts from inside itself. Those are requests the *map* makes, not the
page, and they are the one remaining off-site request.

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
can be set there too (Settings → Email): it is encrypted at rest with
`FERNET_KEY`, never rendered back, and an empty box keeps the current
one. An earlier version of this file said the password was not editable
through the website; that stopped being true in 59a07cc.

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

**Two workers, and no `--timeout` set — so no request may be slow.** A
gunicorn sync worker only heartbeats to the arbiter *between* requests,
so anything blocking past the 30-second default has its worker killed
mid-request and the caller gets a 502 (nginx gives up at 60 seconds of
its own). That is why "Back up now" starts a thread and returns
immediately rather than writing the archive inside the request, and why
anything else long-running added later must do the same — or be a CLI
command run from cron, which is a different process with no timeout over
it at all.

nginx server block (then certbot for HTTPS):

```nginx
server {
    server_name ebwa.org.uk www.ebwa.org.uk;
    client_max_body_size 10M;

    location /static/ {
        alias /opt/ebwa/static/;
        # Safe to cache hard: the app appends a content hash to the
        # stylesheet and icon URLs (asset_version() in app.py), so a
        # deploy that changes a file changes its URL, and one that does
        # not leaves the visitor's copy alone. Do not shorten this
        # thinking it fixes a stale asset — check the ?v= instead.
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

> **A tick means VERIFIED APPLIED, not intended.** Tick a box only once
> you have seen that entry's own steps run in that environment —
> `check-schema` clean, the statement executed, the page working. Never
> tick because a deploy was planned, scheduled, or believed to have
> happened: the whole value of the file is that an unticked box makes
> somebody go and look. Equally, an unticked box is not a claim that the
> step is missing — it means nobody has checked yet. If you cannot check
> from where you are, write what you could and could not establish beside
> the entry instead of ticking it hopefully.
>
> This is not hypothetical. Every entry sat ticked Local-only while the
> demo VPS had been serving the site for weeks, so the file said nothing
> useful about either environment. See the verification record at the top
> of DEPLOY.md for what that took to untangle, and for the two commands
> that settle the parts a browser cannot see.

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

## Where the data lives

- `instance/ebwa.db` — the database
- `static/uploads/` — every uploaded image

**Backing those up is the app's own job** — see [Backups](#backups)
above, and [RESTORE.md](RESTORE.md) for putting them back.

> **Do not zip `instance/` on a running site.** This section used to
> recommend exactly that, and it is worse than useless. SQLite runs in
> WAL mode, so a plain `zip` of the folder captures the database
> mid-write along with its `-wal` and `-shm` companions — and a restore
> from that is not obviously broken. Rehearsing it gave `PRAGMA
> integrity_check` → `ok` and a table with **zero rows**: it looks like a
> successful restore and the data is gone. `backup-now` exists because of
> this. It takes the snapshot through SQLite's own backup API, which is
> consistent even while the site is serving.

## Notes / possible next steps

- Event RSVP and Bengali page translations are the obvious remaining
  additions. Phase 2 in CLAUDE.md is the current list — read it there.
- **This is SQLite by design, not by accident, and it stays that way.**
  `DATABASE_URL` exists so tests and rehearsals can point at a throwaway
  file; it is an escape hatch, never a default, and swapping in
  PostgreSQL is *not* a configuration change. WAL mode is set with a
  `PRAGMA` on connect, `run_backup()` uses `sqlite3.connect` and the
  SQLite backup API for its snapshot, and `check-schema` knows about
  `sqlite_autoindex_*` entries. See CLAUDE.md → Stack.
