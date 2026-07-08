import { test } from "@playwright/test";

test.describe("MSc Thesis: Bug Injection PoC", () => {
  test("Should open Google and inject a Layout Shift bug", async ({
    page,
    context,
  }) => {
    await page.goto("https://www.google.com");

    await page.waitForTimeout(1000);

    await page.evaluate(() => {
      const searchForm = document.querySelector("form");
      if (searchForm) {
        const randomOffset = Math.floor(Math.random() * 400) + 200;
        (searchForm as HTMLElement).style.marginTop = `${randomOffset}px`;
        (searchForm as HTMLElement).style.transform = "rotate(10deg)";
      }
    });

    await page.waitForTimeout(2000);

    console.log("Successfully injected layout shift anomaly into the page.");
  });
});
