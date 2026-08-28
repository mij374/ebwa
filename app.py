"""
EBWA Community Website + CMS
Flask + SQLite. Admin can edit page content/images, manage events and gallery.

First run:
    pip install -r requirements.txt
    flask --app app init-db
    flask --app app create-admin admin@ebwa.org.uk
    flask --app app run --debug

After every deploy, before restarting (see DEPLOY.md):
    flask --app app check-schema
"""
import base64
import hashlib
import hmac
import io
import json
import os
import re
import platform
import secrets
import shutil
import smtplib
import socket
import sqlite3
import subprocess
import ssl
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from email.message import EmailMessage
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from functools import wraps
from zoneinfo import ZoneInfo

import click
import pyotp
import qrcode
import qrcode.image.svg
import stripe
from PIL import Image, ImageOps
from cryptography.fernet import Fernet

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, abort, g, jsonify, send_from_directory,
                   has_request_context, session as flask_session)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Local development only: read a .env file sitting beside this one, so a
# developer can keep SECRET_KEY, FERNET_KEY, the SMTP password and the
# Stripe TEST keys in a file instead of exporting shell variables.
#
# It happens BEFORE anything reads os.environ below, does nothing when
# there is no .env, and never overrides a variable that is already set —
# so the VPS, which gets its environment from systemd's EnvironmentFile,
# behaves exactly as it did. Production secrets live in /etc/ebwa/env and
# nowhere near this repository.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             ".env"), override=False)
except ImportError:
    # python-dotenv is in requirements.txt, but a deploy that pulls the
    # code before pip install must still start rather than crash on an
    # import — it simply reads the environment it was given.
    pass

# Where this install lives on its server. NOTHING reads or writes these
# paths — they exist so the admin can print accurate instructions (the
# "how to change the SMTP password" box on Settings). A deployment
# somewhere else sets these variables and the instructions follow it
# instead of confidently telling somebody the wrong path.
DEPLOY_ENV_FILE = os.environ.get("DEPLOY_ENV_FILE", "/etc/ebwa/env")
DEPLOY_PATH = os.environ.get("DEPLOY_PATH", "/opt/ebwa")
DEPLOY_SERVICE = os.environ.get("DEPLOY_SERVICE", "ebwa")
DEPLOY_USER = os.environ.get("DEPLOY_USER", "www-data")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MAX_UPLOAD_MB = 8

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "instance", "ebwa.db"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Admin sessions expire after 20 minutes of INACTIVITY. Flask signs the
# session cookie with a timestamp and refuses it once it is older than
# PERMANENT_SESSION_LIFETIME; SESSION_REFRESH_EACH_REQUEST re-issues the
# cookie on every request, so the clock restarts each time someone does
# something and only genuine idleness ends the session.
IDLE_SESSION_MINUTES = 20
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
    minutes=IDLE_SESSION_MINUTES)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

# In production gunicorn sits behind nginx, so every request arrives from
# 127.0.0.1. Without this the audit log records the proxy instead of the
# caller, and — worse — the whole internet shares one rate-limit bucket.
#
# EXACTLY ONE HOP (nginx), never an arbitrary forwarded chain: ProxyFix
# takes the RIGHTMOST value of each header, which is the one nginx itself
# appended via `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`.
# Anything a client forges into the header sits to the left of that and is
# ignored. Raise these numbers only if a real extra proxy is added in
# front, and never let the app be reachable except through nginx.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Stripe keys from env vars only — never committed (CLAUDE.md donations rules)
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "admin_login"


# SQLite tuning: WAL mode lets readers keep reading while a write happens,
# and busy_timeout makes a second writer wait briefly instead of erroring.
from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine


@sa_event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
    except Exception:
        pass  # non-SQLite engines (e.g. PostgreSQL) ignore this


# ---------------------------------------------------------------- models
ROLES = ("admin", "super_admin")


class User(UserMixin, db.Model):
    """Site admin. role 'admin' is the client's own admins; 'super_admin'
    is Netbus only (feature flags / settings). A future board-member tier
    becomes another value here — never a second user table.
    """
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="admin")
    # Two-factor authentication — per user and optional. The secret is
    # server-side only: it reaches the authenticator app via the enrolment
    # QR and is never put in a session, cookie or form field.
    totp_secret = db.Column(db.String(64), default="")     # base32
    totp_enabled = db.Column(db.Boolean, nullable=False, default=False)
    totp_last_counter = db.Column(db.Integer)   # replay guard, see verify_totp
    # Nullable, unlike the created_at on content models: accounts that
    # predate this column keep NULL and show as "—" in the admin list.
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def is_super_admin(self):
        return self.role == "super_admin"


class RecoveryCode(db.Model):
    """One single-use 2FA recovery code, stored hashed like a password.

    Codes are shown to the user exactly once, at enrolment. Spending one
    stamps used_at rather than deleting the row, so there is a record
    that it was used.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    user = db.relationship("User")
    code_hash = db.Column(db.String(255), nullable=False)
    used_at = db.Column(db.DateTime)      # NULL = still usable
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC


class FeatureFlag(db.Model):
    """On/off switch for one optional module, seeded from FEATURES.

    Switching a feature off only hides it: no content is deleted, and
    switching it back on restores the pages exactly as they were.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), unique=True, nullable=False)   # FEATURES name
    enabled = db.Column(db.Boolean, nullable=False, default=True)


class AuditLog(db.Model):
    """One recorded admin action. APPEND-ONLY BY DESIGN.

    There is deliberately no route, helper or CLI command that edits or
    deletes an entry — a log that can be tidied up afterwards is not a
    log. Add nothing here that writes to an existing row.

    user_id is nullable because a failed login has no user, and
    user_email is a snapshot rather than a join so entries stay
    meaningful after an account is deleted.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    user_email = db.Column(db.String(120), nullable=False, default="")
    action = db.Column(db.String(40), nullable=False)      # short verb
    entity_type = db.Column(db.String(40))                 # model name
    entity_id = db.Column(db.Integer)
    summary = db.Column(db.Text, default="")
    ip = db.Column(db.String(45), default="")              # fits IPv6
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC

    # This table only ever grows — nothing prunes an append-only log —
    # and two things read it on a schedule: the dashboard's failed
    # sign-in count and the health panel's counts for today, the week
    # and the month. Both filter on (action, created_at), so without
    # this they are table scans that get slower every day the site is
    # used. The log list orders by created_at on its own, hence the
    # second one.
    # NOTE: create_all() does NOT add an index to a table that already
    # exists, so an existing database needs the CREATE INDEX statements
    # in DEPLOY.md. check-schema compares columns, not indexes, and will
    # not tell you they are missing.
    __table_args__ = (
        db.Index("ix_auditlog_action_created", "action", "created_at"),
        db.Index("ix_auditlog_created", "created_at"),
    )


# ===================================================================
# VISITOR STATISTICS
# ===================================================================
# Counted here, on this server, with NO third-party analytics and NO
# extra cookie. The cookie notice and the privacy notice both tell
# visitors there is no tracking, and that has to stay literally true —
# so nothing below identifies anybody, and nothing below can be turned
# back into a person.
#
# WHAT IS STORED PER PAGE LOAD: the path, the DAY (not the time), and a
# one-way hash. That is all.
#
# WHY A SALTED DAILY HASH RATHER THAN AN IDENTIFIER. To say "how many
# people" rather than "how many page loads" you have to be able to tell
# two loads by the same person apart from two by different people. The
# obvious ways all store something about the visitor: an IP address is
# personal data under the UK GDPR, a user agent plus an IP is close to
# a fingerprint, and a cookie would be a new cookie the notice does not
# mention and PECR would want consent for. So instead:
#
#   sha256(salt_for_today + ip + user_agent) -> 64 hex characters
#
# The IP and the user agent are used in the request and never written
# anywhere. The hash cannot be reversed. THE SALT IS RANDOM AND IS
# REPLACED EVERY DAY, and the old one is overwritten rather than kept —
# so the moment the day turns, yesterday's hashes cannot be recomputed
# even by somebody holding the database and a list of candidate
# addresses. That is what stops the counts being correlated from one
# day to the next: the same visitor gets an unrelated hash tomorrow.
#
# WHAT THAT DOES NOT CLAIM, said plainly rather than glossed: while
# today's salt exists, somebody with both the database and a specific IP
# and user agent in mind could test whether that combination visited
# today. That is inherent in counting same-day uniqueness at all, it
# lasts until the next rotation, and it is the reason the salt is
# generated fresh rather than derived from anything (a salt derived from
# the date would be reproducible for ever, which is the failure this
# design exists to avoid).
STATS_REPORT_KEY = "stats_report_enabled"
STATS_REPORT_TO_KEY = "stats_report_to"
STATS_TARGET_KEY = "stats_monthly_target"
STATS_REPORT_ACTION = "stats_report"
# From the client's brief. A target, not a promise — it is there so the
# report can say "ahead" or "behind" rather than leaving a trustee to do
# the arithmetic in their head.
STATS_TARGET_DEFAULT = 2000
ORG_NAME_KEY = "org_name"
ORG_CHARITY_NO_KEY = "org_charity_number"
VISITOR_HASH_LENGTH = 64          # sha256 hex
# How long the per-page-load rows are kept before they are folded into
# daily totals and deleted. 62 days covers "this month" and the whole of
# last month, which is as far back as the per-page figures are ever
# shown; the daily totals are what answer "the same month last year".
#
# CONFIGURABLE, within a range, because the trade is a real one and it
# depends on the site: a row costs about 460 bytes with its three
# indexes (measured), so at 200 views a day the window is 2.6MB at 30
# days and 32MB at 365. What it buys is how far back "most visited
# pages" can look.
#
# THE DAILY TOTALS ARE NOT CONFIGURABLE AND MUST NOT BECOME SO. They
# cost about 70KB a year, they are the whole of the year-on-year figure,
# and the raw rows they were computed from are long gone — a control
# that can delete them is a control somebody will use by accident. See
# the note on PageViewDaily.
PAGEVIEW_RAW_DAYS = 62
PAGEVIEW_RAW_MIN = 30
PAGEVIEW_RAW_MAX = 365
STATS_RAW_DAYS_KEY = "stats_raw_days"
# Measured: one PageView row plus its share of ix_pageview_day,
# ix_pageview_day_visitor and ix_pageview_day_path.
PAGEVIEW_ROW_BYTES = 460


def human_bytes(n):
    """A size somebody can read, for helper text rather than a table."""
    for unit, step in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= step:
            value = n / step
            return "%.0f%s" % (value, unit) if value >= 10                 else "%.1f%s" % (value, unit)
    return "%d bytes" % n


def pageview_raw_days():
    """The configured window, or the default if nobody has set one."""
    block = Block.query.filter_by(key=STATS_RAW_DAYS_KEY).first()
    try:
        value = int((block.value or "").strip()) if block else 0
    except ValueError:
        value = 0
    if not PAGEVIEW_RAW_MIN <= value <= PAGEVIEW_RAW_MAX:
        return PAGEVIEW_RAW_DAYS
    return value


class PageView(db.Model):
    """ONE PAGE LOAD. Nothing here identifies a person — see above.

    `day` is a DATE, not a timestamp, and it is the UK local day: the
    admin figures are read by somebody in Enfield, per the date
    convention. Storing the hour would make the rows more revealing
    without making any figure on the page more useful.
    """
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, nullable=False)
    path = db.Column(db.String(255), nullable=False)
    # sha256(daily salt + ip + user agent). Not an identifier: see the
    # note above for what it can and cannot be used for.
    visitor = db.Column(db.String(VISITOR_HASH_LENGTH), nullable=False,
                        default="")

    # This table grows faster than anything else here — one row per page
    # load rather than one per admin action — so every query the panel
    # runs has to be an index seek. (day) answers the daily chart and
    # the totals, and is the prefix of both others; (day, visitor) is
    # the COUNT(DISTINCT) for "how many people"; (day, path) is the
    # most-visited list. create_all() makes these with the table, but a
    # database that somehow has the table without them needs the
    # statements in DEPLOY.md.
    __table_args__ = (
        db.Index("ix_pageview_day", "day"),
        db.Index("ix_pageview_day_visitor", "day", "visitor"),
        db.Index("ix_pageview_day_path", "day", "path"),
    )


class PageViewDaily(db.Model):
    """One day's totals. KEPT FOR EVER — nothing prunes this table.

    Written by `aggregate-pageviews` when a day passes out of the raw
    window. The raw rows are the disposable half; these are the point of
    keeping anything at all, and the year-on-year figure is the one
    number that cannot be recovered once it is gone — the rows it would
    be recomputed from were deleted two months after they were written.

    So: NEVER ADD A PRUNE HERE, and be suspicious of anything that
    offers to tidy the table up. Five years costs about 350KB (measured:
    196 bytes a day including the unique index), against 6.5MB for the
    raw table's 62-day window at 200 views a day. The history is roughly
    five per cent of the working set and it is what makes the panel
    worth having.

    Deliberately holds NO paths: a day's per-page figures are only ever
    shown for the current month, so keeping them for years would be
    storing more than anything asks for.
    """
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, nullable=False, unique=True)
    views = db.Column(db.Integer, nullable=False, default=0)
    visitors = db.Column(db.Integer, nullable=False, default=0)


class VisitorSalt(db.Model):
    """The salt for one day. ONE ROW, overwritten in place.

    Shared through the database rather than held in memory because
    gunicorn runs several workers: a per-worker salt would give the same
    visitor a different hash in each worker and count them several
    times. Overwritten rather than appended so yesterday's salt is
    genuinely gone.
    """
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, nullable=False)
    salt = db.Column(db.String(64), nullable=False)


# The three rich-content layouts, shared by every content type.
CONTENT_LAYOUTS = ("classic", "gallery", "alternating")
CONTENT_LAYOUT_LABELS = (
    ("classic", "Classic — a lead photo beside the text, the rest in a "
                "strip underneath"),
    ("gallery", "Gallery — the text first, then the photos in a grid"),
    ("alternating", "Alternating — text and photos side by side, swapping "
                    "sides down the page"),
)


ABOUT_VIDEO_POSITION_KEY = "about_video_position"

# WHERE THE VIDEO SITS in an item's content. Three fixed places, not an
# arbitrary order: EBWA has one video and asked where it goes, and one
# video has three sensible answers. If somebody ever wants TWO, or a
# video BETWEEN two photographs, this is the wrong shape and the right
# answer is to make a video another ordered attachment on ContentImage —
# see the note in CLAUDE.md before reaching for a fourth position here.
#
# 'lead' is first and is the default, so nothing moves for content that
# already has a video.
VIDEO_POSITIONS = (
    ("lead", "At the top",
     "Before the words. What a video has always done."),
    ("after_text", "After the text",
     "Below the words, above any photographs."),
    ("end", "At the end",
     "After everything, including the photographs."),
)
VIDEO_POSITION_KEYS = tuple(k for k, _l, _d in VIDEO_POSITIONS)
VIDEO_POSITION_DEFAULT = VIDEO_POSITIONS[0][0]

# The collection page's own picture takes the SAME THREE PLACES. Same
# keys deliberately — one set of positions, one validator, one order to
# reason about — with wording that suits a photograph rather than a
# video. Only campaigns have this: everywhere else photographs are
# ContentImage attachments laid out by a preset, and their order is the
# preset's business.
IMAGE_POSITIONS = (
    ("lead", "At the top",
     "Before the words. Where the picture has always been."),
    ("after_text", "After the text",
     "Below the description, above the figures."),
    ("end", "At the end",
     "Below the payment form, at the foot of the page."),
)
assert tuple(k for k, _l, _d in IMAGE_POSITIONS) == VIDEO_POSITION_KEYS

# WHEN THE PICTURE AND THE VIDEO ARE IN THE SAME PLACE, THE VIDEO GOES
# FIRST. Not a template accident — it is what 'lead' has always done on
# this page (video, then image), so an admin who moves both to the same
# slot gets the order they already know rather than a new one. It is
# also the more useful way round: the video is the thing somebody came
# to watch, and the photograph reads as the still beneath it.
MEDIA_ORDER = ("video", "image")


def clean_media_position(value):
    """A stored or submitted position, or the default.

    Anything unrecognised falls back to 'lead' rather than raising: a
    hand-edited row or a form from an older deploy should put the media
    where it has always been, not break the page.
    """
    return (value if value in VIDEO_POSITION_KEYS
            else VIDEO_POSITION_DEFAULT)


# The old name, kept because it reads correctly at the video call sites.
clean_video_position = clean_media_position


class ContentImage(db.Model):
    """An image attached to a piece of content, by owner_type + owner_id.

    Generic on purpose: one table, one admin partial and one rendering
    macro serve About, news posts, events and milestones, rather than a
    photo table and a bespoke template per module. Singleton owners — the
    About page, which is Blocks rather than a row — use owner_id 0.
    """
    id = db.Column(db.Integer, primary_key=True)
    owner_type = db.Column(db.String(40), nullable=False)  # see CONTENT_OWNERS
    owner_id = db.Column(db.Integer, nullable=False, default=0)
    filename = db.Column(db.String(255), nullable=False)   # uploads filename
    caption = db.Column(db.String(300), default="")
    alt_text = db.Column(db.String(300), nullable=False, default="")
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC

    __table_args__ = (
        db.Index("ix_content_image_owner", "owner_type", "owner_id"),
    )


class Block(db.Model):
    """A named editable content block (text or image) used on public pages."""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)   # e.g. 'home_hero_title'
    label = db.Column(db.String(160), nullable=False)             # human name shown in admin
    kind = db.Column(db.String(10), nullable=False, default="text")  # 'text' | 'image'
    value = db.Column(db.Text, default="")                        # text content or image filename
    group = db.Column(db.String(40), default="general")           # admin grouping: home/about/contact...
    sort = db.Column(db.Integer, default=0)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(220), unique=True, nullable=False)
    event_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.String(20), default="")     # free text: "6:30 PM"
    venue = db.Column(db.String(200), default="")
    summary = db.Column(db.String(300), default="")       # short line for listing cards
    description = db.Column(db.Text, default="")          # full details, paragraphs
    image = db.Column(db.String(255), default="")         # uploads filename
    published = db.Column(db.Boolean, default=True)
    layout = db.Column(db.String(20), nullable=False, default="classic")
    # A YouTube or Vimeo link, validated on the way in — never raw
    # markup — and the poster fetched to our own uploads folder so that
    # nothing third-party is contacted until a visitor presses play.
    # See parse_video_url() and fetch_video_poster().
    video_url = db.Column(db.String(300), default="")
    video_thumb = db.Column(db.String(255), default="")   # uploads filename
    # Where the video sits in this item: see VIDEO_POSITIONS.
    # server_default matches the Python one — every existing
    # video was in the lead slot, which is what 'lead' means.
    video_position = db.Column(db.String(20), nullable=False,
                               default=VIDEO_POSITION_DEFAULT,
                               server_default="lead")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC

    @property
    def is_past(self):
        return self.event_date < date.today()


class NewsPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(220), unique=True, nullable=False)
    published_date = db.Column(db.Date, nullable=False)
    summary = db.Column(db.String(300), default="")       # short line for listing cards
    body = db.Column(db.Text, default="")                 # full article, paragraphs
    image = db.Column(db.String(255), default="")         # uploads filename
    published = db.Column(db.Boolean, default=True)
    layout = db.Column(db.String(20), nullable=False, default="classic")
    # A YouTube or Vimeo link, validated on the way in — never raw
    # markup — and the poster fetched to our own uploads folder so that
    # nothing third-party is contacted until a visitor presses play.
    # See parse_video_url() and fetch_video_poster().
    video_url = db.Column(db.String(300), default="")
    video_thumb = db.Column(db.String(255), default="")   # uploads filename
    # Where the video sits in this item: see VIDEO_POSITIONS.
    # server_default matches the Python one — every existing
    # video was in the lead slot, which is what 'lead' means.
    video_position = db.Column(db.String(20), nullable=False,
                               default=VIDEO_POSITION_DEFAULT,
                               server_default="lead")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC


class GalleryAlbum(db.Model):
    """A named set of gallery photos — an event, a trip, a year.

    Deleting an album never deletes photographs: they go back to being
    unfiled and stay reachable through "All photos". An album is an
    arrangement of the gallery, and an arrangement is not worth losing
    irreplaceable pictures over.
    """
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(220), unique=True, nullable=False)
    description = db.Column(db.String(300), default="")
    cover_image = db.Column(db.String(255), default="")   # uploads filename
    sort = db.Column(db.Integer, default=0)
    published = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC


class GalleryImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    caption = db.Column(db.String(200), default="")
    # NULL = unfiled; every photo predating albums starts here and stays
    # reachable through the "All photos" view.
    album_id = db.Column(db.Integer, db.ForeignKey("gallery_album.id"))
    album = db.relationship("GalleryAlbum")
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Testimonial(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(160), default="")      # e.g. "Parent, Bengali school"
    quote = db.Column(db.Text, nullable=False)
    published = db.Column(db.Boolean, default=True)
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Service(db.Model):
    """A "What we do" card on the homepage.

    icon is a single emoji character typed straight into the admin form —
    the cards have always rendered emoji, and a whole icon library would
    be a build step this site deliberately does not have.
    """
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.String(300), default="")
    icon = db.Column(db.String(16), default="")
    sort = db.Column(db.Integer, default=0)
    published = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC


PARTNER_MODES = ("text", "image", "both")

# How the partner row moves, chosen once for the site on the partners
# admin page rather than per partner — it is a property of the row, not
# of any one organisation. Every mode falls back to no movement under
# prefers-reduced-motion; that is not a fourth setting and must not
# become one.
# How a scrolling row moves. Shared by the partner row and the
# testimonial row, which use one marquee between them.
ROW_MOTIONS = (
    ("scroll", "Continuous smooth scroll",
     "The row glides from right to left and loops. Pauses when the "
     "pointer is over it, when something in it has keyboard focus, and "
     "while it is being dragged."),
    ("step", "Step every few seconds",
     "The row advances one card at a time, right to left, and waits "
     "in between."),
    ("none", "No movement",
     "A still row that can be scrolled or dragged. The scrollbar stays "
     "visible in this mode, because nothing else says there is more."),
)
PARTNER_MOTIONS = ROW_MOTIONS          # the name the partner page uses
PARTNER_MOTION_KEY = "partners_motion"
PARTNER_STEP_KEY = "partners_step_seconds"
PARTNER_STEP_DEFAULT = 4
PARTNER_STEP_MIN, PARTNER_STEP_MAX = 1, 60
# ---- the two SPEED settings, which are a different question from the
# two above: those say what the row does, these say how fast it does it.
# Both ship with the value the row already had, so a deploy changes
# nothing until somebody deliberately changes it, and both can be put
# back with one button (admin_partner_motion_reset).
#
# How long ONE step takes, against how often a step happens. 360ms is
# not a taste: it is what Chromium's own smooth scroll took for a
# 278px stride, measured at 1440, 1024, 390 and 360 (344-374ms). The
# step used to hand the browser `behavior: 'smooth'` and get whatever
# that engine felt like — which is unknowable, differs between engines
# and cannot be a setting — so it is our own glide now, at the duration
# the browser was already using.
PARTNER_GLIDE_KEY = "partners_step_glide_ms"
PARTNER_GLIDE_DEFAULT = 360
PARTNER_GLIDE_MIN, PARTNER_GLIDE_MAX = 300, 3000
# The continuous drift, in PIXELS A SECOND — the unit the row is
# actually driven in, and the only one that means the same thing on
# every site. A duration would have to be a duration of something, and
# the only candidate is one lap of the row, which changes with the
# number of partners: the same "8 seconds" would be a gentle drift with
# five partners and a blur with twenty. Pixels a second is the same
# speed whatever is in the row, so the admin page translates it instead
# ("about one partner card every six seconds").
PARTNER_DRIFT_KEY = "partners_drift_speed"
PARTNER_DRIFT_DEFAULT = 45
PARTNER_DRIFT_MIN, PARTNER_DRIFT_MAX = 10, 200
# One place naming both, for the settings page, the reset and the audit
# entry: (key, default, min, max, label).
PARTNER_SPEEDS = (
    (PARTNER_GLIDE_KEY, PARTNER_GLIDE_DEFAULT,
     PARTNER_GLIDE_MIN, PARTNER_GLIDE_MAX, "step glide"),
    (PARTNER_DRIFT_KEY, PARTNER_DRIFT_DEFAULT,
     PARTNER_DRIFT_MIN, PARTNER_DRIFT_MAX, "drift speed"),
)

# ---- the two rows that use the marquee, and their settings
# SEPARATE settings rather than one shared set, on the grounds that the
# two rows are read differently: partner logos are glanced at, and
# testimonials are somebody's WORDS, which a visitor has to read. Moving
# text is harder to read than a moving logo, so one setting would force
# a compromise on whichever row lost. The bounds and the speed defaults
# are the same for both, so nothing has to be decided twice — only the
# DEFAULT MODE differs, and only because of the same reasoning:
# testimonials start still.
#
# Everything below is keyed by row name. Adding a third row means adding
# an entry here, a settings form, and the two thin routes at the bottom
# of the admin section — not another copy of any of this.
MOTION_ROWS = {
    "partners": {
        "label": "partner",            # for flashes and audit entries
        "mode_key": PARTNER_MOTION_KEY,
        "step_key": PARTNER_STEP_KEY,
        "glide_key": PARTNER_GLIDE_KEY,
        "drift_key": PARTNER_DRIFT_KEY,
        "default_mode": "scroll",
        "back_to": "admin_partners",
    },
    "testimonials": {
        "label": "testimonial",
        "mode_key": "testimonials_motion",
        "step_key": "testimonials_step_seconds",
        "glide_key": "testimonials_step_glide_ms",
        "drift_key": "testimonials_drift_speed",
        # STILL by default. A quote is read, not glanced at, and a row
        # of moving text is a row nobody finishes a sentence in — so the
        # testimonial row ships as a still row with arrows, and an admin
        # who wants it drifting turns that on deliberately. It is one
        # value here if that judgement is ever reversed.
        "default_mode": "none",
        "back_to": "admin_testimonials",
    },
}
# The threshold at which a row stops being a grid and becomes a
# scroller. Quote cards are far wider than logo tiles, so three fill a
# row where it takes five logos.
ROW_SCROLLER_MIN = {"partners": 5, "testimonials": 4}
# How many quotes the homepage shows. It was six, from when the section
# was a grid of three by two and a seventh would have started a lonely
# third row. The row scrolls now, so that constraint is gone and this is
# simply how far a visitor can scroll before it repeats.
#
# IT IS ALSO THE ONLY PLACE TESTIMONIALS APPEAR: there is no public page
# listing them all, so a quote past this number is published and
# invisible. Twelve rather than "all of them" because the homepage is
# still a homepage — but if EBWA ever collects more than a dozen, the
# answer is a page of their own rather than a bigger number here.
HOME_TESTIMONIALS = 12


class Partner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    url = db.Column(db.String(300), default="")
    blurb = db.Column(db.String(300), default="")
    logo = db.Column(db.String(255), default="")      # uploads filename
    # 'text' (name + blurb, the original look), 'image' (logo only) or
    # 'both'. A NEW partner defaults to the logo, because that is what a
    # partner row is usually for and it is the tidier wall of cards. Only
    # the Python-side default changed: the column's own default stays
    # 'text', so no existing row moves — and a partner whose mode says
    # logo but has none still falls back to the name (`shows_logo`), so
    # the default can never produce an empty card.
    display_mode = db.Column(db.String(10), nullable=False, default="image",
                             server_default="text")
    sort = db.Column(db.Integer, default=0)

    @property
    def shows_logo(self):
        """A logo-ish mode only counts if there is actually a logo."""
        return bool(self.logo) and self.display_mode in ("image", "both")

    @property
    def shows_text(self):
        return not self.shows_logo or self.display_mode == "both"


class Resource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(80), nullable=False)   # e.g. "Council services"
    description = db.Column(db.String(300), default="")
    phone = db.Column(db.String(40), default="")
    url = db.Column(db.String(300), default="")
    sort = db.Column(db.Integer, default=0)


class Faq(db.Model):
    """One question and its answer on the public FAQ page.

    `answer` holds paragraphs the same way `about_body` and event
    descriptions do — split on newlines when rendering. Category is
    optional: with none, the question sits in the ungrouped run at the
    top rather than under an invented heading.
    """
    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.String(300), nullable=False)
    answer = db.Column(db.Text, nullable=False, default="")
    category = db.Column(db.String(80), default="")   # optional grouping
    sort = db.Column(db.Integer, default=0)
    published = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC


class Milestone(db.Model):
    """An entry on the public Our Journey page (milestones + funded work).

    Funder fields are optional. Institutional funders only (councils,
    trusts, foundations) — individual donors must NEVER be published
    here without documented consent.
    """
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    summary = db.Column(db.String(300), default="")   # short line for the card
    outcome = db.Column(db.Text, default="")          # what it achieved
    funder_name = db.Column(db.String(160), default="")
    amount_pence = db.Column(db.Integer)              # NULL = none / not disclosed
    funder_url = db.Column(db.String(300), default="")
    image = db.Column(db.String(255), default="")     # uploads filename
    sort = db.Column(db.Integer, default=0)
    published = db.Column(db.Boolean, default=True)
    layout = db.Column(db.String(20), nullable=False, default="classic")
    # A YouTube or Vimeo link, validated on the way in — never raw
    # markup — and the poster fetched to our own uploads folder so that
    # nothing third-party is contacted until a visitor presses play.
    # See parse_video_url() and fetch_video_poster().
    video_url = db.Column(db.String(300), default="")
    video_thumb = db.Column(db.String(255), default="")   # uploads filename
    # Where the video sits in this item: see VIDEO_POSITIONS.
    # server_default matches the Python one — every existing
    # video was in the lead slot, which is what 'lead' means.
    video_position = db.Column(db.String(20), nullable=False,
                               default=VIDEO_POSITION_DEFAULT,
                               server_default="lead")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC


class Subscriber(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


MEMBERSHIP_STATUSES = ("new", "contacted", "approved", "declined")


class MembershipApplication(db.Model):
    """Personal data — admin-only, never shown in public templates.

    bangladeshi_origin is SPECIAL-CATEGORY data (ethnic origin): admin-only,
    excluded from any CSV export unless explicitly needed, covered by the
    privacy notice on the form.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(40), default="")
    address = db.Column(db.String(300), default="")
    reason = db.Column(db.Text, default="")           # why they want to join
    # Eligibility declarations — all four must be ticked to apply
    over_18 = db.Column(db.Boolean, nullable=False, default=False)
    bangladeshi_origin = db.Column(db.Boolean, nullable=False, default=False)
    lives_works_enfield = db.Column(db.Boolean, nullable=False, default=False)
    fee_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(20), default="new")  # see MEMBERSHIP_STATUSES
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class BackupRun(db.Model):
    """One run of the backup command, successful or not.

    History matters more than the archive list on disk: retention deletes
    old archives, but the record that a backup ran — or failed — stays.
    """
    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC
    finished_at = db.Column(db.DateTime)
    filename = db.Column(db.String(255), default="")
    size_bytes = db.Column(db.Integer, default=0)
    file_count = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default="running")  # running|ok|failed
    reason = db.Column(db.String(40), default="manual")   # manual|cli|scheduled
    error = db.Column(db.Text, default="")
    # Getting the archive to the NAS is part of THIS run, not a second
    # concept: one row answers "did we back up, and did it leave the
    # building?" — the only two questions anybody asks.
    transfer_status = db.Column(db.String(20), default="none")
    # none | pending | ok | failed
    remote_filename = db.Column(db.String(255), default="")
    transfer_error = db.Column(db.Text, default="")
    transfer_attempts = db.Column(db.Integer, default=0)
    transferred_at = db.Column(db.DateTime)


MESSAGE_STATUSES = ("new", "read", "replied")


class ContactMessage(db.Model):
    """An enquiry sent through the form on /contact.

    Personal data — admin-only, never rendered on a public page, and no
    CSV export: there is no reason to bulk-download somebody's question
    about a lunch club. Reading the list, changing a status and deleting
    one are all recorded in the audit log.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(40), default="")
    subject = db.Column(db.String(200), default="")
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="new")   # MESSAGE_STATUSES
    ip = db.Column(db.String(45), default="")          # fits IPv6
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC


class Campaign(db.Model):
    """An event collection (e.g. seaside trip) donors/attendees pay toward."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(220), unique=True, nullable=False)
    description = db.Column(db.Text, default="")
    image = db.Column(db.String(255), default="")          # uploads filename
    target_pence = db.Column(db.Integer)                   # optional target amount
    fee_pence = db.Column(db.Integer)                      # fixed price per place, if any
    # A YouTube or Vimeo link, validated on the way in — never raw
    # markup — and the poster fetched to our own uploads folder so that
    # nothing third-party is contacted until a visitor presses play.
    # See parse_video_url() and fetch_video_poster().
    video_url = db.Column(db.String(300), default="")
    video_thumb = db.Column(db.String(255), default="")   # uploads filename
    # Where the video sits in this item: see VIDEO_POSITIONS.
    # server_default matches the Python one — every existing
    # video was in the lead slot, which is what 'lead' means.
    video_position = db.Column(db.String(20), nullable=False,
                               default=VIDEO_POSITION_DEFAULT,
                               server_default="lead")
    # `image` does TWO JOBS: the cover on the listing cards and the
    # homepage strip, and a picture on the collection's own page. This
    # says whether it does the second one. A page with its own video, or
    # with photographs of its own, may not want the cover repeated on it
    # — but the cover is not optional, so this switch cannot touch it.
    show_image_on_page = db.Column(db.Boolean, nullable=False, default=True,
                                   server_default="1")
    # ...and WHERE it sits when it is shown, the same three places the
    # video has (IMAGE_POSITIONS). Only the page picture moves; the
    # cover is not positioned, it is a card.
    image_position = db.Column(db.String(20), nullable=False,
                               default=VIDEO_POSITION_DEFAULT,
                               server_default="lead")
    # VISIBILITY AND TRADING ARE TWO QUESTIONS, and `active` answered both
    # at once: closing a collection took it off the website, so the trip
    # that was paid for and ran vanished the moment it stopped selling
    # places. See CAMPAIGN_STATES.
    state = db.Column(db.String(20), nullable=False, default="open",
                      server_default="hidden")
    # LEGACY. Nothing reads this any more; `state` is the only authority.
    # It is kept in step in the one place state is written, purely so
    # that a database opened by hand does not contradict the website.
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC

    @property
    def is_public(self):
        """On the website at all — taking payments or closed."""
        return self.state in PUBLIC_CAMPAIGN_STATES

    @property
    def takes_payments(self):
        return self.state == "open"

    @property
    def contributor_count(self):
        """How many people paid. A COUNT, never len() of a fetched list."""
        return db.session.query(db.func.count(Payment.id)).filter(
            Payment.campaign_id == self.id,
            Payment.status == "complete").scalar()

    @property
    def raised_pence(self):
        """Running total of completed payments (fee + donation)."""
        return db.session.query(
            db.func.coalesce(db.func.sum(Payment.fee_pence
                                         + Payment.donation_pence), 0)
        ).filter(Payment.campaign_id == self.id,
                 Payment.status == "complete").scalar()

    @property
    def target_percent(self):
        if not self.target_pence:
            return None
        return min(100, self.raised_pence * 100 // self.target_pence)


# What a collection is DOING, which is two questions and not one. The
# old `active` boolean answered both with the same tick, so an admin who
# closed a finished trip also deleted it from the website — the day out
# that ran, that people paid for and that the funder asked about was
# simply gone. Ticking it back on to show it would have reopened the
# payment form.
#   open   — on the site, taking payments. What `active` used to mean.
#   closed — on the site as a RECORD: the final total, how many people
#            gave, and a line saying it has finished. No payment form,
#            and the route refuses a POST as well as hiding the form.
#   hidden — not on the public site at all: no card, no detail page, not
#            in the sitemap. What unticking `active` used to mean.
# Order matters: it is the order of the admin's dropdown, and the first
# entry is what a new collection gets.
CAMPAIGN_STATES = (
    ("open", "Taking payments",
     "On the website, with the payment form."),
    ("closed", "Closed — still on the website",
     "Shown with its final total and a line saying it has finished. "
     "Nobody can pay."),
    ("hidden", "Hidden",
     "Off the public website entirely. Contributor lists and Gift Aid "
     "records stay here in the admin."),
)
CAMPAIGN_STATE_LABELS = {k: label for k, label, _d in CAMPAIGN_STATES}
# The two a visitor can reach. Keep the ORDER — open before closed is
# the order the collections page renders its two sections in.
PUBLIC_CAMPAIGN_STATES = ("open", "closed")


class Payment(db.Model):
    """A Stripe payment. campaign_id NULL = general donation to the charity.

    HMRC Gift Aid rule, modelled structurally (CLAUDE.md — do not relax):
    fee_pence pays for a benefit and can NEVER carry Gift Aid; only
    donation_pence (a genuine gift) may. General donations are 100%
    donation_pence. The CHECK constraints make violating rows impossible.
    Payer/declaration data is personal data — admin-only, never public.
    """
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"))
    campaign = db.relationship("Campaign")
    name = db.Column(db.String(120), default="")
    email = db.Column(db.String(200), default="")
    fee_pence = db.Column(db.Integer, nullable=False, default=0)
    donation_pence = db.Column(db.Integer, nullable=False, default=0)
    gift_aid = db.Column(db.Boolean, nullable=False, default=False)
    gift_aid_name = db.Column(db.String(120), default="")
    gift_aid_address = db.Column(db.String(200), default="")   # house name/number
    gift_aid_postcode = db.Column(db.String(20), default="")
    stripe_session_id = db.Column(db.String(255), unique=True)
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending | complete
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC

    __table_args__ = (
        # Gift Aid only ever on a real donation portion, never a fee alone
        db.CheckConstraint("NOT (gift_aid = 1 AND donation_pence <= 0)",
                           name="gift_aid_requires_donation"),
        # General donations (no campaign) are 100% donation
        db.CheckConstraint("campaign_id IS NOT NULL OR fee_pence = 0",
                           name="general_donation_no_fee"),
    )

    @property
    def total_pence(self):
        return self.fee_pence + self.donation_pence

    @property
    def gift_aid_pence(self):
        """The only amount Gift Aid may be claimed on — never the fee."""
        return self.donation_pence if self.gift_aid else 0


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------- helpers
def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


def unique_slug(model, title, obj_id=None):
    base = slugify(title)
    slug, n = base, 2
    while True:
        q = model.query.filter_by(slug=slug)
        if obj_id:
            q = q.filter(model.id != obj_id)
        if not q.first():
            return slug
        slug = "%s-%d" % (base, n)
        n += 1


# ------------------------------------------------------- image pipeline
# Every upload is normalised on the way in: orientation applied, EXIF
# dropped, anything huge scaled down, and a 600px thumbnail written
# alongside it. Phone photos are the reason — they arrive at 4000px and
# several megabytes, sideways, with the street the photograph was taken
# in recorded in the EXIF GPS tags.
MAX_IMAGE_WIDTH = 1600     # full-size ceiling; plenty for a hero at 2x
THUMB_WIDTH = 600          # listing cards, grids and admin previews
JPEG_QUALITY = 82
THUMB_SUFFIX = "-thumb"


def thumb_name(filename):
    """The thumbnail filename belonging to a stored upload."""
    if not filename:
        return ""
    stem, _dot, ext = filename.rpartition(".")
    return "%s%s.%s" % (stem or filename, THUMB_SUFFIX, ext)


def is_thumb(filename):
    stem = filename.rpartition(".")[0] or filename
    return stem.endswith(THUMB_SUFFIX)


def _has_alpha(im):
    return (im.mode in ("RGBA", "LA")
            or (im.mode == "P" and "transparency" in im.info))


def _is_animated(im):
    return getattr(im, "n_frames", 1) > 1


def _encode(im, fmt):
    """Encode an image to bytes, carrying no metadata of any kind."""
    buf = io.BytesIO()
    if fmt == "JPEG":
        im = im.convert("RGB")
        im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True,
                progressive=True)
    elif fmt == "PNG":
        im.save(buf, "PNG", optimize=True)
    else:                              # WEBP
        im.save(buf, "WEBP", quality=JPEG_QUALITY, method=6)
    return buf.getvalue()


def _scaled(im, width):
    """A copy no wider than `width`, aspect ratio preserved."""
    if im.width <= width:
        return im
    height = max(1, round(im.height * width / im.width))
    return im.resize((width, height), Image.LANCZOS)


def open_upload(raw):
    """Decode bytes to an image with its orientation applied and its EXIF
    gone, or None if this is not an image we can read.

    exif_transpose() rotates the pixels the way the camera meant them to
    be seen; dropping the tags afterwards takes the GPS coordinates with
    it, which is the point.
    """
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return None
    if not _is_animated(im):
        im = ImageOps.exif_transpose(im) or im
    im.info.pop("exif", None)
    return im


def process_image(raw, ext):
    """Normalise an uploaded image.

    Returns (ext, full_bytes, thumb_bytes_or_None), or None if the bytes
    are not a readable image.

    An image with transparency stays PNG so logos keep their transparent
    background; anything else becomes a progressive JPEG, unless the
    original format encodes it smaller. An animated GIF is passed through
    untouched rather than flattened to its first frame, and gets no
    thumbnail — the original is what every view shows.
    """
    im = open_upload(raw)
    if im is None:
        return None
    if _is_animated(im):
        return ext, raw, None

    had_exif = bool(im.getexif())
    full = _scaled(im, MAX_IMAGE_WIDTH)     # `im` itself when it fits
    too_wide = full is not im

    # Already small enough, carrying nothing private, and in a format we
    # would not change anyway: leave the bytes exactly as uploaded.
    # Re-encoding here could only cost quality — a photo that arrived at
    # quality 75 does not get better by being saved again at 82.
    settled = ext in ("jpg", "gif") or _has_alpha(im)
    if not too_wide and not had_exif and settled:
        best_ext, best = ext, raw
    else:
        if _has_alpha(full):
            best_ext, best = "png", _encode(full, "PNG")
        else:
            best_ext, best = "jpg", _encode(full, "JPEG")
            if ext in ("png", "webp"):
                # A flat graphic often survives better, and smaller, in
                # its own format than as a JPEG. Let the bytes decide.
                same = _encode(full, "PNG" if ext == "png" else "WEBP")
                if len(same) < len(best):
                    best_ext, best = ext, same
        # Converting formats has to earn its place: a tenth off is worth
        # having, a rounding error is not.
        if not too_wide and not had_exif and len(best) > len(raw) * 0.9:
            best_ext, best = ext, raw

    thumb = None
    if im.width > THUMB_WIDTH:
        small = _scaled(full, THUMB_WIDTH)
        thumb = _encode(small, "PNG" if _has_alpha(small) else "JPEG")
    return best_ext, best, thumb


def store_upload(file_storage):
    """Validate, optimise and store an uploaded image.

    Returns `(filename, None)`, or `(None, "why not")` — the reason as a
    sentence somebody can act on, and NOT flashed here.

    `save_upload()` below is the flashing wrapper every ordinary admin
    form uses, and is unchanged from its callers' point of view. This
    half exists because the gallery's one-photo-per-request upload has to
    REPORT each failure back to a script: a flash is written for a page
    that is about to be drawn, and in that path no page is drawn until
    every file has been through here. A failure that only flashes is a
    failure the person watching a progress bar never hears about.
    """
    if not file_storage or not file_storage.filename:
        return None, None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXT:
        return None, ("Image must be one of: "
                      + ", ".join(sorted(ALLOWED_EXT)))

    raw = file_storage.read()
    if not raw:
        return None, "That file was empty — please choose an image."

    processed = process_image(raw, "jpg" if ext == "jpeg" else ext)
    if processed is None:
        return None, ("That file could not be read as an image. Please "
                      "upload a JPG, PNG, WebP or GIF photo.")

    final_ext, data, thumb = processed
    stem = uuid.uuid4().hex
    name = "%s.%s" % (stem, final_ext)
    with open(os.path.join(UPLOAD_DIR, secure_filename(name)), "wb") as fh:
        fh.write(data)
    if thumb:
        with open(os.path.join(UPLOAD_DIR,
                               secure_filename(thumb_name(name))), "wb") as fh:
            fh.write(thumb)
    return name, None


def save_upload(file_storage):
    """Store an uploaded image, flashing the reason when it will not go.

    The signature every admin form has always used: the stored filename,
    or None with the reason already on its way to the next page. An empty
    field is not a failure and says nothing.
    """
    name, why = store_upload(file_storage)
    if why:
        flash(why, "error")
    return name


def delete_upload(filename):
    """Remove a stored upload and its thumbnail."""
    if not filename:
        return
    for name in (filename, thumb_name(filename)):
        path = os.path.join(UPLOAD_DIR, secure_filename(name))
        if os.path.isfile(path):
            os.remove(path)


# ---- cache busting for the site's own static files
# nginx serves /static/ with `expires 30d`, which is right for a file
# whose URL changes when the file does and wrong for one that does not:
# without a version in the URL, a returning visitor keeps last month's
# stylesheet for up to a month after a deploy. That does not look like a
# caching problem to anybody — it looks like the deploy broke the site,
# on their phone only, while it is plainly fine on yours. Hence a token
# in the query string, and hence `expires 30d` stays exactly as it is.
_asset_cache = {}              # filename -> ((mtime, size), token)


@app.template_global("asset_version")
def asset_version(filename):
    """A short token for a static file, or None if it is not there.

    CONTENT, not the clock: the first eight hex of the file's sha256,
    cached per worker on (mtime, size) exactly like `aspect_ratio_of`,
    so it is one read per file per change and a stat thereafter.

    Content rather than mtime because the token has to hold STILL
    between deploys for the caching to be worth anything, and an mtime
    does not: a fresh clone, a rebuilt server, an rsync without -t or a
    stray `touch` all restamp a file whose bytes never moved, and every
    visitor re-downloads the lot for nothing. Two identical files have
    one token whatever the filesystem thinks.

    None when the file is missing — Werkzeug drops a None query value,
    so a typo costs the cache busting on that one URL and nothing else.
    A template must never 500 over a version string.
    """
    if not filename:
        return None
    path = os.path.join(app.static_folder, *filename.split("/"))
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (stat.st_mtime, stat.st_size)
    cached = _asset_cache.get(filename)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            token = hashlib.sha256(fh.read()).hexdigest()[:8]
    except OSError:
        return None
    _asset_cache[filename] = (key, token)
    return token


# The homepage alternates plain and tinted bands. Which band a section
# gets is a property of WHERE IT LANDS, not of which section it is.
BAND_TINTED = "tinted"

# THE HOMEPAGE SECTIONS: key, the name an admin sees, and what it holds.
# The key is also the partial's filename (templates/home/_<key>.html) and
# the only thing the include path is ever built from — which is why every
# helper below filters against THIS tuple and returns nothing that is not
# in it. A template path assembled from anything a request could
# influence is not a thing to have on a site that takes card payments.
#
# ADDING A SECTION: an entry here, a partial in templates/home/, and the
# content behind it in home(). Nothing else. It appears at the END of
# the order for anyone who had already saved one, and is shown by
# default — see the two helpers for why round that way.
HOME_SECTIONS = (
    ("services", "What we do",
     "The cards describing EBWA's work."),
    ("events", "Upcoming events",
     "The next three published events."),
    ("news", "Latest news",
     "The three most recent news posts."),
    ("collections", "Collections",
     "Collections that are open and taking payments."),
    ("testimonials", "What our community says",
     "The quote row."),
    ("partners", "Our partners",
     "The partner logo row."),
)
HOME_SECTION_KEYS = tuple(k for k, _n, _d in HOME_SECTIONS)
HOME_SECTION_NAMES = {k: n for k, n, _d in HOME_SECTIONS}
HOME_ORDER_KEY = "home_section_order"
HOME_HIDDEN_KEY = "home_sections_hidden"
HOME_ORDER_DEFAULT = ",".join(HOME_SECTION_KEYS)
# The default order in plain language, for the reset button's caption:
# the keys are for the code, and nobody arranging a front page should be
# reading "collections,testimonials".
HOME_ORDER_DEFAULT_NAMES = [n for _k, n, _d in HOME_SECTIONS]


def _block_list(key):
    """A stored comma-separated setting, as a list of trimmed strings."""
    block = Block.query.filter_by(key=key).first()
    raw = (block.value if block else "") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def home_section_order():
    """The section keys in the order the homepage should emit them.

    Two fallbacks, and both are about the same failure: A SECTION MUST
    NOT DISAPPEAR BECAUSE SOMEBODY EDITED A LIST.

      * a key in the setting that is no longer a section is IGNORED, so
        removing a section from the code does not leave the page trying
        to include a partial that is not there;
      * a section that exists but is missing from the setting is
        APPENDED, so it still renders. The alternative — showing only
        what the setting names — means a typo, a half-saved form or a
        new section added in a deploy silently takes content off the
        front page, and nobody would connect the two.

    An empty or absent setting therefore gives exactly HOME_SECTIONS in
    its own order, which is what the site shipped with.
    """
    seen, order = set(), []
    for key in _block_list(HOME_ORDER_KEY):
        if key in HOME_SECTION_KEYS and key not in seen:
            seen.add(key)
            order.append(key)
    order += [k for k in HOME_SECTION_KEYS if k not in seen]
    return order


def home_hidden_sections():
    """Section keys switched off FOR THE HOMEPAGE ONLY.

    Stored as the hidden ones rather than the shown ones, deliberately.
    Storing "shown" would mean a section added in a later deploy is
    absent from every site that had ever saved this setting — invisible
    on the front page from the moment it shipped, and looking like a bug
    in the new feature rather than in this list.

    This is NOT a feature flag. The flag removes a module from the whole
    site — its page, its nav link, its sitemap entry. This takes one
    section off the front page and leaves everything else exactly where
    it was.
    """
    return {k for k in _block_list(HOME_HIDDEN_KEY) if k in HOME_SECTION_KEYS}


def home_layout(present):
    """[(key, band class)] for the sections that will actually render.

    `present` maps a section key to whether it has anything to show. A
    section renders when it has content AND has not been switched off
    for the homepage; the bands then alternate across whatever is left,
    in whatever order Settings puts them in.
    """
    hidden = home_hidden_sections()
    visible = [(key, bool(present.get(key)) and key not in hidden)
               for key in home_section_order()]
    bands = alternating_bands(visible)
    return [(key, bands[key]) for key, on in visible if on]


@app.template_global("alternating_bands")
def alternating_bands(sections):
    """name -> "" or "tinted", counting only the sections that RENDER.

    `sections` is [(name, is_visible), ...] in the order the page emits
    them. The classes used to be written into the template one by one,
    which is correct only when every section is on the page: hide one
    — a feature flag off, or simply no upcoming events this month — and
    the two either side of the gap come out with the same background,
    so the page has a seam in it where a band quietly doubles in height.
    Five of the six can disappear, in any combination, which is 32 ways
    for a hardcoded sequence to be wrong and one way for it to be right.

    Skipped sections take no turn, so the visible ones alternate however
    many are missing. The first visible section is always PLAIN, which
    is what it was when nothing was hidden.

    A section not named here gets "" — plain, and visibly wrong beside
    another plain one rather than silently shifting everything after it.
    That is deliberate: forgetting to list a new section should look
    like a mistake, not like a different design.
    """
    bands, shown = {}, 0
    for name, visible in sections:
        if not visible:
            bands[name] = ""
            continue
        bands[name] = BAND_TINTED if shown % 2 else ""
        shown += 1
    return bands


@app.template_global("upload_url")
def upload_url(filename):
    """Full-size upload — detail views and anything that fills a page."""
    return url_for("static", filename="uploads/" + filename)


@app.template_global("thumb_url")
def thumb_url(filename):
    """The 600px variant for cards, grids and admin previews.

    Falls back to the original when there is no thumbnail: an upload
    small enough not to need one, an animated GIF, or a file that
    predates `reprocess-images` having been run.
    """
    if not filename:
        return ""
    small = thumb_name(filename)
    if os.path.isfile(os.path.join(UPLOAD_DIR, secure_filename(small))):
        return url_for("static", filename="uploads/" + small)
    return upload_url(filename)


@app.template_filter("pounds")
def pounds_filter(pence):
    """Render pence as £: 1500 -> £15, 1250 -> £12.50, 300000 -> £3,000."""
    if pence is None:
        return ""
    if pence % 100 == 0:
        return "£{:,}".format(pence // 100)
    return "£{:,.2f}".format(pence / 100.0)


def parse_pounds(raw):
    """Parse a pounds amount like '10' or '10.50' into pence, else None."""
    try:
        pounds = Decimal(raw.strip())
    except (InvalidOperation, AttributeError):
        return None
    pence = pounds * 100
    if pence != int(pence):        # more than two decimal places
        return None
    return int(pence)


def blocks_for(group):
    rows = (Block.query.filter_by(group=group)
            .order_by(Block.sort, Block.id).all())
    return {b.key: b.value for b in rows}


def row_motion(row):
    """How a marquee row should move, ready for the template.

    One helper for both rows — see MOTION_ROWS. Falls back to the
    defaults when the Blocks are missing (a database that has not had
    init-db run yet) or hold something unexpected, so the homepage can
    never be broken by a bad value in a settings row.
    """
    conf = MOTION_ROWS[row]
    rows = blocks_for(row)
    mode = (rows.get(conf["mode_key"]) or "").strip()
    if mode not in [m for m, _label, _help in ROW_MOTIONS]:
        mode = conf["default_mode"]
    def _number(key, default, low, high):
        """A stored number, clamped, or the default if it is nonsense.

        The homepage must never be broken by a settings row, so every
        one of these falls back rather than raising — the same rule the
        mode above follows.
        """
        try:
            value = int((rows.get(key) or "").strip())
        except ValueError:
            return default
        return max(low, min(high, value))

    seconds = _number(conf["step_key"], PARTNER_STEP_DEFAULT,
                      PARTNER_STEP_MIN, PARTNER_STEP_MAX)
    glide = _number(conf["glide_key"], PARTNER_GLIDE_DEFAULT,
                    PARTNER_GLIDE_MIN, PARTNER_GLIDE_MAX)
    drift = _number(conf["drift_key"], PARTNER_DRIFT_DEFAULT,
                    PARTNER_DRIFT_MIN, PARTNER_DRIFT_MAX)
    # A glide longer than the gap between steps would have the row
    # starting its next move before it finished the last one. The form
    # refuses that combination, but a database can hold anything — an
    # older row, a hand edit — so the page that renders it caps it too.
    glide = min(glide, seconds * 1000)
    return {"mode": mode, "step_seconds": seconds,
            "glide_ms": glide, "drift_speed": drift,
            "glide_default": PARTNER_GLIDE_DEFAULT,
            "drift_default": PARTNER_DRIFT_DEFAULT,
            "row": row}


def partner_motion():
    """The partner row's settings — the name the rest of this file and
    the partner tests already use."""
    return row_motion("partners")


# ------------------------------------------------------- rich content
# One generic attachment table plus one set of helpers, so a content type
# opts in by naming itself here and rendering the shared macro. 'about' is
# the page itself (Blocks, no row), so it uses owner_id 0 and keeps its
# layout in a Block; the rest keep theirs in a `layout` column.
CONTENT_OWNERS = {
    "about": None,
    "news_post": None,      # filled in below, once the models exist
    "event": None,
    "milestone": None,
}
ABOUT_LAYOUT_KEY = "about_layout"
# Blocks the plain content editor must not show as a text box
HIDDEN_BLOCK_KEYS = (ABOUT_LAYOUT_KEY, "about_video_url",
                     "about_video_thumb", ABOUT_VIDEO_POSITION_KEY, "site_mail_to", "smtp_host",
                     "smtp_port", "smtp_user", "smtp_security", "smtp_from",
                     "smtp_password_enc",
                     "security_alert_email", "site_security_alert_to",
                     "sftp_enabled", "sftp_host", "sftp_port", "sftp_user",
                     "sftp_password_enc", "sftp_remote_path",
                     "sftp_schedule", "sftp_schedule_tz", "sftp_keep",
                     PARTNER_MOTION_KEY, PARTNER_STEP_KEY,
                     PARTNER_GLIDE_KEY, PARTNER_DRIFT_KEY,
                     HOME_ORDER_KEY, HOME_HIDDEN_KEY,
                     STATS_REPORT_KEY, STATS_REPORT_TO_KEY,
                     STATS_TARGET_KEY, STATS_RAW_DAYS_KEY,
                     ) + tuple(
    MOTION_ROWS["testimonials"][k]
    for k in ("mode_key", "step_key", "glide_key", "drift_key"))


class LegacyLeadImage:
    """The old single `about_image` Block, shaped like a ContentImage so
    the macro can render it without a special case."""

    def __init__(self, filename, alt_text):
        self.id = None
        self.filename = filename
        self.alt_text = alt_text
        self.caption = ""
        self.sort = 0


def images_for(owner_type, owner_id=0):
    return (ContentImage.query
            .filter_by(owner_type=owner_type, owner_id=owner_id)
            .order_by(ContentImage.sort, ContentImage.id).all())


def attach_image(owner_type, owner_id, file_storage, alt_text,
                 caption="", sort=0):
    """Store an upload and attach it. Returns (image, error_message).

    Alt text is required, not optional: an image nobody can describe is
    an image a screen reader user simply loses.
    """
    alt_text = (alt_text or "").strip()
    if not alt_text:
        return None, ("Please describe the image in the alt text box — it "
                      "is what people using a screen reader hear.")
    if not file_storage or not file_storage.filename:
        return None, "Please choose an image file."
    name = save_upload(file_storage)      # flashes its own type/size error
    if not name:
        return None, None
    img = ContentImage()
    img.owner_type = owner_type
    img.owner_id = owner_id
    img.filename = name
    img.alt_text = alt_text[:300]
    img.caption = (caption or "").strip()[:300]
    img.sort = sort
    db.session.add(img)
    db.session.commit()
    return img, None


def _upload_still_referenced(filename, excluding_id=None):
    """True if some other row still points at this file — another
    attachment, or a Block (the legacy single About image, which stays
    put so the flag-off view keeps working)."""
    q = ContentImage.query.filter_by(filename=filename)
    if excluding_id is not None:
        q = q.filter(ContentImage.id != excluding_id)
    if q.first():
        return True
    return Block.query.filter_by(kind="image",
                                 value=filename).first() is not None


def delete_content_image(img):
    """Detach an image, and delete its file unless something else uses it."""
    filename, img_id = img.filename, img.id
    db.session.delete(img)
    db.session.commit()
    if not _upload_still_referenced(filename, excluding_id=img_id):
        delete_upload(filename)


def delete_images_for(owner_type, owner_id):
    """Cascade for a deleted owner: attachments and their files go too."""
    gone = images_for(owner_type, owner_id)
    for img in gone:
        delete_content_image(img)
    return len(gone)


def layout_for(owner_type, owner_id=0):
    """The chosen preset, always one of CONTENT_LAYOUTS."""
    if owner_type == "about":
        block = Block.query.filter_by(key=ABOUT_LAYOUT_KEY).first()
        value = block.value if block else ""
    else:
        model = CONTENT_OWNERS.get(owner_type)
        obj = db.session.get(model, owner_id) if model else None
        value = getattr(obj, "layout", "") if obj else ""
    return value if value in CONTENT_LAYOUTS else "classic"


ABOUT_VIDEO_KEYS = ("about_video_url", "about_video_thumb")

def video_settings_for(owner_type, owner_id=0):
    """This owner's stored (url, thumb) — a row's columns, or About's
    Blocks, exactly as layout_for does it for the layout."""
    if owner_type == "about":
        rows = {b.key: (b.value or "") for b in Block.query
                .filter(Block.key.in_(ABOUT_VIDEO_KEYS)).all()}
        return (rows.get("about_video_url", ""),
                rows.get("about_video_thumb", ""))
    model = CONTENT_OWNERS.get(owner_type)
    obj = db.session.get(model, owner_id) if model else None
    if not obj:
        return ("", "")
    return (obj.video_url or "", obj.video_thumb or "")


def video_position_for(owner_type, owner_id=0):
    """This owner's stored position, exactly as video_settings_for does
    the url and the poster."""
    if owner_type == "about":
        block = Block.query.filter_by(key=ABOUT_VIDEO_POSITION_KEY).first()
        return clean_video_position(block.value if block else "")
    model = CONTENT_OWNERS.get(owner_type)
    obj = db.session.get(model, owner_id) if model else None
    return clean_video_position(getattr(obj, "video_position", "")
                                if obj else "")


def set_video_position(owner_type, owner_id, position):
    """Store it. Returns True when it actually moved."""
    position = clean_video_position(position)
    if owner_type == "about":
        block = Block.query.filter_by(key=ABOUT_VIDEO_POSITION_KEY).first()
        if block is None:
            block = Block(group="about", key=ABOUT_VIDEO_POSITION_KEY,
                          label="Video position", kind="text")
            db.session.add(block)
        changed = clean_video_position(block.value or "") != position
        block.value = position
        return changed
    model = CONTENT_OWNERS.get(owner_type)
    obj = db.session.get(model, owner_id) if model else None
    if not obj:
        return False
    changed = clean_video_position(obj.video_position or "") != position
    obj.video_position = position
    return changed


def set_video_settings(owner_type, owner_id, url, thumb):
    if owner_type == "about":
        for key, value in zip(ABOUT_VIDEO_KEYS, (url, thumb)):
            block = Block.query.filter_by(key=key).first()
            if not block:
                block = Block(group="about", key=key, label=key, kind="text")
                db.session.add(block)
            block.value = value
        return True
    model = CONTENT_OWNERS.get(owner_type)
    obj = db.session.get(model, owner_id) if model else None
    if not obj:
        return False
    obj.video_url, obj.video_thumb = url, thumb
    return True


def store_video(owner_type, owner_id, raw):
    """Validate a pasted link and store it. Returns (changed, error).

    WHAT IS STORED IS OUR OWN CANONICAL URL, built from the provider and
    id we extracted — never the text that was pasted. Somebody who
    pastes a whole <iframe> into the box gets the right video and none
    of their markup anywhere near the database.

    Clearing the box removes the video and its poster file. Changing it
    fetches a new poster and removes the old one, on the same terms as
    replacing an uploaded image.
    """
    was_url, was_thumb = video_settings_for(owner_type, owner_id)
    text = (raw or "").strip()
    if not text:
        if not was_url and not was_thumb:
            return ([], None)
        set_video_settings(owner_type, owner_id, "", "")
        if was_thumb:
            delete_upload(was_thumb)
        return (["video_url"], None)

    parsed = parse_video_url(text)
    if not parsed:
        return ([], "That does not look like a YouTube or Vimeo link. "
                    "Paste the address from the browser's address bar — "
                    "for example https://www.youtube.com/watch?v=… or "
                    "https://vimeo.com/… — or leave the box empty for no "
                    "video.")
    canonical = video_watch_url(parsed["provider"], parsed["id"])
    if canonical == was_url and was_thumb:
        return ([], None)              # same video, poster already here
    poster = fetch_video_poster(parsed["provider"], parsed["id"])
    set_video_settings(owner_type, owner_id, canonical, poster)
    if was_thumb and was_thumb != poster:
        delete_upload(was_thumb)
    return (["video_url"], None)


def set_layout(owner_type, owner_id, value):
    if value not in CONTENT_LAYOUTS:
        return False
    if owner_type == "about":
        block = Block.query.filter_by(key=ABOUT_LAYOUT_KEY).first()
        if not block:
            return False
        block.value = value
    else:
        model = CONTENT_OWNERS.get(owner_type)
        obj = db.session.get(model, owner_id) if model else None
        if not obj:
            return False
        obj.layout = value
    db.session.commit()
    return True


def legacy_lead_image(owner_type, owner_id=0):
    """The single image a content type had before rich layouts: the
    `about_image` Block for About, the `image` column for the rest.
    Returns (filename, alt_text) or (None, None)."""
    if owner_type == "about":
        block = Block.query.filter_by(key="about_image").first()
        filename = block.value if block else ""
        return (filename or None,
                "Enfield Bangladesh Welfare Association")
    model = CONTENT_OWNERS.get(owner_type)
    obj = db.session.get(model, owner_id) if model else None
    filename = getattr(obj, "image", "") if obj else ""
    return (filename or None, getattr(obj, "title", "") if obj else "")


def migrate_legacy_lead_image(owner_type, owner_id=0):
    """Bring the old single image into ContentImage the first time the
    manager is used, so nothing is lost when a page goes rich.

    The original column or Block keeps its value: it is what the site
    falls back to when the rich_layouts flag is off, what the listing
    cards still use, and `_upload_still_referenced` stops the shared file
    being deleted out from under either.
    """
    if images_for(owner_type, owner_id):
        return None
    filename, alt_text = legacy_lead_image(owner_type, owner_id)
    if not filename:
        return None
    img = ContentImage()
    img.owner_type = owner_type
    img.owner_id = owner_id
    img.filename = filename
    img.alt_text = alt_text or "Photograph"
    img.sort = 0
    db.session.add(img)
    db.session.commit()
    return img


def upload_on_disk(filename):
    """Is this uploads filename actually a file?

    One stat, the same cost thumb_url() already pays per image. Kept
    deliberately simple: no cache, because the answer changing is the
    whole point — an admin who re-uploads the missing photograph must
    see the page mend on the next request, not when a worker recycles.
    """
    if not filename:
        return False
    return os.path.isfile(os.path.join(UPLOAD_DIR, secure_filename(filename)))


def present_images(images):
    """(kept, missing) — images whose file is on disk, and the rest.

    A ContentImage row and the file it names are two different things,
    and they can part company: a file deleted from disk by hand, an
    uploads folder restored from an incomplete copy, a rsync that missed
    one. What the visitor then saw was an empty panel with alt text
    sitting in it, which reads as a broken website rather than as a
    missing photograph — and nothing anywhere said so.

    The caller decides what to do with `missing`: the public page drops
    them and lets the layout close up, the admin sees them named. Both
    beat rendering a hole.
    """
    kept, missing = [], []
    for img in images:
        (kept if upload_on_disk(img.filename) else missing).append(img)
    return kept, missing


def rich_content_for(owner_type, owner_id=0):
    """Everything a detail page needs: (layout, images).

    With the rich_layouts flag off this is always the classic preset with
    the one legacy image, so a site can ship either way.
    """
    if not feature_enabled("rich_layouts"):
        layout, images = "classic", []
    else:
        layout = layout_for(owner_type, owner_id)
        images = images_for(owner_type, owner_id)
    if not images:
        filename, alt_text = legacy_lead_image(owner_type, owner_id)
        if filename:
            images = [LegacyLeadImage(filename, alt_text or "Photograph")]
    # A row whose file is gone is not rendered as a hole. Signed-in
    # admins get them back, marked, because they are the ones who can
    # put the photograph back — see the macro.
    images, missing = present_images(images)
    if missing and current_user.is_authenticated:
        for img in missing:
            img.file_missing = True
        images = images + missing
    return layout, images


def rich_content_for_many(owner_type, objs):
    """{id: (layout, images)} for a page that renders MANY owners at once.

    The same answer as calling rich_content_for() per row, in one query
    for the lot rather than two per row: Our Journey puts every published
    milestone on a single page, so per-row lookups would be an N+1 that
    grows with the charity's history.
    """
    if not objs:
        return {}
    rich = feature_enabled("rich_layouts")
    by_owner = {}
    if rich:
        for img in (ContentImage.query
                    .filter(ContentImage.owner_type == owner_type,
                            ContentImage.owner_id.in_([o.id for o in objs]))
                    .order_by(ContentImage.sort, ContentImage.id).all()):
            by_owner.setdefault(img.owner_id, []).append(img)

    out = {}
    for obj in objs:
        layout = getattr(obj, "layout", "") if rich else ""
        images = by_owner.get(obj.id, [])
        if not images and getattr(obj, "image", ""):
            # Same fallback as rich_content_for: the old single image
            # keeps working until someone opens the manager on this row.
            images = [LegacyLeadImage(obj.image, obj.title or "Photograph")]
        images, missing = present_images(images)
        if missing and current_user.is_authenticated:
            for img in missing:
                img.file_missing = True
            images = images + missing
        out[obj.id] = (layout if layout in CONTENT_LAYOUTS else "classic",
                       images)
    return out


def paragraphs_of(text):
    """Body text into paragraphs — the existing blank-line convention."""
    return [p.strip() for p in (text or "").split("\n") if p.strip()]


# Below this many characters, a paragraph is too short to hold up its
# half of a two-column row. TUNED BY EYE against a 5/4 image, which is
# 406px tall at 1440 and 368px at 1024, with the text centred beside it:
#
#    55 chars   1 line,  33px  — marooned in 373px of nothing. The bug.
#   105 chars   2 lines, 67px  — thin; the eye reads it as a gap.
#   150 chars   3 lines, 100px — fine. A short paragraph beside a photo.
#   204 chars   4 lines, 133px — comfortable.
#   493 chars   9 lines, 299px — the text is nearly the taller half.
#
# So the cut is between two lines and three, not higher. 200 was the
# first guess and it was too aggressive: it turned ordinary three-line
# paragraphs into full-width bands, which would have stripped the
# left-right alternation out of most real content — and the alternation
# is the entire point of this preset. 130 is about two lines in that
# column.
#
# The threshold is on the ROW's total text, not on one paragraph: two
# short paragraphs together fill the column perfectly well, and it is the
# column that has to be filled.
ALT_MIN_TEXT_CHARS = 130


@app.template_global("interleave_content")
def interleave_content(paragraphs, images):
    """Pair paragraphs with images for the alternating preset.

    Paragraphs are spread as evenly as the image count allows; anything
    left over becomes a final text-only row, and spare images get rows of
    their own. Never drops either.

    Each row also carries `wide`: a row whose words are too short to sit
    beside a photograph is rendered as a FULL-WIDTH BAND instead, the
    same treatment a spare image with no words already gets. Two columns
    is a pairing, and a pairing needs two things of roughly comparable
    weight; a two-word line beside a 406px photograph is not that, and
    centring it — which is what already happens — only puts the emptiness
    on both sides of it instead of below.
    """
    if not images:
        return [{"paragraphs": paragraphs, "image": None, "wide": False}]
    per = max(1, -(-len(paragraphs) // len(images)))     # ceiling division
    rows, taken = [], 0
    for img in images:
        rows.append({"paragraphs": paragraphs[taken:taken + per],
                     "image": img})
        taken += per
    if taken < len(paragraphs):
        rows.append({"paragraphs": paragraphs[taken:], "image": None})
    for row in rows:
        chars = sum(len(p) for p in row["paragraphs"])
        # A row with no image is already full width, and one with no
        # words is already a band; `wide` is only about the third case.
        row["wide"] = bool(row["image"] and row["paragraphs"]
                           and chars < ALT_MIN_TEXT_CHARS)
    return rows



# ------------------------------------------------- events: day order
# Enough rows for the day ordering below to be sure of its answer. The
# SQL has already put the right DAYS first, and the sort only ever moves
# events around WITHIN a day, so the handful a page actually shows come
# from the first day or two — nowhere near this. It is a guard against
# loading years of events to print twelve, not a correctness knob.
EVENT_FETCH = 200


def start_minutes(text):
    """Minutes past midnight from an event's free-text start time.

    `start_time` is a text box — "6:30 PM", "18:30", "7pm", "Doors 6.45"
    — because that is what somebody typing an event actually writes, and
    it prints on the page exactly as typed. Sorting it as text is
    nonsense, though: "10:00 AM" comes before "6:30 AM" alphabetically.

    So read the first time-like thing in it. Returns None when there is
    no number to read at all ("", "Evening", "TBC"), which is what puts
    those entries AFTER the timed ones rather than before.
    """
    match = re.search(r"(\d{1,2})\s*[:.]?\s*(\d{2})?\s*(a\.?m|p\.?m)?",
                      (text or "").lower())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    half = (match.group(3) or "").replace(".", "")
    if half == "pm" and hour < 12:
        hour += 12
    elif half == "am" and hour == 12:      # 12:20am is twenty past midnight
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def events_in_day_order(rows, newest_day_first=False):
    """Events by day, and WITHIN a day by when they start.

    Somebody reading one day's programme expects it in the order they
    could attend it, so the time sort is always ascending — even in the
    past list, where the DAYS run backwards. Inside a day: timed entries
    first, in time order; then the ones with no readable time, by title;
    and `id` last so that two identical entries still have an order.

    Done in Python rather than SQL because no database can sort
    "6:30 PM" against "18:30", and normalising it into a column would be
    a schema change for something only ever read a page at a time.
    """
    def key(ev):
        minutes = start_minutes(ev.start_time)
        day = ev.event_date.toordinal()
        return (-day if newest_day_first else day,
                minutes is None,                       # timed entries first
                minutes if minutes is not None else 0,
                (ev.title or "").lower(),
                ev.id)
    return sorted(rows, key=key)



# ---------------------------------------------------------------- video
# YouTube and Vimeo only, and only ever as an ID we extracted ourselves.
# NOTHING an admin types is stored as markup or put on a page as markup:
# they paste a link, we pull the provider and the id out of it, and the
# page is built from those. An admin who pastes an <iframe> gets told to
# use the video field instead (see body_embed_problem below), because
# the alternative — rendering somebody's HTML — is how a CMS becomes a
# way to run scripts on its own site.
#
# CLICK TO LOAD, and this is the part that matters beyond tidiness: the
# poster is fetched ONCE, when the video is saved, and stored in our own
# uploads folder like any other image. A visitor's browser contacts
# YouTube or Vimeo only if they press play. That is what keeps the
# cookie notice honest — the site really does set two first-party
# cookies and contact nobody — and it is why no consent flow is needed
# for a page that merely HAS a video on it. See CLAUDE.md.
VIDEO_PATTERNS = (
    # (provider, compiled pattern). Each captures the id as group 1.
    ("youtube", re.compile(
        r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/|"
        r"youtube\.com/embed/|youtube\.com/shorts/|youtube\.com/v/|"
        r"youtube-nocookie\.com/embed/)"
        r"([A-Za-z0-9_-]{11})")),
    ("vimeo", re.compile(
        r"vimeo\.com/(?:video/|channels/[A-Za-z0-9_-]+/|groups/"
        r"[A-Za-z0-9_-]+/videos/)?(\d{6,12})")),
)
VIDEO_LABELS = {"youtube": "YouTube", "vimeo": "Vimeo"}
# Where a poster may be fetched from. Checked against the URL we build
# ourselves, so it is a belt-and-braces guard rather than the only one.
VIDEO_POSTER_HOSTS = ("i.ytimg.com", "vimeo.com", "i.vimeocdn.com")
VIDEO_FETCH_TIMEOUT = 6          # seconds; a save must not hang on this
VIDEO_POSTER_MAX = 4 * 1024 * 1024


def parse_video_url(raw):
    """A pasted link to (provider, id), or None if it is neither.

    Deliberately generous about the FORM of the link — watch, youtu.be,
    embed, shorts, a channel URL, extra query parameters, a share hash —
    and completely ungenerous about anything else. If no provider
    pattern matches, the answer is None and the caller refuses it.
    """
    text = (raw or "").strip()
    if not text:
        return None
    for provider, pattern in VIDEO_PATTERNS:
        found = pattern.search(text)
        if found:
            return {"provider": provider, "id": found.group(1)}
    return None


def video_embed_url(provider, video_id):
    """The player URL, in each provider's least intrusive form.

    youtube-nocookie.com is YouTube's own no-tracking-cookie host, and
    Vimeo's dnt=1 asks it not to track the session. Neither is contacted
    at all until the visitor presses play — this is only what they get
    when they do.
    """
    if provider == "youtube":
        return ("https://www.youtube-nocookie.com/embed/%s"
                "?autoplay=1&rel=0&modestbranding=1" % video_id)
    if provider == "vimeo":
        return ("https://player.vimeo.com/video/%s"
                "?autoplay=1&dnt=1" % video_id)
    return ""


def video_watch_url(provider, video_id):
    """Where the video lives, for a plain link in the admin."""
    if provider == "youtube":
        return "https://www.youtube.com/watch?v=%s" % video_id
    if provider == "vimeo":
        return "https://vimeo.com/%s" % video_id
    return ""


def _poster_source(provider, video_id):
    """The provider's own thumbnail URL, asked for at SAVE time only."""
    if provider == "youtube":
        # hqdefault exists for every video; maxresdefault does not.
        return "https://i.ytimg.com/vi/%s/hqdefault.jpg" % video_id
    if provider == "vimeo":
        try:
            api = ("https://vimeo.com/api/oembed.json?url="
                   + urllib.parse.quote(video_watch_url(provider, video_id),
                                        safe=""))
            with urllib.request.urlopen(api,
                                        timeout=VIDEO_FETCH_TIMEOUT) as resp:
                data = json.loads(resp.read(VIDEO_POSTER_MAX).decode("utf-8"))
            return (data.get("thumbnail_url") or "").strip()
        except Exception:
            return ""
    return ""


def fetch_video_poster(provider, video_id):
    """Fetch the provider's thumbnail into our own uploads folder.

    Returns a filename, or "" if anything at all went wrong. IT NEVER
    RAISES, on the same terms as send_mail: a poster is a nicety, and
    saving somebody's video must not fail because YouTube was slow. The
    caller falls back to the content's own lead image, and failing that
    to a plain play button.

    The bytes go through the ordinary image pipeline, so a fetched
    poster is resized, stripped of metadata and re-encoded exactly like
    an uploaded photograph, and gets a thumbnail beside it.
    """
    source = _poster_source(provider, video_id)
    if not source:
        return ""
    host = urllib.parse.urlparse(source).hostname or ""
    if not any(host == h or host.endswith("." + h)
               for h in VIDEO_POSTER_HOSTS):
        return ""                      # not a host we asked for
    try:
        with urllib.request.urlopen(source,
                                    timeout=VIDEO_FETCH_TIMEOUT) as resp:
            raw = resp.read(VIDEO_POSTER_MAX + 1)
        if not raw or len(raw) > VIDEO_POSTER_MAX:
            return ""
        processed = process_image(raw, "jpg")
        if not processed:
            return ""
        ext, data, thumb = processed
        name = "%s.%s" % (uuid.uuid4().hex, ext)
        with open(os.path.join(UPLOAD_DIR, secure_filename(name)), "wb") as fh:
            fh.write(data)
        if thumb:
            with open(os.path.join(UPLOAD_DIR,
                                   secure_filename(thumb_name(name))),
                      "wb") as fh:
                fh.write(thumb)
        return name
    except Exception as err:
        # Worth knowing about, not worth failing a save over.
        try:
            log_action("edit", entity=("Video", None),
                       summary="Could not fetch the %s poster image — %s."
                               % (VIDEO_LABELS.get(provider, provider),
                                  err.__class__.__name__))
        except Exception:
            pass
        return ""


# Markup an admin might paste into a BODY field, thinking that is how a
# video goes in. It is not: it renders as escaped source code on the
# live page, which is what this refuses so they are told where the
# video field is instead.
BODY_EMBED_TAGS = ("iframe", "script", "embed", "object", "video", "source")
BODY_EMBED_RE = re.compile(r"<\s*/?\s*(%s)\b" % "|".join(BODY_EMBED_TAGS),
                           re.IGNORECASE)


def embed_paste_message(tag):
    """What to say to somebody who pasted an embed code into a text box.

    They were trying to do the right thing — this is exactly what a CMS
    without a video field teaches people to do — so it points at the
    field that does work rather than just refusing.
    """
    return ("That looks like a <%s> embed code pasted into a text box. "
            "It would appear on the page as the code itself rather than "
            "as a video. Use the Video box on this page instead: paste "
            "the ordinary YouTube or Vimeo address and the video is "
            "added for you." % tag)


def body_embed_problem(*texts):
    """The name of the first embed-ish tag found, or None.

    Only these tags, and only as an opening or closing tag: "5 < 10" and
    "<3" are ordinary things to write and must go through untouched.
    """
    for text in texts:
        found = BODY_EMBED_RE.search(text or "")
        if found:
            return found.group(1).lower()
    return None


@app.template_global("video_of")
def video_of(owner, fallback_image=""):
    """What the rich-content macro needs to draw a video, or None.

    Takes anything with `video_url` and `video_thumb` — every owner has
    the same pair — or a dict for About, whose settings live in Blocks.
    Returns None when the flag is off or there is no video, so the
    templates simply do not ask the question.
    """
    # feature_enabled() is a QUERY, and Our Journey calls this once per
    # milestone — that is the page whose whole rule is that its cost
    # must not grow with the number of entries. The context processor
    # has already read every flag for this request, so use that.
    if has_request_context():
        flags = getattr(g, "_feature_flags", None)
        if flags is None:
            flags = feature_flags()
            g._feature_flags = flags
        allowed = flags.get("video", True)
    else:
        allowed = feature_enabled("video")
    if not allowed:
        return None
    url = (owner.get("video_url") if isinstance(owner, dict)
           else getattr(owner, "video_url", "")) or ""
    thumb = (owner.get("video_thumb") if isinstance(owner, dict)
             else getattr(owner, "video_thumb", "")) or ""
    parsed = parse_video_url(url)
    if not parsed:
        return None
    poster = thumb or fallback_image or ""
    position = (owner.get("video_position") if isinstance(owner, dict)
                else getattr(owner, "video_position", "")) or ""
    return {"provider": parsed["provider"],
            "label": VIDEO_LABELS[parsed["provider"]],
            "embed": video_embed_url(parsed["provider"], parsed["id"]),
            "watch": video_watch_url(parsed["provider"], parsed["id"]),
            "poster": poster,
            "position": clean_video_position(position)}


# ---------------------------------------------------------------- stats
# Paths that are never counted. Admin is not a visitor, /static is not a
# page, and the monitoring endpoints are a robot by definition — counting
# any of them would make the figures a measure of Netbus rather than of
# EBWA's audience.
PAGEVIEW_SKIP_PREFIXES = ("/admin", "/static", "/healthz", "/sitemap.xml",
                          "/robots.txt", "/favicon")
# Cheap bot detection: a substring of the user agent, lower-cased. Not a
# complete list and not meant to be — the expensive kinds of detection
# need either a third-party service or a fingerprint, and both are
# exactly what this feature is avoiding. It catches the loud ones.
BOT_MARKERS = ("bot", "crawl", "spider", "slurp", "curl", "wget",
               "python-requests", "httpx", "headlesschrome", "phantomjs",
               "monitor", "uptime", "pingdom", "lighthouse", "preview",
               "facebookexternalhit", "feedfetcher", "scraper")


def looks_like_a_bot(user_agent):
    """True for the obvious ones. Never stored — read and discarded."""
    ua = (user_agent or "").lower()
    if not ua:
        return True            # a browser sends one; a script often does not
    return any(marker in ua for marker in BOT_MARKERS)


def visitor_salt_for(day):
    """Today's salt, making a new one the first time the day turns.

    The previous salt is OVERWRITTEN, not kept — that is the whole
    mechanism by which yesterday's hashes stop being recomputable.
    """
    row = VisitorSalt.query.first()
    if row is None:
        row = VisitorSalt(day=day, salt=secrets.token_hex(32))
        db.session.add(row)
    elif row.day != day:
        row.day = day
        row.salt = secrets.token_hex(32)
    return row.salt


def visitor_hash(day, ip, user_agent):
    """One-way, salted, and unrelated to the same visitor's hash
    tomorrow. See the note on the models for what this is and is not."""
    salt = visitor_salt_for(day)
    raw = "%s|%s|%s" % (salt, ip or "", user_agent or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def should_count(path, method, status, user_agent):
    """Is this page load one a visitor made, on a page of the site?"""
    if method != "GET" or status != 200:
        return False
    if any(path.startswith(p) for p in PAGEVIEW_SKIP_PREFIXES):
        return False
    # Staff looking at their own site are not an audience. Guarded,
    # because current_user needs a request context and this helper is
    # also the thing you call to reason about a path.
    if has_request_context() and current_user.is_authenticated:
        return False
    return not looks_like_a_bot(user_agent)


# ---- recording, off the critical path -------------------------------
# after_request only STASHES the status, because that is the one thing
# teardown cannot see. The insert itself happens in teardown_request,
# after the response object is finished with, so nothing here sits
# between the visitor and their page.
@app.after_request
def _note_status(response):
    g._pv_status = response.status_code
    return response


@app.teardown_request
def _record_page_view(exc=None):
    """ONE INSERT, and it never raises.

    Same rule as send_mail(): a statistics table must not be able to
    turn somebody's visit into a 500, and a failure here is worth less
    than the page it would break. Anything that goes wrong is swallowed
    after rolling the session back.
    """
    if exc is not None or not has_request_context():
        return
    try:
        status = getattr(g, "_pv_status", None)
        if status is None:
            return
        agent = request.headers.get("User-Agent", "")
        if not should_count(request.path, request.method, status, agent):
            return
        day = utc_as_uk(datetime.utcnow()).date()
        db.session.add(PageView(
            day=day, path=request.path[:255],
            # request.remote_addr is the real client because ProxyFix is
            # wrapped with exactly one hop. It is hashed here and never
            # written; see the note on the models.
            visitor=visitor_hash(day, request.remote_addr, agent)))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


# ---- reading the figures --------------------------------------------
# Every query below is a COUNT or a GROUP BY over an indexed column.
# Nothing loads rows to len() them: this is the biggest table on the
# site and the panel is on a page an admin opens casually.
STATS_CHART_DAYS = 30
STATS_TOP_PAGES = 8


def _pv_counts(start, end):
    """(views, visitors) for [start, end], raw rows plus daily totals.

    A range can straddle the retention boundary — "this month" does, on
    the first days of a month — so both tables are asked and the answers
    added. A day is only ever in one of them: aggregate-pageviews
    deletes the raw rows in the same transaction that writes the total.
    """
    views = db.session.query(db.func.count(PageView.id)).filter(
        PageView.day >= start, PageView.day <= end).scalar() or 0
    visitors = db.session.query(
        db.func.count(db.distinct(PageView.visitor))).filter(
        PageView.day >= start, PageView.day <= end).scalar() or 0
    rolled = db.session.query(
        db.func.coalesce(db.func.sum(PageViewDaily.views), 0),
        db.func.coalesce(db.func.sum(PageViewDaily.visitors), 0)).filter(
        PageViewDaily.day >= start, PageViewDaily.day <= end).first()
    return (views + (rolled[0] or 0), visitors + (rolled[1] or 0))


def _pv_daily_series(start, end):
    """{day: (views, visitors)} across both tables, days with none absent."""
    out = {}
    rows = (db.session.query(PageView.day, db.func.count(PageView.id),
                             db.func.count(db.distinct(PageView.visitor)))
            .filter(PageView.day >= start, PageView.day <= end)
            .group_by(PageView.day).all())
    for day, views, visitors in rows:
        out[day] = (views, visitors)
    for day, views, visitors in (
            db.session.query(PageViewDaily.day, PageViewDaily.views,
                             PageViewDaily.visitors)
            .filter(PageViewDaily.day >= start,
                    PageViewDaily.day <= end).all()):
        got = out.get(day, (0, 0))
        out[day] = (got[0] + views, got[1] + visitors)
    return out


def visitor_stats():
    """Everything the Settings panel draws. All of it aggregate."""
    today = utc_as_uk(datetime.utcnow()).date()
    week_start = today - timedelta(days=today.weekday())      # Monday
    month_start = today.replace(day=1)
    chart_start = today - timedelta(days=STATS_CHART_DAYS - 1)
    series = _pv_daily_series(chart_start, today)
    days = [chart_start + timedelta(days=i) for i in range(STATS_CHART_DAYS)]
    chart = [{"day": d, "views": series.get(d, (0, 0))[0],
              "visitors": series.get(d, (0, 0))[1]} for d in days]

    # The same month a year ago, only if there is anything there — a row
    # of zeros would read as "we lost last year's traffic" rather than
    # "the site was not up yet".
    try:
        last_year_start = month_start.replace(year=month_start.year - 1)
    except ValueError:                       # 29 February
        last_year_start = month_start.replace(year=month_start.year - 1,
                                              day=28)
    last_year_end = _month_end(last_year_start)
    ly_views, ly_visitors = _pv_counts(last_year_start, last_year_end)

    top = (db.session.query(PageView.path, db.func.count(PageView.id))
           .filter(PageView.day >= month_start, PageView.day <= today)
           .group_by(PageView.path)
           .order_by(db.func.count(PageView.id).desc(), PageView.path)
           .limit(STATS_TOP_PAGES).all())
    oldest_raw = db.session.query(db.func.min(PageView.day)).scalar()
    oldest_daily = db.session.query(db.func.min(PageViewDaily.day)).scalar()
    return {
        "today": _pv_counts(today, today),
        "week": _pv_counts(week_start, today),
        "month": _pv_counts(month_start, today),
        "last_year": ((ly_views, ly_visitors)
                      if (ly_views or ly_visitors) else None),
        "last_year_label": last_year_start.strftime("%B %Y"),
        "month_label": month_start.strftime("%B %Y"),
        "chart": chart,
        "chart_max": max([c["views"] for c in chart] or [0]),
        "top": [{"path": p, "views": n} for p, n in top],
        "since": min([d for d in (oldest_raw, oldest_daily) if d] or [today]),
        "raw_days": pageview_raw_days(),
    }


def _month_end(first):
    """Last day of the month `first` starts."""
    if first.month == 12:
        return first.replace(day=31)
    return first.replace(month=first.month + 1, day=1) - timedelta(days=1)


def aggregate_page_views(today=None):
    """Fold days older than the raw window into totals, then delete them.

    Returns (days_rolled, rows_deleted). Idempotent: a day already in
    PageViewDaily is added to rather than duplicated, and its raw rows
    are gone by then anyway.
    """
    today = today or utc_as_uk(datetime.utcnow()).date()
    cutoff = today - timedelta(days=pageview_raw_days())
    rows = (db.session.query(PageView.day, db.func.count(PageView.id),
                             db.func.count(db.distinct(PageView.visitor)))
            .filter(PageView.day < cutoff)
            .group_by(PageView.day).all())
    deleted = 0
    for day, views, visitors in rows:
        existing = PageViewDaily.query.filter_by(day=day).first()
        if existing:
            existing.views += views
            existing.visitors += visitors
        else:
            db.session.add(PageViewDaily(day=day, views=views,
                                         visitors=visitors))
        deleted += PageView.query.filter_by(day=day).delete()
    db.session.commit()
    return (len(rows), deleted)


# ------------------------------------------------------- feature flags
# The optional/phased modules Netbus can switch on or off per site. Core
# pages (home, about, events, gallery, contact) are deliberately NOT
# listed and are never flaggable. To add a flag, append here — init-db is
# idempotent and inserts only missing names, exactly like DEFAULT_BLOCKS.
FEATURES = [
    # name, label, what switching it off hides, default
    ("news", "News & projects",
     "The news listing and article pages, and the homepage "
     "‘Latest news’ strip.", True),
    ("resources", "Community resources",
     "The /resources directory of local services.", True),
    ("our_journey", "Our Journey",
     "The /our-journey milestones and funding track record page.", True),
    ("membership_form", "Become a member",
     "The public membership application form at /membership.", True),
    ("donations", "Donations & collections",
     "The /donate page, collection campaign pages and the homepage "
     "collections strip.", True),
    ("rich_layouts", "Rich page layouts",
     "Whether the layout chooser and multi-image manager appear in the "
     "admin. With it off every page renders in the classic layout with "
     "its single image, exactly as before.", True),
    ("contact_form", "Contact form",
     "The enquiry form on /contact. The address, phone number and map "
     "stay on the page either way.", True),
    ("faq", "Frequently asked questions",
     "The /faq page and its links in the menu and footer.", True),
    ("video", "Video embedding",
     "A YouTube or Vimeo video on a news post, event, milestone, "
     "campaign or the About page. Videos are click-to-load: nothing "
     "reaches YouTube or Vimeo until a visitor presses play.", True),
    ("audit_log", "Audit log (client visibility)",
     "Whether EBWA's own admins can see the audit log page. Recording "
     "never stops, and super admins can always read it — this only "
     "decides whether the client sees the page.", True),
]

# Owner types point at their models now that both exist. 'about' stays
# None: it is the page, not a row.
CONTENT_OWNERS.update({"news_post": NewsPost, "event": Event,
                       "milestone": Milestone})

FEATURE_DEFAULTS = {name: default for name, _l, _d, default in FEATURES}
FEATURE_LABELS = {name: label for name, label, _d, _de in FEATURES}


def feature_flags():
    """All flags as {name: enabled}. A name with no row yet falls back to
    its FEATURES default, so a newly added flag works before init-db."""
    flags = dict(FEATURE_DEFAULTS)
    for row in FeatureFlag.query.all():
        if row.name in flags:
            flags[row.name] = row.enabled
    return flags


def feature_enabled(name):
    """A name that is not in FEATURES is a core feature: always on."""
    if name not in FEATURE_DEFAULTS:
        return True
    row = FeatureFlag.query.filter_by(name=name).first()
    return row.enabled if row else FEATURE_DEFAULTS[name]


# Admin pages whose ROUTE must enforce a flag, not merely hide its menu
# link. There is deliberately only one: everywhere else an admin page
# stays reachable with its module switched off, so content is never
# stranded (see CLAUDE.md). Anything added here must 403 for a client
# admin when its flag is off, and the URL-map test proves it does.
ADMIN_FLAG_GATES = {"admin_audit": "audit_log"}


def flag_explicitly_on(name):
    """True only when a row SAYS so — no falling back to the default.

    feature_enabled() falls back to the FEATURES default when no row
    exists yet, which is right for a module that should work before
    init-db runs. It is wrong for a flag that decides who may READ
    something: on a database where the row has never been written, "we
    do not know" must mean "no", or the gate opens itself.
    """
    row = FeatureFlag.query.filter_by(name=name).first()
    return bool(row and row.enabled)


def can_read_audit():
    """Super admins always. Client admins only when the flag says so."""
    return bool(current_user.is_authenticated
                and (current_user.is_super_admin
                     or flag_explicitly_on("audit_log")))


def feature_required(name):
    """Public route guard: a disabled feature 404s. Data is untouched."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not feature_enabled(name):
                abort(404)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# --------------------------------------------- two-factor authentication
# Optional, per user, TOTP (RFC 6238) — the standard 30-second 6-digit
# codes any authenticator app produces. Codes are verified server-side;
# the shared secret never leaves the database except inside the enrolment
# QR that the user scans.
TOTP_ISSUER = "EBWA Admin"
TOTP_WINDOW = 1              # accept one 30s step either side, for clock drift
RECOVERY_CODE_COUNT = 10
# No look-alike characters (0/o, 1/l/i) — these get written down.
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def verify_totp(user, code):
    """True if `code` is a valid current code for `user`.

    Each code is accepted once: the counter it matched is remembered, so
    a code that was shoulder-surfed or intercepted cannot be replayed
    while it is still inside the window.
    """
    code = re.sub(r"\s+", "", code or "")
    if not user.totp_secret or len(code) != 6 or not code.isdigit():
        return False
    totp = pyotp.TOTP(user.totp_secret)
    now = int(time.time())
    for offset in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        at = now + offset * totp.interval
        if hmac.compare_digest(totp.at(at), code):
            counter = at // totp.interval
            if (user.totp_last_counter is not None
                    and counter <= user.totp_last_counter):
                return False        # already spent
            user.totp_last_counter = counter
            db.session.commit()
            return True
    return False


def make_recovery_codes(user):
    """Replace any existing codes with a fresh single-use set.

    Returns the plain codes: they are shown once and only the hashes are
    kept, so there is no way to display them again afterwards.
    """
    RecoveryCode.query.filter_by(user_id=user.id).delete()
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(8))
        code = raw[:4] + "-" + raw[4:]
        codes.append(code)
        db.session.add(RecoveryCode(user_id=user.id,
                                    code_hash=generate_password_hash(code)))
    db.session.commit()
    return codes


def use_recovery_code(user, raw):
    """Spend one unused recovery code; True if it matched. Single-use."""
    candidate = re.sub(r"[^a-z0-9]", "", (raw or "").lower())
    if len(candidate) != 8:
        return False
    formatted = candidate[:4] + "-" + candidate[4:]
    for rc in RecoveryCode.query.filter_by(user_id=user.id,
                                           used_at=None).all():
        if check_password_hash(rc.code_hash, formatted):
            rc.used_at = datetime.utcnow()
            db.session.commit()
            return True
    return False


def unused_recovery_codes(user):
    return RecoveryCode.query.filter_by(user_id=user.id, used_at=None).count()


def totp_qr_data_uri(user):
    """The enrolment QR as an inline SVG data URI (the CSP allows data:
    images, and this keeps the secret off any third-party chart service)."""
    uri = pyotp.TOTP(user.totp_secret).provisioning_uri(
        name=user.email, issuer_name=TOTP_ISSUER)
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + \
        base64.b64encode(buf.getvalue()).decode("ascii")


# Between password and code the user is not logged in yet. Only their id
# and a timestamp go in the signed session cookie — never the secret.
PENDING_2FA_USER = "pending_2fa_user"
PENDING_2FA_AT = "pending_2fa_at"
PENDING_2FA_MAX_AGE = 300        # seconds to finish the second step


def pending_2fa_user():
    """The user waiting on a code, or None if there is no live hand-off."""
    uid = flask_session.get(PENDING_2FA_USER)
    started = flask_session.get(PENDING_2FA_AT, 0)
    if not uid or time.time() - started > PENDING_2FA_MAX_AGE:
        clear_pending_2fa()
        return None
    user = db.session.get(User, uid)
    return user if user and user.totp_enabled else None


def clear_pending_2fa():
    flask_session.pop(PENDING_2FA_USER, None)
    flask_session.pop(PENDING_2FA_AT, None)


# ------------------------------------------------------- idle expiry
def start_admin_session(user):
    """Log someone in with an idle-expiry session (see the config note)."""
    login_user(user)
    flask_session.permanent = True


def session_expired():
    """True if this request carried one of OUR session cookies that had
    someone logged in, and it is simply too old to be accepted.

    Checked by re-reading the cookie with the age limit lifted: that
    separates a timed-out admin from a first-time visitor, and from an
    anonymous visitor who merely picked up a session cookie from a flash
    message (those carry no user id).
    """
    raw = request.cookies.get(app.config.get("SESSION_COOKIE_NAME", "session"))
    if not raw:
        return False
    serializer = app.session_interface.get_signing_serializer(app)
    if serializer is None:
        return False
    try:
        data = serializer.loads(raw, max_age=None)   # age deliberately ignored
    except Exception:
        return False        # not ours, or tampered with
    return bool(data.get("_user_id"))


@login_manager.unauthorized_handler
def handle_unauthorized():
    """Say WHY the login page is being shown, instead of bouncing silently."""
    if session_expired():
        flash("Your session has expired, please log in again.", "error")
    else:
        flash("Please log in to continue.", "error")
    return redirect(url_for("admin_login"))


def super_admin_required(fn):
    """Netbus-only admin route: anonymous users are sent to the login
    page as usual, logged-in client admins get a flat 403."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_super_admin:
            abort(403)
        return fn(*args, **kwargs)
    guarded = login_required(wrapper)
    # Marked so a test can ask which routes are Netbus-only rather than
    # keeping a hand-written list that goes stale the moment somebody
    # adds a page.
    guarded._super_admin_only = True
    return guarded


# ------------------------------------------------------------ audit log
# An edit records WHICH fields changed, never what they changed from or
# to. Copying the values would duplicate page content and personal data
# into a second table that is never pruned — the audit log says what
# happened, the record itself says what it now holds.
def changed_fields(obj, values):
    """Names of the fields whose submitted value differs from the stored
    one. `values` is {field_name: new_value}; call BEFORE applying it."""
    return [name for name, new in values.items() if getattr(obj, name) != new]


def apply_values(obj, values):
    for name, new in values.items():
        setattr(obj, name, new)


def describe_changes(changed):
    """'changed: title, venue', or plain wording when nothing moved."""
    if not changed:
        return "no fields changed"
    return "changed: " + ", ".join(changed)


def save_summary(noun, name, is_new, changed):
    """Audit wording for a create or an edit of a named record."""
    if is_new:
        return "Created %s “%s”." % (noun, name)
    return "Edited %s “%s” (%s)." % (noun, name, describe_changes(changed))


def current_actor():
    """Who is acting, as a plain tuple, for work that outlives the request.

    The backup runs in a background thread, where there is no request and
    no `current_user` — and an entry reading "anonymous" for something a
    named super admin pressed a button to start is an audit trail that
    has lost the only fact worth keeping. Capture this IN the request and
    hand it to the thread.
    """
    if has_request_context() and current_user.is_authenticated:
        return (current_user.id, current_user.email,
                request.remote_addr or "")
    return (None, "anonymous", "")


def log_action(action, entity=None, summary="", actor=None):
    """Append one entry to the audit log, and commit it.

    `entity` is a model instance, or a (type_name, id) pair for a row
    that no longer exists — capture those values BEFORE deleting it.
    Recording is not conditional on any feature flag: the flag only
    decides who may read the log back.

    `actor` is a `current_actor()` tuple, for a caller running outside
    the request that started it.
    """
    entry = AuditLog()
    if actor is not None:
        entry.user_id, entry.user_email, entry.ip = actor
        entry.action = action
        entry.summary = summary
        if isinstance(entity, tuple):
            entry.entity_type, entry.entity_id = entity
        elif entity is not None:
            entry.entity_type = type(entity).__name__
            entry.entity_id = entity.id
        db.session.add(entry)
        db.session.commit()
        return
    if has_request_context() and current_user.is_authenticated:
        entry.user_id = current_user.id
        entry.user_email = current_user.email
    else:
        entry.user_email = "anonymous"
    if isinstance(entity, tuple):
        entry.entity_type, entry.entity_id = entity
    elif entity is not None:
        entry.entity_type = type(entity).__name__
        entry.entity_id = entity.id
    entry.action = action
    entry.summary = summary
    entry.ip = (request.remote_addr or "") if has_request_context() else ""
    db.session.add(entry)
    db.session.commit()


def is_last_super_admin(user):
    """True if demoting or deleting this account would leave the site
    with no super admin at all — i.e. nobody who can undo it."""
    return (user.role == "super_admin"
            and User.query.filter_by(role="super_admin").count() <= 1)


def clear_user_2fa(user):
    """Wipe every trace of a user's 2FA so they can enrol from scratch."""
    RecoveryCode.query.filter_by(user_id=user.id).delete()
    user.totp_secret = ""
    user.totp_enabled = False
    user.totp_last_counter = None


# The site's only cookie besides the login session: a flag saying the
# notice has been read. No tracking, no analytics, nothing to consent to.
COOKIE_NOTICE_NAME = "ebwa_notice"


@app.context_processor
def inject_globals():
    site = blocks_for("site")
    seen = request.cookies.get(COOKIE_NOTICE_NAME) if has_request_context() \
        else "1"
    return {"site": site, "current_year": datetime.utcnow().year,
            "features": feature_flags(),
            "show_cookie_notice": seen != "1",
            # Only the admin chrome uses this, and only as a number —
            # a count is not personal data.
            "unread_messages": unread_messages()
            if has_request_context() and current_user.is_authenticated else 0,
            # The link follows the ROUTE's rule, so the two can never
            # disagree about who may read the audit log.
            "audit_readable": can_read_audit()
            if has_request_context() else False}


# Security headers on every response. CSP allows exactly what the
# templates use: inline scripts and styles, and the Google Maps embed on
# the contact page.
#
# THE FONT HOSTS ARE GONE FROM HERE because the fonts are ours now, and
# the two must stay in step: leaving the domains in the policy after
# removing the links would quietly re-permit the thing that was removed.
# `font-src 'self'` is the assertion — if a stylesheet ever links a
# hosted face again, the browser refuses it rather than fetching it.
CSP = ("default-src 'self'; "
       "script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; "
       "img-src 'self' data:; "
       # The map, and the two video players — and ONLY on the player
       # hosts, which are the no-cookie/do-not-track ones. Nothing here
       # is contacted until a visitor presses play: the poster on the
       # page is our own file. See the video helpers above.
       "frame-src https://www.google.com "
       "https://www.youtube-nocookie.com https://player.vimeo.com; "
       "form-action 'self'; "
       "frame-ancestors 'self'; "
       "base-uri 'self'")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


# Simple in-memory rate limiter (per worker process — enough to blunt
# brute force and form spam without new dependencies).
# Manual backups allowed per hour from the Settings button. Ten is
# enough to set the NAS up and test it without fighting the limit, and
# still stops the button being held down.
BACKUP_MANUAL_PER_HOUR = 10
# A run that started this long ago and never finished is treated as
# abandoned rather than active — a process killed mid-backup must not
# block every future backup with a row that says "running" for ever.
BACKUP_STALE_MINUTES = 30

# One place deciding what each state is CALLED and how it is painted, so
# the panel rendered on the page and the one the poll repaints cannot
# drift apart into two vocabularies.
BACKUP_STATE_LABELS = {
    "none": "Never run",
    "running": "Running",
    "interrupted": "Interrupted",
    "ok": "Finished",
    "failed": "Failed",
}
BACKUP_STATE_PILLS = {
    "none": "grey", "running": "amber", "interrupted": "amber",
    "ok": "green", "failed": "red",
}

RATE_LIMITS = {          # scope -> (max attempts, window seconds)
    "login": (5, 600),
    "totp": (10, 600),   # a 6-digit code is guessable without this
    "subscribe": (5, 3600),
    "donate": (10, 3600),
    "contact": (5, 3600),   # enough for a genuine follow-up, not a flood
    # A "send a test to any address" button is a relay if it is not
    # limited, however few people can reach it.
    "test_mail": (5, 3600),
    # Manual backups. The real risk was never the count but two runs
    # overlapping, and backup_in_progress() handles that properly now, so
    # this is only a brake on someone leaning on the button. Adjust
    # BACKUP_MANUAL_PER_HOUR above rather than editing this line.
    "backup": (BACKUP_MANUAL_PER_HOUR, 3600),
    "sftp_test": (5, 3600),   # a connection test is cheap, but not free
    # The health panel refreshes every 30s; this allows that with room to
    # spare and still stops the endpoint being hammered.
    "health": (180, 3600),
}
_rate_buckets = {}       # (scope, ip) -> [attempt timestamps]


def rate_limited(scope):
    limit, window = RATE_LIMITS[scope]
    now = time.time()
    key = (scope, request.remote_addr or "?")
    hits = [t for t in _rate_buckets.get(key, []) if now - t < window]
    if len(hits) >= limit:
        _rate_buckets[key] = hits
        return True
    hits.append(now)
    _rate_buckets[key] = hits
    return False


# ------------------------------------------------------------------ mail
# The app's outbound email, written generically because the membership and
# ticketing modules will send through it too.
#
# Every setting resolves the same way: a Block a super admin filled in on
# the Settings page WINS, and the matching environment variable is the
# fallback. So a deployment that only ever set environment variables
# carries on exactly as it did, and anything set through the web overrides
# it without a redeploy.
#
# THE PASSWORD is handled differently from the rest: it can be set on
# the Settings page, but it is ENCRYPTED AT REST with Fernet (key in
# FERNET_KEY) and never rendered — the page shows only whether one is
# stored. SMTP_PASSWORD stays as the fallback when nothing is saved, so
# an existing deployment is unaffected. The reasoning is in CLAUDE.md,
# and is the same as for the NAS credential: a backup archive contains
# the database, so the key must live somewhere the archive does not.
MAIL_TIMEOUT = 10                # seconds; a hung server must not hang a page
MAIL_TO_KEY = "site_mail_to"     # kept: the recipient's Block key

# field -> (Block key, env var, label)
MAIL_SETTINGS = (
    ("host", "smtp_host", "SMTP_HOST", "Server"),
    ("port", "smtp_port", "SMTP_PORT", "Port"),
    ("user", "smtp_user", "SMTP_USER", "Username"),
    ("security", "smtp_security", "SMTP_USE_TLS", "Encryption"),
    ("sender", "smtp_from", "MAIL_FROM", "From address"),
    ("recipient", MAIL_TO_KEY, "MAIL_TO", "Enquiries go to"),
)
MAIL_SETTING_KEYS = tuple(key for _f, key, _e, _l in MAIL_SETTINGS)
MAIL_ENV_VARS = tuple(env for _f, _k, env, _l in MAIL_SETTINGS) \
    + ("SMTP_PASSWORD",)
SECURITY_MODES = (
    ("starttls", "STARTTLS — upgrade the connection (usual, port 587)"),
    ("ssl", "SSL/TLS — encrypted from the start (port 465)"),
    ("none", "None — unencrypted (only for a relay on this machine)"),
)
DEFAULT_PORT = 587


def _env_flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _security_from_env(port):
    """The old SMTP_USE_TLS flag, expressed as one of SECURITY_MODES."""
    if port == 465:
        return "ssl"
    return "starttls" if _env_flag("SMTP_USE_TLS", True) else "none"


def mail_blocks():
    """{block key: value} for the mail settings, in one query."""
    rows = Block.query.filter(Block.key.in_(MAIL_SETTING_KEYS)).all()
    return {b.key: (b.value or "").strip() for b in rows}


def mail_settings():
    """Every setting, with where it came from.

    Returns {field: {"value", "source", "env", "key", "label"}} where
    source is 'database', 'environment' or 'unset' — the Settings page
    shows that, because "which of the two is actually in force?" is the
    question you have when email is misbehaving.
    """
    stored = mail_blocks()
    out = {}
    for field, key, env, label in MAIL_SETTINGS:
        saved = stored.get(key, "")
        from_env = (os.environ.get(env, "") or "").strip()
        if field == "port":
            from_env = from_env or ""
        if field == "security" and not saved:
            # The environment expresses this as a boolean, so translate
            # rather than showing a raw 1/0 that means nothing here.
            try:
                port = int(stored.get("smtp_port")
                           or os.environ.get("SMTP_PORT") or DEFAULT_PORT)
            except ValueError:
                port = DEFAULT_PORT
            from_env = _security_from_env(port)
        value = saved or from_env
        out[field] = {"value": value, "env": env, "key": key, "label": label,
                      "source": ("database" if saved
                                 else "environment" if from_env else "unset")}
    return out


def mail_config():
    """The settings as the sending code wants them: database over env."""
    settings = mail_settings()
    try:
        port = int(settings["port"]["value"] or DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    security = settings["security"]["value"] or _security_from_env(port)
    if security not in dict(SECURITY_MODES):
        security = "starttls"
    return {"host": settings["host"]["value"],
            "port": port,
            "user": settings["user"]["value"],
            # Settings page first, environment as the fallback — and
            # encrypted at rest either way. See the note at the top.
            "password": mail_password(),
            "security": security,
            "sender": settings["sender"]["value"]}


def mail_recipient():
    """Where site email goes: the address on Settings, else MAIL_TO."""
    return mail_settings()["recipient"]["value"]


MAIL_PASSWORD_KEY = "smtp_password_enc"   # Fernet ciphertext, never plain


def mail_password():
    """The SMTP password: the one saved on Settings, else the environment.

    Stored ENCRYPTED (Fernet, key in FERNET_KEY) for the same reason the
    NAS password is — see the note in CLAUDE.md. Nothing else in the app
    may read the Block directly.
    """
    block = Block.query.filter_by(key=MAIL_PASSWORD_KEY).first()
    stored = decrypt_secret(block.value if block else "")
    return stored or os.environ.get("SMTP_PASSWORD", "")


def mail_password_setting():
    """Whether a password is set and which one is in force. NEVER the value.

    Reported like every other mail setting, because "which of the two is
    the server actually using?" is the question when authentication
    fails.
    """
    block = Block.query.filter_by(key=MAIL_PASSWORD_KEY).first()
    if block and (block.value or "").strip():
        return {"stored": True, "source": "database", "label": "This page"}
    if os.environ.get("SMTP_PASSWORD", ""):
        return {"stored": True, "source": "environment",
                "label": "Server (SMTP_PASSWORD)"}
    return {"stored": False, "source": "unset", "label": "Not set"}


def password_is_set():
    """Whether a password exists at all. Never the value itself."""
    return mail_password_setting()["stored"]


def mail_configured():
    cfg = mail_config()
    return bool(cfg["host"] and cfg["sender"])


def _scrubbed(text, cfg):
    """Belt and braces: no password can ever reach a page or the log."""
    secret = cfg.get("password") or ""
    text = str(text)
    return text.replace(secret, "***") if secret else text


def describe_mail_failure(exc, cfg):
    """A plain sentence saying what went wrong, safe to show and to log.

    Specific enough to fix the problem — refused, rejected credentials,
    TLS — because "sending failed" tells whoever is configuring this
    nothing at all.
    """
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("the server rejected the username or password "
                "(check SMTP_USER and SMTP_PASSWORD)")
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "the server refused the from address (%s)" % cfg["sender"]
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "the server refused the recipient address"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return ("the server does not support what was asked of it — "
                "usually the wrong encryption setting for this port")
    if isinstance(exc, ssl.SSLError):
        return ("the encrypted connection failed — usually SSL/TLS on a "
                "STARTTLS port, or the other way round")
    if isinstance(exc, ConnectionRefusedError):
        return ("nothing is listening on %s port %d — check the server "
                "and port" % (cfg["host"], cfg["port"]))
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return ("the server did not answer within %d seconds"
                % MAIL_TIMEOUT)
    if isinstance(exc, socket.gaierror):
        return "the server name %s could not be looked up" % cfg["host"]
    return "%s: %s" % (type(exc).__name__, _scrubbed(exc, cfg))


def recipient_header(to):
    """`to` as a header value, whatever shape the caller had it in.

    ACCEPTS A STRING OR A LIST, on purpose. Every caller has a list of
    addresses in mind; some of them hold it as text a super admin typed
    into a box, and some as the list that text was already parsed into.
    Making each one remember which is how send_monthly_report came to
    hand a list to something that called .strip() on it — an
    AttributeError on every send, including the button whose whole job
    was to prove the address worked.

    Both shapes go through parse_addresses(), so one function knows what
    an address list is: commas or semicolons, blanks dropped, a list
    whose elements are themselves comma-separated flattened. Returns ""
    when there is nobody, which the caller reports rather than raising.
    """
    if to is None:
        return ""
    if isinstance(to, str):
        parts = parse_addresses(to)
    else:
        parts = []
        for item in to:
            parts.extend(parse_addresses(item if isinstance(item, str)
                                         else str(item)))
    seen, out = set(), []
    for address in parts:
        if address.lower() not in seen:      # the same person, once
            seen.add(address.lower())
            out.append(address)
    return ", ".join(out)


def send_mail_result(to, subject, body, reply_to=None):
    """Send one plain-text email. Returns (sent, reason).

    `to` may be a string or a list of addresses — see recipient_header().

    THIS NEVER RAISES. Everything that calls it has already saved the
    thing the visitor typed, and an SMTP server that is down, slow or
    misconfigured must not turn somebody's thank-you page into an error
    page. `reason` is a sentence fit to show an admin or write to the
    audit log, and never contains the password.
    """
    to = recipient_header(to)
    cfg = mail_config()
    if not to:
        return False, "no recipient address is set"
    if not cfg["host"] or not cfg["sender"]:
        return False, "email is not configured on this server"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg["sender"]
    message["To"] = to
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(body)

    try:
        if cfg["security"] == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"],
                                      timeout=MAIL_TIMEOUT)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"],
                                  timeout=MAIL_TIMEOUT)
        with server:
            if cfg["security"] == "starttls":
                server.starttls()
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(message)
    except Exception as exc:
        try:
            return False, describe_mail_failure(exc, cfg)
        except Exception:
            # Even working out WHY it failed must not raise: this
            # function's whole promise is that it cannot break a page.
            return False, "sending failed"
    return True, "sent"


def send_mail(to, subject, body, reply_to=None):
    """send_mail_result(), for callers that only need to know if it went.

    A failure is recorded in the audit log here, so a silence nobody
    noticed is still visible after the fact.
    """
    ok, reason = send_mail_result(to, subject, body, reply_to=reply_to)
    if not ok:
        # The NORMALISED header, so the log reads the same whichever
        # shape the caller passed.
        _mail_failed(subject, recipient_header(to), reason)
    return ok


def _mail_failed(subject, to, reason):
    log_action("mail_failed", summary=(
        "Could not send the email “%s” to %s — %s. The message it was "
        "about is saved and can still be read in the admin."
        % (subject, to or "(nobody)", reason)))


# -------------------------------------------------------------- security
# Failed logins are already in the audit log; this makes them visible
# without anybody thinking to go and look.
#
# NOTHING here ever records a password, an attempted password, or any
# part of one. The audit entries hold the attempted EMAIL and the IP, and
# that is all these functions read.
FAILED_LOGIN_ACTION = "login_failed"
FAILED_LOGIN_WINDOW_HOURS = 24     # dashboard: how far back to look
FAILED_LOGIN_NOTICE = 5            # dashboard: show it above this many
ALERT_IP_THRESHOLD = 10            # email: failures from ONE ip in an hour
ALERT_COOLDOWN_MINUTES = 60        # email: never more often than this
SECURITY_ALERT_KEY = "security_alert_email"   # Block: "1" to switch on


SECURITY_ALERT_TO_KEY = "site_security_alert_to"


def parse_addresses(raw):
    """A comma-separated list of addresses, cleaned up."""
    parts = (raw or "").replace(";", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def security_alert_setting():
    """Where security alerts go, and where that address came from.

    Alerts and contact enquiries are DIFFERENT audiences: an enquiry is
    for EBWA, and "somebody is working through passwords on your admin"
    is for whoever looks after the server. They only look like the same
    address while both fall back to MAIL_TO, and they stop looking alike
    the day enquiries move to an @ebwa.org.uk mailbox — which is exactly
    when nobody would notice the alerts had followed them.
    """
    block = Block.query.filter_by(key=SECURITY_ALERT_TO_KEY).first()
    chosen = (block.value or "").strip() if block else ""
    if chosen:
        return {"value": chosen, "recipients": parse_addresses(chosen),
                "source": "database", "label": "This page"}
    # Falling back: say WHICH fallback, since the enquiries address is
    # itself either typed here or set on the server.
    enquiries = mail_settings()["recipient"]
    label = ("Same as enquiries (this page)"
             if enquiries["source"] == "database"
             else "Same as enquiries (MAIL_TO)"
             if enquiries["source"] == "environment" else "Not set")
    return {"value": enquiries["value"],
            "recipients": parse_addresses(enquiries["value"]),
            "source": "fallback" if enquiries["value"] else "unset",
            "label": label}


def security_alert_to():
    """The recipients as one header value, or "" if there are none.

    send_mail() takes either shape now, so this exists for the places
    that want the addresses as TEXT — a flash, a log line — rather than
    as a thing to send to.
    """
    return recipient_header(security_alert_setting()["recipients"])


def failed_logins_since(hours=FAILED_LOGIN_WINDOW_HOURS):
    since = datetime.utcnow() - timedelta(hours=hours)
    return db.session.query(db.func.count(AuditLog.id)).filter(
        AuditLog.action == FAILED_LOGIN_ACTION,
        AuditLog.created_at >= since).scalar() or 0


def security_alerts_on():
    block = Block.query.filter_by(key=SECURITY_ALERT_KEY).first()
    return bool(block and (block.value or "").strip() == "1")


def alert_cooldown_active():
    """True if an alert went out recently.

    Checked in the DATABASE rather than in memory: gunicorn runs several
    workers, and an attacker hitting one worker after another must not
    get one email per worker.
    """
    since = datetime.utcnow() - timedelta(minutes=ALERT_COOLDOWN_MINUTES)
    return db.session.query(AuditLog.id).filter(
        AuditLog.action == "security_alert",
        AuditLog.created_at >= since).first() is not None


def audit_log_link():
    """A link to the failed-sign-in log, or the path if one cannot be built.

    url_for(_external=True) needs a request or SERVER_NAME, and this runs
    from a background caller as happily as from the login page. A
    relative path in an email is not ideal; an exception that stops the
    alert going out at all is worse.
    """
    try:
        return url_for("admin_audit", action=FAILED_LOGIN_ACTION,
                       _external=True)
    except Exception:
        return "/admin/audit?action=%s" % FAILED_LOGIN_ACTION


def note_failed_login(attempted, ip):
    """Called after a failed login is logged. Emails only if it is worth it.

    The threshold is per IP within the hour, so one person mistyping their
    password never triggers anything, and a machine working through a
    password list does.
    """
    if not security_alerts_on() or not ip:
        return
    since = datetime.utcnow() - timedelta(hours=1)
    recent = db.session.query(db.func.count(AuditLog.id)).filter(
        AuditLog.action == FAILED_LOGIN_ACTION,
        AuditLog.ip == ip,
        AuditLog.created_at >= since).scalar() or 0
    if recent < ALERT_IP_THRESHOLD or alert_cooldown_active():
        return

    tried = [row[0] for row in db.session.query(AuditLog.user_email)
             .filter(AuditLog.action == FAILED_LOGIN_ACTION,
                     AuditLog.ip == ip,
                     AuditLog.created_at >= since).distinct().limit(10)]
    addresses = ", ".join(a for a in tried if a) or attempted or "unknown"
    body = (
        "%d failed sign-in attempts have come from %s in the last hour.\n\n"
        "Addresses tried: %s\n\n"
        "No password is recorded anywhere in this site, and none is in "
        "this email.\n\n"
        "The full history is in the audit log:\n%s\n\n"
        "If this was you, nothing needs doing. If it was not, the accounts "
        "are still protected by their passwords and, where it is switched "
        "on, two-factor authentication.\n"
        % (recent, ip, addresses, audit_log_link()))
    sent = send_mail(security_alert_to(),
                     "EBWA website: repeated failed sign-ins", body)
    # Logged either way — and this log line is what the cooldown reads,
    # so a failed send still stops a flood.
    log_action("security_alert",
               summary=("Emailed a failed-sign-in alert: %d attempts from "
                        "%s in the last hour." % (recent, ip) if sent else
                        "Tried to email a failed-sign-in alert about %s "
                        "(%d attempts) and could not." % (ip, recent)))


# ---------------------------------------------------------------- backups
# The app makes and tracks archives. GETTING THEM OFF THE SERVER IS NOT
# THIS APP'S JOB and never will be: that is a cron job with an ssh key,
# and an archive sitting on the same disk as the thing it backs up is not
# a backup. The Settings panel and the README both say so.
#
# Nothing here shells out. The database is copied with sqlite3's own
# backup API — which is consistent while the site is running, unlike
# copying the file — and the archive is written with zipfile. There is no
# command line anywhere for a web request to influence.
BACKUP_DIR = os.environ.get("BACKUP_DIR",
                            os.path.join(BASE_DIR, "backups"))
try:
    BACKUP_KEEP = max(1, int(os.environ.get("BACKUP_KEEP", "7") or 7))
except ValueError:
    BACKUP_KEEP = 7
BACKUP_PREFIX = "ebwa-backup-"


def backup_paths():
    """Where the pieces live, resolved once so the panel and the CLI agree."""
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    db_path = uri[len("sqlite:///"):] if uri.startswith("sqlite:///") else ""
    return {"dir": BACKUP_DIR, "database": db_path, "uploads": UPLOAD_DIR}


def _dir_size(path):
    total = files = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                pass
    return total, files


def run_backup(reason="manual", run=None):
    """Write one archive and record it. Returns the BackupRun row.

    Every outcome is recorded, including a failure — a backup you believe
    in but that never ran is worse than none at all.

    `run` is an already-claimed row (see `claim_backup_slot`), for the
    manual backup, which claims its slot in the request and does the work
    in a thread. The CLI passes nothing and gets the old behaviour: claim
    and run in one go, synchronously.
    """
    paths = backup_paths()
    if run is None:
        run = BackupRun()
        run.started_at = datetime.utcnow()
        run.status = "running"
        run.reason = reason
        db.session.add(run)
        db.session.commit()

    stamp = utc_as_uk(run.started_at).strftime("%Y%m%d-%H%M%S")
    name = "%s%s.zip" % (BACKUP_PREFIX, stamp)
    # Two runs in the same second would otherwise write the same name and
    # the second would silently replace the first — exactly the sort of
    # quiet loss a backup system must never have.
    suffix = 2
    while os.path.exists(os.path.join(paths["dir"], name)):
        name = "%s%s-%d.zip" % (BACKUP_PREFIX, stamp, suffix)
        suffix += 1
    target = os.path.join(paths["dir"], name)
    snapshot = target + ".db-snapshot"
    try:
        os.makedirs(paths["dir"], exist_ok=True)
        files = 0
        # 1. A consistent copy of the database, taken through sqlite's
        #    backup API so a write halfway through cannot tear it.
        if paths["database"] and os.path.isfile(paths["database"]):
            source = dest = None
            try:
                source = sqlite3.connect(paths["database"])
                dest = sqlite3.connect(snapshot)
                with dest:
                    source.backup(dest)
            finally:
                # Closed even if the copy fails: a leaked handle keeps the
                # database file locked, and the next thing to touch it is
                # the site.
                for conn in (dest, source):
                    if conn is not None:
                        conn.close()
        # 2. That, plus every upload, into one archive.
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            if os.path.isfile(snapshot):
                archive.write(snapshot, "database/ebwa.db")
                files += 1
            if os.path.isdir(paths["uploads"]):
                for root, _dirs, names in os.walk(paths["uploads"]):
                    for entry in sorted(names):
                        full = os.path.join(root, entry)
                        rel = os.path.relpath(full, paths["uploads"])
                        archive.write(full, os.path.join("uploads", rel))
                        files += 1
            archive.writestr("README.txt", BACKUP_README)
        run.filename = name
        run.size_bytes = os.path.getsize(target)
        run.file_count = files
        run.status = "ok"
    except Exception as exc:
        run.status = "failed"
        run.error = "%s: %s" % (type(exc).__name__, exc)
        if os.path.isfile(target):
            try:
                os.remove(target)
            except OSError:
                pass
    finally:
        if os.path.isfile(snapshot):
            try:
                os.remove(snapshot)
            except OSError:
                pass
        run.finished_at = datetime.utcnow()
        db.session.commit()

    if run.status == "ok":
        prune_backups()
    return run


BACKUP_README = (
    "EBWA website backup\n"
    "===================\n\n"
    "database/ebwa.db  - the site's database, a consistent snapshot\n"
    "uploads/          - every uploaded photograph and logo\n\n"
    "To restore: stop the service, put ebwa.db in instance/ and the\n"
    "contents of uploads/ in static/uploads/, then start it again.\n\n"
    "This archive is only a backup once a COPY OF IT IS SOMEWHERE ELSE.\n"
    "On the same server it protects against a mistake, not against the\n"
    "server being lost.\n")


def prune_backups(keep=None):
    """Delete all but the newest `keep` archives. Returns how many went."""
    keep = BACKUP_KEEP if keep is None else keep
    try:
        names = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith(BACKUP_PREFIX) and f.endswith(".zip"))
    except OSError:
        return 0
    removed = 0
    for name in names[:-keep] if keep else names:
        try:
            os.remove(os.path.join(BACKUP_DIR, name))
            removed += 1
        except OSError:
            pass
    return removed


def backup_in_progress():
    """The BackupRun currently running, or None.

    This is what the rate limit was really standing in for. Two
    overlapping runs would write archives at the same time and upload
    them at the same time, over one connection each, for no gain — and
    the second would prune the first's archive out from under it.

    A row left at "running" by a crash stops counting after
    BACKUP_STALE_MINUTES: a guard that can lock everybody out for ever
    because a process died is worse than the thing it guards against.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=BACKUP_STALE_MINUTES)
    return (BackupRun.query
            .filter(BackupRun.status == "running",
                    BackupRun.started_at >= cutoff)
            .order_by(BackupRun.started_at.desc(),
                              BackupRun.id.desc()).first())


def backup_stale_cutoff():
    """Before this, a row still saying "running" is a row nobody is on."""
    return datetime.utcnow() - timedelta(minutes=BACKUP_STALE_MINUTES)


def claim_backup_slot(reason="manual"):
    """Take the one backup slot, or return None if somebody else has it.

    `backup_in_progress()` is a read, and a read is not a claim: two
    requests could both see nothing running and both start. That window
    used to be small in practice because the button held the browser for
    the whole backup, so nobody clicked twice. Now the button returns
    immediately, and two clicks a moment apart can land on the two
    gunicorn workers at the same time.

    So the row is INSERTED and then the race is settled on the rows
    themselves: whoever holds the lowest id among the live "running" rows
    has the slot, and everybody else stands down. Both sides see the same
    two rows and reach the same answer, with no lock and nothing to leak
    if a process dies mid-decision.
    """
    run = BackupRun()
    run.started_at = datetime.utcnow()
    run.status = "running"
    run.reason = reason
    db.session.add(run)
    db.session.commit()

    winner = (BackupRun.query
              .filter(BackupRun.status == "running",
                      BackupRun.started_at >= backup_stale_cutoff())
              .order_by(BackupRun.id.asc()).first())
    if winner is not None and winner.id != run.id:
        # We lost. Take our row back out rather than leaving a phantom
        # "running" that the panel would show and the stale rule would
        # take half an hour to forget.
        db.session.delete(run)
        db.session.commit()
        return None
    return run


def backup_job(run_id, actor=None):
    """Write the archive and send it to the NAS. Runs in a THREAD.

    Everything it needs is passed by value — a row id and who asked for
    it — because a background thread has no request, no `current_user`
    and no session belonging to the request that started it. Flask-SQLAl-
    chemy's session is thread-local, so this gets its own; the app
    context is pushed here for the same reason.

    It NEVER raises out of the thread. An exception escaping here would
    go to stderr and leave the row saying "running" until the stale rule
    forgot it half an hour later — the panel would show a backup in
    progress that nothing was working on. Anything unexpected is recorded
    on the row as a failure, which is what the panel and the audit log
    are for.
    """
    with app.app_context():
        try:
            run = db.session.get(BackupRun, run_id)
            if run is None:
                return
            run = run_backup(reason=run.reason, run=run)
            if run.status != "ok":
                log_action("backup", summary="Backup from the Settings page "
                                             "failed — %s." % run.error,
                           actor=actor)
                return
            log_action("backup",
                       summary="Ran a backup from the Settings page: %s (%s)."
                               % (run.filename,
                                  filesize_filter(run.size_bytes)),
                       actor=actor)
            if sftp_ready():
                # Same button, whole job: an archive that has not left the
                # server is only half a backup.
                transfer_with_retry(run)
        except Exception as exc:               # noqa: BLE001 - see above
            try:
                run = db.session.get(BackupRun, run_id)
                if run is not None and run.status == "running":
                    run.status = "failed"
                    run.error = "%s: %s" % (type(exc).__name__, exc)
                    run.finished_at = datetime.utcnow()
                    db.session.commit()
                log_action("backup",
                           summary="Backup from the Settings page failed — "
                                   "%s: %s" % (type(exc).__name__, exc),
                           actor=actor)
            except Exception:
                pass
        finally:
            # Hand the connection back rather than leaving one checked out
            # per backup for the life of the worker.
            db.session.remove()


def start_backup(reason="manual", actor=None):
    """Claim the slot and hand the work to a thread. Returns the row or None.

    A DAEMON thread, deliberately. On a deploy or a restart gunicorn
    gives its workers a moment and then kills them; a non-daemon thread
    would hold the shutdown open for that moment and be killed anyway,
    turning a restart into a wait for no gain. What it leaves behind is a
    row still saying "running", and that case is already handled: the
    guard ignores such a row after BACKUP_STALE_MINUTES, and the panel
    shows it as interrupted rather than in progress.
    """
    run = claim_backup_slot(reason)
    if run is None:
        return None
    # LOGGED BEFORE THE THREAD STARTS, not after. A backup that fails
    # immediately can finish before the caller gets its next line run,
    # which put "Started" in the log after "failed" and read as a second
    # backup nobody asked for.
    log_action("backup", summary="Started a backup from the Settings page.",
               actor=actor)
    worker = threading.Thread(target=backup_job, args=(run.id, actor),
                              name="ebwa-backup-%d" % run.id, daemon=True)
    worker.start()
    return run


def backup_state():
    """What the panel says is happening, as plain data for HTML or JSON.

    Driven entirely off the BackupRun row — started, finished, status and
    error are all already there, so there is no second idea of state to
    keep in step with the first.

    Four states, because "running" alone cannot tell the difference
    between a backup in progress and one whose process was killed
    underneath it:

      running     — started, not finished, and recent enough to believe
      interrupted — started, never finished, and older than the stale
                    window: the worker was restarted mid-backup
      ok / failed — it finished, one way or the other
    """
    run = (BackupRun.query
           .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
           .first())
    if run is None:
        return {"state": "none", "run": None, "started": "", "finished": "",
                "detail": "", "busy": False}
    if run.status == "running":
        stale = run.started_at < backup_stale_cutoff()
        return {
            "state": "interrupted" if stale else "running",
            "run": run,
            "started": utc_as_uk(run.started_at).strftime("%H:%M:%S"),
            "finished": "",
            "detail": ("The server was restarted while this backup was "
                       "running, so it did not finish. Nothing was "
                       "damaged — start another one."
                       if stale else
                       "Writing the archive%s."
                       % (" and sending it to the NAS" if sftp_ready()
                          else "")),
            "busy": not stale,
        }
    if run.status == "ok":
        detail = "%s — %s, %d file%s." % (
            run.filename, filesize_filter(run.size_bytes), run.file_count,
            "" if run.file_count == 1 else "s")
        if run.transfer_status == "ok":
            detail += " Sent to the NAS as %s." % run.remote_filename
        elif run.transfer_status == "failed":
            detail += (" It could not be sent to the NAS — %s. It is safe "
                       "on the server; the next scheduled run will try "
                       "again." % run.transfer_error)
        elif sftp_ready():
            detail += " Sending to the NAS."
        return {"state": "ok", "run": run,
                "started": utc_as_uk(run.started_at).strftime("%H:%M:%S"),
                "finished": (utc_as_uk(run.finished_at).strftime("%H:%M:%S")
                             if run.finished_at else ""),
                "detail": detail, "busy": False}
    return {"state": "failed", "run": run,
            "started": utc_as_uk(run.started_at).strftime("%H:%M:%S"),
            "finished": (utc_as_uk(run.finished_at).strftime("%H:%M:%S")
                         if run.finished_at else ""),
            "detail": run.error or "No reason was recorded.",
            "busy": False}


def backup_status():
    """Everything the Settings panel shows. Read-only, no side effects."""
    paths = backup_paths()
    last = (BackupRun.query.filter_by(status="ok")
            .order_by(BackupRun.started_at.desc(),
                              BackupRun.id.desc()).first())
    last_failed = (BackupRun.query.filter_by(status="failed")
                   .order_by(BackupRun.started_at.desc(),
                              BackupRun.id.desc()).first())
    try:
        archives = [f for f in os.listdir(paths["dir"])
                    if f.startswith(BACKUP_PREFIX) and f.endswith(".zip")]
    except OSError:
        archives = []
    uploads_size, uploads_files = _dir_size(paths["uploads"])
    try:
        free = shutil.disk_usage(paths["dir"] if os.path.isdir(paths["dir"])
                                 else BASE_DIR).free
    except OSError:
        free = None
    db_size = (os.path.getsize(paths["database"])
               if paths["database"] and os.path.isfile(paths["database"])
               else 0)
    return {"last": last, "last_failed": last_failed,
            "count": len(archives), "keep": BACKUP_KEEP,
            "dir": paths["dir"], "database": paths["database"],
            "db_size": db_size, "uploads_size": uploads_size,
            "uploads_files": uploads_files, "disk_free": free}


# ----------------------------------------------------- offsite transfer
# Uploading the archive to the NAS over SFTP, reached across Tailscale.
#
# THE PASSWORD IS ENCRYPTED AT REST (Fernet, key in FERNET_KEY), which is
# deliberately different from SMTP_PASSWORD living in the environment.
# The reason is this module: the backup archive contains the database, so
# a plaintext credential to the backup destination stored in the database
# would be copied into every archive — and then onto the NAS, where a
# copy of that credential would sit beside the data it protects. The key
# stays in the environment, so an archive on its own opens nothing.
SFTP_KEYS = {
    "enabled": "sftp_enabled",          # "1" or ""
    "host": "sftp_host",
    "port": "sftp_port",
    "user": "sftp_user",
    "password": "sftp_password_enc",    # Fernet ciphertext, never plaintext
    "path": "sftp_remote_path",
    "schedule": "sftp_schedule",        # "HH:MM", UK LOCAL (see below)
    # Empty on a database written before the schedule became UK local,
    # "uk" once it holds a local time. Absent means the stored value is
    # still the old UTC one and is converted on the way out, so a
    # deployment that never runs the migration command cannot quietly
    # start backing up an hour off.
    "schedule_tz": "sftp_schedule_tz",
    "keep": "sftp_keep",                # how many to keep ON THE NAS
}
SFTP_DEFAULT_PORT = 22
SFTP_DEFAULT_SCHEDULE = "02:30"
SFTP_DEFAULT_KEEP = 14
SFTP_TIMEOUT = 20            # seconds; a NAS asleep must not hang a page
SFTP_MAX_ATTEMPTS = 2        # the first go, then ONE retry that day


def fernet():
    """The cipher, or None when no key is configured.

    Without FERNET_KEY nothing can be stored: the settings page says so
    rather than pretending to keep a password it cannot protect.
    """
    key = os.environ.get("FERNET_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode("ascii"))
    except Exception:
        return None


def encrypt_secret(plaintext):
    cipher = fernet()
    if cipher is None or not plaintext:
        return ""
    return cipher.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(stored):
    """Plaintext, or "" if there is nothing stored or the key is wrong."""
    cipher = fernet()
    if cipher is None or not stored:
        return ""
    try:
        return cipher.decrypt(stored.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def sftp_settings():
    """Every transfer setting, ready for the page. NEVER the password."""
    rows = {b.key: (b.value or "").strip() for b in
            Block.query.filter(Block.key.in_(SFTP_KEYS.values())).all()}
    port = rows.get(SFTP_KEYS["port"], "")
    keep = rows.get(SFTP_KEYS["keep"], "")
    return {
        "enabled": rows.get(SFTP_KEYS["enabled"], "") == "1",
        "host": rows.get(SFTP_KEYS["host"], ""),
        "port": int(port) if port.isdigit() else SFTP_DEFAULT_PORT,
        "user": rows.get(SFTP_KEYS["user"], ""),
        "path": rows.get(SFTP_KEYS["path"], ""),
        "schedule": schedule_in_uk(
            rows.get(SFTP_KEYS["schedule"], "") or SFTP_DEFAULT_SCHEDULE,
            rows.get(SFTP_KEYS["schedule_tz"], "")),
        "schedule_migrated":
            rows.get(SFTP_KEYS["schedule_tz"], "") == "uk",
        "keep": int(keep) if keep.isdigit() else SFTP_DEFAULT_KEEP,
        # Whether one is stored — never what it is.
        "password_set": bool(rows.get(SFTP_KEYS["password"], "")),
        "key_present": fernet() is not None,
    }


def schedule_in_uk(stored, marker, on=None):
    """The schedule as a UK local "HH:MM", converting a legacy UTC one.

    The field used to be UTC and is now UK local. A stored value written
    before that change is still UTC, and reading it as local would move
    every backup by an hour for half the year without a word to anybody
    — the failure this exists to prevent. So an unmarked value is
    CONVERTED ON THE WAY OUT, which means a deployment that never runs
    `migrate-backup-schedule` still behaves correctly and still shows the
    admin the time their backup actually happens.

    Converted against the offset in force TODAY, which is the ambiguous
    part and is discussed in the CLI command below.
    """
    if marker == "uk":
        return stored
    hour, minute = schedule_parts(stored)
    on = on or datetime.utcnow().date()
    return utc_as_uk(datetime(on.year, on.month, on.day, hour,
                              minute)).strftime("%H:%M")


def sftp_password():
    block = Block.query.filter_by(key=SFTP_KEYS["password"]).first()
    return decrypt_secret(block.value if block else "")


def sftp_ready():
    cfg = sftp_settings()
    return bool(cfg["enabled"] and cfg["host"] and cfg["user"]
                and cfg["path"] and cfg["password_set"] and cfg["key_present"])


def describe_sftp_failure(exc, cfg):
    """What went wrong, in a sentence, with no password in it."""
    import paramiko
    if isinstance(exc, paramiko.AuthenticationException):
        return ("the NAS rejected the username or password for %s"
                % cfg["user"])
    if isinstance(exc, paramiko.BadHostKeyException):
        return "the NAS presented a different host key than last time"
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return ("%s did not answer within %d seconds — is Tailscale up on "
                "both ends?" % (cfg["host"], SFTP_TIMEOUT))
    if isinstance(exc, socket.gaierror):
        return ("the name %s could not be looked up — check the Tailscale "
                "name or address" % cfg["host"])
    if isinstance(exc, ConnectionRefusedError):
        return ("nothing is listening on %s port %d"
                % (cfg["host"], cfg["port"]))
    if isinstance(exc, PermissionError):
        return "the remote path %s is not writable by %s" % (cfg["path"],
                                                             cfg["user"])
    if isinstance(exc, FileNotFoundError):
        return "the remote path %s does not exist" % cfg["path"]
    secret = sftp_password()
    text = "%s: %s" % (type(exc).__name__, exc)
    return text.replace(secret, "***") if secret else text


@contextmanager
def sftp_session(cfg=None, password=None):
    """An open SFTP connection, closed whatever happens."""
    import paramiko
    cfg = cfg or sftp_settings()
    password = sftp_password() if password is None else password
    client = paramiko.SSHClient()
    # The NAS is on the Tailscale network and is not in known_hosts on a
    # fresh VPS. AutoAdd is the pragmatic choice for a private tailnet
    # where the transport is already authenticated and encrypted; it
    # would NOT be acceptable over the open internet.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sftp = None
    try:
        client.connect(cfg["host"], port=cfg["port"], username=cfg["user"],
                       password=password, timeout=SFTP_TIMEOUT,
                       banner_timeout=SFTP_TIMEOUT,
                       auth_timeout=SFTP_TIMEOUT,
                       look_for_keys=False, allow_agent=False)
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(SFTP_TIMEOUT)
        yield sftp
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        client.close()


def test_sftp(password=None):
    """Connect, prove the remote path is WRITABLE, clean up. (ok, message).

    Writing and deleting a probe file is the only honest test: a path can
    exist, be listable, and still refuse the upload at 2am.
    """
    cfg = sftp_settings()
    if not cfg["host"] or not cfg["user"] or not cfg["path"]:
        return False, "fill in the host, username and remote path first"
    secret = sftp_password() if password is None else password
    if not secret:
        return False, ("no password is stored — type one in and save, or "
                       "set FERNET_KEY on the server first"
                       if not cfg["key_present"] else
                       "no password is stored — type one in and save")
    probe = "ebwa-write-test-%s.tmp" % uuid.uuid4().hex[:8]
    try:
        with sftp_session(cfg, secret) as sftp:
            try:
                sftp.stat(cfg["path"])
            except IOError:
                return False, ("the remote path %s does not exist on the NAS"
                               % cfg["path"])
            remote = _remote_join(cfg["path"], probe)
            try:
                with sftp.file(remote, "w") as handle:
                    handle.write("ebwa write test")
                sftp.remove(remote)
            except IOError:
                return False, ("%s exists but %s cannot write to it"
                               % (cfg["path"], cfg["user"]))
    except Exception as exc:
        return False, describe_sftp_failure(exc, cfg)
    return True, ("connected to %s and wrote a test file into %s"
                  % (cfg["host"], cfg["path"]))


def _remote_join(path, name):
    return path.rstrip("/") + "/" + name


def upload_backup(run):
    """Send one finished archive to the NAS. Records the outcome on `run`.

    Never raises: a NAS that is off must not turn a backup that DID work
    into an error page or a failed cron job. The archive is on disk
    either way, and the row says the transfer failed and why.
    """
    cfg = sftp_settings()
    if not run or run.status != "ok" or not run.filename:
        return False
    if not sftp_ready():
        run.transfer_status = "none"
        db.session.commit()
        return False

    local = os.path.join(BACKUP_DIR, run.filename)
    if not os.path.isfile(local):
        run.transfer_status = "failed"
        run.transfer_error = "the archive is no longer on disk"
        db.session.commit()
        return False

    run.transfer_attempts = (run.transfer_attempts or 0) + 1
    remote = _remote_join(cfg["path"], run.filename)
    try:
        with sftp_session(cfg) as sftp:
            # Upload beside the final name, then rename: an interrupted
            # transfer leaves a .part, never a truncated archive that
            # looks complete.
            part = remote + ".part"
            sftp.put(local, part)
            try:
                sftp.remove(remote)
            except IOError:
                pass
            sftp.rename(part, remote)
        run.transfer_status = "ok"
        run.remote_filename = run.filename
        run.transfer_error = ""
        run.transferred_at = datetime.utcnow()
        db.session.commit()
        prune_remote_backups()
        return True
    except Exception as exc:
        run.transfer_status = "failed"
        run.transfer_error = describe_sftp_failure(exc, cfg)
        db.session.commit()
        return False


def prune_remote_backups(keep=None):
    """Keep the newest N archives on the NAS. Failure here is not fatal.

    Separate from local retention on purpose: the NAS has room for far
    more history than the VPS, and that is most of the point of it.
    """
    cfg = sftp_settings()
    keep = cfg["keep"] if keep is None else keep
    if not sftp_ready() or keep < 1:
        return 0
    removed = 0
    try:
        with sftp_session(cfg) as sftp:
            names = sorted(n for n in sftp.listdir(cfg["path"])
                           if n.startswith(BACKUP_PREFIX)
                           and n.endswith(".zip"))
            for name in names[:-keep]:
                try:
                    sftp.remove(_remote_join(cfg["path"], name))
                    removed += 1
                except IOError:
                    pass
    except Exception as exc:
        log_action("backup",
                   summary="Could not tidy old archives on the NAS — %s."
                           % describe_sftp_failure(exc, cfg))
    return removed


def transfer_with_retry(run):
    """Upload, and if it fails try ONCE more. Then leave it until tomorrow.

    Matching the behaviour the settings page promises: two goes and then
    silence, rather than a machine hammering a NAS that is switched off
    and filling the audit log while it does.
    """
    if upload_backup(run):
        return True
    if (run.transfer_attempts or 0) < SFTP_MAX_ATTEMPTS:
        time.sleep(2)
        if upload_backup(run):
            return True
    log_action("backup",
               summary=("Backup %s could not be sent to the NAS after %d "
                        "attempt(s) — %s. It stays on the server, and the "
                        "next scheduled run will try again."
                        % (run.filename, run.transfer_attempts or 0,
                           run.transfer_error)))
    return False


# --------------------------------------------------------- server health
# A READ-ONLY window on the machine, for the super admin who gets the
# call when something is slow. See CLAUDE.md for the constraints; the
# short version is that nothing here acts. No restart, no service
# control, no log tailing, no command execution beyond the one fixed
# `systemctl is-active` below, and no value from a request ever reaches a
# shell, a path or a unit name.
#
# Everything degrades: a metric that cannot be read on this machine —
# Windows in development, a container without /proc — comes back None and
# the panel says "not available here" rather than the page erroring.
APP_STARTED_AT = datetime.utcnow()
HEALTH_UNITS = (DEPLOY_SERVICE, "nginx")   # fixed at startup, never from a
# request
SYSTEMCTL_TIMEOUT = 3


def _psutil():
    """psutil if it is installed, else None. It is in requirements, but
    the panel must not be the reason a deploy that skipped pip install
    cannot start."""
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def health_cpu():
    ps = _psutil()
    cores = os.cpu_count()
    load = None
    if hasattr(os, "getloadavg"):
        try:
            load = [round(v, 2) for v in os.getloadavg()]
        except OSError:
            load = None
    elif ps is not None and hasattr(ps, "getloadavg"):
        try:
            load = [round(v, 2) for v in ps.getloadavg()]
        except Exception:
            load = None
    # Load against cores is the only reading that means anything: 4.0 is
    # a quiet afternoon on eight cores and a queue on one.
    per_core = round(load[0] / cores, 2) if load and cores else None
    return {"cores": cores, "load": load, "per_core": per_core,
            "level": _level(per_core * 100 if per_core is not None else None,
                            amber=70, red=100)}


def health_memory():
    ps = _psutil()
    if ps is not None:
        try:
            mem = ps.virtual_memory()
            return {"total": mem.total, "used": mem.total - mem.available,
                    "available": mem.available,
                    "percent": round(mem.percent, 1),
                    "level": _level(mem.percent, amber=80, red=92)}
        except Exception:
            pass
    info = _meminfo()          # /proc/meminfo, where there is one
    if not info:
        return {"total": None, "used": None, "available": None,
                "percent": None, "level": "unknown"}
    total, available = info["MemTotal"], info.get("MemAvailable", 0)
    used = total - available
    percent = round(used * 100.0 / total, 1) if total else None
    return {"total": total, "used": used, "available": available,
            "percent": percent, "level": _level(percent, amber=80, red=92)}


def health_swap():
    """Swap, read beside memory rather than on its own.

    2GB of memory at 85% with no swap is a machine about to start
    killing processes; the same figure with swap behind it is a machine
    with somewhere to go. The two only mean something together, which is
    why the panel shows them side by side.

    Heavy swap USE is its own warning, even when memory looks
    comfortable: it means the machine has already been under pressure
    and is now reading pages back off a disk, which is where a site
    starts feeling slow for reasons the memory number does not explain.

    Degrades to "not available" rather than raising: a machine with no
    swap configured is a normal machine, and so is a Windows development
    box where the numbers do not mean the same thing.
    """
    ps = _psutil()
    if ps is not None:
        try:
            sw = ps.swap_memory()
            if not sw.total:
                return {"total": 0, "used": 0, "free": 0, "percent": None,
                        "level": "none"}
            return {"total": sw.total, "used": sw.used, "free": sw.free,
                    "percent": round(sw.percent, 1),
                    "level": _level(sw.percent, amber=25, red=60)}
        except Exception:
            pass
    info = _meminfo()          # /proc/meminfo, where there is one
    total = info.get("SwapTotal")
    if total is None:
        return {"total": None, "used": None, "free": None, "percent": None,
                "level": "unknown"}
    if not total:
        return {"total": 0, "used": 0, "free": 0, "percent": None,
                "level": "none"}
    free = info.get("SwapFree", 0)
    used = total - free
    percent = round(used * 100.0 / total, 1)
    # Amber at a QUARTER, not at the 80% memory uses. Swap is the
    # overflow: any sustained use of it is worth knowing about, where
    # memory sitting at 60% is simply a machine doing its job.
    return {"total": total, "used": used, "free": free, "percent": percent,
            "level": _level(percent, amber=25, red=60)}


# How many failed sign-ins is unusual for a DAY. A community charity has
# a handful of admins: a wrong password now and then is ordinary, a
# dozen in one day is somebody trying. The thresholds are for the "today"
# figure; the week and month are context for it rather than alarms of
# their own.
SIGNIN_AMBER, SIGNIN_RED = 6, 20


def health_signins():
    """Failed sign-ins today, this week and this month.

    ONE TOTAL, not three kinds. All three failures — a wrong password, a
    wrong two-factor code, and an attempt refused by the rate limiter —
    are recorded under the same `login_failed` action and told apart
    only by the sentence in their summary. Counting them separately
    would mean matching on that prose, which is written for a trustee to
    read and is free to change; and operationally the number that
    matters is "how many times did somebody fail to get in", not which
    door they were stopped at. The breakdown is one click away in the
    log itself, which is what the figures link to.

    Day boundaries are UK LOCAL, per the admin date convention: "today"
    means today as somebody in Enfield would count it, even though the
    column is naive UTC.
    """
    today = utc_as_uk(datetime.utcnow()).date()
    spans = (("today", today), ("week", today - timedelta(days=6)),
             ("month", today - timedelta(days=29)))
    out = {}
    for name, start in spans:
        out[name] = db.session.query(db.func.count(AuditLog.id)).filter(
            AuditLog.action == FAILED_LOGIN_ACTION,
            AuditLog.created_at >= uk_midnight_as_utc(start)).scalar() or 0
        out[name + "_from"] = start.isoformat()
    out["to"] = today.isoformat()
    out["action"] = FAILED_LOGIN_ACTION
    out["level"] = _level(out["today"], amber=SIGNIN_AMBER, red=SIGNIN_RED)
    return out


def _meminfo():
    """/proc/meminfo as bytes, or {} where there is no /proc."""
    try:
        with open("/proc/meminfo", "r") as fh:
            out = {}
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    out[parts[0].rstrip(":")] = int(parts[1]) * 1024
            return out
    except OSError:
        return {}


def health_disk():
    """The filesystem holding the app, plus what this app puts on it."""
    try:
        usage = shutil.disk_usage(BASE_DIR)
        percent = round(usage.used * 100.0 / usage.total, 1) \
            if usage.total else None
        disk = {"total": usage.total, "used": usage.used,
                "free": usage.free, "percent": percent,
                "level": _level(percent, amber=80, red=92)}
    except OSError:
        disk = {"total": None, "used": None, "free": None, "percent": None,
                "level": "unknown"}
    paths = backup_paths()
    database = (os.path.getsize(paths["database"])
                if paths["database"] and os.path.isfile(paths["database"])
                else 0)
    uploads, upload_files = _dir_size(paths["uploads"])
    backups, backup_files = _dir_size(paths["dir"])
    disk.update({"database": database, "uploads": uploads,
                 "upload_files": upload_files, "backups": backups,
                 "backup_files": backup_files})
    return disk


def health_uptime():
    ps = _psutil()
    boot = None
    if ps is not None:
        try:
            boot = datetime.utcfromtimestamp(ps.boot_time())
        except Exception:
            boot = None
    if boot is None:
        try:                    # /proc/uptime: seconds since boot
            with open("/proc/uptime", "r") as fh:
                boot = datetime.utcnow() - timedelta(
                    seconds=float(fh.read().split()[0]))
        except (OSError, ValueError, IndexError):
            boot = None
    return {"boot": boot, "app_started": APP_STARTED_AT,
            "boot_seconds": (datetime.utcnow() - boot).total_seconds()
            if boot else None,
            "app_seconds": (datetime.utcnow()
                            - APP_STARTED_AT).total_seconds()}


def health_services():
    """Whether the units are active, via ONE fixed command per unit.

    The names come from HEALTH_UNITS, decided at startup from the
    environment — never from a request, never joined with anything a
    request supplied. No shell: a fixed argv list, so there is nothing to
    quote and nothing to escape.
    """
    out = []
    systemctl = shutil.which("systemctl")
    for unit in HEALTH_UNITS:
        if not systemctl:
            out.append({"unit": unit, "state": None,
                        "note": "systemd is not available here"})
            continue
        try:
            result = subprocess.run(
                [systemctl, "is-active", "--quiet", unit],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=SYSTEMCTL_TIMEOUT, shell=False)
            out.append({"unit": unit,
                        "state": "active" if result.returncode == 0
                                 else "inactive", "note": ""})
        except Exception as exc:
            out.append({"unit": unit, "state": None,
                        "note": type(exc).__name__})
    return out


def health_network():
    """Bytes in and out since boot. Deliberately NOT a speed test: that
    means putting traffic on a client's server to make a number."""
    ps = _psutil()
    if ps is not None:
        try:
            io = ps.net_io_counters()
            return {"sent": io.bytes_sent, "received": io.bytes_recv}
        except Exception:
            pass
    try:
        sent = received = 0
        with open("/proc/net/dev", "r") as fh:
            for line in fh.readlines()[2:]:
                name, _, rest = line.partition(":")
                if name.strip() == "lo":
                    continue
                parts = rest.split()
                received += int(parts[0])
                sent += int(parts[8])
        return {"sent": sent, "received": received}
    except (OSError, ValueError, IndexError):
        return {"sent": None, "received": None}


def app_version():
    """The deployed commit, read from .git — no subprocess, no git needed.

    Returns the short hash, or None outside a checkout (a release
    unpacked from an archive, for instance).
    """
    head = os.path.join(BASE_DIR, ".git", "HEAD")
    try:
        with open(head, "r") as fh:
            ref = fh.read().strip()
        if ref.startswith("ref: "):
            name = ref[5:]
            # Only ever a path INSIDE .git, from .git's own contents.
            direct = os.path.join(BASE_DIR, ".git", *name.split("/"))
            if os.path.isfile(direct):
                with open(direct, "r") as fh:
                    return fh.read().strip()[:7]
            packed = os.path.join(BASE_DIR, ".git", "packed-refs")
            if os.path.isfile(packed):
                with open(packed, "r") as fh:
                    for line in fh:
                        if line.rstrip().endswith(" " + name):
                            return line.split()[0][:7]
            return None
        return ref[:7]
    except (OSError, IndexError):
        return None


def schema_state():
    """What `check-schema` would say, as a fact rather than an exit code."""
    try:
        from sqlalchemy import inspect
        insp = inspect(db.engine)
        present = set(insp.get_table_names())
        missing_tables, missing_columns = [], []
        for table in db.metadata.sorted_tables:
            if table.name not in present:
                missing_tables.append(table.name)
                continue
            actual = {c["name"] for c in insp.get_columns(table.name)}
            missing_columns += ["%s.%s" % (table.name, col.name)
                                for col in table.columns
                                if col.name not in actual]
        return {"tables": len(db.metadata.sorted_tables),
                "missing_tables": missing_tables,
                "missing_columns": missing_columns,
                "ok": not missing_tables and not missing_columns}
    except Exception as exc:
        return {"tables": None, "missing_tables": [], "missing_columns": [],
                "ok": None, "error": type(exc).__name__}


def _level(percent, amber, red):
    """green / amber / red, or unknown when there is nothing to judge."""
    if percent is None:
        return "unknown"
    if percent >= red:
        return "red"
    if percent >= amber:
        return "amber"
    return "green"


def server_health():
    """Every metric, in one dict. Read-only from top to bottom."""
    return {"cpu": health_cpu(), "memory": health_memory(),
            "swap": health_swap(),
            "disk": health_disk(), "uptime": health_uptime(),
            "signins": health_signins(),
            "services": health_services(), "network": health_network(),
            "python": platform.python_version(),
            "system": "%s %s" % (platform.system(), platform.release()),
            "version": app_version(), "schema": schema_state(),
            "psutil": _psutil() is not None,
            "checked_at": datetime.utcnow()}


@app.template_filter("filesize")
def filesize_filter(size):
    """Bytes as something a person reads: 1.4 MB, 812 KB."""
    if size is None:
        return "unknown"
    size = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d %s" % (size, unit) if unit == "bytes"
                    else "%.1f %s" % (size, unit))
        size /= 1024.0


# ---------------------------------------------------------------- public
@app.route("/")
def home():
    content = blocks_for("home")
    upcoming = (Event.query
                .filter_by(published=True)
                .filter(Event.event_date >= date.today())
                .order_by(Event.event_date.asc(), Event.id.asc())
                .limit(EVENT_FETCH).all())
    upcoming = events_in_day_order(upcoming)[:3]
    latest_news = []
    if feature_enabled("news"):
        latest_news = (NewsPost.query.filter_by(published=True)
                       .order_by(NewsPost.published_date.desc(),
                                 NewsPost.created_at.desc(),
                                 NewsPost.id.desc())
                       .limit(3).all())
    campaigns = []
    if feature_enabled("donations"):
        # OPEN only. The strip is an invitation to give; a finished
        # collection belongs on /collections, where it reads as a record.
        campaigns = (Campaign.query.filter_by(state="open")
                     .order_by(Campaign.created_at.desc(),
                               Campaign.id.desc()).all())
    testimonials = (Testimonial.query.filter_by(published=True)
                    .order_by(Testimonial.sort,
                              Testimonial.created_at.desc(),
                              Testimonial.id.desc())
                    .limit(HOME_TESTIMONIALS).all())
    partners = (Partner.query
                .order_by(Partner.sort, Partner.name, Partner.id).all())
    services = (Service.query.filter_by(published=True)
                .order_by(Service.sort, Service.id).all())
    # Which sections render, in which order, with which band — resolved
    # in one place from the Settings order, the per-section switches and
    # what there is to show. The template just loops over the answer.
    home_bands = home_layout({"services": services, "events": upcoming,
                              "news": latest_news, "collections": campaigns,
                              "testimonials": testimonials,
                              "partners": partners})
    return render_template("index.html", c=content, upcoming=upcoming,
                           home_bands=home_bands,
                           latest_news=latest_news, campaigns=campaigns,
                           testimonials=testimonials, partners=partners,
                           motion=partner_motion(),
                           quote_motion=row_motion("testimonials"),
                           partner_min=ROW_SCROLLER_MIN["partners"],
                           quote_min=ROW_SCROLLER_MIN["testimonials"],
                           services=services)


@app.route("/subscribe", methods=["POST"])
def subscribe():
    email = request.form.get("email", "").lower().strip()
    if rate_limited("subscribe"):
        flash("Too many attempts — please try again a little later.", "error")
    elif not email or "@" not in email or len(email) > 200:
        flash("Please enter a valid email address.", "error")
    elif Subscriber.query.filter_by(email=email).first():
        flash("You're already subscribed — thank you!", "ok")
    else:
        db.session.add(Subscriber(email=email))
        db.session.commit()
        flash("Thank you for subscribing to our newsletter!", "ok")
    return redirect(request.referrer or url_for("home"))


@app.route("/sitemap.xml")
def sitemap():
    base = request.url_root.rstrip("/")
    flags = feature_flags()
    pages = [   # endpoint, feature flag (None = core page, always listed)
        ("home", None), ("about", None), ("events", None),
        ("news", "news"), ("resources", "resources"),
        ("journey", "our_journey"), ("gallery", None),
        ("faq", "faq"), ("collections", "donations"),
        ("donate", "donations"),
        ("membership", "membership_form"), ("contact", None),
        ("privacy", None), ("terms", None)]
    urls = [url_for(e) for e, f in pages if f is None or flags[f]]
    urls += [url_for("event_detail", slug=ev.slug) for ev in
             Event.query.filter_by(published=True).all()]
    albums = GalleryAlbum.query.filter_by(published=True).all()
    urls += [url_for("gallery_album", slug=a.slug) for a in albums]
    if GalleryImage.query.first():
        urls.append(url_for("gallery_all"))
    if flags["news"]:
        urls += [url_for("news_detail", slug=p.slug) for p in
                 NewsPost.query.filter_by(published=True).all()]
    if flags["donations"]:
        # Both public states: a closed collection still has a page, and
        # a page nothing points a crawler at is a page nobody finds.
        # Hidden ones stay out, exactly as a hidden album's photos do.
        urls += [url_for("collection_detail", slug=c.slug) for c in
                 Campaign.query.filter(
                     Campaign.state.in_(PUBLIC_CAMPAIGN_STATES)).all()]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        xml.append("<url><loc>%s%s</loc></url>" % (base, u))
    xml.append("</urlset>")
    return app.response_class("\n".join(xml), mimetype="application/xml")


@app.route("/healthz")
def healthz():
    db.session.execute(db.text("SELECT 1"))   # confirms the db is reachable
    return "ok"


@app.route("/robots.txt")
def robots():
    base = request.url_root.rstrip("/")
    body = "User-agent: *\nDisallow: /admin\nSitemap: %s/sitemap.xml\n" % base
    return app.response_class(body, mimetype="text/plain")


def about_video():
    """About's video, built HERE rather than in the template.

    Every other owner hands `video_of()` a row and it reads the fields
    off it. About has no row — its settings are Blocks — so something
    has to assemble an object-shaped thing, and that something used to
    be about.html, listing the field names by hand.

    That is how the position setting came to be saved correctly and
    ignored completely: `video_position` was added to `video_of()` and
    to every model, and the one hand-built dict in a template was not
    updated. `video_of()` then read no key, got "", and fell back to
    the lead slot — the safe default doing its job, which is exactly
    why nothing broke loudly.

    Built from the same accessors the admin writes through, so the
    field names live in one place and a future field cannot go missing
    from About alone.
    """
    url, thumb = video_settings_for("about")
    return video_of({"video_url": url, "video_thumb": thumb,
                     "video_position": video_position_for("about")},
                    blocks_for("about").get("about_image", ""))


@app.route("/about")
def about():
    c = blocks_for("about")
    layout, images = rich_content_for("about")
    return render_template("about.html", c=c, layout=layout, images=images,
                           video=about_video(),
                           paragraphs=paragraphs_of(c.get("about_body", "")))


@app.route("/events")
def events():
    today = date.today()
    upcoming = events_in_day_order(
        Event.query.filter_by(published=True)
        .filter(Event.event_date >= today)
        .order_by(Event.event_date.asc(), Event.id.asc()).all())
    past = events_in_day_order(
        Event.query.filter_by(published=True)
        .filter(Event.event_date < today)
        .order_by(Event.event_date.desc(), Event.id.desc())
        .limit(EVENT_FETCH).all(), newest_day_first=True)[:12]
    return render_template("events.html", upcoming=upcoming, past=past)


@app.route("/events/<slug>")
def event_detail(slug):
    ev = Event.query.filter_by(slug=slug, published=True).first_or_404()
    layout, images = rich_content_for("event", ev.id)
    return render_template("event_detail.html", ev=ev, layout=layout,
                           images=images,
                           paragraphs=paragraphs_of(ev.description))


@app.route("/news")
@feature_required("news")
def news():
    posts = (NewsPost.query.filter_by(published=True)
             .order_by(NewsPost.published_date.desc(),
                       NewsPost.created_at.desc(),
                       NewsPost.id.desc()).all())
    return render_template("news.html", posts=posts)


@app.route("/news/<slug>")
@feature_required("news")
def news_detail(slug):
    post = NewsPost.query.filter_by(slug=slug, published=True).first_or_404()
    layout, images = rich_content_for("news_post", post.id)
    return render_template("news_detail.html", post=post, layout=layout,
                           images=images, paragraphs=paragraphs_of(post.body))


# ------------------------------------------------------------ gallery
# The gallery is a showcase, so the shapes of the photographs matter: a
# masonry column layout gives every picture its own aspect ratio instead
# of cropping portrait phone photos into squares. That needs each ratio
# BEFORE the image loads, or the page reflows as they arrive.
ALL_PHOTOS_SLUG = "all"        # /gallery/all — reserved, never an album
_ratio_cache = {}              # filename -> (mtime, size, "w / h")


def aspect_ratio_of(filename, default="4 / 3"):
    """The photo's own aspect ratio, as a CSS aspect-ratio value.

    Read from the file header — Pillow does not decode the pixels for
    this — and cached per worker on (mtime, size), so a page of thirty
    photographs costs thirty stats once and nothing thereafter. Kept out
    of the database deliberately: no column to backfill, no way for the
    stored number to drift from the file on disk.
    """
    if not filename:
        return default
    path = os.path.join(UPLOAD_DIR, secure_filename(filename))
    try:
        stat = os.stat(path)
    except OSError:
        return default
    key = (stat.st_mtime, stat.st_size)
    cached = _ratio_cache.get(filename)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            with Image.open(fh) as im:
                ratio = "%d / %d" % im.size
    except Exception:
        ratio = default
    _ratio_cache[filename] = (key, ratio)
    return ratio


def gallery_photos(album=None, unfiled_only=False):
    """Photos for a view, newest first unless `sort` says otherwise.

    sort ascending comes first so an admin can pin a photograph to the
    top of an album; everything else falls back to newest.
    """
    q = GalleryImage.query
    if album is not None:
        q = q.filter(GalleryImage.album_id == album.id)
    elif unfiled_only:
        q = q.filter(GalleryImage.album_id.is_(None))
    else:
        # "All photos": everything except what sits in a hidden album.
        hidden = db.select(GalleryAlbum.id).where(
            GalleryAlbum.published == False)               # noqa: E712
        q = q.filter(db.or_(GalleryImage.album_id.is_(None),
                            GalleryImage.album_id.notin_(hidden)))
    return q.order_by(GalleryImage.sort,
                      GalleryImage.created_at.desc(),
                      GalleryImage.id.desc()).all()


def with_ratios(photos):
    """Photos paired with their aspect ratio, ready for the template."""
    return [{"img": p, "ratio": aspect_ratio_of(p.filename)} for p in photos]


@app.route("/gallery")
def gallery():
    """Album covers, plus a way into everything that is not in an album."""
    albums = (GalleryAlbum.query.filter_by(published=True)
              .order_by(GalleryAlbum.sort, GalleryAlbum.created_at.desc(),
                        GalleryAlbum.id.desc())
              .all())
    # One grouped count rather than a query per album.
    counts = dict(db.session.query(GalleryImage.album_id,
                                   db.func.count(GalleryImage.id))
                  .group_by(GalleryImage.album_id).all())
    covers = {}
    for a in albums:
        if not a.cover_image:
            newest = (GalleryImage.query.filter_by(album_id=a.id)
                      .order_by(GalleryImage.sort,
                                GalleryImage.created_at.desc(),
                                GalleryImage.id.desc()).first())
            covers[a.id] = newest.filename if newest else ""
    cards = [{"album": a,
              "count": counts.get(a.id, 0),
              "cover": a.cover_image or covers.get(a.id, "")}
             for a in albums]
    # What "All photos" will show: the unfiled ones plus everything in a
    # published album. Photos inside a hidden album are not counted,
    # because that view will not show them either.
    unfiled = counts.get(None, 0)
    total = unfiled + sum(n for aid, n in counts.items()
                          if aid in {a.id for a in albums})
    return render_template("gallery.html", cards=cards, total=total,
                           unfiled=unfiled)


@app.route("/gallery/" + ALL_PHOTOS_SLUG)
def gallery_all():
    photos = gallery_photos()
    return render_template("gallery_album.html", album=None,
                           title="All photos",
                           description="Every photograph on the site, newest "
                                       "first.",
                           photos=with_ratios(photos))


@app.route("/gallery/<slug>")
def gallery_album(slug):
    album = GalleryAlbum.query.filter_by(slug=slug,
                                         published=True).first_or_404()
    return render_template("gallery_album.html", album=album,
                           title=album.title, description=album.description,
                           photos=with_ratios(gallery_photos(album=album)))


@app.route("/resources")
@feature_required("resources")
def resources():
    rows = Resource.query.order_by(Resource.category, Resource.sort,
                                   Resource.name, Resource.id).all()
    grouped = []   # [(category, [resources])], in query order
    for r in rows:
        if grouped and grouped[-1][0] == r.category:
            grouped[-1][1].append(r)
        else:
            grouped.append((r.category, [r]))
    return render_template("resources.html", grouped=grouped)


@app.route("/faq")
@feature_required("faq")
def faq():
    """Questions grouped by category, ungrouped ones first.

    Ordering is category, then `sort`, then oldest first, so an admin can
    lift the question everybody asks to the top of its group without
    renumbering the rest.
    """
    rows = (Faq.query.filter_by(published=True)
            .order_by(Faq.category, Faq.sort, Faq.id).all())
    grouped = []   # [(category, [faqs])], "" first — see the ordering above
    for row in rows:
        if grouped and grouped[-1][0] == row.category:
            grouped[-1][1].append(row)
        else:
            grouped.append((row.category, [row]))
    # The same answers again, as FAQPage structured data. Google shows
    # these under the search result, which is worth having for a charity
    # nobody is searching for by name yet.
    schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question",
             "name": row.question,
             "acceptedAnswer": {
                 "@type": "Answer",
                 "text": " ".join(paragraphs_of(row.answer))}}
            for row in rows],
    }
    return render_template("faq.html", grouped=grouped, count=len(rows),
                           schema=json.dumps(schema, ensure_ascii=False))


@app.route("/our-journey")
@feature_required("our_journey")
def journey():
    rows = (Milestone.query.filter_by(published=True)
            .order_by(Milestone.year.desc(), Milestone.sort,
                      Milestone.title, Milestone.id).all())
    rich = rich_content_for_many("milestone", rows)
    grouped = []   # [(year, [entries])], in query order
    for m in rows:
        layout, images = rich[m.id]
        # The summary is the opening line of the entry, as it always was,
        # with the outcome's paragraphs after it.
        paragraphs = ([m.summary] if m.summary else []) +             paragraphs_of(m.outcome)
        entry = {"m": m, "layout": layout, "images": images,
                 "paragraphs": paragraphs}
        if grouped and grouped[-1][0] == m.year:
            grouped[-1][1].append(entry)
        else:
            grouped.append((m.year, [entry]))
    return render_template("journey.html", c=blocks_for("journey"),
                           grouped=grouped)


@app.route("/donate", methods=["GET", "POST"])
@feature_required("donations")
def donate():
    if request.method == "POST":
        if rate_limited("donate"):
            flash("Too many attempts — please try again a little later.",
                  "error")
            return render_template("donate.html")
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").lower().strip()
        pence = parse_pounds(request.form.get("amount", ""))
        wants_gift_aid = request.form.get("gift_aid") == "on"
        ga_name = request.form.get("gift_aid_name", "").strip()
        ga_address = request.form.get("gift_aid_address", "").strip()
        ga_postcode = request.form.get("gift_aid_postcode", "").upper().strip()
        ga_declared = request.form.get("gift_aid_declaration") == "on"

        if not name or not email or "@" not in email or len(email) > 200:
            flash("Please give us your name and a valid email address.", "error")
        elif pence is None or pence < 100 or pence > 1000000:
            flash("Please enter an amount between £1 and £10,000.", "error")
        elif wants_gift_aid and not (ga_name and ga_address and ga_postcode
                                     and ga_declared):
            flash("To add Gift Aid we need your full name, house name or "
                  "number, postcode and the taxpayer declaration tick-box.",
                  "error")
        else:
            try:
                session = stripe.checkout.Session.create(
                    mode="payment",
                    line_items=[{"price_data": {
                        "currency": "gbp",
                        "unit_amount": pence,
                        "product_data": {"name": "Donation to EBWA"},
                    }, "quantity": 1}],
                    customer_email=email,
                    success_url=url_for("donate_success", _external=True),
                    cancel_url=url_for("donate_cancelled", _external=True),
                )
            except Exception:
                flash("Sorry — we couldn't start the payment. Please try "
                      "again, or call the centre to donate.", "error")
            else:
                # General donation: 100% donation_pence, no fee (CLAUDE.md)
                p = Payment()
                p.campaign_id = None
                p.name = name
                p.email = email
                p.fee_pence = 0
                p.donation_pence = pence
                p.gift_aid = wants_gift_aid
                p.gift_aid_name = ga_name if wants_gift_aid else ""
                p.gift_aid_address = ga_address if wants_gift_aid else ""
                p.gift_aid_postcode = ga_postcode if wants_gift_aid else ""
                p.stripe_session_id = session.id
                p.status = "pending"
                db.session.add(p)
                db.session.commit()
                return redirect(session.url, code=303)
    return render_template("donate.html")


@app.route("/collections")
@feature_required("donations")
def collections():
    """Every open collection, so campaigns are reachable from the menu.

    They were only ever linked from the homepage strip, which meant a
    campaign fell off the site the moment three newer things pushed it
    out of that row.
    """
    open_now = (Campaign.query.filter_by(state="open")
                .order_by(Campaign.created_at.desc(),
                          Campaign.id.desc()).all())
    # CLOSED, not "everything that is not open". A hidden collection is
    # off the website; it must not reappear in the Completed section,
    # which is the bug the old active=False filter would have become the
    # moment there were three states.
    closed = (Campaign.query.filter_by(state="closed")
              .order_by(Campaign.created_at.desc(),
                        Campaign.id.desc()).all())
    return render_template("collections.html", open_now=open_now,
                           closed=closed)


@app.route("/collections/<slug>", methods=["GET", "POST"])
@feature_required("donations")
def collection_detail(slug):
    camp = (Campaign.query
            .filter(Campaign.slug == slug,
                    Campaign.state.in_(PUBLIC_CAMPAIGN_STATES))
            .first_or_404())
    if request.method == "POST":
        # A CLOSED COLLECTION REFUSES THE PAYMENT ITSELF. Hiding the form
        # is a courtesy to the person reading the page; it is not a
        # control. A stale tab, a back button, a bookmarked POST or
        # anything typing at the endpoint directly all arrive here, and
        # taking money for a trip that has been and gone is not a thing
        # to leave resting on a template `{% if %}`.
        if not camp.takes_payments:
            flash("This collection has finished, so it is no longer taking "
                  "payments. Thank you for wanting to give — our "
                  "donations page supports everything we do.", "error")
            return redirect(url_for("collection_detail", slug=camp.slug))
        if rate_limited("donate"):
            flash("Too many attempts — please try again a little later.",
                  "error")
            return render_template("collection_detail.html", camp=camp)
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").lower().strip()
        include_fee = bool(camp.fee_pence) and \
            request.form.get("include_fee") == "on"
        fee_pence = camp.fee_pence if include_fee else 0
        donation_raw = request.form.get("donation", "").strip()
        donation_pence = parse_pounds(donation_raw) if donation_raw else 0
        wants_gift_aid = request.form.get("gift_aid") == "on"
        ga_name = request.form.get("gift_aid_name", "").strip()
        ga_address = request.form.get("gift_aid_address", "").strip()
        ga_postcode = request.form.get("gift_aid_postcode", "").upper().strip()
        ga_declared = request.form.get("gift_aid_declaration") == "on"

        # HMRC rule (CLAUDE.md): the fee pays for a benefit and can NEVER
        # carry Gift Aid. No donation portion means no Gift Aid, whatever
        # the submitted form claims.
        if not donation_pence or donation_pence <= 0:
            wants_gift_aid = False

        if not name or not email or "@" not in email or len(email) > 200:
            flash("Please give us your name and a valid email address.", "error")
        elif donation_pence is None or donation_pence < 0 \
                or donation_pence > 1000000:
            flash("Please enter a valid donation amount (up to £10,000).",
                  "error")
        elif fee_pence + donation_pence < 100:
            flash("Please choose a place or enter a donation of at least £1.",
                  "error")
        elif wants_gift_aid and not (ga_name and ga_address and ga_postcode
                                     and ga_declared):
            flash("To add Gift Aid we need your full name, house name or "
                  "number, postcode and the taxpayer declaration tick-box.",
                  "error")
        else:
            line_items = []
            if fee_pence:
                line_items.append({"price_data": {
                    "currency": "gbp",
                    "unit_amount": fee_pence,
                    "product_data": {"name": "%s — place" % camp.title},
                }, "quantity": 1})
            if donation_pence:
                line_items.append({"price_data": {
                    "currency": "gbp",
                    "unit_amount": donation_pence,
                    "product_data": {"name": "Donation — %s" % camp.title},
                }, "quantity": 1})
            try:
                session = stripe.checkout.Session.create(
                    mode="payment",
                    line_items=line_items,
                    customer_email=email,
                    success_url=url_for("donate_success", _external=True),
                    cancel_url=url_for("collection_detail", slug=camp.slug,
                                       _external=True),
                )
            except Exception:
                flash("Sorry — we couldn't start the payment. Please try "
                      "again, or call the centre.", "error")
            else:
                p = Payment()
                p.campaign_id = camp.id
                p.name = name
                p.email = email
                p.fee_pence = fee_pence
                p.donation_pence = donation_pence
                p.gift_aid = wants_gift_aid
                p.gift_aid_name = ga_name if wants_gift_aid else ""
                p.gift_aid_address = ga_address if wants_gift_aid else ""
                p.gift_aid_postcode = ga_postcode if wants_gift_aid else ""
                p.stripe_session_id = session.id
                p.status = "pending"
                db.session.add(p)
                db.session.commit()
                return redirect(session.url, code=303)
    return render_template("collection_detail.html", camp=camp)


@app.route("/donate/success")
@feature_required("donations")
def donate_success():
    return render_template("donate_success.html")


@app.route("/donate/cancelled")
@feature_required("donations")
def donate_cancelled():
    return render_template("donate_cancelled.html")


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    # Signature-verified and idempotent (CLAUDE.md donations rules)
    try:
        event = stripe.Webhook.construct_event(
            request.get_data(),
            request.headers.get("Stripe-Signature", ""),
            STRIPE_WEBHOOK_SECRET)
    except Exception:
        abort(400)
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        p = Payment.query.filter_by(stripe_session_id=session["id"]).first()
        if p and p.status != "complete":   # replays are a no-op
            p.status = "complete"
            db.session.commit()
    return "", 200


@app.route("/membership", methods=["GET", "POST"])
@feature_required("membership_form")
def membership():
    if request.method == "POST":
        # Honeypot: real visitors never see this field. Pretend success so
        # bots get no signal, but store nothing.
        if request.form.get("website", ""):
            flash("Thank you — we've received your application and will "
                  "be in touch soon.", "ok")
            return redirect(url_for("membership"))
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").lower().strip()
        all_ticked = all(request.form.get(f) == "on" for f in
                         ("over_18", "bangladeshi_origin",
                          "lives_works_enfield", "fee_confirmed"))
        if not name or not email or "@" not in email or len(email) > 200:
            flash("Please give us your name and a valid email address.",
                  "error")
        elif not all_ticked:
            flash("Please confirm all four membership declarations — they "
                  "are required by the EBWA constitution.", "error")
        else:
            application = MembershipApplication()
            application.name = name
            application.email = email
            application.phone = request.form.get("phone", "").strip()
            application.address = request.form.get("address", "").strip()
            application.reason = request.form.get("reason", "").strip()
            application.over_18 = True
            application.bangladeshi_origin = True
            application.lives_works_enfield = True
            application.fee_confirmed = True
            db.session.add(application)
            db.session.commit()
            flash("Thank you — we've received your application and will "
                  "be in touch soon.", "ok")
            return redirect(url_for("membership"))
    return render_template("membership.html")


@app.route("/privacy")
def privacy():
    c = blocks_for("legal")
    return render_template("legal.html",
                           page_title=c.get("privacy_title", ""),
                           page_body=c.get("privacy_body", ""))


@app.route("/terms")
def terms():
    c = blocks_for("legal")
    return render_template("legal.html",
                           page_title=c.get("terms_title", ""),
                           page_body=c.get("terms_body", ""))


@app.route("/cookie-notice/dismiss", methods=["POST"])
def dismiss_cookie_notice():
    """Remember that the notice has been read, in a first-party cookie.

    Server-side rather than localStorage so no inline script is needed and
    the existing CSP stays as tight as it is. This is an acknowledgement,
    NOT a consent record — see the cookie note in CLAUDE.md.
    """
    target = request.form.get("next", "")
    if not target.startswith("/") or target.startswith("//"):
        target = url_for("home")      # never bounce off-site
    resp = redirect(target)
    resp.set_cookie(COOKIE_NOTICE_NAME, "1",
                    max_age=60 * 60 * 24 * 365, path="/",
                    httponly=True, samesite="Lax",
                    secure=request.is_secure)
    return resp


@app.route("/contact", methods=["GET", "POST"])
def contact():
    """The centre's details, and — behind the `contact_form` flag — a
    form for asking a question.

    The page itself is core and never goes away: with the form switched
    off the address, phone number and map are still here, which is what
    somebody looking for us actually needs.
    """
    form_on = feature_enabled("contact_form")

    if request.method == "POST":
        if not form_on:
            abort(404)
        # Honeypot: a real visitor never sees this field. Say thank you
        # anyway, so a bot learns nothing from the difference, and store
        # nothing.
        if request.form.get("website", ""):
            flash(CONTACT_THANKS, "ok")
            return redirect(url_for("contact"))

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        message = request.form.get("message", "").strip()

        if rate_limited("contact"):
            flash("You have sent us several messages already. Please give "
                  "us a little time to reply, or call the centre if it is "
                  "urgent.", "error")
        elif not too_quick():
            # A person cannot read the form and fill it in this fast; a
            # script fills it instantly. One more cheap signal alongside
            # the honeypot, not a security control.
            flash(CONTACT_THANKS, "ok")
            return redirect(url_for("contact"))
        elif not name or not email or "@" not in email or len(email) > 200:
            flash("Please give us your name and a valid email address so "
                  "we can reply.", "error")
        elif not message:
            flash("Please tell us how we can help.", "error")
        else:
            enquiry = ContactMessage()
            enquiry.name = name
            enquiry.email = email
            enquiry.phone = request.form.get("phone", "").strip()
            enquiry.subject = request.form.get("subject", "").strip()
            enquiry.message = message
            enquiry.ip = request.remote_addr or ""
            db.session.add(enquiry)
            db.session.commit()      # saved BEFORE anything can go wrong
            notify_enquiry(enquiry)  # never raises; logs its own failures
            flash(CONTACT_THANKS, "ok")
            return redirect(url_for("contact"))

    return render_template("contact.html", c=blocks_for("contact"),
                           form_on=form_on, started=int(time.time()))


CONTACT_THANKS = ("Thank you — your message is with us and we will reply "
                  "as soon as we can.")
MIN_FORM_SECONDS = 3


def too_quick():
    """False when the form came back faster than a person could type it.

    The timestamp is a plain hidden field: forgeable by anyone who looks,
    which is fine. It is here to stop the dumb bots that post every form
    they find in milliseconds, and it costs a visitor nothing.
    """
    try:
        started = int(request.form.get("started", "0"))
    except ValueError:
        return False
    return time.time() - started >= MIN_FORM_SECONDS


def notify_enquiry(enquiry):
    """Email the enquiry to whoever answers them, replying to the sender.

    Reply-To is the whole point: hitting reply in the mailbox writes
    straight back to the person who asked, with no copying of addresses.
    There is deliberately NO auto-reply to the enquirer yet — see
    CLAUDE.md; it needs a decision about what it should say and a
    bounce-handling story first.
    """
    lines = ["A message from the EBWA website.", "",
             "From:    %s <%s>" % (enquiry.name, enquiry.email)]
    if enquiry.phone:
        lines.append("Phone:   %s" % enquiry.phone)
    if enquiry.subject:
        lines.append("Subject: %s" % enquiry.subject)
    lines += ["Sent:    %s" % utc_as_uk(enquiry.created_at)
                                 .strftime("%d %b %Y at %H:%M"),
              "", enquiry.message, "",
              "-- ", "Reply to this email to answer them directly.",
              "It is also in the admin: %s"
              % url_for("admin_messages", _external=True)]
    subject = "EBWA enquiry: %s" % (enquiry.subject or enquiry.name)
    send_mail(mail_recipient(), subject, "\n".join(lines),
              reply_to=enquiry.email)


# ---------------------------------------------------------------- admin: auth
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        attempted = request.form.get("email", "").lower().strip()
        if rate_limited("login"):
            log_action("login_failed",
                       summary="Rate limited. Attempted email: %s" % attempted)
            flash("Too many login attempts — please wait ten minutes and "
                  "try again.", "error")
            return render_template("admin/login.html")
        user = User.query.filter_by(email=attempted).first()
        if user and user.check_password(request.form.get("password", "")):
            if user.totp_enabled:
                # Correct password is not enough: hand off to the code
                # step without creating the session.
                clear_pending_2fa()
                flask_session[PENDING_2FA_USER] = user.id
                flask_session[PENDING_2FA_AT] = int(time.time())
                return redirect(url_for("admin_login_2fa"))
            start_admin_session(user)
            log_action("login", summary="Password only (no two-factor).")
            return redirect(url_for("admin_dashboard"))
        # The attempted email is recorded; the password never is.
        log_action("login_failed",
                   summary="Attempted email: %s" % attempted)
        note_failed_login(attempted, request.remote_addr)
        flash("Incorrect email or password.", "error")
    return render_template("admin/login.html")


@app.route("/admin/login/2fa", methods=["GET", "POST"])
def admin_login_2fa():
    """Second login step: the 6-digit code (or a recovery code).

    Deliberately not @login_required — there is no session yet. Anyone
    without a live hand-off from the password step is sent back to the
    login page, so the route is useless on its own.
    """
    user = pending_2fa_user()
    if not user:
        return redirect(url_for("admin_login"))
    if request.method == "POST":
        if rate_limited("totp"):
            flash("Too many codes tried — please wait ten minutes and "
                  "start again.", "error")
            clear_pending_2fa()
            return redirect(url_for("admin_login"))
        code = request.form.get("code", "")
        if verify_totp(user, code):
            clear_pending_2fa()
            start_admin_session(user)
            log_action("login", summary="Password and two-factor code.")
            return redirect(url_for("admin_dashboard"))
        if use_recovery_code(user, code):
            clear_pending_2fa()
            start_admin_session(user)
            left = unused_recovery_codes(user)
            log_action("login", summary="Password and a recovery code — "
                                        "%d left." % left)
            flash("Recovery code accepted — that one is now used up and "
                  "you have %d left. If you have lost your authenticator, "
                  "turn two-factor authentication off and set it up again "
                  "on your new phone." % left, "ok")
            return redirect(url_for("admin_account"))
        log_action("login_failed",
                   summary="Wrong two-factor code. Attempted email: %s"
                           % user.email)
        flash("That code was not right. Codes change every 30 seconds — "
              "check your authenticator app and try the current one.",
              "error")
    return render_template("admin/login_2fa.html", email=user.email)


@app.route("/admin/logout")
@login_required
def admin_logout():
    log_action("logout")          # before the session goes, so it is attributed
    logout_user()
    return redirect(url_for("home"))


# ---------------------------------------------------------------- admin: account
MIN_PASSWORD_LEN = 10


@app.route("/admin/account", methods=["GET", "POST"])
@login_required
def admin_account():
    """Change your own password. Logged-in users only — there is
    deliberately no reset flow on the login page."""
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not current_user.check_password(current):
            flash("Your current password is not correct.", "error")
        elif len(new) < MIN_PASSWORD_LEN:
            flash("Your new password must be at least %d characters long."
                  % MIN_PASSWORD_LEN, "error")
        elif new != confirm:
            flash("The two new passwords do not match — please retype them.",
                  "error")
        else:
            current_user.set_password(new)
            db.session.commit()
            log_action("password_change", entity=current_user._get_current_object(),
                       summary="Changed their own password.")
            flash("Your password has been changed.", "ok")
            return redirect(url_for("admin_account"))
    return render_template("admin/account.html",
                           min_length=MIN_PASSWORD_LEN,
                           recovery_left=unused_recovery_codes(current_user))


@app.route("/admin/account/2fa/enable", methods=["GET", "POST"])
@login_required
def admin_2fa_enable():
    """Enrol this account in two-factor authentication.

    The secret is generated straight onto the User row (never into the
    session or a hidden form field) and only switches on once the user
    proves, with a working code, that their app has it too.
    """
    if current_user.totp_enabled:
        flash("Two-factor authentication is already switched on.", "error")
        return redirect(url_for("admin_account"))
    if not current_user.totp_secret:
        current_user.totp_secret = pyotp.random_base32()
        current_user.totp_last_counter = None
        db.session.commit()

    if request.method == "POST":
        if verify_totp(current_user, request.form.get("code", "")):
            current_user.totp_enabled = True
            db.session.commit()
            codes = make_recovery_codes(current_user)
            log_action("2fa_enable", entity=current_user._get_current_object(),
                       summary="Turned on two-factor authentication and "
                               "took a set of recovery codes.")
            # Shown exactly once. Rendered straight into the POST response
            # instead of the usual redirect+flash because only the hashes
            # are kept — after this page they cannot be displayed again.
            return render_template("admin/2fa_codes.html", codes=codes)
        flash("That code was not right, so two-factor authentication is "
              "still off. Codes change every 30 seconds — try the current "
              "one.", "error")

    return render_template("admin/2fa_enable.html",
                           qr=totp_qr_data_uri(current_user),
                           secret=current_user.totp_secret)


@app.route("/admin/account/2fa/disable", methods=["POST"])
@login_required
def admin_2fa_disable():
    """Switch 2FA off. Needs a current code (or a recovery code, for
    someone whose authenticator is gone) so a borrowed session can't."""
    if not current_user.totp_enabled:
        flash("Two-factor authentication is not switched on.", "error")
        return redirect(url_for("admin_account"))
    code = request.form.get("code", "")
    if verify_totp(current_user, code) or use_recovery_code(current_user, code):
        current_user.totp_enabled = False
        current_user.totp_secret = ""       # a future enrolment starts fresh
        current_user.totp_last_counter = None
        RecoveryCode.query.filter_by(user_id=current_user.id).delete()
        db.session.commit()
        log_action("2fa_disable", entity=current_user._get_current_object(),
                   summary="Turned off their own two-factor authentication.")
        flash("Two-factor authentication is now off, and your old recovery "
              "codes no longer work.", "ok")
    else:
        flash("That code was not right — two-factor authentication is "
              "still on.", "error")
    return redirect(url_for("admin_account"))


# ---------------------------------------------------------------- admin: dashboard
# The dashboard is the first page after every login, so it is counted
# rather than loaded: nothing here fetches rows to len() them, and every
# "needs attention" check is a single query, never one per record.
#
# The cards are AGGREGATE ONLY — a count or a total, never a name, an
# address or an amount tied to a person, so the page is safe to have open
# in a room. The one exception is the recent-activity list, which is the
# audit log's own summaries and is shown to super admins only. Neither is
# a view of personal data, so the page does not log_action(); the
# contributor, Gift Aid and membership pages are where that happens, and
# those do log.
PLACEHOLDER_MARK = "PLACEHOLDER"   # seeded copy EBWA still has to replace
RECENT_ACTIVITY = 6                # audit entries shown on the dashboard
STALE_PAYMENT_DAYS = 1


def _count(model, *criteria):
    """COUNT(*) for a model, with optional filters."""
    q = db.session.query(db.func.count(model.id))
    return (q.filter(*criteria) if criteria else q).scalar() or 0


def _published_split(model):
    """(total, drafts) for a model with a `published` flag, in one query."""
    total, live = db.session.query(
        db.func.count(model.id),
        db.func.coalesce(db.func.sum(
            db.case((model.published == True, 1), else_=0)), 0)  # noqa: E712
    ).one()
    return total, total - live


def _plural(n, word, plural=None):
    return "%d %s" % (n, word if n == 1 else (plural or word + "s"))


def _no_photo_count(model, owner_type):
    """Published rows with neither a lead image nor a rich-content one.

    A NOT IN against the attachment table, so it stays one query however
    much content there is — asking per row would be an N+1.
    """
    attached = (db.select(ContentImage.owner_id)
                .where(ContentImage.owner_type == owner_type))
    return _count(model,
                  model.published == True,          # noqa: E712
                  db.or_(model.image == "", model.image.is_(None)),
                  model.id.notin_(attached))


def _card(group, label, value, url, note="", drafts=0, alert=False):
    return {"group": group, "label": label, "value": value, "url": url,
            "note": note, "drafts": drafts, "alert": alert}


def dashboard_cards(flags):
    """The overview cards, grouped, honouring the feature flags.

    Every module has a card, so a new one means a card here — a module
    with no card is a module the admin forgets exists. A flagged module's
    card is built inside `if flags[...]`, so cards appear and disappear
    exactly as the nav links do.
    """
    today = date.today()
    cards = []
    content, people = "Pages and content", "People"

    total, drafts = _published_split(Event)
    cards.append(_card(
        content, "Events", total, url_for("admin_events"), drafts=drafts,
        note="%d upcoming · %d past"
             % (_count(Event, Event.event_date >= today,
                       Event.published == True),                # noqa: E712
                _count(Event, Event.event_date < today,
                       Event.published == True))))              # noqa: E712

    if flags["news"]:
        total, drafts = _published_split(NewsPost)
        cards.append(_card(content, "News & projects", total,
                           url_for("admin_news"), drafts=drafts))

    if flags["our_journey"]:
        total, drafts = _published_split(Milestone)
        funded = _count(Milestone, Milestone.funder_name != "")
        cards.append(_card(content, "Journey milestones", total,
                           url_for("admin_milestones"), drafts=drafts,
                           note=("%s with a funder" % _plural(funded, "one",
                                                              "ones"))
                                if funded else ""))

    if flags["resources"]:
        categories = db.session.query(Resource.category).distinct().count()
        cards.append(_card(content, "Community resources", _count(Resource),
                           url_for("admin_resources"),
                           note="across %s" % _plural(categories,
                                                      "category",
                                                      "categories")))

    total, drafts = _published_split(Service)
    cards.append(_card(content, "“What we do” cards", total,
                       url_for("admin_services"), drafts=drafts))

    logos = _count(Partner, Partner.logo != "",
                   Partner.display_mode.in_(("image", "both")))
    cards.append(_card(content, "Partners", _count(Partner),
                       url_for("admin_partners"),
                       note=("%s with a logo" % _plural(logos, "one", "ones"))
                            if logos else "name and text only"))

    total, drafts = _published_split(Testimonial)
    cards.append(_card(content, "Testimonials", total,
                       url_for("admin_testimonials"), drafts=drafts))

    cards.append(_card(content, "Gallery photos", _count(GalleryImage),
                       url_for("admin_gallery")))

    unread = _count(ContactMessage, ContactMessage.status == "new")
    cards.append(_card(people, "Enquiries", _count(ContactMessage),
                       url_for("admin_messages"), alert=bool(unread),
                       note=("%d unread" % unread) if unread
                            else "all read"))

    cards.append(_card(people, "Newsletter subscribers", _count(Subscriber),
                       url_for("admin_subscribers")))

    if flags["membership_form"]:
        new = _count(MembershipApplication,
                     MembershipApplication.status == "new")
        approved = _count(MembershipApplication,
                          MembershipApplication.status == "approved")
        # The one card that shouts, and only while something is actually
        # waiting on a human. Keep it to that.
        cards.append(_card(people, "Membership applications",
                           _count(MembershipApplication),
                           url_for("admin_membership"), alert=bool(new),
                           note=("%d waiting for a reply · %d approved"
                                 % (new, approved)) if new
                                else ("%d approved · nothing waiting"
                                      % approved)))

    if flags["donations"]:
        money = "Donations and collections"
        total = db.func.coalesce(
            db.func.sum(Payment.fee_pence + Payment.donation_pence), 0)
        done = Payment.query.filter(Payment.status == "complete")
        raised = done.with_entities(total).scalar()
        # "This year" is the UK calendar year to date: admin-facing
        # figures are Europe/London, storage stays naive UTC.
        year = done.filter(
            Payment.created_at >= uk_midnight_as_utc(date(today.year, 1, 1))
        ).with_entities(total).scalar()
        cards.append(_card(money, "Raised this year",
                           pounds_filter(year), url_for("admin_campaigns"),
                           note="%s since the site opened"
                                % pounds_filter(raised)))

        closed_n = _count(Campaign, Campaign.state == "closed")
        cards.append(_card(money, "Collections open",
                           _count(Campaign, Campaign.state == "open"),
                           url_for("admin_campaigns"),
                           note="%d in total%s"
                                % (_count(Campaign),
                                   ", %d completed" % closed_n
                                   if closed_n else "")))

        # The claim page's own filter, summed in SQL rather than fetching
        # the rows — order_by is dropped because this is an aggregate.
        claimable = (gift_aid_claimable_query().order_by(None)
                     .with_entities(db.func.coalesce(
                         db.func.sum(Payment.donation_pence), 0)).scalar())
        # 25p per pound, rounded half-up in integer maths — the same sum
        # the Gift Aid claim page shows, so the two never disagree.
        cards.append(_card(money, "Gift Aid to claim",
                           pounds_filter((claimable * 25 + 50) // 100),
                           url_for("admin_gift_aid"),
                           note="on %s of eligible donations"
                                % pounds_filter(claimable)))

    # Visitors gets a card like every other module — a module with no
    # card is one the admin forgets exists — and the card is all it gets
    # here. The chart lives on its own page: on the dashboard it would be
    # the biggest thing on the screen and would compete with the one red
    # card that is meant to stand out.
    today_start = utc_as_uk(datetime.utcnow()).date()
    month_start = today_start.replace(day=1)
    month_views, _month_visits = _pv_counts(month_start, today_start)
    target = stats_monthly_target()
    cards.append(_card("Website", "Page views this month",
                       "{:,}".format(month_views),
                       url_for("admin_visitors"),
                       note="target %s%s"
                            % ("{:,}".format(target),
                               " — met" if month_views >= target else "")))

    groups = []
    for c in cards:
        if not groups or groups[-1]["heading"] != c["group"]:
            groups.append({"heading": c["group"], "cards": []})
        groups[-1]["cards"].append(c)
    return groups


def dashboard_attention(flags):
    """Things worth acting on, blockers first.

    An empty list hides the panel completely — a permanent "all clear"
    box is noise the admin soon learns to skip past.
    """
    items = []

    def add(text, url=None, action="", level="todo"):
        items.append({"text": text, "url": url, "action": action,
                      "level": level})

    # Seeded placeholder copy, counted per section in one grouped query.
    # The legal pages are a launch blocker: /privacy and /terms are linked
    # from the footer of every page, and Netbus cannot write a charity's
    # privacy notice on its behalf.
    for group, n in (db.session.query(Block.group, db.func.count(Block.id))
                     .filter(Block.value.like("%" + PLACEHOLDER_MARK + "%"))
                     .group_by(Block.group).all()):
        if group == "legal":
            add("LAUNCH BLOCKER — the privacy notice and terms are still the "
                "placeholder wording (%s). EBWA must supply the real text "
                "before the site goes live." % _plural(n, "page"),
                url_for("admin_content", group=group), "Write them",
                level="blocker")
        else:
            add("The %s section still has %s of placeholder text."
                % (group, _plural(n, "block")),
                url_for("admin_content", group=group), "Edit")

    # Failed sign-ins are already in the audit log; this is what makes
    # anybody look. Below the threshold it stays quiet, because a
    # trustee mistyping a password twice is not news.
    failed = failed_logins_since()
    if failed > FAILED_LOGIN_NOTICE:
        add("%d failed sign-in attempts in the last %d hours."
            % (failed, FAILED_LOGIN_WINDOW_HOURS),
            url_for("admin_audit", action=FAILED_LOGIN_ACTION),
            "See them", level="blocker" if failed >= 25 else "todo")

    # Enquiries are the ONE check that ignores its feature flag. The flag
    # stops new messages arriving; it does not answer the ones already
    # sent, and a person waiting for a reply is not a module's content.
    n = _count(ContactMessage, ContactMessage.status == "new")
    if n:
        add("%s nobody has read yet." % _plural(n, "enquiry", "enquiries"),
            url_for("admin_messages"), "Read them")

    if flags["membership_form"]:
        n = _count(MembershipApplication,
                   MembershipApplication.status == "new")
        if n:
            add("%s waiting to be looked at."
                % _plural(n, "new membership application"),
                url_for("admin_membership"), "Review")

    n = _count(Event, Event.event_date < date.today(),
               Event.published == True)                       # noqa: E712
    if n:
        add("%s now past — still published, and showing under “Past events”."
            % _plural(n, "event"), url_for("admin_events"), "Review")

    for model, owner_type, word, url, on in (
            (Event, "event", "event", url_for("admin_events"), True),
            (NewsPost, "news_post", "news post", url_for("admin_news"),
             flags["news"]),
            (Milestone, "milestone", "milestone",
             url_for("admin_milestones"), flags["our_journey"])):
        if not on:
            continue
        n = _no_photo_count(model, owner_type)
        if n:
            add("%s published with no photo attached." % _plural(n, word),
                url, "Add one")

    if flags["donations"]:
        # Public states only: a hidden collection is not on the website,
        # so there is no page for a missing photo to look bare on.
        n = _count(Campaign, Campaign.state.in_(PUBLIC_CAMPAIGN_STATES),
                   db.or_(Campaign.image == "", Campaign.image.is_(None)))
        if n:
            add("%s with no photo — a collection page raises more with one."
                % _plural(n, "collection"), url_for("admin_campaigns"),
                "Add one")

        cutoff = datetime.utcnow() - timedelta(days=STALE_PAYMENT_DAYS)
        n = _count(Payment, Payment.status != "complete",
                   Payment.created_at < cutoff)
        if n:
            add("%s still unfinished after a day. Usually someone closed the "
                "payment page before paying — check Stripe if you were "
                "expecting the money." % _plural(n, "payment"))

    items.sort(key=lambda i: 0 if i["level"] == "blocker" else 1)
    return items


@app.route("/admin")
@login_required
def admin_dashboard():
    # Recent activity is super admins only. They can always read the log;
    # the audit_log flag decides whether EBWA's own admins get the audit
    # PAGE, and this dashboard summary is deliberately not part of that.
    recent = []
    if current_user.is_super_admin:
        recent = (AuditLog.query
                  .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                  .limit(RECENT_ACTIVITY).all())
    flags = feature_flags()
    return render_template("admin/dashboard.html",
                           groups=dashboard_cards(flags),
                           attention=dashboard_attention(flags),
                           recent=recent)


# ---------------------------------------------------------------- admin: content blocks
@app.route("/admin/content", methods=["GET", "POST"])
@login_required
def admin_content():
    group = request.args.get("group", "home")
    groups = [g[0] for g in db.session.query(Block.group).distinct().order_by(Block.group)]
    # about_layout is chosen with the layout picker, not typed into a box
    blocks = (Block.query.filter_by(group=group)
              .filter(Block.key.notin_(HIDDEN_BLOCK_KEYS))
              .order_by(Block.sort, Block.id).all())

    if request.method == "POST":
        changed = []          # block keys, never the text that was typed
        for b in blocks:
            if b.kind == "text":
                new_value = request.form.get("block_%d" % b.id, b.value)
                if new_value != b.value:
                    changed.append(b.key)
                b.value = new_value
            else:  # image
                f = request.files.get("block_%d" % b.id)
                if f and f.filename:
                    new_name = save_upload(f)
                    if new_name:
                        delete_upload(b.value)
                        b.value = new_name
                        changed.append(b.key)
        db.session.commit()
        log_action("edit", entity=("Block", None),
                   summary="Saved page content for the %s section (%s)."
                           % (group, describe_changes(changed)))
        flash("Content saved.", "ok")
        return redirect(url_for("admin_content", group=group))

    # The About tab also carries the layout picker and image manager.
    rich = group == "about" and feature_enabled("rich_layouts")
    return render_template("admin/content.html", blocks=blocks,
                           group=group, groups=groups, rich=rich,
                           owner_type="about", owner_id=0,
                           layouts=CONTENT_LAYOUT_LABELS,
                           layout=layout_for("about"),
                           images=images_for("about"),
                           video_url=video_settings_for("about")[0],
                           video_thumb=video_settings_for("about")[1],
                           video_positions=VIDEO_POSITIONS,
                           video_position=video_position_for("about"))


# ---------------------------------------------------------------- admin: content images
# Generic: any owner_type in CONTENT_OWNERS uses these four routes and the
# admin/_content_images.html partial. Only About is wired up so far.
def owner_admin_url(owner_type, owner_id):
    """Where to send the admin back to after changing an owner's images."""
    if owner_type == "about":
        return url_for("admin_content", group="about")
    endpoints = {"news_post": ("admin_news_form", "post_id"),
                 "event": ("admin_event_form", "event_id"),
                 "milestone": ("admin_milestone_form", "milestone_id")}
    if owner_type in endpoints:
        endpoint, arg = endpoints[owner_type]
        return url_for(endpoint, **{arg: owner_id})
    return url_for("admin_dashboard")


def rich_admin_context(owner_type, obj):
    """Template context for the shared image manager on an admin form.

    `rich` is False on a brand-new record: there is no owner to hang
    images off until it has been saved once, so the form says so rather
    than offering a manager that cannot work.
    """
    if obj is None or not feature_enabled("rich_layouts"):
        return {"rich": False,
                # Only nudge when the feature is on and the record is new
                "rich_hint": obj is None and feature_enabled("rich_layouts"),
                "owner_type": owner_type, "owner_id": 0,
                "layouts": CONTENT_LAYOUT_LABELS, "layout": "classic",
                "images": [], "video_url": "", "video_thumb": "",
                "video_positions": VIDEO_POSITIONS,
                "video_position": VIDEO_POSITION_DEFAULT}
    url, thumb = video_settings_for(owner_type, obj.id)
    return {"rich": True, "rich_hint": False,
            "owner_type": owner_type, "owner_id": obj.id,
            "layouts": CONTENT_LAYOUT_LABELS,
            "layout": layout_for(owner_type, obj.id),
            "images": images_for(owner_type, obj.id),
            "video_url": url, "video_thumb": thumb,
            "video_positions": VIDEO_POSITIONS,
            "video_position": video_position_for(owner_type, obj.id)}


def rich_layouts_available():
    """The flag gates the admin UI. Refused server-side too, so a stale
    tab cannot post to a feature the client has switched off."""
    if feature_enabled("rich_layouts"):
        return True
    flash("Rich page layouts are switched off for this site.", "error")
    return False


def known_owner(owner_type, owner_id):
    if owner_type not in CONTENT_OWNERS:
        return False
    if owner_type == "about":
        return owner_id == 0
    model = CONTENT_OWNERS[owner_type]
    return db.session.get(model, owner_id) is not None


@app.route("/admin/content-images/<owner_type>/<int:owner_id>/add",
           methods=["POST"])
@login_required
def admin_image_add(owner_type, owner_id):
    if not rich_layouts_available():
        return redirect(owner_admin_url(owner_type, owner_id))
    if not known_owner(owner_type, owner_id):
        abort(404)
    # Check the alt text BEFORE migrating, so a rejected upload leaves
    # everything exactly as it was.
    if not request.form.get("alt_text", "").strip():
        flash("Please describe the image in the alt text box — it is what "
              "people using a screen reader hear.", "error")
        return redirect(owner_admin_url(owner_type, owner_id))
    migrate_legacy_lead_image(owner_type, owner_id)   # keep the old one first
    try:
        sort = int(request.form.get("sort", "0"))
    except ValueError:
        sort = 0
    img, error = attach_image(owner_type, owner_id,
                              request.files.get("image"),
                              request.form.get("alt_text", ""),
                              request.form.get("caption", ""), sort)
    if error:
        flash(error, "error")
    elif img:
        log_action("create", entity=img,
                   summary="Added an image to %s." % owner_type)
        flash("Image added.", "ok")
    return redirect(owner_admin_url(owner_type, owner_id))


@app.route("/admin/content-images/<int:image_id>/save", methods=["POST"])
@login_required
def admin_image_save(image_id):
    img = db.session.get(ContentImage, image_id) or abort(404)
    if not rich_layouts_available():
        return redirect(owner_admin_url(img.owner_type, img.owner_id))
    alt_text = request.form.get("alt_text", "").strip()
    if not alt_text:
        flash("Alt text cannot be emptied — it is what screen readers "
              "announce.", "error")
        return redirect(owner_admin_url(img.owner_type, img.owner_id))
    try:
        sort = int(request.form.get("sort", "0"))
    except ValueError:
        sort = img.sort
    values = {"alt_text": alt_text[:300],
              "caption": request.form.get("caption", "").strip()[:300],
              "sort": sort}
    changed = changed_fields(img, values)
    apply_values(img, values)
    db.session.commit()
    log_action("edit", entity=img,
               summary=save_summary("image on %s" % img.owner_type,
                                    img.filename, False, changed))
    flash("Image updated.", "ok")
    return redirect(owner_admin_url(img.owner_type, img.owner_id))


@app.route("/admin/content-images/<int:image_id>/delete", methods=["POST"])
@login_required
def admin_image_delete(image_id):
    img = db.session.get(ContentImage, image_id) or abort(404)
    if not rich_layouts_available():
        return redirect(owner_admin_url(img.owner_type, img.owner_id))
    owner_type, owner_id = img.owner_type, img.owner_id
    gone, filename = ("ContentImage", img.id), img.filename
    delete_content_image(img)
    log_action("delete", entity=gone,
               summary="Removed the image %s from %s." % (filename, owner_type))
    flash("Image removed.", "ok")
    return redirect(owner_admin_url(owner_type, owner_id))


@app.route("/admin/content-images/<owner_type>/<int:owner_id>/layout",
           methods=["POST"])
@login_required
def admin_layout_save(owner_type, owner_id):
    if not rich_layouts_available():
        return redirect(owner_admin_url(owner_type, owner_id))
    if not known_owner(owner_type, owner_id):
        abort(404)
    migrate_legacy_lead_image(owner_type, owner_id)
    value = request.form.get("layout", "")
    was = layout_for(owner_type, owner_id)
    if not set_layout(owner_type, owner_id, value):
        flash("Unknown layout.", "error")
    else:
        log_action("edit", entity=("Layout", owner_id),
                   summary="Set the %s layout to %s (%s)."
                           % (owner_type, value,
                              describe_changes(["layout"] if value != was
                                               else [])))
        flash("Layout saved.", "ok")
    return redirect(owner_admin_url(owner_type, owner_id))


@app.route("/admin/<owner_type>/<int:owner_id>/video", methods=["POST"])
@login_required
def admin_video_save(owner_type, owner_id):
    """Save the video link for any rich-content owner.

    One route for News, Events, Our Journey and About, the same way the
    layout picker works — the video field lives in the shared partial,
    so a fifth content type would get it by including that partial and
    nothing else.
    """
    if not feature_enabled("video"):
        return redirect(owner_admin_url(owner_type, owner_id))
    if not known_owner(owner_type, owner_id):
        abort(404)
    changed, problem = store_video(owner_type, owner_id,
                                   request.form.get("video_url", ""))
    # The position is saved even when the link itself did not move: it is
    # the same form, and "I changed where it sits" is a change.
    moved = set_video_position(owner_type, owner_id,
                               request.form.get("video_position", ""))
    if problem:
        db.session.rollback()
        flash(problem, "error")
        return redirect(owner_admin_url(owner_type, owner_id))
    db.session.commit()
    if changed or moved:
        url, thumb = video_settings_for(owner_type, owner_id)
        where = dict((k, l) for k, l, _d in VIDEO_POSITIONS)[
            video_position_for(owner_type, owner_id)]
        log_action("edit", entity=("Video", owner_id),
                   summary="%s the video on the %s page.%s%s"
                           % ("Removed" if not url else "Set", owner_type,
                              "" if url and thumb else
                              " The poster image could not be fetched, so "
                              "the page falls back to its own photo."
                              if url else "",
                              " Position: %s." % where.lower() if url else ""))
    flash("Video saved." if (changed or moved)
          else "No change to the video.", "ok")
    return redirect(owner_admin_url(owner_type, owner_id))


# ---------------------------------------------------------------- admin: events
@app.route("/admin/events")
@login_required
def admin_events():
    rows = events_in_day_order(
        Event.query.order_by(Event.event_date.desc(), Event.id.desc()).all(),
        newest_day_first=True)
    return render_template("admin/events_list.html", rows=rows)


@app.route("/admin/events/new", methods=["GET", "POST"])
@app.route("/admin/events/<int:event_id>/edit", methods=["GET", "POST"])
@login_required
def admin_event_form(event_id=None):
    ev = db.session.get(Event, event_id) if event_id else None
    if event_id and not ev:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        date_str = request.form.get("event_date", "")
        pasted = body_embed_problem(request.form.get("description", ""),
                                    request.form.get("summary", ""))
        if not title or not date_str:
            flash("Title and date are required.", "error")
        elif pasted:
            flash(embed_paste_message(pasted), "error")
        else:
            is_new = ev is None
            if is_new:
                ev = Event()
            values = {
                "title": title,
                "event_date": datetime.strptime(date_str, "%Y-%m-%d").date(),
                "start_time": request.form.get("start_time", "").strip(),
                "venue": request.form.get("venue", "").strip(),
                "summary": request.form.get("summary", "").strip(),
                "description": request.form.get("description", "").strip(),
                "published": request.form.get("published") == "on",
            }
            changed = [] if is_new else changed_fields(ev, values)
            apply_values(ev, values)
            ev.slug = unique_slug(Event, title, ev.id)   # derived from title
            f = request.files.get("image")
            if f and f.filename:
                new_name = save_upload(f)
                if new_name:
                    delete_upload(ev.image)
                    ev.image = new_name
                    changed.append("image")
            if is_new:
                db.session.add(ev)
            db.session.commit()
            log_action("create" if is_new else "edit", entity=ev,
                       summary=save_summary("event", ev.title, is_new,
                                            changed))
            flash("Event saved.", "ok")
            return redirect(url_for("admin_events"))

    return render_template("admin/event_form.html", ev=ev,
                           **rich_admin_context("event", ev))


@app.route("/admin/events/<int:event_id>/delete", methods=["POST"])
@login_required
def admin_event_delete(event_id):
    ev = db.session.get(Event, event_id) or abort(404)
    gone, title = ("Event", ev.id), ev.title
    delete_images_for("event", ev.id)
    delete_upload(ev.image)
    db.session.delete(ev)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted event “%s”." % title)
    flash("Event deleted.", "ok")
    return redirect(url_for("admin_events"))


# ---------------------------------------------------------------- admin: news
@app.route("/admin/news")
@login_required
def admin_news():
    rows = NewsPost.query.order_by(NewsPost.published_date.desc(),
                                   NewsPost.created_at.desc(),
                                   NewsPost.id.desc()).all()
    return render_template("admin/news_list.html", rows=rows)


@app.route("/admin/news/new", methods=["GET", "POST"])
@app.route("/admin/news/<int:post_id>/edit", methods=["GET", "POST"])
@login_required
def admin_news_form(post_id=None):
    post = db.session.get(NewsPost, post_id) if post_id else None
    if post_id and not post:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        date_str = request.form.get("published_date", "")
        pasted = body_embed_problem(request.form.get("body", ""),
                                    request.form.get("summary", ""))
        if not title or not date_str:
            flash("Title and date are required.", "error")
        elif pasted:
            flash(embed_paste_message(pasted), "error")
        else:
            is_new = post is None
            if is_new:
                post = NewsPost()
            values = {
                "title": title,
                "published_date": datetime.strptime(date_str,
                                                    "%Y-%m-%d").date(),
                "summary": request.form.get("summary", "").strip(),
                "body": request.form.get("body", "").strip(),
                "published": request.form.get("published") == "on",
            }
            changed = [] if is_new else changed_fields(post, values)
            apply_values(post, values)
            post.slug = unique_slug(NewsPost, title, post.id)
            f = request.files.get("image")
            if f and f.filename:
                new_name = save_upload(f)
                if new_name:
                    delete_upload(post.image)
                    post.image = new_name
                    changed.append("image")
            if is_new:
                db.session.add(post)
            db.session.commit()
            log_action("create" if is_new else "edit", entity=post,
                       summary=save_summary("news post", post.title, is_new,
                                            changed))
            flash("News post saved.", "ok")
            return redirect(url_for("admin_news"))

    return render_template("admin/news_form.html", post=post,
                           **rich_admin_context("news_post", post))


@app.route("/admin/news/<int:post_id>/delete", methods=["POST"])
@login_required
def admin_news_delete(post_id):
    post = db.session.get(NewsPost, post_id) or abort(404)
    gone, title = ("NewsPost", post.id), post.title
    delete_images_for("news_post", post.id)
    delete_upload(post.image)
    db.session.delete(post)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted news post “%s”." % title)
    flash("News post deleted.", "ok")
    return redirect(url_for("admin_news"))


# ---------------------------------------------------------------- admin: services
@app.route("/admin/services")
@login_required
def admin_services():
    rows = Service.query.order_by(Service.sort, Service.id).all()
    return render_template("admin/services_list.html", rows=rows)


@app.route("/admin/services/new", methods=["GET", "POST"])
@app.route("/admin/services/<int:service_id>/edit", methods=["GET", "POST"])
@login_required
def admin_service_form(service_id=None):
    svc = db.session.get(Service, service_id) if service_id else None
    if service_id and not svc:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("Title is required.", "error")
        else:
            is_new = svc is None
            if is_new:
                svc = Service()
            try:
                sort = int(request.form.get("sort", "0"))
            except ValueError:
                sort = 0
            values = {
                "title": title,
                "description": request.form.get("description", "").strip(),
                "icon": request.form.get("icon", "").strip()[:16],
                "sort": sort,
                "published": request.form.get("published") == "on",
            }
            changed = [] if is_new else changed_fields(svc, values)
            apply_values(svc, values)
            if is_new:
                db.session.add(svc)
            db.session.commit()
            log_action("create" if is_new else "edit", entity=svc,
                       summary=save_summary("service card", svc.title,
                                            is_new, changed))
            flash("Service saved.", "ok")
            return redirect(url_for("admin_services"))

    return render_template("admin/service_form.html", svc=svc)


@app.route("/admin/services/<int:service_id>/toggle", methods=["POST"])
@login_required
def admin_service_toggle(service_id):
    svc = db.session.get(Service, service_id) or abort(404)
    svc.published = not svc.published
    db.session.commit()
    log_action("status_change", entity=svc,
               summary="Service card “%s” is now %s (%s)."
                       % (svc.title,
                          "published" if svc.published else "hidden",
                          describe_changes(["published"])))
    return redirect(url_for("admin_services"))


@app.route("/admin/services/<int:service_id>/delete", methods=["POST"])
@login_required
def admin_service_delete(service_id):
    svc = db.session.get(Service, service_id) or abort(404)
    gone, title = ("Service", svc.id), svc.title
    db.session.delete(svc)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted service card “%s”." % title)
    flash("Service deleted.", "ok")
    return redirect(url_for("admin_services"))


# ---------------------------------------------------------------- admin: gallery
def album_choices():
    """Every album, for the upload and move pickers.

    Alphabetical within a sort group, which is DIFFERENT from the album
    list and the public gallery — both of those end newest-first. The
    divergence is deliberate: a picker is for FINDING one album by name
    in a dropdown, where alphabetical is the only order anybody can
    predict, while a list is for PRESENTING them, where the arrangement
    an admin has chosen is the point. Keep them apart.
    """
    return GalleryAlbum.query.order_by(GalleryAlbum.sort,
                                       GalleryAlbum.title,
                                       GalleryAlbum.id).all()


def album_arg(name="album_id"):
    """A posted album id, or None for unfiled. Unknown ids become None."""
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return None
    try:
        album = db.session.get(GalleryAlbum, int(raw))
    except ValueError:
        return None
    return album.id if album else None


# ---- uploading photographs, one at a time or twelve at once
#
# TWO PATHS INTO ONE ROUTE, and the plain one is the one that must never
# break. With no JavaScript the form posts every chosen file in a single
# multipart request, exactly as it always has, and the page it lands on
# says how it went. With JavaScript the script posts the SAME form one
# file at a time so it can draw a bar, and asks for JSON back.
#
# Per-file requests are not only about the bar. MAX_CONTENT_LENGTH is 8MB
# PER REQUEST, so a dozen photographs straight off a phone can be refused
# as one batch and go through one at a time — and a file Pillow cannot
# read no longer sits in the middle of eleven good ones with nothing on
# screen to say which one it was.
GALLERY_UPLOAD_FIELDS = ("images", "image")

# The claim "one at a time" is kept by the CLIENT, which does not start a
# request until the last one has answered. This lock is the backstop
# inside a worker: it stops a second script, a hand-made parallel POST or
# a stale tab putting two Pillow encodes through the same process at
# once. It cannot see the OTHER gunicorn worker, and saying so is the
# point — nothing here should be read as a site-wide limit.
_gallery_upload_lock = threading.Lock()
GALLERY_UPLOAD_WAIT = 45          # seconds; a long photo, not for ever

# What a run of one-file-at-a-time uploads is remembered as, between the
# requests that did the work and the page that reports it.
GALLERY_UPLOAD_KEY = "gallery_upload"
GALLERY_UPLOAD_NAMES = 8          # failures NAMED in the summary


def uploaded_files():
    """Every image file on this request, whichever field carried it.

    The plain form posts `images` and may hold twelve; the progress
    script posts one, under the same name, so nothing downstream has to
    know which path it is on. `image` is accepted as well, so a
    single-file POST from anywhere else works without a second route.
    """
    files = []
    for field in GALLERY_UPLOAD_FIELDS:
        files += [f for f in request.files.getlist(field)
                  if f and f.filename]
    return files


def store_gallery_files(files, album_id, caption):
    """Save each file and record the row. Returns (added, [(name, why)])."""
    added, failed = 0, []
    for f in files:
        name, why = store_upload(f)
        if name:
            db.session.add(GalleryImage(filename=name, album_id=album_id,
                                        caption=caption))
            added += 1
        elif why:
            failed.append((f.filename, why))
    db.session.commit()
    if added:
        album = db.session.get(GalleryAlbum, album_id) if album_id else None
        log_action("create", entity=("GalleryImage", None),
                   summary="Uploaded %d gallery image(s)%s."
                           % (added, " to album “%s”" % album.title
                              if album else ""))
    return added, failed


def note_gallery_upload(added, failed):
    """Remember one request's outcome for the page that comes next.

    A run of per-file uploads has no request that knows how it ended, so
    each one adds its own line to a tally in the session and the next
    render of the gallery turns the tally into one sentence. Only a
    bounded number of failures is NAMED — a hundred rejected files must
    not try to travel back in a cookie — but every one is COUNTED, so the
    sentence can never quietly under-report.
    """
    tally = flask_session.get(GALLERY_UPLOAD_KEY) or {}
    tally["added"] = tally.get("added", 0) + added
    tally["failed"] = tally.get("failed", 0) + len(failed)
    named = tally.get("why", [])
    for name, why in failed:
        if len(named) < GALLERY_UPLOAD_NAMES:
            named.append([(name or "a file")[:60], why])
    tally["why"] = named
    flask_session[GALLERY_UPLOAD_KEY] = tally


def gallery_upload_flash():
    """Flash the summary of a per-file upload run, if one just finished.

    COMPOSED WHEN THE PAGE IS DRAWN, not in any of the POSTs that did the
    work — the same rule as `backup_started_flash()`, for a plainer
    reason here: no single one of those requests knows the totals. The
    last file cannot say "11 photos added" because it only ever saw its
    own, and a sentence written by each of them in turn would be eleven
    flashes all counting to one.
    """
    tally = flask_session.pop(GALLERY_UPLOAD_KEY, None)
    if not tally:
        return
    added, failed = tally.get("added", 0), tally.get("failed", 0)
    if not added and not failed:
        return
    parts = ["%d photo%s added" % (added, "" if added == 1 else "s")
             if added else "No photos added"]
    if failed:
        named = ["%s — %s" % (name, (why or "").rstrip("."))
                 for name, why in tally.get("why", [])]
        if failed > len(named):
            named.append("and %d more" % (failed - len(named)))
        parts.append("%d failed: %s" % (failed, "; ".join(named)))
    flash(", ".join(parts) + ".", "error" if failed else "ok")


@app.route("/admin/gallery", methods=["GET", "POST"])
@login_required
def admin_gallery():
    if request.method == "POST":
        album_id = album_arg()
        caption = request.form.get("caption", "").strip()
        files = uploaded_files()

        # The script's path: one photograph, one answer, no page drawn.
        # It says so in a form field rather than in an Accept header —
        # a field is visible in the request somebody is reading back.
        if request.form.get("ajax") == "1":
            if not files:
                return jsonify(added=0, failed=[
                    {"name": "", "error": "No file reached the server."}])
            if not _gallery_upload_lock.acquire(timeout=GALLERY_UPLOAD_WAIT):
                # Reported as this file failing, which is exactly what
                # happened, and the run carries on with the rest.
                return jsonify(added=0, failed=[
                    {"name": files[0].filename,
                     "error": "The server was still busy with another "
                              "upload."}])
            try:
                added, failed = store_gallery_files(files, album_id, caption)
            finally:
                _gallery_upload_lock.release()
            note_gallery_upload(added, failed)
            return jsonify(added=added,
                           failed=[{"name": n, "error": w} for n, w in failed])

        # The plain form, unchanged: every file in one request, every
        # refusal flashed as it happens by save_upload(), and a count on
        # the page it redirects to.
        added = 0
        for f in files:
            name = save_upload(f)
            if name:
                db.session.add(GalleryImage(
                    filename=name, album_id=album_id, caption=caption))
                added += 1
        db.session.commit()
        if added:
            album = db.session.get(GalleryAlbum, album_id) if album_id else None
            log_action("create", entity=("GalleryImage", None),
                       summary="Uploaded %d gallery image(s)%s."
                               % (added, " to album “%s”" % album.title
                                  if album else ""))
            flash("%d image(s) uploaded." % added, "ok")
        return redirect(url_for("admin_gallery",
                                album=album_id or None))

    gallery_upload_flash()
    albums = album_choices()
    # Which photos to show: one album, the unfiled ones, or everything.
    view = (request.args.get("album") or "").strip()
    if view == "unfiled":
        images = gallery_photos(unfiled_only=True)
    elif view.isdigit() and db.session.get(GalleryAlbum, int(view)):
        images = gallery_photos(album=db.session.get(GalleryAlbum, int(view)))
    else:
        view = ""
        images = GalleryImage.query.order_by(
            GalleryImage.sort, GalleryImage.created_at.desc(),
            GalleryImage.id.desc()).all()
    counts = dict(db.session.query(GalleryImage.album_id,
                                   db.func.count(GalleryImage.id))
                  .group_by(GalleryImage.album_id).all())
    return render_template("admin/gallery.html", images=images,
                           albums=albums, counts=counts, view=view,
                           unfiled=counts.get(None, 0))


@app.route("/admin/gallery/move", methods=["POST"])
@login_required
def admin_gallery_move():
    """Move the ticked photos into an album, or out of one."""
    ids = [int(i) for i in request.form.getlist("photo_ids") if i.isdigit()]
    if not ids:
        flash("Tick the photos you want to move first.", "error")
        return redirect(url_for("admin_gallery",
                                album=request.form.get("view") or None))
    album_id = album_arg("target_album")
    moved = (GalleryImage.query.filter(GalleryImage.id.in_(ids))
             .update({"album_id": album_id}, synchronize_session=False))
    db.session.commit()
    album = db.session.get(GalleryAlbum, album_id) if album_id else None
    log_action("edit", entity=("GalleryImage", None),
               summary="Moved %d gallery photo(s) %s."
                       % (moved, "into album “%s”" % album.title if album
                          else "out of their album"))
    flash("%d photo(s) moved." % moved, "ok")
    return redirect(url_for("admin_gallery",
                            album=request.form.get("view") or None))


@app.route("/admin/gallery/<int:image_id>/delete", methods=["POST"])
@login_required
def admin_gallery_delete(image_id):
    img = db.session.get(GalleryImage, image_id) or abort(404)
    gone, caption = ("GalleryImage", img.id), img.caption or img.filename
    delete_upload(img.filename)
    db.session.delete(img)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted gallery image “%s”." % caption)
    flash("Image deleted.", "ok")
    return redirect(url_for("admin_gallery",
                            album=request.form.get("view") or None))


# ---------------------------------------------------------- admin: albums
@app.route("/admin/gallery/albums")
@login_required
def admin_albums():
    rows = GalleryAlbum.query.order_by(GalleryAlbum.sort,
                                       GalleryAlbum.created_at.desc(),
                                       GalleryAlbum.id.desc()).all()
    counts = dict(db.session.query(GalleryImage.album_id,
                                   db.func.count(GalleryImage.id))
                  .group_by(GalleryImage.album_id).all())
    return render_template("admin/albums_list.html", rows=rows, counts=counts)


@app.route("/admin/gallery/albums/new", methods=["GET", "POST"])
@app.route("/admin/gallery/albums/<int:album_id>/edit",
           methods=["GET", "POST"])
@login_required
def admin_album_form(album_id=None):
    album = db.session.get(GalleryAlbum, album_id) if album_id else None
    if album_id and not album:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("Title is required.", "error")
        else:
            is_new = album is None
            if is_new:
                album = GalleryAlbum()
            try:
                sort = int(request.form.get("sort", "0"))
            except ValueError:
                sort = 0
            values = {
                "title": title,
                "description": request.form.get("description", "").strip(),
                "sort": sort,
                "published": request.form.get("published") == "on",
            }
            changed = [] if is_new else changed_fields(album, values)
            apply_values(album, values)
            album.slug = unique_slug(GalleryAlbum, title, album.id)
            if album.slug == ALL_PHOTOS_SLUG:
                # /gallery/all is the everything view; an album may not
                # take that address or it would be unreachable.
                album.slug = ALL_PHOTOS_SLUG + "-album"
            f = request.files.get("cover_image")
            if f and f.filename:
                new_name = save_upload(f)
                if new_name:
                    delete_upload(album.cover_image)
                    album.cover_image = new_name
                    changed.append("cover_image")
            if is_new:
                db.session.add(album)
            db.session.commit()
            log_action("create" if is_new else "edit", entity=album,
                       summary=save_summary("album", album.title, is_new,
                                            changed))
            flash("Album saved.", "ok")
            return redirect(url_for("admin_albums"))

    return render_template("admin/album_form.html", album=album)


@app.route("/admin/gallery/albums/<int:album_id>/delete", methods=["POST"])
@login_required
def admin_album_delete(album_id):
    album = db.session.get(GalleryAlbum, album_id) or abort(404)
    gone, title = ("GalleryAlbum", album.id), album.title
    # The photographs OUTLIVE the album: they go back to unfiled and stay
    # reachable under "All photos". Deleting an arrangement must never
    # destroy the pictures it arranged — an album can be rebuilt in a
    # minute, a photograph of somebody's grandmother cannot.
    freed = (GalleryImage.query.filter_by(album_id=album.id)
             .update({"album_id": None}, synchronize_session=False))
    delete_upload(album.cover_image)      # the cover is the album's own file
    db.session.delete(album)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted album “%s”. %d photo(s) kept, now unfiled."
                       % (title, freed))
    flash("Album deleted. Its %d photo(s) are now unfiled — find them under "
          "“Unfiled” in the gallery." % freed, "ok")
    return redirect(url_for("admin_albums"))


# ---------------------------------------------------------------- admin: testimonials
@app.route("/admin/testimonials")
@login_required
def admin_testimonials():
    rows = Testimonial.query.order_by(Testimonial.sort,
                                      Testimonial.created_at.desc(),
                                      Testimonial.id.desc()).all()
    # The homepage is the only place testimonials appear, so anything
    # past its cap is published and visible nowhere. Counted from the
    # rows already loaded rather than with a second query — this page
    # has them all in hand.
    published = sum(1 for t in rows if t.published)
    return render_template("admin/testimonials.html", rows=rows,
                           published_count=published,
                           home_cap=HOME_TESTIMONIALS,
                           motions=ROW_MOTIONS,
                           motion=row_motion("testimonials"),
                           scroller_min=ROW_SCROLLER_MIN["testimonials"],
                           step_min=PARTNER_STEP_MIN,
                           step_max=PARTNER_STEP_MAX,
                           glide_min=PARTNER_GLIDE_MIN,
                           glide_max=PARTNER_GLIDE_MAX,
                           drift_min=PARTNER_DRIFT_MIN,
                           drift_max=PARTNER_DRIFT_MAX)


@app.route("/admin/testimonials/new", methods=["GET", "POST"])
@app.route("/admin/testimonials/<int:t_id>/edit", methods=["GET", "POST"])
@login_required
def admin_testimonial_form(t_id=None):
    """Add or EDIT a testimonial.

    Editing matters more here than on most of these forms: a
    testimonial is somebody else's words, and without this the only way
    to fix a typo in a quote was to delete it and type it again from
    memory. Same list-plus-form-page shape as partners, resources and
    services.
    """
    t = db.session.get(Testimonial, t_id) if t_id else None
    if t_id and not t:
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        quote = request.form.get("quote", "").strip()
        if not (name and quote):
            flash("Name and quote are required.", "error")
        else:
            is_new = t is None
            if is_new:
                t = Testimonial()
            try:
                sort = int(request.form.get("sort", "0"))
            except ValueError:
                sort = 0
            values = {
                "name": name,
                "quote": quote,
                "role": request.form.get("role", "").strip(),
                "sort": sort,
                "published": request.form.get("published") == "on",
            }
            changed = [] if is_new else changed_fields(t, values)
            apply_values(t, values)
            if is_new:
                db.session.add(t)
            db.session.commit()
            log_action("create" if is_new else "edit", entity=t,
                       summary=save_summary("testimonial", t.name, is_new,
                                            changed))
            flash("Testimonial saved.", "ok")
            return redirect(url_for("admin_testimonials"))

    return render_template("admin/testimonial_form.html", t=t)


@app.route("/admin/testimonials/<int:t_id>/delete", methods=["POST"])
@login_required
def admin_testimonial_delete(t_id):
    t = db.session.get(Testimonial, t_id) or abort(404)
    gone, name = ("Testimonial", t.id), t.name
    db.session.delete(t)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted testimonial from %s." % name)
    flash("Testimonial deleted.", "ok")
    return redirect(url_for("admin_testimonials"))


@app.route("/admin/testimonials/<int:t_id>/toggle", methods=["POST"])
@login_required
def admin_testimonial_toggle(t_id):
    t = db.session.get(Testimonial, t_id) or abort(404)
    t.published = not t.published
    db.session.commit()
    log_action("status_change", entity=t,
               summary="Testimonial from %s is now %s (%s)."
                       % (t.name, "published" if t.published else "hidden",
                          describe_changes(["published"])))
    return redirect(url_for("admin_testimonials"))


# ---------------------------------------------------------------- admin: partners
@app.route("/admin/partners")
@login_required
def admin_partners():
    rows = (Partner.query
            .order_by(Partner.sort, Partner.name, Partner.id).all())
    return render_template("admin/partners.html", rows=rows,
                           motions=PARTNER_MOTIONS, motion=partner_motion(),
                           step_min=PARTNER_STEP_MIN,
                           step_max=PARTNER_STEP_MAX,
                           glide_min=PARTNER_GLIDE_MIN,
                           glide_max=PARTNER_GLIDE_MAX,
                           drift_min=PARTNER_DRIFT_MIN,
                           drift_max=PARTNER_DRIFT_MAX)


def save_row_motion(row):
    """Save how ONE marquee row moves. Shared by both rows.

    Everything here was written for the partner row and is now used by
    the testimonial row unchanged — the validation, the refusals, the
    audit entry and the "an empty speed box keeps what is stored" rule.
    A second copy of it would be a second place to fix the next thing
    found in it.
    """
    conf = MOTION_ROWS[row]
    back = redirect(url_for(conf["back_to"]))
    mode = (request.form.get("motion") or "").strip()
    if mode not in [m for m, _label, _help in ROW_MOTIONS]:
        flash("Unknown movement option.", "error")
        return back
    try:
        seconds = int((request.form.get("step_seconds") or "").strip())
    except ValueError:
        flash("The step interval must be a whole number of seconds.",
              "error")
        return back
    if not PARTNER_STEP_MIN <= seconds <= PARTNER_STEP_MAX:
        flash("The step interval must be between %d and %d seconds."
              % (PARTNER_STEP_MIN, PARTNER_STEP_MAX), "error")
        return back
    # The two speeds live behind a collapsed section of the same form.
    # A field that is absent or left empty KEEPS WHAT IS STORED, the way
    # an empty password box on Settings does: posting the form without
    # having opened the advanced section must not quietly reset a speed
    # somebody chose. Anything actually typed is validated.
    current = row_motion(row)
    speeds, problem = {}, None
    for field, key, now, low, high, label in (
            ("glide_ms", conf["glide_key"], current["glide_ms"],
             PARTNER_GLIDE_MIN, PARTNER_GLIDE_MAX,
             "How long one step takes"),
            ("drift_speed", conf["drift_key"], current["drift_speed"],
             PARTNER_DRIFT_MIN, PARTNER_DRIFT_MAX, "The drift speed")):
        raw = (request.form.get(field) or "").strip()
        if not raw:
            speeds[key] = now
            continue
        try:
            value = int(raw)
        except ValueError:
            problem = "%s must be a whole number." % label
            break
        if not low <= value <= high:
            problem = ("%s must be between %d and %d." % (label, low, high))
            break
        speeds[key] = value
    if problem:
        flash(problem, "error")
        return back
    glide, drift = speeds[conf["glide_key"]], speeds[conf["drift_key"]]
    # A step that takes longer than the wait between steps would start
    # its next move before finishing the last. Refused here rather than
    # papered over in the script, so the admin is told which two numbers
    # disagree instead of watching the row misbehave.
    if glide > seconds * 1000:
        flash("A step cannot take longer than the wait between steps: "
              "%dms of movement every %d second%s. Either slow the "
              "interval down or shorten the step."
              % (glide, seconds, "" if seconds == 1 else "s"), "error")
        return back

    changed = []
    for key, value in ((conf["mode_key"], mode),
                       (conf["step_key"], str(seconds)),
                       (conf["glide_key"], str(glide)),
                       (conf["drift_key"], str(drift))):
        block = Block.query.filter_by(key=key).first()
        if block is None:      # a database predating these settings
            block = Block(group=row, key=key, label=key, kind="text")
            db.session.add(block)
        if (block.value or "") != value:
            changed.append(key)
        block.value = value
    db.session.commit()
    if changed:
        log_action("edit", entity=("Block", None),
                   summary="Changed how the %s row moves: %s, a step "
                           "every %d second%s taking %dms, drifting at "
                           "%d pixels a second."
                           % (conf["label"],
                              dict((m, label) for m, label, _h
                                   in ROW_MOTIONS)[mode].lower(),
                              seconds, "" if seconds == 1 else "s",
                              glide, drift))
    flash("%s row movement saved." % conf["label"].capitalize(), "ok")
    return back


def reset_row_speeds(row):
    """Put one row's two speeds back to the values the app ships with.

    To the CONSTANTS, never to whatever was saved last: the point of the
    button is that it returns the row to a known-good state however far
    somebody has wandered, and a "restore what was there before" button
    would only take them back to their previous experiment. It leaves
    the mode and the interval alone — those are what the row DOES, and
    somebody resetting a speed has not asked to stop the row moving.
    """
    conf = MOTION_ROWS[row]
    changed = []
    for key, default in ((conf["glide_key"], PARTNER_GLIDE_DEFAULT),
                         (conf["drift_key"], PARTNER_DRIFT_DEFAULT)):
        block = Block.query.filter_by(key=key).first()
        if block is None:          # a database predating these settings
            block = Block(group=row, key=key, label=key, kind="text")
            db.session.add(block)
        if (block.value or "") != str(default):
            changed.append(key)
        block.value = str(default)
    db.session.commit()
    # Logged either way. "Somebody pressed reset and nothing moved" is
    # still somebody pressing reset, and the audit log is where you look
    # to find out why the row changed at four o'clock.
    log_action("edit", entity=("Block", None),
               summary="Reset the %s row speeds to the defaults: a "
                       "step takes %dms, the drift runs at %d pixels a "
                       "second.%s"
                       % (conf["label"], PARTNER_GLIDE_DEFAULT,
                          PARTNER_DRIFT_DEFAULT,
                          "" if changed else " They were already both set "
                          "to those values."))
    flash("%s row speeds put back to the defaults."
          % conf["label"].capitalize(), "ok")
    return redirect(url_for(conf["back_to"]))


@app.route("/admin/partners/motion", methods=["POST"])
@login_required
def admin_partner_motion():
    """Save how the partner row moves. One setting for the row, not a
    field on every partner — see ROW_MOTIONS."""
    return save_row_motion("partners")


@app.route("/admin/partners/motion/reset", methods=["POST"])
@login_required
def admin_partner_motion_reset():
    return reset_row_speeds("partners")


@app.route("/admin/testimonials/motion", methods=["POST"])
@login_required
def admin_testimonial_motion():
    """The same, for the row of quotes."""
    return save_row_motion("testimonials")


@app.route("/admin/testimonials/motion/reset", methods=["POST"])
@login_required
def admin_testimonial_motion_reset():
    return reset_row_speeds("testimonials")


@app.route("/admin/partners/new", methods=["GET", "POST"])
@app.route("/admin/partners/<int:p_id>/edit", methods=["GET", "POST"])
@login_required
def admin_partner_form(p_id=None):
    pt = db.session.get(Partner, p_id) if p_id else None
    if p_id and not pt:
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        mode = request.form.get("display_mode", "text")
        if not name:
            flash("Partner name is required.", "error")
        elif mode not in PARTNER_MODES:
            flash("Unknown display option.", "error")
        else:
            is_new = pt is None
            if is_new:
                pt = Partner()
            try:
                sort = int(request.form.get("sort", "0"))
            except ValueError:
                sort = 0
            values = {
                "name": name,
                "url": request.form.get("url", "").strip(),
                "blurb": request.form.get("blurb", "").strip(),
                "display_mode": mode,
                "sort": sort,
            }
            changed = [] if is_new else changed_fields(pt, values)
            apply_values(pt, values)
            f = request.files.get("logo")
            if f and f.filename:
                new_name = save_upload(f)
                if new_name:
                    delete_upload(pt.logo)
                    pt.logo = new_name
                    changed.append("logo")
            if is_new:
                db.session.add(pt)
            db.session.commit()
            log_action("create" if is_new else "edit", entity=pt,
                       summary=save_summary("partner", pt.name, is_new,
                                            changed))
            flash("Partner saved.", "ok")
            return redirect(url_for("admin_partners"))

    return render_template("admin/partner_form.html", pt=pt,
                           modes=PARTNER_MODES)


@app.route("/admin/partners/<int:p_id>/delete", methods=["POST"])
@login_required
def admin_partner_delete(p_id):
    pt = db.session.get(Partner, p_id) or abort(404)
    gone, name = ("Partner", pt.id), pt.name
    delete_upload(pt.logo)
    db.session.delete(pt)
    db.session.commit()
    log_action("delete", entity=gone, summary="Deleted partner %s." % name)
    flash("Partner deleted.", "ok")
    return redirect(url_for("admin_partners"))


# ---------------------------------------------------------------- admin: resources
@app.route("/admin/resources")
@login_required
def admin_resources():
    rows = Resource.query.order_by(Resource.category, Resource.sort,
                                   Resource.name, Resource.id).all()
    return render_template("admin/resources_list.html", rows=rows)


@app.route("/admin/resources/new", methods=["GET", "POST"])
@app.route("/admin/resources/<int:resource_id>/edit", methods=["GET", "POST"])
@login_required
def admin_resource_form(resource_id=None):
    res = db.session.get(Resource, resource_id) if resource_id else None
    if resource_id and not res:
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        if not name or not category:
            flash("Name and category are required.", "error")
        else:
            is_new = res is None
            if is_new:
                res = Resource()
            try:
                sort = int(request.form.get("sort", "0"))
            except ValueError:
                sort = 0
            values = {
                "name": name,
                "category": category,
                "description": request.form.get("description", "").strip(),
                "phone": request.form.get("phone", "").strip(),
                "url": request.form.get("url", "").strip(),
                "sort": sort,
            }
            changed = [] if is_new else changed_fields(res, values)
            apply_values(res, values)
            if is_new:
                db.session.add(res)
            db.session.commit()
            log_action("create" if is_new else "edit", entity=res,
                       summary=save_summary("resource", res.name, is_new,
                                            changed))
            flash("Resource saved.", "ok")
            return redirect(url_for("admin_resources"))

    categories = [c[0] for c in db.session.query(Resource.category)
                  .distinct().order_by(Resource.category)]
    return render_template("admin/resource_form.html", res=res,
                           categories=categories)


@app.route("/admin/resources/<int:resource_id>/delete", methods=["POST"])
@login_required
def admin_resource_delete(resource_id):
    res = db.session.get(Resource, resource_id) or abort(404)
    gone, name = ("Resource", res.id), res.name
    db.session.delete(res)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted resource “%s”." % name)
    flash("Resource deleted.", "ok")
    return redirect(url_for("admin_resources"))


# ---------------------------------------------------------------- admin: faq
@app.route("/admin/faq")
@login_required
def admin_faqs():
    rows = Faq.query.order_by(Faq.category, Faq.sort, Faq.id).all()
    return render_template("admin/faqs_list.html", rows=rows)


@app.route("/admin/faq/new", methods=["GET", "POST"])
@app.route("/admin/faq/<int:faq_id>/edit", methods=["GET", "POST"])
@login_required
def admin_faq_form(faq_id=None):
    faq_row = db.session.get(Faq, faq_id) if faq_id else None
    if faq_id and not faq_row:
        abort(404)

    if request.method == "POST":
        question = request.form.get("question", "").strip()
        answer = request.form.get("answer", "").strip()
        if not question or not answer:
            flash("Question and answer are both required.", "error")
        else:
            is_new = faq_row is None
            if is_new:
                faq_row = Faq()
            try:
                sort = int(request.form.get("sort", "0"))
            except ValueError:
                sort = 0
            values = {
                "question": question,
                "answer": answer,
                "category": request.form.get("category", "").strip(),
                "sort": sort,
                "published": request.form.get("published") == "on",
            }
            changed = [] if is_new else changed_fields(faq_row, values)
            apply_values(faq_row, values)
            if is_new:
                db.session.add(faq_row)
            db.session.commit()
            log_action("create" if is_new else "edit", entity=faq_row,
                       summary=save_summary("question", faq_row.question,
                                            is_new, changed))
            flash("Question saved.", "ok")
            return redirect(url_for("admin_faqs"))

    categories = [c[0] for c in db.session.query(Faq.category).distinct()
                  .order_by(Faq.category) if c[0]]
    return render_template("admin/faq_form.html", faq=faq_row,
                           categories=categories)


@app.route("/admin/faq/<int:faq_id>/delete", methods=["POST"])
@login_required
def admin_faq_delete(faq_id):
    faq_row = db.session.get(Faq, faq_id) or abort(404)
    gone, question = ("Faq", faq_row.id), faq_row.question
    db.session.delete(faq_row)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted question “%s”." % question)
    flash("Question deleted.", "ok")
    return redirect(url_for("admin_faqs"))


# ---------------------------------------------------------------- admin: milestones
@app.route("/admin/journey")
@login_required
def admin_milestones():
    rows = Milestone.query.order_by(Milestone.year.desc(), Milestone.sort,
                                    Milestone.title, Milestone.id).all()
    return render_template("admin/milestones_list.html", rows=rows)


@app.route("/admin/journey/new", methods=["GET", "POST"])
@app.route("/admin/journey/<int:milestone_id>/edit", methods=["GET", "POST"])
@login_required
def admin_milestone_form(milestone_id=None):
    m = db.session.get(Milestone, milestone_id) if milestone_id else None
    if milestone_id and not m:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        try:
            year = int(request.form.get("year", ""))
        except ValueError:
            year = None
        amount_raw = request.form.get("amount", "").strip()
        amount_pence = parse_pounds(amount_raw) if amount_raw else None
        pasted = body_embed_problem(request.form.get("summary", ""),
                                    request.form.get("outcome", ""))
        if not title:
            flash("Title is required.", "error")
        elif pasted:
            flash(embed_paste_message(pasted), "error")
        elif year is None or year < 1900 or year > 2100:
            flash("Please enter a valid four-digit year.", "error")
        elif amount_raw and (amount_pence is None or amount_pence <= 0):
            flash("Amount must be a valid amount in pounds, or left blank.",
                  "error")
        else:
            is_new = m is None
            if is_new:
                m = Milestone()
            try:
                sort = int(request.form.get("sort", "0"))
            except ValueError:
                sort = 0
            values = {
                "title": title,
                "year": year,
                "summary": request.form.get("summary", "").strip(),
                "outcome": request.form.get("outcome", "").strip(),
                "funder_name": request.form.get("funder_name", "").strip(),
                "amount_pence": amount_pence,
                "funder_url": request.form.get("funder_url", "").strip(),
                "sort": sort,
                "published": request.form.get("published") == "on",
            }
            changed = [] if is_new else changed_fields(m, values)
            apply_values(m, values)
            f = request.files.get("image")
            if f and f.filename:
                new_name = save_upload(f)
                if new_name:
                    delete_upload(m.image)
                    m.image = new_name
                    changed.append("image")
            if is_new:
                db.session.add(m)
            db.session.commit()
            log_action("create" if is_new else "edit", entity=m,
                       summary=save_summary("milestone", m.title, is_new,
                                            changed))
            flash("Milestone saved.", "ok")
            return redirect(url_for("admin_milestones"))

    return render_template("admin/milestone_form.html", m=m,
                           **rich_admin_context("milestone", m))


@app.route("/admin/journey/<int:milestone_id>/delete", methods=["POST"])
@login_required
def admin_milestone_delete(milestone_id):
    m = db.session.get(Milestone, milestone_id) or abort(404)
    gone, title = ("Milestone", m.id), m.title
    delete_images_for("milestone", m.id)
    delete_upload(m.image)
    db.session.delete(m)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted milestone “%s”." % title)
    flash("Milestone deleted.", "ok")
    return redirect(url_for("admin_milestones"))


# ---------------------------------------------------------------- admin: messages
# Enquiries are personal data: admin-only, never public, and deliberately
# with NO CSV export — there is no reason to bulk-download somebody's
# question about a lunch club. Opening the list, changing a status and
# deleting one are all recorded in the audit log.
def unread_messages():
    return db.session.query(db.func.count(ContactMessage.id)).filter(
        ContactMessage.status == "new").scalar() or 0


@app.route("/admin/messages")
@login_required
def admin_messages():
    status = request.args.get("status", "").strip()
    q = ContactMessage.query
    if status in MESSAGE_STATUSES:
        q = q.filter(ContactMessage.status == status)
    else:
        status = ""
    rows = q.order_by(ContactMessage.created_at.desc(),
                      ContactMessage.id.desc()).all()
    counts = dict(db.session.query(ContactMessage.status,
                                   db.func.count(ContactMessage.id))
                  .group_by(ContactMessage.status).all())
    # Reading enquiries IS a view of personal data, so it is logged, the
    # same as an export. The count keeps the entry useful without copying
    # anybody's name into a second table.
    log_action("view", entity=("ContactMessage", None),
               summary="Viewed the enquiry list (%d message(s)%s)."
                       % (len(rows),
                          ", filtered to %s" % status if status else ""))
    return render_template("admin/messages.html", rows=rows, status=status,
                           counts=counts, statuses=MESSAGE_STATUSES,
                           total=sum(counts.values()))


@app.route("/admin/messages/<int:message_id>/status", methods=["POST"])
@login_required
def admin_message_status(message_id):
    msg = db.session.get(ContactMessage, message_id) or abort(404)
    new_status = request.form.get("status", "").strip()
    if new_status not in MESSAGE_STATUSES:
        flash("Unknown status.", "error")
        return redirect(url_for("admin_messages",
                                status=request.form.get("view") or None))
    was, msg.status = msg.status, new_status
    db.session.commit()
    log_action("status", entity=msg,
               summary="Marked the enquiry from %s as %s (was %s)."
                       % (msg.name, new_status, was))
    flash("Marked as %s." % new_status, "ok")
    return redirect(url_for("admin_messages",
                            status=request.form.get("view") or None))


@app.route("/admin/messages/<int:message_id>/delete", methods=["POST"])
@login_required
def admin_message_delete(message_id):
    msg = db.session.get(ContactMessage, message_id) or abort(404)
    gone, name = ("ContactMessage", msg.id), msg.name
    db.session.delete(msg)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted the enquiry from %s." % name)
    flash("Message deleted.", "ok")
    return redirect(url_for("admin_messages",
                            status=request.form.get("view") or None))


# ---------------------------------------------------------------- admin: subscribers
@app.route("/admin/subscribers")
@login_required
def admin_subscribers():
    rows = Subscriber.query.order_by(Subscriber.created_at.desc(),
                                     Subscriber.id.desc()).all()
    return render_template("admin/subscribers.html", rows=rows)


@app.route("/admin/subscribers.csv")
@login_required
def admin_subscribers_csv():
    lines = ["email,subscribed_on"]
    for s in Subscriber.query.order_by(Subscriber.created_at,
                                       Subscriber.id).all():
        lines.append("%s,%s" % (s.email,
                                utc_as_uk(s.created_at).strftime("%Y-%m-%d")))
    log_action("export", entity=("Subscriber", None),
               summary="Exported the subscriber list as CSV (%d addresses)."
                       % (len(lines) - 1))
    resp = app.response_class("\n".join(lines), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=ebwa-subscribers.csv"
    return resp


@app.route("/admin/subscribers/<int:s_id>/delete", methods=["POST"])
@login_required
def admin_subscriber_delete(s_id):
    s = db.session.get(Subscriber, s_id) or abort(404)
    gone, email = ("Subscriber", s.id), s.email
    db.session.delete(s)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Removed newsletter subscriber %s." % email)
    flash("Subscriber removed.", "ok")
    return redirect(url_for("admin_subscribers"))


# ---------------------------------------------------------------- admin: collections
@app.route("/admin/campaigns")
@login_required
def admin_campaigns():
    rows = Campaign.query.order_by(Campaign.created_at.desc(),
                                   Campaign.id.desc()).all()
    return render_template("admin/campaigns_list.html", rows=rows)


@app.route("/admin/campaigns/new", methods=["GET", "POST"])
@app.route("/admin/campaigns/<int:campaign_id>/edit", methods=["GET", "POST"])
@login_required
def admin_campaign_form(campaign_id=None):
    camp = db.session.get(Campaign, campaign_id) if campaign_id else None
    if campaign_id and not camp:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        target_raw = request.form.get("target", "").strip()
        fee_raw = request.form.get("fee", "").strip()
        target_pence = parse_pounds(target_raw) if target_raw else None
        fee_pence = parse_pounds(fee_raw) if fee_raw else None
        pasted = body_embed_problem(request.form.get("description", ""))
        video_raw = request.form.get("video_url", "").strip()
        video = parse_video_url(video_raw) if video_raw else None
        # A SELECT, not a tick-box, which is also why there is no
        # "unticked posts nothing" trap here (CLAUDE.md, the publish
        # checkbox rule): a select always posts something. An unknown
        # value still falls back to what the row already had rather than
        # to a default, so a hand-crafted POST cannot publish a hidden
        # collection or reopen a closed one.
        state = request.form.get("state", "")
        if state not in CAMPAIGN_STATE_LABELS:
            state = camp.state if camp else CAMPAIGN_STATES[0][0]
        if not title:
            flash("Title is required.", "error")
        elif pasted:
            flash(embed_paste_message(pasted), "error")
        elif video_raw and not video:
            flash("That does not look like a YouTube or Vimeo link. Paste "
                  "the address from the browser's address bar, or leave "
                  "the box empty for no video.", "error")
        elif target_raw and (target_pence is None or target_pence <= 0):
            flash("Target must be a valid amount in pounds.", "error")
        elif fee_raw and (fee_pence is None or fee_pence <= 0):
            flash("Place fee must be a valid amount in pounds.", "error")
        else:
            is_new = camp is None
            if is_new:
                camp = Campaign()
            values = {
                "title": title,
                "description": request.form.get("description", "").strip(),
                "target_pence": target_pence,
                "fee_pence": fee_pence,
                "state": state,
                # An unticked checkbox posts nothing, which is what
                # "do not show it on the page" means here. The COVER is
                # not affected either way — that is the point of it.
                "show_image_on_page":
                    request.form.get("show_image_on_page") == "on",
                # Saved whether or not the box above is ticked: unticking
                # it must not silently forget where the picture goes.
                "image_position": clean_media_position(
                    request.form.get("image_position", "")),
            }
            changed = [] if is_new else changed_fields(camp, values)
            apply_values(camp, values)
            # The legacy column, kept in step here and nowhere else, so
            # that somebody reading the table with sqlite3 is not told
            # the opposite of what the website is doing. Nothing reads it.
            camp.active = (camp.state == "open")
            camp.slug = unique_slug(Campaign, title, camp.id)
            f = request.files.get("image")
            if f and f.filename:
                new_name = save_upload(f)
                if new_name:
                    delete_upload(camp.image)
                    camp.image = new_name
                    changed.append("image")
            if feature_enabled("video"):
                position = clean_video_position(
                    request.form.get("video_position", ""))
                if position != clean_video_position(camp.video_position):
                    camp.video_position = position
                    changed.append("video_position")
                canonical = (video_watch_url(video["provider"], video["id"])
                             if video else "")
                if canonical != (camp.video_url or ""):
                    old_thumb = camp.video_thumb or ""
                    camp.video_url = canonical
                    camp.video_thumb = (
                        fetch_video_poster(video["provider"], video["id"])
                        if video else "")
                    if old_thumb and old_thumb != camp.video_thumb:
                        delete_upload(old_thumb)
                    changed.append("video_url")
            if is_new:
                db.session.add(camp)
            db.session.commit()
            log_action("create" if is_new else "edit", entity=camp,
                       summary=save_summary("collection", camp.title, is_new,
                                            changed))
            flash("Collection saved.", "ok")
            return redirect(url_for("admin_campaigns"))

    return render_template("admin/campaign_form.html", camp=camp,
                           campaign_states=CAMPAIGN_STATES,
                           video_positions=VIDEO_POSITIONS,
                           video_position=clean_media_position(
                               camp.video_position if camp else ""),
                           image_positions=IMAGE_POSITIONS,
                           image_position=clean_media_position(
                               camp.image_position if camp else ""))


@app.route("/admin/campaigns/<int:campaign_id>/delete", methods=["POST"])
@login_required
def admin_campaign_delete(campaign_id):
    camp = db.session.get(Campaign, campaign_id) or abort(404)
    # Payments are financial records — never orphan or delete them.
    if Payment.query.filter_by(campaign_id=camp.id).count():
        flash("This collection has payments recorded against it, so it "
              "can't be deleted. Set it to “Closed” to keep it on the "
              "site as a record, or “Hidden” to take it off the website "
              "— either way the contributor list and Gift Aid records "
              "stay here.", "error")
        return redirect(url_for("admin_campaigns"))
    gone, title = ("Campaign", camp.id), camp.title
    delete_upload(camp.image)
    db.session.delete(camp)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted collection “%s”." % title)
    flash("Collection deleted.", "ok")
    return redirect(url_for("admin_campaigns"))


@app.route("/admin/campaigns/<int:campaign_id>/contributors")
@login_required
def admin_campaign_contributors(campaign_id):
    camp = db.session.get(Campaign, campaign_id) or abort(404)
    rows = (Payment.query.filter_by(campaign_id=camp.id)
            .order_by(Payment.created_at.desc(),
                      Payment.id.desc()).all())
    log_action("export", entity=camp,
               summary="Viewed the printable contributor list for “%s” "
                       "(%d payments)." % (camp.title, len(rows)))
    return render_template("admin/contributors.html", camp=camp, rows=rows)


@app.route("/admin/campaigns/<int:campaign_id>/contributors.csv")
@login_required
def admin_campaign_contributors_csv(campaign_id):
    import csv
    import io
    camp = db.session.get(Campaign, campaign_id) or abort(404)
    rows = (Payment.query.filter_by(campaign_id=camp.id)
            .order_by(Payment.created_at, Payment.id).all())
    log_action("export", entity=camp,
               summary="Exported the contributor list for “%s” as CSV "
                       "(%d payments)." % (camp.title, len(rows)))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "name", "email", "fee_gbp", "donation_gbp",
                "total_gbp", "gift_aid", "status"])
    for p in rows:
        w.writerow([utc_as_uk(p.created_at).strftime("%Y-%m-%d"),
                    p.name, p.email,
                    "%.2f" % (p.fee_pence / 100.0),
                    "%.2f" % (p.donation_pence / 100.0),
                    "%.2f" % (p.total_pence / 100.0),
                    "yes" if p.gift_aid else "no", p.status])
    resp = app.response_class(out.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = \
        "attachment; filename=ebwa-%s-contributors.csv" % camp.slug
    return resp


# ---------------------------------------------------------------- admin: gift aid
UK_TZ = ZoneInfo("Europe/London")


def uk_midnight_as_utc(d):
    """Start of the given UK calendar date, as a naive-UTC datetime."""
    return (datetime(d.year, d.month, d.day, tzinfo=UK_TZ)
            .astimezone(timezone.utc).replace(tzinfo=None))


def utc_as_uk(dt):
    """Naive-UTC datetime -> aware UK local datetime."""
    return dt.replace(tzinfo=timezone.utc).astimezone(UK_TZ)


def uk_clock_change(d):
    """The instant the UK clocks change on date `d`, as naive UTC, or None.

    Found by halving the day rather than by knowing when the government
    moves the clocks: the rule has changed before and zoneinfo already
    holds the answer.
    """
    lo = uk_midnight_as_utc(d)
    hi = uk_midnight_as_utc(d + timedelta(days=1))
    first = utc_as_uk(lo).utcoffset()
    if utc_as_uk(hi - timedelta(seconds=1)).utcoffset() == first:
        return None                      # an ordinary day
    for _ in range(40):
        mid = lo + (hi - lo) / 2
        if utc_as_uk(mid).utcoffset() == first:
            lo = mid
        else:
            hi = mid
    # `hi` is the first instant on the new offset; transitions land on
    # the hour, so round away the binary search's last fractions.
    return (hi + timedelta(seconds=1) - timedelta(
        microseconds=hi.microsecond)).replace(second=0, microsecond=0)


def uk_wall_as_utc(d, hour, minute):
    """A UK wall-clock time on a UK calendar date, as naive UTC.

    The same idea as `uk_midnight_as_utc`, for a time somebody typed.
    Two days a year that is not a straightforward sum, and both are
    handled here rather than at the call site:

    SPRING, the hour that does not happen. At 01:00 GMT the clocks go to
    02:00 BST, so nothing between 01:00 and 01:59 exists that day. A
    schedule set inside it is due AT THE MOMENT THE CLOCKS CHANGE — the
    first instant that day at or after the time that was asked for. It
    runs once, as early as it could have, rather than being skipped for
    the year or dragged to the following midnight.

    AUTUMN, the hour that happens twice. At 02:00 BST the clocks go back
    to 01:00 GMT, so 01:00-01:59 comes round again an hour later. The
    FIRST of the two is used (`fold=0`), so the backup happens at the
    first opportunity; the caller's "has today's run already happened?"
    check then stops the second one, which is why this returns one
    instant and not two.
    """
    naive = datetime(d.year, d.month, d.day, hour, minute)
    # fold=0 is the first of a repeated pair, which is what autumn wants.
    utc = (naive.replace(tzinfo=UK_TZ).astimezone(timezone.utc)
           .replace(tzinfo=None))
    if utc_as_uk(utc).replace(tzinfo=None) == naive:
        return utc                       # the wall time exists; done
    # It does not exist: this is the spring gap. Use the change itself.
    change = uk_clock_change(d)
    return change if change is not None else utc


def schedule_parts(text):
    """"HH:MM" -> (hour, minute), falling back to the default.

    A hand-edited or empty Block must not stop the nightly backup, so
    anything unreadable becomes the default time rather than an error.
    """
    try:
        hour, minute = [int(part) for part in str(text).split(":")]
    except (ValueError, AttributeError, TypeError):
        hour, minute = [int(part) for part in SFTP_DEFAULT_SCHEDULE.split(":")]
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        hour, minute = [int(part) for part in SFTP_DEFAULT_SCHEDULE.split(":")]
    return hour, minute


@app.template_filter("uk_date")
def uk_date_filter(dt):
    """Display a naive-UTC timestamp as its UK local calendar date."""
    return utc_as_uk(dt).strftime("%d %b %Y") if dt else ""


@app.template_filter("uk_datetime")
def uk_datetime_filter(dt):
    """Display a naive-UTC timestamp as its UK local date and time."""
    return utc_as_uk(dt).strftime("%d %b %Y, %H:%M") if dt else ""


def gift_aid_claimable_query(date_from=None, date_to=None):
    """Completed payments whose donation portion carries a valid declaration.

    Only donation_pence is ever claimable — fee_pence never appears here
    (CLAUDE.md HMRC rule; the Payment CHECK constraints back this up).
    date_from/date_to are UK calendar dates (convention: admin-facing
    filters work in Europe/London; storage stays naive UTC).
    """
    q = Payment.query.filter(
        Payment.status == "complete",
        Payment.gift_aid == True,          # noqa: E712
        Payment.donation_pence > 0,
        Payment.gift_aid_name != "",
        Payment.gift_aid_address != "",
        Payment.gift_aid_postcode != "")
    if date_from:
        q = q.filter(Payment.created_at >= uk_midnight_as_utc(date_from))
    if date_to:
        q = q.filter(Payment.created_at
                     < uk_midnight_as_utc(date_to + timedelta(days=1)))
    return q.order_by(Payment.created_at, Payment.id)


def describe_range(date_from, date_to):
    """Human wording for a UK-local date filter, for audit summaries."""
    if date_from and date_to:
        return "%s to %s" % (date_from.strftime("%d %b %Y"),
                             date_to.strftime("%d %b %Y"))
    if date_from:
        return "%s onwards" % date_from.strftime("%d %b %Y")
    if date_to:
        return "up to %s" % date_to.strftime("%d %b %Y")
    return "all dates"


def _parse_date_arg(name):
    try:
        return datetime.strptime(request.args.get(name, ""),
                                 "%Y-%m-%d").date()
    except ValueError:
        return None


@app.route("/admin/gift-aid")
@login_required
def admin_gift_aid():
    date_from, date_to = _parse_date_arg("from"), _parse_date_arg("to")
    rows = gift_aid_claimable_query(date_from, date_to).all()
    claimable_pence = sum(p.gift_aid_pence for p in rows)
    # 25p per £1, rounded half-up to the penny (integer maths, no floats)
    reclaim_pence = (claimable_pence * 25 + 50) // 100
    log_action("export", entity=("Payment", None),
               summary="Viewed the printable Gift Aid claim for %s — "
                       "%d donations, %s claimable, %s reclaim."
                       % (describe_range(date_from, date_to), len(rows),
                          pounds_filter(claimable_pence),
                          pounds_filter(reclaim_pence)))
    return render_template("admin/gift_aid.html", rows=rows,
                           claimable_pence=claimable_pence,
                           reclaim_pence=reclaim_pence,
                           date_from=date_from, date_to=date_to)


@app.route("/admin/gift-aid.csv")
@login_required
def admin_gift_aid_csv():
    """CSV in the HMRC Charities Online Gift Aid schedule column layout."""
    import csv
    import io
    date_from, date_to = _parse_date_arg("from"), _parse_date_arg("to")
    claim_rows = gift_aid_claimable_query(date_from, date_to).all()
    log_action("export", entity=("Payment", None),
               summary="Exported the HMRC Gift Aid claim as CSV for %s — "
                       "%d donations, %s claimable."
                       % (describe_range(date_from, date_to), len(claim_rows),
                          pounds_filter(sum(p.gift_aid_pence
                                            for p in claim_rows))))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Title", "First name", "Last name",
                "House name or number", "Postcode",
                "Aggregated donations", "Donation date", "Amount"])
    for p in claim_rows:
        parts = p.gift_aid_name.split()
        first = parts[0] if len(parts) > 1 else ""
        last = " ".join(parts[1:]) if len(parts) > 1 else p.gift_aid_name
        w.writerow(["", first, last, p.gift_aid_address,
                    p.gift_aid_postcode, "",
                    utc_as_uk(p.created_at).strftime("%d/%m/%y"),
                    "%.2f" % (p.gift_aid_pence / 100.0)])
    resp = app.response_class(out.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = \
        "attachment; filename=ebwa-gift-aid-claim.csv"
    return resp


@app.route("/admin/gift-aid/declarations")
@login_required
def admin_gift_aid_declarations():
    rows = (Payment.query.filter(Payment.gift_aid == True)  # noqa: E712
            .order_by(Payment.created_at.desc(),
                      Payment.id.desc()).all())
    log_action("export", entity=("Payment", None),
               summary="Viewed the Gift Aid declaration records "
                       "(%d declarations)." % len(rows))
    return render_template("admin/gift_aid_declarations.html", rows=rows)


# ---------------------------------------------------------------- admin: membership
@app.route("/admin/membership")
@login_required
def admin_membership():
    rows = (MembershipApplication.query
            .order_by(MembershipApplication.created_at.desc(),
                      MembershipApplication.id.desc()).all())
    return render_template("admin/membership.html", rows=rows,
                           statuses=MEMBERSHIP_STATUSES)


@app.route("/admin/membership.csv")
@login_required
def admin_membership_csv():
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "email", "phone", "address", "reason", "status",
                "applied_on"])
    count = 0
    for m in (MembershipApplication.query
              .order_by(MembershipApplication.created_at,
                        MembershipApplication.id).all()):
        count += 1
        w.writerow([m.name, m.email, m.phone, m.address, m.reason,
                    m.status,
                    utc_as_uk(m.created_at).strftime("%Y-%m-%d")])
    log_action("export", entity=("MembershipApplication", None),
               summary="Exported membership applications as CSV "
                       "(%d applications)." % count)
    resp = app.response_class(out.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = \
        "attachment; filename=ebwa-membership-applications.csv"
    return resp


@app.route("/admin/membership/<int:m_id>/status", methods=["POST"])
@login_required
def admin_membership_status(m_id):
    m = db.session.get(MembershipApplication, m_id) or abort(404)
    status = request.form.get("status", "")
    if status in MEMBERSHIP_STATUSES:
        changed = changed_fields(m, {"status": status})
        m.status = status
        db.session.commit()
        log_action("status_change", entity=m,
                   summary="Membership application from %s marked %s (%s)."
                           % (m.name, status, describe_changes(changed)))
        flash("Status updated.", "ok")
    else:
        flash("Unknown status.", "error")
    return redirect(url_for("admin_membership"))


@app.route("/admin/membership/<int:m_id>/delete", methods=["POST"])
@login_required
def admin_membership_delete(m_id):
    m = db.session.get(MembershipApplication, m_id) or abort(404)
    gone, name = ("MembershipApplication", m.id), m.name
    db.session.delete(m)
    db.session.commit()
    log_action("delete", entity=gone,
               summary="Deleted membership application from %s." % name)
    flash("Application removed.", "ok")
    return redirect(url_for("admin_membership"))


# ---------------------------------------------------------------- admin: audit
# Read-only. There is no route here that writes to an existing entry or
# removes one, and there must never be — see the AuditLog docstring.
AUDIT_PER_PAGE = 50


@app.route("/admin/audit")
@login_required
def admin_audit():
    """The audit log, newest first.

    Super admins can always read it. The audit_log feature flag only
    decides whether EBWA's own admins see the page — recording never
    stops either way, so the log can't be quietly switched off.
    """
    if not can_read_audit():
        abort(403)

    who = request.args.get("user", "").strip()
    what = request.args.get("action", "").strip()
    date_from, date_to = _parse_date_arg("from"), _parse_date_arg("to")

    q = AuditLog.query
    if who:
        q = q.filter(AuditLog.user_email == who)
    if what:
        q = q.filter(AuditLog.action == what)
    if date_from:      # UK local dates in, naive UTC on the column
        q = q.filter(AuditLog.created_at >= uk_midnight_as_utc(date_from))
    if date_to:
        q = q.filter(AuditLog.created_at
                     < uk_midnight_as_utc(date_to + timedelta(days=1)))

    total = q.count()
    pages = max(1, (total + AUDIT_PER_PAGE - 1) // AUDIT_PER_PAGE)
    try:
        page = min(max(1, int(request.args.get("page", "1"))), pages)
    except ValueError:
        page = 1
    rows = (q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset((page - 1) * AUDIT_PER_PAGE).limit(AUDIT_PER_PAGE).all())

    users = [r[0] for r in db.session.query(AuditLog.user_email)
             .distinct().order_by(AuditLog.user_email)]
    actions = [r[0] for r in db.session.query(AuditLog.action)
               .distinct().order_by(AuditLog.action)]
    return render_template("admin/audit.html", rows=rows, users=users,
                           actions=actions, page=page, pages=pages,
                           total=total, who=who, what=what,
                           date_from=date_from, date_to=date_to)


@app.route("/admin/visitors")
@login_required
def admin_visitors():
    """EBWA's own figures, for EVERY admin.

    The Settings panel this grew out of is super-admin only because of
    what sits BESIDE it — the mail server, the NAS password, the state
    of the machine — not because visitor numbers are Netbus's business.
    They are EBWA's, and a charity that cannot see how many people read
    its own website is being counted at rather than for.

    ITS OWN PAGE RATHER THAN THE DASHBOARD, deliberately. The dashboard
    is the first thing after every login and its job is "what needs me
    today" — an attention panel, then counts. A thirty-day chart there
    would be the largest thing on it and would compete with the one red
    card that is supposed to stand out. So: a card on the dashboard that
    links here, which is the pattern every other module follows.

    Read-only: no toggle, no recipient, no target. Those are settings and
    they live with the other settings.
    """
    return render_template("admin/visitors.html", stats=visitor_stats(),
                           target=stats_monthly_target())


@app.route("/admin/visitors/report")
@login_required
def admin_visitor_report():
    """A period's figures as a document EBWA can hand to a funder.

    PRINT-FRIENDLY HTML RATHER THAN A GENERATED PDF, deliberately. The
    only PDF libraries worth using here are a real dependency — reportlab
    is not in requirements.txt and would be a pip install on the server
    plus hand-positioned text — and the browser's own "Save as PDF"
    produces a better artefact than either: selectable text, the site's
    real fonts (self-hosted, so nothing is fetched while printing), and
    a page that is readable on screen and by a screen reader before
    anybody prints it.

    Open to EVERY admin, like the visitor summary. A grant application
    is EBWA's work, not Netbus's.
    """
    start, end, error = parse_report_period(request.args)
    if error:
        flash(error, "error")
    return render_template("admin/visitor_report.html",
                           r=period_report(start, end), error=error)


# ---------------------------------------------------------------- admin: users
# Super admin (Netbus) only. Every guard here is enforced server-side —
# the UI hides impossible actions, but the route is what refuses them.
@app.route("/admin/users")
@super_admin_required
def admin_users():
    rows = User.query.order_by(User.email).all()
    return render_template("admin/users.html", rows=rows, roles=ROLES,
                           min_length=MIN_PASSWORD_LEN)


@app.route("/admin/users/new", methods=["POST"])
@super_admin_required
def admin_user_create():
    email = request.form.get("email", "").lower().strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "admin")
    if not email or "@" not in email or len(email) > 120:
        flash("Please enter a valid email address.", "error")
    elif role not in ROLES:
        flash("Unknown role.", "error")
    elif len(password) < MIN_PASSWORD_LEN:
        flash("The password must be at least %d characters long."
              % MIN_PASSWORD_LEN, "error")
    elif User.query.filter_by(email=email).first():
        flash("There is already an account for %s." % email, "error")
    else:
        u = User(email=email)
        u.role = role
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        log_action("user_create", entity=u,
                   summary="Created account %s with role %s." % (email, role))
        flash("Account created for %s." % email, "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/password", methods=["POST"])
@super_admin_required
def admin_user_password(user_id):
    u = db.session.get(User, user_id) or abort(404)
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")
    if len(password) < MIN_PASSWORD_LEN:
        flash("The password must be at least %d characters long."
              % MIN_PASSWORD_LEN, "error")
    elif password != confirm:
        flash("The two passwords do not match — please retype them.", "error")
    else:
        u.set_password(password)
        db.session.commit()
        log_action("user_password_reset", entity=u,
                   summary="Reset the password for %s." % u.email)
        flash("Password reset for %s. Tell them to change it once they are "
              "in, from their own Account page." % u.email, "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/reset-2fa", methods=["POST"])
@super_admin_required
def admin_user_reset_2fa(user_id):
    u = db.session.get(User, user_id) or abort(404)
    if not u.totp_enabled and not u.totp_secret:
        flash("%s does not have two-factor authentication set up." % u.email,
              "error")
    else:
        clear_user_2fa(u)
        db.session.commit()
        log_action("user_2fa_reset", entity=u,
                   summary="Cleared two-factor authentication for %s."
                           % u.email)
        flash("Two-factor authentication cleared for %s — they can log in "
              "with their password and set it up again." % u.email, "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@super_admin_required
def admin_user_role(user_id):
    u = db.session.get(User, user_id) or abort(404)
    role = request.form.get("role", "")
    # The last-super-admin rail is checked before the self rail because it
    # is the stronger invariant, and because on the web it is only ever
    # reachable by the sole super admin targeting themselves — anyone else
    # who could ask would themselves be a second super admin. The CLI can
    # hit it for any user, which is where it does the real work.
    if role not in ROLES:
        flash("Unknown role.", "error")
    elif role != u.role and is_last_super_admin(u):
        flash("%s is the only super admin left, so they can't be demoted. "
              "Promote someone else first." % u.email, "error")
    elif u.id == current_user.id:
        flash("You can't change your own role — ask another super admin to "
              "do it.", "error")
    elif role == u.role:
        flash("%s already has that role." % u.email, "error")
    else:
        was = u.role
        u.role = role
        db.session.commit()
        log_action("user_role_change", entity=u,
                   summary="Changed %s from %s to %s." % (u.email, was, role))
        flash("%s is now %s." % (u.email, role), "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@super_admin_required
def admin_user_delete(user_id):
    u = db.session.get(User, user_id) or abort(404)
    if is_last_super_admin(u):      # checked first — see admin_user_role
        flash("%s is the only super admin left, so this account can't be "
              "deleted. Promote someone else first." % u.email, "error")
    elif u.id == current_user.id:
        flash("You can't delete your own account — ask another super admin "
              "to do it.", "error")
    else:
        RecoveryCode.query.filter_by(user_id=u.id).delete()
        gone, email, was = ("User", u.id), u.email, u.role
        db.session.delete(u)
        db.session.commit()
        log_action("user_delete", entity=gone,
                   summary="Deleted account %s (role %s)." % (email, was))
        flash("Account deleted: %s." % email, "ok")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------- admin: settings
# Super admin (Netbus) only — client admins never see the nav link and
# get a 403 if they find the URL.
@app.route("/admin/features")
@super_admin_required
def admin_features():
    backup_started_flash()
    return render_template(
        "admin/features.html", rows=FEATURES, flags=feature_flags(),
        settings=mail_settings(), fields=MAIL_SETTINGS,
        modes=SECURITY_MODES, mail_ready=mail_configured(),
        password_set=password_is_set(), password_setting=mail_password_setting(),
        fernet_key_present=fernet() is not None,
        home_sections=HOME_SECTIONS, home_order=home_section_order(),
        home_hidden=home_hidden_sections(),
        home_order_default=HOME_ORDER_DEFAULT_NAMES,
        stats=visitor_stats(), report_on=stats_report_on(),
        report_to=stats_report_setting(),
        target=stats_monthly_target(),
        default_target=STATS_TARGET_DEFAULT,
        raw_days=pageview_raw_days(), raw_min=PAGEVIEW_RAW_MIN,
        raw_max=PAGEVIEW_RAW_MAX, row_bytes=PAGEVIEW_ROW_BYTES,
        storage_low=human_bytes(200 * PAGEVIEW_RAW_MIN * PAGEVIEW_ROW_BYTES),
        storage_high=human_bytes(200 * PAGEVIEW_RAW_MAX * PAGEVIEW_ROW_BYTES),
        health=server_health(), backup_limit=BACKUP_MANUAL_PER_HOUR,
        backup=backup_status(), alerts_on=security_alerts_on(),
        backup_state=backup_state(),
        backup_labels=BACKUP_STATE_LABELS, backup_pills=BACKUP_STATE_PILLS,
        backup_stale_minutes=BACKUP_STALE_MINUTES,
        sftp=sftp_settings(), sftp_ready=sftp_ready(),
        last_transfer=(BackupRun.query
                       .filter(BackupRun.transfer_status.in_(("ok", "failed")))
                       .order_by(BackupRun.started_at.desc(),
                              BackupRun.id.desc()).first()),
        alert_to=security_alert_setting(),
        failed_logins=failed_logins_since(),
        failed_window=FAILED_LOGIN_WINDOW_HOURS,
        alert_threshold=ALERT_IP_THRESHOLD,
        # For the "how to change the password" instructions: real paths
        # for THIS deployment, never hardcoded in the template.
        deploy={"env_file": DEPLOY_ENV_FILE, "path": DEPLOY_PATH,
                "service": DEPLOY_SERVICE, "user": DEPLOY_USER})


def valid_address(value):
    """Good enough for a form: one @, something either side, no spaces."""
    if not value or len(value) > 200 or " " in value:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".")


def set_mail_block(key, value):
    block = Block.query.filter_by(key=key).first()
    if not block:
        label = dict((k, l) for _f, k, _e, l in MAIL_SETTINGS).get(key, key)
        block = Block(key=key, label=label, kind="text", group="site")
        db.session.add(block)
    block.value = value


@app.route("/admin/settings/mail", methods=["POST"])
@super_admin_required
def admin_mail_settings():
    """Save the SMTP settings a super admin typed.

    Every field may be left blank, and blank means "fall back to the
    environment variable" rather than "no value" — that is what keeps an
    existing deployment working after this page appears.
    """
    host = request.form.get("host", "").strip()
    port = request.form.get("port", "").strip()
    user = request.form.get("user", "").strip()
    security = request.form.get("security", "").strip()
    sender = request.form.get("sender", "").strip()
    recipient = request.form.get("recipient", "").strip()
    # NEVER stripped, never logged, never echoed back: an empty box means
    # "keep the one already stored", exactly as the field says.
    password = request.form.get("password", "")

    errors = []
    if password and not fernet():
        errors.append("There is no FERNET_KEY on the server, so a password "
                      "cannot be stored safely. Ask Netbus to set one.")
    if port:
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            errors.append("The port must be a number between 1 and 65535.")
    if security and security not in dict(SECURITY_MODES):
        errors.append("Choose one of the encryption options.")
    if sender and not valid_address(sender):
        errors.append("The from address does not look like an email address.")
    if recipient and not valid_address(recipient):
        errors.append("The enquiries address does not look like an email "
                      "address.")
    # A username, port or encryption setting with no server anywhere is a
    # half-configured mailer that will fail at the worst moment.
    if not host and (port or user or security) \
            and not os.environ.get("SMTP_HOST", "").strip():
        errors.append("Set the mail server as well — the other settings "
                      "have nothing to connect to without it.")

    if errors:
        for message in errors:
            flash(message, "error")
        return redirect(url_for("admin_features"))

    before = {f: v["value"] for f, v in mail_settings().items()}
    for key, value in (("smtp_host", host), ("smtp_port", port),
                       ("smtp_user", user), ("smtp_security", security),
                       ("smtp_from", sender), (MAIL_TO_KEY, recipient)):
        set_mail_block(key, value)
    if password:
        set_mail_block(MAIL_PASSWORD_KEY, encrypt_secret(password))
    db.session.commit()

    after = {f: v["value"] for f, v in mail_settings().items()}
    changed = sorted(f for f in after if after[f] != before[f])
    # The summary says THAT the password changed, never what it is — the
    # same rule as every other credential in here.
    note = "%s%s" % (describe_changes(changed) if changed else "no changes",
                     "; password changed" if password else "")
    log_action("edit", entity=("Block", None),
               summary="Saved the email settings (%s)." % note)
    flash("Email settings saved. Enquiries go to %s."
          % (mail_recipient() or "nobody — set an address"),
          "ok" if mail_recipient() else "error")
    return redirect(url_for("admin_features"))


@app.route("/admin/settings/test-mail", methods=["POST"])
@super_admin_required
def admin_test_mail():
    """Send one test email and say exactly what happened.

    Rate limited: a button that emails an address somebody types is a
    relay otherwise, however few people can reach it. Every attempt is
    logged with the recipient and the outcome — and never the password,
    which describe_mail_failure() and _scrubbed() both take care of.
    """
    to = request.form.get("to", "").strip() or mail_recipient()
    if not valid_address(to):
        flash("Give a valid address to send the test to.", "error")
        return redirect(url_for("admin_features"))
    if rate_limited("test_mail"):
        flash("That is enough test emails for one hour — the button is "
              "limited so it cannot be used to send mail to strangers.",
              "error")
        log_action("test_mail", summary="Test email to %s refused: too many "
                                        "attempts in an hour." % to)
        return redirect(url_for("admin_features"))

    ok, reason = send_mail_result(
        to, "EBWA website test email",
        "This is a test from the EBWA website, sent from the Settings "
        "page.\n\nIf you are reading it, outgoing email works and "
        "enquiries from the contact form will arrive.\n")
    log_action("test_mail",
               summary=("Sent a test email to %s." % to if ok else
                        "Test email to %s failed — %s." % (to, reason)))
    if ok:
        flash("Test email sent to %s. If it does not arrive, check the "
              "spam folder before changing anything." % to, "ok")
    else:
        flash("Could not send to %s — %s." % (to, reason), "error")
    return redirect(url_for("admin_features"))


@app.route("/admin/settings/backup", methods=["POST"])
@super_admin_required
def admin_backup_now():
    """START a backup from the Settings page, and return straight away.

    IT USED TO DO THE WHOLE JOB INSIDE THE REQUEST, and that was not
    merely slow to look at. gunicorn's sync worker sends the arbiter a
    heartbeat between requests, not during one, so a request that blocks
    past `--timeout` (30 seconds by default, and the unit sets no other)
    has its worker killed and restarted underneath it. At 6MB the archive
    beat that; with a real uploads folder plus a minute of SFTP it would
    not, and what the super admin would see is a 502 from a backup that
    may well have been written — with nginx's own 60-second read timeout
    behind it and a BackupRun stuck at "running". So the page now starts
    the work and reports on it, which is both the visible-progress
    feature and the fix for that.

    Still the same Python the CLI runs — no shell, no command built from
    anything a request supplied, and nothing on this page that could
    become one.

    Refuses while another run is in progress, and beyond
    BACKUP_MANUAL_PER_HOUR an hour. Both refusals are logged: the audit
    trail should show that somebody tried, not just the runs that
    happened.
    """
    # Concurrency before counting: one running backup is a real reason to
    # refuse, where "you have pressed this a lot" is only a brake.
    running = backup_in_progress()
    if running:
        log_action("backup",
                   summary="Refused a backup from the Settings page: one "
                           "started at %s is still running."
                           % utc_as_uk(running.started_at)
                           .strftime("%H:%M"))
        flash("A backup is already running (started %s). Wait for it to "
              "finish rather than starting a second one alongside it."
              % utc_as_uk(running.started_at).strftime("%H:%M"), "error")
        return redirect(url_for("admin_features") + "#backups")
    if rate_limited("backup"):
        log_action("backup",
                   summary="Refused a backup from the Settings page: more "
                           "than %d in an hour." % BACKUP_MANUAL_PER_HOUR)
        flash("That is %d backups in an hour, which is the limit for this "
              "button. The nightly job is what keeps them current."
              % BACKUP_MANUAL_PER_HOUR, "error")
        return redirect(url_for("admin_features") + "#backups")

    # Who pressed it, captured HERE: the thread has no request to ask.
    run = start_backup(reason="manual", actor=current_actor())
    if run is None:
        # Lost the claim to another click in the same instant.
        flash("A backup was already starting, so this one was not begun. "
              "The one that is running is shown under Backups.", "error")
        return redirect(url_for("admin_features") + "#backups")
    # NOT a flash written here. Between this line and the page being
    # drawn the backup may well have finished — on a small site it takes
    # under a second — and a message composed now would be describing a
    # state that no longer exists by the time anybody reads it. The next
    # render says what is actually true; see `backup_started_flash()`.
    flask_session["backup_started"] = run.id
    return redirect(url_for("admin_features") + "#backups")


def backup_started_flash():
    """Flash the outcome of a backup just started, if one just was.

    THE MESSAGE AND THE PANEL MUST AGREE, and the only way to guarantee
    that is to write the message from the same read of the same row that
    the panel is drawn from, in the same render. The button used to flash
    "the panel below shows how it is getting on" from inside the POST,
    which was wrong in two ways at once:

      * on a small site the backup FINISHES before the page is drawn, so
        the sentence promised progress on something already done while
        the panel — correctly — said Finished;
      * it pointed "below" at a panel about 1,900px down a 9,000px page.
        The redirect lands on #backups, so the panel is on screen and the
        flash is far above it; read the flash instead, at the top, and
        the panel is as good as absent. Neither is ever in view with the
        other, so each has to stand on its own.

    So: no direction, no "below", and no claim about what is happening
    that this render has not just checked.
    """
    run_id = flask_session.pop("backup_started", None)
    if not run_id:
        return
    state = backup_state()
    run = state["run"]
    if run is None or run.id != run_id:
        # It has been overtaken by a later run, or pruned from under us.
        # Say the plain true thing rather than describing the wrong row.
        flash("Backup started.", "ok")
        return
    if state["state"] == "running":
        flash("Backup started, and it is running now. It carries on if you "
              "leave this page; the Backups panel keeps itself up to date.",
              "ok")
    elif state["state"] == "ok":
        flash("Backup finished already — %s. %s"
              % (state["detail"].rstrip("."),
                 "It was quick because this site is small."), "ok")
    elif state["state"] == "failed":
        flash("The backup failed: %s" % state["detail"], "error")
    else:
        flash("Backup started.", "ok")


@app.route("/admin/help")
@login_required
def admin_help():
    """The written guide to running the site, with screenshots.

    Every role, deliberately: it is EBWA's manual for their own website,
    and the pages it explains are the ones a client admin uses all day.
    The handful of super-admin-only screens it mentions are labelled as
    Netbus's rather than hidden, so a trustee reading it knows the
    feature exists and who to ask.

    The pictures come from `tools/capture-guide-shots.py`, which drives
    the real admin in a browser — so when a screen changes the guide is
    brought back into line by re-running it rather than by somebody
    noticing.
    """
    return render_template("admin/help.html")


@app.route("/admin/settings/backup.json")
@super_admin_required
def admin_backup_json():
    """What the backup panel shows, for its poll while one is running.

    Super admins only and rate limited, like the health panel's. READ-
    ONLY and parameterless: it starts nothing, and there is nothing from
    the request to sanitise.
    """
    if rate_limited("health"):
        return jsonify({"error": "Too many refreshes — slow down."}), 429
    state = backup_state()
    return jsonify({
        "state": state["state"],
        "busy": state["busy"],
        "started": state["started"],
        "finished": state["finished"],
        "detail": state["detail"],
        "label": BACKUP_STATE_LABELS.get(state["state"], ""),
        "pill": BACKUP_STATE_PILLS.get(state["state"], "grey"),
    })


@app.route("/admin/settings/security-alerts", methods=["POST"])
@super_admin_required
def admin_security_alerts():
    """Switch the failed-sign-in alert on or off, and say where it goes."""
    on = request.form.get("enabled") == "on"
    raw = request.form.get("alert_to", "").strip()
    addresses = parse_addresses(raw)
    bad = [a for a in addresses if not valid_address(a)]
    if bad:
        flash("These do not look like email addresses: %s. Separate several "
              "with commas." % ", ".join(bad), "error")
        return redirect(url_for("admin_features"))

    block = Block.query.filter_by(key=SECURITY_ALERT_KEY).first()
    if not block:
        block = Block(key=SECURITY_ALERT_KEY, group="site", kind="text",
                      label="Email alerts for failed sign-ins")
        db.session.add(block)
    block.value = "1" if on else ""
    set_mail_block(SECURITY_ALERT_TO_KEY, ", ".join(addresses))
    db.session.commit()

    setting = security_alert_setting()
    log_action("edit", entity=("Block", block.id),
               summary=("Failed-sign-in alerts %s; alerts go to %s."
                        % ("on" if on else "off",
                           setting["value"] or "nobody — no address set")))
    if on and not setting["recipients"]:
        flash("Alerts are on, but there is no address to send them to — "
              "set one here or an enquiries address under Email.", "error")
    else:
        flash("Saved. Alerts are %s%s."
              % ("on" if on else "off",
                 ", going to %s" % setting["value"] if on
                 and setting["recipients"] else ""), "ok")
    return redirect(url_for("admin_features"))


@app.route("/admin/settings/test-alert", methods=["POST"])
@super_admin_required
def admin_test_alert():
    """Send a sample security alert, so the route can be proved.

    Worth having separately from the mail test: this one goes to the
    SECURITY address, which is the whole point of the setting, and it
    proves it without anybody having to fail ten sign-ins to find out.
    """
    setting = security_alert_setting()
    to = ", ".join(setting["recipients"])
    if not to:
        flash("There is no address to send alerts to yet.", "error")
        return redirect(url_for("admin_features"))
    if rate_limited("test_mail"):
        flash("That is enough test emails for one hour.", "error")
        return redirect(url_for("admin_features"))

    ok, reason = send_mail_result(
        to, "EBWA website: test security alert",
        "This is a TEST alert from the EBWA website, sent from the "
        "Settings page.\n\n"
        "A real one is sent when %d or more sign-ins fail from the same "
        "address within an hour, and looks like this — with the addresses "
        "that were tried and the IP they came from. It never contains a "
        "password: the site does not record one.\n\n"
        "If you are reading this, alerts will reach you.\n"
        % ALERT_IP_THRESHOLD)
    log_action("test_mail",
               summary=("Sent a test security alert to %s." % to if ok else
                        "Test security alert to %s failed — %s."
                        % (to, reason)))
    flash("Test alert sent to %s." % to if ok else
          "Could not send to %s — %s." % (to, reason), "ok" if ok else "error")
    return redirect(url_for("admin_features"))


@app.route("/admin/settings/sftp", methods=["POST"])
@super_admin_required
def admin_sftp_settings():
    """Save the NAS transfer settings.

    An EMPTY password box means "keep the one already stored", exactly as
    the field label says — otherwise every save of an unrelated field
    would quietly wipe the credential.
    """
    host = request.form.get("host", "").strip()
    port = request.form.get("port", "").strip() or str(SFTP_DEFAULT_PORT)
    user = request.form.get("user", "").strip()
    path = request.form.get("path", "").strip()
    schedule = request.form.get("schedule", "").strip() \
        or SFTP_DEFAULT_SCHEDULE
    keep = request.form.get("keep", "").strip() or str(SFTP_DEFAULT_KEEP)
    password = request.form.get("password", "")
    enabled = request.form.get("enabled") == "on"

    errors = []
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        errors.append("The port must be a number between 1 and 65535.")
    if not keep.isdigit() or int(keep) < 1:
        errors.append("Keep at least one archive on the NAS.")
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", schedule):
        errors.append("The transfer time must look like 02:30, in British "
                      "time.")
    if path and not path.startswith("/"):
        errors.append("The folder on the NAS must start with a slash.")
    if enabled and not (host and user and path):
        errors.append("To switch transfers on, fill in the address, "
                      "username and folder.")
    if password and not fernet():
        errors.append("There is no FERNET_KEY on the server, so a password "
                      "cannot be stored safely. Ask Netbus to set one.")
    if enabled and not password and not sftp_settings()["password_set"]:
        errors.append("Type the NAS password before switching transfers on.")

    if errors:
        for message in errors:
            flash(message, "error")
        return redirect(url_for("admin_features"))

    values = {SFTP_KEYS["enabled"]: "1" if enabled else "",
              SFTP_KEYS["host"]: host, SFTP_KEYS["port"]: port,
              SFTP_KEYS["user"]: user, SFTP_KEYS["path"]: path,
              SFTP_KEYS["schedule"]: schedule, SFTP_KEYS["keep"]: keep,
              # Whatever was typed here is a UK local time, so saving is
              # also what settles a legacy UTC value for good.
              SFTP_KEYS["schedule_tz"]: "uk"}
    if password:
        # Encrypted here and nowhere else. The plaintext never reaches
        # the database, an audit entry or a page.
        values[SFTP_KEYS["password"]] = encrypt_secret(password)
    for key, value in values.items():
        set_mail_block(key, value)
    db.session.commit()

    log_action("edit", entity=("Block", None),
               summary=("Saved the NAS backup settings: transfers %s, %s@%s"
                        ":%s%s, daily at %s UTC, keeping %s there%s."
                        % ("on" if enabled else "off", user or "—",
                           host or "—", port, path or "", schedule, keep,
                           ", password changed" if password else "")))
    flash("NAS backup settings saved.", "ok")
    return redirect(url_for("admin_features"))


@app.route("/admin/settings/sftp/test", methods=["POST"])
@super_admin_required
def admin_sftp_test():
    """Prove the NAS is reachable, and that the folder can be written to."""
    if rate_limited("sftp_test"):
        flash("That is enough connection tests for one hour.", "error")
        return redirect(url_for("admin_features"))
    ok, message = test_sftp()
    log_action("sftp_test",
               summary=("NAS connection test succeeded — %s." % message
                        if ok else "NAS connection test failed — %s."
                        % message))
    flash("Connection test: %s." % message if ok else
          "Could not use the NAS — %s." % message, "ok" if ok else "error")
    return redirect(url_for("admin_features"))


@app.route("/admin/settings/health.json")
@super_admin_required
def admin_health_json():
    """The health panel's numbers, for its 30-second refresh.

    Super admins only and rate limited: it is cheap, but it reads the
    machine, and an endpoint that reads the machine should not be
    something anybody can sit on. READ-ONLY — it takes no parameters at
    all, so there is nothing from the request to sanitise.
    """
    if rate_limited("health"):
        return jsonify({"error": "Too many refreshes — slow down."}), 429
    health = server_health()
    # Datetimes out as text; the panel only ever displays them.
    health["checked_at"] = utc_as_uk(health["checked_at"]).strftime(
        "%d %b %Y, %H:%M:%S")
    up = health["uptime"]
    up["boot"] = utc_as_uk(up["boot"]).strftime("%d %b %Y, %H:%M")         if up["boot"] else None
    up["app_started"] = utc_as_uk(up["app_started"]).strftime(
        "%d %b %Y, %H:%M")
    return jsonify(health)


# ---- Settings: how the homepage is arranged
def _set_block(key, value, group="home", label=""):
    """Write a settings Block, creating it on a database that predates it."""
    block = Block.query.filter_by(key=key).first()
    if block is None:
        block = Block(group=group, key=key, label=label or key, kind="text")
        db.session.add(block)
    changed = (block.value or "") != value
    block.value = value
    return changed


@app.route("/admin/stats-report", methods=["POST"])
@super_admin_required
def admin_stats_report_save():
    """Save the monthly report's switch, recipient and target."""
    back = redirect(url_for("admin_features") + "#visitorStats")
    raw_to = (request.form.get("report_to") or "").strip()
    if raw_to and not parse_addresses(raw_to):
        flash("That does not look like an email address. Separate several "
              "with commas, or leave it empty to use the enquiries "
              "address.", "error")
        return back
    try:
        target = int((request.form.get("target") or "").strip())
    except ValueError:
        flash("The target must be a whole number of page views.", "error")
        return back
    # Mirrors the field's min/max, and the field's step base is its min
    # — see the note beside it.
    if not 100 <= target <= 1000000:
        flash("The target must be between 100 and 1,000,000 page views.",
              "error")
        return back
    try:
        raw_days = int((request.form.get("raw_days") or "").strip())
    except ValueError:
        flash("The number of days of detail must be a whole number.",
              "error")
        return back
    if not PAGEVIEW_RAW_MIN <= raw_days <= PAGEVIEW_RAW_MAX:
        flash("Keep between %d and %d days of per-page detail."
              % (PAGEVIEW_RAW_MIN, PAGEVIEW_RAW_MAX), "error")
        return back
    was_on = stats_report_on()
    changed = []
    if _set_block(STATS_REPORT_KEY,
                  "1" if request.form.get("enabled") == "on" else "",
                  group="stats"):
        changed.append("whether it is sent")
    if _set_block(STATS_REPORT_TO_KEY, raw_to, group="stats"):
        changed.append("the recipient")
    if _set_block(STATS_TARGET_KEY, str(target), group="stats"):
        changed.append("the target")
    if _set_block(STATS_RAW_DAYS_KEY, str(raw_days), group="stats"):
        # Shortening it does not delete anything here and now: the next
        # aggregate-pageviews run folds the newly-out-of-window days into
        # totals, which is the same path every other day takes.
        changed.append("how long per-page detail is kept")
    db.session.commit()
    # The ADDRESS is not in the summary — it is somebody's email, and the
    # audit log is not the place for it. Which field moved is enough.
    log_action("edit", entity=("Block", 0),
               summary="Changed the monthly website report (%s). Now %s."
                       % (" and ".join(changed) if changed
                          else "nothing changed",
                          "on" if stats_report_on() else "off"))
    flash("Report settings saved." if changed else "No change.", "ok")
    return back


@app.route("/admin/stats-report/send", methods=["POST"])
@super_admin_required
def admin_stats_report_test():
    """Send last month's report now, however many have gone before.

    Rate limited like the test email is: a button that emails a typed
    address is a relay otherwise.
    """
    if rate_limited("test_mail"):
        flash("Too many sends just now — try again shortly.", "error")
    else:
        flash(send_monthly_report(force=True), "ok")
    return redirect(url_for("admin_features") + "#visitorStats")


@app.route("/admin/home-sections", methods=["POST"])
@super_admin_required
def admin_home_sections_save():
    """Save the homepage's section order and which of them are shown.

    The positions arrive as one number per section. They are SORTED
    rather than trusted: two sections given the same number, or numbers
    with gaps in them, are an arrangement somebody meant, not an error
    worth refusing a whole form over. Ties keep the order the sections
    are already in, so nudging one number does not shuffle the rest.

    Visibility is read from HOME_SECTIONS, never from the form's own
    keys: an unticked checkbox posts nothing at all, so a form-shaped
    loop would read "unticked" and "not submitted" as the same thing.
    Iterating the server's own list of sections is what makes an absent
    key mean "switched off" safely.
    """
    back = redirect(url_for("admin_features") + "#homeSections")
    current = home_section_order()
    positions = []
    for index, key in enumerate(current):
        raw = (request.form.get("pos_%s" % key) or "").strip()
        try:
            pos = int(raw)
        except ValueError:
            flash("Each position must be a whole number.", "error")
            return back
        if not 1 <= pos <= len(HOME_SECTIONS):
            flash("Positions must be between 1 and %d." % len(HOME_SECTIONS),
                  "error")
            return back
        positions.append((pos, index, key))
    order = [key for _pos, _i, key in sorted(positions)]
    hidden = [k for k in order if request.form.get("show_%s" % k) != "on"]

    was_hidden = home_hidden_sections()
    changed = []
    if order != current:
        changed.append("order")
    if set(hidden) != was_hidden:
        changed.append("which sections are shown")
    _set_block(HOME_ORDER_KEY, ",".join(order))
    _set_block(HOME_HIDDEN_KEY, ",".join(hidden))
    db.session.commit()
    # The summary names the ARRANGEMENT, which is the whole content of
    # this setting — there is no personal data here, and "the order
    # changed" without saying to what would send somebody to the
    # database to find out what they had just done.
    log_action("edit", entity=("Block", 0),
               summary="Changed the homepage sections (%s). Order: %s. "
                       "Hidden: %s."
                       % (" and ".join(changed) if changed
                          else "nothing changed",
                          " → ".join(HOME_SECTION_NAMES[k] for k in order),
                          ", ".join(HOME_SECTION_NAMES[k] for k in hidden)
                          or "none"))
    flash("Homepage sections saved.", "ok")
    return back


@app.route("/admin/home-sections/reset", methods=["POST"])
@super_admin_required
def admin_home_sections_reset():
    """Back to the order the site ships with, and everything shown.

    To the CONSTANT, never to whatever was saved last — the same rule as
    the marquee speed reset. A button that restores the previous
    arrangement only takes somebody back to their last experiment.
    """
    moved = _set_block(HOME_ORDER_KEY, HOME_ORDER_DEFAULT)
    shown = _set_block(HOME_HIDDEN_KEY, "")
    db.session.commit()
    # Logged even when it changed nothing: "somebody pressed reset" is
    # exactly what you want to see when asking why the front page looks
    # different from how a trustee remembers leaving it.
    log_action("edit", entity=("Block", 0),
               summary="Reset the homepage sections to the default order, "
                       "with every section shown%s."
                       % ("" if (moved or shown) else " (nothing changed)"))
    flash("Homepage sections put back to the default order.", "ok")
    return redirect(url_for("admin_features") + "#homeSections")


@app.route("/admin/features/<name>/toggle", methods=["POST"])
@super_admin_required
def admin_feature_toggle(name):
    if name not in FEATURE_DEFAULTS:
        abort(404)
    flag = FeatureFlag.query.filter_by(name=name).first()
    if not flag:
        flag = FeatureFlag(name=name, enabled=FEATURE_DEFAULTS[name])
        db.session.add(flag)
    flag.enabled = not flag.enabled
    db.session.commit()
    log_action("feature_toggle", entity=flag,
               summary="Switched the %s feature %s."
                       % (FEATURE_LABELS[name],
                          "on" if flag.enabled else "off"))
    flash("%s is now %s. Nothing was deleted — switching it back on "
          "restores the pages as they were."
          % (FEATURE_LABELS[name], "on" if flag.enabled else "off"), "ok")
    return redirect(url_for("admin_features"))


# ---------------------------------------------------------------- CLI
DEFAULT_BLOCKS = [
    # group, key, label, kind, default value
    ("site", "site_phone", "Phone number", "text", "020 8804 4006"),
    ("site", "site_address", "Address", "text", "180 High Street, Ponders End, Enfield EN3 4EU"),
    # Mail settings, all set from the super-admin Settings page and all
    # seeded EMPTY: blank means "use the environment variable", so an
    # existing deployment carries on exactly as it was. Hidden from the
    # ordinary content editor below. The PASSWORD is deliberately absent
    # — it lives in SMTP_PASSWORD and nowhere else.
    ("site", "site_mail_to", "Where enquiries are emailed", "text", ""),
    ("site", "smtp_host", "Mail server", "text", ""),
    ("site", "smtp_port", "Mail server port", "text", ""),
    ("site", "smtp_user", "Mail server username", "text", ""),
    ("site", "smtp_security", "Mail server encryption", "text", ""),
    ("site", "smtp_from", "Email is sent from", "text", ""),
    # Fernet ciphertext, never plaintext; empty means "use SMTP_PASSWORD".
    ("site", "smtp_password_enc", "Mail server password (encrypted)",
     "text", ""),
    # "1" switches on the failed-sign-in alert email. Off by default:
    # nobody wants a mailbox full of alerts they did not ask for.
    ("site", "security_alert_email", "Email alerts for failed sign-ins",
     "text", ""),
    # Empty means "wherever enquiries go". Set it once EBWA's own address
    # is in use, so alerts keep reaching whoever runs the server.
    ("site", "site_security_alert_to", "Security alerts go to", "text", ""),
    # Offsite transfer to the NAS over SFTP. All empty by default: the
    # feature is off until somebody fills it in. The password Block holds
    # FERNET-ENCRYPTED ciphertext and never plaintext.
    ("site", "sftp_enabled", "Send backups to the NAS", "text", ""),
    ("site", "sftp_host", "NAS address", "text", ""),
    ("site", "sftp_port", "NAS SFTP port", "text", ""),
    ("site", "sftp_user", "NAS username", "text", ""),
    ("site", "sftp_password_enc", "NAS password (encrypted)", "text", ""),
    ("site", "sftp_remote_path", "Folder on the NAS", "text", ""),
    ("site", "sftp_schedule", "Daily transfer time (UK time)", "text", ""),
    ("site", "sftp_schedule_tz", "Schedule time zone marker", "text", ""),
    ("site", "sftp_keep", "Archives to keep on the NAS", "text", ""),
    ("home", "home_hero_title", "Hero headline", "text",
     "Empowering communities, enriching lives in Enfield."),
    ("home", "home_hero_text", "Hero paragraph", "text",
     "EBWA is a community-driven organisation dedicated to improving the quality of life "
     "for the Bangladeshi and wider communities across the London Borough of Enfield."),
    ("home", "home_hero_image", "Hero photo", "image", ""),
    ("home", "home_stat_1", "Stat 1 (elderly supported)", "text", "100+"),
    ("home", "home_stat_2", "Stat 2 (women trained)", "text", "24"),
    ("home", "home_stat_3", "Stat 3 (cricket project)", "text", "25"),
    ("home", "home_stat_4", "Stat 4 (first aid trained)", "text", "20"),
    # Both HIDDEN from the content editor: arranging the front page is a
    # design decision made on Settings, not day-to-day content work, and
    # a comma-separated list of keys is not something to hand somebody
    # in a text box beside "Hero headline". Seeded with the order the
    # site shipped with, and with nothing hidden, so neither changes
    # anything until a super admin moves something.
    ("home", HOME_ORDER_KEY, "Homepage section order", "text",
     HOME_ORDER_DEFAULT),
    ("home", HOME_HIDDEN_KEY, "Homepage sections switched off", "text", ""),
    # About's video position, beside its url and poster. Hidden from the
    # content editor like the other two — it is set on the About tab's
    # video form, not typed into a text box.
    ("about", ABOUT_VIDEO_POSITION_KEY, "About video position", "text",
     VIDEO_POSITION_DEFAULT),
    # The monthly report. Seeded OFF and empty: a site that starts
    # emailing a board the moment it is deployed is a site nobody asked.
    # All three hidden from the content editor — they are settings, and
    # the recipient is an address rather than page copy.
    ("stats", STATS_REPORT_KEY, "Monthly report on", "text", ""),
    ("stats", STATS_REPORT_TO_KEY, "Monthly report goes to", "text", ""),
    ("stats", STATS_TARGET_KEY, "Monthly page-view target", "text",
     str(STATS_TARGET_DEFAULT)),
    ("stats", STATS_RAW_DAYS_KEY, "Days of per-page detail", "text",
     str(PAGEVIEW_RAW_DAYS)),
    # The association's own identity, for the downloadable report. A
    # grant document with the wrong legal name or no charity number is
    # worse than no document, and neither is Netbus's to invent.
    ("org", ORG_NAME_KEY, "Association name (legal)", "text",
     "Enfield Bangladesh Welfare Association"),
    ("org", ORG_CHARITY_NO_KEY, "Registered charity number", "text", ""),
    # How the partner row moves. Set on the partners admin page, not in
    # the text editor, so both are hidden below.
    ("partners", PARTNER_MOTION_KEY, "Partner row movement", "text",
     "scroll"),
    ("partners", PARTNER_STEP_KEY, "Seconds between steps", "text",
     str(PARTNER_STEP_DEFAULT)),
    ("partners", PARTNER_GLIDE_KEY, "How long one step takes (ms)", "text",
     str(PARTNER_GLIDE_DEFAULT)),
    ("partners", PARTNER_DRIFT_KEY, "Drift speed (pixels a second)", "text",
     str(PARTNER_DRIFT_DEFAULT)),
    # The same four for the row of quotes, which uses the same marquee.
    # It starts STILL — see MOTION_ROWS for why a row of words is not a
    # row of logos.
    ("testimonials", MOTION_ROWS["testimonials"]["mode_key"],
     "Testimonial row movement", "text",
     MOTION_ROWS["testimonials"]["default_mode"]),
    ("testimonials", MOTION_ROWS["testimonials"]["step_key"],
     "Seconds between steps", "text", str(PARTNER_STEP_DEFAULT)),
    ("testimonials", MOTION_ROWS["testimonials"]["glide_key"],
     "How long one step takes (ms)", "text", str(PARTNER_GLIDE_DEFAULT)),
    ("testimonials", MOTION_ROWS["testimonials"]["drift_key"],
     "Drift speed (pixels a second)", "text", str(PARTNER_DRIFT_DEFAULT)),
    ("about", "about_title", "Page title", "text", "About EBWA"),
    ("about", "about_body", "Main text", "text",
     "Founded by Choudhury Mohammed Anwar MBE, former Mayor of Enfield, EBWA provides "
     "education, welfare and social support to the whole community."),
    ("about", "about_image", "Founder / about photo", "image", ""),
    # Chosen through the layout picker, not the plain text editor.
    ("about", "about_layout", "Page layout", "text", "classic"),
    # Set with the video box in the shared rich-content partial, not
    # typed into the content editor — like the layout above.
    ("about", "about_video_url", "Video link", "text", ""),
    ("about", "about_video_thumb", "Video poster image", "text", ""),
    ("journey", "journey_intro", "Intro text", "text",
     "From our earliest gatherings to the projects we run today, this is "
     "the story of EBWA's work in Enfield — the milestones we have reached "
     "and the funders who helped us get there."),
    ("contact", "contact_eyebrow", "Section eyebrow", "text", "Contact us"),
    ("contact", "contact_heading", "Page heading", "text",
     "Visit us in Ponders End"),
    ("contact", "contact_intro", "Intro text", "text",
     "Our centre welcomes visitors for support every week. Drop in, call, or find us on the High Street."),
    ("contact", "contact_card_title", "Details box heading", "text",
     "Get in touch"),
    ("contact", "contact_label_address", "Label: address", "text", "Address"),
    ("contact", "contact_label_phone", "Label: telephone", "text",
     "Telephone"),
    ("contact", "contact_label_hours", "Label: drop-in times", "text",
     "Drop-in"),
    ("contact", "contact_hours", "Opening / drop-in times", "text",
     "Weekly sessions — call for current times"),
    # Placeholders only. EBWA must supply the real wording before launch —
    # Netbus cannot write a charity's privacy notice on its behalf.
    ("legal", "privacy_title", "Privacy page title", "text",
     "Privacy notice"),
    ("legal", "privacy_body", "Privacy page text", "text",
     "PLACEHOLDER — EBWA needs to replace this with the association's own "
     "privacy notice before the site goes live.\n"
     "It should say what personal information EBWA collects (for example "
     "enquiries sent through the contact form, membership applications, "
     "newsletter sign-ups and donation and Gift Aid records), why it is "
     "held, how long it is kept, who it is shared with, and how someone "
     "can ask to see or delete their information.\n"
     "It should also give a contact point for questions about personal "
     "data.\n"
     "Edit this page in Admin → Page content → legal."),
    ("legal", "terms_title", "Terms page title", "text", "Terms of use"),
    ("legal", "terms_body", "Terms page text", "text",
     "PLACEHOLDER — EBWA needs to replace this with its own terms before "
     "the site goes live.\n"
     "It should cover how the website may be used, who owns the content "
     "and photographs, what EBWA does and does not promise about the "
     "information published here, and how to get in touch about anything "
     "on the site.\n"
     "Edit this page in Admin → Page content → legal."),
]

# The six "What we do" cards the homepage shipped with, so an existing
# site keeps exactly what it had. Seeded only into an EMPTY table: unlike
# DEFAULT_BLOCKS these are ordinary records an admin may legitimately
# delete, and a deploy must not resurrect them.
DEFAULT_SERVICES = [
    # icon, title, description
    ("📚", "Education & schools",
     "Weekend Arabic and Bengali schools, supplementary education and "
     "cultural activities."),
    ("🤝", "Elderly drop-in",
     "Regular recreational and fitness sessions tackling social isolation "
     "for older residents."),
    ("💼", "Training & employment",
     "Employability, childcare and volunteering courses for women."),
    ("⚖️", "Legal advice & translation",
     "Free advice and translation to navigate social services with "
     "confidence."),
    ("❤️", "Health & wellbeing",
     "Health awareness campaigns, counselling and wellbeing initiatives "
     "for all ages."),
    ("🛡️", "Community safety",
     "Working with local authorities and the police on legal awareness "
     "and crime prevention."),
]


def seed_services():
    """Insert the default cards if there are none. Returns how many."""
    if Service.query.count():
        return 0
    for sort, (icon, title, description) in enumerate(DEFAULT_SERVICES):
        s = Service()
        s.icon = icon
        s.title = title
        s.description = description
        s.sort = sort
        s.published = True
        db.session.add(s)
    return len(DEFAULT_SERVICES)


@app.cli.command("init-db")
def init_db():
    """Create tables and seed default content blocks and feature flags."""
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
    for name, _label, _desc, default in FEATURES:
        if not FeatureFlag.query.filter_by(name=name).first():
            db.session.add(FeatureFlag(name=name, enabled=default))
    seeded = seed_services()
    db.session.commit()
    if seeded:
        print("Seeded %d 'What we do' cards." % seeded)
    print("Database initialised.")


def _suggested_alter(table_name, col):
    """A best-effort ALTER TABLE for a column the database is missing.

    SQLite only accepts a CONSTANT default on ADD COLUMN, so a NOT NULL
    column whose default is computed (a timestamp, say) is suggested as
    nullable — which is what those columns end up as anyway. Always check
    the suggestion against the real statement in DEPLOY.md.
    """
    ddl_type = col.type.compile(db.engine.dialect)
    sql = "ALTER TABLE %s ADD COLUMN %s %s" % (table_name, col.name, ddl_type)
    # A column can deliberately give EXISTING rows one value and new ones
    # another — Partner.display_mode backfills 'text' so partners already
    # on the site keep the look they had, while a new partner defaults to
    # its logo. An ALTER is about the rows already there, so a server
    # default wins over the Python one.
    server = getattr(getattr(col, "server_default", None), "arg", None)
    if isinstance(server, str):
        sql += " DEFAULT '%s'" % server
        if not col.nullable:
            sql = sql.replace(" DEFAULT", " NOT NULL DEFAULT")
        return sql + ";"
    default = getattr(col.default, "arg", None)
    if col.default is not None and not callable(default):
        if isinstance(default, bool):
            sql += " DEFAULT %d" % int(default)
        elif isinstance(default, (int, float)):
            sql += " DEFAULT %s" % default
        else:
            sql += " DEFAULT '%s'" % default
        if not col.nullable:
            sql = sql.replace(" DEFAULT", " NOT NULL DEFAULT")
    return sql + ";"


def _suggested_index(table_name, index):
    """A CREATE INDEX for an index the database is missing.

    IF NOT EXISTS, so running the whole block from DEPLOY.md twice is
    harmless — the same reason the ALTERs above are listed one per line
    rather than as a script that stops on the first duplicate.
    """
    columns = [c.name for c in index.columns] or [
        str(e) for e in index.expressions]
    return "CREATE %sINDEX IF NOT EXISTS %s ON %s (%s);" % (
        "UNIQUE " if index.unique else "", index.name, table_name,
        ", ".join(columns))


# Everything in the database that names a file in static/uploads. One
# entry per (model, column); adding a column that holds an upload means
# adding it here, or the file it points at goes unchecked.
UPLOAD_REFERENCES = (
    ("ContentImage", "content_image", "filename", "id"),
    ("Event", "event", "image", "title"),
    ("Event", "event", "video_thumb", "title"),
    ("NewsPost", "news_post", "image", "title"),
    ("NewsPost", "news_post", "video_thumb", "title"),
    ("Milestone", "milestone", "image", "title"),
    ("Milestone", "milestone", "video_thumb", "title"),
    ("Campaign", "campaign", "image", "title"),
    ("Campaign", "campaign", "video_thumb", "title"),
    ("GalleryImage", "gallery_image", "filename", "caption"),
    ("GalleryAlbum", "gallery_album", "cover_image", "title"),
    ("Partner", "partner", "logo", "name"),
    ("Block", "block", "value", "key"),
)


def dangling_uploads():
    """[(model, column, id, label, filename)] for files that are not there.

    A row and the file it names are two different things and they can
    part company — a file deleted by hand, an uploads folder restored
    from an incomplete copy, an rsync that missed one. The page then
    renders an empty panel with alt text in it, which reads as a broken
    site, and nothing anywhere says so. This is what says so.

    Reads the uploads directory ONCE and compares in memory rather than
    stat-ing per row: a gallery of two thousand photographs is one
    listing, not two thousand syscalls.
    """
    try:
        on_disk = set(os.listdir(UPLOAD_DIR))
    except OSError:
        on_disk = set()
    out = []
    for model, table, column, label_col in UPLOAD_REFERENCES:
        try:
            rows = db.session.execute(db.text(
                "SELECT id, %s, %s FROM %s WHERE %s IS NOT NULL AND %s != ''"
                % (column, label_col, table, column, column))).fetchall()
        except Exception:
            continue                      # a table this database has not got
        for row_id, filename, label in rows:
            # The Block table holds every kind of setting; only the ones
            # that look like an upload are ours to worry about.
            if table == "block" and not re.match(r"^[0-9a-f]{32}\.[a-z0-9]+$",
                                                 filename or ""):
                continue
            if filename not in on_disk:
                out.append((model, column, row_id, label or "", filename))
    return out


# ---- the monthly report ---------------------------------------------
# A board reads this, so the wording is part of the feature rather than
# decoration. Two rules it has to keep:
#   * say what a "visit" is, ONCE and plainly. The number will be read
#     out at a meeting and quoted afterwards without whatever caveat sat
#     three paragraphs away, so the caveat goes on the same line as the
#     figure it qualifies.
#   * never imply more precision than counting person-days gives.


def stats_report_on():
    block = Block.query.filter_by(key=STATS_REPORT_KEY).first()
    return bool(block and (block.value or "").strip() == "1")


def stats_monthly_target():
    block = Block.query.filter_by(key=STATS_TARGET_KEY).first()
    try:
        value = int((block.value or "").strip()) if block else 0
    except ValueError:
        value = 0
    return value if value > 0 else STATS_TARGET_DEFAULT


def stats_report_setting():
    """Where the report goes, and where that address came from.

    Falls back to the enquiries address, like the security alerts do —
    and for the same reason it is a SEPARATE setting: a monthly figure
    is for EBWA's trustees, and the day enquiries move to a shared
    mailbox nobody would notice the report had followed them there.
    """
    block = Block.query.filter_by(key=STATS_REPORT_TO_KEY).first()
    chosen = (block.value or "").strip() if block else ""
    if chosen:
        return {"value": chosen, "recipients": parse_addresses(chosen),
                "source": "database", "label": "This page"}
    fallback = mail_settings()["recipient"]
    return {"value": fallback["value"],
            "recipients": parse_addresses(fallback["value"]),
            "source": fallback["source"],
            "label": "The enquiries address"}


def _month_before(first):
    """The first of the month before the one `first` starts."""
    if first.month == 1:
        return first.replace(year=first.year - 1, month=12, day=1)
    return first.replace(month=first.month - 1, day=1)


def _change_phrase(now, before, noun):
    """"up 12% on ..." — or something honest when there is no baseline."""
    if not before:
        return "no figure for %s to compare with" % noun
    diff = now - before
    if diff == 0:
        return "exactly the same as %s" % noun
    pct = abs(diff) * 100 // before
    return "%s %d%% on %s (%s%d)" % ("up" if diff > 0 else "down", pct,
                                     noun, "+" if diff > 0 else "", diff)


def monthly_report(for_month=None):
    """The report for a finished month: (subject, body, month_start).

    `for_month` is any date in the month being reported. Defaults to the
    month before today, which is what the first of the month wants.
    """
    today = utc_as_uk(datetime.utcnow()).date()
    month = (for_month or _month_before(today.replace(day=1))).replace(day=1)
    end = _month_end(month)
    views, visits = _pv_counts(month, end)
    prev = _month_before(month)
    prev_views, _prev_visits = _pv_counts(prev, _month_end(prev))
    try:
        year_ago = month.replace(year=month.year - 1)
    except ValueError:
        year_ago = month.replace(year=month.year - 1, day=28)
    ly_views, ly_visits = _pv_counts(year_ago, _month_end(year_ago))
    target = stats_monthly_target()
    top = (db.session.query(PageView.path, db.func.count(PageView.id))
           .filter(PageView.day >= month, PageView.day <= end)
           .group_by(PageView.path)
           .order_by(db.func.count(PageView.id).desc(), PageView.path)
           .limit(STATS_TOP_PAGES).all())

    label = month.strftime("%B %Y")
    lines = ["Website figures for %s" % label, "", ""]
    lines.append("%s page views, from %s visits."
                 % ("{:,}".format(views), "{:,}".format(visits)))
    # The caveat sits on the line under the number it qualifies, not in a
    # footnote. Somebody reading this out has to trip over it.
    lines.append("(A visit is one person on one day. Somebody who comes "
                 "back next week")
    lines.append(" counts twice, so this is not the number of different "
                 "people.)")
    lines.append("")
    lines.append("Against %s: %s."
                 % (prev.strftime("%B"),
                    _change_phrase(views, prev_views,
                                   prev.strftime("%B"))))
    if ly_views:
        lines.append("Against %s: %s."
                     % (year_ago.strftime("%B %Y"),
                        _change_phrase(views, ly_views,
                                       year_ago.strftime("%B %Y"))))
    else:
        lines.append("There are no figures for %s to compare with — the "
                     "site was not" % year_ago.strftime("%B %Y"))
        lines.append("counting visits that far back yet.")
    lines.append("")
    if views >= target:
        lines.append("The monthly target of %s page views was MET: %s over."
                     % ("{:,}".format(target), "{:,}".format(views - target)))
    else:
        lines.append("The monthly target of %s page views was not met: %s "
                     "short." % ("{:,}".format(target),
                                 "{:,}".format(target - views)))
    lines.append("")
    if top:
        lines.append("Most visited pages:")
        for path, count in top:
            lines.append("  %-28s %s" % (path, "{:,}".format(count)))
    else:
        lines.append("No per-page figures for this month. Page-level "
                     "detail is kept for")
        lines.append("%d days, so a report run late will show the totals "
                     "and not the pages." % pageview_raw_days())
    lines.append("")
    lines.append("")
    lines.append("These figures are counted on EBWA's own server. There is "
                 "no analytics")
    lines.append("service, no advertising, and nothing stored that "
                 "identifies anybody.")
    lines.append("Sent once a month by the website. To stop it, or to "
                 "change where it goes,")
    lines.append("ask Netbus.")
    return ("EBWA website: %s" % label, "\n".join(lines), month)


def monthly_report_sent_for(month):
    """Has a report for this month already gone out?

    Read from the AUDIT LOG rather than a flag, exactly as the security
    alert cooldown is: it is the record that already exists, it cannot
    drift from what actually happened, and cron may run this on several
    machines or several times without a second email.
    """
    return db.session.query(AuditLog.id).filter(
        AuditLog.action == STATS_REPORT_ACTION,
        AuditLog.summary.like("%%%s%%" % month.strftime("%B %Y"))
    ).first() is not None


def send_monthly_report(for_month=None, force=False):
    """Send it if it is due. Returns a short sentence saying what happened.

    Never raises: send_mail() does not, and neither does this — a report
    that fails is worth less than the cron job it would turn into an
    error email at three in the morning.
    """
    if not stats_report_on():
        return "The monthly report is switched off."
    subject, body, month = monthly_report(for_month)
    if not force and monthly_report_sent_for(month):
        return "Already sent the report for %s." % month.strftime("%B %Y")
    setting = stats_report_setting()
    if not setting["recipients"]:
        return "No recipient for the monthly report, and no enquiries " \
               "address to fall back to."
    # The docstring above promises this never raises, so it has to
    # be true of everything in here and not only of send_mail(): a
    # cron job that tracebacks at seven in the morning is an email
    # nobody wanted about an email nobody got.
    try:
        ok = send_mail(setting["recipients"], subject, body)
        log_action(STATS_REPORT_ACTION, entity=("Stats", 0),
                   summary="%s the monthly website report for %s to %d "
                           "recipient%s."
                           % ("Sent" if ok else "Failed to send",
                              month.strftime("%B %Y"),
                              len(setting["recipients"]),
                              "" if len(setting["recipients"]) == 1 else "s"))
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return ("Could not send the report for %s (%s)."
                % (month.strftime("%B %Y"), type(exc).__name__))
    return ("Sent the report for %s." if ok else
            "Could not send the report for %s (see the audit log).") \
        % month.strftime("%B %Y")


def period_report(start, end):
    """Everything the downloadable report shows, for [start, end].

    A DOCUMENT rather than a screenshot: it goes into grant
    applications, so it carries who it is about, what period it covers,
    what the figures mean and when it was produced. Somebody reading it
    a year later, with no access to the admin, has to be able to tell
    all of that from the page.
    """
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    views, visits = _pv_counts(start, end)
    prev_views, prev_visits = _pv_counts(prev_start, prev_end)
    try:
        ly_start = start.replace(year=start.year - 1)
        ly_end = end.replace(year=end.year - 1)
    except ValueError:                       # 29 February
        ly_start = start.replace(year=start.year - 1, day=28)
        ly_end = end.replace(year=end.year - 1, day=28)
    ly_views, ly_visits = _pv_counts(ly_start, ly_end)

    # Per-page detail only exists while the raw rows do. Saying "no
    # pages" for an older period would read as "nobody visited any";
    # saying WHY is the difference between a gap and a mistake.
    kept_from = (utc_as_uk(datetime.utcnow()).date()
                 - timedelta(days=pageview_raw_days()))
    pages_held = start >= kept_from
    top = []
    if pages_held:
        top = [{"path": path, "views": n} for path, n in
               db.session.query(PageView.path, db.func.count(PageView.id))
               .filter(PageView.day >= start, PageView.day <= end)
               .group_by(PageView.path)
               .order_by(db.func.count(PageView.id).desc(), PageView.path)
               .limit(STATS_TOP_PAGES).all()]

    blocks = blocks_for("org")
    return {
        "org": blocks.get(ORG_NAME_KEY, "") or "This website",
        "charity_number": blocks.get(ORG_CHARITY_NO_KEY, "").strip(),
        "start": start, "end": end, "days": length,
        "views": views, "visits": visits,
        "previous": {"start": prev_start, "end": prev_end,
                     "views": prev_views, "visits": prev_visits,
                     "phrase": _change_phrase(views, prev_views,
                                              "the previous %d days"
                                              % length)},
        "last_year": ({"start": ly_start, "end": ly_end,
                       "views": ly_views, "visits": ly_visits,
                       "phrase": _change_phrase(views, ly_views,
                                                "the same period last year")}
                      if (ly_views or ly_visits) else None),
        "top": top, "pages_held": pages_held, "kept_from": kept_from,
        "raw_days": pageview_raw_days(),
        "produced": utc_as_uk(datetime.utcnow()),
    }


def parse_report_period(args):
    """(start, end, error) from the query string, defaulting to last month.

    Refuses a backwards range and one longer than five years rather than
    letting somebody build a query that reads the whole table.
    """
    today = utc_as_uk(datetime.utcnow()).date()
    default_start = _month_before(today.replace(day=1))
    default_end = _month_end(default_start)

    def parse(name, fallback):
        raw = (args.get(name) or "").strip()
        if not raw:
            return fallback, None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date(), None
        except ValueError:
            return fallback, "Dates go in as YYYY-MM-DD."

    start, err1 = parse("from", default_start)
    end, err2 = parse("to", default_end)
    error = err1 or err2
    if not error and end < start:
        error = "The end of the period comes before the start."
    if not error and (end - start).days > 366 * 5:
        error = "That is more than five years — choose a shorter period."
    if error:
        return default_start, default_end, error
    return start, end, None


@app.cli.command("aggregate-pageviews")
def aggregate_pageviews_command():
    """Roll page views older than the raw window into daily totals.

    Run daily from cron, beside run-scheduled-backup:

        5 3 * * *  cd /opt/ebwa && ./venv/bin/flask --app app aggregate-pageviews

    Safe to run twice: a day already rolled has no raw rows left to
    roll. Nothing is lost by NOT running it either — the figures stay
    correct, the table just keeps growing.
    """
    days, rows = aggregate_page_views()
    if not days:
        print("Nothing older than %d days to roll up."
              % pageview_raw_days())
        return
    print("Rolled %d day%s into daily totals and deleted %d raw row%s."
          % (days, "" if days == 1 else "s", rows, "" if rows == 1 else "s"))


@app.cli.command("send-monthly-report")
@click.option("--force", is_flag=True,
              help="Send even if one has already gone out this month.")
@click.option("--month", default="",
              help="Report on this month instead, as YYYY-MM.")
def send_monthly_report_command(force, month):
    """Email the previous month's website figures, if one is due.

    Meant for cron, daily — it does nothing on the other thirty days:

        20 7 * * *  cd /opt/ebwa && ./venv/bin/flask --app app send-monthly-report

    Daily rather than monthly on purpose: a machine that was off on the
    first of the month would otherwise skip that month entirely, and
    nobody would notice until somebody asked for the figures.
    """
    for_month = None
    if month:
        try:
            for_month = datetime.strptime(month + "-01", "%Y-%m-%d").date()
        except ValueError:
            raise SystemExit("--month wants YYYY-MM, for example 2026-08.")
    print(send_monthly_report(for_month=for_month, force=force))


@app.cli.command("check-uploads")
def check_uploads():
    """Name every database row pointing at a file that is not on disk.

    Read-only. Run it after moving, restoring or syncing
    static/uploads/, and after anything that deletes files — the same
    habit as check-schema before a restart.
    """
    missing = dangling_uploads()
    if not missing:
        print("Every image reference has a file: nothing dangling.")
        return
    print("DANGLING IMAGE REFERENCES (%d):" % len(missing))
    for model, column, row_id, label, filename in missing:
        print("  - %s #%s %s.%s -> %s"
              % (model, row_id, model.lower(), column, filename))
        if label:
            print("      %s" % label[:70])
    print()
    print("  The site does NOT render these to visitors — a content image")
    print("  with no file is skipped and the layout closes up. Signed-in")
    print("  admins see a notice in its place saying which file is missing.")
    print()
    print("  Fix by uploading the photograph again on the item's own page,")
    print("  or by removing the entry. Nothing here is deleted for you:")
    print("  a file that is missing today may be one somebody is about to")
    print("  restore from a backup.")
    raise SystemExit(1)


@app.cli.command("check-schema")
def check_schema():
    """Compare the models against the database; exit 1 if anything is
    missing. Run it at the end of every deploy, before the restart:
    flask --app app check-schema"""
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    present = set(insp.get_table_names())

    missing_tables, missing_columns, missing_indexes = [], [], []
    for table in db.metadata.sorted_tables:
        if table.name not in present:
            missing_tables.append(table)
            continue
        actual = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in actual:
                missing_columns.append((table.name, col))
        # INDEXES TOO, because nothing else will ever tell you they are
        # missing: create_all() skips a table that already exists, so a
        # new index on an old table is simply never created, and until
        # this was here the only symptom was the site getting slower.
        # Compared by NAME — every index in the models is named — and
        # only in one direction: an index the database has and the
        # models do not is left alone, exactly like an orphan table.
        # SQLite's own sqlite_autoindex_* entries for unique constraints
        # are therefore ignored for free.
        have = {i["name"] for i in insp.get_indexes(table.name)}
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            if index.name and index.name not in have:
                missing_indexes.append((table.name, index))

    # Columns and tables the database has but the models no longer do.
    # Never a failure — this project never drops anything, so retired
    # modules leave harmless leftovers behind (see DEPLOY.md).
    model_tables = {t.name for t in db.metadata.sorted_tables}
    orphan_tables = sorted(present - model_tables)

    if missing_tables:
        print("MISSING TABLES (%d):" % len(missing_tables))
        for table in missing_tables:
            print("  - %s" % table.name)
        print("  Fix: flask --app app init-db")
    if missing_columns:
        print("MISSING COLUMNS (%d):" % len(missing_columns))
        for table_name, col in missing_columns:
            print("  - %s.%s" % (table_name, col.name))
        print("  Fix (check each against DEPLOY.md first):")
        for table_name, col in missing_columns:
            print("    %s" % _suggested_alter(table_name, col))
    if missing_indexes:
        print("MISSING INDEXES (%d):" % len(missing_indexes))
        for table_name, index in missing_indexes:
            print("  - %s.%s" % (table_name, index.name))
        print("  Fix (safe to re-run; each is IF NOT EXISTS):")
        for table_name, index in missing_indexes:
            print("    %s" % _suggested_index(table_name, index))
        # ASCII only: this prints on a Windows console during a deploy,
        # and the smoke test holds the whole command to that.
        print("  The site RUNS without these: they are not a 500 the way "
              "a missing column is.")
        print("  It runs more slowly, on tables that only grow, and "
              "nothing else reports them.")
    if orphan_tables:
        print("Tables in the database but not in the models: %s"
              % ", ".join(orphan_tables))
        print("  Expected for retired modules. Nothing to do: this "
              "project never drops tables.")

    if missing_tables or missing_columns or missing_indexes:
        print("\nSchema is BEHIND the code. Apply the above BEFORE "
              "restarting the app.")
        raise SystemExit(1)
    index_count = sum(len([i for i in t.indexes if i.name])
                      for t in db.metadata.sorted_tables)
    print("Schema is up to date: %d tables, all columns present, "
          "%d index%s present."
          % (len(model_tables), index_count,
             "" if index_count == 1 else "es"))


@app.cli.command("reprocess-images")
def reprocess_images():
    """Optimise every existing upload and give it a thumbnail.

    Safe to run as often as you like. A file already within the size
    ceiling, carrying no EXIF and impossible to encode smaller is left
    exactly as it is, and an existing thumbnail is never regenerated.

    Filenames NEVER change here, so nothing in the database has to be
    updated: unlike a fresh upload, a PNG stays a PNG even where a JPEG
    would be smaller. Every write goes to a temporary file first and is
    then moved into place, so an interrupted run cannot leave half an
    image behind.
    """
    if not os.path.isdir(UPLOAD_DIR):
        print("No uploads folder at %s — nothing to do." % UPLOAD_DIR)
        return

    files = sorted(f for f in os.listdir(UPLOAD_DIR)
                   if os.path.isfile(os.path.join(UPLOAD_DIR, f))
                   and f.rpartition(".")[2].lower() in ALLOWED_EXT
                   and not is_thumb(f))
    optimised = thumbed = skipped = unreadable = 0
    saved = 0

    for name in files:
        path = os.path.join(UPLOAD_DIR, name)
        ext = name.rpartition(".")[2].lower()
        before = os.path.getsize(path)
        with open(path, "rb") as fh:
            raw = fh.read()
        im = open_upload(raw)
        if im is None:
            print("  ? %-40s not a readable image, left alone" % name[:40])
            unreadable += 1
            continue

        did_something = False
        if not _is_animated(im):
            full = _scaled(im, MAX_IMAGE_WIDTH)
            fmt = ("PNG" if _has_alpha(full)
                   else {"png": "PNG", "webp": "WEBP"}.get(ext, "JPEG"))
            data = _encode(full, fmt)
            # Rewrite when there is a real reason to: it was too wide, it
            # carried EXIF, or the new bytes are MEANINGFULLY smaller.
            # The 10% floor is what makes the command safe to re-run: a
            # JPEG re-encoded from a JPEG comes out a few bytes shorter
            # every single time, so "any saving at all" would keep
            # rewriting the same files and quietly degrade them on each
            # pass. Below the floor there is nothing worth having.
            worthwhile = len(data) < before * 0.9
            if full is not im or bool(im.getexif()) or worthwhile:
                tmp = path + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
                saved += before - len(data)
                optimised += 1
                did_something = True
                print("  ~ %-40s %s -> %s" % (name[:40], _kb(before),
                                              _kb(len(data))))

            thumb_path = os.path.join(UPLOAD_DIR, thumb_name(name))
            if im.width > THUMB_WIDTH and not os.path.isfile(thumb_path):
                small = _scaled(full, THUMB_WIDTH)
                thumb = _encode(small, "PNG" if _has_alpha(small) else "JPEG")
                tmp = thumb_path + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(thumb)
                os.replace(tmp, thumb_path)
                thumbed += 1
                did_something = True
                print("  + %-40s thumbnail %s" % (name[:40], _kb(len(thumb))))
        if not did_something:
            skipped += 1

    print("\n%d file(s) looked at: %d optimised, %d thumbnail(s) created, "
          "%d already done, %d unreadable."
          % (len(files), optimised, thumbed, skipped, unreadable))
    print("Space saved on the originals: %s%s"
          % (_kb(saved), " (thumbnails add to disk, and save far more "
                         "on the wire)" if thumbed else ""))


def _kb(n):
    return "%.0f KB" % (n / 1024.0) if abs(n) < 1024 * 1024 \
        else "%.1f MB" % (n / 1024.0 / 1024.0)


@app.cli.command("test-mail")
@click.argument("recipient", required=False)
def test_mail(recipient):
    """Send a test email: flask --app app test-mail [address]

    With no address it goes wherever enquiries go. Use this after setting
    the SMTP variables, so configuration is proved before a visitor's
    question is the thing that discovers it is wrong.
    """
    settings = mail_settings()
    to = (recipient or mail_recipient()).strip()
    # Say where each value came from: the commonest email problem is not
    # a wrong setting but the wrong ONE of the two being in force.
    for field in ("host", "port", "user", "security", "sender"):
        info = settings[field]
        source = {"database": "Settings page",
                  "environment": info["env"],
                  "unset": "not set"}[info["source"]]
        print("%-14s: %-26s (%s)"
              % (info["label"], info["value"] or "—", source))
    print("%-14s: %-26s (%s)"
          % ("Password", "set" if password_is_set() else "not set",
             "SMTP_PASSWORD"))
    print("%-14s: %s" % ("Sending to", to or "—"))
    if not to or not mail_configured():
        print("\nNot enough configuration to send. Fill in the Settings "
              "page, or set the missing environment variables.")
        raise SystemExit(1)
    ok, reason = send_mail_result(
        to, "EBWA website test email",
        "This is a test from the EBWA website.\n\n"
        "If you are reading it, outgoing email works and enquiries from "
        "the contact form will arrive here.\n")
    log_action("test_mail",
               summary=("Sent a test email to %s from the command line."
                        % to if ok else
                        "Test email to %s from the command line failed — %s."
                        % (to, reason)))
    if ok:
        print("\nSent. Check %s (including the spam folder)." % to)
    else:
        print("\nFailed: %s" % reason)
        raise SystemExit(1)


@app.cli.command("backup-now")
@click.option("--keep", type=int, default=None,
              help="Archives to keep (default: the BACKUP_KEEP setting).")
def backup_now(keep):
    """Write a backup archive: flask --app app backup-now

    Database snapshot plus every upload, into one timestamped zip in
    BACKUP_DIR, with the oldest archives pruned to the retention setting.
    Safe to run at any time and safe to run twice; the site keeps serving
    throughout.

    THIS DOES NOT COPY ANYTHING OFF THIS SERVER. A zip beside the
    database survives a mistake, not a dead machine — see the README for
    the rsync cron line that turns it into a real backup.
    """
    running = backup_in_progress()
    if running:
        print("A backup started at %s is still running. Not starting a "
              "second one." % running.started_at)
        raise SystemExit(1)
    status = backup_status()
    print("Database : %s (%s)" % (status["database"] or "—",
                                  filesize_filter(status["db_size"])))
    print("Uploads  : %s (%d files, %s)"
          % (UPLOAD_DIR, status["uploads_files"],
             filesize_filter(status["uploads_size"])))
    print("Into     : %s" % status["dir"])
    run = run_backup(reason="cli")
    if run.status == "ok":
        print("\nWrote %s — %s, %d file(s)."
              % (run.filename, filesize_filter(run.size_bytes),
                 run.file_count))
        removed = prune_backups(keep) if keep is not None else 0
        kept = keep if keep is not None else BACKUP_KEEP
        print("Keeping the newest %d archive(s)%s."
              % (kept, "; removed %d older" % removed if removed else ""))
        log_action("backup",
                   summary="Wrote backup %s (%s) from the command line."
                           % (run.filename, filesize_filter(run.size_bytes)))
    else:
        print("\nBackup FAILED: %s" % run.error)
        log_action("backup",
                   summary="Backup from the command line failed — %s."
                           % run.error)
        raise SystemExit(1)


def scheduled_run_due(now=None):
    """(due, why): has today's scheduled backup time passed unserved?

    All in UTC, and answered from the BackupRun table rather than any
    state of its own — cron fires this every fifteen minutes, and the
    only safe question to ask is "has today's run happened yet?".
    """
    cfg = sftp_settings()
    now = now or datetime.utcnow()
    # THE SCHEDULE IS A UK WALL-CLOCK TIME, like every other admin-facing
    # time in this app. It used to be read as UTC while being typed by
    # somebody in Enfield, so a 19:36 schedule ran at 20:36 their time
    # for seven months of the year and read as simply not working.
    #
    # "Today" is therefore the UK calendar date, not the UTC one: for
    # half an hour after midnight BST the two disagree, and using the UTC
    # date there would ask whether YESTERDAY's run had happened.
    hour, minute = schedule_parts(cfg["schedule"])
    today = utc_as_uk(now).date()
    due_at = uk_wall_as_utc(today, hour, minute)
    if now < due_at:
        return False, ("today's %s run (UK time) is not due yet"
                       % cfg["schedule"])

    done = (BackupRun.query
            .filter(BackupRun.reason == "scheduled",
                    BackupRun.started_at >= due_at)
            .order_by(BackupRun.started_at.desc(),
                              BackupRun.id.desc()).first())
    if done is None:
        return True, "today's run has not happened yet"
    if done.status != "ok":
        return False, ("today's run already ran and failed — leaving it "
                       "until tomorrow rather than hammering it")
    if not sftp_ready() or done.transfer_status == "ok":
        return False, "today's run is done"
    if (done.transfer_attempts or 0) >= SFTP_MAX_ATTEMPTS:
        return False, ("today's transfer failed %d times — leaving it until "
                       "tomorrow, as the settings page says"
                       % done.transfer_attempts)
    return True, "today's backup is done but has not reached the NAS"


@app.cli.command("migrate-backup-schedule")
def migrate_backup_schedule():
    """Rewrite a legacy UTC backup schedule as the UK local time it means.

    WHY THIS IS A CHOICE AND NOT AN OBVIOUS SUM. A time of day in UTC is
    two different local times depending on the season: 19:36 UTC is 20:36
    in Enfield in July and 19:36 in January. So "what did the admin
    mean?" has no single answer, and there were three on offer:

      1. Leave the digits alone. The field would keep saying 19:36 and
         start meaning local, so the backup would move an hour earlier in
         real terms every summer. Rejected: it changes what the site DOES
         while showing the admin nothing, which is the exact failure this
         whole change is about.
      2. Assume GMT, so the digits never move. Correct only in winter,
         and wrong by an hour for the seven months that matter.
      3. Convert against the offset in force on the day of migration, so
         the backup goes on happening at the moment it happens now.

    Number 3, taken. The admin chose 19:36 UTC by doing the arithmetic
    for a quiet hour in their own evening; keeping the real moment keeps
    that choice, and from then on it stays put in local terms across the
    clock changes instead of drifting. On a site currently on BST the
    displayed time moves forward an hour — which is not the schedule
    changing, it is the label finally agreeing with it. On GMT nothing
    moves at all.

    Idempotent: the marker Block records that it has been done, and a
    second run says so rather than shifting the time again.
    """
    rows = {b.key: (b.value or "") for b in Block.query.filter(
        Block.key.in_((SFTP_KEYS["schedule"], SFTP_KEYS["schedule_tz"]))
    ).all()}
    if rows.get(SFTP_KEYS["schedule_tz"], "") == "uk":
        print("Already done: the schedule is stored as UK local time.")
        return
    stored = rows.get(SFTP_KEYS["schedule"], "") or SFTP_DEFAULT_SCHEDULE
    local = schedule_in_uk(stored, "")
    _set_block(SFTP_KEYS["schedule"], local, group="site",
               label="Daily transfer time (UK time)")
    _set_block(SFTP_KEYS["schedule_tz"], "uk", group="site",
               label="Schedule time zone marker")
    db.session.commit()
    if local == stored:
        print("Schedule %s kept as it was — the clocks are on GMT today, "
              "so the UTC time and the British time are the same." % stored)
    else:
        print("Schedule %s UTC is %s British time, which is when the backup "
              "already runs.\nStored as %s. The time it happens has not "
              "changed; the label now matches it." % (stored, local, local))
    log_action("backup",
               summary=("Backup schedule re-recorded as %s British time "
                        "(was %s UTC)." % (local, stored)))


@app.cli.command("run-scheduled-backup")
def run_scheduled_backup():
    """Back up and send to the NAS if today's run is due.

    Meant for cron every fifteen minutes:

      */15 * * * * cd /opt/ebwa && ./venv/bin/flask --app app \\
          run-scheduled-backup

    Deliberately NOT a background thread: gunicorn runs several workers,
    and a thread in each would mean several backups at once, all writing
    the same archive name. Cron runs one process, once.

    Idempotent within the day — run it as often as you like; it does
    nothing until the configured time has passed without a good run.
    """
    running = backup_in_progress()
    if running:
        # Most likely cause: somebody pressed the button a minute before
        # cron fired. Leave it alone; the next tick is 15 minutes away.
        print("A backup started at %s is still running. Leaving it."
              % running.started_at)
        return
    due, why = scheduled_run_due()
    if not due:
        print("Nothing to do: %s." % why)
        return

    cfg = sftp_settings()
    existing = (BackupRun.query
                .filter(BackupRun.reason == "scheduled",
                        BackupRun.status == "ok")
                .order_by(BackupRun.started_at.desc(),
                              BackupRun.id.desc()).first())
    today = datetime.utcnow().date()
    if (existing and existing.started_at.date() == today
            and existing.transfer_status != "ok"):
        # The archive exists; only the transfer is outstanding.
        print("Retrying the transfer of %s." % existing.filename)
        run = existing
    else:
        print("Backing up (%s)." % why)
        run = run_backup(reason="scheduled")
        if run.status != "ok":
            print("Backup FAILED: %s" % run.error)
            log_action("backup", summary="Scheduled backup failed — %s."
                                         % run.error)
            raise SystemExit(1)
        print("Wrote %s (%s)." % (run.filename,
                                  filesize_filter(run.size_bytes)))
        log_action("backup",
                   summary="Scheduled backup wrote %s (%s)."
                           % (run.filename,
                              filesize_filter(run.size_bytes)))

    if not sftp_ready():
        print("Transfers to the NAS are off, so the archive stays here.")
        return
    print("Sending to %s:%s ..." % (cfg["host"], cfg["path"]))
    if transfer_with_retry(run):
        print("Sent as %s. Keeping the newest %d there."
              % (run.remote_filename, cfg["keep"]))
        log_action("backup",
                   summary="Sent backup %s to the NAS at %s."
                           % (run.filename, cfg["host"]))
    else:
        print("Transfer FAILED: %s" % run.transfer_error)
        raise SystemExit(1)


@app.cli.command("create-admin")
def create_admin():
    """Create an admin user: flask --app app create-admin"""
    import click
    email = click.prompt("Admin email").lower().strip()
    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    if User.query.filter_by(email=email).first():
        print("User already exists.")
        return
    u = User(email=email)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    print("Admin created:", email)


@app.cli.command("reset-admin-password")
def reset_admin_password():
    """Set a new password for a user who is locked out:
    flask --app app reset-admin-password"""
    import click
    email = click.prompt("User email").lower().strip()
    u = User.query.filter_by(email=email).first()
    if not u:
        print("No such user.")
        return
    password = click.prompt("New password", hide_input=True,
                            confirmation_prompt=True)
    if len(password) < MIN_PASSWORD_LEN:
        print("Password must be at least %d characters." % MIN_PASSWORD_LEN)
        return
    u.set_password(password)
    db.session.commit()
    print("Password reset for", email)


@app.cli.command("disable-2fa")
def disable_2fa():
    """Clear two-factor authentication for a user who has lost both their
    authenticator and their recovery codes:
    flask --app app disable-2fa"""
    import click
    email = click.prompt("User email").lower().strip()
    u = User.query.filter_by(email=email).first()
    if not u:
        print("No such user.")
        return
    clear_user_2fa(u)
    db.session.commit()
    print("Two-factor authentication cleared for", email)


@app.cli.command("delete-admin")
def delete_admin():
    """Delete a user: flask --app app delete-admin"""
    import click
    email = click.prompt("User email").lower().strip()
    u = User.query.filter_by(email=email).first()
    if not u:
        print("No such user.")
        return
    if is_last_super_admin(u):
        print("Refusing: %s is the only super admin left. Promote someone "
              "else first." % email)
        return
    if not click.confirm("Delete %s permanently?" % email):
        print("Cancelled.")
        return
    RecoveryCode.query.filter_by(user_id=u.id).delete()
    db.session.delete(u)
    db.session.commit()
    print("Deleted", email)


@app.cli.command("promote-super-admin")
def promote_super_admin():
    """Promote an existing admin to super_admin (Netbus only — grants the
    feature-flag settings page): flask --app app promote-super-admin"""
    import click
    email = click.prompt("User email").lower().strip()
    u = User.query.filter_by(email=email).first()
    if not u:
        print("No such user.")
        return
    u.role = "super_admin"
    db.session.commit()
    print("Promoted to super_admin:", email)


if __name__ == "__main__":
    app.run(debug=True)
