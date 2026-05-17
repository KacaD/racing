"""
bw.py — Winsmore dashboard builder.
Wraps build_dashboard.py and post-processes the HTML to add:
  - Clickable trainer/jockey names (popover with runners today + stats)
  - Dismiss (−) button per runner to collapse horses you don't want
Run: python bw.py  [optional/path/to/card.json]
"""
from __future__ import annotations
import json, re, sys
from html import escape, unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_dashboard as bd
import tags
from conn_pop import build_conn_json

# ── CSS injected before </style> ─────────────────────────────────────────────
EXTRA_CSS = (
    ".conn-name{background:transparent;border:0;padding:0;font:inherit;"
    "color:var(--text-2);cursor:pointer;text-decoration:underline dotted var(--text-3);"
    "text-underline-offset:3px;}.conn-name:hover{color:var(--text);}"
    ".runner-min{background:transparent;border:1px solid var(--border);border-radius:4px;"
    "color:var(--text-3);cursor:pointer;font-size:13px;line-height:1;padding:1px 6px;"
    "margin-left:6px;flex-shrink:0;transition:color .1s,border-color .1s;}"
    ".runner-min:hover{color:var(--text);border-color:var(--text-3);}"
    ".runner.dismissed .tag-row,.runner.dismissed .rc-stats,"
    ".runner.dismissed .rc-conn{display:none;}"
    ".runner.dismissed{opacity:0.38;}.runner.dismissed .rc-head{margin-bottom:0;}"
)

# ── JS injected before </body> ────────────────────────────────────────────────
EXTRA_JS = r"""<script>
(function(){
  var CD=window.__connData||{T:{},J:{}};
  var bd=document.getElementById('popover-backdrop'),
      pt=document.getElementById('pop-title'),pb=document.getElementById('pop-body');
  function show(t,h){pt.textContent=t;pb.innerHTML=h;bd.classList.add('show');}
  function sr(d){return(!d||!d.runs)?'—':d.wins+'W / '+d.runs+'R ('+d.win_pct.toFixed(0)+'%)';}
  function cpop(name,data){
    var rr=data.r||[],n=rr.length,crs=[...new Set(rr.map(function(x){return x.c;}))];
    var h='<p style="margin:0 0 4px;color:var(--text-2);font-size:12px"><b>'+n+'</b> runner'+(n!==1?'s':'')+' today</p>';
    if(crs.length>1)h+='<p class="pop-alert">&#x26A1; Active at '+crs.length+' courses: '+crs.join(', ')+'</p>';
    h+='<p class="pop-section-label">Runners today</p><table class="pop-table pop-runners-today"><tbody>';
    rr.slice().sort(function(a,b){return a.t.localeCompare(b.t);}).forEach(function(e){
      h+='<tr><td class="pop-time">'+e.t+'</td><td><span class="pop-badge">'+e.c+'</span>'+e.h+'</td><td class="pop-odds-cell">'+(e.o||'—')+'</td></tr>';
    });
    h+='</tbody></table>';
    var cs=data.cs||{},ck=Object.keys(cs);
    if(ck.length){h+='<p class="pop-section-label">Course record</p><table class="pop-table"><tbody>';
      ck.forEach(function(c){var d=cs[c],p=d.win_pct||0,col=p>=20?'#6ee7a4':p>=12?'#e7b75f':'';
        h+='<tr><td>'+c+'</td><td'+(col?' style="color:'+col+'"':'')+'>'+d.wins+'W / '+d.runs+'R ('+p.toFixed(0)+'%)</td></tr>';
      });h+='</tbody></table>';}
    h+='<p class="pop-section-label">Form</p><table class="pop-table"><tbody>'
      +'<tr><td>All-time</td><td>'+sr(data.all)+'</td></tr>'
      +'<tr><td>Last 14 days</td><td>'+sr(data.d14)+'</td></tr>'
      +'<tr><td>Last 28 days</td><td>'+sr(data.d28)+'</td></tr>'+'</tbody></table>';
    return h;
  }
  document.addEventListener('click',function(e){
    var mb=e.target.closest('.runner-min');
    if(mb){e.stopPropagation();var r=mb.closest('.runner'),d=r.classList.toggle('dismissed');
      mb.textContent=d?'+':'−';mb.title=d?'Restore runner':'Dismiss runner';return;}
    var el=e.target.closest('.conn-name');
    if(!el)return;
    e.stopPropagation();
    if(el.dataset.trainer){var d=CD.T[el.dataset.trainer];if(d){show('Trainer: '+el.dataset.trainer,cpop(el.dataset.trainer,d));return;}}
    if(el.dataset.jockey){var d=CD.J[el.dataset.jockey];if(d){show('Jockey: '+el.dataset.jockey,cpop(el.dataset.jockey,d));return;}}
  });
})();
</script>"""

# ── post-processing steps ─────────────────────────────────────────────────────
_DISMISS_BTN = '<button class="runner-min" title="Dismiss runner">−</button>'

def _dismiss(html):
    return re.sub(
        r'(<div class="rc-head">)(.*?)(</div>)',
        lambda m: m.group(1)+m.group(2)+_DISMISS_BTN+m.group(3),
        html, flags=re.DOTALL)

def _clickable(html):
    def repl(m):
        k, ne = m.group(1), m.group(2)
        attr = "data-jockey" if k=="J" else "data-trainer"
        return (f'<span class="conn"><span class="conn-k">{k}</span> '
                f'<button class="conn-name" {attr}="{escape(unescape(ne))}">{ne}</button></span>')
    return re.sub(r'<span class="conn"><span class="conn-k">([JT])</span> ([^<]+)</span>', repl, html)

def post_process(html, conn_json):
    html = html.replace("</style>", EXTRA_CSS+"</style>", 1)
    html = _dismiss(html)
    html = _clickable(html)
    html = html.replace("</body>",
        f'<script>window.__connData={conn_json};</script>'+EXTRA_JS+"</body>", 1)
    return html

# ── find stats dir ────────────────────────────────────────────────────────────
def _find_stats_dir(card_path):
    """Winsmore is self-contained. Look only in the script dir and the card's
    parent directory — never in C:\\Betfair or any external folder."""
    for candidate in [bd.SCRIPT_DIR, card_path.resolve().parent]:
        if (candidate / "trainer_stats.json").exists():
            return candidate
    return None

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else bd.DEFAULT_CARD
    if not src.exists():
        sys.exit(f"ERROR: card file not found: {src}")
    card = bd.load_card(src)
    stats_dir = _find_stats_dir(src)
    ctx = tags.TagContext.load(stats_dir, card) if stats_dir else None
    if ctx is None:
        print("WARNING: no stats dir found — tags and popovers will be empty")
    html = bd.render_dashboard(card, ctx)
    conn_json = build_conn_json(card, ctx.trainer_stats, ctx.jockey_stats) if ctx else "{}"
    html = post_process(html, conn_json)
    bd.OUTPUT_HTML.write_text(html, encoding="utf-8")
    (bd.SCRIPT_DIR/"index.html").write_text(html, encoding="utf-8")
    n = len(card.get("races",[])); r = sum(len(x.get("runners",[])) for x in card.get("races",[]))
    print(f"Built {card.get('date')}  {n} races  {r} runners  "
          f"{html.count('runner-min')} dismiss-btns  {html.count('conn-name')} conn-btns"
          + (f"  data-dir={stats_dir}" if stats_dir else "  (no stats)"))

if __name__ == "__main__":
    main()
