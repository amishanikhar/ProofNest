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
