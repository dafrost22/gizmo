// Gizmo's console. Live state comes from /api/state (polled); each view fetches the rest when it opens.
const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
let S = null; // /api/state
let P = null; // /api/personalities
let V = null; // /api/servers
let G = null; // /api/settings
let H = null; // /api/health
let tab = "talk";
let selTurn = null; // the turn whose trace shows (null: the latest)
let pKey = null; // the personality in the editor ("+new" for one not saved yet)
let sSel = null; // what the Servers view shows: {server}, {form}, {skill} or null
let mDraft = null; // the model provider picked in Settings but not saved yet
let cap = null; // the reply being spoken: /api/state's captions, each word timed, and when they arrived here

const NEW = "+new";
const SEP_URL = "https://modelcontextprotocol.io/seps/2640-skills-extension";

function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const kb = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} kB` : `${(n / 1048576).toFixed(1)} MB`);
const secs = (ms) => (ms == null ? "" : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`);

function toast(text, bad = false) {
    const el = document.createElement("div");
    el.className = `toast${bad ? " bad" : ""}`;
    el.textContent = text;
    $("toasts").append(el);
    setTimeout(() => el.remove(), bad ? 6500 : 3200);
}

// Asks before anything hard to undo, in the page's own <dialog>: embedded browsers (Reachy Mini Control's panel,
// VS Code's Simple Browser) block confirm(), which then answers "cancel" without showing anything.
function ask(title, detail = "", ok = "OK") {
    const d = $("ask");
    $("ask-title").textContent = title;
    $("ask-detail").textContent = detail;
    $("ask-detail").hidden = !detail;
    $("ask-ok").textContent = ok;
    d.returnValue = "";
    d.showModal();
    return new Promise((resolve) => d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true }));
}
$("ask").onclick = (ev) => { if (ev.target === $("ask")) $("ask").close(); }; // a click outside the box cancels

async function call(method, path, body, { quiet = false, raw = false } = {}) {
    let resp;
    try {
        resp = await fetch(path, {
            method,
            headers: body !== undefined ? { "Content-Type": "application/json" } : {},
            body: body !== undefined ? JSON.stringify(body) : undefined,
        });
    } catch (e) {
        if (!quiet) toast(`Gizmo didn't answer (${e.message})`, true);
        throw e;
    }
    if (raw && resp.ok) return resp;
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        const msg = data.detail || `${path}: ${resp.status}`;
        if (!quiet) toast(msg, true);
        throw new Error(msg);
    }
    return data;
}
const fileB64 = (file) => new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
});

// ---------------------------------------------------------------- banners

// A dismissed banner stays closed until its message changes. Remembered for this tab only.
const dismissed = (() => { try { return JSON.parse(sessionStorage.getItem("dismissed") || "{}"); } catch { return {}; } })();
function showAlert(id, text) {
    if (!text && dismissed[id]) { delete dismissed[id]; saveDismissed(); } // cleared: the next one shows
    $(id).hidden = !text || dismissed[id] === text;
    $(id).querySelector("p").textContent = text;
}
function saveDismissed() { try { sessionStorage.setItem("dismissed", JSON.stringify(dismissed)); } catch {} }
for (const el of $$(".alert")) el.querySelector(".alert-close").onclick = () => {
    dismissed[el.id] = el.querySelector("p").textContent;
    saveDismissed();
    el.hidden = true;
};

// A page left open across a deploy keeps running the old script. Reachy Mini Control's panel only loads the console
// when the app starts and has no reload button, so the console watches its own files and offers the reload itself.
const CONSOLE_FILES = ["/static/index.html", "/static/main.js", "/static/style.css"];
let consoleVersion = null; // the files' ETags when this page loaded
async function checkConsoleVersion() {
    try {
        const tags = await Promise.all(CONSOLE_FILES.map(async (f) => {
            const r = await fetch(f, { method: "HEAD", cache: "no-store" });
            return r.ok ? r.headers.get("etag") || r.headers.get("last-modified") : null;
        }));
        if (tags.includes(null)) return;
        consoleVersion ??= tags.join(" ");
        showAlert("updated", tags.join(" ") === consoleVersion ? "" : "This console was updated after the page loaded. Reload it to use the new version.");
    } catch {} // Gizmo restarting: next time
}
$("reload").onclick = () => location.reload();

// ---------------------------------------------------------------- the header: who Gizmo is and what it's doing

// The conversation as turns: what was heard, what the brain did about it (skills, tools), what was said back.
function buildTurns(events) {
    const items = [];
    const norm = (t) => String(t || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    let cur = null;
    const open = (e, heard) => {
        cur = { id: `${e.time}|${heard ?? e.text}`, time: e.time, heard, typed: e.kind === "typed", steps: [], reply: "", brain: null,
                timing: [], ignored: null, interrupted: false, intro: Boolean(e.intro) };
        items.push({ turn: cur });
        return cur;
    };
    for (const e of [...events].reverse()) {
        if (e.kind === "user" || e.kind === "typed") {
            // Nova Sonic hands a typed line on as if it had heard it: the same line, once.
            if (cur && cur.typed && !cur.reply && e.kind === "user" && norm(e.text) === norm(cur.heard)) continue;
            open(e, e.private ? "(someone talking, not to Gizmo)" : e.text);
        } else if (["skill", "mcp", "tool", "body", "sound"].includes(e.kind)) {
            (cur || open(e, null)).steps.push(e);
        } else if (e.kind === "brain") {
            if (cur) cur.brain = e;
        } else if (e.kind === "assistant") {
            const turn = cur || open(e, null);
            if (!turn.reply.endsWith(e.text)) turn.reply = `${turn.reply} ${e.text}`.trim();
        } else if (e.kind === "timing") {
            if (cur) cur.timing.push(e.text);
        } else if (e.kind === "ignored") {
            if (cur) cur.ignored = e.text;
        } else if (e.kind === "barge_in") {
            if (cur) cur.interrupted = true;
        } else {
            items.push({ note: e });
        }
    }
    return items;
}
let turnItems = [];
const turns = () => turnItems.filter((i) => i.turn).map((i) => i.turn);
const stage = (t) => t.intro || (t.heard || "").startsWith("(Stage direction"); // asked to introduce itself, not something said

function thinking() {
    const last = turns().at(-1);
    return Boolean(S && last && last.heard && !last.reply && !last.ignored && !S.off && S.status === "listening" && turnAge(last) < 40);
}
function turnAge(t) {
    const [h, m, s] = t.time.split(":").map(Number);
    const now = new Date();
    let d = now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds() - (h * 3600 + m * 60 + s);
    if (d < -43200) d += 86400; // just after midnight
    return Math.abs(d);
}

function faceState(s) {
    if (s.switching) return "starting";
    if (s.off) return "off";
    if (s.asleep) return "asleep";
    if (s.status === "error") return "error";
    if (s.speaking) return "speaking";
    if (thinking()) return "thinking";
    return s.status === "listening" ? "listening" : "starting";
}
const STATUS = { off: "off · no session open", asleep: "hidden · asleep", error: "error", speaking: "speaking", thinking: "thinking…" };

function renderTop(s) {
    const face = faceState(s);
    document.body.classList.toggle("desk", Boolean(s.desk)); // gizmo.desk: no robot, so no mic, power or body controls
    $("face").dataset.state = face;
    $("status").textContent = s.switching === "down" ? "shutting down…" : s.switching === "up" ? "starting up…"
        : STATUS[face] || (s.status === "listening" ? (s.desk ? "ready · typed turns" : `listening${s.muted ? " · mic muted" : ""}`) : s.status);
    $("say").placeholder = s.desk ? "Type a line for Gizmo" : "Type a line for Gizmo, as if you'd said it";
    const who = Object.entries(s.personalities).map(([k, name]) =>
        `<button data-who="${esc(k)}" class="${k === s.personality ? "on" : ""}">${esc(name)}</button>`).join("");
    if ($("who").innerHTML !== who) $("who").innerHTML = who;
    $("now").textContent = s.off ? "" : s.now.short;
    $("mute").setAttribute("aria-pressed", String(s.muted));
    $("mute-label").textContent = s.muted ? "Mic muted" : "Mic on";
    $("meter").style.width = `${s.muted ? 0 : Math.max(0, Math.min(100, (s.mic_dbfs + 62) * 2.1))}%`;
    $("power-label").textContent = s.switching === "down" ? "Shutting down…" : s.switching === "up" ? "Starting up…" : s.off ? "Start up" : "Shut down";
    $("power-btn").dataset.off = String(Boolean(s.off && !s.switching));
    $("power-btn").disabled = Boolean(s.switching);
    const p = s.power || {};
    showAlert("power", p.undervoltage_now ? "Under-voltage right now: check Gizmo's power supply before it resets."
        : p.undervoltage_since_boot ? "Gizmo's power dipped since it started (under-voltage). Keep it on its own power supply." : "");
    showAlert("error", s.error || "");
    showAlert("fallback", s.off ? "" : s.now.note);
    const down = Object.values((s.tools || {}).servers || {}).filter((ok) => !ok).length;
    $("servers-badge").hidden = !down;
    $("servers-badge").textContent = down;
    $("track").setAttribute("aria-pressed", String(s.tracking));
    $("hide-reveal").textContent = s.asleep ? "Reveal" : "Hide";
    $("hide-reveal").dataset.hidden = String(Boolean(s.asleep && !s.off));
    for (const id of ["hide-reveal", "interrupt", "track", "sit-up", "introduce"]) $(id).disabled = Boolean(s.off);
    $("new-convo").disabled = Boolean(s.off || s.switching);
    $("introduce").disabled = Boolean(s.off || s.asleep || s.status !== "listening");
    // The mic open but only silence coming in: the robot's microphone volume is at 0 (set outside this app), so
    // Gizmo can't hear, and Nova Sonic drops the session every five minutes for lack of audio.
    silentPolls = !s.desk && !s.off && !s.asleep && !s.muted && s.status === "listening" && s.mic_dbfs < -75 ? silentPolls + 1 : 0;
    showAlert("silent", silentPolls >= 8 ? "Gizmo's microphone is sending silence, so it can't hear you. The robot's microphone volume is probably at 0: turn it up in Reachy Mini Control." : "");
}
let silentPolls = 0;

// ---------------------------------------------------------------- Talk

function stepTag(e) {
    const d = e.detail || {};
    if (e.kind === "body") return d.turned ? `<span class="tag">turned to the voice</span>` : "";
    if (e.kind === "sound") return `<span class="tag ${d.error ? "bad" : ""}">sound · ${esc(d.name || "")}</span>`;
    if (e.kind === "skill") return `<span class="tag ${d.error ? "bad" : "skill"}">${d.file ? "file" : "skill"} · ${esc(d.skill || (d.uri || "").split("/").pop())}</span>`;
    if (e.kind === "mcp") return `<span class="tag ${d.error ? "bad" : "mcp"}">${esc(d.server)} · ${esc(d.name)}</span>`;
    return `<span class="tag ${d.error ? "bad" : ""}">${esc(d.name || e.text)}</span>`;
}
function turnMeta(t) {
    const bits = [];
    if (t.brain) {
        const d = t.brain.detail || {};
        bits.push(`${d.model || ""} · thought ${d.seconds ?? "?"} s`);
        if ((d.actions || []).length) bits.push(d.actions.map((a) => a.split(":").pop()).join(", "));
    }
    const voice = t.timing.map((x) => /voice started ([\d.]+)s/.exec(x)).find(Boolean);
    if (voice && !t.typed) bits.push(`voice ${voice[1]} s after you`);
    if (t.interrupted) bits.push("interrupted");
    return bits.join(" · ");
}

// The words of a reply, lit up as Gizmo says them. The robot knows when each sentence's audio starts and ends
// (robot_io.Captions). Inside a sentence each word gets a share of that time by its length, and a little more
// after punctuation, where a voice pauses.
const wordsOf = (text) => String(text || "").split(/\s+/).filter(Boolean);
const bare = (w) => w.toLowerCase().replace(/[^a-z0-9]/g, "");
function wordWeight(w) {
    const said = w.replace(/[^A-Za-z0-9]/g, "");
    const digits = (said.match(/\d/g) || []).length; // "14" takes as long to say as "fourteen"
    return Math.max(1, said.length + digits * 3) + (/[.!?]["')\]]*$/.test(w) ? 5 : /[,;:]$/.test(w) ? 2.5 : 0);
}
function timedWords(c) {
    const out = [];
    c.segments.forEach((seg, n) => {
        const ws = wordsOf(seg.text), weights = ws.map(wordWeight);
        const total = weights.reduce((a, b) => a + b, 0) || 1;
        // The last sentence's audio may still be arriving: while it's clearly short of its words, go by them.
        let length = seg.end - seg.start;
        if (n === c.segments.length - 1 && length < total * 0.03) length = total * 0.06;
        let t = seg.start;
        ws.forEach((w, i) => { const d = (length * weights[i]) / total; out.push({ w, start: t, end: t + d }); t += d; });
    });
    return out;
}
// The turn being answered out loud, the text to show for it and where in that text the spoken words begin.
// Nova Sonic's words reach the log once it has finished making them, a few seconds into saying them, so until
// then they come from the captions; a voice server's reply is in the log from its first word.
function spokenTurn(all) {
    if (!cap || !cap.speaking || !cap.words.length) return null;
    for (const t of all.slice(-3).reverse()) {
        if (t.ignored) continue;
        if (!t.reply) {
            const wrapped = cap.started < "00:01:00" && t.time > "23:59:00"; // a reply just past midnight
            if (cap.started >= t.time || wrapped) return { id: t.id, offset: 0, text: cap.segments.map((x) => x.text).join(" ") };
            continue; // still speaking the answer to an earlier turn
        }
        const said = wordsOf(t.reply), n = Math.min(4, cap.words.length);
        for (let k = 0; k + n <= said.length; k++) {
            if (cap.words.slice(0, n).every((x, i) => bare(said[k + i]) === bare(x.w))) return { id: t.id, offset: k, text: t.reply };
        }
    }
    return null;
}
let liveSpans = [], litKey = "";
function lightWords(force = false) {
    if (!liveSpans.length || !cap) return;
    const at = Math.min(cap.audio, cap.at + (cap.speaking ? (performance.now() - cap.got) / 1000 : 0));
    let done = 0;
    while (done < cap.words.length && cap.words[done].end <= at) done++;
    const now = done < cap.words.length && cap.words[done].start <= at ? done : -1;
    const key = `${cap.reply}|${done}|${now}`;
    if (key === litKey && !force) return;
    litKey = key;
    for (const span of liveSpans) {
        const i = Number(span.dataset.i);
        span.classList.toggle("said", i < done);
        span.classList.toggle("lit", i === now);
    }
}
(function frame() { lightWords(); requestAnimationFrame(frame); })();

let talkSig = "";
function renderTalk(s) {
    $("off-panel").hidden = !(s.off || s.switching);
    if (s.off || s.switching) {
        $("off-title").textContent = s.switching === "down" ? "Shutting down…" : s.switching === "up" ? "Starting up…" : "Off";
        $("off-text").textContent = s.switching ? "" : "No cloud session is open: nothing calls AWS. Gizmo is asleep with its motors off.";
        $("off-steps").innerHTML = (s.off_steps || []).map((x) =>
            `<li><span>${x.ok === true ? "✓" : x.ok === false ? "✕" : "–"}</span><b>${esc(x.name)}</b><span>${esc(x.detail)}</span></li>`).join("");
        $("off-start").hidden = Boolean(s.switching) || !s.off;
    }
    $("hidden-panel").hidden = !(s.asleep && !s.off && !s.switching);
    if (document.activeElement !== $("reveal-greet")) $("reveal-greet").checked = Boolean(s.greet);
    turnItems = buildTurns(s.events);
    const all = turns();
    const spoken = spokenTurn(all);
    const sig = `${s.events.length}|${s.events[0]?.time}|${s.events[0]?.text}|${s.events[0]?.kind}|${thinking()}|${selTurn}`
        + `|${spoken?.id}|${spoken?.offset}|${spoken?.text.length}`;
    if (sig === talkSig) return;
    talkSig = sig;
    const list = $("turns");
    const pinned = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
    const shown = selTurn && all.some((t) => t.id === selTurn) ? selTurn : all.at(-1)?.id;
    // A run of system notes (a few settings changed in a row) shows its last two, so it doesn't bury the turns.
    const shown_items = [];
    for (let i = 0; i < turnItems.length; i++) {
        let end = i;
        while (turnItems[end]?.note && turnItems[end].note.kind !== "power") end++;
        if (end - i > 3) {
            shown_items.push({ note: { kind: "system", text: `${end - i - 2} earlier notes` } }, ...turnItems.slice(end - 2, end));
            i = end - 1;
        } else shown_items.push(turnItems[i]);
    }
    list.innerHTML = shown_items.map((i) => {
        if (i.note) return `<li class="note ${i.note.kind === "power" ? "power" : ""}">${esc(i.note.text)}</li>`;
        const t = i.turn;
        const you = t.heard === null ? "" : stage(t) ? `<div class="you typed">Introduce yourself</div>`
            : `<div class="you ${t.typed ? "typed" : ""}">${esc(t.heard)}</div>`;
        const tags = t.steps.map(stepTag).join("");
        const marks = tags ? `<div class="marks">${tags}</div>` : "";
        const waiting = !t.reply && !t.ignored && t === all.at(-1) && thinking();
        const live = spoken && spoken.id === t.id
            ? wordsOf(spoken.text).map((w, n) => `<span class="w" data-i="${n - spoken.offset}">${esc(w)}</span>`).join(" ") : "";
        const bot = live ? `<div class="bot live">${live}</div>` : t.reply ? `<div class="bot">${esc(t.reply)}</div>`
            : waiting ? `<div class="bot waiting">thinking…</div>` : "";
        const meta = t.ignored ? `<div class="meta">${esc(t.ignored)}</div>` : turnMeta(t) ? `<div class="meta">${esc(turnMeta(t))}</div>` : "";
        return `<li class="turn ${t.id === shown ? "sel" : ""} ${t.ignored ? "ignored" : ""}" data-turn="${esc(t.id)}">${you}${marks}${bot}${meta}</li>`;
    }).join("");
    $("empty").hidden = turnItems.length > 0 || s.off;
    liveSpans = $$(".bot.live .w", list);
    lightWords(true);
    if (pinned) list.scrollTop = list.scrollHeight;
    renderTrace(all.find((t) => t.id === shown), shown === all.at(-1)?.id);
}

function renderTrace(t, latest) {
    if (!t) {
        $("trace-meta").textContent = "";
        $("trace").innerHTML = `<li class="step"><div class="k"><b>Waiting</b></div><div class="d">The next turn shows here, step by step: what Gizmo heard, the skill it loaded, each tool it called and how long each took.</div></li>`;
        return;
    }
    $("trace-meta").textContent = `${t.time}${latest ? " · latest turn" : ""}`;
    const total = Math.max((t.brain?.detail?.seconds || 0) * 1000, ...t.steps.map((e) => e.detail?.ms || 0), 1);
    const bar = (ms) => (ms ? `<div class="bar" style="width:${Math.max(2, Math.round((ms / total) * 100))}%"></div>` : "");
    const row = (cls, kind, title, d = "", ms = null, more = "", delay = 0) =>
        `<li class="step ${cls}" style="animation-delay:${delay}ms"><div class="k"><b>${kind}</b><span class="ms">${secs(ms)}</span></div>`
        + `<div class="t">${title}</div>${d ? `<div class="d">${d}</div>` : ""}${bar(ms)}${more}</li>`;
    const rows = [];
    if (t.heard !== null) rows.push(stage(t) ? row("", "Asked", "Introduce yourself", esc(t.heard)) : row("", t.typed ? "Typed" : "Heard", esc(t.heard)));
    if (t.ignored) rows.push(row("err", "Ignored", esc(t.ignored)));
    for (const e of t.steps) {
        const d = e.detail || {};
        const err = d.error ? `<div class="d"><span class="tag bad">failed</span> ${esc(d.error)}</div>` : "";
        if (e.kind === "body") {
            rows.push(row("", "Body", d.turned ? "Turned toward the voice" : "Didn't turn", esc(e.text)));
        } else if (e.kind === "sound") {
            rows.push(row(d.error ? "err" : "", "Sound", d.words ? `“${esc(d.words)}”` : esc(e.text),
                `<span class="mono">[sound:${esc(d.name || "")}]</span>${d.seconds ? ` · a ${d.seconds} s recording, played ahead of the words` : ""}`, null, err));
        } else if (e.kind === "skill") {
            const where = d.server ? `from the MCP server <b>${esc(d.server)}</b>` : "kept on this robot";
            const facts = [d.uri ? `<span class="mono">${esc(d.uri)}</span>` : "", d.bytes ? kb(d.bytes) : "",
                d.files ? `${plural(d.files, "more file")}` : ""].filter(Boolean).join(" · ");
            const check = d.error || d.file || !d.server ? "" : d.verified
                ? ` <span class="tag ok">matches the listing</span>` : ` <span class="tag warn">generated: not checkable</span>`;
            rows.push(row(`skill ${d.error ? "err" : ""}`, d.file ? "Skill file" : "Skill", esc(d.skill || d.uri), `${where}${check}${facts ? `<br>${facts}` : ""}`, d.ms, err));
        } else {
            const args = d.args && Object.keys(d.args).length ? `<details><summary>arguments</summary><pre class="code">${esc(JSON.stringify(d.args, null, 2))}</pre></details>` : "";
            const result = d.result ? `<details ${t.steps.length < 3 ? "open" : ""}><summary>result</summary><pre class="code">${esc(d.result)}</pre></details>` : "";
            const title = e.kind === "mcp" ? `${esc(d.server)} · ${esc(d.name)}` : esc(d.name || e.text);
            rows.push(row(`${e.kind === "mcp" ? "mcp" : ""} ${d.error ? "err" : ""}`, e.kind === "mcp" ? "MCP tool" : "Tool", title,
                d.where === "agentcore" ? esc(e.text) : "", d.ms, err + args + result));
        }
    }
    if (t.brain) {
        const d = t.brain.detail || {};
        const moves = (d.actions || []).map((a) => `<span class="tag">${esc(a)}</span>`).join(" ");
        rows.push(row("", "Model", esc(d.model || t.brain.text), `thought for ${d.seconds ?? "?"} s in all${moves ? `<br>${moves}` : ""}`));
    }
    if (t.reply) rows.push(row("reply", "Said", esc(t.reply), esc(t.timing.join(" · "))));
    $("trace").innerHTML = rows.map((r, i) => r.replace("animation-delay:0ms", `animation-delay:${i * 45}ms`)).join("");
}

function renderInPlay() {
    if (!S || !P) return;
    const me = P.personalities[S.personality];
    if (!me) return;
    $("inplay-who").textContent = `for ${me.name}`;
    const loaded = (name, server) => (S.skills_loaded || []).some((x) => x.name === name && (x.server || null) === server);
    const blocks = [];
    for (const [name, pick] of Object.entries(me.tools.servers)) {
        if (!pick.on) continue;
        const st = (P.servers.find((x) => x.name === name) || {}).status || { ok: false };
        const tools = (st.tools || []).filter((x) => !pick.tools || pick.tools.includes(x.name));
        const skills = (st.skills || []).filter((x) => !pick.skills || pick.skills.includes(x.uri));
        const tags = [
            ...skills.map((k) => approved(pick, k)
                ? `<span class="tag skill" title="${esc(k.uri)}">${loaded(k.name, name) ? "● " : ""}${esc(k.name)}</span>`
                : `<span class="tag warn" title="Its files changed since it was approved. Approve it again under Personalities.">${esc(k.name)} · needs approval</span>`),
            st.ok ? `<span class="tag mcp">${plural(tools.length, "tool")}</span>` : `<span class="tag bad">not connected</span>`,
        ].join(" ");
        blocks.push(`<div class="srv"><b><span class="dot ${st.ok ? "ok" : "bad"}"></span> ${esc(name)}</b><div class="stack">${tags}</div></div>`);
    }
    if (me.tools.skills.length) {
        blocks.push(`<div class="srv"><b>on this robot</b><div class="stack">${me.tools.skills.map((k) =>
            `<span class="tag skill">${loaded(k, null) ? "● " : ""}${esc(k)}</span>`).join(" ")}</div></div>`);
    }
    $("inplay").innerHTML = blocks.join("") || `<p class="hint">Nothing switched on: this personality answers from what the model knows.</p>`;
}

// ---------------------------------------------------------------- Personalities

const slug = (name) => name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^[^a-z]+|_+$/g, "").slice(0, 30);
const BUILTIN_RANGES = { mix_amount: (v) => `${Math.round(v * 100)}%`, speed: (v) => `${Number(v).toFixed(2)}×`,
    pitch: (v) => `${v > 0 ? "+" : ""}${v} st`, robot: (v) => `${Math.round(v * 100)}%` };

async function loadPeople() {
    P = await call("GET", "/api/personalities", undefined, { quiet: true }).catch(() => P);
    renderInPlay();
    if (tab === "people") renderPeople();
}

function renderPeople() {
    if (!P) return;
    if (!pKey || (pKey !== NEW && !P.personalities[pKey])) pKey = P.active;
    $("p-list").innerHTML = Object.values(P.personalities).map((p) => {
        const on = Object.values(p.tools.servers).filter((x) => x.on).length;
        const voice = p.voice.kind === "nova" ? `Nova ${P.nova_voices[p.voice.nova].split(" (")[0]}` : p.voice.kind === "clone" ? "cloned voice" : "built-in voice";
        return `<li data-p="${esc(p.key)}" class="${p.key === pKey ? "sel" : ""}"><span class="name">${esc(p.name)}`
            + `${p.key === P.active ? ` <span class="tag ok">in use</span>` : ""}${p.edited ? ` <span class="tag">edited</span>` : ""}</span>`
            + `<span class="sub">${esc(voice)} · ${plural(on, "server")}${p.tools.skills.length ? ` · ${plural(p.tools.skills.length, "local skill")}` : ""}</span></li>`;
    }).join("") + (pKey === NEW ? `<li class="sel"><span class="name">New personality</span><span class="sub">not saved yet</span></li>` : "");
    fillPersonality();
}

let pNew = null; // the fields a new personality starts from
function current() {
    return pKey === NEW ? pNew : P.personalities[pKey];
}

function fillPersonality() {
    const p = current();
    if (!p) return;
    const isNew = pKey === NEW;
    $("p-name").value = p.name;
    $("p-key").textContent = isNew ? "its key comes from the name" : `${p.key}${p.builtin ? " · built in" : ""}`;
    $("p-prompt").value = p.prompt;
    $("p-rendered").textContent = p.rendered || "Save it to see the whole prompt.";
    $("p-delivery").value = p.delivery || "";
    $("p-greeting").value = p.greeting || "";
    $("p-greeting").placeholder = P.greeting_default;
    $("p-use").hidden = isNew || p.key === P.active;
    $("p-reset").hidden = isNew || !p.builtin || !p.edited;
    $("p-delete").hidden = isNew || p.builtin;
    $("p-duplicate").hidden = isNew;
    $("p-sounds").hidden = isNew; // a clip is saved into the personality's folder, which a new one doesn't have yet
    // voice
    const def = p.defaults?.voice?.nova;
    $("p-nova").innerHTML = Object.entries(P.nova_voices).map(([k, label]) =>
        `<option value="${k}" ${k === p.voice.nova ? "selected" : ""}>${esc(label)}${k === def ? " · its default" : ""}</option>`).join("");
    const opts = (sel, none) => (none ? `<option value="">nothing</option>` : "") + Object.entries(P.builtin_voices).map(([k, label]) =>
        `<option value="${k}" ${k === sel ? "selected" : ""}>${k.split("_")[1]} · ${esc(label)}</option>`).join("");
    const b = p.voice.builtin || {};
    $("b-voice").innerHTML = opts(b.voice);
    $("b-mix").innerHTML = opts(b.mix, true);
    for (const k of Object.keys(BUILTIN_RANGES)) { $(`b-${k}`).value = b[k] ?? 0; $(`b-${k}-val`).textContent = BUILTIN_RANGES[k](Number(b[k] ?? 0)); }
    setKind(p.voice.kind);
    fillClip(p);
    $("p-tools").innerHTML = toolsEditor(p);
}

function setKind(kind) {
    const addon = Boolean(S?.addons?.voice);
    if (!addon && kind !== "nova") kind = "nova";
    $("p-kind").dataset.kind = kind;
    for (const b of $$("#p-kind button")) {
        b.classList.toggle("on", b.dataset.kind === kind);
        if (b.classList.contains("addon-voice")) b.hidden = !addon;
    }
    $("p-kind").hidden = !addon;
    $("p-builtin").hidden = kind !== "builtin";
    $("p-clone").hidden = kind !== "clone" || pKey === NEW;
    $("p-nova-label").textContent = kind === "nova" ? "Nova Sonic voice" : "Nova Sonic voice (the stand-in)";
    const p = current();
    const def = p?.defaults?.voice?.nova;
    $("p-voice-note").innerHTML = kind === "nova"
        ? (def && $("p-nova").value !== def ? `Its default voice is ${esc(P.nova_voices[def].split(" (")[0])}. <button type="button" class="link" id="p-nova-default">Back to the default</button>` : "Nova Sonic hears and speaks. The change takes effect when you save.")
        : "Spoken by your voice server. While none answers, Nova Sonic speaks instead, in the voice above."
          + (kind === "clone" && pKey === NEW ? " Save the personality first, then add its clip." : "");
}

function fillClip(p) {
    const c = p.clip || {};
    $("clip-now").innerHTML = c.file
        ? `<span class="tag ${c.ready ? "ok" : "warn"}">${c.ready ? "ready" : "needs its transcript"}</span> <span>${c.seconds ? `${c.seconds} s · ` : ""}saved ${esc(c.saved || "")}</span> <button type="button" data-play="${esc(c.file)}">Play</button>`
        : `<span class="tag">no clip yet</span>`;
    $("clip-text").value = c.ref_text || "";
    $("clip-file").value = "";
    const versions = c.versions || [];
    $("clip-versions-box").hidden = !versions.length;
    $("clip-versions").innerHTML = versions.map((v) =>
        `<li><span class="grow">${esc(v.saved || v.id)}${v.seconds ? ` · ${v.seconds} s` : ""}<br><span class="dim">${esc(v.ref_text)}</span></span>`
        + `<button type="button" data-play="${esc(v.file)}">Play</button><button type="button" data-restore="${esc(v.id)}">Load back in</button>`
        + `<button type="button" class="danger" data-delversion="${esc(v.id)}">Delete</button></li>`).join("");
    $("catch-list").innerHTML = (p.catchphrases || []).map((x, i) =>
        `<li><span class="grow"><span class="mono">[sound:${esc(x.name)}]</span> “${esc(x.text)}”`
        + `${x.plays_here ? "" : ` <span class="tag warn" title="The robot only decodes WAV files itself. This one still stands in for its exact line when a cloned voice speaks.">not a WAV: cloned voice only</span>`}</span>`
        + `<button type="button" data-play="${esc(x.file)}">Play</button>`
        + `<button type="button" class="danger" data-delcatch="${i}">Remove</button></li>`).join("") || `<li class="dim">None yet.</li>`;
}

// Does this personality's approval of a skill still hold? All of a server's skills on (null) is trust in the server.
const approved = (pick, k) => !pick.skills || Boolean(k.fingerprint && (pick.approved || {})[k.uri] === k.fingerprint);

// The tools editor: one block per MCP server (a switch, then its skills and tools), and the skills on the robot.
function toolsEditor(p) {
    const check = (attr, value, on, name, desc) =>
        `<label class="check"><input type="checkbox" ${attr}="${esc(value)}" ${on ? "checked" : ""}><div><b>${esc(name)}</b><span>${esc(desc || "")}</span></div></label>`;
    const blocks = P.servers.map((srv) => {
        const pick = p.tools.servers[srv.name] || { on: false, tools: null, skills: null };
        const st = srv.status || {};
        const skills = st.skills || [], tools = st.tools || [];
        // A skill ticked on its own is approved as it was then: if its files have changed since, it doesn't load.
        const skillRow = (k) => {
            const on = !pick.skills || pick.skills.includes(k.uri);
            const row = check("data-skill", k.uri, on, k.name, k.description);
            if (!pick.skills || !on || approved(pick, k)) return row;
            return row + (k.fingerprint
                ? `<p class="approval"><span class="tag warn">not loading</span> Its files aren't the ones that were approved. <button type="button" class="link" data-approve="${esc(k.uri)}">Approve it as it is now</button></p>`
                : `<p class="approval"><span class="tag warn">not loading</span> It's generated on request, so it can't be approved on its own. Tick all of this server's skills to use it.</p>`);
        };
        const facts = !st.ok ? `not connected: ${st.error || ""}` : `${plural(tools.length, "tool")}`
            + (st.skills_extension ? ` · ${plural(skills.length, "skill")} over MCP` : " · no skills served");
        const body = !st.ok ? `<p class="hint">It keeps its place in this personality and comes back when the server does.</p>` : `
            ${skills.length ? `<h4>Skills it serves <button type="button" class="link" data-all="skill">all</button> <button type="button" class="link" data-none="skill">none</button></h4>
            <div class="checks">${skills.map(skillRow).join("")}</div>
            <p class="hint">All ticked: it gets every skill this server serves, including ones added later. Ticking only some approves each one as it is now. If its files change, it stops loading until you approve it again.</p>` : ""}
            ${tools.length ? `<details ${pick.tools ? "open" : ""}><summary>Tools <i data-count="tool">${pick.tools ? pick.tools.length : tools.length} of ${tools.length} on</i></summary>
            <h4><button type="button" class="link" data-all="tool">all</button> <button type="button" class="link" data-none="tool">none</button></h4>
            <div class="checks">${tools.map((t) => check("data-tool", t.name, !pick.tools || pick.tools.includes(t.name), t.name, t.description)).join("")}</div></details>` : ""}`;
        return `<div class="pick" data-server="${esc(srv.name)}" data-listed="${st.ok ? "1" : ""}" data-pick="${esc(JSON.stringify(pick))}">
            <header><label class="switch"><input type="checkbox" data-on ${pick.on ? "checked" : ""} aria-label="Use ${esc(srv.name)}"><i></i></label>
            <div class="grow"><b>${esc(srv.name)}</b><small>${esc(facts)}</small></div><span class="dot ${st.ok ? "ok" : "bad"}"></span></header>
            <div class="body" ${pick.on ? "" : "hidden"}>${body}</div></div>`;
    });
    const local = P.local_skills.length
        ? `<div class="checks">${P.local_skills.map((k) => check("data-local", k.name, p.tools.skills.includes(k.name), k.name, k.description)).join("")}</div>`
        : `<p class="hint">None yet. Write or upload one under Servers.</p>`;
    blocks.push(`<div class="pick"><header><div class="grow"><b>Skills on this robot</b><small>loaded by Strands' AgentSkills plugin</small></div></header><div class="body">${local}</div></div>`);
    return blocks.join("");
}

function readTools() {
    const servers = {};
    for (const el of $$("#p-tools .pick[data-server]")) {
        const was = JSON.parse(el.dataset.pick);
        const on = el.querySelector("[data-on]").checked;
        if (!el.dataset.listed) { servers[el.dataset.server] = { ...was, on }; continue; } // not connected: keep its picks
        const picked = (attr) => {
            const boxes = $$(`[data-${attr}]`, el);
            const yes = boxes.filter((b) => b.checked).map((b) => b.dataset[attr]);
            return yes.length === boxes.length ? null : yes; // everything ticked means "all, and whatever it adds later"
        };
        if (on || was.on || was.tools || was.skills) servers[el.dataset.server] = { on, tools: picked("tool"), skills: picked("skill") };
    }
    const p = current();
    for (const [name, pick] of Object.entries(p.tools.servers)) if (!(name in servers) && !P.servers.some((s) => s.name === name)) servers[name] = pick;
    return { servers, skills: $$("#p-tools [data-local]").filter((b) => b.checked).map((b) => b.dataset.local) };
}
const readBuiltin = () => ({ voice: $("b-voice").value, mix: $("b-mix").value || null,
    ...Object.fromEntries(Object.keys(BUILTIN_RANGES).map((k) => [k, Number($(`b-${k}`).value)])) });

// ---------------------------------------------------------------- Servers

async function loadServers() {
    V = await call("GET", "/api/servers", undefined, { quiet: true }).catch(() => V);
    if (tab === "servers") renderServers();
}

function renderServers() {
    if (!V) return;
    if (sSel?.server && !V.servers.some((x) => x.name === sSel.server)) sSel = null;
    if (!sSel && V.servers.length) sSel = { server: V.servers[0].name };
    $("s-list").innerHTML = V.servers.map((x) => {
        const st = x.status || {};
        const facts = st.ok ? `${plural(st.tools.length, "tool")} · ${st.skills_extension ? `${plural(st.skills.length, "skill")} over MCP` : "no skills served"}` : "not connected";
        return `<li data-server="${esc(x.name)}" class="${sSel?.server === x.name ? "sel" : ""}"><span class="name"><span class="dot ${st.ok ? "ok" : "bad"}"></span>${esc(x.name)}</span>`
            + `<span class="sub">${esc(x.url)}</span><span class="sub">${esc(facts)}</span></li>`;
    }).join("") || `<li class="none">No servers yet. Add the one you're building.</li>`;
    $("k-list").innerHTML = V.local_skills.map((k) =>
        `<li data-skill="${esc(k.name)}" class="${sSel?.skill === k.name ? "sel" : ""}"><span class="name">${esc(k.name)}</span><span class="desc">${esc(k.description)}</span></li>`).join("")
        || `<li class="none">None. Most skills can live on the server that needs them.</li>`;
    renderServerDetail();
}

function renderServerDetail() {
    const el = $("s-detail");
    if (!sSel) { el.innerHTML = serverIntro(); return; }
    if (sSel.form) { el.innerHTML = serverForm(sSel.form); return; }
    if (sSel.skill !== undefined) { el.innerHTML = skillForm(sSel); return; }
    const x = V.servers.find((s) => s.name === sSel.server);
    const st = x.status || {};
    const used = (V.used_by[x.name] || []).join(", ");
    const head = `<header class="editor-head"><div><h2 class="big">${esc(x.name)}</h2><p class="mono dim">${esc(x.url)}</p></div>
        <div class="editor-actions"><button data-act="refresh" class="quiet">Ask again</button><button data-act="edit" class="quiet">Edit</button>
        <button data-act="remove" class="quiet danger">Remove</button></div></header>`;
    if (!st.ok) {
        el.innerHTML = `${head}<div class="facts"><span class="tag bad">not connected</span></div><pre class="code">${esc(st.error || "no answer")}</pre>
            <p class="hint">Gizmo connects over streamable HTTP. Check the address ends in the server's MCP path (often /mcp), that the robot can reach it, and any headers it needs.</p>`;
        return;
    }
    const facts = `<div class="facts"><span class="tag ok">connected in ${secs(st.connect_ms)}</span>
        ${st.skills_extension ? `<span class="tag skill">serves skills over MCP</span>` : `<span class="tag">no Skills extension declared</span>`}
        <span class="tag">${used ? `used by ${esc(used)}` : "not used by any personality"}</span></div>`;
    const problems = (st.problems || []).length ? `<fieldset><legend>Problems with its skills</legend>${st.problems.map((p) =>
        `<p class="hint"><span class="tag bad">skipped</span> ${esc(p)}</p>`).join("")}</fieldset>` : "";
    const skills = st.skills_extension
        ? (st.skills.length ? st.skills.map(skillItem).join("") : `<p class="hint">It declares the extension but lists no skills. A server may leave skills out of its listing: the model can still load one by URI.</p>`)
        : `<p class="hint">This server doesn't declare <span class="mono">io.modelcontextprotocol/skills</span> in its capabilities, so Gizmo doesn't treat any of its resources as a skill. <a href="${SEP_URL}" target="_blank" rel="noopener">How a server serves skills (SEP-2640)</a>.</p>`;
    const tools = st.tools.length ? st.tools.map(toolItem).join("") : `<p class="hint">No tools.</p>`;
    el.innerHTML = `${head}${facts}${problems}
        <fieldset><legend>Skills it serves</legend>${skills}</fieldset>
        <fieldset><legend>Tools</legend><p class="hint">The model sees each as <span class="mono">${esc(x.name)}_&lt;name&gt;</span>. Run one here to see exactly what it would get back.</p>${tools}</fieldset>`;
}

function skillItem(k) {
    const size = k.dynamic ? "generated on request" : `${plural(k.files.length, "file")} · ${kb(k.total_bytes)}`;
    return `<div class="item skill" data-uri="${esc(k.uri)}"><header><b>${esc(k.name)}</b><button data-act="read-skill">Read and check</button></header>
        <p>${esc(k.description)}</p><p class="mono dim">${esc(k.uri)} · ${size}</p><div class="out"></div></div>`;
}
function toolItem(t) {
    const props = (t.schema || {}).properties || {};
    const sample = Object.fromEntries(((t.schema || {}).required || []).map((k) => [k, props[k]?.type === "number" || props[k]?.type === "integer" ? 0 : props[k]?.type === "boolean" ? false : ""]));
    return `<div class="item tool" data-tool="${esc(t.name)}"><header><b>${esc(t.name)}</b><button data-act="run-tool">Run</button></header>
        <p>${esc(t.description)}</p>
        ${Object.keys(props).length ? `<label class="field"><span>Arguments (JSON)</span><textarea rows="2" class="mono" spellcheck="false">${esc(JSON.stringify(sample))}</textarea></label>
        <details><summary class="mono dim">input schema</summary><pre class="code">${esc(JSON.stringify(t.schema, null, 2))}</pre></details>` : ""}
        <div class="out"></div></div>`;
}
function serverIntro() {
    return `<h2 class="big">Point Gizmo at a server</h2>
        <p>Add the MCP server you're building, then ask Gizmo something it should be able to answer. The Talk view shows which skill it loaded and which tools it called, with timings.</p>
        <p class="hint">A server can serve skills as well as tools (<a href="${SEP_URL}" target="_blank" rel="noopener">SEP-2640</a>): the instructions that teach a model to use its tools travel with the server instead of being installed separately.</p>`;
}
function serverForm(f) {
    const rows = Object.entries(f.headers || {}).map(([k, v]) => headerRow(k, v)).join("");
    return `<header class="editor-head"><div><h2 class="big">${f.original ? `Edit ${esc(f.original)}` : "Add a server"}</h2></div></header>
        <div class="row"><label class="field"><span>Name <i>(it prefixes the tool names)</i></span><input id="f-name" value="${esc(f.name || "")}" placeholder="kb" autocomplete="off"></label>
        <label class="field grow"><span>URL <i>(streamable HTTP)</i></span><input id="f-url" value="${esc(f.url || "")}" placeholder="http://192.168.1.20:8000/mcp" autocomplete="off"></label></div>
        <fieldset><legend>Headers</legend><p class="hint">For a token, say. Saved values come back masked and stay on the robot.</p>
        <div id="f-headers">${rows}</div><div><button data-act="add-header" class="quiet">Add a header</button></div></fieldset>
        <div class="editor-actions"><button data-act="test" class="quiet">Test</button><button data-act="save-server" class="primary">Save</button>
        <button data-act="cancel" class="quiet">Cancel</button></div><div id="f-out"></div>
        ${f.original ? "" : `<p class="hint">A new server is switched on for ${esc(V.active_name)} straight away.</p>`}`;
}
const headerRow = (k = "", v = "") => `<div class="kv"><input placeholder="Authorization" value="${esc(k)}" autocomplete="off"><input placeholder="Bearer …" value="${esc(v)}" autocomplete="off"><button data-act="del-header" aria-label="Remove this header">✕</button></div>`;
function readServerForm() {
    const headers = {};
    for (const row of $$("#f-headers .kv")) { const [k, v] = $$("input", row).map((i) => i.value.trim()); if (k) headers[k] = v; }
    return { name: $("f-name").value.trim(), url: $("f-url").value.trim(), headers };
}
function skillForm(sel) {
    const isNew = sel.skill === null;
    const template = "---\nname: my-skill\ndescription: What it's for, and when to use it\n---\n\n# My skill\n\nWhat to do, step by step, and which tools to use.\n";
    return `<header class="editor-head"><div><h2 class="big">${isNew ? "Write a skill" : esc(sel.skill)}</h2><p class="mono dim">kept on this robot</p></div></header>
        <p class="hint">A SKILL.md: frontmatter with a name and a description (which is all the model sees until it loads the skill), then the instructions.${isNew ? " The name in the frontmatter names the skill." : ""}</p>
        <textarea id="k-content" rows="20" class="mono" spellcheck="false">${esc(isNew ? template : sel.content || "")}</textarea>
        <div class="editor-actions"><button data-act="save-skill" class="primary">Save</button>${isNew ? "" : `<button data-act="delete-skill" class="quiet danger">Delete</button>`}
        <button data-act="cancel" class="quiet">Cancel</button></div>`;
}
function summarize(st) {
    if (!st.ok) return `<div class="facts"><span class="tag bad">didn't connect</span></div><pre class="code">${esc(st.error)}</pre>`;
    return `<div class="facts"><span class="tag ok">answered in ${secs(st.connect_ms)}</span><span class="tag mcp">${plural(st.tools.length, "tool")}</span>
        ${st.skills_extension ? `<span class="tag skill">${plural(st.skills.length, "skill")} over MCP</span>` : `<span class="tag">no Skills extension</span>`}</div>
        <p class="hint">${esc(st.tools.map((t) => t.name).join(", "))}${st.skills.length ? ` · skills: ${esc(st.skills.map((k) => k.name).join(", "))}` : ""}</p>`;
}

// ---------------------------------------------------------------- Settings

// Connecting a voice server changes nobody's voice by itself: say what does.
const VOICE_NEXT = `<p class="hint">Connecting makes these voices available; it doesn't change how anyone sounds. To use one, open <button class="link" data-act="to-people">Personalities</button>, choose <b>Built-in</b> or <b>Cloned</b> under Voice, and save.</p>`;

async function loadSettings() {
    [G, H] = await Promise.all([
        call("GET", "/api/settings", undefined, { quiet: true }).catch(() => G),
        call("GET", "/api/health", undefined, { quiet: true }).catch(() => H),
    ]);
    if (G) $("foot-versions").textContent = Object.entries(G.versions).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(" · ");
    if (tab === "settings") renderSettings();
}

function renderSettings() {
    if (!G) return;
    const m = G.model, provider = mDraft || m.provider;
    const about = { bedrock: "Claude on Amazon Bedrock, from the robot. The default.", openai: "Any OpenAI-compatible endpoint: Ollama, LM Studio, OpenAI, OpenRouter…",
        agentcore: "The same Strands agent, hosted on AgentCore Runtime." };
    const choices = Object.entries(m.providers).map(([k, label]) =>
        `<button class="choice ${k === provider ? "on" : ""}" data-provider="${k}"><b>${esc(label)}</b><span>${about[k]}</span></button>`).join("");
    const known = Object.keys(m.bedrock.models).includes(m.bedrock.model);
    const fields = provider === "bedrock" ? `
        <div class="row"><label class="field"><span>Model</span><select id="m-bedrock">${Object.entries(m.bedrock.models).map(([id, label]) =>
            `<option value="${id}" ${id === m.bedrock.model ? "selected" : ""}>${esc(label)}</option>`).join("")}<option value="" ${known ? "" : "selected"}>Another model id…</option></select></label>
        <label class="field grow" id="m-bedrock-other-box" ${known ? "hidden" : ""}><span>Model id or inference profile</span><input id="m-bedrock-other" value="${esc(known ? "" : m.bedrock.model)}" placeholder="us.amazon.nova-pro-v1:0"></label></div>
        <p class="hint">Uses the robot's AWS credentials (profile ${esc(G.aws.profile)}, ${esc(G.aws.region)}). If the model is slow or fails, the other Claude takes that turn.</p>`
        : provider === "openai" ? `
        <div class="row"><label class="field grow"><span>Base URL${m.openai.auto ? " <i>(empty: LM Studio on the home box)</i>" : ""}</span><input id="m-url" value="${esc(m.openai.base_url)}" placeholder="http://192.168.1.20:11434/v1" autocomplete="off"></label>
        <label class="field"><span>Model</span><input id="m-model" value="${esc(m.openai.model)}" placeholder="qwen3:14b" list="m-models" autocomplete="off"><datalist id="m-models"></datalist></label></div>
        <div class="row"><label class="field"><span>API key <i>(if it needs one)</i></span><input id="m-key" type="password" value="${esc(m.openai.api_key)}" autocomplete="off"></label>
        <label class="field grow"><span>Extra request parameters <i>(JSON, optional)</i></span><input id="m-params" class="mono" value="${esc(JSON.stringify(m.openai.params))}" placeholder='{"reasoning_effort": "none"}' autocomplete="off"></label></div>
        <p class="hint">The model needs to be able to call tools. Claude on Bedrock stands by: if this endpoint is slow or doesn't answer, it takes the turn.</p><div id="m-out"></div>`
        : `<div class="row"><label class="field grow"><span>Runtime ARN</span><input id="m-arn" class="mono" value="${esc(m.agentcore.arn)}" placeholder="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/gizmo_brain-AbCdEf1234" autocomplete="off"></label></div>
        <p class="hint">Deploy it with <span class="mono">agentcore/deploy.py</span>, which prints the ARN. The brain on the robot stands by, and still does the camera.</p>`;
    const L = G.listening, box = G.addons.box, voice = G.addons.voice;
    const sys = (name, ok, text) => `<div class="${ok === true ? "ok" : ok === false ? "bad" : "warn"}"><b>${esc(name)}</b><span>${esc(text)}</span></div>`;
    const h = H || {};
    const health = !H ? "" : [
        sys("Session", h.session.ok, h.session.ok ? `open: ${h.session.status}` : S?.off ? "off: nothing calls AWS" : h.session.error || h.session.status),
        sys("Model", h.model.ok, `${h.model.model}${h.model.url ? ` at ${h.model.url}` : ""}${h.model.ok ? "" : ": not answering"}`),
        ...Object.entries(h.servers).map(([n, ok]) => sys(`MCP · ${n}`, ok, ok ? "connected" : "not connected")),
        !h.robot ? "" : sys("Motors", h.motors === "enabled" ? true : h.motors ? null : false, h.motors || "no answer from the robot"),
        !h.robot ? "" : sys("Power", h.power ? !h.power.undervoltage_now : null, !h.power ? "unknown" : h.power.undervoltage_now ? "under-voltage now" : h.power.undervoltage_since_boot ? "dipped since it started" : "steady"),
        h.voice_server ? sys("Voice server", h.voice_server.ok, h.voice_server.url || "none answering") : "",
        h.box_ears ? sys("Home-box ears", h.box_ears.ok, h.box_ears.url || "not answering") : "",
    ].join("");
    $("settings").innerHTML = `
        <section class="wide"><h2>What thinks <small>the brain's model</small></h2><div class="choices">${choices}</div>${fields}
            <div class="editor-actions"><button data-act="save-model" class="primary">Save</button>${provider === "openai" ? `<button data-act="test-model" class="quiet">Check the endpoint</button>` : ""}</div></section>
        ${box ? `<section><h2>Who hears and speaks</h2><div class="seg">${Object.entries(G.speech_choices).map(([k, label]) =>
            `<button data-speech="${k}" class="${G.speech === k ? "on" : ""}">${esc(label)}</button>`).join("")}</div>
            <p class="hint">Nova Sonic on Bedrock, or the home box's own ears and voice server. Gizmo falls back to Nova Sonic while the box doesn't answer.</p>
            <label class="toggle"><span class="switch"><input type="checkbox" id="g-barge" ${G.barge_in ? "checked" : ""}><i></i></span> People can talk over Gizmo</label>
            <label class="field"><span>A turn ends after <output id="g-ears-val">${(G.ears_end_ms / 1000).toFixed(2)} s</output> of silence</span><input type="range" id="g-ears" min="300" max="2000" step="50" value="${G.ears_end_ms}"></label></section>` : ""}
        <section><h2>Listening</h2><div class="seg"><button data-listen="free" class="${L.mode === "free" ? "on" : ""}">Free talk</button><button data-listen="wake" class="${L.mode === "wake" ? "on" : ""}">Wake word</button></div>
            <p class="hint">${L.mode === "free" ? "Gizmo answers whatever it hears." : "A turn needs Gizmo's name. Follow-ups right after it speaks don't."} Typed lines always go through.</p>
            <div ${L.mode === "wake" ? "" : "hidden"}><label class="field"><span>Names it answers to <i>(comma separated)</i></span><input id="g-names" value="${esc(L.names.join(", "))}"></label>
            <label class="field" style="margin-top:10px"><span>Follow-ups for <output id="g-follow-val">${L.follow_up_s} s</output></span><input type="range" id="g-follow" min="0" max="60" step="5" value="${L.follow_up_s}"></label>
            <label class="toggle" style="margin-top:12px"><span class="switch"><input type="checkbox" id="g-ignored" ${G.show_ignored ? "checked" : ""}><i></i></span> Show the words it heard but ignored</label>
            <p class="hint">Off, speech without its name isn't shown here or written to the robot's log, only counted. It still goes to Nova Sonic to be recognised: mute the mic to stop that. Turn this on to see why it missed its name.</p></div></section>
        <section><h2>The robot</h2>
            <label class="toggle"><span class="switch"><input type="checkbox" id="g-face" ${G.face_speaker ? "checked" : ""}><i></i></span> Turn to look at whoever speaks to it</label>
            <p class="hint">It turns toward the voice once a turn is for it: any speech in free talk, its name in wake-word mode. ${G.face_speaker ? `<button class="link" data-act="flip-doa">It turns the wrong way</button>` : ""}</p>
            <label class="toggle"><span class="switch"><input type="checkbox" id="g-greet" ${G.greet ? "checked" : ""}><i></i></span> Introduce itself when revealed</label>
            <label class="field"><span>Voice boost <output id="g-boost-val">${G.boost_db} dB</output></span><input type="range" id="g-boost" min="0" max="15" step="1" value="${G.boost_db}"></label>
            <p class="hint">The speaker is already at full volume; boost makes the voice itself louder, with a limiter.</p></section>
        <section><h2>Voice add-in <small>optional</small></h2>
            <p class="hint">A voice server on your network (<span class="mono">addons/voice</span>) gives a personality a built-in voice of its own, or a clone of a short clip. Without one, Nova Sonic speaks.</p>
            <label class="field"><span>Voice server addresses <i>(one per line)</i></span><textarea id="g-voices" rows="2" class="mono" placeholder="http://192.168.1.50:8770">${esc(G.voice_servers.join("\n"))}</textarea></label>
            <div class="editor-actions"><button data-act="save-voices" class="quiet">Connect</button></div><div id="g-voices-out">${voice ? `<div class="facts"><span class="tag ${G.clone.url ? "ok" : "warn"}">${G.clone.url ? `using ${esc(G.clone.url)}` : "none answering"}</span></div>${G.clone.url ? VOICE_NEXT : ""}` : ""}</div></section>
        <section class="wide"><h2>Systems <small><button data-act="health" class="link">check again</button></small></h2><div class="sys">${health}</div></section>
        <section class="wide"><h2>Built with</h2><div class="stack">
            <span class="tag">Strands Agents ${esc(G.versions["strands-agents"] || "")}</span><span class="tag">BidiAgent · Nova Sonic</span><span class="tag">Agent · ${esc(S?.now?.short?.split(" · ")[1] || "Claude")}</span>
            <span class="tag mcp">MCPClient · mcp ${esc(G.versions.mcp || "")}</span><span class="tag skill">AgentSkills plugin</span><span class="tag skill">Skills over MCP · SEP-2640</span><span class="tag">Bedrock AgentCore Runtime</span></div>
            <p class="hint">Two <a href="https://strandsagents.com" target="_blank" rel="noopener">Strands</a> agents share each turn: a speech-to-speech one that hears and speaks, and the brain, which gets the personality's MCP tools and skills. The skill and tool rows of a trace come from Strands' tool hooks.</p></section>`;
}

// ---------------------------------------------------------------- wiring

function render(s) {
    const rev = S?.config_rev, who = S?.personality;
    S = s;
    const c = s.captions;
    cap = c && c.segments.length ? { ...c, words: timedWords(c), speaking: s.speaking, got: performance.now() } : null;
    renderTop(s);
    renderTalk(s);
    if (rev !== undefined && (rev !== s.config_rev || who !== s.personality)) { loadPeople(); if (V) loadServers(); } // something was saved: what's in play changed
    else renderInPlay();
}
async function refresh() {
    try {
        render(await call("GET", "/api/state", undefined, { quiet: true }));
    } catch {
        $("face").dataset.state = "starting";
        $("status").textContent = "connecting";
    }
}
const act = (method, path, body) => call(method, path, body).then(render).catch(() => {});

function show(name) {
    tab = name;
    for (const x of $$(".tabs button[data-tab]")) x.classList.toggle("active", x.dataset.tab === name);
    for (const v of $$(".view")) v.classList.toggle("active", v.id === `view-${name}`);
    if (name === "people") loadPeople();
    if (name === "servers") loadServers();
    if (name === "settings") { mDraft = null; loadSettings(); }
    try { history.replaceState(null, "", `#${name}`); } catch {}
}
for (const b of $$(".tabs button[data-tab]")) b.onclick = () => show(b.dataset.tab);
$("theme").onclick = () => {
    const dark = (document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", dark ? "light" : "dark"); } catch {}
};

// Header
$("who").onclick = (ev) => {
    const b = ev.target.closest("[data-who]");
    if (b) act("POST", "/api/personality", { key: b.dataset.who });
};
$("mute").onclick = () => act("POST", S && S.muted ? "/api/unmute" : "/api/mute");
const SHUTDOWN_DETAIL = "Closes the cloud session (so nothing calls AWS), and puts Gizmo to sleep with the motors off and the mic muted. "
    + "It stays off through restarts until you press Start up.";
$("power-btn").onclick = async () => {
    if (!S || S.switching) return;
    if (S.off) act("POST", "/api/startup");
    else if (await ask("Shut Gizmo down?", SHUTDOWN_DETAIL, "Shut down")) act("POST", "/api/shutdown");
};
$("off-start").onclick = () => act("POST", "/api/startup");

// Talk
$("turns").onclick = (ev) => {
    const li = ev.target.closest("[data-turn]");
    if (!li) return;
    selTurn = li.dataset.turn === turns().at(-1)?.id ? null : li.dataset.turn;
    if (S) renderTalk(S);
};
$("new-convo").onclick = () => { selTurn = null; act("POST", "/api/conversation/new"); };
$("say-form").onsubmit = (ev) => {
    ev.preventDefault();
    const text = $("say").value.trim();
    if (!text) return;
    $("say").value = "";
    selTurn = null;
    act("POST", "/api/say", { text });
};
$("interrupt").onclick = () => act("POST", "/api/interrupt");
$("track").onclick = () => act("POST", "/api/track", { on: !(S && S.tracking) });
$("sit-up").onclick = () => act("POST", "/api/motors/recover");
$("hide-reveal").onclick = () => (S && S.asleep ? act("POST", "/api/reveal", { greet: $("reveal-greet").checked }) : act("POST", "/api/hide"));
$("reveal").onclick = () => act("POST", "/api/reveal", { greet: $("reveal-greet").checked });
$("reveal-greet").onchange = (ev) => call("POST", "/api/greet", { on: ev.target.checked }).then((g) => { G = g; }).catch(() => {});
$("introduce").onclick = () => { selTurn = null; act("POST", "/api/introduce"); };
$("inplay-edit").onclick = () => { pKey = S?.personality || null; show("people"); };

// Personalities
$("p-list").onclick = (ev) => {
    const li = ev.target.closest("[data-p]");
    if (li) { pKey = li.dataset.p; renderPeople(); }
};
const blank = (from) => ({ key: "", name: from ? `${from.name} copy` : "", prompt: from?.prompt || "", delivery: from?.delivery || "", greeting: from?.greeting || "",
    voice: from ? structuredClone(from.voice) : { kind: "nova", nova: "tiffany", builtin: {} },
    tools: from ? structuredClone(from.tools) : { servers: {}, skills: [] } });
$("p-new").onclick = () => { pNew = blank(null); pKey = NEW; renderPeople(); $("p-name").focus(); };
$("p-duplicate").onclick = () => { pNew = blank(current()); pKey = NEW; renderPeople(); $("p-name").focus(); };
$("p-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const key = pKey === NEW ? slug($("p-name").value) : pKey;
    if (!key) return toast("Give it a name that starts with a letter", true);
    if (pKey === NEW && P.personalities[key]) return toast(`There's already a personality called ${key}`, true);
    const body = { name: $("p-name").value, prompt: $("p-prompt").value, delivery: $("p-delivery").value, greeting: $("p-greeting").value,
        voice: { kind: $("p-kind").dataset.kind, nova: $("p-nova").value, builtin: readBuiltin() }, tools: readTools() };
    P = await call("PUT", `/api/personalities/${encodeURIComponent(key)}`, body).catch(() => null) || P;
    if (!P.personalities[key]) return;
    pKey = key;
    toast(key === P.active ? "Saved. Gizmo is reloading with it." : "Saved");
    renderPeople();
    renderInPlay();
};
$("p-use").onclick = async () => { await act("POST", "/api/personality", { key: pKey }); await loadPeople(); };
$("p-reset").onclick = async () => {
    if (!await ask(`Reset ${current().name}?`, "Its prompt, voice and tools go back to how it shipped. Its voice clips stay.", "Reset")) return;
    P = await call("POST", `/api/personalities/${pKey}/reset`).catch(() => P);
    renderPeople();
};
$("p-delete").onclick = async () => {
    if (!await ask(`Delete ${current().name}?`, "Its prompt, voice and clips all go.", "Delete")) return;
    P = await call("DELETE", `/api/personalities/${pKey}`).catch(() => P);
    pKey = null;
    renderPeople();
};
$("p-kind").onclick = (ev) => { const b = ev.target.closest("[data-kind]"); if (b) setKind(b.dataset.kind); };
$("p-nova").onchange = () => setKind($("p-kind").dataset.kind);
$("p-voice-note").onclick = (ev) => {
    if (ev.target.id !== "p-nova-default") return;
    $("p-nova").value = current().defaults.voice.nova;
    setKind("nova");
};
for (const k of Object.keys(BUILTIN_RANGES)) $(`b-${k}`).oninput = (ev) => { $(`b-${k}-val`).textContent = BUILTIN_RANGES[k](Number(ev.target.value)); };
// The player is pointed at the preview and started inside the click itself: an embedded browser (Reachy Mini
// Control's panel) may refuse a sound that starts only after a fetch has come back.
function hear(body) {
    const key = pKey === NEW ? P.active : pKey;
    const q = new URLSearchParams({ t: String(Date.now()), ...(body.builtin ? { builtin: JSON.stringify(body.builtin) } : {}) });
    const player = $("player");
    player.onerror = async () => { // ask again the plain way: that plays it if the URL was the trouble, and says why if not
        player.onerror = null;
        const resp = await call("POST", `/api/personalities/${key}/preview`, body, { raw: true }).catch(() => null);
        if (!resp) return;
        player.src = URL.createObjectURL(await resp.blob());
        player.play().catch(() => {});
    };
    player.src = `/api/personalities/${key}/preview?${q}`;
    player.play().catch((e) => { if (e.name === "NotAllowedError") toast("Your browser blocked the sound: click Hear it again", true); });
}
$("b-hear").onclick = () => hear({ builtin: readBuiltin() });
$("clip-hear").onclick = () => hear({});
$("clip-save").onclick = async () => {
    const file = $("clip-file").files[0];
    const body = { ref_text: $("clip-text").value, ...(file ? { filename: file.name, data: await fileB64(file) } : {}) };
    const out = await call("PUT", `/api/personalities/${pKey}/clip`, body).catch(() => null);
    if (out) { P = out; toast(out.notice || "Saved"); renderPeople(); }
};
$("catch-add").onclick = async () => {
    const file = $("catch-file").files[0];
    if (!file || !$("catch-text").value.trim()) return toast("A sound clip needs its words and its recording", true);
    const out = await call("POST", `/api/personalities/${pKey}/catchphrases`, { text: $("catch-text").value, name: $("catch-name").value, filename: file.name, data: await fileB64(file) }).catch(() => null);
    if (out) { P = out; $("catch-text").value = ""; $("catch-name").value = ""; $("catch-file").value = ""; toast(out.notice || "Added"); renderPeople(); }
};
// Play, restore and remove, for the voice clip's panel and the sound clips' (both list recordings the same way).
$("p-clone").onclick = $("p-sounds").onclick = async (ev) => {
    const b = ev.target.closest("button");
    if (!b || b.id === "catch-add") return;
    if (b.dataset.play) { $("player").src = `/api/personalities/${pKey}/file/${encodeURIComponent(b.dataset.play)}?t=${Date.now()}`; $("player").play().catch(() => {}); }
    let out = null;
    if (b.dataset.restore) out = await call("POST", `/api/personalities/${pKey}/clip/versions/${b.dataset.restore}/restore`).catch(() => null);
    else if (b.dataset.delversion && await ask("Delete this saved version of the voice?", "", "Delete")) out = await call("DELETE", `/api/personalities/${pKey}/clip/versions/${b.dataset.delversion}`).catch(() => null);
    else if (b.dataset.delcatch !== undefined && await ask("Remove this sound clip?", "", "Remove")) out = await call("DELETE", `/api/personalities/${pKey}/catchphrases/${b.dataset.delcatch}`).catch(() => null);
    if (out) { P = out; if (out.notice) toast(out.notice); renderPeople(); }
};
$("p-tools").onclick = async (ev) => {
    const again = ev.target.closest("[data-approve]");
    if (again) { // a ticked skill whose files changed: approve it as its server lists it now
        const body = { server: again.closest(".pick").dataset.server, uri: again.dataset.approve };
        const out = await call("POST", `/api/personalities/${pKey}/approve`, body).catch(() => null);
        if (out) { P = out; toast("Approved as it is now"); renderPeople(); renderInPlay(); }
        return;
    }
    const b = ev.target.closest("[data-all], [data-none]");
    if (!b) return;
    const kind = b.dataset.all || b.dataset.none;
    for (const box of $$(`[data-${kind}]`, b.closest(".pick"))) box.checked = "all" in b.dataset;
    countTools(b.closest(".pick"));
};
function countTools(pick) {
    const boxes = $$("[data-tool]", pick), label = pick.querySelector('[data-count="tool"]');
    if (label) label.textContent = `${boxes.filter((x) => x.checked).length} of ${boxes.length} on`;
}
$("p-tools").onchange = (ev) => {
    if (ev.target.matches("[data-on]")) ev.target.closest(".pick").querySelector(".body").hidden = !ev.target.checked;
    if (ev.target.matches("[data-tool]")) countTools(ev.target.closest(".pick"));
};

// Servers
$("s-list").onclick = (ev) => { const li = ev.target.closest("[data-server]"); if (li) { sSel = { server: li.dataset.server }; renderServers(); } };
$("k-list").onclick = async (ev) => {
    const li = ev.target.closest("[data-skill]");
    if (!li) return;
    const k = await call("GET", `/api/skills/${li.dataset.skill}`).catch(() => null);
    if (k) { sSel = { skill: k.name, content: k.content }; renderServers(); }
};
$("s-new").onclick = () => { sSel = { form: { name: "", url: "", headers: {} } }; renderServers(); $("f-name").focus(); };
$("k-new").onclick = () => { sSel = { skill: null }; renderServers(); };
$("k-upload").onclick = () => $("k-file").click();
$("k-file").onchange = async (ev) => {
    const file = ev.target.files[0];
    ev.target.value = "";
    if (!file) return;
    const out = await call("POST", "/api/skills/upload", { filename: file.name, data: await fileB64(file) }).catch(() => null);
    if (out) { V = out; toast(`Installed ${out.installed}, and switched on for ${out.active_name}`); renderServers(); loadPeople(); }
};
$("s-detail").onclick = async (ev) => {
    const b = ev.target.closest("[data-act]");
    if (!b) return;
    const what = b.dataset.act, name = sSel?.server;
    const busy = async (fn) => { b.disabled = true; try { return await fn(); } finally { b.disabled = false; } };
    if (what === "cancel") { sSel = null; renderServers(); }
    else if (what === "edit") { const x = V.servers.find((s) => s.name === name); sSel = { form: { original: x.name, name: x.name, url: x.url, headers: x.headers } }; renderServers(); }
    else if (what === "add-header") $("f-headers").insertAdjacentHTML("beforeend", headerRow());
    else if (what === "del-header") b.closest(".kv").remove();
    else if (what === "test") $("f-out").innerHTML = summarize(await busy(() => call("POST", "/api/servers/test", readServerForm()).catch((e) => ({ ok: false, error: e.message }))));
    else if (what === "save-server") {
        const server = readServerForm(), original = sSel.form.original;
        const out = await busy(() => call("PUT", "/api/servers", { server, original_name: original }).catch(() => null));
        if (out) { V = out; sSel = { server: server.name }; toast(original ? "Saved" : `Added, and switched on for ${out.active_name}`); renderServers(); loadPeople(); }
    } else if (what === "refresh") { const out = await busy(() => call("POST", `/api/servers/${name}/refresh`).catch(() => null)); if (out) { V = out; renderServers(); loadPeople(); } }
    else if (what === "remove") {
        if (!await ask(`Remove ${name}?`, "Personalities that use it lose its tools and skills.", "Remove")) return;
        V = await call("DELETE", `/api/servers/${name}`).catch(() => V);
        sSel = null; renderServers(); loadPeople();
    } else if (what === "read-skill") {
        const item = b.closest(".item"), out = item.querySelector(".out");
        const r = await busy(() => call("POST", `/api/servers/${name}/skill`, { uri: item.dataset.uri }).catch((e) => ({ ok: false, error: e.message })));
        if (!r.ok) { out.innerHTML = `<div class="facts"><span class="tag bad">not loaded</span></div><pre class="code">${esc(r.error)}</pre>`; return; }
        const files = r.entry.dynamic ? "" : `<table>${r.entry.files.map((f) => {
            const rel = f.uri.slice(r.entry.root.length + 1);
            return `<tr><td>${rel === "SKILL.md" ? esc(rel) : `<button data-act="read-file" data-file="${esc(f.uri)}">${esc(rel)}</button>`}</td><td>${kb(f.size)}</td><td>${esc(f.digest.slice(7, 19))}…</td></tr>`;
        }).join("")}</table>`;
        out.innerHTML = `<div class="facts"><span class="tag ${r.verified ? "ok" : "warn"}">${r.verified ? "size, SHA-256 and frontmatter match the listing" : "generated on request: nothing to check against"}</span>
            <span class="tag">${kb(r.bytes)}</span></div>${files}<pre class="code">${esc(r.text)}</pre><div class="file-out"></div>`;
    } else if (what === "read-file") {
        const item = b.closest(".item");
        const r = await call("POST", `/api/servers/${name}/file`, { skill: item.dataset.uri, uri: b.dataset.file }).catch((e) => ({ ok: false, error: e.message }));
        item.querySelector(".file-out").innerHTML = `<p class="mono dim">${esc(b.dataset.file)}</p><pre class="code">${esc(r.ok ? r.text : r.error)}</pre>`;
    } else if (what === "run-tool") {
        const item = b.closest(".item"), out = item.querySelector(".out"), box = item.querySelector("textarea");
        let args = {};
        try { args = box ? JSON.parse(box.value || "{}") : {}; } catch { return toast("The arguments aren't valid JSON", true); }
        const r = await busy(() => call("POST", `/api/servers/${name}/call`, { tool: item.dataset.tool, arguments: args }).catch((e) => ({ ok: false, text: e.message })));
        out.innerHTML = `<div class="facts"><span class="tag ${r.ok ? "ok" : "bad"}">${r.ok ? "ok" : "error"}</span>${r.ms != null ? `<span class="tag">${secs(r.ms)}</span>` : ""}</div><pre class="code">${esc(r.text || "(nothing)")}</pre>`;
    } else if (what === "save-skill") {
        const content = $("k-content").value;
        const skill = sSel.skill ?? (/^name:\s*["']?([a-z0-9-]+)/m.exec(content) || [])[1];
        if (!skill) return toast("Give it a name in the frontmatter: lowercase letters, digits and hyphens", true);
        const out = await busy(() => call("PUT", `/api/skills/${skill}`, { content }).catch(() => null));
        if (out) { V = out; sSel = { skill, content }; toast("Saved"); renderServers(); loadPeople(); }
    } else if (what === "delete-skill") {
        if (!await ask(`Delete the skill ${sSel.skill}?`, "It's removed from the robot and from every personality that uses it.", "Delete")) return;
        V = await call("DELETE", `/api/skills/${sSel.skill}`).catch(() => V);
        sSel = null; renderServers(); loadPeople();
    }
};

// Settings (rendered as one block, so its handlers are delegated)
const setG = (out) => { if (out) { G = out; renderSettings(); } };
$("settings").onclick = async (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.provider) { mDraft = b.dataset.provider; renderSettings(); }
    else if (b.dataset.speech) setG(await call("POST", "/api/settings/speech", { speech: b.dataset.speech }).catch(() => null));
    else if (b.dataset.listen) setG(await call("POST", "/api/listening", { mode: b.dataset.listen }).catch(() => null));
    else if (b.dataset.act === "health") { H = await call("GET", "/api/health").catch(() => H); renderSettings(); }
    else if (b.dataset.act === "to-people") show("people");
    else if (b.dataset.act === "flip-doa") { setG(await call("POST", "/api/face-speaker", { flip: true }).catch(() => null)); toast("Swapped. Speak to it from one side to check."); }
    else if (b.dataset.act === "save-voices") {
        const out = await call("POST", "/api/voice-servers", { urls: $("g-voices").value.split("\n") }).catch(() => null);
        if (!out) return;
        setG(out);
        $("g-voices-out").innerHTML = Object.entries(out.tested).map(([u, t]) => `<p class="hint"><span class="tag ${t.ok ? "ok" : "bad"}">${t.ok ? "answering" : "no answer"}</span> ${esc(u)}`
            + `${t.ok ? `: ${t.cloning ? "cloning" : "no cloning"}, ${plural((t.builtin || []).length, "built-in voice")}` : ""}</p>`).join("") || `<p class="hint">None: Nova Sonic speaks.</p>`;
        if (Object.values(out.tested).some((t) => t.ok)) $("g-voices-out").insertAdjacentHTML("beforeend", VOICE_NEXT);
    } else if (b.dataset.act === "test-model") {
        const r = await call("POST", "/api/settings/model/test", { base_url: $("m-url").value, api_key: $("m-key").value }).catch((e) => ({ ok: false, error: e.message }));
        $("m-out").innerHTML = r.ok ? `<div class="facts"><span class="tag ok">answering</span><span class="tag">${plural(r.models.length, "model")}</span></div><p class="hint mono">${esc(r.models.slice(0, 30).join(", "))}</p>`
            : `<div class="facts"><span class="tag bad">${esc(r.error)}</span></div>`;
        if (r.ok) $("m-models").innerHTML = r.models.map((m) => `<option value="${esc(m)}">`).join("");
    } else if (b.dataset.act === "save-model") {
        const provider = mDraft || G.model.provider;
        const body = { provider };
        if (provider === "bedrock") body.bedrock = { model: $("m-bedrock").value || $("m-bedrock-other").value.trim() };
        if (provider === "openai") {
            let params = {};
            try { params = JSON.parse($("m-params").value || "{}"); } catch { return toast("The extra parameters aren't valid JSON", true); }
            body.openai = { base_url: $("m-url").value, model: $("m-model").value, api_key: $("m-key").value, params };
        }
        if (provider === "agentcore") body.agentcore = { arn: $("m-arn").value };
        const out = await call("POST", "/api/settings/model", body).catch(() => null);
        if (out) { mDraft = null; setG(out); toast("Saved. Gizmo is reloading with it."); }
    }
};
$("settings").onchange = async (ev) => {
    const t = ev.target;
    if (t.id === "m-bedrock") $("m-bedrock-other-box").hidden = t.value !== "";
    else if (t.id === "g-barge") setG(await call("POST", "/api/barge-in", { on: t.checked }).catch(() => null));
    else if (t.id === "g-ears") setG(await call("POST", "/api/ears", { end_silence_ms: Number(t.value) }).catch(() => null));
    else if (t.id === "g-names") setG(await call("POST", "/api/listening", { names: t.value.split(",").map((n) => n.trim()).filter(Boolean) }).catch(() => null));
    else if (t.id === "g-follow") setG(await call("POST", "/api/listening", { follow_up_s: Number(t.value) }).catch(() => null));
    else if (t.id === "g-greet") setG(await call("POST", "/api/greet", { on: t.checked }).catch(() => null));
    else if (t.id === "g-face") setG(await call("POST", "/api/face-speaker", { on: t.checked }).catch(() => null));
    else if (t.id === "g-ignored") setG(await call("POST", "/api/listening", { show_ignored: t.checked }).catch(() => null));
    else if (t.id === "g-boost") setG(await call("POST", "/api/boost", { db: Number(t.value) }).catch(() => null));
};
$("settings").oninput = (ev) => {
    const t = ev.target;
    if (t.id === "g-ears") $("g-ears-val").textContent = `${(Number(t.value) / 1000).toFixed(2)} s`;
    if (t.id === "g-follow") $("g-follow-val").textContent = `${t.value} s`;
    if (t.id === "g-boost") $("g-boost-val").textContent = `${t.value} dB`;
};

// The state, again and again: more often while a reply is on its way or being spoken, so that its words can be
// lit up in time with the voice.
async function poll() {
    await refresh();
    setTimeout(poll, S && (S.speaking || thinking()) ? 250 : 700);
}
refresh().then(() => { loadPeople(); loadSettings(); setTimeout(poll, 700); });
checkConsoleVersion();
if (["people", "servers", "settings"].includes(location.hash.slice(1))) show(location.hash.slice(1));
setInterval(checkConsoleVersion, 15000);
