"""Render the standalone landing page served by an entry node's nginx."""
import argparse
from html import escape
from http.client import HTTPException
import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


STYLE = """
:root { color-scheme: light; --paper: #f5f2e9; --ink: #1c2924; --muted: #4a5a50; --accent: #bd572f; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 1rem/1.55 system-ui,sans-serif; }
main { max-width: 74rem; margin: auto; padding: clamp(1.3rem,5vw,4.2rem); }
header { display:flex; align-items:center; gap:.75rem; padding-bottom:1rem; border-bottom:2px solid; font-weight:650; }
header::before { content:''; width:1rem; height:1rem; background:var(--accent); border-radius:50%; }
h1,h2 { font-family: 'Iowan Old Style', Georgia, serif; font-weight:400; line-height:1.05; }
h1 { font-size:clamp(3.1rem,8vw,6.8rem); margin:0 0 1.5rem; letter-spacing:-.045em; overflow-wrap:anywhere; }
h2 { font-size:clamp(1.75rem,3vw,2.5rem); margin:0 0 1rem; }
.hero { display:grid; grid-template-columns:minmax(0,1.2fr) minmax(0,1fr); align-items:center; gap:clamp(2rem,6vw,5rem); padding-block:clamp(3rem,7vw,6rem); }
.lede { color:var(--muted); max-width:39rem; font-size:clamp(1.05rem,2vw,1.32rem); overflow-wrap:anywhere; }
.notes { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:2rem; padding-top:2rem; border-top:1px solid var(--ink); }
.notes p { max-width:36rem; }
.art { min-height:15rem; aspect-ratio:1.2; overflow:hidden; position:relative; background:#b3baa3; }
.circle::before { content:''; position:absolute; width:80%; aspect-ratio:1; left:-15%; top:14%; border-radius:50%; background:#203e35; }
.circle::after { content:''; position:absolute; width:62%; height:130%; left:61%; top:-8%; background:#cf673e; transform:rotate(25deg); }
.fold { background:linear-gradient(140deg,#d5b485 0 33%,#2d4b42 33% 48%,#a7b8a8 48% 67%,#e1c9a2 67%); }
.initial { display:grid; place-items:center; background:#274637; color:#faf2d7; font:clamp(6rem,16vw,14rem)/1 Georgia,serif; }
a { color:var(--ink); text-underline-offset:.25em; }
a:focus-visible { outline:3px solid var(--accent); outline-offset:4px; }
@media(max-width:650px) { .hero,.notes { grid-template-columns:1fr; } .art { min-height:10rem; max-height:17rem; } }
"""


def document(title, header, hero, notes):
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{escape(title)}</title><style>{STYLE}</style></head>
<body><main><header>{escape(header)}</header><section class="hero">{hero}</section>
<section class="notes">{notes}</section></main></body></html>"""


STUDIO = (
    "Этюды формы", "Графическая студия",
    '<div><h1>Этюды формы</h1><p class="lede">Круг, плоскость и срез. Небольшая коллекция '
    'композиций, построенных на взаимодействии простых фигур.</p></div>'
    '<div class="art circle" role="img" aria-label="Тёмно-зелёный круг и наклонная терракотовая плоскость"></div>',
    '<article><h2>Ритм</h2><p>Дуга удерживает взгляд у левого края. Диагональ справа задаёт '
    'второе движение и оставляет между фигурами узкую полосу фона.</p></article>'
    '<article><h2>Палитра</h2><p>Хвойный зелёный, шалфейный и обожжённая глина. Светлый фон '
    'связывает три цвета и сохраняет контраст на линиях пересечения.</p></article>',
)
JOURNAL = (
    "Бумажные формы", "Журнал о бумаге",
    '<div><h1>Бумажные формы</h1><p class="lede">Полевые заметки о сгибах, свете и том, '
    'как один лист превращается в объём.</p></div>'
    '<div class="art fold" role="img" aria-label="Диагональные полосы света и тени на сложенном листе"></div>',
    '<article><h2>Первый сгиб</h2><p>Сложите лист пополам и приоткройте его под углом. '
    'Поверните сгиб к окну. Обе плоскости одного цвета, но освещённая сторона выглядит светлее.</p></article>'
    '<article><h2>Боковой свет</h2><p>Разверните лист ребром к источнику света. '
    'Складка даст узкую тень. Плавно меняйте угол и наблюдайте, как тень расширяется и смягчается.</p></article>',
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_profile(username):
    if not re.fullmatch(r"(?=.{1,39}$)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", username):
        raise ValueError("Введите логин GitHub из латинских букв, цифр и одиночных дефисов (до 39 символов).")
    request = Request(f"https://api.github.com/users/{username}", headers={
        "Accept": "application/vnd.github+json", "User-Agent": "anet-site-installer",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            payload = response.read(65537)
    except HTTPError as error:
        error.close()
        if error.code == 404:
            raise ValueError("Публичный профиль GitHub с таким логином не найден.") from error
        if error.code in (403, 429):
            raise ValueError("GitHub ограничил доступ. Повторите позже или выберите локальный шаблон.") from error
        raise ValueError(f"GitHub вернул HTTP {error.code}. Проверьте логин или выберите локальный шаблон.") from error
    except (OSError, HTTPException, URLError) as error:
        raise ValueError("Не удалось связаться с GitHub. Проверьте сеть или выберите локальный шаблон.") from error
    if len(payload) > 65536:
        raise ValueError("Ответ GitHub превышает допустимый размер.")
    try:
        profile = json.loads(payload)
    except ValueError as error:
        raise ValueError("GitHub вернул некорректный профиль.") from error
    if not isinstance(profile, dict) or not isinstance(profile.get("login"), str) or profile["login"].lower() != username.lower():
        raise ValueError("Логин в ответе GitHub не совпадает с запрошенным.")
    return profile


def render_site(kind: str, github_user: str = "") -> str:
    if kind in ("default", "studio"):
        return document(*STUDIO)
    if kind == "journal":
        return document(*JOURNAL)
    if kind != "github":
        raise ValueError("Неизвестный шаблон сайта.")
    profile = public_profile(github_user)
    login = profile["login"]
    name = profile.get("name") or login
    bio = profile.get("bio") or ""
    location = profile.get("location") or ""
    count = profile.get("public_repos")
    if not all(isinstance(field, str) for field in (name, bio, location)) or type(count) is not int or count < 0:
        raise ValueError("GitHub вернул некорректные поля профиля.")
    address = "https://github.com/" + github_user
    hero = (f'<div><h1>{escape(name)}</h1><p class="lede">{escape(bio)}</p></div>'
            f'<div class="art initial" aria-hidden="true">{escape(name[:1].upper())}</div>')
    notes = (f'<article><h2>Профиль</h2><p>{escape(location)}</p><a href="{address}">@{escape(login)}</a></article>'
             f'<article><h2>Репозитории</h2><p>Публичных репозиториев: {count}</p>'
             f'<a href="{address}?tab=repositories">Посмотреть на GitHub</a></article>')
    return document(name, "Профиль GitHub", hero, notes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("default", "studio", "journal", "github"))
    parser.add_argument("--github-user", default="")
    args = parser.parse_args()
    try:
        print(render_site(args.kind, args.github_user))
    except ValueError as error:
        sys.exit(str(error))
