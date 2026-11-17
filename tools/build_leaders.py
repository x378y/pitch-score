#!/usr/bin/env python3
"""Build the league-wide leaderboard file for one or more MLB seasons.

Usage:  python3 tools/build_leaders.py 2026            (one season)
        python3 tools/build_leaders.py 2020 2026       (a range)

Writes data/leaders-<season>.json next to index.html. Uses only the Python
standard library (plus certifi if it's installed, for Macs without system certs).
The scoring here must match index.html exactly.
"""
import json, os, ssl, sys, time, statistics as st, urllib.request, concurrent.futures as cf
from datetime import datetime, timezone

API = "https://statsapi.mlb.com/api/v1"
try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    CTX = ssl.create_default_context()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

def get(url, tries=5):
    for t in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60, context=CTX) as r:
                return json.load(r)
        except Exception:
            time.sleep(1.5 * (t + 1))
    raise RuntimeError("failed: " + url)

# ---------- scoring (keep in sync with index.html) ----------
def start_score(g):
    return 40 + 2*g["outs"] + g["k"] - 2*g["h"] - 3*g["bb"] - 3*g["hbp"] - 4*g["er"] - 6*g["solo"] - 8*g["multi"]

CLOSE = {0: 8, 1: 8, 2: 5}
def relief_score(g):
    perfect = g["outs"] >= 3 and g["h"] + g["bb"] + g["hbp"] == 0
    clean = g["r"] == 0 and g["irs"] == 0
    late = g["inn"] >= g["sched"] - 2
    close = CLOSE.get(g["lead"], 0) if (late and clean and g["inn"]) else 0
    return (40 + 3*g["outs"] + g["k"] - 2*g["h"] - 3*g["bb"] - 3*g["hbp"] - 6*g["er"] - 8*g["solo"] - 10*g["multi"]
            - 3*g["irs"] + 2*(g["ir"] - g["irs"]) + 10*g["sv"] + 6*g["hld"] - 10*g["bs"]
            + 2*max(0, g["outs"]//3 - 1) + 5*g["xi"] + (8 if perfect else 0) + close)

def classify(apps):
    """Split one pitcher's games into starts and relief outings.
    - Relief outing of 4+ IP with no save or hold -> scored as a start (long relief).
    - Opener: a start under 2 IP and under 40 pitches by a pitcher who relieves more than he starts -> relief."""
    n_start = sum(1 for g in apps if g["gs"])
    reliever = len(apps) - n_start > n_start
    starts, relief = [], []
    for g in apps:
        if g["gs"]:
            if reliever and g["outs"] < 6 and g["np"] < 40:
                g["opener"] = True
                relief.append(g)
            else:
                starts.append(g)
        elif g["outs"] >= 12 and not g["sv"] and not g["hld"]:
            g["lr"] = True
            starts.append(g)
        else:
            relief.append(g)
    return starts, relief

# Values keep 3 decimals; the app rounds them for display so numbers match the pitcher pages.
# ---------- data ----------
def season_pitchers(season):
    ids = {}
    for gt in ("R", "P"):
        d = get(f"{API}/stats?stats=season&group=pitching&season={season}&sportId=1&gameType={gt}&playerPool=all&limit=5000&fields=stats,splits,player,id,fullName")
        for x in (d.get("stats") or [{}])[0].get("splits", []):
            ids[x["player"]["id"]] = x["player"]["fullName"]
    return ids

def game_logs(pid, season):
    out = []
    for gt in ("R", "P"):
        j = get(f"{API}/people/{pid}/stats?stats=gameLog&group=pitching&season={season}&gameType={gt}")
        for s in (j.get("stats") or [{}])[0].get("splits", []):
            x = s["stat"]
            out.append(dict(pk=s["game"]["gamePk"], date=s["date"], post=s.get("gameType", gt) != "R",
                team=(s.get("team") or {}).get("id"), gs=x.get("gamesStarted", 0) == 1,
                outs=x.get("outs", 0), h=x.get("hits", 0), bb=x.get("baseOnBalls", 0), hbp=x.get("hitBatsmen", 0),
                er=x.get("earnedRuns", 0), r=x.get("runs", 0), k=x.get("strikeOuts", 0), hr=x.get("homeRuns", 0),
                np=x.get("numberOfPitches", 0), ir=x.get("inheritedRunners", 0), irs=x.get("inheritedRunnersScored", 0),
                sv=x.get("saves", 0), hld=x.get("holds", 0), bs=x.get("blownSaves", 0)))
    return pid, out

def scheduled_innings(season):
    d = get(f"{API}/schedule?sportId=1&season={season}&gameType=R,F,D,L,W&hydrate=linescore&fields=dates,games,gamePk,linescore,scheduledInnings")
    return {g["gamePk"]: (g.get("linescore") or {}).get("scheduledInnings") or 9 for dt in d.get("dates", []) for g in dt["games"]}

def game_context(pk):
    """Per pitcher in one game: inning and lead when he entered, extra-inning flag inputs, solo/multi HR."""
    j = get(f"{API}/game/{pk}/playByPlay?fields=allPlays,result,eventType,rbi,awayScore,homeScore,about,inning,isTopInning,matchup,pitcher,id")
    seen, prev = {}, (0, 0)
    for p in j.get("allPlays", []):
        pid = p["matchup"]["pitcher"]["id"]
        r = p.get("result", {})
        c = seen.get(pid)
        if c is None:
            a, h = prev
            c = seen[pid] = dict(inn=p["about"]["inning"], lead=(h - a) if p["about"]["isTopInning"] else (a - h), maxinn=0, solo=0, multi=0)
        c["maxinn"] = max(c["maxinn"], p["about"]["inning"])
        if r.get("eventType") == "home_run":
            if r.get("rbi", 0) > 1: c["multi"] += 1
            else: c["solo"] += 1
        prev = (r.get("awayScore", prev[0]), r.get("homeScore", prev[1]))
    return pk, seen

def build(season):
    t0 = time.time()
    teams = {t["id"]: t["abbreviation"] for t in get(f"{API}/teams?sportId=1&season={season}&fields=teams,id,abbreviation")["teams"]}
    names = season_pitchers(season)
    print(f"{season}: {len(names)} pitchers", flush=True)
    logs = {}
    with cf.ThreadPoolExecutor(10) as ex:
        for pid, out in ex.map(lambda p: game_logs(p, season), names):
            if out: logs[pid] = out
    sched = scheduled_innings(season)
    pks = sorted({g["pk"] for gs in logs.values() for g in gs})
    print(f"{season}: {sum(len(v) for v in logs.values())} appearances in {len(pks)} games", flush=True)
    ctx = {}
    with cf.ThreadPoolExecutor(10) as ex:
        for pk, seen in ex.map(game_context, pks): ctx[pk] = seen
    rows = {"sp": [], "rp": []}
    for pid, apps in logs.items():
        apps.sort(key=lambda g: g["date"])
        for g in apps:
            c = ctx.get(g["pk"], {}).get(pid)
            g["sched"] = sched.get(g["pk"], 9)
            if c:
                g["inn"], g["lead"], g["xi"] = c["inn"], c["lead"], int(c["maxinn"] > g["sched"])
                solo, multi = c["solo"], c["multi"]
                if solo + multi != g["hr"]: solo = min(solo, g["hr"]); multi = g["hr"] - solo
                g["solo"], g["multi"] = solo, multi
            else:
                g["inn"], g["lead"], g["xi"], g["solo"], g["multi"] = 0, None, 0, 0, g["hr"]
        starts, relief = classify(apps)
        for g in starts: g["score"] = start_score(g)
        for g in relief: g["score"] = relief_score(g)
        team = teams.get(apps[-1]["team"], "")
        for role, gs, good in (("sp", starts, 55), ("rp", relief, 60)):
            reg = [g for g in gs if not g["post"]]
            post = [g for g in gs if g["post"]]
            if not gs: continue
            sc = [g["score"] for g in reg]
            outs = sum(g["outs"] for g in reg); np_ = sum(g["np"] for g in reg)
            row = dict(id=pid, name=names[pid], team=team, n=len(reg),
                avg=round(st.mean(sc), 3) if sc else None,
                med=st.median(sc) if sc else None,
                sd=round(st.pstdev(sc), 3) if len(sc) >= 2 else None,
                best=max(sc) if sc else None,
                good=round(100 * sum(1 for v in sc if v >= good) / len(sc)) if sc else None,
                ppi=round(np_ / (outs / 3), 3) if outs and np_ else None,
                pn=len(post), pavg=round(st.mean(g["score"] for g in post), 3) if post else None,
                sc=sc)
            if role == "rp":
                row["sv"] = sum(g["sv"] for g in reg); row["hld"] = sum(g["hld"] for g in reg)
            rows[role].append(row)
    for r in rows.values(): r.sort(key=lambda x: -(x["avg"] if x["avg"] is not None else -999))
    os.makedirs(OUT, exist_ok=True)
    out = dict(season=season, built=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), sp=rows["sp"], rp=rows["rp"])
    with open(os.path.join(OUT, f"leaders-{season}.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"{season}: done in {time.time() - t0:.0f}s ({len(rows['sp'])} starters, {len(rows['rp'])} relievers)", flush=True)

if __name__ == "__main__":
    a = [int(x) for x in sys.argv[1:]] or [datetime.now().year]
    for s in range(a[0], (a[1] if len(a) > 1 else a[0]) + 1):
        build(s)
