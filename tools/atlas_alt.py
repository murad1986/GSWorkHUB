#!/usr/bin/env python3
"""Атлас развития — вид B: светлая тема с чипами.

Данные и логика страниц — из `atlas.collect` и `atlas.SCRIPT`.
Оболочка своя: светлый UI в духе компонентных китов (чипы статусов,
фильтры-пилюли, мягкие карточки). Основа `tools/atlas.py` не меняется.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
from pathlib import Path

import yaml
from atlas import KIND_RU, NODE_RU, ROOT, SCRIPT, STATUS_RU, collect

# Светлый кит, отполированный: единые списки-карточки, тихие чипы с точкой,
# мягкий lift, без крика «синей заливкой».
STYLE = """
:root{
  --bg:#f8fafc;
  --surface:#ffffff;
  --ink:#0f172a;
  --soft:#64748b;
  --faint:#94a3b8;
  --line:#e2e8f0;
  --line-strong:#cbd5e1;
  --proved:#c2410c;
  --proved-bg:#fff7ed;
  --proved-border:#fed7aa;
  --open:#1d4ed8;
  --open-bg:#eff6ff;
  --open-border:#bfdbfe;
  --shut:#64748b;
  --shut-bg:#f1f5f9;
  --accent:#2563eb;
  --accent-soft:#eff6ff;
  --chip-face:var(--surface);
  --shadow:0 1px 2px rgba(15,23,42,.04), 0 1px 3px rgba(15,23,42,.06);
  --shadow-lg:0 4px 6px -1px rgba(15,23,42,.05), 0 10px 24px -4px rgba(15,23,42,.08);
  --fd:"Plus Jakarta Sans", "Avenir Next", "Segoe UI", sans-serif;
  --fb:"Plus Jakarta Sans", "Avenir Next", "Segoe UI", sans-serif;
  --fm:"IBM Plex Mono", "SF Mono", Menlo, monospace;
  --r:14px; --r-sm:10px; --chip:999px;
  --ease:cubic-bezier(.2,.8,.2,1);
}
:root[data-theme="dark"]{
  --bg:#0a0e14; --surface:#121820; --ink:#eef3fa; --soft:#a8b4c4; --faint:#738194;
  --line:#1e2733; --line-strong:#2a3545;
  --proved:#fb923c; --proved-bg:rgba(251,146,60,.14); --proved-border:rgba(251,146,60,.36);
  --open:#7db4ff; --open-bg:rgba(125,180,255,.12); --open-border:rgba(125,180,255,.34);
  --shut:#94a3b8; --shut-bg:rgba(148,163,184,.12);
  --accent:#7db4ff; --accent-soft:rgba(125,180,255,.14);
  --chip-face:rgba(255,255,255,.04);
  --shadow:0 1px 2px rgba(0,0,0,.4);
  --shadow-lg:0 14px 36px rgba(0,0,0,.45);
}
:root[data-theme="dark"] body{
  background:
    radial-gradient(800px 380px at 0% -10%, color-mix(in srgb,var(--accent) 12%,transparent), transparent 55%),
    var(--bg);
}
:root[data-theme="dark"] .top{
  background:color-mix(in srgb,var(--bg) 88%, transparent);
  border-bottom-color:var(--line-strong);
}
:root[data-theme="dark"] .find input,
:root[data-theme="dark"] .nav,
:root[data-theme="dark"] .bar,
:root[data-theme="dark"] .rows,
:root[data-theme="dark"] .tile,
:root[data-theme="dark"] .take,
:root[data-theme="dark"] .rolebar{
  box-shadow:none;
}
:root[data-theme="dark"] .rolebar.cold{
  border-color:rgba(251,146,60,.35);
  background:rgba(251,146,60,.08);
}
*{box-sizing:border-box}
::selection{background:color-mix(in srgb,var(--accent) 22%,transparent)}
body{
  margin:0; background:
    radial-gradient(900px 420px at 0% -10%, color-mix(in srgb,var(--accent) 8%,transparent), transparent 60%),
    var(--bg);
  color:var(--ink); font-family:var(--fb); font-size:14.5px; line-height:1.55;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
a{color:inherit; text-decoration:none}
a:hover{color:var(--accent)}
.wrap{max-width:1080px; margin:0 auto; padding:0 28px 88px}
.top{
  position:sticky; top:0; z-index:30; border-bottom:1px solid var(--line);
  background:color-mix(in srgb,var(--bg) 78%, transparent); backdrop-filter:saturate(1.2) blur(14px);
}
.topin{
  max-width:1080px; margin:0 auto; padding:10px 28px; display:flex;
  align-items:center; gap:14px; flex-wrap:wrap;
}
.mark{
  display:flex; align-items:center; gap:10px; font-family:var(--fd);
  font-size:14.5px; font-weight:700; letter-spacing:-.02em; white-space:nowrap;
}
.mark .pill{
  font-family:var(--fm); font-size:10px; font-weight:500; letter-spacing:.08em;
  text-transform:uppercase; color:var(--accent); background:var(--accent-soft);
  border:1px solid var(--open-border); padding:3px 8px; border-radius:var(--chip);
}
.nav{display:flex; gap:4px; flex-wrap:wrap; padding:2px; background:color-mix(in srgb,var(--surface) 70%, transparent);
  border:1px solid var(--line); border-radius:var(--chip)}
.nav a{
  font-size:13px; color:var(--soft); padding:6px 12px; border-radius:var(--chip);
  border:1px solid transparent; font-weight:600; transition:background .15s var(--ease), color .15s var(--ease);
}
.nav a:hover{background:var(--surface); color:var(--ink)}
.nav a.on{
  color:var(--ink); background:var(--surface); border-color:var(--line); box-shadow:var(--shadow);
}
.nav-sub{
  display:flex; flex-wrap:wrap; gap:4px 12px; align-items:center;
  font-size:12.5px; color:var(--faint); margin-left:4px;
}
.nav-sub a{color:var(--soft); font-weight:550; text-decoration:none; border-bottom:1px solid transparent}
.nav-sub a:hover,.nav-sub a.on{color:var(--ink); border-bottom-color:var(--line-strong)}
.nav-sub .sep{opacity:.45}
.grow{flex:1}
.find{position:relative; width:270px}
.find input{
  width:100%; padding:9px 14px; font:inherit; font-size:13px; color:var(--ink);
  background:var(--surface); border:1px solid var(--line); border-radius:var(--chip);
  box-shadow:var(--shadow); transition:border-color .15s var(--ease), box-shadow .15s var(--ease);
}
.find input::placeholder{color:var(--faint)}
.find input:focus{
  outline:none; border-color:var(--open-border);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 18%,transparent), var(--shadow);
}
.pop{
  position:absolute; top:46px; right:0; width:min(420px, calc(100vw - 32px));
  max-height:60vh; overflow:auto; background:var(--surface); border:1px solid var(--line);
  border-radius:var(--r); padding:6px; box-shadow:var(--shadow-lg);
}
.pop a{
  display:grid; grid-template-columns:58px 1fr; gap:10px; padding:9px 10px;
  border-radius:var(--r-sm); font-size:13.5px; align-items:baseline;
}
.pop a:hover{background:var(--bg); color:inherit}
.pop .k{
  display:inline-flex; align-items:center; justify-content:center;
  font-family:var(--fm); font-size:10px; color:var(--soft); text-transform:uppercase;
  letter-spacing:.06em; background:var(--shut-bg); border:1px solid var(--line);
  border-radius:var(--chip); padding:3px 7px; width:max-content;
}
.pop .sub{display:block; color:var(--soft); font-size:12px; margin-top:2px}
.pop .none{padding:10px; font-size:13px; color:var(--soft)}
.theme{
  font:inherit; font-size:12px; font-weight:650; padding:8px 12px; cursor:pointer;
  color:var(--soft); background:var(--surface); border:1px solid var(--line);
  border-radius:var(--chip); box-shadow:var(--shadow); transition:color .15s var(--ease);
}
.theme:hover{color:var(--ink)}
@media (max-width:760px){
  .grow{display:none}
  .find{order:6; flex:1 1 100%; width:auto}
  .nav{width:100%; justify-content:flex-start; overflow:auto}
  .topin{gap:10px; padding:10px 18px}
  .wrap{padding:0 18px 72px}
}
.crumb{
  display:flex; flex-wrap:wrap; gap:8px; align-items:center; font-size:12px; color:var(--faint);
  padding:28px 0 0; font-family:var(--fm);
}
.crumb span:not(:first-child)::before,
.crumb a + span::before{content:""; }
.crumb a:hover{color:var(--accent)}
#page{animation:in .28s var(--ease) both}
@keyframes in{from{opacity:0; transform:translateY(4px)} to{opacity:1; transform:none}}
h1{
  font-family:var(--fd); font-weight:750; font-size:clamp(28px, 4vw, 34px);
  line-height:1.12; margin:10px 0 0; letter-spacing:-.035em; text-wrap:pretty;
}
.lede{margin:10px 0 0; font-size:15px; color:var(--soft); max-width:60ch; text-wrap:pretty}
.en{font-family:var(--fm); font-size:12px; color:var(--faint); margin:8px 0 0}
.chips{display:flex; flex-wrap:wrap; gap:8px 14px; margin:16px 0 0; align-items:center}
/* Чип оставляем только статусам (.row .st, .stat span, фильтры .bar). Остальное — текст. */
.chip{
  display:inline; font-size:13px; font-weight:550; padding:0; border-radius:0;
  background:transparent; color:var(--soft); border:0;
}
a.chip:hover{border:0; color:var(--open); background:transparent;
  text-decoration:underline; text-underline-offset:3px}
.code-link{
  display:inline; font-family:var(--fm); font-size:12px; font-weight:500;
  padding:0; border-radius:0; background:transparent; color:var(--open); border:0;
  white-space:nowrap; vertical-align:baseline;
}
.code-link:hover{background:transparent; text-decoration:underline; text-underline-offset:3px}
.sect{margin:28px 0 0}
.sect>h2{
  font-size:11.5px; letter-spacing:.04em; text-transform:uppercase; color:var(--faint);
  font-weight:700; margin:0 0 10px; padding-bottom:8px; border-bottom:1px solid var(--line);
  display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px 16px; align-items:center;
}
.sect>h2 span:first-child{min-width:0; line-height:1.35}
.sect>h2 span:last-child:empty{display:none}
.sect>h2 span:last-child:not(:empty){
  justify-self:end; max-width:100%;
  font-family:var(--fm); font-size:12px; font-weight:550; letter-spacing:0;
  text-transform:none; color:var(--faint); font-variant-numeric:tabular-nums;
  display:inline; padding:0; border:0; background:transparent; white-space:nowrap;
}
.sect>h2 span:last-child:not(:empty)::before{content:none}
.note{font-size:14.5px; color:var(--soft); margin:0; max-width:66ch; text-wrap:pretty}
.comp{margin:0; padding:0; list-style:none; counter-reset:comp;
  background:var(--surface); border:1px solid var(--line); border-radius:var(--r); overflow:hidden; box-shadow:var(--shadow)}
.comp li{
  counter-increment:comp; display:grid; grid-template-columns:32px 1fr; gap:10px;
  max-width:none; padding:10px 14px; border-bottom:1px solid var(--line); align-items:baseline;
}
.comp li:last-child{border-bottom:none}
.comp li::before{
  content:counter(comp); font-family:var(--fm); font-size:11px; color:var(--faint);
  background:var(--shut-bg); border:1px solid var(--line); border-radius:var(--chip);
  width:24px; height:24px; display:grid; place-items:center;
}
.comp li>div{grid-column:2}
.comp-t{font-size:14.5px; font-weight:650}
.comp-t:hover{color:var(--accent)}
.comp li .chip{
  margin-left:8px; vertical-align:baseline; color:var(--faint);
  background:transparent; border:0; font-size:12px; font-weight:550; padding:0;
}
.comp-d{grid-column:2; margin:5px 0 0; font-size:13.5px; color:var(--soft); text-wrap:pretty}
.tree{margin-top:20px; position:relative}
.tlayer{position:relative; padding:0 0 28px 28px; border-left:2px solid var(--line)}
.tlayer:last-child{border-left-color:transparent; padding-bottom:0}
.tlayer::before{
  content:""; position:absolute; left:-5px; top:10px; width:8px; height:8px;
  border-radius:50%; background:var(--accent); box-shadow:0 0 0 3px var(--accent-soft);
}
.thead{display:flex; align-items:baseline; gap:10px; margin:0 0 12px}
.tlv{
  font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase;
  color:var(--faint); background:transparent; border:0; border-radius:0; padding:0; box-shadow:none;
}
.tcnt{font-family:var(--fm); font-size:11px; color:var(--faint)}
.tnodes{display:grid; grid-template-columns:repeat(auto-fill,minmax(196px,1fr)); gap:10px}
.tnode{
  display:block; padding:12px 13px; border-radius:var(--r-sm); background:var(--surface);
  border:1px solid var(--line); box-shadow:var(--shadow);
  transition:border-color .15s var(--ease), transform .15s var(--ease), box-shadow .15s var(--ease);
}
.tnode:hover{border-color:var(--open-border); transform:translateY(-1px); color:inherit; box-shadow:var(--shadow-lg)}
.tid{display:block; font-family:var(--fm); font-size:11px; color:var(--faint);
  background:transparent; border:0; border-radius:0; padding:0; margin-bottom:6px}
.tnm{display:block; font-size:13.5px; font-weight:650; margin-top:0; text-wrap:pretty}
.tdom{display:block; font-size:11.5px; color:var(--faint); margin-top:6px}
.tnode.p{background:var(--proved-bg); border-color:var(--proved-border)}
.tnode.p .tid{color:var(--proved); background:transparent; border:0}
.tnode.o{background:var(--open-bg); border-color:var(--open-border)}
.tnode.o .tid{color:var(--open); background:transparent; border:0}
.tiles{display:grid; grid-template-columns:repeat(auto-fill,minmax(248px,1fr)); gap:12px}
.tile{
  display:block; padding:12px 13px; background:var(--surface); border:1px solid var(--line);
  border-radius:var(--r); box-shadow:var(--shadow);
  transition:border-color .15s var(--ease), transform .15s var(--ease), box-shadow .15s var(--ease);
}
.tile:hover{border-color:var(--open-border); transform:translateY(-1px); color:inherit; box-shadow:var(--shadow-lg)}
.tile h3{font-family:var(--fd); font-size:16px; font-weight:700; margin:0; letter-spacing:-.02em}
.tile p{
  margin:8px 0 0; font-size:13px; color:var(--soft); line-height:1.5;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;
  min-height:calc(1.5em * 2);
}
.tile .foot{margin-top:14px; font-family:var(--fm); font-size:11px; color:var(--faint)}
.meter{display:flex; gap:3px; margin-top:14px}
.meter i{height:5px; flex:1; border-radius:var(--chip); background:var(--line)}
.meter i.p{background:var(--proved)}
.meter i.o{background:var(--open)}
.meter i.s{background:var(--faint); opacity:.28}
.stats{
  display:flex; gap:12px; margin:22px 0 0; width:100%;
  align-items:stretch;
}
.stats.stats-4{display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px}
.meta-line{
  margin:14px 0 0; font-size:13.5px; color:var(--soft);
  display:flex; flex-wrap:wrap; gap:6px 14px; align-items:baseline;
}
.meta-line a{color:var(--open); font-weight:650}
.meta-line b{color:var(--ink); font-weight:700; font-variant-numeric:tabular-nums}
.stat{
  flex:1 1 0; min-width:0; width:0; /* равные доли ряда, без сжатия по тексту чипа */
  padding:14px 15px; background:var(--surface); border:1px solid var(--line-strong);
  border-radius:var(--r); box-shadow:var(--shadow);
  display:flex; flex-direction:column; align-items:flex-start;
  box-sizing:border-box;
}
.stat b{
  display:block; font-family:var(--fd); font-size:26px; font-weight:750; line-height:1.1;
  letter-spacing:-.03em; font-variant-numeric:tabular-nums;
}
.stat span{
  display:inline-flex; align-items:center; gap:6px; max-width:100%; margin-top:10px;
  font-size:11px; font-weight:700; letter-spacing:.03em; text-transform:uppercase; color:var(--soft);
  background:var(--chip-face); border:1px solid var(--line-strong); border-radius:var(--chip);
  padding:3px 9px; box-sizing:border-box;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.stat span::before{content:""; width:6px; height:6px; border-radius:50%; background:currentColor; opacity:.9; flex:none}
.stat.p b{color:var(--proved)}
.stat.o b{color:var(--open)}
.stat.p span{color:var(--proved); background:var(--proved-bg); border-color:var(--proved-border)}
.stat.o span{color:var(--open); background:var(--open-bg); border-color:var(--open-border)}
.rows{
  display:flex; flex-direction:column; gap:0; background:var(--surface);
  border:1px solid var(--line); border-radius:var(--r); overflow:hidden; box-shadow:var(--shadow);
}
.row{
  display:grid; grid-template-columns:52px 1fr 88px 96px; gap:8px 12px; align-items:center;
  padding:7px 12px; background:transparent; border:0; border-bottom:1px solid var(--line);
  border-radius:0; box-shadow:none; margin:0;
  transition:background .12s var(--ease);
}
.row:last-child{border-bottom:0}
.row:hover{background:color-mix(in srgb,var(--bg) 80%, var(--surface)); color:inherit}
.row .id{
  font-family:var(--fm); font-size:11.5px; color:var(--faint); background:transparent;
  border:0; border-radius:0; padding:0; text-align:left; justify-self:start;
}
.row.p .id{color:var(--proved); background:transparent; border:0}
.row.o .id{color:var(--open); background:transparent; border:0}
.row .nm{font-size:13.5px; font-weight:650; line-height:1.35}
.row .nm em{display:block; font-style:normal; font-size:12px; color:var(--soft); font-weight:500; margin-top:1px; line-height:1.35}
.row .st{
  display:inline-flex; align-items:center; justify-content:center; gap:5px;
  font-size:10.5px; font-weight:700; letter-spacing:.02em; text-transform:uppercase;
  border-radius:var(--chip); padding:2px 8px; border:1px solid var(--line);
  background:var(--shut-bg); color:var(--shut); justify-self:end;
}
.row .st::before{content:""; width:5px; height:5px; border-radius:50%; background:currentColor}
.row.p .st{color:var(--proved); background:var(--proved-bg); border-color:var(--proved-border)}
.row.o .st{color:var(--open); background:var(--open-bg); border-color:var(--open-border)}
.steps{display:flex; gap:4px; justify-content:flex-end}
.steps i{
  width:8px; height:8px; border-radius:50%; background:var(--line);
  border:1.5px solid transparent;
}
.steps i.k{background:transparent; border-color:var(--proved)}
.steps i.v{background:var(--proved); border-color:var(--proved)}
.steps i.t{border-color:var(--faint); border-style:dashed; background:transparent}
.gauge{
  display:flex; gap:12px; margin:22px 0 0; width:100%;
  align-items:stretch;
}
.gau{
  flex:1 1 0; min-width:0; width:0;
  padding:14px 15px; border-radius:var(--r); background:var(--surface); border:1px solid var(--line);
  box-shadow:var(--shadow); box-sizing:border-box;
  display:flex; flex-direction:column; align-items:flex-start;
}
.gau h4{
  margin:0; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--faint);
  font-weight:700;
}
.gau .val{font-family:var(--fd); font-size:28px; font-weight:750; line-height:1.15; margin-top:6px; letter-spacing:-.03em}
.gau .val small{font-size:14px; color:var(--faint)}
.gau p{margin:8px 0 0; font-size:12.5px; color:var(--soft); line-height:1.5}
.gau p.warn{color:var(--ink); border-top:1px solid var(--line); padding-top:8px}
.rolebar{
  display:flex; justify-content:space-between; gap:18px; align-items:flex-start;
  margin-top:16px; padding:16px 18px; border-radius:var(--r); background:var(--surface);
  border:1px solid var(--line); box-shadow:var(--shadow);
  transition:border-color .15s var(--ease), box-shadow .15s var(--ease);
}
.rolebar:hover{border-color:var(--open-border); box-shadow:var(--shadow-lg); color:inherit}
.rolebar.cold{border-color:var(--proved-border); background:var(--proved-bg)}
.rolebar.cold em{color:var(--proved)}
.rolebar b{font-family:var(--fd); font-size:16px; font-weight:700; display:block; letter-spacing:-.02em}
.rolebar span{font-size:12.5px; color:var(--soft); display:block; margin-top:4px}
.rolebar em{font-style:normal; font-size:12px; color:var(--faint); max-width:38ch; text-align:right; line-height:1.45}
.nowbook{
  display:flex; gap:14px; align-items:center; padding:12px 14px; border-radius:var(--r);
  background:var(--surface); border:1px solid var(--line); box-shadow:var(--shadow);
}
.nowbook + .nowbook{margin-top:10px}
.nowbook>div{min-width:0}
.nowbook img{width:48px; height:70px; object-fit:cover; border-radius:6px; flex:none; box-shadow:var(--shadow)}
.nowbook .nc{
  width:48px; height:70px; display:grid; place-items:center; flex:none; border-radius:6px;
  background:var(--open-bg); border:1px solid var(--open-border); font-family:var(--fd);
  font-size:18px; font-weight:700; color:var(--open);
}
.nowbook .cv{flex:none; display:block}
.nowbook .ttl{font-family:var(--fd); font-size:15px; font-weight:700; display:block; letter-spacing:-.015em}
.nowbook span{display:block; font-size:12.5px; color:var(--soft); margin-top:2px}
.nowbook .lifts{margin-top:8px; font-size:12.5px; color:var(--soft)}
.nowbook .lifts a{
  display:inline; margin:0; padding:0; border-radius:0;
  background:transparent; color:var(--open); border:0; font-weight:650; font-size:12.5px;
  text-decoration:underline; text-underline-offset:3px;
}
.take{
  padding:0; border:0; background:transparent; box-shadow:none; border-radius:0;
}
.take::before{content:none}
.take .head{
  display:flex; gap:10px; align-items:baseline; flex-wrap:wrap;
  padding:0; text-decoration:none;
}
.take .head:hover{color:inherit}
.take .head:hover b{color:var(--accent)}
.take .head .id{
  font-family:var(--fm); font-size:12px; color:var(--open); background:transparent;
  border:0; border-radius:0; padding:0; flex:none;
}
.take .head b{font-family:var(--fd); font-size:20px; font-weight:750; letter-spacing:-.025em}
.take .why{
  margin:6px 0 0; font-size:13px; color:var(--soft); font-weight:550;
}
.take .step{
  margin-top:14px; padding:14px 16px; border-radius:var(--r);
  background:var(--surface); border:1px solid var(--line-strong);
  box-shadow:var(--shadow); width:100%; box-sizing:border-box;
}
.take .step-lbl{
  display:block; font-size:11px; font-weight:700; letter-spacing:.04em;
  text-transform:uppercase; color:var(--faint); margin:0 0 8px;
}
.take .step p{
  margin:0; font-size:15px; line-height:1.55; max-width:none; color:var(--ink);
  text-wrap:pretty;
}
.take .theory{
  margin:12px 0 0; font-size:13.5px; color:var(--soft); line-height:1.5;
}
.take .theory a{
  color:var(--open); font-weight:650;
  text-decoration:underline; text-underline-offset:3px;
}
.take .theory-list{
  margin-top:12px; display:flex; flex-direction:column; gap:8px; width:100%;
}
.take .theory-list > .step-lbl{margin:0 0 2px}
.take .take-skill{
  margin-top:14px; padding:14px 16px; border-radius:var(--r);
  background:var(--surface); border:1px solid var(--line-strong);
  box-shadow:var(--shadow); width:100%; box-sizing:border-box;
}
.take .take-skill:first-of-type{margin-top:0}
.take .take-skill .head{margin:0}
.take .take-skill .why{margin:4px 0 0}
.take .take-skill .step{
  margin-top:12px; padding:12px 14px;
  background:color-mix(in srgb,var(--bg) 70%, var(--surface));
  border:1px solid var(--line); box-shadow:none;
}
.take .theory-book{
  display:flex; gap:12px; align-items:center; margin-top:12px;
  padding:0; border:0; border-radius:0; background:transparent;
  box-shadow:none; width:100%; box-sizing:border-box; max-width:none;
  text-decoration:none; color:inherit;
}
.take .theory-book:hover{color:inherit}
.take .theory-book:hover .tb-title{color:var(--accent)}
.take .theory-book img,
.take .theory-book .nc{
  width:36px; height:52px; object-fit:cover; border-radius:5px; flex:none;
}
.take .theory-book .nc{
  display:grid; place-items:center; background:var(--open-bg); border:1px solid var(--open-border);
  font-family:var(--fd); font-size:15px; font-weight:700; color:var(--open);
}
.take .theory-book .tb-lbl{
  display:block; font-size:11px; font-weight:700; letter-spacing:.04em;
  text-transform:uppercase; color:var(--faint); margin:0 0 4px;
}
.take .theory-book .tb-title{display:block; font-size:14px; font-weight:650; letter-spacing:-.01em}
.take .theory-book .tb-meta{display:block; font-size:12.5px; color:var(--soft); margin-top:2px}
.take .take-skill .theory{margin-top:12px}
.take .alt{margin-top:14px; font-size:13px; color:var(--soft)}
.take .alt a{color:var(--open); font-weight:650}
:root[data-theme="dark"] .take .step,
:root[data-theme="dark"] .take .take-skill{box-shadow:none}
.who{
  margin:14px 0 0; font-size:14.5px; max-width:60ch; padding:12px 14px 12px 16px;
  background:var(--surface); border:1px solid var(--line); border-radius:var(--r);
  border-left:3px solid var(--proved); box-shadow:var(--shadow);
}
.gau .steps{margin-top:11px}
.ladder{
  display:flex; flex-direction:column; gap:0; background:var(--surface);
  border:1px solid var(--line); border-radius:var(--r); overflow:hidden; box-shadow:var(--shadow);
}
.rung{
  display:grid; grid-template-columns:168px 1fr; gap:16px; padding:12px 16px;
  border-radius:0; align-items:baseline; background:transparent; border:0; border-bottom:1px solid var(--line);
}
.rung:last-child{border-bottom:0}
.rung .lb{
  font-size:11px; font-weight:700; color:var(--faint); font-family:var(--fm);
  background:var(--shut-bg); border:1px solid var(--line); border-radius:var(--chip);
  padding:4px 9px; justify-self:start;
}
.rung .tx{font-size:14.5px; max-width:62ch; text-wrap:pretty}
.rung.reached{background:var(--proved-bg)}
.rung.reached .lb{color:var(--proved); background:var(--chip-face); border-color:var(--proved-border)}
.rung.goal .lb::after{content:" · цель"}
.cards{
  display:grid; grid-template-columns:repeat(auto-fill, minmax(min(100%, 240px), 1fr));
  gap:12px;
}
.mini{
  display:block; min-width:0; padding:12px 13px; border-radius:var(--r-sm); background:var(--surface);
  border:1px solid var(--line); box-shadow:var(--shadow);
  transition:border-color .15s var(--ease), box-shadow .15s var(--ease);
}
.mini:hover{border-color:var(--open-border); color:inherit; box-shadow:var(--shadow-lg)}
.mini .id{
  display:inline; font-family:var(--fm); font-size:12px; color:var(--faint);
  background:transparent; border:0; border-radius:0; padding:0;
}
.mini .nm{font-size:14px; font-weight:650; margin-top:8px; overflow-wrap:anywhere}
.mini .cond{font-family:var(--fm); font-size:11px; margin-top:8px; color:var(--faint)}
.mini.done{background:var(--proved-bg); border-color:var(--proved-border)}
.mini.done .id,.mini.done .cond{color:var(--proved); border:0; background:transparent}
.brow{
  display:grid; grid-template-columns:32px 36px 1fr auto; gap:8px 10px; align-items:center;
  padding:7px 12px; background:transparent; border:0; border-bottom:1px solid var(--line);
  border-radius:0; margin:0; transition:background .12s var(--ease);
}
.rows > .brow:last-child{border-bottom:0}
.brow:hover{background:color-mix(in srgb,var(--bg) 80%, var(--surface)); color:inherit}
/* Книги вне .rows — отдельные карточки, не порванная лента */
.sect > .brow, .split .brow, p + .brow, .empty + .brow{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--r-sm);
  margin-bottom:8px; box-shadow:var(--shadow);
}
.sect > .brow:hover, .split .brow:hover{border-color:var(--open-border); box-shadow:var(--shadow-lg)}
.brow .d{
  font-family:var(--fm); font-size:12px; font-weight:650; color:var(--proved);
  background:transparent; border:0; border-radius:0; text-align:left; padding:0; min-width:0;
}
.brow .t{font-size:13.5px; font-weight:650; text-wrap:pretty; line-height:1.35}
.brow .t em{display:block; font-style:normal; font-size:12px; color:var(--soft); font-weight:500; margin-top:1px}
.brow .m{
  font-size:12px; font-weight:550; color:var(--faint); text-align:right;
  background:transparent; border:0; border-radius:0; padding:0; justify-self:end; white-space:nowrap;
}
.cov-s{
  width:24px; height:34px; object-fit:cover; border-radius:4px; background:var(--line);
  display:flex; align-items:center; justify-content:center; font-size:11px; color:var(--faint);
  font-family:var(--fd);
}
.cov-l{
  width:128px; height:184px; object-fit:cover; border-radius:12px; background:var(--line);
  display:flex; align-items:center; justify-content:center; font-size:40px; color:var(--faint);
  font-family:var(--fd); flex:none; box-shadow:var(--shadow-lg);
}
.cov-l.ph,.cov-s.ph{border:1px solid var(--line)}
.bk-head{display:flex; gap:22px; align-items:flex-start}
.bk-head>div{min-width:0}
.facts{margin:10px 0 0; font-family:var(--fm); font-size:12px; color:var(--faint); line-height:1.5}
.bk-about{margin:24px 0 0; font-size:15px; line-height:1.65; max-width:68ch; text-wrap:pretty; color:var(--ink)}
.ev{
  padding:14px 15px; border-radius:var(--r-sm); background:var(--proved-bg); margin-bottom:8px;
  border:1px solid var(--proved-border);
}
.ev .res{font-size:14.5px; text-wrap:pretty}
.ev .meta{font-family:var(--fm); font-size:11px; color:var(--soft); margin-top:8px}
.empty{
  padding:14px 15px; border-radius:var(--r); border:1px dashed var(--line-strong);
  font-size:14px; color:var(--soft); max-width:66ch; background:var(--surface); text-wrap:pretty;
}
.bar{
  display:flex; flex-wrap:wrap; gap:4px; margin:0 0 14px; padding:4px; width:fit-content;
  max-width:100%; background:var(--surface); border:1px solid var(--line); border-radius:var(--chip);
  box-shadow:var(--shadow);
}
.bar button{
  font:inherit; font-size:12.5px; font-weight:650; padding:7px 12px; border-radius:var(--chip);
  cursor:pointer; color:var(--soft); background:transparent; border:1px solid transparent;
  transition:background .15s var(--ease), color .15s var(--ease);
}
.bar button:hover{color:var(--ink); background:var(--bg)}
.bar button.on{
  color:var(--ink); background:var(--bg); border-color:var(--line); box-shadow:var(--shadow);
}
@media (min-width:920px){
  .split{display:grid; grid-template-columns:1fr 250px; gap:28px; align-items:start}
}
.rail{
  font-size:13px; color:var(--soft); background:var(--surface); border:1px solid var(--line);
  border-radius:var(--r); padding:14px 15px; box-shadow:var(--shadow); position:sticky; top:72px;
}
.rail dl{margin:0; display:grid; gap:12px}
.rail dt{
  font-size:10px; letter-spacing:.1em; text-transform:uppercase; color:var(--faint); font-weight:700;
}
.rail dd{margin:4px 0 0; color:var(--ink); font-size:13.5px; font-weight:650}
.rail dd.path{font-family:var(--fm); font-size:11.5px; font-weight:500; overflow-wrap:anywhere}
.foot{
  margin-top:52px; padding-top:16px; border-top:1px solid var(--line); font-size:12px;
  color:var(--faint); font-family:var(--fm); line-height:1.7;
}
.more{margin-top:14px}
.more button{
  font:inherit; font-size:13px; font-weight:650; color:var(--accent); background:var(--surface);
  border:1px solid var(--line); border-radius:var(--chip); cursor:pointer; padding:8px 14px;
  box-shadow:var(--shadow); transition:border-color .15s var(--ease), box-shadow .15s var(--ease);
}
.more button:hover{border-color:var(--open-border); box-shadow:var(--shadow-lg)}
@media (max-width:760px){
  .stats{flex-direction:column}
  .stats.stats-4{grid-template-columns:repeat(2,minmax(0,1fr))}
  .stat{width:auto}
  .gauge{flex-direction:column}
  .gau{width:auto}
  .row{grid-template-columns:56px 1fr; gap:8px 12px}
  .row .steps,.row .st{display:none}
  .brow{grid-template-columns:28px 34px 1fr}
  .brow .m{display:none}
  .rung{grid-template-columns:1fr}
  .nav-sub{margin-left:0; width:100%}
}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important; animation:none!important}
}
"""

PAGE = """<!doctype html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Развитие · вид B</title>
<script>
(function () {{
  try {{
    var t = localStorage.getItem("wh-atlas-b-theme");
    if (!t) t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", t);
  }} catch (e) {{}}
}})();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Plus+Jakarta+Sans:ital,wght@0,400;0,500;0,600;0,700;0,800;1,400&display=swap" rel="stylesheet">
<style>{style}</style>
</head>
<body>
<div class="top"><div class="topin">
  <div class="mark">Развитие <span class="pill">вид B</span></div>
  <nav class="nav">
    <a id="nav-home" href="#/">Сегодня</a>
    <a id="nav-build" href="#/build">Профиль</a>
  </nav>
  <nav class="nav-sub" aria-label="Каталог">
    <a id="nav-map" href="#/map">Карта</a>
    <span class="sep">·</span>
    <a id="nav-skills" href="#/skills">Навыки</a>
    <span class="sep">·</span>
    <a id="nav-books" href="#/books">Ресурсы</a>
  </nav>
  <!-- заглушка: основа SCRIPT пишет в #nav-tree; дерево сведено в Навыки -->
  <a id="nav-tree" href="#/skills" hidden aria-hidden="true"></a>
  <span class="grow"></span>
  <div class="find">
    <input id="q" type="search" placeholder="навык, книга, автор" autocomplete="off">
    <div id="pop" class="pop" style="display:none"></div>
  </div>
  <button id="theme" class="theme" type="button">Тёмная</button>
</div></div>
<div class="wrap">
  <div id="page"></div>
  <div class="foot">{foot}</div>
</div>
<script>window.WH = {data};</script>
<script>{script}</script>
<script>{override}</script>
<script>
(function () {{
  const btn = document.getElementById("theme");
  if (!btn) return;
  const sync = () => {{
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    btn.textContent = dark ? "Светлая" : "Тёмная";
  }};
  sync();
  btn.addEventListener("click", () => {{
    setTimeout(() => {{
      try {{
        localStorage.setItem(
          "wh-atlas-b-theme",
          document.documentElement.getAttribute("data-theme") || "light"
        );
      }} catch (e) {{}}
      sync();
    }}, 0);
  }});
}})();
</script>
</body>
</html>
"""

# Поведение вида B поверх SCRIPT основы: узкий home, метрики дисциплины, навигация.
OVERRIDE = r"""
function home() {
  const build = (W.builds || []).filter((x) => x.status === "active")[0];
  const weight = {};
  if (build) build.rows.forEach((r) => { weight[r.id] = r.weight; });
  const open = Object.values(S)
    .filter((s) => s.state === "открыт" || s.state === "доказан")
    .sort((a, b) =>
      (weight[b.id] || 0) - (weight[a.id] || 0) ||
      (a.layer || 0) - (b.layer || 0) ||
      a.target - b.target || a.id.localeCompare(b.id));
  const ranked = open.slice().sort((a, b) =>
    (weight[b.id] || 0) - (weight[a.id] || 0) ||
    b.opens.length - a.opens.length || a.id.localeCompare(b.id));
  const pick = ranked[0];
  const now = Object.values(B).filter((x) => x.status === "reading");
  const mastery = build ? Math.round(build.mastery * 100) : 0;
  const theoryPct = build ? Math.round(build.theory * 100) : 0;
  const role = build
    ? '<a class="rolebar' + (mastery === 0 ? " cold" : "") + '" href="#/build"><div><b>' +
      esc(build.title) + "</b><span>" +
      plural(build.rows.length, "требование", "требования", "требований") +
      " · владение " + mastery + "% · теория " + theoryPct + "%</span></div>" +
      "<em>" + (mastery === 0
        ? "Сейчас ноль. Роль поднимется только доказанной работой — не чтением."
        : "Владение растёт последним: когда основание уже собрано.") + "</em></a>"
    : "";
  const nowBlock = now.length
    ? now.map((b) => {
        const top = b.skills.filter((m) => S[m[0]]).slice(0, 3)
          .map((m) => '<a href="#/skill/' + m[0].toLowerCase() + '">' + m[0] + " " +
            esc(S[m[0]].ru) + "</a>").join(" · ");
        return '<div class="nowbook">' +
          '<a class="cv" href="#/reading/' + b.slug + '">' +
          (b.cover ? '<img src="data:image/jpeg;base64,' + b.cover + '" alt="">'
                   : '<span class="nc">' + esc((b.title || "?").slice(0, 1)) + "</span>") +
          "</a><div>" +
          '<a class="ttl" href="#/reading/' + b.slug + '">' + esc(b.title) + "</a>" +
          "<span>" + esc(b.author || "") + "</span>" +
          (top ? '<span class="lifts">поднимает: ' + top + "</span>" : "") +
          "</div></div>";
      }).join("")
    : '<p class="note">Сейчас ничего не читается.</p>';
  let takeBlock = '<p class="note">Открытых навыков нет: сначала нужны доказательства по тем, что стоят перед ними.</p>';
  if (pick) {
    const picks = ranked.slice(0, 5);
    const bookFor = (skill) => {
      const marks = (skill.books || []).filter((x) => {
        const b = B[x[0]];
        if (!b) return false;
        if (b.status === "read") return false;
        if (x[1] <= skill.known) return false;
        return !!(b.owned || b.set);
      }).sort((a, b) => {
        const ba = B[a[0]], bb = B[b[0]];
        return (bb.owned | 0) - (ba.owned | 0) || (bb.set | 0) - (ba.set | 0) ||
          ((bb.cover ? 1 : 0) - (ba.cover ? 1 : 0)) || b[1] - a[1];
      });
      return marks[0] || null;
    };
    const bookCard = (skill, mark) => {
      if (!mark) {
        return '<p class="theory">Доступной неосвоенной книги нет (нужны «на руках» или набор).</p>';
      }
      const res = B[mark[0]], depth = mark[1];
      const where = res.owned ? "на руках" : "в наборе";
      const cover = res.cover
        ? '<img src="data:image/jpeg;base64,' + res.cover + '" alt="">'
        : '<span class="nc">' + esc((res.title || "?").slice(0, 1)) + "</span>";
      return '<a class="theory-book" href="#/reading/' + res.slug + '">' + cover +
        "<div><span class=\"tb-lbl\">книга · " + where + " · " + skill.known +
        " → L" + depth + "</span>" +
        '<span class="tb-title">' + esc(res.title) + "</span>" +
        (res.author ? '<span class="tb-meta">' + esc(res.author) + "</span>" : "") +
        "</div></a>";
    };
    takeBlock = '<div class="take">' + picks.map((skill, i) => {
      const why = weight[skill.id]
        ? "требование роли · сейчас доступно"
        : "открывает следующих: " + skill.opens.length;
      const step = (i === 0 && skill.first)
        ? '<div class="step"><span class="step-lbl">Шаг</span><p>' +
          esc(skill.first) + "</p></div>"
        : "";
      return '<div class="take-skill">' +
        '<a class="head" href="#/skill/' + skill.id.toLowerCase() +
        '"><span class="id">' + skill.id + "</span><b>" + esc(skill.ru) + "</b></a>" +
        '<p class="why">' + why + "</p>" +
        step + bookCard(skill, bookFor(skill)) + "</div>";
    }).join("") + "</div>";
  }
  const moreOpen = Math.max(0, open.length - Math.min(5, ranked.length));
  return '<div class="crumb"><span>сегодня</span></div>' +
    "<h1>Что брать</h1>" +
    (W.whoami ? '<p class="who">' + esc(W.whoami) + "</p>" : "") +
    role +
    section("Читаю", now.length ? "" : "пусто", nowBlock) +
    section("Взять сейчас", pick ? ("до " + Math.min(5, ranked.length)) : "", takeBlock) +
    (moreOpen
      ? '<p class="meta-line">Ещё <b>' + moreOpen + "</b> открыто — " +
        '<a href="#/skills">список</a> · <a href="#/map">карта</a></p>'
      : "");
}

function domainPage(key) {
  const d = D[key];
  if (!d) return lost("Дисциплины «" + key + "» в складе нет.");
  const rows = d.skills.map((x) => skillRow(S[x])).join("");
  const books = d.books.map((x) => {
    const b = B[x];
    if (!b) return "";
    const own = b.skills.filter((m) => d.skills.indexOf(m[0]) >= 0);
    return bookRow(b, own.length ? Math.max.apply(null, own.map((m) => m[1])) : 0);
  });
  const ownedHere = d.books.map((x) => B[x]).filter((b) => b && b.owned).length;
  return '<div class="crumb"><a href="#/">сегодня</a><span>/</span>' +
    '<a href="#/map">карта</a><span>/</span><span>дисциплина</span></div><h1>' +
    esc(d.ru) + '</h1><p class="lede">' + esc(d.about) + "</p>" +
    '<div class="stats">' +
    '<div class="stat p"><b>' + d.proved + "</b><span>доказано</span></div>" +
    '<div class="stat o"><b>' + d.open + "</b><span>открыто</span></div>" +
    '<div class="stat"><b>' + d.shut + "</b><span>закрыто</span></div></div>" +
    '<p class="meta-line">Книги вторичны: поднимают <b>' + d.books.length +
    "</b>, на руках <b>" + ownedHere + "</b>" +
    (d.books.length ? ' · <a href="#books-here">к списку</a>' : "") + "</p>" +
    (d.composition.items.length ? section("Из чего состоит",
      plural(d.composition.items.length, "навык", "навыка", "навыков"),
      (d.composition.intro ? '<p class="note" style="margin-bottom:14px">' +
        esc(d.composition.intro) + "</p>" : "") +
      '<ol class="comp">' + d.composition.items.map((x) => "<li><div>" +
        (x.id
          ? '<a class="comp-t" href="#/skill/' + x.id.toLowerCase() + '">' +
            esc(x.title) + "</a>" + (x.note ? '<span class="chip">' + esc(x.note) +
            "</span>" : "")
          : "") +
        '</div><p class="comp-d">' + esc(x.desc) + "</p></li>").join("") +
      "</ol>") : "") +
    (d.order_hint ? section("В каком порядке брать", "", '<p class="note">' +
      linkCodes(d.order_hint) + "</p>") : "") +
    section("Навыки", plural(d.skills.length, "навык", "навыка", "навыков"),
      '<div class="rows">' + rows + "</div>") +
    (d.guests.length ? section("Нужны и здесь",
      plural(d.guests.length, "навык", "навыка", "навыков"),
      '<p class="note">Эти навыки живут в других дисциплинах, но без них здесь ' +
      "не обойтись.</p><div class=\"rows\" style=\"margin-top:14px\">" +
      d.guests.map((x) => skillRow(S[x])).join("") + "</div>") : "") +
    '<div id="books-here">' +
    section("Чем поднимать", plural(d.books.length, "книга", "книги", "книг"),
      '<div class="rows">' + capped("dom-" + key, books, (x) => x) + "</div>") +
    "</div>" +
    (d.closure ? section("Чем закрывается", "", '<p class="note">' +
      linkCodes(d.closure) + "</p>") : "");
}

function mapPage() {
  const tiles = Object.values(D).map((d) => {
    let cells = "";
    for (let i = 0; i < d.proved; i++) cells += '<i class="p"></i>';
    for (let i = 0; i < d.open; i++) cells += '<i class="o"></i>';
    for (let i = 0; i < d.shut; i++) cells += '<i class="s"></i>';
    return '<a class="tile" href="#/domain/' + d.key + '"><h3>' + esc(d.ru) +
      "</h3><p>" + esc(d.teaser || d.about) + '</p><div class="meter">' + cells +
      '</div><div class="foot">' +
      plural(d.skills.length, "навык", "навыка", "навыков") +
      " · доказано " + d.proved + " · открыто " + d.open + "</div></a>";
  }).join("");
  return '<div class="crumb"><a href="#/">сегодня</a><span>/</span>' +
    "<span>карта</span></div><h1>Карта</h1>" +
    '<p class="lede">Дисциплины — единственный обзор «где я». Список и слои предпосылок — ' +
    'на <a href="#/skills">Навыках</a>. Владение — только от сделанной работы.</p>' +
    '<div class="stats">' +
    '<div class="stat p"><b>' + T.proved + "</b><span>доказано</span></div>" +
    '<div class="stat o"><b>' + T.open + "</b><span>открыто</span></div>" +
    '<div class="stat"><b>' + T.shut + "</b><span>закрыто</span></div></div>" +
    '<p class="meta-line">Каталог: <b>' + T.set + "</b> в наборе · <b>" + T.owned +
    "</b> на руках · прочитано <b>" + T.read + "</b></p>" +
    section("Дисциплины", plural(Object.keys(D).length, "дисциплина", "дисциплины",
      "дисциплин"), '<div class="tiles">' + tiles + "</div>");
}

let skView = "список";

function skillsLayers() {
  const all = Object.values(S);
  const maxLayer = Math.max.apply(null, all.map((s) => s.layer));
  const rows = [];
  for (let lv = 0; lv <= maxLayer; lv++) {
    const ord = (s) => (D[s.domains[0]] || {}).order || 99;
    const here = all.filter((s) => s.layer === lv)
      .sort((a, b) => b.opens.length - a.opens.length ||
        ord(a) - ord(b) || a.id.localeCompare(b.id));
    if (!here.length) continue;
    const proved = here.filter((s) => s.state === "доказан").length;
    const open = here.filter((s) => s.state === "открыт").length;
    const cards = here.map((s) =>
      '<a class="' + stateCls("tnode", s.state) + '" href="#/skill/' +
      s.id.toLowerCase() + '"><span class="tid">' + s.id + "</span>" +
      '<span class="tnm">' + esc(s.ru) + "</span>" +
      '<span class="tdom">' + esc(s.domains.map(domRu)[0] || "") + "</span></a>").join("");
    rows.push('<div class="tlayer"><div class="thead"><span class="tlv">' +
      (lv === 0 ? "начало" : "шаг " + lv) + '</span><span class="tcnt">' +
      plural(here.length, "навык", "навыка", "навыков") +
      (proved ? " · доказано " + proved : "") +
      (open ? " · открыто " + open : "") + '</span></div>' +
      '<div class="tnodes">' + cards + "</div></div>");
  }
  return '<div class="tree">' + rows.join("") + "</div>";
}

function skillsPage() {
  const all = Object.values(S);
  const counts = {
    "все": all.length,
    "доказан": all.filter((s) => s.state === "доказан").length,
    "открыт": all.filter((s) => s.state === "открыт").length,
    "закрыт": all.filter((s) => s.state === "закрыт").length,
  };
  const views = [["список", "список"], ["слои", "слои"]].map((pair) =>
    '<button data-sk="' + pair[0] + '"' + (skView === pair[0] ? ' class="on"' : "") +
    ">" + pair[1] + "</button>").join("");
  const bar = ["все", "доказан", "открыт", "закрыт"].map((k) =>
    '<button data-sf="' + k + '"' + (sf === k ? ' class="on"' : "") + ">" + k + " · " +
    counts[k] + "</button>").join("");
  const ordS = (s) => (D[s.domains[0]] || {}).order || 99;
  const filtered = all.filter((s) => sf === "все" || s.state === sf)
    .sort((a, b) => a.layer - b.layer || b.opens.length - a.opens.length ||
      ordS(a) - ordS(b) || a.id.localeCompare(b.id));
  const body = skView === "слои"
    ? skillsLayers()
    : '<div class="rows">' + capped("skills-" + sf, filtered, (s) => skillRow(s), 40) +
      "</div>";
  return '<div class="crumb"><a href="#/">сегодня</a><span>/</span>' +
    "<span>навыки</span></div><h1>Навыки</h1>" +
    '<p class="lede">Список — фильтр и поиск глазами. Слои — сколько ступеней до навыка. ' +
    'Дисциплины целиком — на <a href="#/map">Карте</a>.</p>' +
    '<div class="sect"><div class="bar">' + views + '</div>' +
    (skView === "список" ? '<div class="bar">' + bar + "</div>" : "") +
    body + "</div>";
}

function treePage() {
  skView = "слои";
  return skillsPage();
}

document.addEventListener("click", (e) => {
  const v = e.target.closest("[data-sk]");
  if (!v) return;
  skView = v.dataset.sk;
  draw();
});

const _draw = draw;
draw = function () {
  _draw();
  const r = route();
  const homeNav = document.getElementById("nav-home");
  if (homeNav) homeNav.className = r.view === "home" ? "on" : "";
  ["map", "build", "skills", "books"].forEach((id) => {
    const el = document.getElementById("nav-" + id);
    if (!el) return;
    const on = r.view === id ||
      (id === "map" && r.view === "domain") ||
      (id === "skills" && r.view === "tree");
    el.className = on ? "on" : "";
  });
};
draw();
"""


def home_cover_slugs(data: dict) -> set[str]:
    """Обложки только для Home: чтение сейчас и первые доступные книги топ-навыков."""
    books = data.get("books") or {}
    skills = data.get("skills") or {}
    keep = {
        slug for slug, book in books.items()
        if book.get("status") == "reading" and book.get("cover")
    }
    weight: dict[str, float] = {}
    active = next(
        (b for b in (data.get("builds") or []) if b.get("status") == "active"),
        None,
    )
    if active:
        for row in active.get("rows") or []:
            weight[str(row["id"])] = float(row.get("weight") or 0)
    open_skills = [
        s for s in skills.values()
        if s.get("state") in ("открыт", "доказан")
    ]
    open_skills.sort(
        key=lambda s: (
            -(weight.get(s["id"]) or 0),
            -len(s.get("opens") or []),
            s["id"],
        )
    )
    for skill in open_skills[:5]:
        marks = []
        for slug, depth in skill.get("books") or []:
            book = books.get(slug)
            if not book or not book.get("cover"):
                continue
            if book.get("status") == "read":
                continue
            if int(depth) <= int(skill.get("known") or 0):
                continue
            if not (book.get("owned") or book.get("set")):
                continue
            marks.append((slug, int(depth)))
        marks.sort(
            key=lambda item: (
                -(1 if books[item[0]].get("owned") else 0),
                -(1 if books[item[0]].get("set") else 0),
                -item[1],
            )
        )
        if marks:
            keep.add(marks[0][0])
    return keep


def lighten(data: dict) -> dict:
    """Убирает обложки из каталога, оставляет на Home — иначе страница ~3 МБ."""
    keep = home_cover_slugs(data)
    payload = dict(data)
    books = {}
    for slug, book in (data.get("books") or {}).items():
        item = dict(book)
        if slug not in keep:
            item.pop("cover", None)
        books[slug] = item
    payload["books"] = books
    return payload


def render(data: dict, now: dt.datetime) -> str:
    foot = f"вид B · {now:%d.%m.%Y} · обложки на сегодня · основа не изменена"
    payload = lighten(data)
    payload["kindRu"] = KIND_RU
    payload["statusRu"] = STATUS_RU
    payload["nodeRu"] = NODE_RU
    return PAGE.format(
        style=STYLE,
        foot=html.escape(foot),
        script=SCRIPT,
        override=OVERRIDE,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .replace("</", "<\\/"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, help="куда положить страницу")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = args.root / "config" / "attention.yml"
    config = {}
    if settings.exists():
        config = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
    gate = int((config.get("skills") or {}).get("gate", 3))

    data = collect(args.root, gate)
    t = data["totals"]
    if not data["skills"] or not data["books"]:
        print(f"АТЛАС-B: ВАКУУМ — навыков {t['skills']}, ресурсов {t['catalog']}.")
        return 1

    page = render(data, dt.datetime.now().replace(microsecond=0))
    if args.dry_run or not args.out:
        print(f"атлас-b: навыков {t['skills']} (доказано {t['proved']}, открыто {t['open']}), "
              f"вес {len(page.encode('utf-8')) // 1024} КБ")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(f"атлас-b: {args.out} — навыков {t['skills']} (доказано {t['proved']}), "
          f"вес {len(page.encode('utf-8')) // 1024} КБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
