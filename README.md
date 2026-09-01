# Mailing UA — розсилки через eSputnik на GitHub (усе в корені репозиторію)

Усі файли лежать у корені — так їх можна залити через веб-інтерфейс GitHub одним перетягуванням, без тек.
Теки `data/` (шаблони, черга, групи) і `media/` (фото) адмінка створить сама при першому записі.

## Кроки

1. Репозиторій `mailing-ua` (Public) → **Add file → Upload files** → перетягнути всі файли з цієї теки, крім `mailer.yml`
   і `README.md` можна теж → **Commit changes**.
2. Вкладка **Actions** → **set up a workflow yourself** → у полі назви файлу замінити `main.yml` на `mailer.yml` →
   стерти шаблонний текст і вставити вміст файлу `mailer.yml` → **Commit changes**.
3. **Settings → Pages** → Source: Deploy from a branch → `main` / `/ (root)` → Save. Через хвилину вгорі буде адреса
   `https://<логін>.github.io/mailing-ua/`.
4. **Settings → Secrets and variables → Actions → New repository secret**: `ESPUTNIK_LOGIN_EMAIL`, `ESPUTNIK_API_KEY`.
5. Токен = пароль входу: фото профілю → Settings → Developer settings → Personal access tokens → Fine-grained →
   Generate new token: Only select repositories → `mailing-ua`; Contents — Read and write; Actions — Read and write.
6. Відкрити адресу з кроку 3 → вставити токен → **Увійти** → Групи отримувачів → Оновити з eSputnik.

## Файли

`index.html`, `admin.js` — адмінка · `mailer_worker.py`, `esputnik_ext.py`, `validators.py` — воркер відправки (GitHub Actions) ·
`mailer.yml` — workflow (має лежати як `.github/workflows/mailer.yml`, крок 2) · `logo-*.png` — логотипи брендів у шапку ·
`.nojekyll` — щоб Pages віддавав файли як є. Вихідний код адмінки (`src/MailingAdmin.jsx`) — в архіві `mailing-ua-github.zip`.
