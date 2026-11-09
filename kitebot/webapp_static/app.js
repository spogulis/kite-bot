/* KiteBot Mini App: spot overview and per-spot hourly chart.
   Plain browser JS, no build step. All data comes from api/forecast, which
   only answers when Telegram's signed initData is sent along. */
"use strict";
(function () {
  const tg = window.Telegram && window.Telegram.WebApp;
  const view = document.getElementById("view");
  const title = document.getElementById("title");
  const back = document.getElementById("back");
  const state = {
    day: 0, dry: false, cache: {}, ticket: 0,
    route: { day: 0, prefer: "any", km: 100, dry: false, home: true },
    profile: null, draft: null,
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
    const cards = data.spots.map((s) => {
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
    view.innerHTML = dayTabs(data)
      + `<div class="bar"><label class="toggle"><input type="checkbox" id="dry"${state.dry ? " checked" : ""}> ☀️ Bez lietus</label>`
      + '<span><i class="sw" style="background:var(--kite)"></i>kaits<i class="sw" style="background:var(--foil)"></i>matracis</span></div>'
      + `<div class="axis"><span>${pad(data.day_start)}</span><span>${pad(mid)}</span><span>${pad(data.day_end)}</span></div>`
      + (cards.length ? cards.join("") : '<p class="msg">Vēl nav neviena spota.</p>')
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

  // --- navigation ----------------------------------------------------------------

  function currentPage() {
    const hash = location.hash;
    if (hash.startsWith("#/spot/")) return { page: "spot", name: decodeURIComponent(hash.slice(7)) };
    if (hash === "#/route") return { page: "route" };
    if (hash === "#/profile") return { page: "profile" };
    return { page: "forecast" };
  }

  async function render() {
    if (!initData()) {
      view.innerHTML = '<p class="msg">Atver šo lietotni no KiteBot Telegram čata.</p>';
      return;
    }
    const ticket = ++state.ticket;
    const { page, name } = currentPage();
    tabs.forEach((a) => a.classList.toggle("on", a.dataset.tab === (page === "spot" ? "forecast" : page)));
    if (page !== "spot") {
      back.hidden = true;
      if (tg && tg.BackButton) tg.BackButton.hide();
    }
    const cached = page === "profile" ? state.profile : page === "route" ? null : state.cache[state.day];
    if (!cached) view.innerHTML = '<p class="msg">Ielādē…</p>';
    try {
      if (page === "route") { await renderRoute(ticket); return; }
      if (page === "profile") { await renderProfile(ticket); return; }
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
      setSeg(option.parentElement.dataset.seg, option.dataset.val);
      render();
      return;
    }
    const action = event.target.closest("[data-act]");
    if (!action) return;
    if (action.dataset.act === "share") shareRoute(action);
    else profileAction(action.dataset.act, action);
  });
  view.addEventListener("change", (event) => {
    const id = event.target.id;
    if (id === "dry") state.dry = event.target.checked;
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
    if (tg.BackButton) tg.BackButton.onClick(() => { location.hash = "#/"; });
    // t.me/<bot>/<app>?startapp=d1 opens on tomorrow, d2 the day after
    const start = tg.initDataUnsafe && tg.initDataUnsafe.start_param;
    const match = start && /^d(\d)$/.exec(start);
    if (match) state.day = Number(match[1]);
  }
  render();
})();
