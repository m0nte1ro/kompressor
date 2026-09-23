import {$, $$, api} from './common.js';

let version = null;
let busy = false;
let stopped = false;
const button = $('#scan-library');
const live = $('#discovery-status');
const isLibrary = ['movies', 'shows'].includes(document.body.dataset.page);

async function updateLibrary() {
  // Same-origin Jinja output supplies escaping and policy results. No policy in JS.
  const response = await fetch(window.location.pathname, {headers: {'X-Library-Fragment': '1'}});
  if (!response.ok) throw new Error(`Library update failed (${response.status}).`);
  const page = new DOMParser().parseFromString(await response.text(), 'text/html');
  const tbody = $('#library tbody');
  if (tbody) {
    const chosen = new Set($$('.media-row').filter(row => $('.media-select', row).checked).map(row => row.dataset.id));
    const expanded = new Set($$('.media-row').filter(row => $('details[open]', row)).map(row => row.dataset.id));
    const incoming = page.querySelector('#library tbody');
    if (!incoming) return;
    tbody.replaceChildren(...incoming.childNodes);
    $$('.media-row').forEach(row => {
      $('.media-select', row).checked = chosen.has(row.dataset.id);
      const details = $('details', row);
      if (details) details.open = expanded.has(row.dataset.id);
    });
    $('.stats')?.replaceWith(page.querySelector('.stats'));
  } else {
    const grid = $('.show-grid');
    const incoming = page.querySelector('.show-grid');
    if (grid && incoming) grid.replaceChildren(...incoming.childNodes);
    const count = $('.toolbar p');
    if (count && page.querySelector('.toolbar p')) count.textContent = page.querySelector('.toolbar p').textContent;
  }
  window.dispatchEvent(new Event('library-updated'));
}

function showReport(report) {
  const discovered = report.roots.reduce((sum, root) => sum + root.discovered, 0) + (report.phase === 'discovering' ? (report.discovered_so_far || 0) : 0);
  const processed = report.roots.reduce((sum, root) => sum + (root.processed || 0), 0);
  const text = report.state === 'running'
    ? `Library scan · ${report.phase} · ${discovered} files discovered · ${processed} checked. Results update automatically.`
    : `Library scan ${report.state} · ${report.roots.reduce((sum, root) => sum + root.discovered, 0)} files.${report.error ? ` ${report.error}` : ''}`;
  live.hidden = ['idle', 'disabled'].includes(report.state);
  live.textContent = text;
  if (button) button.disabled = report.state === 'running';
  if ($('#scan-status')) $('#scan-status').textContent = text;
  const errors = $('#scan-errors');
  if (errors) {
    errors.replaceChildren();
    const messages = report.roots.flatMap(root => [...root.errors, ...root.reconciliation.issues.map(issue => `${issue.path}: ${issue.reason}`)]);
    for (const message of messages.slice(0, 100)) {
      const item = document.createElement('li');
      item.textContent = message;
      errors.append(item);
    }
    if (messages.length > 100) {
      const item = document.createElement('li');
      item.textContent = `${messages.length - 100} more errors; the full report is available at /api/library/scan.`;
      errors.append(item);
    }
  }
}

button?.addEventListener('click', async () => {
  button.disabled = true;
  try {
    showReport(await api('/api/library/scan', {method: 'POST'}));
    version = null;
  } catch (error) {
    $('#scan-status').textContent = error.message;
    button.disabled = false;
  }
});

async function poll() {
  if (busy || stopped) return;
  busy = true;
  try {
    const report = await api('/api/library/scan');
    if (report.backend === 'seed') {stopped = true; return;}
    showReport(report);
    const current = `${report.scan_id || ''}:${report.generation}`;
    if (isLibrary && current !== version && !document.querySelector('dialog[open]')) {
      await updateLibrary();
      version = current;
    }
  } catch (error) {
    live.hidden = false;
    live.textContent = `${error.message} Retrying…`;
  } finally {
    busy = false;
    if (!stopped) window.setTimeout(poll, document.hidden ? 5000 : 1500);
  }
}
poll();
