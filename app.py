import os, uuid, datetime, secrets
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
TABLE_NAME = os.environ.get("MEDTRACK_TABLE", "MedTrack")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "")
USE_AWS = os.environ.get("MEDTRACK_USE_AWS", "false").lower() == "true"

class LocalTable:
    """Small DynamoDB-shaped store used until AWS integration is enabled."""

    def __init__(self):
        self.items = []

    def scan(self, **kwargs):
        start_key = kwargs.get("ExclusiveStartKey")
        items = self.items
        if start_key:
            start_index = next(
                (index for index, item in enumerate(items)
                 if item.get("PK") == start_key.get("PK")
                 and item.get("SK") == start_key.get("SK")),
                -1,
            )
            items = items[start_index + 1:]
        return {"Items": list(items)}

    def put_item(self, Item):
        self.items = [item for item in self.items
                      if item.get("PK") != Item.get("PK")
                      or item.get("SK") != Item.get("SK")]
        self.items.append(Item.copy())

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, **kwargs):
        item = next((item for item in self.items
                     if item.get("PK") == Key.get("PK")
                     and item.get("SK") == Key.get("SK")), None)
        if item is None:
            return
        for placeholder, value in ExpressionAttributeValues.items():
            attribute = {":s": "Status", ":h": "StatusHistory", ":u": "UpdatedAt",
                         ":r": "Report", ":d": "Date"}.get(placeholder)
            if attribute:
                item[attribute] = value


if USE_AWS:
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = dynamodb.Table(TABLE_NAME)
    sns = boto3.client("sns", region_name=AWS_REGION)
    ses = boto3.client("ses", region_name=AWS_REGION)
else:
    table = LocalTable()
    sns = None
    ses = None

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def entity_id(prefix):
    return f"{prefix}#{uuid.uuid4().hex[:10]}"

def scan_all():
    items = []
    kwargs = {}
    while True:
        result = table.scan(**kwargs)
        items.extend(result.get("Items", []))
        if "LastEvaluatedKey" not in result:
            break
        kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]
    return items

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first.")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def role_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if session.get("role") not in roles:
                flash("You are not authorized for this action.")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator

@app.errorhandler(NoCredentialsError)
def handle_missing_aws_credentials(error):
    flash("AWS credentials are not configured. Configure AWS credentials before using MedTrack.")
    destination = "register" if request.endpoint == "register" else "login"
    return redirect(url_for(destination))

def find_user_by_id(user_id, items=None):
    items = items if items is not None else scan_all()
    return next((x for x in items if x.get("Entity") == "Users" and x.get("UserID") == user_id), None)

def find_profile(profile_id, entity, items=None):
    items = items if items is not None else scan_all()
    return next((x for x in items if x.get("Entity") == entity and
                 x.get("DoctorID" if entity == "Doctors" else "PatientID") == profile_id), None)

def send_email(to_email, subject, body):
    if not USE_AWS or not to_email or not FROM_EMAIL:
        return False
    try:
        ses.send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}}
            }
        )
        return True
    except ClientError as e:
        print("SES error:", e)
        return False

def publish_notification(message, recipient_user_ids=None, items=None):
    """Store in-app notifications and optionally publish through SNS."""
    items = items if items is not None else scan_all()
    recipients = set(recipient_user_ids or [])
    if not recipients and session.get("user_id"):
        recipients.add(session["user_id"])

    for user_id in recipients:
        table.put_item(Item={
            "PK": entity_id("NOTIF"), "SK": "DETAIL", "Entity": "Notifications",
            "NotificationID": entity_id("NOTIF"), "UserID": user_id,
            "Message": message, "Timestamp": now()
        })

    if USE_AWS and SNS_TOPIC_ARN:
        try:
            sns.publish(TopicArn=SNS_TOPIC_ARN, Message=message, Subject="MedTrack Notification")
        except ClientError as e:
            print("SNS error:", e)

@app.route("/")
def index():
    return render_template("index.html", user=session.get("user"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        role = request.form.get("role", "").strip().upper()
        password = request.form.get("password", "")

        if not name or not email or not phone or len(password) < 8:
            flash("Name, email, phone, and a password of at least 8 characters are required.")
            return render_template("register.html")
        if role not in {"DOCTOR", "PATIENT"}:
            flash("Invalid role.")
            return render_template("register.html")
        if role == "DOCTOR" and not request.form.get("specialization", "").strip():
            flash("Doctor specialization is required.")
            return render_template("register.html")
        if role == "DOCTOR" and not request.form.get("qualification", "").strip():
            flash("Doctor qualification is required.")
            return render_template("register.html")

        existing = next((x for x in scan_all()
                         if x.get("Entity") == "Users" and x.get("Email") == email), None)
        if existing:
            flash("An account with this email already exists.")
            return render_template("register.html")

        user_id = entity_id("USER")
        table.put_item(Item={
            "PK": user_id, "SK": "PROFILE", "Entity": "Users", "UserID": user_id,
            "Name": name, "Email": email, "Role": role,
            "PasswordHash": generate_password_hash(password), "Phone": phone,
            "Status": "ACTIVE", "CreatedAt": now()
        })

        if role == "DOCTOR":
            table.put_item(Item={
                "PK": entity_id("DOC"), "SK": "PROFILE", "Entity": "Doctors",
                "DoctorID": entity_id("DOC"), "UserID": user_id, "Name": name,
                "Specialization": request.form.get("specialization", "").strip(),
                "Qualification": request.form.get("qualification", "").strip(),
                "Experience": request.form.get("experience", "0").strip()
            })
        else:
            table.put_item(Item={
                "PK": entity_id("PAT"), "SK": "PROFILE", "Entity": "Patients",
                "PatientID": entity_id("PAT"), "UserID": user_id, "Name": name,
                "Age": request.form.get("age", "0"),
                "MedicalHistory": request.form.get("medical_history", "None")
            })

        flash("Registration successful. Please login.")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = next((x for x in scan_all()
                     if x.get("Entity") == "Users" and x.get("Email") == email), None)
        if user and user.get("Status", "ACTIVE") != "ACTIVE":
            flash("Your account is not active.")
        elif user and check_password_hash(user.get("PasswordHash", ""), password):
            session["user_id"] = user["UserID"]
            session["role"] = user["Role"]
            session["email"] = user["Email"]
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid email or password.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    items = scan_all()
    doctors = [x for x in items if x.get("Entity") == "Doctors"]
    patients = [x for x in items if x.get("Entity") == "Patients"]
    appointments = [x for x in items if x.get("Entity") == "Appointments"]
    diagnoses = [x for x in items if x.get("Entity") == "Diagnosis"]
    notifications = [x for x in items if x.get("Entity") == "Notifications"
                     and x.get("UserID") == session["user_id"]]

    current_user = find_user_by_id(session["user_id"], items)
    if not current_user:
        session.clear()
        flash("Your account could not be found.")
        return redirect(url_for("login"))
    if session.get("role") == "DOCTOR":
        doctor = next((d for d in doctors if d.get("UserID") == current_user["UserID"]), None)
        doctor_id = doctor.get("DoctorID") if doctor else ""
        my_appointments = [a for a in appointments if a.get("DoctorID") == doctor_id]
        assigned_patient_ids = {a.get("PatientID") for a in my_appointments}
        patients = [p for p in patients if p.get("PatientID") in assigned_patient_ids]
        return render_template("doctor_dashboard.html", doctor=doctor,
                               appointments=my_appointments, patients=patients,
                               diagnosis_appointments=my_appointments,
                               patient_map={p["PatientID"]: p for p in patients},
                               diagnoses=diagnoses, notifications=notifications,
                               search_term="", user=current_user)

    patient = next((p for p in patients if p.get("UserID") == current_user["UserID"]), None)
    patient_id = patient.get("PatientID") if patient else ""
    my_appointments = [a for a in appointments if a.get("PatientID") == patient_id]
    # Patients can still see all doctors in the booking dropdown.
    return render_template("dashboard.html", doctors=doctors, patients=patients,
                           appointments=my_appointments, diagnoses=diagnoses,
                           notifications=notifications, user=current_user,
                           patient=patient,
                           doctor_map={d["DoctorID"]: d for d in doctors})

@app.route("/doctor/search")
@login_required
@role_required("DOCTOR")
def doctor_search():
    term = request.args.get("q", "").strip().lower()
    items = scan_all()
    doctor = next((d for d in items if d.get("Entity") == "Doctors" and
                   d.get("UserID") == session["user_id"]), None)
    doctor_id = doctor.get("DoctorID") if doctor else ""
    appointments = [a for a in items if a.get("Entity") == "Appointments"
                    and a.get("DoctorID") == doctor_id]
    diagnosis_appointments = list(appointments)
    patients = [x for x in items if x.get("Entity") == "Patients"
                and x.get("PatientID") in {a.get("PatientID") for a in appointments}]
    if term:
        appointments = [
            a for a in appointments
            if term in next((p.get("Name", "") for p in patients
                             if p.get("PatientID") == a.get("PatientID")), "").lower()
            or term in a.get("PatientID", "").lower()
            or term in a.get("Date", "").lower()
            or term in a.get("Status", "").lower()
        ]
    current_user = find_user_by_id(session["user_id"], items)
    diagnoses = [x for x in items if x.get("Entity") == "Diagnosis"]
    notifications = [x for x in items if x.get("Entity") == "Notifications"
                     and x.get("UserID") == session["user_id"]]
    return render_template("doctor_dashboard.html", doctor=doctor,
                           appointments=appointments, patients=patients,
                           diagnosis_appointments=diagnosis_appointments,
                           patient_map={p["PatientID"]: p for p in patients},
                           diagnoses=diagnoses, notifications=notifications,
                           search_term=term, user=current_user)

@app.route("/appointment", methods=["POST"])
@login_required
@role_required("PATIENT")
def appointment():
    items = scan_all()
    patient = next((p for p in items if p.get("Entity") == "Patients"
                    and p.get("UserID") == session["user_id"]), None)
    patient_id = patient.get("PatientID") if patient else ""
    doctor_id = request.form.get("doctor_id", "").strip()
    appointment_date = request.form.get("date", "").strip()
    appointment_time = request.form.get("time", "").strip()
    doctor = next((d for d in items if d.get("Entity") == "Doctors"
                   and d.get("DoctorID") == doctor_id), None)

    try:
        datetime.date.fromisoformat(appointment_date)
        datetime.time.fromisoformat(appointment_time)
    except ValueError:
        flash("Enter a valid appointment date and time.")
        return redirect(url_for("dashboard"))
    if not patient or not doctor:
        flash("Please select a valid doctor.")
        return redirect(url_for("dashboard"))

    appointment_id = entity_id("APT")
    item = {
        "PK": appointment_id, "SK": "DETAIL", "Entity": "Appointments",
        "AppointmentID": appointment_id, "PatientID": patient_id, "DoctorID": doctor_id,
        "Date": appointment_date, "Time": appointment_time, "Status": "PENDING",
        "StatusHistory": [{"Status": "PENDING", "Timestamp": now()}],
        "CreatedAt": now()
    }
    table.put_item(Item=item)

    doctor = find_profile(doctor_id, "Doctors", items)
    recipient_ids = []
    if patient and patient.get("UserID"):
        recipient_ids.append(patient["UserID"])
    if doctor and doctor.get("UserID"):
        recipient_ids.append(doctor["UserID"])

    msg = f"MedTrack appointment requested: {appointment_date} {appointment_time}"
    publish_notification(msg, recipient_ids, items)

    if patient:
        patient_user = find_user_by_id(patient["UserID"], items)
        if patient_user:
            send_email(patient_user.get("Email"), "MedTrack Appointment Booked", msg)
    if doctor:
        doctor_user = find_user_by_id(doctor["UserID"], items)
        if doctor_user:
            send_email(doctor_user.get("Email"), "New MedTrack Appointment", msg)
    if ADMIN_EMAIL:
        send_email(ADMIN_EMAIL, "New MedTrack Appointment", msg)
    flash("Appointment booked.")
    return redirect(url_for("dashboard"))

@app.route("/appointment/<appointment_id>/status", methods=["POST"])
@login_required
@role_required("DOCTOR")
def update_appointment_status(appointment_id):
    new_status = request.form.get("status", "").strip().upper()
    allowed = {"CONFIRMED", "COMPLETED", "CANCELED", "CANCELLED", "REJECTED"}
    if new_status not in allowed:
        flash("Invalid appointment status.")
        return redirect(url_for("dashboard"))

    items = scan_all()
    appointment_item = next((a for a in items if a.get("Entity") == "Appointments"
                             and a.get("AppointmentID") == appointment_id), None)
    doctor = next((d for d in items if d.get("Entity") == "Doctors"
                   and d.get("UserID") == session["user_id"]), None)
    if not appointment_item or not doctor or appointment_item.get("DoctorID") != doctor.get("DoctorID"):
        flash("Appointment not found or not assigned to you.")
        return redirect(url_for("dashboard"))

    history = appointment_item.get("StatusHistory", [])
    history.append({"Status": new_status, "Timestamp": now()})
    table.update_item(
        Key={"PK": appointment_item["PK"], "SK": appointment_item["SK"]},
        UpdateExpression="SET #s=:s, StatusHistory=:h, UpdatedAt=:u",
        ExpressionAttributeNames={"#s": "Status"},
        ExpressionAttributeValues={":s": new_status, ":h": history, ":u": now()}
    )

    patient = find_profile(appointment_item["PatientID"], "Patients", items)
    recipients = [patient["UserID"]] if patient and patient.get("UserID") else []
    msg = f"Appointment {appointment_id} status updated to {new_status}."
    publish_notification(msg, recipients + [session["user_id"]], items)

    if patient:
        patient_user = find_user_by_id(patient["UserID"], items)
        if patient_user:
            send_email(patient_user.get("Email"), "MedTrack Appointment Update", msg)
    if ADMIN_EMAIL:
        send_email(ADMIN_EMAIL, "MedTrack Appointment Status Update", msg)

    flash("Appointment status updated.")
    return redirect(url_for("dashboard"))

@app.route("/diagnosis", methods=["POST"])
@login_required
@role_required("DOCTOR")
def diagnosis():
    items = scan_all()
    appointment_id = request.form.get("appointment_id", "").strip()
    appointment_item = next((a for a in items if a.get("Entity") == "Appointments"
                             and a.get("AppointmentID") == appointment_id), None)
    doctor = next((d for d in items if d.get("Entity") == "Doctors"
                   and d.get("UserID") == session["user_id"]), None)
    if not appointment_item or not doctor or appointment_item.get("DoctorID") != doctor.get("DoctorID"):
        flash("Please select an existing appointment assigned to you.")
        return redirect(url_for("dashboard"))

    report = request.form.get("report", "").strip()
    if not report:
        flash("A diagnosis report is required.")
        return redirect(url_for("dashboard"))

    existing = next((d for d in items if d.get("Entity") == "Diagnosis"
                     and d.get("AppointmentID") == appointment_id), None)
    if existing:
        table.update_item(
            Key={"PK": existing["PK"], "SK": existing["SK"]},
            UpdateExpression="SET Report=:r, #d=:d",
            ExpressionAttributeNames={"#d": "Date"},
            ExpressionAttributeValues={":r": report, ":d": now()}
        )
    else:
        diagnosis_id = entity_id("DX")
        table.put_item(Item={
            "PK": diagnosis_id, "SK": "DETAIL", "Entity": "Diagnosis",
            "DiagnosisID": diagnosis_id, "AppointmentID": appointment_id,
            "DoctorID": appointment_item["DoctorID"], "PatientID": appointment_item["PatientID"],
            "Report": report, "Date": now()
        })

    flash("Diagnosis report saved.")
    return redirect(url_for("dashboard"))

@app.route("/health")
def health():
    return {"status": "ok", "project": "MedTrack"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
