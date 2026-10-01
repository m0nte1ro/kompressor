import {$, duration, escapeHTML as esc, label, size} from './common.js';

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

// Measured outputs that grew are shown in red, savings in green.
function savingClass(job) {
  return (job.measured_saving ?? 0) < 0 ? 'saving larger' : 'saving';
}

// Linear extrapolation from progress so far; hidden until 1% so early noise is not shown.
function remaining(job) {
  if (job.status !== 'encoding' || !(job.progress >= 1 && job.progress < 100) || !(job.elapsed_seconds > 0)) return null;
  return `Estimated: ${duration(job.elapsed_seconds * (100 - job.progress) / job.progress)} left`;
}

function jobDetails(job) {
  const estimate = job.estimate_basis === 'planning_range'
    ? `${esc(size(job.estimated_saving_low))} – ${esc(size(job.estimated_saving_high))} planning estimate`
    : `~${esc(size(job.estimated_saving))} estimated reduction`;
  const source = job.execution_mode !== 'real'
    ? job.replace_source ? 'Replace after validation · simulated only' : 'Keep original · simulated test copy'
    : job.source_replaced ? `Source replaced · ${esc(job.output_path)}`
    : job.reuse_output_path ? 'Replace source with the kept output after re-validation'
    : job.replace_source ? 'Replace source after validation and bit-exact audio check'
    : `Keep original · output ${esc(job.output_path || 'in workspace after validation')}`;
  return `<div class="job-name">${esc(job.name)}</div>
    <p class="job-meta">${esc(job.preset.name)} · ${esc(label(job.backend))} · ${esc(label(job.preset.destination_codec))}</p>
    <p class="${savingClass(job)}">${measuredChange(job) ? esc(measuredChange(job)) : estimate}</p>
    <p class="job-meta">${source}</p>`;
}

export function renderQueue(queue) {
  const workerLanes = queue.workers?.lanes;
  const globalToggle = $('[data-worker-action="toggle-pause-all"]');
  if (globalToggle && workerLanes) {
    const lanes = Object.values(workerLanes).filter(lane => lane.available !== false);
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
      const mode = !lane.available ? 'unavailable'
        : control.paused ? 'paused'
        : control.quiet_active ? 'quiet hours'
        : 'ready';
      const badge = $('.worker-mode', section);
      if (badge) badge.textContent = mode;
      section.classList.toggle('lane-paused', lane.available && (control.paused || control.quiet_active));
      const toggle = $('[data-worker-action="toggle-pause"]', section);
      if (toggle) {
        toggle.textContent = control.paused ? 'Resume worker' : 'Pause after current';
        toggle.dataset.paused = String(control.paused);
        toggle.setAttribute('aria-pressed', String(control.paused));
      }
      const stop = $('[data-worker-action="stop-active"]', section);
      if (stop) stop.disabled = !lane.active || lane.active.status === 'replacing';
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
      const seconds = Math.floor(lane.active.elapsed_seconds);
      const clock = `${duration(seconds)} ${lane.active.execution_mode === 'real' ? 'elapsed' : 'simulated'}`;
      $('.progress-text', active).textContent = [progress, clock, remaining(lane.active)].filter(Boolean).join(' · ');
      $('.active-status', active).textContent = lane.active.status.toUpperCase();
      // A source swap cannot be interrupted; the backend refuses Stop & Skip too.
      $('[data-queue-action="skip"]', active).hidden = lane.active.status === 'replacing';
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

// Completed keep-original real encodes whose output is still in the workspace.
export function keptOutput(job) {
  return job.execution_mode === 'real' && job.status === 'completed' && !job.replace_source
    && !job.source_replaced && Boolean(job.output_path);
}

function analysisResults(job) {
  const lines = [];
  if (job.comparison) {
    lines.push(`Screenshots (${job.comparison.files.length}): <code>${esc(job.comparison.folder)}</code>`);
  }
  if (job.vmaf) {
    const v = job.vmaf;
    lines.push(`VMAF ${esc(v.mean)} mean · 5% low ${esc(v.p5)} · min ${esc(v.min)} · ${esc(duration(v.duration_seconds))} from ${esc(duration(v.start_seconds))}`);
  }
  return lines.map(line => `<small>${line}</small>`).join('');
}

function historyActions(job, pending) {
  const replacing = pending.some(other => other.replaces_job_id === job.id);
  const remove = replacing ? '' : '<button data-history-action="delete" title="Remove this entry from History">Delete</button>';
  if (!keptOutput(job)) return `${remove ? `<div class="job-controls">${remove}</div>` : ''}${analysisResults(job)}`;
  return `<div class="job-controls">${replacing
    ? '<span class="badge blue">Replacement queued</span>'
    : '<button class="danger" data-history-action="replace">Replace source</button>'}
    <button data-history-action="compare" title="Random side-by-side screenshots: source left, output right">Compare</button>
    <button data-history-action="benchmark" title="VMAF score for a segment of chosen length">Benchmark</button>
    ${remove}</div>
    ${analysisResults(job)}`;
}

export function renderAnalysis(status) {
  const box = $('#analysis-status');
  if (!box) return;
  const labels = {comparison: 'Comparison screenshots', vmaf: 'VMAF benchmark'};
  const what = `${labels[status.kind] ?? 'Analysis'} · ${status.name ?? ''}`;
  const text = status.state === 'running' ? `${what} · ${Math.round(status.progress ?? 0)}%…`
    : status.state === 'completed' && status.kind === 'comparison' ? `${what} · saved to ${status.result.folder}`
    : status.state === 'completed' && status.kind === 'vmaf'
      ? `${what} · ${status.result.mean} mean · 5% low ${status.result.p5} · min ${status.result.min} (${status.result.frames} frames)`
    : status.state === 'failed' || status.state === 'cancelled' ? `${what} · ${status.state}: ${status.error}`
    : '';
  box.textContent = text;
  box.hidden = !text;
  box.classList.toggle('error', status.state === 'failed');
}

export function renderHistory(queue) {
  const rows = $('#history-rows');
  if (!rows) return;
  const pending = queue.lanes.flatMap(lane => [...(lane.active ? [lane.active] : []), ...lane.queued]);
  const key = JSON.stringify([queue.history, pending.map(job => job.replaces_job_id)]);
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
    return `<tr data-job="${esc(job.id)}" data-name="${esc(job.name)}" data-sort-name="${esc(job.name)}" data-sort-status="${esc(job.status)}"
      data-sort-preset="${esc(job.preset.name)}" data-sort-source="${job.source_size ?? 0}"
      data-sort-output="${actualOutput ? job.output_size : outputEstimate}" data-sort-saving="${actualSaving ? job.measured_saving : planningSaving(job)}"
      data-sort-video="${esc(job.source_codec)}" data-sort-elapsed="${job.elapsed_seconds ?? 0}"
      data-sort-finished="${job.finished_at ? new Date(job.finished_at).getTime() : 0}">
    <th scope="row">${esc(job.name)}${(job.reasons ?? []).map(reason => `<small class="reason">${esc(reason)}</small>`).join('')}${error}</th>
    <td><span class="badge ${job.status === 'completed' ? 'green' : job.status === 'failed' ? 'red' : 'blue'}">${esc(job.status)}</span></td>
    <td>${esc(job.preset.name)}<small>${isReal ? (job.source_replaced ? 'Source replaced' : 'Source kept') : 'Simulation · source unchanged'}</small><small>${esc(label(job.backend))}</small></td>
    <td>${esc(size(job.source_size))}</td>
    <td>${actualOutput ? esc(size(job.output_size)) : job.status === 'completed' ? job.estimate_basis === 'planning_range' ? `${esc(size(job.estimated_output_size_low))} – ${esc(size(job.estimated_output_size_high))} planning` : `~${esc(size(job.estimated_output_size))} estimate` : '—'}${outputPath}</td>
    <td class="${actualSaving ? savingClass(job) : 'saving'}">${savingText}</td>
    <td>${esc(label(job.source_codec))} → ${esc(label(job.preset.destination_codec))}</td>
    <td>${esc(duration(job.elapsed_seconds))}</td><td>${esc(finished)}</td>
    <td>${historyActions(job, pending)}</td>
  </tr>`;
  }).join('') || '<tr><td colspan="10" class="empty">No completed or stopped jobs yet. Add jobs from Movies or Shows to get started.</td></tr>';
  window.dispatchEvent(new CustomEvent('table-updated', {detail: {table: rows.closest('table')}}));
}
