"""Чекліст перед відправкою — ті самі правила, що в адмінці (validateDraft), плюс правила eSputnik.

Помилка (error) блокує постановку в чергу. Зауваження (warn) лише показується.
"""
import re

# Rule 3 eSputnik: незаповнені плейсхолдери -> silent INVALID_DATA_IN_MESSAGE
PLACEHOLDER_RE = re.compile(r"\[[A-ZА-ЯІЇЄҐ][^\]\n]{0,30}\]|\$\{[^}]*\}|%\{[^}]*\}|\{\{[^}]*\}\}|\$!?[A-Za-z_]+(?:\.[A-Za-z_]+)*")
# Rule 4: голий IP у листі = спам-фільтр
BARE_IP_RE = re.compile(r"https?://\d{1,3}(?:\.\d{1,3}){3}")
# Rule 5: data-URI картинки eSputnik викидає
DATA_URI_RE = re.compile(r"src=[\"']\s*data:", re.I)
HTTPS_RE = re.compile(r"^https://", re.I)
CONTACT_RE = re.compile(r"^(https://|mailto:|tel:)", re.I)
# Вимога бренду: кожен лист закінчується цим реченням (адмінка вставляє його з налаштувань)
FOOTER_REQUIRED = "Ви отримали цей лист"


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "")


def check_campaign(*, subject: str, content: dict, html: str, allowed_tokens: str = "") -> list[dict]:
    issues: list[dict] = []
    err = lambda m: issues.append({"level": "error", "msg": m})  # noqa: E731
    warn = lambda m: issues.append({"level": "warn", "msg": m})  # noqa: E731

    c = content or {}
    headline = (c.get("headline") or "").strip()
    body_html = c.get("bodyHtml") or ""
    buttons = c.get("buttons") or []
    contact = c.get("contact") or {}
    image = c.get("image") or {}
    preheader = c.get("preheader") or ""

    if not (subject or "").strip():
        err("Немає теми листа")
    elif len(subject) > 78:
        warn("Тема довша за 78 символів — у вхідних обріжеться")
    if not headline:
        err("Немає заголовка")
    if not _strip_tags(body_html).strip():
        err("Порожній текст листа")
    if not buttons:
        err("Потрібна щонайменше одна кнопка")
    for i, b in enumerate(buttons[:2], start=1):
        if not (b.get("text") or "").strip():
            err(f"Кнопка {i}: немає тексту")
        if not HTTPS_RE.match(b.get("url") or ""):
            err(f"Кнопка {i}: посилання має починатися з https://")
    if not (contact.get("text") or "").strip():
        err("Кнопка контактів: немає тексту")
    if not CONTACT_RE.match(contact.get("url") or ""):
        err("Кнопка контактів: потрібне https-посилання на сторінку звʼязку")

    scan = "\n".join([subject or "", preheader, headline, body_html, c.get("contactLead") or "", contact.get("text") or "",
                      *[b.get("text") or "" for b in buttons]])
    if allowed_tokens:
        for tok in [t.strip() for t in allowed_tokens.split(",") if t.strip()]:
            scan = scan.replace(tok, "")
    if PLACEHOLDER_RE.search(scan):
        err("Незаповнений плейсхолдер ([...], ${...}, {{...}}) — eSputnik мовчки відхилить лист")

    img_url = image.get("url") or ""
    if img_url and image.get("placement", "top") != "none":
        if img_url.lower().startswith("data:"):
            err("Фото вставлене як data-URI — eSputnik його викине")
        elif not HTTPS_RE.match(img_url):
            err("Фото має бути за https-адресою на сервері")
        if not (image.get("alt") or "").strip():
            warn("Фото без alt-тексту")

    if DATA_URI_RE.search(html or ""):
        err("У HTML є data-URI зображення")
    if BARE_IP_RE.search(html or ""):
        err("У листі є посилання на голий IP — миттєвий спам-фільтр")
    if FOOTER_REQUIRED not in (html or ""):
        err("У кінці листа немає обовʼязкового тексту «Ви отримали цей лист, оскільки підписані на новини Aclima»")
    if not preheader.strip():
        warn("Немає прехедера")
    return issues


def has_errors(issues: list[dict]) -> bool:
    return any(i["level"] == "error" for i in issues)
