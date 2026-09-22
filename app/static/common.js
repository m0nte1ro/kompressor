export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
export const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
})[char]);
export const size = value => value == null ? '—' : `${(value / 1e9).toFixed(1)} GB`;
export const bitrate = value => `${value / 1e6} Mbps`;
export const label = value => ({cpu: 'CPU · x265', qsv: 'Intel QSV', hevc: 'HEVC', h264: 'H.264',
  preserve: 'Preserve source', efficient: 'Efficient E-AC3 / AAC', max_1080p: 'Max 1080p',
  max_720p: 'Max 720p', hdr10: 'HDR10', dolby_vision: 'Dolby Vision', dolby_vision_hdr10: 'DV + HDR10',
  preserve_quality: 'Preserve perceived quality', streaming_quality: 'Streaming-style quality',
  built_in: 'Built-in', custom: 'Custom'
})[value] ?? String(value ?? 'SDR').replaceAll('_', ' ').toUpperCase();

export async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {'Content-Type': 'application/json', ...options.headers},
    signal: options.signal ?? AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body = await response.json();
      if (typeof body.detail === 'string') message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map(e => e.msg).join(' ');
    } catch { /* Keep the HTTP error for non-JSON responses. */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

export function notify(message, error = false) {
  const box = $('#message');
  box.textContent = message;
  box.classList.toggle('error', error);
  box.hidden = false;
}

export function pendingJobs(queue) {
  return queue.lanes.flatMap(lane => [...(lane.active ? [lane.active] : []), ...lane.queued]);
}

export async function mapLimit(items, mapper, limit = 8) {
  const results = new Array(items.length);
  let cursor = 0;
  await Promise.all(Array.from({length: Math.min(limit, items.length)}, async () => {
    while (cursor < items.length) {
      const index = cursor++;
      results[index] = await mapper(items[index], index);
    }
  }));
  return results;
}
