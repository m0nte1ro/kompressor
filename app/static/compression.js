import {$, $$, api, bitrate, escapeHTML as esc, label, mapLimit, notify, pendingJobs, size} from './common.js';

export function compressionModal(scope, refreshQueue) {
  const dialog = $('#compression-dialog');
  const form = $('#compression-form');
  let selected = [], presets = [], results = [];
  let presetsRequest = null;
  let generation = 0, evaluation = 0, submitting = false, preserveAudioTouched = false;
  const errorBox = $('#modal-error');
  const submit = $('#add-to-queue');

  async function loadPresets() {
    if (presets.length) return presets;
    if (!presetsRequest) {
      presetsRequest = api(`/api/presets?scope=${encodeURIComponent(scope)}`)
        .then(value => {
          presets = value;
          return value;
        })
        .finally(() => { presetsRequest = null; });
    }
    return presetsRequest;
  }

  async function loadSourceSummary(rows, session) {
    try {
      const details = await mapLimit(rows, row =>
        api(`/api/media/${encodeURIComponent(scope)}/${encodeURIComponent(row.dataset.id)}`));
      if (session !== generation || !dialog.open) return;
      $('#source-summary').innerHTML = details.map(({name, item}) => {
        const audio = item.audio.map(a =>
          `${label(a.codec)} ${a.channels ?? 'Unknown'} ch${a.language ? ` · ${a.language}` : ''}`
        ).join(' / ') || 'No audio';
        const sourceBitrate = item.video_bitrate_estimated ? `≈ ${bitrate(item.video_bitrate)}` : bitrate(item.video_bitrate);
        return `<div class="source-item"><strong>${esc(name)}</strong><p class="muted">${esc(
          [item.source, item.resolution, label(item.video_codec), sourceBitrate, size(item.size), label(item.hdr), audio]
            .filter(Boolean).join(' · ')
        )}</p></div>`;
      }).join('');
    } catch (err) {
      if (session === generation && dialog.open) {
        $('#source-summary').textContent = `Source details unavailable: ${err.message}`;
      }
    }
  }

  function error(message) {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  const realMode = () => $('#library')?.dataset.mediaBackend === 'filesystem';

  function outputHandling() {
    const replace = $('#replace-source').value === 'true';
    $('#output-handling-note').textContent = !realMode()
      ? 'Seed mode simulates the job: no file is created, replaced or deleted.'
      : replace
        ? 'Replace source: the original is swapped out only after the output passes validation, every copied audio track matches the source bit-for-bit, and the measured saving meets the preset minimum. The original stays beside it as a hidden backup until the replaced file is verified. MKV sources only.'
        : 'Keep original: a separate validated MKV is written under the Kompressor workspace. This reclaims no storage and needs free workspace space.';
    const warnings = [];
    if (replace && !$('#preserve-audio').checked) {
      warnings.push('Preserve Audio is off: tracks listed as "convert" below are permanently re-encoded in the replaced file.');
    }
    if (replace && !$('#preserve-subtitles').checked) {
      warnings.push('Subtitles, chapters, attachments and container metadata will be permanently removed from the replaced file.');
    }
    $('#replace-warnings').textContent = warnings.join(' ');
    $('#replace-warnings').hidden = !warnings.length;
  }

  function audioSummary(plan) {
    if (!plan?.length) return 'Audio: no audio tracks';
    const converted = plan.filter(track => track.action !== 'copy');
    const copied = plan.length - converted.length;
    if (!converted.length) return `Audio: all ${copied} track${copied === 1 ? '' : 's'} copied bit-for-bit`;
    return `Audio: ${copied} copied bit-for-bit · ${converted.length} convert → ${
      converted.map(track => `${label(track.codec)} ${track.channels} ch`).join(', ')}`;
  }

  function properties(preset) {
    const mappedRates = preset.qvbr_bitrates_by_resolution ?? {};
    const nominal = Object.keys(mappedRates).length
      ? Object.entries(mappedRates).map(([resolution, rate]) => `${resolution} ${bitrate(rate)}`).join(' · ')
      : bitrate(preset.target_video_bitrate);
    const rateControl = preset.rate_control === 'qvbr'
      ? `QVBR ${preset.quality_value} · nominal ${nominal}`
      : `${preset.rate_control.toUpperCase()} ${preset.quality_value ?? bitrate(preset.target_video_bitrate)}`;
    const fields = [
      ['Backend', label(preset.backend)], ['Destination codec', label(preset.destination_codec)],
      ['Rate control', rateControl], ...(preset.backend === 'qsv' ? [['Output bit depth', `${preset.output_bit_depth} bit`], ['Validation', 'Experimental']] : [['Encoder effort', preset.encoder_preset], ['Output bit depth', `${preset.output_bit_depth} bit`]]), ['Target resolution', label(preset.target_resolution)],
      ['Audio policy', preset.audio_policy === 'efficient' ? 'Efficient conversion' : 'Preserve every audio track by default'], ...(preset.audio_policy === 'efficient' ? [['Conversion profile', 'AAC stereo / E-AC3 multichannel']] : []), ['HDR input support', label(preset.hdr_support)], ['HDR output policy', label(preset.hdr_policy)],
    ];
    $('#preset-properties').innerHTML = fields.map(([name, value]) =>
      `<label class="field">${esc(name)}<select disabled><option>${esc(value)}</option></select></label>`).join('');
    $('#audio-rule').textContent = 'Audio is always copied unless you untick Preserve Audio; Preserve Audio tags and preserve-only presets copy it regardless.';
    const movieAudioForced = scope === 'movie' && preset.origin === 'built_in' && preset.audio_policy === 'preserve' && preset.audio_conversion_policy === 'preserve';
    // Never pre-select conversion: the box starts ticked and only the user can untick it.
    if (movieAudioForced || !preserveAudioTouched) $('#preserve-audio').checked = true;
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
    outputHandling();
    $('#eligibility').textContent = 'Checking eligibility…';
    ['estimated-output', 'estimated-saving', 'estimated-percent'].forEach(id => $(`#${id}`).textContent = '—');
    const overrides = {preserve_audio: $('#preserve-audio').checked, preserve_subtitles: $('#preserve-subtitles').checked,
      replace_source: $('#replace-source').value === 'true'};
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
        <p class="muted">Planning estimates for eligible, unqueued items only. CRF/ICQ/QVBR ranges are assumptions, not output bounds. ${overrides.replace_source ? 'Replacement happens only if the measured saving meets the preset minimum.' : 'Keeping originals reclaims 0 bytes.'}</p>
        <div class="eligibility-items">${results.map(r => `<div class="eligibility-item">
          <strong>${esc(r.row.dataset.name)}</strong> <span class="badge ${r.excluded ? 'red' : 'green'}">${r.excluded ? 'Excluded' : 'Eligible'}</span>
          <p class="muted">${esc(audioSummary(r.audio_plan))} · ${r.estimate_basis === 'planning_range' ? `Planning saving ${esc(size(r.estimated_saving_low))} – ${esc(size(r.estimated_saving_high))}` : `Estimated saving ~${esc(size(r.estimated_saving))}`}</p>
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
  $('#preset').addEventListener('change', evaluate);
  $('#preserve-audio').addEventListener('change', () => {preserveAudioTouched = true; evaluate();});
  $('#preserve-subtitles').addEventListener('change', evaluate);
  $('#replace-source').addEventListener('change', evaluate);

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

  // Presets are tiny and change only via Settings, which reloads its page after edits.
  // Warm this cache while the user is browsing so first modal open is near-instant.
  void loadPresets().catch(() => {});

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
    $('#replace-source').value = 'true';
    outputHandling();
    dialog.showModal();

    // Source details are an indexed per-item lookup and are deliberately not on
    // the critical path for preset selection or eligibility.
    void loadSourceSummary(rows, session);

    try {
      await loadPresets();
      if (session !== generation || !dialog.open) return;
      $('#preset').innerHTML = presets.map(p => `<option value="${esc(p.id)}" ${!p.enabled || p.destination_codec === 'av1' ? 'disabled' : ''}>${esc(p.name)} · ${esc(label(p.backend))}</option>`).join('');
      const usable = p => p.enabled && p.destination_codec !== 'av1';
      // The backend suggestion already prefers an eligible GPU preset for shows.
      const suggested = presets.find(p => p.id === rows[0].dataset.preset && usable(p));
      const chosen = suggested
        ?? presets.find(p => usable(p) && (scope !== 'show' || p.backend === 'qsv'))
        ?? presets.find(usable);
      if (!chosen) throw new Error('No available presets for this media scope.');
      $('#preset').value = chosen.id;
      properties(chosen);
      await evaluate();
    } catch (err) {
      if (session === generation && dialog.open) error(err.message);
    }
  };
}
