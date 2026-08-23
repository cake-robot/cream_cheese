/* Pure (data, opts) -> DOM builders. No fetching, no page knowledge.
 * Every builder appends a <details> table twin alongside its chart --
 * a chart without one isn't done, per the dataviz skill's non-negotiables. */

const SVGNS = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c) node.appendChild(c);
  return node;
}
function svgText(x, y, text, attrs = {}) {
  const t = svg("text", { x, y, ...attrs });
  t.textContent = text;
  return t;
}
function linScale(domain, range) {
  const [d0, d1] = domain, [r0, r1] = range;
  const span = d1 - d0 || 1;
  return (v) => r0 + ((v - d0) / span) * (r1 - r0);
}

/* ---------------------------------------------------------------------
 * wpChart -- the game-detail centrepiece. x = play ordinal (never elapsed
 * time -- 356 games have non-monotonic clocks), y = home win probability.
 * One axis. Period bands, one line, score-change markers, a single end
 * label, crosshair+tooltip on hover AND on keyboard focus.
 * ------------------------------------------------------------------- */
function wpChart(mount, wp, game) {
  const W = 760, H = 280;
  const margin = { top: 14, right: 18, bottom: 30, left: 46 };
  const plotW = W - margin.left - margin.right;
  const plotH = H - margin.top - margin.bottom;
  const n = wp.n;
  const x = linScale([0, Math.max(n - 1, 1)], [margin.left, margin.left + plotW]);
  const y = linScale([0, 1], [margin.top + plotH, margin.top]);

  const homeAbbr = game.home.abbr, awayAbbr = game.away.abbr;
  const root = svg("svg", {
    viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: "0",
    "aria-label": `Win probability, ${awayAbbr} at ${homeAbbr}, ${n} plays`,
    "shape-rendering": "geometricPrecision",
  });

  // period bands (alternating tint)
  wp.meta.period_starts.forEach((p, i) => {
    const nextI = wp.meta.period_starts[i + 1] ? wp.meta.period_starts[i + 1].i : n - 1;
    if (i % 2 === 1) {
      root.appendChild(svg("rect", {
        x: x(p.i).toFixed(1), y: margin.top, width: Math.max(x(nextI) - x(p.i), 0).toFixed(1), height: plotH,
        fill: "var(--page)",
      }));
    }
  });

  // grid + 50% reference
  [0, 0.25, 0.5, 0.75, 1].forEach((v) => {
    root.appendChild(svg("line", {
      x1: margin.left, x2: margin.left + plotW, y1: y(v).toFixed(1), y2: y(v).toFixed(1),
      stroke: v === 0.5 ? "var(--axis)" : "var(--grid)", "stroke-width": "1",
      "vector-effect": "non-scaling-stroke",
    }));
  });
  root.appendChild(svgText(4, y(1) + 8, `100% ${homeAbbr}`, { "font-size": "9.5", fill: "var(--ink-muted)" }));
  root.appendChild(svgText(4, y(0.5) + 4, "50%", { "font-size": "9.5", fill: "var(--ink-muted)" }));
  root.appendChild(svgText(4, y(0) + 4, `100% ${awayAbbr}`, { "font-size": "9.5", fill: "var(--ink-muted)" }));

  // period boundary rules + labels
  wp.meta.period_starts.forEach((p, i) => {
    if (i > 0) {
      root.appendChild(svg("line", {
        x1: x(p.i).toFixed(1), x2: x(p.i).toFixed(1), y1: margin.top, y2: margin.top + plotH,
        stroke: "var(--grid)", "stroke-width": "1", "vector-effect": "non-scaling-stroke",
      }));
    }
    const nextI = wp.meta.period_starts[i + 1] ? wp.meta.period_starts[i + 1].i : n - 1;
    const midX = (x(p.i) + x(nextI)) / 2;
    root.appendChild(svgText(midX, H - 6, p.label, {
      "font-size": "9.5", fill: "var(--ink-muted)", "text-anchor": "middle",
    }));
  });

  // the line
  const path = wp.home_win_pct.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  root.appendChild(svg("path", {
    d: path, fill: "none", stroke: "var(--series-1)", "stroke-width": "2",
    "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke",
  }));

  // score-change markers
  wp.meta.score_changes.forEach((sc) => {
    root.appendChild(svg("circle", {
      cx: x(sc.i).toFixed(1), cy: y(wp.home_win_pct[sc.i]).toFixed(1), r: "4",
      fill: "var(--series-1)", stroke: "var(--surface-1)", "stroke-width": "2",
    }));
  });

  // end marker + the one direct label
  const lastI = n - 1;
  root.appendChild(svg("circle", {
    cx: x(lastI).toFixed(1), cy: y(wp.home_win_pct[lastI]).toFixed(1), r: "4",
    fill: "var(--series-1)", stroke: "var(--surface-1)", "stroke-width": "2",
  }));
  const finalLabel = `${wp.meta.final.away}–${wp.meta.final.home}`;
  root.appendChild(svgText(x(lastI) - 4, y(wp.home_win_pct[lastI]) - 10, finalLabel, {
    "font-size": "11", fill: "var(--ink-1)", "text-anchor": "end", "font-weight": "600",
  }));

  // hover / keyboard crosshair
  const crosshair = svg("line", {
    x1: 0, x2: 0, y1: margin.top, y2: margin.top + plotH,
    stroke: "var(--ink-muted)", "stroke-width": "1", "vector-effect": "non-scaling-stroke",
    visibility: "hidden",
  });
  const crossDot = svg("circle", { r: "4", fill: "var(--series-1)", stroke: "var(--surface-1)", "stroke-width": "2", visibility: "hidden" });
  root.appendChild(crosshair);
  root.appendChild(crossDot);

  function moveTo(idx, clientX, clientY) {
    idx = Math.max(0, Math.min(n - 1, idx));
    const px = x(idx);
    crosshair.setAttribute("x1", px.toFixed(1));
    crosshair.setAttribute("x2", px.toFixed(1));
    crosshair.setAttribute("visibility", "visible");
    crossDot.setAttribute("cx", px.toFixed(1));
    crossDot.setAttribute("cy", y(wp.home_win_pct[idx]).toFixed(1));
    crossDot.setAttribute("visibility", "visible");
    const pct = (wp.home_win_pct[idx] * 100).toFixed(1);
    const period = wp.meta.period_starts.slice().reverse().find((p) => p.i <= idx);
    const clock = wp.clock_display[idx] || "";
    const hs = wp.home_score_clean[idx], as = wp.away_score_clean[idx];
    const html = `<div class="tt-value">${pct}% ${homeAbbr}</div>` +
      `<div class="tt-sub">${period ? period.label : ""}${clock ? " · " + clock : ""}</div>` +
      `<div class="tt-sub">${awayAbbr} ${as} – ${homeAbbr} ${hs}</div>`;
    if (clientX !== undefined) showTooltip(html, clientX, clientY);
  }

  const hitRect = svg("rect", {
    x: margin.left, y: margin.top, width: plotW, height: plotH, fill: "transparent",
  });
  hitRect.addEventListener("pointermove", (ev) => {
    const rect = root.getBoundingClientRect();
    const scale = W / rect.width;
    const localX = (ev.clientX - rect.left) * scale;
    const idx = Math.round((localX - margin.left) / plotW * (n - 1));
    moveTo(idx, ev.clientX, ev.clientY);
  });
  hitRect.addEventListener("pointerleave", () => {
    crosshair.setAttribute("visibility", "hidden");
    crossDot.setAttribute("visibility", "hidden");
    hideTooltip();
  });
  root.appendChild(hitRect);

  let focusIdx = 0;
  root.addEventListener("keydown", (ev) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"].includes(ev.key)) ev.preventDefault();
    if (ev.key === "ArrowLeft") focusIdx -= 1;
    else if (ev.key === "ArrowRight") focusIdx += 1;
    else if (ev.key === "PageDown") focusIdx -= 10;
    else if (ev.key === "PageUp") focusIdx += 10;
    else if (ev.key === "Home") focusIdx = 0;
    else if (ev.key === "End") focusIdx = n - 1;
    else return;
    const rect = root.getBoundingClientRect();
    const px = x(Math.max(0, Math.min(n - 1, focusIdx))) / W * rect.width + rect.left;
    const py = rect.top + rect.height / 2;
    moveTo(focusIdx, px, py);
  });
  root.addEventListener("blur", () => {
    crosshair.setAttribute("visibility", "hidden");
    crossDot.setAttribute("visibility", "hidden");
    hideTooltip();
  });

  const key = el("div", { class: "chart-key" }, [
    el("span", { class: "k" }, [el("span", { class: "line" }), "Home win probability"]),
    el("span", { class: "k" }, [el("span", { class: "dot" }), "Scoring play"]),
  ]);

  mount.appendChild(key);
  mount.appendChild(el("div", { class: "viz" }, root));

  const rows = wp.i.map((i) => [
    String(i),
    wp.meta.period_starts.slice().reverse().find((p) => p.i <= i)?.label || "",
    wp.clock_display[i] || "",
    (wp.home_win_pct[i] * 100).toFixed(1) + "%",
    String(wp.home_score_clean[i]),
    String(wp.away_score_clean[i]),
  ]);
  mount.appendChild(tableTwin(`Show as table (${n} plays)`,
    ["Play #", "Period", "Clock", "Home WP", "Home score", "Away score"], rows));

  if (wp.meta.wp_final !== null && wp.meta.wp_final > 0.02 && wp.meta.wp_final < 0.98) {
    const finalPct = (wp.meta.wp_final * 100).toFixed(1);
    mount.appendChild(el("div", { class: "caveat" },
      `Note: ESPN's final win-probability value for this game is ${finalPct}%, which disagrees with the ` +
      `${wp.meta.final.away}–${wp.meta.final.home} final. Overtime tails are often noisy.`));
  }
}

/* ---------------------------------------------------------------------
 * wpDualCard -- scored-game-detail centerpiece (design_handoff_game_detail,
 * locked to the Dual chart / Bars ladder variant). x = play ordinal (same
 * rationale as wpChart -- elapsed is non-monotonic in a chunk of games).
 * Two stacked SVGs sharing one 1000-unit-wide coordinate space: a filled
 * area chart of home win probability, and a waterfall score ladder below
 * it. All text is an absolutely-positioned HTML overlay rather than SVG
 * <text> -- an SVG viewBox downscales text authored inside it along with
 * everything else (a 10-unit label in a 1000-unit box rendered at 714px
 * comes out at 7.1px), so real px type has to live outside the viewBox.
 * ------------------------------------------------------------------- */
function wpDualCard(mount, wp, game) {
  const n = wp.n;
  const homeAbbr = game.home.abbr, awayAbbr = game.away.abbr;
  const PAD_L = 44, PAD_R = 12, TOP = 14, plotW = 1000 - PAD_L - PAD_R;
  // Both charts render at width:100% off the same 1000-unit-wide card, so
  // giving them the same viewBox height is what makes them occupy the same
  // vertical space on screen -- CHART_H is shared with the ladder below.
  const CHART_H = 340;
  const plotH = CHART_H - TOP - 20, bottom = TOP + plotH, vbH = TOP + plotH + 20;
  const x = (i) => PAD_L + (n > 1 ? i / (n - 1) : 0) * plotW;
  const y = (wpv) => TOP + (1 - wpv) * plotH;
  const mid = y(0.5);

  const wpPoints = wp.i.map((i) => `${x(i).toFixed(1)},${y(wp.home_win_pct[i]).toFixed(1)}`).join(" ");
  const areaPoints = `${x(0).toFixed(1)},${mid.toFixed(1)} ${wpPoints} ${x(n - 1).toFixed(1)},${mid.toFixed(1)}`;

  const starts = wp.meta.period_starts;
  const bands = starts.map((p, k) => {
    const nextI = starts[k + 1] ? starts[k + 1].i : n - 1;
    return {
      x: x(p.i), w: x(nextI) - x(p.i), label: p.label,
      fill: k % 2 ? "rgba(255,255,255,0.012)" : "transparent",
    };
  });

  const plotRoot = svg("svg", { viewBox: `0 0 1000 ${vbH.toFixed(1)}` });
  const clipHome = `clip-home-${Math.random().toString(36).slice(2)}`;
  const clipAway = `clip-away-${Math.random().toString(36).slice(2)}`;
  const defs = svg("defs", {}, [
    svg("clipPath", { id: clipHome }, svg("rect", { x: 0, y: 0, width: 1000, height: mid.toFixed(1) })),
    svg("clipPath", { id: clipAway }, svg("rect", { x: 0, y: mid.toFixed(1), width: 1000, height: vbH.toFixed(1) })),
  ]);
  plotRoot.appendChild(defs);
  bands.forEach((b) => {
    plotRoot.appendChild(svg("g", {}, [
      svg("rect", { x: b.x.toFixed(1), y: TOP, width: b.w.toFixed(1), height: plotH, fill: b.fill }),
      svg("line", { x1: b.x.toFixed(1), y1: TOP, x2: b.x.toFixed(1), y2: bottom, stroke: "var(--g-border)" }),
    ]));
  });
  plotRoot.appendChild(svg("line", {
    x1: PAD_L, y1: mid.toFixed(1), x2: 1000 - PAD_R, y2: mid.toFixed(1),
    stroke: "var(--g-border-strong2)", "stroke-dasharray": "3 4",
  }));
  plotRoot.appendChild(svg("polygon", { points: areaPoints, fill: "var(--g-ink-bright)", opacity: ".16", "clip-path": `url(#${clipHome})` }));
  plotRoot.appendChild(svg("polygon", { points: areaPoints, fill: "var(--g-accent)", opacity: ".34", "clip-path": `url(#${clipAway})` }));
  plotRoot.appendChild(svg("polyline", { points: wpPoints, fill: "none", stroke: "var(--g-ink-bright)", "stroke-width": "1.4", "stroke-linejoin": "round" }));
  wp.meta.score_changes.forEach((sc) => {
    plotRoot.appendChild(svg("circle", {
      cx: x(sc.i).toFixed(1), cy: y(wp.home_win_pct[sc.i]).toFixed(1), r: "5.2",
      fill: sc.team === "home" ? "var(--g-ink-bright)" : "var(--g-accent)",
      stroke: "var(--g-card)", "stroke-width": "1",
    }));
  });

  // hover / keyboard crosshair -- same contract as wpChart's, ported to
  // this chart's 1000-unit coordinate space and page-scoped colors.
  const crosshair = svg("line", {
    x1: 0, x2: 0, y1: TOP, y2: bottom, stroke: "var(--g-ink-muted)", "stroke-width": "1", visibility: "hidden",
  });
  const crossDot = svg("circle", { r: "3.5", fill: "var(--g-ink-bright)", stroke: "var(--g-card)", "stroke-width": "1.5", visibility: "hidden" });
  plotRoot.appendChild(crosshair);
  plotRoot.appendChild(crossDot);

  function moveTo(idx, clientX, clientY) {
    idx = Math.max(0, Math.min(n - 1, idx));
    const px = x(idx);
    crosshair.setAttribute("x1", px.toFixed(1));
    crosshair.setAttribute("x2", px.toFixed(1));
    crosshair.setAttribute("visibility", "visible");
    crossDot.setAttribute("cx", px.toFixed(1));
    crossDot.setAttribute("cy", y(wp.home_win_pct[idx]).toFixed(1));
    crossDot.setAttribute("visibility", "visible");
    const pct = (wp.home_win_pct[idx] * 100).toFixed(1);
    const period = wp.meta.period_starts.slice().reverse().find((p) => p.i <= idx);
    const clock = wp.clock_display[idx] || "";
    const hs = wp.home_score_clean[idx], as = wp.away_score_clean[idx];
    const html = `<div class="tt-value">${pct}% ${homeAbbr}</div>` +
      `<div class="tt-sub">${period ? period.label : ""}${clock ? " · " + clock : ""}</div>` +
      `<div class="tt-sub">${awayAbbr} ${as} – ${homeAbbr} ${hs}</div>`;
    if (clientX !== undefined) showTooltip(html, clientX, clientY);
  }

  const wpHit = svg("rect", { x: PAD_L, y: 0, width: plotW.toFixed(1), height: vbH.toFixed(1), fill: "transparent" });
  wpHit.addEventListener("pointermove", (ev) => {
    const rect = plotRoot.getBoundingClientRect();
    const scale = 1000 / rect.width;
    const localX = (ev.clientX - rect.left) * scale;
    const idx = Math.round(((localX - PAD_L) / plotW) * (n - 1));
    moveTo(idx, ev.clientX, ev.clientY);
  });
  wpHit.addEventListener("pointerleave", () => {
    crosshair.setAttribute("visibility", "hidden");
    crossDot.setAttribute("visibility", "hidden");
    hideTooltip();
  });
  plotRoot.appendChild(wpHit);

  plotRoot.setAttribute("tabindex", "0");
  plotRoot.setAttribute("role", "img");
  plotRoot.setAttribute("aria-label", `Win probability, ${awayAbbr} at ${homeAbbr}, ${n} plays`);
  let focusIdx = 0;
  plotRoot.addEventListener("keydown", (ev) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"].includes(ev.key)) ev.preventDefault();
    if (ev.key === "ArrowLeft") focusIdx -= 1;
    else if (ev.key === "ArrowRight") focusIdx += 1;
    else if (ev.key === "PageDown") focusIdx -= 10;
    else if (ev.key === "PageUp") focusIdx += 10;
    else if (ev.key === "Home") focusIdx = 0;
    else if (ev.key === "End") focusIdx = n - 1;
    else return;
    focusIdx = Math.max(0, Math.min(n - 1, focusIdx));
    const rect = plotRoot.getBoundingClientRect();
    const px = (x(focusIdx) / 1000) * rect.width + rect.left;
    const py = rect.top + rect.height / 2;
    moveTo(focusIdx, px, py);
  });
  plotRoot.addEventListener("blur", () => {
    crosshair.setAttribute("visibility", "hidden");
    crossDot.setAttribute("visibility", "hidden");
    hideTooltip();
  });

  const plotWrap = el("div", { class: "g-wp-plot" }, plotRoot);
  bands.forEach((b) => {
    plotWrap.appendChild(el("span", { class: "g-band-label", style: `left:${(b.x + 8) / 10}%` }, b.label));
  });
  [[1, "100%"], [0.5, "50%"], [0, "0%"]].forEach(([v, label]) => {
    plotWrap.appendChild(el("span", { class: "g-ytick", style: `top:${(y(v) / vbH) * 100}%` }, label));
  });

  // --- score ladder (waterfall bars) -------------------------------------
  const LH = CHART_H, zero = LH / 2;
  const margins = wp.meta.score_changes.map((sc) => ({ i: sc.i, m: sc.home - sc.away, team: sc.team, pts: sc.delta, home: sc.home, away: sc.away }));
  const maxAbs = Math.max(...margins.map((d) => Math.abs(d.m)), 1);
  const barMax = zero - 62;
  const barW = Math.min(15, plotW / Math.max(margins.length, 1) / 1.9 / 2);
  const my = (m) => zero - (m / maxAbs) * barMax;

  let prevMargin = 0;
  const ladderBars = margins.map((d) => {
    const from = prevMargin, to = d.m;
    prevMargin = to;
    const up = to > from;
    const yTop = my(Math.max(from, to)), yBot = my(Math.min(from, to));
    const tip = up ? yTop : yBot;
    const fill = d.team === "home" ? "var(--g-ink-bright)" : "var(--g-accent)";
    // The chevron's apex sits exactly on the tip (the true net-margin
    // value), so the bar itself has to stop short of the tip by the same
    // amount the chevron's base recedes -- otherwise the chevron draws
    // right on top of the bar's own fill color and disappears into it.
    // barSpan is the bar's natural (pre-gap) length; carving out GAP would
    // push a short bar below the 5-unit floor, so gapActual shrinks (down
    // to 0) rather than the bar ever inverting.
    const GAP = 8;
    const barSpan = yBot - yTop;
    const finalH = Math.max(5, barSpan - GAP);
    const gapActual = Math.max(0, barSpan - finalH);
    const rectY = up ? yBot - finalH : yTop;
    return {
      x: (x(d.i) - barW / 2).toFixed(1), w: barW.toFixed(1),
      y: rectY.toFixed(1), h: finalH.toFixed(1),
      cx: x(d.i), tip, up, fill,
      chev: [
        `${(x(d.i) - 7).toFixed(1)},${(tip + (up ? gapActual : -gapActual)).toFixed(1)}`,
        `${x(d.i).toFixed(1)},${tip.toFixed(1)}`,
        `${(x(d.i) + 7).toFixed(1)},${(tip + (up ? gapActual : -gapActual)).toFixed(1)}`,
      ].join(" "),
      label: `${d.pts}`,
      labelTop: ((tip + (up ? -14 : 14)) / LH) * 100,
      labelFill: d.team === "home" ? "var(--g-ink-body2)" : "var(--g-accent-tint)",
      scorer: `${d.team === "home" ? homeAbbr : awayAbbr} +${d.pts}`,
      i: d.i, team: d.team, pts: d.pts, from, to, home: d.home, away: d.away,
    };
  });
  const carries = margins.map((d, k) => ({
    x1: x(d.i) + barW / 2,
    x2: k === margins.length - 1 ? x(n - 1) : x(margins[k + 1].i) - barW / 2,
    y: my(d.m),
  }));

  const ladderRoot = svg("svg", { viewBox: `0 0 1000 ${LH}` });
  bands.forEach((b) => {
    ladderRoot.appendChild(svg("line", { x1: b.x.toFixed(1), y1: 0, x2: b.x.toFixed(1), y2: LH - 6, stroke: "var(--g-hairline)" }));
  });
  ladderRoot.appendChild(svg("line", { x1: PAD_L, y1: zero.toFixed(1), x2: 1000 - PAD_R, y2: zero.toFixed(1), stroke: "var(--g-muted-graphic)" }));
  carries.forEach((c) => {
    ladderRoot.appendChild(svg("line", {
      x1: c.x1.toFixed(1), y1: c.y.toFixed(1), x2: c.x2.toFixed(1), y2: c.y.toFixed(1),
      stroke: "var(--g-border-strong2)", "stroke-dasharray": "2 3",
    }));
  });
  const marginLabel = (v) => (v === 0 ? "TIE" : `${v > 0 ? "+" : ""}${v}`);
  ladderBars.forEach((b) => {
    ladderRoot.appendChild(svg("g", {}, [
      svg("rect", { x: b.x, y: b.y, width: b.w, height: b.h, fill: b.fill }),
      svg("polyline", { points: b.chev, fill: "none", stroke: b.fill, "stroke-width": "2.4", "stroke-linecap": "round", "stroke-linejoin": "round" }),
    ]));
    // Full-height hit target, not just the (sometimes 5-unit-tall) bar
    // itself -- a thin field-goal bar would otherwise be nearly unhoverable.
    const hit = svg("rect", { x: b.x, y: 0, width: b.w, height: LH, fill: "transparent" });
    const period = wp.meta.period_starts.slice().reverse().find((p) => p.i <= b.i);
    const clock = wp.clock_display[b.i] || "";
    const scorerAbbr = b.team === "home" ? homeAbbr : awayAbbr;
    hit.addEventListener("pointermove", (ev) => {
      const html = `<div class="tt-value">${scorerAbbr} +${b.pts}</div>` +
        `<div class="tt-sub">${period ? period.label : ""}${clock ? " · " + clock : ""}</div>` +
        `<div class="tt-sub">${awayAbbr} ${b.away} – ${homeAbbr} ${b.home}</div>` +
        `<div class="tt-sub">Margin ${marginLabel(b.from)} → ${marginLabel(b.to)}</div>`;
      showTooltip(html, ev.clientX, ev.clientY);
    });
    hit.addEventListener("pointerleave", hideTooltip);
    ladderRoot.appendChild(hit);
  });

  const ladderWrap = el("div", { class: "g-ladder-plot" }, ladderRoot);
  ladderWrap.appendChild(el("span", { class: "g-ladder-axis", style: `top:${((zero - barMax) / LH) * 100}%` }, `±${maxAbs}`));
  ladderWrap.appendChild(el("span", { class: "g-ladder-axis", style: `top:${((zero + barMax) / LH) * 100}%` }, `±${maxAbs}`));
  ladderBars.forEach((b) => {
    const left = (b.cx / 10).toFixed(2) + "%";
    ladderWrap.appendChild(el("span", { class: "g-ladder-value", style: `left:${left}; top:${b.labelTop}%; color:${b.labelFill}` }, b.label));
    ladderWrap.appendChild(el("span", { class: "g-ladder-scorer", style: `left:${left}; top:${((LH - 10) / LH) * 100}%` }, b.scorer));
  });

  mount.appendChild(plotWrap);
  const ladderSection = el("div", { class: "g-ladder-section" }, [
    el("div", { class: "g-ladder-label-row" }, el("span", { class: "g-ladder-label", text: "NET SCORE — ONE BAR PER SCORING PLAY" })),
    ladderWrap,
    el("div", {
      class: "g-ladder-caption",
      text: "Each bar runs from the margin before the score to the margin after it — the chevron points the way the margin moved, "
        + `so its length is the points scored and its tip is the new margin. Bone = ${homeAbbr} scored, oxblood = ${awayAbbr}. Dashed = margin holding until the next score.`,
    }),
  ]);
  mount.appendChild(ladderSection);

  const rows = wp.i.map((i) => [
    String(i),
    wp.meta.period_starts.slice().reverse().find((p) => p.i <= i)?.label || "",
    wp.clock_display[i] || "",
    (wp.home_win_pct[i] * 100).toFixed(1) + "%",
    String(wp.home_score_clean[i]),
    String(wp.away_score_clean[i]),
  ]);
  mount.appendChild(tableTwin(`Show as table (${n} plays)`,
    ["Play #", "Period", "Clock", "Home WP", "Home score", "Away score"], rows));

  if (wp.meta.wp_final !== null && wp.meta.wp_final > 0.02 && wp.meta.wp_final < 0.98) {
    const finalPct = (wp.meta.wp_final * 100).toFixed(1);
    mount.appendChild(el("div", { class: "caveat" },
      `Note: ESPN's final win-probability value for this game is ${finalPct}%, which disagrees with the ` +
      `${wp.meta.final.away}–${wp.meta.final.home} final. Overtime tails are often noisy.`));
  }
}

/* ---------------------------------------------------------------------
 * scoreChart -- the running score, away vs. home, as a step function.
 * x defaults to event ordinal (play index for ESPN -- 356 games have
 * non-monotonic clocks, so ESPN never gets a time axis), but an adapter
 * can supply `data.x`, an elapsed-seconds position per event index, for a
 * genuinely time-aligned axis -- see foxScoreSeries()'s use of Fox's
 * per-play clock (PATs/two-point tries pinned to their touchdown's time,
 * since the game clock is frozen for the whole try attempt).
 * One shared y-axis (both lines are the same unit, points), stairsteps
 * rather than a smooth line because the value is genuinely constant
 * between scoring plays, markers only where the score actually changed.
 * Consecutive same-team score_changes sharing an x (a PAT/2pt pinned to
 * its touchdown) draw as ONE marker at the final value rather than two
 * markers stacked on the same pixel -- the latter both looked redundant
 * and made the second one permanently unreachable by mouse, since the
 * nearest-point hover test always resolved the tie to whichever index
 * came first. The tooltip for a merged marker always describes the
 * touchdown first (never the try, even though hover ties resolve to the
 * try's own index) -- see tdIdx in moveTo().
 *
 * Takes a source-agnostic `data` shape rather than the raw API payload,
 * so the same renderer draws both the ESPN and Fox score progressions --
 * see espnScoreSeries()/foxScoreSeries() below for the two adapters.
 * data = { n, home, away, x?, period_starts, score_changes, subtitle(idx),
 *          tryInfo?(idx), ariaLabel, unitLabel,
 *          tableColumns: [{header, cell(idx)}] }
 * tryInfo is optional (Fox only) -- for a touchdown index, describes its
 * PAT/two-point try's outcome even when the try FAILED and so never
 * produced its own score_changes entry: { result: 'good'|'failed'|null,
 * evidence: string, decisive: string|null }. `decisive` is the exact
 * substring of `evidence` that decided the result, highlighted alone
 * rather than coloring the whole line.
 * ------------------------------------------------------------------- */
function scoreChart(mount, data, game) {
  const W = 760, H = 220;
  const margin = { top: 14, right: 18, bottom: 30, left: 30 };
  const plotW = W - margin.left - margin.right;
  const plotH = H - margin.top - margin.bottom;
  const n = data.n;
  const home = data.home, away = data.away;
  const homeAbbr = game.home.abbr, awayAbbr = game.away.abbr;

  // x-position per event index: real elapsed seconds when the source
  // supplies one (Fox -- time-aligned, PATs pinned to their TD's clock),
  // otherwise the event ordinal (ESPN -- 356 games have non-monotonic
  // clocks, so ordinal stays the only safe axis there).
  const xVals = data.x || Array.from({ length: n }, (_, i) => i);
  const xMin = xVals[0];
  const xMax = Math.max(xVals[n - 1], xMin + 1);

  const rawMax = Math.max(1, ...home, ...away);
  const maxY = Math.ceil((rawMax * 1.15) / 5) * 5;

  const x = linScale([xMin, xMax], [margin.left, margin.left + plotW]);
  const y = linScale([0, maxY], [margin.top + plotH, margin.top]);

  const root = svg("svg", {
    viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: "0",
    "aria-label": data.ariaLabel || `Score progression, ${awayAbbr} at ${homeAbbr}`,
    "shape-rendering": "geometricPrecision",
  });

  // period bands (alternating tint), same convention as wpChart
  data.period_starts.forEach((p, i) => {
    const nextI = data.period_starts[i + 1] ? data.period_starts[i + 1].i : n - 1;
    if (i % 2 === 1) {
      root.appendChild(svg("rect", {
        x: x(xVals[p.i]).toFixed(1), y: margin.top, width: Math.max(x(xVals[nextI]) - x(xVals[p.i]), 0).toFixed(1), height: plotH,
        fill: "var(--page)",
      }));
    }
  });

  // gridlines
  [0, 0.25, 0.5, 0.75, 1].forEach((f) => {
    const v = maxY * f;
    root.appendChild(svg("line", {
      x1: margin.left, x2: margin.left + plotW, y1: y(v).toFixed(1), y2: y(v).toFixed(1),
      stroke: "var(--grid)", "stroke-width": "1", "vector-effect": "non-scaling-stroke",
    }));
    root.appendChild(svgText(margin.left - 5, y(v).toFixed(1), String(Math.round(v)), {
      "font-size": "9.5", fill: "var(--ink-muted)", "text-anchor": "end", "dominant-baseline": "middle",
    }));
  });

  // period boundary rules + labels
  data.period_starts.forEach((p, i) => {
    if (i > 0) {
      root.appendChild(svg("line", {
        x1: x(xVals[p.i]).toFixed(1), x2: x(xVals[p.i]).toFixed(1), y1: margin.top, y2: margin.top + plotH,
        stroke: "var(--grid)", "stroke-width": "1", "vector-effect": "non-scaling-stroke",
      }));
    }
    const nextI = data.period_starts[i + 1] ? data.period_starts[i + 1].i : n - 1;
    const midX = (x(xVals[p.i]) + x(xVals[nextI])) / 2;
    root.appendChild(svgText(midX, H - 6, p.label, {
      "font-size": "9.5", fill: "var(--ink-muted)", "text-anchor": "middle",
    }));
  });

  function stepPath(values) {
    let d = `M${x(xVals[0]).toFixed(1)},${y(values[0]).toFixed(1)}`;
    for (let i = 1; i < n; i++) {
      d += ` H${x(xVals[i]).toFixed(1)} V${y(values[i]).toFixed(1)}`;
    }
    return d;
  }

  const series = [
    { values: away, color: "var(--series-1)", abbr: awayAbbr, team: "away" },
    { values: home, color: "var(--series-2)", abbr: homeAbbr, team: "home" },
  ];

  series.forEach((s) => {
    root.appendChild(svg("path", {
      d: stepPath(s.values), fill: "none", stroke: s.color, "stroke-width": "2",
      "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke",
    }));
  });

  // Group consecutive same-team score_changes that share an x (a PAT/2pt
  // pinned to its touchdown's clock -- see foxScoreSeries) into one
  // "scoring instant". A no-op for ESPN, whose x is always distinct
  // ordinals: two markers on the same pixel were both visually redundant
  // and, worse, made the second one unreachable by mouse (nearestIdx below
  // always resolved the tie to whichever came first).
  const scoreGroups = [];
  data.score_changes.forEach((sc) => {
    const g = scoreGroups[scoreGroups.length - 1];
    if (g && g.team === sc.team && xVals[g.members[g.members.length - 1].i] === xVals[sc.i]) {
      g.members.push(sc);
    } else {
      scoreGroups.push({ team: sc.team, members: [sc] });
    }
  });
  const groupByRepIdx = new Map();
  scoreGroups.forEach((g) => {
    if (g.members.length > 1) groupByRepIdx.set(g.members[g.members.length - 1].i, g);
  });

  // one marker per group, at its final (post-try) value
  scoreGroups.forEach((g) => {
    const sc = g.members[g.members.length - 1];
    const s = sc.team === "home" ? series[1] : series[0];
    root.appendChild(svg("circle", {
      cx: x(xVals[sc.i]).toFixed(1), cy: y(sc.team === "home" ? sc.home : sc.away).toFixed(1), r: "4",
      fill: s.color, stroke: "var(--surface-1)", "stroke-width": "2",
    }));
  });

  // end markers + direct labels, floated above the line/dot (nudged apart if the two labels would collide)
  const lastI = n - 1;
  const dotYs = series.map((s) => y(s.values[lastI]));
  const labelYs = dotYs.map((v) => v - 10);
  if (Math.abs(dotYs[0] - dotYs[1]) < 13) {
    const mid = (dotYs[0] + dotYs[1]) / 2;
    const lo = dotYs[0] <= dotYs[1] ? 0 : 1; // higher-on-screen (smaller y) series
    labelYs[lo] = mid - 14;
    labelYs[1 - lo] = mid + 16;
  }
  series.forEach((s, i) => {
    root.appendChild(svg("circle", {
      cx: x(xVals[lastI]).toFixed(1), cy: dotYs[i].toFixed(1), r: "4",
      fill: s.color, stroke: "var(--surface-1)", "stroke-width": "2",
    }));
    root.appendChild(svgText(x(xVals[lastI]) - 6, labelYs[i], `${s.abbr} ${s.values[lastI]}`, {
      "font-size": "11", fill: s.color, "text-anchor": "end", "font-weight": "600",
    }));
  });

  // hover / keyboard crosshair
  const crosshair = svg("line", {
    x1: 0, x2: 0, y1: margin.top, y2: margin.top + plotH,
    stroke: "var(--ink-muted)", "stroke-width": "1", "vector-effect": "non-scaling-stroke",
    visibility: "hidden",
  });
  const crossDots = series.map((s) => svg("circle", { r: "4", fill: s.color, stroke: "var(--surface-1)", "stroke-width": "2", visibility: "hidden" }));
  root.appendChild(crosshair);
  crossDots.forEach((d) => root.appendChild(d));

  function moveTo(idx, clientX, clientY) {
    idx = Math.max(0, Math.min(n - 1, idx));
    const px = x(xVals[idx]);
    crosshair.setAttribute("x1", px.toFixed(1));
    crosshair.setAttribute("x2", px.toFixed(1));
    crosshair.setAttribute("visibility", "visible");
    series.forEach((s, i) => {
      crossDots[i].setAttribute("cx", px.toFixed(1));
      crossDots[i].setAttribute("cy", y(s.values[idx]).toFixed(1));
      crossDots[i].setAttribute("visibility", "visible");
    });
    // Describe the touchdown, never the try, as the primary line -- for a
    // merged group that's the group's FIRST member regardless of which
    // (tie-broken-to-last) index the crosshair actually landed on.
    const group = groupByRepIdx.get(idx);
    const tdIdx = group ? group.members[0].i : idx;
    const period = data.period_starts.slice().reverse().find((p) => p.i <= tdIdx);
    const sub = data.subtitle(tdIdx);

    // the try is a quiet footnote under the touchdown, not a second
    // headline -- dimmed like any other secondary line, except the word
    // that actually decided good/failed, which stays at full strength
    let extra = "";
    const tryInfo = data.tryInfo && data.tryInfo(tdIdx);
    if (tryInfo && tryInfo.result && tryInfo.evidence) {
      const color = tryInfo.result === "good" ? "#1f8f4d" : "var(--critical)";
      const text = tryInfo.evidence;
      let line;
      const di = tryInfo.decisive ? text.indexOf(tryInfo.decisive) : -1;
      if (di !== -1) {
        const before = text.slice(0, di);
        const after = text.slice(di + tryInfo.decisive.length);
        line = `<span class="dim">+ ${before}</span>` +
          `<span style="color:${color};font-weight:600">${tryInfo.decisive}</span>` +
          `<span class="dim">${after}</span>`;
      } else {
        line = `<span class="dim">+ ${text}</span>`;
      }
      extra = `<div class="tt-sub">${line}</div>`;
    }

    const html = `<div class="tt-value">${awayAbbr} ${away[idx]} – ${homeAbbr} ${home[idx]}</div>` +
      `<div class="tt-sub">${period ? period.label : ""}${sub ? " · " + sub : ""}</div>` + extra;
    if (clientX !== undefined) showTooltip(html, clientX, clientY);
  }

  // nearest event to a pixel position -- not a uniform division, since
  // points are spaced by elapsed time (Fox) rather than always by index.
  // Ties (a PAT pinned to its TD's exact x) resolve to the LATER index --
  // the group's representative marker drawn above -- via <=.
  function nearestIdx(localX) {
    const value = xMin + (localX - margin.left) / plotW * (xMax - xMin);
    let best = 0, bestDist = Infinity;
    for (let i = 0; i < n; i++) {
      const d = Math.abs(xVals[i] - value);
      if (d <= bestDist) { bestDist = d; best = i; }
    }
    return best;
  }

  const hitRect = svg("rect", {
    x: margin.left, y: margin.top, width: plotW, height: plotH, fill: "transparent",
  });
  hitRect.addEventListener("pointermove", (ev) => {
    const rect = root.getBoundingClientRect();
    const scale = W / rect.width;
    const localX = (ev.clientX - rect.left) * scale;
    moveTo(nearestIdx(localX), ev.clientX, ev.clientY);
  });
  hitRect.addEventListener("pointerleave", () => {
    crosshair.setAttribute("visibility", "hidden");
    crossDots.forEach((d) => d.setAttribute("visibility", "hidden"));
    hideTooltip();
  });
  root.appendChild(hitRect);

  let focusIdx = 0;
  root.addEventListener("keydown", (ev) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"].includes(ev.key)) ev.preventDefault();
    if (ev.key === "ArrowLeft") focusIdx -= 1;
    else if (ev.key === "ArrowRight") focusIdx += 1;
    else if (ev.key === "PageDown") focusIdx -= 10;
    else if (ev.key === "PageUp") focusIdx += 10;
    else if (ev.key === "Home") focusIdx = 0;
    else if (ev.key === "End") focusIdx = n - 1;
    else return;
    const rect = root.getBoundingClientRect();
    const clamped = Math.max(0, Math.min(n - 1, focusIdx));
    const px = x(xVals[clamped]) / W * rect.width + rect.left;
    const py = rect.top + rect.height / 2;
    moveTo(focusIdx, px, py);
  });
  root.addEventListener("blur", () => {
    crosshair.setAttribute("visibility", "hidden");
    crossDots.forEach((d) => d.setAttribute("visibility", "hidden"));
    hideTooltip();
  });

  const key = el("div", { class: "chart-key" }, [
    el("span", { class: "k" }, [el("span", { class: "line", style: "background:var(--series-1)" }), awayAbbr]),
    el("span", { class: "k" }, [el("span", { class: "line", style: "background:var(--series-2)" }), homeAbbr]),
    el("span", { class: "k" }, [el("span", { class: "dot" }), "Scoring play"]),
  ]);

  mount.appendChild(key);
  mount.appendChild(el("div", { class: "viz" }, root));

  const rows = Array.from({ length: n }, (_, i) => [
    String(i),
    data.period_starts.slice().reverse().find((p) => p.i <= i)?.label || "",
    ...data.tableColumns.map((c) => c.cell(i)),
    String(away[i]),
    String(home[i]),
  ]);
  mount.appendChild(tableTwin(`Show as table (${data.unitLabel})`,
    [data.indexLabel || "#", "Period", ...data.tableColumns.map((c) => c.header), `${awayAbbr} score`, `${homeAbbr} score`], rows));
  if (data.note) mount.appendChild(el("div", { class: "caveat" }, data.note));
}

/* ---------------------------------------------------------------------
 * espnScoreSeries / foxScoreSeries -- adapt the two API payload shapes
 * (build_wp_payload's wp, build_fox_score_payload's fox_score) into the
 * source-agnostic `data` shape scoreChart() draws.
 * ------------------------------------------------------------------- */
function espnScoreSeries(wp) {
  return {
    n: wp.n,
    home: wp.home_score_clean,
    away: wp.away_score_clean,
    period_starts: wp.meta.period_starts,
    score_changes: wp.meta.score_changes,
    subtitle: (i) => wp.clock_display[i] || "",
    ariaLabel: `ESPN score progression, ${wp.n} plays`,
    unitLabel: `${wp.n} plays`,
    indexLabel: "Play #",
    tableColumns: [{ header: "Clock", cell: (i) => wp.clock_display[i] || "" }],
  };
}

function foxScoreSeries(fox) {
  const mismatch = fox.meta.final.home !== fox.meta.box_score_final.home
    || fox.meta.final.away !== fox.meta.box_score_final.away;
  const clockCell = (i) => {
    const clock = fox.clock_display[i];
    if (!clock) return i === 0 ? "" : "OT";
    return fox.clock_pinned[i] ? `${clock} (PAT, same as TD)` : clock;
  };
  return {
    n: fox.n,
    home: fox.home_score_clean,
    away: fox.away_score_clean,
    x: fox.elapsed_seconds,
    period_starts: fox.meta.period_starts,
    score_changes: fox.meta.score_changes,
    subtitle: (i) => {
      // scoreChart() only ever calls this with a touchdown's (or other
      // ungrouped play's) own index, never a merged-in try's -- so there's
      // no "(same clock as TD)" case to annotate here; see tryInfo below
      // for the try itself.
      const clock = fox.clock_display[i];
      const evidence = fox.evidence[i] || (i === 0 ? "Pregame" : "");
      return clock ? `${clock} · ${evidence}` : evidence;
    },
    tryInfo: (i) => fox.try_info[i] || null,
    ariaLabel: `Fox score progression, ${fox.n} scoring events, time-aligned`,
    unitLabel: `${fox.n} scoring events`,
    indexLabel: "Step #",
    tableColumns: [
      { header: "Clock", cell: clockCell },
      { header: "Play", cell: (i) => fox.evidence[i] || "" },
    ],
    note: mismatch
      ? `Note: Fox's own final score (${fox.meta.final.away}–${fox.meta.final.home}) disagrees with ` +
        `the box score (${fox.meta.box_score_final.away}–${fox.meta.box_score_final.home}) -- ` +
        `Fox's play-by-play for this game is incomplete or unreliable near the end.`
      : null,
  };
}

/* ---------------------------------------------------------------------
 * contributionBars -- the highest-value chart. One horizontal bar per
 * metric, sorted by weighted contribution descending. Missing (n/a) and
 * real-zero metrics are drawn differently in geometry, never in shade.
 *
 * `uwLossBonus` being present (even as 0) vs. undefined is what switches
 * between two layouts:
 *  - undefined (live so_far/from_here callers, which have no bonus concept)
 *    keeps the original simple layout: one bar per metric + a text footer.
 *  - a number (retrospective/completed-game callers) turns on the richer
 *    layout: a second "contribution" bar per metric (its post-÷weight
 *    share, plotted on one axis shared with the flat bonus so the two are
 *    directly comparable), and -- only when the bonus is actually nonzero,
 *    since otherwise it's just restating the same number twice -- a
 *    subtotal/total pair spelling out the arithmetic.
 * ------------------------------------------------------------------- */
function contributionBars(mount, metricsMap, registry, applicableWeight, uwLossBonus) {
  const rich = uwLossBonus !== undefined;
  const bonus = uwLossBonus || 0;
  const rows = registry.map((m) => ({ ...m, v: metricsMap[m.name] }));
  const applicable = rows.filter((r) => r.v !== null);
  const naRows = rows.filter((r) => r.v === null);
  applicable.sort((a, b) => b.v.weighted - a.v.weighted);
  const ordered = applicable.concat(naRows);

  const weightedSum = applicable.reduce((s, r) => s + r.v.weighted, 0);
  const base = applicableWeight ? weightedSum / applicableWeight : 0;
  const total = base + bonus;
  const excludedNames = naRows.map((r) => r.label).join(", ");
  // Track width is scaled to the heaviest metric's weight, not to
  // applicableWeight (7+ metrics) -- a weight-1.0 metric's box should read
  // as "this is the biggest a bar gets," not a sliver 1/7th of the row.
  // A weight-0.5 metric still renders at half that box, so relative
  // proportion between metrics survives; only the absolute scale changes.
  const maxWeight = Math.max(...registry.map((m) => m.weight));

  const wrap = el("div", {});

  if (rich) {
    const head = el("div", { class: "contrib-row rich contrib-head" });
    head.appendChild(el("div"));
    head.appendChild(el("div", { class: "group-head", text: "score (saturation vs. cap)" }));
    head.appendChild(el("div"));
    head.appendChild(el("div"));
    head.appendChild(el("div", { class: "group-head", text: `contribution (share of ${total.toFixed(3)})` }));
    head.appendChild(el("div"));
    wrap.appendChild(head);
  }

  ordered.forEach((r) => {
    const row = el("div", { class: rich ? "contrib-row rich" : "contrib-row" });
    row.appendChild(el("div", { class: "label", text: r.label }));
    if (r.v === null) {
      row.appendChild(el("div", { class: "na", text: `not applicable — overtime game` }));
      row.appendChild(el("div"));
      if (rich) { row.appendChild(el("div")); row.appendChild(el("div")); }
      row.appendChild(el("div", { class: "contrib-value", text: "—" }));
    } else {
      const trackPct = (r.weight / maxWeight) * 100;
      const meter = el("div", { class: "contrib-meter" });
      meter.style.width = `${trackPct}%`;
      const fill = el("i", { class: rich && r.v.at_cap ? "cap" : "" });
      fill.style.width = `${applicableWeight ? (r.v.weighted / (r.weight)) * 100 : 0}%`;
      meter.appendChild(fill);
      row.title = `raw ${r.v.raw.toFixed(3)} / cap ${r.cap ?? "—"} → norm ${r.v.norm.toFixed(3)} × weight ${r.weight} = ${r.v.weighted.toFixed(3)}. ${r.description}`;

      if (rich) {
        // Rich layout drops the "⚠ AT CAP" chip -- it used to share a flex
        // row with the meter, shrinking the bar to make room for it. A
        // capped bar's fill turns var(--warning) instead (set above), so
        // width stays purely a function of the score.
        row.appendChild(el("div", { class: "meter-cell" }, meter));
        row.appendChild(el("div", { class: "contrib-value", text: `+${r.v.weighted.toFixed(3)}` }));
        row.appendChild(el("div", { class: "op", text: `÷${applicableWeight}` }));
        const contribution = r.v.weighted / applicableWeight;
        const cbar = el("div", { class: "contrib-bar" });
        const cfill = el("i");
        cfill.style.width = `${total > 0 ? (contribution / total) * 100 : 0}%`;
        cbar.appendChild(cfill);
        row.appendChild(cbar);
        row.appendChild(el("div", { class: "contrib-value", text: contribution.toFixed(3) }));
      } else {
        const chip = r.v.at_cap ? el("span", { class: "chip warn", text: "⚠ AT CAP" }) : null;
        row.appendChild(el("div", {}, meter));
        row.appendChild(el("div", {}, chip));
        row.appendChild(el("div", { class: "contrib-value", text: `+${r.v.weighted.toFixed(3)}` }));
      }
    }
    wrap.appendChild(row);
  });

  if (rich && bonus > 0) {
    const row = el("div", { class: "contrib-row rich bonus" });
    row.appendChild(el("div", { class: "label", text: "UW loss bonus" }));
    row.appendChild(el("div", { class: "meter-cell" }, el("span", { class: "chip good", text: "FLAT" })));
    row.appendChild(el("div", { class: "contrib-value", text: `+${bonus.toFixed(3)}` }));
    row.appendChild(el("div", { class: "op", text: "—" }));
    const cbar = el("div", { class: "contrib-bar" });
    const cfill = el("i", { class: "bonus" });
    cfill.style.width = `${total > 0 ? (bonus / total) * 100 : 0}%`;
    cbar.appendChild(cfill);
    row.appendChild(cbar);
    row.appendChild(el("div", { class: "contrib-value", text: `+${bonus.toFixed(3)}` }));
    row.title = "Flat +0.07 whenever Washington loses — deliberate rooting bias, added on top of the weighted composite rather than as one of its metrics.";
    wrap.appendChild(row);

    wrap.appendChild(el("div", { class: "divider" }));

    const subRow = el("div", { class: "contrib-row rich subtotal" });
    subRow.appendChild(el("div", { class: "label", text: "Metrics subtotal" }));
    subRow.appendChild(el("div"));
    subRow.appendChild(el("div"));
    subRow.appendChild(el("div"));
    const subBar = el("div", { class: "contrib-bar" });
    const subFill = el("i");
    subFill.style.width = `${total > 0 ? (base / total) * 100 : 0}%`;
    subBar.appendChild(subFill);
    subRow.appendChild(subBar);
    subRow.appendChild(el("div", { class: "contrib-value", text: base.toFixed(3) }));
    wrap.appendChild(subRow);

    const totalRow = el("div", { class: "contrib-row rich total" });
    totalRow.appendChild(el("div", { class: "label", text: "Total" }));
    totalRow.appendChild(el("div"));
    totalRow.appendChild(el("div"));
    totalRow.appendChild(el("div"));
    const totalBar = el("div", { class: "contrib-bar" });
    const aPct = total > 0 ? (base / total) * 100 : 0;
    const bPct = total > 0 ? (bonus / total) * 100 : 0;
    const segA = el("i", { class: "split-a" });
    segA.style.width = `${aPct}%`;
    const segB = el("i", { class: "split-b" });
    segB.style.width = `${bPct}%`;
    segB.style.left = `${aPct}%`;
    totalBar.appendChild(segA);
    totalBar.appendChild(segB);
    totalRow.appendChild(totalBar);
    totalRow.appendChild(el("div", { class: "contrib-value", text: total.toFixed(3) }));
    wrap.appendChild(totalRow);
  }

  if (rich) {
    if (bonus <= 0) {
      let footerText = `Σ ${weightedSum.toFixed(3)} ÷ applicable weight ${applicableWeight.toFixed(1)} = ${base.toFixed(3)}`;
      if (naRows.length) footerText += ` · ${excludedNames} excluded (overtime)`;
      wrap.appendChild(el("div", { class: "contrib-footer", text: footerText }));
    } else if (naRows.length) {
      wrap.appendChild(el("div", { class: "contrib-footer", text: `${excludedNames} excluded (overtime)` }));
    }
    const legend = el("div", { class: "legend" });
    legend.appendChild(el("span", {}, [el("i", { style: "background:var(--track)" }), "max possible (weight share)"]));
    legend.appendChild(el("span", {}, [el("i", { style: "background:var(--series-1)" }), "actual (saturation)"]));
    if (bonus > 0) legend.appendChild(el("span", {}, [el("i", { style: "background:var(--good)" }), "UW loss bonus (flat)"]));
    legend.appendChild(el("span", {}, [el("i", { style: "background:var(--warning)" }), "at cap"]));
    wrap.appendChild(legend);
  } else {
    const footerText = `Σ ${weightedSum.toFixed(3)} ÷ applicable weight ${applicableWeight.toFixed(1)} = ${base.toFixed(3)}`
      + (naRows.length ? ` · ${excludedNames} excluded (overtime)` : "");
    wrap.appendChild(el("div", { class: "contrib-footer", text: footerText }));
  }

  mount.appendChild(wrap);

  const twinRows = registry.map((r) => {
    const v = metricsMap[r.name];
    if (v === null) return [r.label, "—", String(r.cap ?? "—"), "—", String(r.weight), "—", "—", "no", "no"];
    return [r.label, v.raw.toFixed(4), String(r.cap ?? "—"), v.norm.toFixed(4), String(r.weight),
      v.weighted.toFixed(4), `${((v.weighted / applicableWeight) * 100).toFixed(1)}%`, v.at_cap ? "yes" : "no", "yes"];
  });
  mount.appendChild(tableTwin("Show as table",
    ["Metric", "Raw", "Cap", "Normalized", "Weight", "Weighted", "Share", "At cap", "Applicable"], twinRows));
}

/* ---------------------------------------------------------------------
 * driversWeighted -- scored-game-detail "what drove this score", locked to
 * the Weighted variant (design_handoff_game_detail). One row per metric,
 * sorted by actual contribution (norm x weight) descending -- sorting by
 * norm instead would float the two 0.5-weight metrics above metrics that
 * contributed more. The UW loss bonus draws with its own grammar below a
 * dashed rule: no bar, no track, since it has no weight or cap to plot
 * against. The integrity strip lives in this card (not a separate one),
 * spelling out the same arithmetic the bars just drew.
 * ------------------------------------------------------------------- */
function driversWeighted(mount, metricsMap, registry, si) {
  const RAMP = ["#e6e3d8", "#d6cfc0", "#c5b7a6", "#b69c8b", "#a88170", "#9b6656", "#8d5a4c", "#7d4a41"];
  const bonus = si.uw_loss_bonus || 0;
  const rows = registry
    .map((r) => ({ ...r, v: metricsMap[r.name] }))
    .filter((r) => r.v !== null)
    .sort((a, b) => b.v.weighted - a.v.weighted);
  const maxWeight = Math.max(...registry.map((r) => r.weight));

  const wrap = el("div", { class: "g-drivers-rows" });
  rows.forEach((r, k) => {
    const color = RAMP[k % RAMP.length];
    const weightText = r.weight.toFixed(1).replace(/\.0$/, "");
    const row = el("div", { class: "g-drivers-row" }, [
      el("span", { class: "label", text: r.label }),
      el("span", { class: "weight", text: `×${weightText}` }),
      el("span", { class: "g-drivers-bar" }, [
        el("span", { class: "g-drivers-track", style: `width:${(r.weight / maxWeight) * 100}%` }),
        el("span", { class: "g-drivers-fill", style: `width:${(r.v.weighted / maxWeight) * 100}%; background:${color}` }),
      ]),
      el("span", { class: "value", text: r.v.weighted.toFixed(3) }),
    ]);
    row.title = r.description;
    wrap.appendChild(row);
  });
  mount.appendChild(wrap);

  if (bonus > 0) {
    const uwRow = el("div", { class: "g-uw-row" }, [
      el("span", { class: "label", text: "UW loss" }),
      el("span", { class: "flat", text: "FLAT" }),
      el("span", { class: "note", text: "Added straight to the composite — no weight, no cap, not diluted" }),
      el("span", { class: "value", text: `+${bonus.toFixed(3)}` }),
    ]);
    uwRow.title = "Flat +0.07 whenever Washington loses — deliberate rooting bias, added on top of the weighted composite rather than as one of its metrics.";
    mount.appendChild(uwRow);
  }

  mount.appendChild(el("div", {
    class: "g-drivers-caption",
    text: "Sorted by contribution. Dim track = the most this metric could contribute at its weight; fill = what it actually contributed.",
  }));

  const base = si.composite_recomputed !== null ? si.composite_recomputed - bonus : si.weighted_sum / si.applicable_weight;
  let formula = `Σ contributions ${si.weighted_sum.toFixed(3)} ÷ applicable weight ${si.applicable_weight.toFixed(1)} = ${base.toFixed(4)}`;
  if (bonus) formula += ` + UW loss bonus ${bonus.toFixed(3)} = ${si.composite_recomputed.toFixed(4)}`;
  const verdict = si.matches
    ? el("span", { class: "ok", text: "✓ matches stored score" })
    : el("span", { class: "bad", text: `⚠ differs from stored score by ${si.delta.toFixed(4)} — caps may have changed since this game was scored` });
  mount.appendChild(el("div", { class: "g-integrity-strip" }, [el("span", { text: formula }), verdict]));

  const twinRows = registry.map((r) => {
    const v = metricsMap[r.name];
    if (v === null) return [r.label, "—", String(r.cap ?? "—"), "—", String(r.weight), "—", "no"];
    return [r.label, v.raw.toFixed(4), String(r.cap ?? "—"), v.norm.toFixed(4), String(r.weight), v.weighted.toFixed(4), v.at_cap ? "yes" : "no"];
  });
  mount.appendChild(tableTwin("Show as table",
    ["Metric", "Raw", "Cap", "Normalized", "Weight", "Weighted", "At cap"], twinRows));
}

/* ---------------------------------------------------------------------
 * distributionRail -- "where this sits" rail card. Same 20-bin histogram
 * as /api/analytics' distribution.histogram, but styled as a dim bar field
 * with only the bucket containing this game lit, plus a marker line and
 * label at the composite's exact position (design_handoff_game_detail).
 * ------------------------------------------------------------------- */
function distributionRail(mount, hist, composite) {
  const nBins = hist.bins.length;
  const domainMax = hist.bin_width * nBins;
  const activeIdx = Math.max(0, Math.min(nBins - 1, Math.floor(composite / hist.bin_width)));
  const maxCount = Math.max(1, ...hist.bins);
  const spacing = 300 / nBins;
  const barW = Math.max(spacing - 2, 1);

  const root = svg("svg", { viewBox: "0 0 300 104", role: "img", "aria-label": "Score distribution histogram" });
  hist.bins.forEach((c, k) => {
    const h = (c / maxCount) * 96;
    root.appendChild(svg("rect", {
      x: (k * spacing + 1).toFixed(1), y: (102 - h).toFixed(1), width: barW.toFixed(1), height: h.toFixed(1),
      fill: k === activeIdx ? "var(--g-muted-graphic)" : "var(--track)",
    }));
  });
  const frac = Math.max(0, Math.min(1, composite / domainMax));
  const markerX = (frac * 300).toFixed(1);
  root.appendChild(svg("line", { x1: markerX, y1: 0, x2: markerX, y2: 102, stroke: "var(--g-accent)", "stroke-width": "1.5" }));

  const wrap = el("div", { class: "g-hist-wrap" }, root);
  wrap.appendChild(el("div", { class: "g-hist-marker-row" },
    el("span", { class: "g-hist-marker-label", style: `left:${frac * 100}%`, text: fmtScore(composite) })));
  wrap.appendChild(el("div", { class: "g-hist-axis" }, [el("span", { text: "0.0" }), el("span", { text: "1.0" })]));
  mount.appendChild(wrap);
}

/* ---------------------------------------------------------------------
 * histogram -- 20 bins, used at three sizes. `markers` optionally draws
 * labeled vertical rules (p10/median/p90, or a single game's position).
 * ------------------------------------------------------------------- */
function histogram(mount, hist, opts = {}) {
  const { width: W = 420, height: H = 100, markers = [] } = opts;
  const margin = { top: 8, right: 6, bottom: 16, left: 6 };
  const plotW = W - margin.left - margin.right;
  const plotH = H - margin.top - margin.bottom;
  const nBins = hist.bins.length;
  const maxCount = Math.max(1, ...hist.bins);
  const barW = plotW / nBins;
  const y = linScale([0, maxCount], [margin.top + plotH, margin.top]);
  const domainMax = hist.bin_width * nBins;

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Score distribution histogram" });
  hist.bins.forEach((c, i) => {
    const bx = margin.left + i * barW;
    const bh = margin.top + plotH - y(c);
    const rect = svg("rect", {
      x: (bx + 1).toFixed(1), y: y(c).toFixed(1), width: Math.max(barW - 2, 1).toFixed(1), height: bh.toFixed(1),
      fill: "var(--series-1)", rx: "2",
    });
    rect.addEventListener("pointermove", (ev) => {
      const lo = (i * hist.bin_width).toFixed(3), hi = ((i + 1) * hist.bin_width).toFixed(3);
      showTooltip(`<div class="tt-value">${c} games</div><div class="tt-sub">${lo}–${hi}</div>`, ev.clientX, ev.clientY);
    });
    rect.addEventListener("pointerleave", hideTooltip);
    root.appendChild(rect);
  });
  markers.forEach((m) => {
    const mx = margin.left + (m.value / domainMax) * plotW;
    root.appendChild(svg("line", {
      x1: mx.toFixed(1), x2: mx.toFixed(1), y1: margin.top, y2: margin.top + plotH,
      stroke: "var(--axis)", "stroke-width": "1", "vector-effect": "non-scaling-stroke",
    }));
    if (m.label) {
      root.appendChild(svgText(mx, H - 3, m.label, { "font-size": "9", fill: "var(--ink-muted)", "text-anchor": "middle" }));
    }
  });
  mount.appendChild(el("div", { class: "viz" }, root));
}

/* ---------------------------------------------------------------------
 * groupedBars -- weight vs delivered contribution. Two series, one axis
 * (both are a share of the same weight budget, so a dual axis would lie).
 * ------------------------------------------------------------------- */
function groupedBars(mount, rows) {
  const sorted = rows.slice().sort((a, b) => b.delivered_share - a.delivered_share);
  const W = 720, H = 30 * sorted.length + 30;
  const margin = { top: 10, right: 60, bottom: 10, left: 130 };
  const plotW = W - margin.left - margin.right;
  const maxShare = Math.max(...sorted.map((r) => Math.max(r.designed_share, r.delivered_share))) * 1.15;
  const x = linScale([0, maxShare], [0, plotW]);

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Designed weight vs delivered contribution" });
  sorted.forEach((r, i) => {
    const rowY = margin.top + i * 30;
    root.appendChild(svgText(margin.left - 8, rowY + 20, r.label, { "font-size": "11.5", fill: "var(--ink-2)", "text-anchor": "end" }));
    const bar1 = svg("rect", { x: margin.left, y: rowY, width: x(r.designed_share).toFixed(1), height: 11, fill: "var(--series-1)", rx: "2" });
    const bar2 = svg("rect", { x: margin.left, y: rowY + 13, width: x(r.delivered_share).toFixed(1), height: 11, fill: "var(--series-2)", rx: "2" });
    [bar1, bar2].forEach((b, bi) => {
      b.addEventListener("pointermove", (ev) => {
        const share = bi === 0 ? r.designed_share : r.delivered_share;
        const kind = bi === 0 ? "Designed weight share" : "Delivered share of composite";
        showTooltip(`<div class="tt-value">${(share * 100).toFixed(1)}%</div><div class="tt-sub">${kind} · ${r.label}</div>`, ev.clientX, ev.clientY);
      });
      b.addEventListener("pointerleave", hideTooltip);
    });
    root.appendChild(bar1);
    root.appendChild(bar2);
    root.appendChild(svgText(margin.left + x(r.designed_share) + 4, rowY + 10, `${(r.designed_share * 100).toFixed(1)}%`, { "font-size": "9.5", fill: "var(--ink-muted)" }));
    root.appendChild(svgText(margin.left + x(r.delivered_share) + 4, rowY + 22, `${(r.delivered_share * 100).toFixed(1)}%`, { "font-size": "9.5", fill: "var(--ink-muted)" }));
  });

  const legend = el("div", { class: "chart-key" }, [
    el("span", { class: "k" }, [el("span", { class: "line", style: "background:var(--series-1)" }), "Designed weight share"]),
    el("span", { class: "k" }, [el("span", { class: "line", style: "background:var(--series-2)" }), "Delivered share of composite"]),
  ]);
  mount.appendChild(legend);
  mount.appendChild(el("div", { class: "viz" }, root));
  mount.appendChild(tableTwin("Show as table", ["Metric", "Weight", "Designed share", "Delivered share", "Delta"],
    sorted.map((r) => [r.label, String(r.weight), `${(r.designed_share * 100).toFixed(1)}%`, `${(r.delivered_share * 100).toFixed(1)}%`, `${(r.delta * 100).toFixed(1)}%`])));
}

/* ---------------------------------------------------------------------
 * multiLine -- mean score by week, one line per season.
 * ------------------------------------------------------------------- */
function multiLine(mount, byWeek) {
  const seasons = Object.keys(byWeek).sort();
  const W = 720, H = 220;
  const margin = { top: 14, right: 50, bottom: 26, left: 40 };
  const plotW = W - margin.left - margin.right;
  const plotH = H - margin.top - margin.bottom;
  const allWeeks = [].concat(...seasons.map((s) => byWeek[s].map((r) => r.week)));
  const maxWeek = Math.max(...allWeeks, 1);
  const allScores = [].concat(...seasons.map((s) => byWeek[s].map((r) => r.avg_score)));
  const maxScore = Math.max(...allScores, 0.01) * 1.15;
  const x = linScale([1, maxWeek], [margin.left, margin.left + plotW]);
  const y = linScale([0, maxScore], [margin.top + plotH, margin.top]);
  const colors = ["var(--series-1)", "var(--series-2)"];

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Mean watchability score by week" });
  [0, 0.25, 0.5, 0.75, 1].forEach((f) => {
    const v = maxScore * f;
    root.appendChild(svg("line", { x1: margin.left, x2: margin.left + plotW, y1: y(v).toFixed(1), y2: y(v).toFixed(1), stroke: "var(--grid)", "stroke-width": "1", "vector-effect": "non-scaling-stroke" }));
  });

  seasons.forEach((s, si) => {
    const rows = byWeek[s].slice().sort((a, b) => a.week - b.week);
    const path = rows.map((r, i) => `${i === 0 ? "M" : "L"}${x(r.week).toFixed(1)},${y(r.avg_score).toFixed(1)}`).join(" ");
    root.appendChild(svg("path", { d: path, fill: "none", stroke: colors[si % 2], "stroke-width": "2", "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round" }));
    rows.forEach((r) => {
      const c = svg("circle", { cx: x(r.week).toFixed(1), cy: y(r.avg_score).toFixed(1), r: "4", fill: colors[si % 2], stroke: "var(--surface-1)", "stroke-width": "2" });
      c.addEventListener("pointermove", (ev) => showTooltip(`<div class="tt-value">${r.avg_score.toFixed(3)}</div><div class="tt-sub">${s} week ${r.week} · n=${r.n}</div>`, ev.clientX, ev.clientY));
      c.addEventListener("pointerleave", hideTooltip);
      root.appendChild(c);
    });
    const last = rows[rows.length - 1];
    if (last) root.appendChild(svgText(x(last.week) + 6, y(last.avg_score) + 3, s, { "font-size": "11", fill: colors[si % 2], "font-weight": "600" }));
  });

  mount.appendChild(el("div", { class: "chart-key" }, seasons.map((s, si) =>
    el("span", { class: "k" }, [el("span", { class: "line", style: `background:${colors[si % 2]}` }), s]))));
  mount.appendChild(el("div", { class: "viz" }, root));
  const twinRows = [];
  seasons.forEach((s) => byWeek[s].forEach((r) => twinRows.push([s, String(r.week), r.avg_score.toFixed(4), String(r.n)])));
  mount.appendChild(tableTwin("Show as table", ["Season", "Week", "Mean score", "N"], twinRows));
}

/* ---------------------------------------------------------------------
 * heatmap -- diverging correlation matrix. Values printed in every cell
 * (a correlation matrix is its own table view -- the legitimate exception
 * to "no number on every point").
 * ------------------------------------------------------------------- */
function heatmap(mount, correlation) {
  const labels = correlation.labels;
  const nCells = labels.length;
  const cell = 62, gap = 2, labelW = 130;
  const W = labelW + nCells * (cell + gap), H = labelW + nCells * (cell + gap);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Metric correlation matrix" });

  function color(r) {
    if (r === null) return "var(--grid)";
    const t = Math.max(-1, Math.min(1, r));
    if (t >= 0) return `color-mix(in srgb, var(--div-mid), var(--div-pos) ${Math.round(t * 100)}%)`;
    return `color-mix(in srgb, var(--div-mid), var(--div-neg) ${Math.round(-t * 100)}%)`;
  }

  labels.forEach((lab, i) => {
    root.appendChild(svgText(labelW - 6, labelW + i * (cell + gap) + cell / 2 + 4, lab, { "font-size": "10.5", fill: "var(--ink-2)", "text-anchor": "end" }));
    const tx = labelW + i * (cell + gap) + cell / 2;
    const g = svg("g", { transform: `translate(${tx}, ${labelW - 6}) rotate(-45)` });
    g.appendChild(svgText(0, 0, lab, { "font-size": "10.5", fill: "var(--ink-2)", "text-anchor": "start" }));
    root.appendChild(g);
  });

  for (let i = 0; i < nCells; i++) {
    for (let j = 0; j < nCells; j++) {
      const r = correlation.matrix[i][j];
      const n = correlation.n_matrix[i][j];
      const cx = labelW + j * (cell + gap), cy = labelW + i * (cell + gap);
      const rect = svg("rect", { x: cx, y: cy, width: cell, height: cell, fill: color(r), rx: "2" });
      rect.addEventListener("pointermove", (ev) => showTooltip(
        `<div class="tt-value">r = ${r === null ? "—" : r.toFixed(3)}</div><div class="tt-sub">${labels[i]} × ${labels[j]} · n=${n}</div>`,
        ev.clientX, ev.clientY));
      rect.addEventListener("pointerleave", hideTooltip);
      root.appendChild(rect);
      if (r !== null) {
        const luminance = 0.5 + 0.5 * Math.abs(r); // rough: darker fill -> lighter text
        root.appendChild(svgText(cx + cell / 2, cy + cell / 2 + 4, r.toFixed(2), {
          "font-size": "10", "text-anchor": "middle",
          fill: Math.abs(r) > 0.5 ? "#fff" : "var(--ink-1)",
        }));
      }
    }
  }
  mount.appendChild(el("div", { class: "heatmap-wrap" }, el("div", { class: "viz" }, root)));
  const twinRows = [];
  for (let i = 0; i < nCells; i++) for (let j = i + 1; j < nCells; j++) {
    twinRows.push([labels[i], labels[j], correlation.matrix[i][j]?.toFixed(4) ?? "—", String(correlation.n_matrix[i][j])]);
  }
  mount.appendChild(tableTwin("Show as table", ["Metric A", "Metric B", "r", "N"], twinRows));
}

/* ---------------------------------------------------------------------
 * weeklyPeaksChart -- Top Games "Weekly peaks" card (design_handoff_top_games).
 * One polyline per season, fixed 0.55-0.90 y domain so seasons stay
 * honestly comparable, weeks positioned by index within the union of
 * rendered weeks (not by raw week number, so a bye/missing week in one
 * season doesn't skew another's spacing). Per the handoff this chart has
 * no hover state -- unlike every other chart on this site, Top Games
 * deliberately gives hover only to nav/filters/rows, not chart elements --
 * and per the handoff's chart-label note, all text lives in the HTML
 * overlay at real px since the SVG uses preserveAspectRatio="none".
 * `byWeek` is {season: [{week, watchability_score}, ...]}.
 * ------------------------------------------------------------------- */
function weeklyPeaksChart(mount, byWeek) {
  const seasons = Object.keys(byWeek).sort();
  const PAD_L = 46, RIGHT = 988, TOP = 14, BASELINE = 190, INSET = 24;
  const usableW = RIGHT - PAD_L - 48;
  const yMin = 0.55, yMax = 0.90;

  const weekSet = new Set();
  seasons.forEach((s) => byWeek[s].forEach((r) => weekSet.add(r.week)));
  const weeks = Array.from(weekSet).sort((a, b) => a - b);
  const n = weeks.length;
  const weekIdx = {};
  weeks.forEach((w, i) => (weekIdx[w] = i));

  const xOf = (i) => (n > 1 ? PAD_L + INSET + (usableW * i) / (n - 1) : PAD_L + INSET + usableW / 2);
  const groupW = n > 1 ? usableW / (n - 1) : usableW;
  const yOf = (v) => {
    const c = Math.max(yMin, Math.min(yMax, v));
    return BASELINE - ((c - yMin) / (yMax - yMin)) * (BASELINE - TOP);
  };

  const NEWEST_COLORS = ["#e6e3d8", "var(--g-line-2)", "var(--g-ink-dim)"];
  const seasonColor = (rankFromNewest) => (rankFromNewest < NEWEST_COLORS.length ? NEWEST_COLORS[rankFromNewest] : "var(--g-border-strong2)");

  const root = svg("svg", { viewBox: "0 0 1000 250", preserveAspectRatio: "none", role: "img", "aria-label": "Weekly peak watchability score by season" });

  weeks.forEach((w, i) => {
    if (i % 2 !== 1) return;
    root.appendChild(svg("rect", {
      x: (xOf(i) - groupW / 2).toFixed(1), y: TOP, width: groupW.toFixed(1), height: (BASELINE - TOP).toFixed(1),
      fill: "rgba(255,255,255,0.012)",
    }));
  });
  [0.9, 0.8, 0.7, 0.6].forEach((v) => {
    root.appendChild(svg("line", { x1: PAD_L, x2: RIGHT, y1: yOf(v).toFixed(1), y2: yOf(v).toFixed(1), stroke: "var(--g-border)", "stroke-width": "1" }));
  });
  root.appendChild(svg("line", { x1: PAD_L, x2: RIGHT, y1: BASELINE, y2: BASELINE, stroke: "var(--g-border-strong2)", "stroke-width": "1" }));

  const overlay = el("div", { class: "t-peaks-plot" }, root);

  const seasonsNewestFirst = seasons.slice().reverse();
  seasonsNewestFirst.forEach((s, rankFromNewest) => {
    const rows = byWeek[s].slice().sort((a, b) => a.week - b.week);
    if (!rows.length) return;
    const color = seasonColor(rankFromNewest);
    const points = rows.map((r) => `${xOf(weekIdx[r.week]).toFixed(1)},${yOf(r.watchability_score).toFixed(1)}`).join(" ");
    root.appendChild(svg("polyline", {
      points, fill: "none", stroke: color, "stroke-width": rankFromNewest === 0 ? "1.8" : "1.4",
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    let peak = rows[0];
    rows.forEach((r) => { if (r.watchability_score > peak.watchability_score) peak = r; });
    rows.forEach((r) => {
      const isPeak = r === peak;
      root.appendChild(svg("circle", {
        cx: xOf(weekIdx[r.week]).toFixed(1), cy: yOf(r.watchability_score).toFixed(1),
        r: isPeak ? "3.4" : "2", fill: isPeak ? "var(--g-accent)" : color,
        stroke: "var(--g-card)", "stroke-width": "1",
      }));
    });
    overlay.appendChild(el("div", {
      class: "t-peaks-callout",
      style: `left:${(xOf(weekIdx[peak.week]) / 1000) * 100}%;top:${(yOf(peak.watchability_score) / 250) * 100}%`,
      text: `${s} · ${peak.watchability_score.toFixed(3)}`,
    }));
  });

  [0.9, 0.8, 0.7, 0.6, 0.55].forEach((v) => {
    overlay.appendChild(el("div", { class: "t-peaks-ylabel", style: `top:${(yOf(v) / 250) * 100}%`, text: v.toFixed(2) }));
  });
  weeks.forEach((w, i) => {
    overlay.appendChild(el("div", { class: "t-peaks-xlabel", style: `left:${(xOf(i) / 1000) * 100}%`, text: String(w) }));
  });

  mount.appendChild(overlay);
  mount.appendChild(el("div", { class: "t-peaks-caption", text: "One point per week: the single highest composite of that week. Oxblood marks each season's highest week. Postseason weeks are excluded from this view." }));
}
