/* KiteBot Mini App: spot overview and per-spot hourly chart.
   Plain browser JS, no build step. All data comes from api/forecast, which
   only answers when Telegram's signed initData is sent along. */
"use strict";
(function () {
  const tg = window.Telegram && window.Telegram.WebApp;
  // "only spots with wind" is on unless this viewer switched it off before
  function storedWindyOnly() {
    try { return localStorage.getItem("kb.windyOnly") !== "0"; } catch (e) { return true; }
  }
  const view = document.getElementById("view");
  const title = document.getElementById("title");
  const back = document.getElementById("back");
  const state = {
    day: 0, dry: false, cache: {}, ticket: 0, windyOnly: storedWindyOnly(),
    route: { day: 0, prefer: "any", km: 100, dry: false, home: true },
    profile: null, draft: null,
    riders: null, ridersTab: "records", hidden: new Set(),
    edit: null, moving: false, map: null,
  };
  const tabs = document.querySelectorAll(".tabs a");
  const DIRS = ["Z", "ZA", "A", "DA", "D", "DR", "R", "ZR"];

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v) => String(Math.round(v));
  const dec = (v) => v.toFixed(1).replace(".", ",");
  const pad = (h) => String(h).padStart(2, "0");
  const abbr = (deg) => DIRS[Math.floor(deg / 45 + 0.5) % 8];
  const pct = (v) => `${(Math.max(0, Math.min(100, v))).toFixed(2)}%`;
  const kiteSize = (k) => (Number.isInteger(k) ? String(k) : dec(k));
  const range = (lo, hi, unit) => (Math.round(lo) === Math.round(hi)
    ? `${num(lo)} ${unit}` : `${num(lo)}–${num(hi)} ${unit}`);
  // an up-arrow turned to where the wind blows TO (it comes FROM `dir`)
  const arrow = (dir) => `<b style="transform:rotate(${(dir + 180) % 360}deg)">↑</b>`;

  function initData() {
    return (tg && tg.initData) || "";
  }

  async function api(path, body) {
    const init = { headers: { "X-Telegram-Init-Data": initData() } };
    if (body !== undefined) {
      init.method = "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const res = await fetch(path, init);
    let data = {};
    try { data = await res.json(); } catch (e) { /* not JSON: keep the status */ }
    if (!res.ok) throw new Error(data.error || `Kļūda ${res.status}`);
    return data;
  }

  async function load(day) {
    if (!state.cache[day]) state.cache[day] = await api(`api/forecast?day=${day}`);
    return state.cache[day];
  }

  function notify(text, ok) {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(ok ? "success" : "error");
    if (tg && tg.showAlert && tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) {
      try { tg.showAlert(text); } catch (e) { /* another popup is still open: skip this one */ }
    } else {
      window.alert(text);
    }
  }

  // a segmented control; clicks set state through setSeg(name, value)
  function seg(name, options, current) {
    return `<div class="seg" role="tablist" data-seg="${name}">${options.map(([value, label]) => (
      `<button role="tab" data-val="${esc(value)}" aria-selected="${String(value) === String(current)}">${esc(label)}</button>`
    )).join("")}</div>`;
  }

  function setSeg(name, value) {
    if (name === "day") state.day = Number(value);
    else if (name === "rday") state.route.day = Number(value);
    else if (name === "prefer") state.route.prefer = value;
    else if (name === "km") state.route.km = Number(value);
    else if (name === "rtab") state.ridersTab = value;
    else if (name === "cell" && state.edit) { state.edit.cell = value; state.edit.dirty = true; }
  }

  // windows left after the "Bez lietus" filter, as a set of indexes
  function shownWindows(spot) {
    const out = new Set();
    spot.windows.forEach((w, i) => { if (!state.dry || w.dry) out.add(i); });
    return out;
  }

  function hourKind(spot, hour, shown) {
    if (hour.w === null || !shown.has(hour.w)) return "none";
    return spot.windows[hour.w].foil ? "foil" : "kite";
  }

  function bestWindow(spot, shown) {
    const list = spot.windows.filter((w, i) => shown.has(i));
    const kite = list.filter((w) => !w.foil);
    const pool = kite.length ? kite : list;
    return pool.reduce((best, w) => (!best || w.end_h - w.start_h > best.end_h - best.start_h ? w : best), null);
  }

  function dayTabs(data, name = "day", current = state.day) {
    return seg(name, data.days.map((d) => [d.offset, d.label]), current);
  }

  function renderOverview(data) {
    title.textContent = "KiteBot";
    back.hidden = true;
    if (tg && tg.BackButton) tg.BackButton.hide();
    const mid = Math.round((data.day_start + data.day_end) / 2);
    // spots with wind first, earliest window first; calm ones after, or hidden
    const ranked = data.spots.map((s, order) => {
      const shown = shownWindows(s);
      const starts = s.windows.filter((w, i) => shown.has(i)).map((w) => w.start_h);
      return { s, order, first: starts.length ? Math.min(...starts) : null };
    });
    const windy = ranked.filter((r) => r.first !== null).sort((a, b) => a.first - b.first || a.order - b.order);
    const calm = ranked.filter((r) => r.first === null);
    const visible = state.windyOnly ? windy : windy.concat(calm);
    const cards = visible.map(({ s }) => {
      const shown = shownWindows(s);
      const best = bestWindow(s, shown);
      let pill = '<span class="pill none">—</span>';
      let sub = "braucama vēja nav";
      if (s.error) {
        pill = '<span class="pill none">⚠️</span>';
        sub = "prognozi šobrīd neizdevās iegūt";
      } else if (best) {
        const more = shown.size > 1 ? ` +${shown.size - 1}` : "";
        pill = `<span class="pill ${best.foil ? "foil" : "kite"}">${esc(best.start.slice(0, 2))}–${esc(best.end.slice(0, 2))}${more}</span>`;
        sub = `${range(best.min, best.max, data.unit)} · brāzmas ${num(best.gust)} · ${best.dir_lv}`
          + (best.foil ? " · tikai ar matraci" : "")
          + (best.rain >= 0.2 ? ` · 🌧 ${dec(best.rain)} mm` : "");
      }
      const byHour = new Map(s.hours.map((h) => [h.h, h]));
      const cells = [];
      for (let h = data.day_start; h < data.day_end; h += 1) {
        const hour = byHour.get(h);
        const cls = hour ? hourKind(s, hour, shown) + (hour.past ? " past" : "") : "none";
        cells.push(`<i class="${cls}"></i>`);
      }
      return `<a class="card" href="#/spot/${encodeURIComponent(s.name)}">`
        + `<div class="row"><span class="name">${esc(s.name)}</span>${pill}</div>`
        + `<div class="strip">${cells.join("")}</div><div class="sub">${esc(sub)}</div></a>`;
    });
    const empty = !data.spots.length ? "Vēl nav neviena spota."
      : "Šai dienai nevienā spotā nav braucama vēja.";
    const hidden = state.windyOnly && calm.length
      ? `<p class="note">Paslēpti ${calm.length} spoti bez vēja.</p>` : "";
    view.innerHTML = dayTabs(data)
      + '<div class="bar"><div class="switches">'
      + `<label class="toggle"><input type="checkbox" id="windy"${state.windyOnly ? " checked" : ""}> 💨 Tikai ar vēju</label>`
      + `<label class="toggle"><input type="checkbox" id="dry"${state.dry ? " checked" : ""}> ☀️ Bez lietus</label></div>`
      + '<span class="key"><i class="sw" style="background:var(--kite)"></i>kaits<i class="sw" style="background:var(--foil)"></i>matracis</span></div>'
      + `<div class="axis"><span>${pad(data.day_start)}</span><span>${pad(mid)}</span><span>${pad(data.day_end)}</span></div>`
      + (cards.length ? cards.join("") : `<p class="msg">${empty}</p>`)
      + hidden
      + (data.is_admin ? '<a class="btn ghost" href="#/edit">➕ Jauns spots</a>' : "")
      + `<p class="foot">Atjaunots ${esc(data.updated)} · Open-Meteo</p>`;
  }

  // round the axis top up to a step that gives at most five grid lines
  function scale(peak) {
    for (const step of [1, 2, 3, 5, 10, 15, 20, 25, 50, 100]) {
      const top = Math.max(step, Math.ceil(peak / step) * step);
      if (top / step <= 5) return { top, step };
    }
    return { top: Math.ceil(peak), step: Math.ceil(peak) };
  }

  function compass(sectors, dir, ok) {
    const rad = (d) => (d * Math.PI) / 180;
    const at = (d, r) => [50 + r * Math.sin(rad(d)), 50 - r * Math.cos(rad(d))];
    const f = (v) => v.toFixed(1);
    let wedges = "";
    const full = '<circle cx="50" cy="50" r="44" style="fill:var(--kite)" opacity=".7"/>';
    if (!sectors.length) wedges = full;
    for (const [lo, hi] of sectors) {
      const span = (((hi - lo) % 360) + 360) % 360 || 360;
      if (span >= 360) { wedges = full; break; }
      const [x1, y1] = at(lo, 44);
      const [x2, y2] = at(hi, 44);
      wedges += `<path d="M50 50 L${f(x1)} ${f(y1)} A44 44 0 ${span > 180 ? 1 : 0} 1 ${f(x2)} ${f(y2)} Z" style="fill:var(--kite)" opacity=".75"/>`;
    }
    const [ax, ay] = at(dir, 42);
    const [bx, by] = at(dir + 180, 22);
    const ux = -Math.sin(rad(dir));
    const uy = Math.cos(rad(dir));
    const tip = [bx + ux * 14, by + uy * 14];
    const left = [bx - uy * 9, by + ux * 9];
    const right = [bx + uy * 9, by - ux * 9];
    const ink = ok ? "var(--text)" : "var(--warn)";
    return `<svg viewBox="0 0 100 100" role="img" aria-label="${ok ? "vējš sektorā" : "vējš ārpus sektora"}">`
      + '<circle cx="50" cy="50" r="44" style="fill:var(--none)"/>' + wedges
      + `<line x1="${f(ax)}" y1="${f(ay)}" x2="${f(bx)}" y2="${f(by)}" style="stroke:${ink}" stroke-width="8"/>`
      + `<polygon points="${f(tip[0])},${f(tip[1])} ${f(left[0])},${f(left[1])} ${f(right[0])},${f(right[1])}" style="fill:${ink}"/></svg>`;
  }

  function tiles(s, best, data) {
    const tile = (big, small) => `<div class="tile"><b>${big}</b><span>${small}</span></div>`;
    if (best) {
      const wind = Math.round(best.min) === Math.round(best.max) ? num(best.min) : `${num(best.min)}–${num(best.max)}`;
      const kite = best.kite ? tile(`${kiteSize(best.kite)} m²`, "kaits")
        : tile("—", data.has_kites ? "kaits" : "/quiver");
      return `<div class="tiles">${tile(wind, esc(data.unit))}${tile(num(best.gust), "brāzmas")}${kite}`
        + `<div class="tile">${compass(s.sectors, best.dir, true)}<span>${best.dir_lv} ✓</span></div></div>`;
    }
    const windiest = s.hours.reduce((a, h) => (!a || h.speed > a.speed ? h : a), null);
    const gust = Math.max(...s.hours.map((h) => h.gust));
    return `<div class="tiles">${tile(num(windiest.speed), `maks. ${esc(data.unit)}`)}${tile(num(gust), "brāzmas")}`
      + `${tile("—", "nav loga")}<div class="tile">${compass(s.sectors, windiest.dir, windiest.in_sector)}`
      + `<span>${abbr(windiest.dir)} ${windiest.in_sector ? "✓" : "✗"}</span></div></div>`;
  }

  function renderSpot(data, name) {
    const s = data.spots.find((x) => x.name === name);
    if (!s) { location.hash = "#/"; return; }
    title.textContent = s.name;
    back.hidden = false;
    if (tg && tg.BackButton) tg.BackButton.show();
    const foot = `<p class="foot">${esc(s.model)} · Open-Meteo · atjaunots ${esc(data.updated)}</p>`;
    if (s.error || !s.hours.length) {
      view.innerHTML = dayTabs(data) + `<p class="msg">${s.error ? "Prognozi šobrīd neizdevās iegūt." : "Šai dienai prognozes nav."}</p>` + foot;
      return;
    }
    const hours = s.hours;
    const n = hours.length;
    const shown = shownWindows(s);
    const { top, step } = scale(Math.max(s.min_wind, ...hours.map((h) => h.gust)));
    const y = (v) => (v / top) * 100;

    let yLabels = "";
    let grid = "";
    for (let v = step; v <= top + 1e-9; v += step) {
      yLabels += `<span style="bottom:${pct(y(v))}">${v}</span>`;
      grid += `<div class="g" style="bottom:${pct(y(v))}"></div>`;
    }
    const minLine = s.min_wind <= top ? `<div class="m" style="bottom:${pct(y(s.min_wind))}"></div>` : "";

    let shades = "";
    let labels = "";
    s.windows.forEach((w, i) => {
      if (!shown.has(i)) return;
      const first = hours.findIndex((h) => h.w === i);
      if (first < 0) return;
      let last = first;
      while (last + 1 < n && hours[last + 1].w === i) last += 1;
      const left = (first / n) * 100;
      const width = ((last - first + 1) / n) * 100;
      shades += `<div class="w${w.foil ? " foil" : ""}" style="left:${pct(left)};width:${pct(width)}"></div>`;
      labels += `<span class="win${w.foil ? " foil" : ""}" style="left:${pct(left + width / 2)}">${w.foil ? "" : "✅ "}${esc(w.start.slice(0, 2))}–${esc(w.end.slice(0, 2))}</span>`;
    });
    let turns = "";
    for (const note of s.notes) {
      const i = hours.findIndex((h) => h.h === note.h);
      if (i < 0) continue;
      turns += `<div class="t" style="left:${pct((i / n) * 100)}"></div>`;
      const text = note.kind === "sector" ? `${arrow(note.dir)} ${esc(note.dir_lv)} ✗` : "💨 par stipru";
      labels += `<span class="turn" style="left:${pct((i / n) * 100)}">${text}</span>`;
    }

    const cols = hours.map((h) => {
      const kind = hourKind(s, h, shown);
      const tip = `${pad(h.h)}:00 · ${num(h.speed)} ${data.unit}, brāzmas ${num(h.gust)} · ${abbr(h.dir)}`;
      return `<div class="col ${kind}" title="${esc(tip)}">`
        + `<i style="height:${pct(y(h.speed))}"></i><u style="bottom:calc(${pct(y(h.gust))} - 3px)"></u></div>`;
    }).join("");
    if (data.now_h !== null) {
      const at = ((data.now_h - hours[0].h) / n) * 100;
      if (at > 0 && at < 100) turns += `<div class="now" style="left:${pct(at)}" title="tagad"></div>`;
    }
    const xaxis = hours.map((h) => `<span>${h.h % 2 === 0 ? pad(h.h) : ""}</span>`).join("");
    const dirs = hours.map((h) => `<span class="${h.in_sector ? "ok" : "no"}" title="${abbr(h.dir)}">${arrow(h.dir)}</span>`).join("");
    const wet = hours.some((h) => h.rain >= 0.2);
    const rain = wet ? `<div class="rain">${hours.map((h) => `<span>${h.rain >= 0.2 ? "💧" : ""}</span>`).join("")}</div>` : "";

    view.innerHTML = dayTabs(data)
      + '<div class="chart">'
      + `<div class="legend"><span>${esc(data.unit)}</span><span><i class="gl"></i>brāzmas</span><span><i class="ml"></i>min ${num(s.min_wind)}</span></div>`
      + `<div class="notes">${labels}</div>`
      + `<div class="plot-wrap"><div class="yaxis">${yLabels}</div><div class="plot">${grid}${shades}${minLine}${turns}${cols}</div></div>`
      + `<div class="xaxis">${xaxis}</div><div class="dirs">${dirs}</div>${rain}</div>`
      + tiles(s, bestWindow(s, shown), data)
      + `<a class="btn ghost" href="#/edit/${encodeURIComponent(s.name)}">🧭 Virzieni kartē</a>`
      + foot;
  }

  // --- route planner -------------------------------------------------------------

  const hoursLv = (h) => (Number.isInteger(h) ? String(h) : dec(h));
  function duration(minutes) {
    const m = Math.round(minutes / 5) * 5;
    const h = Math.floor(m / 60);
    const rest = m % 60;
    return h && rest ? `${h} h ${rest} min` : h ? `${h} h` : `${rest} min`;
  }
  const windLine = (w, unit) => `${range(w.min, w.max, unit)} · brāzmas ${num(w.gust)} · ${esc(w.dir_lv)}`
    + (w.rain >= 0.2 ? ` · 🌧 ${dec(w.rain)} mm` : "") + (w.kite ? ` · 🪁 ${kiteSize(w.kite)}` : "");

  function routeResult(data, unit) {
    if (data.kind === "none") {
      return `<p class="msg">Šai dienai${state.route.dry ? " bez lietus" : ""} nevienā spotā nav braucama vēja.</p>`;
    }
    if (data.kind === "route") {
      const legs = data.legs.map((leg, i) => (
        (leg.travel_min ? `<div class="drive">🚗 ${i === 0 ? "no mājām · " : ""}${duration(leg.travel_min)} · ${leg.travel_km} km</div>` : "")
        + `<div class="leg"><div class="row"><b>${i + 1} · ${esc(leg.spot)}</b><span>${esc(leg.start)}–${esc(leg.end)}</span></div>`
        + `<div class="sub">${windLine(leg, unit)}</div></div>`
      )).join("");
      const take = data.kites.length > 1 ? `ņem ${data.kites.map(kiteSize).join(" + ")}` : `🚗 ~${data.drive_km} km`;
      return `<div class="card">${legs}<div class="row sum"><b>~${hoursLv(data.total_h)} h ūdenī</b><span>${take}</span></div></div>`;
    }
    const drive = data.drive_km !== null ? ` · 🚗 ~${data.drive_km} km` : "";
    const head = data.within
      ? `Braukāt apkārt neatmaksājas — labāk palikt: <b>${esc(data.spot)}</b>`
      : `${data.km} km robežās vēja nav. Tuvākais spots ar vēju: <b>${esc(data.spot)}</b>`;
    const rows = data.windows.map((w) => (
      `<div class="row"><span>${esc(w.start)}–${esc(w.end)}</span><span class="sub">${windLine(w, unit)}</span></div>`)).join("");
    return `<div class="card"><h2>${head}</h2><p class="note">~${hoursLv(data.hours)} h ūdenī${drive}</p><div class="wins">${rows}</div></div>`;
  }

  async function renderRoute(ticket) {
    title.textContent = "Maršruts";
    const r = state.route;
    const query = new URLSearchParams({
      day: r.day, prefer: r.prefer, km: r.km, dry: r.dry ? 1 : 0, home: r.home ? 1 : 0,
    });
    const [forecast, data] = await Promise.all([load(0), api(`api/route?${query}`)]);
    if (ticket !== state.ticket) return;
    const homeOff = data.has_home ? "" : " off";
    view.innerHTML = '<div class="opts">'
      + dayTabs(forecast, "rday", r.day)
      + seg("prefer", [["strong", "💨 Stiprāks"], ["light", "🍃 Vieglāks"], ["any", "Jebkāds"]], r.prefer)
      + seg("km", [[50, "≤50 km"], [100, "≤100 km"], [200, "≤200 km"], [0, "♾️"]], r.km)
      + `<div class="switches"><label><input type="checkbox" id="rdry"${r.dry ? " checked" : ""}> ☀️ Tikai sauss</label>`
      + `<label class="${homeOff.trim()}"><input type="checkbox" id="rhome"${r.home && data.has_home ? " checked" : ""}${data.has_home ? "" : " disabled"}> 🏠 No mājām</label></div>`
      + "</div>"
      + routeResult(data, forecast.unit)
      + (data.can_share && data.kind !== "none" ? '<button class="btn" data-act="share">📤 Nosūtīt grupai</button>' : "")
      + (data.has_home ? "" : '<p class="note">🏠 Mājas vietu iestata cilnē Profils vai ar /home čatā.</p>');
  }

  async function shareRoute(button) {
    button.disabled = true;
    try {
      const out = await api("api/route/share", state.route);
      notify(out.sent ? "Nosūtīts grupai." : "Neizdevās nosūtīt — vai esi pierakstītajā grupā?", out.sent > 0);
    } catch (err) {
      notify(err.message, false);
    }
    button.disabled = false;
  }

  // --- profile -------------------------------------------------------------------

  const canLocate = () => !!(tg && tg.LocationManager && tg.isVersionAtLeast && tg.isVersionAtLeast("8.0"));

  function profileView() {
    const p = state.profile;
    const d = state.draft;
    const changed = JSON.stringify(d.quiver) !== JSON.stringify(p.quiver) || d.weight !== p.weight_kg;
    const chips = d.quiver.map((size, i) => (
      `<span class="chip">${kiteSize(size)} m²<button data-act="kite-del" data-i="${i}" aria-label="Dzēst ${kiteSize(size)}">×</button></span>`
    )).join("");
    const table = p.kite_table.map((k) => {
      const wind = k.lo === null ? `līdz ${num(k.hi)}` : k.hi === null ? `virs ${num(k.lo)}` : `${num(k.lo)}–${num(k.hi)}`;
      return `<div class="row"><span>${wind} ${esc(p.unit)}</span><b>${kiteSize(k.size)} m²</b></div>`;
    }).join("");
    const homeButtons = (canLocate() ? '<button class="btn ghost" data-act="home-gps">📍 Tālruņa atrašanās vieta</button>' : "")
      + (p.home ? '<button class="btn ghost" data-act="home-clear">Dzēst</button>' : "");
    view.innerHTML = `<div class="card"><h2>🏠 Mājas vieta</h2>`
      + `<div class="sub">${p.home ? "saglabāta — maršruts rēķina braucienu no mājām" : "nav iestatīta"}</div>`
      + (homeButtons ? `<div class="btns">${homeButtons}</div>` : "")
      + (canLocate() ? "" : '<p class="note">Iestati ar /home čatā (dalies ar atrašanās vietu).</p>')
      + "</div>"
      + `<div class="card"><h2>🪁 Kaiti</h2><div class="chips">${chips || '<span class="sub">vēl nav</span>'}</div>`
      + '<div class="add"><input id="kite-new" inputmode="decimal" placeholder="m², piem. 10,5" aria-label="Jauna kaita izmērs">'
      + `<button class="btn" data-act="kite-add"${d.quiver.length >= 8 ? " disabled" : ""}>Pievienot</button></div></div>`
      + '<div class="card"><h2>⚖️ Svars</h2><div class="stepper">'
      + `<button data-act="w-dec" aria-label="Mazāk">−</button><b>${d.weight ? `${num(d.weight)} kg` : "—"}</b>`
      + '<button data-act="w-inc" aria-label="Vairāk">+</button></div></div>'
      + `<button class="btn" data-act="save"${changed ? "" : " disabled"}>Saglabāt</button>`
      + `<div class="card ktable"><h2>Kurš kaits kādam vējam</h2>${table || '<p class="note">Ievadi kaitus un svaru, lai redzētu.</p>'}`
      + (table ? '<p class="note">2,2 × kg ÷ vējš mezglos</p>' : "") + "</div>";
  }

  async function renderProfile(ticket) {
    title.textContent = "Profils";
    if (!state.profile) {
      const p = await api("api/profile");
      if (ticket !== state.ticket) return;
      state.profile = p;
    }
    if (!state.draft) state.draft = { quiver: [...state.profile.quiver], weight: state.profile.weight_kg };
    profileView();
  }

  async function saveProfile(body) {
    state.profile = await api("api/profile", body);
    state.draft = { quiver: [...state.profile.quiver], weight: state.profile.weight_kg };
    state.cache = {};  // kite picks in the forecast change with the profile
    profileView();
  }

  async function profileAction(name, el) {
    const d = state.draft;
    if (name === "kite-del") d.quiver.splice(Number(el.dataset.i), 1);
    else if (name === "kite-add") {
      const input = document.getElementById("kite-new");
      const size = parseFloat(input.value.replace(",", "."));
      if (!(size >= 2 && size <= 25)) { notify("Kaita izmērs 2–25 m².", false); return; }
      if (!d.quiver.includes(size)) d.quiver.push(size);
      d.quiver.sort((a, b) => b - a);
    } else if (name === "w-dec" || name === "w-inc") {
      const step = name === "w-inc" ? 1 : -1;
      d.weight = Math.min(200, Math.max(30, (d.weight || 75) + (d.weight ? step : 0)));
    } else if (name === "save") {
      try {
        await saveProfile({ quiver: d.quiver, weight_kg: d.weight });
        notify("Saglabāts.", true);
      } catch (err) { notify(err.message, false); }
      return;
    } else if (name === "home-clear") {
      try { await saveProfile({ home: null }); } catch (err) { notify(err.message, false); }
      return;
    } else if (name === "home-gps") {
      const lm = tg.LocationManager;
      lm.init(() => {
        if (!lm.isLocationAvailable) { notify("Atrašanās vieta šajā ierīcē nav pieejama.", false); return; }
        lm.getLocation(async (loc) => {
          if (!loc) { notify("Atrašanās vietu neizdevās iegūt — atļauj to Telegram iestatījumos.", false); return; }
          try {
            await saveProfile({ home: { lat: loc.latitude, lon: loc.longitude } });
            notify("Mājas vieta saglabāta.", true);
          } catch (err) { notify(err.message, false); }
        });
      });
      return;
    }
    profileView();
  }

  // --- riders --------------------------------------------------------------------

  const SERIES = [
    ["var(--kite)", ""], ["var(--gust)", ""], ["var(--text)", "6 4"],
    ["var(--foil)", ""], ["var(--warn)", "2 3"], ["#5B8C3A", "8 3"],
  ];
  const shortDate = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
  const MONTHS_LV = ["jan", "feb", "mar", "apr", "mai", "jūn", "jūl", "aug", "sep", "okt", "nov", "dec"];
  const dayNumber = (iso) => Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)) / 864e5;

  function progressChart(series) {
    const W = 360; const H = 230; const L = 34; const R = 276; const T = 12; const B = 196;
    const all = series.flatMap((s) => s.points);
    const days = all.map(([d]) => dayNumber(d));
    const first = Math.min(...days); const last = Math.max(...days);
    const heights = all.map(([, h]) => h);
    const { top, step } = scale(Math.max(...heights) + 0.5);
    const floor = Math.max(0, Math.floor((Math.min(...heights) - 0.5) / step) * step);
    const x = (iso) => (last === first ? (L + R) / 2 : L + ((dayNumber(iso) - first) / (last - first)) * (R - L));
    const y = (h) => B - ((h - floor) / (top - floor)) * (B - T);
    let svg = "";
    for (let v = floor; v <= top + 1e-9; v += step) {
      svg += `<line x1="${L}" x2="${R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" style="stroke:var(--line)"/>`
        + `<text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end" font-size="12" style="fill:var(--hint)">${hoursLv(v)}</text>`;
    }
    const ticks = [...new Set(all.map(([d]) => d))].sort();
    const every = Math.max(1, Math.ceil(ticks.length / 4));
    // over months, "11.25" reads as a day — name the month and year instead
    const long = last - first > 120;
    const label = (d) => (long ? `${MONTHS_LV[+d.slice(5, 7) - 1]} ’${d.slice(2, 4)}` : shortDate(d));
    ticks.filter((_, i) => i % every === 0).forEach((d) => {
      svg += `<text x="${x(d).toFixed(1)}" y="${H - 14}" text-anchor="middle" font-size="12" style="fill:var(--hint)">${label(d)}</text>`;
    });
    const labels = [];
    series.forEach((s) => {
      const [color, dash] = SERIES[s.index % SERIES.length];
      const pts = s.points.map(([d, h]) => `${x(d).toFixed(1)},${y(h).toFixed(1)}`).join(" ");
      svg += `<polyline points="${pts}" fill="none" stroke-width="3" stroke-linejoin="round"${dash ? ` stroke-dasharray="${dash}"` : ""} style="stroke:${color}"/>`;
      const best = s.points.reduce((a, p) => (p[1] > a[1] ? p : a));
      s.points.forEach(([d, h]) => {
        const ring = d === best[0] && h === best[1];
        svg += `<circle cx="${x(d).toFixed(1)}" cy="${y(h).toFixed(1)}" r="${ring ? 6 : 3}" style="${ring ? `fill:var(--card);stroke:${color};stroke-width:3` : `fill:${color}`}"/>`;
      });
      const [ld, lh] = s.points[s.points.length - 1];
      labels.push({ y: y(lh), x: x(ld), name: s.name, color });
    });
    labels.sort((a, b) => a.y - b.y);  // push apart so names never overlap
    for (let i = 1; i < labels.length; i += 1) labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 14);
    labels.forEach((l) => {
      svg += `<text x="${(Math.max(l.x, R) + 8).toFixed(1)}" y="${(l.y + 4).toFixed(1)}" font-size="13" font-weight="600" style="fill:${l.color}">${esc(l.name)}</text>`;
    });
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Labākais lēciens katrā sesijā">${svg}</svg>`;
  }

  function ridersBody(d) {
    if (!d.riders.length) return '<p class="msg">Vēl nav neviena braucēja — admins pievieno ar /woorider vai /surfrider.</p>';
    if (state.ridersTab === "records") {
      const best = Math.max(...d.riders.map((r) => r.record), 0.1);
      return `<div class="card">${d.riders.map((r) => (
        `<div class="rd"><div class="row"><span class="who"><span class="avt">${esc(r.name.charAt(0).toUpperCase())}</span>`
        + `<b>${esc(r.name)}</b>${r.new_record ? '<span class="new">🎉 jauns</span>' : ""}</span><b>${dec(r.record)} m</b></div>`
        + `<div class="hb"><i style="width:${pct((r.record / best) * 100)}"></i></div></div>`)).join("")}</div>`;
    }
    if (state.ridersTab === "progress") {
      if (!d.series.length) {
        return '<p class="msg">Vēsture vēl ielādējas (pēc atjaunināšanas tas aizņem ~30 min) — pamēģini vēlāk.</p>';
      }
      const indexed = d.series.map((s, index) => ({ ...s, index }));
      const chips = indexed.map((s) => {
        const [color] = SERIES[s.index % SERIES.length];
        return `<button class="rchip${state.hidden.has(s.name) ? " off" : ""}" data-act="rider-toggle" data-name="${esc(s.name)}"><i style="background:${color}"></i>${esc(s.name)}</button>`;
      }).join("");
      const shown = indexed.filter((s) => !state.hidden.has(s.name));
      return `<div class="rchips">${chips}</div>`
        + `<div class="card progress"><h2>Labākais lēciens, m</h2>${shown.length ? progressChart(shown) : '<p class="note">Ieslēdz kādu braucēju.</p>'}`
        + `<p class="note">Aplis — labākais · ${d.daily_since
          ? `līdz ${shortDate(d.daily_since)} viens punkts mēnesī, pēc tam katra sesija`
          : "viens punkts mēnesī (mēneša labākais)"}</p></div>`;
    }
    if (!d.latest.date) return '<p class="msg">Vēl nav pierakstīta neviena sesija.</p>';
    const rows = d.latest.entries.map((e) => {
      const parts = [
        e.height_m && `⬆️ ${dec(e.height_m)} m`, e.jump_distance_m && `↔️ ${dec(e.jump_distance_m)} m`,
        e.distance_km && `🛣️ ${dec(e.distance_km)} km`, e.kite && `🪁 ${esc(e.kite)}`, e.spot && `📍 ${esc(e.spot)}`,
      ].filter(Boolean).join(" · ");
      return `<div class="rd"><b>${esc(e.name)}</b><div class="sub">${parts}</div></div>`;
    }).join("");
    return `<div class="card"><h2>Sesijas ${shortDate(d.latest.date)}</h2>${rows || '<p class="note">Neviens nebrauca.</p>'}</div>`;
  }

  async function renderRiders(ticket) {
    title.textContent = "Braucēji";
    if (!state.riders) {
      const d = await api("api/riders");
      if (ticket !== state.ticket) return;
      state.riders = d;
    }
    view.innerHTML = seg("rtab", [["records", "Rekordi"], ["progress", "Progress"], ["latest", "Pēdējās"]], state.ridersTab)
      + ridersBody(state.riders);
  }

  // --- spot editor (map + compass) -------------------------------------------------

  const LEAFLET_CSS = ["https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="];
  const LEAFLET_JS = ["https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="];
  const OCTANTS = [[337.5, 22.5], [22.5, 67.5], [67.5, 112.5], [112.5, 157.5],
    [157.5, 202.5], [202.5, 247.5], [247.5, 292.5], [292.5, 337.5]];
  const OCTANT_LV = ["Z", "ZA", "A", "DA", "D", "DR", "R", "ZR"];
  let leafletLoading = null;

  function loadLeaflet() {
    if (window.L) return Promise.resolve();
    if (!leafletLoading) {
      leafletLoading = new Promise((resolve, reject) => {
        const css = document.createElement("link");
        css.rel = "stylesheet";
        [css.href, css.integrity] = LEAFLET_CSS;
        css.crossOrigin = "anonymous";
        document.head.appendChild(css);
        const js = document.createElement("script");
        [js.src, js.integrity] = LEAFLET_JS;
        js.crossOrigin = "anonymous";
        js.onload = resolve;
        js.onerror = () => { leafletLoading = null; reject(new Error("Karti neizdevās ielādēt.")); };
        document.head.appendChild(js);
      });
    }
    return leafletLoading;
  }

  // the same rules as the chat's direction buttons (analysis.py)
  function inSectors(deg, sectors) {
    if (!sectors.length) return true;
    const d = ((deg % 360) + 360) % 360;
    return sectors.some(([lo, hi]) => (lo <= hi ? lo <= d && d <= hi : d >= lo || d <= hi));
  }
  const coveredOctants = (sector) => [0, 1, 2, 3, 4, 5, 6, 7].filter((i) => inSectors(i * 45, [sector]));
  function sectorsFromToggles(toggles, current) {
    const out = [];
    toggles.forEach((on, i) => {
      if (!on) return;
      const keep = current.find((s) => {
        const cov = coveredOctants(s);
        return cov.includes(i) && cov.every((j) => toggles[j]);
      });
      const sector = keep ? [...keep] : [...OCTANTS[i]];
      if (!out.some((s) => s[0] === sector[0] && s[1] === sector[1])) out.push(sector);
    });
    return out;
  }
  function describeSectors(sectors) {
    if (!sectors.length) return "jebkurš virziens";
    return sectors.map(([lo, hi]) => {
      const i = OCTANTS.findIndex(([a, b]) => a === lo && b === hi);
      return i >= 0 ? OCTANT_LV[i] : `${Math.round(lo)}°–${Math.round(hi)}°`;
    }).join(", ");
  }

  function editorCompass(sectors, editable) {
    const rad = (d) => (d * Math.PI) / 180;
    const at = (d, r) => [r * Math.sin(rad(d)), -r * Math.cos(rad(d))];
    const f = (v) => v.toFixed(1);
    const R = 96;
    const wedge = (lo, hi, r) => {
      const span = (((hi - lo) % 360) + 360) % 360 || 360;
      if (span >= 360) return `<circle r="${r}"/>`;
      const [x1, y1] = at(lo, r);
      const [x2, y2] = at(hi, r);
      return `<path d="M0 0 L${f(x1)} ${f(y1)} A${r} ${r} 0 ${span > 180 ? 1 : 0} 1 ${f(x2)} ${f(y2)} Z"/>`;
    };
    let svg = `<circle r="${R}" fill="#FBFBF8" fill-opacity=".35" stroke="#10202E" stroke-opacity=".4" stroke-width="2"/>`;
    const filled = sectors.length ? sectors.map(([lo, hi]) => wedge(lo, hi, R)).join("") : `<circle r="${R}"/>`;
    svg += `<g fill="#22709A" fill-opacity=".42" stroke="#22709A" stroke-width="2" pointer-events="none">${filled}</g>`;
    if (editable) {
      svg += `<g fill="transparent" style="cursor:pointer">${OCTANTS.map(([lo, hi], i) => wedge(lo, hi, R).replace(/^<(path|circle)/, `<$1 data-oct="${i}"`)).join("")}</g>`;
      sectors.forEach(([lo, hi], i) => {
        [lo, hi].forEach((deg, end) => {
          const [x, y] = at(deg, R);
          svg += `<circle data-s="${i}" data-e="${end}" cx="${f(x)}" cy="${f(y)}" r="11" fill="#FBFBF8" stroke="#B4471A" stroke-width="4" style="cursor:grab"/>`;
        });
      });
    }
    svg += '<circle r="8" fill="#10202E" stroke="#FBFBF8" stroke-width="3" pointer-events="none"/>';
    svg += '<g font-size="18" font-weight="700" fill="#10202E" stroke="#FBFBF8" stroke-width="4" paint-order="stroke" text-anchor="middle" pointer-events="none" font-family="system-ui, sans-serif">'
      + `<text y="${-R - 8}">Z</text><text x="${R + 14}" y="6">A</text><text y="${R + 22}">D</text><text x="${-R - 14}" y="6">R</text></g>`;
    return `<svg width="220" height="220" viewBox="-110 -110 220 220">${svg}</svg>`;
  }

  function editorForm(admin, unit) {
    const e = state.edit;
    const valid = e.name.trim() && e.lat !== null && e.min_wind < e.max_wind;
    if (!admin) {
      return `<div class="card"><div class="row"><b>${esc(e.name)}</b><span class="sub">${num(e.min_wind)}–${num(e.max_wind)} ${esc(unit)}</span></div>`
        + `<p class="note">Virzieni: ${esc(describeSectors(e.sectors))}. Mainīt var tikai admini.</p></div>`;
    }
    const step = (act, value) => `<div class="stepper"><button data-act="${act}-dec" aria-label="Mazāk">−</button><b>${hoursLv(value)}</b><button data-act="${act}-inc" aria-label="Vairāk">+</button></div>`;
    return '<div class="card">'
      + (e.lat === null ? '<p class="note">Pieskaries kartē, kur atrodas spots.</p>'
        : '<p class="note">Pieskaries kompasam, lai ieslēgtu vai izslēgtu virzienu; velc oranžos punktus, lai sašaurinātu.</p>')
      + `<div class="field"><input id="sp-name" maxlength="25" placeholder="Nosaukums" value="${esc(e.name)}" aria-label="Spota nosaukums"></div>`
      + `<div class="field"><span>Vējš no, ${esc(unit)}</span>${step("min", e.min_wind)}</div>`
      + `<div class="field"><span>līdz</span>${step("max", e.max_wind)}</div>`
      + `<div class="field"><span>Virzieni</span><span class="sub">${esc(describeSectors(e.sectors))}</span></div>`
      + '<p class="note">Prognozes šūna</p>'
      + seg("cell", [["land", "Sauszeme"], ["sea", "Jūra"], ["nearest", "Tuvākā"]], e.cell)
      + '<div class="btns">'
      + (e.lat !== null ? `<button class="btn ghost small" data-act="spot-move">${state.moving ? "Pieskaries kartē…" : "📍 Pārvietot"}</button>` : "")
      + `<button class="btn" data-act="spot-save"${valid && e.dirty ? "" : " disabled"}>Saglabāt</button></div>`
      + (e.original ? '<div class="btns"><button class="btn warn" data-act="spot-delete">🗑 Dzēst spotu</button></div>' : "")
      + "</div>";
  }

  function drawEditor(admin, unit) {
    const e = state.edit;
    const form = document.getElementById("spot-form");
    if (form) form.innerHTML = editorForm(admin, unit);
    const map = state.map;
    if (!map) return;
    if (e.lat === null) return;
    if (!state.marker) {
      state.marker = window.L.marker([e.lat, e.lon], {
        icon: window.L.divIcon({ className: "compass-icon", html: "", iconSize: [220, 220] }),
        keyboard: false,
      }).addTo(map);
      wireCompass(state.marker.getElement(), admin, unit);
    }
    state.marker.setLatLng([e.lat, e.lon]);
    state.marker.getElement().innerHTML = editorCompass(e.sectors, admin);
  }

  function wireCompass(el, admin, unit) {
    window.L.DomEvent.disableClickPropagation(el);
    if (!admin) return;
    let drag = null;
    el.addEventListener("pointerdown", (ev) => {
      const handle = ev.target.closest("[data-s]");
      if (!handle) return;
      ev.preventDefault();
      drag = { s: Number(handle.dataset.s), e: Number(handle.dataset.e) };
      state.map.dragging.disable();
      el.setPointerCapture(ev.pointerId);
    });
    el.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      const box = el.getBoundingClientRect();
      const dx = ev.clientX - (box.left + box.width / 2);
      const dy = ev.clientY - (box.top + box.height / 2);
      const deg = (Math.round((((Math.atan2(dx, -dy) * 180) / Math.PI + 360) % 360) / 5) * 5) % 360;
      const sector = state.edit.sectors[drag.s];
      if (deg === sector[1 - drag.e] || deg === sector[drag.e]) return;
      sector[drag.e] = deg;
      state.edit.dirty = true;
      el.innerHTML = editorCompass(state.edit.sectors, true);
    });
    const stop = () => {
      if (!drag) return;
      drag = null;
      state.map.dragging.enable();
      drawEditor(admin, unit);
    };
    el.addEventListener("pointerup", stop);
    el.addEventListener("pointercancel", stop);
    el.addEventListener("click", (ev) => {
      const hit = ev.target.closest("[data-oct]");
      if (!hit) return;
      const e = state.edit;
      const toggles = OCTANTS.map((_, i) => e.sectors.length > 0 && inSectors(i * 45, e.sectors));
      const i = Number(hit.dataset.oct);
      toggles[i] = !toggles[i];
      e.sectors = sectorsFromToggles(toggles, e.sectors);
      e.dirty = true;
      drawEditor(admin, unit);
    });
  }

  async function renderEditor(ticket, name) {
    title.textContent = name || "Jauns spots";
    back.hidden = false;
    if (tg && tg.BackButton) tg.BackButton.show();
    const [list] = await Promise.all([api("api/spots"), loadLeaflet()]);
    if (ticket !== state.ticket) return;
    const spot = name ? list.spots.find((s) => s.name === name) : null;
    if (name && !spot) { view.innerHTML = '<p class="msg">Šāda spota nav.</p>'; return; }
    if (!state.edit || state.edit.key !== (name || "")) {
      state.edit = spot
        ? { ...spot, sectors: spot.sectors.map((s) => [...s]), original: spot.name, key: name, dirty: false }
        : { name: "", lat: null, lon: null, min_wind: 6, max_wind: 20, sectors: [], cell: "sea", original: null, key: "", dirty: false };
      state.moving = false;
    }
    if (!list.is_admin && !spot) { view.innerHTML = '<p class="msg">Spotus pievienot var tikai admini.</p>'; return; }
    view.innerHTML = '<div id="map" class="map"></div><div id="spot-form"></div>';
    const L = window.L;
    const e = state.edit;
    const home = list.spots[0];
    const center = e.lat !== null ? [e.lat, e.lon] : home ? [home.lat, home.lon] : [57.0, 24.0];
    state.map = L.map("map").setView(center, e.lat !== null ? 13 : 9);
    state.marker = null;
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(state.map);
    list.spots.filter((s) => s.name !== e.original).forEach((s) => {
      L.circleMarker([s.lat, s.lon], { radius: 6, color: "#10202E", weight: 2, fillColor: "#9CC9DE", fillOpacity: 1 })
        .bindTooltip(s.name).addTo(state.map);
    });
    if (list.is_admin) {
      state.map.on("click", (ev) => {
        if (e.lat !== null && !state.moving) return;
        e.lat = Number(ev.latlng.lat.toFixed(5));
        e.lon = Number(ev.latlng.lng.toFixed(5));
        e.dirty = true;
        state.moving = false;
        drawEditor(true, list.unit);
      });
    }
    state.editUnit = list.unit;
    state.editAdmin = list.is_admin;
    drawEditor(list.is_admin, list.unit);
  }

  async function editorAction(name) {
    const e = state.edit;
    if (!e) return;
    const stepWind = (key, delta) => {
      e[key] = Math.max(0.5, Math.min(60, e[key] + delta));
      e.dirty = true;
    };
    if (name === "min-dec") stepWind("min_wind", -0.5);
    else if (name === "min-inc") stepWind("min_wind", 0.5);
    else if (name === "max-dec") stepWind("max_wind", -0.5);
    else if (name === "max-inc") stepWind("max_wind", 0.5);
    else if (name === "spot-move") state.moving = !state.moving;
    else if (name === "spot-save") {
      try {
        await api("api/spots/save", {
          original: e.original, name: e.name.trim(), lat: e.lat, lon: e.lon,
          min_wind: e.min_wind, max_wind: e.max_wind, sectors: e.sectors, cell: e.cell,
        });
        state.cache = {};
        const saved = e.name.trim();
        state.edit = null;
        notify("Spots saglabāts.", true);
        location.hash = `#/spot/${encodeURIComponent(saved)}`;
      } catch (err) { notify(err.message, false); }
      return;
    } else if (name === "spot-delete") {
      const go = async () => {
        try {
          await api("api/spots/delete", { name: e.original });
          state.cache = {};
          state.edit = null;
          location.hash = "#/";
        } catch (err) { notify(err.message, false); }
      };
      const question = `Dzēst spotu „${e.original}”?`;
      if (tg && tg.showConfirm && tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) {
        tg.showConfirm(question, (ok) => { if (ok) go(); });
      } else if (window.confirm(question)) go();
      return;
    }
    drawEditor(state.editAdmin, state.editUnit);
  }

  // --- navigation ----------------------------------------------------------------

  function currentPage() {
    const hash = location.hash;
    if (hash.startsWith("#/spot/")) return { page: "spot", name: decodeURIComponent(hash.slice(7)) };
    if (hash === "#/route") return { page: "route" };
    if (hash === "#/profile") return { page: "profile" };
    if (hash === "#/riders") return { page: "riders" };
    if (hash === "#/edit") return { page: "edit", name: null };
    if (hash.startsWith("#/edit/")) return { page: "edit", name: decodeURIComponent(hash.slice(7)) };
    return { page: "forecast" };
  }

  async function render() {
    if (!initData()) {
      view.innerHTML = '<p class="msg">Atver šo lietotni no KiteBot Telegram čata.</p>';
      return;
    }
    const ticket = ++state.ticket;
    const { page, name } = currentPage();
    const tab = page === "spot" || page === "edit" ? "forecast" : page;
    tabs.forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
    if (page !== "spot" && page !== "edit") {
      back.hidden = true;
      if (tg && tg.BackButton) tg.BackButton.hide();
    }
    if (state.map) { state.map.remove(); state.map = null; state.marker = null; }
    if (page !== "edit") state.edit = null;
    const cached = { profile: state.profile, riders: state.riders, route: null, edit: null }[page]
      ?? state.cache[state.day];
    if (!cached) view.innerHTML = '<p class="msg">Ielādē…</p>';
    try {
      if (page === "route") { await renderRoute(ticket); return; }
      if (page === "profile") { await renderProfile(ticket); return; }
      if (page === "riders") { await renderRiders(ticket); return; }
      if (page === "edit") { await renderEditor(ticket, name); return; }
      const data = await load(state.day);
      if (ticket !== state.ticket) return;  // a newer tap already took over
      if (page === "spot") renderSpot(data, name);
      else renderOverview(data);
    } catch (err) {
      if (ticket === state.ticket) view.innerHTML = `<p class="msg">${esc(err.message)}</p>`;
    }
  }

  view.addEventListener("click", (event) => {
    const option = event.target.closest("[data-seg] button");
    if (option) {
      const segName = option.parentElement.dataset.seg;
      setSeg(segName, option.dataset.val);
      if (segName === "cell") drawEditor(state.editAdmin, state.editUnit);  // keep the map
      else render();
      return;
    }
    const action = event.target.closest("[data-act]");
    if (!action) return;
    const act = action.dataset.act;
    if (act === "share") shareRoute(action);
    else if (act === "rider-toggle") {
      const who = action.dataset.name;
      if (state.hidden.has(who)) state.hidden.delete(who); else state.hidden.add(who);
      render();
    } else if (/^(min|max)-(dec|inc)$|^spot-/.test(act)) editorAction(act);
    else profileAction(act, action);
  });
  view.addEventListener("input", (event) => {
    if (event.target.id !== "sp-name" || !state.edit) return;
    state.edit.name = event.target.value;
    state.edit.dirty = true;
    const save = view.querySelector('[data-act="spot-save"]');
    if (save) save.disabled = !(state.edit.name.trim() && state.edit.lat !== null);
  });
  view.addEventListener("change", (event) => {
    const id = event.target.id;
    if (id === "dry") state.dry = event.target.checked;
    else if (id === "windy") {
      state.windyOnly = event.target.checked;
      try { localStorage.setItem("kb.windyOnly", state.windyOnly ? "1" : "0"); } catch (e) { /* storage off */ }
    }
    else if (id === "rdry") state.route.dry = event.target.checked;
    else if (id === "rhome") state.route.home = event.target.checked;
    else return;
    render();
  });
  window.addEventListener("hashchange", render);

  if (tg) {
    tg.ready();
    tg.expand();
    const scheme = () => { document.documentElement.dataset.scheme = tg.colorScheme || "light"; };
    scheme();
    tg.onEvent("themeChanged", scheme);
    if (tg.BackButton) {
      tg.BackButton.onClick(() => {
        if (history.length > 1) history.back();
        else location.hash = "#/";
      });
    }
    // t.me/<bot>/<app>?startapp=d1 opens on tomorrow, d2 the day after
    const start = tg.initDataUnsafe && tg.initDataUnsafe.start_param;
    const match = start && /^d(\d)$/.exec(start);
    if (match) state.day = Number(match[1]);
  }
  render();
})();
