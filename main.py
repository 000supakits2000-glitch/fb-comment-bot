import os
import json
import requests
from fastapi import FastAPI, Request, Response
from google import genai
from google.genai import types

app = FastAPI()

# ดึงค่า Environment Variables จาก Render
VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN", "mybot_verify_2026")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# โหลด PAGES_CONFIG ในรูปแบบ JSON: {"page_id": "page_access_token", ...}
try:
    PAGES_CONFIG = json.loads(os.getenv("PAGES_CONFIG", "{}"))
except json.JSONDecodeError:
    print("Error: รูปแบบ JSON ใน PAGES_CONFIG ไม่ถูกต้อง")
    PAGES_CONFIG = {}

# ตั้งค่า Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# กำหนดบุคลิกและสไตล์การตอบของแต่ละเพจแยกกัน
PAGE_PERSONAS = {
    "1427512030449790": (
        "คุณคือแอดมินเพจ 'เล่าข่าวเช้านี้' ตอบคอมเมนต์ลูกเพจด้วยน้ำเสียงสุภาพ เป็นกันเอง น่าเชื่อถือ "
        "กระชับ ได้ใจความ ชวนคุยเรื่องข่าวหรือประเด็นที่คอมเมนต์มาอย่างสร้างสรรค์ ความยาวไม่เกิน 2 ประโยค"
    ),
    "1259689820560633": (
        "คุณคือแอดมินเพจ 'ของมันต้องมี - ชี้เป้าไอเทมฮิต' ตอบคอมเมนต์ลูกเพจด้วยน้ำเสียงสดใส กระตือรือร้น "
        "ป้ายยาแบบเป็นธรรมชาติ น่ารัก น่าซื้อ ให้ข้อมูลที่เป็นประโยชน์และกระตุ้นความสนใจ ความยาวไม่เกิน 2 ประโยค"
    )
}

DEFAULT_PERSONA = (
    "คุณคือแอดมินเพจ Facebook ตอบคอมเมนต์ลูกเพจอย่างสุภาพ เป็นมิตร กระชับ และเป็นธรรมชาติ ความยาวไม่เกิน 2 ประโยค"
)

def generate_ai_reply(page_id: str, comment_text: str, user_name: str) -> str:
    """สร้างข้อความตอบกลับด้วย Gemini AI โดยแยกสไตล์ตาม Page ID"""
    if not client:
        return "ขอบคุณที่ร่วมแสดงความคิดเห็นครับ 🙏"

    system_instruction = PAGE_PERSONAS.get(page_id, DEFAULT_PERSONA)
    prompt = f"ลูกเพจชื่อ '{user_name}' คอมเมนต์ว่า: \"{comment_text}\"\nจงเขียนข้อความตอบกลับคอมเมนต์นี้:"

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7,
                max_output_tokens=150,
            ),
        )
        return response.text.strip()
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return "ขอบคุณสำหรับคอมเมนต์ครับ 🙏"

def reply_to_comment(comment_id: str, message: str, page_access_token: str):
    """ส่งข้อความตอบกลับคอมเมนต์ผ่าน Facebook Graph API"""
    url = f"https://graph.facebook.com/v27.0/{comment_id}/comments"
    payload = {
        "message": message,
        "access_token": page_access_token
    }
    try:
        res = requests.post(url, data=payload, timeout=10)
        if res.status_code == 200:
            print(f"✅ ตอบคอมเมนต์ {comment_id} สำเร็จ: {message}")
        else:
            print(f"❌ ตอบคอมเมนต์ไม่สำเร็จ ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"❌ Request Error: {e}")

@app.get("/")
async def health_check():
    """Endpoint สำหรับเช็กสถานะเซิร์ฟเวอร์บน Render"""
    return {
        "status": "running",
        "configured_pages": list(PAGES_CONFIG.keys())
    }

@app.get("/webhook")
async def verify_webhook(request: Request):
    """ยืนยัน Webhook กับ Meta for Developers"""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook Verified Successfully!")
        return Response(content=challenge, media_type="text/plain")
    
    return Response(content="Verification failed", status_code=403)

@app.post("/webhook")
async def handle_webhook(request: Request):
    """รับเหตุการณ์คอมเมนต์จาก Facebook และสั่งบอทตอบกลับ"""
    try:
        body = await request.json()
    except Exception:
        return Response(content="Invalid JSON", status_code=400)

    if body.get("object") == "page":
        for entry in body.get("entry", []):
            page_id = str(entry.get("id", ""))
            page_token = PAGES_CONFIG.get(page_id)

            # ข้ามหากเพจนี้ไม่ได้ตั้งค่าไว้ใน PAGES_CONFIG
            if not page_token:
                continue

            for change in entry.get("changes", []):
                if change.get("field") == "feed":
                    value = change.get("value", {})
                    item = value.get("item")
                    verb = value.get("verb")

                    # ทำงานเฉพาะเมื่อมีคอมเมนต์ใหม่ (item == 'comment' และ verb == 'add')
                    if item == "comment" and verb == "add":
                        comment_id = value.get("comment_id")
                        message = value.get("message", "").strip()
                        sender_info = value.get("from", {})
                        sender_id = str(sender_info.get("id", ""))
                        sender_name = sender_info.get("name", "คุณลูกค้า")

                        # ป้องกันบอทตอบคอมเมนต์ของเพจตัวเองซ้ำ (Loop Protection)
                        if sender_id == page_id or sender_id in PAGES_CONFIG:
                            continue

                        # ข้ามคอมเมนต์ที่ไม่มีข้อความ (เช่น สติกเกอร์หรือรูปภาพอย่างเดียว)
                        if not message or not comment_id:
                            continue

                        print(f"📩 คอมเมนต์ใหม่จากเพจ {page_id} โดย {sender_name}: {message}")
                        ai_reply = generate_ai_reply(page_id, message, sender_name)
                        reply_to_comment(comment_id, ai_reply, page_token)

    return {"status": "ok"}
