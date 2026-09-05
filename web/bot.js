/* FablMyLog — bot control: which coins trade, the edge engine, exchange keys
   and the 24/7 supervisor. Self-contained IIFE. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const get = async (u) => (await fetch(u)).json();
  const send = async (u, b, method) =>
    (
      await fetch(u, {
        method: method || "POST",
        headers: { "Content-Type": "application/json" },
        body: b ? JSON.stringify(b) : undefined,
      })
    ).json();
  const say = (m) => (window.FML ? window.FML.toast(m) : null);
  const esc = (s) => String(s == null ? "" : s).replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
  const ago = (ts) => (window.FML && window.FML.ago ? window.FML.ago(ts) : new Date(ts * 1000).toLocaleTimeString());

  let selection = null;
  let edge = null;
  let uptime = null;
  let timer = null;

  const money = (v) => {
    const n = Number(v || 0);
    if (n >= 1e9) return "$" + (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return "$" + (n / 1e3).toFixed(1) + "K";
    return "$" + n.toFixed(0);
  };
  const pct = (v) => {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    return `<span class="${n >= 0 ? "up" : "down"}">${(n >= 0 ? "+" : "") + n.toFixed(2)}%</span>`;
  };

  // ------------------------------------------------------- coin selection
  async function loadSelection() {
    selection = await get("/api/trading/selection");
    const modes = $("selModes");
    modes.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.mode === selection.mode));
    $("selAutoBar").style.display = selection.mode === "auto" ? "flex" : "none";
    $("selTopN").value = selection.auto_top_n;
    $("selMetric").value = selection.auto_metric;
    if (selection.auto_min_volume) $("selMinVol").value = selection.auto_min_volume;
    $("selBasket").textContent = (selection.auto_basket || []).length
      ? "now trading: " + selection.auto_basket.join(", ")
      : "basket fills once the screener has scored the board";
    $("selSub").textContent =
      selection.mode === "all"
        ? `Trading every coin on the watchlist (${selection.active.length})`
        : `${selection.active.length} of ${(selection.candidates || []).length} coins armed for trading`;

    $("selBody").innerHTML = (selection.candidates || [])
      .map((c) => {
        const on = selection.mode === "selected" ? c.selected : c.active;
        return `<tr>
          <td><input type="checkbox" class="sw" data-sym="${c.symbol}" ${on ? "checked" : ""} ${
          selection.mode === "selected" ? "" : "disabled"
        } /></td>
          <td><b>${c.symbol}</b>${c.in_position ? ' <span class="badge">in position</span>' : ""}</td>
          <td class="mono">${c.price ? Number(c.price).toFixed(4) : "—"}</td>
          <td>${pct(c.change_pct)}</td>
          <td class="mono">${c.score == null ? "—" : Number(c.score).toFixed(1)}</td>
          <td><input class="mini tiny-num" type="number" step="0.1" min="0.1" max="3" value="${c.size_mult}" data-size="${c.symbol}" /></td>
          <td>${c.enabled === false ? '<span class="down">disarmed</span>' : c.active ? '<span class="up">trading</span>' : '<span class="muted">idle</span>'}</td>
        </tr>`;
      })
      .join("");

    $("selBody").querySelectorAll("[data-sym]").forEach((box) => {
      box.onchange = async () => {
        selection = await send("/api/trading/toggle", { symbol: box.dataset.sym, on: box.checked });
        loadSelection();
      };
    });
    $("selBody").querySelectorAll("[data-size]").forEach((inp) => {
      inp.onchange = async () => {
        await send("/api/trading/toggle", { symbol: inp.dataset.size, size_mult: parseFloat(inp.value) || 1 });
        say(`${inp.dataset.size} size ×${inp.value}`);
      };
    });
  }

  // ---------------------------------------------------------- edge engine
  const EDGE_FIELDS = [
    ["enabled", "Edge engine on", "bool"],
    ["min_quality", "Min quality score", "num"],
    ["require_mtf", "Require timeframe agreement", "bool"],
    ["min_mtf_agreement", "Min agreement (0-1)", "num"],
    ["block_htf_downtrend", "Block bearish higher TF", "bool"],
    ["require_forecast", "Require a forecast", "bool"],
    ["min_forecast_prob", "Min forecast probability", "num"],
    ["regime_filter", "Skip risk-off regimes", "bool"],
    ["max_spread_bps", "Max spread (bps)", "num"],
    ["min_atr_pct", "Min ATR %", "num"],
    ["max_atr_pct", "Max ATR %", "num"],
    ["max_rsi", "Max RSI", "num"],
    ["max_open_correlated", "Max open positions", "num"],
    ["max_trades_per_day", "Max trades / day", "num"],
    ["max_consecutive_losses", "Max losses in a row", "num"],
    ["loss_cooldown_min", "Cooldown after loss (min)", "num"],
    ["symbol_cooldown_min", "Per-coin cooldown (min)", "num"],
    ["min_strategy_winrate", "Bench strategy below win rate", "num"],
    ["vol_target_pct", "Risk per trade (% equity)", "num"],
    ["kelly_cap", "Kelly bonus cap", "num"],
    ["atr_stop_mult", "Stop = ATR ×", "num"],
    ["atr_take_mult", "Target = ATR ×", "num"],
    ["atr_trail_mult", "Trail = ATR ×", "num"],
    ["breakeven_at_r", "Break-even at R", "num"],
    ["partial_1_r", "Partial 1 at R", "num"],
    ["partial_1_frac", "Partial 1 size", "num"],
    ["partial_2_r", "Partial 2 at R", "num"],
    ["partial_2_frac", "Partial 2 size", "num"],
    ["giveback_pct", "Close after giving back", "num"],
    ["time_stop_min", "Time stop (min)", "num"],
  ];

  async function loadEdge() {
    edge = await get("/api/edge");
    const cfg = edge.cfg || {};
    const cell = (l, v, cls) => `<div class="m"><span>${l}</span><b class="${cls || ""}">${v}</b></div>`;
    const best = (edge.by_strategy || [])[0];
    $("edgeKpis").innerHTML =
      cell("Taken", edge.accepted) +
      cell("Rejected", edge.rejected) +
      cell("Today", edge.trades_today) +
      cell("Loss streak", edge.consecutive_losses, edge.consecutive_losses ? "down" : "") +
      cell("Gate", cfg.enabled ? "on" : "off", cfg.enabled ? "up" : "down") +
      cell("Best strategy", best ? `${best.name} ${(best.win_rate * 100).toFixed(0)}%` : "—");

    $("edgeForm").innerHTML = EDGE_FIELDS.map(([key, label, kind]) => {
      const v = cfg[key];
      if (kind === "bool") {
        return `<label class="flag"><input type="checkbox" data-edge="${key}" ${v ? "checked" : ""} /> ${label}</label>`;
      }
      return `<label>${label}<input type="number" step="0.01" data-edge="${key}" value="${v}" /></label>`;
    }).join("");

    $("edgeBlocks").innerHTML = (edge.top_blocks || []).length
      ? edge.top_blocks.map((b) => `<tr><td>${esc(b.reason)}</td><td class="mono">${b.count}</td></tr>`).join("")
      : `<tr><td colspan="2" class="hint">No rejections yet.</td></tr>`;
    $("edgeRejects").innerHTML = (edge.recent_rejections || []).length
      ? edge.recent_rejections
          .slice(0, 12)
          .map(
            (r) =>
              `<tr><td class="muted">${ago(r.ts)}</td><td>${esc(r.symbol)}</td><td class="mono">${
                r.score == null ? "—" : r.score
              }</td><td class="muted">${esc((r.blocks || []).join(" · "))}</td></tr>`
          )
          .join("")
      : `<tr><td colspan="4" class="hint">Nothing rejected yet — the bot is either trading or seeing no signals.</td></tr>`;
  }

  async function saveEdge() {
    const patch = {};
    $("edgeForm").querySelectorAll("[data-edge]").forEach((el) => {
      patch[el.dataset.edge] = el.type === "checkbox" ? el.checked : parseFloat(el.value);
    });
    await send("/api/edge", { patch });
    say("Edge settings saved");
    loadEdge();
  }

  // ------------------------------------------------------------- api keys
  async function loadKeys() {
    const data = await get("/api/keys");
    const live = await get("/api/live");
    const venues = data.venues || [];
    const opts = venues.map((v) => `<option value="${v.venue}">${v.venue}</option>`).join("");
    if ($("keyVenue").innerHTML !== opts) $("keyVenue").innerHTML = opts;
    const tradable = venues.filter((v) => v.order_routing);
    $("liveVenue").innerHTML = tradable.map((v) => `<option value="${v.venue}">${v.venue}</option>`).join("");
    $("liveVenue").value = live.venue;
    $("keyNote").textContent =
      data.encryption === "fernet"
        ? "Encrypted with Fernet on this machine — never shown again after saving"
        : data.encryption_note;

    $("keyBody").innerHTML = venues
      .map(
        (v) => `<tr>
          <td><span class="venue-tag ${v.venue}">${v.venue}</span>${
          v.order_routing ? "" : ' <span class="muted" title="keys stored, order routing not wired">read-only</span>'
        }</td>
          <td class="mono">${v.configured ? esc(v.key_masked) : '<span class="muted">not set</span>'}</td>
          <td>${
            v.configured && v.order_routing
              ? `<input type="checkbox" class="sw" data-trade="${v.venue}" ${v.trade_enabled ? "checked" : ""} />`
              : "—"
          }</td>
          <td>${
            v.last_test
              ? `<span class="${v.last_test.ok ? "up" : "down"}">${v.last_test.ok ? "ok" : "failed"}</span> <span class="muted">${esc(
                  (v.last_test.detail || "").slice(0, 42)
                )}</span>`
              : '<span class="muted">never</span>'
          }</td>
          <td class="row-btns">${
            v.configured
              ? `<button class="btn tiny" data-test="${v.venue}">Test</button><button class="btn tiny danger" data-del="${v.venue}">Delete</button>`
              : ""
          }</td>
        </tr>`
      )
      .join("");

    $("keyBody").querySelectorAll("[data-test]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        b.textContent = "…";
        const res = await send("/api/keys/test", { venue: b.dataset.test });
        say(`${b.dataset.test}: ${res.ok ? "connected" : res.detail}`);
        loadKeys();
      };
    });
    $("keyBody").querySelectorAll("[data-del]").forEach((b) => {
      b.onclick = async () => {
        await fetch("/api/keys/" + b.dataset.del, { method: "DELETE" });
        say(b.dataset.del + " credentials deleted");
        loadKeys();
      };
    });
    $("keyBody").querySelectorAll("[data-trade]").forEach((box) => {
      box.onchange = async () => {
        await send("/api/keys/trading", { venue: box.dataset.trade, on: box.checked });
        loadKeys();
      };
    });

    const box = $("liveBox");
    box.className = "fb " + (live.armed ? "bad" : "");
    box.innerHTML = live.armed
      ? `<b>LIVE — real orders are going to ${live.venue}</b>, capped at $${live.max_notional} per entry. ${
          live.errors.length ? live.errors.length + " routing errors." : ""
        }`
      : `Paper mode. Orders are simulated. To trade for real: save keys, enable orders for the venue, run a successful test, then type <b>ARM LIVE</b>. ${
          live.can_trade ? "" : "<span class='muted'>" + esc(live.reason) + "</span>"
        }`;
  }

  // --------------------------------------------------------------- uptime
  const UP_FIELDS = [
    ["always_on", "Keep running 24/7", "bool"],
    ["auto_restart_loop", "Restart a stalled loop", "bool"],
    ["stall_timeout_sec", "Stall timeout (sec)", "num"],
    ["heartbeat_sec", "Check every (sec)", "num"],
    ["daily_reset_hour_utc", "Daily reset hour (UTC)", "num"],
    ["auto_resume_halt", "Auto-resume after a halt", "bool"],
    ["auto_resume_after_min", "Resume after (min)", "num"],
    ["maintenance_enabled", "Maintenance window", "bool"],
    ["maintenance_start_hour_utc", "Window start (UTC)", "num"],
    ["maintenance_end_hour_utc", "Window end (UTC)", "num"],
    ["flatten_in_maintenance", "Flatten before window", "bool"],
  ];

  async function loadUptime() {
    uptime = await get("/api/uptime");
    const cfg = uptime.cfg || {};
    const cell = (l, v, cls) => `<div class="m"><span>${l}</span><b class="${cls || ""}">${v}</b></div>`;
    $("upKpis").innerHTML =
      cell("Uptime", uptime.uptime_human) +
      cell("Loops", uptime.loops) +
      cell("Loop age", uptime.loop_age_sec + "s", uptime.loop_healthy ? "up" : "down") +
      cell("Restarts", uptime.restarts) +
      cell("State", uptime.halted ? "halted" : uptime.paused ? "paused" : "running", uptime.halted ? "down" : "up") +
      cell("Maintenance", uptime.in_maintenance ? "yes" : "no");
    $("upSub").textContent = uptime.halted
      ? "Halted: " + uptime.halt_reason
      : "Watchdog, daily reset and maintenance window";

    $("upForm").innerHTML = UP_FIELDS.map(([key, label, kind]) => {
      const v = cfg[key];
      if (kind === "bool") {
        return `<label class="flag"><input type="checkbox" data-up="${key}" ${v ? "checked" : ""} /> ${label}</label>`;
      }
      return `<label>${label}<input type="number" data-up="${key}" value="${v}" /></label>`;
    }).join("");

    $("upEvents").innerHTML = (uptime.events || []).length
      ? uptime.events
          .map((e) => `<tr><td class="muted">${ago(e.ts)}</td><td>${esc(e.kind)}</td><td class="muted">${esc(e.detail)}</td></tr>`)
          .join("")
      : `<tr><td colspan="3" class="hint">No supervisor events yet.</td></tr>`;
  }

  async function saveUptime() {
    const patch = {};
    $("upForm").querySelectorAll("[data-up]").forEach((el) => {
      patch[el.dataset.up] = el.type === "checkbox" ? el.checked : parseFloat(el.value);
    });
    await send("/api/uptime", { patch });
    say("Runtime settings saved");
    loadUptime();
  }

  // ----------------------------------------------------------------- boot
  function active() {
    const v = document.querySelector('.view[data-view="bot"]');
    return v && v.classList.contains("on");
  }

  async function loadAll() {
    try {
      await Promise.all([loadSelection(), loadEdge(), loadKeys(), loadUptime()]);
    } catch (err) {
      console.warn("bot control", err);
    }
  }

  function boot() {
    if (!$("selBody")) return;

    $("selModes").querySelectorAll(".tab").forEach((t) => {
      t.onclick = async () => {
        await send("/api/trading/selection", { mode: t.dataset.mode });
        loadSelection();
      };
    });
    $("selAll").onclick = async () => {
      await send("/api/trading/select_all?on=true");
      loadSelection();
    };
    $("selNone").onclick = async () => {
      await send("/api/trading/select_all?on=false");
      loadSelection();
    };
    ["selTopN", "selMetric", "selMinVol"].forEach((id) => {
      $(id).onchange = async () => {
        await send("/api/trading/selection", {
          mode: "auto",
          auto_top_n: parseInt($("selTopN").value, 10) || 5,
          auto_metric: $("selMetric").value,
          auto_min_volume: parseFloat($("selMinVol").value) || 0,
        });
        loadSelection();
      };
    });

    $("edgeSave").onclick = saveEdge;
    $("edgeReset").onclick = async () => {
      await send("/api/edge/reset");
      say("Edge settings back to defaults");
      loadEdge();
    };
    $("edgeRefresh").onclick = loadEdge;

    $("keySave").onclick = async () => {
      const venue = $("keyVenue").value;
      const res = await send("/api/keys", {
        venue,
        key: $("keyKey").value.trim(),
        secret: $("keySecret").value.trim(),
        passphrase: $("keyPass").value.trim(),
      });
      if (res.ok) {
        $("keyKey").value = $("keySecret").value = $("keyPass").value = "";
        say(venue + " credentials saved — run a test");
      } else {
        say(res.error || "could not save credentials");
      }
      loadKeys();
    };
    $("liveArm").onclick = async () => {
      const res = await send("/api/live/arm", {
        confirm: $("liveConfirm").value,
        venue: $("liveVenue").value,
        max_notional: parseFloat($("liveMax").value) || 50,
      });
      say(res.armed ? "LIVE trading armed" : res.error || "could not arm");
      $("liveConfirm").value = "";
      loadKeys();
    };
    $("liveDisarm").onclick = async () => {
      await send("/api/live/disarm");
      say("Back to paper trading");
      loadKeys();
    };

    $("upSave").onclick = saveUptime;
    $("upCheck").onclick = async () => {
      const res = await send("/api/uptime/check");
      say("Supervisor check: " + (Object.keys(res.did || {}).length ? JSON.stringify(res.did) : "all healthy"));
      loadUptime();
    };

    document.querySelectorAll(".nav").forEach((btn) => {
      const prev = btn.onclick;
      btn.onclick = (e) => {
        if (prev) prev(e);
        if (btn.dataset.view === "bot") loadAll();
      };
    });
    if (timer) clearInterval(timer);
    timer = setInterval(() => {
      if (active()) {
        loadSelection();
        loadUptime();
      }
    }, 15000);
  }

  window.BotControl = { loadAll, boot };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
