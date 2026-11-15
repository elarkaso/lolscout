#!/usr/bin/env python3
"""
lolscout – scouting soupeřů v League of Legends přes oficiální Riot API.

Vstup:
  * Riot match ID zápasu soupeře (např. EUN1_4023742408 – najdeš ho na stránce
    odehraného zápasu na Challengermode jako „Riot match ID“), nebo
  * Riot ID hráčů ve tvaru "Jméno#TAG".

Výstup: Markdown report – rank, forma, role, nejhranější championi,
mastery a kandidáti na ban pro celý tým.

Použití:
  export RIOT_API_KEY=RGAPI-...            (nebo soubor .env)
  python lolscout.py EUN1_4023742408 --us "MujNick#EUNE"
  python lolscout.py "Hrac1#EUNE" "Hrac2#EUNE" --games 30 --queue all
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

# --------------------------------------------------------------------------- konstanty

PLATFORMS = {"eune": "eun1", "euw": "euw1", "tr": "tr1", "ru": "ru", "na": "na1",
             "br": "br1", "lan": "la1", "las": "la2", "kr": "kr", "jp": "jp1", "oce": "oc1"}
REGIONAL = {"eun1": "europe", "euw1": "europe", "tr1": "europe", "ru": "europe",
            "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
            "kr": "asia", "jp1": "asia", "oc1": "sea"}
OPGG_REGION = {"eun1": "eune", "euw1": "euw", "tr1": "tr", "ru": "ru", "na1": "na", "br1": "br",
               "la1": "lan", "la2": "las", "kr": "kr", "jp1": "jp", "oc1": "oce"}
QUEUE_FILTER = {"ranked": "ranked", "solo": 420, "flex": 440, "all": None}
QUEUE_NAMES = {420: "Solo/Duo", 440: "Flex", 400: "Normal", 430: "Blind", 490: "Quickplay",
               450: "ARAM", 0: "Custom", 1700: "Arena", 1900: "URF", 900: "ARURF"}
ROLE_CZ = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
ROLE_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
TIER_CZ = {"IRON": "Iron", "BRONZE": "Bronze", "SILVER": "Silver", "GOLD": "Gold",
           "PLATINUM": "Platinum", "EMERALD": "Emerald", "DIAMOND": "Diamond",
           "MASTER": "Master", "GRANDMASTER": "Grandmaster", "CHALLENGER": "Challenger"}
MATCH_ID_RE = re.compile(r"^[A-Z]{2,4}\d?_\d+$")
REMAKE_SECONDS = 300


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- rate limit

class RateLimiter:
    """Klientský limiter podle limitů klíče (výchozí: personal/dev key 20/1 s a 100/120 s)."""

    def __init__(self, limits=((20, 1.0), (100, 120.0))):
        self.limits = limits
        self.window = max(p for _, p in limits)
        self.calls = collections.deque()

    def wait(self):
        while True:
            now = time.monotonic()
            while self.calls and now - self.calls[0] >= self.window:
                self.calls.popleft()
            pause = 0.0
            for n, per in self.limits:
                recent = [t for t in self.calls if now - t < per]
                if len(recent) >= n:
                    pause = max(pause, per - (now - recent[-n]) + 0.05)
            if pause <= 0:
                self.calls.append(now)
                return
            if pause > 2:
                log(f"  … rate limit, čekám {pause:.0f} s")
            time.sleep(pause)


# --------------------------------------------------------------------------- API klient

class Riot:
    def __init__(self, key, platform, cache_dir, limits):
        self.platform = platform
        self.regional = REGIONAL[platform]
        # account-v1 má jen americas/asia/europe (účet lze dohledat z kteréhokoli)
        self.account_host = "asia" if self.regional == "sea" else self.regional
        self.s = requests.Session()
        self.s.headers.update({"X-Riot-Token": key, "User-Agent": "lolscout/1.0"})
        self.cache = Path(cache_dir)
        self.limiters = collections.defaultdict(lambda: RateLimiter(limits))
        self.requests_made = 0

    # -- cache
    def _cache_path(self, key):
        return self.cache / (re.sub(r"[^\w.-]+", "_", key) + ".json")

    def _cache_get(self, key, ttl):
        p = self._cache_path(key)
        if p.exists() and (ttl is None or time.time() - p.stat().st_mtime < ttl):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                return None
        return None

    def _cache_put(self, key, data):
        p = self._cache_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")

    # -- HTTP
    def get(self, host, path, params=None, cache_key=None, ttl=None):
        if cache_key:
            hit = self._cache_get(cache_key, ttl)
            if hit is not None:
                return hit
        url = f"https://{host}.api.riotgames.com{path}"
        for attempt in range(6):
            self.limiters[host].wait()
            try:
                r = self.s.get(url, params=params, timeout=20)
            except requests.RequestException as e:
                log(f"  ! síťová chyba ({e}), zkouším znovu")
                time.sleep(2 * (attempt + 1))
                continue
            self.requests_made += 1
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "10"))
                log(f"  … Riot vrátil 429 (rate limit), čekám {wait} s")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403):
                sys.exit("Chyba: API klíč je neplatný nebo vypršel (vývojový klíč platí 24 h). "
                         "Vygeneruj nový na https://developer.riotgames.com")
            if r.status_code == 400:
                try:
                    msg = r.json().get("status", {}).get("message", "")
                except ValueError:
                    msg = ""
                if "decrypt" in msg.lower():
                    sys.exit(f"Chyba: Riot nerozšifroval PUUID z cache ({self.cache}). PUUID jsou šifrované "
                             "pro každou aplikaci v Riot Developer Portalu zvlášť a cache pochází z klíče "
                             "jiné aplikace (např. přechod z development na personal klíč). Přesuň starou "
                             f"cache stranou a spusť znovu: mv {self.cache} {self.cache}.old")
                sys.exit(f"Chyba: Riot API odmítlo požadavek (400 Bad Request): {msg or path}")
            r.raise_for_status()
            data = r.json()
            if cache_key:
                self._cache_put(cache_key, data)
            return data
        raise RuntimeError(f"Riot API neodpovídá: {url}")

    # -- endpointy
    def account_by_riot_id(self, name, tag):
        return self.get(self.account_host,
                        f"/riot/account/v1/accounts/by-riot-id/{quote(name)}/{quote(tag)}",
                        cache_key=f"acc/{name.lower()}#{tag.lower()}", ttl=7 * 86400)

    def account_by_puuid(self, puuid):
        return self.get(self.account_host, f"/riot/account/v1/accounts/by-puuid/{puuid}",
                        cache_key=f"accp/{puuid}", ttl=7 * 86400)

    def match(self, match_id):
        # odehraný zápas se nemění -> cache bez expirace
        return self.get(self.regional, f"/lol/match/v5/matches/{match_id}",
                        cache_key=f"match/{match_id}")

    def match_ids(self, puuid, count, queue):
        params = {"start": 0, "count": min(count, 100)}
        if queue == "ranked":
            params["type"] = "ranked"
        elif queue is not None:
            params["queue"] = queue
        return self.get(self.regional, f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                        params=params, cache_key=f"ids/{puuid}_{queue}_{count}", ttl=600) or []

    def league(self, puuid):
        return self.get(self.platform, f"/lol/league/v4/entries/by-puuid/{puuid}",
                        cache_key=f"league/{puuid}", ttl=1800) or []

    def mastery_top(self, puuid, count=5):
        return self.get(self.platform,
                        f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top",
                        params={"count": count}, cache_key=f"mastery/{puuid}_{count}", ttl=86400) or []

    def summoner(self, puuid):
        # None = hráč nemá účet na této platformě
        return self.get(self.platform, f"/lol/summoner/v4/summoners/by-puuid/{puuid}",
                        cache_key=f"summoner/{self.platform}/{puuid}", ttl=86400)


# --------------------------------------------------------------------------- championi (Data Dragon)

class Champions:
    """Mapování ID/interních jmen championů na zobrazovaná jména (Data Dragon, bez API klíče)."""

    def __init__(self, cache_dir):
        self.by_key, self.by_id = {}, {}
        p = Path(cache_dir) / "ddragon_champions.json"
        data = None
        if p.exists() and time.time() - p.stat().st_mtime < 7 * 86400:
            data = json.loads(p.read_text(encoding="utf-8"))
        else:
            try:
                ver = requests.get("https://ddragon.leagueoflegends.com/api/versions.json",
                                   timeout=15).json()[0]
                data = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json",
                                    timeout=15).json()["data"]
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(data), encoding="utf-8")
            except Exception as e:
                log(f"  ! Nepodařilo se načíst seznam championů z Data Dragon ({e}); "
                    "použiji interní jména.")
        for cid, c in (data or {}).items():
            self.by_key[str(c["key"])] = c["name"]
            self.by_id[cid] = c["name"]

    def name(self, internal=None, key=None):
        if key is not None and str(key) in self.by_key:
            return self.by_key[str(key)]
        if internal and internal in self.by_id:
            return self.by_id[internal]
        return internal or f"#{key}"


# --------------------------------------------------------------------------- statistiky

def participant(match, puuid):
    for p in match["info"]["participants"]:
        if p["puuid"] == puuid:
            return p
    return None


def riot_id_of(p):
    name, tag = p.get("riotIdGameName"), p.get("riotIdTagline")
    return f"{name}#{tag}" if name else None


def pct(a, b):
    return round(100 * a / b) if b else 0


def her(n):
    """1 hra, 2–4 hry, 5+ her (a 0 her)."""
    return "hra" if n == 1 else "hry" if 2 <= n <= 4 else "her"


def posledni(n):
    return f"{'Poslední' if 1 <= n <= 4 else 'Posledních'} {n} {her(n)}"


def kda(k, d, a):
    return (k + a) / max(1, d)


def game_seconds(info):
    dur = info.get("gameDuration", 0)
    return dur // 1000 if dur > 100000 else dur   # velmi staré zápasy uvádějí ms


def player_stats(puuid, matches, champs):
    games = []
    for m in matches:
        info = m["info"]
        p = participant(m, puuid)
        dur = game_seconds(info)
        if not p or dur < REMAKE_SECONDS:
            continue
        games.append({
            "champ": champs.name(p.get("championName"), p.get("championId")),
            "role": p.get("teamPosition") or p.get("individualPosition") or "",
            "win": bool(p.get("win")),
            "k": p.get("kills", 0), "d": p.get("deaths", 0), "a": p.get("assists", 0),
            "cs": p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
            "dur": dur, "queue": info.get("queueId"), "ts": info.get("gameCreation", 0),
        })
    games.sort(key=lambda g: g["ts"], reverse=True)
    n = len(games)
    st = {"n": n, "games": games}
    if not n:
        return st
    st["wins"] = sum(g["win"] for g in games)
    K, D, A = (sum(g[x] for g in games) for x in "kda")
    st["kda"] = kda(K, D, A)
    st["avg"] = (K / n, D / n, A / n)
    st["cspm"] = sum(g["cs"] for g in games) / (sum(g["dur"] for g in games) / 60)
    roles = collections.Counter(g["role"] for g in games if g["role"] in ROLE_CZ)
    st["roles"] = roles.most_common()
    st["main_role"] = roles.most_common(1)[0][0] if roles else ""
    ch = {}
    for g in games:
        c = ch.setdefault(g["champ"], {"n": 0, "w": 0, "k": 0, "d": 0, "a": 0})
        c["n"] += 1
        c["w"] += g["win"]
        c["k"] += g["k"]; c["d"] += g["d"]; c["a"] += g["a"]
    st["champs"] = sorted(ch.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["w"]))
    st["form"] = "".join("W" if g["win"] else "L" for g in games[:10])
    st["last_days"] = (time.time() * 1000 - games[0]["ts"]) / 86400000
    st["queues"] = collections.Counter(QUEUE_NAMES.get(g["queue"], str(g["queue"])) for g in games)
    return st


def rank_str(entries, queue_type):
    for e in entries:
        if e.get("queueType") == queue_type:
            tier = TIER_CZ.get(e["tier"], e["tier"].title())
            div = "" if e["tier"] in ("MASTER", "GRANDMASTER", "CHALLENGER") else f" {e['rank']}"
            w, l = e.get("wins", 0), e.get("losses", 0)
            return f"{tier}{div} {e.get('leaguePoints', 0)} LP ({pct(w, w + l)} % z {w + l})"
    return "unranked"


def short_rank(entries):
    for e in entries:
        if e.get("queueType") == "RANKED_SOLO_5x5":
            tier = TIER_CZ.get(e["tier"], e["tier"].title())
            div = "" if e["tier"] in ("MASTER", "GRANDMASTER", "CHALLENGER") else f" {e['rank']}"
            return f"{tier}{div}"
    return "–"


def ban_candidates(players, min_games=3, min_wr=55):
    out = []
    for pl in players:
        st = pl["stats"]
        for champ, c in st.get("champs", []):
            wr = pct(c["w"], c["n"])
            if c["n"] >= min_games and wr >= min_wr:
                share = c["n"] / st["n"]
                out.append({"champ": champ, "player": pl["name"], "n": c["n"], "wr": wr,
                            "score": c["n"] * wr / 100 + 3 * share})
    out.sort(key=lambda x: -x["score"])
    return out[:6]


# --------------------------------------------------------------------------- report

def opgg_link(riot_id, platform):
    name, _, tag = riot_id.partition("#")
    return f"https://op.gg/lol/summoners/{OPGG_REGION.get(platform, platform)}/{quote(name)}-{quote(tag)}"


def render(players, meta, platform):
    L = []
    L.append(f"# Scouting: {meta['title']}")
    L.append("")
    L.append(f"_Vygenerováno {dt.datetime.now():%d. %m. %Y %H:%M} · {posledni(meta['games']).lower()} "
             f"({meta['queue_label']}) · region {OPGG_REGION.get(platform, platform).upper()}_")
    if meta.get("source"):
        L.append(f"_Zdroj hráčů: {meta['source']}_")
    L.append("")

    # přehled
    L.append("## Přehled")
    L.append("")
    L.append("| Hráč | Hlavní role | Solo/Duo | WR | Forma (10) | Top championi |")
    L.append("|---|---|---|---|---|---|")
    order = {r: i for i, r in enumerate(ROLE_ORDER)}
    players.sort(key=lambda p: order.get(p["stats"].get("main_role") or p.get("league_role", ""), 9))
    for pl in players:
        st = pl["stats"]
        if not st["n"]:
            L.append(f"| {pl['name']} | – | {short_rank(pl['league'])} | – | – | žádné hry |")
            continue
        top = ", ".join(f"{c} ({v['n']})" for c, v in st["champs"][:3])
        L.append(f"| {pl['name']} | {ROLE_CZ.get(st['main_role'], '?')} | {short_rank(pl['league'])} | "
                 f"{pct(st['wins'], st['n'])} % | `{st['form']}` | {top} |")
    L.append("")

    bans = ban_candidates(players)
    if bans:
        L.append("### Kandidáti na ban")
        L.append("")
        for i, b in enumerate(bans, 1):
            L.append(f"{i}. **{b['champ']}** – {b['player']}: {b['n']} {her(b['n'])}, {b['wr']} % WR")
        L.append("")
        L.append("_Championi s ≥ 3 hrami a ≥ 55 % WR, seřazeno podle počtu her, winrate a podílu na hráčově poolu._")
        L.append("")

    # detail hráčů
    for pl in players:
        st = pl["stats"]
        role = ROLE_CZ.get(st.get("main_role", ""), "")
        L.append(f"## {pl['name']}" + (f" — {role}" if role else ""))
        L.append("")
        L.append(f"- **Rank:** Solo/Duo {rank_str(pl['league'], 'RANKED_SOLO_5x5')} · "
                 f"Flex {rank_str(pl['league'], 'RANKED_FLEX_SR')}")
        if pl.get("in_matches"):
            picks = "; ".join(f"{x['champ']} ({ROLE_CZ.get(x['role'], x['role'] or '?')}, "
                              f"{'W' if x['win'] else 'L'} {x['k']}/{x['d']}/{x['a']})"
                              for x in pl["in_matches"])
            L.append(f"- **V zadaných zápasech:** {picks}")
        if not st["n"]:
            L.append(f"- Žádné hry ve filtru ({meta['queue_label']}). "
                     "Zkus `--queue all` nebo vyšší `--games`.")
            L.append(f"- [op.gg]({opgg_link(pl['name'], platform)})")
            L.append("")
            continue
        k, d, a = st["avg"]
        last = st["last_days"]
        last_s = "dnes" if last < 1 else f"před {last:.0f} dny"
        L.append(f"- **{posledni(st['n'])}:** {st['wins']}W {st['n'] - st['wins']}L "
                 f"({pct(st['wins'], st['n'])} %) · KDA {st['kda']:.2f} ({k:.1f}/{d:.1f}/{a:.1f}) · "
                 f"CS/min {st['cspm']:.1f} · naposledy hrál {last_s}")
        L.append(f"- **Forma (nejnovější vlevo):** `{st['form']}`")
        if st["roles"]:
            L.append("- **Role:** " + " · ".join(f"{ROLE_CZ[r]} {pct(c, st['n'])} %" for r, c in st["roles"]))
        if len(st["queues"]) > 1:
            L.append("- **Fronty:** " + " · ".join(f"{q} {c}" for q, c in st["queues"].most_common()))
        top_share = st["champs"][0][1]["n"] / st["n"]
        notes = []
        if top_share >= 0.4 and st["n"] >= 8:
            notes.append(f"⚠️ one-trick: {pct(st['champs'][0][1]['n'], st['n'])} % her na "
                         f"{st['champs'][0][0]}")
        if len(st["champs"]) >= 0.7 * st["n"] and st["n"] >= 8:
            notes.append("široký champion pool (skoro každá hra jiný champion)")
        if last > 14:
            notes.append("dlouho nehrál – data nemusí odpovídat aktuální formě")
        if notes:
            L.append("- **Poznámky:** " + "; ".join(notes))
        L.append("")
        L.append("| Champion | Hry | WR | KDA |")
        L.append("|---|---:|---:|---:|")
        for champ, c in st["champs"][:6]:
            L.append(f"| {champ} | {c['n']} | {pct(c['w'], c['n'])} % | {kda(c['k'], c['d'], c['a']):.2f} |")
        L.append("")
        if pl.get("mastery"):
            L.append("**Mastery top:** " + " · ".join(f"{m['name']} ({m['points'] // 1000}k)"
                                                      for m in pl["mastery"]))
            L.append("")
        L.append(f"[op.gg]({opgg_link(pl['name'], platform)})")
        L.append("")

    L.append("---")
    L.append("_lolscout isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games "
             "or anyone officially involved in producing or managing Riot Games properties. Riot Games, and "
             "all associated properties are trademarks or registered trademarks of Riot Games, Inc._")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- hlavní logika

def load_dotenv(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def split_riot_id(s):
    if "#" not in s:
        sys.exit(f"Chyba: '{s}' není match ID ani Riot ID ve tvaru Jméno#TAG.")
    name, _, tag = s.rpartition("#")
    return name.strip(), tag.strip()


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(
        description="Scouting soupeřů v LoL přes Riot API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Příklady:\n"
               "  lolscout.py EUN1_4023742408 --us \"MujNick#EUNE\"\n"
               "  lolscout.py EUN1_111 EUN1_222 --side red\n"
               "  lolscout.py \"Hrac1#EUNE\" \"Hrac2#EUNE\" --queue all --games 30")
    ap.add_argument("targets", nargs="+", help="match ID (EUN1_…) a/nebo Riot ID (\"Jméno#TAG\")")
    ap.add_argument("--us", action="append", default=[],
                    help="Riot ID hráče z NAŠEHO týmu (lze opakovat). Z match ID se pak vezme "
                         "druhý tým. Lze nastavit i proměnnou LOLSCOUT_US=\"A#1,B#2\".")
    ap.add_argument("--side", choices=["blue", "red", "both"],
                    help="který tým ze zápasu scoutovat, když není zadáno --us (výchozí both)")
    ap.add_argument("--games", type=int, default=20, help="kolik posledních her analyzovat (max 100)")
    ap.add_argument("--queue", choices=list(QUEUE_FILTER), default="ranked",
                    help="ranked = solo+flex (výchozí), solo, flex, all")
    ap.add_argument("--region", default="eune", choices=list(PLATFORMS))
    ap.add_argument("--no-mastery", action="store_true", help="nestahovat champion mastery")
    ap.add_argument("-o", "--out", help="výstupní .md soubor (výchozí scout_<datum>.md)")
    ap.add_argument("--json", help="uložit i surová spočítaná data do JSON souboru")
    ap.add_argument("--key", help="Riot API klíč (jinak proměnná RIOT_API_KEY nebo .env)")
    ap.add_argument("--cache-dir", default=".lolscout-cache")
    ap.add_argument("--production-limits", action="store_true",
                    help="limity production klíče (500/10 s) místo personal/dev (20/1 s, 100/2 min)")
    a = ap.parse_args(argv)

    key = a.key or os.environ.get("RIOT_API_KEY")
    if not key:
        sys.exit("Chyba: chybí API klíč. Nastav RIOT_API_KEY (nebo .env) – klíč získáš na "
                 "https://developer.riotgames.com")
    platform = PLATFORMS[a.region]
    limits = ((500, 10.0), (30000, 600.0)) if a.production_limits else ((20, 1.0), (100, 120.0))
    riot = Riot(key, platform, a.cache_dir, limits)
    champs = Champions(a.cache_dir)

    us = set()
    for item in a.us + [x for x in os.environ.get("LOLSCOUT_US", "").split(",") if x.strip()]:
        n, t = split_riot_id(item)
        us.add(f"{n}#{t}".lower())

    players = {}   # puuid -> dict
    match_ids = [t for t in a.targets if MATCH_ID_RE.match(t.strip().upper())]
    riot_ids = [t for t in a.targets if t not in match_ids]

    # 1) hráči ze zadaných zápasů
    for mid in match_ids:
        mid = mid.strip().upper()
        log(f"Zápas {mid}")
        m = riot.match(mid)
        if not m:
            sys.exit(f"Chyba: zápas {mid} nenalezen (zkontroluj ID a --region).")
        parts = m["info"]["participants"]
        teams = {100: [p for p in parts if p["teamId"] == 100], 200: [p for p in parts if p["teamId"] == 200]}
        chosen = None
        if us:
            for tid, ps in teams.items():
                if any((riot_id_of(p) or "").lower() in us for p in ps):
                    chosen = [200 if tid == 100 else 100]
                    break
            if chosen is None:
                log("  ! nikdo z --us v tomto zápase nehrál, beru oba týmy")
        if chosen is None:
            side = a.side or "both"
            chosen = {"blue": [100], "red": [200], "both": [100, 200]}[side]
        for tid in chosen:
            for p in teams[tid]:
                pl = players.setdefault(p["puuid"], {"puuid": p["puuid"], "name": riot_id_of(p),
                                                     "team": "Blue" if tid == 100 else "Red",
                                                     "in_matches": []})
                pl["in_matches"].append({
                    "match": mid, "champ": champs.name(p.get("championName"), p.get("championId")),
                    "role": p.get("teamPosition") or p.get("individualPosition") or "",
                    "win": bool(p.get("win")), "k": p.get("kills", 0), "d": p.get("deaths", 0),
                    "a": p.get("assists", 0)})

    # 2) hráči zadaní přes Riot ID
    for rid in riot_ids:
        name, tag = split_riot_id(rid)
        acc = riot.account_by_riot_id(name, tag)
        if not acc:
            log(f"  ! Riot ID {name}#{tag} neexistuje – přeskakuji")
            continue
        players.setdefault(acc["puuid"], {"puuid": acc["puuid"],
                                          "name": f"{acc.get('gameName', name)}#{acc.get('tagLine', tag)}",
                                          "in_matches": []})

    if not players:
        sys.exit("Žádní hráči ke scoutování.")

    # 3) data o hráčích
    queue = QUEUE_FILTER[a.queue]
    plist = list(players.values())
    est = len(plist) * (a.games + 2 + (0 if a.no_mastery else 1))
    eta = "pár sekund" if est <= 100 else f"cca {((est - 1) // 100) * 2} min"
    log(f"Scoutuji {len(plist)} hráčů (až ~{est} požadavků na API; s personal klíčem {eta}, "
        "opakovaný běh je díky cache rychlejší)")
    for i, pl in enumerate(plist, 1):
        if not pl["name"]:
            acc = riot.account_by_puuid(pl["puuid"]) or {}
            pl["name"] = f"{acc.get('gameName', '?')}#{acc.get('tagLine', '?')}"
        log(f"[{i}/{len(plist)}] {pl['name']}")
        ids = riot.match_ids(pl["puuid"], a.games, queue)
        matches = [m for m in (riot.match(x) for x in ids) if m]
        pl["stats"] = player_stats(pl["puuid"], matches, champs)
        pl["league"] = riot.league(pl["puuid"])
        if not a.no_mastery:
            pl["mastery"] = [{"name": champs.name(key=m.get("championId")),
                              "points": m.get("championPoints", 0)}
                             for m in riot.mastery_top(pl["puuid"], 5)]
        if not pl["stats"]["n"] and pl.get("in_matches"):
            pl["league_role"] = pl["in_matches"][0]["role"]

    # 4) report
    queue_label = {"ranked": "ranked solo+flex", "solo": "solo/duo", "flex": "flex", "all": "všechny fronty"}[a.queue]
    title = ", ".join(match_ids) if match_ids else ", ".join(p["name"] for p in plist)
    meta = {"title": title, "games": a.games, "queue_label": queue_label,
            "source": f"zápas {', '.join(match_ids)}" if match_ids else None}
    md = render(plist, meta, platform)
    out = Path(a.out or f"scout_{dt.datetime.now():%Y-%m-%d_%H%M}.md")
    out.write_text(md, encoding="utf-8")
    if a.json:
        slim = [{k: v for k, v in p.items() if k != "stats"} | {"stats": {k: v for k, v in p["stats"].items()
                                                                           if k != "games"}}
                for p in plist]
        Path(a.json).write_text(json.dumps(slim, ensure_ascii=False, indent=2, default=list), encoding="utf-8")
    log(f"\nHotovo: {out}  ({riot.requests_made} požadavků na Riot API)")
    return out


if __name__ == "__main__":
    main()
