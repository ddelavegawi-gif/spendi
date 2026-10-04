"""One-time imports: every JSON file in imports/ is loaded once, on startup.

File format:
{
  "batch": "amex-2026-09-16",          # unique name; a batch never loads twice
  "clear_existing": false,             # true = archive every expense logged before this import
  "expenses": [
    {"spent_on": "2026-09-20", "amount": 2727.06, "category": "Groceries", "person": "",
     "logged_by": "Diego", "merchant": "H-E-B", "note": "..."}
  ]
}
"""
import json
import logging
from datetime import datetime
from pathlib import Path

from config import Config
from storage import Store

log = logging.getLogger("spendi.importer")


def run_imports(cfg: Config, store: Store, folder: str = "imports") -> None:
    path = Path(folder)
    if not path.is_dir():
        return
    for file in sorted(path.glob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            batch = data["batch"]
            if store.import_done(batch):
                continue
            rows = [_validate(cfg, e, i) for i, e in enumerate(data["expenses"])]
            n = store.run_import(batch, rows, bool(data.get("clear_existing")),
                                 datetime.now(cfg.tz).strftime("%Y-%m-%d %H:%M:%S"))
            log.info("Imported %s expenses from %s (batch %s)", n, file.name, batch)
        except Exception:
            log.exception("Import %s failed — nothing from it was saved", file.name)


def _validate(cfg: Config, e: dict, i: int) -> dict:
    cat = e["category"]
    if cat not in cfg.categories:
        raise ValueError(f"expense {i}: unknown category {cat!r}")
    person = e.get("person", "") or ""
    if cfg.is_personal(cat) and person not in cfg.people:
        raise ValueError(f"expense {i}: personal expense needs person in {cfg.people}")
    if not cfg.is_personal(cat):
        person = ""
    if e["logged_by"] not in cfg.people:
        raise ValueError(f"expense {i}: unknown logged_by {e['logged_by']!r}")
    datetime.strptime(e["spent_on"], "%Y-%m-%d")
    return {**e, "person": person, "amount": round(float(e["amount"]), 2)}
