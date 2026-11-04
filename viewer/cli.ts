// Serve or build the map viewer for one pyramid folder made by pipeline/build_pyramid.py.
//
//   npm run viewer -- dev   --map ../dist/sodermalm-renders [--port 5180]
//   npm run viewer -- build --map ../dist/sodermalm-renders
//
// `build` writes index.html + assets *into* the map folder (tiles are kept), so the folder is a
// complete static site you can host as-is. The page reads ./manifest.json and ./map.dzi.

import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { build, createServer } from 'vite';

const root = dirname(fileURLToPath(import.meta.url));
const { positionals, values: a } = parseArgs({
  allowPositionals: true,
  options: { map: { type: 'string' }, port: { type: 'string', default: '5180' } },
});
const cmd = positionals[0];
if ((cmd !== 'dev' && cmd !== 'build') || !a.map) {
  console.error('usage: cli.ts dev|build --map DIST_DIR [--port N]');
  process.exit(2);
}
const mapDir = resolve(a.map);
if (!existsSync(resolve(mapDir, 'manifest.json'))) {
  console.error(`${mapDir}/manifest.json not found: run pipeline/build_pyramid.py first`);
  process.exit(2);
}

if (cmd === 'dev') {
  const server = await createServer({
    root,
    configFile: false,
    publicDir: mapDir,
    server: { host: '127.0.0.1', port: Number(a.port) },
  });
  await server.listen();
  console.log(`viewer: ${server.resolvedUrls!.local[0]}   (Ctrl-C to stop)`);
} else {
  await build({
    root,
    configFile: false,
    base: './',
    publicDir: false,
    logLevel: 'warn',
    build: { outDir: mapDir, emptyOutDir: false, assetsDir: 'viewer-assets' },
  });
  console.log(`built viewer into ${mapDir} (static site: serve that folder)`);
}
