// Cloudflare Pages Function: serve episode mp3s with HTTP byte-range support (Pages static assets ignore Range).
// Route: /<slug>/episodes/<file>. Everything else (feed.xml, cover.jpg) stays plain static.
export async function onRequest({ request, env, next, waitUntil }) {
  const url = new URL(request.url);
  if (!url.pathname.endsWith(".mp3") || !["GET", "HEAD"].includes(request.method)) return next();
  const res = await env.ASSETS.fetch(new Request(url.toString(), { method: "GET" }));
  if (res.status !== 200) return res;
  let size = Number(res.headers.get("Content-Length") || 0);
  let body = res.body;
  if (!size) { const buf = await res.arrayBuffer(); size = buf.byteLength; body = new Blob([buf]).stream(); }
  const h = new Headers({
    "Content-Type": "audio/mpeg", "Accept-Ranges": "bytes", "Cache-Control": "public, max-age=86400",
    "X-Robots-Tag": "noindex, nofollow", "Access-Control-Allow-Origin": "*",
  });
  const etag = res.headers.get("ETag"); if (etag) h.set("ETag", etag);
  const range = request.headers.get("Range");
  let start = 0, end = size - 1, status = 200;
  if (range) {
    const m = /^bytes=(\d*)-(\d*)$/.exec(range.trim());
    if (m && (m[1] !== "" || m[2] !== "")) {
      if (m[1] === "") { start = Math.max(0, size - Number(m[2])); }
      else { start = Number(m[1]); if (m[2] !== "") end = Math.min(Number(m[2]), size - 1); }
      if (start > end || start >= size) {
        if (body) waitUntil(body.cancel());
        return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}`, "Accept-Ranges": "bytes" } });
      }
      status = 206; h.set("Content-Range", `bytes ${start}-${end}/${size}`);
    }
  }
  const len = end - start + 1;
  h.set("Content-Length", String(len));
  if (request.method === "HEAD") { if (body) waitUntil(body.cancel()); return new Response(null, { status, headers: h }); }
  if (status === 200) return new Response(body, { status, headers: h });
  const { readable, writable } = new FixedLengthStream(len);
  const pump = (async () => {
    const reader = body.getReader(), writer = writable.getWriter();
    let pos = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const cs = pos, ce = pos + value.byteLength; pos = ce;
        if (ce <= start) continue;
        const a = Math.max(start, cs) - cs, b = Math.min(end + 1, ce) - cs;
        if (b > a) await writer.write(value.subarray(a, b));
        if (ce > end) { await reader.cancel(); break; }
      }
      await writer.close();
    } catch (e) { try { await writer.abort(e); } catch (_) {} }
  })();
  waitUntil(pump);
  return new Response(readable, { status, headers: h });
}
