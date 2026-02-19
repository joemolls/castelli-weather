import os
import httpx
from datetime import datetime

UPSTASH_URL   = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

def _headers():
    return {"Authorization": f"Bearer {UPSTASH_TOKEN}"}

def _redis(*args):
    """
    Esegue un comando Redis su Upstash REST API.
    Esempio: _redis("INCR", "visits:total")
    """
    cmd = "/".join(str(a) for a in args)
    url = f"{UPSTASH_URL}/{cmd}"
    try:
        r = httpx.get(url, headers=_headers(), timeout=5.0)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"⚠️ Upstash Redis error: {e}")
        return None

def increment_visit(page: str = "dashboard"):
    """
    Incrementa i contatori visite e restituisce le stats.
    
    Chiavi Redis:
      visits:total              → totale sito (tutte le pagine)
      visits:day:YYYY-MM-DD     → visite oggi (tutte le pagine)
      visits:month:YYYY-MM      → visite questo mese (tutte le pagine)
      visits:page:<page>        → totale per singola pagina
    """
    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")

    key_total      = "visits:total"
    key_today      = f"visits:day:{today}"
    key_month      = f"visits:month:{month}"
    key_page       = f"visits:page:{page}"
    key_page_today = f"visits:page:{page}:day:{today}"

    total       = _redis("INCR", key_total)
    today_count = _redis("INCR", key_today)
    month_count = _redis("INCR", key_month)
    page_total  = _redis("INCR", key_page)
    page_today  = _redis("INCR", key_page_today)

    # TTL: giornaliero scade dopo 2 giorni, mensile dopo 35 giorni
    if today_count == 1:
        _redis("EXPIRE", key_today, 172800)       # 2 giorni
    if month_count == 1:
        _redis("EXPIRE", key_month, 3024000)      # 35 giorni
    if page_today == 1:
        _redis("EXPIRE", key_page_today, 172800)  # 2 giorni

    return {
        "total":      total       or 0,
        "today":      today_count or 0,
        "this_month": month_count or 0,
        "page":       page,
        "page_total": page_total  or 0,
        "page_today": page_today  or 0,
    }
