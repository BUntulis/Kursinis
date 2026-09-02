const fs = require("fs");
const path = require("path");

const source = path.join(__dirname, "..", "node_modules", "chart.js", "dist", "chart.umd.js");
const targetDir = path.join(__dirname, "..", "dashboard", "static", "dashboard", "vendor");
const target = path.join(targetDir, "chart.umd.js");

fs.mkdirSync(targetDir, { recursive: true });
fs.copyFileSync(source, target);
console.log(`Copied ${source} -> ${target}`);
