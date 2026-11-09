/* KiteBot Mini App: spot overview and per-spot hourly chart.
   Plain browser JS, no build step. All data comes from api/forecast, which
   only answers when Telegram's signed initData is sent along. */
"use strict";
(function () {
  const tg = window.Telegram && window.Telegram.WebApp;
  const view = document.getElementById("view");
  const title = document.getElementById("title");
  const back = document.getElementById("back");
  const state = { day: 0, dry: false, cache: {}, ticket: 0 };
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

  async function load(day) {
    if (state.cache[day]) return state.cache[day];
    const res = await fetch(`api/forecast?day=${day}`, {
      headers: { "X-Telegram-Init-Data": initData() },
    });
    let body = {};
    try { body = await res.json(); } catch (e) { /* not JSON: keep the status */ }
    if (!res.ok) throw new Error(body.error || `Kļūda ${res.status}`);
    state.cache[day] = body;
    return body;
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

  function dayTabs(data) {
    return `<div class="seg" role="tablist">${data.days.map((d) => (
      `<button role="tab" data-day="${d.offset}" aria-selected="${d.offset === state.day}">${esc(d.label)}</button>`
    )).join("")}</div>`;
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

  async function render() {
    if (!initData()) {
      view.innerHTML = '<p class="msg">Atver šo lietotni no KiteBot Telegram čata.</p>';
      return;
    }
    const ticket = ++state.ticket;
    const route = /^#\/spot\/(.+)$/.exec(location.hash);
    if (!state.cache[state.day]) view.innerHTML = '<p class="msg">Ielādē prognozi…</p>';
    try {
      const data = await load(state.day);
      if (ticket !== state.ticket) return;  // a newer tap already took over
      if (route) renderSpot(data, decodeURIComponent(route[1]));
      else renderOverview(data);
    } catch (err) {
      if (ticket === state.ticket) view.innerHTML = `<p class="msg">${esc(err.message)}</p>`;
    }
  }

  view.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-day]");
    if (!tab) return;
    state.day = Number(tab.dataset.day);
    render();
  });
  view.addEventListener("change", (event) => {
    if (event.target.id !== "dry") return;
    state.dry = event.target.checked;
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
