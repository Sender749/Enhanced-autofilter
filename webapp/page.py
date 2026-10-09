"""The Mini App page (HTML/CSS/JS), served by webapp.api. Kept in Python so updates are a plain .py swap."""

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Movies</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#0e0e10;--s1:#1a1a1d;--s2:#26262a;--tx:#f1f1f1;--mut:#8c8c92;--acc:#e24b4a;--tg:#229ed9;--line:#2a2a2e}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
body{padding-top:env(safe-area-inset-top,0);padding-bottom:calc(64px + env(safe-area-inset-bottom,0))}
button{font:inherit;color:inherit;border:0;cursor:pointer}
.top{display:flex;align-items:center;justify-content:space-between;padding:12px}
.logo{font-size:18px;font-weight:600;color:var(--acc)}
.chips{display:flex;gap:8px;overflow-x:auto;padding:0 12px 10px;scrollbar-width:none}
.chip{flex:none;padding:6px 13px;border-radius:16px;background:var(--s2);color:#d8d8d8;font-size:13px}
.chip.on{background:var(--tx);color:var(--bg)}
.hero{position:relative;margin:0 12px;height:210px;border-radius:12px;overflow:hidden;background:#3c3489;background-size:cover;background-position:center}
.hero .sh{position:absolute;inset:0;background:linear-gradient(transparent 35%,rgba(0,0,0,.85))}
.hero .in{position:absolute;left:12px;right:12px;bottom:12px}
.hero h2{margin:2px 0 3px;font-size:19px;font-weight:600}
.meta{font-size:12px;color:#c9c9ce}
.btns{display:flex;gap:8px;margin-top:8px;align-items:center}
.btn{flex:none;display:inline-flex;align-items:center;justify-content:center;gap:5px;padding:6px 14px;border-radius:6px;font-weight:600;font-size:12px;text-decoration:none;color:inherit}.btn svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.b1{background:var(--tx);color:var(--bg)}.b2{background:rgba(255,255,255,.2)}.btg{background:var(--tg);color:#fff}
.h{font-size:15px;font-weight:600;margin:16px 12px 8px}.hh{display:flex;justify-content:space-between;align-items:baseline;margin:16px 12px 8px}.hh b{font-size:15px;font-weight:600}.hh a{font-size:12px;color:var(--mut);cursor:pointer;padding:2px 0 2px 10px}.pc img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.pc .tag,.pc .q{z-index:1}.sk .pc{animation:pl 1.1s ease-in-out infinite alternate}@keyframes pl{from{opacity:.45}to{opacity:1}}
.row{display:flex;gap:8px;overflow-x:auto;padding:0 12px 4px;scrollbar-width:none}
.card{flex:none;width:104px;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px 8px;padding:0 12px}
.grid .card{width:auto}
.pc{position:relative;aspect-ratio:2/3;border-radius:7px;overflow:hidden;background:var(--s2);display:flex;flex-direction:column;justify-content:flex-end;padding:7px}
.pc.fb .ini{position:absolute;top:8px;left:9px;font-size:34px;font-weight:700;opacity:.9;line-height:1}
.pc.fb .nm{font-size:12px;font-weight:600;line-height:1.25;word-break:break-word;color:#fff}
.tag{position:absolute;top:6px;left:6px;font-size:10px;padding:1px 5px;border-radius:3px;background:var(--acc);font-weight:600}
.q{position:absolute;top:6px;right:6px;font-size:10px;padding:1px 5px;border-radius:3px;background:rgba(0,0,0,.6)}
.ct{font-size:12px;margin:5px 1px 0;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nav{position:fixed;left:0;right:0;bottom:0;display:flex;justify-content:space-around;background:var(--bg);border-top:1px solid var(--line);padding:8px 0 calc(8px + env(safe-area-inset-bottom,0));z-index:5}
.nav button{background:none;display:flex;flex-direction:column;align-items:center;gap:2px;font-size:11px;color:var(--mut);padding:0 14px}
.nav button.on{color:var(--tx)}.nav svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.sb{margin:0 12px 10px;display:flex;gap:8px}
.sb input{flex:1;background:var(--s2);border:0;border-radius:8px;padding:11px 12px;color:var(--tx);font-size:15px;outline:none}
.muted{color:var(--mut);padding:0 12px 8px;font-size:12px}
.req{margin:14px 12px;border:1px dashed #4a4a50;border-radius:8px;padding:12px;text-align:center;color:#c9c9ce;cursor:pointer}
.bd{height:200px;background:#3c3489;background-size:cover;background-position:center;position:relative}
.bd .sh{position:absolute;inset:0;background:linear-gradient(transparent 50%,var(--bg))}
.pad{padding:0 12px}.pad h1{font-size:22px;margin:6px 0 4px;font-weight:600}
.ov{color:#c9c9ce;margin:10px 0}
.sc{display:flex;gap:8px;overflow-x:auto;margin:14px 0 6px;scrollbar-width:none}
.fr{display:flex;align-items:center;gap:8px;padding:10px 0;border-top:1px solid var(--line)}
.fr .t{flex:1;min-width:0}.fr .t b{font-weight:500;font-size:13px}.fr .t i{display:block;font-style:normal;font-size:11px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ic{width:34px;height:34px;border-radius:7px;background:var(--s2);display:flex;align-items:center;justify-content:center}
.ic svg{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.ep{border-top:1px solid var(--line);padding:11px 0;display:flex;justify-content:space-between;cursor:pointer}
.ep small{color:var(--mut)}
.back{position:absolute;top:12px;left:12px;width:36px;height:36px;border-radius:50%;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:2}
.pl{position:fixed;inset:0;background:#000;z-index:20;display:flex;flex-direction:column;padding-top:env(safe-area-inset-top,0)}
.pl video{width:100%;max-height:60vh;background:#000}
.pl .bar{display:flex;justify-content:space-between;align-items:center;padding:10px 12px}
.pl .bar span{font-size:12px;color:#aaa;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;max-width:75%}
.pl .alt{padding:12px;display:flex;flex-wrap:wrap;gap:8px}
.toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);background:#333;color:#fff;padding:9px 14px;border-radius:8px;z-index:30;max-width:85%;text-align:center}
.spin{text-align:center;color:var(--mut);padding:30px}
</style>
</head>
<body>
<div id="app"></div>
<nav class="nav" id="nav"></nav>
<script>
const tg = window.Telegram && Telegram.WebApp || {initData:"",ready(){},expand(){}};
tg.ready(); tg.expand && tg.expand();
try{tg.setHeaderColor("#0e0e10");tg.setBackgroundColor("#0e0e10")}catch(e){}
const $ = s => document.querySelector(s), app = $("#app");
const TMDB = "https://image.tmdb.org/t/p/";
const ICO = {
 home:'<path d="M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10"/>',search:'<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
 list:'<path d="M6 3h12v18l-6-4-6 4z"/>',fire:'<path d="M12 3c1 4 5 5 5 10a5 5 0 0 1-10 0c0-2 1-3 2-4 0 2 1 3 2 3 0-3-1-5 1-9z"/>',
 play:'<path d="M7 4l13 8-13 8z"/>',dl:'<path d="M12 4v11m-5-4l5 5 5-5M5 20h14"/>',tg:'<path d="M21 4L3 11l6 2 2 6 3-4 5 4z"/>',
 plus:'<path d="M12 5v14M5 12h14"/>',check:'<path d="M5 12l5 5 9-10"/>',back:'<path d="M15 5l-7 7 7 7"/>'};
const svg = n => `<svg viewBox="0 0 24 24">${ICO[n]}</svg>`;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = b => !b ? "" : b >= 1e9 ? (b/1e9).toFixed(1)+" GB" : Math.round(b/1e6)+" MB";
const COLORS = ["#3C3489","#0F6E56","#993C1D","#185FA5","#72243E","#854F0B","#0C447C","#085041","#534AB7","#A32D2D","#3B6D11"];
const colorOf = s => COLORS[[...s].reduce((a,c)=>a+c.charCodeAt(0),0) % COLORS.length];

async function api(path, body){
  const o = {headers:{"X-Init-Data": tg.initData || "", "Content-Type":"application/json"}};
  if (body !== undefined){ o.method = "POST"; o.body = JSON.stringify(body); }
  const r = await fetch(path, o), j = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(j.error === "auth" ? "Open this app from the bot." : (j.error || "Something went wrong"));
  return j;
}
function toast(m){ const t=document.createElement("div"); t.className="toast"; t.textContent=m; document.body.append(t); setTimeout(()=>t.remove(),2800); }

function card(t){
  const fb = t.poster ? "" : `<span class="ini">${esc((t.name||"?")[0].toUpperCase())}</span><span class="nm">${esc(t.name)}</span>`;
  const img = t.poster ? `<img loading="lazy" decoding="async" alt="" src="${TMDB}w185${t.poster}" srcset="${TMDB}w185${t.poster} 1x,${TMDB}w342${t.poster} 2x">` : "";
  const st = t.poster ? "" : `style="background:${colorOf(t.name)}"`;
  return `<div class="card" data-id="${esc(t.id)}"><div class="pc ${t.poster?"":"fb"}" ${st}>${img}${t.new?'<span class="tag">NEW</span>':""}${t.quality?`<span class="q">${esc(t.quality)}</span>`:""}${fb}</div><div class="ct">${esc(t.name)}${t.year?` (${esc(t.year)})`:""}</div></div>`;
}
const skel = () => `<div class="sk"><div class="h" style="opacity:.0">.</div><div class="row">${Array(5).fill('<div class="card"><div class="pc"></div></div>').join("")}</div></div>`;
app.addEventListener("click", e => { const c = e.target.closest(".card"); if (c) openTitle(c.dataset.id); });

/* ── navigation ── */
let stack = [], tab = "home", homeData = null;
const NAV = [["home","Home","home"],["search","Search","search"],["list","My list","list"],["trend","Trending","fire"]];
function renderNav(){ $("#nav").innerHTML = NAV.map(([k,l,i])=>`<button data-k="${k}" class="${tab===k?"on":""}">${svg(i)}${l}</button>`).join(""); }
$("#nav").onclick = e => { const b=e.target.closest("button"); if(b){ stack=[]; go(b.dataset.k); } };
function go(k, arg){ tab = k; renderNav(); tg.BackButton && tg.BackButton.hide(); window.scrollTo(0,0);
  ({home:viewHome,search:viewSearch,list:viewList,trend:viewTrend})[k](arg); }
function pushView(fn){ stack.push(fn); tg.BackButton && tg.BackButton.show(); window.scrollTo(0,0); fn(); }
function back(){ stack.pop(); if (stack.length){ stack[stack.length-1](); } else { go(tab); } }
tg.BackButton && tg.BackButton.onClick(back);

/* ── home ── */
let heroIdx = 0, heroTimer;
const CHIPS = [["All",null],["Movies",{type:"movie"}],["Series",{type:"series"}],["Anime",{type:"anime"}],["Hindi",{lang:"hindi"}],["Punjabi",{lang:"punjabi"}]];
function paintHome(d){
  homeData = d;
  app.innerHTML = `<div class="top"><span class="logo">MoviesHub</span></div>
   <div class="chips">${CHIPS.map(([l],i)=>`<span class="chip ${i?"":"on"}" data-i="${i}">${l}</span>`).join("")}</div>
   <div id="hero"></div>${d.rows.map(r=>`<div class="hh"><b>${esc(r.title)}</b><a data-cat="${esc(r.key)}" data-t="${esc(r.title)}">View all ›</a></div><div class="row">${r.items.map(card).join("")}</div>`).join("")}`;
  document.querySelector(".chips").onclick = e => { const c=e.target.closest(".chip"); if(c && +c.dataset.i){ stack=[]; tab="search"; renderNav(); viewSearch(CHIPS[c.dataset.i][1]); } };
  heroIdx = 0; drawHero(); clearInterval(heroTimer);
  if (d.hero.length > 1) heroTimer = setInterval(()=>{ if(tab==="home" && !stack.length){ heroIdx=(heroIdx+1)%homeData.hero.length; drawHero(); } }, 6000);
}
async function viewHome(){
  let cached = null; try { cached = JSON.parse(localStorage.getItem("home_v2")); } catch(e){}
  if (cached) paintHome(cached);
  else app.innerHTML = `<div class="top"><span class="logo">MoviesHub</span></div>${skel()}${skel()}${skel()}`;
  try {
    const d = await api("/api/home");
    try { localStorage.setItem("home_v2", JSON.stringify(d)); } catch(e){}
    if (tab === "home" && !stack.length && JSON.stringify(d) !== JSON.stringify(cached)) paintHome(d);
  } catch(e){ if(!cached) app.innerHTML = `<div class="spin">${esc(e.message)}</div>`; }
}
function drawHero(){
  const h = homeData.hero[heroIdx], el = $("#hero"); if(!h || !el) return;
  el.innerHTML = `<div class="hero" style="background-image:url(${TMDB}w500${h.backdrop})" data-id="${esc(h.id)}"><div class="sh"></div><div class="in">
   <div class="meta">${esc(h.kind)} · ${esc(h.year||"")} ${h.quality?"· "+esc(h.quality):""}</div><h2>${esc(h.name)}</h2>
   <div class="meta">${h.rating?"★ "+h.rating+" · ":""}${esc((h.genres||[]).join(" · "))}</div>
   <div class="btns"><button class="btn b1" data-a="open">${svg("play")} Play</button><button class="btn b2" data-a="list">${svg("plus")} My list</button></div></div></div>`;
  el.onclick = async e => { const b = e.target.closest("button"); if(!b && !e.target.closest(".hero")) return;
    if (b && b.dataset.a === "list"){ await toggleList(h.id); toast("Added to My list"); } else openTitle(h.id); };
}

app.addEventListener("click", e => { const a = e.target.closest("a[data-cat]"); if(a) pushView(() => viewCat(a.dataset.cat, a.dataset.t)); });
let CAT = {key:"",page:0,busy:false};
async function viewCat(key, title){
  CAT = {key,page:0,busy:false};
  app.innerHTML = `<div class="top"><span class="logo">${esc(title)}</span></div><div class="grid" id="res"></div><div id="more"></div>`;
  loadCat();
}
async function loadCat(){
  if (CAT.busy) return; CAT.busy = true;
  try {
    const r = await api(`/api/category/${CAT.key}?page=${CAT.page}`);
    $("#res").insertAdjacentHTML("beforeend", r.items.map(card).join(""));
    $("#more").innerHTML = r.more ? '<div class="req" id="mb">Load more</div>' : (!$("#res").children.length ? '<div class="spin">Nothing here yet.</div>' : "");
    if (r.more) $("#mb").onclick = () => { CAT.page++; loadCat(); };
  } catch(e){ $("#more").innerHTML = `<div class="spin">${esc(e.message)}</div>`; }
  CAT.busy = false;
}

/* ── lists ── */
let mine = null;
async function loadMine(){ if(!mine) mine = new Set((await api("/api/mylist")).ids); return mine; }
async function toggleList(id){ const m = await loadMine(), on = !m.has(id); on ? m.add(id) : m.delete(id); await api("/api/mylist",{id,on}); return on; }
async function viewList(){
  app.innerHTML = '<div class="top"><span class="logo">My list</span></div><div class="spin">Loading…</div>';
  const r = await api("/api/mylist?full=1");
  app.innerHTML = `<div class="top"><span class="logo">My list</span></div>` + (r.items.length ? `<div class="grid">${r.items.map(card).join("")}</div>` : '<div class="spin">Nothing saved yet. Tap “+ My list” on any title.</div>');
}
async function viewTrend(){
  app.innerHTML = '<div class="top"><span class="logo">Trending</span></div><div class="spin">Loading…</div>';
  const d = homeData || await api("/api/home"); const r = d.rows.find(x=>x.key==="trending");
  app.innerHTML = `<div class="top"><span class="logo">Trending</span></div>` + (r ? `<div class="grid">${r.items.map(card).join("")}</div>` : '<div class="spin">No trending titles yet.</div>');
}

/* ── search ── */
let S = {q:"",type:"",lang:"",quality:"",sort:"latest",page:0,busy:false};
function viewSearch(pre){
  if (pre) S = {q:"",type:"",lang:"",quality:"",sort:"latest",page:0,busy:false,...pre};
  const chip = (k,v,l) => `<span class="chip ${S[k]===v?"on":""}" data-k="${k}" data-v="${v}">${l}</span>`;
  app.innerHTML = `<div class="top"><span class="logo">Search</span></div>
   <div class="sb"><input id="q" type="search" placeholder="Search movies, series, anime" value="${esc(S.q)}" autocomplete="off"></div>
   <div class="chips" id="f1">${chip("sort","latest","Latest")}${chip("sort","release","Released")}${chip("sort","rating","Rating")}${chip("sort","az","A–Z")}</div>
   <div class="chips" id="f2">${chip("type","","All")}${chip("type","movie","Movies")}${chip("type","series","Series")}${chip("type","anime","Anime")}${chip("quality","1080p","1080p")}${chip("quality","720p","720p")}${chip("lang","hindi","Hindi")}${chip("lang","punjabi","Punjabi")}</div>
   <div class="muted" id="cnt"></div><div class="grid" id="res"></div><div id="more"></div>`;
  let t; $("#q").oninput = e => { S.q = e.target.value; clearTimeout(t); t = setTimeout(()=>runSearch(true), 300); };
  for (const id of ["#f1","#f2"]) $(id).onclick = e => { const c=e.target.closest(".chip"); if(!c) return;
    const {k,v} = c.dataset; S[k] = (S[k]===v && k!=="sort" && k!=="type") ? "" : v; viewSearch(); };
  runSearch(true);
}
async function runSearch(reset){
  if (S.busy) return; if (reset){ S.page = 0; $("#res").innerHTML = ""; } S.busy = true;
  const p = new URLSearchParams({q:S.q,type:S.type,lang:S.lang,quality:S.quality,sort:S.sort,page:S.page});
  try {
    const r = await api("/api/search?"+p);
    $("#res").insertAdjacentHTML("beforeend", r.items.map(card).join(""));
    const n = $("#res").children.length;
    $("#cnt").textContent = n ? (S.q ? `Results for “${S.q}”` : "Latest first") : "";
    $("#more").innerHTML = r.more ? '<div class="req" id="mb">Load more</div>' :
      (S.q ? `<div class="req" id="rq">${n ? "Missing something?" : "No results."} Request “${esc(S.q)}”</div>` : "");
    if (r.more) $("#mb").onclick = () => { S.page++; runSearch(false); };
    else if (S.q) $("#rq").onclick = async () => { try { await api("/api/request",{text:S.q}); toast("Request sent to admin"); } catch(e){ toast(e.message); } };
  } catch(e){ $("#cnt").textContent = e.message; }
  S.busy = false;
}

/* ── title page ── */
async function openTitle(id){ pushView(() => viewTitle(id)); }
async function viewTitle(id){
  app.innerHTML = '<div class="spin">Loading…</div>';
  let t; try { t = await api("/api/title/"+encodeURIComponent(id)); } catch(e){ app.innerHTML = `<div class="spin">${esc(e.message)}</div>`; return; }
  const m = await loadMine().catch(()=>new Set()), saved = m.has(id);
  const bg = t.backdrop ? `style="background-image:url(${TMDB}w500${t.backdrop})"` : `style="background:${colorOf(t.name)}"`;
  app.innerHTML = `<div class="bd" ${bg}><div class="sh"></div><button class="back" id="bk">${svg("back")}</button></div>
   <div class="pad"><h1>${esc(t.name)}</h1>
   <div class="meta">${esc(t.year||"")} ${t.langs.length?"· "+esc(t.langs.map(l=>l[0].toUpperCase()+l.slice(1)).join(", ")):""} ${(t.genres||[]).length?"· "+esc(t.genres.join(", ")):""} ${t.rating?"· ★ "+t.rating:""}</div>
   <div class="btns"><button class="btn b2" id="ml">${svg(saved?"check":"plus")} <span>${saved?"In my list":"My list"}</span></button></div>
   ${t.overview?`<div class="ov">${esc(t.overview)}</div>`:""}<div id="body"></div></div>`;
  $("#bk").onclick = back;
  $("#ml").onclick = async () => { const on = await toggleList(id); $("#ml").innerHTML = `${svg(on?"check":"plus")} <span>${on?"In my list":"My list"}</span>`; };
  if (t.kind === "movie") { $("#body").innerHTML = `<div class="h" style="margin:8px 0">Files</div>${(t.files||[]).map(fileRow).join("")||'<div class="muted">No files.</div>'}`; }
  else { const ss = t.seasons_list || []; if(!ss.length) { $("#body").innerHTML = '<div class="muted">No episodes found.</div>'; return; }
    $("#body").innerHTML = `<div class="sc">${ss.map((s,i)=>`<span class="chip ${i?"":"on"}" data-s="${s}">${s?("Season "+s):"Episodes"}</span>`).join("")}</div><div id="eps"></div>`;
    $(".sc").onclick = e => { const c=e.target.closest(".chip"); if(!c) return; document.querySelectorAll(".sc .chip").forEach(x=>x.classList.toggle("on",x===c)); loadSeason(id,+c.dataset.s); };
    loadSeason(id, ss[0]); }
}
let FILES = {};
function fileRow(f){
  FILES[f.id] = f;
  const meta = [f.quality, ...f.langs.map(l=>l[0].toUpperCase()+l.slice(1))].filter(Boolean).join(" · ");
  return `<div class="fr" data-f="${f.id}"><div class="t"><b>${esc(meta||"File")}</b><i>${fmt(f.size)}${f.size?" · ":""}${esc(f.label)}</i></div>
   ${f.video?`<button class="ic" data-a="play">${svg("play")}</button>`:""}<button class="ic" data-a="dl">${svg("dl")}</button><button class="ic" data-a="dm" style="color:var(--tg)">${svg("tg")}</button></div>`;
}
async function loadSeason(id, n){
  const el = $("#eps"); el.innerHTML = '<div class="spin">Loading…</div>';
  const r = await api(`/api/title/${encodeURIComponent(id)}/season/${n}`);
  const sec = (title, fs) => fs.length ? `<div class="h" style="margin:14px 0 4px">${title}</div>${fs.map(fileRow).join("")}` : "";
  const eps = r.episodes.map(e=>`<div class="ep" data-e="${e.ep}"><span>Episode ${e.ep} <small>· ${e.files.length} file${e.files.length>1?"s":""}</small></span><small>▾</small></div><div class="eb" id="e${e.ep}" hidden>${e.files.map(fileRow).join("")}</div>`).join("");
  const comb = r.combined.length ? `<div style="border-top:1px solid var(--line);margin-top:4px">${r.combined.map(fileRow).join("")}</div>` : "";
  el.innerHTML = (eps + comb + sec("Full season", r.packs) + sec("Extras", r.extras)) || '<div class="muted">No files for this season.</div>';
  el.onclick = e => { const ep = e.target.closest(".ep"); if(ep){ const b=$("#e"+ep.dataset.e); b.hidden=!b.hidden; } };
}
document.addEventListener("click", async e => {
  const b = e.target.closest(".fr button"); if(!b) return;
  const f = FILES[b.closest(".fr").dataset.f], a = b.dataset.a;
  try {
    if (a === "dm") { toast("Sending…"); const r = await api("/api/send",{file:f.id}); openBot(r.bot); }
    else { toast("Preparing link…"); const l = await api("/api/link",{file:f.id});
      if (a === "dl") (tg.openLink ? tg.openLink(l.download) : window.open(l.download)); else player(l); }
  } catch(err){ toast(err.message); }
});
function openBot(bot){
  const url = "https://t.me/" + bot;
  if (tg.openTelegramLink) tg.openTelegramLink(url); else window.open(url, "_blank");
}
function player(l){
  const d = document.createElement("div"); d.className = "pl";
  const mx = l.watch ? `intent:${l.stream.split("://")[1]}#Intent;scheme=${l.stream.split("://")[0]};package=com.mxtech.videoplayer.ad;type=video/*;end` : "";
  d.innerHTML = `<div class="bar"><span>${esc(l.name)}</span><button class="btn b2" style="flex:none;padding:8px 14px" id="x">Close</button></div>
   <video controls autoplay playsinline src="${esc(l.stream)}"></video>
   <div class="alt"><div class="muted" style="width:100%;padding:0">If the video won't play here (some MKV audio formats), use an external player:</div>
   <a class="btn b2" href="vlc://${esc(l.stream)}">VLC</a><a class="btn b2" href="${esc(mx)}">MX Player</a>
   <button class="btn b2" id="ow">Player page</button><button class="btn b1" id="od">Download</button></div>`;
  document.body.append(d);
  d.querySelector("#x").onclick = () => d.remove();
  d.querySelector("#ow").onclick = () => tg.openLink ? tg.openLink(l.watch) : window.open(l.watch);
  d.querySelector("#od").onclick = () => tg.openLink ? tg.openLink(l.download) : window.open(l.download);
}

go("home");
</script>
</body>
</html>
"""
