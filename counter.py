import json
import os
from datetime import datetime

COUNTER_FILE = "visit_counter.json"

def load_counter():
    """Carica il contatore dal file"""
    if os.path.exists(COUNTER_FILE):
        try:
            with open(COUNTER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "total": 0,
        "today": 0,
        "last_date": "",
        "monthly": {}
    }

def save_counter(data):
    """Salva il contatore nel file"""
    try:
        with open(COUNTER_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"❌ Errore salvataggio contatore: {e}")

def increment_visit():
    """Incrementa il contatore visite e restituisce le stats"""
    data = load_counter()
    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")

    # Reset contatore giornaliero se è un nuovo giorno
    if data.get("last_date") != today:
        data["today"] = 0
        data["last_date"] = today

    # Incrementa contatori
    data["total"] = data.get("total", 0) + 1
    data["today"] = data.get("today", 0) + 1

    if "monthly" not in data:
        data["monthly"] = {}
    data["monthly"][month] = data["monthly"].get(month, 0) + 1

    save_counter(data)

    return {
        "total": data["total"],
        "today": data["today"],
        "this_month": data["monthly"].get(month, 0)
    }
