import os, hmac, hashlib, json, sqlite3
from contextlib import contextmanager
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse
import uvicorn, httpx

app = FastAPI()

VERIFY_TOKEN = "josito"
WABA_TOKEN   = 'EAAUZARzoZC4moBPsNF9iX2VVKpDYYv00NoOR0SNqtRM0HjXPMfcGitPVIdTbH0ndwo30D0or4HACf5HUMu1ApQBLDDOztdHZBdcrY81566Fa4YZA4byfZBcqDsDcF6YupH9pVZCgZCmQsUroRxHVyJK3XaNZB2nRZCB6wQ4H742RJZAz8NsPzm90Kd17FdacQCNlzXVYZBbK1Dp0gsZAVIsOjP6REJ68r9IFNdO2I1xHZAgNUH6PN4ywZD'
APP_SECRET   = 'd1f05b3acbeb8c7af7ba1371b9006794'
GRAPH_VER    = "v22.0"
OPENAI_API_KEY = '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'

DB_PATH = os.getenv("DB_PATH", "data.db")

@contextmanager
def db():
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
        )""")
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
        "text": {"body": text}
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, headers=headers, json=payload)
        print("Send response:", r.status_code, r.text)

async def ask_gpt(message: str) -> str:
    """
    Llama a la API de OpenAI para generar la respuesta.
    """
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4o-mini",  # usa el modelo que prefieras
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
        con.execute("INSERT OR IGNORE INTO processed_messages(id, from_wa, ts, type) VALUES (?,?,?,?)",
                    (msg_id, from_wa, ts, mtype))

async def handle_message(value: dict, msg: dict):
    phone_number_id = value.get("metadata", {}).get("phone_number_id")
    from_wa = msg.get("from")
    msg_type = msg.get("type")

    if msg_type == "text":
        text = msg.get("text", {}).get("body", "")
        print(f"[TEXT] {from_wa}: {text}")

        # ---- Aquí va la llamada a GPT ----
        reply = await ask_gpt(text)

        # Enviar la respuesta a WhatsApp
        if phone_number_id and from_wa:
            await send_text(phone_number_id, from_wa, reply)

@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

@app.get("/webhook", response_class=PlainTextResponse)
async def verify(hub_mode: str | None = None,
                 hub_challenge: str | None = None,
                 hub_verify_token: str | None = None):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return hub_challenge or ""
    raise HTTPException(status_code=403, detail="Verification failed")

@app.post("/webhook")
async def receive(request: Request,
                  background: BackgroundTasks,
                  x_hub_signature_256: str | None = Header(default=None)):
    raw = await request.body()

    if APP_SECRET:
        if not x_hub_signature_256 or not verify_signature(APP_SECRET, raw, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid signature")

    data = json.loads(raw or "{}")

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []) or []:
                msg_id = msg.get("id")
                from_wa = msg.get("from")
                ts = msg.get("timestamp")
                mtype = msg.get("type")

                if already_processed(msg_id):
                    continue
                mark_processed(msg_id, from_wa, ts, mtype)
                background.add_task(handle_message, value, msg)

    return {"status": "ok"}

if _name_ == "_main_":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
