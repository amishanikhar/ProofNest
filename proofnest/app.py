"""ProofNest - rental inspection & evidence platform.
Local mode : SQLite + ./data folder (no AWS needed).
AWS mode   : set DATABASE_URL (RDS MySQL) and S3_BUCKET (versioned, private bucket).
"""
import io, os, shutil, uuid, datetime as dt
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, abort, send_file, g)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageOps

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me"),
    SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///proofnest.db"),
    MAX_CONTENT_LENGTH=20 * 1024 * 1024)
db = SQLAlchemy(app)
S3_BUCKET = os.environ.get("S3_BUCKET")
DATA_DIR = os.environ.get("DATA_DIR", "data")  # on EC2: the mounted EBS volume
CONDITIONS = ["Good", "Minor wear", "Damaged", "Missing"]
CHANGES = ["No change", "New damage", "Damage worsened", "Repaired / improved"]


def now():
    return dt.datetime.utcnow()


@app.template_filter("ist")
def ist(d):
    return (d + dt.timedelta(hours=5, minutes=30)).strftime("%d %b %Y, %H:%M") if d else ""


# ---------------------------------------------------------------- storage
class Storage:
    """S3 (with versioning) in AWS mode, local folder with .versions in dev mode."""
    def __init__(self):
        self.s3 = None
        if S3_BUCKET:
            import boto3  # credentials come from the EC2 IAM role
            from botocore.config import Config
            # Region + SigV4 are set explicitly so presigned URLs (photos, PDF)
            # point at the right regional endpoint and open in the browser.
            self.s3 = boto3.client(
                "s3",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
                config=Config(signature_version="s3v4"))

    def _path(self, key):
        if ".." in key or key.startswith("/"):
            abort(400)
        return os.path.join(DATA_DIR, "files", key)

    def put(self, key, data, ctype):
        if self.s3:
            self.s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=ctype,
                               ServerSideEncryption="AES256")
            return
        p = self._path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if os.path.exists(p):  # keep the old copy = local "versioning"
            vd = p + ".versions"
            os.makedirs(vd, exist_ok=True)
            shutil.copy2(p, os.path.join(vd, now().strftime("%Y%m%d%H%M%S%f")))
        with open(p, "wb") as f:
            f.write(data)

    def get(self, key):
        if self.s3:
            return self.s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
        with open(self._path(key), "rb") as f:
            return f.read()

    def url(self, key):
        if self.s3:
            return self.s3.generate_presigned_url(
                "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=300)

    def local_path(self, key):
        return os.path.abspath(self._path(key))

    def versions(self, key):
        out = []
        if self.s3:
            r = self.s3.list_object_versions(Bucket=S3_BUCKET, Prefix=key)
            for v in r.get("Versions", []):
                if v["Key"] == key:
                    out.append(dict(id=v["VersionId"], time=v["LastModified"].replace(tzinfo=None),
                                    size=v["Size"], current=v["IsLatest"]))
            return out
        p = self._path(key)
        if os.path.exists(p):
            out.append(dict(id="current", time=dt.datetime.utcfromtimestamp(os.path.getmtime(p)),
                            size=os.path.getsize(p), current=True))
        vd = p + ".versions"
        if os.path.isdir(vd):
            for n in sorted(os.listdir(vd), reverse=True):
                out.append(dict(id=n, time=dt.datetime.strptime(n, "%Y%m%d%H%M%S%f"),
                                size=os.path.getsize(os.path.join(vd, n)), current=False))
        return out

    def restore(self, key, vid):
        if self.s3:  # copying an old version on top makes it the newest version
            self.s3.copy_object(Bucket=S3_BUCKET, Key=key,
                                CopySource={"Bucket": S3_BUCKET, "Key": key, "VersionId": vid})
            return
        vp = os.path.join(self._path(key) + ".versions", os.path.basename(vid))
        if not os.path.exists(vp):
            abort(404)
        with open(vp, "rb") as f:
            self.put(key, f.read(), "application/octet-stream")


store = Storage()


# ----------------------------------------------------------------- models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # tenant | landlord


class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    property_name = db.Column(db.String(150), nullable=False)
    address = db.Column(db.String(255), default="")
    room_number = db.Column(db.String(30), nullable=False)
    tenant_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    landlord_email = db.Column(db.String(150))
    tenant = db.relationship("User")
    inspections = db.relationship("Inspection", backref="room", order_by="Inspection.id")


class Inspection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    kind = db.Column(db.String(10), nullable=False)  # move_in | move_out
    inspection_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(10), default="draft")  # draft | saved
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=now)
    items = db.relationship("Item", backref="inspection", order_by="Item.id")


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    inspection_id = db.Column(db.Integer, db.ForeignKey("inspection.id"), nullable=False)
    area = db.Column(db.String(100), nullable=False)
    condition = db.Column(db.String(20), nullable=False)
    description = db.Column(db.String(500), default="")
    photo_key = db.Column(db.String(255))
    uploaded_at = db.Column(db.DateTime, default=now)
    change = db.Column(db.String(30), default="")      # set on move-out items
    change_note = db.Column(db.String(500), default="")


class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    code = db.Column(db.String(40), nullable=False)
    s3_object_key = db.Column(db.String(255), nullable=False)
    generated_at = db.Column(db.DateTime, default=now)


class Audit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    room_id = db.Column(db.Integer)
    action = db.Column(db.String(60))
    detail = db.Column(db.String(300), default="")
    event_time = db.Column(db.DateTime, default=now)
    user = db.relationship("User")


with app.app_context():
    db.create_all()


def audit(action, detail="", room_id=None):
    db.session.add(Audit(user_id=g.user.id if g.user else None, room_id=room_id,
                         action=action, detail=detail))


# ------------------------------------------------------------------- auth
@app.before_request
def load_user():
    g.user = db.session.get(User, session["uid"]) if "uid" in session else None


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            return redirect(url_for("login"))
        return f(*a, **k)
    return w


def get_room(rid, owner=False):
    r = db.get_or_404(Room, rid)
    mine = r.tenant_id == g.user.id
    shared = bool(r.landlord_email) and r.landlord_email.lower() == g.user.email.lower()
    if not (mine or shared) or (owner and not mine):
        abort(403)
    return r


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        f = request.form
        email = f["email"].strip().lower()
        if f["role"] not in ("tenant", "landlord") or len(f["password"]) < 6:
            flash("Choose a role and a password of at least 6 characters.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("That email is already registered. Log in instead.", "danger")
        else:
            u = User(name=f["name"].strip(), email=email, role=f["role"],
                     password_hash=generate_password_hash(f["password"]))
            db.session.add(u)
            db.session.commit()
            session["uid"] = u.id
            return redirect(url_for("index"))
    return render_template("auth.html", mode="signup")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(email=request.form["email"].strip().lower()).first()
        if u and check_password_hash(u.password_hash, request.form["password"]):
            session["uid"] = u.id
            return redirect(url_for("index"))
        flash("Email or password is incorrect.", "danger")
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ rooms
@app.route("/")
def index():
    if not g.user:
        return render_template("landing.html")
    rooms = Room.query.filter((Room.tenant_id == g.user.id) |
                              (db.func.lower(Room.landlord_email) == g.user.email)).all()
    return render_template("index.html", rooms=rooms)


@app.post("/rooms")
@login_required
def new_room():
    if g.user.role != "tenant":
        abort(403)
    f = request.form
    r = Room(property_name=f["property_name"].strip(), address=f.get("address", "").strip(),
             room_number=f["room_number"].strip(), tenant_id=g.user.id,
             landlord_email=f.get("landlord_email", "").strip().lower() or None)
    db.session.add(r)
    db.session.flush()
    audit("room_created", f"{r.property_name} / {r.room_number}", r.id)
    db.session.commit()
    return redirect(url_for("room", rid=r.id))


@app.route("/room/<int:rid>")
@login_required
def room(rid):
    r = get_room(rid)
    rep = Report.query.filter_by(room_id=rid).order_by(Report.id.desc()).first()
    return render_template("room.html", room=r, owner=r.tenant_id == g.user.id,
                           report=rep, today=dt.date.today())


@app.post("/room/<int:rid>/share")
@login_required
def share(rid):
    r = get_room(rid, owner=True)
    r.landlord_email = request.form["landlord_email"].strip().lower() or None
    audit("shared_with_landlord", r.landlord_email or "access removed", rid)
    db.session.commit()
    flash("Landlord access updated.", "success")
    return redirect(url_for("room", rid=rid))


# ------------------------------------------------------------ inspections
@app.post("/room/<int:rid>/inspections")
@login_required
def new_inspection(rid):
    r = get_room(rid, owner=True)
    kind = request.form["kind"]
    if kind not in ("move_in", "move_out"):
        abort(400)
    i = Inspection(room_id=rid, kind=kind, created_by=g.user.id,
                   inspection_date=dt.date.fromisoformat(request.form["date"]))
    db.session.add(i)
    db.session.flush()
    audit("inspection_started", kind.replace("_", "-"), rid)
    db.session.commit()
    return redirect(url_for("inspection", iid=i.id))


@app.route("/inspection/<int:iid>")
@login_required
def inspection(iid):
    i = db.get_or_404(Inspection, iid)
    r = get_room(i.room_id)
    return render_template("inspection.html", i=i, room=r, conditions=CONDITIONS,
                           editable=r.tenant_id == g.user.id and i.status == "draft")


def process_image(fs):
    try:
        im = ImageOps.exif_transpose(Image.open(fs.stream)).convert("RGB")
    except Exception:
        return None
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


@app.post("/inspection/<int:iid>/items")
@login_required
def add_item(iid):
    i = db.get_or_404(Inspection, iid)
    r = get_room(i.room_id, owner=True)
    if i.status != "draft":
        abort(400)
    f = request.form
    data = process_image(request.files["photo"]) if request.files.get("photo") else None
    if not data or not f.get("area", "").strip() or f.get("condition") not in CONDITIONS:
        flash("Add an area, a condition and a valid photo.", "danger")
        return redirect(url_for("inspection", iid=iid))
    key = f"rooms/{r.id}/photos/{uuid.uuid4().hex}.jpg"
    store.put(key, data, "image/jpeg")
    db.session.add(Item(inspection_id=iid, area=f["area"].strip(), condition=f["condition"],
                        description=f.get("description", "").strip(), photo_key=key))
    audit("photo_uploaded", f["area"].strip(), r.id)
    db.session.commit()
    return redirect(url_for("inspection", iid=iid))


@app.post("/item/<int:item_id>/replace")
@login_required
def replace_photo(item_id):
    it = db.get_or_404(Item, item_id)
    r = get_room(it.inspection.room_id, owner=True)
    if it.inspection.status != "draft":
        abort(400)
    data = process_image(request.files["photo"]) if request.files.get("photo") else None
    if not data:
        flash("That file is not a valid image.", "danger")
    else:
        store.put(it.photo_key, data, "image/jpeg")  # same key -> new version
        it.uploaded_at = now()
        audit("photo_replaced", it.area, r.id)
        db.session.commit()
    return redirect(url_for("inspection", iid=it.inspection_id))


@app.post("/inspection/<int:iid>/finish")
@login_required
def finish(iid):
    i = db.get_or_404(Inspection, iid)
    r = get_room(i.room_id, owner=True)
    if not i.items:
        flash("Add at least one area before saving.", "danger")
        return redirect(url_for("inspection", iid=iid))
    i.status = "saved"
    audit("inspection_saved", i.kind.replace("_", "-"), r.id)
    db.session.commit()
    return redirect(url_for("room", rid=r.id))


@app.route("/file/<path:key>")
@login_required
def file(key):
    p = key.split("/")
    if len(p) < 3 or p[0] != "rooms" or not p[1].isdigit():
        abort(404)
    get_room(int(p[1]))
    u = store.url(key)
    if u:
        return redirect(u)
    path = store.local_path(key)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="application/pdf" if key.endswith(".pdf") else "image/jpeg")


# ------------------------------------------------------------- comparison
def latest(room_id, kind):
    return (Inspection.query.filter_by(room_id=room_id, kind=kind)
            .order_by(Inspection.id.desc()).first())


def pair_items(a, b):
    ia = {x.area.strip().lower(): x for x in (a.items if a else [])}
    ib = {x.area.strip().lower(): x for x in (b.items if b else [])}
    keys = list(ia) + [k for k in ib if k not in ia]
    return [(ia.get(k), ib.get(k)) for k in keys]


@app.route("/room/<int:rid>/compare")
@login_required
def compare(rid):
    r = get_room(rid)
    mi, mo = latest(rid, "move_in"), latest(rid, "move_out")
    return render_template("compare.html", room=r, mi=mi, mo=mo, pairs=pair_items(mi, mo),
                           owner=r.tenant_id == g.user.id, changes=CHANGES)  # owner=tenant


@app.post("/item/<int:item_id>/change")
@login_required
def mark_change(item_id):
    it = db.get_or_404(Item, item_id)
    r = get_room(it.inspection.room_id)
    if r.tenant_id == g.user.id or g.user.role != "landlord":
        abort(403)  # only the landlord reviews and marks changes
    if request.form["change"] not in CHANGES or it.inspection.kind != "move_out":
        abort(400)
    it.change = request.form["change"]
    it.change_note = request.form.get("note", "").strip()
    audit("change_marked", f"{it.area}: {it.change}", r.id)
    db.session.commit()
    return redirect(url_for("compare", rid=r.id) + f"#item{it.id}")


# ----------------------------------------------------------------- report
def build_pdf(room, mi, mo, code):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, Image as RLImage)
    ss = getSampleStyleSheet()
    small = ss["BodyText"].clone("small", fontSize=8.5, leading=11)

    def cell(it):
        if not it:
            return [Paragraph("<i>Area not recorded in this inspection.</i>", small)]
        data = store.get(it.photo_key)
        w0, h0 = Image.open(io.BytesIO(data)).size
        w = 7.6 * cm
        h = w * h0 / w0
        if h > 6 * cm:
            h, w = 6 * cm, 6 * cm * w0 / h0
        out = [RLImage(io.BytesIO(data), width=w, height=h),
               Paragraph(f"<b>Condition:</b> {it.condition}", small)]
        if it.description:
            out.append(Paragraph(it.description, small))
        return out

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.5 * cm, bottomMargin=1.5 * cm, title=f"ProofNest {code}")
    s = [Paragraph("ProofNest - Room Inspection Evidence Report", ss["Title"]),
         Paragraph(f"<b>Report ID:</b> {code} &nbsp; <b>Generated:</b> {ist(now())} IST", ss["BodyText"]),
         Paragraph(f"<b>Property:</b> {room.property_name} &nbsp; <b>Room:</b> {room.room_number}<br/>"
                   f"<b>Address:</b> {room.address or '-'}<br/><b>Tenant:</b> {room.tenant.name} "
                   f"&nbsp; <b>Landlord:</b> {room.landlord_email or '-'}", ss["BodyText"]),
         Paragraph(f"<b>Move-in:</b> {mi.inspection_date if mi else 'not recorded'} &nbsp; "
                   f"<b>Move-out:</b> {mo.inspection_date if mo else 'not recorded'}", ss["BodyText"]),
         Spacer(1, 8)]
    for a, b in pair_items(mi, mo):
        title = (a or b).area
        rows = [[Paragraph(f"<b>{title}</b>", ss["Heading4"]), ""],
                [cell(a), cell(b)]]
        if b and b.change:
            rows.append([Paragraph(f"<b>Change observed by landlord:</b> {b.change}. "
                                   f"{b.change_note}", small), ""])
        t = Table(rows, colWidths=[9 * cm, 9 * cm])
        st = [("SPAN", (0, 0), (1, 0)), ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
              ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8ecef")),
              ("VALIGN", (0, 0), (-1, -1), "TOP")]
        if len(rows) == 3:
            st.append(("SPAN", (0, 2), (1, 2)))
        t.setStyle(TableStyle(st))
        s += [t, Spacer(1, 8)]
    s.append(Paragraph("<i>Note: This report organises photos and user-recorded observations. "
                       "Photos and timestamps alone do not conclusively prove who caused a "
                       "particular damage.</i>", small))
    doc.build(s)
    return buf.getvalue()


@app.post("/room/<int:rid>/report")
@login_required
def make_report(rid):
    r = get_room(rid)
    mi, mo = latest(rid, "move_in"), latest(rid, "move_out")
    if not (mi and mi.items) and not (mo and mo.items):
        flash("Record at least one inspection before generating a report.", "danger")
        return redirect(url_for("room", rid=rid))
    n = Report.query.filter_by(room_id=rid).count() + 1
    code = f"PN-{now():%Y%m%d}-{rid:03d}-{n:02d}"
    key = f"rooms/{rid}/reports/evidence.pdf"   # same key each time -> versions pile up
    store.put(key, build_pdf(r, mi, mo, code), "application/pdf")
    db.session.add(Report(room_id=rid, code=code, s3_object_key=key))
    audit("report_generated", code, rid)
    db.session.commit()
    flash(f"Report {code} generated.", "success")
    return redirect(url_for("history", rid=rid))


# ---------------------------------------------------------------- history
@app.route("/room/<int:rid>/history")
@login_required
def history(rid):
    r = get_room(rid)
    logs = Audit.query.filter_by(room_id=rid).order_by(Audit.id.desc()).all()
    reports = Report.query.filter_by(room_id=rid).order_by(Report.id.desc()).all()
    files = []
    if reports:
        files.append(("Evidence report", reports[0].s3_object_key, store.versions(reports[0].s3_object_key)))
    for ins in r.inspections:
        for it in ins.items:
            v = store.versions(it.photo_key)
            if len(v) > 1:
                files.append((f"{ins.kind.replace('_', '-')} photo: {it.area}", it.photo_key, v))
    return render_template("history.html", room=r, logs=logs, reports=reports, files=files,
                           owner=r.tenant_id == g.user.id)


@app.post("/room/<int:rid>/restore")
@login_required
def restore(rid):
    get_room(rid, owner=True)
    key, vid = request.form["key"], request.form["vid"]
    if not key.startswith(f"rooms/{rid}/") or ".." in key:
        abort(400)
    store.restore(key, vid)
    audit("version_restored", key.split("/")[-1], rid)
    db.session.commit()
    flash("Older version restored. It is now the current file.", "success")
    return redirect(url_for("history", rid=rid))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=bool(os.environ.get("DEBUG")))
