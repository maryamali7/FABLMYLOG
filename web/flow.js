/* FablMyLog — volume dots.

   A candlestick chart of the consolidated cross-exchange tape, with one dot per
   bar placed at that bar's volume-weighted price: green when buyers were the
   aggressors, red when sellers were, sized by how much volume traded. Under it,
   a delta histogram and the cumulative delta line.

   Drawn with lightweight-charts v5 (vendored, no CDN). Self-contained IIFE. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const get = async (u) => (await fetch(u)).json();
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
  const num = (v, d) => (Number.isFinite(Number(v)) ? Number(v) : d || 0);

  const compact = (v) => {
    const n = Math.abs(num(v));
    const sign = num(v) < 0 ? "-" : "";
    if (n >= 1e9) return sign + (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return sign + (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return sign + (n / 1e3).toFixed(1) + "K";
    if (n >= 1) return sign + n.toFixed(2);
    return sign + n.toPrecision(3);
  };
  const px = (v) => {
    const n = num(v);
    if (!n) return "—";
    if (n >= 1000) return n.toFixed(2);
    if (n >= 1) return n.toFixed(4);
    return n.toPrecision(5);
  };
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  const GREEN = "#3dff9a";
  const RED = "#ff5d73";
  const state = { symbol: "BTC/USDT", tf: "1m", venue: "", data: null, timer: null, built: false };

  let chart = null;
  let candles = null;
  let deltaSeries = null;
  let cvdSeries = null;
  let markers = null;
  let ro = null;

  const LWC = () => window.LightweightCharts;

  // --------------------------------------------------------------- the chart
  function build() {
    const lib = LWC();
    const host = $("vdChart");
    if (!lib || !host || state.built) return !!state.built;

    chart = lib.createChart(host, {
      layout: {
        background: { color: "transparent" },
        textColor: "#8fa0b3",
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
        fontSize: 11,
        panes: { separatorColor: "#1d2d3d", separatorHoverColor: "#2a4157" },
      },
      grid: { vertLines: { color: "rgba(29,45,61,0.45)" }, horzLines: { color: "rgba(29,45,61,0.45)" } },
      rightPriceScale: { borderColor: "#1d2d3d", scaleMargins: { top: 0.08, bottom: 0.08 } },
      timeScale: { borderColor: "#1d2d3d", timeVisible: true, secondsVisible: true, rightOffset: 4 },
      crosshair: {
        mode: lib.CrosshairMode ? lib.CrosshairMode.Normal : 0,
        vertLine: { color: "#2ee6c8", width: 1, style: 3, labelBackgroundColor: "#17293a" },
        horzLine: { color: "#2ee6c8", width: 1, style: 3, labelBackgroundColor: "#17293a" },
      },
      autoSize: false,
      width: host.clientWidth,
      height: host.clientHeight,
    });

    candles = chart.addSeries(lib.CandlestickSeries, {
      upColor: "rgba(61,255,154,0.22)",
      downColor: "rgba(255,93,115,0.22)",
      borderUpColor: "rgba(61,255,154,0.65)",
      borderDownColor: "rgba(255,93,115,0.65)",
      wickUpColor: "rgba(61,255,154,0.55)",
      wickDownColor: "rgba(255,93,115,0.55)",
      priceLineVisible: false,
    });

    deltaSeries = chart.addSeries(lib.HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false }, 1);
    cvdSeries = chart.addSeries(lib.LineSeries, {
      color: "#d7b56a", lineWidth: 2, priceLineVisible: false, lastValueVisible: true,
    }, 2);

    try {
      chart.panes()[0].setHeight(320);
      chart.panes()[1].setHeight(90);
      chart.panes()[2].setHeight(90);
    } catch (e) { /* older pane API — the defaults are fine */ }

    markers = lib.createSeriesMarkers(candles, []);
    chart.subscribeCrosshairMove(onHover);

    if (window.ResizeObserver) {
      ro = new window.ResizeObserver(() => {
        if (host.clientWidth) chart.resize(host.clientWidth, host.clientHeight);
      });
      ro.observe(host);
    }
    state.built = true;
    return true;
  }

  function onHover(param) {
    const box = $("vdHover");
    if (!box) return;
    if (!param || !param.time || !state.data) {
      box.innerHTML = "";
      return;
    }
    const dot = (state.data.dots || []).find((d) => Math.floor(d.ts) === param.time);
    const bar = (state.data.candles || []).find((c) => Math.floor(c.ts) === param.time);
    if (!bar) {
      box.innerHTML = "";
      return;
    }
    const venues = dot && dot.venues ? Object.entries(dot.venues) : [];
    box.innerHTML = `
      <b>${clock(bar.ts)}</b>
      <span>O ${px(bar.open)} H ${px(bar.high)} L ${px(bar.low)} C ${px(bar.close)}</span>
      <span>vol <b>${compact(bar.volume)}</b></span>
      <span class="${bar.delta >= 0 ? "up" : "down"}">delta <b>${bar.delta >= 0 ? "+" : ""}${compact(bar.delta)}</b>${
      dot ? ` (${(dot.delta_pct * 100).toFixed(0)}%)` : ""
    }</span>
      ${dot ? `<span class="vd-kind k-${dot.kind}">${esc(dot.kind)}</span><span class="muted">${esc(dot.note)}</span>` : ""}
      ${venues.length ? `<span class="muted">${venues.map(([v, s]) => `${esc(v)} ${compact(s.buy - s.sell)}`).join(" · ")}</span>` : ""}`;
  }

  // ---------------------------------------------------------------- painting
  function paint(d) {
    if (!build()) {
      $("vdChart").innerHTML = '<div class="muted" style="padding:24px">chart library did not load</div>';
      return;
    }
    const bars = (d.candles || []).filter((c) => c.close > 0);
    if (!bars.length) {
      candles.setData([]);
      deltaSeries.setData([]);
      cvdSeries.setData([]);
      markers.setMarkers([]);
      return;
    }
    // lightweight-charts wants whole seconds, strictly ascending and unique
    const seen = new Set();
    const rows = [];
    for (const c of bars) {
      const t = Math.floor(c.ts);
      if (seen.has(t)) continue;
      seen.add(t);
      rows.push({ ...c, t });
    }
    rows.sort((a, b) => a.t - b.t);

    candles.setData(rows.map((c) => ({ time: c.t, open: c.open, high: c.high, low: c.low, close: c.close })));

    deltaSeries.setData(
      rows.map((c) => ({
        time: c.t,
        value: c.delta,
        color: c.delta >= 0 ? "rgba(61,255,154,0.6)" : "rgba(255,93,115,0.6)",
      }))
    );
    deltaSeries.applyOptions({ visible: true });

    const cvdMap = new Map((d.cvd || []).map((p) => [Math.floor(p.ts), p.value]));
    cvdSeries.setData(rows.filter((c) => cvdMap.has(c.t)).map((c) => ({ time: c.t, value: cvdMap.get(c.t) })));
    cvdSeries.applyOptions({ visible: $("vdCvd").checked });

    // the dots themselves
    if ($("vdDots").checked) {
      const byTime = new Map();
      for (const dot of d.dots || []) {
        const t = Math.floor(dot.ts);
        if (seen.has(t)) byTime.set(t, dot);
      }
      const out = [];
      for (const [t, dot] of byTime) {
        const bull = dot.delta >= 0;
        const strength = Math.min(1, Math.abs(dot.delta_pct) * 1.8 + 0.25);
        const rgb = bull ? "61,255,154" : "255,93,115";
        const loud = dot.kind === "absorption" || dot.kind === "divergence";
        out.push({
          time: t,
          position: "atPriceMiddle",
          price: dot.price,
          shape: loud ? "square" : "circle",
          size: Math.max(0.6, dot.size * 0.55),
          color: loud ? (bull ? "#d7b56a" : "#d7b56a") : `rgba(${rgb},${strength.toFixed(2)})`,
          text: dot.size >= 5 ? compact(dot.volume) : undefined,
        });
      }
      out.sort((a, b) => a.time - b.time);
      markers.setMarkers(out);
    } else {
      markers.setMarkers([]);
    }

    if (d.poc) {
      if (paint._poc) {
        try { candles.removePriceLine(paint._poc); } catch (e) { /* gone already */ }
      }
      paint._poc = candles.createPriceLine({
        price: d.poc, color: "#7aa7ff", lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: "POC",
      });
    }
    if (!paint._fitted) {
      chart.timeScale().fitContent();
      paint._fitted = true;
    }
  }

  // ------------------------------------------------------------------ panels
  function paintKpis(d) {
    const s = d.summary || {};
    const r = d.read || {};
    const kpi = (lbl, val, sub, cls) =>
      `<div class="kpi"><div class="lbl">${lbl}</div><div class="val ${cls || ""}">${val}</div><div class="sub">${sub}</div></div>`;
    const tilt = num(s.delta_pct) * 100;
    $("vdKpis").innerHTML =
      kpi("Window delta", (s.delta >= 0 ? "+" : "") + compact(s.delta), `${tilt.toFixed(1)}% of volume`,
          s.delta >= 0 ? "up" : "down") +
      kpi("Volume", compact(s.volume), `${s.trades || 0} prints`) +
      kpi("Buy / sell bars", `${s.buy_bars || 0} / ${s.sell_bars || 0}`, `${s.bars || 0} bars on screen`) +
      kpi("Last bar", (s.last_delta >= 0 ? "+" : "") + compact(s.last_delta),
          `${(num(s.last_delta_pct) * 100).toFixed(0)}% one-sided`, s.last_delta >= 0 ? "up" : "down") +
      kpi("Exchanges", String((d.venues || []).length), (d.coverage && d.coverage.venues || []).join(", ") || "—") +
      kpi("Flow read", r.direction ? r.direction.toUpperCase() : "—", `score ${r.score || 0} · ${r.confidence || 0}% conf`,
          r.direction === "up" ? "up" : r.direction === "down" ? "down" : "");
  }

  function paintRead(d) {
    const r = d.read || {};
    $("vdReadSub").textContent = r.ok ? `${r.bars_read} bars of ${d.tf} flow` : "";
    if (!r.ok) {
      $("vdRead").innerHTML = `<div class="muted">${esc(r.headline || "not enough tape yet")}</div>`;
      return;
    }
    const arrow = r.direction === "up" ? "▲" : r.direction === "down" ? "▼" : "◆";
    const gauge = Math.min(100, Math.max(0, (num(r.score) + 100) / 2));
    $("vdRead").innerHTML = `
      <div class="fc-dir ${r.direction === "up" ? "up" : r.direction === "down" ? "down" : "flat"}">
        <span class="arrow">${arrow}</span>
        <div><b>${esc(r.headline)}</b><small>${r.confidence}% confidence · score ${r.score >= 0 ? "+" : ""}${r.score}</small></div>
      </div>
      <div class="vd-gauge"><i style="left:${gauge}%"></i><span class="down">sellers</span><span class="up">buyers</span></div>
      <ul class="vd-reasons">
        ${(r.reasons || [])
          .map(
            (x) => `<li class="${x.bull ? "bull" : "bear"}">
              <b>${esc(x.text)}</b><span>${esc(x.detail || "")}</span>
              <i>${x.weight > 0 ? "+" : ""}${x.weight}</i></li>`
          )
          .join("")}
      </ul>
      <p class="hint">Price slope ${r.price_slope} vs delta slope ${r.cvd_slope} (×1000). When they disagree, the
      move is not being paid for.${r.estimated ? " Some bars are estimated from candle shape, not real prints." : ""}</p>`;
  }

  function paintVenues(d) {
    const rows = d.venues || [];
    $("vdVenues").innerHTML = rows.length
      ? rows
          .map(
            (v) => `<tr>
        <td><b>${esc(v.venue)}</b></td>
        <td>${(v.share * 100).toFixed(0)}%</td>
        <td class="up">${compact(v.buy)}</td>
        <td class="down">${compact(v.sell)}</td>
        <td class="${v.delta >= 0 ? "up" : "down"}">${v.delta >= 0 ? "+" : ""}${compact(v.delta)}
          <div class="wbar ${v.delta >= 0 ? "" : "neg"}"><i style="width:${Math.min(100, Math.abs(v.delta_pct) * 200)}%"></i></div>
        </td></tr>`
          )
          .join("")
      : '<tr><td colspan="5" class="muted">no per-exchange tape for this symbol yet</td></tr>';

    const cur = $("vdVenue").value;
    const opts = ['<option value="">all exchanges</option>']
      .concat(rows.map((v) => `<option value="${esc(v.venue)}" ${v.venue === cur ? "selected" : ""}>${esc(v.venue)}</option>`))
      .join("");
    if ($("vdVenue").options.length !== rows.length + 1 || cur !== state.venue) $("vdVenue").innerHTML = opts;
  }

  function paintPrints(d) {
    const rows = d.prints || [];
    $("vdPrints").innerHTML = rows.length
      ? rows
          .map(
            (p) => `<div class="row">
        <span>${clock(p.ts)} · <b class="${p.side === "buy" ? "up" : "down"}">${p.side.toUpperCase()}</b> ${esc(p.venue)}</span>
        <b>${compact(p.qty)} @ ${px(p.price)} <span class="muted">${p.ratio}×</span></b></div>`
          )
          .join("")
      : '<div class="muted">nothing outsized has crossed the tape yet</div>';
  }

  // -------------------------------------------------------------------- load
  let busy = false;
  let queued = false;
  async function load() {
    if (busy) {
      queued = true;
      return;
    }
    busy = true;
    try {
      const url = `/api/flow/${encodeURIComponent(state.symbol)}?tf=${state.tf}&bars=240${
        state.venue ? "&venue=" + encodeURIComponent(state.venue) : ""
      }`;
      const d = await get(url);
      state.data = d;
      if ($("vdSymbol") !== document.activeElement) $("vdSymbol").value = d.symbol;
      $("vdList").innerHTML = (d.watchlist || []).map((s) => `<option value="${esc(s)}">`).join("");
      const q = d.quote || {};
      $("vdLast").textContent = q.last ? px(q.last) : "—";
      const chg = $("vdChg");
      chg.textContent = q.change_pct == null ? "—" : (q.change_pct >= 0 ? "+" : "") + q.change_pct.toFixed(2) + "%";
      chg.className = "pill " + (q.change_pct == null ? "dim" : q.change_pct >= 0 ? "up" : "down");
      const src = {
        tape: "live cross-exchange tape",
        estimated: "estimated from candle shape — no tape yet",
        mixed: "tape + estimated history",
      }[d.source];
      $("vdSource").innerHTML =
        esc(src) + (d.simulated ? ' <span class="badge warn">SIMULATED</span>' : "");
      $("vdChartSub").textContent = `${(d.venues || []).length} exchanges · ${d.coverage ? d.coverage.prints : 0} prints recorded`;

      paintKpis(d);
      paint(d);
      paintRead(d);
      paintVenues(d);
      paintPrints(d);
    } catch (e) {
      $("vdChartSub").textContent = "flow unavailable: " + e.message;
    } finally {
      busy = false;
      if (queued) {
        queued = false;
        load();
      }
    }
  }

  const active = () => {
    const v = document.querySelector('.view[data-view="volume"]');
    return v && v.classList.contains("on");
  };

  function boot() {
    if (!$("vdSymbol")) return;

    $("vdTf").querySelectorAll(".tab").forEach((t) => {
      t.onclick = () => {
        state.tf = t.dataset.tf;
        $("vdTf").querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
        paint._fitted = false;
        load();
      };
    });
    $("vdSymbol").onchange = () => {
      state.symbol = $("vdSymbol").value.trim().toUpperCase().replace("-", "/");
      state.venue = "";
      paint._fitted = false;
      load();
    };
    $("vdVenue").onchange = () => {
      state.venue = $("vdVenue").value;
      load();
    };
    $("vdDots").onchange = () => (state.data ? paint(state.data) : null);
    $("vdCvd").onchange = () => (state.data ? paint(state.data) : null);

    document.querySelectorAll(".nav").forEach((btn) => {
      const prev = btn.onclick;
      btn.onclick = (e) => {
        if (prev) prev(e);
        if (btn.dataset.view === "volume") {
          paint._fitted = false;
          load();
        }
      };
    });

    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(() => {
      if (active() && $("vdLive").checked) load();
    }, 4000);
  }

  window.Flow = { load, boot, state };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
