import os
import glob
import time
import secrets
import datetime
import io
import csv
from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Column, Integer, String, DateTime, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# --- Database Setup (New file per re-host, old files archived) ---
os.makedirs("databases", exist_ok=True)

RUN_TIMESTAMP = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
SQLALCHEMY_DATABASE_URL = f"sqlite:///./databases/attendance_{RUN_TIMESTAMP}.db"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class AttendanceRecord(Base):
    __tablename__ = "attendance"
    id = Column(Integer, primary_key=True, index=True)
    roll_number = Column(String, index=True)
    session_id = Column(String, index=True)
    credential_id = Column(String, index=True, nullable=True) # Tracks device/fingerprint hardware ID
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Dynamic QR Attendance Tracker")

app.mount("/static", StaticFiles(directory="static"), name="static")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Dynamic Token Manager (15s interval) ---
class DynamicTokenManager:
    def __init__(self, interval: int = 15):
        self.interval = interval
        self._current_token = secrets.token_hex(8)
        self._last_rotation = time.time()

    def get_token(self) -> str:
        now = time.time()
        if now - self._last_rotation > self.interval:
            self._current_token = secrets.token_hex(8)
            self._last_rotation = now
        return self._current_token

    def verify(self, token: str) -> bool:
        return token == self._current_token

token_manager = DynamicTokenManager(interval=15)

# --- Pydantic Schemas ---
class AttendanceSubmit(BaseModel):
    roll_number: str
    token: str
    session_id: str
    credential_id: str = None

# --- API Endpoints ---

@app.get("/", response_class=HTMLResponse)
def teacher_dashboard():
    with open("static/index.html", "r") as f:
        return f.read()

@app.get("/student", response_class=HTMLResponse)
def student_portal():
    with open("static/student.html", "r") as f:
        return f.read()

@app.get("/archive", response_class=HTMLResponse)
def archive_page():
    with open("static/archive.html", "r") as f:
        return f.read()

@app.get("/api/get-token")
def get_dynamic_token():
    return {"token": token_manager.get_token()}

@app.post("/api/mark-attendance")
def mark_attendance(data: AttendanceSubmit, db: Session = Depends(get_db)):
    if not token_manager.verify(data.token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="QR code expired or invalid. Scan the latest code on display."
        )
    
    roll = data.roll_number.upper()
    
    # 1. Prevent duplicate roll number submission in the same session
    existing_roll = db.query(AttendanceRecord).filter(
        AttendanceRecord.roll_number == roll,
        AttendanceRecord.session_id == data.session_id
    ).first()
    
    if existing_roll:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Attendance already marked for roll number {roll} in session {data.session_id}."
        )

    # 2. Prevent the same device/fingerprint from submitting multiple different roll numbers
    if data.credential_id:
        existing_device = db.query(AttendanceRecord).filter(
            AttendanceRecord.credential_id == data.credential_id,
            AttendanceRecord.session_id == data.session_id
        ).first()

        if existing_device:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This device or fingerprint has already submitted attendance for this session."
            )

    # Save record with the credential_id
    record = AttendanceRecord(
        roll_number=roll, 
        session_id=data.session_id, 
        credential_id=data.credential_id
    )
    db.add(record)
    db.commit()
    return {"status": "success", "message": f"Attendance successfully marked for {roll}"}

@app.get("/api/records")
def get_records(session_id: str = Query(...), db: Session = Depends(get_db)):
    records = db.query(AttendanceRecord).filter(
        AttendanceRecord.session_id == session_id
    ).order_by(AttendanceRecord.timestamp.desc()).all()
    
    return [
        {"roll_number": r.roll_number, "timestamp": r.timestamp.strftime("%Y-%m-%d %H:%M:%S")}
        for r in records
    ]

@app.get("/api/past-sessions")
def get_past_sessions():
    sessions_data = []
    db_files = glob.glob("databases/*.db")
    
    for db_path in sorted(db_files, reverse=True):
        try:
            sub_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
            SubSession = sessionmaker(bind=sub_engine)
            sub_db = SubSession()
            records = sub_db.query(AttendanceRecord).all()
            sub_db.close()
            
            if records:
                session_map = {}
                for r in records:
                    if r.session_id not in session_map:
                        session_map[r.session_id] = []
                    session_map[r.session_id].append({
                        "roll_number": r.roll_number,
                        "timestamp": r.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                    })
                for sess_id, recs in session_map.items():
                    sessions_data.append({
                        "db_file": os.path.basename(db_path),
                        "session_id": sess_id,
                        "total_students": len(recs),
                        "records": recs
                    })
        except Exception as e:
            print(f"Error reading {db_path}: {e}")
            
    return sessions_data

@app.get("/api/export-csv")
def export_session_csv(session_id: str = Query(...), db: Session = Depends(get_db)):
    records = db.query(AttendanceRecord).filter(
        AttendanceRecord.session_id == session_id
    ).order_by(AttendanceRecord.timestamp.asc()).all()

    if not records:
        raise HTTPException(status_code=404, detail="No attendance records found for this session.")

    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow(["Sl No", "Roll Number", "Session Name", "Timestamp"])
    for idx, r in enumerate(records, start=1):
        writer.writerow([idx, r.roll_number, r.session_id, r.timestamp.strftime("%Y-%m-%d %H:%M:%S")])

    output.seek(0)
    filename = f"attendance_{session_id}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )