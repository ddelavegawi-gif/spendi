"""Chat with Spendi in your terminal — no WhatsApp/Twilio needed.

    python chat_sim.py
    Type messages as the current person. Switch person with /Diego or /Romi. Ctrl+C to quit.
"""
import os

from dotenv import load_dotenv

load_dotenv()

from bot import Spendi  # noqa: E402
from config import Config  # noqa: E402
from interpreter import Interpreter  # noqa: E402
from storage import Store  # noqa: E402

cfg = Config(os.getenv("SPENDI_CONFIG", "config.yaml"))
spendi = Spendi(cfg, Store(os.getenv("SPENDI_DB", "spendi_sim.db")), Interpreter(cfg))
phone_of = {name.lower(): phone for phone, name in cfg.users.items()}
current = cfg.people[0]

print(f"Chatting as {current}. Switch with " + " / ".join(f"/{p}" for p in cfg.people))
while True:
    try:
        text = input(f"\n{current}> ").strip()
    except (EOFError, KeyboardInterrupt):
        break
    if text.startswith("/") and text[1:].lower() in phone_of:
        current = next(p for p in cfg.people if p.lower() == text[1:].lower())
        print(f"(now chatting as {current})")
        continue
    print(f"\n{cfg.bot_name}:\n{spendi.handle(phone_of[current.lower()], text)}")
