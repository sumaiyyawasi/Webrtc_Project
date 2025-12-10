import os
import json
import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from pydantic import BaseModel
from dotenv import load_dotenv
from jose import jwt, JWTError
from typing import Dict, Set

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")
ALGORITHM = "HS256"
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")

app = FastAPI(title="WebRTC Whiteboard")
app.mount("/static", StaticFiles(directory="static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

mongo_client = MongoClient(MONGO_URL)
db = mongo_client["webrtc_whiteboard"]
whiteboards = db["whiteboards"]
users = db["users"]  # Collection for user data

sessions: Dict[str, Set[WebSocket]] = {}

def create_access_token(data: dict, expires_minutes: int = 60):
    to_encode = data.copy()
    expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=expires_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        session_name = payload.get("sub")
        if not session_name:
            raise HTTPException(status_code=403, detail="Invalid token")
        return session_name
    except JWTError:
        raise HTTPException(status_code=403, detail="Could not validate token")

class UserCreate(BaseModel):
    username: str
    password: str

@app.post("/auth/register")
async def register_user(user: UserCreate):
    existing_user = users.find_one({"username": user.username})
    if existing_user:
        return {"status": "error", "detail": "Username already exists"}  # No alert for frontend
    users.insert_one({"username": user.username, "password": user.password})
    return {"status": "success", "detail": "User registered"}

@app.post("/auth/login")
async def login_user(user: UserCreate):
    db_user = users.find_one({"username": user.username, "password": user.password})
    if not db_user:
        return {"status": "error", "detail": "Invalid credentials"}
    token = create_access_token({"sub": user.username})
    return {"status": "success", "access_token": token, "token_type": "bearer"}

@app.get("/", response_class=HTMLResponse)
async def get_home():
    with open("static/client.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

class SessionCreate(BaseModel):
    session_name: str

@app.post("/sessions/create")
async def create_session(data: SessionCreate):
    session = whiteboards.find_one({"session_name": data.session_name})
    if session:
        raise HTTPException(status_code=400, detail="Session already exists")
    whiteboards.insert_one({
        "session_name": data.session_name,
        "canvas_state": [],
        "created_at": datetime.datetime.utcnow()
    })
    token = create_access_token({"sub": data.session_name})
    return {"status": "success", "session_name": data.session_name, "token": token}

class CanvasSave(BaseModel):
    session_name: str
    canvas_data: list

@app.post("/sessions/save")
async def save_canvas(data: CanvasSave, token: str = Depends(verify_token)):
    whiteboards.update_one(
        {"session_name": data.session_name},
        {"$set": {"canvas_state": data.canvas_data}},
        upsert=True
    )
    return {"status": "success"}

@app.get("/sessions/{session_name}")
async def load_session(session_name: str):
    session = whiteboards.find_one({"session_name": session_name})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_name": session_name, "canvas_state": session["canvas_state"]}

@app.websocket("/ws/{session_name}")
async def websocket_endpoint(websocket: WebSocket, session_name: str):
    await websocket.accept()
    if session_name not in sessions:
        sessions[session_name] = set()
    sessions[session_name].add(websocket)
    print(f"✅ Client joined session: {session_name}")

    session = whiteboards.find_one({"session_name": session_name})
    if session and session.get("canvas_state"):
        await websocket.send_json({"type": "load", "canvas_state": session["canvas_state"]})

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            for client in sessions[session_name]:
                if client != websocket:
                    await client.send_text(data)
    except WebSocketDisconnect:
        sessions[session_name].remove(websocket)
        print(f"❌ Client left session: {session_name}")
