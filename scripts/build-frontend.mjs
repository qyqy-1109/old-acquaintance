import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const assets = path.join(root, "app/views/assets");
await mkdir(path.join(assets, "icons"), { recursive: true });
const icons = ["house", "messages-square", "book-open", "list-tree", "clock-3", "contact-round",
  "heart-pulse", "volume-2", "volume-x", "panel-left", "x", "log-out", "arrow-up", "square",
  "plus", "trash-2", "refresh-cw", "wand-sparkles", "circle-check", "chevron-right", "shield-check",
  "key-round", "user-round", "triangle-alert", "arrow-down", "command", "message-square-warning",
  "copy", "check", "loader-circle", "eye", "eye-off", "chevron-down", "pencil", "search"];
for (const name of icons) {
  await copyFile(path.join(root, "node_modules/lucide-static/icons", name + ".svg"),
    path.join(assets, "icons", name + ".svg"));
}
await writeFile(path.join(assets, "icons.css"), icons.map(name =>
  `[data-icon="${name}"]{mask-image:url("./icons/${name}.svg")}`
).join("\n") + "\n");
await copyFile(path.join(root, "node_modules/eventsource-parser/dist/index.js"),
  path.join(assets, "eventsource-parser.js"));
const licenseFiles = [["lucide-static/LICENSE", "lucide-license.txt"],
  ["eventsource-parser/LICENSE", "eventsource-parser-license.txt"],
  ["tailwindcss/LICENSE", "tailwind-license.txt"]];
for (const [source, target] of licenseFiles) {
  await copyFile(path.join(root, "node_modules", source), path.join(assets, target));
}
const result = spawnSync(process.execPath, [path.join(root, "node_modules/@tailwindcss/cli/dist/index.mjs"),
  "-i", "app/views/css/styles.css", "-o", "app/views/assets/styles.css", "--minify"],
  { cwd: root, stdio: "inherit" });
if (result.status !== 0) process.exit(result.status ?? 1);
const size = (await readFile(path.join(assets, "styles.css"))).byteLength;
console.log(`frontend assets ready (${size} bytes CSS, ${icons.length} local Lucide icons)`);
