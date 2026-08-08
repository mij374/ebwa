"""
EBWA Community Website + CMS
Flask + SQLite. Admin can edit page content/images, manage events and gallery.

First run:
    pip install -r requirements.txt
    flask --app app init-db
    flask --app app create-admin admin@ebwa.org.uk
    flask --app app run --debug
"""
import base64
import hmac
import io
import os
import re
import secrets
import time
import uuid
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo

import pyotp
import qrcode
import qrcode.image.svg
import stripe

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, abort, send_from_directory,
                   session as flask_session)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MAX_UPLOAD_MB = 8

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "instance", "ebwa.db"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC


class GalleryImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    caption = db.Column(db.String(200), default="")
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


class Partner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    url = db.Column(db.String(300), default="")
    blurb = db.Column(db.String(300), default="")
    sort = db.Column(db.Integer, default=0)


class Resource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(80), nullable=False)   # e.g. "Council services"
    description = db.Column(db.String(300), default="")
    phone = db.Column(db.String(40), default="")
    url = db.Column(db.String(300), default="")
    sort = db.Column(db.Integer, default=0)


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


class Campaign(db.Model):
    """An event collection (e.g. seaside trip) donors/attendees pay toward."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(220), unique=True, nullable=False)
    description = db.Column(db.Text, default="")
    image = db.Column(db.String(255), default="")          # uploads filename
    target_pence = db.Column(db.Integer)                   # optional target amount
    fee_pence = db.Column(db.Integer)                      # fixed price per place, if any
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # naive UTC

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


def save_upload(file_storage):
    """Validate and store an uploaded image; returns stored filename or None."""
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXT:
        flash("Image must be one of: " + ", ".join(sorted(ALLOWED_EXT)), "error")
        return None
    name = "%s.%s" % (uuid.uuid4().hex, ext)
    file_storage.save(os.path.join(UPLOAD_DIR, secure_filename(name)))
    return name


def delete_upload(filename):
    if not filename:
        return
    path = os.path.join(UPLOAD_DIR, secure_filename(filename))
    if os.path.isfile(path):
        os.remove(path)


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
    rows = Block.query.filter_by(group=group).order_by(Block.sort).all()
    return {b.key: b.value for b in rows}


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
]

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


def super_admin_required(fn):
    """Netbus-only admin route: anonymous users are sent to the login
    page as usual, logged-in client admins get a flat 403."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_super_admin:
            abort(403)
        return fn(*args, **kwargs)
    return login_required(wrapper)


@app.context_processor
def inject_globals():
    site = blocks_for("site")
    return {"site": site, "current_year": datetime.utcnow().year,
            "features": feature_flags()}


# Security headers on every response. CSP allows exactly what the
# templates use: inline scripts/styles, Google Fonts, and the Google
# Maps embed on the contact page.
CSP = ("default-src 'self'; "
       "script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; "
       "img-src 'self' data:; "
       "frame-src https://www.google.com; "
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
RATE_LIMITS = {          # scope -> (max attempts, window seconds)
    "login": (5, 600),
    "totp": (10, 600),   # a 6-digit code is guessable without this
    "subscribe": (5, 3600),
    "donate": (10, 3600),
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


# ---------------------------------------------------------------- public
@app.route("/")
def home():
    content = blocks_for("home")
    upcoming = (Event.query
                .filter_by(published=True)
                .filter(Event.event_date >= date.today())
                .order_by(Event.event_date.asc())
                .limit(3).all())
    latest_news = []
    if feature_enabled("news"):
        latest_news = (NewsPost.query.filter_by(published=True)
                       .order_by(NewsPost.published_date.desc(),
                                 NewsPost.created_at.desc())
                       .limit(3).all())
    campaigns = []
    if feature_enabled("donations"):
        campaigns = (Campaign.query.filter_by(active=True)
                     .order_by(Campaign.created_at.desc()).all())
    testimonials = (Testimonial.query.filter_by(published=True)
                    .order_by(Testimonial.sort, Testimonial.created_at.desc())
                    .limit(6).all())
    partners = Partner.query.order_by(Partner.sort, Partner.name).all()
    return render_template("index.html", c=content, upcoming=upcoming,
                           latest_news=latest_news, campaigns=campaigns,
                           testimonials=testimonials, partners=partners)


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
        ("membership", "membership_form"), ("contact", None)]
    urls = [url_for(e) for e, f in pages if f is None or flags[f]]
    urls += [url_for("event_detail", slug=ev.slug) for ev in
             Event.query.filter_by(published=True).all()]
    if flags["news"]:
        urls += [url_for("news_detail", slug=p.slug) for p in
                 NewsPost.query.filter_by(published=True).all()]
    if flags["donations"]:
        urls += [url_for("collection_detail", slug=c.slug) for c in
                 Campaign.query.filter_by(active=True).all()]
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


@app.route("/about")
def about():
    return render_template("about.html", c=blocks_for("about"))


@app.route("/events")
def events():
    today = date.today()
    upcoming = (Event.query.filter_by(published=True)
                .filter(Event.event_date >= today)
                .order_by(Event.event_date.asc()).all())
    past = (Event.query.filter_by(published=True)
            .filter(Event.event_date < today)
            .order_by(Event.event_date.desc()).limit(12).all())
    return render_template("events.html", upcoming=upcoming, past=past)


@app.route("/events/<slug>")
def event_detail(slug):
    ev = Event.query.filter_by(slug=slug, published=True).first_or_404()
    return render_template("event_detail.html", ev=ev)


@app.route("/news")
@feature_required("news")
def news():
    posts = (NewsPost.query.filter_by(published=True)
             .order_by(NewsPost.published_date.desc(),
                       NewsPost.created_at.desc()).all())
    return render_template("news.html", posts=posts)


@app.route("/news/<slug>")
@feature_required("news")
def news_detail(slug):
    post = NewsPost.query.filter_by(slug=slug, published=True).first_or_404()
    return render_template("news_detail.html", post=post)


@app.route("/gallery")
def gallery():
    images = GalleryImage.query.order_by(GalleryImage.sort,
                                         GalleryImage.created_at.desc()).all()
    return render_template("gallery.html", images=images)


@app.route("/resources")
@feature_required("resources")
def resources():
    rows = Resource.query.order_by(Resource.category, Resource.sort,
                                   Resource.name).all()
    grouped = []   # [(category, [resources])], in query order
    for r in rows:
        if grouped and grouped[-1][0] == r.category:
            grouped[-1][1].append(r)
        else:
            grouped.append((r.category, [r]))
    return render_template("resources.html", grouped=grouped)


@app.route("/our-journey")
@feature_required("our_journey")
def journey():
    rows = (Milestone.query.filter_by(published=True)
            .order_by(Milestone.year.desc(), Milestone.sort,
                      Milestone.title).all())
    grouped = []   # [(year, [milestones])], in query order
    for m in rows:
        if grouped and grouped[-1][0] == m.year:
            grouped[-1][1].append(m)
        else:
            grouped.append((m.year, [m]))
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


@app.route("/collections/<slug>", methods=["GET", "POST"])
@feature_required("donations")
def collection_detail(slug):
    camp = Campaign.query.filter_by(slug=slug, active=True).first_or_404()
    if request.method == "POST":
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


@app.route("/contact")
def contact():
    return render_template("contact.html", c=blocks_for("contact"))


# ---------------------------------------------------------------- admin: auth
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        if rate_limited("login"):
            flash("Too many login attempts — please wait ten minutes and "
                  "try again.", "error")
            return render_template("admin/login.html")
        user = User.query.filter_by(email=request.form.get("email", "").lower().strip()).first()
        if user and user.check_password(request.form.get("password", "")):
            if user.totp_enabled:
                # Correct password is not enough: hand off to the code
                # step without creating the session.
                clear_pending_2fa()
                flask_session[PENDING_2FA_USER] = user.id
                flask_session[PENDING_2FA_AT] = int(time.time())
                return redirect(url_for("admin_login_2fa"))
            login_user(user)
            return redirect(url_for("admin_dashboard"))
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
            login_user(user)
            return redirect(url_for("admin_dashboard"))
        if use_recovery_code(user, code):
            clear_pending_2fa()
            login_user(user)
            left = unused_recovery_codes(user)
            flash("Recovery code accepted — that one is now used up and "
                  "you have %d left. If you have lost your authenticator, "
                  "turn two-factor authentication off and set it up again "
                  "on your new phone." % left, "ok")
            return redirect(url_for("admin_account"))
        flash("That code was not right. Codes change every 30 seconds — "
              "check your authenticator app and try the current one.",
              "error")
    return render_template("admin/login_2fa.html", email=user.email)


@app.route("/admin/logout")
@login_required
def admin_logout():
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
        flash("Two-factor authentication is now off, and your old recovery "
              "codes no longer work.", "ok")
    else:
        flash("That code was not right — two-factor authentication is "
              "still on.", "error")
    return redirect(url_for("admin_account"))


# ---------------------------------------------------------------- admin: dashboard
@app.route("/admin")
@login_required
def admin_dashboard():
    counts = {
        "events": Event.query.count(),
        "upcoming": Event.query.filter(Event.event_date >= date.today(),
                                       Event.published == True).count(),  # noqa: E712
        "gallery": GalleryImage.query.count(),
    }
    return render_template("admin/dashboard.html", counts=counts)


# ---------------------------------------------------------------- admin: content blocks
@app.route("/admin/content", methods=["GET", "POST"])
@login_required
def admin_content():
    group = request.args.get("group", "home")
    groups = [g[0] for g in db.session.query(Block.group).distinct().order_by(Block.group)]
    blocks = Block.query.filter_by(group=group).order_by(Block.sort).all()

    if request.method == "POST":
        for b in blocks:
            if b.kind == "text":
                b.value = request.form.get("block_%d" % b.id, b.value)
            else:  # image
                f = request.files.get("block_%d" % b.id)
                if f and f.filename:
                    new_name = save_upload(f)
                    if new_name:
                        delete_upload(b.value)
                        b.value = new_name
        db.session.commit()
        flash("Content saved.", "ok")
        return redirect(url_for("admin_content", group=group))

    return render_template("admin/content.html", blocks=blocks,
                           group=group, groups=groups)


# ---------------------------------------------------------------- admin: events
@app.route("/admin/events")
@login_required
def admin_events():
    rows = Event.query.order_by(Event.event_date.desc()).all()
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
        if not title or not date_str:
            flash("Title and date are required.", "error")
        else:
            is_new = ev is None
            if is_new:
                ev = Event()
            ev.title = title
            ev.slug = unique_slug(Event, title, ev.id)
            ev.event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            ev.start_time = request.form.get("start_time", "").strip()
            ev.venue = request.form.get("venue", "").strip()
            ev.summary = request.form.get("summary", "").strip()
            ev.description = request.form.get("description", "").strip()
            ev.published = request.form.get("published") == "on"
            f = request.files.get("image")
            if f and f.filename:
                new_name = save_upload(f)
                if new_name:
                    delete_upload(ev.image)
                    ev.image = new_name
            if is_new:
                db.session.add(ev)
            db.session.commit()
            flash("Event saved.", "ok")
            return redirect(url_for("admin_events"))

    return render_template("admin/event_form.html", ev=ev)


@app.route("/admin/events/<int:event_id>/delete", methods=["POST"])
@login_required
def admin_event_delete(event_id):
    ev = db.session.get(Event, event_id) or abort(404)
    delete_upload(ev.image)
    db.session.delete(ev)
    db.session.commit()
    flash("Event deleted.", "ok")
    return redirect(url_for("admin_events"))


# ---------------------------------------------------------------- admin: news
@app.route("/admin/news")
@login_required
def admin_news():
    rows = NewsPost.query.order_by(NewsPost.published_date.desc(),
                                   NewsPost.created_at.desc()).all()
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
        if not title or not date_str:
            flash("Title and date are required.", "error")
        else:
            is_new = post is None
            if is_new:
                post = NewsPost()
            post.title = title
            post.slug = unique_slug(NewsPost, title, post.id)
            post.published_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            post.summary = request.form.get("summary", "").strip()
            post.body = request.form.get("body", "").strip()
            post.published = request.form.get("published") == "on"
            f = request.files.get("image")
            if f and f.filename:
                new_name = save_upload(f)
                if new_name:
                    delete_upload(post.image)
                    post.image = new_name
            if is_new:
                db.session.add(post)
            db.session.commit()
            flash("News post saved.", "ok")
            return redirect(url_for("admin_news"))

    return render_template("admin/news_form.html", post=post)


@app.route("/admin/news/<int:post_id>/delete", methods=["POST"])
@login_required
def admin_news_delete(post_id):
    post = db.session.get(NewsPost, post_id) or abort(404)
    delete_upload(post.image)
    db.session.delete(post)
    db.session.commit()
    flash("News post deleted.", "ok")
    return redirect(url_for("admin_news"))


# ---------------------------------------------------------------- admin: gallery
@app.route("/admin/gallery", methods=["GET", "POST"])
@login_required
def admin_gallery():
    if request.method == "POST":
        added = 0
        for f in request.files.getlist("images"):
            name = save_upload(f)
            if name:
                db.session.add(GalleryImage(
                    filename=name,
                    caption=request.form.get("caption", "").strip()))
                added += 1
        db.session.commit()
        if added:
            flash("%d image(s) uploaded." % added, "ok")
        return redirect(url_for("admin_gallery"))
    images = GalleryImage.query.order_by(GalleryImage.sort,
                                         GalleryImage.created_at.desc()).all()
    return render_template("admin/gallery.html", images=images)


@app.route("/admin/gallery/<int:image_id>/delete", methods=["POST"])
@login_required
def admin_gallery_delete(image_id):
    img = db.session.get(GalleryImage, image_id) or abort(404)
    delete_upload(img.filename)
    db.session.delete(img)
    db.session.commit()
    flash("Image deleted.", "ok")
    return redirect(url_for("admin_gallery"))


# ---------------------------------------------------------------- admin: testimonials
@app.route("/admin/testimonials", methods=["GET", "POST"])
@login_required
def admin_testimonials():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        quote = request.form.get("quote", "").strip()
        if name and quote:
            db.session.add(Testimonial(
                name=name, quote=quote,
                role=request.form.get("role", "").strip(),
                published=request.form.get("published") == "on"))
            db.session.commit()
            flash("Testimonial added.", "ok")
        else:
            flash("Name and quote are required.", "error")
        return redirect(url_for("admin_testimonials"))
    rows = Testimonial.query.order_by(Testimonial.sort,
                                      Testimonial.created_at.desc()).all()
    return render_template("admin/testimonials.html", rows=rows)


@app.route("/admin/testimonials/<int:t_id>/delete", methods=["POST"])
@login_required
def admin_testimonial_delete(t_id):
    t = db.session.get(Testimonial, t_id) or abort(404)
    db.session.delete(t)
    db.session.commit()
    flash("Testimonial deleted.", "ok")
    return redirect(url_for("admin_testimonials"))


@app.route("/admin/testimonials/<int:t_id>/toggle", methods=["POST"])
@login_required
def admin_testimonial_toggle(t_id):
    t = db.session.get(Testimonial, t_id) or abort(404)
    t.published = not t.published
    db.session.commit()
    return redirect(url_for("admin_testimonials"))


# ---------------------------------------------------------------- admin: partners
@app.route("/admin/partners", methods=["GET", "POST"])
@login_required
def admin_partners():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            db.session.add(Partner(
                name=name,
                url=request.form.get("url", "").strip(),
                blurb=request.form.get("blurb", "").strip()))
            db.session.commit()
            flash("Partner added.", "ok")
        else:
            flash("Partner name is required.", "error")
        return redirect(url_for("admin_partners"))
    rows = Partner.query.order_by(Partner.sort, Partner.name).all()
    return render_template("admin/partners.html", rows=rows)


@app.route("/admin/partners/<int:p_id>/delete", methods=["POST"])
@login_required
def admin_partner_delete(p_id):
    pt = db.session.get(Partner, p_id) or abort(404)
    db.session.delete(pt)
    db.session.commit()
    flash("Partner deleted.", "ok")
    return redirect(url_for("admin_partners"))


# ---------------------------------------------------------------- admin: resources
@app.route("/admin/resources")
@login_required
def admin_resources():
    rows = Resource.query.order_by(Resource.category, Resource.sort,
                                   Resource.name).all()
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
            res.name = name
            res.category = category
            res.description = request.form.get("description", "").strip()
            res.phone = request.form.get("phone", "").strip()
            res.url = request.form.get("url", "").strip()
            try:
                res.sort = int(request.form.get("sort", "0"))
            except ValueError:
                res.sort = 0
            if is_new:
                db.session.add(res)
            db.session.commit()
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
    db.session.delete(res)
    db.session.commit()
    flash("Resource deleted.", "ok")
    return redirect(url_for("admin_resources"))


# ---------------------------------------------------------------- admin: milestones
@app.route("/admin/journey")
@login_required
def admin_milestones():
    rows = Milestone.query.order_by(Milestone.year.desc(), Milestone.sort,
                                    Milestone.title).all()
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
        if not title:
            flash("Title is required.", "error")
        elif year is None or year < 1900 or year > 2100:
            flash("Please enter a valid four-digit year.", "error")
        elif amount_raw and (amount_pence is None or amount_pence <= 0):
            flash("Amount must be a valid amount in pounds, or left blank.",
                  "error")
        else:
            is_new = m is None
            if is_new:
                m = Milestone()
            m.title = title
            m.year = year
            m.summary = request.form.get("summary", "").strip()
            m.outcome = request.form.get("outcome", "").strip()
            m.funder_name = request.form.get("funder_name", "").strip()
            m.amount_pence = amount_pence
            m.funder_url = request.form.get("funder_url", "").strip()
            try:
                m.sort = int(request.form.get("sort", "0"))
            except ValueError:
                m.sort = 0
            m.published = request.form.get("published") == "on"
            f = request.files.get("image")
            if f and f.filename:
                new_name = save_upload(f)
                if new_name:
                    delete_upload(m.image)
                    m.image = new_name
            if is_new:
                db.session.add(m)
            db.session.commit()
            flash("Milestone saved.", "ok")
            return redirect(url_for("admin_milestones"))

    return render_template("admin/milestone_form.html", m=m)


@app.route("/admin/journey/<int:milestone_id>/delete", methods=["POST"])
@login_required
def admin_milestone_delete(milestone_id):
    m = db.session.get(Milestone, milestone_id) or abort(404)
    delete_upload(m.image)
    db.session.delete(m)
    db.session.commit()
    flash("Milestone deleted.", "ok")
    return redirect(url_for("admin_milestones"))


# ---------------------------------------------------------------- admin: subscribers
@app.route("/admin/subscribers")
@login_required
def admin_subscribers():
    rows = Subscriber.query.order_by(Subscriber.created_at.desc()).all()
    return render_template("admin/subscribers.html", rows=rows)


@app.route("/admin/subscribers.csv")
@login_required
def admin_subscribers_csv():
    lines = ["email,subscribed_on"]
    for s in Subscriber.query.order_by(Subscriber.created_at).all():
        lines.append("%s,%s" % (s.email,
                                utc_as_uk(s.created_at).strftime("%Y-%m-%d")))
    resp = app.response_class("\n".join(lines), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=ebwa-subscribers.csv"
    return resp


@app.route("/admin/subscribers/<int:s_id>/delete", methods=["POST"])
@login_required
def admin_subscriber_delete(s_id):
    s = db.session.get(Subscriber, s_id) or abort(404)
    db.session.delete(s)
    db.session.commit()
    flash("Subscriber removed.", "ok")
    return redirect(url_for("admin_subscribers"))


# ---------------------------------------------------------------- admin: collections
@app.route("/admin/campaigns")
@login_required
def admin_campaigns():
    rows = Campaign.query.order_by(Campaign.created_at.desc()).all()
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
        if not title:
            flash("Title is required.", "error")
        elif target_raw and (target_pence is None or target_pence <= 0):
            flash("Target must be a valid amount in pounds.", "error")
        elif fee_raw and (fee_pence is None or fee_pence <= 0):
            flash("Place fee must be a valid amount in pounds.", "error")
        else:
            is_new = camp is None
            if is_new:
                camp = Campaign()
            camp.title = title
            camp.slug = unique_slug(Campaign, title, camp.id)
            camp.description = request.form.get("description", "").strip()
            camp.target_pence = target_pence
            camp.fee_pence = fee_pence
            camp.active = request.form.get("active") == "on"
            f = request.files.get("image")
            if f and f.filename:
                new_name = save_upload(f)
                if new_name:
                    delete_upload(camp.image)
                    camp.image = new_name
            if is_new:
                db.session.add(camp)
            db.session.commit()
            flash("Collection saved.", "ok")
            return redirect(url_for("admin_campaigns"))

    return render_template("admin/campaign_form.html", camp=camp)


@app.route("/admin/campaigns/<int:campaign_id>/delete", methods=["POST"])
@login_required
def admin_campaign_delete(campaign_id):
    camp = db.session.get(Campaign, campaign_id) or abort(404)
    # Payments are financial records — never orphan or delete them.
    if Payment.query.filter_by(campaign_id=camp.id).count():
        flash("This collection has payments recorded against it, so it "
              "can't be deleted. Untick 'active' to take it off the "
              "website instead.", "error")
        return redirect(url_for("admin_campaigns"))
    delete_upload(camp.image)
    db.session.delete(camp)
    db.session.commit()
    flash("Collection deleted.", "ok")
    return redirect(url_for("admin_campaigns"))


@app.route("/admin/campaigns/<int:campaign_id>/contributors")
@login_required
def admin_campaign_contributors(campaign_id):
    camp = db.session.get(Campaign, campaign_id) or abort(404)
    rows = (Payment.query.filter_by(campaign_id=camp.id)
            .order_by(Payment.created_at.desc()).all())
    return render_template("admin/contributors.html", camp=camp, rows=rows)


@app.route("/admin/campaigns/<int:campaign_id>/contributors.csv")
@login_required
def admin_campaign_contributors_csv(campaign_id):
    import csv
    import io
    camp = db.session.get(Campaign, campaign_id) or abort(404)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "name", "email", "fee_gbp", "donation_gbp",
                "total_gbp", "gift_aid", "status"])
    for p in (Payment.query.filter_by(campaign_id=camp.id)
              .order_by(Payment.created_at).all()):
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


@app.template_filter("uk_date")
def uk_date_filter(dt):
    """Display a naive-UTC timestamp as its UK local calendar date."""
    return utc_as_uk(dt).strftime("%d %b %Y") if dt else ""


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
    return q.order_by(Payment.created_at)


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
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Title", "First name", "Last name",
                "House name or number", "Postcode",
                "Aggregated donations", "Donation date", "Amount"])
    for p in gift_aid_claimable_query(date_from, date_to).all():
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
            .order_by(Payment.created_at.desc()).all())
    return render_template("admin/gift_aid_declarations.html", rows=rows)


# ---------------------------------------------------------------- admin: membership
@app.route("/admin/membership")
@login_required
def admin_membership():
    rows = (MembershipApplication.query
            .order_by(MembershipApplication.created_at.desc()).all())
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
    for m in (MembershipApplication.query
              .order_by(MembershipApplication.created_at).all()):
        w.writerow([m.name, m.email, m.phone, m.address, m.reason,
                    m.status,
                    utc_as_uk(m.created_at).strftime("%Y-%m-%d")])
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
        m.status = status
        db.session.commit()
        flash("Status updated.", "ok")
    else:
        flash("Unknown status.", "error")
    return redirect(url_for("admin_membership"))


@app.route("/admin/membership/<int:m_id>/delete", methods=["POST"])
@login_required
def admin_membership_delete(m_id):
    m = db.session.get(MembershipApplication, m_id) or abort(404)
    db.session.delete(m)
    db.session.commit()
    flash("Application removed.", "ok")
    return redirect(url_for("admin_membership"))


# ---------------------------------------------------------------- admin: settings
# Super admin (Netbus) only — client admins never see the nav link and
# get a 403 if they find the URL.
@app.route("/admin/features")
@super_admin_required
def admin_features():
    return render_template("admin/features.html", rows=FEATURES,
                           flags=feature_flags())


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
    flash("%s is now %s. Nothing was deleted — switching it back on "
          "restores the pages as they were."
          % (FEATURE_LABELS[name], "on" if flag.enabled else "off"), "ok")
    return redirect(url_for("admin_features"))


# ---------------------------------------------------------------- CLI
DEFAULT_BLOCKS = [
    # group, key, label, kind, default value
    ("site", "site_phone", "Phone number", "text", "020 8804 4006"),
    ("site", "site_address", "Address", "text", "180 High Street, Ponders End, Enfield EN3 4EU"),
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
    ("about", "about_title", "Page title", "text", "About EBWA"),
    ("about", "about_body", "Main text", "text",
     "Founded by Choudhury Mohammed Anwar MBE, former Mayor of Enfield, EBWA provides "
     "education, welfare and social support to the whole community."),
    ("about", "about_image", "Founder / about photo", "image", ""),
    ("journey", "journey_intro", "Intro text", "text",
     "From our earliest gatherings to the projects we run today, this is "
     "the story of EBWA's work in Enfield — the milestones we have reached "
     "and the funders who helped us get there."),
    ("contact", "contact_intro", "Intro text", "text",
     "Our centre welcomes visitors for support every week. Drop in, call, or find us on the High Street."),
    ("contact", "contact_hours", "Opening / drop-in times", "text",
     "Weekly sessions — call for current times"),
]


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
    db.session.commit()
    print("Database initialised.")


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
