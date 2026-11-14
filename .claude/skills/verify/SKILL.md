---
name: verify
description: Ověří změny v lolscout bez API klíče a sítě – kontrola syntaxe a offline běh statistik a reportu nad uloženými zápasy v .lolscout-cache/. Použij po každé změně kódu a před předáním reviewerovi.
allowed-tools: Bash(.venv/bin/python *), Bash(git status*), Bash(git diff*)
---

Z kořene repozitáře postupně (zastav u první chyby, ohlas ji se `soubor:řádek`):

1. Syntaxe: `.venv/bin/python -m py_compile lolscout.py`
2. CLI: `.venv/bin/python lolscout.py --help` – nové přepínače mají českou nápovědu.
3. Offline běh nad cache (potřebuje `.lolscout-cache/match_*.json`; pokud cache chybí, ohlas to a krok přeskoč):

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

4. Přečti `/tmp/lolscout-verify.md` v části, které se změna týká: počet sloupců tabulek, čeština a skloňování, žádné `None`/`nan`/tracebacky.
5. Ostrý běh proti Riot API (`.venv/bin/python lolscout.py <match ID> …`) jen pokud změna mění volání API a uživatel to odsouhlasí – spotřebuje kvótu jeho klíče.

Výstup: tabulka krok → výsledek; u selhání první relevantní chyba. Připomeň aktualizaci README, pokud se změnilo CLI nebo podoba reportu.
