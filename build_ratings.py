"""
build_ratings.py  --  Process RP_Results JSON files into rating tables.

Reads rp_results_*.json from RP_Results/ and produces:
  horse_form.json, trainer_stats.json, jockey_stats.json,
  course_draw_stats.json, par_times.json

Incremental by default: only processes files newer than the existing
horse_form.json so the daily run stays fast (typically 1-2 files).
Use --full to force a complete rebuild from all files.

Usage:
    python build_ratings.py           # incremental (daily use)
    python build_ratings.py --full    # full rebuild
"""

import os, re, json, glob, sys
from datetime import date, datetime, timedelta
from collections import defaultdict
import statistics
from pathlib import Path
_BASE = Path(__file__).resolve().parent

# ── Paths ─────────────────────────────────────────────────────────────────────
RP_DIR   = str(_BASE / "RP_Results")
OUT_DIR  = str(_BASE)
TODAY    = date.today()

# ── Incremental: skip files already in horse_form.json ───────────────────────
def _last_processed_date() -> str:
    """Return the latest date already processed, or \'\' if none.
    Prefers last_processed.txt (written by incremental runs, very fast to read)
    over scanning horse_form.json (slow on large files).
    """
    cp = Path(OUT_DIR) / "last_processed.txt"
    if cp.exists():
        try:
            return cp.read_text().strip()
        except Exception:
            pass
    hf = Path(OUT_DIR) / "horse_form.json"
    if not hf.exists():
        return ""
    try:
        text = hf.read_text(encoding="utf-8", errors="ignore")
        dates = re.findall(r'"date":"(20\d\d-\d\d-\d\d)"', text)
        return max(dates) if dates else ""
    except Exception:
        return ""

FULL_REBUILD = "--full" in sys.argv
if FULL_REBUILD:
    print("Full rebuild requested — processing all files.")
    _cutoff = ""
else:
    _cutoff = _last_processed_date()
    if _cutoff:
        print(f"Incremental mode — skipping files up to and including {_cutoff}")

# ── Excuse keywords in race comments ──────────────────────────────────────────
EXCUSE_KEYWORDS = [
    "not clear run", "no clear run", "no room", "short of room",
    "hampered", "bumped", "stumbled", "slowly away", "dwelt",
    "lost ground start", "lost all chance start", "carried wide",
    "ran wide", "hung", "reared", "badly hampered", "checked",
    "squeezed", "tight for room", "interference",
]

# ── Going normalisation ────────────────────────────────────────────────────────
def norm_going(g):
    g = (g or "").lower().strip()
    if not g:                             return "unknown"
    if "heavy"        in g:              return "heavy"
    if "soft"         in g and "good" in g: return "good_to_soft"
    if "soft"         in g:              return "soft"
    if "good to firm" in g:              return "good_to_firm"
    if "good"         in g and "firm" in g: return "good_to_firm"
    if "firm"         in g:              return "firm"
    if "good"         in g:              return "good"
    if "standard to slow" in g:          return "standard_to_slow"
    if "standard to fast" in g:          return "standard_to_fast"
    if "standard"     in g:              return "standard"
    if "slow"         in g:              return "slow"
    if "fast"         in g:              return "fast"
    return "other"

# ── Distance band ──────────────────────────────────────────────────────────────
def dist_band(dist_str):
    """Classify distance string into sprint/mile/middle/staying."""
    d = (dist_str or "").lower()
    m = re.search(r'(\d+)f', d)
    if not m:
        m2 = re.search(r'(\d+)m', d)
        if m2:
            furlongs = int(m2.group(1)) * 8
        else:
            return "unknown"
    else:
        furlongs = int(m.group(1))
        extra = re.search(r'(\d+)y', d)
        if extra:
            furlongs += int(extra.group(1)) / 220
    if furlongs <= 6:   return "sprint"
    if furlongs <= 9:   return "mile"
    if furlongs <= 13:  return "middle"
    return "staying"

# ── Win time → seconds ────────────────────────────────────────────────────────
def time_to_secs(t):
    if not t:
        return None
    t = t.strip()
    m = re.match(r'(\d+)m\s*([\d.]+)s', t)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.match(r'([\d.]+)s', t)
    if m:
        return float(m.group(1))
    return None

# ── Horse name normalisation ───────────────────────────────────────────────────
def norm_name(n):
    return re.sub(r"[^a-z0-9]", "", (n or "").lower())

# ── Load files (filter by cutoff in incremental mode) ────────────────────────
all_files = sorted(glob.glob(os.path.join(RP_DIR, "rp_results_*.json")))
if _cutoff:
    files = [f for f in all_files
             if os.path.basename(f).replace("rp_results_", "").replace(".json", "") > _cutoff]
    print(f"Loading {len(files)} new files from RP_Results (of {len(all_files)} total)...")
    if not files:
        print("Nothing new to process — ratings are already up to date.")
        exit(0)
else:
    files = all_files
    print(f"Loading {len(files)} files from RP_Results...")

# ── Accumulators ──────────────────────────────────────────────────────────────
# horse_runs[norm_name] = list of run dicts (chronological)
horse_runs = defaultdict(list)

# trainer/jockey: {name: [(date, won), ...]}
trainer_runs = defaultdict(list)
jockey_runs  = defaultdict(list)

# draw: {(course, dist_band): {stall: [positions]}}
draw_data = defaultdict(lambda: defaultdict(list))

# par times: {(course, distance, going_norm): [winning_secs, ...]}
par_data = defaultdict(list)

total_races = 0
total_runners = 0

# ── Incremental: pre-load existing horse_form + trainer/jockey runs ───────────
SKIP_DRAW_PAR = False
if _cutoff:
    hf_path = Path(OUT_DIR) / "horse_form.json"
    if hf_path.exists():
        print("Loading existing horse_form.json to merge with new data...")
        try:
            existing_hf = json.loads(hf_path.read_bytes())
            for norm, entry in existing_hf.items():
                for run in entry.get("runs", []):
                    horse_runs[norm].append(run)
                    t = run.get("trainer", ""); j = run.get("jockey", "")
                    d_str = run.get("date", ""); won = run.get("won", False)
                    r_course = run.get("course", "")
                    r_going  = run.get("going", "")
                    r_hcap   = run.get("is_hcap", False)
                    try:
                        d_obj = date.fromisoformat(d_str)
                    except Exception:
                        continue
                    if t: trainer_runs[t].append((d_obj, won, r_course, r_going, r_hcap))
                    if j: jockey_runs[j].append((d_obj, won, r_course, r_going, r_hcap))
            print(f"  Loaded {len(existing_hf):,} horses — will add new runs on top")
            SKIP_DRAW_PAR = True   # draw/par can't be rebuilt from horse_form alone
            del existing_hf        # free memory before processing new files
        except Exception as e:
            print(f"  Warning: could not load existing horse_form ({e}) — full rebuild")
            horse_runs.clear(); trainer_runs.clear(); jockey_runs.clear()
            SKIP_DRAW_PAR = False

for fpath in files:
    try:
        with open(fpath, encoding="utf-8") as f:
            raw = f.read()
        end = raw.rfind('}') + 1
        d   = json.loads(raw[:end])
    except Exception:
        continue

    race_date_str = d.get("date", "")
    try:
        race_date = date.fromisoformat(race_date_str)
    except Exception:
        continue

    for race in d.get("races", []):
        # URL format: https://www.racingpost.com/results/{id}/{course-slug}/{date}/{race_id}
        _url_parts = race.get("url", "").split("/")
        course    = _url_parts[5] if len(_url_parts) > 5 else ""
        dist_str  = race.get("distance", "")
        going_raw = race.get("going", "")
        going     = norm_going(going_raw)
        dband     = dist_band(dist_str)
        r_class   = race.get("race_class", "")
        win_time  = race.get("win_time", "")
        win_grade = race.get("win_time_grade", "")
        n_runners = race.get("num_runners") or len(race.get("runners", []))
        prize     = race.get("prize_1st", "")
        # Handicap detection — rating_band like "0-75" indicates a handicap
        _rb       = race.get("rating_band", "") or ""
        is_hcap   = bool(re.search(r"\d+-\d+", _rb))

        # Par time
        win_secs = time_to_secs(win_time)
        if win_secs and course and dist_str:
            par_data[(course, dist_str, going)].append(win_secs)

        runners = race.get("runners", [])
        total_races   += 1
        total_runners += len(runners)

        # Winner
        winner_name = ""
        for r in runners:
            if r.get("pos") == 1:
                winner_name = norm_name(r.get("horse", ""))
                break

        for runner in runners:
            pos     = runner.get("pos")
            hname   = runner.get("horse", "")
            hnorm   = norm_name(hname)
            jockey  = runner.get("jockey", "")
            trainer = runner.get("trainer", "")
            draw    = runner.get("draw")
            rpr     = runner.get("rpr")
            ts      = runner.get("ts")
            or_val  = runner.get("or")
            sp_dec  = runner.get("sp_dec")
            comment = runner.get("comment", "")
            beaten  = runner.get("beaten", "")
            weight  = runner.get("weight", "")
            won     = (pos == 1)

            # Excuse flag
            comment_lower = comment.lower()
            excuse = any(kw in comment_lower for kw in EXCUSE_KEYWORDS)

            # Horse run record
            if hnorm:
                horse_runs[hnorm].append({
                    "date":    race_date_str,
                    "horse":   hname,
                    "course":  course,
                    "dist":    dist_str,
                    "dband":   dband,
                    "going":   going,
                    "class":   r_class,
                    "pos":     pos,
                    "runners": n_runners,
                    "or":      or_val,
                    "rpr":     rpr,
                    "ts":      ts,
                    "sp_dec":  sp_dec,
                    "beaten":  beaten,
                    "weight":  weight,
                    "comment": comment[:200],
                    "excuse":  excuse,
                    "won":     won,
                    "prize":   prize,
                    "trainer": trainer,
                    "jockey":  jockey,
                })

            # Trainer/jockey — include is_hcap for course split by race type
            if trainer:
                trainer_runs[trainer].append((race_date, won, course, going, is_hcap))
            if jockey:
                jockey_runs[jockey].append((race_date, won, course, going, is_hcap))

            # Draw — only flat (draws are meaningless in jumps)
            if draw is not None and pos is not None and dband != "unknown":
                draw_data[(course, dband)][draw].append(pos)

print(f"Loaded {total_races:,} races, {total_runners:,} runners")

# ── Build horse_form ──────────────────────────────────────────────────────────
print("Building horse form profiles...")

def _build_jockey_form(runs):
    """Return {jockey_name: {runs, wins, last_date}} for every jockey this horse has run with."""
    from collections import defaultdict
    jf = defaultdict(lambda: {"runs": 0, "wins": 0, "last_date": ""})
    for r in runs:
        j = (r.get("jockey") or "").strip()
        if not j:
            continue
        jf[j]["runs"] += 1
        if r.get("won"):
            jf[j]["wins"] += 1
        d = r.get("date", "")
        if d > jf[j]["last_date"]:
            jf[j]["last_date"] = d
    return dict(jf)

def _build_jockey_form_by_course(runs):
    """Return {jockey_name: {course_slug: {runs, wins}}} — per-course breakdown.
    Allows predict.py to distinguish 'won on horse at THIS course' vs 'won elsewhere'.
    """
    from collections import defaultdict
    jfc = defaultdict(lambda: defaultdict(lambda: {"runs": 0, "wins": 0}))
    for r in runs:
        j = (r.get("jockey") or "").strip()
        c = (r.get("course") or "").strip()
        if not j or not c:
            continue
        jfc[j][c]["runs"] += 1
        if r.get("won"):
            jfc[j][c]["wins"] += 1
    return {j: dict(courses) for j, courses in jfc.items()}

WEIGHTS = [1.0, 0.7, 0.5, 0.35, 0.25]   # decay weights for recent runs

def weighted_trend(values):
    """Given values most-recent-first, return weighted average and direction."""
    vals = [(v, WEIGHTS[i] if i < len(WEIGHTS) else 0.2)
            for i, v in enumerate(values) if v is not None]
    if not vals:
        return None, None
    total_w  = sum(w for _, w in vals)
    w_avg    = sum(v * w for v, w in vals) / total_w
    # Trend: compare first half vs second half (recent vs older)
    if len(vals) >= 3:
        recent = vals[:len(vals)//2 + 1]
        older  = vals[len(vals)//2:]
        r_avg  = sum(v*w for v,w in recent) / sum(w for _,w in recent)
        o_avg  = sum(v*w for v,w in older)  / sum(w for _,w in older)
        trend  = round(r_avg - o_avg, 1)
    else:
        trend  = None
    return round(w_avg, 1), trend

horse_form = {}

for hnorm, runs in horse_runs.items():
    runs_sorted = sorted(runs, key=lambda x: x["date"])
    recent      = runs_sorted[-10:]   # last 10 runs for storage
    recent_rev  = list(reversed(runs_sorted))   # most recent first

    # RPR/TS trend
    rpr_vals  = [r["rpr"] for r in recent_rev[:6]]
    ts_vals   = [r["ts"]  for r in recent_rev[:6]]
    rpr_wavg, rpr_trend = weighted_trend(rpr_vals)
    ts_wavg,  ts_trend  = weighted_trend(ts_vals)

    # Going preference: avg RPR delta per going type vs overall weighted avg
    going_rpr = defaultdict(list)
    for r in runs_sorted:
        if r["rpr"] and r["going"] != "unknown":
            going_rpr[r["going"]].append(r["rpr"])
    going_pref = {}
    if rpr_wavg:
        for g, rprs in going_rpr.items():
            if len(rprs) >= 2:
                going_pref[g] = round(statistics.mean(rprs) - rpr_wavg, 1)
    best_going = max(going_pref, key=going_pref.get) if going_pref else None

    # Course form: avg RPR delta per course vs overall weighted avg (min 3 runs)
    course_rpr = defaultdict(list)
    for r in runs_sorted:
        if r["rpr"] and r.get("course"):
            course_rpr[r["course"]].append(r["rpr"])
    course_form = {}
    if rpr_wavg:
        for c, rprs in course_rpr.items():
            if len(rprs) >= 2:
                course_form[c] = round(statistics.mean(rprs) - rpr_wavg, 1)

    # Distance-band form: avg RPR delta per distance band vs overall (min 3 runs)
    dist_rpr = defaultdict(list)
    for r in runs_sorted:
        if r["rpr"] and r.get("dband") and r["dband"] != "unknown":
            dist_rpr[r["dband"]].append(r["rpr"])
    dist_form = {}
    if rpr_wavg:
        for db, rprs in dist_rpr.items():
            if len(rprs) >= 2:
                dist_form[db] = round(statistics.mean(rprs) - rpr_wavg, 1)

    # OR vs RPR gap from last run (positive = running above official rating)
    last = recent_rev[0] if recent_rev else {}
    last_rpr  = last.get("rpr")
    last_or   = last.get("or")
    or_rpr_gap = (last_rpr - last_or) if (last_rpr and last_or) else None

    # Days since last run
    try:
        last_date_obj = date.fromisoformat(last.get("date", ""))
        days_since    = (TODAY - last_date_obj).days
    except Exception:
        days_since = None

    horse_form[hnorm] = {
        "name":        last.get("horse", hnorm),
        "runs":        recent,
        "rpr_wavg":    rpr_wavg,       # weighted avg RPR (last 5 runs) — overall ability
        "rpr_trend":   rpr_trend,      # direction: positive = improving
        "ts_wavg":     ts_wavg,        # weighted avg TS
        "ts_trend":    ts_trend,
        "going_pref":  going_pref,     # {going: RPR_delta} — prefers going if positive
        "course_form": course_form,    # {course_slug: RPR_delta} — prefers course if positive
        "dist_form":   dist_form,      # {dist_band: RPR_delta} — prefers distance if positive
        "best_going":  best_going,
        "last_rpr":    last_rpr,
        "last_or":     last_or,
        "or_rpr_gap":  or_rpr_gap,
        "last_class":  last.get("class", ""),
        "last_comment":last.get("comment", ""),
        "last_excuse": last.get("excuse", False),
        "last_date":   last.get("date", ""),
        "days_since":  days_since,
        "total_runs":  len(runs_sorted),
        "total_wins":  sum(1 for r in runs_sorted if r.get("won")),
        "trainer":     last.get("trainer", ""),
        "jockey":      last.get("jockey", ""),
        "jockey_form":           _build_jockey_form(runs_sorted),
        "jockey_form_by_course": _build_jockey_form_by_course(runs_sorted),
    }

print(f"  {len(horse_form):,} unique horses profiled")

# ── Build trainer / jockey stats ──────────────────────────────────────────────
print("Building trainer & jockey stats...")

def build_person_stats(runs_dict):
    stats = {}
    cutoff_14 = TODAY - timedelta(days=14)
    cutoff_28 = TODAY - timedelta(days=28)

    def pct(w, r): return round(w/r*100, 1) if r else 0.0

    for name, runs in runs_dict.items():
        all_r  = len(runs)
        all_w  = sum(1 for r in runs if r[1])
        r14    = [r for r in runs if r[0] >= cutoff_14]
        r28    = [r for r in runs if r[0] >= cutoff_28]

        # By course — overall
        by_course       = defaultdict(lambda: [0, 0])
        by_course_hcap  = defaultdict(lambda: [0, 0])
        by_course_cond  = defaultdict(lambda: [0, 0])
        for r in runs:
            _, won, course, _, is_hcap = r[0], r[1], r[2], r[3], r[4] if len(r) > 4 else False
            by_course[course][0] += 1
            if won: by_course[course][1] += 1
            if is_hcap:
                by_course_hcap[course][0] += 1
                if won: by_course_hcap[course][1] += 1
            else:
                by_course_cond[course][0] += 1
                if won: by_course_cond[course][1] += 1

        # By going
        by_going = defaultdict(lambda: [0, 0])
        for r in runs:
            _, won, _, going = r[0], r[1], r[2], r[3]
            by_going[going][0] += 1
            if won: by_going[going][1] += 1

        stats[name] = {
            "all":    {"runs": all_r, "wins": all_w, "win_pct": pct(all_w, all_r)},
            "last14": {"runs": len(r14), "wins": sum(1 for r in r14 if r[1]),
                       "win_pct": pct(sum(1 for r in r14 if r[1]), len(r14))},
            "last28": {"runs": len(r28), "wins": sum(1 for r in r28 if r[1]),
                       "win_pct": pct(sum(1 for r in r28 if r[1]), len(r28))},
            "by_course": {c: {"runs": v[0], "wins": v[1], "win_pct": pct(v[1],v[0])}
                          for c, v in by_course.items() if v[0] >= 3},
            "by_course_hcap": {c: {"runs": v[0], "wins": v[1], "win_pct": pct(v[1],v[0])}
                               for c, v in by_course_hcap.items() if v[0] >= 1},
            "by_course_cond": {c: {"runs": v[0], "wins": v[1], "win_pct": pct(v[1],v[0])}
                               for c, v in by_course_cond.items() if v[0] >= 1},
            "by_going":  {g: {"runs": v[0], "wins": v[1], "win_pct": pct(v[1],v[0])}
                          for g, v in by_going.items() if v[0] >= 3},
        }
    return stats

trainer_stats = build_person_stats(trainer_runs)
jockey_stats  = build_person_stats(jockey_runs)
print(f"  {len(trainer_stats):,} trainers  |  {len(jockey_stats):,} jockeys")

# ── Build draw stats ───────────────────────────────────────────────────────────
print("Building draw stats...")

course_draw_stats = {}
for (course, dband), stall_data in draw_data.items():
    if course not in course_draw_stats:
        course_draw_stats[course] = {}
    if dband not in course_draw_stats[course]:
        course_draw_stats[course][dband] = {}
    for stall, positions in stall_data.items():
        if len(positions) < 3:
            continue
        wins = sum(1 for p in positions if p == 1)
        course_draw_stats[course][dband][str(stall)] = {
            "runs":    len(positions),
            "wins":    wins,
            "win_pct": round(wins / len(positions) * 100, 1),
            "avg_pos": round(statistics.mean(positions), 2),
        }

draw_courses = sum(1 for c in course_draw_stats
                   for d in course_draw_stats[c]
                   if course_draw_stats[c][d])
print(f"  {draw_courses} course/distance combinations with draw data")

# ── Build par times ────────────────────────────────────────────────────────────
print("Building speed par times...")

par_times = {}
for (course, dist, going), times in par_data.items():
    if len(times) < 5:   # need at least 5 samples for a reliable par
        continue
    par_secs = round(statistics.median(times), 2)
    if course not in par_times:
        par_times[course] = {}
    if dist not in par_times[course]:
        par_times[course][dist] = {}
    par_times[course][dist][going] = {
        "par_secs": par_secs,
        "count":    len(times),
        "fastest":  round(min(times), 2),
        "slowest":  round(max(times), 2),
    }

par_entries = sum(len(par_times[c][d]) for c in par_times for d in par_times[c])
print(f"  {par_entries} course/distance/going par times computed")

# ── Save outputs ───────────────────────────────────────────────────────────────
print("Saving...")

def save(obj, filename, compact=False):
    """Write obj to filename in OUT_DIR.

    compact=True writes with no indentation — vital for large files like
    horse_form.json where the indented version hits ~165 MB and silently
    truncates on Windows.  predict.py only needs to load() the data so
    human-readable indentation is unnecessary.
    """
    path = os.path.join(OUT_DIR, filename)
    sep  = (',', ':') if compact else (',', ': ')
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f,
                  indent=None if compact else 2,
                  separators=sep,
                  ensure_ascii=True)
        f.flush()
        f.truncate()           # cut any leftover bytes from a previous larger file
        os.fsync(f.fileno())   # force OS write cache → disk
    size_mb = os.path.getsize(path) / 1_000_000
    print(f"  ✓ {filename}  ({size_mb:.1f} MB)")

# ── Save outputs ──────────────────────────────────────────────────────────────
if SKIP_DRAW_PAR:
    # Incremental mode: only write the NEW/changed horse entries as a small
    # patch file, plus trainer/jockey stats. Avoids rewriting the 180MB
    # horse_form.json on every daily run (slow over network mounts).
    new_norm_names = set()
    for fpath in files:
        try:
            raw = open(fpath, encoding="utf-8").read()
            d = json.loads(raw[:raw.rfind('}')+1])
        except Exception:
            continue
        for race in d.get("races", []):
            for runner in race.get("runners", []):
                new_norm_names.add(norm_name(runner.get("name", "")))

    patch = {k: v for k, v in horse_form.items() if k in new_norm_names}
    patch_path = os.path.join(OUT_DIR, "horse_form_patch.json")
    with open(patch_path, "w", encoding="utf-8") as f:
        json.dump(patch, f, separators=(',', ':'), ensure_ascii=True)
        f.flush(); f.truncate(); os.fsync(f.fileno())
    print(f"  ✓ horse_form_patch.json  ({os.path.getsize(patch_path)/1e6:.1f} MB, {len(patch)} horses)")

    # Checkpoint so next incremental run knows where to start
    checkpoint = max(
        os.path.basename(f).replace("rp_results_","").replace(".json","")
        for f in files
    )
    Path(os.path.join(OUT_DIR, "last_processed.txt")).write_text(checkpoint)
    print(f"  ✓ last_processed.txt  ({checkpoint})")

    save(trainer_stats, "trainer_stats.json")
    save(jockey_stats,  "jockey_stats.json")
    print("  (horse_form.json and draw/par unchanged — incremental mode)")
else:
    # Full rebuild: write everything, clean up any stale patch/checkpoint
    save(horse_form,        "horse_form.json",  compact=True)
    save(trainer_stats,     "trainer_stats.json")
    save(jockey_stats,      "jockey_stats.json")
    save(course_draw_stats, "course_draw_stats.json")
    save(par_times,         "par_times.json")
    for leftover in ["horse_form_patch.json", "last_processed.txt"]:
        lp = Path(os.path.join(OUT_DIR, leftover))
        if lp.exists(): lp.unlink()

print("\nDone. Rating tables ready in", OUT_DIR)
print(f"  Horses:   {len(horse_form):,}")
print(f"  Trainers: {len(trainer_stats):,}")
print(f"  Jockeys:  {len(jockey_stats):,}")
