# lolscout – pokyny pro Claude Code

CLI nástroj pro scouting soupeřů v League of Legends přes **oficiální Riot API**. Vstup: Riot match ID (`EUN1_…`) nebo Riot ID (`Jméno#TAG`); výstup: Markdown report (`scout_<datum>.md`), volitelně JSON. Uživatel je český, hraje amatérské turnaje (Challengermode, EUNE). Komunikace, komentáře a texty reportu česky; identifikátory anglicky (výjimky `her()`, `posledni()` jsou české skloňování – nech je).

Projekt je **MVP v jednom souboru** a bude se rozšiřovat. Zdrojem pravdy je kód, ne README.

## Stack

- Python 3.14 (`.venv`, mise), jediná závislost `requests` (`requirements.txt`). Žádný `pyproject.toml`, žádné testy, žádný linter.
- `lolscout.py` (~580 ř.) je rozdělený komentářovými sekcemi `# ---- <název>`:

| Sekce | Obsah |
|---|---|
| konstanty | `PLATFORMS` (alias → platforma), `REGIONAL` (platforma → regionální routing), `OPGG_REGION`, `QUEUE_FILTER`, `QUEUE_NAMES`, `ROLE_CZ`, `TIER_CZ`, `MATCH_ID_RE`, `REMAKE_SECONDS` |
| rate limit | `RateLimiter` – klouzavé okno, jeden limiter na routing host |
| API klient | `Riot.get()` – cache → limiter → HTTP s retry (429 `Retry-After`, 5xx backoff, 404 → `None`, 401/403 → `sys.exit`); endpointy `account_by_*`, `match`, `match_ids`, `league`, `mastery_top` |
| championi | `Champions` – Data Dragon, bez klíče, cache 7 dní |
| statistiky | čisté funkce: `player_stats`, `rank_str`, `short_rank`, `ban_candidates`, `pct`, `kda` |
| report | `render(players, meta, platform)` → Markdown string |
| hlavní logika | `load_dotenv`, `split_riot_id`, `main(argv)` – CLI, výběr týmu (`--us`/`--side`), sběr dat, zápis |

- Datový tok: `main` → hráči (`dict` podle puuid) → `riot.*` → `player_stats` → `render` → soubor. Slovník hráče: `puuid, name, team?, in_matches[], stats, league, mastery?, league_role?`. Tvar `stats` z `player_stats` je kontrakt pro `render`, `ban_candidates` i `--json` výstup.

## Příkazy

- Kontrola syntaxe: `.venv/bin/python -m py_compile lolscout.py`
- Offline ověření (bez API klíče a sítě, nad `.lolscout-cache/`): skill `/verify`.
- Ostrý běh: `.venv/bin/python lolscout.py EUN1_… --us "Nick#TAG"` – spotřebuje kvótu klíče uživatele a jde na síť. **Spouštěj jen se souhlasem uživatele**; `.env` s klíčem existuje, nečti ho.
- Nápověda CLI: `.venv/bin/python lolscout.py --help`

## Invarianty – neporušuj

- **Rate limit:** každý požadavek na Riot API jde přes `Riot.get()` (limiter + retry + cache). Žádné přímé `requests.get` na `*.api.riotgames.com`. Výchozí limity = personal/dev klíč (20/1 s, 100/120 s), `--production-limits` = 500/10 s, 30000/600 s.
- **Cache (`.lolscout-cache/`, klíč → soubor přes `_cache_path`):** odehraný zápas nemá TTL; ostatní data mají TTL (ids 10 min, league 30 min, mastery 1 den, account 7 dní). Nová data = nový prefix klíče a vědomě zvolené TTL. Změna tvaru uložených dat vyžaduje nový prefix, jinak se načte stará cache.
- **Routing:** account-v1 a match-v5 jdou na regionální host (`REGIONAL`), league-v4 a champion-mastery-v4 na platformu. Viz skill `riot-api`.
- **Statistiky:** remaky (< `REMAKE_SECONDS`) se nepočítají; `gameDuration` u starých zápasů je v ms; role jen z `ROLE_CZ`; `teamPosition` může být prázdné.
- **Riot pravidla:** API klíč nikdy do gitu ani do výstupu/logu. Personal klíč nesmí pohánět veřejnou aplikaci (web/bot pro cizí lidi = production klíč – eskaluj). Custom/turnajové hry (queue 0) jen s `--queue all` a reporty zůstávají v týmu. Disclaimer „isn't endorsed by Riot Games…“ musí zůstat v README i v reportu.
- **Výstup:** CLI log jde na stderr (`log()`), report do souboru. Chybové stavy pro uživatele = `sys.exit("Chyba: …")` česky, bez tracebacku. Čeština ve výstupu musí sedět gramaticky (`her()`, `posledni()`).
- **Kompatibilita:** stávající CLI přepínače, proměnné `RIOT_API_KEY`/`LOLSCOUT_US` a struktura reportu jsou veřejné rozhraní – měň je jen záměrně a aktualizuj README.

## Rozšiřování

- Malé funkce přidávej do odpovídající sekce `lolscout.py`. Rozdělení na balíček (`lolscout/` s moduly api/stats/report/cli) nebo přidání testovací infrastruktury navrhuje `architect` až ve chvíli, kdy to konkrétní změna potřebuje – ne preventivně.
- Nové závislosti jen s odůvodněním (stdlib má přednost); přidej do `requirements.txt`.
- Až vzniknou testy: nejdřív čisté funkce (`player_stats`, `ban_candidates`, `render`, `rank_str`, skloňování) nad JSON fixtures. Fixtures odvozené z cache anonymizuj (puuid, Riot ID jsou údaje reálných hráčů).
- `.gitignore` ignoruje `*.json` a `scout_*.md` – nové verzované JSON soubory potřebují výjimku `!cesta`.

## Pravidla práce

- Commity a push jen na výslovnou žádost uživatele. Nemazat `.lolscout-cache/` (zbytečně spálí kvótu).
- Po každé změně kódu: `py_compile` + `/verify`. Změna CLI/chování → aktualizuj README (česky, stejný styl).
- Eskaluj jen: změnu veřejného CLI/formátu reportu, novou závislost nebo službu, cokoli, co by vyžadovalo production klíč nebo zveřejnění dat hráčů.

## Tým agentů (`.claude/agents`)

`architect` (návrh větší změny, needituje) → `developer` (implementace + ověření) → `reviewer` (nezávislé review diffu, needituje). Pro malé lokální změny stačí `developer` + `/verify`.
Skills: `/deliver <funkce>` (orchestrace celé funkce), `/verify` (offline ověření), `riot-api` (referenční znalosti o API a pravidlech, načítá se automaticky).
