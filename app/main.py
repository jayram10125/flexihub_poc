import hashlib
import hmac
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from app import hardware
from app import storage

ROOT = Path(__file__).resolve().parent.parent
DB = storage.database_path(ROOT)
app = FastAPI(title="Portway USB control plane and relay", version="0.4.0")


@contextmanager
def db():
    connection = sqlite3.connect(str(DB), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@app.on_event("startup")
def init_db():
    storage.preserve_existing_database(ROOT, DB)
    DB.parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires_at INTEGER NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS machines(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT NOT NULL,os TEXT NOT NULL,kind TEXT NOT NULL,last_seen INTEGER NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,machine_id TEXT NOT NULL,name TEXT NOT NULL,vid TEXT,pid TEXT,serial_hash TEXT,kind TEXT NOT NULL,eligible INTEGER NOT NULL,last_seen INTEGER NOT NULL,FOREIGN KEY(machine_id) REFERENCES machines(id));
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,device_id TEXT NOT NULL,requester_id TEXT NOT NULL,state TEXT NOT NULL,created_at INTEGER NOT NULL,ended_at INTEGER,FOREIGN KEY(device_id) REFERENCES devices(id));
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_device ON sessions(device_id) WHERE ended_at IS NULL;
        CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,action TEXT NOT NULL,subject TEXT NOT NULL,created_at INTEGER NOT NULL);
        """)
        hardware.init(c)


class Credentials(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)


class MachineInput(BaseModel):
    id: str = Field(min_length=8, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    os: str = Field(max_length=100)


class DeviceInput(BaseModel):
    id: str = Field(min_length=8, max_length=150)
    name: str = Field(min_length=1, max_length=150)
    vid: Optional[str] = None
    pid: Optional[str] = None
    serial_hash: Optional[str] = None
    kind: str = "mass-storage"
    eligible: bool = True
    usbip_busid: Optional[str] = Field(default=None, pattern=r'^\d+-\d+(?:\.\d+)*$', max_length=32)


class HeartbeatInput(BaseModel):
    machine: MachineInput
    devices: List[DeviceInput] = Field(max_length=100)
    machine_key: Optional[str] = Field(default=None, min_length=32, max_length=128)
    host_ready: bool = False
    guest_ready: bool = False


class SessionInput(BaseModel):
    device_id: str
    requester_machine_id: str


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    result = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 300000)
    return salt.hex() + ":" + result.hex()


def issue_token(c, user_id):
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO tokens VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, int(time.time()) + 86400))
    return token


def current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail={"code": "AUTH_REQUIRED", "message": "Please sign in."})
    token_hash = hashlib.sha256(authorization[7:].encode()).hexdigest()
    with db() as c:
        row = c.execute("SELECT users.id,users.email FROM tokens JOIN users ON tokens.user_id=users.id WHERE tokens.hash=? AND tokens.expires_at>?", (token_hash, int(time.time()))).fetchone()
    if not row:
        raise HTTPException(401, detail={"code": "TOKEN_EXPIRED", "message": "Please sign in again."})
    return dict(row)


@app.post("/api/auth/register")
def register(body: Credentials):
    email = body.email.strip().lower()
    if "@" not in email or len(email) > 254:
        raise HTTPException(422, detail={"code": "INVALID_EMAIL", "message": "Enter a valid email."})
    with db() as c:
        try:
            user_id = str(uuid.uuid4())
            c.execute("INSERT INTO users VALUES(?,?,?,?)", (user_id, email, password_hash(body.password), int(time.time())))
        except sqlite3.IntegrityError:
            raise HTTPException(409, detail={"code": "EMAIL_EXISTS", "message": "Account already exists."})
        token = issue_token(c, user_id)
    return {"token": token, "user": {"id": user_id, "email": email}}


@app.post("/api/auth/login")
def login(body: Credentials):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (body.email.strip().lower(),)).fetchone()
        valid = False
        if row:
            salt, expected = row["password_hash"].split(":")
            valid = hmac.compare_digest(password_hash(body.password, bytes.fromhex(salt)).split(":")[1], expected)
        if not valid:
            raise HTTPException(401, detail={"code": "INVALID_CREDENTIALS", "message": "Email or password is incorrect."})
        token = issue_token(c, row["id"])
    return {"token": token, "user": {"id": row["id"], "email": row["email"]}}


@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(None), user=Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM tokens WHERE hash=?", (hashlib.sha256(authorization[7:].encode()).hexdigest(),))
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


@app.get('/api/capabilities')
def capabilities():
    return {'version': '0.4.0', 'agent_protocol': 1, 'transport': 'authenticated-wss-relay',
            'required_workers': 1, 'hardware_acceptance': 'pending-two-physical-computers',
            'storage_ready': storage.hardware_storage_error(DB) is None}


@app.get('/healthz')
def health():
    with db() as c:
        c.execute('SELECT 1').fetchone()
    return {'status': 'ok', 'usb_storage_ready': storage.hardware_storage_error(DB) is None}


@app.post("/api/machines/heartbeat")
def heartbeat(body: HeartbeatInput, user=Depends(current_user)):
    now = int(time.time())
    with db() as c:
        existing = c.execute("SELECT user_id,kind FROM machines WHERE id=?", (body.machine.id,)).fetchone()
        if existing and (existing["user_id"] != user["id"] or existing["kind"] != "connector"):
            raise HTTPException(403, detail={"code": "MACHINE_OWNERSHIP", "message": "Machine ID belongs to another account."})
        c.execute("INSERT INTO machines VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,os=excluded.os,last_seen=excluded.last_seen", (body.machine.id, user["id"], body.machine.name, body.machine.os, "connector", now))
        hardware.runtime_heartbeat(c, body)
        ids = []
        for item in body.devices:
            # Windows PnP IDs are not globally unique. For example, two PCs may
            # expose a USB Root Hub with the same reported ID. Scope each device
            # to its registered connector before storing or returning it.
            device_id = "{}:{}".format(body.machine.id, item.id)
            ids.append(device_id)
            c.execute("INSERT INTO devices(id,machine_id,name,vid,pid,serial_hash,kind,eligible,last_seen,usbip_busid) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,vid=excluded.vid,pid=excluded.pid,serial_hash=excluded.serial_hash,kind=excluded.kind,eligible=excluded.eligible,last_seen=excluded.last_seen,usbip_busid=excluded.usbip_busid", (device_id, body.machine.id, item.name, item.vid, item.pid, item.serial_hash, item.kind, int(item.eligible), now, item.usbip_busid))
        if ids:
            c.execute("UPDATE devices SET last_seen=0 WHERE machine_id=? AND id NOT IN ({})".format(",".join("?" for _ in ids)), [body.machine.id] + ids)
        else:
            c.execute("UPDATE devices SET last_seen=0 WHERE machine_id=?", (body.machine.id,))
    return {"ok": True, "reported": len(ids)}


@app.get("/api/overview")
def overview(user=Depends(current_user)):
    now = int(time.time())
    with db() as c:
        machines = [dict(r) for r in c.execute("SELECT id,name,os,kind,last_seen FROM machines WHERE user_id=? ORDER BY name", (user["id"],))]
        devices = [dict(r) for r in c.execute("SELECT d.id,d.machine_id,d.name,d.vid,d.pid,d.kind,d.eligible,d.last_seen,d.usbip_busid,s.id AS session_id,s.state AS session_state,s.requester_id,h.error AS session_error FROM devices d JOIN machines m ON d.machine_id=m.id LEFT JOIN sessions s ON s.device_id=d.id AND s.ended_at IS NULL LEFT JOIN hardware_sessions h ON h.id=s.id WHERE m.user_id=? ORDER BY d.name", (user["id"],))]
    for m in machines:
        m["online"] = m["kind"] == "demo" or now - m["last_seen"] < 45
    machine_map = {m["id"]: m for m in machines}
    for d in devices:
        host = machine_map[d["machine_id"]]
        d['status'] = d['session_state'] if d['session_id'] else ('offline' if not host['online'] or (d['kind'] != 'demo' and now - d['last_seen'] >= 45) else 'available')
    return {"user": user, "machines": machines, "devices": devices, "driver_ready": False, "mode": "usbip-agent"}


@app.post("/api/demo")
def demo(user=Depends(current_user)):
    now = int(time.time())
    uid = user["id"]
    a, b, device = ["demo-" + hashlib.sha256((uid + part).encode()).hexdigest()[:16] for part in ("a", "b", "usb")]
    with db() as c:
        for mid, name in ((a, "Studio Workstation"), (b, "Travel Laptop")):
            c.execute("INSERT INTO machines VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen", (mid, uid, name, "Windows 11 · Demo", "demo", now))
        c.execute("INSERT INTO devices(id,machine_id,name,vid,pid,serial_hash,kind,eligible,last_seen) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen", (device, a, "SanDisk Cruzer Blade", "0781", "5567", None, "demo", 1, now))
    return {"requester_machine_id": b, "device_id": device}


@app.post("/api/sessions")
def connect(body: SessionInput, user=Depends(current_user)):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        device = c.execute("SELECT d.*,m.user_id,m.kind AS machine_kind,m.last_seen AS host_seen FROM devices d JOIN machines m ON m.id=d.machine_id WHERE d.id=?", (body.device_id,)).fetchone()
        requester = c.execute("SELECT * FROM machines WHERE id=? AND user_id=?", (body.requester_machine_id, user["id"])).fetchone()
        if not device or device["user_id"] != user["id"] or not requester:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Device or requesting computer not found."})
        if device["machine_id"] == requester["id"]:
            raise HTTPException(409, detail={"code": "LOCAL_DEVICE", "message": "Choose a different computer."})
        if not device["eligible"]:
            raise HTTPException(409, detail={"code": "DEVICE_INELIGIBLE", "message": "This device class is not supported."})
        if (device["machine_kind"] != "demo" and int(time.time()) - device["host_seen"] >= 45) or (requester["kind"] != "demo" and int(time.time()) - requester["last_seen"] >= 45):
            raise HTTPException(409, detail={"code": "MACHINE_OFFLINE", "message": "Both computers must be online."})
        if device["kind"] != "demo" or requester["kind"] != "demo":
            return hardware.reserve(c, device, requester, user, DB)
        sid = str(uuid.uuid4())
        try:
            c.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", (sid, user["id"], device["id"], requester["id"], "demo-connected", int(time.time()), None))
        except sqlite3.IntegrityError:
            raise HTTPException(409, detail={"code": "DEVICE_BUSY", "message": "This device is already reserved."})
        c.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (str(uuid.uuid4()), user["id"], "demo.connect", sid, int(time.time())))
    return {"id": sid, "state": "demo-connected"}


@app.post("/api/sessions/{session_id}/disconnect")
def disconnect(session_id: str, user=Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT id,state FROM sessions WHERE id=? AND user_id=? AND ended_at IS NULL", (session_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(404, detail={"code": "SESSION_NOT_FOUND", "message": "Active session not found."})
        if c.execute('SELECT 1 FROM hardware_sessions WHERE id=?', (session_id,)).fetchone():
            if row['state'] not in ('host-releasing', 'recovery-required'):
                c.execute("UPDATE sessions SET state='disconnecting' WHERE id=?", (session_id,))
            return {'state': 'disconnecting'}
        c.execute("UPDATE sessions SET state='released',ended_at=? WHERE id=?", (int(time.time()), session_id))
        c.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (str(uuid.uuid4()), user["id"], "demo.disconnect", session_id, int(time.time())))
    return {"state": "released"}


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/app.css")
def css():
    return FileResponse(ROOT / "web" / "app.css", media_type="text/css")


@app.get("/app.js")
def js():
    return FileResponse(ROOT / "web" / "app.js", media_type="application/javascript")


@app.get("/connector.py")
def connector_download():
    return FileResponse(ROOT / "connector" / "connector.py", media_type="text/x-python", filename="portway-connector-prototype.py")


@app.get("/download/connector")
def connector_exe_download():
    binary = ROOT / "dist" / "PortwayConnectorSetup.exe"
    if not binary.exists():
        raise HTTPException(503, detail={"code": "CONNECTOR_BUILD_MISSING", "message": "Build the Windows connector executable before offering a download."})
    return FileResponse(binary, media_type="application/vnd.microsoft.portable-executable", filename="PortwayConnectorSetup.exe")


hardware.install(app, db, current_user)
