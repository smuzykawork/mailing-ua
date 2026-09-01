"""Розширення eSputnik API для модуля розсилок.

Дотримується правил із esputnik-skill:
  Rule 1  smartsend body = {"recipients": [...], "email": true} і НІЧОГО більше
  Rule 2  HTML живе тільки в повідомленні eSputnik (створюємо/оновлюємо через /messages/email)
  Rule 3  плейсхолдери перевіряються у validators.py до відправки
  Rule 4/5 голий IP і data-URI перевіряються у validators.py
Auth: BasicAuth(login_email, api_key) — через існуючий _auth() із app/services/esputnik.py.
"""
import logging
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


# ---------- групи ----------

async def list_groups() -> list[dict]:
    async with _client() as c:
        r = await c.get("/groups", params={"startindex": 1, "maxrows": 500})
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


async def group_emails(group_id: int) -> list[str]:
    """Усі email-адреси групи. Основний шлях — перевірений у проєкті GET /contact/email?groupIds=.
    Якщо відповідь не 200 — fallback на посторінковий GET /contacts?groupId=."""
    async with _client() as c:
        r = await c.get("/contact/email", params={"groupIds": group_id})
        if r.status_code == 200:
            return _extract_emails(r.json())
        log.warning("GET /contact/email?groupIds=%s -> %s, fallback to /contacts", group_id, r.status_code)
        emails, start = [], 1
        while True:
            r = await c.get("/contacts", params={"groupId": group_id, "startindex": start, "maxrows": 500})
            r.raise_for_status()
            page = r.json()
            contacts = page.get("contacts", page) if isinstance(page, dict) else page
            if not contacts:
                break
            emails.extend(_extract_emails(contacts))
            if len(contacts) < 500:
                break
            start += 500
        return emails


# ---------- повідомлення (шаблон з HTML) ----------

def _message_body(*, name: str, subject: str, html: str, plain_text: str, from_: str) -> dict:
    # Поля методу "Add/Update email message" eSputnik. Якщо API відповість 400 — текст відповіді потрапить у last_error кампанії.
    return {"name": name[:100], "from": from_, "subject": subject, "htmlText": html, "plainText": plain_text or ""}


async def create_message(**kw) -> int:
    async with _client() as c:
        r = await c.post("/messages/email", json=_message_body(**kw))
        if r.status_code >= 400:
            raise RuntimeError(f"eSputnik create message {r.status_code}: {r.text[:300]}")
        data = r.json() if r.text.strip() else {}
    mid = data.get("id") if isinstance(data, dict) else data
    if not mid:
        raise RuntimeError(f"eSputnik не повернув id повідомлення: {str(data)[:200]}")
    return int(mid)


async def update_message(message_id: int, **kw) -> None:
    async with _client() as c:
        r = await c.put(f"/messages/email/{message_id}", json=_message_body(**kw))
        if r.status_code >= 400:
            raise RuntimeError(f"eSputnik update message {r.status_code}: {r.text[:300]}")


async def message_exists(message_id: int) -> bool:
    """Пункт 1 pre-send чекліста: GET /message/{id} має повернути 200."""
    async with _client() as c:
        r = await c.get(f"/message/{message_id}")
        return r.status_code == 200


# ---------- відправка і статус ----------

async def smartsend(message_id: int, recipients: list[str]) -> dict:
    """Rule 1: тіло МІНІМАЛЬНЕ. Жодних fromName/from/html/subject/replyTo."""
    if len(recipients) > 1000:
        raise ValueError("smartsend приймає до 1000 адрес за запит")
    async with _client() as c:
        r = await c.post(f"/message/{message_id}/smartsend", json={"recipients": recipients, "email": True})
        if r.status_code >= 400:
            raise RuntimeError(f"eSputnik smartsend {r.status_code}: {r.text[:300]}")
        return r.json() if r.text.strip() else {}


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
            r = await c.get("/message/email/status", params={"ids": ",".join(chunk)})
            if r.status_code >= 400:
                log.warning("status %s: %s", r.status_code, r.text[:200])
                continue
            data = r.json()
            results = data.get("results") if isinstance(data, dict) else data
            if isinstance(results, dict):
                results = [results]
            out.extend([x for x in results or [] if isinstance(x, dict)])
    return out
