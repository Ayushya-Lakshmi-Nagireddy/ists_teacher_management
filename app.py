import json
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import scheduler
from database import INSTANCE, create_tables, get_db, get_setting, set_setting


app = Flask(__name__)


def _secret_key():
    os.makedirs(INSTANCE, exist_ok=True)
    path = os.path.join(INSTANCE, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


app.secret_key = os.environ.get("SECRET_KEY") or _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

create_tables()

MAX_YEAR = 5
MAX_CLASSES = 12
MAX_PERIODS = 10
MIN_PASSWORD = 8
DEFAULT_SEM = 60
MAX_SEM = 500
SUB_DAILY_LIMIT = 6
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,30}$")
TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sri", "smt", "shri"}


def hash_password(password):
    return generate_password_hash(password, method="pbkdf2:sha256:600000")


def clean(text):
    return " ".join((text or "").split())


def norm_name(text):
    words = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()
    return " ".join(w for w in words if w not in TITLES)


def to_int(text, default=None):
    try:
        return int(text)
    except (ValueError, TypeError):
        return default


def year_label(n):
    if not n:
        return ""
    return "%d%s Year" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n, "th"))


def class_label(dept, year_no, section):
    return "%s %s %s" % (dept, year_label(year_no), section)


def subject_hue(text):
    return sum(ord(c) * (i + 3) for i, c in enumerate((text or "").lower())) * 37 % 360


def nice_time(text):
    try:
        return datetime.strptime(text, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return text


def nice_date(d):
    return d.strftime("%A, %d %B %Y")


def weekly_periods(sem, weeks):
    return max(1, -(-sem // weeks))


app.add_template_filter(year_label, "year_label")
app.add_template_filter(nice_time, "nice_time")
app.add_template_filter(subject_hue, "subject_hue")


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["DAY_SHORT"] = DAY_SHORT
app.jinja_env.globals["DAY_NAMES"] = DAY_NAMES


@app.before_request
def protect():
    g.ws = session.get("ws")
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Your session expired. Go back, refresh the page and try again.")


@app.after_request
def headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def ws():
    return g.ws


def S(db, key):
    return get_setting(db, key, ws())


def setS(db, key, value):
    set_setting(db, key, value, ws())


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapped


def hod_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "hod":
            abort(403)
        return view(*a, **kw)
    return wrapped


def teacher_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "teacher":
            abort(403)
        return view(*a, **kw)
    return wrapped


@app.errorhandler(403)
def forbidden(e):
    return render_template("auth/error.html", code=403, text="This page is only for the HOD."), 403


@app.errorhandler(400)
def bad_request(e):
    return render_template("auth/error.html", code=400, text=getattr(e, "description", "Bad request.")), 400


@app.errorhandler(404)
def not_found(e):
    return render_template("auth/error.html", code=404, text="That page does not exist."), 404


def load_departments(db):
    depts = db.execute("SELECT * FROM departments WHERE owner_id = ? ORDER BY name COLLATE NOCASE", (ws(),)).fetchall()
    years = db.execute("""SELECT y.* FROM department_years y JOIN departments d ON d.id = y.department_id
                          WHERE d.owner_id = ? ORDER BY y.year_no""", (ws(),)).fetchall()
    result = []
    for d in depts:
        ylist = [{"id": y["id"], "year_no": y["year_no"], "label": year_label(y["year_no"]),
                  "count": y["class_count"], "ppd": y["periods_per_day"],
                  "classes": ["D%d" % n for n in range(1, y["class_count"] + 1)]}
                 for y in years if y["department_id"] == d["id"]]
        result.append({"id": d["id"], "name": d["name"], "years": ylist})
    return result


def all_classes(departments):
    out = []
    for d in departments:
        for y in d["years"]:
            for s in y["classes"]:
                out.append({"key": "%d-%d-%s" % (d["id"], y["year_no"], s), "dept_id": d["id"],
                            "dept": d["name"], "year_no": y["year_no"], "section": s, "ppd": y["ppd"],
                            "label": class_label(d["name"], y["year_no"], s)})
    return out


def load_grid(db):
    return json.loads(S(db, "grid"))


def load_days(db):
    return sorted(int(x) for x in S(db, "working_days").split(",") if x != "")


def teaching_weeks(db):
    return max(1, to_int(S(db, "weeks_per_sem"), 15))


def parse_days(text):
    return {int(x) for x in (text or "").split(",") if x.strip().isdigit() and 0 <= int(x) <= 6}


def has_timetable(db):
    return bool(db.execute("""SELECT 1 FROM timetable t JOIN departments d ON d.id = t.department_id
                              WHERE d.owner_id = ? LIMIT 1""", (ws(),)).fetchone())


def mark_stale(db):
    if has_timetable(db):
        setS(db, "stale", "1")


def own_dept_ids_sql():
    return "(SELECT id FROM departments WHERE owner_id = %d)" % int(ws())


def load_teachers(db):
    rows = db.execute("""
        SELECT t.*, d.name AS dept_name, u.username
        FROM teachers t
        LEFT JOIN departments d ON d.id = t.department_id
        LEFT JOIN users u ON u.teacher_id = t.id
        WHERE t.owner_id = ?
        ORDER BY t.name COLLATE NOCASE""", (ws(),)).fetchall()
    return [dict(r, days=parse_days(r["available_days"])) for r in rows]


def load_assignments(db):
    return db.execute("""
        SELECT a.*, d.name AS dept_name FROM assignments a
        JOIN departments d ON d.id = a.department_id
        WHERE d.owner_id = ?
        ORDER BY d.name COLLATE NOCASE, a.year_no, a.section, a.subject COLLATE NOCASE""", (ws(),)).fetchall()


def load_schedule(db):
    return db.execute("""
        SELECT t.day, t.period_no, t.department_id, t.year_no, t.section,
               a.id AS assignment_id, a.subject, a.block, a.teacher_id,
               te.name AS teacher_name, te.department_id AS teacher_dept, d.name AS dept_name
        FROM timetable t
        JOIN assignments a ON a.id = t.assignment_id
        JOIN teachers te ON te.id = a.teacher_id
        JOIN departments d ON d.id = t.department_id
        WHERE d.owner_id = ?""", (ws(),)).fetchall()


def grid_rows(grid):
    out = []
    for i, p in enumerate(grid):
        if i > 0:
            gap = _minutes(p["start"]) - _minutes(grid[i - 1]["end"])
            if gap > 0:
                out.append({"type": "break", "label": "Lunch" if gap >= 30 else "Break",
                            "start": grid[i - 1]["end"], "end": p["start"]})
        out.append({"type": "period", "no": p["no"], "start": p["start"], "end": p["end"]})
    return out


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def current_period(grid, now):
    mins = now.hour * 60 + now.minute
    for p in grid:
        if _minutes(p["start"]) <= mins < _minutes(p["end"]):
            return p["no"]
    return None


def next_period(grid, now):
    mins = now.hour * 60 + now.minute
    for p in grid:
        if _minutes(p["start"]) > mins:
            return p["no"]
    return None


def day_entries(db, when):
    day = when.weekday()
    iso = when.isoformat()
    absent = {r["teacher_id"] for r in db.execute(
        """SELECT a.teacher_id FROM absences a JOIN teachers t ON t.id = a.teacher_id
           WHERE a.date = ? AND t.owner_id = ?""", (iso, ws()))}
    subs = {}
    for r in db.execute("""SELECT s.*, te.name AS name FROM substitutions s
                           JOIN teachers te ON te.id = s.teacher_id
                           WHERE s.date = ? AND te.owner_id = ?""", (iso, ws())):
        subs[(r["department_id"], r["year_no"], r["section"], r["period_no"])] = r
    entries = []
    for r in load_schedule(db):
        if r["day"] != day:
            continue
        e = {"dept_id": r["department_id"], "dept": r["dept_name"], "year_no": r["year_no"],
             "section": r["section"], "period": r["period_no"], "subject": r["subject"], "block": r["block"],
             "orig_id": r["teacher_id"], "orig_name": r["teacher_name"], "orig_dept": r["teacher_dept"],
             "teacher_id": r["teacher_id"], "teacher_name": r["teacher_name"], "status": "normal",
             "label": class_label(r["dept_name"], r["year_no"], r["section"])}
        if r["teacher_id"] in absent:
            sub = subs.get((r["department_id"], r["year_no"], r["section"], r["period_no"]))
            if sub:
                e.update(teacher_id=sub["teacher_id"], teacher_name=sub["name"], status="covered")
            else:
                e["status"] = "uncovered"
        entries.append(e)
    entries.sort(key=lambda e: (e["period"], e["dept"], e["year_no"], e["section"]))
    return entries, absent


def free_pool(db, when, entries, absent, teachers):
    day = when.weekday()
    busy, load = set(), {}
    for e in entries:
        busy.add((e["teacher_id"], e["period"]))
        if e["teacher_id"] not in absent:
            load[e["teacher_id"]] = load.get(e["teacher_id"], 0) + 1

    def options(entry, extra_busy=None, extra_load=None):
        out = []
        for t in teachers:
            tid = t["id"]
            if tid in absent or day not in t["days"] or (tid, entry["period"]) in busy:
                continue
            if extra_busy and (tid, entry["period"]) in extra_busy:
                continue
            used = load.get(tid, 0) + (extra_load or {}).get(tid, 0)
            if used >= SUB_DAILY_LIMIT:
                continue
            same = 0 if t["department_id"] == entry["dept_id"] else 1
            out.append((same, used, t["name"].lower(), t))
        out.sort(key=lambda x: x[:3])
        return [x[3] for x in out]

    return options, busy, load


FAILS = {}


def throttled(key):
    count, first = FAILS.get(key, (0, 0))
    if count >= 5 and time.time() - first < 300:
        return True
    if time.time() - first >= 300:
        FAILS.pop(key, None)
    return False


def fail(key):
    count, first = FAILS.get(key, (0, time.time()))
    FAILS[key] = (count + 1, first)


def start_session(user):
    session.clear()
    session.update(user_id=user["id"], role=user["role"], full_name=user["full_name"],
                   teacher_id=user["teacher_id"], ws=user["owner_id"])


def read_role():
    role = request.values.get("role", "hod")
    return role if role in ("hod", "teacher") else "hod"


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    role = read_role()
    if request.method == "POST":
        username = clean(request.form.get("username"))
        key = (request.remote_addr, username.lower())
        if throttled(key):
            flash("Too many wrong attempts. Wait a few minutes and try again.", "error")
        else:
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            db.close()
            if user and user["role"] == role and check_password_hash(user["password_hash"], request.form.get("password", "")):
                FAILS.pop(key, None)
                start_session(user)
                return redirect(url_for("dashboard"))
            fail(key)
            if user and user["role"] != role and check_password_hash(user["password_hash"], request.form.get("password", "")):
                flash("That is a %s account. Use the %s tab." % (user["role"].upper() if user["role"] == "hod" else "teacher",
                                                              "HOD" if user["role"] == "hod" else "Teacher"), "error")
            else:
                flash("Wrong username or password.", "error")
    return render_template("auth/login.html", role=role)


@app.route("/setup")
def setup():
    return redirect(url_for("register"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    role = read_role()
    if request.method == "POST":
        db = get_db()
        username = clean(request.form.get("username"))
        error = None
        if not USERNAME_RE.match(username):
            error = "Username must be 3-30 letters, numbers, dots, dashes or underscores."
        elif db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            error = "The username %s is already taken." % username

        if role == "hod" and not error:
            name = clean(request.form.get("full_name"))
            password = request.form.get("password", "")
            if not name:
                error = "Enter your name."
            elif len(password) < MIN_PASSWORD:
                error = "Password must be at least %d characters." % MIN_PASSWORD
            elif password != request.form.get("confirm_password", ""):
                error = "Passwords do not match."
            else:
                with db:
                    uid = db.execute("INSERT INTO users (username, password_hash, role, full_name) VALUES (?,?, 'hod', ?)",
                                     (username, hash_password(password), name)).lastrowid
                    db.execute("UPDATE users SET owner_id = ? WHERE id = ?", (uid, uid))
                user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                db.close()
                start_session(user)
                flash("Welcome! Your workspace is ready - start by adding a department.", "success")
                return redirect(url_for("dashboard"))

        if role == "teacher" and not error:
            password = request.form.get("password", "")
            if len(password) < MIN_PASSWORD:
                error = "Password must be at least %d characters." % MIN_PASSWORD
            elif password != request.form.get("confirm_password", ""):
                error = "Passwords do not match."
            else:
                error = register_teacher(db, username, password)
            if error is None:
                user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
                db.close()
                start_session(user)
                flash("You are registered. This is your schedule.", "success")
                return redirect(url_for("dashboard"))
        db.close()
        flash(error, "error")
    return render_template("auth/register.html", role=role)


def register_teacher(db, username, password):
    name = clean(request.form.get("full_name"))
    subject = clean(request.form.get("subject")).lower()
    hod_username = clean(request.form.get("hod_username"))
    if not name or not subject:
        return "Enter your name and one subject you teach."
    candidates = db.execute("""
        SELECT t.id, t.name, t.owner_id, u.id AS user_id, h.username AS hod_username FROM teachers t
        JOIN users h ON h.id = t.owner_id
        LEFT JOIN users u ON u.teacher_id = t.id
        WHERE EXISTS (SELECT 1 FROM assignments a WHERE a.teacher_id = t.id AND lower(a.subject) = ?)""",
        (subject,)).fetchall()
    found = [c for c in candidates if norm_name(name) and norm_name(c["name"]) == norm_name(name)]
    if hod_username:
        found = [c for c in found if c["hod_username"].lower() == hod_username.lower()]
    if not found:
        return ("We could not find a teacher with that name teaching that subject. "
                "Ask your HOD to add you with your subject first, then register.")
    if len(found) > 1:
        return "More than one HOD has a teacher with that name and subject. Enter your HOD's username to continue."
    teacher = found[0]
    if teacher["user_id"]:
        return "This teacher is already registered. Sign in, or ask your HOD to reset your login."
    with db:
        db.execute("""INSERT INTO users (username, password_hash, role, full_name, teacher_id, owner_id)
                      VALUES (?, ?, 'teacher', ?, ?, ?)""",
                   (username, hash_password(password), teacher["name"], teacher["id"], teacher["owner_id"]))
    return None


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def home():
    return redirect(url_for("dashboard"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        new = request.form.get("new_password", "")
        if not check_password_hash(user["password_hash"], request.form.get("old_password", "")):
            flash("Your current password is wrong.", "error")
        elif len(new) < MIN_PASSWORD:
            flash("New password must be at least %d characters." % MIN_PASSWORD, "error")
        elif new != request.form.get("confirm_password", ""):
            flash("New passwords do not match.", "error")
        else:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new), user["id"]))
            db.commit()
            flash("Password changed.", "success")
        db.close()
        return redirect(url_for("account"))
    db = get_db()
    user = db.execute("SELECT username FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    db.close()
    return render_template("account.html", username=user["username"])


def read_year_form(form):
    years, errors, seen = [], [], set()
    for y, c in zip(form.getlist("year_no"), form.getlist("class_count")):
        y, c = to_int(y), to_int(c)
        if y is None or not 1 <= y <= MAX_YEAR or c is None or not 1 <= c <= MAX_CLASSES:
            errors.append("Years must be 1-%d with 1-%d classes each." % (MAX_YEAR, MAX_CLASSES))
        elif y in seen:
            errors.append("%s is added twice." % year_label(y))
        else:
            seen.add(y)
            years.append((y, c))
    if not years:
        errors.append("Add at least one year.")
    return sorted(years), sorted(set(errors))


def save_department(db, dept_id, name, years):
    removed = 0
    if dept_id is None:
        dept_id = db.execute("INSERT INTO departments (owner_id, name) VALUES (?, ?)", (ws(), name)).lastrowid
    else:
        db.execute("UPDATE departments SET name = ? WHERE id = ?", (name, dept_id))
        keep = {y for y, _ in years}
        for r in db.execute("SELECT year_no FROM department_years WHERE department_id = ?", (dept_id,)).fetchall():
            if r["year_no"] not in keep:
                db.execute("DELETE FROM department_years WHERE department_id = ? AND year_no = ?",
                           (dept_id, r["year_no"]))
    for y, c in years:
        db.execute("""INSERT INTO department_years (department_id, year_no, class_count) VALUES (?, ?, ?)
                      ON CONFLICT(department_id, year_no) DO UPDATE SET class_count = excluded.class_count""",
                   (dept_id, y, c))
    counts = dict(years)
    for a in db.execute("SELECT id, year_no, section FROM assignments WHERE department_id = ?", (dept_id,)).fetchall():
        n = to_int(a["section"][1:], 0)
        if a["year_no"] not in counts or n > counts[a["year_no"]]:
            db.execute("DELETE FROM assignments WHERE id = ?", (a["id"],))
            removed += 1
    for table in ("timetable", "substitutions"):
        for r in db.execute("SELECT DISTINCT year_no, section FROM %s WHERE department_id = ?" % table, (dept_id,)).fetchall():
            n = to_int(r["section"][1:], 0)
            if r["year_no"] not in counts or n > counts[r["year_no"]]:
                db.execute("DELETE FROM %s WHERE department_id = ? AND year_no = ? AND section = ?" % table,
                           (dept_id, r["year_no"], r["section"]))
    return dept_id, removed


def own_department(db, dept_id):
    return db.execute("SELECT * FROM departments WHERE id = ? AND owner_id = ?", (dept_id, ws())).fetchone()


@app.route("/departments")
@hod_required
def departments():
    db = get_db()
    data = load_departments(db)
    teacher_counts = {r["department_id"]: r["n"] for r in db.execute(
        """SELECT department_id, COUNT(*) n FROM teachers WHERE owner_id = ? AND department_id IS NOT NULL
           GROUP BY department_id""", (ws(),))}
    db.close()
    return render_template("departments.html", departments=data, teacher_counts=teacher_counts)


@app.route("/departments/add", methods=["GET", "POST"])
@hod_required
def add_department():
    dept = {"id": None, "name": "", "years": [{"year_no": 1, "count": 3}]}
    if request.method == "POST":
        name = clean(request.form.get("name"))
        years, errors = read_year_form(request.form)
        dept["name"] = name
        dept["years"] = [{"year_no": y, "count": c} for y, c in years] or dept["years"]
        db = get_db()
        if not name:
            errors.append("Enter the department name.")
        elif db.execute("SELECT 1 FROM departments WHERE owner_id = ? AND name = ?", (ws(), name)).fetchone():
            errors.append("%s already exists." % name)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            with db:
                save_department(db, None, name, years)
            db.close()
            flash("Department %s added." % name, "success")
            return redirect(url_for("departments"))
        db.close()
    return render_template("department_form.html", editing=False, dept=dept, max_year=MAX_YEAR, max_classes=MAX_CLASSES)


@app.route("/departments/<int:id>/edit", methods=["GET", "POST"])
@hod_required
def edit_department(id):
    db = get_db()
    if not own_department(db, id):
        db.close()
        abort(404)
    dept = next(d for d in load_departments(db) if d["id"] == id)
    if request.method == "POST":
        name = clean(request.form.get("name"))
        years, errors = read_year_form(request.form)
        if not name:
            errors.append("Enter the department name.")
        elif db.execute("SELECT 1 FROM departments WHERE owner_id = ? AND name = ? AND id != ?", (ws(), name, id)).fetchone():
            errors.append("%s already exists." % name)
        if errors:
            for e in errors:
                flash(e, "error")
            dept = dict(dept, name=name, years=[{"year_no": y, "count": c} for y, c in years] or dept["years"])
        else:
            with db:
                _, removed = save_department(db, id, name, years)
                if removed:
                    mark_stale(db)
            db.close()
            msg = "Department updated."
            if removed:
                msg += " %d subject assignment(s) of removed classes were deleted - regenerate the timetable." % removed
            flash(msg, "success")
            return redirect(url_for("departments"))
    db.close()
    return render_template("department_form.html", editing=True, dept=dept, max_year=MAX_YEAR, max_classes=MAX_CLASSES)


@app.route("/departments/<int:id>/delete", methods=["POST"])
@hod_required
def delete_department(id):
    db = get_db()
    if not own_department(db, id):
        db.close()
        abort(404)
    n = db.execute("SELECT COUNT(*) n FROM teachers WHERE department_id = ?", (id,)).fetchone()["n"]
    if n:
        flash("Move or delete the %d teacher(s) of this department first." % n, "error")
    else:
        with db:
            db.execute("DELETE FROM departments WHERE id = ?", (id,))
            mark_stale(db)
        flash("Department deleted.", "success")
    db.close()
    return redirect(url_for("departments"))


def read_assignments(form, departments, weeks):
    lookup = {d["id"]: {y["year_no"]: y["count"] for y in d["years"]} for d in departments}
    names = {d["id"]: d["name"] for d in departments}
    depts, years = form.getlist("asg_dept"), form.getlist("asg_year")
    subjects, totals, blocks = form.getlist("asg_subject"), form.getlist("asg_sem"), form.getlist("asg_block")
    row_ids = form.getlist("asg_row")
    rows, errors, seen = [], [], set()
    for i in range(min(len(row_ids), len(depts), len(years), len(subjects), len(totals), len(blocks))):
        n = i + 1
        dept, year = to_int(depts[i]), to_int(years[i])
        subject = clean(subjects[i])
        sem, block = to_int(totals[i]), to_int(blocks[i], 1)
        count = lookup.get(dept, {}).get(year)
        sections = sorted({clean(x).upper() for x in form.getlist("asg_sec_" + row_ids[i])},
                          key=lambda x: to_int(x[1:], 0))
        if not subject:
            errors.append("Class row %d: enter the subject." % n)
        elif count is None:
            errors.append("Class row %d: pick a department and year that exist." % n)
        elif not sections:
            errors.append("Class row %d: tick at least one class (D1, D2...)." % n)
        elif any(not re.fullmatch(r"D\d{1,2}", x) or not 1 <= int(x[1:]) <= count for x in sections):
            errors.append("Class row %d: that class does not exist in this year." % n)
        elif sem is None or not 1 <= sem <= MAX_SEM:
            errors.append("Class row %d: total periods in the semester must be 1-%d." % (n, MAX_SEM))
        elif block not in (1, 2):
            errors.append("Class row %d: invalid period type." % n)
        else:
            for section in sections:
                key = (dept, year, section, subject.lower())
                if key in seen:
                    errors.append("%s for %s is listed twice." % (subject, class_label(names[dept], year, section)))
                seen.add(key)
                rows.append({"dept": dept, "year": year, "section": section, "subject": subject,
                             "sem": sem, "ppw": weekly_periods(sem, weeks), "block": block})
    return rows, errors


def group_rows(rows):
    groups = {}
    for r in rows:
        key = (r["dept"], r["year"], r["subject"].lower(), r["sem"], r["block"])
        grp = groups.setdefault(key, {"dept": r["dept"], "year": r["year"], "subject": r["subject"],
                                      "sem": r["sem"], "ppw": r["ppw"], "block": r["block"], "sections": []})
        grp["sections"].append(r["section"])
    return list(groups.values())


def assignment_signature(rows):
    return sorted((r["dept"], r["year"], r["section"], r["subject"].lower(), r["sem"], r["ppw"], r["block"])
                  for r in rows)


def assignment_rows(db, teacher_id):
    return [{"dept": a["department_id"], "year": a["year_no"], "section": a["section"], "subject": a["subject"],
             "sem": a["sem_periods"], "ppw": a["periods_per_week"], "block": a["block"]}
            for a in db.execute("SELECT * FROM assignments WHERE teacher_id = ? ORDER BY id", (teacher_id,))]


def own_teacher(db, teacher_id):
    return db.execute("""SELECT t.*, u.username FROM teachers t LEFT JOIN users u ON u.teacher_id = t.id
                         WHERE t.id = ? AND t.owner_id = ?""", (teacher_id, ws())).fetchone()


@app.route("/teachers")
@hod_required
def teachers():
    db = get_db()
    days = load_days(db)
    grid = load_grid(db)
    weeks = teaching_weeks(db)
    depts = load_departments(db)
    people = load_teachers(db)
    by_teacher = {}
    for a in load_assignments(db):
        by_teacher.setdefault(a["teacher_id"], []).append(a)
    for t in people:
        mine = by_teacher.get(t["id"], [])
        t["lessons"] = group_rows([{"dept": a["dept_name"], "year": a["year_no"], "section": a["section"],
                                    "subject": a["subject"], "sem": a["sem_periods"],
                                    "ppw": a["periods_per_week"], "block": a["block"]} for a in mine])
        t["load"] = sum(a["periods_per_week"] for a in mine)
        t["sem"] = sum(a["sem_periods"] for a in mine)
        t["capacity"] = len(grid) * len([d for d in days if d in t["days"]])
    groups = [("Guest teachers", [t for t in people if t["department_id"] is None])]
    for d in depts:
        groups.insert(len(groups) - 1, (d["name"], [t for t in people if t["department_id"] == d["id"]]))
    db.close()
    return render_template("teachers.html", groups=groups, total=len(people), weeks=weeks)


def _teacher_post(db, teacher):
    f = request.form
    departments = load_departments(db)
    dept_raw = f.get("department", "guest")
    dept_id = None if dept_raw == "guest" else to_int(dept_raw)
    name = clean(f.get("name"))
    avail = sorted({int(x) for x in f.getlist("days") if x.isdigit() and 0 <= int(x) <= 6})
    weeks = teaching_weeks(db)
    rows, errors = read_assignments(f, departments, weeks)
    editing = teacher is not None

    if not name:
        errors.append("Enter the teacher's name.")
    elif any(norm_name(t["name"]) == norm_name(name) and (not editing or t["id"] != teacher["id"])
             for t in load_teachers(db)):
        errors.append("A teacher named %s already exists." % name)
    if dept_id is not None and not any(d["id"] == dept_id for d in departments):
        errors.append("Choose a department or Guest.")
    if not avail:
        errors.append("Pick at least one day the teacher is available.")

    me = teacher["id"] if editing else 0
    for r in rows:
        other = db.execute("""SELECT te.name FROM assignments a JOIN teachers te ON te.id = a.teacher_id
                              WHERE a.department_id = ? AND a.year_no = ? AND a.section = ?
                              AND a.subject = ? COLLATE NOCASE AND a.teacher_id != ?""",
                           (r["dept"], r["year"], r["section"], r["subject"], me)).fetchone()
        if other:
            label = next(class_label(d["name"], r["year"], r["section"]) for d in departments if d["id"] == r["dept"])
            errors.append("%s for %s is already taught by %s." % (r["subject"], label, other["name"]))

    ctx = {"form": {"name": name, "department": dept_raw, "days": avail or [0, 1, 2, 3, 4, 5]},
           "assignments": group_rows(rows)}
    if errors:
        for e in sorted(set(errors)):
            flash(e, "error")
        return False, ctx

    with db:
        if editing:
            changed = (assignment_signature(assignment_rows(db, me)) != assignment_signature(rows)
                       or parse_days(teacher["available_days"]) != set(avail))
            db.execute("UPDATE teachers SET name=?, department_id=?, available_days=? WHERE id=?",
                       (name, dept_id, ",".join(map(str, avail)), me))
            db.execute("UPDATE users SET full_name = ? WHERE teacher_id = ?", (name, me))
            tid = me
            mine = {(a["department_id"], a["year_no"], a["section"], a["subject"].lower()): a
                    for a in db.execute("SELECT * FROM assignments WHERE teacher_id = ?", (me,))}
            wanted = {(r["dept"], r["year"], r["section"], r["subject"].lower()) for r in rows}
            for key, a in mine.items():
                if key not in wanted:
                    db.execute("DELETE FROM assignments WHERE id = ?", (a["id"],))
        else:
            changed = True
            tid = db.execute("INSERT INTO teachers (owner_id, name, department_id, available_days) VALUES (?,?,?,?)",
                             (ws(), name, dept_id, ",".join(map(str, avail)))).lastrowid
            mine = {}
        for r in rows:
            a = mine.get((r["dept"], r["year"], r["section"], r["subject"].lower()))
            if a:
                if (a["sem_periods"], a["periods_per_week"], a["block"]) != (r["sem"], r["ppw"], r["block"]):
                    db.execute("DELETE FROM timetable WHERE assignment_id = ?", (a["id"],))
                db.execute("UPDATE assignments SET subject=?, sem_periods=?, periods_per_week=?, block=? WHERE id=?",
                           (r["subject"], r["sem"], r["ppw"], r["block"], a["id"]))
            else:
                db.execute("""INSERT INTO assignments (teacher_id, department_id, year_no, section, subject,
                              sem_periods, periods_per_week, block) VALUES (?,?,?,?,?,?,?,?)""",
                           (tid, r["dept"], r["year"], r["section"], r["subject"], r["sem"], r["ppw"], r["block"]))
        if changed:
            mark_stale(db)
    return True, ctx


@app.route("/teachers/add", methods=["GET", "POST"])
@hod_required
def add_teacher():
    db = get_db()
    departments = load_departments(db)
    ctx = {"form": {"name": "", "department": "guest" if not departments else str(departments[0]["id"]),
                    "days": [0, 1, 2, 3, 4, 5]}, "assignments": []}
    if request.method == "POST":
        ok, ctx = _teacher_post(db, None)
        if ok:
            db.close()
            flash("Teacher added. They can now register with their name and one of these subjects.", "success")
            return redirect(url_for("teachers"))
    wd, weeks = load_days(db), teaching_weeks(db)
    db.close()
    return render_template("teacher_form.html", editing=False, departments=departments, working_days=wd,
                           weeks=weeks, default_sem=DEFAULT_SEM, **ctx)


@app.route("/teachers/<int:id>/edit", methods=["GET", "POST"])
@hod_required
def edit_teacher(id):
    db = get_db()
    teacher = own_teacher(db, id)
    if not teacher:
        db.close()
        abort(404)
    departments = load_departments(db)
    ctx = {"form": {"name": teacher["name"],
                    "department": "guest" if teacher["department_id"] is None else str(teacher["department_id"]),
                    "days": sorted(parse_days(teacher["available_days"]))},
           "assignments": group_rows(assignment_rows(db, id))}
    if request.method == "POST":
        ok, ctx = _teacher_post(db, teacher)
        if ok:
            db.close()
            flash("Teacher updated.", "success")
            return redirect(url_for("teachers"))
    wd, weeks = load_days(db), teaching_weeks(db)
    db.close()
    return render_template("teacher_form.html", editing=True, teacher=teacher, departments=departments,
                           working_days=wd, weeks=weeks, default_sem=DEFAULT_SEM, **ctx)


@app.route("/teachers/<int:id>/delete", methods=["POST"])
@hod_required
def delete_teacher(id):
    db = get_db()
    if not own_teacher(db, id):
        db.close()
        abort(404)
    with db:
        db.execute("DELETE FROM users WHERE teacher_id = ?", (id,))
        db.execute("DELETE FROM teachers WHERE id = ?", (id,))
        mark_stale(db)
    db.close()
    flash("Teacher deleted along with their login and classes.", "success")
    return redirect(url_for("teachers"))


@app.route("/teachers/<int:id>/reset-login", methods=["POST"])
@hod_required
def reset_teacher_login(id):
    db = get_db()
    if not own_teacher(db, id):
        db.close()
        abort(404)
    db.execute("DELETE FROM users WHERE teacher_id = ?", (id,))
    db.commit()
    db.close()
    flash("Login removed. The teacher can register again with a new username.", "success")
    return redirect(url_for("teachers"))


def plan_inputs(db):
    days = load_days(db)
    grid = load_grid(db)
    classes = {}
    for d in load_departments(db):
        for y in d["years"]:
            for s in y["classes"]:
                classes[(d["id"], y["year_no"], s)] = min(y["ppd"], len(grid))
    lessons = [{"id": a["id"], "class": (a["department_id"], a["year_no"], a["section"]), "subject": a["subject"],
                "teacher_id": a["teacher_id"], "periods": a["periods_per_week"], "block": a["block"]}
               for a in load_assignments(db)]
    load = {}
    for l in lessons:
        load[l["teacher_id"]] = load.get(l["teacher_id"], 0) + l["periods"]
    people = {}
    for t in load_teachers(db):
        avail = t["days"] & set(days)
        cap = min(len(grid), max(2, -(-load.get(t["id"], 0) // len(avail)) + 1)) if avail else 0
        people[t["id"]] = {"max_day": cap, "days": avail}
    return classes, lessons, people, days, grid


@app.route("/planner")
@hod_required
def planner():
    db = get_db()
    depts = load_departments(db)
    grid = load_grid(db)
    days = load_days(db)
    classes, lessons, people, _, _ = plan_inputs(db)
    names = {t["id"]: t["name"] for t in load_teachers(db)}
    dept_names = {d["id"]: d["name"] for d in depts}

    need = {}
    for l in lessons:
        need[l["class"]] = need.get(l["class"], 0) + l["periods"]
    class_rows = []
    for ck, ppd in classes.items():
        required, room = need.get(ck, 0), ppd * len(days)
        status = "empty" if required == 0 else "over" if required > room else "full" if required == room else "ok"
        class_rows.append({"label": class_label(dept_names[ck[0]], ck[1], ck[2]), "required": required,
                           "room": room, "status": status, "free": max(0, room - required)})
    load = {}
    for l in lessons:
        load[l["teacher_id"]] = load.get(l["teacher_id"], 0) + l["periods"]
    teacher_rows = []
    for tid, total in sorted(load.items(), key=lambda kv: names[kv[0]].lower()):
        room = len(grid) * len(people[tid]["days"])
        teacher_rows.append({"name": names[tid], "load": total, "room": room, "over": total > room,
                             "pct": min(100, round(100 * total / room)) if room else 100})

    placed = {r["assignment_id"]: r["n"] for r in db.execute(
        """SELECT t.assignment_id, COUNT(*) n FROM timetable t JOIN departments d ON d.id = t.department_id
           WHERE d.owner_id = ? GROUP BY t.assignment_id""", (ws(),))}
    coverage = []
    for a in load_assignments(db):
        got = placed.get(a["id"], 0)
        if got != a["periods_per_week"]:
            coverage.append({"teacher": names[a["teacher_id"]], "subject": a["subject"],
                             "label": class_label(a["dept_name"], a["year_no"], a["section"]),
                             "need": a["periods_per_week"], "got": got})
    has_table = has_timetable(db)
    stale = S(db, "stale") == "1"
    weeks = teaching_weeks(db)
    db.close()
    return render_template("planner.html", weeks=weeks, departments=depts, grid=grid, working_days=days,
                           class_rows=class_rows, teacher_rows=teacher_rows, coverage=coverage, has_table=has_table,
                           stale=stale, lesson_count=len(lessons), max_periods=MAX_PERIODS, default_sem=DEFAULT_SEM)


@app.route("/planner/save", methods=["POST"])
@hod_required
def planner_save():
    f = request.form
    days = sorted({int(x) for x in f.getlist("days") if x.isdigit() and 0 <= int(x) <= 6})
    starts, ends = f.getlist("p_start"), f.getlist("p_end")
    errors = []
    if not days:
        errors.append("Pick at least one working day.")
    if not 1 <= len(starts) <= MAX_PERIODS or len(starts) != len(ends):
        errors.append("Add between 1 and %d periods." % MAX_PERIODS)
    grid = []
    prev_end = -1
    for i, (s, e) in enumerate(zip(starts, ends)):
        if not TIME_RE.match(s or "") or not TIME_RE.match(e or ""):
            errors.append("Period %d needs a start and end time." % (i + 1))
        elif _minutes(s) >= _minutes(e):
            errors.append("Period %d must end after it starts." % (i + 1))
        elif _minutes(s) < prev_end:
            errors.append("Period %d starts before the previous one ends." % (i + 1))
        else:
            prev_end = _minutes(e)
            grid.append({"no": i + 1, "start": s, "end": e})
    weeks = to_int(f.get("weeks_per_sem"))
    if weeks is None or not 1 <= weeks <= 60:
        errors.append("Teaching weeks in the semester must be 1-60.")
    db = get_db()
    depts = load_departments(db)
    ppd = {}
    for d in depts:
        for y in d["years"]:
            v = to_int(f.get("ppd_%d" % y["id"]), y["ppd"])
            if not 1 <= v <= max(1, len(grid)):
                errors.append("Periods per day for %s %s must be 1-%d." % (d["name"], y["label"], len(grid)))
            ppd[y["id"]] = v
    if errors:
        for e in sorted(set(errors)):
            flash(e, "error")
        db.close()
        return redirect(url_for("planner"))

    before = (S(db, "working_days"), S(db, "grid"), teaching_weeks(db),
              [(d["id"], y["ppd"]) for d in depts for y in d["years"]])
    with db:
        setS(db, "working_days", ",".join(map(str, days)))
        setS(db, "grid", json.dumps(grid))
        if teaching_weeks(db) != weeks:
            setS(db, "weeks_per_sem", weeks)
            for a in load_assignments(db):
                new = weekly_periods(a["sem_periods"], weeks)
                if new != a["periods_per_week"]:
                    db.execute("UPDATE assignments SET periods_per_week = ? WHERE id = ?", (new, a["id"]))
                    db.execute("DELETE FROM timetable WHERE assignment_id = ?", (a["id"],))
        for yid, v in ppd.items():
            db.execute("UPDATE department_years SET periods_per_day = ? WHERE id = ?", (v, yid))
        db.execute("""DELETE FROM timetable WHERE department_id IN %s AND EXISTS (SELECT 1 FROM department_years y
                      WHERE y.department_id = timetable.department_id AND y.year_no = timetable.year_no
                      AND timetable.period_no > y.periods_per_day)""" % own_dept_ids_sql())
        db.execute("DELETE FROM timetable WHERE department_id IN %s AND day NOT IN (%s)"
                   % (own_dept_ids_sql(), ",".join(map(str, days))))
        after = (S(db, "working_days"), S(db, "grid"), teaching_weeks(db),
                 [(d["id"], y["ppd"]) for d in load_departments(db) for y in d["years"]])
        if before != after:
            mark_stale(db)
    db.close()
    flash("Planner settings saved.", "success")
    return redirect(url_for("planner"))


@app.route("/planner/generate", methods=["POST"])
@hod_required
def planner_generate():
    db = get_db()
    classes, lessons, people, days, grid = plan_inputs(db)
    if not lessons:
        db.close()
        flash("Add teachers with subjects and classes first - there is nothing to schedule yet.", "error")
        return redirect(url_for("planner"))
    result = scheduler.generate(classes, lessons, people, days, grid, seconds=6.0)
    with db:
        db.execute("DELETE FROM timetable WHERE department_id IN %s" % own_dept_ids_sql())
        db.execute("DELETE FROM substitutions WHERE date >= ? AND department_id IN %s" % own_dept_ids_sql(),
                   (date.today().isoformat(),))
        lookup = {l["id"] for l in lessons}
        db.executemany(
            "INSERT INTO timetable (department_id, year_no, section, day, period_no, assignment_id) VALUES (?,?,?,?,?,?)",
            [(p["class"][0], p["class"][1], p["class"][2], p["day"], p["period"], p["lesson_id"])
             for p in result["placements"] if p["lesson_id"] in lookup])
        setS(db, "stale", "0")
        setS(db, "generated_at", datetime.now().strftime("%d %b %Y, %I:%M %p"))
    total = len(result["placements"])
    missing = sum(u["missing"] for u in result["unplaced"])
    if missing:
        flash("Timetable generated with %d period(s) placed, but %d could not fit. See 'Not fully scheduled' below."
              % (total, missing), "error")
    else:
        flash("Timetable generated: %d periods placed with no clashes." % total, "success")
    db.close()
    return redirect(url_for("planner"))


def build_cells(rows):
    cells = {}
    for r in rows:
        cells[(r["day"], r["period_no"])] = {"subject": r["subject"], "teacher": r["teacher_name"],
                                             "label": class_label(r["dept_name"], r["year_no"], r["section"]),
                                             "lab": r["block"] == 2}
    return cells


@app.route("/timetable")
@hod_required
def timetable():
    db = get_db()
    depts = load_departments(db)
    classes = all_classes(depts)
    people = load_teachers(db)
    grid = load_grid(db)
    days = load_days(db)
    view = request.args.get("view", "class")
    view = view if view in ("class", "teacher") else "class"
    rows = load_schedule(db)
    cells, title, sub, selected = {}, "", "", ""
    max_p = len(grid)
    if view == "class" and classes:
        selected = request.args.get("class") or classes[0]["key"]
        cur = next((c for c in classes if c["key"] == selected), classes[0])
        selected = cur["key"]
        mine = [r for r in rows if (r["department_id"], r["year_no"], r["section"]) == (cur["dept_id"], cur["year_no"], cur["section"])]
        cells = build_cells(mine)
        title, sub = cur["label"], "%d periods a week" % len(mine)
        max_p = cur["ppd"]
    elif view == "teacher" and people:
        selected = request.args.get("teacher") or str(people[0]["id"])
        cur = next((t for t in people if str(t["id"]) == selected), people[0])
        selected = str(cur["id"])
        mine = [r for r in rows if r["teacher_id"] == cur["id"]]
        cells = build_cells(mine)
        title, sub = cur["name"], "%d periods a week" % len(mine)
    generated = S(db, "generated_at")
    stale = S(db, "stale") == "1"
    db.close()
    return render_template("timetable.html", view=view, classes=classes, teachers=people, selected=selected,
                           cells=cells, title=title, sub=sub, rows=grid_rows(grid[:max_p]), days=days,
                           mode=view, generated=generated, stale=stale, own=False)


def my_classes(db, tid):
    return db.execute("""SELECT a.*, d.name dept_name FROM assignments a JOIN departments d ON d.id = a.department_id
                         WHERE a.teacher_id = ? ORDER BY d.name, a.year_no, a.section""", (tid,)).fetchall()


@app.route("/my-timetable")
@teacher_required
def my_timetable():
    db = get_db()
    tid = session["teacher_id"]
    grid = load_grid(db)
    days = load_days(db)
    mine = [r for r in load_schedule(db) if r["teacher_id"] == tid]
    cells = build_cells(mine)
    classes = my_classes(db, tid)
    generated = S(db, "generated_at")
    db.close()
    return render_template("timetable.html", view="teacher", cells=cells, title=session["full_name"],
                           sub="%d periods a week" % len(mine), rows=grid_rows(grid), days=days, mode="teacher",
                           generated=generated, stale=False, own=True, my_classes=classes)


def read_date(text):
    try:
        return datetime.strptime(text or "", "%Y-%m-%d").date()
    except ValueError:
        return date.today()


@app.route("/dashboard")
@login_required
def dashboard():
    if session["role"] == "teacher":
        return teacher_dashboard()
    return hod_dashboard()


def hod_dashboard():
    db = get_db()
    when = read_date(request.args.get("date"))
    today = date.today()
    now = datetime.now()
    depts = load_departments(db)
    classes = all_classes(depts)
    people = load_teachers(db)
    grid = load_grid(db)
    days = load_days(db)
    entries, absent = day_entries(db, when)
    working = when.weekday() in days

    dept_filter = to_int(request.args.get("dept"))
    shown = [c for c in classes if dept_filter in (None, c["dept_id"])]
    cell = {(e["dept_id"], e["year_no"], e["section"], e["period"]): e for e in entries}
    max_p = min(len(grid), max([c["ppd"] for c in shown] or [0]))

    options, _, _ = free_pool(db, when, entries, absent, people)
    need_cover = []
    for e in entries:
        if e["status"] in ("uncovered", "covered"):
            need_cover.append(dict(e, options=options(e) if e["status"] == "uncovered" else []))

    absent_list = [t for t in people if t["id"] in absent]
    present = [t for t in people if t["id"] not in absent]
    on_now = current_period(grid, now) if when == today else None
    free_now = []
    if on_now:
        busy = {e["teacher_id"] for e in entries if e["period"] == on_now}
        free_now = [t for t in present if t["id"] not in busy and when.weekday() in t["days"]]

    asg = {}
    for a in load_assignments(db):
        asg[a["teacher_id"]] = asg.get(a["teacher_id"], 0) + a["periods_per_week"]
    workload = []
    for t in people:
        room = len(grid) * len([d for d in days if d in t["days"]])
        workload.append({"name": t["name"], "guest": t["department_id"] is None, "load": asg.get(t["id"], 0),
                         "room": room, "pct": min(100, round(100 * asg.get(t["id"], 0) / room)) if room else 0})
    workload.sort(key=lambda w: -w["pct"])

    stats = {"departments": len(depts), "classes": len(classes), "teachers": len(people),
             "periods": len(entries), "absent": len(absent),
             "uncovered": len([e for e in entries if e["status"] == "uncovered"])}
    stale = S(db, "stale") == "1"
    has_table = has_timetable(db)
    db.close()
    return render_template("dashboard.html", when=when, when_text=nice_date(when), is_today=when == today,
                           prev_day=(when - timedelta(days=1)).isoformat(), next_day=(when + timedelta(days=1)).isoformat(),
                           working=working, departments=depts, dept_filter=dept_filter, shown=shown, cell=cell,
                           grid=grid[:max_p], on_now=on_now, stats=stats, need_cover=need_cover, absent_list=absent_list,
                           present=present, free_now=free_now, workload=workload[:8], stale=stale, has_table=has_table)


def teacher_dashboard():
    db = get_db()
    tid = session["teacher_id"]
    when = read_date(request.args.get("date"))
    today = date.today()
    grid = load_grid(db)
    days = load_days(db)
    entries, absent = day_entries(db, when)
    mine = [e for e in entries if e["teacher_id"] == tid]
    covering = [e for e in mine if e["status"] == "covered"]
    week = db.execute("SELECT COUNT(*) n FROM timetable t JOIN assignments a ON a.id = t.assignment_id WHERE a.teacher_id = ?",
                      (tid,)).fetchone()["n"]
    on_now = current_period(grid, datetime.now()) if when == today else None
    upcoming = next_period(grid, datetime.now()) if when == today else None
    times = {p["no"]: p for p in grid}
    classes = my_classes(db, tid)
    db.close()
    return render_template("teacher_home.html", when=when, when_text=nice_date(when), is_today=when == today,
                           prev_day=(when - timedelta(days=1)).isoformat(), next_day=(when + timedelta(days=1)).isoformat(),
                           working=when.weekday() in days, mine=mine, times=times, on_now=on_now, upcoming=upcoming,
                           absent=tid in absent, covering=len(covering), week=week, classes=classes)


def _iso(text):
    return read_date(text).isoformat()


@app.route("/absence/add", methods=["POST"])
@hod_required
def absence_add():
    d, tid = _iso(request.form.get("date")), to_int(request.form.get("teacher_id"))
    db = get_db()
    if tid and own_teacher(db, tid):
        db.execute("INSERT OR IGNORE INTO absences (date, teacher_id) VALUES (?, ?)", (d, tid))
        db.commit()
        flash("Marked absent. Their classes are listed below - assign cover.", "success")
    db.close()
    return redirect(url_for("dashboard", date=d))


@app.route("/absence/remove", methods=["POST"])
@hod_required
def absence_remove():
    d, tid = _iso(request.form.get("date")), to_int(request.form.get("teacher_id"))
    day = read_date(d).weekday()
    db = get_db()
    if tid and own_teacher(db, tid):
        with db:
            db.execute("DELETE FROM absences WHERE date = ? AND teacher_id = ?", (d, tid))
            db.execute("""DELETE FROM substitutions WHERE date = ? AND EXISTS (
                            SELECT 1 FROM timetable t JOIN assignments a ON a.id = t.assignment_id
                            WHERE a.teacher_id = ? AND t.day = ? AND t.department_id = substitutions.department_id
                            AND t.year_no = substitutions.year_no AND t.section = substitutions.section
                            AND t.period_no = substitutions.period_no)""", (d, tid, day))
        flash("Absence removed.", "success")
    db.close()
    return redirect(url_for("dashboard", date=d))


def _class_from_form(f):
    return to_int(f.get("dept_id")), to_int(f.get("year_no")), clean(f.get("section")), to_int(f.get("period"))


@app.route("/substitute", methods=["POST"])
@hod_required
def substitute():
    f = request.form
    d = _iso(f.get("date"))
    when = read_date(d)
    dept_id, year_no, section, period = _class_from_form(f)
    tid = to_int(f.get("teacher_id"))
    db = get_db()
    entries, absent = day_entries(db, when)
    target = next((e for e in entries if (e["dept_id"], e["year_no"], e["section"], e["period"]) ==
                   (dept_id, year_no, section, period) and e["status"] == "uncovered"), None)
    options, _, _ = free_pool(db, when, entries, absent, load_teachers(db))
    if not target or tid not in {t["id"] for t in options(target)}:
        flash("That teacher cannot cover this period (busy, absent, unavailable or at their daily limit).", "error")
    else:
        db.execute("INSERT OR REPLACE INTO substitutions (date, department_id, year_no, section, period_no, teacher_id) "
                   "VALUES (?,?,?,?,?,?)", (d, dept_id, year_no, section, period, tid))
        db.commit()
        flash("Cover assigned.", "success")
    db.close()
    return redirect(url_for("dashboard", date=d))


@app.route("/substitute/remove", methods=["POST"])
@hod_required
def substitute_remove():
    f = request.form
    d = _iso(f.get("date"))
    dept_id, year_no, section, period = _class_from_form(f)
    db = get_db()
    if dept_id and own_department(db, dept_id):
        db.execute("DELETE FROM substitutions WHERE date=? AND department_id=? AND year_no=? AND section=? AND period_no=?",
                   (d, dept_id, year_no, section, period))
        db.commit()
    db.close()
    return redirect(url_for("dashboard", date=d))


@app.route("/substitute/auto", methods=["POST"])
@hod_required
def substitute_auto():
    d = _iso(request.form.get("date"))
    when = read_date(d)
    db = get_db()
    people = load_teachers(db)
    entries, absent = day_entries(db, when)
    options, busy, load = free_pool(db, when, entries, absent, people)
    extra_busy, extra_load, done, left = set(), {}, 0, 0
    todo = [e for e in entries if e["status"] == "uncovered"]
    todo.sort(key=lambda e: len(options(e)))
    with db:
        for e in todo:
            picks = options(e, extra_busy, extra_load)
            if not picks:
                left += 1
                continue
            t = picks[0]
            db.execute("INSERT OR REPLACE INTO substitutions (date, department_id, year_no, section, period_no, teacher_id) "
                       "VALUES (?,?,?,?,?,?)", (d, e["dept_id"], e["year_no"], e["section"], e["period"], t["id"]))
            extra_busy.add((t["id"], e["period"]))
            extra_load[t["id"]] = extra_load.get(t["id"], 0) + 1
            done += 1
    db.close()
    flash("Assigned cover for %d period(s)%s." % (done, ", %d still need someone" % left if left else ""),
          "error" if left else "success")
    return redirect(url_for("dashboard", date=d))


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
