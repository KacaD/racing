"""
Winsmore — daily racing display dashboard builder.

Reads C:\\Betfair\\todays_card.json (the race card produced by the Betfair
tipping pipeline) and renders a single self-contained HTML file at
C:\\Winsmore\\dashboard.html (and index.html for GitHub Pages).

Display only -- no model scores, no picks, no settlement. Just the card
and the tags derived from trainer/jockey stats.

Run:
    python build_dashboard.py            # use C:\\Betfair\\todays_card.json
    python build_dashboard.py path.json  # use a different card file
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from html import escape
from pathlib import Path

import tags  # local module — runner tag engine

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

def _resolve_betfair_dir() -> Path:
    env = os.environ.get("BETFAIR_DIR")
    if env:
        p = Path(env)
        if p.exists():
            return p
    win = Path(r"C:\Betfair")
    if win.exists():
        return win
    for cand in Path("/sessions").glob("*/mnt/Betfair"):
        if cand.exists():
            return cand
    return win

BETFAIR_DIR  = _resolve_betfair_dir()
DEFAULT_CARD = BETFAIR_DIR / "todays_card.json"
OUTPUT_HTML  = SCRIPT_DIR / "dashboard.html"


# ---------------------------------------------------------------------------
# Card loading (with truncation tolerance)
# ---------------------------------------------------------------------------
def load_card(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    closer = "\n      ]\n    }\n  ]\n}"
    for m in reversed(list(re.finditer(r"\n        \}", raw))):
        try:
            return json.loads(raw[: m.end()] + closer)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"Could not parse {path} even with truncation repair")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fmt_int(v):
    return "—" if v in (None, "", 0) else str(v)

def fmt_or(v):
    return "—" if v in (None, "", 0) else str(v)

def fmt_form(form):
    return form if form else "—"


# Colour each character of a form string.
#   1            → win    (green)
#   2,3          → place  (amber)
#   4-9, 0       → also-ran (grey)
#   F,U,P,R,B,S,D,C,O → did-not-finish (red)
#   '-' '/' separators kept dim
_FORM_FAIL = set("FUPRBSDCO")

def render_form_button(runner: dict, ctx) -> str:
    """Render the form string as a clickable button that opens a popover
    showing the last few runs in detail (date, course, distance, type,
    going, position, comment).

    Falls back to a plain inline span if we don't have ctx / horse_form
    data for this horse — keeps colouring either way.
    """
    form = runner.get("form") or ""
    coloured = render_form_coloured(form)
    inner = f'<b>F</b> {coloured}'

    # Without context we can't build the breakdown — render plain
    if ctx is None or not getattr(ctx, "horse_form", None):
        return f'<span class="st mono">{inner}</span>'

    import tags as _tags  # local — avoid hard import at module top
    key = _tags._norm_horse_name(runner.get("name", ""))
    entry = ctx.horse_form.get(key)
    if not entry or not entry.get("runs"):
        return f'<span class="st mono">{inner}</span>'

    runs = list(entry["runs"])
    # Most recent first
    runs.sort(key=lambda r: r.get("date", ""), reverse=True)
    runs = runs[:6]

    rows = []
    for r in runs:
        rt = _tags._run_race_type(r)
        rt_label = "NH" if rt == "nh" else "Flat"
        pos = r.get("pos", "?")
        n = r.get("runners", "?")
        won = r.get("won")
        pos_disp = f'<span class="pop-pos {"win" if won else ("place" if str(pos) in ("2","3") else "")}">{pos}/{n}</span>'
        going = (r.get("going") or "—").replace("_", " ")
        course = (r.get("course") or "—").replace("-aw", " AW")
        dist = (r.get("dist") or "—").strip("()") or "—"
        date = r.get("date", "—")
        cls = r.get("class", "")
        comment = r.get("comment") or ""
        # Trim long comment text — RP comments often have trailing odds info in parens
        comment = re.sub(r"\s*\([^)]*\)\s*$", "", comment).strip()
        if len(comment) > 140:
            comment = comment[:138].rstrip() + "…"
        comment_html = (
            f'<div class="pop-comment">{escape(comment)}</div>' if comment else ""
        )
        rows.append(
            f"<tr>"
            f"<td>{escape(date)}</td>"
            f"<td>{escape(course)}</td>"
            f"<td>{escape(dist)}</td>"
            f"<td>{rt_label}</td>"
            f"<td>{escape(going.title())}</td>"
            f"<td>{pos_disp}</td>"
            f"</tr>"
            + (
                f"<tr class='pop-comment-row'><td></td><td colspan='5'>{comment_html}</td></tr>"
                if comment_html
                else ""
            )
        )

    body_html = (
        f"<p><b>{escape(runner.get('name','—'))}</b> — last {len(runs)} runs</p>"
        + "<table class='pop-table pop-runs'>"
        + "<thead><tr><th>Date</th><th>Course</th><th>Dist</th><th>Type</th><th>Going</th><th>Pos</th></tr></thead>"
        + "<tbody>" + "".join(rows) + "</tbody></table>"
        + "<p class='pop-foot'>From horse_form.json runs[]</p>"
    )

    return (
        f'<button class="st mono form-pop" type="button" '
        f'data-title="Recent Form" '
        f'data-body="{escape(body_html)}">{inner}</button>'
    )


def render_form_coloured(form: str) -> str:
    if not form:
        return "—"
    out = []
    for ch in form:
        u = ch.upper()
        if ch == "1":
            cls = "f-win"
        elif ch in ("2", "3"):
            cls = "f-place"
        elif ch in ("0", "4", "5", "6", "7", "8", "9"):
            cls = "f-also"
        elif u in _FORM_FAIL:
            cls = "f-fail"
        elif ch in ("-", "/"):
            cls = "f-sep"
        else:
            cls = "f-also"
        out.append(f'<span class="{cls}">{escape(ch)}</span>')
    return "".join(out)

def fmt_days(d):
    if d in (None, ""):
        return "—"
    try:
        d = int(d)
    except (TypeError, ValueError):
        return str(d)
    if d == 0:
        return "today"
    if d == 1:
        return "1d"
    if d < 60:
        return f"{d}d"
    return f"{d//7}w"

def fmt_tips(t):
    if t in (None, "", 0):
        return ""
    return f"★{t}"

def fmt_odds_chip(frac, dec):
    if not frac and dec in (None, ""):
        return ("—", "")
    label = frac if frac else (f"{dec:.2f}" if dec else "—")
    title = f"{dec:.2f}" if dec else ""
    return (label, title)

def parse_time_minutes(t: str) -> int:
    """'1:50' -> minutes since midnight (UK afternoon racing)."""
    if not t or ":" not in t:
        return 9999
    try:
        h, m = t.split(":")
        h, m = int(h), int(m)
    except ValueError:
        return 9999
    if h < 12:
        h += 12
    return h * 60 + m

def runner_sort_key(r: dict):
    """Favourite first, missing odds last, then by tipster count desc, then name."""
    od = r.get("odds_dec")
    if od is None or od == 0:
        return (1, 9999.0, 0, r.get("name", ""))
    tips = r.get("tips") or 0
    return (0, float(od), -int(tips), r.get("name", ""))


# ---------------------------------------------------------------------------
# Rendering — runner card (mobile-first)
# ---------------------------------------------------------------------------
def render_tags(tag_list) -> str:
    if not tag_list:
        return ""
    pills = []
    for t in tag_list:
        cls = f"tag cat-{escape(t.category)} sev-{escape(t.severity)}"
        pills.append(
            f'<button class="{cls}" type="button" '
            f'data-title="{escape(t.popover_title)}" '
            f'data-body="{escape(t.popover_body_html)}">'
            f'{escape(t.label)}</button>'
        )
    return f'<div class="tag-row">{"".join(pills)}</div>'


def render_runner_card(idx: int, r: dict, tag_list=(), ctx=None) -> str:
    odds_label, odds_title = fmt_odds_chip(r.get("odds_frac"), r.get("odds_dec"))
    is_fav = idx == 0 and r.get("odds_dec")

    name_html = (
        f'<span class="rc-name">{escape(r.get("name") or "—")}</span>'
        f'<span class="rc-odds {"fav" if is_fav else ""}" title="{escape(odds_title)}">'
        f'{escape(odds_label)}</span>'
    )

    # Inline stats strip — small, dense
    stats_bits = []
    if r.get("or"):  stats_bits.append(f'<span class="st"><b>OR</b> {fmt_or(r.get("or"))}</span>')
    if r.get("rpr"): stats_bits.append(f'<span class="st"><b>RPR</b> {fmt_int(r.get("rpr"))}</span>')
    if r.get("ts"):  stats_bits.append(f'<span class="st"><b>TS</b> {fmt_int(r.get("ts"))}</span>')
    if r.get("form"):
        stats_bits.append(render_form_button(r, ctx))
    if r.get("days_since") not in (None, ""):
        stats_bits.append(f'<span class="st"><b>L</b> {fmt_days(r.get("days_since"))}</span>')
    if r.get("tips"):
        stats_bits.append(f'<span class="st tips">{fmt_tips(r.get("tips"))}</span>')
    stats_html = '<div class="rc-stats">' + " ".join(stats_bits) + '</div>' if stats_bits else ""

    conn_bits = []
    if r.get("jockey"):
        conn_bits.append(f'<span class="conn"><span class="conn-k">J</span> {escape(r["jockey"])}</span>')
    if r.get("trainer"):
        conn_bits.append(f'<span class="conn"><span class="conn-k">T</span> {escape(r["trainer"])}</span>')
    conn_html = '<div class="rc-conn">' + "".join(conn_bits) + '</div>' if conn_bits else ""

    return (
        f'<div class="runner{" fav-row" if is_fav else ""}">'
        f'<div class="rc-head">{name_html}</div>'
        f'{render_tags(tag_list)}'
        f'{stats_html}'
        f'{conn_html}'
        '</div>'
    )


def render_race(race: dict, ctx=None) -> str:
    runners = sorted(race.get("runners", []), key=runner_sort_key)
    if ctx is not None:
        cards = "\n".join(
            render_runner_card(i, r, tags.evaluate(r, race, ctx), ctx)
            for i, r in enumerate(runners)
        )
    else:
        cards = "\n".join(render_runner_card(i, r) for i, r in enumerate(runners))

    going_full = race.get("going") or "—"
    going_main = going_full.split(",", 1)[0].strip()

    badges = []
    if race.get("is_hcap"):
        badges.append('<span class="badge hcap">HCAP</span>')
    if race.get("race_class"):
        badges.append(f'<span class="badge cls">{escape(race["race_class"])}</span>')
    if race.get("prize_1st"):
        try:
            prize = int(race["prize_1st"])
            badges.append(f'<span class="badge prize">£{prize:,}</span>')
        except (TypeError, ValueError):
            pass

    course = race.get("course", "—")
    course_slug = race.get("course_slug", "")
    time = race.get("time", "")

    return (
        f'<details class="race" id="race-{escape(course_slug)}-{parse_time_minutes(time)}" data-course="{escape(course_slug)}" '
        f'data-time="{parse_time_minutes(time)}">'
        f'<summary class="race-head">'
        f'<div class="race-head-l">'
        f'<span class="race-chevron">▶</span>'
        f'<span class="race-time">{escape(time)}</span>'
        f'<span class="race-course">{escape(course)}</span>'
        f'<span class="race-dist">{escape(race.get("distance") or "—")}</span>'
        f'<span class="race-going">{escape(going_main)}</span>'
        f'<span class="race-runners-mini">· {len(runners)} runners</span>'
        f'</div>'
        f'<div class="race-head-r">{"".join(badges)}</div>'
        f'</summary>'
        f'<h3 class="race-title">{escape(race.get("race_title") or "")}'
        f' <span class="race-meta">{len(runners)} runners</span></h3>'
        f'<div class="runners">{cards}</div>'
        f'</details>'
    )


# ---------------------------------------------------------------------------
# Dashboard shell
# ---------------------------------------------------------------------------
CSS = r"""
:root {
  --bg: #0e1116;
  --bg-2: #161b22;
  --bg-3: #1f2630;
  --border: #2a313c;
  --text: #e6edf3;
  --text-2: #9aa4b1;
  --text-3: #6e7681;
  --accent: #e7b75f;
  --accent-2: #5fa8d3;
  --fav: #6ee7a4;
  --hcap: #c084fc;
  --cls: #5fa8d3;
  --prize: #e7b75f;
  /* Tag category palette */
  --cat-T: #5fa8d3; /* trainer  - cyan       */
  --cat-J: #c084fc; /* jockey   - violet     */
  --cat-H: #e7b75f; /* horse    - amber      */
  --cat-C: #f87171; /* caution  - red        */
  --cat-M: #facc15; /* market   - gold       */
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font: 14px/1.4 -apple-system, "Segoe UI", system-ui, sans-serif;
  padding: 0 0 60px;
}

/* Topbar -------------------------------------------------------- */
.topbar {
  position: sticky; top: 0; z-index: 10;
  background: var(--bg-2); border-bottom: 1px solid var(--border);
  padding: 8px 14px;
}
.topbar-row { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.topbar h1 {
  margin: 0; font-size: 17px; font-weight: 600; letter-spacing: 0.2px; flex-shrink: 0;
}
.topbar h1 .accent { color: var(--accent); }
.topbar .sub {
  color: var(--text-2); font-size: 12px;
  display: flex; gap: 10px; flex-wrap: wrap;
}
.topbar .sub b { color: var(--text); font-weight: 600; }

/* Tab bar ------------------------------------------------------- */
.tabbar {
  display: flex;
  background: var(--bg-2); border-bottom: 1px solid var(--border);
  position: sticky; top: 45px; z-index: 9;
  overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none;
}
.tabbar::-webkit-scrollbar { display: none; }
.tab-btn {
  flex-shrink: 0;
  background: transparent; color: var(--text-2);
  border: 0; border-bottom: 2px solid transparent;
  padding: 8px 14px; font: inherit; cursor: pointer; font-size: 13px;
  white-space: nowrap;
}
.tab-btn.on { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
.tab-btn .count {
  display: inline-block;
  background: var(--bg-3); color: var(--text-3);
  border-radius: 9px; padding: 0 6px; margin-left: 4px; font-size: 11px;
}
.tab-btn.on .count { background: var(--accent); color: #000; }

/* Sub-controls (course filter + expand) */
.subcontrols {
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  padding: 6px 12px; background: var(--bg-2);
  border-bottom: 1px solid var(--border);
}
.subcontrols.hidden { display: none !important; }
.expandtoggle { display: inline-flex; gap: 4px; flex-shrink: 0; }
.expandtoggle button {
  background: var(--bg-3); color: var(--text-2);
  border: 1px solid var(--border); border-radius: 6px;
  padding: 4px 8px; font: inherit; cursor: pointer; font-size: 12px;
}
.expandtoggle button:hover { color: var(--text); }
.course-row { display: flex; gap: 5px; flex-wrap: wrap; flex: 1; }
.course-btn {
  background: var(--bg-3); color: var(--text-2);
  border: 1px solid var(--border); border-radius: 6px;
  padding: 3px 8px; font: inherit; cursor: pointer; font-size: 12px;
}
.course-btn .count {
  display: inline-block; min-width: 16px; text-align: center;
  background: var(--bg); color: var(--text-3);
  border-radius: 9px; padding: 0 5px; margin-left: 3px; font-size: 11px;
}
.course-btn.on { background: var(--accent-2); color: #000; border-color: var(--accent-2); }
.course-btn.on .count { background: rgba(0,0,0,0.2); color: #000; }

/* Returning horses panel --------------------------------------- */
.returning-panel { padding: 12px; max-width: 1100px; margin: 0 auto; }
.returning-panel h2 {
  font-size: 14px; color: var(--text-2); font-weight: 500;
  margin: 0 0 10px; padding-bottom: 6px; border-bottom: 1px solid var(--border);
}
.returning-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.returning-table th {
  color: var(--text-3); font-weight: 500; font-size: 11px;
  text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border);
}
.returning-table td { padding: 8px 8px; border-bottom: 1px solid var(--border-2); vertical-align: middle; }
.returning-table tr:last-child td { border-bottom: 0; }
.returning-table tr.ret-row { cursor: pointer; }
.returning-table tr.ret-row:hover td { background: var(--bg-3); }
.ret-horse { font-weight: 600; color: var(--text); }
.ret-venue { color: var(--text-2); font-size: 12px; }
.ret-time  { color: var(--text-3); font-size: 12px; font-variant-numeric: tabular-nums; }
.ret-days  { color: var(--accent-2); font-size: 12px; font-weight: 600; }
.ret-wr    { font-variant-numeric: tabular-nums; }
.ret-wr.good { color: #4ade80; }
.ret-wr.ok   { color: var(--accent); }
.ret-odds  { color: var(--text-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.ret-empty { color: var(--text-3); font-style: italic; padding: 20px 8px; }

/* Main / meeting / race ----------------------------------------- */
main { padding: 10px 12px; max-width: 1100px; margin: 0 auto; }
.meeting-block { margin-bottom: 22px; }
.meeting-heading {
  margin: 12px 0 8px; padding-bottom: 5px;
  border-bottom: 2px solid var(--accent);
  font-size: 17px; color: var(--accent);
}
.meeting-heading .meeting-count {
  color: var(--text-3); font-size: 12px; font-weight: 400; margin-left: 8px;
}
.race {
  background: var(--bg-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 9px 11px 11px; margin-bottom: 12px;
}
.race[open] { padding-bottom: 11px; }
.race:not([open]) { padding-bottom: 9px; }
.race summary { list-style: none; cursor: pointer; user-select: none; }
.race summary::-webkit-details-marker { display: none; }
.race-head {
  display: flex; justify-content: space-between; align-items: center;
  gap: 8px; flex-wrap: wrap; margin-bottom: 2px;
}
.race-head-l { display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap; }
.race-chevron {
  display: inline-block; color: var(--text-3);
  font-size: 10px; transition: transform 0.15s; transform-origin: center;
  width: 10px;
}
.race[open] .race-chevron { transform: rotate(90deg); }
.race-time { font-weight: 700; color: var(--accent); font-size: 16px; }
.race-course { font-weight: 600; color: var(--text); }
.race-dist, .race-going { color: var(--text-2); font-size: 12px; }
.race-runners-mini { color: var(--text-3); font-size: 12px; }
.race[open] .race-runners-mini { display: none; }
.race-head-r { display: flex; gap: 4px; flex-wrap: wrap; }
.badge {
  font-size: 10.5px; padding: 1.5px 5px; border-radius: 4px;
  background: var(--bg-3); color: var(--text-2); font-weight: 600;
  letter-spacing: 0.3px;
}
.badge.hcap  { background: rgba(192,132,252,0.15); color: var(--hcap); }
.badge.cls   { background: rgba(95,168,211,0.15);  color: var(--cls); }
.badge.prize { background: rgba(231,183,95,0.15);  color: var(--prize); }
.race-title {
  margin: 3px 0 6px; font-size: 13px; color: var(--text); font-weight: 500;
}
.race-title .race-meta { color: var(--text-3); font-weight: 400; margin-left: 6px; }

/* Runner card --------------------------------------------------- */
.runners { display: flex; flex-direction: column; gap: 6px; }
.runner {
  padding: 7px 9px;
  border-left: 3px solid transparent;
  border-radius: 4px;
  background: rgba(255,255,255,0.014);
}
.runner.fav-row {
  border-left-color: var(--fav);
}
.rc-head {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 8px;
}
.rc-name {
  font-weight: 600; font-size: 14.5px; color: var(--text);
}
.rc-odds {
  flex-shrink: 0;
  font: 600 12px/1 ui-monospace, "SFMono-Regular", Menlo, monospace;
  color: var(--text-2);
  background: var(--bg-3);
  padding: 3px 7px; border-radius: 4px;
}
.rc-odds.fav { background: rgba(110,231,164,0.12); color: var(--fav); }

.rc-stats {
  display: flex; flex-wrap: wrap; gap: 10px; row-gap: 3px;
  margin-top: 3px; font-size: 12px;
  color: var(--text-2); font-variant-numeric: tabular-nums;
}
.rc-stats .st b {
  color: var(--text-3); font-weight: 500; margin-right: 2px; font-size: 11px;
}
.rc-stats .st.mono { font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; letter-spacing: 0.5px; }
.rc-stats .st.mono .f-win   { color: #6ee7a4; font-weight: 700; }
.rc-stats .st.mono .f-place { color: #e7b75f; font-weight: 600; }
.rc-stats .st.mono .f-also  { color: var(--text-3); }
.rc-stats .st.mono .f-fail  { color: #f87171; font-weight: 700; }
.rc-stats .st.mono .f-sep   { color: var(--text-3); opacity: 0.5; margin: 0 1px; }
.rc-stats .st.mono.form-pop {
  background: transparent; border: 0; padding: 0;
  font: inherit; color: inherit; cursor: pointer;
  text-decoration: underline dotted var(--text-3);
  text-underline-offset: 3px;
}
.rc-stats .st.mono.form-pop:hover { filter: brightness(1.2); }

/* Wider popover when showing the recent-form table */
#popover .pop-runs { font-size: 11.5px; }
#popover .pop-runs th, #popover .pop-runs td {
  white-space: nowrap; padding: 3px 5px;
}
#popover .pop-runs .pop-pos.win   { color: #6ee7a4; font-weight: 700; }
#popover .pop-runs .pop-pos.place { color: #e7b75f; font-weight: 600; }
#popover .pop-runs .pop-comment-row td {
  padding-top: 0; padding-bottom: 6px; white-space: normal;
}
#popover .pop-runs .pop-comment {
  color: var(--text-3); font-size: 11px; font-style: italic;
}
#popover:has(.pop-runs) {
  max-width: 540px;
}
.rc-stats .st.tips { color: var(--accent); }

.rc-conn {
  display: flex; flex-wrap: wrap; gap: 12px; row-gap: 2px;
  margin-top: 3px; font-size: 12px; color: var(--text-3);
}
.rc-conn .conn-k {
  display: inline-block; min-width: 12px;
  color: var(--text-3); font-weight: 600; opacity: 0.7;
  margin-right: 4px;
}
.rc-conn .conn { color: var(--text-2); }

/* Tags ---------------------------------------------------------- */
.tag-row {
  display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px;
}
.tag {
  display: inline-flex; align-items: center;
  font: 600 11px/1 inherit; letter-spacing: 0.2px;
  padding: 2.5px 7px; border-radius: 9px;
  border: 1px solid; cursor: pointer;
  transition: filter 0.1s;
}
.tag:hover { filter: brightness(1.2); }
.tag.cat-T { color: var(--cat-T); border-color: rgba(95,168,211,0.45); }
.tag.cat-J { color: var(--cat-J); border-color: rgba(192,132,252,0.45); }
.tag.cat-H { color: var(--cat-H); border-color: rgba(231,183,95,0.45); }
.tag.cat-C { color: var(--cat-C); border-color: rgba(248,113,113,0.45); }
.tag.cat-M { color: var(--cat-M); border-color: rgba(250,204,21,0.45); }
.tag.sev-strong.cat-T { background: rgba(95,168,211,0.15); }
.tag.sev-strong.cat-J { background: rgba(192,132,252,0.15); }
.tag.sev-strong.cat-H { background: rgba(231,183,95,0.15); }
.tag.sev-strong.cat-C { background: rgba(248,113,113,0.15); }
.tag.sev-strong.cat-M { background: rgba(250,204,21,0.15); }
.tag.sev-soft { background: transparent; }

/* Popover ------------------------------------------------------- */
#popover-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,0.55);
  z-index: 100; display: none; align-items: center; justify-content: center;
}
#popover-backdrop.show { display: flex; }
#popover-wrap { position: relative; }
#popover {
  background: var(--bg-2); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 18px;
  max-width: 360px; width: calc(100vw - 32px);
  box-shadow: 0 10px 30px rgba(0,0,0,0.5);
  position: relative;
}
#popover h4 { margin: 0 0 8px; font-size: 14px; color: var(--accent); }
#popover p  { margin: 6px 0; font-size: 13px; color: var(--text); }
#popover .pop-foot { color: var(--text-3); font-size: 11px; margin-top: 10px; }
#popover .pop-table {
  width: 100%; margin: 6px 0; border-collapse: collapse; font-size: 12px;
}
#popover .pop-table th, #popover .pop-table td {
  padding: 3px 6px; text-align: left; border-bottom: 1px solid var(--border);
  color: var(--text-2);
}
#popover .pop-table th { color: var(--text-3); font-weight: 500; font-size: 11px; }
.pop-close {
  position: absolute; top: 6px; right: 10px;
  background: transparent; border: 0; color: var(--text-3);
  font-size: 20px; cursor: pointer; line-height: 1;
}
.pop-close:hover { color: var(--text); }

.hidden { display: none !important; }

/* Mobile tweaks ------------------------------------------------- */
@media (max-width: 480px) {
  .topbar { padding: 6px 12px; }
  .topbar h1 { font-size: 15px; }
  .tabbar { top: 41px; }
  .tab-btn { padding: 7px 11px; font-size: 12px; }
  main { padding: 8px 8px; }
  .returning-table th, .returning-table td { padding: 6px 5px; font-size: 12px; }
  .race { padding: 8px 9px 9px; }
  .rc-name { font-size: 14px; }
  .rc-stats { font-size: 11.5px; gap: 8px; }
  .rc-conn { font-size: 11.5px; }
  .tag { font-size: 10.5px; padding: 2px 6px; }
  .race-title { font-size: 12.5px; }
}
"""

JS = r"""
(function(){
  const RACE_TABS = ['timeline', 'meetings'];
  const setTab = (tab) => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
    document.getElementById('view-timeline').classList.toggle('hidden', tab !== 'timeline');
    document.getElementById('view-meetings').classList.toggle('hidden', tab !== 'meetings');
    document.getElementById('view-returning').classList.toggle('hidden', tab !== 'returning');
    const sc = document.getElementById('subcontrols');
    if (sc) sc.classList.toggle('hidden', !RACE_TABS.includes(tab));
    localStorage.setItem('winsmore.tab', tab);
  };
  document.querySelectorAll('.tab-btn').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
  setTab(localStorage.getItem('winsmore.tab') || 'timeline');

  const filterCourse = (slug) => {
    document.querySelectorAll('.course-btn').forEach(b => b.classList.toggle('on', b.dataset.course === slug));
    document.querySelectorAll('#view-timeline .race').forEach(r => {
      r.classList.toggle('hidden', slug !== 'all' && r.dataset.course !== slug);
    });
    document.querySelectorAll('#view-meetings .meeting-block').forEach(m => {
      m.classList.toggle('hidden', slug !== 'all' && m.dataset.course !== slug);
    });
    localStorage.setItem('winsmore.course', slug);
  };
  document.querySelectorAll('.course-btn').forEach(b => b.addEventListener('click', () => filterCourse(b.dataset.course)));
  filterCourse(localStorage.getItem('winsmore.course') || 'all');

  const setAllOpen = (open) => {
    document.querySelectorAll('details.race').forEach(d => {
      if (open) d.setAttribute('open', ''); else d.removeAttribute('open');
    });
  };
  document.getElementById('expand-all')?.addEventListener('click', () => setAllOpen(true));
  document.getElementById('collapse-all')?.addEventListener('click', () => setAllOpen(false));

  document.querySelectorAll('.ret-row').forEach(row => {
    row.addEventListener('click', () => {
      setTab('timeline');
      const target = document.getElementById('race-' + row.dataset.raceId);
      if (target) { target.setAttribute('open',''); setTimeout(() => target.scrollIntoView({behavior:'smooth',block:'start'}), 80); }
    });
  });

  // Tag popover
  const backdrop = document.getElementById('popover-backdrop');
  const popTitle = document.getElementById('pop-title');
  const popBody  = document.getElementById('pop-body');
  const closePop = () => backdrop.classList.remove('show');
  document.addEventListener('click', (e) => {
    const tag = e.target.closest('.tag, .form-pop');
    if (tag) {
      popTitle.textContent = tag.dataset.title || '';
      popBody.innerHTML    = tag.dataset.body  || '';
      backdrop.classList.add('show');
      e.stopPropagation();
      return;
    }
    if (e.target === backdrop) closePop();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closePop();
  });
  var pc = document.querySelector('.pop-close');
  if (pc) pc.addEventListener('click', closePop);
})();
"""



def build_returning_horses_html(races: list, ctx=None) -> tuple:
    """Return (html, count) for horses 120+ days off with 2+ wins since 2024."""
    CUTOFF_DAYS = 120
    MIN_WINS    = 2
    SINCE       = "2024-01-01"
    horse_form  = getattr(ctx, "horse_form", {}) if ctx else {}
    rows = []
    for race in races:
        course_slug = race.get("course_slug", "")
        time_mins   = parse_time_minutes(race.get("time", ""))
        race_id     = f"{course_slug}-{time_mins}"
        for runner in race.get("runners", []):
            days = runner.get("days_since")
            if not isinstance(days, (int, float)) or days < CUTOFF_DAYS:
                continue
            name  = runner.get("name", "")
            key   = tags._norm_horse_name(name)
            entry = horse_form.get(key, {})
            since_runs = [r for r in entry.get("runs", []) if r.get("date", "") >= SINCE]
            n_runs = len(since_runs)
            n_wins = sum(1 for r in since_runs if r.get("won"))
            win_pct = (n_wins / n_runs * 100) if n_runs > 0 else 0.0
            if n_wins < MIN_WINS:
                continue
            rows.append({"time": race.get("time", "—"), "course": race.get("course", "—"),
                         "name": name, "days": int(days), "n_wins": n_wins, "n_runs": n_runs,
                         "win_pct": win_pct, "odds": runner.get("odds_frac", "—"), "race_id": race_id})
    rows.sort(key=lambda x: -x["win_pct"])
    if not rows:
        return ("<div class='returning-panel'>"
                "<h2>Returning Horses — 120+ Day Absence, 2+ Wins Since 2024</h2>"
                "<p class='ret-empty'>No horses match today.</p></div>", 0)
    thead = ("<thead><tr><th>Time</th><th>Course</th><th>Horse</th>"
             "<th>Days off</th><th>W/R since 2024</th><th>Odds</th></tr></thead>")
    trows = []
    for r in rows:
        wr_cls = "good" if r["win_pct"] >= 33 else "ok"
        trows.append(
            f"<tr class='ret-row' data-race-id='{escape(r['race_id'])}'>"
            f"<td class='ret-time'>{escape(r['time'])}</td>"
            f"<td class='ret-venue'>{escape(r['course'])}</td>"
            f"<td class='ret-horse'>{escape(r['name'])}</td>"
            f"<td class='ret-days'>{r['days']}d off</td>"
            f"<td class='ret-wr {wr_cls}'>{r['n_wins']}/{r['n_runs']} ({r['win_pct']:.0f}%)</td>"
            f"<td class='ret-odds'>{escape(str(r['odds']))}</td></tr>"
        )
    html = ("<div class='returning-panel'>"
            "<h2>Returning Horses — 120+ Day Absence, 2+ Wins Since 2024</h2>"
            f"<table class='returning-table'>{thead}<tbody>{''.join(trows)}</tbody></table></div>")
    return html, len(rows)


def render_dashboard(card: dict, ctx=None) -> str:
    races = sorted(card.get("races", []), key=lambda r: parse_time_minutes(r.get("time", "")))

    meetings = {}
    for r in races:
        c = r.get("course", "—")
        if c not in meetings:
            meetings[c] = {"slug": r.get("course_slug", c.lower()), "races": []}
        meetings[c]["races"].append(r)

    course_buttons = "".join(
        f'<button class="course-btn" data-course="{escape(info["slug"])}">'
        f'{escape(course)} <span class="count">{len(info["races"])}</span></button>'
        for course, info in meetings.items()
    )

    timeline_html = "\n".join(render_race(r, ctx) for r in races)

    meeting_blocks = []
    for course, info in meetings.items():
        block_races = sorted(info["races"], key=lambda r: parse_time_minutes(r.get("time", "")))
        race_html = "\n".join(render_race(r, ctx) for r in block_races)
        meeting_blocks.append(
            f'<div class="meeting-block" data-course="{escape(info["slug"])}">'
            f'<h2 class="meeting-heading">{escape(course)} '
            f'<span class="meeting-count">{len(block_races)} races</span></h2>'
            f'{race_html}</div>'
        )
    meetings_html = "\n".join(meeting_blocks)

    date_str = card.get("date", datetime.today().strftime("%Y-%m-%d"))
    try:
        date_pretty = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A %d %B %Y")
    except ValueError:
        date_pretty = date_str

    total_races = len(races)
    total_runners = sum(len(r.get("runners", [])) for r in races)
    returning_html, n_returning = build_returning_horses_html(races, ctx)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Winsmore — {escape(date_pretty)}</title>
<style>{CSS}</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-row">
    <h1><span class="accent">Winsmore</span> — Daily Racing</h1>
    <div class="sub">
      <span><b>{escape(date_pretty)}</b></span>
      <span><b>{total_races}</b> races</span>
      <span><b>{total_runners}</b> runners</span>
      <span><b>{len(meetings)}</b> meetings</span>
    </div>
  </div>
</header>

<nav class="tabbar">
  <button class="tab-btn" data-tab="timeline">By time</button>
  <button class="tab-btn" data-tab="meetings">By meeting</button>
  <button class="tab-btn" data-tab="returning">Returning Horses <span class="count">{n_returning}</span></button>
</nav>

<div class="subcontrols" id="subcontrols">
  <div class="course-row">
    <button class="course-btn" data-course="all">All <span class="count">{total_races}</span></button>
    {course_buttons}
  </div>
  <div class="expandtoggle">
    <button id="expand-all"   title="Open all races">▼ all</button>
    <button id="collapse-all" title="Close all races">▲ all</button>
  </div>
</div>

<main>
  <div id="view-timeline">{timeline_html}</div>
  <div id="view-meetings">{meetings_html}</div>
  <div id="view-returning">{returning_html}</div>
</main>

<div id="popover-backdrop" role="dialog" aria-modal="true">
  <div id="popover-wrap">
    <div id="popover">
      <button class="pop-close" aria-label="Close">×</button>
      <h4 id="pop-title"></h4>
      <div id="pop-body"></div>
    </div>
  </div>
</div>

<script>{JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CARD
    if not src.exists():
        print(f"ERROR: card file not found: {src}", file=sys.stderr)
        sys.exit(1)

    card = load_card(src)

    ctx = tags.TagContext.load(BETFAIR_DIR, card) if BETFAIR_DIR.exists() else None
    if ctx is None:
        print(f"  [tags] {BETFAIR_DIR} not found — building without tags")

    html = render_dashboard(card, ctx)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    (SCRIPT_DIR / "index.html").write_text(html, encoding="utf-8")

    n_races = len(card.get("races", []))
    n_runners = sum(len(r.get("runners", [])) for r in card.get("races", []))
    n_tags = 0
    if ctx is not None:
        for race in card.get("races", []):
            for runner in race.get("runners", []):
                n_tags += len(tags.evaluate(runner, race, ctx))

    print(f"Wrote {OUTPUT_HTML} (and index.html)")
    print(f"  date={card.get('date')}  races={n_races}  runners={n_runners}  tags={n_tags}")


if __name__ == "__main__":
    main()                                   