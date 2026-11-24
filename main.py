import os
import json
import time
import random
import asyncio
import requests
from collections import deque
from fastapi import FastAPI, Request, Response, BackgroundTasks
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

# ระบบจำกัดจำนวนการตอบต่อชั่วโมง (ป้องกันโพสต์ไวรัลแล้วบอทตอบรัวเกินไป)
MAX_REPLIES_PER_HOUR = 15
reply_timestamps = {}  # เก็บประวัติเวลาที่ตอบของแต่ละเพจ

# กำหนดบุคลิกและกฎเหล็กให้ตอบเหมือนคนไทยพิมพ์คุยจริงๆ
ANTI_BOT_RULES = (
    "กฎเหล็กในการตอบ:\n"
    "1. พิมพ์ตอบสั้นๆ แค่ 1 ประโยค (ไม่เกิน 10-15 คำ) เหมือนคนไทยพิมพ์ตอบคอมเมนต์ในมือถือ\n"
    "2. ห้ามทักทายชื่อคนคอมเมนต์ (ห้ามขึ้นต้นด้วย 'สวัสดีครับคุณ...')\n"
    "3. ห้ามใช้ภาษาทางการแบบคอลเซ็นเตอร์หรือ AI (ห้ามใช้คำว่า 'ยินดีเป็นอย่างยิ่ง', 'ขอขอบพระคุณ', 'สามารถสอบถามเพิ่มเติมได้')\n"
    "4. ใช้อีโมจิได้ไม่เกิน 1 ตัว หรือไม่ต้องใส่เลยก็ได้ในบางครั้ง เพื่อให้ดูไม่ซ้ำซาก\n"
    "5. ตอบรับตามอารมณ์ของคอมเมนต์นั้นแบบเป็นธรรมชาติ"
)

PAGE_PERSONAS = {
    "1427512030449790": (
        "คุณคือแอดมินเพจ 'เล่าข่าวเช้านี้' เป็นคนอัปเดตข่าวสารและกีฬา คุยกับลูกเพจแบบพี่น้องเป็นกันเอง "
        "ใช้คำลงท้าย 'ครับ' แบบสบายๆ ชวนคุยเรื่องข่าวหรือผลการแข่งขันสั้นๆ\n" + ANTI_BOT_RULES
    ),
    "1259689820560633": (
        "คุณคือแอดมินเพจ 'ของมันต้องมี - ชี้เป้าไอเทมฮิต' เป็นสายรีวิวของใช้ คุยกับลูกเพจแบบเพื่อนป้ายยาเพื่อน "
        "ใช้คำลงท้าย 'ค่า' หรือ 'เลยจ้า' แบบน่ารัก สดใส ไม่ยัดเยียดขายของ\n" + ANTI_BOT_RULES
    )
}

DEFAULT_PERSONA = (
    "คุณคือแอดมินเพจ Facebook ตอบคอมเมนต์ลูกเพจแบบคนกันเอง สั้นๆ กระชับ\n" + ANTI_BOT_RULES
)

def can_reply_now(page_id: str) -> bool:
    """ตรวจสอบว่าเพจนี้ตอบเกินโควตาต่อชั่วโมงหรือยัง"""
    now = time.time()
    if page_id not in reply_timestamps:
        reply_timestamps[page_id] = deque()

    # ลบประวัติที่เก่าเกิน 1 ชั่วโมง (3600 วินาที) ออก
    while reply_timestamps[page_id] and now - reply_timestamps[page_id][0] > 3600:
        reply_timestamps[page_id].popleft()

    return len(reply_timestamps[page_id]) < MAX_REPLIES_PER_HOUR

def record_reply(page_id: str):
    """บันทึกเวลาที่ตอบสำเร็จ"""
    if page_id not in reply_timestamps:
        reply_timestamps[page_id] = deque()
    reply_timestamps[page_id].append(time.time())

def generate_ai_reply(page_id: str, comment_text: str) -> str:
    """สร้างข้อความตอบกลับด้วย Gemini AI ให้สั้นและเป็นธรรมชาติ"""
    if not client:
        return random.choice(["ขอบคุณคร้าบ 🙏", "จริงเลยครับ", "ขอบคุณที่ติดตามนะครับ"])

    system_instruction = PAGE_PERSONAS.get(page_id, DEFAULT_PERSONA)
    prompt = f"ลูกเพจคอมเมนต์ว่า: \"{comment_text}\"\nพิมพ์ตอบกลับสั้นๆ 1 ประโยค:"

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.85,  # เพิ่มความหลากหลายของคำตอบไม่ให้ซ้ำเดิม
                max_output_tokens=80,
            ),
        )
        reply = response.text.strip().strip('"').strip("'")
        return reply if reply else "ขอบคุณครับ 🙏"
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return "ขอบคุณที่ร่วมแสดงความคิดเห็นครับ 🙏"

def like_comment(comment_id: str, page_access_token: str):
    """กดถูกใจ (Like) คอมเมนต์"""
    url = f"https://graph.facebook.com/v27.0/{comment_id}/likes"
    try:
        requests.post(url, data={"access_token": page_access_token}, timeout=10)
        print(f"👍 กดถูกใจคอมเมนต์ {comment_id} เรียบร้อย")
    except Exception as e:
        print(f"❌ Like Error: {e}")

def reply_to_comment(comment_id: str, message: str, page_access_token: str) -> bool:
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
            return True
        else:
            print(f"❌ ตอบคอมเมนต์ไม่สำเร็จ ({res.status_code}): {res.text}")
            return False
    except Exception as e:
        print(f"❌ Request Error: {e}")
        return False

async def process_human_like_reply(page_id: str, comment_id: str, message: str, sender_name: str, page_token: str):
    """จำลองพฤติกรรมแอดมินคนจริง: รอเวลาไม่เท่ากัน + กดไลก์ + บางครั้งกดไลก์อย่างเดียวไม่ตอบ"""
    # 1. สุ่มเวลารอระหว่าง 1 นาที (60 วิ) ถึง 3.5 นาที (210 วิ)
    delay = random.randint(60, 210)
    print(f"⏳ [เพจ {page_id}] รอ {delay} วินาที ก่อนจัดการคอมเมนต์ของ {sender_name}...")
    await asyncio.sleep(delay)

    # 2. เช็กโควตาต่อชั่วโมง หากเกินลิมิตให้กดแค่ Like เพื่อความปลอดภัย
    if not can_reply_now(page_id):
        print(f"🛡️ เกินโควตา {MAX_REPLIES_PER_HOUR} ครั้ง/ชม. เปลี่ยนเป็นกด Like แทน")
        like_comment(comment_id, page_token)
        return

    # 3. สุ่มพฤติกรรม: 85% กดไลก์ + ตอบกลับ / 15% กดไลก์อย่างเดียว (เหมือนแอดมินอ่านแล้วถูกใจ)
    if random.random() < 0.15:
        print(f"🎲 สุ่มเลือกกด Like อย่างเดียวสำหรับคอมเมนต์นี้ (เพื่อความเป็นธรรมชาติ)")
        like_comment(comment_id, page_token)
        return

    # 4. กดไลก์คอมเมนต์ก่อน แล้วเว้นจังหวะพิมพ์ 3-8 วินาที เหมือนคนกำลังพิมพ์
    like_comment(comment_id, page_token)
    await asyncio.sleep(random.randint(3, 8))

    # 5. สร้างคำตอบและส่งกลับ
    ai_reply = generate_ai_reply(page_id, message)
    if reply_to_comment(comment_id, ai_reply, page_token):
        record_reply(page_id)

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
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    """รับเหตุการณ์คอมเมนต์จาก Facebook และสั่งบอทตอบกลับแบบเป็นธรรมชาติ"""
    try:
        body = await request.json()
    except Exception:
        return Response(content="Invalid JSON", status_code=400)

    if body.get("object") == "page":
        for entry in body.get("entry", []):
            page_id = str(entry.get("id", ""))
            page_token = PAGES_CONFIG.get(page_id)

            if not page_token:
                continue

            for change in entry.get("changes", []):
                if change.get("field") == "feed":
                    value = change.get("value", {})
                    item = value.get("item")
                    verb = value.get("verb")

                    if item == "comment" and verb == "add":
                        comment_id = value.get("comment_id")
                        message = value.get("message", "").strip()
                        sender_info = value.get("from", {})
                        sender_id = str(sender_info.get("id", ""))
                        sender_name = sender_info.get("name", "ลูกเพจ")

                        # ป้องกันบอทตอบคอมเมนต์ของเพจตัวเองซ้ำ
                        if sender_id == page_id or sender_id in PAGES_CONFIG:
                            continue

                        if not message or not comment_id:
                            continue

                        print(f"📩 คอมเมนต์ใหม่จากเพจ {page_id} โดย {sender_name}: {message}")
                        background_tasks.add_task(
                            process_human_like_reply,
                            page_id,
                            comment_id,
                            message,
                            sender_name,
                            page_token
                        )

    return {"status": "ok"}
