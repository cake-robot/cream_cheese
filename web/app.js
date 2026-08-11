/* Shared across all four pages: fetch wrapper, formatters, URL-state helpers,
 * the tooltip singleton, and the theme toggle. Each page's own <script> reads
 * `document.body.dataset.page` and calls the matching init function below. */

const API = "/api";

async function api(path, params) {
  const url = new URL(API + path, location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, item));
      else url.searchParams.set(k, v);
    }
  }
  const res = await fetch(url);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

// ---- formatters -----------------------------------------------------------

function fmtScore(v) {
  return v === null || v === undefined ? "—" : v.toFixed(3);
}
function fmtRaw(v, digits = 3) {
  return v === null || v === undefined ? "—" : v.toFixed(digits);
}
function fmtPct(v) {
  return v === null || v === undefined ? "—" : `${v}${ordinalSuffix(v)}`;
}
function ordinalSuffix(n) {
  const j = n % 10, k = n % 100;
  if (j === 1 && k !== 11) return "st";
  if (j === 2 && k !== 12) return "nd";
  if (j === 3 && k !== 13) return "rd";
  return "th";
}
function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
function fmtMatchup(g) {
  const away = g.away.rank ? `#${g.away.rank} ${g.away.name}` : g.away.name;
  const home = g.home.rank ? `#${g.home.rank} ${g.home.name}` : g.home.name;
  if (g.completed && g.away.score !== null && g.home.score !== null) {
    return `${away} ${g.away.score} at ${home} ${g.home.score}`;
  }
  return `${away} at ${home}`;
}
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

// ---- URL state --------------------------------------------------------------

function getParams() {
  return Object.fromEntries(new URLSearchParams(location.search));
}
function setParams(patch, { push = false } = {}) {
  const params = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined || v === null || v === "" || v === "all" || v === "any") params.delete(k);
    else params.set(k, v);
  }
  const url = `${location.pathname}?${params.toString()}`;
  if (push) history.pushState(null, "", url);
  else history.replaceState(null, "", url);
}

// ---- postseason labeling ----------------------------------------------------

const CFP_ROUND_ABBR = { "First Round": "R1", "Quarterfinal": "QF", "Semifinal": "SF", "National Championship": "NCG" };

// ESPN's `event_note` is the per-game branded-event headline (e.g. "College
// Football Playoff Quarterfinal at the Rose Bowl Presented by Prudential",
// or plain "Duke's Mayo Bowl" for a non-playoff bowl). Every postseason game
// is stored with week=1 -- ESPN has no real week numbering once the regular
// season ends -- so this is what actually distinguishes one postseason game
// from another. New Year's Six bowls (Rose, Sugar, Orange, Cotton, Peach,
// Fiesta) host CFP quarterfinals/semifinals on a rotating basis, so the same
// bowl name can appear either as a plain bowl or as a CFP round depending on
// the season -- `isCFP` is what should drive the distinct-from-bowls styling,
// not the bowl name itself.
function postseasonInfo(g) {
  if (g.season_type !== 3 || !g.event_note) return null;
  const note = g.event_note.replace(/\s+Presented by\b.*$/i, "").trim();
  const isCFP = /college football playoff/i.test(note);
  if (!isCFP) return { isCFP: false, label: note, short: note };
  const roundMatch = note.match(/First Round|Quarterfinal|Semifinal|National Championship/i);
  const round = roundMatch ? roundMatch[0] : "Playoff";
  const bowlMatch = note.match(/at the (.+)$/i);
  const bowl = bowlMatch ? bowlMatch[1].trim() : null;
  const abbr = CFP_ROUND_ABBR[round] || round;
  return {
    isCFP: true,
    round,
    bowl,
    label: bowl ? `CFP ${round} · ${bowl}` : `CFP ${round}`,
    short: bowl ? `${abbr} · ${bowl}` : abbr,
  };
}

// Full "<year> <postseason detail | week N>" string for detail views.
function seasonWeekLabel(g) {
  const pi = postseasonInfo(g);
  if (pi) return `${g.season_year} · ${pi.label}`;
  if (g.season_type === 3) return `${g.season_year} · postseason`;
  return `${g.season_year} week ${g.week ?? "—"}`;
}

// g.venue_name and a CFP bowl host (e.g. the Rose Bowl is both the stadium
// and the event name) can be the literal same string -- suppress the venue
// so it isn't printed twice in a row.
function venueLabel(g) {
  const pi = postseasonInfo(g);
  if (pi && pi.bowl && g.venue_name && pi.bowl.toLowerCase() === g.venue_name.toLowerCase()) return null;
  return g.venue_name || null;
}

// ---- chips ------------------------------------------------------------------

function gameChips(g) {
  const chips = [];
  if (g.ot) chips.push(["OT", "warn"]);
  if (postseasonInfo(g)?.isCFP) chips.push(["CFP", "accent"]);
  if (g.conference_game) chips.push(["CONF", "muted"]);
  if (g.neutral_site) chips.push(["NEU", "muted"]);
  // Deliberately separate: a real Fox-derived value substitution vs. a
  // hand-verified manual override are different provenance, and a game can
  // have either, both, or neither -- collapsing them into one "FOX" chip
  // wrongly claims Fox involvement on games only ever fixed by hand.
  if (g.has_fox_correction) chips.push(["FOX", "muted"]);
  if (g.has_manual_correction) chips.push(["MANUAL", "muted"]);
  if (g.watchability_score === null) chips.push(["NOT SCORED", "muted"]);
  return chips;
}
function renderChips(g) {
  return gameChips(g).map(([label, cls]) => el("span", { class: `chip ${cls}`, text: label }));
}

// ---- tooltip singleton --------------------------------------------------------

let tooltipEl = null;
function tooltip() {
  if (!tooltipEl) {
    tooltipEl = el("div", { id: "viz-tooltip", role: "tooltip" });
    document.body.appendChild(tooltipEl);
  }
  return tooltipEl;
}
function showTooltip(html, x, y) {
  const t = tooltip();
  t.innerHTML = html;
  t.style.display = "block";
  const pad = 14;
  let left = x + pad, top = y + pad;
  const rect = t.getBoundingClientRect();
  if (left + rect.width > window.innerWidth - 8) left = x - rect.width - pad;
  if (top + rect.height > window.innerHeight - 8) top = y - rect.height - pad;
  t.style.left = `${left}px`;
  t.style.top = `${top}px`;
}
function hideTooltip() {
  if (tooltipEl) tooltipEl.style.display = "none";
}

// ---- theme toggle -------------------------------------------------------------

function initThemeToggle() {
  const box = document.getElementById("theme-toggle");
  if (!box) return;
  const current = localStorage.getItem("theme") || "system";
  const apply = (mode) => {
    if (mode === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", mode);
    localStorage.setItem("theme", mode);
    box.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  };
  box.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => apply(b.dataset.mode));
  });
  apply(current);
}

// ---- shared table-twin builder (non-chart tables reuse this too) --------------

function tableTwin(summaryText, headers, rows) {
  const table = el("table", {}, [
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", { text: h })))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c) => el("td", { text: c }))))),
  ]);
  return el("details", { class: "table-twin" }, [
    el("summary", { text: summaryText }),
    el("div", { class: "twin-scroll" }, table),
  ]);
}

document.addEventListener("DOMContentLoaded", () => {
  initThemeToggle();
});
