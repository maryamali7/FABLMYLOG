const $ = (id) => document.getElementById(id);

let state = {};
let tickers = [];
let selected = "BTC/USDT";
let candles = [];
let universe = [];
let screener = { boards: {}, meta: {}, heatmap: [], alerts: [], summary: {} };

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
  return (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
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

function showView(name) {
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("on", v.dataset.view === name));
  document.querySelectorAll(".nav").forEach((b) => b.classList.toggle("on", b.dataset.view === name));
  if (name === "overview") drawChart();
}

document.querySelectorAll(".nav").forEach((btn) => {
  btn.onclick = () => showView(btn.dataset.view);
});

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    $("connDot").classList.add("on");
    $("connLabel").textContent = "Live dashboard";
  };
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
        if (typeof renderScreenerPro === "function") renderScreenerPro();
      }
      if (msg.alerts) renderAlerts(msg.alerts);
      if (msg.rule_alerts && typeof renderRuleFeed === "function") renderRuleFeed(msg.rule_alerts);
    }
  };
  ws.onclose = () => {
    $("connDot").classList.remove("on");
    $("connLabel").textContent = "Reconnecting…";
    setTimeout(connect, 1500);
  };
}

function render() {
  const s = state;
  const mode = (s.mode || "paper").toUpperCase();
  $("modePill").textContent = mode;
  $("modePill").className = "badge " + (mode === "LIVE" ? "warn" : "teal");
  const run = s.paused ? "PAUSED" : s.running ? "RUNNING 24/7" : "STOPPED";
  $("runPill").textContent = s.halted ? "HALTED" : run;
  $("runPill").className = "badge " + (s.halted || s.paused ? "warn" : "");
  const up = s.uptime || 0;
  const hh = String(Math.floor(up / 3600)).padStart(2, "0");
  const mm = String(Math.floor((up % 3600) / 60)).padStart(2, "0");
  const ss = String(Math.floor(up % 60)).padStart(2, "0");
  $("upPill").textContent = `${hh}:${mm}:${ss}`;
  const rg = s.regime || {};
  $("regimePill").textContent = (rg.name || "regime").replace("_", " ").toUpperCase();
  $("regimePill").className = "badge " + (rg.risk_on === false ? "warn" : "gold");

  const pnl = (s.equity || 0) - (s.starting_equity || 0);
  const pnlCls = pnl >= 0 ? "up" : "down";
  $("kpis").innerHTML = [
    kpi("Equity", "$" + fmt(s.equity, 2), `${(s.positions || []).length} open`),
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
      "Exchange TLS is blocked here, so paper mode is using a simulated tape. Real venue sockets take over when the network allows.";
  }
  renderTickers();
  renderPositions(s.positions || []);
  renderStrats(s.strategies || []);
  if (typeof onProState === "function") onProState(s);
  if (s.alerts) renderAlerts(s.alerts);
  const t = tickers.find((x) => x.symbol === selected);
  if (t) {
    $("lastPrice").textContent = fmt(t.last);
    $("lastPrice").className = "price-xl " + (Number(t.change_pct) >= 0 ? "up" : "down");
    $("chartSub").textContent = `${pct(t.change_pct)} · spread ${fmt(t.spread_bps, 1)} bps · ${Object.keys(t.venues || {}).join(" · ") || t.exchange}`;
  }
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
        }</span>`
    )
    .join("");
}

function renderTickers() {
  $("tickerBody").innerHTML = (tickers || [])
    .map((t) => {
      const chg = Number(t.change_pct || 0);
      const venues = Object.keys(t.venues || {}).join(" · ") || t.exchange;
      return `<tr data-sym="${t.symbol}">
        <td>${t.symbol}</td>
        <td>${fmt(t.last)}</td>
        <td class="${chg >= 0 ? "up" : "down"}">${pct(chg)}</td>
        <td>${fmt(t.spread_bps, 1)}</td>
        <td class="muted">${venues}</td>
      </tr>`;
    })
    .join("");
  $("tickerBody").querySelectorAll("tr").forEach((tr) => {
    tr.onclick = () => {
      select(tr.dataset.sym);
      showView("overview");
    };
  });
}

function renderPositions(pos) {
  if (!pos.length) {
    $("positions").innerHTML = `<div class="muted">No open inventory. The ensemble is waiting for confluence.</div>`;
    return;
  }
  $("positions").innerHTML = pos
    .map((p) => {
      const u = Number(p.unrealized || 0);
      return `<div class="row">
        <div><b>${p.symbol}</b><div class="muted">${p.strategy}${p.scaled ? " · scaled" : ""}</div></div>
        <div class="${u >= 0 ? "up" : "down"}">${u >= 0 ? "+" : ""}${fmt(u, 2)}
          <button class="xbtn" data-close="${p.symbol}">close</button>
          <div class="muted">mark ${fmt(p.mark)}</div>
        </div>
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
  const action = (rows || []).filter((s) => s.kind !== "hold").slice(0, 12);
  if (!action.length) {
    $("signals").innerHTML = `<div class="muted">Listening for confluence…</div>`;
    return;
  }
  $("signals").innerHTML = action
    .map(
      (s) => `<div class="row">
        <div><b class="${s.kind}">${s.kind.toUpperCase()}</b> ${s.symbol}<div class="muted">${s.strategy}</div></div>
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
        <div><b>${a.symbol}</b><div class="muted">buy ${a.buy_ex} → sell ${a.sell_ex}</div></div>
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
    .slice(0, 50)
    .map((e) => {
      const p = e.payload || {};
      const extra = p.symbol
        ? `${p.side || ""} ${p.symbol} @ ${fmt(p.price)} ${p.reason || ""}`
        : JSON.stringify(p).slice(0, 140);
      return `<div class="row"><span class="muted">${ago(e.ts)}</span><span>${e.kind}</span><span class="muted">${extra}</span></div>`;
    })
    .join("");
}

function renderAlerts(rows) {
  const el = $("alerts");
  if (!rows || !rows.length) {
    el.innerHTML = `<div class="chip">Screener quiet — waiting for alpha, flow, and bounce alerts</div>`;
    return;
  }
  el.innerHTML = rows
    .slice(0, 8)
    .map((a) => `<div class="chip">${a.text || a.kind + " " + a.symbol}</div>`)
    .join("");
}

function renderStrats(rows) {
  $("stratGrid").innerHTML = (rows || [])
    .map((s) => {
      const pnl = Number(s.pnl || 0);
      return `<div class="strat ${s.enabled ? "on" : "off"} ${s.custom ? "mine" : ""}" data-name="${s.name}">
        <div class="fam">${s.custom ? "custom" : s.family || "core"}</div>
        <div class="nm">${s.title || s.name}</div>
        <div class="muted">weight ${fmt(s.weight, 2)} · <span class="${pnl >= 0 ? "up" : "down"}">${fmt(pnl, 2)}</span></div>
        <div class="muted">${s.fires ? s.fires + " signals" : "idle"}</div>
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
  const ticketSym = document.querySelector('#ticket input[name="symbol"]');
  if (ticketSym) ticketSym.value = sym;
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
  const maxQ = Math.max(0.0001, ...asks.map((l) => l.qty), ...bids.map((l) => l.qty));
  $("book").innerHTML =
    asks
      .map(
        (l) =>
          `<div class="book-row ask"><span>${fmt(l.price)}</span><span>${fmt(l.qty, 4)}</span><span class="bar" style="width:${(l.qty / maxQ) * 100}%;background:#ff5d73"></span></div>`
      )
      .join("") +
    `<div class="row"><b>${selected}</b></div>` +
    bids
      .map(
        (l) =>
          `<div class="book-row bid"><span>${fmt(l.price)}</span><span>${fmt(l.qty, 4)}</span><span class="bar" style="width:${(l.qty / maxQ) * 100}%;background:#3dff9a"></span></div>`
      )
      .join("");
}

function drawChart() {
  const c = $("chart");
  if (!c) return;
  const ctx = c.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || 640;
  const h = 340;
  c.width = w * dpr;
  c.height = h * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0a131c";
  ctx.fillRect(0, 0, w, h);
  const data = candles.slice(-140);
  if (data.length < 2) {
    ctx.fillStyle = "#8fa0b3";
    ctx.font = "13px DM Sans, sans-serif";
    ctx.fillText("Waiting for candles…", 18, 28);
    return;
  }
  const highs = data.map((d) => d.high);
  const lows = data.map((d) => d.low);
  const max = Math.max(...highs);
  const min = Math.min(...lows);
  const pad = (max - min) * 0.08 || max * 0.01;
  const top = max + pad;
  const bot = min - pad;
  const y = (px) => ((top - px) / (top - bot)) * (h - 28) + 10;
  const cw = (w - 12) / data.length;
  data.forEach((d, i) => {
    const x = i * cw + 6;
    const up = d.close >= d.open;
    ctx.strokeStyle = up ? "#3dff9a" : "#ff5d73";
    ctx.fillStyle = up ? "#3dff9a" : "#ff5d73";
    ctx.beginPath();
    ctx.moveTo(x + cw * 0.45, y(d.high));
    ctx.lineTo(x + cw * 0.45, y(d.low));
    ctx.stroke();
    const y1 = y(d.open);
    const y2 = y(d.close);
    ctx.fillRect(x + cw * 0.18, Math.min(y1, y2), Math.max(1.2, cw * 0.55), Math.max(1, Math.abs(y2 - y1)));
  });
  ctx.fillStyle = "#8fa0b3";
  ctx.font = "12px JetBrains Mono, monospace";
  ctx.fillText(fmt(max), 10, 16);
  ctx.fillText(fmt(min), 10, h - 10);
}

async function loadUniverse() {
  try {
    universe = await (await fetch("/api/universe")).json();
    if (Array.isArray(universe) && universe.length) {
      $("uniHint").textContent = `${universe.length} tradable USDT pairs available. Search to add any coin.`;
    }
  } catch {}
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
  showView("overview");
  e.target.value = "";
});

$("btnStart").onclick = () => fetch("/api/start", { method: "POST" }).then(() => toast("Robot resumed"));
$("btnPause").onclick = () => fetch("/api/pause", { method: "POST" }).then(() => toast("Entries paused"));
$("btnFlat").onclick = () => fetch("/api/flatten", { method: "POST" }).then(() => toast("Flattened inventory"));

$("ticket").onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = { symbol: fd.get("symbol"), side: "buy" };
  const r = await fetch("/api/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json();
  toast(j.ok ? "Buy sent" : j.error || "Rejected");
  loadFills();
};

$("btnSell").onclick = async () => {
  const fd = new FormData($("ticket"));
  const r = await fetch("/api/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol: fd.get("symbol"), side: "sell" }),
  });
  const j = await r.json();
  toast(j.ok ? "Sell sent" : j.error || "Rejected");
  loadFills();
};

window.addEventListener("resize", drawChart);
connect();
select(selected);
loadUniverse();
loadFills();
setInterval(loadFills, 5000);
setInterval(() => {
  if (!selected) return;
  fetch("/api/candles/" + encodeURIComponent(selected))
    .then(async (r) => {
      const rows = await r.json();
      if (Array.isArray(rows)) {
        candles = rows;
        drawChart();
      }
    })
    .catch(() => {});
}, 4000);


// bridge used by pro.js
window.FML = {
  get state() { return state; },
  get screener() { return screener; },
  get tickers() { return tickers; },
  select,
  showView,
  toast,
  fmt,
  pct,
  ago,
};
