/* FablMyLog — multi-timeframe analysis + next-move forecasting view.
   Loaded after app.js / pro.js. Everything lives inside one IIFE so no
   top-level identifier can clash with the other classic scripts. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const get = async (u) => (await fetch(u)).json();
  const post = async (u) => (await fetch(u, { method: "POST" })).json();
  const say = (m) => (window.FML ? window.FML.toast(m) : null);
  const enc = (s) => encodeURIComponent(s);

  let TFS = [];
  let symbol = "BTC/USDT";
  let snap = null;
  let forecast = null;
  let matrix = [];
  let busy = false;
  let timer = null;

  const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(d));
  const signed = (v, d = 2) => (v >= 0 ? "+" : "") + num(v, d);

  function stateClass(st) {
    if (st === "overbought" || st === "bearish") return "down";
    if (st === "oversold" || st === "bullish") return "up";
    return "flat";
  }
  function ratingClass(r) {
    if (!r) return "flat";
    if (r.indexOf("buy") >= 0) return "up";
    if (r.indexOf("sell") >= 0) return "down";
    return "flat";
  }
  function rsiClass(v) {
    if (v >= 70) return "hot";
    if (v >= 60) return "warm";
    if (v <= 30) return "cold";
    if (v <= 40) return "cool";
    return "mid";
  }

  // ---------------------------------------------------------------- symbols
  function symbols() {
    const st = window.FML && window.FML.state ? window.FML.state : {};
    const list = (st.watchlist || []).slice();
    if (!list.length && window.FML && window.FML.tickers) {
      (window.FML.tickers || []).forEach((t) => list.push(t.symbol));
    }
    if (!list.length) list.push("BTC/USDT");
    return Array.from(new Set(list));
  }

  function fillSymbols() {
    const sel = $("mtfSymbol");
    if (!sel) return;
    const list = symbols();
    if (list.indexOf(symbol) < 0) symbol = list[0];
    const sig = list.join(",");
    if (sel.dataset.sig !== sig) {
      sel.dataset.sig = sig;
      sel.innerHTML = list.map((s) => `<option value="${s}">${s}</option>`).join("");
    }
    sel.value = symbol;
  }

  // ------------------------------------------------------------ timeframes
  function renderStrip() {
    const wrap = $("mtfStrip");
    if (!wrap) return;
    const rows = (snap && snap.timeframes) || [];
    if (!rows.length) {
      wrap.innerHTML = `<p class="hint">Loading timeframes…</p>`;
      return;
    }
    wrap.innerHTML = rows
      .map((f) => {
        if (!f.available) {
          return `<div class="tf-card off">
            <div class="tf-top"><b>${f.label}</b><span class="pill dim">n/a</span></div>
            <p class="hint">${f.reason || "not enough history yet"}</p>
          </div>`;
        }
        const w = Math.max(2, Math.min(100, f.rsi));
        const scoreW = Math.min(50, Math.abs(f.score) / 2);
        return `<div class="tf-card ${ratingClass(f.rating)}">
          <div class="tf-top">
            <b>${f.label}</b>
            <span class="pill ${ratingClass(f.rating)}">${f.rating}</span>
          </div>
          <div class="tf-rsi">
            <div class="tf-rsi-bar"><span class="${rsiClass(f.rsi)}" style="width:${w}%"></span><i class="m30"></i><i class="m70"></i></div>
            <div class="tf-rsi-lbl">RSI <b class="${stateClass(f.rsi_state)}">${num(f.rsi, 1)}</b> <span class="${stateClass(f.rsi_state)}">${f.rsi_state}</span></div>
          </div>
          <div class="tf-score"><span class="${f.score >= 0 ? "up" : "down"}" style="width:${scoreW}%;margin-left:${f.score >= 0 ? 50 : 50 - scoreW}%"></span></div>
          <div class="tf-grid">
            <div><span>Trend</span><b class="${f.trend === "up" ? "up" : f.trend === "down" ? "down" : "flat"}">${f.trend}</b></div>
            <div><span>ADX</span><b>${num(f.adx, 1)}</b></div>
            <div><span>MACD</span><b class="${f.macd_state === "bullish" ? "up" : "down"}">${f.macd_state}</b></div>
            <div><span>Chg</span><b class="${f.change_pct >= 0 ? "up" : "down"}">${signed(f.change_pct)}%</b></div>
            <div><span>ATR%</span><b>${num(f.atr_pct)}</b></div>
            <div><span>Bars</span><b>${f.bars}</b></div>
          </div>
        </div>`;
      })
      .join("");
  }

  function renderAlign() {
    const a = (snap && snap.alignment) || null;
    const fill = $("mtfAlignFill");
    const facts = $("mtfAlignFacts");
    if (!fill || !facts) return;
    const score = a ? a.score : 0;
    const w = Math.min(50, Math.abs(score) / 2);
    fill.className = score >= 0 ? "up" : "down";
    fill.style.width = w + "%";
    fill.style.marginLeft = (score >= 0 ? 50 : 50 - w) + "%";
    if (!a) {
      facts.innerHTML = "";
      return;
    }
    const ob = (snap.overbought || []).join(", ");
    const os = (snap.oversold || []).join(", ");
    facts.innerHTML = `
      <div class="fact"><span>Verdict</span><b class="${a.bias === "long" ? "up" : a.bias === "short" ? "down" : "flat"}">${a.verdict}</b></div>
      <div class="fact"><span>Score</span><b class="${score >= 0 ? "up" : "down"}">${signed(score, 1)}</b></div>
      <div class="fact"><span>Agreement</span><b>${num(a.agreement, 0)}%</b></div>
      <div class="fact"><span>Bull / bear</span><b>${a.bulls} / ${a.bears}</b></div>
      <div class="fact"><span>Frames</span><b>${a.timeframes}</b></div>
      <div class="fact"><span>Overbought</span><b class="${ob ? "down" : "flat"}">${ob || "none"}</b></div>
      <div class="fact"><span>Oversold</span><b class="${os ? "up" : "flat"}">${os || "none"}</b></div>
      <div class="fact wide"><span>Conflicts</span><b>${(a.conflicts || []).join(" · ") || "none — frames agree"}</b></div>`;
  }

  // -------------------------------------------------------------- forecast
  function renderForecast() {
    const head = $("fcHead");
    const models = $("fcModels");
    const why = $("fcWhy");
    if (!head) return;
    if (!forecast || !forecast.ok) {
      head.innerHTML = `<p class="hint">${(forecast && forecast.error) || "No forecast yet — hit Predict."}</p>`;
      models.innerHTML = "";
      why.innerHTML = "";
      drawCone();
      return;
    }
    const f = forecast;
    const cls = f.direction === "up" ? "up" : f.direction === "down" ? "down" : "flat";
    head.innerHTML = `
      <div class="fc-dir ${cls}">
        <span class="arrow">${f.direction === "up" ? "▲" : f.direction === "down" ? "▼" : "▬"}</span>
        <div>
          <b>${f.direction.toUpperCase()}</b>
          <small>${f.horizon_label} · ${f.timeframe}</small>
        </div>
      </div>
      <div class="fc-stats">
        <div><span>Probability up</span><b class="${f.probability_up >= 50 ? "up" : "down"}">${num(f.probability_up, 1)}%</b></div>
        <div><span>Expected move</span><b class="${f.expected_move_pct >= 0 ? "up" : "down"}">${signed(f.expected_move_pct)}%</b></div>
        <div><span>Target</span><b>${num(f.target, 6)}</b></div>
        <div><span>Range</span><b>${num(f.lower, 6)} – ${num(f.upper, 6)}</b></div>
        <div><span>Confidence</span><b>${num(f.confidence, 0)}%</b></div>
        <div><span>Risk / reward</span><b>${f.risk_reward ? num(f.risk_reward) : "—"}</b></div>
      </div>`;
    models.innerHTML = (f.models || [])
      .map((m) => {
        const pctW = Math.min(50, Math.abs(m.score) * 50);
        const c = m.score >= 0 ? "up" : "down";
        return `<div class="model-row">
          <span class="mname">${m.name}</span>
          <span class="mbar"><i class="${c}" style="width:${pctW}%;margin-left:${m.score >= 0 ? 50 : 50 - pctW}%"></i></span>
          <span class="mval ${c}">${signed(m.score * 100, 0)}</span>
          <span class="mdetail">${m.detail}</span>
        </div>`;
      })
      .join("");
    why.innerHTML = `<h3>Why</h3><ul>${(f.rationale || []).map((r) => `<li>${r}</li>`).join("")}</ul>`;
    drawCone();
  }

  function renderLevels() {
    const box = $("fcLevels");
    const reg = $("fcRegime");
    if (!box) return;
    const lv = forecast && forecast.ok ? forecast.levels : null;
    if (!lv) {
      box.innerHTML = `<p class="hint">Run a prediction to map support and resistance.</p>`;
      reg.innerHTML = "";
      return;
    }
    const price = lv.price;
    const rows = []
      .concat((lv.resistance || []).slice().reverse().map((l) => ({ ...l, kind: "R" })))
      .concat([{ price, kind: "px" }])
      .concat((lv.support || []).map((l) => ({ ...l, kind: "S" })));
    box.innerHTML = rows
      .map((l) => {
        if (l.kind === "px") return `<div class="lvl px"><b>price</b><span>${num(price, 6)}</span><i></i></div>`;
        const cls = l.kind === "R" ? "res" : "sup";
        return `<div class="lvl ${cls}">
          <b>${l.kind === "R" ? "Resistance" : "Support"}</b>
          <span>${num(l.price, 6)}</span>
          <i class="${l.distance_pct >= 0 ? "up" : "down"}">${signed(l.distance_pct)}% · ${l.touches}×</i>
        </div>`;
      })
      .join("");
    const r = forecast.regime || {};
    reg.innerHTML = `<b class="pill ${r.name === "trending" ? "up" : r.name === "volatile" ? "down" : "flat"}">${r.name || "—"}</b>
      <p>${r.detail || ""}</p>
      <div class="fact"><span>ADX</span><b>${num(r.adx, 1)}</b></div>
      <div class="fact"><span>ATR%</span><b>${num(r.atr_pct, 2)}</b></div>`;
  }

  function drawCone() {
    const cv = $("fcChart");
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || cv.parentElement.clientWidth || 600;
    const h = 200;
    cv.width = w * dpr;
    cv.height = h * dpr;
    cv.style.width = "100%";
    cv.style.height = h + "px";
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    if (!forecast || !forecast.ok) return;
    const hist = (forecast.history || []).slice(-60);
    const path = forecast.path || [];
    if (!hist.length || !path.length) return;

    const vals = hist.concat(path.map((p) => p.upper), path.map((p) => p.lower));
    let lo = Math.min.apply(null, vals);
    let hi = Math.max.apply(null, vals);
    const pad = (hi - lo) * 0.08 || hi * 0.001;
    lo -= pad;
    hi += pad;
    const n = hist.length + path.length;
    const x = (i) => (i / (n - 1)) * (w - 8) + 4;
    const y = (v) => h - 12 - ((v - lo) / (hi - lo)) * (h - 24);

    // history line
    g.strokeStyle = "#9db4d0";
    g.lineWidth = 1.4;
    g.beginPath();
    hist.forEach((v, i) => (i ? g.lineTo(x(i), y(v)) : g.moveTo(x(i), y(v))));
    g.stroke();

    const up = forecast.direction === "up";
    const base = hist.length - 1;
    const col = up ? "88, 214, 141" : forecast.direction === "down" ? "231, 106, 106" : "150, 160, 175";

    // cone
    g.fillStyle = `rgba(${col},0.16)`;
    g.beginPath();
    g.moveTo(x(base), y(hist[hist.length - 1]));
    path.forEach((p, i) => g.lineTo(x(base + 1 + i), y(p.upper)));
    for (let i = path.length - 1; i >= 0; i--) g.lineTo(x(base + 1 + i), y(path[i].lower));
    g.closePath();
    g.fill();

    // mid projection
    g.strokeStyle = `rgba(${col},0.95)`;
    g.setLineDash([4, 3]);
    g.lineWidth = 1.6;
    g.beginPath();
    g.moveTo(x(base), y(hist[hist.length - 1]));
    path.forEach((p, i) => g.lineTo(x(base + 1 + i), y(p.mid)));
    g.stroke();
    g.setLineDash([]);

    // target marker
    const last = path[path.length - 1];
    g.fillStyle = `rgba(${col},1)`;
    g.beginPath();
    g.arc(x(n - 1), y(last.mid), 3.2, 0, Math.PI * 2);
    g.fill();
    g.font = "11px ui-monospace, monospace";
    g.fillText(num(last.mid, 6), Math.max(4, x(n - 1) - 66), y(last.mid) - 8);
  }

  // ----------------------------------------------------------- leaderboard
  function renderBoard(data) {
    const body = $("fcBoard");
    if (!body) return;
    const rows = (data.up || []).concat(data.down || []);
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="7" class="hint">Warming up the models…</td></tr>`;
      return;
    }
    rows.sort((a, b) => (b.edge || 0) - (a.edge || 0));
    body.innerHTML = rows
      .slice(0, 12)
      .map((r) => {
        const c = r.direction === "up" ? "up" : "down";
        return `<tr data-sym="${r.symbol}" class="clickable">
          <td><b>${r.symbol}</b></td>
          <td class="${c}">${r.direction === "up" ? "▲" : "▼"}</td>
          <td class="${r.probability_up >= 50 ? "up" : "down"}">${num(r.probability_up, 1)}%</td>
          <td class="${r.expected_move_pct >= 0 ? "up" : "down"}">${signed(r.expected_move_pct)}%</td>
          <td>${num(r.confidence, 0)}%</td>
          <td>${r.risk_reward ? num(r.risk_reward) : "—"}</td>
          <td class="muted small">${r.top_reason || ""}</td>
        </tr>`;
      })
      .join("");
    body.querySelectorAll("tr[data-sym]").forEach((tr) => {
      tr.onclick = () => load(tr.dataset.sym, true);
    });
  }

  function renderMatrix() {
    const head = $("mtfMatrixHead");
    const body = $("mtfMatrix");
    if (!head || !body) return;
    head.innerHTML =
      `<th>Symbol</th><th>Score</th><th>Agree</th>` + TFS.map((t) => `<th>${t.tf}</th>`).join("");
    if (!matrix.length) {
      body.innerHTML = `<tr><td colspan="${TFS.length + 3}" class="hint">Scanning timeframes…</td></tr>`;
      return;
    }
    body.innerHTML = matrix
      .slice(0, 14)
      .map((r) => {
        const cells = TFS.map((t) => {
          const v = r["rsi_" + t.tf];
          if (v === undefined || v === null) return `<td class="muted">—</td>`;
          const tr = r["trend_" + t.tf];
          return `<td class="rsi ${rsiClass(v)}" title="${t.tf} trend ${tr}">${num(v, 0)}<i class="${tr === "up" ? "up" : tr === "down" ? "down" : "flat"}">${tr === "up" ? "▲" : tr === "down" ? "▼" : "·"}</i></td>`;
        }).join("");
        return `<tr data-sym="${r.symbol}" class="clickable">
          <td><b>${r.symbol}</b></td>
          <td class="${r.mtf_score >= 0 ? "up" : "down"}">${signed(r.mtf_score, 0)}</td>
          <td>${num(r.agreement, 0)}%</td>${cells}</tr>`;
      })
      .join("");
    body.querySelectorAll("tr[data-sym]").forEach((tr) => {
      tr.onclick = () => load(tr.dataset.sym, true);
    });
  }

  // ------------------------------------------------------------ scoreboard
  function renderScore(st) {
    const kpis = $("scoreKpis");
    if (!kpis) return;
    const pct = (v) => (v === null || v === undefined ? "—" : num(v, 1) + "%");
    const cell = (label, value, cls) =>
      `<div class="m"><span>${label}</span><b class="${cls || ""}">${value}</b></div>`;
    const hr = st.hit_rate;
    kpis.innerHTML =
      cell("Hit rate", pct(hr), hr === null ? "" : hr >= 50 ? "up" : "down") +
      cell("Edge vs coin flip", st.edge === null || st.edge === undefined ? "—" : signed(st.edge, 1), st.edge > 0 ? "up" : st.edge < 0 ? "down" : "") +
      cell("Graded calls", st.graded) +
      cell("Awaiting", st.open) +
      cell("Brier score", st.brier === null ? "—" : num(st.brier, 3), st.brier !== null && st.brier < 0.25 ? "up" : "") +
      cell("Band coverage", pct(st.band_coverage)) +
      cell("Move error", st.mae_pct === null ? "—" : num(st.mae_pct, 2) + "%") +
      cell("Avg confidence", pct(st.avg_confidence));

    const meta = $("scoreMeta");
    if (meta) {
      meta.textContent = st.settled
        ? `${st.settled} forecasts settled · ${st.open} still running · lower Brier is better (0.25 = coin flip)`
        : "Every forecast is graded against the real price once its horizon elapses";
    }

    const models = $("scoreModels");
    models.innerHTML = (st.by_model || []).length
      ? st.by_model
          .map(
            (m) =>
              `<tr><td>${m.name}</td><td>${m.n}</td><td class="${m.hit_rate >= 50 ? "up" : "down"}">${num(m.hit_rate, 1)}%</td><td class="${m.edge >= 0 ? "up" : "down"}">${signed(m.edge, 1)}</td></tr>`
          )
          .join("")
      : `<tr><td colspan="4" class="hint">No settled calls yet — the first 1m forecasts mature after ~15 minutes.</td></tr>`;

    const cal = $("scoreCal");
    cal.innerHTML = (st.calibration || []).length
      ? st.calibration
          .map((c) => {
            const w = Math.max(2, Math.min(100, c.realized));
            const good = Math.abs(c.gap) <= 10;
            return `<div class="cal">
              <span class="cal-l">${c.bucket}<i>n=${c.n}</i></span>
              <span class="cal-bar"><i class="${good ? "up" : "down"}" style="width:${w}%"></i><em style="left:${Math.min(100, c.predicted)}%"></em></span>
              <span class="cal-v ${good ? "up" : "down"}">${num(c.realized, 0)}% vs ${num(c.predicted, 0)}%</span>
            </div>`;
          })
          .join("")
      : `<p class="hint">Calibration appears once calls settle in each probability bucket.</p>`;

    const pending = $("scorePending");
    pending.innerHTML = (st.pending || []).length
      ? st.pending
          .map(
            (p) =>
              `<div class="row"><span>${p.symbol}</span><span class="${p.direction === "up" ? "up" : p.direction === "down" ? "down" : "flat"}">${p.direction}</span><span>${num(p.probability_up, 0)}%</span><span class="muted">${p.due_in > 60 ? Math.round(p.due_in / 60) + "m" : Math.round(p.due_in) + "s"}</span></div>`
          )
          .join("")
      : `<p class="hint">No forecasts in flight.</p>`;

    const recent = $("scoreRecent");
    recent.innerHTML = (st.recent || []).length
      ? st.recent
          .map((r) => {
            const res =
              r.hit === null || r.hit === undefined
                ? `<span class="muted">no call</span>`
                : r.hit
                ? `<span class="up">hit</span>`
                : `<span class="down">miss</span>`;
            return `<tr><td><b>${r.symbol}</b></td><td>${r.timeframe}</td>
              <td class="${r.direction === "up" ? "up" : r.direction === "down" ? "down" : "flat"}">${r.direction}</td>
              <td>${num(r.probability_up, 0)}%</td>
              <td class="${r.expected_move_pct >= 0 ? "up" : "down"}">${signed(r.expected_move_pct)}%</td>
              <td class="${r.actual_move_pct >= 0 ? "up" : "down"}">${signed(r.actual_move_pct)}%</td>
              <td>${res}</td></tr>`;
          })
          .join("")
      : `<tr><td colspan="7" class="hint">Nothing graded yet.</td></tr>`;
  }

  async function loadScore() {
    try {
      renderScore(await get("/api/forecasts/accuracy"));
    } catch (err) {
      console.warn("score", err);
    }
  }

  // --------------------------------------------------------------- loading
  async function load(sym, predictToo) {
    if (busy) return;
    busy = true;
    symbol = sym || symbol;
    fillSymbols();
    try {
      snap = await get("/api/mtf/" + enc(symbol));
      renderStrip();
      renderAlign();
      const meta = $("mtfMeta");
      if (meta && snap.alignment) {
        meta.textContent = `${symbol} — ${snap.alignment.detail} · ${snap.alignment.verdict}`;
      }
      if (predictToo !== false) await runPredict();
    } catch (err) {
      console.warn("mtf load", err);
    } finally {
      busy = false;
    }
  }

  async function runPredict() {
    const tf = ($("fcTf") && $("fcTf").value) || "1m";
    const meta = $("fcMeta");
    if (meta) meta.textContent = `Running ${TFS.length ? tf : ""} ensemble for ${symbol}…`;
    try {
      forecast = await get(`/api/predict/${enc(symbol)}?tf=${tf}`);
    } catch (err) {
      forecast = { ok: false, error: String(err) };
    }
    if (meta) {
      meta.textContent = forecast.ok
        ? `${forecast.models.length} models · ${forecast.bars} bars of ${tf} · ${forecast.horizon_bars} bars ahead`
        : "Ensemble of trend, mean-reversion, analog, drift, flow and volatility models";
    }
    renderForecast();
    renderLevels();
  }

  async function loadBoards() {
    try {
      const [fc, mt] = await Promise.all([get("/api/forecasts?limit=8"), get("/api/mtf?limit=25")]);
      renderBoard(fc);
      matrix = mt.rows || [];
      if (mt.timeframes) TFS = mt.timeframes;
      renderMatrix();
    } catch (err) {
      console.warn("mtf boards", err);
    }
  }

  function active() {
    const v = document.querySelector('.view[data-view="mtf"]');
    return v && v.classList.contains("on");
  }

  function tick() {
    if (!active()) return;
    loadBoards();
    loadScore();
    load(symbol, false);
  }

  async function boot() {
    const sel = $("mtfSymbol");
    if (!sel) return;
    try {
      const cat = await get("/api/builder/catalog");
      TFS = cat.timeframes || [];
    } catch (err) {
      TFS = ["1m", "5m", "15m", "1h", "4h", "1d", "1w"].map((t) => ({ tf: t, label: t }));
    }
    const tfSel = $("fcTf");
    if (tfSel) {
      tfSel.innerHTML = TFS.map((t) => `<option value="${t.tf}">${t.label}</option>`).join("");
      tfSel.value = "15m";
      tfSel.onchange = runPredict;
    }
    fillSymbols();
    sel.onchange = () => load(sel.value, true);
    const rf = $("mtfRefresh");
    if (rf)
      rf.onclick = async () => {
        rf.disabled = true;
        await post(`/api/mtf/refresh?symbol=${enc(symbol)}`);
        await load(symbol, true);
        await loadBoards();
        rf.disabled = false;
        say(symbol + " timeframes refreshed");
      };
    const wt = $("mtfWatch");
    if (wt)
      wt.onclick = async () => {
        await fetch("/api/screener/watch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol }),
        });
        say("Watching " + symbol);
      };

    document.querySelectorAll(".nav").forEach((btn) => {
      const prev = btn.onclick;
      btn.onclick = (e) => {
        if (prev) prev(e);
        if (btn.dataset.view === "mtf") {
          const cur = window.FML && window.FML.selected;
          if (cur && symbols().indexOf(cur) >= 0) symbol = cur;
          load(symbol, true);
          loadBoards();
          loadScore();
        }
      };
    });
    window.addEventListener("resize", drawCone);
    if (timer) clearInterval(timer);
    timer = setInterval(tick, 15000);
  }

  window.MTFView = { load, boot, runPredict, loadScore };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
