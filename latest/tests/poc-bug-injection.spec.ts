import { test, expect, type Page, type TestInfo } from "@playwright/test";
import fs from "node:fs";
import { PNG } from "pngjs";
import pixelmatch from "pixelmatch";

/**
 * PoC: Data-driven visual-bug injection on TodoMVC (tastejs/todomvc).
 *
 * Target: the official hosted static build of the React example.
 * No cookie walls, no login, fully deterministic UI — ideal for a clean
 * control group. Override with TODOMVC_URL to point at a local build, e.g.
 *   TODOMVC_URL=http://localhost:8080 npx playwright test
 */
const TARGET_URL =
  process.env.TODOMVC_URL ?? "https://todomvc.com/examples/react/dist/";

/**
 * How many videos to generate per group. 60 + 6 bug types x 10 = 120 cases,
 * a 60/60 normal-vs-anomalous split with equal allocation per bug type.
 * Frozen for EXP001 — see experiments/exp001/config.frozen.json.
 */
const CONTROL_CASES = 60;
const CASES_PER_BUG = 10;

/**
 * How long the post-gate (defective) state is held on screen, in ms.
 *
 * Playwright finalises the recording asynchronously when the context closes,
 * so the tail of the video can be lost. dataset_v1 was silently corrupted this
 * way: the defect lived only in the final ~2.5s, and in several clips
 * (notably every flyaway_offset and 3/5 vanish_invisible) that window was
 * truncated away — leaving a video labelled "anomaly" that shows a perfectly
 * normal app. Holding far longer than any observed truncation keeps the defect
 * unmistakably present in the surviving footage.
 *
 * This window is applied identically to BOTH groups: if buggy clips ran shorter
 * than control clips (as they did in v1, because typing is skipped when the app
 * is invisible), clip duration would itself correlate with the label and leak
 * the answer to anything that samples frames.
 */
const DEFECT_HOLD_MS = 7000;

// ---------------------------------------------------------------------------
// 1. Scenario diversification — randomized user behavior
//    Even within the same page, each run performs a different interaction
//    sequence so control-group videos never look identical.
// ---------------------------------------------------------------------------

const TODO_PHRASES = [
  "Buy groceries for the week",
  "Finish the MSc thesis draft",
  "Review Playwright test results",
  "Book flight tickets to Taipei",
  "Reply to supervisor's email",
  "Clean the apartment",
  "Prepare slides for Friday demo",
  "Run 5km in the park",
  "Read one chapter of the textbook",
  "Water the plants",
  "Back up the experiment dataset",
  "Schedule dentist appointment",
];

/**
 * Master seed for every random choice in the generator. Frozen for EXP001;
 * override with RNG_SEED=... to produce an independent replicate.
 */
const RNG_SEED = Number(process.env.RNG_SEED ?? 20260725);

type Rng = () => number;

/**
 * mulberry32 — a compact, well-distributed 32-bit PRNG.
 *
 * Seeded PER SCENARIO rather than globally, because tests run in parallel: with
 * one shared stream the values a case draws would depend on worker count and
 * completion order, so the same seed would not rebuild the same dataset. Keying
 * each scenario's seed off its id makes a case's random choices a pure function
 * of (RNG_SEED, scenario.id) — reproducible at any --workers setting.
 */
function mulberry32(seed: number): Rng {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** FNV-1a — folds a scenario id into a 32-bit seed offset. */
function hashString(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

function rngForScenario(id: string): Rng {
  return mulberry32((RNG_SEED ^ hashString(id)) >>> 0);
}

function randInt(rng: Rng, min: number, max: number): number {
  return Math.floor(rng() * (max - min + 1)) + min;
}

/**
 * Uniform random sample via Fisher-Yates. Replaces the previous
 * `sort(() => rng() - 0.5)` shuffle, which was not uniform (a comparison sort
 * driven by a random comparator biases toward the input order) and whose result
 * depended on the engine's sort implementation — neither acceptable once the
 * dataset is meant to be reproducible.
 */
function sample<T>(rng: Rng, arr: T[], n: number): T[] {
  const out = [...arr];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out.slice(0, n);
}

/**
 * Seed the page with a randomized todo state: a random number of todos,
 * then optionally toggle some as completed, delete one, and switch filters.
 */
async function runRandomUserScenario(page: Page, rng: Rng): Promise<void> {
  const input = page.locator(".new-todo");
  const todos = sample(rng, TODO_PHRASES, randInt(rng, 3, 6));

  for (const text of todos) {
    await input.fill(text);
    await input.press("Enter");
    await page.waitForTimeout(randInt(rng, 200, 600)); // human-like variable pacing
  }

  // ~70% of runs: mark 1-2 random todos as completed
  if (rng() < 0.7) {
    const toggles = page.locator(".todo-list li .toggle");
    const count = await toggles.count();
    for (const idx of sample(rng, [...Array(count).keys()], randInt(rng, 1, 2))) {
      await toggles.nth(idx).click();
      await page.waitForTimeout(randInt(rng, 200, 500));
    }
  }

  // ~40% of runs: delete one random todo
  if (rng() < 0.4) {
    const items = page.locator(".todo-list li");
    const count = await items.count();
    if (count > 1) {
      const victim = items.nth(randInt(rng, 0, count - 1));
      await victim.hover();
      await victim.locator(".destroy").click();
      await page.waitForTimeout(randInt(rng, 200, 500));
    }
  }

  // ~50% of runs: walk through the filters and come back to "All"
  if (rng() < 0.5) {
    for (const name of sample(rng, ["Active", "Completed"], randInt(rng, 1, 2))) {
      await page.getByRole("link", { name }).click();
      await page.waitForTimeout(randInt(rng, 300, 700));
    }
    await page.getByRole("link", { name: "All" }).click();
    await page.waitForTimeout(randInt(rng, 200, 400));
  }
}

// ---------------------------------------------------------------------------
// 2. Bug Config Matrix — diversified, exaggerated CSS defect injection
//    Each entry generates a malicious stylesheet (with randomized magnitude)
//    and declares the minimum pixel-diff ratio it must produce to count as a
//    "clearly visible" bug.
// ---------------------------------------------------------------------------

interface BugConfig {
  key: string;
  label: string;
  /** Quality gate: buggy screenshot must differ by at least this ratio. */
  minDiffRatio: number;
  /**
   * Returns the malicious CSS to inject. Magnitudes are drawn from the
   * scenario's seeded RNG, so they are randomized across cases but
   * reproducible for a given (RNG_SEED, scenario.id).
   */
  buildCss: (rng: Rng) => string;
}

// Gate thresholds below were calibrated empirically against the live page
// (measured ratios: skew ~28%, blur ~7%, vanish ~55%, flyaway ~66%,
// flicker ~50%, displaynone ~47%; control = 0.0000%).
const BUG_MATRIX: BugConfig[] = [
  {
    key: "skew_distortion",
    label: "Severe geometric distortion (skew + rotate)",
    minDiffRatio: 0.1,
    buildCss: (rng) => {
      const skew = randInt(rng, 25, 35);
      const rot = randInt(rng, 8, 15);
      return `
        .todoapp {
          transform: skewX(${skew}deg) rotate(-${rot}deg) scale(1.2) !important;
        }`;
    },
  },
  {
    key: "blur_unreadable",
    label: "Whole app blurred beyond readability",
    minDiffRatio: 0.05,
    buildCss: (rng) => {
      const blur = randInt(rng, 10, 14);
      return `
        .todoapp {
          filter: blur(${blur}px) contrast(1.5) !important;
        }`;
    },
  },
  {
    key: "vanish_invisible",
    label: "Entire app mysteriously invisible (opacity 0)",
    minDiffRatio: 0.15,
    // Hiding only inner sections leaves the white card intact (~3% diff);
    // vanishing the whole card exposes the grey page background → huge diff.
    buildCss: () => `
        .todoapp {
          opacity: 0 !important;
          visibility: hidden !important;
        }`, // deterministic by construction — no magnitude to randomize
  },
  {
    key: "flyaway_offset",
    label: "App flies off-screen (huge negative margin → massive layout shift)",
    minDiffRatio: 0.15,
    buildCss: (rng) => {
      const up = randInt(rng, 450, 700);
      const left = randInt(rng, 150, 300);
      return `
        .todoapp {
          margin-top: -${up}px !important;
          margin-left: -${left}px !important;
        }`;
    },
  },
  {
    key: "flicker_seizure",
    label: "Elements flicker violently between two broken states",
    minDiffRatio: 0.15,
    // Both keyframe states are drastically different from the clean baseline
    // (color swap instead of pure opacity), so the quality-gate screenshot
    // registers a large diff no matter which frame it lands on.
    buildCss: (rng) => {
      // One shift value shared by both halves of the second state. The previous
      // version drew it twice, which under `steps(1)` left the 100% keyframe's
      // value unused while still consuming an RNG draw.
      const shift = randInt(rng, 30, 60);
      return `
        @keyframes poc-flicker {
          0%   { background: #ff3b30 !important; transform: translateX(0); }
          49%  { background: #ff3b30 !important; transform: translateX(0); }
          50%  { background: #0033ff !important; transform: translateX(${shift}px); }
          100% { background: #0033ff !important; transform: translateX(${shift}px); }
        }
        .todoapp .new-todo,
        .todo-list li {
          animation: poc-flicker 0.12s steps(1) infinite !important;
          background: #ff3b30 !important;
        }`;
    },
  },
  {
    key: "displaynone_collapse",
    label: "Key section removed with display:none → content below jumps up",
    minDiffRatio: 0.12,
    buildCss: () => `
        .todoapp .main {
          display: none !important;
        }`,
  },
];

// ---------------------------------------------------------------------------
// 3. Dual-layer automated Quality Gate (pixelmatch)
// ---------------------------------------------------------------------------

/**
 * Compare two same-size viewport screenshots; returns the ratio of pixels
 * that differ. Also writes a diff.png heat-map for auditing.
 */
function pixelDiffRatio(
  normalPath: string,
  buggyPath: string,
  diffPath: string,
): number {
  const a = PNG.sync.read(fs.readFileSync(normalPath));
  const b = PNG.sync.read(fs.readFileSync(buggyPath));
  if (a.width !== b.width || a.height !== b.height) {
    throw new Error(
      `Screenshot size mismatch: ${a.width}x${a.height} vs ${b.width}x${b.height}`,
    );
  }
  const diff = new PNG({ width: a.width, height: a.height });
  const changedPixels = pixelmatch(a.data, b.data, diff.data, a.width, a.height, {
    // Calibrated for TodoMVC's low-contrast palette: the white app card
    // (#fff) sits on a #f5f5f5 page background, a luminance gap pixelmatch
    // only registers with threshold < ~0.038. Safe because control-group
    // screenshots of this static page are byte-identical (measured 0.0000%).
    threshold: 0.03,
  });
  fs.writeFileSync(diffPath, PNG.sync.write(diff));
  return changedPixels / (a.width * a.height);
}

/**
 * Remove nondeterministic pixels (blinking caret, hover state, focus ring)
 * so the control group can genuinely hit a 0% diff.
 */
async function stabilizeForScreenshot(page: Page): Promise<void> {
  await page.evaluate(() => {
    (document.activeElement as HTMLElement | null)?.blur();
  });
  await page.mouse.move(0, 0);
  await page.waitForTimeout(300);
}

interface ClipRegion {
  x: number;
  y: number;
  width: number;
  height: number;
}

// ---------------------------------------------------------------------------
// 3b. DOM signal capture (Path B's structural channel)
// ---------------------------------------------------------------------------

interface DomMutation {
  t: number;
  type: string;
  tag: string | null;
  cls: string | null;
  attr: string | null;
  added: number;
  removed: number;
}

/**
 * Install a MutationObserver that timestamps every DOM mutation in the app.
 *
 * IMPORTANT: we observe document.body only. page.addStyleTag() appends the
 * malicious stylesheet to <head>, so the injection itself is structurally
 * invisible to this trace and cannot leak the answer. What remains is the
 * legitimate signal: DOM activity caused by real user actions.
 */
async function installDomRecorder(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const trace: DomMutation[] = [];
    (window as unknown as { __domTrace: DomMutation[] }).__domTrace = trace;
    const t0 = performance.now();
    const observer = new MutationObserver((records) => {
      for (const r of records) {
        if (trace.length >= 3000) return; // cap: keep the artifact small
        const el = r.target as HTMLElement;
        // The screencast heartbeat dot flips its style 4x/second; recording
        // that would drown the real user-driven signal.
        if (el && el.id === "__rec_heartbeat") continue;
        trace.push({
          t: Math.round(performance.now() - t0),
          type: r.type,
          tag: el.nodeName ? el.nodeName.toLowerCase() : null,
          cls:
            typeof el.className === "string" ? el.className.slice(0, 80) : null,
          attr: r.attributeName ?? null,
          added: r.addedNodes.length,
          removed: r.removedNodes.length,
        });
      }
    });
    const start = () =>
      observer.observe(document.body, {
        subtree: true,
        childList: true,
        attributes: true,
        characterData: true,
      });
    if (document.body) start();
    else document.addEventListener("DOMContentLoaded", start);
  });
}

/**
 * Fixed rectangle around the app's pre-injection position (plus padding),
 * clamped to the viewport. Diffing only this region keeps the metric about
 * the app itself instead of being diluted by the page's empty margins; the
 * rectangle stays fixed, so an app that flies away out of it still counts.
 */
async function appClipRegion(page: Page): Promise<ClipRegion> {
  const box = await page.locator(".todoapp").boundingBox();
  const vp = page.viewportSize();
  if (!box || !vp) throw new Error("Cannot resolve .todoapp bounding box");
  const pad = 60;
  const x = Math.max(0, box.x - pad);
  const y = Math.max(0, box.y - pad);
  return {
    x,
    y,
    width: Math.min(vp.width - x, box.width + 2 * pad),
    height: Math.min(vp.height - y, box.height + 2 * pad),
  };
}

async function captureScreenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
  clip: ClipRegion,
): Promise<string> {
  const path = testInfo.outputPath(name);
  await stabilizeForScreenshot(page);
  await page.screenshot({ path, clip }); // same clip → identical dimensions
  await testInfo.attach(name, { path, contentType: "image/png" });
  return path;
}

/**
 * Keep screencast frames flowing while the defective state is held.
 *
 * Playwright's recorder only receives a frame when the page repaints. A static
 * broken state (vanished app, app pushed off-screen) stops repainting after
 * the injection, so the whole hold window produces zero frames and the single
 * defect frame can still lose the async close race — the exact corruption the
 * hold was meant to prevent (and why animated bugs like flicker always
 * survived while vanish/flyaway did not).
 *
 * The heartbeat is a small grey dot in the bottom-right viewport corner that
 * toggles shade 4x/second, forcing a steady ~4fps into the encoder. It is
 * fair and non-leaking by construction: injected AFTER the gate screenshots
 * (pixelmatch never sees it), outside the app clip region (the video gate
 * never sees it), identical in both groups, and filtered from the DOM trace.
 */
async function installScreencastHeartbeat(page: Page): Promise<void> {
  await page.evaluate(() => {
    const dot = document.createElement("div");
    dot.id = "__rec_heartbeat";
    dot.style.cssText =
      "position:fixed;right:6px;bottom:6px;width:10px;height:10px;" +
      "border-radius:50%;background:#999;z-index:2147483647;pointer-events:none;";
    document.body.appendChild(dot);
    let on = false;
    setInterval(() => {
      on = !on;
      dot.style.background = on ? "#999" : "#ccc";
    }, 250);
  });
}

// ---------------------------------------------------------------------------
// 4. Test scenario list: control group + bug matrix, all data-driven
// ---------------------------------------------------------------------------

interface Scenario {
  id: string;
  group: "control" | "bug";
  bug?: BugConfig;
}

const scenarios: Scenario[] = [
  ...Array.from({ length: CONTROL_CASES }, (_, i): Scenario => ({
    id: `control_normal_${i + 1}`,
    group: "control",
  })),
  ...BUG_MATRIX.flatMap((bug) =>
    Array.from({ length: CASES_PER_BUG }, (_, i): Scenario => ({
      id: `bug_${bug.key}_${i + 1}`,
      group: "bug",
      bug,
    })),
  ),
];

for (const scenario of scenarios) {
  const title =
    scenario.group === "control"
      ? `[Control/Normal] ${scenario.id}`
      : `[Bug/${scenario.bug!.label}] ${scenario.id}`;

  test(title, async ({ page }, testInfo) => {
    test.setTimeout(120_000);

    // Every random choice this case makes comes from here — one stream per
    // scenario, so parallel execution cannot perturb it.
    const rng = rngForScenario(scenario.id);

    // Must be installed before navigation so the observer sees the whole session.
    await installDomRecorder(page);

    await page.goto(TARGET_URL, { waitUntil: "networkidle" });

    // Hide the text caret globally (both groups) so an idle focused input
    // can never produce phantom pixel noise in the control group.
    await page.addStyleTag({ content: `* { caret-color: transparent !important; }` });

    // Randomized pre-injection user behavior → diverse "normal" footage
    await runRandomUserScenario(page, rng);

    // ── Quality Gate step 1: clean baseline ────────────────────────────────
    // Clip is computed once from the pre-injection layout and reused for the
    // buggy shot, so both screenshots cover the exact same region.
    const clip = await appClipRegion(page);
    const normalPng = await captureScreenshot(page, testInfo, "normal.png", clip);

    // ── Inject the defect (no-op for the control group) ────────────────────
    if (scenario.bug) {
      await page.addStyleTag({ content: scenario.bug.buildCss(rng) });
      await page.waitForTimeout(500); // let layout/animation settle visibly
    }

    // ── Quality Gate step 2: post-injection screenshot + pixelmatch ────────
    // NOTE: buggy.png is taken immediately after injection — before any
    // further typing — so the control group's diff is provably 0% (typing
    // between the two shots would itself change pixels and poison the gate).
    const buggyPng = await captureScreenshot(page, testInfo, "buggy.png", clip);
    const diffPng = testInfo.outputPath("diff.png");
    const ratio = pixelDiffRatio(normalPng, buggyPng, diffPng);
    await testInfo.attach("diff.png", { path: diffPng, contentType: "image/png" });

    console.log(
      `[GATE] ${scenario.id}: pixel diff ratio = ${(ratio * 100).toFixed(2)}%`,
    );

    if (scenario.group === "control") {
      // Control group: the frame must be perfectly clean — zero noise.
      expect(
        ratio,
        `Control case ${scenario.id} is not pixel-clean (${(ratio * 100).toFixed(2)}% diff) — video rejected`,
      ).toBe(0);
    } else {
      // Bug group: the defect must be unmistakably visible on screen.
      expect(
        ratio,
        `Injected bug "${scenario.bug!.key}" is too subtle ` +
          `(${(ratio * 100).toFixed(2)}% < required ${scenario.bug!.minDiffRatio * 100}%) — case rejected`,
      ).toBeGreaterThanOrEqual(scenario.bug!.minDiffRatio);
    }

    // Gate passed → persist the verdict so dataset-collection scripts can
    // harvest this case (failed cases never write this file).
    fs.writeFileSync(
      testInfo.outputPath("gate-report.json"),
      JSON.stringify(
        {
          id: scenario.id,
          group: scenario.group,
          bugKey: scenario.bug?.key ?? null,
          bugLabel: scenario.bug?.label ?? null,
          diffRatio: ratio,
          minDiffRatio: scenario.bug?.minDiffRatio ?? 0,
          targetUrl: TARGET_URL,
          browser: testInfo.project.name,
          viewport: page.viewportSize(),
          // The video gate needs this to map a decoded frame back onto the
          // exact region normal.png / buggy.png cover.
          clip,
          defectHoldMs: DEFECT_HOLD_MS,
          // Provenance for reproducibility: this case's random choices are a
          // pure function of these two values.
          rngSeed: RNG_SEED,
          rngScenarioSeed: (RNG_SEED ^ hashString(scenario.id)) >>> 0,
          generatedAt: new Date().toISOString(),
        },
        null,
        2,
      ),
    );

    // ── Post-gate: hold the (defective) state on screen ────────────────────
    // The window is a fixed wall-clock duration for BOTH groups. Interaction
    // is attempted but is best-effort: a vanished or flown-away app cannot be
    // typed into, and in v1 that shortened those clips — making duration
    // correlate with the label. Here the hold length is independent of whether
    // typing succeeded, so both groups yield comparable footage.
    await installScreencastHeartbeat(page);

    const holdUntil = Date.now() + DEFECT_HOLD_MS;
    const input = page.locator(".new-todo");
    // The typed text MUST NOT identify the scenario: typing the scenario id
    // (as an earlier version did) paints the ground-truth label — including
    // the bug name — into the video for any model that can read the screen.
    if (await input.isVisible().catch(() => false)) {
      await input
        .pressSequentially(sample(rng, TODO_PHRASES, 1)[0], {
          delay: randInt(rng, 40, 90),
          // Don't let Playwright's default 30s retry loop stall the clip when
          // the app is too broken to reach (e.g. pushed off-screen).
          timeout: 5000,
        })
        .catch(() => {
          /* app too broken to type into — that is itself valid footage */
        });
    }
    const remaining = holdUntil - Date.now();
    if (remaining > 0) await page.waitForTimeout(remaining);

    // Persist the DOM signal alongside the pixels. Note this trace contains
    // only body mutations (user-driven activity); the injected stylesheet lives
    // in <head> and is deliberately not observed.
    const domTrace = await page.evaluate(
      () => (window as unknown as { __domTrace?: DomMutation[] }).__domTrace ?? [],
    );
    fs.writeFileSync(
      testInfo.outputPath("domtrace.json"),
      JSON.stringify(domTrace),
    );

    console.log(
      `[SUCCESS] ${scenario.id}: video held ${DEFECT_HOLD_MS}ms post-injection, ` +
        `${domTrace.length} DOM mutations recorded`,
    );
  });
}
