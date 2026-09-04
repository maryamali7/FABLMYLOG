const $ = (id) => document.getElementById(id);

let state = {};
let tickers = [];
let selected = "BTC/USDT";
let candles = [];
let universe = [];
let screener = { boards: {}, meta: {}, heatmap: [], alerts: [] };
let boardKey = "alpha";

function fmt(n, d = 2) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  n = Number(n);
  const abs = Math.abs(n);
  if (abs >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: d });
  if (abs !== 0 && abs < 0.001) return n.toPrecision(3);
  return n.toLocaleString(undefined, { maximumFractionDigits: abs < 1 ? 6 : d });
}

function pct(n) {
  if (n == null) return "—";
  const v = Number(n);
  const s = (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  return s;
}

function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.style.display = "block";
  setTimeout(() => (el.style.display = "none"), 2800);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "hello" || msg.type === "state") {
      if (msg.state) state = msg.state;
      if (msg.tickers) tickers = msg.tickers;
      render();
      if (msg.tape) renderTape(msg.tape);
      if (msg.signals) renderSignals(msg.signals);
      if (msg.arb) renderArb(msg.arb);
      if (msg.events) renderJournal(msg.events);
      if (msg.screener) {
        screener = msg.screener;
        renderScreener();
        renderHeat();
      }
      if (msg.alerts) renderAlerts(msg.alerts);
    }
    if (msg.type === "event") {
      renderJournal([msg, ...(state._events || [])]);
    }
  };
  ws.onclose = () => setTimeout(connect, 1500);
}

function render() {
  const s = state;
  const mode = (s.mode || "paper").toUpperCase();
  $("modePill").textContent = mode;
  $("modePill").className = "pill" + (mode === "LIVE" ? " warn" : "");
  const run = s.paused ? "PAUSED" : s.running ? "RUNNING 24/7" : "STOPPED";
  $("runPill").textContent = s.halted ? "HALTED" : run;
  $("runPill").className = "pill" + (s.halted || s.paused ? " warn" : "");
  const up = s.uptime || 0;
  const hh = String(Math.floor(up / 3600)).padStart(2, "0");
  const mm = String(Math.floor((up % 3600) / 60)).padStart(2, "0");
  const ss = String(Math.floor(up % 60)).padStart(2, "0");
  $("upPill").textContent = `UP ${hh}:${mm}:${ss}`;
  const rg = s.regime || {};
  $("regimePill").textContent = (rg.name || "regime").replace("_", " ").toUpperCase();
  $("regimePill").className = "pill " + (rg.risk_on === false ? "warn" : "gold");

  const pnl = (s.equity || 0) - (s.starting_equity || 0);
  const pnlCls = pnl >= 0 ? "up" : "down";
  $("kpis").innerHTML = [
    kpi("Equity", "$" + fmt(s.equity, 2), `${s.positions ? s.positions.length : 0} open`),
    kpi("PnL", (pnl >= 0 ? "+" : "") + "$" + fmt(pnl, 2), `realized ${fmt(s.realized, 2)}`, pnlCls),
    kpi("Cash", "$" + fmt(s.cash, 2), `exposure $${fmt(s.exposure, 2)}`),
    kpi("Win rate", ((s.win_rate || 0) * 100).toFixed(1) + "%", `${s.wins || 0}W / ${s.losses || 0}L`),
    kpi("Drawdown", ((s.drawdown || 0) * 100).toFixed(2) + "%", s.halt_reason || "risk engine armed"),
    kpi("Universe", fmt(s.universe_size || 0, 0), `${(s.watchlist || []).length} on sockets`),
  ].join("");

  renderFeeds(s.feeds || []);
  const liveFeeds = (s.feeds || []).filter((f) => f.name !== "sim" && f.connected && !f.stale);
  const simOn = (s.feeds || []).some((f) => f.name === "sim" && f.connected);
  if (simOn && !liveFeeds.length) {
    $("uniHint").textContent =
      "Live exchange TLS is blocked on this host. Paper engine is running on a simulated multi-venue tape. Real Binance / Bybit / OKX / Coinbase / Kraken websockets start automatically when the network allows.";
  }
  renderTickers();
  renderPositions(s.positions || []);
  renderStrats(s.strategies || []);
  if (s.alerts) renderAlerts(s.alerts);
}

function kpi(lbl, val, sub, cls = "") {
  return `<div class="kpi"><div class="lbl">${lbl}</div><div class="val ${cls}">${val}</div><div class="sub">${sub || ""}</div></div>`;
}

function renderFeeds(feeds) {
  $("feeds").innerHTML = feeds
    .map(
      (f) =>
        `<span class="feed ${f.connected && !f.stale ? "on" : "off"}">${f.name} ${
          f.connected ? "live" : "down"
        } · ${fmt(f.messages, 0)} msgs</span>`
    )
    .join("");
}

function renderTickers() {
  const rows = tickers.length ? tickers : [];
  $("tickerBody").innerHTML = rows
    .map((t) => {
      const chg = Number(t.change_pct || 0);
      const venues = Object.keys(t.venues || {}).join(" · ") || t.exchange;
      return `<tr data-sym="${t.symbol}">
        <td>${t.symbol}</td>
        <td>${fmt(t.last)}</td>
        <td class="${chg >= 0 ? "up" : "down"}">${pct(chg)}</td>
        <td>${fmt(t.spread_bps, 1)}</td>
        <td class="venues">${venues}</td>
      </tr>`;
    })
    .join("");
  $("tickerBody").querySelectorAll("tr").forEach((tr) => {
    tr.onclick = () => select(tr.dataset.sym);
  });
}

function renderPositions(pos) {
  if (!pos.length) {
    $("positions").innerHTML = `<div class="muted">No inventory. Ensemble is waiting for confluence.</div>`;
    return;
  }
  $("positions").innerHTML = pos
    .map((p) => {
      const u = Number(p.unrealized || 0);
      return `<div class="row">
        <div><b>${p.symbol}</b><br/><span class="muted">${p.strategy} · ${fmt(p.qty, 6)}${p.scaled ? " · scaled" : ""}</span></div>
        <div class="${u >= 0 ? "up" : "down"}">${u >= 0 ? "+" : ""}${fmt(u, 2)}
        <button class="xbtn" data-close="${p.symbol}">close</button><br/>
        <span class="muted">mark ${fmt(p.mark)} stop ${fmt(p.stop)}</span></div>
      </div>`;
    })
    .join("");
  $("positions").querySelectorAll("[data-close]").forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      fetch("/api/close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: btn.dataset.close }),
      }).then(() => toast("Closed " + btn.dataset.close));
    };
  });
}

function renderSignals(rows) {
  const action = (rows || []).filter((s) => s.kind !== "hold").slice(0, 18);
  if (!action.length) {
    $("signals").innerHTML = `<div class="muted">Listening…</div>`;
    return;
  }
  $("signals").innerHTML = action
    .map(
      (s) => `<div class="row">
        <div><b class="${s.kind}">${s.kind.toUpperCase()}</b> ${s.symbol}<br/><span class="muted">${s.strategy} · ${s.reason}</span></div>
        <div>${(s.confidence * 100).toFixed(0)}%</div>
      </div>`
    )
    .join("");
}

function renderArb(rows) {
  if (!rows || !rows.length) {
    $("arb").innerHTML = `<div class="muted">No crossed books right now.</div>`;
    return;
  }
  $("arb").innerHTML = rows
    .slice(0, 12)
    .map(
      (a) => `<div class="row">
        <div><b>${a.symbol}</b><br/><span class="muted">buy ${a.buy_ex} → sell ${a.sell_ex}</span></div>
        <div class="up">${fmt(a.edge_bps, 1)} bps</div>
      </div>`
    )
    .join("");
}

function renderTape(tape) {
  $("tape").innerHTML = (tape || [])
    .slice(0, 40)
    .map(
      (t) =>
        `<div class="row"><span class="${t.side}">${t.side}</span><span>${t.symbol}</span><span>${fmt(
          t.price
        )}</span><span class="muted">${t.exchange}</span></div>`
    )
    .join("");
}

function renderJournal(events) {
  $("journal").innerHTML = (events || [])
    .slice(0, 40)
    .map((e) => {
      const p = e.payload || {};
      const extra = p.symbol
        ? `${p.side || ""} ${p.symbol} @ ${fmt(p.price)} ${p.reason || ""}`
        : JSON.stringify(p).slice(0, 140);
      return `<div class="row"><span class="muted">${ago(e.ts)}</span><span>${e.kind}</span><span class="muted">${extra}</span></div>`;
    })
    .join("");
}

async function loadFills() {
  try {
    const rows = await (await fetch("/api/fills")).json();
    $("fills").innerHTML = (rows || [])
      .slice(0, 30)
      .map(
        (f) =>
          `<div class="row"><span class="${f.side}">${f.side}</span><span>${f.symbol}</span><span>${fmt(
            f.price
          )}</span><span class="${Number(f.pnl) >= 0 ? "up" : "down"}">${fmt(f.pnl, 2)}</span></div>`
      )
      .join("");
  } catch {}
}

async function select(sym) {
  selected = sym;
  $("chartTitle").textContent = sym;
  try {
    const rows = await (await fetch("/api/candles/" + encodeURIComponent(sym))).json();
    if (Array.isArray(rows)) candles = rows;
    drawChart();
    const book = await (await fetch("/api/book/" + encodeURIComponent(sym))).json();
    renderBook(book);
  } catch {}
}

function renderBook(book) {
  $("bookEx").textContent = book.exchange || "";
  const asks = (book.asks || []).slice(0, 8).reverse();
  const bids = (book.bids || []).slice(0, 8);
  $("book").innerHTML =
    asks
      .map(
        (l) =>
          `<div class="book-row ask"><span>${fmt(l.price)}</span><span>${fmt(l.qty, 4)}</span><span></span></div>`
      )
      .join("") +
    `<div class="row"><b>${selected}</b></div>` +
    bids
      .map(
        (l) =>
          `<div class="book-row bid"><span>${fmt(l.price)}</span><span>${fmt(l.qty, 4)}</span><span></span></div>`
      )
      .join("");
}

function drawChart() {
  const c = $("chart");
  const ctx = c.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || 640;
  const h = 280;
  c.width = w * dpr;
  c.height = h * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0a0e14";
  ctx.fillRect(0, 0, w, h);
  const data = candles.slice(-120);
  if (data.length < 2) {
    ctx.fillStyle = "#8d97a8";
    ctx.fillText("Waiting for websocket candles…", 16, 24);
    return;
  }
  const highs = data.map((d) => d.high);
  const lows = data.map((d) => d.low);
  const max = Math.max(...highs);
  const min = Math.min(...lows);
  const pad = (max - min) * 0.08 || max * 0.01;
  const top = max + pad;
  const bot = min - pad;
  const y = (px) => ((top - px) / (top - bot)) * (h - 24) + 8;
  const cw = (w - 10) / data.length;
  data.forEach((d, i) => {
    const x = i * cw + 4;
    const up = d.close >= d.open;
    ctx.strokeStyle = up ? "#4cff91" : "#ff5d73";
    ctx.fillStyle = up ? "#4cff91" : "#ff5d73";
    ctx.beginPath();
    ctx.moveTo(x + cw * 0.45, y(d.high));
    ctx.lineTo(x + cw * 0.45, y(d.low));
    ctx.stroke();
    const y1 = y(d.open);
    const y2 = y(d.close);
    ctx.fillRect(x + cw * 0.15, Math.min(y1, y2), cw * 0.6, Math.max(1, Math.abs(y2 - y1)));
  });
  ctx.fillStyle = "#8d97a8";
  ctx.font = "11px monospace";
  ctx.fillText(fmt(max), 8, 14);
  ctx.fillText(fmt(min), 8, h - 8);
}

async function loadUniverse() {
  try {
    universe = await (await fetch("/api/universe")).json();
    if (Array.isArray(universe)) {
      $("uniHint").textContent = `${universe.length} tradable USDT pairs loaded from Binance. Search to add any coin to the 24/7 sockets.`;
    }
  } catch {
    $("uniHint").textContent = "Universe feed unavailable — watchlist still live.";
  }
}

$("search").addEventListener("keydown", async (e) => {
  if (e.key !== "Enter") return;
  const q = e.target.value.trim().toUpperCase();
  if (!q) return;
  let sym = q.includes("/") ? q : q + "/USDT";
  const hit = (universe || []).find((u) => u.symbol === sym || u.symbol.startsWith(q + "/"));
  if (hit) sym = hit.symbol;
  const list = Array.from(new Set([...(state.watchlist || []), sym]));
  await fetch("/api/watchlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbols: list }),
  });
  toast("Watching " + sym);
  select(sym);
  e.target.value = "";
});

$("btnStart").onclick = () => fetch("/api/start", { method: "POST" }).then(() => toast("Robot resumed"));
$("btnPause").onclick = () => fetch("/api/pause", { method: "POST" }).then(() => toast("Entries paused — stops still live"));
$("btnFlat").onclick = () => fetch("/api/flatten", { method: "POST" }).then(() => toast("Flattened all paper inventory"));

$("ticket").onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = { symbol: fd.get("symbol"), side: fd.get("side") };
  const r = await fetch("/api/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json();
  toast(j.ok ? "Ticket sent" : j.error || "Rejected");
  loadFills();
};

function renderAlerts(rows) {
  const el = $("alerts");
  if (!el) return;
  if (!rows || !rows.length) {
    el.innerHTML = `<div class="chip muted">Screener quiet — waiting for alpha / flow / bounce alerts</div>`;
    return;
  }
  el.innerHTML = rows
    .slice(0, 8)
    .map((a) => `<div class="chip">${a.text || a.kind + " " + a.symbol}</div>`)
    .join("");
}

function renderScreener() {
  const boards = screener.boards || {};
  const meta = screener.meta || {};
  const keys = Object.keys(meta).length ? Object.keys(meta) : Object.keys(boards);
  $("screenerTabs").innerHTML = keys
    .map((k) => {
      const title = (meta[k] && meta[k].title) || k;
      return `<button class="tab ${k === boardKey ? "on" : ""}" data-board="${k}">${title}</button>`;
    })
    .join("");
  $("screenerTabs").querySelectorAll(".tab").forEach((btn) => {
    btn.onclick = () => {
      boardKey = btn.dataset.board;
      const m = meta[boardKey] || {};
      $("screenerMeta").textContent = m.blurb || boardKey;
      renderScreener();
    };
  });
  const rows = boards[boardKey] || [];
  $("screenerBody").innerHTML = rows
    .map((r) => {
      const chg = Number(r.change_pct || 0);
      return `<tr data-sym="${r.symbol}">
        <td>${r.symbol}</td>
        <td><b>${fmt(r.alpha, 1)}</b></td>
        <td>${fmt(r.last)}</td>
        <td class="${chg >= 0 ? "up" : "down"}">${pct(chg)}</td>
        <td>${fmt(r.rsi, 1)}</td>
        <td>${fmt(r.adx, 1)}</td>
        <td>${fmt(r.vol_ratio, 2)}</td>
        <td class="${r.rs_btc >= 0 ? "up" : "down"}">${fmt(r.rs_btc, 2)}</td>
        <td class="bias-${r.bias}">${r.bias}</td>
        <td><button class="btn tiny" data-watch="${r.symbol}">watch</button></td>
      </tr>`;
    })
    .join("");
  $("screenerBody").querySelectorAll("tr").forEach((tr) => {
    tr.onclick = (e) => {
      if (e.target.dataset.watch) return;
      select(tr.dataset.sym);
    };
  });
  $("screenerBody").querySelectorAll("[data-watch]").forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      fetch("/api/screener/watch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: btn.dataset.watch }),
      }).then(() => toast("Watching " + btn.dataset.watch));
    };
  });
}

function renderHeat() {
  const rows = screener.heatmap || [];
  $("heat").innerHTML = rows
    .map((h) => {
      const chg = Number(h.change_pct || 0);
      const t = Math.max(-8, Math.min(8, chg));
      const bg =
        t >= 0
          ? `rgba(76, 255, 145, ${0.12 + t / 20})`
          : `rgba(255, 93, 115, ${0.12 + Math.abs(t) / 20})`;
      return `<div class="cell" data-sym="${h.symbol}" style="background:${bg}">
        <div class="sym">${h.symbol.replace("/USDT", "")}</div>
        <div class="${chg >= 0 ? "up" : "down"}">${pct(chg)}</div>
      </div>`;
    })
    .join("");
  $("heat").querySelectorAll(".cell").forEach((c) => {
    c.onclick = () => select(c.dataset.sym);
  });
}

function renderStrats(rows) {
  $("stratGrid").innerHTML = (rows || [])
    .map((s) => {
      const pnl = Number(s.pnl || 0);
      return `<div class="strat ${s.enabled ? "on" : "off"}" data-name="${s.name}">
        <div class="fam">${s.family || "core"}</div>
        <div class="nm">${s.title || s.name}</div>
        <div class="muted">w ${fmt(s.weight, 2)} · <span class="${pnl >= 0 ? "up" : "down"}">${fmt(pnl, 2)}</span></div>
      </div>`;
    })
    .join("");
  $("stratGrid").querySelectorAll(".strat").forEach((el) => {
    el.onclick = () => {
      fetch("/api/strategies/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: el.dataset.name }),
      }).then(async (r) => {
        const j = await r.json();
        toast((j.enabled ? "Armed " : "Disarmed ") + el.dataset.name);
      });
    };
  });
}

window.addEventListener("resize", drawChart);
connect();
select(selected);
loadUniverse();
loadFills();
setInterval(loadFills, 5000);
setInterval(() => {
  if (selected) fetch("/api/candles/" + encodeURIComponent(selected)).then(async (r) => {
    const rows = await r.json();
    if (Array.isArray(rows)) {
      candles = rows;
      drawChart();
    }
  }).catch(() => {});
}, 4000);
