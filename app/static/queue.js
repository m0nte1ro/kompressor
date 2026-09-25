import {$, $$, escapeHTML as esc, label, size} from './common.js';

function planningSaving(job) {
  return job.planning_saving ?? job.estimated_saving ?? 0;
}

function measuredChange(job) {
  const saving = job.measured_saving;
  if (saving === null || saving === undefined) return null;
  const percent = job.source_size ? 100 * saving / job.source_size : 0;
  return saving < 0
    ? `${size(-saving)} larger (${Math.abs(percent).toFixed(1)}%) measured`
    : `${size(saving)} saved (${percent.toFixed(1)}%) measured`;
}

function jobDetails(job) {
  const estimate = job.estimate_basis === 'planning_range'
    ? `${esc(size(job.estimated_saving_low))} – ${esc(size(job.estimated_saving_high))} planning estimate`
    : `~${esc(size(job.estimated_saving))} estimated reduction`;
  const source = job.execution_mode === 'real'
    ? `Keep original · output ${esc(job.output_path || 'in workspace after validation')}`
    : job.replace_source ? 'Replace after validation · simulated only' : 'Keep original · simulated test copy';
  return `<div class="job-name">${esc(job.name)}</div>
    <p class="job-meta">${esc(job.preset.name)} · ${esc(label(job.backend))} · ${esc(label(job.preset.destination_codec))}</p>
    <p class="saving">${measuredChange(job) ? esc(measuredChange(job)) : estimate}</p>
    <p class="job-meta">${source}</p>`;
}

export function renderQueue(queue) {
  const workerLanes = queue.workers?.lanes;
  const globalToggle = $('[data-worker-action="toggle-pause-all"]');
  if (globalToggle && workerLanes) {
    const lanes = Object.values(workerLanes);
    const allPaused = lanes.length > 0 && lanes.every(lane => lane.paused);
    globalToggle.textContent = allPaused ? 'Resume all workers' : 'Pause all after current';
    globalToggle.dataset.paused = String(allPaused);
    globalToggle.setAttribute('aria-pressed', String(allPaused));
  }

  for (const lane of queue.lanes) {
    const section = $(`[data-backend="${lane.backend}"]`);
    const control = queue.workers?.lanes?.[lane.backend];
    if (!section) continue;
    if (control) {
      const mode = control.paused ? 'paused' : control.quiet_active ? 'quiet hours' : 'ready';
      const badge = $('.worker-mode', section);
      if (badge) badge.textContent = mode;
      section.classList.toggle('lane-paused', control.paused || control.quiet_active);
      const toggle = $('[data-worker-action="toggle-pause"]', section);
      if (toggle) {
        toggle.textContent = control.paused ? 'Resume worker' : 'Pause after current';
        toggle.dataset.paused = String(control.paused);
        toggle.setAttribute('aria-pressed', String(control.paused));
      }
      const stop = $('[data-worker-action="stop-active"]', section);
      if (stop) stop.disabled = !lane.active;
    }
    const active = $('.active-slot', section);
    if (active.dataset.job !== (lane.active?.id ?? 'idle')) {
      active.dataset.job = lane.active?.id ?? 'idle';
      active.innerHTML = lane.active ? `<article class="active-job" data-job="${esc(lane.active.id)}">
        <span class="badge green active-status"></span>${jobDetails(lane.active)}
        <progress max="100" value="0" aria-label="Encoding progress"></progress>
        <p class="job-meta"><span class="progress-text"></span> · Priority: ${esc(lane.active.priority)}</p>
        <div class="job-controls"><button class="danger" data-queue-action="skip">Stop &amp; Skip</button></div>
      </article>` : '<p class="empty">Worker idle · add a job from the library</p>';
    }
    if (lane.active) {
      $('progress', active).value = lane.active.progress;
      const progress = lane.active.progress > 0 ? `${lane.active.progress.toFixed(1)}%` : 'Progress unavailable';
      const elapsed = `${Math.floor(lane.active.elapsed_seconds)}s elapsed`;
      const clock = lane.active.execution_mode === 'real' ? elapsed : `${Math.floor(lane.active.elapsed_seconds)}s simulated`;
      $('.progress-text', active).textContent = `${progress} · ${clock}`;
      $('.active-status', active).textContent = lane.active.status === 'validating' ? 'VALIDATING' : lane.active.status.toUpperCase();
    }
    $('.queued-count', section).textContent = lane.queued.length;
    const list = $('.queued-list', section);
    const key = JSON.stringify(lane.queued);
    if (list.dataset.rendered === key) continue;
    list.dataset.rendered = key;
    list.innerHTML = lane.queued.map(job => `<li class="queued-job" data-job="${esc(job.id)}">
      ${jobDetails(job)}${job.move_next_order ? '<span class="badge blue">Move next override</span>' : ''}
      <div class="job-controls"><label>Priority <select data-priority aria-label="Priority for ${esc(job.name)}">
        ${['urgent', 'high', 'normal', 'low'].map(p => `<option value="${p}" ${p === job.priority ? 'selected' : ''}>${p[0].toUpperCase() + p.slice(1)}</option>`).join('')}
      </select></label><button data-queue-action="move-next">↑ Move next</button>
      <button data-queue-action="remove" aria-label="Remove ${esc(job.name)} from queue">× Remove</button></div>
    </li>`).join('') || '<li class="empty">No queued jobs</li>';
  }
}

export function renderHistory(queue) {
  const rows = $('#history-rows');
  if (!rows) return;
  const key = JSON.stringify(queue.history);
  if (rows.dataset.rendered === key) return;
  rows.dataset.rendered = key;
  const completed = queue.history.filter(j => j.status === 'completed');
  const realCompleted = completed.filter(j => j.execution_mode === 'real');
  const realSaving = realCompleted.reduce((sum, j) => sum + (j.measured_saving ?? 0), 0);
  const estimatedCompleted = completed.filter(j => j.execution_mode !== 'real');
  const estimatedSaving = estimatedCompleted.reduce((sum, j) => sum + planningSaving(j), 0);
  $('#history-stats').innerHTML = [
    ['Net measured saving', size(realSaving)], ['Real encodes completed', realCompleted.length],
    ['Estimated simulation reduction', `~${size(estimatedSaving)}`], ['Failed / skipped / blocked', queue.history.filter(j => ['failed', 'skipped', 'blocked'].includes(j.status)).length],
  ].map(([title, value]) => `<div class="card stat"><span>${esc(title)}</span><strong>${esc(value)}</strong></div>`).join('');
  rows.innerHTML = queue.history.map(job => {
    const outputEstimate = job.estimate_basis === 'planning_range'
      ? ((job.estimated_output_size_low ?? 0) + (job.estimated_output_size_high ?? 0)) / 2
      : job.estimated_output_size ?? 0;
    const isReal = job.execution_mode === 'real';
    const actualOutput = isReal && job.status === 'completed' && job.output_size !== null;
    const actualSaving = isReal && job.status === 'completed' && job.measured_saving !== null;
    const savingText = actualSaving
      ? esc(measuredChange(job))
      : job.status === 'completed' ? job.estimate_basis === 'planning_range'
        ? `${esc(size(job.estimated_saving_low))} – ${esc(size(job.estimated_saving_high))} planning`
        : `~${esc(size(job.estimated_saving))} estimated`
        : '—';
    const error = [job.error_message, ...(job.validation_errors ?? [])].filter(Boolean)
      .map(message => `<small class="reason">${esc(message)}</small>`).join('');
    const outputPath = job.output_path ? `<small><code>${esc(job.output_path)}</code></small>` : '';
    const finished = job.finished_at ? new Date(job.finished_at).toLocaleString() : '—';
    return `<tr data-sort-name="${esc(job.name)}" data-sort-status="${esc(job.status)}"
      data-sort-preset="${esc(job.preset.name)}" data-sort-source="${job.source_size ?? 0}"
      data-sort-output="${actualOutput ? job.output_size : outputEstimate}" data-sort-saving="${actualSaving ? job.measured_saving : planningSaving(job)}"
      data-sort-video="${esc(job.source_codec)}" data-sort-elapsed="${job.elapsed_seconds ?? 0}"
      data-sort-finished="${job.finished_at ? new Date(job.finished_at).getTime() : 0}">
    <th scope="row">${esc(job.name)}${(job.reasons ?? []).map(reason => `<small class="reason">${esc(reason)}</small>`).join('')}${error}</th>
    <td><span class="badge ${job.status === 'completed' ? 'green' : job.status === 'failed' ? 'red' : 'blue'}">${esc(job.status)}</span></td>
    <td>${esc(job.preset.name)}<small>${isReal ? 'CPU · libx265 · source kept' : 'Simulation · source unchanged'}</small><small>${esc(label(job.backend))}</small></td>
    <td>${esc(size(job.source_size))}</td>
    <td>${actualOutput ? esc(size(job.output_size)) : job.status === 'completed' ? job.estimate_basis === 'planning_range' ? `${esc(size(job.estimated_output_size_low))} – ${esc(size(job.estimated_output_size_high))} planning` : `~${esc(size(job.estimated_output_size))} estimate` : '—'}${outputPath}</td>
    <td class="saving">${savingText}</td>
    <td>${esc(label(job.source_codec))} → ${esc(label(job.preset.destination_codec))}</td>
    <td>${Math.round(job.elapsed_seconds)}s</td><td>${esc(finished)}</td>
  </tr>`;
  }).join('') || '<tr><td colspan="9" class="empty">No completed or stopped jobs yet. Add jobs from Movies or Shows to get started.</td></tr>';
  window.dispatchEvent(new CustomEvent('table-updated', {detail: {table: rows.closest('table')}}));
}
