# Restoring the EBWA website

**Start here. Read the two lines below, pick your scenario, and go
straight to it.** Everything is written for somebody who has not read
this before and is under pressure. Each step stands on its own — you can
stop, take a break, and come back at the step you were on.

| Which are you in? | Go to |
| --- | --- |
| **Content is wrong or missing** — something was deleted or corrupted, but the server, the code and the settings are all fine. | [Scenario 1](#scenario-1--data-loss-server-intact) |
| **The server is gone** — the VPS is destroyed, unreachable or being replaced, and you are building it again from nothing. | [Scenario 2](#scenario-2--server-lost-rebuilding-from-nothing) |

Not sure? If you can still SSH into the machine and `sudo systemctl
status ebwa` answers, you are in **Scenario 1**.

**Before you touch anything, read this one paragraph.** Scenario 1
begins by copying the current broken state aside. Do not skip it. It
takes thirty seconds, it is what lets you undo the restore itself, and
it is the only proof of what the site looked like before you began. A
restore that makes things worse and cannot be unwound is the bad day
inside the bad day.

Throughout, the paths are the ones this site is actually deployed with:

| Thing | Where |
| --- | --- |
| Code and venv | `/opt/ebwa` |
| Database | `/opt/ebwa/instance/ebwa.db` |
| Uploaded photos | `/opt/ebwa/static/uploads/` |
| Local archives | `/opt/ebwa/backups/` |
| Secrets | `/etc/ebwa/env` (mode 640, `root:www-data`) |
| Service | `ebwa` (systemd), gunicorn on 127.0.0.1:8011 |
| Runs as | `www-data` |

If a deployment ever moves these, it also sets `DEPLOY_PATH`,
`DEPLOY_ENV_FILE`, `DEPLOY_SERVICE` and `DEPLOY_USER` in the environment
file — check those first, because the Settings page builds its own
instructions from them and they are the truth.

**Every `flask` command must be the one in the venv** —
`/opt/ebwa/venv/bin/flask`, never a bare `flask`. A bare one either does
not exist or is a different Python without the site's packages.

**And `sudo -u www-data` does NOT load `/etc/ebwa/env`.** systemd reads
that file for the service; running a command by hand as `www-data` does
not. It makes no difference to the read-only checks used here
(`check-schema`, `check-uploads`) or to `backup-now`, which only writes a
local archive. It matters for anything that needs a secret — `test-mail`
falling back to the `SMTP_*` variables, or `run-scheduled-backup` needing
`FERNET_KEY` to decrypt the NAS password. For those, source it first, the
way the cron line does:

```bash
cd /opt/ebwa && set -a && . /etc/ebwa/env && set +a
sudo -u www-data --preserve-env ./venv/bin/flask --app app test-mail
```

---

## Scenario 1 — data loss, server intact

Something has been deleted or corrupted. The machine, the code and
`/etc/ebwa/env` are all fine. You are putting the data back and nothing
else.

Roughly fifteen minutes. The site is down for the middle part of it.

### 1.1 Stop the service

```bash
sudo systemctl stop ebwa
```

Nothing is writing to the database now. Do not skip this and restore
underneath a running site — you would be replacing a file that gunicorn
has open, and the result is neither the old data nor the new.

nginx can stay up. Visitors get a 502, which is honest.

### 1.2 Copy the current state aside — before anything else

```bash
sudo mkdir -p /var/tmp/ebwa-before-restore-$(date +%Y%m%d-%H%M%S)
BEFORE=$(ls -d /var/tmp/ebwa-before-restore-* | tail -1)
sudo cp -a /opt/ebwa/instance/ebwa.db* "$BEFORE"/
sudo cp -a /opt/ebwa/static/uploads "$BEFORE"/uploads
ls -la "$BEFORE"
```

The `ebwa.db*` copies the database **and its `-wal` and `-shm`
companions**, which is what you want here: together they are the current
state, exactly as it is. `cp -a` keeps ownership and timestamps.

You now have a way back from the restore itself, and a record of what
the site held before you started. Keep it until everyone agrees the
restore was right — a fortnight is not excessive.

### 1.3 Get an archive

Archives are named `ebwa-backup-YYYYmmdd-HHMMSS.zip`, in UK local time.

**First look on the server.** The newest seven are kept locally
(`BACKUP_KEEP`, default 7):

```bash
ls -lat /opt/ebwa/backups/
```

If one of those is from before the damage, use it and skip to 1.4.

**Otherwise fetch one from the NAS**, which keeps far more (`sftp_keep`,
default 14). It is reached over Tailscale, so check that first:

```bash
tailscale status | head
```

The NAS address, username and folder are on **Settings → Send backups to
the NAS**, or in the `sftp_host`, `sftp_user` and `sftp_remote_path`
rows of the `block` table. The password is **not** readable there — it is
encrypted — so use the copy in the credential store.

```bash
sftp <user>@<nas-tailnet-name>
sftp> cd /volume1/backups/ebwa      # whatever the folder is set to
sftp> ls -lt
sftp> get ebwa-backup-20260826-023000.zip /var/tmp/
sftp> bye
```

> **Pick the newest archive from BEFORE the damage, not simply the
> newest.** If the deletion happened on Tuesday and the nightly backup
> ran on Tuesday night, that archive contains the deletion. Look at the
> timestamps and think about when the problem started. This is the step
> people get wrong.

### 1.4 Look inside it before you trust it

Never restore an archive you have not opened. This reads it without
unpacking anything:

```bash
unzip -l /var/tmp/ebwa-backup-20260826-023000.zip | head -20
```

You should see exactly this shape:

```
   192512  database/ebwa.db
     2528  uploads/photo-0.jpg
      ...
      438  README.txt
```

Sanity checks, in order of how often they matter:

- **`database/ebwa.db` is present and is not a few kilobytes.** A real
  one is hundreds of KB at least.
- **The `uploads/` count looks right.** Compare with
  `ls /opt/ebwa/static/uploads | wc -l`.
- **The date in the filename is before the damage.**

To go further and actually query the database inside the archive without
touching the live one, see [Reading an archive without restoring
it](#reading-an-archive-without-restoring-it).

### 1.5 Unpack to a staging directory

**Not straight over `/opt/ebwa`.** The archive's top level is
`database/` and `uploads/`, which do not match the site's layout
(`instance/` and `static/uploads/`), so unzipping in place would leave
you with two stray directories and nothing restored.

```bash
sudo rm -rf /var/tmp/ebwa-restore && sudo mkdir -p /var/tmp/ebwa-restore
sudo unzip -q /var/tmp/ebwa-backup-20260826-023000.zip -d /var/tmp/ebwa-restore
find /var/tmp/ebwa-restore -maxdepth 2 | head
```

### 1.6 Put the database back, and DELETE the stale -wal and -shm

```bash
sudo cp /var/tmp/ebwa-restore/database/ebwa.db /opt/ebwa/instance/ebwa.db
sudo rm -f /opt/ebwa/instance/ebwa.db-wal /opt/ebwa/instance/ebwa.db-shm
ls -la /opt/ebwa/instance/
```

Only `ebwa.db` should be there afterwards.

> **This is the step that silently undoes a restore, and it does not
> look like an error.** The site runs SQLite in WAL mode, so alongside
> `ebwa.db` there are normally `ebwa.db-wal` and `ebwa.db-shm` holding
> recent writes. They belong to the database they were written beside.
> Leave them next to a *restored* database and SQLite may replay them
> over the top of it — putting back the very changes you are trying to
> undo.
>
> **Measured, not assumed.** Restoring an archive holding 7 events while
> leaving the old `-wal` in place gave `PRAGMA integrity_check` → `ok`
> and a table with **0 events**. Nothing errored, nothing warned, and the
> restore appeared to have worked. Delete both files and the same archive
> gave 7 events and 4 news posts.
>
> There is nothing to lose by deleting them: whatever they held is in
> the copy you took in 1.2, and every write they describe is either
> already in the database file or is part of the damage you are undoing.

### 1.7 Put the uploads back

```bash
sudo cp -a /var/tmp/ebwa-restore/uploads/. /opt/ebwa/static/uploads/
```

The trailing `/.` copies the *contents* into the existing folder. This
**adds and overwrites, and does not delete** — a photo uploaded after the
archive was taken survives, which is almost always what you want.

If you genuinely need the folder to match the archive exactly, and you
have the copy from 1.2, then and only then:

```bash
sudo rm -rf /opt/ebwa/static/uploads && sudo mkdir -p /opt/ebwa/static/uploads
sudo cp -a /var/tmp/ebwa-restore/uploads/. /opt/ebwa/static/uploads/
```

### 1.8 Fix ownership

The service runs as `www-data`, and you have just written files as root.
If you skip this the site starts and then fails on the first write —
which reads as a mysterious 500 on saving anything.

```bash
sudo chown -R www-data:www-data /opt/ebwa/instance /opt/ebwa/static/uploads
sudo chmod 664 /opt/ebwa/instance/ebwa.db
sudo find /opt/ebwa/static/uploads -type f -exec chmod 664 {} +
sudo find /opt/ebwa/static/uploads -type d -exec chmod 775 {} +
```

SQLite writes its `-wal` and `-shm` **into the containing directory**, so
`instance/` itself must be writable by `www-data`, not just the database
file.

### 1.9 Check the schema BEFORE you start the service

```bash
cd /opt/ebwa
sudo -u www-data ./venv/bin/flask --app app check-schema
```

**Do not skip this, and do not start the service first.** The code is at
today's version; the archive is from whenever it was taken. If a column
has been added since, the restored database does not have it and the
site 500s on the first request that touches it — for every visitor, until
somebody works out why.

`check-schema` is read-only and either prints

```
Schema is up to date: 25 tables, all columns present, 6 indexes present.
```

— in which case go to 1.10 — or names exactly what is missing and hands
you the statement:

```
MISSING COLUMNS (1):
  - campaign.image_position
  Fix (check each against DEPLOY.md first):
    ALTER TABLE campaign ADD COLUMN image_position VARCHAR(20) NOT NULL DEFAULT 'lead';

Schema is BEHIND the code. Apply the above BEFORE restarting the app.
```

**Check each one against `DEPLOY.md` before running it.** That file is
the record of every schema change with the exact statement, newest at the
top; the suggestion above is generated from the models and is a very good
guess, but DEPLOY.md is what was actually applied. Work down from the
oldest entry newer than your archive.

```bash
sudo -u www-data sqlite3 /opt/ebwa/instance/ebwa.db \
  "ALTER TABLE campaign ADD COLUMN image_position VARCHAR(20) NOT NULL DEFAULT 'lead';"
```

If DEPLOY.md's entry also says `init-db` (a new table, or new seeded
content blocks), run that too — it only ever creates what is missing and
never drops or alters:

```bash
cd /opt/ebwa && sudo -u www-data ./venv/bin/flask --app app init-db
```

Then run `check-schema` again. Repeat until it is clean. **A missing
INDEX is reported too, and is not urgent** — the site runs without it,
only slower — but apply the `CREATE INDEX` while you are here.

### 1.10 Start, and verify properly

```bash
sudo systemctl start ebwa
sudo systemctl status ebwa --no-pager
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8011/healthz
```

`200` means it is up. **That is not the same as the restore having
worked** — go to [Did the restore actually
work?](#did-the-restore-actually-work) and do it now, while you still
have the staging copies.

### 1.11 Tidy up, afterwards

Once everyone agrees the site is right — days later, not minutes:

```bash
sudo rm -rf /var/tmp/ebwa-restore /var/tmp/ebwa-backup-*.zip
# and, when you are certain, the pre-restore copy:
sudo rm -rf /var/tmp/ebwa-before-restore-*
```

Take a fresh backup so the next restore starts from the fixed state —
**Settings → Back up now**, or:

```bash
cd /opt/ebwa && sudo -u www-data ./venv/bin/flask --app app backup-now
```

---

## Scenario 2 — server lost, rebuilding from nothing

### Read this first: three things must come together, and only two are in the backup

| Piece | Where it lives | In the backup? |
| --- | --- | --- |
| **The code** | GitHub | No — and it does not need to be |
| **The data** | The archive on the NAS | **Yes** |
| **The secrets** | `/etc/ebwa/env` on the machine you have lost | **NO. Deliberately.** |

The environment file is not in any backup **on purpose**. The archive
contains the database, the archive is copied to the NAS, and a key stored
inside the thing it protects is not a key — it is a copy of the lock left
in the door. So the archive is safe to hold, safe to move, and worth
nothing on its own to whoever gets hold of it.

The cost of that decision is this: **if `/etc/ebwa/env` is not in a
credential store off the machine, part of this rebuild is not
recoverable.** Not "difficult" — not recoverable.

Two variables in particular:

- **`FERNET_KEY`** encrypts, at rest, the SMTP password and the NAS
  password that a super admin typed on the Settings page. Lose it and
  those two stay in the database as ciphertext nobody can read again.
  The site still runs; the passwords are gone and must be reset from
  scratch at the mail provider and the NAS. **The database cannot help
  you here — the key was never in it.**
- **`SECRET_KEY`** signs the login session cookie. A new one is fine to
  generate, and the only consequence is that everybody signed in is
  signed out. Do **not** reuse an old one you are unsure of.

Also in that file and equally not in the backup: `STRIPE_SECRET_KEY` and
`STRIPE_WEBHOOK_SECRET` (recoverable — from the Stripe dashboard) and
`SMTP_PASSWORD` (resettable at the mail provider).

> **THE CHICKEN AND EGG, and it catches people.** To fetch the archive
> from the NAS you need the NAS login. The NAS password the site uses is
> encrypted in the database — which is inside the archive you cannot yet
> reach — with a key that was on the server you have lost. **The NAS
> credentials must be in the credential store in their own right**, next
> to `FERNET_KEY`. If they are not, you need whoever administers the NAS
> to let you in another way.

**The single point of failure in a full rebuild is `/etc/ebwa/env`.**
Everything else here is a procedure. That file is either somewhere else
or it is not. If you are reading this on a calm day: go and check that it
is, and that somebody other than one person can get at it.

### 2.1 Provision the machine

Ubuntu LTS, the same as before. If the address has changed, leave DNS
until 2.10 — do the work first and cut over when it is proven.

### 2.2 Packages, code, virtualenv

Follow **README → VPS deployment**, which is the authoritative setup and
is not repeated here. In summary:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip nginx sqlite3 unzip git
sudo mkdir -p /opt/ebwa && sudo chown $USER /opt/ebwa
git clone <the GitHub URL> /opt/ebwa
cd /opt/ebwa
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

`requirements.txt` is the whole dependency list — Pillow, paramiko,
cryptography, psutil, stripe, pyotp, qrcode. Do not install piecemeal.

**Do not run `init-db` or `create-admin` yet.** You are about to restore
a database that already has both, and seeding first only muddies what you
then have to reconcile.

### 2.3 Recreate the environment file

From the credential store, not from memory.

```bash
sudo mkdir -p /etc/ebwa
sudo tee /etc/ebwa/env >/dev/null <<'EOF'
SECRET_KEY='...'
FERNET_KEY='...'
STRIPE_SECRET_KEY='...'
STRIPE_WEBHOOK_SECRET='...'
SMTP_HOST='...'
SMTP_PORT='587'
SMTP_USER='...'
SMTP_PASSWORD='...'
MAIL_FROM='...'
MAIL_TO='...'
EOF
sudo chown root:www-data /etc/ebwa/env
sudo chmod 640 /etc/ebwa/env
```

Single quotes around every value — they protect `$`, `#`, `!` and
spaces. `640 root:www-data` means the service can read it and nobody else
can. `.env.example` in the repo lists every variable the app reads and is
the complete inventory.

**If `FERNET_KEY` is genuinely lost**, generate a new one and accept what
that means:

```bash
./venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The site will run. The stored SMTP and NAS passwords are unreadable, and
Settings will show them as set while failing to use them — so clear and
retype both after 2.9, having reset them at the provider and the NAS.

### 2.4 Restore the data

Get the archive onto the machine (from the NAS, or from wherever your
off-machine copy is), then follow **[Scenario 1, steps 1.4 to
1.8](#14-look-inside-it-before-you-trust-it)** — inspect, unpack to
staging, copy the database and uploads into place, delete the stale
`-wal`/`-shm`, fix ownership. Those steps are identical here.

On a fresh machine `/opt/ebwa/instance/` may not exist:

```bash
sudo mkdir -p /opt/ebwa/instance /opt/ebwa/static/uploads /opt/ebwa/backups
```

There is nothing to copy aside (1.2) — the machine is new — and nothing
to stop (1.1), since the service does not exist yet.

### 2.5 Check the schema

```bash
cd /opt/ebwa
sudo -u www-data ./venv/bin/flask --app app check-schema
```

Same rules as [1.9](#19-check-the-schema-before-you-start-the-service).
More likely to find something here, because the code you have just cloned
is at HEAD while the archive may be weeks old. Work through DEPLOY.md
from the oldest entry newer than the archive.

### 2.6 systemd

`/etc/systemd/system/ebwa.service`, exactly as in the README:

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

```bash
sudo chown -R www-data:www-data /opt/ebwa/instance /opt/ebwa/static/uploads /opt/ebwa/backups
sudo systemctl daemon-reload
sudo systemctl enable --now ebwa
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8011/healthz
```

Gunicorn binds to `127.0.0.1` only. Keep it that way — the app is
unreachable except through nginx, which is what the single trusted proxy
hop assumes.

### 2.7 nginx, then certbot

The server block is in **README → VPS deployment**. Two things in it are
load-bearing and easy to lose in a rebuild:

- the four `proxy_set_header` lines. Without them every request looks
  like it came from 127.0.0.1, so the audit log records nothing useful
  and the whole internet shares one rate-limit bucket;
- `expires 30d` on `/static/`, which is safe **only** because the app
  appends a content hash to those URLs.

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d ebwa.org.uk -d www.ebwa.org.uk
```

Certbot needs DNS to be pointing here already, so if the address has
changed, do [2.10](#210-dns-if-the-address-changed) first and come back.

### 2.8 Tailscale

Needed for the NAS transfer, not for the website.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale status | head
```

The VPS must be able to reach the NAS by its tailnet name. Approve the
new machine in the Tailscale admin if your tailnet requires it — a
rebuilt VPS is a *new* device, even with the same hostname.

### 2.9 Cron

None of this is a scheduler inside the app, deliberately — gunicorn runs
two workers and a timer in each would mean two of everything.

```cron
*/15 * * * * cd /opt/ebwa && set -a && . /etc/ebwa/env && set +a && ./venv/bin/flask --app app run-scheduled-backup >> /var/log/ebwa-backup.log 2>&1
5 3 * * *  cd /opt/ebwa && ./venv/bin/flask --app app aggregate-pageviews
20 7 * * * cd /opt/ebwa && ./venv/bin/flask --app app send-monthly-report
```

Install as the user the DEPLOY.md entries specify, and copy them from
DEPLOY.md rather than from here if the two ever disagree — that file is
the record.

> Note the backup line sources `/etc/ebwa/env` and the other two do not.
> That is how they are recorded, and it matters: `run-scheduled-backup`
> needs `FERNET_KEY` to decrypt the NAS password. If
> `send-monthly-report` ever stops finding the mail server, give it the
> same `set -a && . /etc/ebwa/env && set +a` prefix — its SMTP settings
> fall back to environment variables when they are not typed on the
> Settings page.

Then prove the backup path end to end rather than waiting for the
schedule:

```bash
cd /opt/ebwa && sudo -u www-data ./venv/bin/flask --app app backup-now
```

and check **Settings → Backups** says the archive reached the NAS.

### 2.10 DNS, if the address changed

Point the A/AAAA records at the new machine, wait for propagation, then
run certbot. Two more things belong to this step:

- **Stripe**: the webhook endpoint is registered against a URL. If the
  domain changed, update it in the Stripe dashboard
  (`https://<domain>/stripe/webhook`, listening for
  `checkout.session.completed`) and put the new signing secret into
  `/etc/ebwa/env` as `STRIPE_WEBHOOK_SECRET`. Payments stay "pending"
  until the webhook confirms them, so a wrong secret looks like donations
  silently not completing.
- **Mail**: if SPF or DKIM name the old host, outgoing mail starts going
  to spam without any error you would notice. Use **Settings → Email →
  Send a test email**.

Finish with [Did the restore actually
work?](#did-the-restore-actually-work).

---

## Did the restore actually work?

**The service starting is not the test.** It starts happily with an empty
database. Do all of this.

### In the database

```bash
cd /opt/ebwa
sudo -u www-data ./venv/bin/flask --app app check-schema     # must be clean
sudo -u www-data ./venv/bin/flask --app app check-uploads    # must be clean
```

`check-uploads` names every database row pointing at a file that is not
on disk, across every column that holds one. It is the direct test of
"did the photos come back as well as the rows", and it exits 1 when it
finds any. A clean run prints:

```
Every image reference has a file: nothing dangling.
```

Then count what should be there. Compare against what you know, or
against the figures on the dashboard before the incident:

```bash
sudo -u www-data sqlite3 /opt/ebwa/instance/ebwa.db "
  SELECT 'events',       COUNT(*) FROM event
  UNION ALL SELECT 'news',         COUNT(*) FROM news_post
  UNION ALL SELECT 'photos',       COUNT(*) FROM gallery_image
  UNION ALL SELECT 'albums',       COUNT(*) FROM gallery_album
  UNION ALL SELECT 'testimonials', COUNT(*) FROM testimonial
  UNION ALL SELECT 'enquiries',    COUNT(*) FROM contact_message
  UNION ALL SELECT 'members',      COUNT(*) FROM membership_application
  UNION ALL SELECT 'payments',     COUNT(*) FROM payment
  UNION ALL SELECT 'admins',       COUNT(*) FROM user;"
```

**Look hard at the last three.** Payments and membership applications are
people's money and personal data; if those numbers are lower than you
expect, stop and work out why before anybody adds anything new on top.
And **`admins` must not be 0** — if it is, the archive predates the
accounts, and you need `create-admin` (below).

### On the site

- **The homepage loads** and the sections are in the right order.
- **A photograph appears** — on the homepage and in the gallery. Rows
  restoring without files is the classic half-restore, which is what
  `check-uploads` catches, but look anyway.
- **Open one event and one news post** and check the body text and the
  photos are there, not just the titles.
- **`/gallery/all`** — every photograph should be reachable there.
- **Sign in to `/admin`.** If nobody can:
  ```bash
  cd /opt/ebwa
  sudo -u www-data ./venv/bin/flask --app app reset-admin-password
  # or, if there is no account at all:
  sudo -u www-data ./venv/bin/flask --app app create-admin
  ```
- **Save something trivial** — edit a page-content block and save it.
  This is the test that ownership in 1.8 was right; a read-only site
  looks perfect until somebody presses Save.
- **The dashboard** — the figures should look like the site you know.
- **Settings → Backups** — take one now, and confirm it reaches the NAS.
  A restored site with a broken backup path is one incident from the
  same afternoon repeating.
- **Settings → Email → Send a test email**, if mail matters that day.

### Then say so

Write down, in `DEPLOY.md` or wherever the client record lives: what was
lost, which archive was used, what its timestamp was, what was still
missing afterwards, and anything created between the archive and the
incident that will have to be re-entered by hand. **That last one is the
part people forget** — a restore rolls the site back to the archive, so
anything added in between is gone and somebody has to be told which
afternoon's work to do again.

---

## Reading an archive without restoring it

To check what a backup holds — or to recover one deleted item rather than
rolling the whole site back — without touching production.

**List the contents:**

```bash
unzip -l /opt/ebwa/backups/ebwa-backup-20260826-023000.zip | head -30
```

**Pull just the database out and query it:**

```bash
cd /var/tmp
unzip -o -j /opt/ebwa/backups/ebwa-backup-20260826-023000.zip \
      database/ebwa.db -d /var/tmp/peek
sqlite3 /var/tmp/peek/ebwa.db "SELECT COUNT(*) FROM event;"
sqlite3 /var/tmp/peek/ebwa.db "SELECT id, title, event_date FROM event ORDER BY id DESC LIMIT 10;"
```

That copy is inert — nothing points at it and nothing is running against
it. Read it, copy the text of the one thing somebody deleted, paste it
back into the live admin by hand, and delete `/var/tmp/peek`. For one
event or one news post that is quicker and far safer than a full restore,
and it costs nobody else their afternoon's work.

**Check a specific photo is in there:**

```bash
unzip -l /opt/ebwa/backups/ebwa-backup-20260826-023000.zip | grep 3f2a1b
```

### What the archive contains, and what it does not

**Contains:**

- `database/ebwa.db` — the whole database, taken through SQLite's own
  backup API so it is a consistent snapshot even though the site was
  serving while it was made. All content, all settings, every admin
  account, the audit log, enquiries, membership applications, payment
  records and Gift Aid declarations.
- `uploads/` — every uploaded file from `static/uploads/`: photographs,
  partner logos, video posters, and the generated `-thumb` versions.
- `README.txt` — a short note on what it is and how to put it back.

**Does not contain, and you must not expect it to:**

- **`/etc/ebwa/env` — the secrets.** Deliberate, and the reason
  [Scenario 2](#read-this-first-three-things-must-come-together-and-only-two-are-in-the-backup)
  opens the way it does.
- **The code.** That is what GitHub is for. An archive is data.
- **The venv or installed packages** — rebuilt from
  `requirements.txt`.
- **nginx config, the systemd unit, certbot's certificates, crontabs.**
  All in the README; all quick to recreate; none of them in here.
- **Other archives.** `backups/` is not backed up into itself.
- **`instance/` as a folder** — only `ebwa.db` from inside it. The
  `-wal` and `-shm` are not in the archive **and should not be**: the
  snapshot already includes everything they held.

**Because the database is in it, an archive is personal data** — names,
addresses, enquiries, donation records. Treat every copy the way you
would treat the live database: encrypted at rest where you can, never on
a personal laptop, never in email, never in the repository.

---

## Rehearsing a restore safely

A restore procedure nobody has run is a guess. Rehearse it against a
scratch copy — this touches nothing live and takes about ten minutes.

**On a laptop or any spare machine**, not the VPS:

```bash
git clone <the GitHub URL> ~/ebwa-rehearsal
cd ~/ebwa-rehearsal
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
mkdir -p instance static/uploads backups
```

Copy a real archive into `~/ebwa-rehearsal/` and restore it the same way
Scenario 1 does — but with everything pointed at the scratch copy:

```bash
unzip -q ebwa-backup-20260826-023000.zip -d /tmp/rehearse
cp /tmp/rehearse/database/ebwa.db instance/ebwa.db
rm -f instance/ebwa.db-wal instance/ebwa.db-shm
cp -a /tmp/rehearse/uploads/. static/uploads/
./venv/bin/flask --app app check-schema
./venv/bin/flask --app app check-uploads
./venv/bin/flask --app app run --port 5001
```

Then open `http://127.0.0.1:5001`, sign in, and look at the pages listed
in [Did the restore actually work?](#did-the-restore-actually-work).

**Four rules for a rehearsal:**

1. **Never point it at `/opt/ebwa`.** Give it its own directory. If you
   are ever unsure which database a command is about to touch, set
   `DATABASE_URL` explicitly and remove all doubt.
2. **Do not put the real secrets in it.** No `FERNET_KEY`, no Stripe
   keys, no SMTP password. A rehearsal does not need to send mail or take
   money, and the point is to test the procedure, not to make a second
   copy of the credentials.
3. **Delete it afterwards**, database and uploads together. It holds
   real people's enquiries and donation records — see the note above.
4. **Do it after a schema change**, not only when it occurs to you. That
   is precisely when an old archive stops restoring cleanly, and finding
   that out during a rehearsal is the entire point.

**Rehearse the awkward one too.** Copy the archive somewhere, drop a
recent column from the copy, and run `check-schema` against it — so that
the first time you see `Schema is BEHIND the code` is not the day you
needed the site back in a hurry.

---

## Quick reference

```bash
# stop / start
sudo systemctl stop ebwa
sudo systemctl start ebwa
sudo systemctl status ebwa --no-pager
sudo journalctl -u ebwa -n 50 --no-pager

# is it alive
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8011/healthz

# archives
ls -lat /opt/ebwa/backups/
unzip -l /opt/ebwa/backups/<archive>.zip | head -20

# the four checks, all read-only except backup-now
cd /opt/ebwa
sudo -u www-data ./venv/bin/flask --app app check-schema
sudo -u www-data ./venv/bin/flask --app app check-uploads
sudo -u www-data ./venv/bin/flask --app app test-mail
sudo -u www-data ./venv/bin/flask --app app backup-now

# ownership, after any restore
sudo chown -R www-data:www-data /opt/ebwa/instance /opt/ebwa/static/uploads
```

**Netbus IT Support** maintain this site. If a restore is going wrong
rather than merely slowly, stop and ring them — the pre-restore copy from
[1.2](#12-copy-the-current-state-aside--before-anything-else) means
nothing has been made worse yet, and that is worth far more than pressing
on.
