import os
import io
import uuid
import json
import re
import asyncio
import hashlib
from datetime import datetime
from typing import Dict, List, Any, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import google.generativeai as genai
from motor.motor_asyncio import AsyncIOMotorClient
from pypdf import PdfReader

# --- HIGH-AVAILABILITY CLOUD STORAGE CONNECTION POOLING ---
class DatabaseEngine:
    def __init__(self):
        self.client: Optional[AsyncIOMotorClient] = None
        self.db = None

    def establish_pool(self):
        mongo_uri = os.getenv("MDB_MCP_CONNECTION_STRING")
        if not mongo_uri:
            raise RuntimeError("CRITICAL ENVIRONMENT ERROR: MDB_MCP_CONNECTION_STRING is missing.")
        self.client = AsyncIOMotorClient(mongo_uri, maxPoolSize=50, minPoolSize=10)
        self.db = self.client["Nexa_Workspace"]

    def drain_pool(self):
        if self.client:
            self.client.close()

db_engine = DatabaseEngine()

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_engine.establish_pool()
    yield
    db_engine.drain_pool()

app = FastAPI(title="Nexa Master Production Engine V3 Pro", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def get_secure_collection(collection_name: str):
    if db_engine.db is None:
        raise HTTPException(status_code=500, detail="Core Database Subsystem Offline.")
    return db_engine.db[collection_name]

# --- HARDENED PROTOCOL TYPE LAYOUT BOUNDARIES ---
class UserRegister(BaseModel):
    team_username: str = Field(..., min_length=3, max_length=30)
    password: str = Field(..., min_length=6)
    admin_token: str

class UserLogin(BaseModel):
    team_username: str
    password: str

class AgentPayload(BaseModel):
    team_id: str
    event_details: Dict[str, Any]
    team_members: List[Dict[str, Any]]
    current_inventory: List[Dict[str, Any]]

class CommunityQuery(BaseModel):
    scrapped_component_text: str
    target_mcu: str
    author_signature: str

class DeleteCommunityQuery(BaseModel):
    comment_id: str
    author_signature: str

class DiagnosticPayload(BaseModel):
    team_id: str
    module_name: str
    error_log_text: str
    symptom_description: str

def isolate_pdf_text_bytes(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        extracted = []
        for page in reader.pages[:5]:
            text = page.extract_text()
            if text:
                extracted.append(text)
        return "\n".join(extracted)
    except Exception as pdf_err:
        print(f"Non-fatal background reader warning: {str(pdf_err)}")
        return ""

async def call_gemini_async(model, prompt):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, model.generate_content, prompt)

# --- APPLICATION DISPATCH VECTOR ROUTERS ---
@app.get("/")
def read_root():
    return {"status": "healthy", "engine": "Nexa Production Ready V3 (Google Search Disabled)"}

@app.post("/auth/register")
async def register_team(payload: UserRegister):
    coll = await get_secure_collection("users")
    username = payload.team_username.strip().lower()
    if await coll.find_one({"team_username": username}):
        raise HTTPException(status_code=400, detail="Namespace claimed.")
    await coll.insert_one({"team_username": username, "password": payload.password, "admin_token": payload.admin_token.strip()})
    return {"success": True}

@app.post("/auth/login")
async def login_team(payload: UserLogin):
    coll = await get_secure_collection("users")
    username = payload.team_username.strip().lower()
    user = await coll.find_one({"team_username": username, "password": payload.password})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    return {"success": True, "team_id": username, "admin_token": user.get("admin_token", "")}

@app.get("/modules/{team_id}/{module_name}")
async def get_module_template(team_id: str, module_name: str):
    coll = await get_secure_collection("module_templates")
    clean_module = module_name.strip()
    safe_search_pattern = re.escape(clean_module)
    document = await coll.find_one({"team_id": team_id.lower(), "module_name": {"$regex": f"^{safe_search_pattern}$", "$options": "i"}})
    if not document:
        return {
            "team_id": team_id.lower(), "module_name": clean_module, "circuit_diagram_url": "",
            "specs": {"Chassis": "Unassigned", "Microcontroller": "Unassigned", "Motors": "Unassigned", "Motor Driver": "Unassigned", "Sensors": "Unassigned"},
            "budget": [], "firmware": "// No current firmware footprint synchronized."
        }
    document.pop("_id", None)
    return document

@app.post("/modules/add")
async def add_new_module(
    team_id: str = Form(...), module_name: str = Form(...), circuit_diagram_url: str = Form(...),
    chassis: str = Form(...), mcu: str = Form(...), motors: str = Form(...), drivers: str = Form(...),
    sensors: str = Form(...), firmware: str = Form(...), budget_json: str = Form(...), file: Optional[UploadFile] = File(None)
):
    coll = await get_secure_collection("module_templates")
    datasheet_text = ""
    if file and file.filename.endswith('.pdf'):
        file_bytes = await file.read()
        datasheet_text = await asyncio.to_thread(isolate_pdf_text_bytes, file_bytes)

    try: parsed_budget = json.loads(budget_json)
    except Exception: raise HTTPException(status_code=400, detail="Invalid budget serialization formatting layout.")

    clean_module_name = str(module_name).strip()
    safe_search_pattern = re.escape(clean_module_name)
    await coll.delete_many({"team_id": str(team_id).lower(), "module_name": {"$regex": f"^{safe_search_pattern}$", "$options": "i"}})
    
    payload = {
        "team_id": str(team_id).lower(), "module_name": clean_module_name, "circuit_diagram_url": str(circuit_diagram_url).strip(),
        "specs": {"Chassis": str(chassis).strip(), "Microcontroller": str(mcu).strip(), "Motors": str(motors).strip(), "Motor Driver": str(drivers).strip(), "Sensors": str(sensors).strip()},
        "budget": parsed_budget, "firmware": str(firmware), "datasheet_grounding_text": datasheet_text
    }
    await coll.insert_one(payload)
    return {"success": True}

@app.get("/agent/cached/{team_id}")
async def get_cached_sprint(team_id: str):
    coll = await get_secure_collection("sprint_history")
    record = await coll.find_one({"team_id": team_id.lower()})
    return {"found": bool(record), "sprint_plan": record.get("sprint_plan", "") if record else ""}

@app.post("/agent/sprint")
async def generate_agentic_sprint(payload: AgentPayload):
    gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("gemini_api_key")
    if not gemini_api_key: raise HTTPException(status_code=500, detail="Missing API token configuration.")
    try:
        coll = await get_secure_collection("module_templates")
        
        # Safe extraction handles both raw dictionaries and Pydantic object footprints
        event_details = payload.event_details
        if isinstance(event_details, dict):
            categories = event_details.get("categories", [])
        else:
            categories = getattr(event_details, "categories", [])

        known_blueprints = []
        for cat in categories:
            safe_cat = re.escape(cat.strip())
            bp = await coll.find_one({"team_id": payload.team_id.lower(), "module_name": {"$regex": f"^{safe_cat}$", "$options": "i"}})
            if bp:
                bp.pop("_id", None)
                known_blueprints.append(bp)

        known_blueprints_summary = str(known_blueprints) if known_blueprints else "Base evaluation on default safe generic mechatronics specs."
        
        # Synchronized config matches working HUD diagnose exactly (NO search tools to trigger authorization faults)
        genai.configure(api_key=gemini_api_key.strip())
        model = genai.GenerativeModel('gemini-2.5-flash')

        logistics_prompt = f"You are an AI Logistics Specialist. Calculate component gap deficits in PKR based on inventory: {str(payload.current_inventory)} and requirement blueprints: {known_blueprints_summary}"
        firmware_prompt = f"You are a Senior Drivetrain Firmware Engineer. Build exact microcontroller path solutions for these engineers: {str(payload.team_members)} using technical specs: {known_blueprints_summary}"

        logistics_task = call_gemini_async(model, logistics_prompt)
        firmware_task = call_gemini_async(model, firmware_prompt)
        logistics_response, firmware_response = await asyncio.gather(logistics_task, firmware_task)

        critic_validation_prompt = f"""
        You are the Lead Nexa Structural Architecture Reviewer. Synthesize logs into a single markdown report.
        [LOGISTICS INPUT]: {logistics_response.text}
        [FIRMWARE INPUT]: {firmware_response.text}
        CRITICAL PARSING CONDITION: Split response layout exactly using these exact anchor headers.
        === PROCUREMENT PROFILE ===
        === DEVELOPER SPRINT BLOCKS ===
        """
        final_verified_response = await call_gemini_async(model, critic_validation_prompt)
        sprint_content_text = final_verified_response.text

        history_coll = await get_secure_collection("sprint_history")
        await history_coll.delete_many({"team_id": payload.team_id.lower()})
        await history_coll.insert_one({"team_id": payload.team_id.lower(), "sprint_plan": sprint_content_text})
        return {"success": True, "sprint_plan": sprint_content_text}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/public/community/all")
async def get_community_ledger_posts():
    ledger_coll = await get_secure_collection("community_ledger")
    cursor = ledger_coll.find().sort("_id", -1)
    sanitized_posts = []
    async for document in cursor:
        document.pop("_id", None)
        document.pop("signature_hash", None)
        sanitized_posts.append(document)
    return sanitized_posts

@app.post("/public/community/resolve", response_model=dict)
async def public_community_resolve(payload: CommunityQuery):
    ledger_coll = await get_secure_collection("community_ledger")
    unique_id = str(uuid.uuid4())[:8]
    raw_sig = payload.author_signature.strip().lower()
    hashed_sig = hashlib.sha256(raw_sig.encode()).hexdigest()
    
    # 🧠 HIGH-INTELLIGENCE MULTI-KEYWORD TOKENIZER FALLBACK
    txt_lower = payload.scrapped_component_text.lower()
    detected_part = "Custom Workspace Module"
    pin_configuration = "VCC -> 5V, GND -> GND, Data Pin -> MCU Digital Pin 2"
    cpp_logic_array = "// Custom Component Handshake Initialization Core\nvoid setup() {\n  Serial.begin(9600);\n  pinMode(2, INPUT_PULLUP);\n}\nvoid loop() {\n  int componentState = digitalRead(2);\n  Serial.println(componentState);\n  delay(50);\n}"

    if any(x in txt_lower for x in ["bluetooth", "hc-05", "hc-06", "wireless", "serial"]):
        detected_part = "HC-05 Wireless Serial Bluetooth Transceiver Node"
        pin_configuration = "VCC -> 5V, GND -> GND, TXD -> MCU RX (Pin 10), RXD -> MCU TX (Pin 11 via Resistor Divider Matrix)"
        cpp_logic_array = "#include <SoftwareSerial.h>\nSoftwareSerial BTSerial(10, 11); // RX, TX Line Maps\nvoid setup() {\n  Serial.begin(9600);\n  BTSerial.begin(9600);\n}\nvoid loop() {\n  if (BTSerial.available()) Serial.write(BTSerial.read());\n  if (Serial.available()) BTSerial.write(Serial.read());\n}"
    elif any(x in txt_lower for x in ["motor", "driver", "l298n", "l293d", "h-bridge", "drivetrain"]):
        detected_part = "L298N Dual H-Bridge DC Motor Driver Controller Module"
        pin_configuration = "IN1 -> MCU Pin 4, IN2 -> MCU Pin 5, ENA -> MCU Pin 3 (PWM Velocity Control), GND -> Shared MCU Ground Platform"
        cpp_logic_array = "// Drivetrain Kinematics Vector Execution Loop\nvoid setup() {\n  pinMode(4, OUTPUT); pinMode(5, OUTPUT); pinMode(3, OUTPUT);\n}\nvoid loop() {\n  digitalWrite(4, HIGH); digitalWrite(5, LOW); // Forward Vector Pulse\n  analogWrite(3, 220); // 85% Power Speed Command\n}"
    elif any(x in txt_lower for x in ["sonar", "ultrasonic", "hc-sr04", "distance", "range"]):
        detected_part = "HC-SR04 Ultrasonic Distance Time-of-Flight Sonar Rangefinder"
        pin_configuration = "VCC -> 5V, GND -> GND, Trigger -> MCU Pin 7, Echo -> MCU Pin 6"
        cpp_logic_array = "// Radar Sonic Echo Range Calculation Block\nvoid setup() {\n  pinMode(7, OUTPUT); pinMode(6, INPUT); Serial.begin(9600);\n}\nvoid loop() {\n  digitalWrite(7, LOW); delayMicroseconds(2);\n  digitalWrite(7, HIGH); delayMicroseconds(10); digitalWrite(7, LOW);\n  long flightDuration = pulseIn(6, HIGH);\n  long computedDistanceCm = flightDuration * 0.034 / 2;\n  Serial.print(\"Computed Telemetry Range: \"); Serial.println(computedDistanceCm);\n  delay(100);\n}"
    elif any(x in txt_lower for x in ["lcd", "display", "screen", "i2c", "16x2"]):
        detected_part = "16x2 Character LCD Panel Display Layer with I2C Backpack"
        pin_configuration = "VCC -> 5V, GND -> GND, SDA -> MCU SDA (A4), SCL -> MCU SCL (A5)"
        cpp_logic_array = "#include <Wire.h>\n#include <LiquidCrystal_I2C.h>\nLiquidCrystal_I2C lcd(0x27, 16, 2); // Core I2C Hex Intercept\nvoid setup() {\n  lcd.init(); lcd.backlight();\n  lcd.setCursor(0, 0); lcd.print(\"NEXA CORESYNC V3\");\n}\nvoid loop() {}"

    block_start = "```cpp"
    block_end = "```"
    mentor_guidance_text = (
        f"### 🧠 Nexa Local Heuristic Engine Fallback Solution\n"
        f"* **Component:** `{detected_part}`\n"
        f"* **Pin Mapping Architecture:** `{pin_configuration}`\n\n"
        f"#### 💻 Production-Ready Firmware Block:\n"
        f"{block_start}\n{cpp_logic_array}\n{block_end}\n"
        f"*Note: This fallback script was injected instantly via Nexa's native tokenizer firmware matrix.*"
    )

    gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("gemini_api_key")
    if gemini_api_key and gemini_api_key.strip() != "":
        try:
            # Synchronized config matches working HUD diagnose exactly (NO search tools to trigger authorization faults)
            genai.configure(api_key=gemini_api_key.strip())
            model = genai.GenerativeModel('gemini-2.5-flash')
            
            community_prompt = f"""
            You are the elite Nexa Public AI Engineering Mentor. Provide advanced component analysis and hardware help.
            Target MCU Platform: {payload.target_mcu}
            User Input Description: "{payload.scrapped_component_text}"
            
            Output a concise Markdown report detailing:
            1. Verified identification and safe operational threshold voltages.
            2. Exact integration topology pin maps back to the {payload.target_mcu}.
            3. A functional, concise C++ code block executing it.
            """
            response = await call_gemini_async(model, community_prompt)
            if response and response.text:
                mentor_guidance_text = response.text
        except Exception as api_err:
            print(f"API Hook timeout. Defaulting to high-intelligence tokenizer matrix: {str(api_err)}")

    document_entry = {
        "comment_id": unique_id, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_mcu": payload.target_mcu, "scrapped_component_text": payload.scrapped_component_text,
        "mentor_guidance": mentor_guidance_text, "signature_hash": hashed_sig
    }
    await ledger_coll.insert_one(document_entry)
    return {"success": True, "comment_id": unique_id, "mentor_guidance": mentor_guidance_text}

@app.post("/public/community/delete")
async def delete_community_post(payload: DeleteCommunityQuery):
    ledger_coll = await get_secure_collection("community_ledger")
    target_id = payload.comment_id.strip()
    input_sig = payload.author_signature.strip().lower()
    hashed_input = hashlib.sha256(input_sig.encode()).hexdigest()
    
    record = await ledger_coll.find_one({"comment_id": target_id})
    if not record: raise HTTPException(status_code=404, detail="Target tracking post hash token not found.")
    if record.get("signature_hash") != hashed_input: raise HTTPException(status_code=403, detail="Signature breach: Denied.")
    await ledger_coll.delete_one({"comment_id": target_id})
    return {"success": True}

@app.post("/agent/diagnose")
async def execute_hardware_diagnostics(payload: DiagnosticPayload):
    gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("gemini_api_key")
    if not gemini_api_key: raise HTTPException(status_code=500, detail="Missing secure key configurations.")
    try:
        coll = await get_secure_collection("module_templates")
        safe_mod = re.escape(payload.module_name.strip())
        bp = await coll.find_one({"team_id": payload.team_id.lower(), "module_name": {"$regex": f"^{safe_mod}$", "$options": "i"}})
        datasheet_grounding = bp.get("datasheet_grounding_text", "") if bp else ""
        specs_grounding = bp.get("specs", {}) if bp else {}

        diagnostic_report_text = f"### 📟 Nexa Local Diagnostic Hardware Engine Fallback\n* **Subsystem Target:** `{payload.module_name}`\n\n**⚠️ Symptom:** \"{payload.symptom_description}\"\n\n*Review telemetry arrays or serial monitors directly.*"

        if gemini_api_key and gemini_api_key.strip() != "":
            try:
                genai.configure(api_key=gemini_api_key.strip())
                model = genai.GenerativeModel('gemini-2.5-flash')
                diagnostic_prompt = f"You are an elite Embedded Systems Diagnostic Analyzer. \n[BLUEPRINT]: {str(specs_grounding)}\n[DATASHEET]: {datasheet_grounding}\n[FAULT]: \"{payload.symptom_description}\"\n[SERIAL]:\n{payload.error_log_text}\nOutput Markdown detailing: 1. IDENTIFIED ROOT CAUSE, 2. PHYSICAL TROUBLESHOOTING STEPS."
                response = await call_gemini_async(model, diagnostic_prompt)
                if response and response.text: diagnostic_report_text = response.text
            except Exception as api_err: print(f"Diagnostics API fallback triggered: {str(api_err)}")

        return {"success": True, "diagnostic_report": diagnostic_report_text}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
