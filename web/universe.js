/* FablMyLog — multi-venue instrument universe (Binance · Bybit · OKX · MEXC,
   spot and futures). Self-contained IIFE so nothing clashes with the other
   classic scripts. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const get = async (u) => (await fetch(u)).json();
  const post = async (u, b) =>
    (
      await fetch(u, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: b ? JSON.stringify(b) : undefined,
      })
    ).json();
  const say = (m) => (window.FML ? window.FML.toast(m) : null);

  const VENUES = ["binance", "bybit", "okx", "mexc"];
  const MARKETS = ["spot", "futures"];

  let venueSel = new Set();
  let marketSel = new Set();
  let rows = [];
  let mode = "instruments";
  let busy = false;
  let queued = false;
  let timer = null;

  function money(v) {
    const n = Number(v || 0);
    if (n >= 1e9) return "$" + (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return "$" + (n / 1e3).toFixed(1) + "K";
    return "$" + n.toFixed(0);
  }
  function px(v) {
    const n = Number(v || 0);
    if (!n) return "—";
    if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (n >= 1) return n.toFixed(4);
    return n.toPrecision(4);
  }
  function pct(v, d = 2) {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    return `<span class="${n >= 0 ? "up" : "down"}">${(n >= 0 ? "+" : "") + n.toFixed(d)}%</span>`;
  }
  function fund(v) {
    if (v === null || v === undefined) return "—";
    const n = Number(v) * 100;
    return `<span class="${n >= 0 ? "up" : "down"}">${(n >= 0 ? "+" : "") + n.toFixed(4)}%</span>`;
  }
  const tag = (v) => `<span class="venue-tag ${v}">${v}</span>`;
  const mkt = (m) => `<span class="mkt-tag ${m}">${m === "futures" ? "perp" : "spot"}</span>`;

  // ------------------------------------------------------------- filters
  function chips() {
    const vc = $("uniVenueChips");
    const mc = $("uniMarketChips");
    if (!vc || !mc) return;
    const chip = (label, on, kind, value) =>
      `<button class="chip ${on ? "on" : ""}" data-kind="${kind}" data-value="${value}">${label}</button>`;
    vc.innerHTML =
      chip("All venues", venueSel.size === 0, "venue", "") +
      VENUES.map((v) => chip(v, venueSel.has(v), "venue", v)).join("");
    mc.innerHTML =
      chip("Spot + futures", marketSel.size === 0, "market", "") +
      MARKETS.map((m) => chip(m === "futures" ? "futures / perps" : "spot", marketSel.has(m), "market", m)).join("");
    [...vc.querySelectorAll(".chip"), ...mc.querySelectorAll(".chip")].forEach((b) => {
      b.onclick = () => {
        const set = b.dataset.kind === "venue" ? venueSel : marketSel;
        if (!b.dataset.value) set.clear();
        else if (set.has(b.dataset.value)) set.delete(b.dataset.value);
        else set.add(b.dataset.value);
        chips();
        load();
      };
    });
  }

  function params() {
    const p = new URLSearchParams();
    if (venueSel.size) p.set("venue", [...venueSel].join(","));
    if (marketSel.size) p.set("market", [...marketSel].join(","));
    const q = $("uniQuote").value;
    if (q) p.set("quote", q);
    const s = $("uniSearch").value.trim();
    if (s) p.set("search", s);
    p.set("sort", $("uniSort").value);
    p.set("limit", $("uniLimit").value || 60);
    return p;
  }

  // -------------------------------------------------------------- render
  const COLS = [
    { l: "Symbol", k: "symbol" },
    { l: "Venue", k: "venue" },
    { l: "Market", k: "market" },
    { l: "Price", k: "last" },
    { l: "24h", k: "change_pct" },
    { l: "Volume", k: "volume_usd" },
    { l: "Funding", k: "funding_rate" },
    { l: "Open interest", k: "open_interest" },
    { l: "", k: "act" },
  ];
  const COIN_COLS = [
    { l: "Coin", k: "base" },
    { l: "Venues", k: "venues" },
    { l: "Listings", k: "listings" },
    { l: "Price", k: "last" },
    { l: "24h", k: "change_pct" },
    { l: "Volume", k: "volume_usd" },
    { l: "Venue spread", k: "spread_pct" },
    { l: "Avg funding", k: "avg_funding" },
    { l: "", k: "act" },
  ];

  function renderTable() {
    const head = $("uniHead");
    const body = $("uniBody");
    const cols = mode === "coins" ? COIN_COLS : COLS;
    head.innerHTML = cols.map((c) => `<th>${c.l}</th>`).join("");
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="${cols.length}" class="hint">No instruments match those filters.</td></tr>`;
      return;
    }
    body.innerHTML = rows
      .map((r) => {
        if (mode === "coins") {
          return `<tr>
            <td><b>${r.base}</b><span class="muted">/${r.quote}</span></td>
            <td>${r.venues.map(tag).join("")}</td>
            <td>${r.listings} <span class="muted">${r.spot_venues}s/${r.perp_venues}p</span></td>
            <td class="mono">${px(r.last)}</td>
            <td>${pct(r.change_pct)}</td>
            <td class="mono">${money(r.volume_usd)}</td>
            <td class="mono ${r.spread_pct > 0.25 ? "warn" : ""}">${r.spread_pct.toFixed(3)}%</td>
            <td>${fund(r.avg_funding)}</td>
            <td><button class="btn tiny" data-watch="${r.symbol}">Watch</button></td>
          </tr>`;
        }
        return `<tr>
          <td><b>${r.symbol}</b></td>
          <td>${tag(r.venue)}</td>
          <td>${mkt(r.market)}</td>
          <td class="mono">${px(r.last)}</td>
          <td>${pct(r.change_pct)}</td>
          <td class="mono">${money(r.volume_usd)}</td>
          <td>${fund(r.funding_rate)}</td>
          <td class="mono">${r.open_interest ? money(r.open_interest) : "—"}</td>
          <td><button class="btn tiny" data-watch="${r.symbol}">Watch</button></td>
        </tr>`;
      })
      .join("");
    body.querySelectorAll("[data-watch]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        const res = await post("/api/instruments/watch", { symbol: b.dataset.watch });
        say(res.added && res.added.length ? "Watching " + res.added.join(", ") : b.dataset.watch + " already watched");
        b.disabled = false;
      };
    });
  }

  function renderStats(st) {
    const grid = $("uniStats");
    const cell = (l, v, cls) => `<div class="m"><span>${l}</span><b class="${cls || ""}">${v}</b></div>`;
    grid.innerHTML =
      cell("Instruments", st.instruments) +
      cell("Coins", st.coins) +
      cell("Spot", st.spot) +
      cell("Futures", st.futures) +
      cell("Venues", (st.venues || []).length) +
      cell("24h volume", money(st.volume_usd)) +
      cell("Source", st.source) +
      cell("Age", st.age_sec === null || st.age_sec === undefined ? "—" : Math.round(st.age_sec) + "s");

    $("uniVenues").innerHTML = (st.venues || [])
      .map(
        (v) =>
          `<tr><td>${tag(v.venue)}</td><td>${v.spot}</td><td>${v.futures}</td><td><b>${v.total}</b></td><td>${v.coins}</td><td class="mono">${money(v.volume_usd)}</td></tr>`
      )
      .join("");

    const banner = $("uniBanner");
    const failed = Object.keys(st.failed || {});
    if (st.source === "offline") {
      banner.innerHTML = `<div class="fb bad">Venue REST is unreachable from this host — showing the bundled offline catalog with <b>simulated prices</b>. ${
        failed.length ? failed.length + " catalogs failed." : ""
      }</div>`;
    } else if (failed.length) {
      banner.innerHTML = `<div class="fb">Live catalogs loaded, but ${failed.length} failed: ${failed.join(", ")}</div>`;
    } else {
      banner.innerHTML = "";
    }
    const meta = $("uniMeta");
    if (meta) {
      meta.textContent = `${st.instruments} instruments · ${st.coins} coins · ${st.spot} spot / ${st.futures} futures across ${
        (st.venues || []).length
      } venues`;
    }
  }

  async function loadArb() {
    const market = $("uniArbMarket").value;
    const data = await get(`/api/instruments/arb?market=${market}&limit=12`);
    $("uniArb").innerHTML = (data.rows || []).length
      ? data.rows
          .map(
            (r) =>
              `<tr><td><b>${r.base}</b></td><td>${tag(r.buy_venue)}<span class="mono">${px(r.buy_price)}</span></td>
               <td>${tag(r.sell_venue)}<span class="mono">${px(r.sell_price)}</span></td>
               <td class="mono ${r.spread_pct >= 0.3 ? "up" : ""}">${r.spread_pct.toFixed(3)}%</td>
               <td>${r.venues}</td></tr>`
          )
          .join("")
      : `<tr><td colspan="5" class="hint">Need the same coin on two venues to compare.</td></tr>`;
  }

  async function loadFunding() {
    const side = $("uniFundSide").value;
    const data = await get("/api/instruments/funding?limit=12");
    const list = data[side] || [];
    $("uniFunding").innerHTML = list.length
      ? list
          .map(
            (r) =>
              `<tr><td><b>${r.symbol}</b></td><td>${tag(r.venue)}</td><td>${fund(r.funding_rate)}</td>
               <td class="mono ${r.funding_apr >= 0 ? "up" : "down"}">${r.funding_apr === null ? "—" : r.funding_apr.toFixed(1) + "%"}</td>
               <td>${r.basis_pct === null ? "—" : pct(r.basis_pct, 3)}</td></tr>`
          )
          .join("")
      : `<tr><td colspan="5" class="hint">No perp data yet.</td></tr>`;
  }

  // -------------------------------------------------------------- loading
  async function load() {
    if (busy) {
      queued = true; // never drop a filter change — re-run once the in-flight load lands
      return;
    }
    busy = true;
    try {
      mode = $("uniMode").value;
      if (mode === "coins") {
        const q = $("uniQuote").value || "USDT";
        const data = await get(`/api/instruments/coins?quote=${q}&limit=${$("uniLimit").value || 60}`);
        rows = data.rows || [];
        $("uniCount").textContent = `${rows.length} coins merged across venues`;
      } else {
        const data = await get("/api/instruments?" + params().toString());
        rows = data.rows || [];
        $("uniCount").textContent = `${rows.length} shown of ${data.total} matching instruments`;
      }
      renderTable();
      renderStats(await get("/api/instruments/stats"));
    } catch (err) {
      console.warn("universe", err);
    } finally {
      busy = false;
      if (queued) {
        queued = false;
        load();
      }
    }
  }

  function active() {
    const v = document.querySelector('.view[data-view="universe"]');
    return v && v.classList.contains("on");
  }

  function boot() {
    if (!$("uniBody")) return;
    chips();
    ["uniQuote", "uniSort", "uniMode"].forEach((id) => ($(id).onchange = load));
    $("uniLimit").onchange = load;
    let t = null;
    $("uniSearch").oninput = () => {
      clearTimeout(t);
      t = setTimeout(load, 250);
    };
    $("uniArbMarket").onchange = loadArb;
    $("uniFundSide").onchange = loadFunding;
    $("uniReload").onclick = async () => {
      const btn = $("uniReload");
      btn.disabled = true;
      btn.textContent = "Refreshing…";
      const res = await post("/api/instruments/refresh");
      btn.disabled = false;
      btn.textContent = "Refresh catalogs";
      say(`${(res.stats || {}).instruments || 0} instruments loaded`);
      await load();
      loadArb();
      loadFunding();
    };
    $("uniCsv").onclick = () => {
      window.open("/api/instruments/export.csv?" + params().toString(), "_blank");
    };
    $("uniWatchTop").onclick = async () => {
      const syms = rows.slice(0, 5).map((r) => r.symbol);
      if (!syms.length) return;
      const res = await post("/api/instruments/watch", { symbols: syms });
      say(res.added && res.added.length ? "Watching " + res.added.join(", ") : "Already watching those");
    };

    document.querySelectorAll(".nav").forEach((btn) => {
      const prev = btn.onclick;
      btn.onclick = (e) => {
        if (prev) prev(e);
        if (btn.dataset.view === "universe") {
          load();
          loadArb();
          loadFunding();
        }
      };
    });
    if (timer) clearInterval(timer);
    timer = setInterval(() => {
      if (active()) load();
    }, 30000);
  }

  window.UniverseView = { load, boot };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
