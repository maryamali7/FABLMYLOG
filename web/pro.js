/* FablMyLog Pro — screener queries, visual strategy builder, backtest lab,
   alert rules, analytics and the risk console. Loaded after app.js. */

const P = (id) => document.getElementById(id);
const jget = async (u) => (await fetch(u)).json();
const jpost = async (u, b) =>
  (await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) })).json();
const jdel = async (u) => (await fetch(u, { method: "DELETE" })).json();

let catalog = { fields: [], comparators: [], templates: [], alert_templates: [], presets: [] };
let scnMode = "boards";
let scnBoardKey = "alpha";
let scnFilters = [];
let scnRows = [];
let scnPreset = "";
let bldSpec = blankSpec();
let bldEntry = [];
let bldExit = [];
let alSpec = blankAlert();
let alConds = [];
let alRules = [];
let btResult = null;
let anData = null;

const SCN_COLS = [
  { k: "symbol", l: "Symbol", t: "text" },
  { k: "last", l: "Last", t: "price" },
  { k: "change_pct", l: "Chg", t: "pct" },
  { k: "alpha", l: "Alpha", t: "n1" },
  { k: "quality", l: "Quality", t: "n1" },
  { k: "grade", l: "Grade", t: "grade" },
  { k: "rsi", l: "RSI", t: "n1" },
  { k: "adx", l: "ADX", t: "n1" },
  { k: "trend_score", l: "Trend", t: "n1" },
  { k: "mom_score", l: "Mom", t: "n1" },
  { k: "vol_ratio", l: "Vol×", t: "n2" },
  { k: "atr_pct", l: "ATR%", t: "n2" },
  { k: "risk_score", l: "Risk", t: "n1" },
  { k: "signal_count", l: "Conf", t: "n0" },
  { k: "rs_btc", l: "vs BTC", t: "signed" },
  { k: "mtf_score", l: "MTF", t: "signed" },
  { k: "prob_up", l: "P(up)", t: "n1" },
  { k: "bias", l: "Bias", t: "bias" },
];

function nf(v, d = 2) {
  return window.FML ? window.FML.fmt(v, d) : Number(v || 0).toFixed(d);
}
function say(msg) {
  if (window.FML) window.FML.toast(msg);
}
function cell(row, col) {
  const v = row[col.k];
  switch (col.t) {
    case "pct": {
      const n = Number(v || 0);
      return `<span class="${n >= 0 ? "up" : "down"}">${(n >= 0 ? "+" : "") + n.toFixed(2)}%</span>`;
    }
    case "signed": {
      const n = Number(v || 0);
      return `<span class="${n >= 0 ? "up" : "down"}">${n.toFixed(2)}</span>`;
    }
    case "price":
      return nf(v);
    case "n0":
      return Number(v || 0).toFixed(0);
    case "n1":
      return Number(v || 0).toFixed(1);
    case "n2":
      return Number(v || 0).toFixed(2);
    case "grade":
      return `<span class="grade g${v || "D"}">${v || "—"}</span>`;
    case "bias":
      return `<span class="bias-${v}">${v || "—"}</span>`;
    default:
      return v == null ? "—" : String(v);
  }
}

/* ------------------------------------------------------------------ */
/* condition rows (shared by builder, screener and alerts)             */
/* ------------------------------------------------------------------ */

function fieldOptions(selected) {
  return catalog.fields
    .map(
      (g) =>
        `<optgroup label="${g.group}">` +
        g.fields.map((f) => `<option value="${f.key}" ${f.key === selected ? "selected" : ""}>${f.label}</option>`).join("") +
        `</optgroup>`
    )
    .join("");
}

function cmpOptions(selected) {
  return catalog.comparators
    .map((c) => `<option value="${c.op}" ${c.op === selected ? "selected" : ""}>${c.label}</option>`)
    .join("");
}

function condRow(cond, i, ns) {
  const op = cond.cmp || ">";
  const arity = (catalog.comparators.find((c) => c.op === op) || {}).arity ?? 2;
  let valueHtml = "";
  if (arity === 3) {
    const b = Array.isArray(cond.right) ? cond.right : [0, 0];
    valueHtml = `<input class="mini val" data-ns="${ns}" data-i="${i}" data-part="lo" value="${b[0]}" />
                 <input class="mini val" data-ns="${ns}" data-i="${i}" data-part="hi" value="${b[1]}" />`;
  } else if (arity === 2) {
    valueHtml = `<input class="mini val" data-ns="${ns}" data-i="${i}" data-part="v" value="${cond.right ?? ""}" placeholder="value or field" />`;
  } else {
    valueHtml = `<span class="mini ghost">—</span>`;
  }
  return `<div class="cond">
    <select class="mini fld-sel" data-ns="${ns}" data-i="${i}">${fieldOptions(cond.left)}</select>
    <select class="mini cmp-sel" data-ns="${ns}" data-i="${i}">${cmpOptions(op)}</select>
    ${valueHtml}
    <button class="xbtn" data-del="${ns}" data-i="${i}">✕</button>
  </div>`;
}

function renderConds(el, list, ns) {
  if (!el) return;
  el.innerHTML = list.length
    ? list.map((c, i) => condRow(c, i, ns)).join("")
    : `<div class="muted">No conditions yet — click “+ Condition”.</div>`;
  el.querySelectorAll(".fld-sel").forEach((sel) => {
    sel.onchange = () => {
      list[+sel.dataset.i].left = sel.value;
    };
  });
  el.querySelectorAll(".cmp-sel").forEach((sel) => {
    sel.onchange = () => {
      const c = list[+sel.dataset.i];
      c.cmp = sel.value;
      const arity = (catalog.comparators.find((x) => x.op === sel.value) || {}).arity ?? 2;
      if (arity === 3 && !Array.isArray(c.right)) c.right = [0, 0];
      if (arity === 2 && Array.isArray(c.right)) c.right = c.right[0];
      renderConds(el, list, ns);
    };
  });
  el.querySelectorAll(".val").forEach((inp) => {
    inp.oninput = () => {
      const c = list[+inp.dataset.i];
      const raw = inp.value.trim();
      const num = raw !== "" && !Number.isNaN(Number(raw)) ? Number(raw) : raw;
      if (inp.dataset.part === "v") c.right = num;
      else {
        if (!Array.isArray(c.right)) c.right = [0, 0];
        c.right[inp.dataset.part === "lo" ? 0 : 1] = num;
      }
    };
  });
  el.querySelectorAll("[data-del]").forEach((btn) => {
    btn.onclick = () => {
      list.splice(+btn.dataset.i, 1);
      renderConds(el, list, ns);
    };
  });
}

function flatten(group) {
  if (!group) return [];
  const rules = Array.isArray(group) ? group : group.rules || [];
  const out = [];
  rules.forEach((r) => {
    if (r && r.rules) out.push(...flatten(r));
    else if (r && (r.left || r.field)) out.push({ left: r.left || r.field, cmp: r.cmp || r.op || ">", right: r.right ?? r.value });
  });
  return out;
}

/* ------------------------------------------------------------------ */
/* SCREENER PRO                                                        */
/* ------------------------------------------------------------------ */

function renderSummary(sum) {
  const el = P("scnSummary");
  if (!el || !sum || !sum.n) return;
  const item = (l, v, cls = "") => `<div class="bx"><span>${l}</span><b class="${cls}">${v}</b></div>`;
  el.innerHTML = [
    item("Scanned", sum.n),
    item("Breadth", `${sum.breadth_pct}%`, sum.breadth_pct >= 50 ? "up" : "down"),
    item("Advancers", sum.advancers, "up"),
    item("Decliners", sum.decliners, "down"),
    item("Avg alpha", sum.avg_alpha),
    item("Avg chg", `${sum.avg_change}%`, sum.avg_change >= 0 ? "up" : "down"),
    item("Squeezes", sum.squeezes),
    item("Breakouts", sum.breakouts),
    item("Grade A", sum.grade_a, "up"),
    item("Avg risk", sum.avg_risk),
  ].join("");
}

function renderScreenerTable(rows) {
  scnRows = rows || [];
  const head = P("screenerHead");
  if (head) head.innerHTML = SCN_COLS.map((c) => `<th data-sort="${c.k}">${c.l}</th>`).join("") + "<th></th>";
  const body = P("screenerBody");
  if (!body) return;
  if (!scnRows.length) {
    body.innerHTML = `<tr><td colspan="${SCN_COLS.length + 1}" class="muted">Nothing matches yet — loosen the filters or wait for candles.</td></tr>`;
    return;
  }
  body.innerHTML = scnRows
    .map(
      (r) =>
        `<tr data-sym="${r.symbol}">` +
        SCN_COLS.map((c) => `<td>${cell(r, c)}</td>`).join("") +
        `<td><button class="btn tiny" data-watch="${r.symbol}">Watch</button></td></tr>`
    )
    .join("");
  body.querySelectorAll("tr").forEach((tr) => {
    tr.onclick = (e) => {
      if (e.target.dataset.watch) return;
      window.FML.select(tr.dataset.sym);
      window.FML.showView("overview");
    };
  });
  body.querySelectorAll("[data-watch]").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      jpost("/api/screener/watch", { symbol: b.dataset.watch }).then(() => say("Watching " + b.dataset.watch));
    };
  });
  if (head)
    head.querySelectorAll("th[data-sort]").forEach((th) => {
      th.onclick = () => {
        const sel = P("scnSort");
        if (!sel) return;
        sel.value = th.dataset.sort;
        if (scnMode === "query") runScreen();
        else {
          const key = th.dataset.sort;
          renderScreenerTable([...scnRows].sort((a, b) => (Number(b[key]) || 0) - (Number(a[key]) || 0)));
        }
      };
    });
}

function renderBoards() {
  const sc = window.FML ? window.FML.screener : {};
  const meta = (sc && sc.meta) || {};
  const boards = (sc && sc.boards) || {};
  const keys = Object.keys(meta).length ? Object.keys(meta) : Object.keys(boards);
  const tabs = P("screenerTabs");
  if (tabs) {
    tabs.innerHTML = keys
      .map((k) => {
        const n = (boards[k] || []).length;
        return `<button class="tab ${k === scnBoardKey ? "on" : ""}" data-board="${k}">${(meta[k] || {}).title || k}<span class="cnt">${n}</span></button>`;
      })
      .join("");
    tabs.querySelectorAll(".tab").forEach((b) => {
      b.onclick = () => {
        scnBoardKey = b.dataset.board;
        const m = meta[scnBoardKey] || {};
        P("screenerMeta").textContent = m.blurb || scnBoardKey;
        renderBoards();
      };
    });
  }
  renderScreenerTable(boards[scnBoardKey] || []);
  renderHeatPro(sc);
}

function renderHeatPro(sc) {
  const el = P("heat");
  if (!el) return;
  el.innerHTML = ((sc && sc.heatmap) || [])
    .map((h) => {
      const chg = Number(h.change_pct || 0);
      const t = Math.max(-8, Math.min(8, chg));
      const bg = t >= 0 ? `rgba(61,255,154,${0.12 + t / 18})` : `rgba(255,93,115,${0.12 + Math.abs(t) / 18})`;
      return `<div class="cell" data-sym="${h.symbol}" style="background:${bg}">
        <div><b>${String(h.symbol).replace("/USDT", "")}</b><span class="grade g${h.grade || "D"}">${h.grade || ""}</span></div>
        <div class="${chg >= 0 ? "up" : "down"}">${(chg >= 0 ? "+" : "") + chg.toFixed(2)}%</div>
      </div>`;
    })
    .join("");
  el.querySelectorAll(".cell").forEach((c) => {
    c.onclick = () => {
      window.FML.select(c.dataset.sym);
      window.FML.showView("overview");
    };
  });
}

function renderScreenerPro() {
  const sc = window.FML ? window.FML.screener : {};
  renderSummary((sc && sc.summary) || {});
  if (scnMode === "boards") renderBoards();
  else renderHeatPro(sc);
}

async function runScreen() {
  const body = {
    filters: scnFilters.filter((c) => c.left),
    sort_by: P("scnSort").value || "alpha",
    sort_dir: P("scnDir").value,
    limit: Number(P("scnLimit").value || 50),
    search: P("scnSearch").value,
    preset: scnPreset || null,
    match: P("scnMatch").value,
  };
  const res = await jpost("/api/screener/query", body);
  renderScreenerTable(res.rows || []);
  renderSummary(res.summary || {});
  P("scnStatus").textContent = `${res.returned}/${res.total} matches out of ${res.scanned} scanned · ${res.elapsed_ms} ms${
    (res.errors || []).length ? " · " + res.errors[0] : ""
  }`;
}

function setScnMode(mode) {
  scnMode = mode;
  P("scnBoardsWrap").style.display = mode === "boards" ? "" : "none";
  P("scnQueryWrap").style.display = mode === "query" ? "" : "none";
  P("scnModeBoards").classList.toggle("primary", mode === "boards");
  P("scnModeQuery").classList.toggle("primary", mode === "query");
  if (mode === "query") runScreen();
  else renderBoards();
}

function initScreenerPro() {
  const sort = P("scnSort");
  if (sort)
    sort.innerHTML = SCN_COLS.filter((c) => c.k !== "symbol")
      .map((c) => `<option value="${c.k}">Sort: ${c.l}</option>`)
      .join("") + `<option value="volume">Sort: Volume</option><option value="liquidity">Sort: Liquidity</option>`;
  const presets = P("scnPresets");
  if (presets) {
    presets.innerHTML = (catalog.presets || [])
      .map((p) => `<button class="tab" data-preset="${p.id}" title="${p.blurb}">${p.title}</button>`)
      .join("");
    presets.querySelectorAll("[data-preset]").forEach((b) => {
      b.onclick = () => {
        scnPreset = scnPreset === b.dataset.preset ? "" : b.dataset.preset;
        presets.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.preset === scnPreset));
        if (scnPreset) {
          scnFilters = [];
          renderConds(P("scnFilters"), scnFilters, "scn");
        }
        runScreen();
      };
    });
  }
  P("scnAddFilter").onclick = () => {
    scnPreset = "";
    P("scnPresets").querySelectorAll(".tab").forEach((t) => t.classList.remove("on"));
    scnFilters.push({ left: "alpha", cmp: ">", right: 60 });
    renderConds(P("scnFilters"), scnFilters, "scn");
  };
  P("scnClear").onclick = () => {
    scnFilters = [];
    scnPreset = "";
    P("scnSearch").value = "";
    P("scnPresets").querySelectorAll(".tab").forEach((t) => t.classList.remove("on"));
    renderConds(P("scnFilters"), scnFilters, "scn");
    runScreen();
  };
  P("scnRun").onclick = runScreen;
  P("scnSearch").onkeydown = (e) => {
    if (e.key === "Enter") runScreen();
  };
  P("scnModeBoards").onclick = () => setScnMode("boards");
  P("scnModeQuery").onclick = () => setScnMode("query");
  P("scnExport").onclick = () => {
    const url = scnMode === "boards" ? `/api/screener/export.csv?board=${scnBoardKey}` : `/api/screener/export.csv?preset=${scnPreset}`;
    window.open(url, "_blank");
  };
  renderConds(P("scnFilters"), scnFilters, "scn");
}

/* ------------------------------------------------------------------ */
/* STRATEGY BUILDER                                                    */
/* ------------------------------------------------------------------ */

function blankSpec() {
  return {
    id: "",
    name: "",
    description: "",
    side: "long",
    confidence: 0.66,
    weight: 1,
    cooldown_sec: 180,
    stop_loss_pct: null,
    take_profit_pct: null,
    trail_pct: null,
    symbols: [],
    tags: [],
    enabled: true,
  };
}

function specFromForm() {
  const pctVal = (id) => {
    const v = Number(P(id).value);
    return P(id).value === "" || Number.isNaN(v) ? null : v / 100;
  };
  return {
    ...bldSpec,
    name: P("bldName").value.trim(),
    description: P("bldDesc").value.trim(),
    tags: P("bldTags").value.split(",").map((t) => t.trim()).filter(Boolean),
    side: P("bldSide").value,
    confidence: Number(P("bldConf").value) || 0.66,
    weight: Number(P("bldWeight").value) || 1,
    cooldown_sec: Number(P("bldCooldown").value) || 0,
    stop_loss_pct: pctVal("bldStop"),
    take_profit_pct: pctVal("bldTake"),
    trail_pct: pctVal("bldTrail"),
    symbols: P("bldSymbols").value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean),
    entry: { op: P("bldEntryOp").value, rules: bldEntry.filter((c) => c.left) },
    exit: bldExit.length ? { op: P("bldExitOp").value, rules: bldExit.filter((c) => c.left) } : null,
  };
}

function loadSpec(spec) {
  bldSpec = { ...blankSpec(), ...(spec || {}) };
  P("bldId").textContent = bldSpec.id || "unsaved";
  P("bldTitle").textContent = bldSpec.name ? `Editing · ${bldSpec.name}` : "Strategy editor";
  P("bldName").value = bldSpec.name || "";
  P("bldDesc").value = bldSpec.description || "";
  P("bldTags").value = (bldSpec.tags || []).join(", ");
  P("bldSide").value = bldSpec.side || "long";
  P("bldConf").value = bldSpec.confidence ?? 0.66;
  P("bldWeight").value = bldSpec.weight ?? 1;
  P("bldCooldown").value = bldSpec.cooldown_sec ?? 180;
  P("bldStop").value = bldSpec.stop_loss_pct ? (bldSpec.stop_loss_pct * 100).toFixed(2) : "";
  P("bldTake").value = bldSpec.take_profit_pct ? (bldSpec.take_profit_pct * 100).toFixed(2) : "";
  P("bldTrail").value = bldSpec.trail_pct ? (bldSpec.trail_pct * 100).toFixed(2) : "";
  P("bldSymbols").value = (bldSpec.symbols || []).join(", ");
  P("bldEntryOp").value = (bldSpec.entry && bldSpec.entry.op) || "all";
  P("bldExitOp").value = (bldSpec.exit && bldSpec.exit.op) || "any";
  bldEntry = flatten(bldSpec.entry);
  bldExit = flatten(bldSpec.exit);
  renderConds(P("bldEntry"), bldEntry, "entry");
  renderConds(P("bldExit"), bldExit, "exit");
  P("bldFeedback").innerHTML = "";
}

function renderCustomList(rows) {
  const el = P("bldList");
  if (!el) return;
  if (!rows || !rows.length) {
    el.innerHTML = `<div class="muted">No custom strategies yet. Pick a template or hit “+ New”.</div>`;
    return;
  }
  el.innerHTML = rows
    .map(
      (s) => `<div class="row mine ${s.enabled ? "" : "dim"}">
      <div>
        <b>${s.name}</b>
        <div class="muted">${s.side} · ${s.conditions || 0} rules · ${s.fires || 0} signals</div>
      </div>
      <div class="row-btns">
        <button class="btn tiny" data-edit="${s.id}">Edit</button>
        <button class="btn tiny" data-toggle="${s.id}">${s.enabled ? "Disarm" : "Arm"}</button>
        <button class="btn tiny" data-dup="${s.id}">Copy</button>
      </div>
    </div>`
    )
    .join("");
  el.querySelectorAll("[data-edit]").forEach((b) => {
    b.onclick = () => {
      const spec = rows.find((r) => r.id === b.dataset.edit);
      loadSpec(spec);
    };
  });
  el.querySelectorAll("[data-toggle]").forEach((b) => {
    b.onclick = async () => {
      const r = await jpost(`/api/strategies/custom/${b.dataset.toggle}/toggle`);
      say(r.enabled ? "Armed" : "Disarmed");
      refreshCustom();
    };
  });
  el.querySelectorAll("[data-dup]").forEach((b) => {
    b.onclick = async () => {
      await jpost(`/api/strategies/custom/${b.dataset.dup}/duplicate`);
      refreshCustom();
    };
  });
}

async function refreshCustom() {
  const r = await jget("/api/strategies/custom");
  renderCustomList(r.strategies || []);
}

function feedback(el, ok, title, lines) {
  el.innerHTML = `<div class="fb ${ok ? "good" : "bad"}"><b>${title}</b>${
    (lines || []).length ? "<ul>" + lines.map((l) => `<li>${l}</li>`).join("") + "</ul>" : ""
  }</div>`;
}

function initBuilder() {
  const tpl = P("bldTemplate");
  if (tpl)
    tpl.innerHTML =
      `<option value="">Start from a template…</option>` +
      (catalog.templates || []).map((t) => `<option value="${t.template_id}">${t.name}</option>`).join("");
  tpl.onchange = async () => {
    if (!tpl.value) return;
    const r = await jget(`/api/strategies/templates?template_id=${tpl.value}`);
    if (r.spec) {
      r.spec.id = "";
      loadSpec(r.spec);
      say("Template loaded — tweak and save");
    }
    tpl.value = "";
  };
  P("bldNew").onclick = () => loadSpec(blankSpec());
  document.querySelectorAll("[data-add]").forEach((btn) => {
    btn.onclick = () => {
      const ns = btn.dataset.add;
      if (ns === "entry") {
        bldEntry.push({ left: "rsi", cmp: "<", right: 35 });
        renderConds(P("bldEntry"), bldEntry, "entry");
      } else if (ns === "exit") {
        bldExit.push({ left: "rsi", cmp: ">", right: 70 });
        renderConds(P("bldExit"), bldExit, "exit");
      } else if (ns === "alert") {
        alConds.push({ left: "alpha", cmp: ">", right: 70 });
        renderConds(P("alRules"), alConds, "alert");
      }
    };
  });
  P("bldPreview").onclick = async () => {
    const res = await jpost("/api/strategies/custom/validate", { spec: specFromForm() });
    if (!res.ok) return feedback(P("bldFeedback"), false, "Fix these first:", res.errors);
    const lines = (res.matches || []).map(
      (m) => `${m.symbol} → ${m.kind.toUpperCase()} @ ${nf(m.price)} (${(m.confidence * 100).toFixed(0)}%)`
    );
    feedback(
      P("bldFeedback"),
      true,
      `Valid · ${res.matches.length} live match${res.matches.length === 1 ? "" : "es"} across ${res.checked} symbols`,
      lines.length ? lines : (res.sample_trace || []).map((t) => `sample: ${t}`)
    );
  };
  P("bldSave").onclick = async () => {
    const res = await jpost("/api/strategies/custom", { spec: specFromForm() });
    if (!res.ok) return feedback(P("bldFeedback"), false, "Could not save:", res.errors);
    bldSpec.id = res.strategy.id;
    P("bldId").textContent = res.strategy.id;
    feedback(P("bldFeedback"), true, `Saved & armed — “${res.strategy.name}” now trades in the ensemble.`, []);
    say("Strategy saved");
    refreshCustom();
  };
  P("bldDelete").onclick = async () => {
    if (!bldSpec.id) return say("Nothing saved yet");
    await jdel(`/api/strategies/custom/${bldSpec.id}`);
    loadSpec(blankSpec());
    refreshCustom();
    say("Deleted");
  };
  P("bldBacktest").onclick = () => runBacktest();
  P("btRun").onclick = () => runBacktest();
  P("btBasketRun").onclick = () => runBacktest(true);
  loadSpec(blankSpec());
}

/* ------------------------------------------------------------------ */
/* BACKTEST LAB                                                        */
/* ------------------------------------------------------------------ */

function metricGrid(el, pairs) {
  el.innerHTML = pairs.map(([l, v, cls]) => `<div class="m"><span>${l}</span><b class="${cls || ""}">${v}</b></div>`).join("");
}

async function runBacktest(basket = false) {
  const spec = specFromForm();
  if (!spec.entry.rules.length) return say("Add entry conditions first");
  const cfg = {
    position_pct: Number(P("btSize").value || 25) / 100,
    fee_bps: Number(P("btFees").value || 10),
  };
  P("btMetrics").innerHTML = `<div class="muted">Running…</div>`;
  const body = {
    spec,
    symbol: P("btSymbol").value.trim().toUpperCase() || "BTC/USDT",
    bars: Number(P("btBars").value || 600),
    config: cfg,
  };
  if (basket) {
    const list = P("btBasket").value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
    body.symbols = [body.symbol, ...list];
  }
  const res = await jpost("/api/backtest", body);
  if (!res.ok) {
    P("btMetrics").innerHTML = `<div class="fb bad">${res.error || "backtest failed"}</div>`;
    return;
  }
  if (res.symbols) {
    const t = res.totals;
    metricGrid(P("btMetrics"), [
      ["Symbols", t.symbols],
      ["Trades", t.trades],
      ["Win rate", t.win_rate + "%", t.win_rate >= 50 ? "up" : "down"],
      ["Net PnL", "$" + t.net_pnl, t.net_pnl >= 0 ? "up" : "down"],
      ["Avg return", t.avg_return_pct + "%", t.avg_return_pct >= 0 ? "up" : "down"],
      ["Best", t.best],
    ]);
    P("btTrades").innerHTML = res.symbols
      .map(
        (r) =>
          `<div class="row"><span>${r.symbol}</span><span>${r.trades} trades</span><span class="${
            r.return_pct >= 0 ? "up" : "down"
          }">${r.return_pct}%</span><span class="muted">PF ${r.profit_factor}</span></div>`
      )
      .join("");
    drawBtChart(null);
    return;
  }
  btResult = res;
  const m = res.metrics;
  metricGrid(P("btMetrics"), [
    ["Grade", m.grade, m.grade.startsWith("A") ? "up" : m.grade === "D" ? "down" : ""],
    ["Return", m.return_pct + "%", m.return_pct >= 0 ? "up" : "down"],
    ["Buy & hold", res.buy_hold_pct + "%", res.buy_hold_pct >= 0 ? "up" : "down"],
    ["Trades", m.trades],
    ["Win rate", m.win_rate + "%", m.win_rate >= 50 ? "up" : "down"],
    ["Profit factor", m.profit_factor, m.profit_factor >= 1 ? "up" : "down"],
    ["Expectancy", "$" + m.expectancy],
    ["Max DD", m.max_drawdown_pct + "%", "down"],
    ["Sharpe", m.sharpe],
    ["Exposure", m.exposure_pct + "%"],
    ["Avg hold", m.avg_bars_held + " bars"],
    ["Final", "$" + m.final_equity],
  ]);
  P("btTrades").innerHTML = (res.trades || [])
    .slice(-25)
    .reverse()
    .map(
      (t) =>
        `<div class="row"><span class="${t.side === "long" ? "up" : "down"}">${t.side}</span><span>${nf(t.entry)} → ${nf(
          t.exit
        )}</span><span class="${t.pnl >= 0 ? "up" : "down"}">${t.pnl_pct}%</span><span class="muted">${t.reason}</span></div>`
    )
    .join("");
  drawBtChart(res);
}

function drawLine(canvas, series, color, fillColor) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 500;
  const h = canvas.height / dpr || 150;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0a131c";
  ctx.fillRect(0, 0, w, h);
  if (!series || series.length < 2) {
    ctx.fillStyle = "#8fa0b3";
    ctx.font = "12px DM Sans, sans-serif";
    ctx.fillText("No curve yet", 14, 24);
    return;
  }
  const min = Math.min(...series);
  const max = Math.max(...series);
  const pad = (max - min) * 0.1 || 1;
  const y = (v) => h - 10 - ((v - min + pad) / (max - min + 2 * pad)) * (h - 20);
  const x = (i) => (i / (series.length - 1)) * (w - 8) + 4;
  ctx.beginPath();
  series.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.8;
  ctx.stroke();
  ctx.lineTo(x(series.length - 1), h);
  ctx.lineTo(x(0), h);
  ctx.closePath();
  ctx.fillStyle = fillColor;
  ctx.fill();
  ctx.fillStyle = "#8fa0b3";
  ctx.font = "11px JetBrains Mono, monospace";
  ctx.fillText(nf(max), 8, 14);
  ctx.fillText(nf(min), 8, h - 6);
}

function drawBtChart(res) {
  const up = res && res.metrics && res.metrics.return_pct >= 0;
  drawLine(
    P("btChart"),
    res ? res.equity_curve : [],
    up ? "#3dff9a" : "#ff5d73",
    up ? "rgba(61,255,154,0.12)" : "rgba(255,93,115,0.12)"
  );
}

/* ------------------------------------------------------------------ */
/* ALERT RULES                                                         */
/* ------------------------------------------------------------------ */

function blankAlert() {
  return {
    id: "",
    name: "",
    severity: "info",
    message: "",
    cooldown_sec: 300,
    symbols: [],
    auto_watch: false,
    webhook: "",
    enabled: true,
  };
}

function alertFromForm() {
  return {
    ...alSpec,
    name: P("alName").value.trim(),
    severity: P("alSeverity").value,
    message: P("alMessage").value.trim(),
    cooldown_sec: Number(P("alCooldown").value || 300),
    symbols: P("alSymbols").value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean),
    auto_watch: P("alAutoWatch").value === "yes",
    webhook: P("alWebhook").value.trim(),
    rule: { op: P("alOp").value, rules: alConds.filter((c) => c.left) },
  };
}

function loadAlert(spec) {
  alSpec = { ...blankAlert(), ...(spec || {}) };
  P("alId").textContent = alSpec.id || "unsaved";
  P("alName").value = alSpec.name || "";
  P("alSeverity").value = alSpec.severity || "info";
  P("alMessage").value = alSpec.message || "";
  P("alCooldown").value = alSpec.cooldown_sec || 300;
  P("alSymbols").value = (alSpec.symbols || []).join(", ");
  P("alAutoWatch").value = alSpec.auto_watch ? "yes" : "no";
  P("alWebhook").value = alSpec.webhook || "";
  P("alOp").value = (alSpec.rule && alSpec.rule.op) || "all";
  alConds = flatten(alSpec.rule);
  renderConds(P("alRules"), alConds, "alert");
  P("alFeedback").innerHTML = "";
}

function renderAlertList(rows) {
  alRules = rows || [];
  const el = P("alList");
  if (!el) return;
  el.innerHTML = alRules.length
    ? alRules
        .map(
          (r) => `<div class="row ${r.enabled ? "" : "dim"}">
        <div><b>${r.name}</b><div class="muted sev-${r.severity}">${r.severity} · ${r.hits || 0} hits</div></div>
        <div class="row-btns">
          <button class="btn tiny" data-aedit="${r.id}">Edit</button>
          <button class="btn tiny" data-atoggle="${r.id}">${r.enabled ? "Mute" : "Arm"}</button>
        </div>
      </div>`
        )
        .join("")
    : `<div class="muted">No alert rules yet.</div>`;
  el.querySelectorAll("[data-aedit]").forEach((b) => {
    b.onclick = () => loadAlert(alRules.find((r) => r.id === b.dataset.aedit));
  });
  el.querySelectorAll("[data-atoggle]").forEach((b) => {
    b.onclick = async () => {
      await jpost(`/api/alerts/rules/${b.dataset.atoggle}/toggle`);
      refreshAlerts();
    };
  });
}

function renderRuleFeed(rows) {
  const el = P("alHistory");
  if (!el) return;
  el.innerHTML = (rows || []).length
    ? rows
        .map(
          (a) =>
            `<div class="row"><span class="muted">${window.FML.ago(a.ts)}</span><span class="sev-${a.severity}">${a.symbol}</span><span>${a.text}</span></div>`
        )
        .join("")
    : `<div class="muted">Nothing triggered yet.</div>`;
}

async function refreshAlerts() {
  const r = await jget("/api/alerts/rules");
  renderAlertList(r.rules || []);
  const h = await jget("/api/alerts/history");
  renderRuleFeed(h.alerts || []);
}

function initAlerts() {
  const tpl = P("alTemplate");
  if (tpl) {
    tpl.innerHTML =
      `<option value="">Start from a template…</option>` +
      (catalog.alert_templates || []).map((t, i) => `<option value="${i}">${t.name}</option>`).join("");
    tpl.onchange = () => {
      if (tpl.value === "") return;
      loadAlert({ ...catalog.alert_templates[Number(tpl.value)], id: "" });
      tpl.value = "";
    };
  }
  P("alNew").onclick = () => loadAlert(blankAlert());
  P("alSave").onclick = async () => {
    const res = await jpost("/api/alerts/rules", { spec: alertFromForm() });
    if (!res.ok) return feedback(P("alFeedback"), false, "Could not save:", res.errors);
    alSpec.id = res.rule.id;
    P("alId").textContent = res.rule.id;
    feedback(P("alFeedback"), true, "Alert armed — it now watches every scan.", []);
    refreshAlerts();
  };
  P("alDelete").onclick = async () => {
    if (!alSpec.id) return say("Nothing saved yet");
    await jdel(`/api/alerts/rules/${alSpec.id}`);
    loadAlert(blankAlert());
    refreshAlerts();
  };
  loadAlert(blankAlert());
}

/* ------------------------------------------------------------------ */
/* ANALYTICS + RISK                                                    */
/* ------------------------------------------------------------------ */

function tableRows(el, rows, cols) {
  if (!el) return;
  el.innerHTML = (rows || []).length
    ? rows.map((r) => `<tr>${cols.map((c) => `<td class="${c.cls ? c.cls(r) : ""}">${c.get(r)}</td>`).join("")}</tr>`).join("")
    : `<tr><td colspan="${cols.length}" class="muted">No closed trades yet.</td></tr>`;
}

async function refreshAnalytics() {
  anData = await jget("/api/analytics");
  const o = anData.overall || {};
  const e = anData.equity || {};
  metricGrid(P("anOverall"), [
    ["Trades", o.trades || 0],
    ["Win rate", (o.win_rate || 0) + "%", (o.win_rate || 0) >= 50 ? "up" : "down"],
    ["Net PnL", "$" + (o.net || 0), (o.net || 0) >= 0 ? "up" : "down"],
    ["Profit factor", o.profit_factor || 0, (o.profit_factor || 0) >= 1 ? "up" : "down"],
    ["Expectancy", "$" + (o.expectancy || 0)],
    ["Best / worst", `${o.best || 0} / ${o.worst || 0}`],
    ["Win streak", o.longest_win_streak || 0, "up"],
    ["Loss streak", o.longest_loss_streak || 0, "down"],
    ["Equity", "$" + (e.end_equity || 0)],
    ["Max DD", (e.max_drawdown_pct || 0) + "%", "down"],
    ["Sharpe", e.sharpe || 0],
    ["Fees", "$" + (anData.fees_paid || 0)],
  ]);
  tableRows(P("anStrategy"), anData.by_strategy, [
    { get: (r) => r.strategy },
    { get: (r) => r.trades },
    { get: (r) => r.win_rate + "%", cls: (r) => (r.win_rate >= 50 ? "up" : "down") },
    { get: (r) => r.net, cls: (r) => (r.net >= 0 ? "up" : "down") },
    { get: (r) => r.profit_factor },
    { get: (r) => r.expectancy },
  ]);
  tableRows(P("anSymbol"), anData.by_symbol, [
    { get: (r) => r.symbol },
    { get: (r) => r.trades },
    { get: (r) => r.win_rate + "%", cls: (r) => (r.win_rate >= 50 ? "up" : "down") },
    { get: (r) => r.net, cls: (r) => (r.net >= 0 ? "up" : "down") },
    { get: (r) => r.best },
    { get: (r) => r.worst },
  ]);
  tableRows(P("anReason"), anData.by_reason, [
    { get: (r) => r.reason },
    { get: (r) => r.trades },
    { get: (r) => r.win_rate + "%", cls: (r) => (r.win_rate >= 50 ? "up" : "down") },
    { get: (r) => r.net, cls: (r) => (r.net >= 0 ? "up" : "down") },
  ]);
  const hist = anData.pnl_histogram || [];
  const maxc = Math.max(1, ...hist.map((h) => h.count));
  P("anHist").innerHTML = hist.length
    ? hist
        .map(
          (h) =>
            `<div class="bar"><div class="fill ${h.to <= 0 ? "neg" : "pos"}" style="height:${(h.count / maxc) * 100}%"></div><span>${h.from}</span></div>`
        )
        .join("")
    : `<div class="muted">No trades to distribute yet.</div>`;
  try {
    const eq = await jget("/api/equity");
    drawLine(P("anChart"), (eq || []).map((r) => r.equity), "#2ee6c8", "rgba(46,230,200,0.12)");
  } catch (err) {
    /* ignore */
  }
}

const RISK_FIELDS = [
  ["max_position_pct", "Max position %", 100],
  ["max_open_positions", "Max open", 1],
  ["max_daily_loss_pct", "Daily loss %", 100],
  ["max_drawdown_pct", "Max drawdown %", 100],
  ["stop_loss_pct", "Stop %", 100],
  ["take_profit_pct", "Target %", 100],
  ["trailing_stop_pct", "Trail %", 100],
  ["min_confidence", "Min confidence", 1],
  ["max_spread_bps", "Max spread bps", 1],
  ["cooldown_after_loss_sec", "Loss cooldown s", 1],
  ["fee_bps", "Fee bps", 1],
  ["slippage_bps", "Slippage bps", 1],
];

function renderRisk(risk) {
  const el = P("riskGrid");
  if (!el || !risk) return;
  if (el.dataset.ready) return;
  el.dataset.ready = "1";
  el.innerHTML = RISK_FIELDS.map(
    ([k, label, mult]) =>
      `<label class="fld">${label}<input data-risk="${k}" data-mult="${mult}" type="number" step="0.01" value="${(
        (risk[k] || 0) * mult
      ).toFixed(mult === 100 ? 2 : 2)}" /></label>`
  ).join("");
}

function initRisk() {
  P("riskSave").onclick = async () => {
    const patch = {};
    document.querySelectorAll("[data-risk]").forEach((inp) => {
      const mult = Number(inp.dataset.mult) || 1;
      const v = Number(inp.value);
      if (!Number.isNaN(v)) patch[inp.dataset.risk] = mult === 100 ? v / 100 : v;
    });
    const res = await jpost("/api/risk", { patch });
    say(`Risk updated (${Object.keys(res.applied || {}).length} fields)`);
  };
  P("riskResume").onclick = async () => {
    await jpost("/api/risk/resume");
    say("Halt cleared");
  };
  P("anRefresh").onclick = refreshAnalytics;
}

/* ------------------------------------------------------------------ */
/* wiring                                                              */
/* ------------------------------------------------------------------ */

let lastCustomSig = "";
let lastAlertSig = "";
let lastFeedSig = "";
function onProState(s) {
  renderRisk(s.risk);
  const sig = JSON.stringify((s.custom_strategies || []).map((c) => [c.id, c.enabled, c.fires]));
  if (sig !== lastCustomSig) {
    lastCustomSig = sig;
    renderCustomList(s.custom_strategies || []);
  }
  const asig = JSON.stringify((s.alert_rules || []).map((r) => [r.id, r.enabled, r.hits]));
  if (asig !== lastAlertSig) {
    lastAlertSig = asig;
    renderAlertList(s.alert_rules || []);
  }
  const fsig = (s.rule_alerts || []).length ? String(s.rule_alerts[0].ts) + s.rule_alerts.length : "";
  if (fsig && fsig !== lastFeedSig) {
    lastFeedSig = fsig;
    renderRuleFeed(s.rule_alerts);
  }
}

async function bootPro() {
  try {
    catalog = await jget("/api/builder/catalog");
  } catch (err) {
    console.warn("catalog failed", err);
    return;
  }
  initScreenerPro();
  initBuilder();
  initAlerts();
  initRisk();
  refreshCustom();
  refreshAlerts();
  refreshAnalytics();
  renderScreenerPro();
  document.querySelectorAll(".nav").forEach((btn) => {
    const prev = btn.onclick;
    btn.onclick = (e) => {
      if (prev) prev(e);
      if (btn.dataset.view === "analytics") refreshAnalytics();
      if (btn.dataset.view === "screeners") renderScreenerPro();
      if (btn.dataset.view === "builder") refreshCustom();
      if (btn.dataset.view === "alerts") refreshAlerts();
    };
  });
  window.addEventListener("resize", () => {
    if (btResult) drawBtChart(btResult);
  });
}

bootPro();
