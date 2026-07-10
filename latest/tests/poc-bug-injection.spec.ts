import { test, type Page } from "@playwright/test";

// 1. Define defect taxonomy and randomized injection logic based on literature review
const testScenarios = [
  // --- Control Group: Normal UI Interactions (5 Cases) ---
  ...Array.from({ length: 5 }, (_, i) => ({
    id: `control_normal_${i + 1}`,
    type: "Normal",
    injectFn: () => {}, // Do nothing, maintain normal UI state
  })),

  // --- Test Group: Layer Overlap / Z-index Conflict (3 Cases) ---
  ...Array.from({ length: 3 }, (_, i) => ({
    id: `bug_zindex_${i + 1}`,
    type: "Z-index Conflict",
    injectFn: () => {
      const btn = document.querySelector('input[type="submit"], button');
      if (btn) {
        (btn as HTMLElement).style.position = "relative";
        (btn as HTMLElement).style.zIndex = "-1"; // Push behind background, visible but unclickable
      }
    },
  })),

  // --- Test Group: Layout Shift Regression (4 Cases) ---
  ...Array.from({ length: 4 }, (_, i) => ({
    id: `bug_layout_${i + 1}`,
    type: "Layout Shift",
    injectFn: () => {
      // 💡 FIX: Target the actual main wrapper of Google search bar or center alignment
      const searchContainer = document.querySelector(
        "div.A8w7Ue, div.tsf-p, form",
      );
      const body = document.querySelector("body");

      if (searchContainer) {
        const randomOffset = Math.floor(Math.random() * 300) + 150;
        (searchContainer as HTMLElement).style.position = "absolute";
        (searchContainer as HTMLElement).style.top = `${randomOffset}px`;
        (searchContainer as HTMLElement).style.left = "50px";
        (searchContainer as HTMLElement).style.transform = "rotate(-10deg)"; // Make it visibly crooked
      }

      // Bonus: Let's mess up the background color randomly to ensure visual anomaly
      if (body) {
        body.style.backgroundColor = "#f0f0f0";
      }
    },
  })),

  // --- Test Group: Content Invisibility Anomaly (3 Cases) ---
  ...Array.from({ length: 3 }, (_, i) => ({
    id: `bug_invisible_${i + 1}`,
    type: "Invisibility",
    injectFn: () => {
      // Target the top-right header navigation bar or the sign-in button
      const topHeader = document.querySelector(
        'div.gb_L, div.gb_V, a[href*="accounts.google.com"]',
      );
      if (topHeader) {
        (topHeader as HTMLElement).style.opacity = "0"; // Makes the sign-in button disappear completely
      }
    },
  })),
];

async function dismissGoogleConsent(page: Page) {
  const consentBanner = page
    .locator("text=/Before you continue to Google/i")
    .first();
  const isVisible = await consentBanner.isVisible().catch(() => false);

  if (!isVisible) {
    return;
  }

  const consentButtons = [
    page.getByRole("button", { name: /Reject all/i }).first(),
    page.getByRole("button", { name: /Accept all/i }).first(),
    page.getByRole("button", { name: /I agree/i }).first(),
    page.getByRole("button", { name: /Agree/i }).first(),
  ];

  for (const button of consentButtons) {
    if (await button.isVisible().catch(() => false)) {
      await button.click({ timeout: 2000 });
      break;
    }
  }

  await consentBanner
    .waitFor({ state: "hidden", timeout: 3000 })
    .catch(() => {});
}

// 2. Data-driven loop to execute all 15 test scenarios automatically
for (const scenario of testScenarios) {
  test(`Generate video for [${scenario.type}] - ID: ${scenario.id}`, async ({
    page,
  }) => {
    // FIX: Pre-set consent state and use a neutral locale to avoid the Google consent screen.
    await page.context().addCookies([
      {
        name: "CONSENT",
        value: "YES+1",
        domain: ".google.com",
        path: "/",
        secure: true,
        sameSite: "Lax",
      },
      {
        name: "CONSENT",
        value: "YES+1",
        domain: "consent.google.com",
        path: "/",
        secure: true,
        sameSite: "Lax",
      },
      {
        name: "SOCS",
        value: "CAI",
        domain: ".google.com",
        path: "/",
        secure: true,
        sameSite: "Lax",
      },
      {
        name: "SOCS",
        value: "CAI",
        domain: "consent.google.com",
        path: "/",
        secure: true,
        sameSite: "Lax",
      },
    ]);

    // Navigate to the baseline target URL
    await page.goto("https://www.google.com/?hl=en&gl=us", {
      waitUntil: "domcontentloaded",
    });
    await dismissGoogleConsent(page);
    await page.waitForTimeout(1000); // Wait for page to load

    // Dynamically inject the specified CSS/DOM defect into the runtime browser environment
    await page.evaluate(scenario.injectFn);

    // Simulate active user interaction to capture dynamic sequential frames
    // FIX: Using .first() to prevent strict mode violation with hidden textareas
    const searchInput = page
      .locator('textarea[name="q"], input[name="q"]')
      .first();
    if (await searchInput.isVisible()) {
      await searchInput.fill(`Testing framework PoC - ${scenario.id}`);
    }

    await page.waitForTimeout(2000); // Capture 2 seconds of the post-injection interaction state
    console.log(
      `[SUCCESS] Generated dataset video for scenario: ${scenario.id}`,
    );
  });
}
