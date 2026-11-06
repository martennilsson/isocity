// Tile loader worker: fetch + decode off the main thread, hand back a transferable ImageBitmap.
// Messages in:  { id, url }  |  { cancel: id }
// Messages out: { id, bitmap } | { id, error }

const controllers = new Map<number, AbortController>();

self.onmessage = async (e: MessageEvent) => {
  const msg = e.data;
  if ('cancel' in msg) {
    controllers.get(msg.cancel)?.abort();
    return;
  }
  const { id, url } = msg as { id: number; url: string };
  const ctl = new AbortController();
  controllers.set(id, ctl);
  try {
    const res = await fetch(url, { signal: ctl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const bitmap = await createImageBitmap(await res.blob(), {
      premultiplyAlpha: 'premultiply',
      colorSpaceConversion: 'none',
    });
    (self as unknown as Worker).postMessage({ id, bitmap }, [bitmap]);
  } catch (err) {
    (self as unknown as Worker).postMessage({ id, error: String(err) });
  } finally {
    controllers.delete(id);
  }
};
