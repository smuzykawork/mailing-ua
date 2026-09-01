"""Розширення eSputnik API для модуля розсилок.

Дотримується правил із esputnik-skill:
  Rule 1  smartsend body = {"recipients": [...], "email": true} і НІЧОГО більше
  Rule 2  HTML живе тільки в повідомленні eSputnik (створюємо/оновлюємо через /messages/email)
  Rule 3  плейсхолдери перевіряються у validators.py до відправки
  Rule 4/5 голий IP і data-URI перевіряються у validators.py
Auth: BasicAuth(login_email, api_key) — через існуючий _auth() із app/services/esputnik.py.
"""
import asyncio
import logging
import os
import re
import time
from typing import Any

import httpx

import os


def _auth() -> tuple[str, str]:
    """(login_email, api_key) із секретів репозиторію (GitHub Actions → Secrets)."""
    login, key = os.getenv("ESPUTNIK_LOGIN_EMAIL", ""), os.getenv("ESPUTNIK_API_KEY", "")
    if not login or not key:
        raise RuntimeError("Немає секретів ESPUTNIK_LOGIN_EMAIL / ESPUTNIK_API_KEY у налаштуваннях репозиторію")
    return login, key

BASE = "https://esputnik.com/api/v1"
log = logging.getLogger("mailer.esputnik")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE, auth=httpx.BasicAuth(*_auth()), timeout=60.0)


# eSputnik лімітує частоту запитів (на живому акаунті /contacts дав 429 на другому запиті поспіль).
# Тому всі виклики йдуть не частіше ніж MIN_GAP с, а на 429 — чекаємо і повторюємо.
MIN_GAP = float(os.getenv("ESPUTNIK_MIN_GAP", "1.2"))
_pace_lock = asyncio.Lock()
_last_call = 0.0


async def _paced():
    global _last_call
    async with _pace_lock:
        wait = MIN_GAP - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()


async def _request(c: httpx.AsyncClient, method: str, path: str, *, attempts: int = 6, **kw) -> httpx.Response:
    """Запит із дотриманням паузи та повтором на 429/5xx (Retry-After або 3·2ⁿ секунд)."""
    delay = 3.0
    for i in range(attempts):
        await _paced()
        r = await c.request(method, path, **kw)
        if r.status_code == 429 or 500 <= r.status_code < 600:
            ra = r.headers.get("Retry-After")
            pause = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
            log.warning("%s %s -> %s, retry in %.0fs (%s/%s)", method, path, r.status_code, pause, i + 1, attempts)
            await asyncio.sleep(pause)
            delay = min(delay * 2, 60)
            continue
        return r
    return r


# ---------- групи ----------

async def list_groups() -> list[dict]:
    async with _client() as c:
        r = await _request(c, "GET", "/groups", params={"startindex": 1, "maxrows": 500})
        r.raise_for_status()
        data = r.json()
    items = data if isinstance(data, list) else (data.get("groups") or data.get("items") or [])
    out = []
    for g in items:
        if not g.get("id"):
            continue
        out.append({
            "id": int(g["id"]),
            "name": g.get("name") or str(g["id"]),
            "contacts_count": g.get("contactsCount") or g.get("contactCount"),
        })
    return out


def _extract_emails(data: Any) -> list[str]:
    if isinstance(data, dict):
        data = data.get("emails") or data.get("contacts") or data.get("results") or []
    out = []
    for item in data or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            e = item.get("email") or item.get("value") or item.get("locator")
            if not e:
                for ch in item.get("channels", []) or []:
                    if ch.get("type") == "email" and ch.get("value"):
                        e = ch["value"]
                        break
            if e:
                out.append(e)
    return out


async def group_contacts_page(c: httpx.AsyncClient, group_id: int, start: int, maxrows: int = 500) -> list:
    """Документований метод «Get contacts from a segment»: GET /group/{id}/contacts (перевірено 09.2026)."""
    r = await _request(c, "GET", f"/group/{group_id}/contacts", params={"startindex": start, "maxrows": maxrows})
    r.raise_for_status()
    page = r.json()
    return page.get("contacts", page) if isinstance(page, dict) else page


async def group_contacts_count(group_id: int, max_pages: int = 20) -> int:
    """Скільки контактів у групі (API кількість не віддає — гортаємо сторінками по 500, не більше max_pages)."""
    total = 0
    async with _client() as c:
        for i in range(max_pages):
            contacts = await group_contacts_page(c, group_id, 1 + i * 500)
            total += len(contacts)
            if len(contacts) < 500:
                return total
    return total  # 10 000+ — далі не рахуємо


async def group_emails(group_id: int) -> list[str]:
    """Email-адреси групи (потрібно ЛИШЕ для тестів/діагностики: розсилка йде через broadcast без вивантаження адрес)."""
    emails: list[str] = []
    async with _client() as c:
        start = 1
        while True:
            contacts = await group_contacts_page(c, group_id, start)
            emails.extend(_extract_emails(contacts))
            if len(contacts) < 500:
                return emails
            start += 500


# ---------- повідомлення (шаблон з HTML) ----------

def _from_header(from_: str) -> str:
    """eSputnik зберігає відправника як '"Імʼя" <email>' — беремо імʼя в лапки, якщо їх немає."""
    m = re.match(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$', from_ or "")
    if m and m.group(1).strip():
        return f'"{m.group(1).strip()}" <{m.group(2).strip()}>'
    return from_


def _message_body(*, name: str, subject: str, html: str, plain_text: str, from_: str) -> dict:
    # Поля методу "Add/Update email message" — ті самі, що повертає GET /messages/email/{id} на живому акаунті:
    # name, from, subject, htmlText, plainText. Якщо API відповість 400 — текст відповіді потрапить у last_error.
    return {"name": name[:100], "from": _from_header(from_), "subject": subject, "htmlText": html, "plainText": plain_text or ""}


async def create_message(**kw) -> int:
    async with _client() as c:
        r = await _request(c, "POST", "/messages/email", json=_message_body(**kw))
        if r.status_code >= 400:
            raise RuntimeError(f"eSputnik create message {r.status_code}: {r.text[:300]}")
        data = r.json() if r.text.strip() else {}
    mid = data.get("id") if isinstance(data, dict) else data
    if not mid:
        raise RuntimeError(f"eSputnik не повернув id повідомлення: {str(data)[:200]}")
    return int(mid)


async def update_message(message_id: int, **kw) -> None:
    async with _client() as c:
        r = await _request(c, "PUT", f"/messages/email/{message_id}", json=_message_body(**kw))
        if r.status_code >= 400:
            raise RuntimeError(f"eSputnik update message {r.status_code}: {r.text[:300]}")


async def message_exists(message_id: int) -> bool:
    """Pre-send перевірка: GET /messages/email/{id} має повернути 200 (шлях /message/{id} на живому акаунті дає 404)."""
    async with _client() as c:
        r = await _request(c, "GET", f"/messages/email/{message_id}")
        return r.status_code == 200


# ---------- відправка і статус ----------

async def smartsend(message_id: int, recipients: list[str]) -> dict:
    """Send prepared message — для ТЕСТОВИХ листів (акаунт має ліміт ~100 одиночних листів/годину).
    Rule 1: тіло мінімальне — recipients + email:true. Формат recipients за OpenAPI: [{"locator": email}]."""
    if len(recipients) > 1000:
        raise ValueError("smartsend приймає до 1000 адрес за запит")
    async with _client() as c:
        body = {"recipients": [{"locator": e} for e in recipients], "email": True}
        r = await _request(c, "POST", f"/message/{message_id}/smartsend", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"eSputnik smartsend {r.status_code}: {r.text[:300]}")
        return r.json() if r.text.strip() else {}


# ---------- broadcast: масова розсилка на групи (без ліміту одиночних листів, адреси не вивантажуються) ----------

async def create_broadcast(*, title: str, message_id: int, group_ids: list[int], excluded_group_ids: list[int] | None = None) -> str:
    """POST /broadcast — eSputnik сам дедуплікує адреси між групами і не шле відписаним. Повертає broadcastId."""
    body: dict = {"title": title[:200], "messageId": str(message_id), "groups": [int(g) for g in group_ids]}
    if excluded_group_ids:
        body["excludedGroups"] = [int(g) for g in excluded_group_ids]
    async with _client() as c:
        r = await _request(c, "POST", "/broadcast", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"eSputnik broadcast {r.status_code}: {r.text[:300]}")
        data = r.json() if r.text.strip() else {}
    bid = data.get("broadcastId") if isinstance(data, dict) else None
    if not bid:
        raise RuntimeError(f"eSputnik не повернув broadcastId: {str(data)[:200]}")
    return str(bid)


async def cancel_broadcast(broadcast_id: str) -> bool:
    async with _client() as c:
        r = await _request(c, "DELETE", f"/broadcast/{broadcast_id}")
        return r.status_code < 400


async def activity_stats(*, message_id: int | None, broadcast_id: str | None, date_from: str, date_to: str) -> dict | None:
    """Статистика через GET /v2/contacts/activity (вмикається підтримкою eSputnik на запит). None — недоступно."""
    counts = {"delivered": 0, "opened": 0, "clicked": 0, "failed": 0, "unsubscribed": 0}
    offset = None
    async with _client() as c:
        for _ in range(40):
            params = {"dateFrom": date_from, "dateTo": date_to, "maxrows": 25000}
            if offset is not None:
                params["offset"] = offset
            r = await _request(c, "GET", "https://esputnik.com/api/v2/contacts/activity", params=params)
            if r.status_code in (400, 403, 404):  # 400 "Contact support to enable ContactActivity." — ще не ввімкнено
                return None
            r.raise_for_status()
            rows = r.json() or []
            for a in rows:
                same = (broadcast_id and str(a.get("broadcastId") or "") == str(broadcast_id)) or \
                       (message_id and str(a.get("messageId") or "") == str(message_id))
                if not same:
                    continue
                st = str(a.get("activityStatus") or "").upper()
                if st == "DELIVERED":
                    counts["delivered"] += 1
                elif st == "READ":
                    counts["opened"] += 1
                elif st == "CLICKED":
                    counts["clicked"] += 1
                elif st == "UNDELIVERED":
                    counts["failed"] += 1
                elif st == "UNSUBSCRIBED":
                    counts["unsubscribed"] += 1
            if len(rows) < 25000:
                break
            offset = rows[-1].get("offset")
            if offset is None:
                break
    counts["delivered"] += counts["opened"] + counts["clicked"]  # READ/CLICKED означають доставлено
    return counts


def extract_request_ids(resp: dict | list | None) -> list[str]:
    results = resp.get("results") if isinstance(resp, dict) else resp
    if isinstance(results, dict):
        results = [results]
    ids = []
    for item in results or []:
        if isinstance(item, dict) and item.get("requestId"):
            ids.append(str(item["requestId"]))
    return ids


async def fetch_status(request_ids: list[str]) -> list[dict]:
    """GET /message/email/status?ids=... — auth ОБОВʼЯЗКОВО BasicAuth(login, key), не ("", key)."""
    out: list[dict] = []
    async with _client() as c:
        for i in range(0, len(request_ids), 100):
            chunk = request_ids[i:i + 100]
            r = await _request(c, "GET", "/message/email/status", params={"ids": ",".join(chunk)})
            if r.status_code >= 400:
                log.warning("status %s: %s", r.status_code, r.text[:200])
                continue
            data = r.json()
            results = data.get("results") if isinstance(data, dict) else data
            if isinstance(results, dict):
                results = [results]
            out.extend([x for x in results or [] if isinstance(x, dict)])
    return out
