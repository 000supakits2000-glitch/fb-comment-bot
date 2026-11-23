import os
import re
import time
import random
from datetime import datetime, timedelta, timezone
import requests
from fastapi import FastAPI, Request, Response, BackgroundTasks, HTTPException
from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai import types

load_dotenv()

app = FastAPI(title="Safe FB Comment AI Assistant")

# โหลดค่าตั้งต้นจาก Environment Variables
PAGE_ID = os.getenv("FB_PAGE_ID")
PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# เชื่อมต่อ Supabase และ Gemini Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ตั้งค่าขีดจำกัดความปลอดภัย (Safety Config)
MAX_REPLIES_PER_HOUR = 15  # ตอบสูงสุดไม่เกิน 15 คอมเมนต์/ชั่วโมง
MIN_DELAY_SEC = 8          # หน่วงเวลาขั้นต่ำ (วินาที)
MAX_DELAY_SEC = 22         # หน่วงเวลาสูงสุด (วินาที)


def is_bot_active() -> tuple[bool, str]:
    """เช็กสถานะ Kill Switch และดึง System Prompt ล่าสุดจาก Supabase"""
    res = supabase.table("bot_settings").select("*").eq("id", 1).execute()
    if res.data:
        return res.data[0]["is_active"], res.data[0]["system_prompt"]
    return False, ""


def emergency_pause_bot():
    """หยุดการทำงานบอทอัตโนมัติทันทีหากเจอ Error จาก Facebook API"""
    supabase.table("bot_settings").update({"is_active": False}).eq("id", 1).execute()
    print("🚨 [EMERGENCY PAUSE] บอทถูกสั่งหยุดชั่วคราวอัตโนมัติเนื่องจากพบ Error จาก API")


def is_spam_or_invalid(message: str) -> bool:
    """ด่านที่ 1: กรองคอมเมนต์ขยะ ลิงก์ หรือสั้นเกินไป"""
    if not message:
        return True
    text = message.strip()
    # ข้ามถ้ามีลิงก์
    if re.search(r"(http://|https://|www\.|\.com|\.net|\.co)", text, re.IGNORECASE):
        return True
    # ข้ามถ้าสั้นเกินไป (เช่น จุดเดียว หรืออีโมจิ 1 ตัว)
    if len(text) < 2:
        return True
    return False


def is_already_replied(comment_id: str) -> bool:
    """ด่านที่ 2: เช็กใน Supabase ว่าเคยตอบคอมเมนต์นี้หรือยัง"""
    res = supabase.table("comment_logs").select("comment_id").eq("comment_id", comment_id).execute()
    return len(res.data) > 0


def is_rate_limited() -> bool:
    """ด่านที่ 3: เช็กโควตาว่า 1 ชั่วโมงที่ผ่านมาตอบเกินกำหนดหรือยัง"""
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    res = supabase.table("comment_logs").select("comment_id", count="exact").gte("created_at", one_hour_ago).execute()
    count = res.count if res.count is not None else len(res.data)
    return count >= MAX_REPLIES_PER_HOUR


def get_recent_replies(limit: int = 5) -> list[str]:
    """ดึงคำตอบล่าสุด 5 รายการ เพื่อส่งให้ AI เลี่ยงการใช้ประโยคซ้ำ"""
    res = supabase.table("comment_logs").select("bot_reply").order("created_at", desc=True).limit(limit).execute()
    return [row["bot_reply"] for row in res.data if row.get("bot_reply")]


def get_post_context(post_id: str) -> str:
    """ดึงเนื้อหาของโพสต์เพื่อให้ AI เข้าใจบริบทว่าคนคอมเมนต์เรื่องอะไร"""
    url = f"https://graph.facebook.com/v21.0/{post_id}"
    params = {"fields": "message", "access_token": PAGE_ACCESS_TOKEN}
    try:
        r = requests.get(url, params=params, timeout=5)
        if r.status_code == 200:
            return r.json().get("message", "")[:500]  # ตัดมาแค่ 500 ตัวอักษรแรก
    except Exception:
        pass
    return ""


def generate_ai_reply(user_comment: str, post_context: str, base_prompt: str) -> str:
    """ด่านที่ 4: สร้างคำตอบด้วย Gemini โดยเลี่ยงคำซ้ำ"""
    recent_replies = get_recent_replies(5)
    avoid_list = "\n".join([f"- {r}" for r in recent_replies]) if recent_replies else "ไม่มี"

    prompt = f"""
เนื้อหาโพสต์ต้นทาง: "{post_context}"
คอมเมนต์จากลูกเพจ: "{user_comment}"

คำตอบล่าสุดที่เพจเพิ่งตอบไป (ห้ามใช้รูปประโยคหรือคำขึ้นต้นซ้ำกับรายการนี้เด็ดขาด):
{avoid_list}

จงตอบกลับคอมเมนต์นี้ให้เป็นธรรมชาติ ตรงประเด็น และไม่ซ้ำซาก:
"""
    # เลือกใช้โมเดลตระกูล Flash ที่รวดเร็ว
    response = ai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=base_prompt,
            temperature=0.8,  # เพิ่มความหลากหลายของคำตอบ
            max_output_tokens=150,
        ),
    )
    return response.text.strip()


def process_comment_safely(comment_id: str, post_id: str, sender_id: str, message: str):
    """ฟังก์ชันหลักทำงานเบื้องหลังพร้อมระบบเซฟตี้ครบทุกชั้น"""
    try:
        # 1. ข้ามถ้าเป็นคอมเมนต์ของเพจเอง (กันคุยกับตัวเอง)
        if sender_id == PAGE_ID:
            return

        # 2. เช็ก Kill Switch
        active, system_prompt = is_bot_active()
        if not active:
            print("⏸️ บอทถูกปิดใช้งานอยู่ ข้ามการตอบ")
            return

        # 3. กรองสแปม/ลิงก์
        if is_spam_or_invalid(message):
            print(f"🚫 ข้ามคอมเมนต์เข้าข่ายสแปม/สั้นเกินไป: {message}")
            return

        # 4. กันตอบซ้ำ
        if is_already_replied(comment_id):
            print(f"♻️ เคยตอบคอมเมนต์ {comment_id} ไปแล้ว ข้าม")
            return

        # 5. เช็ก Rate Limit รายชั่วโมง
        if is_rate_limited():
            print("⚠️ ถึงลิมิตการตอบต่อชั่วโมงแล้ว ข้ามเพื่อความปลอดภัย")
            return

        # 6. หน่วงเวลาแบบมนุษย์ (Random Delay)
        delay = random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC)
        print(f"⏳ กำลังหน่วงเวลา {delay:.1f} วินาที ก่อนตอบคอมเมนต์: {message}")
        time.sleep(delay)

        # เช็กซ้ำอีกรอบเผื่อระหว่างรอมีอีก Thread ตอบไปแล้ว
        if is_already_replied(comment_id):
            return

        # 7. ดึงบริบทโพสต์และให้ Gemini สร้างคำตอบ
        post_context = get_post_context(post_id)
        reply_text = generate_ai_reply(message, post_context, system_prompt)

        # 8. ยิง API ตอบกลับคอมเมนต์บน Facebook
        reply_url = f"https://graph.facebook.com/v21.0/{comment_id}/comments"
        payload = {
            "message": reply_text,
            "access_token": PAGE_ACCESS_TOKEN
        }
        fb_res = requests.post(reply_url, data=payload, timeout=10)

        if fb_res.status_code == 200:
            # บันทึกลง Supabase ทันที
            supabase.table("comment_logs").insert({
                "comment_id": comment_id,
                "post_id": post_id,
                "user_message": message,
                "bot_reply": reply_text
            }).execute()
            print(f"✅ ตอบสำเร็จ: {reply_text}")
        else:
            err_data = fb_res.json()
            print(f"❌ Facebook API Error: {err_data}")
            # หากเจอ Error รหัส 368 (บล็อกชั่วคราว) หรือ 4/17 (Rate limit) ให้สั่งหยุดบอททันที
            err_code = err_data.get("error", {}).get("code")
            if err_code in [4, 17, 368]:
                emergency_pause_bot()

    except Exception as e:
        print(f"🔥 เกิดข้อผิดพลาดในระบบ: {e}")


# --- Endpoint 1: สำหรับให้ Meta ยืนยันตัวตน Webhook (GET) ---
@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ ยืนยัน Webhook สำเร็จ!")
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification token mismatch")


# --- Endpoint 2: รับข้อมูลคอมเมนต์ใหม่จาก Meta (POST) ---
@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    if body.get("object") == "page":
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") == "feed":
                    val = change.get("value", {})
                    # ตรวจสอบว่าเป็นเหตุการณ์ "เพิ่มคอมเมนต์ใหม่" เท่านั้น
                    if val.get("item") == "comment" and val.get("verb") == "add":
                        comment_id = val.get("comment_id")
                        post_id = val.get("post_id")
                        sender_id = val.get("from", {}).get("id")
                        message = val.get("message", "")

                        if comment_id and sender_id:
                            # ส่งไปทำงานเบื้องหลังเพื่อให้ตอบ 200 OK กลับ Facebook ทันที
                            background_tasks.add_task(
                                process_comment_safely,
                                comment_id,
                                post_id,
                                sender_id,
                                message
                            )

    return {"status": "ok"}


@app.get("/")
def health_check():
    return {"status": "running", "service": "FB AI Comment Assistant"}
