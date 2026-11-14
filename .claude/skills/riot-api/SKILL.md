---
name: riot-api
description: Referenční znalosti o Riot Games API pro lolscout – routing (platforma vs. regionální host), endpointy, rate limity, cache/TTL, Data Dragon a pravidla Riotu pro personal klíč. Použij při každé práci s voláním Riot API, novém endpointu nebo funkci, která mění, kdo a jak výstup uvidí.
user-invocable: false
---

# Riot API v lolscout

Před použitím endpointu, který zde není, ověř jeho detail na https://developer.riotgames.com/apis (cesta, routing, parametry, limity metody).

## Routing

| API | Host | V kódu |
|---|---|---|
| account-v1 (`/riot/account/v1/…`) | regionální: **jen** `americas`, `asia`, `europe` (libovolný účet z kteréhokoli; `sea` neexistuje) | `self.account_host` (`sea` → `asia`) |
| match-v5 (`/lol/match/v5/…`) | regionální: `americas`, `asia`, `europe`, `sea` (oc1 → `sea`) | `self.regional` |
| league-v4, champion-mastery-v4, summoner-v4, spectator-v5 | platforma: `eun1`, `euw1`, `na1`, `kr`, … | `self.platform` |

Mapování je v `PLATFORMS` (CLI alias → platforma) a `REGIONAL` (platforma → regionální host pro match-v5); account-v1 používá `account_host`. Nové platformy (např. `me1`, `sg2`, `tw2`, `vn2`) přidávej do všech tří map (`PLATFORMS`, `REGIONAL`, `OPGG_REGION`).

## Používané endpointy a cache

| Metoda `Riot` | Endpoint | Cache klíč | TTL |
|---|---|---|---|
| `account_by_riot_id` | `/riot/account/v1/accounts/by-riot-id/{name}/{tag}` | `acc/{name}#{tag}` (lowercase) | 7 dní |
| `account_by_puuid` | `/riot/account/v1/accounts/by-puuid/{puuid}` | `accp/{puuid}` | 7 dní |
| `match` | `/lol/match/v5/matches/{id}` | `match/{id}` | bez expirace (odehraný zápas se nemění) |
| `match_ids` | `/lol/match/v5/matches/by-puuid/{puuid}/ids` (`count` ≤ 100, `type=ranked` nebo `queue=`) | `ids/{puuid}_{queue}_{count}` | 10 min |
| `league` | `/lol/league/v4/entries/by-puuid/{puuid}` | `league/{puuid}` | 30 min |
| `mastery_top` | `/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top` (`count`) | `mastery/{puuid}_{count}` | 1 den |
| `summoner` | `/lol/summoner/v4/summoners/by-puuid/{puuid}` (level, ikona; 404 = účet není na platformě) | `summoner/{platform}/{puuid}` | 1 den |

Pravidla pro nová data: unikátní prefix klíče; TTL podle toho, jak rychle se data mění (živá hra = žádná cache, rank = desítky minut, statická data = dny); parametry, které mění odpověď, patří do klíče. Klíč se převádí na název souboru (`_cache_path`), `/` a `#` se mění na `_`.

## Rate limity

- Development i personal klíč: **20 požadavků / 1 s a 100 / 2 min**, počítáno zvlášť pro každý routing host – proto `Riot.limiters` je `defaultdict` podle hostu. Production (`--production-limits`): 500 / 10 s, 30000 / 10 min.
- Jednotlivé metody mají navíc vlastní limity (hlavičky `X-Method-Rate-Limit`); při 429 `Riot.get()` čeká podle `Retry-After`.
- Typický běh: hráč = 1 (ids) + N (zápasy, jen necachované) + 1 (league) + 1 (mastery). 5 hráčů × 20 her ≈ 115 požadavků ≈ 2 min s personal klíčem. Každý nový endpoint na hráče přidává ~1 požadavek × počet hráčů – uveď to ve shrnutí.
- Development klíč vyprší po 24 h → 401/403 → `sys.exit` s návodem.

## Data z Match-V5 – pasti

- `info.gameDuration` je u starých zápasů v ms (kód: `> 100000` → `// 1000`); remake = < 300 s.
- `teamPosition` bývá prázdné (ARAM, remake, některé custom hry); fallback `individualPosition`.
- `riotIdGameName`/`riotIdTagline` mohou chybět u starých zápasů → `account_by_puuid`.
- `teamId` 100 = blue, 200 = red. `queueId` 420 solo, 440 flex, 0 custom/turnaj.
- `championName` je interní ID (např. `MonkeyKing`) → zobrazované jméno přes `Champions.name()`.
- `participants[].challenges` chybí u starých zápasů a některých módů → metriky z nich musí snést `None`. Hodnoty jsou **prahové příznaky výzev**, ne obecné metriky: např. `laningPhaseGoldExpAdvantage` = 1 jen při ≥ 20 % náskoku gold+XP nad soupeřem na roli ve 14. min (cca 12 % her), proto se porovnává s příznakem soupeře. Před použitím nového klíče z `challenges` ověř jeho význam a rozložení nad cache.

## Data Dragon

Bez klíče a bez rate limitu Riot API: `https://ddragon.leagueoflegends.com/api/versions.json` → `cdn/{ver}/data/en_US/champion.json`. `data[id].key` je číselné `championId` (string), `data[id].name` zobrazované jméno. Cache `ddragon_champions.json`, 7 dní.

## Pravidla Riotu (General Policies)

- Personal klíč: jen soukromý projekt / malá komunita (tým). Veřejná webová aplikace, Discord bot pro cizí servery apod. vyžadují registraci produktu a production klíč → **rozhodnutí uživatele**, neimplementuj potichu.
- Klíč nikdy do repozitáře, reportu, JSON výstupu ani logu. Sdílet jen se spoluhráči.
- Historii custom/turnajových her nezobrazovat veřejně bez souhlasu hráče (proto queue 0 jen s `--queue all`).
- Produkt musí nést disclaimer „lolscout isn't endorsed by Riot Games…“ (README + patička reportu).
- Nevytvářet funkce, které dávají nefér výhodu během hry (např. živé odhalování skrytých informací); pre-game scouting z veřejných dat je v pořádku.
