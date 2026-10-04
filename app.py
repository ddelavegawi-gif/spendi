"""WhatsApp webhook (Twilio) for Spendi.

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
Point your Twilio WhatsApp number's "When a message comes in" webhook to:
      https://<your-domain>/whatsapp   (HTTP POST)
"""
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()
logging.basicConfig(level=logging.INFO)

from bot import Spendi  # noqa: E402
from config import Config  # noqa: E402
from importer import run_imports  # noqa: E402
from interpreter import Interpreter  # noqa: E402
from storage import Store  # noqa: E402

cfg = Config(os.getenv("SPENDI_CONFIG", "config.yaml"))
store = Store(os.getenv("SPENDI_DB", "spendi.db"))
run_imports(cfg, store)  # loads any new file in imports/ (each one only once)
for old, new in cfg.renamed_categories.items():
    moved = store.recategorize(old, new)
    if moved:
        logging.getLogger("spendi").info("Moved %s expenses from %s to %s", moved, old, new)
spendi = Spendi(cfg, store, Interpreter(cfg))

TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

app = FastAPI(title="Spendi")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/whatsapp")
async def whatsapp(request: Request):
    form = dict(await request.form())

    # Reject requests that don't come from Twilio (enabled when TWILIO_AUTH_TOKEN is set).
    if TWILIO_AUTH_TOKEN:
        url = f"{PUBLIC_BASE_URL}{request.url.path}" if PUBLIC_BASE_URL else str(request.url)
        signature = request.headers.get("X-Twilio-Signature", "")
        if not RequestValidator(TWILIO_AUTH_TOKEN).validate(url, form, signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    reply = await run_in_threadpool(spendi.handle, form.get("From", ""), form.get("Body", ""))

    twiml = MessagingResponse()
    if reply:
        twiml.message(reply)
    return Response(content=str(twiml), media_type="application/xml")
