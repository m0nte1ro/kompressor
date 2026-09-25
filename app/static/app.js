import {$, $$, api, escapeHTML as esc, mapLimit, notify, pendingJobs} from './common.js?v=5';
import './presets.js?v=5';
import './settings.js?v=5';
import './discovery.js?v=5';
import {setupTags} from './tags.js?v=5';
import {compressionModal} from './compression.js?v=5';
import {renderHistory, renderQueue} from './queue.js?v=5';

let queue = {lanes: [], history: [], pending_count: 0};

function sortRows(table) {
  const key = table.dataset.sortKey;
  if (!key) return;
  const button = table.querySelector(`[data-sort-key="${key}"]`);
  const type = button?.dataset.sortType ?? 'text';
  const direction = table.dataset.sortDirection === 'desc' ? -1 : 1;
  const tbody = table.tBodies[0];
  if (!tbody) return;
  const values = row => row.dataset[`sort${key[0].toUpperCase()}${key.slice(1)}`] ?? '';
  const compare = (a, b) => {
    if (type === 'number') {
      const left = Number(values(a));
      const right = Number(values(b));
      return direction * ((Number.isFinite(left) ? left : -Infinity) - (Number.isFinite(right) ? right : -Infinity));
    }
    return direction * values(a).localeCompare(values(b), undefined, {numeric: true, sensitivity: 'base'});
  };
  const mediaRows = [...tbody.querySelectorAll('tr[data-sort-name], tr[data-sort-finished]')];
  if (!mediaRows.length) return;
  const separators = [...tbody.querySelectorAll('tr.season')];
  if (separators.length) {
    for (const separator of separators) {
      const group = mediaRows.filter(row => row.dataset.season === separator.dataset.season).sort(compare);
      tbody.append(separator, ...group);
    }
  } else {
    mediaRows.sort(compare).forEach(row => tbody.append(row));
  }
  table.querySelectorAll('thead th').forEach(th => th.removeAttribute('aria-sort'));
  button?.closest('th')?.setAttribute('aria-sort', direction === 1 ? 'ascending' : 'descending');
}

function setupSortableTables() {
  document.addEventListener('click', event => {
    const button = event.target.closest('table[data-sortable] [data-sort-key]');
    if (!button) return;
    const table = button.closest('table[data-sortable]');
    if (!table) return;
    const same = table.dataset.sortKey === button.dataset.sortKey;
    table.dataset.sortKey = button.dataset.sortKey;
    table.dataset.sortDirection = same && table.dataset.sortDirection === 'asc' ? 'desc' : 'asc';
    sortRows(table);
  });
}

setupSortableTables();
window.addEventListener('table-updated', event => {
  if (event.detail?.table) sortRows(event.detail.table);
});

let refreshRevision = 0;
let mutating = false;
const library = $('#library');
let rows = $$('.media-row');
const selected = () => rows.filter(row => $('.media-select', row).checked);
const jobFor = row => pendingJobs(queue).find(job => job.media_id === row.dataset.id && job.scope === library?.dataset.scope);

function selectionChanged() {
  if (!library) return;
  const selection = selected();
  $('#tags-selected').disabled = selection.length === 0;
  $('#selection-count').textContent = `${selection.length} selected`;
  $('#compress-selected').disabled = selection.length === 0;
  $('#remove-selected').disabled = mutating || !selection.some(row => jobFor(row)?.status === 'queued');
  const visible = rows.filter(row => !row.hidden);
  const checked = visible.filter(row => $('.media-select', row).checked).length;
  $('#select-all').checked = visible.length > 0 && checked === visible.length;
  $('#select-all').indeterminate = checked > 0 && checked < visible.length;
}

function filterRows() {
  if (!library) return;
  const search = $('#search').value.toLowerCase().trim();
  const filter = $('#filter').value;
  for (const row of rows) {
    const data = row.dataset;
    const matches = {
      all: true, eligible: data.eligible === 'true', blocked: data.eligible !== 'true',
      h264: data.codec === 'h264', hevc: data.codec === 'hevc', '2160p': data.resolution === '2160p',
      '1080p': data.resolution === '1080p', hdr: Boolean(data.hdr),
      hardlinked: Number(data.hardlinks) > 1, queued: Boolean(jobFor(row)),
    };
    row.hidden = !row.textContent.toLowerCase().includes(search) || !matches[filter];
  }
  $$('.season').forEach(separator => {
    separator.hidden = !rows.some(row => row.dataset.season === separator.dataset.season && !row.hidden);
  });
  $('#no-results').hidden = rows.some(row => !row.hidden);
  selectionChanged();
}

function renderLibraryQueue() {
  for (const row of rows) {
    const job = jobFor(row);
    const key = job ? `${job.id}:${job.status}` : 'none';
    if (row.dataset.queueState === key) continue;
    row.dataset.queueState = key;
    const state = $('.job-state', row);
    state.innerHTML = job ? `<span class="badge blue">${esc(job.status)}</span>` : '';
    $('.row-action', row).innerHTML = job
      ? job.status === 'queued'
        ? `<button data-action="remove" aria-label="Remove ${esc(row.dataset.name)} from queue">× Remove</button>`
        : '<a class="button" href="/queue">View active</a>'
      : `<button data-action="compress" aria-label="Review compression for ${esc(row.dataset.name)}">${row.dataset.eligible === 'true' ? 'Compress' : 'Review'}</button>`;
  }
  filterRows();
}

async function refreshQueue() {
  const revision = ++refreshRevision;
  const data = await api('/api/queue');
  if (revision !== refreshRevision) return;
  queue = data;
  $('#queue-count').textContent = queue.pending_count;
  queue.lanes.forEach(lane => {
    const control = queue.workers?.lanes?.[lane.backend];
    const state = control?.paused ? 'paused'
      : control?.quiet_active ? 'quiet hours'
      : lane.active?.status ?? 'idle';
    $(`#${lane.backend}-state`).textContent = `${lane.backend.toUpperCase()} · ${state}`;
  });
  renderQueue(queue);
  renderHistory(queue);
  renderLibraryQueue();
}

async function queueAction(jobId, action, priority) {
  const suffix = action === 'remove' ? '' : `/${action}`;
  await api(`/api/queue/${encodeURIComponent(jobId)}${suffix}`, {
    method: action === 'remove' ? 'DELETE' : action === 'priority' ? 'PATCH' : 'POST',
    ...(priority ? {body: JSON.stringify({priority})} : {}),
  });
}

async function mutate(action) {
  if (mutating) return;
  mutating = true;
  try {
    await action();
  } catch (error) {
    notify(error.message, true);
  } finally {
    mutating = false;
    try { await refreshQueue(); } catch (error) { notify(error.message, true); }
  }
}

setupTags(() => selected().map(row => ({kind: library.dataset.scope === 'movie' ? 'movie' : 'episode', id: row.dataset.id})));

if (library) {
  const open = compressionModal(library.dataset.scope, refreshQueue);
  $('#search').addEventListener('input', filterRows);
  $('#filter').addEventListener('change', filterRows);
  $('#select-all').addEventListener('change', event => {
    rows.filter(row => !row.hidden).forEach(row => $('.media-select', row).checked = event.target.checked);
    selectionChanged();
  });
  library.addEventListener('change', event => {
    if (event.target.matches('.media-select')) selectionChanged();
  });
  library.addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const row = button.closest('.media-row');
    if (button.dataset.action === 'compress') open([row]);
    else {
      const job = jobFor(row);
      if (job) mutate(() => queueAction(job.id, 'remove'));
    }
  });
  $('#compress-selected').addEventListener('click', () => open(selected()));
  $('#remove-selected').addEventListener('click', () => mutate(async () => {
    const jobs = selected().map(jobFor).filter(job => job?.status === 'queued');
    const outcomes = await mapLimit(jobs, async job => {
      try { await queueAction(job.id, 'remove'); return null; }
      catch (error) { return `${job.name}: ${error.message}`; }
    });
    const errors = outcomes.filter(Boolean);
    notify(`${jobs.length - errors.length} removed.${errors.length ? ` ${errors.join(' ')}` : ''}`, errors.length > 0);
  }));
}

$('#show-search')?.addEventListener('input', event => {
  const search = event.target.value.toLowerCase().trim();
  $$('.show-card').forEach(card => card.hidden = !card.dataset.name.toLowerCase().includes(search));
  $('#no-shows').hidden = $$('.show-card').some(card => !card.hidden);
});

$('#queue-lanes')?.addEventListener('click', event => {
  const button = event.target.closest('[data-queue-action]');
  if (!button) return;
  const jobId = button.closest('[data-job]').dataset.job;
  mutate(() => queueAction(jobId, button.dataset.queueAction));
});
$('#queue-lanes')?.addEventListener('change', event => {
  if (!event.target.matches('[data-priority]')) return;
  const jobId = event.target.closest('[data-job]').dataset.job;
  mutate(() => queueAction(jobId, 'priority', event.target.value));
});

document.addEventListener('click', event => {
  const button = event.target.closest('[data-worker-action]');
  if (!button) return;

  const action = button.dataset.workerAction;
  const backend = button.dataset.backend;
  const laneControl = backend ? queue.workers?.lanes?.[backend] : null;
  const allControls = queue.workers?.lanes ? Object.values(queue.workers.lanes) : [];
  let path;
  let message;

  if (action === 'toggle-pause-all') {
    const allPaused = allControls.length > 0 && allControls.every(control => control.paused);
    path = allPaused ? '/api/queue/workers/resume-all' : '/api/queue/workers/pause-all';
    message = allPaused ? 'All workers resumed.' : 'All workers will pause before claiming another job.';
  } else if (action === 'stop-all') {
    path = '/api/queue/workers/stop-all';
    message = 'Active jobs are stopping and all workers are paused.';
  } else if (action === 'stop-active') {
    path = `/api/queue/workers/${encodeURIComponent(backend)}/stop-active`;
    message = `${backend.toUpperCase()} active job stop requested.`;
  } else if (action === 'toggle-pause') {
    const paused = laneControl?.paused ?? button.dataset.paused === 'true';
    path = `/api/queue/workers/${encodeURIComponent(backend)}/${paused ? 'resume' : 'pause'}`;
    message = paused
      ? `${backend.toUpperCase()} worker resumed.`
      : `${backend.toUpperCase()} worker will pause before claiming another job.`;
  } else {
    return;
  }

  mutate(async () => {
    const response = await api(path, {method: 'POST'});
    if (response?.lanes) queue.workers = response;
    notify(message);
  });
});

async function poll() {
  try { await refreshQueue(); }
  catch (error) { notify(`Queue update failed: ${error.message}`, true); }
  finally { window.setTimeout(poll, 2000); }
}
poll();

// Preserve toolbar listeners, filters and selections while replacing server-rendered rows.
window.addEventListener('library-updated', () => {
  rows = $$('.media-row');
  renderLibraryQueue();
  $('#show-search')?.dispatchEvent(new Event('input'));
});
