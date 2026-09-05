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

  let VENUES = ["binance", "bybit", "okx", "mexc", "kucoin", "gate", "bitget", "htx"];
  let MARKETS = ["spot", "futures", "inverse"];
  let PRESETS = [];
  let preset = "";
  let board = "carry";

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
  const MKT_LABEL = { spot: "spot", futures: "perp", inverse: "coin-m" };
  const mkt = (m) => `<span class="mkt-tag ${m}">${MKT_LABEL[m] || m}</span>`;

  // ------------------------------------------------------------- filters
  function chips() {
    const vc = $("uniVenueChips");
    const mc = $("uniMarketChips");
    const pc = $("uniPresetChips");
    if (!vc || !mc) return;
    if (pc) {
      pc.innerHTML =
        `<button class="chip ${preset ? "" : "on"}" data-kind="preset" data-value="">All instruments</button>` +
        PRESETS.map(
          (p) =>
            `<button class="chip ${preset === p.id ? "on" : ""}" data-kind="preset" data-value="${p.id}" title="${p.desc}">${p.label}</button>`
        ).join("");
      pc.querySelectorAll(".chip").forEach((b) => {
        b.onclick = () => {
          preset = b.dataset.value === preset ? "" : b.dataset.value;
          const spec = PRESETS.find((p) => p.id === preset);
          if (spec) {
            // a preset owns the filter bar: coin-margined books are USD-quoted,
            // so a leftover USDT filter would silently return nothing
            $("uniQuote").value = spec.params.quote || "";
            $("uniSort").value = spec.params.sort || "volume";
            marketSel.clear();
            if (spec.params.market) marketSel.add(spec.params.market);
          }
          chips();
          load();
        };
      });
    }
    const chip = (label, on, kind, value) =>
      `<button class="chip ${on ? "on" : ""}" data-kind="${kind}" data-value="${value}">${label}</button>`;
    vc.innerHTML =
      chip("All venues", venueSel.size === 0, "venue", "") +
      VENUES.map((v) => chip(v, venueSel.has(v), "venue", v)).join("");
    const mLabel = { spot: "spot", futures: "linear perps", inverse: "coin-margined" };
    mc.innerHTML =
      chip("All markets", marketSel.size === 0, "market", "") +
      MARKETS.map((m) => chip(mLabel[m] || m, marketSel.has(m), "market", m)).join("");
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
    const chg = parseFloat($("uniChangeMin").value);
    if (!isNaN(chg)) p.set("change_min", chg);
    const vol = parseFloat($("uniMinVol").value);
    if (!isNaN(vol)) p.set("min_volume", vol);
    if (preset) p.set("preset", preset);
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
            <td><b class="uni-link" data-coin="${r.symbol}">${r.base}</b><span class="muted">/${r.quote}</span></td>
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
          <td><b class="uni-link" data-coin="${r.symbol}">${r.symbol}</b></td>
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
    body.querySelectorAll("[data-coin]").forEach((el) => {
      el.onclick = () => openCoin(el.dataset.coin);
    });
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
      cell("Perps", st.futures) +
      cell("Coin-M", st.inverse || 0) +
      cell("Venues", (st.venues || []).length) +
      cell("24h volume", money(st.volume_usd)) +
      cell("Source", st.source) +
      cell("Age", st.age_sec === null || st.age_sec === undefined ? "—" : Math.round(st.age_sec) + "s");

    $("uniVenues").innerHTML = (st.venues || [])
      .map(
        (v) =>
          `<tr><td>${tag(v.venue)}</td><td>${v.spot}</td><td>${v.futures}${
            v.inverse ? ` <span class="muted">+${v.inverse} coin-m</span>` : ""
          }</td><td><b>${v.total}</b></td><td>${v.coins}</td><td class="mono">${money(v.volume_usd)}</td></tr>`
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
      meta.textContent = `${st.instruments} instruments · ${st.coins} coins · ${st.spot} spot / ${st.futures} perps / ${
        st.inverse || 0
      } coin-margined across ${(st.venues || []).length} venues`;
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

  // -------------------------------------------------------- extra boards
  const BOARDS = {
    carry: {
      title: "Cash-and-carry",
      sub: "Buy spot on the cheapest venue, short the perp that pays the most — funding APR + basis, before fees",
      url: "/api/instruments/carry?limit=20",
      cols: ["Coin", "Long spot", "Short perp", "Basis", "Funding APR", "Carry APR"],
      row: (r) => `<tr>
        <td><b class="uni-link" data-coin="${r.symbol}">${r.base}</b></td>
        <td>${tag(r.spot_venue)}<span class="mono">${px(r.spot_price)}</span></td>
        <td>${tag(r.perp_venue)}<span class="mono">${px(r.perp_price)}</span></td>
        <td>${pct(r.basis_pct, 3)}</td>
        <td class="mono ${r.funding_apr >= 0 ? "up" : "down"}">${r.funding_apr.toFixed(1)}%</td>
        <td class="mono ${r.carry_apr >= 0 ? "up" : "down"}"><b>${r.carry_apr.toFixed(1)}%</b></td></tr>`,
    },
    movers: {
      title: "24h movers",
      sub: "Best and worst performers across every venue, one row per coin",
      url: "/api/instruments/movers?limit=12",
      cols: ["Gainer", "Venue", "24h", "", "Loser", "Venue", "24h"],
      rows: (data) => {
        const g = data.gainers || [];
        const l = data.losers || [];
        const n = Math.max(g.length, l.length);
        let html = "";
        for (let i = 0; i < n; i++) {
          const a = g[i];
          const b = l[i];
          html += `<tr>
            <td>${a ? `<b class="uni-link" data-coin="${a.symbol}">${a.base}</b>` : ""}</td>
            <td>${a ? tag(a.venue) + mkt(a.market) : ""}</td>
            <td>${a ? pct(a.change_pct) : ""}</td>
            <td></td>
            <td>${b ? `<b class="uni-link" data-coin="${b.symbol}">${b.base}</b>` : ""}</td>
            <td>${b ? tag(b.venue) + mkt(b.market) : ""}</td>
            <td>${b ? pct(b.change_pct) : ""}</td></tr>`;
        }
        return html;
      },
    },
    exclusives: {
      title: "Venue exclusives",
      sub: "Coins listed on exactly one venue — listing alpha, and listing risk",
      url: "/api/instruments/exclusives?limit=30",
      cols: ["Coin", "Only on", "Markets", "Price", "24h", "Volume"],
      row: (r) => `<tr>
        <td><b class="uni-link" data-coin="${r.symbol}">${r.base}</b></td>
        <td>${tag(r.venue)}</td>
        <td>${(r.markets || []).map(mkt).join("")}</td>
        <td class="mono">${px(r.last)}</td>
        <td>${pct(r.change_pct)}</td>
        <td class="mono">${money(r.volume_usd)}</td></tr>`,
    },
  };

  async function loadBoard() {
    const spec = BOARDS[board];
    if (!spec) return;
    $("uniBoardTitle").textContent = spec.title;
    $("uniBoardSub").textContent = spec.sub;
    $("uniBoardHead").innerHTML = spec.cols.map((c) => `<th>${c}</th>`).join("");
    const data = await get(spec.url);
    const body = $("uniBoardBody");
    if (spec.rows) body.innerHTML = spec.rows(data);
    else {
      const list = data.rows || [];
      body.innerHTML = list.length
        ? list.map(spec.row).join("")
        : `<tr><td colspan="${spec.cols.length}" class="hint">Nothing to show yet.</td></tr>`;
    }
    body.querySelectorAll("[data-coin]").forEach((el) => {
      el.onclick = () => openCoin(el.dataset.coin);
    });
  }

  // ------------------------------------------------------- coin detail drawer
  async function openCoin(symbol) {
    const data = await get("/api/instruments/symbol/" + encodeURI(symbol));
    const listings = data.listings || [];
    $("uniDrawerTitle").textContent = data.symbol;
    $("uniDrawerSub").textContent = `${listings.length} listings · ${data.venues.join(", ")}`;
    const vol = listings.reduce((a, r) => a + (r.volume_usd || 0), 0);
    const funds = listings.filter((r) => r.funding_rate !== null && r.funding_rate !== undefined);
    const cell = (l, v) => `<div class="m"><span>${l}</span><b>${v}</b></div>`;
    $("uniDrawerStats").innerHTML =
      cell("Venues", data.venues.length) +
      cell("Markets", (data.markets || []).map((m) => MKT_LABEL[m] || m).join(" · ") || "—") +
      cell("Venue spread", (data.spread_pct || 0).toFixed(3) + "%") +
      cell("Total volume", money(vol)) +
      cell("Avg funding", funds.length ? fund(funds.reduce((a, r) => a + r.funding_rate, 0) / funds.length) : "—") +
      cell("Watched", data.in_watchlist ? "yes" : "no");
    $("uniDrawerBody").innerHTML = listings
      .sort((a, b) => (b.volume_usd || 0) - (a.volume_usd || 0))
      .map(
        (r) => `<tr><td>${tag(r.venue)}</td><td>${mkt(r.market)}</td><td class="muted">${r.contract}</td>
          <td class="mono">${px(r.last)}</td><td>${pct(r.change_pct)}</td>
          <td class="mono">${money(r.volume_usd)}</td><td>${fund(r.funding_rate)}</td>
          <td class="mono">${r.open_interest ? money(r.open_interest) : "—"}</td></tr>`
      )
      .join("");
    $("uniDrawerWatch").onclick = async () => {
      const res = await post("/api/instruments/watch", { symbol: data.symbol });
      say(res.added && res.added.length ? "Watching " + res.added.join(", ") : data.symbol + " already watched");
    };
    $("uniDrawerBack").classList.add("on");
  }

  function closeCoin() {
    $("uniDrawerBack").classList.remove("on");
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

  async function loadMeta() {
    try {
      const meta = await get("/api/instruments/presets");
      PRESETS = meta.presets || [];
      VENUES = meta.venues || VENUES;
      MARKETS = meta.markets || MARKETS;
      chips();
      if (active()) load();
    } catch {}
  }

  function boot() {
    if (!$("uniBody")) return;
    // wire everything synchronously first — the venue/preset metadata arrives
    // over the network and the user may click the tab before it lands
    chips();
    ["uniQuote", "uniSort", "uniMode"].forEach((id) => ($(id).onchange = load));
    $("uniLimit").onchange = load;
    $("uniChangeMin").onchange = load;
    $("uniMinVol").onchange = load;
    $("uniClear").onclick = () => {
      preset = "";
      venueSel.clear();
      marketSel.clear();
      $("uniSearch").value = "";
      $("uniChangeMin").value = "";
      $("uniMinVol").value = "";
      $("uniSort").value = "volume";
      chips();
      load();
    };
    $("uniBoardTabs").querySelectorAll(".tab").forEach((t) => {
      t.onclick = () => {
        board = t.dataset.board;
        $("uniBoardTabs").querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
        loadBoard();
      };
    });
    $("uniDrawerClose").onclick = closeCoin;
    $("uniDrawerBack").onclick = (e) => {
      if (e.target === $("uniDrawerBack")) closeCoin();
    };
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeCoin();
    });
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
      loadBoard();
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
          loadBoard();
        }
      };
    });
    if (timer) clearInterval(timer);
    timer = setInterval(() => {
      if (active()) load();
    }, 30000);
    loadMeta();
  }

  window.UniverseView = { load, boot, openCoin, loadBoard };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
