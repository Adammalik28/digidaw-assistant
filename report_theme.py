"""
One look for every report Digidaw opens in the browser (security analysis, system analysis, ...): the HUD's dark
navy and cyan, but laid out for reading — a score ring, stat cards with bars, findings grouped by what to do about
them, commands with a copy button, filter chips, and a clean light version when printed.

Pages are self-contained (inline CSS + JS, no network), so they open offline and can be shared as one file.
"""
import html
import re

COLORS = {"tinggi": "#ff6b6b", "sedang": "#ffb547", "rendah": "#5ccfff", "info": "#8aa4b4", "ok": "#4dffb5"}


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober",
         "November", "Desember"]


def tanggal(dt) -> str:
    """'14 Oktober 2026, 11:42' (strftime's %B gives English month names)."""
    return f"{dt.day} {BULAN[dt.month - 1]} {dt.year}, {dt:%H:%M}"


def level_color(percent: float) -> str:
    return COLORS["tinggi"] if percent >= 90 else COLORS["sedang"] if percent >= 75 else COLORS["ok"]


# ───────────────────────────── components ─────────────────────────────

def score_ring(score: int, label: str = "skor") -> str:
    """An SVG ring that fills up to the score, coloured by how good it is."""
    color = COLORS["ok"] if score >= 80 else COLORS["sedang"] if score >= 55 else COLORS["tinggi"]
    circumference = 2 * 3.14159 * 52
    filled = circumference * max(0, min(score, 100)) / 100
    return f"""<div class="ring" style="--ring:{color}">
  <svg viewBox="0 0 120 120" aria-hidden="true">
    <circle cx="60" cy="60" r="52" class="ring-bg"/>
    <circle cx="60" cy="60" r="52" class="ring-fg" stroke-dasharray="{filled:.1f} {circumference:.1f}"/>
  </svg>
  <div class="ring-text"><b>{score}</b><span>{esc(label)}</span></div>
</div>"""


def stat_card(label: str, value: str, percent: float | None = None, detail: str = "") -> str:
    bar = ""
    if percent is not None:
        bar = (f'<div class="bar"><i style="width:{max(0, min(percent, 100)):.0f}%;'
               f'background:{level_color(percent)}"></i></div>')
    return (f'<div class="stat"><div class="stat-label">{esc(label)}</div>'
            f'<div class="stat-value">{esc(value)}</div>{bar}'
            f'{f"<div class=stat-detail>{esc(detail)}</div>" if detail else ""}</div>')


def chips(items: list[tuple[str, str, str]]) -> str:
    """Filter chips: (key, label, colour). key 'all' shows everything."""
    return '<div class="chips" role="toolbar">' + "".join(
        f'<button class="chip{" on" if key == "all" else ""}" data-filter="{esc(key)}" style="--c:{color}">'
        f'{esc(label)}</button>' for key, label, color in items) + "</div>"


def command_block(command: str) -> str:
    return (f'<div class="cmd"><code>{esc(command)}</code>'
            f'<button class="copy" type="button" title="Salin perintah">Salin</button></div>')


# ───────────────────────────── markdown ─────────────────────────────

STATUS_EMOJI = {"🟢": ("ok", "Baik"), "🟡": ("sedang", "Pantau"), "🟠": ("sedang", "Perhatian"),
                "🔴": ("tinggi", "Masalah"), "✅": ("ok", "OK"), "⚠️": ("sedang", "Perhatian"), "❌": ("tinggi", "Gagal")}


def _sectionize(md: str) -> str:
    """Agent reports often use top-level bullets as sections ("- **Ringkasan Kondisi**: ..."). Turn those into
    real headings and un-indent their content, so tables inside them render and the page gets a structure."""
    out, inside = [], False
    for line in md.splitlines():
        m = re.match(r"^[-*]\s+\*\*(.+?)\*\*\s*:?\s*(.*)$", line)
        if m and not line.startswith(" "):
            out += ["", f"## {m[1].strip().rstrip(':')}", ""]
            if m[2].strip():
                out.append(m[2].strip())
            inside = True
            continue
        if inside and line.startswith("  "):
            line = line[2:]
        elif inside and line.strip() and not line.startswith(" "):
            inside = False
        out.append(line)
    return "\n".join(out)


def render_markdown(md: str) -> str:
    md = _sectionize(md)
    try:
        from markdown_it import MarkdownIt
        body = MarkdownIt("commonmark", {"html": False, "typographer": True}).enable(["table", "strikethrough"]).render(md)
    except Exception:
        body = f"<pre>{esc(md)}</pre>"
    body = re.sub(r"<table>", '<div class="table-wrap"><table>', body).replace("</table>", "</table></div>")
    body = re.sub(r"<pre><code([^>]*)>", r'<div class="code"><button class="copy" type="button">Salin</button><pre><code\1>', body)
    body = body.replace("</code></pre>", "</code></pre></div>")
    for emoji, (kind, label) in STATUS_EMOJI.items():
        body = body.replace(emoji, f'<span class="pill {kind}" title="{label}"></span>')
    return body


def toc(body_html: str) -> str:
    """A table of contents from the h2 headings (anchors are added to the body by add_anchors)."""
    heads = re.findall(r"<h2[^>]*>(.*?)</h2>", body_html)
    if len(heads) < 3:
        return ""
    links = "".join(f'<a href="#s{i}">{re.sub("<[^>]+>", "", h)}</a>' for i, h in enumerate(heads))
    return f'<nav class="toc"><div class="toc-title">Isi laporan</div>{links}</nav>'


def add_anchors(body_html: str) -> str:
    counter = iter(range(1000))
    return re.sub(r"<h2>", lambda m: f'<h2 id="s{next(counter)}">', body_html)


# ───────────────────────────── page ─────────────────────────────

CSS = """
:root{--bg:#030a12;--bg2:#06131f;--card:rgba(10,32,50,.55);--card2:rgba(12,40,62,.8);--line:rgba(92,207,255,.16);
--line2:rgba(92,207,255,.38);--text:#dcf3ff;--muted:#7f9fb1;--cyan:#5ccfff;--cyan2:#a6e9ff;--radius:14px;
--ok:#4dffb5;--warn:#ffb547;--bad:#ff6b6b;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;color:var(--text);font:15.5px/1.65 Inter,'Segoe UI',system-ui,-apple-system,sans-serif;
background:radial-gradient(1200px 600px at 15% -10%,rgba(40,140,200,.18),transparent 60%),
radial-gradient(900px 500px at 110% 10%,rgba(77,255,181,.07),transparent 60%),var(--bg);min-height:100vh}
.wrap{max-width:1040px;margin:0 auto;padding:40px 22px 64px}
header.top{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;
border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:26px}
.brand{font:700 12px/1 'Segoe UI',sans-serif;letter-spacing:.38em;color:var(--cyan);text-transform:uppercase}
h1{margin:10px 0 6px;font-size:30px;line-height:1.15;letter-spacing:.01em;color:#fff}
.sub{color:var(--muted);font-size:14px}
.actions{display:flex;gap:8px}
.btn{background:transparent;border:1px solid var(--line2);color:var(--cyan2);border-radius:999px;padding:7px 14px;
font:600 13px 'Segoe UI',sans-serif;cursor:pointer}
.btn:hover{background:rgba(92,207,255,.1)}
.badges{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.badge{background:rgba(92,207,255,.08);border:1px solid var(--line);padding:3px 10px;border-radius:999px;
font-size:12.5px;color:var(--cyan2)}
.hero{display:grid;grid-template-columns:auto 1fr;gap:28px;align-items:center;background:linear-gradient(135deg,
rgba(16,56,84,.65),rgba(6,20,32,.6));border:1px solid var(--line2);border-radius:20px;padding:26px 30px;margin-bottom:22px;
box-shadow:0 18px 50px rgba(0,0,0,.35)}
.hero h2{margin:0 0 6px;font-size:22px;color:#fff;border:0;padding:0}
.hero p{margin:0;color:#c3e2f0}
.ring{position:relative;width:132px;height:132px}
.ring svg{width:100%;height:100%;transform:rotate(-90deg)}
.ring-bg{fill:none;stroke:rgba(255,255,255,.07);stroke-width:10}
.ring-fg{fill:none;stroke:var(--ring);stroke-width:10;stroke-linecap:round;filter:drop-shadow(0 0 6px var(--ring))}
.ring-text{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.ring-text b{font-size:40px;line-height:1;color:var(--ring)}
.ring-text span{font-size:11px;letter-spacing:.2em;color:var(--muted);text-transform:uppercase;margin-top:4px}
.voice{border-left:3px solid var(--cyan);padding:2px 0 2px 16px;color:#d5eef9;font-size:16px}
.voice .label{display:block;font-size:11px;letter-spacing:.25em;color:var(--cyan);text-transform:uppercase;margin-bottom:4px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:26px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px}
.stat-label{font-size:11.5px;letter-spacing:.18em;color:var(--muted);text-transform:uppercase}
.stat-value{font-size:26px;font-weight:700;color:#fff;margin:4px 0 8px}
.stat-detail{font-size:12.5px;color:var(--muted);margin-top:8px}
.bar{height:6px;background:rgba(255,255,255,.07);border-radius:99px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:99px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px}
.chip{--c:var(--cyan);background:transparent;border:1px solid color-mix(in srgb,var(--c) 45%,transparent);
color:var(--c);border-radius:999px;padding:6px 14px;font:600 13px 'Segoe UI',sans-serif;cursor:pointer}
.chip.on,.chip:hover{background:color-mix(in srgb,var(--c) 16%,transparent)}
.group{margin:28px 0 12px;display:flex;align-items:center;gap:10px;font-size:13px;letter-spacing:.2em;
text-transform:uppercase;color:var(--muted)}
.group::after{content:"";flex:1;height:1px;background:var(--line)}
.finding{--c:var(--cyan);position:relative;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
padding:16px 18px 16px 22px;margin-bottom:12px;overflow:hidden}
.finding::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--c)}
.finding .meta{display:flex;gap:10px;align-items:center;font-size:11.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
.sev{color:var(--c);border:1px solid color-mix(in srgb,var(--c) 50%,transparent);border-radius:999px;padding:1px 9px;letter-spacing:.12em}
.finding h3{margin:6px 0 4px;font-size:17px;color:#fff}
.finding .detail{font:12.5px/1.55 'JetBrains Mono',Consolas,monospace;color:#9fc4d3;word-break:break-word;margin-top:4px}
.finding .advice{margin-top:10px;color:#d6eef9}
details.ok-list summary{cursor:pointer;color:var(--muted);margin:6px 0 12px}
.cmd,.code{position:relative;margin-top:10px;background:#020a12;border:1px solid var(--line);border-radius:10px}
.cmd code{display:block;padding:10px 74px 10px 14px;font:13px 'JetBrains Mono',Consolas,monospace;color:var(--cyan2);
overflow-x:auto;white-space:pre}
.code pre{margin:0;padding:12px 74px 12px 14px;overflow-x:auto}
.copy{position:absolute;top:7px;right:8px;background:rgba(92,207,255,.1);border:1px solid var(--line2);color:var(--cyan2);
border-radius:7px;padding:3px 10px;font:600 12px 'Segoe UI',sans-serif;cursor:pointer}
.copy.done{color:var(--ok);border-color:var(--ok)}
.layout{display:grid;grid-template-columns:220px 1fr;gap:28px;align-items:start}
.toc{position:sticky;top:20px;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:14px}
.toc-title{font-size:11px;letter-spacing:.22em;color:var(--muted);text-transform:uppercase;margin-bottom:8px}
.toc a{display:block;color:#bfe3f2;text-decoration:none;font-size:13.5px;padding:5px 8px;border-radius:7px}
.toc a:hover{background:rgba(92,207,255,.1);color:#fff}
.doc{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:8px 30px 26px}
.doc h1{font-size:22px;margin-top:22px}
.doc h2{font-size:19px;color:#fff;margin:30px 0 10px;padding-top:18px;border-top:1px solid var(--line);scroll-margin-top:16px}
.doc h2:first-of-type{border-top:0}
.doc h3{font-size:16px;color:var(--cyan2);margin:20px 0 6px}
.doc li{margin:4px 0}
.doc code{font:13px 'JetBrains Mono',Consolas,monospace;background:rgba(92,207,255,.1);color:var(--cyan2);
padding:1px 6px;border-radius:5px}
.doc pre code{background:none;padding:0}
.table-wrap{overflow-x:auto;margin:12px 0;border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:14px}
th{background:rgba(92,207,255,.08);color:var(--cyan2);text-align:left;font-weight:600;letter-spacing:.02em}
th,td{padding:9px 14px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:rgba(92,207,255,.04)}
.pill{display:inline-block;width:10px;height:10px;border-radius:50%;margin:0 4px;vertical-align:-1px}
.pill.ok{background:var(--ok);box-shadow:0 0 8px var(--ok)}.pill.sedang{background:var(--warn);box-shadow:0 0 8px var(--warn)}
.pill.tinggi{background:var(--bad);box-shadow:0 0 8px var(--bad)}
footer{margin-top:34px;color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:16px}
[hidden]{display:none!important}
@media (max-width:820px){.layout{grid-template-columns:1fr}.toc{position:static}.hero{grid-template-columns:1fr;justify-items:start}
.doc{padding:4px 18px 20px}h1{font-size:24px}}
@media print{:root{--bg:#fff;--card:#fff;--text:#111;--muted:#555;--line:#ddd;--line2:#bbb;--cyan:#0a6c96;--cyan2:#0a6c96;color-scheme:light}
body{background:#fff}.actions,.chips,.copy,.toc{display:none!important}.hero,.stat,.finding,.doc{box-shadow:none;background:#fff}
h1,.hero h2,.finding h3,.doc h2,.stat-value{color:#111}.hero p,.voice,.finding .advice{color:#222}.layout{display:block}
details.ok-list{display:block}details.ok-list>*{display:block}}
"""

JS = """
document.addEventListener('click', e => {
  const copy = e.target.closest('.copy');
  if (copy) {
    const box = copy.parentElement, text = (box.querySelector('code') || box).innerText.trim();
    navigator.clipboard.writeText(text).then(() => {
      copy.textContent = 'Tersalin'; copy.classList.add('done');
      setTimeout(() => { copy.textContent = 'Salin'; copy.classList.remove('done'); }, 1600);
    });
  }
  const chip = e.target.closest('.chip');
  if (chip) {
    document.querySelectorAll('.chip').forEach(c => c.classList.toggle('on', c === chip));
    const f = chip.dataset.filter;
    document.querySelectorAll('[data-sev]').forEach(el => el.hidden = !(f === 'all' || el.dataset.sev === f));
    document.querySelectorAll('.group').forEach(g => {
      const items = document.querySelectorAll(`[data-group="${g.dataset.group}"]`);
      g.hidden = items.length > 0 && [...items].every(i => i.hidden);
    });
    if (f !== 'all') document.querySelectorAll('details.ok-list').forEach(d => d.open = true);
  }
  if (e.target.closest('[data-print]')) window.print();
});
"""


def page(title: str, brand: str, subtitle: str, body: str, badges: list[str] | None = None) -> str:
    badge_html = ('<div class="badges">' + "".join(f'<span class="badge">{esc(b)}</span>' for b in badges) + "</div>"
                  if badges else "")
    return f"""<!doctype html>
<html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — Digidaw</title><style>{CSS}</style></head>
<body><div class="wrap">
<header class="top"><div><div class="brand">{esc(brand)}</div><h1>{esc(title)}</h1><div class="sub">{esc(subtitle)}</div>
{badge_html}</div><div class="actions"><button class="btn" data-print>Cetak / PDF</button></div></header>
{body}
</div><script>{JS}</script></body></html>"""
