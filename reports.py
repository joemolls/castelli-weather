import os
import json
import uuid
import httpx
from datetime import datetime, timedelta

UPSTASH_URL      = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN    = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
REPORT_TTL_DAYS  = 21
MIN_REPORTS      = 5        # mantieni sempre almeno le ultime N segnalazioni
REPORTS_ZSET_KEY = "reports:index"


def _headers():
    return {
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type": "application/json",
    }


def _pipeline(commands: list):
    try:
        r = httpx.post(
            f"{UPSTASH_URL}/pipeline",
            headers=_headers(),
            content=json.dumps(commands),
            timeout=5.0,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"⚠️ Upstash pipeline error: {e}")
        return None


def _cmd(*args):
    result = _pipeline([list(args)])
    if result and isinstance(result, list):
        return result[0].get("result")
    return None


def save_report(lat: float, lon: float, kind: str, description: str = "") -> dict:
    report_id  = str(uuid.uuid4())
    now        = datetime.utcnow()
    expires_at = now + timedelta(days=REPORT_TTL_DAYS)

    report = {
        "id":          report_id,
        "lat":         lat,
        "lon":         lon,
        "kind":        kind,
        "description": description[:200],
        "created_at":  now.isoformat(),
        "expires_at":  expires_at.isoformat(),
    }

    key         = f"report:{report_id}"
    score       = int(now.timestamp())
    ttl_seconds = (REPORT_TTL_DAYS + 1) * 86400
    value       = json.dumps(report, ensure_ascii=False)

    _pipeline([
        ["SET",    key, value],
        ["EXPIRE", key, ttl_seconds],
        ["ZADD",   REPORTS_ZSET_KEY, score, key],
    ])

    return report


def get_active_reports() -> list:
    now    = datetime.utcnow()
    cutoff = int((now - timedelta(days=REPORT_TTL_DAYS)).timestamp())

    # Conta quante segnalazioni ci sono in totale
    total = _cmd("ZCARD", REPORTS_ZSET_KEY) or 0

    # Rimuovi le scadute solo se ce ne sono più di MIN_REPORTS
    # così le ultime 5 restano sempre visibili anche se vecchie
    if total > MIN_REPORTS:
        # Rimuovi scadute ma lascia almeno MIN_REPORTS
        expired_count = _cmd("ZCOUNT", REPORTS_ZSET_KEY, 0, cutoff) or 0
        removable     = max(0, int(expired_count) - max(0, MIN_REPORTS - (int(total) - int(expired_count))))
        if removable > 0:
            _cmd("ZREMRANGEBYSCORE", REPORTS_ZSET_KEY, 0, cutoff)

    # Recupera tutte le chiavi
    keys = _cmd("ZRANGE", REPORTS_ZSET_KEY, 0, -1)
    if not keys:
        return []

    get_commands = [["GET", k] for k in keys]
    results = _pipeline(get_commands)
    if not results:
        return []

    reports = []
    for res in reversed(results):  # più recenti prima
        raw = res.get("result")
        if not raw:
            continue
        try:
            report = json.loads(raw)
            reports.append(report)   # includi anche scadute se siamo sotto MIN_REPORTS
        except Exception:
            continue

    # Separa attive e scadute
    active  = [r for r in reports if datetime.fromisoformat(r["expires_at"]) > now]
    expired = [r for r in reports if datetime.fromisoformat(r["expires_at"]) <= now]

    # Assicura almeno MIN_REPORTS in totale
    combined = active
    if len(combined) < MIN_REPORTS:
        needed   = MIN_REPORTS - len(combined)
        combined = combined + expired[:needed]

    return combined
