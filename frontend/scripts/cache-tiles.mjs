#!/usr/bin/env node
/**
 * Pre-download basemap tiles for a mission area, so the map works with no
 * network at the site.
 *
 * ----------------------------------------------------------------------------
 * BEFORE YOU RUN THIS, CHECK THE PROVIDER'S TERMS.
 *
 * Bulk-downloading tiles is prohibited by many providers, including
 * OpenStreetMap's standard tile server. Esri's World Imagery allows caching
 * only under specific licence terms. It is your responsibility to use a source
 * you are entitled to cache -- your own imagery, a commercial licence that
 * permits offline use, or a government open-data orthophoto service.
 *
 * This script therefore has no default source. You must pass --url.
 * ----------------------------------------------------------------------------
 *
 * Usage:
 *   node scripts/cache-tiles.mjs \
 *     --url "https://example.com/tiles/{z}/{x}/{y}.jpg" \
 *     --bbox 77.55,12.95,77.62,13.02 \
 *     --zoom 13-18 \
 *     --out ../tiles
 *
 * Output layout is {z}/{x}/{y}.jpg, which is what VITE_LOCAL_TILE_URL expects.
 */

import { mkdir, writeFile, stat } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

/* ---------------------------------------------------------------- arguments */

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith("--")) continue;
    const key = token.slice(2);
    const next = argv[i + 1];
    if (next && !next.startsWith("--")) {
      args[key] = next;
      i += 1;
    } else {
      args[key] = "true";
    }
  }
  return args;
}

const args = parseArgs(process.argv.slice(2));

function fail(message) {
  console.error(`error: ${message}`);
  process.exit(1);
}

if (!args.url) {
  fail(
    "--url is required, and has no default on purpose.\n" +
      "Pass a tile template you are licensed to cache, e.g.\n" +
      '  --url "https://your-server/tiles/{z}/{x}/{y}.jpg"',
  );
}
if (!args.bbox) fail("--bbox minLon,minLat,maxLon,maxLat is required");

const bbox = args.bbox.split(",").map(Number);
if (bbox.length !== 4 || bbox.some((n) => !Number.isFinite(n))) {
  fail("--bbox must be four numbers: minLon,minLat,maxLon,maxLat");
}
const [minLon, minLat, maxLon, maxLat] = bbox;
if (minLon >= maxLon || minLat >= maxLat) fail("--bbox min values must be less than max values");

const zoomSpec = args.zoom ?? "13-17";
const [zMinRaw, zMaxRaw] = zoomSpec.includes("-") ? zoomSpec.split("-") : [zoomSpec, zoomSpec];
const zMin = Number(zMinRaw);
const zMax = Number(zMaxRaw);
if (!Number.isInteger(zMin) || !Number.isInteger(zMax) || zMin < 0 || zMax > 22 || zMin > zMax) {
  fail("--zoom must be like 13-18, between 0 and 22");
}

const outDir = resolve(args.out ?? "../tiles");
const extension = args.ext ?? (args.url.match(/\.(\w+)(\?|$)/)?.[1] ?? "jpg");
const delayMs = Number(args.delay ?? 120);
const dryRun = args["dry-run"] === "true";

/* ------------------------------------------------------------- tile numbers */

function lonToX(lon, z) {
  return Math.floor(((lon + 180) / 360) * 2 ** z);
}

function latToY(lat, z) {
  const rad = (lat * Math.PI) / 180;
  return Math.floor(
    ((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * 2 ** z,
  );
}

const plan = [];
for (let z = zMin; z <= zMax; z += 1) {
  const x0 = lonToX(minLon, z);
  const x1 = lonToX(maxLon, z);
  // y is inverted: north edge gives the smaller y.
  const y0 = latToY(maxLat, z);
  const y1 = latToY(minLat, z);
  for (let x = Math.min(x0, x1); x <= Math.max(x0, x1); x += 1) {
    for (let y = Math.min(y0, y1); y <= Math.max(y0, y1); y += 1) {
      plan.push({ z, x, y });
    }
  }
}

console.log(`Area      ${minLon},${minLat} -> ${maxLon},${maxLat}`);
console.log(`Zoom      ${zMin}-${zMax}`);
console.log(`Tiles     ${plan.length}`);
console.log(`Output    ${outDir}`);
console.log(`Estimate  ~${((plan.length * 25) / 1024).toFixed(1)} MB at ~25 kB/tile`);
console.log("");

if (plan.length > 200_000) {
  fail(`${plan.length} tiles is almost certainly a mistake. Narrow the bbox or the zoom range.`);
}

if (dryRun) {
  console.log("--dry-run given; nothing downloaded.");
  process.exit(0);
}

console.log("Confirm you are licensed to cache this source. Starting in 3 seconds...\n");
await new Promise((r) => setTimeout(r, 3000));

/* ------------------------------------------------------------------ fetch */

let done = 0;
let skipped = 0;
let failed = 0;
let bytes = 0;

for (const { z, x, y } of plan) {
  const path = join(outDir, String(z), String(x), `${y}.${extension}`);

  // Resumable: an existing tile is left alone.
  try {
    const info = await stat(path);
    if (info.size > 0) {
      skipped += 1;
      continue;
    }
  } catch {
    /* not cached yet */
  }

  const url = args.url
    .replace("{z}", String(z))
    .replace("{x}", String(x))
    .replace("{y}", String(y));

  try {
    const response = await fetch(url, {
      headers: { "User-Agent": "NIDAR-RescueSwarm-GCS/0.1 (offline mission cache)" },
    });
    if (!response.ok) {
      failed += 1;
      if (failed <= 5) console.warn(`  HTTP ${response.status} for ${z}/${x}/${y}`);
      if (response.status === 403 || response.status === 429) {
        console.error(
          `\nAborting: the provider returned ${response.status}. ` +
            "This source does not permit, or is rate-limiting, bulk caching.",
        );
        process.exit(1);
      }
    } else {
      const buffer = Buffer.from(await response.arrayBuffer());
      await mkdir(dirname(path), { recursive: true });
      await writeFile(path, buffer);
      bytes += buffer.length;
      done += 1;
    }
  } catch (cause) {
    failed += 1;
    if (failed <= 5) console.warn(`  failed ${z}/${x}/${y}: ${cause.message}`);
  }

  if ((done + skipped + failed) % 200 === 0) {
    process.stdout.write(
      `\r  ${done + skipped + failed}/${plan.length}  downloaded=${done} cached=${skipped} failed=${failed}`,
    );
  }

  if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));
}

console.log(
  `\n\nDone. downloaded=${done} already-cached=${skipped} failed=${failed} size=${(bytes / 1024 / 1024).toFixed(1)} MB`,
);
console.log(`\nServe this directory at /tiles, then set in frontend/.env:`);
console.log(`  VITE_BASEMAP=local`);
console.log(`  VITE_LOCAL_TILE_URL=/tiles/{z}/{x}/{y}.${extension}`);
console.log(`  VITE_LOCAL_TILE_MAXZOOM=${zMax}`);
if (failed > 0) {
  console.log(
    `\n${failed} tiles failed. Those areas will render as blank gaps -- the map still works, ` +
      "but re-run to fill them before you deploy.",
  );
}
