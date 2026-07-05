import asyncio
from bson import ObjectId
from datetime import datetime
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import io
import os  # 👈 ENVIRONMENT VARIABLES UTALNE KE LIYE ADD KIYA
import httpx  # 👈 KEEP-ALIVE PING SYSTEM KE LIYE ADD KIYA
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from typing import List, Dict, Optional
import uvicorn

# ==========================================
# 💤 RENDER NO-SLEEP / KEEP-ALIVE SYSTEM
# ==========================================
async def keep_alive_ping():
    """Ye loop har 10 minute mein aapke Render URL ko khud hi ping karega taaki server sleep na ho"""
    await asyncio.sleep(30)  # Server start hone ke 30 second baad pehla ping karega
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # Render deploy hone ke baad automatic apna external URL utha lega
                RENDER_APP_URL = os.getenv("RENDER_EXTERNAL_URL")
                if RENDER_APP_URL:
                    ping_url = f"{RENDER_APP_URL}/api/meta/cities"
                    response = await client.get(ping_url)
                    print(f"⏰ Keep-Alive Ping Success! Status: {response.status_code}")
                else:
                    print(
                        "⏰ Keep-Alive Pending: Waiting for Render URL environment variable..."
                    )
            except Exception as e:
                print(f"🚨 Keep-Alive Ping Failed: {e}")

            await asyncio.sleep(600)  # 600 seconds = 10 Minutes ke loop par chalega


from contextlib import asynccontextmanager  # 👈 LIFESPAN KE LIYE IMPORTS


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Server chalte hi background mein keep-alive loop shuru ho jayega
    asyncio.create_task(keep_alive_ping())
    yield


# FastAPI initialization with Lifespan & No-Sleep Hack
app = FastAPI(
    title="Hospital Mega App - Optimized", version="3.2.0", lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 🔌 MONGO DB ATLAS CONNECTION (FINAL URL)
# ==========================================
# Render par MONGO_URL set karenge toh wahan se chalega, nahi toh is final Atlas string se connect hoga
MONGO_DETAILS = os.getenv(
    "MONGO_URL",
    "mongodb+srv://25cs3047_db_user:Praveen%40123@cluster0.jp2ljhw.mongodb.net/HospitalDB?retryWrites=true&w=majority&appName=Cluster0",
)
client = AsyncIOMotorClient(MONGO_DETAILS)
db = client.HospitalDB  # 👈 APNA DATABASE NAME CHOOSE KIYA


# --- HELPER FUNCTIONS FOR TIMING VALIDATION ---
def parse_time_str(t_str: str):
    """'9AM' ya '1PM' ya '11:30AM' ko datetime.time object mein badalta h"""
    t_str = t_str.strip().upper().replace(" ", "")
    for fmt in ("%I%p", "%I:%M%p"):
        try:
            return datetime.strptime(t_str, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid time format: {t_str}")


def get_doctor_shift_bounds(opd_timing_dict: dict):
    """Aaj ke din ke liye doctor ka start_time aur end_time nikalta hai"""
    now = datetime.now()
    day_name = now.strftime("%A").lower()  # e.g., monday, tuesday
    shift_str = opd_timing_dict.get(day_name, "Closed")

    if "closed" in shift_str.lower() or not shift_str.strip():
        return None, None

    try:
        start_str, end_str = shift_str.split("-")
        start_time = parse_time_str(start_str)
        end_time = parse_time_str(end_str)
        return start_time, end_time
    except Exception:
        return None, None


def is_booking_allowed(opd_timing_dict: dict) -> bool:
    """Check karta h ki shift khatam toh nahi hui. End time se pehle booking allowed h."""
    start_time, end_time = get_doctor_shift_bounds(opd_timing_dict)
    if not end_time:
        return False  # Agar aaj closed hai toh booking nahi hogi

    current_time = datetime.now().time()
    # Shift ke end time se pehle kabhi bhi book karo (Subah jaldi bhi allowed hai)
    return current_time <= end_time


def is_reception_allowed(opd_timing_dict: dict) -> bool:
    """Reception dashboard strictly shift hours ke andar hi chalega"""
    start_time, end_time = get_doctor_shift_bounds(opd_timing_dict)
    if not start_time or not end_time:
        return False

    current_time = datetime.now().time()
    return start_time <= current_time <= end_time


# --- PYDANTIC SCHEMAS ---
class WeeklyOPD(BaseModel):
    monday: str = "Closed"
    tuesday: str = "Closed"
    wednesday: str = "Closed"
    thursday: str = "Closed"
    friday: str = "Closed"
    saturday: str = "Closed"
    sunday: str = "Closed"


class HospitalModel(BaseModel):
    city: str
    name: str
    mobile: str
    password: str


class DoctorModel(BaseModel):
    hospital_name: str
    city: str
    name: str
    speciality: str
    avg_time_per_patient: int
    opd_timing: WeeklyOPD


class PatientModel(BaseModel):
    city: str
    hospital_name: str
    doctor_name: str
    patient_name: str
    age: int
    gender: str
    mobile: str
    status: Optional[str] = "Unpaid"


class AuthReceptionModel(BaseModel):
    city: str
    hospital_name: str
    password: str
    doctor_name: str


def fix_id(item):
    if item and "_id" in item:
        item["id"] = str(item["_id"])
        del item["_id"]
    return item


# --- API ENDPOINTS ---


@app.get("/api/meta/cities")
async def get_cities():
    return await db.hospitals.distinct("city")


@app.get("/api/meta/hospitals")
async def get_hospitals(city: str):
    hospitals = await db.hospitals.find({"city": city.lower()}).to_list(100)
    return [fix_id(h) for h in hospitals]


@app.get("/api/meta/doctors")
async def get_doctors(city: str, hospital_name: str):
    doctors = await db.doctors.find(
        {"city": city.lower(), "hospital_name": hospital_name}
    ).to_list(100)
    return [fix_id(d) for d in doctors]


@app.post("/api/hospital/register")
async def register_hospital(hospital: HospitalModel):
    hospital_dict = hospital.model_dump()
    hospital_dict["city"] = hospital_dict["city"].lower()
    existing = await db.hospitals.find_one({"mobile": hospital.mobile})
    if existing:
        raise HTTPException(status_code=400, detail="Mobile already registered!")
    result = await db.hospitals.insert_one(hospital_dict)
    return {"status": "success", "id": str(result.inserted_id)}


@app.post("/api/doctor/add")
async def add_doctor(doctor: DoctorModel):
    doc_dict = doctor.model_dump()
    doc_dict["city"] = doc_dict["city"].lower()
    await db.doctors.insert_one(doc_dict)
    return {"status": "success", "message": f"Doctor {doctor.name} added/updated!"}


@app.put("/api/doctor/update/{doctor_id}")
async def update_doctor(doctor_id: str, doctor: DoctorModel):
    doc_dict = doctor.model_dump()
    doc_dict["city"] = doc_dict["city"].lower()
    await db.doctors.update_one({"_id": ObjectId(doctor_id)}, {"$set": doc_dict})
    return {"status": "success", "message": "Doctor updated successfully!"}


@app.delete("/api/doctor/delete/{doctor_id}")
async def delete_doctor(doctor_id: str):
    await db.doctors.delete_one({"_id": ObjectId(doctor_id)})
    return {"status": "success", "message": "Doctor deleted successfully!"}


@app.post("/api/appointment/book")
async def book_token(patient: PatientModel):
    p_dict = patient.model_dump()
    p_dict["city"] = p_dict["city"].lower()

    now = datetime.now()
    today_date = now.strftime("%Y-%m-%d")
    p_dict["created_at"] = now.isoformat()
    p_dict["month_year"] = now.strftime("%Y-%m")
    p_dict["booking_date"] = today_date

    doctor = await db.doctors.find_one({
        "city": p_dict["city"],
        "hospital_name": p_dict["hospital_name"],
        "name": p_dict["doctor_name"],
    })
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor profile not found!")

    # ⏱️ CHANGE: Shift End Time Check (End time ke baad booking block)
    if not is_booking_allowed(doctor.get("opd_timing", {})):
        raise HTTPException(
            status_code=400,
            detail=f"Booking closed! Today's OPD shift for Dr. {p_dict['doctor_name']} has already ended.",
        )

    # 🛑 Rule Check: One token per mobile number per doctor per day
    existing_booking = await db.appointments.find_one({
        "mobile": p_dict["mobile"],
        "city": p_dict["city"],
        "hospital_name": p_dict["hospital_name"],
        "doctor_name": p_dict["doctor_name"],
        "booking_date": today_date,
    })
    if existing_booking:
        raise HTTPException(
            status_code=400,
            detail=f"A token (#{existing_booking['token_no']}) has already been booked today using this mobile number!",
        )

    # 🔄 CHANGE: Daily Reset Counter with Key Mapping
    base_key = (
        f"{p_dict['city']}_{p_dict['hospital_name']}_{p_dict['doctor_name']}"
    )
    daily_counter_key = f"{base_key}_{today_date}"

    em_check = await db.emergencies.find_one({"id": daily_counter_key})
    is_emergency = em_check.get("active", False) if em_check else False

    counter = await db.counters.find_one_and_update(
        {"id": daily_counter_key},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    token_no = counter["seq"]
    p_dict["token_no"] = token_no
    if not p_dict.get("status"):
        p_dict["status"] = "Unpaid"

    await db.appointments.insert_one(p_dict)

    active_token_doc = await db.active_tokens.find_one({"id": daily_counter_key})
    current_live = active_token_doc["current"] if active_token_doc else 1

    waiting_count = max(0, token_no - current_live)
    estimated_wait_mins = (
        "EMERGENCY"
        if is_emergency
        else (waiting_count * int(doctor.get("avg_time_per_patient", 5)))
    )

    hosp = await db.hospitals.find_one({
        "city": p_dict["city"],
        "name": p_dict["hospital_name"],
    })
    return {
        "status": "success",
        "patient_name": p_dict["patient_name"],
        "hospital_name": p_dict["hospital_name"],
        "doctor_name": p_dict["doctor_name"],
        "hospital_mobile": hosp.get("mobile", "N/A") if hosp else "N/A",
        "token_no": token_no,
        "live_ongoing": current_live,
        "wait_time_mins": estimated_wait_mins,
    }


@app.get("/api/patient/track/{mobile}")
async def track_patient(mobile: str):
    patient = await db.appointments.find_one({"mobile": mobile}, sort=[("_id", -1)])
    if not patient:
        raise HTTPException(status_code=404, detail="No active booking found!")

    booking_date = patient.get("booking_date", datetime.now().strftime("%Y-%m-%d"))
    daily_counter_key = f"{patient['city']}_{patient['hospital_name']}_{patient['doctor_name']}_{booking_date}"

    em_check = await db.emergencies.find_one({"id": daily_counter_key})
    is_emergency = em_check.get("active", False) if em_check else False

    doctor = await db.doctors.find_one({
        "city": patient["city"],
        "hospital_name": patient["hospital_name"],
        "name": patient["doctor_name"],
    })
    active_token_doc = await db.active_tokens.find_one({"id": daily_counter_key})
    current_live = active_token_doc["current"] if active_token_doc else 1

    waiting_count = max(0, patient["token_no"] - current_live)
    estimated_wait_mins = (
        "EMERGENCY"
        if is_emergency
        else (
            waiting_count
            * (int(doctor.get("avg_time_per_patient", 5)) if doctor else 5)
        )
    )

    hosp = await db.hospitals.find_one({
        "city": patient["city"],
        "name": patient["hospital_name"],
    })
    return {
        "status": "success",
        "patient_name": patient["patient_name"],
        "hospital_name": patient["hospital_name"],
        "doctor_name": patient["doctor_name"],
        "hospital_mobile": hosp.get("mobile", "N/A") if hosp else "N/A",
        "token_no": patient["token_no"],
        "live_ongoing": current_live,
        "wait_time_mins": estimated_wait_mins,
        "current_status": "🚨 EMERGENCY" if is_emergency else patient["status"],
    }


@app.post("/api/reception/load")
async def load_reception(auth: AuthReceptionModel):
    hosp = await db.hospitals.find_one({
        "city": auth.city.lower(),
        "name": auth.hospital_name,
    })
    if not hosp or hosp.get("password") != auth.password:
        raise HTTPException(status_code=401, detail="Invalid Credentials!")

    today_date = datetime.now().strftime("%Y-%m-%d")
    tokens = await db.appointments.find({
        "city": auth.city.lower(),
        "hospital_name": auth.hospital_name,
        "doctor_name": auth.doctor_name,
        "booking_date": today_date,
    }).to_list(500)

    daily_counter_key = (
        f"{auth.city.lower()}_{auth.hospital_name}_{auth.doctor_name}_{today_date}"
    )
    active_token_doc = await db.active_tokens.find_one({"id": daily_counter_key})
    current_live = active_token_doc["current"] if active_token_doc else 1

    em_check = await db.emergencies.find_one({"id": daily_counter_key})
    is_emergency = em_check.get("active", False) if em_check else False

    return {
        "live_ongoing": current_live,
        "is_emergency": is_emergency,
        "tokens": [fix_id(t) for t in tokens],
    }


@app.post("/api/reception/action")
async def reception_action(
    city: str,
    hospital_name: str,
    doctor_name: str,
    action: str,
    target_token: Optional[int] = None,
):
    doctor = await db.doctors.find_one({
        "city": city.lower(),
        "hospital_name": hospital_name,
        "name": doctor_name,
    })
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor profile mismatch.")

    # ⏱️ CHANGE: Reception Desk Security Shift Hours Constraint Lockdown
    if action in ["next", "emergency"]:
        if not is_reception_allowed(doctor.get("opd_timing", {})):
            raise HTTPException(
                status_code=400,
                detail="Action Denied! Reception engine commands can only be fired within the active OPD shift window.",
            )

    today_date = datetime.now().strftime("%Y-%m-%d")
    daily_counter_key = f"{city.lower()}_{hospital_name}_{doctor_name}_{today_date}"

    if action == "next":
        await db.emergencies.update_one(
            {"id": daily_counter_key}, {"$set": {"active": False}}, upsert=True
        )
        active_token_doc = await db.active_tokens.find_one({"id": daily_counter_key})
        start_search = (active_token_doc["current"] + 1) if active_token_doc else 1

        next_paid = await db.appointments.find_one(
            {
                "city": city.lower(),
                "hospital_name": hospital_name,
                "doctor_name": doctor_name,
                "token_no": {"$gte": start_search},
                "status": "Paid",
                "booking_date": today_date,
            },
            sort=[("token_no", 1)],
        )

        if not next_paid:
            raise HTTPException(
                status_code=400,
                detail="No upcoming PAID patients found in today's queue!",
            )

        await db.active_tokens.update_one(
            {"id": daily_counter_key},
            {"$set": {"current": next_paid["token_no"]}},
            upsert=True,
        )
        return {
            "status": "success",
            "live_ongoing": next_paid["token_no"],
            "message": f"Token #{next_paid['token_no']} Called!",
        }

    elif action == "emergency":
        await db.emergencies.update_one(
            {"id": daily_counter_key}, {"$set": {"active": True}}, upsert=True
        )
        return {"status": "success", "message": "Emergency Declared! Grid frozen."}

    elif action == "mark_paid" and target_token:
        await db.appointments.update_one(
            {
                "city": city.lower(),
                "hospital_name": hospital_name,
                "doctor_name": doctor_name,
                "token_no": target_token,
                "booking_date": today_date,
            },
            {"$set": {"status": "Paid"}},
        )
        return {
            "status": "success",
            "message": f"Token #{target_token} Status changed to PAID!",
        }


@app.get("/api/reception/download-pdf")
async def download_monthly_pdf(
    city: str, hospital_name: str, doctor_name: str, month: str
):
    query = {
        "city": city.lower(),
        "hospital_name": hospital_name,
        "doctor_name": doctor_name,
        "$or": [{"month_year": month}, {"created_at": {"$regex": f"^{month}"}}],
    }

    patients = await db.appointments.find(query).sort("token_no", 1).to_list(1000)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=30,
    )
    story = []

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Heading1"],
        fontSize=18,
        textColor=colors.HexColor("#1e3a8a"),
        spaceAfter=10,
    )
    meta_style = ParagraphStyle(
        "ReportMeta", parent=styles["Normal"], fontSize=10, spaceAfter=20, textColor=colors.gray
    )

    story.append(Paragraph(f"Monthly Patient Report - {hospital_name}", title_style))
    story.append(
        Paragraph(
            f"<b>City:</b> {city.upper()} | <b>Doctor:</b> {doctor_name} | <b>Target Month:</b> {month}",
            meta_style,
        )
    )
    story.append(Spacer(1, 10))

    table_data = [
        ["Token", "Patient Name", "Age/Gender", "Mobile No.", "Payment Status"]
    ]

    if not patients:
        table_data.append(["-", "No Patients Found For This Month", "-", "-", "-"])
    else:
        for p in patients:
            table_data.append([
                f"#{p.get('token_no', 'N/A')}",
                p.get("patient_name", "N/A"),
                f"{p.get('age', 'N/A')} / {p.get('gender', 'N/A')}",
                p.get("mobile", "N/A"),
                p.get("status", "Unpaid"),
            ])

    t = Table(table_data, colWidths=[60, 150, 100, 110, 110])
    t.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
            ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#f8fafc")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            (
                "ROWBACKGROUNDS",
                (0, 1),
                (-1, -1),
                [colors.white, colors.HexColor("#f1f5f9")],
            ),
        ])
    )

    story.append(t)
    doc.build(story)
    buffer.seek(0)

    filename = f"Report_{hospital_name.replace(' ', '_')}_{month}.pdf"
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)