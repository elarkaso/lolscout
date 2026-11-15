#!/usr/bin/env python3
"""
player-scout – hloubková analýza jednoho hráče League of Legends podle Riot ID.

Doplněk k lolscout.py (sdílí s ním API klienta, rate limit, cache i .env).
Výstup: Markdown report – rank, level účtu, forma a trend, role, champion pool,
mastery, lajnovací fáze, výhry vs. prohry, délka her, soupeři v lajně,
opakovaní spoluhráči (duo), aktivita, tipy na draft a poslední zápasy.

Použití:
  python player-scout.py "Hrac#EUNE"
  python player-scout.py "Hrac#EUNE" --games 100 --queue solo --json hrac.json
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import lolscout as ls

# --------------------------------------------------------------------------- konstanty

DEFAULT_GAMES = 50
RECENT_GAMES = 15            # řádků v tabulce posledních zápasů
TOP_CHAMPS = 10
MASTERY_COUNT = 10
MIN_DUO_GAMES = 2            # spoluhráč v ≥ N hrách = pravděpodobně duo/premade
MIN_OPP_GAMES = 2            # soupeř v lajně v ≥ N hrách
LENGTH_BUCKETS = [(0, 25, "do 25 min"), (25, 35, "25–35 min"), (35, 10 ** 6, "nad 35 min")]
DAY_PARTS = [(0, 6, "noc (0–6)"), (6, 12, "dopoledne (6–12)"), (12, 18, "odpoledne (12–18)"),
             (18, 24, "večer (18–24)")]
WEEKDAYS = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"]
HIGH_TIERS = {"EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"}
SMURF_LEVEL = 100            # nízký level + vysoký rank = možný smurf/alt
POCKET_MASTERY = 100_000     # mastery body, od kterých je champion „pocket pick“
QUEUE_LABEL = {"ranked": "ranked solo+flex", "solo": "solo/duo", "flex": "flex", "all": "všechny fronty"}
LANE_DIFF = 0.1             # rozdíl podílu her s výrazným náskokem a mankem v lajně pro postřeh


# --------------------------------------------------------------------------- extrakce ze zápasů

def game_record(match, puuid, champs):
    """Jedna hra hráče jako plochý dict; None pro remake nebo zápas bez hráče."""
    info = match["info"]
    p = ls.participant(match, puuid)
    dur = ls.game_seconds(info)
    if not p or dur < ls.REMAKE_SECONDS:
        return None
    ch = p.get("challenges") or {}
    parts = info["participants"]
    allies = [x for x in parts if x["teamId"] == p["teamId"] and x["puuid"] != puuid]
    enemies = [x for x in parts if x["teamId"] != p["teamId"]]
    role = p.get("teamPosition") or p.get("individualPosition") or ""
    opp = next((x for x in enemies if role and x.get("teamPosition") == role), None)
    k, d, a = p.get("kills", 0), p.get("deaths", 0), p.get("assists", 0)
    team_kills = k + sum(x.get("kills", 0) for x in allies)
    cs10 = None
    if "laneMinionsFirst10Minutes" in ch:
        cs10 = ch["laneMinionsFirst10Minutes"] + ch.get("jungleCsBefore10Minutes", 0)
    minutes = dur / 60
    return {
        "match": match["metadata"]["matchId"], "ts": info.get("gameCreation", 0),
        "queue": info.get("queueId"), "dur": dur,
        "champ": champs.name(p.get("championName"), p.get("championId")),
        "role": role, "win": bool(p.get("win")), "k": k, "d": d, "a": a,
        "cs": p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
        "gpm": p.get("goldEarned", 0) / minutes,
        "dpm": p.get("totalDamageDealtToChampions", 0) / minutes,
        "dmg": p.get("totalDamageDealtToChampions", 0),
        "vspm": p.get("visionScore", 0) / minutes,
        "control": p.get("visionWardsBoughtInGame", 0),
        "kp": ch.get("killParticipation", (k + a) / team_kills if team_kills else 0),
        "dmg_share": ch.get("teamDamagePercentage"),
        "cs10": cs10,
        "solo_kills": ch.get("soloKills"),
        # challenge „Lane Domination“: ≥ 20 % víc gold+XP než soupeř na roli ve 14. min (1/0)
        "lane_adv": ch.get("laningPhaseGoldExpAdvantage"),
        "lane_lost": ((opp.get("challenges") or {}).get("laningPhaseGoldExpAdvantage") if opp else None),
        "first_blood": bool(p.get("firstBloodKill") or p.get("firstBloodAssist")),
        "surrender": bool(p.get("gameEndedInSurrender")),
        "opp_champ": champs.name(opp.get("championName"), opp.get("championId")) if opp else None,
        "allies": [rid for rid in (ls.riot_id_of(x) for x in allies) if rid],
    }


# --------------------------------------------------------------------------- statistiky

def mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def summary(games):
    """Souhrnné metriky pro libovolnou podmnožinu her (n ≥ 1)."""
    n = len(games)
    K, D, A = (sum(g[x] for g in games) for x in "kda")
    minutes = sum(g["dur"] for g in games) / 60
    return {
        "n": n, "wins": sum(g["win"] for g in games),
        "kda": ls.kda(K, D, A), "avg": (K / n, D / n, A / n),
        "cspm": sum(g["cs"] for g in games) / minutes,
        "gpm": mean(g["gpm"] for g in games), "dpm": mean(g["dpm"] for g in games),
        "vspm": mean(g["vspm"] for g in games), "control": mean(g["control"] for g in games),
        "kp": mean(g["kp"] for g in games), "dmg_share": mean(g["dmg_share"] for g in games),
        "cs10": mean(g["cs10"] for g in games), "solo_kills": mean(g["solo_kills"] for g in games),
        "lane_adv": mean(g["lane_adv"] for g in games),
        "lane_lost": mean(g["lane_lost"] for g in games),
        "first_blood": mean(g["first_blood"] for g in games),
        "dur": minutes / n, "last_ts": max(g["ts"] for g in games),
    }


def group(games, key):
    out = collections.defaultdict(list)
    for g in games:
        k = key(g)
        if k:
            out[k].append(g)
    return out


def streak(games):
    """Aktuální série (games seřazené od nejnovější): ('W'|'L', délka)."""
    if not games:
        return None, 0
    first = games[0]["win"]
    n = next((i for i, g in enumerate(games) if g["win"] != first), len(games))
    return ("W" if first else "L"), n


def analyze(games, mastery):
    """Všechny agregace pro report. `games` seřazené od nejnovější."""
    a = {"n": len(games), "games": games}
    if not games:
        return a
    a["total"] = summary(games)
    a["form"] = "".join("W" if g["win"] else "L" for g in games[:20])
    a["streak"] = streak(games)
    if len(games) >= 20:
        a["trend"] = (ls.pct(sum(g["win"] for g in games[:10]), 10),
                      ls.pct(sum(g["win"] for g in games[10:]), len(games) - 10))
    a["roles"] = sorted(((r, summary(gs)) for r, gs in group(games, lambda g: g["role"]).items()
                         if r in ls.ROLE_CZ), key=lambda x: -x[1]["n"])
    a["champs"] = sorted(((c, summary(gs)) for c, gs in group(games, lambda g: g["champ"]).items()),
                         key=lambda x: (-x[1]["n"], -x[1]["wins"]))
    wins = [g for g in games if g["win"]]
    losses = [g for g in games if not g["win"]]
    a["wins"] = summary(wins) if wins else None
    a["losses"] = summary(losses) if losses else None
    a["surrender_losses"] = sum(g["surrender"] for g in losses) / len(losses) if losses else None
    a["length"] = [(label, len(gs), sum(g["win"] for g in gs))
                   for lo, hi, label in LENGTH_BUCKETS
                   for gs in [[g for g in games if lo <= g["dur"] / 60 < hi]]]
    opps = group(games, lambda g: g["opp_champ"])
    a["opponents"] = sorted(((c, len(gs), sum(g["win"] for g in gs)) for c, gs in opps.items()
                             if len(gs) >= MIN_OPP_GAMES), key=lambda x: (-x[1], x[2]))
    mates = collections.defaultdict(list)
    for g in games:
        for rid in g["allies"]:
            mates[rid].append(g)
    a["duos"] = sorted(((rid, len(gs), sum(g["win"] for g in gs)) for rid, gs in mates.items()
                        if len(gs) >= MIN_DUO_GAMES), key=lambda x: -x[1])
    now_ms = dt.datetime.now().timestamp() * 1000
    a["last_days"] = (now_ms - games[0]["ts"]) / 86400000
    a["games_7d"] = sum(now_ms - g["ts"] < 7 * 86400000 for g in games)
    a["games_30d"] = sum(now_ms - g["ts"] < 30 * 86400000 for g in games)
    local = [dt.datetime.fromtimestamp(g["ts"] / 1000) for g in games]
    a["day_parts"] = [(label, sum(lo <= t.hour < hi for t in local)) for lo, hi, label in DAY_PARTS]
    a["weekdays"] = [(WEEKDAYS[i], sum(t.weekday() == i for t in local)) for i in range(7)]
    played = {c for c, _ in a["champs"]}
    a["pocket"] = [m for m in mastery if m["points"] >= POCKET_MASTERY and m["name"] not in played]
    return a


def notes(a, league, summoner):
    """Automatické postřehy pro draft a přípravu."""
    out = []
    solo = next((e for e in league if e.get("queueType") == "RANKED_SOLO_5x5"), None)
    level = (summoner or {}).get("summonerLevel")
    if level and level < SMURF_LEVEL and solo and solo["tier"] in HIGH_TIERS:
        out.append(f"level účtu {level} a rank {ls.TIER_CZ.get(solo['tier'], solo['tier'])} – "
                   "možný smurf/alt účet, data mohou podhodnocovat hráče")
    if not a["n"]:
        return out
    n, top = a["n"], a["champs"][0]
    if n >= 10 and top[1]["n"] / n >= 0.4:
        out.append(f"⚠️ one-trick: {ls.pct(top[1]['n'], n)} % her na {top[0]} "
                   f"({ls.pct(top[1]['wins'], top[1]['n'])} % WR) – zvaž ban")
    kind, length = a["streak"]
    if length >= 3:
        out.append(f"aktuálně série {length} {'výher' if kind == 'W' else 'proher'} v řadě")
    if "trend" in a:
        recent, older = a["trend"]
        if abs(recent - older) >= 20:
            out.append(f"forma {'roste' if recent > older else 'klesá'}: posledních 10 her {recent} % WR "
                       f"vs. {older} % dříve")
    t = a["total"]
    if t["lane_adv"] is not None and t["lane_lost"] is not None and n >= 10:
        diff = t["lane_adv"] - t["lane_lost"]
        lane = (f"výrazný náskok v {f_pct(t['lane_adv'])} her, výrazné manko v {f_pct(t['lane_lost'])} her")
        if diff >= LANE_DIFF:
            out.append(f"silný v lajně – {lane}")
        elif diff <= -LANE_DIFF:
            out.append(f"v lajně často zaostává – {lane}; tlak brzy se vyplatí")
    if n >= 5 and t["avg"][1] >= 7:
        out.append(f"hodně umírá ({t['avg'][1]:.1f} smrtí na hru)")
    (_, short_n, short_w), _, (_, long_n, long_w) = a["length"]
    if short_n >= 5 and long_n >= 5 and abs(ls.pct(short_w, short_n) - ls.pct(long_w, long_n)) >= 20:
        better = "krátké" if ls.pct(short_w, short_n) > ls.pct(long_w, long_n) else "dlouhé"
        out.append(f"lépe mu jdou {better} hry (do 25 min {ls.pct(short_w, short_n)} % WR, "
                   f"nad 35 min {ls.pct(long_w, long_n)} % WR)")
    if a["duos"]:
        rid, dn, dw = a["duos"][0]
        out.append(f"často hraje s {rid} ({dn} {ls.her(dn)}, {ls.pct(dw, dn)} % WR) – pravděpodobně duo")
    if a["pocket"]:
        out.append("pocket picky (vysoká mastery, v analyzovaných hrách nehrál): "
                   + ", ".join(f"{m['name']} ({m['points'] // 1000}k)" for m in a["pocket"][:4]))
    if a["last_days"] > 14:
        out.append(f"naposledy hrál před {a['last_days']:.0f} dny – data nemusí odpovídat aktuální formě")
    return out


# --------------------------------------------------------------------------- report

def f_num(v, spec=".1f", suffix=""):
    return "–" if v is None else f"{v:{spec}}{suffix}"


def f_pct(v):
    return "–" if v is None else f"{round(100 * v)} %"


def ago(ts_ms):
    days = (dt.datetime.now().timestamp() * 1000 - ts_ms) / 86400000
    return "dnes" if days < 1 else "včera" if days < 2 else f"před {days:.0f} dny"


def render(player, a, meta):
    platform = meta["platform"]
    L = [f"# Hráč: {player['name']}", "",
         f"_Vygenerováno {dt.datetime.now():%d. %m. %Y %H:%M} · {ls.posledni(meta['games']).lower()} "
         f"({meta['queue_label']}) · region {ls.OPGG_REGION.get(platform, platform).upper()}_", ""]

    level = (player.get("summoner") or {}).get("summonerLevel")
    L.append(f"- **Rank:** Solo/Duo {ls.rank_str(player['league'], 'RANKED_SOLO_5x5')} · "
             f"Flex {ls.rank_str(player['league'], 'RANKED_FLEX_SR')}")
    if level:
        L.append(f"- **Level účtu:** {level}")
    L.append(f"- [op.gg]({ls.opgg_link(player['name'], platform)})")
    L.append("")

    tips = notes(a, player["league"], player.get("summoner"))
    bans = ls.ban_candidates([{"name": player["name"],
                               "stats": {"n": a["n"], "champs": [(c, {"n": s["n"], "w": s["wins"]})
                                                                  for c, s in a.get("champs", [])]}}])
    if tips or bans:
        L += ["## Postřehy pro draft", ""]
        L += [f"- {t}" for t in tips]
        if bans:
            L.append("- **Kandidáti na ban:** " + ", ".join(f"{b['champ']} ({b['n']} {ls.her(b['n'])}, "
                                                            f"{b['wr']} % WR)" for b in bans))
        L.append("")

    if not a["n"]:
        L += [f"_Žádné hry ve filtru ({meta['queue_label']}). Zkus `--queue all` nebo vyšší `--games`._", ""]
        render_mastery(L, player)
        return finish(L)

    t = a["total"]
    k, d, a_ = t["avg"]
    L += ["## Souhrn", "",
          "| Metrika | Hodnota |", "|---|---:|",
          f"| Hry | {t['n']} ({t['wins']}W {t['n'] - t['wins']}L) |",
          f"| Winrate | {ls.pct(t['wins'], t['n'])} % |",
          f"| KDA | {t['kda']:.2f} ({k:.1f} / {d:.1f} / {a_:.1f}) |",
          f"| Kill participation | {f_pct(t['kp'])} |",
          f"| Podíl na poškození týmu | {f_pct(t['dmg_share'])} |",
          f"| Poškození / min | {f_num(t['dpm'], '.0f')} |",
          f"| CS / min | {t['cspm']:.1f} |",
          f"| Gold / min | {f_num(t['gpm'], '.0f')} |",
          f"| Vision score / min | {f_num(t['vspm'], '.2f')} |",
          f"| Control wardy / hra | {f_num(t['control'])} |",
          f"| Průměrná délka hry | {t['dur']:.0f} min |",
          f"| Vzdané prohry | {f_pct(a['surrender_losses'])} |", ""]

    kind, length = a["streak"]
    L += ["## Forma a aktivita", "",
          f"- **Forma (nejnovější vlevo):** `{a['form']}`",
          f"- **Aktuální série:** {length}× {'výhra' if kind == 'W' else 'prohra'}"]
    if "trend" in a:
        L.append(f"- **Trend:** posledních 10 her {a['trend'][0]} % WR · dřívějších {a['n'] - 10} "
                 f"{ls.her(a['n'] - 10)} {a['trend'][1]} % WR")
    L.append(f"- **Aktivita:** naposledy {ago(a['games'][0]['ts'])} · za 7 dní {a['games_7d']} "
             f"{ls.her(a['games_7d'])} · za 30 dní {a['games_30d']} {ls.her(a['games_30d'])}")
    L.append("- **Kdy hraje (místní čas):** "
             + " · ".join(f"{lbl} {ls.pct(c, a['n'])} %" for lbl, c in a["day_parts"] if c))
    L.append("- **Dny:** " + " · ".join(f"{day} {c}" for day, c in a["weekdays"]))
    queues = collections.Counter(ls.QUEUE_NAMES.get(g["queue"], str(g["queue"])) for g in a["games"])
    if len(queues) > 1:
        L.append("- **Fronty:** " + " · ".join(f"{q} {c}" for q, c in queues.most_common()))
    L.append("")

    if a["roles"]:
        L += ["## Role", "", "| Role | Hry | Podíl | WR | KDA | CS/min |", "|---|---:|---:|---:|---:|---:|"]
        for r, s in a["roles"]:
            L.append(f"| {ls.ROLE_CZ[r]} | {s['n']} | {ls.pct(s['n'], a['n'])} % | "
                     f"{ls.pct(s['wins'], s['n'])} % | {s['kda']:.2f} | {s['cspm']:.1f} |")
        L.append("")

    L += ["## Champion pool", "",
          f"Počet různých championů: {len(a['champs'])} ({ls.posledni(a['n']).lower()}).", "",
          "| Champion | Hry | WR | KDA | CS/min | DMG/min | KP | Naposledy |",
          "|---|---:|---:|---:|---:|---:|---:|---|"]
    for c, s in a["champs"][:TOP_CHAMPS]:
        L.append(f"| {c} | {s['n']} | {ls.pct(s['wins'], s['n'])} % | {s['kda']:.2f} | {s['cspm']:.1f} | "
                 f"{f_num(s['dpm'], '.0f')} | {f_pct(s['kp'])} | {ago(s['last_ts'])} |")
    L.append("")
    render_mastery(L, player)

    L += ["## Lajnovací fáze", "",
          f"- **CS v 10. minutě:** {f_num(t['cs10'])}",
          f"- **Výrazný náskok v lajně** (≥ 20 % gold+XP nad soupeřem ve 14. min): {f_pct(t['lane_adv'])} her",
          f"- **Výrazné manko v lajně** (soupeř o ≥ 20 % napřed): {f_pct(t['lane_lost'])} her",
          f"- **Solo killy / hra:** {f_num(t['solo_kills'])}",
          f"- **Účast na first bloodu:** {f_pct(t['first_blood'])} her", ""]

    if a["wins"] and a["losses"]:
        w, l = a["wins"], a["losses"]
        L += ["## Výhry vs. prohry", "", "| Metrika | Výhry | Prohry |", "|---|---:|---:|",
              f"| KDA | {w['kda']:.2f} | {l['kda']:.2f} |",
              f"| Smrti / hra | {w['avg'][1]:.1f} | {l['avg'][1]:.1f} |",
              f"| Kill participation | {f_pct(w['kp'])} | {f_pct(l['kp'])} |",
              f"| Poškození / min | {f_num(w['dpm'], '.0f')} | {f_num(l['dpm'], '.0f')} |",
              f"| CS v 10. min | {f_num(w['cs10'])} | {f_num(l['cs10'])} |",
              f"| Výrazný náskok v lajně | {f_pct(w['lane_adv'])} | {f_pct(l['lane_adv'])} |",
              f"| Výrazné manko v lajně | {f_pct(w['lane_lost'])} | {f_pct(l['lane_lost'])} |",
              f"| Vision score / min | {f_num(w['vspm'], '.2f')} | {f_num(l['vspm'], '.2f')} |",
              f"| Délka hry | {w['dur']:.0f} min | {l['dur']:.0f} min |", ""]

    L += ["## Délka hry", "", "| Délka | Hry | WR |", "|---|---:|---:|"]
    for label, n, w in a["length"]:
        L.append(f"| {label} | {n} | {f'{ls.pct(w, n)} %' if n else '–'} |")
    L.append("")

    if a["opponents"]:
        L += ["## Soupeři v lajně", "", f"Championi, proti kterým hrál v lajně aspoň {MIN_OPP_GAMES}×.", "",
              "| Soupeř | Hry | WR hráče proti němu |", "|---|---:|---:|"]
        for c, n, w in a["opponents"][:10]:
            L.append(f"| {c} | {n} | {ls.pct(w, n)} % |")
        L.append("")

    if a["duos"]:
        L += ["## Opakovaní spoluhráči", "",
              f"Hráči ve stejném týmu v ≥ {MIN_DUO_GAMES} hrách – pravděpodobně duo/premade "
              "(ve vysokém elu může jít i o náhodu).", "",
              "| Spoluhráč | Spolu her | WR spolu |", "|---|---:|---:|"]
        for rid, n, w in a["duos"][:8]:
            L.append(f"| {rid} | {n} | {ls.pct(w, n)} % |")
        L.append("")

    L += [f"## {ls.posledni(min(RECENT_GAMES, a['n']))}", "",
          "| Datum | Fronta | Champion | Role | Výsledek | KDA | CS/min | DMG | KP | Délka |",
          "|---|---|---|---|---|---|---:|---:|---:|---:|"]
    for g in a["games"][:RECENT_GAMES]:
        when = dt.datetime.fromtimestamp(g["ts"] / 1000)
        L.append(f"| {when:%d. %m. %H:%M} | {ls.QUEUE_NAMES.get(g['queue'], g['queue'])} | {g['champ']} | "
                 f"{ls.ROLE_CZ.get(g['role'], g['role'] or '?')} | {'W' if g['win'] else 'L'} | "
                 f"{g['k']}/{g['d']}/{g['a']} | {g['cs'] / (g['dur'] / 60):.1f} | {g['dmg'] // 1000}k | "
                 f"{f_pct(g['kp'])} | {g['dur'] // 60} min |")
    L.append("")
    return finish(L)


def render_mastery(L, player):
    if not player.get("mastery"):
        return
    L += ["## Mastery", "", "| Champion | Level | Body | Naposledy hrál |", "|---|---:|---:|---|"]
    for m in player["mastery"]:
        L.append(f"| {m['name']} | {m['level']} | {m['points'] // 1000}k | "
                 f"{ago(m['last']) if m['last'] else '–'} |")
    L.append("")


def finish(L):
    L += ["---",
          "_lolscout isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games "
          "or anyone officially involved in producing or managing Riot Games properties. Riot Games, and "
          "all associated properties are trademarks or registered trademarks of Riot Games, Inc._"]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- hlavní logika

def main(argv=None):
    ls.load_dotenv()
    ap = argparse.ArgumentParser(
        description="Hloubková analýza jednoho hráče LoL podle Riot ID (Riot API).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Příklady:\n"
               "  player-scout.py \"Hrac#EUNE\"\n"
               "  player-scout.py \"Hrac#EUNE\" --games 100 --queue solo --json hrac.json")
    ap.add_argument("riot_id", help="Riot ID hráče ve tvaru \"Jméno#TAG\"")
    ap.add_argument("--games", type=int, default=DEFAULT_GAMES,
                    help=f"kolik posledních her analyzovat (výchozí {DEFAULT_GAMES}, max 100)")
    ap.add_argument("--queue", choices=list(ls.QUEUE_FILTER), default="ranked",
                    help="ranked = solo+flex (výchozí), solo, flex, all")
    ap.add_argument("--region", default="eune", choices=list(ls.PLATFORMS))
    ap.add_argument("--no-mastery", action="store_true", help="nestahovat champion mastery")
    ap.add_argument("-o", "--out", help="výstupní .md soubor (výchozí scout_<Jméno-TAG>_<datum>.md)")
    ap.add_argument("--json", help="uložit i spočítaná data do JSON souboru")
    ap.add_argument("--key", help="Riot API klíč (jinak proměnná RIOT_API_KEY nebo .env)")
    ap.add_argument("--cache-dir", default=".lolscout-cache")
    ap.add_argument("--production-limits", action="store_true",
                    help="limity production klíče (500/10 s) místo personal/dev (20/1 s, 100/2 min)")
    a = ap.parse_args(argv)

    key = a.key or os.environ.get("RIOT_API_KEY")
    if not key:
        sys.exit("Chyba: chybí API klíč. Nastav RIOT_API_KEY (nebo .env) – klíč získáš na "
                 "https://developer.riotgames.com")
    games_n = max(1, min(a.games, 100))
    platform = ls.PLATFORMS[a.region]
    limits = ((500, 10.0), (30000, 600.0)) if a.production_limits else ((20, 1.0), (100, 120.0))
    riot = ls.Riot(key, platform, a.cache_dir, limits)
    champs = ls.Champions(a.cache_dir)

    name, tag = ls.split_riot_id(a.riot_id)
    acc = riot.account_by_riot_id(name, tag)
    if not acc:
        sys.exit(f"Chyba: Riot ID {name}#{tag} neexistuje (zkontroluj jméno a tag).")
    puuid = acc["puuid"]
    summoner = riot.summoner(puuid)
    if not summoner:
        sys.exit(f"Chyba: {name}#{tag} nemá účet na platformě {a.region.upper()} – zkus jiný --region.")
    player = {"puuid": puuid, "name": f"{acc.get('gameName', name)}#{acc.get('tagLine', tag)}",
              "summoner": {"summonerLevel": summoner.get("summonerLevel")}}

    est = games_n + 4 + (0 if a.no_mastery else 1)
    eta = "pár sekund" if est <= 100 else f"cca {((est - 1) // 100) * 2} min"
    ls.log(f"Analyzuji {player['name']} – až {games_n} {ls.her(games_n)} (až ~{est} požadavků na API; "
           f"s personal klíčem {eta}, opakovaný běh je díky cache rychlejší)")
    ids = riot.match_ids(puuid, games_n, ls.QUEUE_FILTER[a.queue])
    games = []
    for i, mid in enumerate(ids, 1):
        if i % 10 == 0:
            ls.log(f"  zápasy {i}/{len(ids)}")
        m = riot.match(mid)
        g = game_record(m, puuid, champs) if m else None
        if g:
            games.append(g)
    games.sort(key=lambda g: g["ts"], reverse=True)

    player["league"] = riot.league(puuid)
    player["mastery"] = [] if a.no_mastery else [
        {"name": champs.name(key=m.get("championId")), "level": m.get("championLevel", 0),
         "points": m.get("championPoints", 0), "last": m.get("lastPlayTime", 0)}
        for m in riot.mastery_top(puuid, MASTERY_COUNT)]

    analysis = analyze(games, player["mastery"])
    meta = {"platform": platform, "games": games_n, "queue_label": QUEUE_LABEL[a.queue]}
    md = render(player, analysis, meta)
    safe = re.sub(r"[^\w-]+", "_", player["name"].replace("#", "-"))
    out = Path(a.out or f"scout_{safe}_{dt.datetime.now():%Y-%m-%d_%H%M}.md")
    out.write_text(md, encoding="utf-8")
    if a.json:
        data = {"player": player, "analysis": analysis}
        Path(a.json).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=list), encoding="utf-8")
    ls.log(f"\nHotovo: {out}  ({riot.requests_made} požadavků na Riot API)")
    return out


if __name__ == "__main__":
    main()
