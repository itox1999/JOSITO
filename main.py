import os, hmac, hashlib, json, sqlite3
from contextlib import contextmanager
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks, Query
from fastapi.responses import PlainTextResponse
import uvicorn, httpx

app = FastAPI()

# === Lee todo desde variables de entorno ===
VERIFY_TOKEN     = os.getenv("VERIFY_TOKEN", "")
WABA_TOKEN       = os.getenv("WABA_TOKEN", "")
APP_SECRET       = os.getenv("APP_SECRET", "")
GRAPH_VER        = os.getenv("GRAPH_VER", "v22.0")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
DB_PATH          = os.getenv("DB_PATH", "data.db")

# Pequeña validación (opcional, pero útil):
REQUIRED_VARS = ["VERIFY_TOKEN", "WABA_TOKEN", "APP_SECRET", "OPENAI_API_KEY"]
missing = [k for k in REQUIRED_VARS if not os.getenv(k)]
if missing:
    print(f"[WARN] Faltan variables de entorno: {missing}")

@contextmanager
def db():
    # Nota: en Render el sistema de archivos es efímero sin disco persistente
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages(
            id TEXT PRIMARY KEY,
            from_wa TEXT,
            ts TEXT,
            type TEXT
        )
        """)
init_db()

def verify_signature(app_secret: str, payload: bytes, signature: str) -> bool:
    if not app_secret or not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.split("=",1)[1], expected)

async def send_text(phone_number_id: str, to_msisdn: str, text: str):
    url = f"https://graph.facebook.com/{GRAPH_VER}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {WABA_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_msisdn,
        "type": "text",
        "text": {"body": text[:4096]}  # límite prudente
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, headers=headers, json=payload)
        print("Send response:", r.status_code, r.text)

async def ask_gpt(message: str) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Eres un asistente útil que responde brevemente."},
            {"role": "user", "content": message}
        ]
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]

def already_processed(msg_id: str) -> bool:
    with db() as con:
        cur = con.execute("SELECT 1 FROM processed_messages WHERE id=?", (msg_id,))
        return cur.fetchone() is not None

def mark_processed(msg_id: str, from_wa: str, ts: str, mtype: str):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO processed_messages(id, from_wa, ts, type) VALUES (?,?,?,?)",
            (msg_id, from_wa, ts, mtype)
        )

async def handle_message(value: dict, msg: dict):
    phone_number_id = value.get("metadata", {}).get("phone_number_id")
    from_wa = msg.get("from")
    msg_type = msg.get("type")

    if msg_type == "text":
        text = msg.get("text", {}).get("body", "") or ""
        print(f"[TEXT] {from_wa}: {text}")

        # Llama a GPT y responde
        try:
            reply = await ask_gpt(text)
        except Exception as e:
            print("[GPT ERROR]", e)
            reply = "Lo siento, tuve un problema procesando tu mensaje."

        if phone_number_id and from_wa and reply:
            await send_text(phone_number_id, from_wa, reply)

@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

# GET de verificación con alias correctos para hub.*
@app.get("/webhook", response_class=PlainTextResponse)
async def verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return hub_challenge or ""
    raise HTTPException(status_code=403, detail="Verification failed")

# POST del webhook con alias correcto para la cabecera X-Hub-Signature-256
@app.post("/webhook")
async def receive(
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256")
):
    raw = await request.body()

    if APP_SECRET:
        if not x_hub_signature_256 or not verify_signature(APP_SECRET, raw, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages") or []
            for msg in messages:
                msg_id = msg.get("id")
                from_wa = msg.get("from")
                ts = msg.get("timestamp")
                mtype = msg.get("type")

                if not msg_id:
                    continue
                if already_processed(msg_id):
                    continue
                mark_processed(msg_id, from_wa or "", ts or "", mtype or "")

                # Manejo asíncrono para no bloquear el webhook
                background.add_task(handle_message, value, msg)

    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    # En Render usar host 0.0.0.0
    uvicorn.run("app:app", host="0.0.0.0", port=port)
