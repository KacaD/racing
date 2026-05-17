"""
Winsmore — runner tag engine.

Each tag rule is a pure function: given a runner dict, the parent race
dict, and a TagContext (with all the supporting data loaded once),
return a Tag (or None if the rule doesn't fire).

Categories drive colour:
  T = trainer-derived       (cyan)
  J = jockey-derived        (violet)
  H = horse form / ratings  (amber/orange)
  C = caution / warning     (red)
  M = market / odds signal  (gold)

Severity drives intensity: 'strong' = filled background, 'soft' = outline.
See TAG_PLAN.md for the full catalogue and roll-out order.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
import string
from collections import defaultdict
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Callable, Optional

CATEGORY_CHOICES = ("T", "J", "H", "C", "M")
SEVERITY_CHOICES = ("strong", "soft")


@dataclass
class Tag:
    slug: str
    label: str
    category: str
    severity: str = "strong"
    popover_title: str = ""
    popover_body_html: str = ""

    def __post_init__(self):
        if self.category not in CATEGORY_CHOICES:
            raise ValueError(f"bad category {self.category!r}")
        if self.severity not in SEVERITY_CHOICES:
            raise ValueError(f"bad severity {self.severity!r}")


@dataclass
class TagContext:
    betfair_dir: Path
    trainer_stats: dict = field(default_factory=dict)
    jockey_stats: dict = field(default_factory=dict)
    horse_form: dict = field(default_factory=dict)
    # global aggregates (filled by load, after horse_form is in)
    tj_pair_runs: dict = field(default_factory=dict)        # (trainer, jockey) -> runs
    trainer_total_runs: dict = field(default_factory=dict)  # trainer -> total runs in horse_form
    # per-card aggregates (filled by attach_card)
    trainer_at_course_today: dict = field(default_factory=dict)
    jockey_at_course_today: dict = field(default_factory=dict)
    trainer_runners_today: dict = field(default_factory=dict)
    jockey_runners_today: dict = field(default_factory=dict)

    @classmethod
    def load(cls, betfair_dir: Path, card: Optional[dict] = None) -> "TagContext":
        ctx = cls(betfair_dir=Path(betfair_dir))
        ctx.trainer_stats = _load_json(ctx.betfair_dir / "trainer_stats.json", default={})
        ctx.jockey_stats  = _load_json(ctx.betfair_dir / "jockey_stats.json",  default={})
        ctx.horse_form    = _load_json(ctx.betfair_dir / "horse_form.json",    default={})
        # Merge incremental patch if present (written by daily build_ratings run)
        patch = _load_json(ctx.betfair_dir / "horse_form_patch.json", default={})
        if patch:
            ctx.horse_form.update(patch)
        ctx._build_global_aggregates()
        if card is not None:
            ctx.attach_card(card)
        return ctx

    def _build_global_aggregates(self) -> None:
        """One pass over horse_form to count T/J pairs and trainer totals."""
        from collections import defaultdict as _dd
        tj: dict = _dd(int)
        tt: dict = _dd(int)
        for v in self.horse_form.values():
            if not isinstance(v, dict):
                continue
            for run in v.get("runs", []):
                t, j = run.get("trainer"), run.get("jockey")
                if t and j:
                    tj[(t, j)] += 1
                if t:
                    tt[t] += 1
        self.tj_pair_runs = dict(tj)
        self.trainer_total_runs = dict(tt)

    def attach_card(self, card: dict) -> None:
        t_count: dict = defaultdict(int)
        j_count: dict = defaultdict(int)
        t_runners: dict = defaultdict(list)
        j_runners: dict = defaultdict(list)
        for race in card.get("races", []):
            slug   = race.get("course_slug", "")
            course = race.get("course", slug)
            time   = race.get("time", "")
            for r in race.get("runners", []):
                if r.get("trainer"):
                    t_count[(r["trainer"], slug)] += 1
                    t_runners[r["trainer"]].append(
                        (time, course, slug, r.get("name", "—"),
                         r.get("odds_frac"), r.get("odds_dec"))
                    )
                if r.get("jockey"):
                    j_count[(r["jockey"], slug)] += 1
                    j_runners[r["jockey"]].append(
                        (time, course, slug, r.get("name", "—"),
                         r.get("odds_frac"), r.get("odds_dec"))
                    )
        self.trainer_at_course_today = dict(t_count)
        self.jockey_at_course_today  = dict(j_count)
        for k in t_runners:
            t_runners[k].sort(key=lambda x: x[0])
        for k in j_runners:
            j_runners[k].sort(key=lambda x: x[0])
        self.trainer_runners_today = dict(t_runners)
        self.jockey_runners_today  = dict(j_runners)


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------
_NORM_HORSE_TR = str.maketrans("", "", string.punctuation + " ")
def _norm_horse_name(name: str) -> str:
    """Match horse_form.json key format: lowercase, strip punctuation AND spaces."""
    if not name:
        return ""
    return name.lower().translate(_NORM_HORSE_TR).strip()


_GOING_PREFIXES = ("TAPETA:", "POLYTRACK:", "AW:", "ALL-WEATHER:", "TURF:")
def _norm_going(going_str: str) -> str:
    """todays_card 'GOOD, Good to firm in places' -> 'good'.
    Strips AW prefix, takes pre-comma part, lowercases, spaces -> underscores.
    """
    if not going_str:
        return ""
    s = going_str.strip()
    for pfx in _GOING_PREFIXES:
        if s.upper().startswith(pfx):
            s = s[len(pfx):].strip()
            break
    s = s.split(",", 1)[0].strip()
    return s.lower().replace(" ", "_")


def _dist_token(s: str) -> Optional[str]:
    """Extract leading miles+furlongs token: '7f', '1m', '1m2f', '(7f14yds)' -> '7f'."""
    if not s:
        return None
    m = re.match(r"\(?(?:(\d+)m)?(?:(\d+)f)?", s.strip())
    if not m:
        return None
    miles, furlongs = m.group(1), m.group(2)
    if miles is None and furlongs is None:
        return None
    out = ""
    if miles:    out += f"{miles}m"
    if furlongs: out += f"{furlongs}f"
    return out or None




# ---------------------------------------------------------------------------
# Race-type inference (Flat vs NH)
# ---------------------------------------------------------------------------
# Today's race type is determined from race_title — most reliable signal.
# Past runs in horse_form.json don't carry an explicit race_type field, so we
# infer from course (-aw suffix => Flat AW) plus distance (>= 2m => likely NH).
_NH_KEYWORDS = ("HURDLE", "CHASE", "STEEPLECHASE", "NH FLAT", "NHF", "BUMPER")

def _infer_race_type(race: dict) -> str:
    """Today's race: 'nh' (hurdle/chase/bumper) or 'flat'."""
    title = (race.get("race_title") or "").upper()
    for kw in _NH_KEYWORDS:
        if kw in title:
            return "nh"
    return "flat"


def _run_race_type(run: dict) -> str:
    """Past run: 'nh' or 'flat' from course suffix + distance prefix."""
    course = (run.get("course") or "").lower()
    if course.endswith("-aw"):
        return "flat"
    dist = run.get("dist") or ""
    m = re.match(r"\(?(\d+)m", dist.strip())
    if m and int(m.group(1)) >= 2:
        return "nh"
    return "flat"


def _filter_runs_to_today(runs: list, today_type: str) -> list:
    """Return only runs matching today's race type."""
    return [r for r in runs if _run_race_type(r) == today_type]


# ---------------------------------------------------------------------------
# Helpers shared by trainer/jockey rules
# ---------------------------------------------------------------------------
def _course_slice(stats_for_person: dict, course_slug: str, is_hcap: bool) -> Optional[dict]:
    if not stats_for_person:
        return None
    pref_key = "by_course_hcap" if is_hcap else "by_course_cond"
    slice_pref = stats_for_person.get(pref_key, {}).get(course_slug)
    if slice_pref and slice_pref.get("runs", 0) > 0:
        return slice_pref
    overall = stats_for_person.get("by_course", {}).get(course_slug)
    if overall and overall.get("runs", 0) > 0:
        return overall
    return None


def _course_breakdown_html(stats_for_person: dict, course_slug: str) -> str:
    rows = []
    for label, key in (
        ("Handicaps", "by_course_hcap"),
        ("Conditions", "by_course_cond"),
        ("All races", "by_course"),
    ):
        slice_ = stats_for_person.get(key, {}).get(course_slug)
        if not slice_ or slice_.get("runs", 0) == 0:
            continue
        rows.append(
            f"<tr><td>{label}</td>"
            f"<td>{slice_['wins']}/{slice_['runs']}</td>"
            f"<td>{slice_['win_pct']:.0f}%</td></tr>"
        )
    if not rows:
        return ""
    return (
        "<table class='pop-table'>"
        "<thead><tr><th>Slice</th><th>W/R</th><th>SR</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )




def _fmt_date(d: str) -> str:
    """Convert YYYY-MM-DD to D/M/YY for display."""
    if not d or d == "—":
        return d
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return f"{dt.day}/{dt.month}/{str(dt.year)[2:]}"
    except ValueError:
        return d


def _today_runners_html(runners_list: list) -> str:
    """Render today's runners section for a trainer/jockey popover.
    runners_list: list of (time, course, slug, name, odds_frac, odds_dec)
    """
    n = len(runners_list)
    courses_seen = list(dict.fromkeys(course for _, course, _, _, _, _ in runners_list))
    n_courses = len(courses_seen)

    alert = ""
    if n_courses > 1:
        alert = f"<p class=\'pop-alert\'>⚡ Active at {n_courses} courses today</p>"

    rows = []
    for time, course, slug, name, odds_frac, odds_dec in runners_list:
        odds_str = odds_frac or (f"{float(odds_dec):.0f}" if odds_dec else "—")
        course_badge = (
            f'<span class="pop-badge">{escape(course)}</span> '
            if n_courses > 1 else ""
        )
        rows.append(
            f"<tr>"
            f"<td class=\'pop-time\'>{escape(time)}</td>"
            f"<td>{course_badge}{escape(name)}</td>"
            f"<td class=\'pop-odds-cell\'><b>{escape(str(odds_str))}</b></td>"
            f"</tr>"
        )

    return (
        f"<p><b>{n} runner{'s' if n != 1 else ''} today</b></p>"
        + alert
        + "<table class=\'pop-table pop-runners-today\'>"
        + "<tbody>" + "".join(rows) + "</tbody>"
        + "</table>"
    )


def _courses_record_html(stats: dict, runners_list: list) -> str:
    """Course record for each unique course in runners_list.
    Shows overall record plus hcap/conditions breakdown where available."""
    seen = []
    seen_slugs: set = set()
    for _, course, slug, _, _, _ in runners_list:
        if slug not in seen_slugs:
            seen.append((slug, course))
            seen_slugs.add(slug)
    rows = []
    for slug, name in seen:
        overall = stats.get("by_course", {}).get(slug)
        if not overall or overall.get("runs", 0) == 0:
            continue
        r, w, pct = overall["runs"], overall["wins"], overall["win_pct"]
        rows.append(
            f"<tr><td>{escape(name)}</td>"
            f"<td style='color:var(--text-2);font-size:11px'>Overall</td>"
            f"<td>{w}W / {r}R ({pct:.0f}%)</td></tr>"
        )
        hcap = stats.get("by_course_hcap", {}).get(slug)
        cond = stats.get("by_course_cond", {}).get(slug)
        if hcap and hcap.get("runs", 0) > 0:
            rh, wh, ph = hcap["runs"], hcap["wins"], hcap["win_pct"]
            rows.append(
                f"<tr><td></td>"
                f"<td style='color:var(--text-3);font-size:11px;padding-left:10px'>Hcap</td>"
                f"<td style='color:var(--text-2);font-size:11px'>{wh}W / {rh}R ({ph:.0f}%)</td></tr>"
            )
        if cond and cond.get("runs", 0) > 0:
            rc, wc, pc = cond["runs"], cond["wins"], cond["win_pct"]
            rows.append(
                f"<tr><td></td>"
                f"<td style='color:var(--text-3);font-size:11px;padding-left:10px'>Cond</td>"
                f"<td style='color:var(--text-2);font-size:11px'>{wc}W / {rc}R ({pc:.0f}%)</td></tr>"
            )
    if not rows:
        return ""
    return (
        "<p class=\'pop-section-label\'>COURSE RECORD</p>"
        "<table class=\'pop-table\'><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _overall_form_html(stats: dict) -> str:
    """All-time / last14 / last28 form rows."""
    rows = []
    for label, key in (("All-time", "all"), ("Last 14 days", "last14"), ("Last 28 days", "last28")):
        s = stats.get(key)
        if not s or s.get("runs", 0) == 0:
            continue
        r, w, pct = s["runs"], s["wins"], s["win_pct"]
        rows.append(f"<tr><td>{label}</td><td>{w}W / {r}R ({pct:.0f}%)</td></tr>")
    if not rows:
        return ""
    return (
        "<p class=\'pop-section-label\'>FORM</p>"
        "<table class=\'pop-table\'><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )

# ---------------------------------------------------------------------------
# Horse form helpers
# ---------------------------------------------------------------------------
def _horse_runs_at_course(runs: list, course_slug: str) -> tuple[int, int, list]:
    """Return (runs, wins, list_of_matching_runs)."""
    matches = [r for r in runs if r.get("course") == course_slug]
    return len(matches), sum(1 for r in matches if r.get("won")), matches


def _horse_runs_at_distance(runs: list, dist_token: str) -> tuple[int, int, list]:
    matches = [r for r in runs if _dist_token(r.get("dist", "")) == dist_token]
    return len(matches), sum(1 for r in matches if r.get("won")), matches


def _horse_runs_on_going(runs: list, going: str) -> tuple[int, int, list]:
    matches = [r for r in runs if r.get("going") == going]
    return len(matches), sum(1 for r in matches if r.get("won")), matches


def _last_runs_table(matches: list, n: int = 3) -> str:
    """Tiny table of the n most recent runs to put inside a popover."""
    if not matches:
        return ""
    matches = sorted(matches, key=lambda r: r.get("date", ""), reverse=True)[:n]
    rows = []
    for r in matches:
        pos = r.get("pos", "?")
        runners = r.get("runners", "?")
        date = _fmt_date(r.get("date", "—"))
        course = r.get("course", "—")
        beaten = r.get("beaten", "")
        beaten_str = f" ({beaten}L)" if beaten and r.get("pos") not in (1, "1") else ""
        won = "★" if r.get("won") else ""
        rows.append(
            f"<tr><td>{escape(date)}</td><td>{escape(course)}</td>"
            f"<td>{won}{pos}/{runners}{beaten_str}</td></tr>"
        )
    return (
        "<table class='pop-table'>"
        "<thead><tr><th>Date</th><th>Course</th><th>Pos</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


# ---------------------------------------------------------------------------
# Tag rules
# ---------------------------------------------------------------------------
def tag_trainer_course_specialist(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    trainer = runner.get("trainer")
    course_slug = race.get("course_slug")
    if not trainer or not course_slug:
        return None
    stats = ctx.trainer_stats.get(trainer)
    if not stats:
        return None
    slice_ = _course_slice(stats, course_slug, race.get("is_hcap", False))
    if not slice_:
        return None
    runs, wins, win_pct = slice_["runs"], slice_["wins"], slice_["win_pct"]
    if runs < 8 or win_pct < 20:
        return None

    course = race.get("course", course_slug)
    runners_today = ctx.trainer_runners_today.get(trainer, [])
    body = (
        _today_runners_html(runners_today)
        + _courses_record_html(stats, runners_today)
        + _overall_form_html(stats)
        + "<p class='pop-foot'>From trainer_stats.json</p>"
    )
    return Tag(
        slug="trainer-course-specialist",
        label=f"T {win_pct:.0f}%",
        category="T",
        severity="strong" if win_pct >= 25 else "soft",
        popover_title="Trainer Course Specialist",
        popover_body_html=body,
    )


def tag_jockey_course_specialist(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    jockey = runner.get("jockey")
    course_slug = race.get("course_slug")
    if not jockey or not course_slug:
        return None
    stats = ctx.jockey_stats.get(jockey)
    if not stats:
        return None
    slice_ = _course_slice(stats, course_slug, race.get("is_hcap", False))
    if not slice_:
        return None
    runs, wins, win_pct = slice_["runs"], slice_["wins"], slice_["win_pct"]
    if runs < 10 or win_pct < 20:
        return None

    course = race.get("course", course_slug)
    runners_today = ctx.jockey_runners_today.get(jockey, [])
    body = (
        _today_runners_html(runners_today)
        + _courses_record_html(stats, runners_today)
        + _overall_form_html(stats)
        + "<p class='pop-foot'>From jockey_stats.json</p>"
    )
    return Tag(
        slug="jockey-course-specialist",
        label=f"J {win_pct:.0f}%",
        category="J",
        severity="strong" if win_pct >= 25 else "soft",
        popover_title="Jockey Course Specialist",
        popover_body_html=body,
    )


def tag_trainer_sole_booking(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    trainer = runner.get("trainer")
    course_slug = race.get("course_slug")
    if not trainer or not course_slug:
        return None
    n_today = ctx.trainer_at_course_today.get((trainer, course_slug), 0)
    if n_today != 1:
        return None
    stats = ctx.trainer_stats.get(trainer)
    if not stats:
        return None
    slice_ = _course_slice(stats, course_slug, race.get("is_hcap", False))
    if not slice_:
        return None
    runs, wins, win_pct = slice_["runs"], slice_["wins"], slice_["win_pct"]
    if runs < 5 or win_pct < 18:
        return None

    course = race.get("course", course_slug)
    runners_today = ctx.trainer_runners_today.get(trainer, [])
    body = (
        _today_runners_html(runners_today)
        + _courses_record_html(stats, runners_today)
        + _overall_form_html(stats)
        + "<p class='pop-foot'>Sole-booking: trainer ships only this horse to the meeting.</p>"
    )
    return Tag(
        slug="trainer-sole-booking",
        label=f"T sole {win_pct:.0f}%",
        category="T",
        severity="strong" if win_pct >= 25 else "soft",
        popover_title="Trainer Sole Booking",
        popover_body_html=body,
    )


# ---------------------------------------------------------------------------
# Horse form tags
# ---------------------------------------------------------------------------
def tag_horse_course_winner(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """Horse has won at this course before."""
    if not ctx.horse_form:
        return None
    key = _norm_horse_name(runner.get("name", ""))
    entry = ctx.horse_form.get(key)
    if not entry:
        return None
    course_slug = race.get("course_slug")
    if not course_slug:
        return None
    today_type = _infer_race_type(race)
    type_runs = _filter_runs_to_today(entry.get("runs", []), today_type)
    runs, wins, matches = _horse_runs_at_course(type_runs, course_slug)
    if wins < 1:
        return None

    course = race.get("course", course_slug)
    type_label = "hurdle/chase" if today_type == "nh" else "Flat"
    body = (
        f"<p><b>{escape(runner.get('name','—'))}</b> at <b>{escape(course)}</b> "
        f"({type_label}): <b>{wins}/{runs}</b> "
        f"({100*wins/runs:.0f}% strike rate).</p>"
        + _last_runs_table(matches, n=4)
        + "<p class='pop-foot'>Filtered to today's race type. From horse_form.json</p>"
    )
    return Tag(
        slug="horse-course-winner",
        label=f"C {wins}/{runs}",
        category="H",
        severity="strong" if wins >= 2 else "soft",
        popover_title="Course Winner",
        popover_body_html=body,
    )


def tag_horse_distance_winner(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """Horse has won at this exact distance before (e.g. 7f)."""
    if not ctx.horse_form:
        return None
    key = _norm_horse_name(runner.get("name", ""))
    entry = ctx.horse_form.get(key)
    if not entry:
        return None
    dist_tok = _dist_token(race.get("distance", ""))
    if not dist_tok:
        return None
    today_type = _infer_race_type(race)
    type_runs = _filter_runs_to_today(entry.get("runs", []), today_type)
    runs, wins, matches = _horse_runs_at_distance(type_runs, dist_tok)
    if wins < 1:
        return None

    type_label = "hurdle/chase" if today_type == "nh" else "Flat"
    body = (
        f"<p><b>{escape(runner.get('name','—'))}</b> at <b>{dist_tok}</b> "
        f"({type_label}): <b>{wins}/{runs}</b> "
        f"({100*wins/runs:.0f}% strike rate).</p>"
        + _last_runs_table(matches, n=4)
        + "<p class='pop-foot'>Filtered to today's race type. From horse_form.json</p>"
    )
    return Tag(
        slug="horse-distance-winner",
        label=f"D {wins}/{runs}",
        category="H",
        severity="strong" if wins >= 2 else "soft",
        popover_title=f"Winner at {dist_tok}",
        popover_body_html=body,
    )


def tag_horse_going_winner(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """Horse has won on this going before (need >= 2 wins for a meaningful signal)."""
    if not ctx.horse_form:
        return None
    key = _norm_horse_name(runner.get("name", ""))
    entry = ctx.horse_form.get(key)
    if not entry:
        return None
    going = _norm_going(race.get("going", ""))
    if not going or going == "unknown":
        return None
    today_type = _infer_race_type(race)
    type_runs = _filter_runs_to_today(entry.get("runs", []), today_type)
    runs, wins, matches = _horse_runs_on_going(type_runs, going)
    if wins < 2:
        return None

    type_label = "hurdle/chase" if today_type == "nh" else "Flat"
    going_pretty = going.replace("_", " ").title()
    body = (
        f"<p><b>{escape(runner.get('name','—'))}</b> on <b>{escape(going_pretty)}</b> "
        f"({type_label}): <b>{wins}/{runs}</b> "
        f"({100*wins/runs:.0f}% strike rate).</p>"
        + _last_runs_table(matches, n=4)
        + "<p class='pop-foot'>Filtered to today's race type. From horse_form.json</p>"
    )
    return Tag(
        slug="horse-going-winner",
        label=f"G {wins}/{runs}",
        category="H",
        severity="strong" if wins >= 3 else "soft",
        popover_title=f"Winner on {going_pretty}",
        popover_body_html=body,
    )



def tag_horse_improving(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """Horse's RPR trend is positive in same-type runs only.

    We recompute the trend from the runs that match today's race type
    (Flat / NH) so a hurdle horse's Flat RPRs don't artificially lift its
    'improving' tag, and vice-versa.
    """
    if not ctx.horse_form:
        return None
    key = _norm_horse_name(runner.get("name", ""))
    entry = ctx.horse_form.get(key)
    if not entry:
        return None

    today_type = _infer_race_type(race)
    type_runs = _filter_runs_to_today(entry.get("runs", []), today_type)
    rprs = [r.get("rpr") for r in type_runs
            if isinstance(r.get("rpr"), (int, float)) and r.get("rpr") > 30]
    if len(rprs) < 4:
        # need a few same-type figures for a meaningful trend
        return None

    last3 = rprs[-3:]
    prev3 = rprs[-6:-3] if len(rprs) >= 6 else rprs[:-3]
    if not prev3:
        return None
    delta = (sum(last3) / len(last3)) - (sum(prev3) / len(prev3))
    if delta < 5:
        return None

    type_label = "hurdle/chase" if today_type == "nh" else "Flat"
    spark = " ".join(str(int(x)) for x in rprs[-6:])
    body = (
        f"<p><b>{escape(runner.get('name','—'))}</b> RPR trend ({type_label}): "
        f"<b>+{delta:.1f}</b> (last 3 vs previous 3 same-type runs).</p>"
        + f"<p>Last {len(rprs[-6:])} {type_label} RPR: <span class='mono'>{escape(spark)}</span></p>"
        + "<p class='pop-foot'>Filtered to today's race type. From horse_form.json runs[]</p>"
    )
    return Tag(
        slug="horse-improving",
        label=f"↑ {delta:.0f}",
        category="H",
        severity="strong" if delta >= 10 else "soft",
        popover_title="Improving",
        popover_body_html=body,
    )


def tag_horse_career_best(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """Last same-type run produced the horse's highest RPR for that type.

    Compares the most recent run of today's race type against the horse's
    other runs of the same type — so a flat-track personal best doesn't
    fire when today is a hurdle.
    """
    if not ctx.horse_form:
        return None
    key = _norm_horse_name(runner.get("name", ""))
    entry = ctx.horse_form.get(key)
    if not entry:
        return None

    today_type = _infer_race_type(race)
    type_runs = _filter_runs_to_today(entry.get("runs", []), today_type)
    rprs = [r.get("rpr") for r in type_runs
            if isinstance(r.get("rpr"), (int, float)) and r.get("rpr") > 30]
    if len(rprs) < 3:
        return None
    if rprs[-1] != max(rprs):
        return None
    last_rpr = rprs[-1]
    prev_best = max(rprs[:-1])
    delta = last_rpr - prev_best

    type_label = "hurdle/chase" if today_type == "nh" else "Flat"
    spark = " ".join(str(int(x)) for x in rprs[-6:])
    body = (
        f"<p><b>{escape(runner.get('name','—'))}</b> recorded a {type_label} "
        f"career-best RPR of <b>{int(last_rpr)}</b> last time out"
        + (f", improving on previous {type_label} best by <b>{int(delta)} lb</b>." if delta > 0 else ".")
        + "</p>"
        + f"<p>Last {len(rprs[-6:])} {type_label} RPR: <span class='mono'>{escape(spark)}</span></p>"
        + "<p class='pop-foot'>Filtered to today's race type. From horse_form.json runs[]</p>"
    )
    return Tag(
        slug="horse-career-best",
        label="PB",
        category="H",
        severity="strong",
        popover_title="Career Best Last Time",
        popover_body_html=body,
    )


def tag_horse_returning_from_break(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """Days since last run >= 60: horse is returning from a break."""
    days = runner.get("days_since")
    if not isinstance(days, (int, float)) or days < 60:
        return None

    if days >= 365:
        label_days = f"{days // 365}y off"
    elif days >= 60:
        label_days = f"{int(days)}d off"
    else:
        return None

    body = (
        f"<p><b>{escape(runner.get('name','—'))}</b> has not raced for "
        f"<b>{int(days)} days</b>.</p>"
        f"<p class='pop-foot'>Days-since signal from todays_card.json.</p>"
    )
    return Tag(
        slug="horse-returning-from-break",
        label=label_days,
        category="H",
        severity="soft",
        popover_title="Returning From a Break",
        popover_body_html=body,
    )



# ---------------------------------------------------------------------------
# Caution tags (category C, red)
# ---------------------------------------------------------------------------
def tag_horse_winless_at_course(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """3+ runs at this course, never won."""
    if not ctx.horse_form:
        return None
    key = _norm_horse_name(runner.get("name", ""))
    entry = ctx.horse_form.get(key)
    if not entry:
        return None
    course_slug = race.get("course_slug")
    if not course_slug:
        return None
    today_type = _infer_race_type(race)
    type_runs = _filter_runs_to_today(entry.get("runs", []), today_type)
    runs, wins, matches = _horse_runs_at_course(type_runs, course_slug)
    if runs < 3 or wins > 0:
        return None

    course = race.get("course", course_slug)
    type_label = "hurdle/chase" if today_type == "nh" else "Flat"
    body = (
        f"<p><b>{escape(runner.get('name','—'))}</b> at <b>{escape(course)}</b> "
        f"({type_label}): <b>0/{runs}</b> — has not won here in {runs} starts.</p>"
        + _last_runs_table(matches, n=4)
        + "<p class='pop-foot'>Filtered to today's race type. From horse_form.json</p>"
    )
    return Tag(
        slug="horse-winless-at-course",
        label=f"C 0/{runs}",
        category="C",
        severity="strong" if runs >= 5 else "soft",
        popover_title="Winless At Course",
        popover_body_html=body,
    )


def tag_jockey_winless_on_horse(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """This jockey has ridden this horse 3+ times with 0 wins."""
    if not ctx.horse_form:
        return None
    jockey = runner.get("jockey")
    if not jockey:
        return None
    key = _norm_horse_name(runner.get("name", ""))
    entry = ctx.horse_form.get(key)
    if not entry:
        return None
    jform = (entry.get("jockey_form") or {}).get(jockey)
    if not jform:
        return None
    runs = jform.get("runs", 0)
    wins = jform.get("wins", 0)
    if runs < 3 or wins > 0:
        return None

    body = (
        f"<p><b>{escape(jockey)}</b> on <b>{escape(runner.get('name','—'))}</b>: "
        f"<b>0/{runs}</b> — partnership has not yet clicked.</p>"
        f"<p class='pop-foot'>jockey_form per horse, from horse_form.json</p>"
    )
    return Tag(
        slug="jockey-winless-on-horse",
        label=f"J 0/{runs}",
        category="C",
        severity="strong" if runs >= 4 else "soft",
        popover_title="Jockey Winless On Horse",
        popover_body_html=body,
    )


def tag_heavy_penalty(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """OR has been raised >= 6 lb since the horse's last recorded OR."""
    cur_or = runner.get("or")
    if not isinstance(cur_or, (int, float)) or cur_or <= 0:
        return None
    if not ctx.horse_form:
        return None
    key = _norm_horse_name(runner.get("name", ""))
    entry = ctx.horse_form.get(key)
    if not entry:
        return None
    last_or = entry.get("last_or")
    if not isinstance(last_or, (int, float)) or last_or <= 0:
        return None
    delta = int(cur_or) - int(last_or)
    if delta < 6:
        return None

    body = (
        f"<p><b>{escape(runner.get('name','—'))}</b> raised "
        f"<b>{int(last_or)} → {int(cur_or)}</b> "
        f"(<b>+{delta} lb</b>) since last run.</p>"
        f"<p class='pop-foot'>last_or from horse_form.json vs OR in todays_card.json</p>"
    )
    return Tag(
        slug="horse-heavy-penalty",
        label=f"+{delta} lb",
        category="C",
        severity="strong" if delta >= 8 else "soft",
        popover_title="Heavy Penalty",
        popover_body_html=body,
    )



# ---------------------------------------------------------------------------
# Connection tags (jockey x horse)
# ---------------------------------------------------------------------------
def tag_jockey_first_on_horse(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """Jockey has not ridden this horse in any recorded run.

    Only fires for horses that have prior runs on file (i.e. the horse
    isn't a debutant) and whose jockey list doesn't include today's rider.
    """
    if not ctx.horse_form:
        return None
    jockey = runner.get("jockey")
    if not jockey:
        return None
    key = _norm_horse_name(runner.get("name", ""))
    entry = ctx.horse_form.get(key)
    if not entry:
        return None
    if (entry.get("total_runs") or 0) < 6:
        return None
    jform = entry.get("jockey_form") or {}
    if jockey in jform:
        return None  # not first time

    # Build a small list of past jockeys for the popover
    rows = []
    for jname, info in sorted(jform.items(), key=lambda kv: -kv[1].get("runs", 0))[:4]:
        rows.append(
            f"<tr><td>{escape(jname)}</td>"
            f"<td>{info.get('wins',0)}/{info.get('runs',0)}</td></tr>"
        )
    table = ""
    if rows:
        table = ("<table class='pop-table'>"
                 "<thead><tr><th>Past jockey</th><th>W/R</th></tr></thead>"
                 "<tbody>" + "".join(rows) + "</tbody></table>")

    stats_j = ctx.jockey_stats.get(jockey, {})
    runners_today = ctx.jockey_runners_today.get(jockey, [])
    body = (
        _today_runners_html(runners_today)
        + f"<p><b>{escape(jockey)}</b> rides "
        f"<b>{escape(runner.get('name','—'))}</b> for the first time.</p>"
        + table
        + _courses_record_html(stats_j, runners_today)
        + _overall_form_html(stats_j)
        + "<p class='pop-foot'>jockey_form per horse, from horse_form.json</p>"
    )
    return Tag(
        slug="jockey-first-on-horse",
        label="J 1st",
        category="J",
        severity="soft",
        popover_title="First Time On This Horse",
        popover_body_html=body,
    )


def tag_jockey_winning_combo(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """Jockey has previously won on this horse (>= 2 wins together).

    A 1-from-N record can be coincidence; 2+ from the same partnership is
    a meaningful positive signal.
    """
    if not ctx.horse_form:
        return None
    jockey = runner.get("jockey")
    if not jockey:
        return None
    key = _norm_horse_name(runner.get("name", ""))
    entry = ctx.horse_form.get(key)
    if not entry:
        return None
    jform = (entry.get("jockey_form") or {}).get(jockey)
    if not jform:
        return None
    runs = jform.get("runs", 0)
    wins = jform.get("wins", 0)
    if wins < 2:
        return None

    pct = 100 * wins / runs if runs else 0
    stats_j = ctx.jockey_stats.get(jockey, {})
    runners_today = ctx.jockey_runners_today.get(jockey, [])
    body = (
        _today_runners_html(runners_today)
        + f"<p><b>{escape(jockey)}</b> on <b>{escape(runner.get('name','—'))}</b>: "
        f"<b>{wins}/{runs}</b> ({pct:.0f}% together).</p>"
        + _courses_record_html(stats_j, runners_today)
        + _overall_form_html(stats_j)
        + "<p class='pop-foot'>jockey_form per horse, from horse_form.json</p>"
    )
    return Tag(
        slug="jockey-winning-combo",
        label=f"J w {wins}/{runs}",
        category="J",
        severity="strong" if wins >= 3 or pct >= 50 else "soft",
        popover_title="Jockey Won With Horse Before",
        popover_body_html=body,
    )



def tag_trainer_first_time_booking(runner: dict, race: dict, ctx: TagContext) -> Optional[Tag]:
    """An established trainer is using a jockey they have never used before.

    Fires when the (trainer, jockey) pair has 0 prior runs in horse_form
    AND the trainer has 30+ recorded runs (so we know they have a real
    jockey rotation, this isn't a new yard with no history).
    """
    trainer = runner.get("trainer")
    jockey = runner.get("jockey")
    if not trainer or not jockey:
        return None
    if not ctx.tj_pair_runs:
        return None
    if ctx.tj_pair_runs.get((trainer, jockey), 0) > 0:
        return None
    total = ctx.trainer_total_runs.get(trainer, 0)
    if total < 30:
        return None

    # Top jockeys this trainer normally uses, for the popover
    rotations = [
        (j, n) for (t, j), n in ctx.tj_pair_runs.items()
        if t == trainer
    ]
    rotations.sort(key=lambda kv: -kv[1])
    rows = []
    for jname, n in rotations[:5]:
        rows.append(f"<tr><td>{escape(jname)}</td><td>{n}</td></tr>")
    table = ""
    if rows:
        table = ("<table class='pop-table'>"
                 "<thead><tr><th>Usual jockey</th><th>Past rides</th></tr></thead>"
                 "<tbody>" + "".join(rows) + "</tbody></table>")

    stats_t = ctx.trainer_stats.get(trainer, {})
    runners_today = ctx.trainer_runners_today.get(trainer, [])
    body = (
        _today_runners_html(runners_today)
        + f"<p><b>{escape(jockey)}</b> rides for <b>{escape(trainer)}</b> "
        f"for the first time. Trainer has <b>{total}</b> recorded runs.</p>"
        + table
        + _courses_record_html(stats_t, runners_today)
        + _overall_form_html(stats_t)
        + "<p class='pop-foot'>Pair counts from horse_form.json runs[]</p>"
    )
    return Tag(
        slug="trainer-first-time-booking",
        label="TJ 1st",
        category="T",
        severity="soft",
        popover_title="Trainer Books Jockey for First Time",
        popover_body_html=body,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
TAG_RULES: list[Callable[[dict, dict, TagContext], Optional[Tag]]] = [
    tag_horse_course_winner,
    tag_horse_distance_winner,
    tag_horse_going_winner,
    tag_horse_improving,
    tag_horse_career_best,
    tag_horse_returning_from_break,
    tag_trainer_course_specialist,
    tag_trainer_sole_booking,
    tag_jockey_course_specialist,
    tag_horse_winless_at_course,
    tag_jockey_winless_on_horse,
    tag_heavy_penalty,
    # tag_jockey_first_on_horse,  # too noisy — disabled, see TAG_PLAN.md
    tag_jockey_winning_combo,
    tag_trainer_first_time_booking,
]

# When BOTH tags in a pair fire, drop the first (the second supersedes it).
SUPERSEDES = {
    "trainer-course-specialist": "trainer-sole-booking",
    "horse-improving":           "horse-career-best",
    # J 1st (informational) gets dropped if there's a richer J/horse signal
    # — but those rules require jockey_form entries, so 1st only fires when
    # they don't. No override needed for collisions; here for clarity.
}

# Order tags appear when a runner has multiple. Strong tags come first;
# within a severity tier, sort by category (H, T, J, C, M).
CATEGORY_RANK = {"H": 0, "T": 1, "J": 2, "C": 3, "M": 4}
MAX_TAGS_PER_RUNNER = 7


def evaluate(runner: dict, race: dict, ctx: TagContext) -> list[Tag]:
    tags: list[Tag] = []
    for rule in TAG_RULES:
        try:
            t = rule(runner, race, ctx)
        except Exception as e:
            print(f"  [tag] {rule.__name__} failed for "
                  f"{runner.get('name')!r}: {e!r}")
            continue
        if t is not None:
            tags.append(t)

    present = {t.slug for t in tags}
    tags = [t for t in tags
            if SUPERSEDES.get(t.slug) is None
            or SUPERSEDES[t.slug] not in pre            or SUPERSEDES[t.slug] not in present]
    return tags
