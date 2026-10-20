#!/usr/bin/env python3
"""cs-turnir: ссылка на турнир CS2 (Liquipedia / HLTV) -> таблица команд с FACEIT Elo.

Запуск:
  python3 app/elo_app.py                 веб-страница на http://localhost:8765
  python3 app/elo_app.py <ссылка|demo>   таблица прямо в терминале
Только стандартная библиотека Python 3.9+ и системный curl.
"""
import csv
import datetime
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(ROOT, "results")
DEMO_FILE = os.path.join(ROOT, "app", "demo_tournament.json")
PORT = 8765
LP_UA = "cs-turnir-elo/0.1 (personal tournament Elo checker)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


class FetchError(Exception):
    pass


# ---------------------------------------------------------------- HTTP
def http_get(url, ua, timeout=25):
    r = subprocess.run(["curl", "-s", "-L", "--compressed", "--max-time", str(timeout),
                        "-A", ua, "-w", "\n%{http_code}", url], capture_output=True)
    out = r.stdout.decode("utf-8", "replace")
    body, _, code = out.rpartition("\n")
    try:
        return int(code), body
    except ValueError:
        return 0, body


_last_lp = [0.0]


def lp_api(params):
    """Liquipedia API, не чаще 1 запроса в 2 секунды (их правило)."""
    url = "https://liquipedia.net/counterstrike/api.php?" + urllib.parse.urlencode(params)
    code = 0
    for attempt in range(3):
        wait = 2.2 - (time.time() - _last_lp[0])
        if wait > 0:
            time.sleep(wait)
        code, body = http_get(url, LP_UA, 40)
        _last_lp[0] = time.time()
        if code == 200:
            try:
                return json.loads(body)
            except ValueError:
                pass
        time.sleep(8 * (attempt + 1))
    raise FetchError("Liquipedia не ответила (код %s). Попробуйте через минуту." % code)


_last_fc = [0.0]


def faceit_get(path):
    for attempt in range(3):
        wait = 0.35 - (time.time() - _last_fc[0])
        if wait > 0:
            time.sleep(wait)
        code, body = http_get("https://www.faceit.com" + path, BROWSER_UA, 15)
        _last_fc[0] = time.time()
        if code == 404:
            return None
        if code == 200:
            try:
                return json.loads(body).get("payload")
            except ValueError:
                pass
        time.sleep(20 * (attempt + 1))  # 429 / защита — ждём и пробуем ещё
    return None


# ---------------------------------------------------------------- Liquipedia
def liquipedia_page(url):
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(u.query)
    if "title" in q:
        return q["title"][0]
    m = re.match(r"/counterstrike/(.+)", u.path)
    if not m:
        raise FetchError("Это не ссылка на раздел Counter-Strike в Liquipedia.")
    return urllib.parse.unquote(m.group(1)).replace("_", " ")


LINK_RE = re.compile(r'<a\s[^>]*?href="([^"]*)"[^>]*?title="([^"]*)"[^>]*>(.*?)</a>', re.S)


def _text(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _player_from_link(m):
    href, title, inner = m.group(1), html.unescape(m.group(2)), _text(m.group(3))
    page = None
    if "redlink=1" not in href:
        page = title.replace(" (page does not exist)", "")
    return {"name": inner or title, "page": page}


def parse_participants(h):
    h = h.replace("&#95;", "_")
    teams, seen = [], set()
    # Новый формат карточек (2024+)
    for card in re.split(r'class="[^"]*\bteam-participant-card"', h)[1:]:
        m = re.search(r'<span class="name"[^>]*>(.*?)</span>', card, re.S)
        name = _text(m.group(1)) if m else "?"
        i = card.find('data-toggle-area-content="1"')
        region = card
        if i >= 0:
            j = card.find('data-toggle-area-content="', i + 10)
            region = card[i:j if j > 0 else len(card)]
        players = []
        for piece in region.split('team-participant-card__member"')[1:]:
            role = re.search(r'member-role[^"]*">(.*?)<', piece, re.S)
            if role and "coach" in role.group(1).lower():
                continue
            lm = LINK_RE.search(piece)
            if lm:
                players.append(_player_from_link(lm))
        if name in seen or not players:
            continue
        seen.add(name)
        teams.append({"name": name, "players": players})
    if teams:
        return teams
    # Старый формат (teamcard) для турниров прошлых лет
    for card in re.split(r'<div class="teamcard[^"]*"', h)[1:]:
        m = re.search(r"<center>(.*?)</center>", card, re.S)
        name = _text(m.group(1)) if m else "?"
        t = re.search(r"<table.*?</table>", card, re.S)
        players = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", t.group(0) if t else "", re.S):
            th = re.search(r"<th[^>]*>(.*?)</th>", row, re.S)
            if not th or not _text(th.group(1)).isdigit():
                continue
            lm = LINK_RE.search(row[th.end():])
            if lm:
                players.append(_player_from_link(lm))
        if name in seen or not players:
            continue
        seen.add(name)
        teams.append({"name": name, "players": players})
    return teams


def lp_faceit_refs(pages, log):
    """Из исходного текста страниц игроков берём |faceitdb=<id> и |faceit=<ник>, по 50 за запрос."""
    refs = {}
    pages = [p for p in dict.fromkeys(pages) if p]
    for i in range(0, len(pages), 50):
        batch = pages[i:i + 50]
        log("Liquipedia: страницы игроков %d–%d из %d" % (i + 1, i + len(batch), len(pages)))
        d = lp_api({"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
                    "format": "json", "formatversion": "2", "redirects": "1",
                    "titles": "|".join(batch)})
        q = d.get("query", {})
        alias = {}
        for n in q.get("normalized", []) + q.get("redirects", []):
            alias[n["from"]] = n["to"]
        content = {}
        for p in q.get("pages", []):
            revs = p.get("revisions")
            if revs:
                content[p["title"]] = revs[0]["slots"]["main"]["content"]
        for t in batch:
            final = t
            for _ in range(3):
                final = alias.get(final, final)
            txt = content.get(final, "")
            u = re.search(r"\|\s*faceitdb\s*=\s*([0-9a-f-]{36})", txt, re.I)
            n = re.search(r"\|\s*faceit\s*=\s*([^\s|}]+)", txt, re.I)
            nick = n.group(1).rstrip("/").split("/")[-1] if n else None
            refs[t] = {"uuid": u.group(1) if u else None, "nick": nick}
    return refs


def faceit_lookup(ref):
    p, how = None, ""
    if ref.get("uuid"):
        p, how = faceit_get("/api/users/v1/users/" + ref["uuid"]), "id"
    if not p and ref.get("nick"):
        p, how = faceit_get("/api/users/v1/nicknames/" + urllib.parse.quote(ref["nick"])), "ник"
    if not p:
        return None
    cs2 = (p.get("games") or {}).get("cs2") or {}
    return {"faceit_nick": p.get("nickname"), "elo": cs2.get("faceit_elo"),
            "level": cs2.get("skill_level"), "how": how}


def hltv_to_liquipedia(url, log):
    """HLTV блокирует программы, поэтому ищем этот же турнир на Liquipedia по названию из ссылки."""
    m = re.search(r"/events/\d+/([^/?#]+)", url)
    if not m:
        raise FetchError("Нужна ссылка на турнир HLTV вида hltv.org/events/1234/название.")
    words = m.group(1).replace("-", " ")
    log("HLTV не пускает программы — ищу «%s» на Liquipedia" % words)
    d = lp_api({"action": "query", "list": "search", "srsearch": words, "srlimit": "30",
                "srnamespace": "0", "format": "json", "formatversion": "2"})
    want = set(words.lower().split())
    expand = {"iem": "intel extreme masters", "epl": "pro league", "esl": "esl"}
    best, best_score = None, 0.0
    for h in d.get("query", {}).get("search", []):
        title = h["title"]
        low = title.lower().replace("_", " ")
        if "/" not in title or re.search(r"matches|list of|results", low):
            continue  # страницы игроков, команд и списков не подходят
        if "qualifier" in low and "qualifier" not in want:
            continue
        score = sum(1 for w in want if w in low or (w in expand and expand[w] in low))
        score -= 0.5 * title.count("/")  # основная страница турнира лучше подстраниц
        if score > best_score:
            best, best_score = title, score
    if not best:
        raise FetchError("Не нашёл этот турнир на Liquipedia. Дайте ссылку с Liquipedia.")
    return best


# ---------------------------------------------------------------- основной сценарий
def run_tournament(url, log=print):
    url = url.strip()
    if url.lower() == "demo":
        log("Демо-режим: тестовые данные без интернета")
        with open(DEMO_FILE, encoding="utf-8") as f:
            demo = json.load(f)
        return finish(demo["title"], "demo", demo["teams"])

    host = urllib.parse.urlparse(url).netloc.lower()
    if "hltv.org" in host:
        page = hltv_to_liquipedia(url, log)
        log("Liquipedia: нашёл страницу «%s»" % page)
    elif "liquipedia.net" in host:
        page = liquipedia_page(url)
    else:
        raise FetchError("Нужна ссылка на турнир с liquipedia.net или hltv.org (или слово demo).")

    log("Liquipedia: загружаю страницу турнира")
    d = lp_api({"action": "parse", "page": page, "prop": "text", "format": "json", "redirects": "1"})
    if "error" in d:
        raise FetchError("Liquipedia: страница не найдена (%s)" % page)
    teams = parse_participants(d["parse"]["text"]["*"])
    if not teams:
        raise FetchError("На странице нет блока участников с составами. "
                         "Возможно, составы на подстранице этапа — дайте ссылку на неё.")
    total = sum(len(t["players"]) for t in teams)
    log("Найдено команд: %d, игроков: %d" % (len(teams), total))

    refs = lp_faceit_refs([p["page"] for t in teams for p in t["players"]], log)
    n = 0
    for t in teams:
        for p in t["players"]:
            n += 1
            log("FACEIT: %d из %d — %s" % (n, total, p["name"]))
            ref = refs.get(p["page"]) or {}
            r = faceit_lookup(ref) if p["page"] else None
            if r:
                p.update(r)
            else:
                p["elo"] = None
                if not p["page"]:
                    p["why"] = "нет страницы на Liquipedia"
                elif not ref.get("uuid") and not ref.get("nick"):
                    p["why"] = "на Liquipedia не указан FACEIT"
                else:
                    p["why"] = "FACEIT не отдал профиль"
    return finish(d["parse"]["title"], url, teams)


def finish(title, source, teams):
    for t in teams:
        elos = [p["elo"] for p in t["players"] if isinstance(p.get("elo"), int)]
        t["avg"] = round(sum(elos) / len(elos)) if elos else None
    teams.sort(key=lambda t: t["avg"] or -1, reverse=True)
    res = {"title": title, "source": source, "teams": teams,
           "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "found": sum(1 for t in teams for p in t["players"] if isinstance(p.get("elo"), int)),
           "total": sum(len(t["players"]) for t in teams)}
    res["csv"] = save_csv(res)
    return res


def save_csv(res):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    slug = re.sub(r"[^\w.-]+", "_", res["title"]).strip("_")[:60]
    path = os.path.join(RESULTS_DIR, "%s_%s.csv" % (datetime.date.today().isoformat(), slug))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Команда", "Игрок", "Ник на FACEIT", "Elo", "Уровень", "Среднее по команде"])
        for t in res["teams"]:
            for p in t["players"]:
                w.writerow([t["name"], p["name"], p.get("faceit_nick") or "",
                            p["elo"] if p.get("elo") is not None else "не найден",
                            p.get("level") or "", t["avg"] or ""])
    return path


def text_table(res):
    lines = []
    for t in res["teams"]:
        elos = " ".join(str(p["elo"]) if isinstance(p.get("elo"), int) else "?" for p in t["players"])
        lines.append("%s - %s (%s)" % (t["name"], elos, t["avg"] if t["avg"] else "?"))
    return "\n".join(lines)


# ---------------------------------------------------------------- веб-страница
JOB = {"state": "idle", "log": [], "result": None, "error": None, "url": ""}
JOB_LOCK = threading.Lock()

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--mut:#667;--line:#e2e5ea;--acc:#ff5500;--lo:#fdeeee}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--fg:#e8eaed;--mut:#99a;--line:#2c3138;--lo:#3a1d1d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}.mut{color:var(--mut)}
form{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}input[type=text]{flex:1;min-width:240px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font-size:15px}
button,.btn{padding:10px 16px;border:0;border-radius:8px;background:var(--acc);color:#fff;font-weight:600;cursor:pointer;text-decoration:none;font-size:15px}
.ghost{background:transparent;color:var(--mut);border:1px solid var(--line)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0}.grid .card{margin:0}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 6px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}th{font-size:14px}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.miss{color:var(--mut)}pre{white-space:pre-wrap;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;font-size:13px}
.err{background:var(--lo);border-color:transparent}.lvl{display:inline-block;min-width:20px;text-align:center;border-radius:4px;background:var(--line);font-size:11px;margin-left:6px}
"""


def page(body, refresh=False):
    meta = '<meta http-equiv="refresh" content="2">' if refresh else ""
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1">%s<title>CS2 Elo турнира</title><style>%s</style>'
            '</head><body><main>%s</main></body></html>' % (meta, CSS, body)).encode("utf-8")


def form_html(value=""):
    return ('<h1>FACEIT Elo участников турнира CS2</h1>'
            '<div class="mut">Вставьте ссылку на турнир с Liquipedia или HLTV. Для проверки без интернета — слово <b>demo</b>.</div>'
            '<form method="post" action="/run"><input type="text" name="url" '
            'placeholder="https://liquipedia.net/counterstrike/..." value="%s" autofocus>'
            '<button>Проверить</button></form>' % html.escape(value))


def result_html(res):
    e = html.escape
    cards = []
    for t in res["teams"]:
        cells = []
        for p in t["players"]:
            if isinstance(p.get("elo"), int):
                nick = p.get("faceit_nick") or ""
                alt = (' <span class="mut">(%s)</span>' % e(nick)) if nick and nick.lower() != p["name"].lower() else ""
                lvl = '<span class="lvl">%s</span>' % p["level"] if p.get("level") else ""
                cells.append("<tr><td>%s%s</td><td class=n>%d%s</td></tr>" % (e(p["name"]), alt, p["elo"], lvl))
            else:
                why = (' <span class="mut" title="%s">ⓘ</span>' % e(p["why"])) if p.get("why") else ""
                cells.append('<tr class="miss"><td>%s</td><td class=n>не найден%s</td></tr>' % (e(p["name"]), why))
        cards.append('<div class="card"><table><tr><th>%s</th><th class=n>%s</th></tr>%s</table></div>' % (
            e(t["name"]), ("сред. %s" % t["avg"]) if t["avg"] else "—", "".join(cells)))
    return ('<h2>%s</h2><div class="mut">Эло найдено у %d из %d игроков · %s · таблица сохранена: %s</div>'
            '<div class="grid" style="margin-top:12px">%s</div>'
            '<h3>Коротко (можно скопировать)</h3><pre>%s</pre>' % (
                e(res["title"]), res["found"], res["total"], e(res["date"]),
                e(os.path.relpath(res["csv"], ROOT)), "".join(cards), e(text_table(res))))


def start_job(url):
    with JOB_LOCK:
        if JOB["state"] == "running":
            return
        JOB.update(state="running", log=[], result=None, error=None, url=url)

    def log(msg):
        JOB["log"].append(msg)

    def work():
        try:
            JOB["result"] = run_tournament(url, log)
            JOB["state"] = "done"
        except FetchError as ex:
            JOB["error"], JOB["state"] = str(ex), "error"
        except Exception as ex:  # noqa
            JOB["error"], JOB["state"] = "Непредвиденная ошибка: %r" % ex, "error"
    threading.Thread(target=work, daemon=True).start()


QUIT = '<p style="margin-top:28px"><a class="btn ghost" href="/quit">Выключить приложение</a></p>'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/quit"):
            self.send(page("<h1>Приложение выключено</h1><p class=mut>Вкладку можно закрыть.</p>"))
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path.startswith("/job"):
            st = JOB["state"]
            if st == "running":
                body = (form_html(JOB["url"]) + '<div class="card"><b>Идёт проверка…</b> '
                        '<span class="mut">обычно 1–3 минуты, страница обновляется сама</span>'
                        '<pre>%s</pre></div>' % html.escape("\n".join(JOB["log"][-12:])))
                return self.send(page(body, refresh=True))
            if st == "error":
                return self.send(page(form_html(JOB["url"]) + '<div class="card err">%s</div>' % html.escape(JOB["error"]) + QUIT))
            if st == "done":
                return self.send(page(form_html(JOB["url"]) + result_html(JOB["result"]) + QUIT))
        self.send(page(form_html() + QUIT))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8"))
        url = (data.get("url") or [""])[0].strip()
        if url:
            start_job(url)
        self.redirect("/job")


def serve(open_browser=True):
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Открыто: http://localhost:%d" % PORT)
    if open_browser:
        webbrowser.open("http://localhost:%d" % PORT)
    srv.serve_forever()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        try:
            res = run_tournament(args[0], lambda m: print("  " + m, file=sys.stderr))
        except FetchError as ex:
            print("Ошибка: " + str(ex))
            sys.exit(1)
        print("\n" + res["title"])
        print(text_table(res))
        print("\nНайдено: %d из %d. CSV: %s" % (res["found"], res["total"], res["csv"]))
    else:
        serve(open_browser="--no-browser" not in sys.argv)
