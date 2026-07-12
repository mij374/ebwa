"""
EBWA Community Website + CMS
Flask + SQLite. Admin can edit page content/images, manage events and gallery.

First run:
    pip install -r requirements.txt
    flask --app app init-db
    flask --app app create-admin admin@ebwa.org.uk
    flask --app app run --debug
"""
import os
import re
import uuid
from datetime import datetime, date

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, abort, send_from_directory)
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
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


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


class Subscriber(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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


def blocks_for(group):
    rows = Block.query.filter_by(group=group).order_by(Block.sort).all()
    return {b.key: b.value for b in rows}


@app.context_processor
def inject_globals():
    site = blocks_for("site")
    return {"site": site, "current_year": datetime.utcnow().year}


# ---------------------------------------------------------------- public
@app.route("/")
def home():
    content = blocks_for("home")
    upcoming = (Event.query
                .filter_by(published=True)
                .filter(Event.event_date >= date.today())
                .order_by(Event.event_date.asc())
                .limit(3).all())
    latest_news = (NewsPost.query.filter_by(published=True)
                   .order_by(NewsPost.published_date.desc(),
                             NewsPost.created_at.desc())
                   .limit(3).all())
    testimonials = (Testimonial.query.filter_by(published=True)
                    .order_by(Testimonial.sort, Testimonial.created_at.desc())
                    .limit(6).all())
    partners = Partner.query.order_by(Partner.sort, Partner.name).all()
    return render_template("index.html", c=content, upcoming=upcoming,
                           latest_news=latest_news,
                           testimonials=testimonials, partners=partners)


@app.route("/subscribe", methods=["POST"])
def subscribe():
    email = request.form.get("email", "").lower().strip()
    if not email or "@" not in email or len(email) > 200:
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
    urls = [url_for(e) for e in
            ("home", "about", "events", "news", "gallery", "contact")]
    urls += [url_for("event_detail", slug=ev.slug) for ev in
             Event.query.filter_by(published=True).all()]
    urls += [url_for("news_detail", slug=p.slug) for p in
             NewsPost.query.filter_by(published=True).all()]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        xml.append("<url><loc>%s%s</loc></url>" % (base, u))
    xml.append("</urlset>")
    return app.response_class("\n".join(xml), mimetype="application/xml")


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
def news():
    posts = (NewsPost.query.filter_by(published=True)
             .order_by(NewsPost.published_date.desc(),
                       NewsPost.created_at.desc()).all())
    return render_template("news.html", posts=posts)


@app.route("/news/<slug>")
def news_detail(slug):
    post = NewsPost.query.filter_by(slug=slug, published=True).first_or_404()
    return render_template("news_detail.html", post=post)


@app.route("/gallery")
def gallery():
    images = GalleryImage.query.order_by(GalleryImage.sort,
                                         GalleryImage.created_at.desc()).all()
    return render_template("gallery.html", images=images)


@app.route("/contact")
def contact():
    return render_template("contact.html", c=blocks_for("contact"))


# ---------------------------------------------------------------- admin: auth
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        user = User.query.filter_by(email=request.form.get("email", "").lower().strip()).first()
        if user and user.check_password(request.form.get("password", "")):
            login_user(user)
            return redirect(url_for("admin_dashboard"))
        flash("Incorrect email or password.", "error")
    return render_template("admin/login.html")


@app.route("/admin/logout")
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for("home"))


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
        lines.append("%s,%s" % (s.email, s.created_at.strftime("%Y-%m-%d")))
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
    ("contact", "contact_intro", "Intro text", "text",
     "Our centre welcomes visitors for support every week. Drop in, call, or find us on the High Street."),
    ("contact", "contact_hours", "Opening / drop-in times", "text",
     "Weekly sessions — call for current times"),
]


@app.cli.command("init-db")
def init_db():
    """Create tables and seed default content blocks."""
    db.create_all()
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
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


if __name__ == "__main__":
    app.run(debug=True)
