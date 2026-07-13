// Harvest gate-passed cases from test-results/ into a versioned dataset folder.
//
// Usage:  node scripts/collect-dataset.mjs <output-dir>
//   e.g.  node scripts/collect-dataset.mjs ../dataset_v1
//
// Only cases that passed the pixelmatch quality gate have a gate-report.json,
// so failed/flaky cases are excluded automatically. Output layout:
//
//   <output-dir>/
//     metadata.json            all cases + summary stats
//     metadata.csv             same data, spreadsheet-friendly
//     normal/<case-id>/        video.webm, normal.png, buggy.png, diff.png
//     buggy/<case-id>/         ...

import fs from "node:fs";
import path from "node:path";

const outDir = process.argv[2];
if (!outDir) {
  console.error("Usage: node scripts/collect-dataset.mjs <output-dir>");
  process.exit(1);
}

const resultsDir = path.resolve(import.meta.dirname, "..", "test-results");
const records = [];

for (const entry of fs.readdirSync(resultsDir, { withFileTypes: true })) {
  if (!entry.isDirectory() || entry.name.includes("-retry")) continue;
  const caseDir = path.join(resultsDir, entry.name);
  const reportPath = path.join(caseDir, "gate-report.json");
  if (!fs.existsSync(reportPath)) continue; // gate not passed → skip

  const report = JSON.parse(fs.readFileSync(reportPath, "utf8"));
  const video = fs.readdirSync(caseDir).find((f) => f.endsWith(".webm"));
  if (!video) {
    console.warn(`[SKIP] ${report.id}: no video found in ${entry.name}`);
    continue;
  }

  const group = report.group === "control" ? "normal" : "buggy";
  const destDir = path.join(outDir, group, report.id);
  fs.mkdirSync(destDir, { recursive: true });

  fs.copyFileSync(path.join(caseDir, video), path.join(destDir, "video.webm"));
  for (const png of ["normal.png", "buggy.png", "diff.png"]) {
    fs.copyFileSync(path.join(caseDir, png), path.join(destDir, png));
  }

  records.push({ ...report, path: path.join(group, report.id) });
}

records.sort((a, b) => a.id.localeCompare(b.id));

const summary = {
  createdAt: new Date().toISOString(),
  totalCases: records.length,
  normalCases: records.filter((r) => r.group === "control").length,
  buggyCases: records.filter((r) => r.group === "bug").length,
  cases: records,
};
fs.writeFileSync(
  path.join(outDir, "metadata.json"),
  JSON.stringify(summary, null, 2),
);

const csvHeader =
  "id,group,bug_key,bug_label,diff_ratio,min_diff_ratio,browser,target_url,generated_at,path";
const csvRows = records.map((r) =>
  [
    r.id,
    r.group,
    r.bugKey ?? "",
    `"${r.bugLabel ?? ""}"`,
    r.diffRatio.toFixed(6),
    r.minDiffRatio,
    r.browser,
    r.targetUrl,
    r.generatedAt,
    r.path,
  ].join(","),
);
fs.writeFileSync(
  path.join(outDir, "metadata.csv"),
  [csvHeader, ...csvRows].join("\n") + "\n",
);

console.log(
  `Collected ${summary.totalCases} cases (${summary.normalCases} normal / ${summary.buggyCases} buggy) → ${path.resolve(outDir)}`,
);
