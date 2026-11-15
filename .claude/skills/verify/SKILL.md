---
name: verify
description: Ověří změny v lolscout bez API klíče a sítě – kontrola syntaxe a offline běh statistik a reportu nad uloženými zápasy v .lolscout-cache/. Použij po každé změně kódu a před předáním reviewerovi.
allowed-tools: Bash(.venv/bin/python *), Bash(git status*), Bash(git diff*)
---

Z kořene repozitáře postupně (zastav u první chyby, ohlas ji se `soubor:řádek`):

1. Syntaxe: `.venv/bin/python -m py_compile lolscout.py player-scout.py`
2. CLI: `.venv/bin/python lolscout.py --help` a `.venv/bin/python player-scout.py --help` – nové přepínače mají českou nápovědu.
3. Offline běh `lolscout.py` nad cache (potřebuje `.lolscout-cache/match_*.json`; pokud cache chybí, ohlas to a krok přeskoč):

```bash
.venv/bin/python - <<'EOF'
import collections, glob, json
import lolscout as ls
champs = ls.Champions(".lolscout-cache")
matches = [json.load(open(p, encoding="utf-8")) for p in glob.glob(".lolscout-cache/match_*.json")]
cnt = collections.Counter(p["puuid"] for m in matches for p in m["info"]["participants"])
players = []
for puuid, _ in cnt.most_common(5):
    mine = [m for m in matches if ls.participant(m, puuid)]
    p = ls.participant(mine[0], puuid)
    players.append({"puuid": puuid, "name": ls.riot_id_of(p) or "?#?", "in_matches": [],
                    "stats": ls.player_stats(puuid, mine, champs), "league": []})
players.append({"puuid": "x", "name": "Bez#HER", "in_matches": [],
                "stats": ls.player_stats("x", [], champs), "league": []})   # hráč bez her
md = ls.render(players, {"title": "offline", "games": 20, "queue_label": "ranked solo+flex"}, "eun1")
open("/tmp/lolscout-verify.md", "w", encoding="utf-8").write(md)
print(f"OK: {len(matches)} zápasů, {len(players)} hráčů, report {len(md)} znaků -> /tmp/lolscout-verify.md")
EOF
```

   Pokud změna přidává do slovníku hráče nová pole (league, mastery, …), doplň je do snippetu, aby se cesta v `render` opravdu vykonala.

4. Offline běh `player-scout.py` přes celé `main()` – Riot klient čte jen cache, síť vyhodí `AssertionError` (potřebuje `.lolscout-cache/ids_*.json`):

```bash
.venv/bin/python - <<'EOF'
import glob, importlib.util, json
import lolscout as ls
spec = importlib.util.spec_from_file_location("ps", "player-scout.py")
ps = importlib.util.module_from_spec(spec); spec.loader.exec_module(ps)
ids_file = max(glob.glob(".lolscout-cache/ids_*.json"), key=lambda p: len(json.load(open(p))))
puuid = ids_file.removeprefix(".lolscout-cache/ids_").rsplit("_", 2)[0]
class Offline(ls.Riot):
    def get(self, host, path, params=None, cache_key=None, ttl=None):
        raise AssertionError(f"síť: {path}")
    def account_by_riot_id(self, n, t): return {"puuid": puuid, "gameName": n, "tagLine": t}
    def summoner(self, p): return {"summonerLevel": 57}
    def match_ids(self, p, c, q): return json.load(open(ids_file))
    def match(self, mid): return self._cache_get(f"match/{mid}", None)
    def league(self, p): return self._cache_get(f"league/{p}", None) or []
    def mastery_top(self, p, c):
        return self._cache_get(f"mastery/{p}_{c}", None) or self._cache_get(f"mastery/{p}", None) or []
ls.Riot = Offline
ps.main(["Test#EUNE", "--key", "x", "-o", "/tmp/player-scout-verify.md", "--json", "/tmp/player-scout-verify.json"])
json.load(open("/tmp/player-scout-verify.json"))
empty = ps.render({"name": "Bez#HER", "league": [], "summoner": None, "mastery": []}, ps.analyze([], []),
                  {"platform": "eun1", "games": 50, "queue_label": "ranked solo+flex"})
print("OK: report /tmp/player-scout-verify.md, hráč bez her", len(empty), "znaků")
EOF
```

5. Přečti vygenerované reporty (`/tmp/lolscout-verify.md`, `/tmp/player-scout-verify.md`) v části, které se změna týká: počet sloupců tabulek, čeština a skloňování, žádné `None`/`nan`/tracebacky.
6. Ostrý běh proti Riot API (`lolscout.py <match ID>`, `player-scout.py "Nick#TAG"`) jen pokud změna mění volání API a uživatel to odsouhlasí – spotřebuje kvótu jeho klíče.

Výstup: tabulka krok → výsledek; u selhání první relevantní chyba. Připomeň aktualizaci README, pokud se změnilo CLI nebo podoba reportu.
