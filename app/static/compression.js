import {$, $$, api, bitrate, escapeHTML as esc, label, mapLimit, notify, pendingJobs, size} from './common.js';

export function compressionModal(scope, refreshQueue) {
  const dialog = $('#compression-dialog');
  const form = $('#compression-form');
  let selected = [], presets = [], library, results = [];
  let generation = 0, evaluation = 0, submitting = false, preserveAudioTouched = false;
  const errorBox = $('#modal-error');
  const submit = $('#add-to-queue');

  function error(message) {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  function properties(preset) {
    const rateControl = `${preset.rate_control.toUpperCase()} ${preset.quality_value ?? bitrate(preset.target_video_bitrate)}`;
    const fields = [
      ['Backend', label(preset.backend)], ['Destination codec', label(preset.destination_codec)],
      ['Rate control', rateControl], ...(preset.backend === 'qsv' ? [['Output bit depth', `${preset.output_bit_depth} bit`], ['Validation', 'Experimental']] : [['Encoder effort', preset.encoder_preset], ['Output bit depth', `${preset.output_bit_depth} bit`]]), ['Target resolution', label(preset.target_resolution)],
      ['Audio policy', preset.audio_policy === 'efficient' ? 'Efficient conversion' : 'Preserve every audio track by default'], ...(preset.audio_policy === 'efficient' ? [['Conversion profile', 'AAC stereo / E-AC3 multichannel']] : []), ['HDR input support', label(preset.hdr_support)], ['HDR output policy', label(preset.hdr_policy)],
    ];
    $('#preset-properties').innerHTML = fields.map(([name, value]) =>
      `<label class="field">${esc(name)}<select disabled><option>${esc(value)}</option></select></label>`).join('');
    $('#audio-rule').textContent = 'Preserve Audio tags and the preset’s audio policy take precedence over this checkbox.';
    const movieAudioForced = scope === 'movie' && preset.origin === 'built_in' && preset.audio_policy === 'preserve' && preset.audio_conversion_policy === 'preserve';
    $('#preserve-audio').checked = movieAudioForced || preset.preserve_audio_by_default;
    $('#preserve-audio').disabled = movieAudioForced;
    $('#preserve-audio-text').textContent = movieAudioForced ? 'Preserve Audio · required by this Movie preset' : 'Preserve Audio · copy every audio track';
  }

  async function evaluate() {
    const revision = ++evaluation;
    const session = generation;
    submit.disabled = true;
    results = [];
    error('');
    const preset = presets.find(p => p.id === $('#preset').value);
    if (!preset) return;
    properties(preset);
    $('#eligibility').textContent = 'Checking eligibility…';
    ['estimated-output', 'estimated-saving', 'estimated-percent'].forEach(id => $(`#${id}`).textContent = '—');
    const overrides = {preserve_audio: $('#preserve-audio').checked, preserve_subtitles: $('#preserve-subtitles').checked};
    try {
      const [evaluated, queue] = await Promise.all([
        mapLimit(selected, async row => ({row, ...await api('/api/eligibility', {
          method: 'POST', body: JSON.stringify({media_id: row.dataset.id, scope, preset_id: preset.id, ...overrides}),
        })})), api('/api/queue'),
      ]);
      if (revision !== evaluation || session !== generation || !dialog.open) return;
      const pending = new Set(pendingJobs(queue).filter(j => j.scope === scope).map(j => j.media_id));
      results = evaluated.map(result => ({...result, excluded: !result.eligible || pending.has(result.row.dataset.id),
        reasons: [...result.reasons, ...(pending.has(result.row.dataset.id) ? ['Already queued or active.'] : [])]}));
      const included = results.filter(r => !r.excluded);
      const total = key => included.reduce((sum, r) => sum + (r[key] ?? 0), 0);
      $('#estimated-output').textContent = included.length ? `${size(total('estimated_output_size_low'))} – ${size(total('estimated_output_size_high'))}` : '—';
      $('#estimated-saving').textContent = included.length ? `${size(total('estimated_saving_low'))} – ${size(total('estimated_saving_high'))}` : '—';
      $('#estimated-percent').textContent = total('source_size') ? `${(total('estimated_saving_low') / total('source_size') * 100).toFixed(0)}–${(total('estimated_saving_high') / total('source_size') * 100).toFixed(0)}%` : '—';
      $('#eligibility').innerHTML = `<strong>${included.length} eligible · ${results.length - included.length} excluded</strong>
        <p class="muted">Planning estimates for eligible, unqueued items only. CRF/ICQ ranges are assumptions, not output bounds. Keeping originals reclaims 0 bytes.</p>
        <div class="eligibility-items">${results.map(r => `<div class="eligibility-item">
          <strong>${esc(r.row.dataset.name)}</strong> <span class="badge ${r.excluded ? 'red' : 'green'}">${r.excluded ? 'Excluded' : 'Eligible'}</span>
          <p class="muted">Audio: ${r.preserve_audio ? 'preserved by effective policy' : 'preset conversion policy'} · ${r.estimate_basis === 'planning_range' ? `Planning saving ${esc(size(r.estimated_saving_low))} – ${esc(size(r.estimated_saving_high))}` : `Estimated saving ~${esc(size(r.estimated_saving))}`}</p>
          ${r.reasons.length ? `<ul>${r.reasons.map(reason => `<li>${esc(reason)}</li>`).join('')}</ul>` : ''}
          ${r.warnings.map(warning => `<p class="hdr">${esc(warning)}</p>`).join('')}
        </div>`).join('')}</div>`;
      submit.disabled = included.length === 0;
    } catch (err) {
      if (revision !== evaluation || session !== generation || !dialog.open) return;
      $('#eligibility').textContent = 'Eligibility could not be checked.';
      error(err.message);
    }
  }

  function close() {
    if (submitting) return;
    ++generation;
    ++evaluation;
    dialog.close();
  }
  $$('[data-close]', dialog).forEach(button => button.addEventListener('click', close));
  dialog.addEventListener('cancel', event => {event.preventDefault(); close();});
  dialog.addEventListener('click', event => {
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) close();
  });
  $('#preset').addEventListener('change', () => {
    if (!preserveAudioTouched) {
      const chosen = presets.find(p => p.id === $('#preset').value);
      if (chosen) $('#preserve-audio').checked = chosen.preserve_audio_by_default;
    }
    evaluate();
  });
  $('#preserve-audio').addEventListener('change', () => {preserveAudioTouched = true; evaluate();});
  $('#preserve-subtitles').addEventListener('change', evaluate);

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled || submitting) return;
    submitting = true;
    submit.disabled = true;
    const controls = $$('button, #preset, #replace-source, input', form);
    controls.forEach(control => control.disabled = true);
    error('');
    let failure = '';
    try {
      const response = await api('/api/queue', {method: 'POST', body: JSON.stringify({
        media_ids: selected.map(row => row.dataset.id), scope, preset_id: $('#preset').value,
        preserve_audio: $('#preserve-audio').checked, preserve_subtitles: $('#preserve-subtitles').checked,
        replace_source: $('#replace-source').value === 'true',
      })});
      const exclusions = response.excluded.map(item => {
        const row = selected.find(r => r.dataset.id === item.media_id);
        return `${row?.dataset.name ?? item.media_id}: ${item.reasons.join(' ')}`;
      });
      notify(`${response.added.length} added · ${response.excluded.length} excluded.${exclusions.length ? ` ${exclusions.join(' ')}` : ''}`);
      submitting = false;
      close();
      await refreshQueue();
    } catch (err) {
      failure = err.message;
    } finally {
      submitting = false;
      controls.forEach(control => control.disabled = false);
      if (dialog.open) await evaluate();
      if (failure) {
        if (dialog.open) error(failure);
        else notify(failure, true);
      }
    }
  });

  return async function open(rows) {
    selected = rows;
    const session = ++generation;
    ++evaluation;
    results = [];
    submit.disabled = true;
    error('');
    $('#modal-title').textContent = rows.length === 1 ? rows[0].dataset.name : `Compress ${rows.length} selected ${scope === 'movie' ? 'movies' : 'episodes'}`;
    $('#source-summary').textContent = 'Loading source…';
    $('#eligibility').textContent = 'Loading presets…';
    $('#preset-properties').replaceChildren();
    $('#preset').replaceChildren();
    $('#audio-rule').textContent = 'Preserve Audio tags and preset policy are enforced by the backend.';
    ['estimated-output', 'estimated-saving', 'estimated-percent'].forEach(id => $(`#${id}`).textContent = '—');
    $('#preserve-audio').checked = true;
    $('#preserve-audio').disabled = false;
    $('#preserve-audio-text').textContent = 'Preserve Audio · copy every audio track';
    preserveAudioTouched = false;
    $('#preserve-subtitles').checked = true;
    $('#replace-source').value = 'false';
    dialog.showModal();
    try {
      const loaded = await Promise.all([api(`/api/presets?scope=${encodeURIComponent(scope)}`), library ? Promise.resolve(library) : api('/api/library')]);
      if (session !== generation || !dialog.open) return;
      [presets, library] = loaded;
      $('#preset').innerHTML = presets.map(p => `<option value="${esc(p.id)}" ${!p.enabled || p.destination_codec === 'av1' ? 'disabled' : ''}>${esc(p.name)} · ${esc(label(p.backend))}</option>`).join('');
      const suggested = presets.find(p => p.id === rows[0].dataset.preset && p.enabled && p.destination_codec !== 'av1');
      const chosen = suggested ?? presets.find(p => p.enabled && p.destination_codec !== 'av1');
      if (!chosen) throw new Error('No available presets for this media scope.');
      $('#preset').value = chosen.id;
      properties(chosen);
      const items = scope === 'movie' ? library.movies : library.shows.flatMap(show => show.seasons.flatMap(season => season.episodes));
      $('#source-summary').innerHTML = rows.map(row => {
        const item = items.find(i => i.id === row.dataset.id);
        if (!item) return `<p>${esc(row.dataset.name)} · Source no longer available</p>`;
        const audio = item.audio.map(a => `${label(a.codec)} ${a.channels} ch${a.language ? ` · ${a.language}` : ''}`).join(' / ') || 'No audio';
        return `<div class="source-item"><strong>${esc(row.dataset.name)}</strong><p class="muted">${esc([item.source, item.resolution, label(item.video_codec), bitrate(item.video_bitrate), size(item.size), label(item.hdr), audio].filter(Boolean).join(' · '))}</p></div>`;
      }).join('');
      await evaluate();
    } catch (err) {
      if (session === generation && dialog.open) error(err.message);
    }
  };
}
