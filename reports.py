import os
import json
import uuid
import httpx
from datetime import datetime, timedelta

UPSTASH_URL   = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

REPORT_TTL_DAYS  = 21
REPORTS_ZSET_KEY = "reports:index"


def _headers():
    return {
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type": "application/json",
    }


def _pipeline(commands: list):
    """
    Esegue più comandi Redis in una sola richiesta HTTP via Upstash pipeline.
    commands = [["SET", "key", "value"], ["EXPIRE", "key", 86400], ...]
    """
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
    """Esegue un singolo comando Redis."""
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

    _cmd("ZREMRANGEBYSCORE", REPORTS_ZSET_KEY, 0, cutoff)

    keys = _cmd("ZRANGE", REPORTS_ZSET_KEY, 0, -1)
    if not keys:
        return []

    get_commands = [["GET", k] for k in keys]
    results = _pipeline(get_commands)
    if not results:
        return []

    reports = []
    for res in reversed(results):
        raw = res.get("result")
        if not raw:
            continue
        try:
            report  = json.loads(raw)
            expires = datetime.fromisoformat(report.get("expires_at", ""))
            if expires > now:
                reports.append(report)
        except Exception:
            continue

    return reports
