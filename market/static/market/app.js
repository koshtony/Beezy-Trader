/* Beezy Trader front-end: vanilla JS + HTMX + Plotly (basic bundle). */
(function () {
  "use strict";

  // ------------------------------------------------------------ theme-aware chart colours
  function palette() {
    const css = getComputedStyle(document.documentElement);
    const v = (n) => css.getPropertyValue(n).trim();
    const light = document.documentElement.classList.contains("theme-light");
    return {
      bg: v("--surface"),
      ink: v("--ink"),
      ink2: v("--ink-2"),
      muted: v("--muted"),
      line: v("--line"),
      lineStrong: v("--line-strong"),
      up: v("--up"),
      down: v("--down"),
      // validated categorical slots, stepped per mode (fixed order: blue, orange, aqua)
      s1: light ? "#2a78d6" : "#3987e5",
      s2: light ? "#eb6834" : "#d95926",
      s3: light ? "#1baf7a" : "#199e70",
      s1Fill: light ? "rgba(42,120,214,.16)" : "rgba(57,135,229,.22)",
      s1FillStrong: light ? "rgba(42,120,214,.30)" : "rgba(57,135,229,.38)",
    };
  }
  const MONO = 'ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace';

  function baseLayout(C, extra) {
    const axis = { gridcolor: "rgba(0,0,0,0)", linecolor: C.line, tickfont: { family: MONO, size: 10, color: C.muted }, zeroline: false };
    return Object.assign(
      {
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
        font: { family: MONO, color: C.ink, size: 11 },
        margin: { l: 56, r: 16, t: 16, b: 44 },
        hoverlabel: { bgcolor: C.bg, bordercolor: C.lineStrong, font: { family: MONO, size: 11, color: C.ink } },
        legend: { orientation: "h", x: 0, y: -0.18, font: { size: 11, color: C.ink2 } },
        xaxis: Object.assign({}, axis),
        yaxis: Object.assign({}, axis, { gridcolor: C.line, linecolor: "rgba(0,0,0,0)" }),
      },
      extra || {}
    );
  }
  const CONFIG = {
    displaylogo: false,
    responsive: true,
    displayModeBar: window.innerWidth > 700 ? "hover" : false,
    modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d"],
  };

  // Charts register a draw function so a theme switch can repaint them.
  const charts = new Map();
  function draw(el, fn) {
    if (!el) return;
    charts.set(el, fn);
    fn(palette());
  }
  function redrawAll() {
    for (const [el, fn] of charts) {
      if (document.body.contains(el)) fn(palette());
      else charts.delete(el);
    }
  }

  // ------------------------------------------------------------ theme switcher (shared key with Beezy Sales)
  function setTheme(t) {
    if (!["bw", "navy", "light"].includes(t)) return;
    document.documentElement.className = "theme-" + t;
    try { localStorage.setItem("beezy-theme", t); } catch (e) {}
    redrawAll();
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-theme-set]");
    if (b) setTheme(b.dataset.themeSet);
  });

  const getJSON = (url) =>
    fetch(url, { headers: { Accept: "application/json" } }).then((r) => {
      if (!r.ok) throw new Error(r.status + " " + url);
      return r.json();
    });

  function whenVisible(el, fn) {
    if (!el) return;
    if (!("IntersectionObserver" in window)) return fn();
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { io.disconnect(); fn(); }
    }, { rootMargin: "200px" });
    io.observe(el);
  }
  function fail(el, msg) {
    if (el) el.innerHTML = '<p class="empty" style="padding:1em">' + msg + "</p>";
  }

  // ------------------------------------------------------------ risk / reward scatter
  function scatter(id) {
    const el = document.getElementById(id);
    whenVisible(el, () =>
      getJSON("/api/recommendations/scatter")
        .then((pts) => {
          if (!pts.length) return fail(el, "No analysis yet.");
          const maxCap = Math.max(...pts.map((p) => p.market_cap || 0)) || 1;
          draw(el, (C) => {
            const groups = [
              ["Not eligible", C.lineStrong, 7, false],
              ["Eligible", C.muted, 8, false],
              ["Low risk / steady", C.s3, 12, true],
              ["High risk / high gain", C.s2, 12, true],
              ["Top 5", C.s1, 14, true],
            ];
            const traces = groups
              .map(([g, color, size, label]) => {
                const ps = pts.filter((p) => p.group === g);
                if (!ps.length) return null;
                return {
                  type: "scatter", mode: label ? "markers+text" : "markers", name: g,
                  x: ps.map((p) => p.risk), y: ps.map((p) => p.exp_1y * 100),
                  text: ps.map((p) => (label ? p.ticker : "")), textposition: "top center",
                  textfont: { family: MONO, size: 10, color: C.ink },
                  customdata: ps.map((p) => [p.ticker, p.name, p.composite]),
                  hovertemplate: "<b>%{customdata[0]}</b> %{customdata[1]}<br>Risk %{x:.0f} · 12-mo %{y:+.1f}%<br>Score %{customdata[2]:.0f}<extra>" + g + "</extra>",
                  marker: {
                    color, size: ps.map((p) => Math.max(size, size + 14 * Math.sqrt((p.market_cap || 0) / maxCap))),
                    line: { color: C.bg, width: 2 }, opacity: label ? 0.95 : 0.85,
                  },
                };
              })
              .filter(Boolean);
            const L = baseLayout(C);
            L.xaxis.title = { text: "Risk score (higher = riskier)", font: { size: 11, color: C.muted } };
            Object.assign(L.yaxis, { title: { text: "Projected 12-month change (%)", font: { size: 11, color: C.muted } }, ticksuffix: "%", zeroline: true, zerolinecolor: C.lineStrong, zerolinewidth: 1 });
            L.hovermode = "closest";
            L.margin = { l: 60, r: 16, t: 30, b: 84 };
            Plotly.react(el, traces, L, CONFIG);
          });
          el.on("plotly_click", (e) => {
            const t = e.points && e.points[0] && e.points[0].customdata && e.points[0].customdata[0];
            if (t) location.href = "/stocks/" + encodeURIComponent(t) + "/";
          });
        })
        .catch(() => fail(el, "Chart unavailable."))
    );
  }

  // ------------------------------------------------------------ stock page charts
  function stockCharts(ticker) {
    const priceEl = document.getElementById("price-chart");
    const epsEl = document.getElementById("eps-chart");
    let dataP = null;
    const load = () => (dataP = dataP || getJSON("/api/stocks/" + encodeURIComponent(ticker) + "/chart"));

    whenVisible(priceEl, () =>
      load()
        .then((d) => {
          if (!d.dates.length) return fail(priceEl, "No price history yet.");
          draw(priceEl, (C) => {
            const traces = [
              { type: "scatter", mode: "lines", name: "Close", x: d.dates, y: d.close, line: { color: C.ink, width: 2 }, hovertemplate: "KES %{y:,.2f}<extra>Close</extra>" },
              { type: "scatter", mode: "lines", name: "50-day avg", x: d.dates, y: d.sma50, line: { color: C.muted, width: 1.5, dash: "dash" }, hovertemplate: "%{y:,.2f}<extra>50-day</extra>" },
              { type: "scatter", mode: "lines", name: "200-day avg", x: d.dates, y: d.sma200, line: { color: C.lineStrong, width: 1.5, dash: "dot" }, hovertemplate: "%{y:,.2f}<extra>200-day</extra>" },
            ];
            const p = d.projection;
            if (p && p.dates.length > 1) {
              traces.push(
                { type: "scatter", mode: "lines", x: p.dates, y: p.low, line: { width: 0, color: C.s1 }, hoverinfo: "skip", showlegend: false },
                { type: "scatter", mode: "lines", name: "90% projected range", x: p.dates, y: p.high, fill: "tonexty", fillcolor: C.s1Fill, line: { width: 0, color: C.s1 }, hoverinfo: "skip" },
                {
                  type: "scatter", mode: "lines+markers", name: "Projection", x: p.dates, y: p.mid,
                  line: { color: C.s1, width: 2, dash: "dash" }, marker: { size: 8, color: C.s1, line: { color: C.bg, width: 2 } },
                  customdata: p.labels.map((l, i) => [l, p.low[i], p.high[i]]),
                  hovertemplate: "<b>%{customdata[0]}</b><br>KES %{y:,.2f}<br>range %{customdata[1]:,.2f} – %{customdata[2]:,.2f}<extra></extra>",
                }
              );
            }
            const last = new Date(d.dates[d.dates.length - 1]);
            const start = new Date(last); start.setFullYear(start.getFullYear() - 1);
            const end = p && p.dates.length ? p.dates[p.dates.length - 1] : d.dates[d.dates.length - 1];
            const L = baseLayout(C);
            L.hovermode = "x unified";
            Object.assign(L.xaxis, {
              type: "date", range: [start.toISOString().slice(0, 10), end],
              showspikes: true, spikemode: "across", spikecolor: C.muted, spikethickness: 1, spikedash: "dot",
              rangeselector: {
                x: 0, y: 1.14, bgcolor: C.bg, activecolor: C.lineStrong, bordercolor: C.line, borderwidth: 1,
                font: { family: MONO, size: 10, color: C.ink },
                buttons: [
                  { count: 1, label: "1M", step: "month", stepmode: "backward" },
                  { count: 6, label: "6M", step: "month", stepmode: "backward" },
                  { count: 1, label: "1Y", step: "year", stepmode: "backward" },
                  { count: 5, label: "5Y", step: "year", stepmode: "backward" },
                  { step: "all", label: "All" },
                ],
              },
            });
            L.yaxis.title = { text: "KES", font: { size: 11, color: C.muted } };
            L.legend.y = -0.14;
            L.margin = { l: 60, r: 16, t: 44, b: 60 };
            Plotly.react(priceEl, traces, L, CONFIG);
          });
        })
        .catch(() => fail(priceEl, "Chart unavailable."))
    );

    whenVisible(epsEl, () =>
      load()
        .then((d) => {
          const h = d.eps_history || [];
          if (!h.length) return fail(epsEl, "No EPS history on file.");
          draw(epsEl, (C) => {
            const trace = {
              type: "bar", name: "EPS", x: h.map((r) => r.period), y: h.map((r) => r.eps),
              marker: { color: h.map((r) => (r.period === "TTM" ? C.s1 : C.muted)), line: { color: C.bg, width: 2 } },
              hovertemplate: "%{x}<br>EPS KES %{y:,.2f}<extra></extra>",
              text: h.map((r, i) => (i === h.length - 1 || i === 0 ? r.eps.toFixed(2) : "")),
              textposition: "outside", textfont: { family: MONO, size: 10, color: C.ink }, cliponaxis: false,
            };
            const L = baseLayout(C, { showlegend: false, bargap: 0.35, hovermode: "closest" });
            Object.assign(L.yaxis, { title: { text: "KES per share", font: { size: 11, color: C.muted } }, zeroline: true, zerolinecolor: C.lineStrong });
            L.margin = { l: 56, r: 12, t: 24, b: 40 };
            Plotly.react(epsEl, [trace], L, CONFIG);
          });
        })
        .catch(() => fail(epsEl, "Chart unavailable."))
    );
  }

  // ------------------------------------------------------------ calculator
  const kes = (x) => "KES " + Math.round(x).toLocaleString("en-KE");
  function calcChart() {
    const el = document.getElementById("calc-chart");
    const src = document.getElementById("calc-chart-data");
    if (!el || !src || !window.Plotly) return;
    const d = JSON.parse(src.textContent);
    draw(el, (C) => {
      const band = (lo, hi, name, fill) => [
        { type: "scatter", mode: "lines", x: d.dates, y: lo, line: { width: 0, color: C.s1 }, hoverinfo: "skip", showlegend: false },
        { type: "scatter", mode: "lines", name, x: d.dates, y: hi, fill: "tonexty", fillcolor: fill, line: { width: 0, color: C.s1 }, hoverinfo: "skip" },
      ];
      const traces = [
        ...band(d.p5, d.p95, "90% of outcomes", C.s1Fill),
        ...band(d.p25, d.p75, "Middle half", C.s1FillStrong),
        {
          type: "scatter", mode: "lines", name: "Likely (median)", x: d.dates, y: d.p50, line: { color: C.s1, width: 2.5 },
          customdata: d.p5.map((v, i) => [d.p5[i], d.p95[i]]),
          hovertemplate: "<b>%{x|%d %b %Y}</b><br>Likely KES %{y:,.0f}<br>90%%: %{customdata[0]:,.0f} – %{customdata[1]:,.0f}<extra></extra>",
        },
        {
          type: "scatter", mode: "lines", name: "Amount invested", x: [d.dates[0], d.dates[d.dates.length - 1]], y: [d.amount, d.amount],
          line: { color: C.muted, width: 1.5, dash: "dash" }, hoverinfo: "skip",
        },
      ];
      const L = baseLayout(C, { hovermode: "x" });
      Object.assign(L.yaxis, { tickprefix: "KES ", tickformat: ",.0f" });
      L.xaxis.type = "date";
      L.margin = { l: 84, r: 16, t: 12, b: 60 };
      L.legend.y = -0.2;
      Plotly.react(el, traces, L, CONFIG);
    });
  }

  document.addEventListener("click", (e) => {
    const q = e.target.closest(".quick [data-h]");
    if (q) {
      const f = document.getElementById("calc-form");
      f.horizon.value = q.dataset.h;
      f.unit.value = q.dataset.u;
      htmx.trigger(f, "submit");
      return;
    }
    const p = e.target.closest("[data-pick-profile]");
    if (p) {
      const f = document.getElementById("calc-form");
      const r = f && f.querySelector('input[name="profile"][value="' + p.dataset.pickProfile + '"]');
      if (r) { r.checked = true; htmx.trigger(f, "submit"); f.scrollIntoView({ behavior: "smooth", block: "start" }); }
    }
  });
  // nicer amount input: keep thousands separators
  document.addEventListener("blur", (e) => {
    if (e.target && e.target.id === "calc-amount") {
      const n = parseFloat(e.target.value.replace(/[^0-9.]/g, ""));
      if (Number.isFinite(n)) e.target.value = n.toLocaleString("en-KE");
    }
  }, true);

  // ------------------------------------------------------------ HTMX: progress bar + post-swap hooks
  const bar = () => document.getElementById("page-progress");
  let inflight = 0, timer = null;
  document.addEventListener("htmx:beforeRequest", (e) => {
    if (e.detail.elt && e.detail.elt.id === "status") return; // background polling stays quiet
    inflight++;
    const b = bar(); if (!b) return;
    clearTimeout(timer);
    b.style.opacity = "1"; b.style.width = "30%";
    timer = setTimeout(() => { b.style.width = "70%"; }, 250);
  });
  document.addEventListener("htmx:afterRequest", (e) => {
    if (e.detail.elt && e.detail.elt.id === "status") return;
    inflight = Math.max(0, inflight - 1);
    if (inflight) return;
    const b = bar(); if (!b) return;
    clearTimeout(timer);
    b.style.width = "100%";
    timer = setTimeout(() => { b.style.opacity = "0"; setTimeout(() => { b.style.width = "0"; }, 300); }, 200);
  });
  document.addEventListener("htmx:afterSwap", (e) => {
    if (e.detail.target && e.detail.target.id === "calc-result") calcChart();
  });

  // ------------------------------------------------------------ disclaimer (first visit)
  const COOKIE = "bz_disclaimer";
  function openDisclaimer() {
    const d = document.getElementById("disclaimer");
    if (d && !d.open && d.showModal) d.showModal();
  }
  document.addEventListener("DOMContentLoaded", () => {
    const d = document.getElementById("disclaimer");
    if (!d) return;
    if (d.hasAttribute("data-auto-open")) {
      openDisclaimer();
      // must be acknowledged: Esc doesn't dismiss it the first time
      d.addEventListener("cancel", (ev) => { if (d.hasAttribute("data-auto-open")) ev.preventDefault(); });
    }
    d.addEventListener("close", () => {
      d.removeAttribute("data-auto-open");
      document.cookie = COOKIE + "=1; Max-Age=31536000; Path=/; SameSite=Lax";
      try { localStorage.setItem("beezy-trader-disclaimer", new Date().toISOString()); } catch (e) {}
    });
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-open-disclaimer]")) openDisclaimer();
  });

  // ------------------------------------------------------------ tabs (HTMX-driven)
  document.addEventListener("click", (e) => {
    const tab = e.target.closest("[data-tabgroup]");
    if (!tab) return;
    const g = tab.dataset.tabgroup;
    document.querySelectorAll('[data-tabgroup="' + g + '"]').forEach((t) => {
      t.classList.toggle("on", t === tab);
      t.setAttribute("aria-selected", t === tab ? "true" : "false");
    });
  });

  // ------------------------------------------------------------ sortable tables
  const sortState = new WeakMap();
  function sortTable(table, col, type, dir) {
    const body = table.tBodies[0];
    const rows = Array.from(body.rows).filter((r) => r.cells.length > 1);
    const val = (r) => {
      const c = r.cells[col];
      const raw = c ? c.dataset.v ?? c.textContent.trim() : "";
      if (type === "num") { const n = parseFloat(raw); return Number.isFinite(n) ? n : null; }
      return raw.toLowerCase();
    };
    rows.sort((a, b) => {
      const x = val(a), y = val(b);
      if (x === null && y === null) return 0;
      if (x === null) return 1;
      if (y === null) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
    const frag = document.createDocumentFragment();
    rows.forEach((r) => frag.appendChild(r));
    body.appendChild(frag);
    table.querySelectorAll("th[data-sort]").forEach((th, i) => {
      th.classList.toggle("sorted-asc", i === col && dir === 1);
      th.classList.toggle("sorted-desc", i === col && dir === -1);
    });
    sortState.set(table, { col, type, dir });
  }
  document.addEventListener("click", (e) => {
    const th = e.target.closest("table.sortable th[data-sort]");
    if (!th) return;
    const table = th.closest("table");
    const col = Array.from(th.parentNode.children).indexOf(th);
    const prev = sortState.get(table);
    const dir = prev && prev.col === col ? -prev.dir : th.dataset.sort === "num" ? -1 : 1;
    sortTable(table, col, th.dataset.sort, dir);
  });
  document.addEventListener("htmx:afterSwap", (e) => {
    const table = e.target.closest && e.target.closest("table.sortable");
    const st = table && sortState.get(table);
    if (st) sortTable(table, st.col, st.type, st.dir);
  });

  window.Beezy = { scatter, stockCharts, calcChart, setTheme, openDisclaimer, kes };
})();
