// Zoomable map viewer. Reads ./manifest.json (pipeline/build_pyramid.py) and ./map.dzi.
// URL hash: #x,y,z with x,y in plan screen px (stable across rebuilds) and z = screen px per map px.

import OpenSeadragon from 'openseadragon';

interface Manifest {
  title: string;
  style: 'photo' | 'pixel';
  dzi: string;
  width: number;
  height: number;
  origin_screen_px: [number, number];
  metres_per_pixel: number;
  north_screen_dir: [number, number];
  screen_to_lonlat: [[number, number, number], [number, number, number]];
  attributions: string[];
}

const $ = (id: string) => document.getElementById(id)!;

async function main() {
  const m: Manifest = await (await fetch('./manifest.json')).json();
  const pixel = m.style === 'pixel';
  document.title = m.title;
  $('title').textContent = m.title;
  $('attribution').textContent = m.attributions.length ? `Map data © ${m.attributions.join(', ')}` : '';
  const [nx, ny] = m.north_screen_dir;
  $('needle').setAttribute('transform', `rotate(${(Math.atan2(nx, -ny) * 180) / Math.PI})`);
  if (pixel) $('map').classList.add('pixelated');

  // Canvas drawer: 60 fps panning/zooming vs ~24 fps for OpenSeadragon's default WebGL drawer
  // (viewer/bench.ts, 2026-11-06). ?drawer=webgl|canvas|html overrides, for benchmarking.
  const drawer = new URLSearchParams(location.search).get('drawer') ?? 'canvas';
  const viewer = OpenSeadragon({
    element: $('map'),
    drawer: drawer as any,
    tileSources: `./${m.dzi}`,
    showNavigationControl: false,
    showNavigator: true,
    navigatorPosition: 'ABSOLUTE', // below the title
    navigatorTop: 44,
    navigatorLeft: 10,
    navigatorWidth: 240,
    navigatorHeight: 150,
    imageSmoothingEnabled: !pixel,
    maxZoomPixelRatio: pixel ? 8 : 2,
    visibilityRatio: 0.5,
    constrainDuringPan: true,
    gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: true },
  });

  // image px <-> plan screen px <-> viewport coords
  const toScreen = (ix: number, iy: number) => [ix + m.origin_screen_px[0], iy + m.origin_screen_px[1]];
  const fromScreen = (sx: number, sy: number) => [sx - m.origin_screen_px[0], sy - m.origin_screen_px[1]];
  const lonlat = (sx: number, sy: number) => {
    const [[a, b, c], [d, e, f]] = m.screen_to_lonlat;
    return [a * sx + b * sy + c, d * sx + e * sy + f];
  };

  viewer.addHandler('open', () => {
    const h = location.hash.slice(1).split(',').map(Number);
    if (h.length === 3 && h.every(Number.isFinite)) {
      const [ix, iy] = fromScreen(h[0], h[1]);
      const vp = viewer.viewport;
      vp.panTo(vp.imageToViewportCoordinates(ix, iy), true);
      vp.zoomTo(vp.imageToViewportZoom(h[2]), undefined, true);
    }
  });
  viewer.addHandler('animation-finish', () => {
    const vp = viewer.viewport;
    const c = vp.viewportToImageCoordinates(vp.getCenter(true));
    const [sx, sy] = toScreen(c.x, c.y);
    const z = vp.viewportToImageZoom(vp.getZoom(true));
    history.replaceState(null, '', `#${Math.round(sx)},${Math.round(sy)},${+z.toFixed(3)}`);
  });

  new OpenSeadragon.MouseTracker({
    element: viewer.container,
    moveHandler: (e) => {
      const p = viewer.viewport.viewerElementToImageCoordinates(e.position as OpenSeadragon.Point);
      if (p.x < 0 || p.y < 0 || p.x >= m.width || p.y >= m.height) return;
      const [sx, sy] = toScreen(p.x, p.y);
      const [lon, lat] = lonlat(sx, sy);
      $('coords').textContent = `${lat.toFixed(5)}° N, ${lon.toFixed(5)}° E (at ground level)`;
    },
  });
}

main().catch((e) => {
  $('title').textContent = `error: ${(e as Error).message}`;
  console.error(e);
});
