/* Shared across all four pages: fetch wrapper, formatters, URL-state helpers,
 * the tooltip singleton, and the theme toggle. Each page's own <script> reads
 * `document.body.dataset.page` and calls the matching init function below. */

const API = "/api";

async function api(path, params) {
  const url = new URL(API + path, location.origin);
  const merged = { ...(params || {}) };
  for (const [k, v] of Object.entries(merged)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, item));
    else url.searchParams.set(k, v);
  }
  const res = await fetch(url);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
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
function fmtKickoff(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
}
function fmtMatchup(g) {
  const away = g.away.rank ? `#${g.away.rank} ${g.away.name}` : g.away.name;
  const home = g.home.rank ? `#${g.home.rank} ${g.home.name}` : g.home.name;
  // A live game has a real, meaningful running score just like a completed
  // one -- only an unstarted ('pre') game has no score worth printing.
  if ((g.completed || g.status_state === "in") && g.away.score !== null && g.home.score !== null) {
    return `${away} ${g.away.score} at ${home} ${g.home.score}`;
  }
  return `${away} at ${home}`;
}
// Same away/home split as fmtMatchup, but as two pieces instead of one
// string -- lets a caller keep each side unbreakable (nowrap) while still
// allowing the line to wrap between them on narrow screens, instead of the
// whole matchup running off the edge of the frame.
function fmtMatchupParts(g) {
  const away = g.away.rank ? `#${g.away.rank} ${g.away.name}` : g.away.name;
  const home = g.home.rank ? `#${g.home.rank} ${g.home.name}` : g.home.name;
  if ((g.completed || g.status_state === "in") && g.away.score !== null && g.home.score !== null) {
    return { away: `${away} ${g.away.score}`, home: `at ${home} ${g.home.score}` };
  }
  return { away, home: `at ${home}` };
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
  // Sponsor suffix appears as both spelled-out ("Presented by Prudential")
  // and abbreviated ("Pres. by AT&T", "pres. by Prudential") depending on
  // season -- strip either so it doesn't leak into the bowl name below.
  const note = g.event_note.replace(/\s+(presented|pres\.?)\s+by\b.*$/i, "").trim();
  // 4-team era (2022/2023) notes are the short form "CFP <round> ..."; the
  // 12-team era (2024+) spells out "College Football Playoff <round> ...".
  // Missing the short form here left those games' `short` falling through
  // to the raw, unabbreviated note below -- long enough to blow out the
  // fixed-width mobile grid column it renders in (see Games page mobile
  // layout, gm-wk).
  const isCFP = /college football playoff|\bcfp\b/i.test(note);
  if (!isCFP) return { isCFP: false, label: note, short: `'${String(g.season_year).slice(-2)} ${note}` };
  const roundMatch = note.match(/First Round|Quarterfinal|Semifinal|National Championship/i);
  const round = roundMatch ? roundMatch[0] : "Playoff";
  const bowlMatch = note.match(/at the (.+)$/i);
  // A handful of notes append a qualifier after the bowl name itself (e.g.
  // "Allstate Sugar Bowl - Rescheduled from Jan 1") -- drop it so it doesn't
  // balloon `short`/`label` the same way the missing short-CFP-form case did.
  const bowl = bowlMatch ? bowlMatch[1].replace(/\s+-\s+.*$/, "").trim() : null;
  const abbr = CFP_ROUND_ABBR[round] || round;
  const yy = String(g.season_year).slice(-2);
  return {
    isCFP: true,
    round,
    bowl,
    label: bowl ? `CFP ${round} · ${bowl}` : `CFP ${round}`,
    // "'YY CFP - X" (X = R1/QF/SF/NCG), matching the "'YY wk X" format used
    // for regular-season games elsewhere -- drops the bowl name that used
    // to sit here so playoff and regular-season rows read consistently.
    short: `'${yy} CFP - ${abbr}`,
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

// ---- week lookup (shared by index.html's Week filter and the spoiler
// popover's "This week" section, so there's one implementation of "which
// weeks exist for this season/type" instead of two) --------------------------

function weeksForSeason(meta, season, seasonType) {
  const weeks = new Set();
  if (season && seasonType && seasonType !== "all") {
    (meta.weeks[`${season}:${seasonType}`] || []).forEach((w) => weeks.add(w));
  } else if (season) {
    ["2", "3"].forEach((st) => (meta.weeks[`${season}:${st}`] || []).forEach((w) => weeks.add(w)));
  } else {
    Object.values(meta.weeks).forEach((arr) => arr.forEach((w) => weeks.add(w)));
  }
  return Array.from(weeks).sort((a, b) => a - b);
}

// ---- chips ------------------------------------------------------------------

function gameChips(g) {
  const chips = [];
  // UW loss sorts first and is the one non-neutral chip -- g.uw_loss_bonus
  // is only ever 0 or UW_LOSS_BONUS (scoring.uw_loss_bonus), so truthiness
  // alone is "Washington played and lost," no team-id check needed here.
  if (g.uw_loss_bonus) chips.push(["UW LOSS", "uw"]);
  if (g.spoiler_hidden) chips.push(["HIDDEN", "muted"]);
  if (g.status_state === "in") chips.push(["LIVE", "accent"]);
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
  // "NOT SCORED" means "this game is done and we have nothing" -- a live or
  // not-yet-started game is unscored by design, not by gap, so it gets the
  // LIVE chip (above) or no chip at all instead of this one. A spoiler-
  // hidden game also has a null watchability_score, but it very much HAS
  // been scored -- the HIDDEN chip already covers that case, so exclude it
  // here rather than showing the misleading claim that nothing exists yet.
  if (g.watchability_score === null && g.status_state === "post" && !g.spoiler_hidden) {
    chips.push(["NOT SCORED", "muted"]);
  }
  return chips;
}
function renderChips(g) {
  return gameChips(g).map(([label, cls]) => el("span", { class: `chip ${cls}`, text: label }));
}

// live_metrics rows come back from the API as {raw, normalized, weight,
// applicable} -- adapt to the {raw, norm, weighted, at_cap} shape
// contributionBars() expects (the same shape /api/games' retrospective
// metrics map produces), so one chart function renders both without
// modification. Shared by game.html (a single live game's two halves) and
// slate.html (every live game's expand row).
function adaptLiveMetrics(metricsObj) {
  const out = {};
  for (const [name, v] of Object.entries(metricsObj || {})) {
    out[name] = v.applicable
      ? { raw: v.raw, norm: v.normalized, weighted: v.normalized * v.weight, at_cap: v.normalized >= 1.0 }
      : null;
  }
  return out;
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

// ---- account menu (logout) + mobile nav drawer ------------------------------
// Only reached on pages that carry <header class="chrome"> -- login.html/
// signup.html don't include app.js at all (see serve.py's login-wall
// docstring), so there's no unauthenticated path where this fires and hits
// a 401 from /api/me.
//
// The mobile nav drawer (designed once on the Slate mobile handoff, "1a
// drawer wins", reused verbatim on Game Detail/Games/Top games) is one
// shared markup+JS pattern rather than four copies: each of those four
// pages carries the same `#mnav-bar`/`#mnav-trig`/`#mnav-chev`/`#mnav-drawer`
// skeleton in its HTML, and initMobileNav() below populates the drawer's
// nav items straight off the desktop <nav> already in the DOM (so it can
// never drift from the desktop links) and wires the open/close toggle.
// initAccountMenu() feeds the same single /me fetch into both the existing
// desktop account-menu AND the mobile drawer's account block, rather than
// fetching it twice.

async function initAccountMenu() {
  const header = document.querySelector("header.chrome");
  const drawer = document.getElementById("mnav-drawer");
  if (!header && !drawer) return;
  let me;
  try {
    me = await api("/me");
  } catch (e) {
    return;
  }
  // Feed nav link (desktop <nav> + its initMobileNav()-cloned drawer copy,
  // both carrying data-admin-only) -- one selector covers both since
  // initMobileNav() already ran by the time this async fetch resolves.
  // CSS hides [data-admin-only] by default (display:none) so there's no
  // flash of it before this runs; an admin gets it explicitly revealed
  // below (there's no other CSS rule for JS to "fall back" to), a
  // non-admin gets it removed outright. Either way a non-admin still gets
  // a 403 from the routes themselves (see serve.py's _require_admin) if
  // they hit the URL directly.
  document.querySelectorAll("[data-admin-only]").forEach((node) => {
    if (me.is_admin) node.style.display = node.classList.contains("mnav-item") ? "flex" : "inline";
    else node.remove();
  });
  const logout = async () => {
    try { await apiPost("/logout", {}); } catch (e) { /* ignore -- redirect anyway */ }
    location.href = "/login.html";
  };
  if (header) {
    const logoutBtn = el("button", { class: "reset", text: "Log out" });
    logoutBtn.addEventListener("click", logout);
    header.appendChild(el("div", { class: "account-menu" }, [
      el("span", { class: "account-name", text: me.username }),
      logoutBtn,
    ]));
  }
  if (drawer) {
    drawer.appendChild(el("div", { class: "mnav-acct", text: me.username }));
    const outBtn = el("button", { class: "mnav-out", type: "button", text: "Log out" });
    outBtn.addEventListener("click", logout);
    drawer.appendChild(outBtn);
  }
}

function initMobileNav() {
  const bar = document.getElementById("mnav-bar");
  const drawer = document.getElementById("mnav-drawer");
  const trig = document.getElementById("mnav-trig");
  const chev = document.getElementById("mnav-chev");
  const navLinks = document.querySelectorAll("header.chrome nav a");
  if (!drawer || !trig || !navLinks.length) return;
  navLinks.forEach((a) => {
    const item = el("a", {
      class: "mnav-item" + (a.getAttribute("aria-current") === "page" ? " on" : ""),
      href: a.getAttribute("href"),
      text: a.textContent,
    });
    // Carried onto the clone so initAccountMenu()'s later admin check can
    // find and remove it from the drawer the same way it does the desktop
    // copy -- see that function for why the removal has to wait for /me.
    if (a.hasAttribute("data-admin-only")) item.setAttribute("data-admin-only", "");
    drawer.appendChild(item);
  });
  // Inserted as a child of .mnav-bar (before the drawer), not document.body --
  // .mnav-bar is `position:fixed` and establishes its own stacking context,
  // so a same-z-index-scale sibling of .mnav-bar (like a body-level backdrop)
  // compares against .mnav-bar's OWN z-index (30), not the drawer's (40)
  // nested inside it -- painting over the whole bar+drawer group regardless
  // of the drawer's nominally higher z-index. Keeping the backdrop inside
  // .mnav-bar puts it in the same stacking context as the drawer, so 35 vs
  // 40 compares correctly; `position:fixed` still covers the full viewport
  // either way since .mnav-bar has no transform.
  const backdrop = el("div", { class: "mnav-backdrop" });
  bar.insertBefore(backdrop, drawer);
  function setOpen(open) {
    drawer.hidden = !open;
    trig.setAttribute("aria-expanded", String(open));
    if (chev) chev.classList.toggle("up", open);
    backdrop.classList.toggle("on", open);
  }
  trig.addEventListener("click", () => setOpen(drawer.hidden));
  backdrop.addEventListener("click", () => setOpen(false));
}

document.addEventListener("DOMContentLoaded", () => {
  initMobileNav();
  initAccountMenu();
});
