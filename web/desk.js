/* FablMyLog — trade desk, portfolio risk, trade review and the command palette.
   Self-contained IIFE; shares nothing but window.FML with the other bundles. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const get = async (u) => (await fetch(u)).json();
  const send = async (u, b) =>
    (
      await fetch(u, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: b ? JSON.stringify(b) : undefined,
      })
    ).json();
  const say = (m) => (window.FML && window.FML.toast ? window.FML.toast(m) : null);
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));

  const num = (v, d) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : d === undefined ? 0 : d;
  };
  const px = (v) => {
    const n = Number(v || 0);
    if (!n) return "—";
    if (n >= 1000) return n.toFixed(2);
    if (n >= 1) return n.toFixed(4);
    return n.toPrecision(5);
  };
  const usd = (v) => {
    const n = Number(v || 0);
    const s = Math.abs(n) >= 1000 ? n.toFixed(0) : n.toFixed(2);
    return (n < 0 ? "-$" : "$") + s.replace("-", "");
  };
  const signed = (v, dp) => {
    const n = Number(v || 0);
    return `<span class="${n >= 0 ? "up" : "down"}">${n >= 0 ? "+" : ""}${n.toFixed(dp === undefined ? 2 : dp)}</span>`;
  };
  const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "—");

  const state = {
    symbol: "BTC/USDT",
    side: "buy",
    type: "market",
    qtyMode: "base",
    desk: null,
    portfolio: null,
    timer: null,
  };

  const viewOn = (name) => {
    const el = document.querySelector(`.view[data-view="${name}"]`);
    return el && el.classList.contains("on");
  };

  function goto(view) {
    document.querySelectorAll(".nav").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("on", v.dataset.view === view));
    if (view === "desk") loadDesk();
    if (view === "risk") loadPortfolio();
    if (view === "journal") loadReview();
  }

  // =========================================================== trade desk
  async function loadDesk() {
    const d = await get("/api/desk?symbol=" + encodeURIComponent(state.symbol));
    state.desk = d;
    state.symbol = d.symbol;
    if ($("dkSymbol") !== document.activeElement) $("dkSymbol").value = d.symbol;
    $("dkList").innerHTML = (d.watchlist || []).map((s) => `<option value="${esc(s)}">`).join("");

    const q = d.quote || {};
    $("dkLast").textContent = q.last ? px(q.last) : "—";
    const chg = $("dkChg");
    chg.textContent = q.change_pct == null ? "—" : (q.change_pct >= 0 ? "+" : "") + q.change_pct.toFixed(2) + "%";
    chg.className = "pill " + (q.change_pct == null ? "dim" : q.change_pct >= 0 ? "up" : "down");
    $("dkSpread").textContent = q.spread_bps == null ? "spread —" : `spread ${q.spread_bps.toFixed(1)} bps`;
    $("dkVenue").textContent = q.exchange ? "via " + q.exchange : "";

    $("dkAcct").innerHTML = `
      <div><span>Equity</span><b>${usd(d.equity)}</b></div>
      <div><span>Cash</span><b>${usd(d.cash)}</b></div>
      <div><span>Max position</span><b>${num(d.risk_cfg && d.risk_cfg.max_position_pct).toFixed(1)}%</b></div>
      <div><span>Fees</span><b>${num(d.risk_cfg && d.risk_cfg.fee_bps).toFixed(1)} bps</b></div>`;

    $("dkManage").checked = !!d.manage_manual;
    $("dkTicketSub").textContent = d.manage_manual
      ? "paper · robot manages desk positions"
      : "paper · desk positions are yours";

    renderLadder(d);
    renderPosition(d);
    renderWorking(d.working || []);
    $("dkTape").innerHTML = (d.trades || []).length
      ? d.trades
          .map(
            (t) => `<div class="row"><span>${clock(t.ts)} · ${esc(t.symbol)}</span>
              <b class="${t.pnl >= 0 ? "up" : "down"}">${usd(t.pnl)} · ${signed(t.r, 2)}R</b></div>
              <div class="muted small" style="padding:0 0 6px 2px">${esc(t.reason || "")}</div>`
          )
          .join("")
      : '<div class="muted">no closed trades yet</div>';
    preview();
  }

  function renderLadder(d) {
    const book = d.book || { bids: [], asks: [] };
    const asks = (book.asks || []).slice(0, 10).reverse();
    const bids = (book.bids || []).slice(0, 10);
    const max = Math.max(1e-9, ...asks.map((r) => r[1]), ...bids.map((r) => r[1]));
    const row = (r, cls) => {
      const w = Math.min(100, (r[1] / max) * 100);
      return `<div class="lad ${cls}" data-price="${r[0]}">
        <i style="width:${w}%"></i>
        <span class="lp">${px(r[0])}</span>
        <span class="lq">${Number(r[1]).toPrecision(4)}</span>
        <span class="ln">${usd(r[0] * r[1])}</span></div>`;
    };
    const mid = d.quote && d.quote.last ? px(d.quote.last) : "—";
    $("dkLadder").innerHTML = asks.length + bids.length
      ? asks.map((r) => row(r, "ask")).join("") +
        `<div class="lad mid"><span>${mid}</span><span class="muted small">last</span></div>` +
        bids.map((r) => row(r, "bid")).join("")
      : '<div class="muted">no depth for this symbol yet</div>';
    $("dkLadder")
      .querySelectorAll("[data-price]")
      .forEach((el) => {
        el.onclick = () => {
          const p = Number(el.dataset.price);
          $("dkPrice").value = p;
          if (state.type === "stop" || state.type === "stop_limit") $("dkStop").value = p;
          if (state.type === "market") setType("limit");
          preview();
        };
      });
  }

  function renderPosition(d) {
    const p = d.position;
    if (!p) {
      $("dkPosition").innerHTML = '<div class="muted">flat — no position in ' + esc(d.symbol) + "</div>";
      return;
    }
    const last = (d.quote && d.quote.last) || p.entry;
    const upl = num(p.unrealized);
    const uplPct = p.entry ? ((last - p.entry) / p.entry) * 100 : 0;
    $("dkPosition").innerHTML = `
      <div class="pos-grid">
        <div><span>Side</span><b>${esc(p.side || "buy")}</b></div>
        <div><span>Qty</span><b>${Number(p.qty).toPrecision(5)}</b></div>
        <div><span>Entry</span><b>${px(p.entry)}</b></div>
        <div><span>Mark</span><b>${px(last)}</b></div>
        <div><span>Unrealised</span><b class="${upl >= 0 ? "up" : "down"}">${usd(upl)}</b></div>
        <div><span>Move</span><b>${signed(uplPct)}%</b></div>
        <div><span>Stop</span><b>${p.stop ? px(p.stop) : '<span class="down">none</span>'}</b></div>
        <div><span>Target</span><b>${p.take ? px(p.take) : "—"}</b></div>
      </div>
      <div class="row-btns">
        <button class="mini" data-close="0.25">Close 25%</button>
        <button class="mini" data-close="0.5">Close 50%</button>
        <button class="mini" data-close="1">Close all</button>
        <button class="mini ghost" id="dkAttach">Attach bracket</button>
      </div>`;
    $("dkPosition")
      .querySelectorAll("[data-close]")
      .forEach((b) => {
        b.onclick = async () => {
          const frac = Number(b.dataset.close);
          const res = await send("/api/orders", {
            symbol: d.symbol,
            side: "sell",
            type: "market",
            qty: p.qty * frac,
            reduce_only: true,
            label: "desk close",
          });
          say(res.ok ? `Closing ${(frac * 100).toFixed(0)}% of ${d.symbol}` : res.error);
          loadDesk();
        };
      });
    $("dkAttach").onclick = async () => {
      const sl = num($("dkSl").value) || num(last * 0.97);
      const tp = num($("dkTp").value) || 0;
      const res = await send("/api/orders/bracket", {
        symbol: d.symbol,
        stop_loss: sl,
        take_profit: tp || null,
        trail_pct: num($("dkBt").value) / 100 || null,
      });
      say(res.ok ? `Bracket attached to ${d.symbol}` : res.error);
      loadDesk();
    };
  }

  function renderWorking(rows) {
    $("dkWorkSub").textContent = rows.length + " working";
    if (!rows.length) {
      $("dkWorking").innerHTML = '<tr><td colspan="9" class="muted">nothing resting</td></tr>';
      return;
    }
    $("dkWorking").innerHTML = rows
      .map(
        (o) => `<tr>
        <td>${esc(o.symbol)}</td>
        <td class="${o.side === "buy" ? "up" : "down"}">${esc(o.side)}</td>
        <td>${esc(String(o.type).replace("_", "-"))}${o.reduce_only ? ' <span class="badge dim">RO</span>' : ""}${
          o.post_only ? ' <span class="badge dim">PO</span>' : ""
        }</td>
        <td>${Number(o.remaining ?? o.qty).toPrecision(4)}</td>
        <td>${o.price ? px(o.price) : "mkt"}</td>
        <td>${o.stop_price ? px(o.stop_price) : o.trail_pct ? (o.trail_pct * 100).toFixed(1) + "%" : "—"}</td>
        <td>${esc(o.tif)}</td>
        <td>${esc(o.status)}${o.oco_group ? ' <span class="badge">OCO</span>' : ""}</td>
        <td class="row-btns">
          <button class="mini" data-mod="${o.id}">Edit</button>
          <button class="mini ghost" data-cxl="${o.id}">✕</button>
        </td></tr>`
      )
      .join("");
    $("dkWorking")
      .querySelectorAll("[data-cxl]")
      .forEach((b) => {
        b.onclick = async () => {
          await send(`/api/orders/${b.dataset.cxl}/cancel`);
          say("Order cancelled");
          loadDesk();
        };
      });
    $("dkWorking")
      .querySelectorAll("[data-mod]")
      .forEach((b) => {
        b.onclick = async () => {
          const o = rows.find((r) => r.id === b.dataset.mod);
          const price = window.prompt(`New price for ${o.symbol} (blank keeps ${o.price || "market"})`, o.price || "");
          const qty = window.prompt(`New quantity (blank keeps ${o.remaining ?? o.qty})`, o.remaining ?? o.qty);
          const body = {};
          if (price) body.price = Number(price);
          if (qty) body.qty = Number(qty);
          if (!Object.keys(body).length) return;
          const res = await send(`/api/orders/${o.id}/modify`, body);
          say(res.ok ? "Order amended" : res.error);
          loadDesk();
        };
      });
  }

  // ------------------------------------------------------------ the ticket
  function setSide(side) {
    state.side = side;
    $("dkSide")
      .querySelectorAll(".tab")
      .forEach((t) => t.classList.toggle("on", t.dataset.side === side));
    const btn = $("dkSubmit");
    btn.className = "btn " + side;
    btn.textContent = `Place ${side} order`;
    preview();
  }

  function setType(type) {
    state.type = type;
    $("dkType")
      .querySelectorAll(".tab")
      .forEach((t) => t.classList.toggle("on", t.dataset.type === type));
    const needsPrice = type === "limit" || type === "stop_limit";
    const needsStop = type === "stop" || type === "stop_limit";
    const needsTrail = type === "trailing_stop";
    $("dkPriceWrap").style.display = needsPrice ? "" : "none";
    $("dkStopWrap").style.display = needsStop ? "" : "none";
    $("dkTrailWrap").style.display = needsTrail ? "" : "none";
    if (needsTrail) setSide("sell");
    preview();
  }

  function setQtyMode(mode) {
    state.qtyMode = mode;
    $("dkQtyMode")
      .querySelectorAll(".tab")
      .forEach((t) => t.classList.toggle("on", t.dataset.mode === mode));
    $("dkQtyLabel").textContent = {
      base: "Quantity (base)",
      quote: "Order value ($)",
      equity_pct: "% of equity",
      risk_pct: "Risk per trade (% of equity)",
    }[mode];
    $("dkQtyQuick").style.display = mode === "base" ? "none" : "flex";
    preview();
  }

  function refPrice() {
    const d = state.desk || {};
    const q = d.quote || {};
    if (state.type === "limit" || state.type === "stop_limit") return num($("dkPrice").value) || q.last || 0;
    if (state.type === "stop") return num($("dkStop").value) || q.last || 0;
    return (state.side === "buy" ? q.ask : q.bid) || q.last || 0;
  }

  function ticketBody() {
    const body = {
      symbol: $("dkSymbol").value.trim().toUpperCase().replace("-", "/"),
      side: state.side,
      type: state.type,
      tif: $("dkTif").value,
      reduce_only: $("dkReduce").checked,
      post_only: $("dkPost").checked,
      label: $("dkLabel").value.trim() || "manual",
    };
    const v = num($("dkQty").value);
    if (state.qtyMode === "base") body.qty = v;
    if (state.qtyMode === "quote") body.quote_qty = v;
    if (state.qtyMode === "equity_pct") body.equity_pct = v;
    if (state.qtyMode === "risk_pct") body.risk_pct = v;
    if (state.type === "limit" || state.type === "stop_limit") body.price = num($("dkPrice").value);
    if (state.type === "stop" || state.type === "stop_limit") body.stop_price = num($("dkStop").value);
    if (state.type === "trailing_stop") body.trail_pct = num($("dkTrail").value) / 100;
    if ($("dkBracket").checked) {
      if (num($("dkSl").value)) body.stop_loss = num($("dkSl").value);
      if (num($("dkTp").value)) body.take_profit = num($("dkTp").value);
      if (num($("dkBt").value)) body.bracket_trail_pct = num($("dkBt").value) / 100;
    }
    if (state.qtyMode === "risk_pct" && !body.stop_loss) body.stop_loss = num($("dkSl").value) || num($("dkStop").value);
    return body;
  }

  function preview() {
    const d = state.desk;
    if (!d) return;
    const ref = refPrice();
    const equity = num(d.equity, 1);
    const v = num($("dkQty").value);
    let qty = 0;
    if (state.qtyMode === "base") qty = v;
    else if (state.qtyMode === "quote") qty = ref ? v / ref : 0;
    else if (state.qtyMode === "equity_pct") qty = ref ? (equity * v) / 100 / ref : 0;
    let blocker = "";
    if (state.qtyMode === "risk_pct") {
      const stop = num($("dkSl").value) || num($("dkStop").value);
      if (!stop) blocker = "Risk sizing needs a stop — tick “attach bracket” and set one.";
      else if (ref <= stop) blocker = `The stop (${px(stop)}) has to sit below the entry (${px(ref)}).`;
      else qty = ((equity * v) / 100) / (ref - stop);
    }
    const notional = qty * ref;
    const fee = (notional * num(d.risk_cfg && d.risk_cfg.fee_bps)) / 10000;
    const stop = num($("dkSl").value) || (state.side === "sell" ? 0 : num($("dkStop").value));
    const riskAmt = stop && ref > stop ? qty * (ref - stop) : 0;
    const tp = num($("dkTp").value);
    const rr = riskAmt && tp && tp > ref ? (qty * (tp - ref)) / riskAmt : 0;
    const overCash = state.side === "buy" && notional > num(d.cash);
    const capPct = num(d.risk_cfg && d.risk_cfg.max_position_pct, 100);
    const pctEq = equity ? (notional / equity) * 100 : 0;
    const overCap = state.side === "buy" && capPct && pctEq > capPct;
    $("dkPreview").innerHTML = `
      <div><span>Quantity</span><b>${qty ? qty.toPrecision(5) : "—"}</b></div>
      <div><span>Notional</span><b class="${overCash ? "down" : ""}">${notional ? usd(notional) : "—"}</b></div>
      <div><span>% equity</span><b class="${overCap ? "down" : ""}">${notional ? pctEq.toFixed(1) + "%" : "—"}</b></div>
      <div><span>Est. fee</span><b>${fee ? usd(fee) : "—"}</b></div>
      <div><span>Risk if stopped</span><b class="${riskAmt ? "down" : ""}">${
        riskAmt ? usd(riskAmt) + " · " + ((riskAmt / equity) * 100).toFixed(2) + "%" : "—"
      }</b></div>
      <div><span>Reward:risk</span><b>${rr ? rr.toFixed(2) + "R" : "—"}</b></div>`;
    if (!blocker && overCap)
      blocker = `That is ${pctEq.toFixed(0)}% of equity — your position cap is ${capPct}%. Widen the stop or cut the risk.`;
    $("dkHint").textContent =
      blocker ||
      (overCash
        ? "That order is larger than your cash — it will be rejected."
        : "Paper fills against the live book. Risk-based sizing needs a stop.");
    $("dkHint").className = blocker || overCash ? "hint warn" : "hint";
  }

  async function submit() {
    const body = ticketBody();
    if (!body.symbol) return say("Pick a symbol first");
    const res = await send("/api/orders", body);
    if (!res.ok) return say("Rejected: " + res.error);
    const o = res.order;
    say(`${o.side.toUpperCase()} ${o.symbol} · ${o.type} · ${Number(o.qty).toPrecision(4)}${
      res.brackets && res.brackets.length ? " (+bracket)" : ""
    }`);
    loadDesk();
  }

  // ====================================================== portfolio & risk
  async function loadPortfolio() {
    const p = await get("/api/portfolio");
    state.portfolio = p;
    const exp = p.exposure || {};
    const risk = p.open_risk || {};
    const v = p.var || {};
    const kpi = (lbl, val, sub, cls) =>
      `<div class="kpi"><div class="lbl">${lbl}</div><div class="val ${cls || ""}">${val}</div><div class="sub">${sub}</div></div>`;
    $("pfKpis").innerHTML =
      kpi("Equity", usd(p.equity), `${usd(p.cash)} cash`) +
      kpi("Gross exposure", usd(exp.gross), `${num(exp.gross_pct).toFixed(1)}% of equity`) +
      kpi("Net exposure", usd(exp.net), `${exp.positions || 0} positions`) +
      kpi(
        "Open risk",
        num(risk.pct_equity).toFixed(2) + "%",
        `${usd(risk.total)} to stops`,
        num(risk.pct_equity) > 6 ? "down" : "up"
      ) +
      kpi(
        "VaR 95%",
        v.ok ? num(v.var95_pct).toFixed(2) + "%" : "—",
        v.ok ? `${usd(v.var95_value)} · ES ${num(v.expected_shortfall_pct).toFixed(2)}%` : "needs 20 samples"
      ) +
      kpi("Concentration", num(exp.concentration).toFixed(2), exp.concentration_label || "—");

    $("pfWarn").innerHTML = (p.warnings || []).length
      ? p.warnings.map((w) => `<div class="chip sev-warn">${esc(w)}</div>`).join("")
      : '<div class="chip">book looks balanced</div>';

    $("pfExpSub").textContent = `${num(exp.cash_pct).toFixed(0)}% in cash`;
    $("pfExposure").innerHTML = (exp.rows || []).length
      ? exp.rows
          .map(
            (r) => `<tr><td><b>${esc(r.symbol)}</b></td><td>${usd(r.notional)}</td>
        <td>${num(r.pct_equity).toFixed(1)}%</td>
        <td><div class="wbar"><i style="width:${Math.min(100, num(r.weight) * 100)}%"></i></div></td>
        <td class="${num(r.unrealized) >= 0 ? "up" : "down"}">${usd(r.unrealized)}</td></tr>`
          )
          .join("")
      : '<tr><td colspan="5" class="muted">no open positions</td></tr>';

    $("pfRiskSub").textContent = risk.unprotected && risk.unprotected.length
      ? risk.unprotected.length + " unprotected"
      : "all stopped";
    $("pfRisk").innerHTML = (risk.rows || []).length
      ? risk.rows
          .map(
            (r) => `<tr><td><b>${esc(r.symbol)}</b></td>
        <td>${r.stop ? px(r.stop) : '<span class="down">none</span>'}</td>
        <td>${r.risk ? usd(r.risk) : "—"}</td>
        <td>${num(r.pct_equity).toFixed(2)}%</td>
        <td>${r.r_open == null ? "—" : signed(r.r_open, 2) + "R"}</td></tr>`
          )
          .join("")
      : '<tr><td colspan="5" class="muted">nothing at risk</td></tr>';

    renderCorr(p.correlations || {});
    renderVar(v);
    renderR(p.r_distribution || {});

    const tags = p.tags || [];
    $("pfTags").innerHTML = tags.length
      ? tags
          .map(
            (t) => `<tr><td><b>${esc(t.tag)}</b></td><td>${t.trades}</td>
        <td>${(num(t.win_rate) * 100).toFixed(0)}%</td>
        <td class="${num(t.net) >= 0 ? "up" : "down"}">${usd(t.net)}</td>
        <td>${usd(t.avg)}</td></tr>`
          )
          .join("")
      : '<tr><td colspan="5" class="muted">tag some trades in the journal</td></tr>';
  }

  function renderCorr(c) {
    const syms = c.symbols || [];
    $("pfCorrSub").textContent = c.diversification
      ? `${c.diversification} · avg ${num(c.average).toFixed(2)}`
      : "needs two positions";
    if (syms.length < 2) {
      $("pfCorr").innerHTML = '<div class="muted">watch at least two coins to compare them</div>';
      return;
    }
    const short = (s) => s.split("/")[0];
    const cell = (v) => {
      const n = num(v);
      const hue = n >= 0 ? "255,93,115" : "61,255,154"; // correlated = risk = red
      return `<td style="background:rgba(${hue},${(Math.abs(n) * 0.55).toFixed(2)})">${n.toFixed(2)}</td>`;
    };
    $("pfCorr").innerHTML =
      `<table class="data corr"><thead><tr><th></th>${syms
        .map((s) => `<th>${esc(short(s))}</th>`)
        .join("")}</tr></thead><tbody>` +
      syms
        .map((s, i) => `<tr><th>${esc(short(s))}</th>${(c.matrix[i] || []).map(cell).join("")}</tr>`)
        .join("") +
      "</tbody></table>" +
      ((c.most_correlated || []).length
        ? '<div class="muted small" style="margin-top:8px">tightest pairs: ' +
          c.most_correlated
            .slice(0, 3)
            .map((p) => `${esc(short(p.a))}/${esc(short(p.b))} ${num(p.corr).toFixed(2)}`)
            .join(" · ") +
          "</div>"
        : "");
  }

  function renderVar(v) {
    if (!v.ok) {
      $("pfVar").innerHTML = `<div class="muted">${esc(v.note || "not enough equity history yet")}</div>`;
      return;
    }
    $("pfVar").innerHTML = `
      <div class="metric-grid">
        <div class="m"><span>VaR 95%</span><b class="down">${num(v.var95_pct).toFixed(2)}%</b></div>
        <div class="m"><span>VaR 99%</span><b class="down">${num(v.var99_pct).toFixed(2)}%</b></div>
        <div class="m"><span>Expected shortfall</span><b class="down">${num(v.expected_shortfall_pct).toFixed(2)}%</b></div>
        <div class="m"><span>Worst period</span><b class="down">${num(v.worst_pct).toFixed(2)}%</b></div>
        <div class="m"><span>Volatility</span><b>${num(v.vol_pct).toFixed(2)}%</b></div>
        <div class="m"><span>Samples</span><b>${v.samples}</b></div>
      </div>
      <p class="hint">On 95 of 100 comparable periods the book lost less than ${usd(v.var95_value)}. The other five
      averaged ${num(v.expected_shortfall_pct).toFixed(2)}%.</p>`;
  }

  function renderR(d) {
    if (!d.ok) {
      $("pfRSub").textContent = "no closed trades yet";
      $("pfRDist").innerHTML = '<div class="muted">close a few trades and the shape shows up here</div>';
      return;
    }
    $("pfRSub").textContent = `${d.trades} trades · expectancy ${num(d.expectancy_r).toFixed(2)}R`;
    const max = Math.max(1, ...d.buckets.map((b) => b.count));
    $("pfRDist").innerHTML =
      d.buckets
        .map(
          (b) => `<div class="bar"><span>${esc(b.label)}</span>
        <div class="fill ${b.label.indexOf("-") === 0 ? "neg" : "pos"}" style="width:${(b.count / max) * 100}%"></div>
        <span>${b.count}</span></div>`
        )
        .join("") +
      `<div class="metric-grid" style="margin-top:12px">
        <div class="m"><span>Best</span><b class="up">${num(d.best_r).toFixed(2)}R</b></div>
        <div class="m"><span>Worst</span><b class="down">${num(d.worst_r).toFixed(2)}R</b></div>
        <div class="m"><span>Avg win</span><b class="up">${num(d.avg_win_r).toFixed(2)}R</b></div>
        <div class="m"><span>Avg loss</span><b class="down">${num(d.avg_loss_r).toFixed(2)}R</b></div>
        <div class="m"><span>Win rate</span><b>${(num(d.win_rate) * 100).toFixed(0)}%</b></div>
        <div class="m"><span>Top decile share</span><b>${(num(d.top_decile_share) * 100).toFixed(0)}%</b></div>
      </div>
      <p class="hint">The best 10% of trades produced ${(num(d.top_decile_share) * 100).toFixed(0)}% of the gross gain —
      cutting winners short is the expensive mistake here.</p>`;
  }

  // ======================================================== trade review
  async function loadReview() {
    const tag = $("jrTag").value;
    const j = await get("/api/journal?limit=120" + (tag ? "&tag=" + encodeURIComponent(tag) : ""));
    const cur = $("jrTag").value;
    $("jrTag").innerHTML =
      '<option value="">all tags</option>' + (j.tags || []).map((t) => `<option ${t === cur ? "selected" : ""}>${esc(t)}</option>`).join("");
    $("jrSub").textContent = `${j.rows.length} of ${j.total} closed trades`;
    $("jrBody").innerHTML = j.rows.length
      ? j.rows
          .map((r) => {
            const e = r.journal || {};
            return `<tr>
        <td>${clock(r.closed)}</td>
        <td><b>${esc(r.symbol)}</b></td>
        <td class="${num(r.pnl) >= 0 ? "up" : "down"}">${usd(r.pnl)}</td>
        <td>${signed(r.r, 2)}</td>
        <td>${esc(r.reason || "")}</td>
        <td><input class="mini wide" data-tags="${esc(r.id)}" value="${esc((e.tags || []).join(", "))}" placeholder="breakout, fomo" /></td>
        <td><select class="mini" data-rate="${esc(r.id)}">${[0, 1, 2, 3, 4, 5]
              .map((n) => `<option value="${n}" ${num(e.rating) === n ? "selected" : ""}>${n ? "★".repeat(n) : "—"}</option>`)
              .join("")}</select></td>
        <td><input class="mini wide" data-note="${esc(r.id)}" value="${esc(e.note || "")}" placeholder="what happened?" /></td>
      </tr>`;
          })
          .join("")
      : '<tr><td colspan="8" class="muted">no closed trades yet</td></tr>';

    const write = async (id, patch) => {
      await send("/api/journal", { fill_id: id, ...patch });
      say("Saved");
    };
    $("jrBody")
      .querySelectorAll("[data-tags]")
      .forEach((i) => {
        i.onchange = () =>
          write(i.dataset.tags, { tags: i.value.split(",").map((s) => s.trim()).filter(Boolean) });
      });
    $("jrBody")
      .querySelectorAll("[data-note]")
      .forEach((i) => (i.onchange = () => write(i.dataset.note, { note: i.value })));
    $("jrBody")
      .querySelectorAll("[data-rate]")
      .forEach((s) => (s.onchange = () => write(s.dataset.rate, { rating: Number(s.value) })));
  }

  // ====================================================== command palette
  const commands = () => {
    const sym = state.symbol;
    const list = [
      { k: "Go to trade desk", run: () => goto("desk") },
      { k: "Go to risk & portfolio", run: () => goto("risk") },
      { k: "Go to trade review", run: () => goto("journal") },
      { k: "Go to screener", run: () => goto("screeners") },
      { k: "Go to universe", run: () => goto("universe") },
      { k: "Go to bot control", run: () => goto("bot") },
      {
        k: `Buy ${sym} for 1% of equity`,
        run: async () => {
          const r = await send("/api/orders", { symbol: sym, side: "buy", type: "market", equity_pct: 1, label: "cmdk" });
          say(r.ok ? "Bought " + sym : r.error);
          loadDesk();
        },
      },
      {
        k: `Close ${sym}`,
        run: async () => {
          const r = await send("/api/close", { symbol: sym });
          say(r.ok === false ? r.error || "nothing to close" : "Closed " + sym);
          loadDesk();
        },
      },
      {
        k: `Cancel all orders on ${sym}`,
        run: async () => {
          const r = await send(`/api/orders/cancel_all?symbol=${encodeURIComponent(sym)}`);
          say(`Cancelled ${r.cancelled} order(s)`);
          loadDesk();
        },
      },
      {
        k: "Cancel every working order",
        run: async () => {
          const r = await send("/api/orders/cancel_all");
          say(`Cancelled ${r.cancelled} order(s)`);
          loadDesk();
        },
      },
      { k: "Flatten the whole book", run: async () => { await send("/api/flatten"); say("Flattening"); } },
      { k: "Pause the robot", run: async () => { await send("/api/pause"); say("Paused"); } },
      { k: "Resume the robot", run: async () => { await send("/api/start"); say("Running"); } },
      {
        k: "Toggle: robot may close desk positions",
        run: async () => {
          const r = await send("/api/desk/settings", { manage_manual: !(state.desk && state.desk.manage_manual) });
          say(r.manage_manual ? "Robot manages desk positions" : "Desk positions are yours");
          loadDesk();
        },
      },
    ];
    (state.desk && state.desk.watchlist ? state.desk.watchlist : []).forEach((s) => {
      list.push({
        k: `Desk: ${s}`,
        run: () => {
          state.symbol = s;
          $("dkSymbol").value = s;
          goto("desk");
        },
      });
    });
    return list;
  };

  let cmdIndex = 0;
  let cmdRows = [];

  function paintPalette() {
    const q = $("cmdkInput").value.toLowerCase().trim();
    cmdRows = commands().filter((c) => !q || c.k.toLowerCase().includes(q));
    cmdIndex = Math.min(cmdIndex, Math.max(0, cmdRows.length - 1));
    $("cmdkList").innerHTML = cmdRows.length
      ? cmdRows.map((c, i) => `<div class="cmdk-row ${i === cmdIndex ? "on" : ""}" data-i="${i}">${esc(c.k)}</div>`).join("")
      : '<div class="cmdk-row muted">nothing matches</div>';
    $("cmdkList")
      .querySelectorAll("[data-i]")
      .forEach((r) => {
        r.onclick = () => runCmd(Number(r.dataset.i));
      });
  }

  function openPalette() {
    $("cmdk").style.display = "flex";
    $("cmdkInput").value = "";
    cmdIndex = 0;
    paintPalette();
    $("cmdkInput").focus();
  }
  const closePalette = () => ($("cmdk").style.display = "none");

  function runCmd(i) {
    const c = cmdRows[i];
    closePalette();
    if (c) c.run();
  }

  // ================================================================= boot
  function boot() {
    if (!$("dkSymbol")) return;

    $("dkSide").querySelectorAll(".tab").forEach((t) => (t.onclick = () => setSide(t.dataset.side)));
    $("dkType").querySelectorAll(".tab").forEach((t) => (t.onclick = () => setType(t.dataset.type)));
    $("dkQtyMode").querySelectorAll(".tab").forEach((t) => (t.onclick = () => setQtyMode(t.dataset.mode)));
    $("dkQtyQuick")
      .querySelectorAll("[data-frac]")
      .forEach((b) => {
        b.onclick = () => {
          const d = state.desk || {};
          const frac = Number(b.dataset.frac);
          if (state.qtyMode === "quote") $("dkQty").value = (num(d.cash) * frac).toFixed(2);
          else if (state.qtyMode === "equity_pct") $("dkQty").value = (frac * 100).toFixed(0);
          else if (state.qtyMode === "risk_pct") $("dkQty").value = (frac * 2).toFixed(2);
          preview();
        };
      });
    ["dkQty", "dkPrice", "dkStop", "dkTrail", "dkSl", "dkTp", "dkBt"].forEach((id) => ($(id).oninput = preview));
    $("dkBracket").onchange = () => {
      $("dkBracketBox").style.display = $("dkBracket").checked ? "" : "none";
      preview();
    };
    $("dkSubmit").onclick = submit;
    $("dkSymbol").onchange = () => {
      state.symbol = $("dkSymbol").value.trim().toUpperCase().replace("-", "/");
      loadDesk();
    };
    $("dkFlatten").onclick = async () => {
      await send("/api/close", { symbol: state.symbol });
      await send(`/api/orders/cancel_all?symbol=${encodeURIComponent(state.symbol)}`);
      say("Flat on " + state.symbol);
      loadDesk();
    };
    $("dkCancelAll").onclick = async () => {
      const r = await send("/api/orders/cancel_all");
      say(`Cancelled ${r.cancelled} order(s)`);
      loadDesk();
    };
    $("dkManage").onchange = async () => {
      await send("/api/desk/settings", { manage_manual: $("dkManage").checked });
      say($("dkManage").checked ? "Robot can now exit desk positions" : "Desk positions are yours — stops still apply");
      loadDesk();
    };
    $("dkPalette").onclick = openPalette;
    $("jrRefresh").onclick = loadReview;
    $("jrTag").onchange = loadReview;

    setType("market");
    setQtyMode("quote");
    $("dkQty").value = 250;

    // palette hotkeys
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        $("cmdk").style.display === "flex" ? closePalette() : openPalette();
        return;
      }
      if ($("cmdk").style.display !== "flex") return;
      if (e.key === "Escape") closePalette();
      if (e.key === "ArrowDown") {
        cmdIndex = Math.min(cmdIndex + 1, cmdRows.length - 1);
        paintPalette();
        e.preventDefault();
      }
      if (e.key === "ArrowUp") {
        cmdIndex = Math.max(cmdIndex - 1, 0);
        paintPalette();
        e.preventDefault();
      }
      if (e.key === "Enter") runCmd(cmdIndex);
    });
    $("cmdkInput").oninput = () => {
      cmdIndex = 0;
      paintPalette();
    };
    $("cmdk").onclick = (e) => {
      if (e.target.id === "cmdk") closePalette();
    };

    document.querySelectorAll(".nav").forEach((btn) => {
      const prev = btn.onclick;
      btn.onclick = (e) => {
        if (prev) prev(e);
        const v = btn.dataset.view;
        if (v === "desk") loadDesk();
        if (v === "risk") loadPortfolio();
        if (v === "journal") loadReview();
      };
    });

    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(() => {
      if (viewOn("desk")) loadDesk();
      else if (viewOn("risk")) loadPortfolio();
    }, 5000);
  }

  window.Desk = { loadDesk, loadPortfolio, loadReview, openPalette, goto, boot };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
