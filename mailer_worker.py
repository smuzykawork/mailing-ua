"""Воркер розсилок для GitHub Actions. База даних — JSON-файли в репозиторії, черга — цей скрипт за розкладом.

Задачі (env TASK): sync_groups | process | stats | all (за замовчуванням).
Читає/пише файли через GitHub Contents API (щоб не конфліктувати з правками адмінки), eSputnik — через esputnik_ext.
Адреси отримувачів НІКОЛИ не зберігаються в репозиторії (він публічний) — лише кількості та requestId.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esputnik_ext as esp  # noqa: E402
from validators import check_campaign, has_errors  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("worker")

REPO = os.environ["GITHUB_REPOSITORY"]            # owner/repo — задає GitHub Actions
TOKEN = os.environ["GITHUB_TOKEN"]
BRANCH = os.getenv("GITHUB_REF_NAME", "main")
TASK = os.getenv("TASK", "all") or "all"
CAMPAIGN_ID = os.getenv("CAMPAIGN_ID", "").strip()
BATCH_SIZE = max(50, min(1000, int(os.getenv("MAILER_BATCH_SIZE", "500"))))
BATCH_PAUSE = float(os.getenv("MAILER_BATCH_PAUSE", "1.0"))
FROM_DEFAULT = os.getenv("ESPUTNIK_FROM", "")
GH = "https://api.github.com"

INDEX, GROUPS, SETTINGS = "data/campaigns.json", "data/groups.json", "data/settings.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


# ───────────────────────── GitHub Contents API ─────────────────────────

class Repo:
    def __init__(self):
        self.c = httpx.Client(base_url=GH, headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                                                    "X-GitHub-Api-Version": "2022-11-28"}, timeout=30)

    def read(self, path: str) -> tuple[object | None, str | None]:
        r = self.c.get(f"/repos/{REPO}/contents/{path}", params={"ref": BRANCH})
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        d = r.json()
        raw = base64.b64decode(d["content"]).decode("utf-8")
        return (json.loads(raw) if path.endswith(".json") else raw), d["sha"]

    def write(self, path: str, data, sha: str | None, message: str) -> str:
        body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1)
        payload = {"message": message, "content": base64.b64encode(body.encode("utf-8")).decode(), "branch": BRANCH}
        if sha:
            payload["sha"] = sha
        r = self.c.put(f"/repos/{REPO}/contents/{path}", json=payload)
        if r.status_code in (409, 422):  # хтось змінив файл — беремо свіжий sha і пробуємо ще раз
            raise ConflictError(path)
        r.raise_for_status()
        return r.json()["content"]["sha"]

    def update(self, path: str, fn, message: str, default=None, attempts: int = 6):
        """read-modify-write із повтором при конфлікті. fn(data) -> new data."""
        for _ in range(attempts):
            data, sha = self.read(path)
            new = fn(data if data is not None else default)
            try:
                self.write(path, new, sha, message)
                return new
            except ConflictError:
                continue
        raise RuntimeError(f"Не вдалося записати {path}: постійні конфлікти")


class ConflictError(Exception):
    pass


repo = Repo()


def upsert_index(campaign_id: str, patch: dict) -> list:
    def fn(index):
        index = index or []
        for c in index:
            if c["id"] == campaign_id:
                c.update(patch)
                break
        else:
            index.insert(0, {"id": campaign_id, **patch})
        return index
    return repo.update(INDEX, fn, f"mailer: {campaign_id} → {patch.get('status', 'update')}", default=[])


def index_entry(campaign_id: str) -> dict | None:
    index, _ = repo.read(INDEX)
    return next((c for c in (index or []) if c["id"] == campaign_id), None)


# ───────────────────────── задачі ─────────────────────────

async def sync_groups() -> int:
    remote = await esp.list_groups()
    if not remote:
        raise RuntimeError("eSputnik не повернув груп — перевірте секрети ESPUTNIK_*")
    existing, _ = repo.read(GROUPS)
    old = {g["id"]: g for g in (existing or [])}
    # API не віддає кількість контактів — рахуємо адреси по кожній групі. Виклики eSputnik самі тримають паузу
    # (ліміт частоти), тому для 54 груп це ~1–2 хвилини.
    for g in remote:
        if g.get("contacts_count") is None:
            try:
                g["contacts_count"] = await esp.group_contacts_count(g["id"])
            except Exception as e:  # noqa: BLE001
                log.warning("count group %s failed: %s", g["id"], str(e)[:120])
                g["contacts_count"] = old.get(g["id"], {}).get("contacts_count")
    now = iso(utcnow())
    test_id = int(os.getenv("ESPUTNIK_TEST_GROUP_ID", "202562060"))
    out = [{"id": g["id"], "name": g["name"], "contacts_count": g.get("contacts_count"),
            "is_test": old.get(g["id"], {}).get("is_test", g["id"] == test_id), "synced_at": now} for g in remote]
    out.sort(key=lambda g: (not g["is_test"], g["name"].lower()))
    repo.update(GROUPS, lambda _: out, f"mailer: sync {len(out)} groups", default=[])
    log.info("groups synced: %s", len(out))
    return len(out)


def due_campaigns(index: list) -> list:
    now = utcnow()
    out = []
    for c in index:
        if CAMPAIGN_ID and c["id"] != CAMPAIGN_ID:
            continue
        if c.get("status") == "queued":
            out.append(c)
        elif c.get("status") == "scheduled" and parse(c.get("scheduled_at")) and parse(c["scheduled_at"]) <= now:
            out.append(c)
    return out


async def process_one(entry: dict) -> None:
    """Відправка однієї розсилки.

    Групи → eSputnik broadcast (POST /broadcast): eSputnik сам дедуплікує адреси між групами, не шле відписаним,
    а ліміт «100 одиночних листів/годину» на broadcast не діє. Адреси отримувачів ніколи не вивантажуються.
    Тестові листи (test_emails) → smartsend на 1–10 адрес.
    """
    cid = entry["id"]
    log.info("campaign %s «%s»: start", cid, entry.get("name"))
    upsert_index(cid, {"status": "sending", "started_at": iso(utcnow()), "last_error": None})
    full, _ = repo.read(f"data/campaigns/{cid}.json")
    results, _ = repo.read(f"data/results/{cid}.json")
    results = results or {"request_ids": [], "batches": []}
    try:
        if not full:
            raise RuntimeError("Немає файлу data/campaigns/<id>.json")
        issues = check_campaign(subject=full["subject"], content=full.get("content") or {}, html=full["html"])
        if has_errors(issues):
            raise RuntimeError("Чекліст: " + "; ".join(i["msg"] for i in issues if i["level"] == "error"))

        from_ = f"{full.get('from_name')} <{full.get('from_email')}>" if full.get("from_name") else (full.get("from_email") or FROM_DEFAULT)
        mid = entry.get("esputnik_message_id")
        kw = dict(name=f"[MailingUA {cid}] {full['name']}", subject=full["subject"], html=full["html"], plain_text=full.get("plain_text", ""), from_=from_)
        if mid and await esp.message_exists(mid):
            await esp.update_message(mid, **kw)
        else:
            mid = await esp.create_message(**kw)
            if not await esp.message_exists(mid):
                raise RuntimeError(f"eSputnik: повідомлення {mid} не знайдено після створення")
        upsert_index(cid, {"esputnik_message_id": mid})

        if (index_entry(cid) or {}).get("status") == "cancelled":
            log.info("campaign %s cancelled before send", cid)
            return

        test_emails = [e.strip().lower() for e in (full.get("test_emails") or []) if e.strip()]
        if test_emails:
            resp = await esp.smartsend(mid, test_emails)
            ids = esp.extract_request_ids(resp)
            errors = [x for x in (resp.get("results") if isinstance(resp, dict) else resp) or [] if isinstance(x, dict) and x.get("status") == "ERROR"]
            if isinstance(errors, dict):
                errors = [errors]
            results["request_ids"].extend(ids)
            results["batches"].append({"size": len(test_emails), "status": "sent" if ids else "failed", "at": iso(utcnow())})
            repo.update(f"data/results/{cid}.json", lambda _: results, f"mailer: {cid} results", default={})
            if not ids:
                raise RuntimeError("eSputnik не прийняв тестовий лист: " + "; ".join(str(e.get("message")) for e in errors)[:300])
            upsert_index(cid, {"status": "sent", "finished_at": iso(utcnow()), "total_recipients": len(test_emails), "sent_count": len(ids),
                               "failed_count": len(test_emails) - len(ids), "last_error": ("; ".join(str(e.get("message")) for e in errors)[:300] or None)})
            log.info("campaign %s test sent to %s address(es)", cid, len(ids))
            return

        group_ids = [int(g) for g in (full.get("group_ids") or [])]
        if not group_ids:
            raise RuntimeError("Не вибрано жодної групи отримувачів")
        bid = await esp.create_broadcast(title=f"[MailingUA {cid}] {full['name']}", message_id=mid, group_ids=group_ids)
        total = int(entry.get("total_recipients") or 0)
        results["broadcast_id"] = bid
        repo.update(f"data/results/{cid}.json", lambda _: results, f"mailer: {cid} results", default={})
        upsert_index(cid, {"status": "sent", "finished_at": iso(utcnow()), "esputnik_broadcast_id": bid,
                           "sent_count": total, "failed_count": 0})
        log.info("campaign %s → eSputnik broadcast %s for %s group(s)", cid, bid, len(group_ids))
    except Exception as e:  # noqa: BLE001
        log.exception("campaign %s failed", cid)
        upsert_index(cid, {"status": "failed", "finished_at": iso(utcnow()), "last_error": str(e)[:500]})
    await notify(cid)


async def notify(cid: str) -> None:
    settings, _ = repo.read(SETTINGS)
    url = ((settings or {}).get("n8nWebhook") or os.getenv("MAILER_N8N_NOTIFY_URL") or "").strip()
    if not url:
        return
    c = index_entry(cid) or {}
    payload = {k: c.get(k) for k in ("id", "name", "subject", "status", "total_recipients", "sent_count", "failed_count", "last_error", "groups", "finished_at")}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(url, json=payload)
    except Exception as e:  # noqa: BLE001
        log.warning("notify failed: %s", e)


async def refresh_stats(campaign_id: str | None) -> int:
    index, _ = repo.read(INDEX)
    cutoff = utcnow() - timedelta(days=14)
    targets = [c for c in (index or []) if (c["id"] == campaign_id if campaign_id else
               (c.get("status") == "sent" and parse(c.get("finished_at")) and parse(c["finished_at"]) >= cutoff))]
    for c in targets:
        results, _ = repo.read(f"data/results/{c['id']}.json")
        results = results or {}
        ids = results.get("request_ids") or []
        stats = None
        if ids:  # тестові листи — статус кожного requestId
            stats = {"delivered": 0, "opened": 0, "clicked": 0, "failed": 0, "tracked": len(ids)}
            for st in await esp.fetch_status(ids):
                s = str(st.get("status") or "").upper()
                if str(st.get("delivered")).lower() == "true" or s in ("DELIVERED", "OPENED", "CLICKED", "READ"):
                    stats["delivered"] += 1
                if s in ("OPENED", "READ", "CLICKED"):
                    stats["opened"] += 1
                if s == "CLICKED":
                    stats["clicked"] += 1
                if str(st.get("failed")).lower() == "true" or s in ("FAILED", "ERROR", "BOUNCED"):
                    stats["failed"] += 1
        elif results.get("broadcast_id") or c.get("esputnik_message_id"):
            started = parse(c.get("started_at") or c.get("created_at")) or (utcnow() - timedelta(days=1))
            stats = await esp.activity_stats(message_id=c.get("esputnik_message_id"), broadcast_id=results.get("broadcast_id"),
                                             date_from=started.strftime("%Y-%m-%dT%H:%M:%S"), date_to=utcnow().strftime("%Y-%m-%dT%H:%M:%S"))
            if stats is None:
                stats = {"unavailable": True, "note": "Статистика броадкастів через API вмикається підтримкою eSputnik (ContactActivity). Поки що дивіться звіт у самому eSputnik."}
        upsert_index(c["id"], {"stats": stats, "stats_updated_at": iso(utcnow())})
    return len(targets)


async def main() -> None:
    log.info("task=%s repo=%s campaign=%s", TASK, REPO, CAMPAIGN_ID or "-")
    if TASK in ("sync_groups", "all"):
        groups, _ = repo.read(GROUPS)
        last = max((parse(g.get("synced_at")) for g in (groups or []) if g.get("synced_at")), default=None)
        if TASK == "sync_groups" or not last or utcnow() - last > timedelta(hours=6):
            await sync_groups()
    if TASK in ("process", "all"):
        index, _ = repo.read(INDEX)
        for entry in due_campaigns(index or []):
            await process_one(entry)
    if TASK in ("stats", "all"):
        await refresh_stats(CAMPAIGN_ID or None)
    log.info("done")


if __name__ == "__main__":
    asyncio.run(main())
