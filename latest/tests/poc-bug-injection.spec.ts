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

/** How many videos to generate per group. */
const CONTROL_CASES = 5;
const CASES_PER_BUG = 2;

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

function randInt(min: number, max: number): number {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function sample<T>(arr: T[], n: number): T[] {
  return [...arr].sort(() => Math.random() - 0.5).slice(0, n);
}

/**
 * Seed the page with a randomized todo state: a random number of todos,
 * then optionally toggle some as completed, delete one, and switch filters.
 */
async function runRandomUserScenario(page: Page): Promise<void> {
  const input = page.locator(".new-todo");
  const todos = sample(TODO_PHRASES, randInt(3, 6));

  for (const text of todos) {
    await input.fill(text);
    await input.press("Enter");
    await page.waitForTimeout(randInt(200, 600)); // human-like variable pacing
  }

  // ~70% of runs: mark 1-2 random todos as completed
  if (Math.random() < 0.7) {
    const toggles = page.locator(".todo-list li .toggle");
    const count = await toggles.count();
    for (const idx of sample([...Array(count).keys()], randInt(1, 2))) {
      await toggles.nth(idx).click();
      await page.waitForTimeout(randInt(200, 500));
    }
  }

  // ~40% of runs: delete one random todo
  if (Math.random() < 0.4) {
    const items = page.locator(".todo-list li");
    const count = await items.count();
    if (count > 1) {
      const victim = items.nth(randInt(0, count - 1));
      await victim.hover();
      await victim.locator(".destroy").click();
      await page.waitForTimeout(randInt(200, 500));
    }
  }

  // ~50% of runs: walk through the filters and come back to "All"
  if (Math.random() < 0.5) {
    for (const name of sample(["Active", "Completed"], randInt(1, 2))) {
      await page.getByRole("link", { name }).click();
      await page.waitForTimeout(randInt(300, 700));
    }
    await page.getByRole("link", { name: "All" }).click();
    await page.waitForTimeout(randInt(200, 400));
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
  /** Returns the malicious CSS to inject. Magnitudes are randomized per run. */
  buildCss: () => string;
}

// Gate thresholds below were calibrated empirically against the live page
// (measured ratios: skew ~28%, blur ~7%, vanish ~55%, flyaway ~66%,
// flicker ~50%, displaynone ~47%; control = 0.0000%).
const BUG_MATRIX: BugConfig[] = [
  {
    key: "skew_distortion",
    label: "Severe geometric distortion (skew + rotate)",
    minDiffRatio: 0.1,
    buildCss: () => {
      const skew = randInt(25, 35);
      const rot = randInt(8, 15);
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
    buildCss: () => {
      const blur = randInt(10, 14);
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
        }`,
  },
  {
    key: "flyaway_offset",
    label: "App flies off-screen (huge negative margin → massive layout shift)",
    minDiffRatio: 0.15,
    buildCss: () => {
      const up = randInt(450, 700);
      const left = randInt(150, 300);
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
    buildCss: () => `
        @keyframes poc-flicker {
          0%   { background: #ff3b30 !important; transform: translateX(0); }
          49%  { background: #ff3b30 !important; transform: translateX(0); }
          50%  { background: #0033ff !important; transform: translateX(${randInt(30, 60)}px); }
          100% { background: #0033ff !important; transform: translateX(${randInt(30, 60)}px); }
        }
        .todoapp .new-todo,
        .todo-list li {
          animation: poc-flicker 0.12s steps(1) infinite !important;
          background: #ff3b30 !important;
        }`,
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
    test.setTimeout(90_000);

    await page.goto(TARGET_URL, { waitUntil: "networkidle" });

    // Hide the text caret globally (both groups) so an idle focused input
    // can never produce phantom pixel noise in the control group.
    await page.addStyleTag({ content: `* { caret-color: transparent !important; }` });

    // Randomized pre-injection user behavior → diverse "normal" footage
    await runRandomUserScenario(page);

    // ── Quality Gate step 1: clean baseline ────────────────────────────────
    // Clip is computed once from the pre-injection layout and reused for the
    // buggy shot, so both screenshots cover the exact same region.
    const clip = await appClipRegion(page);
    const normalPng = await captureScreenshot(page, testInfo, "normal.png", clip);

    // ── Inject the defect (no-op for the control group) ────────────────────
    if (scenario.bug) {
      await page.addStyleTag({ content: scenario.bug.buildCss() });
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

    // ── Post-gate interaction: capture dynamic frames for the video ────────
    // (User keeps interacting while the bug is live — or the app has broken
    // so badly that interaction is impossible, which is itself good footage.)
    const input = page.locator(".new-todo");
    if (await input.isVisible().catch(() => false)) {
      await input.pressSequentially(`post-injection check ${scenario.id}`, {
        delay: randInt(40, 90),
      });
    }
    await page.waitForTimeout(2000);

    console.log(`[SUCCESS] Video generated for scenario: ${scenario.id}`);
  });
}
