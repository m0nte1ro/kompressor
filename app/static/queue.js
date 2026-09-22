import {$, $$, escapeHTML as esc, label, size} from './common.js';

function planningSaving(job) {
  return job.planning_saving ?? job.estimated_saving ?? 0;
}

function jobDetails(job) {
  return `<div class="job-name">${esc(job.name)}</div>
    <p class="job-meta">${esc(job.preset.name)} · ${esc(label(job.backend))} · ${esc(label(job.preset.destination_codec))}</p>
    <p class="saving">${job.estimate_basis === 'planning_range' ? `${esc(size(job.estimated_saving_low))} – ${esc(size(job.estimated_saving_high))} planning reduction` : `~${esc(size(job.estimated_saving))} estimated reduction`}</p>
    <p class="job-meta">${job.replace_source ? 'Replace after validation · simulated only' : 'Keep original · test copy · no storage reclaimed'}</p>`;
}

export function renderQueue(queue) {
  for (const lane of queue.lanes) {
    const section = $(`[data-backend="${lane.backend}"]`);
    if (!section) continue;
    const active = $('.active-slot', section);
    if (active.dataset.job !== (lane.active?.id ?? 'idle')) {
      active.dataset.job = lane.active?.id ?? 'idle';
      active.innerHTML = lane.active ? `<article class="active-job" data-job="${esc(lane.active.id)}">
        <span class="badge green active-status"></span>${jobDetails(lane.active)}
        <progress max="100" value="0" aria-label="Simulated encoding progress"></progress>
        <p class="job-meta"><span class="progress-text"></span> · Priority: ${esc(lane.active.priority)}</p>
        <div class="job-controls"><button class="danger" data-queue-action="skip">Stop &amp; Skip</button></div>
      </article>` : '<p class="empty">Worker idle · add a job from the library</p>';
    }
    if (lane.active) {
      $('progress', active).value = lane.active.progress;
      $('.progress-text', active).textContent = `${lane.active.progress.toFixed(1)}% · ${Math.floor(lane.active.elapsed_seconds)}s simulated`;
      $('.active-status', active).textContent = lane.active.status.toUpperCase();
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
  const saving = completed.reduce((sum, j) => sum + planningSaving(j), 0);
  $('#history-stats').innerHTML = [
    ['Planning / estimated reduction', `~${size(saving)}`], ['Completed simulations', completed.length],
    ['CPU / QSV completed', `${completed.filter(j => j.backend === 'cpu').length} / ${completed.filter(j => j.backend === 'qsv').length}`],
    ['Skipped / blocked', queue.history.filter(j => ['skipped', 'blocked'].includes(j.status)).length],
  ].map(([title, value]) => `<div class="card stat"><span>${esc(title)}</span><strong>${esc(value)}</strong></div>`).join('');
  rows.innerHTML = queue.history.map(job => `<tr>
    <th scope="row">${esc(job.name)}${(job.reasons ?? []).map(reason => `<small class="reason">${esc(reason)}</small>`).join('')}</th><td><span class="badge ${job.status === 'completed' ? 'green' : 'red'}">${esc(job.status)}</span></td>
    <td>${esc(job.preset.name)}<small>${job.replace_source ? "Replace after validation · simulated" : "Keep original · no storage reclaimed"}</small><small>${esc(label(job.backend))}</small></td><td>${esc(size(job.source_size))}</td>
    <td>${job.status === 'completed' ? job.estimate_basis === 'planning_range' ? `${esc(size(job.estimated_output_size_low))} – ${esc(size(job.estimated_output_size_high))} planning` : `~${esc(size(job.estimated_output_size))}` : '—'}</td>
    <td class="saving">${job.status === 'completed' ? job.estimate_basis === 'planning_range' ? `${esc(size(job.estimated_saving_low))} – ${esc(size(job.estimated_saving_high))} planning` : `~${esc(size(job.estimated_saving))} (${(100 * job.estimated_saving / job.source_size).toFixed(1)}%)` : '—'}</td>
    <td>${esc(label(job.source_codec))} → ${esc(label(job.preset.destination_codec))}</td>
    <td>${Math.round(job.elapsed_seconds)}s</td><td>${esc(new Date(job.finished_at).toLocaleString())}</td>
  </tr>`).join('') || '<tr><td colspan="9" class="empty">No simulations finished yet. Add jobs from Movies or Shows to get started.</td></tr>';
}
