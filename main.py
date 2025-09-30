from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Hello World"}

# (Opcional) acepta HEAD en "/"
@app.head("/")
async def root_head():
    return PlainTextResponse("")  # 200 vacío

@app.get("/items/{item_id}")
def read_item(item_id: int, q: Optional[str] = None):
    return {"item_id": item_id, "q": q}

# === WEBHOOK META (recomendado usar /webhook, no "/") ===
VERIFY_TOKEN = "pon-tu-token-aqui"

# Verificación (GET)
@app.get("/webhook", response_class=PlainTextResponse)
def verify(hub_mode: str | None = None,
           hub_challenge: str | None = None,
           hub_verify_token: str | None = None):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN and hub_challenge:
        return hub_challenge
    raise HTTPException(status_code=403, detail="Verification failed")

# Eventos (POST)
@app.post("/webhook")
async def receive(request: Request):
    payload = await request.json()
    # TODO: procesa payload
    return {"received": True}

# (Opcional/temporal) acepta POST en "/"
@app.post("/")
async def root_post():
    return {"ok": True}
