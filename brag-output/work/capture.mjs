// Capture frames of video.html via Chrome DevTools Protocol (no npm deps).
// usage: node capture.mjs <outDir> <fps> <duration> [t1,t2,...]  (times list = stills only)
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const [outDir, fpsArg, durArg, timesArg] = process.argv.slice(2);
const fps = Number(fpsArg), dur = Number(durArg);
mkdirSync(outDir, { recursive: true });
const page = "file://" + resolve(new URL(".", import.meta.url).pathname, "video.html");
const port = 9333;
const chrome = spawn("google-chrome", ["--headless=new", "--disable-gpu", `--remote-debugging-port=${port}`,
  "--hide-scrollbars", "--force-device-scale-factor=1", "--user-data-dir=" + resolve(outDir, "../chrome-profile"), "about:blank"],
  { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let wsUrl;
for (let i = 0; i < 50 && !wsUrl; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    wsUrl = list.find((x) => x.type === "page")?.webSocketDebuggerUrl;
  } catch { await sleep(200); }
}
const ws = new WebSocket(wsUrl);
await new Promise((r) => ws.addEventListener("open", r));
let id = 0; const pending = new Map();
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });

await send("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
await send("Page.enable");
await send("Page.navigate", { url: page });
await sleep(1500);
await send("Runtime.evaluate", { expression: "document.fonts.ready.then(() => true)", awaitPromise: true });

const times = timesArg ? timesArg.split(",").map(Number) : Array.from({ length: Math.round(fps * dur) }, (_, i) => i / fps);
for (let i = 0; i < times.length; i++) {
  const t = times[i];
  await send("Runtime.evaluate", { expression: `window.renderAt(${t}); true` });
  const shot = await send("Page.captureScreenshot", { format: "png", clip: { x: 0, y: 0, width: 1920, height: 1080, scale: 1 } });
  const name = timesArg ? `still_${String(t).replace(".", "_")}.png` : `f_${String(i).padStart(5, "0")}.png`;
  writeFileSync(resolve(outDir, name), Buffer.from(shot.result.data, "base64"));
  if (!timesArg && i % 60 === 0) console.log("frame", i);
}
ws.close(); chrome.kill();
