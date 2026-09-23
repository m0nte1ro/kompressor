import {$, $$, api, notify} from './common.js';

const dialog = $('#preset-editor');
if (dialog) {
  const form = $('#preset-form');
  const field = name => form.elements.namedItem(name);
  const error = $('#preset-error');
  let hdrMetadata = null, originalAudioPolicy = null, originalPreserveAudio = true;
  let editing = null, saving = false, revision = 0, efficientAudioRules = null, sourceApplicability = [];
  const allSourceResolutions = ['480p', '576p', '720p', '1080p', '2160p'];
  let planningVideoBitrateLow = null, planningVideoBitrateHigh = null;
  const mbps = ['target_video_bitrate', 'minimum_source_bitrate'];
  const numeric = [...mbps, 'target_audio_bitrate', 'stereo_audio_bitrate', 'minimum_expected_saving_percent', 'quality_value', 'output_bit_depth'];
  const boolean = ['enabled', 'preserve_hdr_metadata', 'allow_hevc_reencode'];
  const text = ['name', 'scope', 'intent', 'backend', 'destination_codec', 'target_resolution', 'audio_policy', 'audio_conversion_policy', 'rate_control', 'encoder_preset', 'hdr_support', 'hdr_policy'];

  function planningDefaults(scope, intent) {
    if (intent === 'preserve_quality') return scope === 'movie' ? [1, 30] : [1, 20];
    return scope === 'movie' ? [2, 8] : [1, 6];
  }

  function intentDefaults(scope, intent) {
    const preserve = intent === 'preserve_quality';
    const planning = planningDefaults(scope, intent);
    return {
      backend: preserve || scope === 'movie' ? 'cpu' : 'qsv',
      destination_codec: 'hevc',
      rate_control: preserve || scope === 'movie' ? 'crf' : 'icq',
      quality_value: preserve ? 18 : scope === 'movie' ? 22 : 23,
      encoder_preset: 'slow',
      output_bit_depth: 10,
      hdr_support: 'hdr10_experimental',
      hdr_policy: 'preserve_source',
      planning_video_bitrate_low: planning[0],
      planning_video_bitrate_high: planning[1],
      target_video_bitrate: '',
      target_resolution: 'keep',
      audio_policy: 'preserve',
      audio_conversion_policy: 'preserve',
      target_audio_bitrate: 640,
      stereo_audio_bitrate: 192,
      minimum_source_bitrate: preserve ? 0 : scope === 'movie' ? 8 : 4,
      minimum_expected_saving_percent: preserve ? 5 : 20,
      preserve_hdr_metadata: true,
      allow_hevc_reencode: false,
    };
  }

  function applyIntentDefaults() {
    const defaults = intentDefaults(field('scope').value, field('intent').value);
    planningVideoBitrateLow = Math.round(defaults.planning_video_bitrate_low * 1e6);
    planningVideoBitrateHigh = Math.round(defaults.planning_video_bitrate_high * 1e6);
    for (const [name, value] of Object.entries(defaults)) {
      const control = field(name);
      if (control?.type === 'checkbox') control.checked = value;
      else if (control) control.value = value;
    }
    audioPolicy();
    backendFields();
    rateFields();
  }

  function audioPolicy() {
    const efficient = field('audio_policy').value === 'efficient' || field('audio_conversion_policy').value === 'efficient';
    field('target_audio_bitrate').disabled = !efficient;
    field('target_audio_bitrate').required = efficient;
    field('stereo_audio_bitrate').disabled = !efficient;
    field('stereo_audio_bitrate').required = efficient;
    $('#audio-conversion-options').hidden = !efficient;
  }
  function backendFields() {
    const qsv = field('backend').value === 'qsv';
    $('#encoder-effort-field').hidden = qsv;
    $('#qsv-validation-field').hidden = !qsv;
    const rateControl = field('rate_control');
    const crf = rateControl.querySelector('option[value="crf"]');
    const icq = rateControl.querySelector('option[value="icq"]');
    crf.disabled = qsv;
    icq.disabled = !qsv;
    if (qsv && rateControl.value === 'crf') rateControl.value = 'icq';
    if (!qsv && rateControl.value === 'icq') rateControl.value = 'crf';
    rateFields();
  }
  function hdrPolicyFields() {
    let toneMap = field('hdr_policy').value === 'tone_map_to_sdr';
    let sdrOnly = field('hdr_support').value === 'sdr_only';
    if (toneMap && sdrOnly) {
      field('hdr_support').value = 'hdr10_experimental';
      sdrOnly = false;
    }
    field('hdr_policy').disabled = sdrOnly;
    if (sdrOnly) field('hdr_policy').value = 'preserve_source';
    toneMap = field('hdr_policy').value === 'tone_map_to_sdr';
    const preserveMetadata = field('preserve_hdr_metadata');
    preserveMetadata.disabled = toneMap || sdrOnly;
    if (toneMap || sdrOnly) preserveMetadata.checked = false;
  }
  function rateFields() {
    const quality = field('rate_control').value !== 'abr';
    const icq = field('rate_control').value === 'icq';
    const qualityValue = field('quality_value');
    field('target_video_bitrate').disabled = quality;
    field('target_video_bitrate').required = !quality;
    $('#abr-target-field').hidden = quality;
    $('#quality-value-field').hidden = !quality;
    for (const name of ['quality_value']) {
      field(name).disabled = !quality;
      field(name).required = quality;
    }
    if (quality) {
      if (planningVideoBitrateLow == null || planningVideoBitrateHigh == null) {
        const planning = planningDefaults(field('scope').value, field('intent').value);
        planningVideoBitrateLow = planning[0] * 1e6;
        planningVideoBitrateHigh = planning[1] * 1e6;
      }
    }
    qualityValue.type = icq ? 'range' : 'number';
    qualityValue.step = icq ? '1' : '0.1';
    qualityValue.min = icq ? '18' : '0';
    qualityValue.max = icq ? '30' : '51';
    $('#quality-value-label').textContent = icq ? 'ICQ quality (18–30; lower = higher quality)' : 'CRF quality (lower = higher quality)';
    updateQualityOutput();
  }
  function updateQualityOutput() {
    const qualityValue = field('quality_value');
    const output = $('#quality-value-output');
    const icq = field('rate_control').value === 'icq';
    const value = Number(qualityValue.value);
    const min = Number(qualityValue.min);
    const max = Number(qualityValue.max);
    output.hidden = !icq || !Number.isFinite(value);
    if (output.hidden) return;
    output.textContent = qualityValue.value;
    output.style.left = `${((value - min) / (max - min)) * 100}%`;
  }
  field('rate_control').addEventListener('change', rateFields);
  function close() {
    if (saving) return;
    ++revision;
    dialog.close();
  }
  $$('[data-preset-close]').forEach(button => button.addEventListener('click', close));
  dialog.addEventListener('cancel', event => {event.preventDefault(); close();});
  field('audio_policy').addEventListener('change', audioPolicy);
  field('audio_conversion_policy').addEventListener('change', audioPolicy);
  field('backend').addEventListener('change', backendFields);
  field('quality_value').addEventListener('input', updateQualityOutput);
  field('hdr_policy').addEventListener('change', hdrPolicyFields);
  field('hdr_support').addEventListener('change', hdrPolicyFields);
  field('intent').addEventListener('change', applyIntentDefaults);
  field('scope').addEventListener('change', applyIntentDefaults);

  function open(preset, id = null) {
    editing = id;
    efficientAudioRules = preset.efficient_audio_rules ?? null;
    sourceApplicability = [...(preset.source_resolutions ?? allSourceResolutions)];
    hdrMetadata = preset.hdr_metadata ?? null;
    originalAudioPolicy = preset.audio_policy;
    originalPreserveAudio = preset.preserve_audio_by_default ?? (preset.audio_policy === 'preserve');
    planningVideoBitrateLow = preset.planning_video_bitrate_low ?? null;
    planningVideoBitrateHigh = preset.planning_video_bitrate_high ?? null;
    error.hidden = true;
    $('#preset-editor-title').textContent = id ? 'Edit preset' : 'New preset';
    [...text, ...numeric].forEach(name => field(name).value = preset[name] ?? '');
    mbps.forEach(name => field(name).value = preset[name] == null ? '' : preset[name] / 1e6);
    for (const name of ['stereo_audio_bitrate', 'target_audio_bitrate']) {
      const value = (preset[name] ?? (name === 'stereo_audio_bitrate' ? 192000 : 640000)) / 1000;
      if (![...field(name).options].some(option => Number(option.value) === value)) field(name).add(new Option(`${value} kbps`, String(value)));
    }
    field('stereo_audio_bitrate').value = (preset.stereo_audio_bitrate ?? 192000) / 1000;
    field('target_audio_bitrate').value = (preset.target_audio_bitrate ?? 640000) / 1000;
    boolean.forEach(name => field(name).checked = preset[name]);
    audioPolicy();
    backendFields();
    field('hdr_policy').querySelector('option[value="tone_map_to_sdr"]').disabled = preset.origin === 'built_in';
    hdrPolicyFields();
    rateFields();
    dialog.showModal();
  }

  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-new-preset], [data-edit-preset], [data-duplicate-preset], [data-toggle-preset]');
    if (!button || saving) return;
    const current = ++revision;
    if (button.hasAttribute('data-new-preset')) {
      const scope = button.dataset.newPreset;
      open({name: '', scope, intent: 'streaming_quality', enabled: true, backend: 'cpu', destination_codec: 'hevc',
        target_video_bitrate: null, rate_control: 'crf', quality_value: scope === 'movie' ? 22 : 23,
        source_resolutions: allSourceResolutions, encoder_preset: 'slow', output_bit_depth: 10, hdr_support: 'hdr10_experimental', hdr_policy: 'preserve_source',
        planning_video_bitrate_low: 2000000, planning_video_bitrate_high: 6000000, stereo_audio_bitrate: 192000, target_resolution: 'keep',
        audio_policy: 'preserve', audio_conversion_policy: 'preserve', target_audio_bitrate: null, preserve_hdr_metadata: true,
        minimum_source_bitrate: 8000000,
        minimum_expected_saving_percent: 20, allow_hevc_reencode: false});
      return;
    }
    button.disabled = true;
    try {
      const id = button.closest('[data-preset-id]').dataset.presetId;
      if (button.hasAttribute('data-duplicate-preset')) {
        await api(`/api/presets/${encodeURIComponent(id)}/duplicate`, {method: 'POST'});
        window.location.reload();
        return;
      }
      const presets = await api('/api/presets');
      if (current !== revision) return;
      const preset = presets.find(p => p.id === id);
      if (!preset) throw new Error('Preset no longer exists. Reload Settings.');
      if (button.hasAttribute('data-edit-preset')) open(preset, id);
      else {
        const {id: ignored, ...payload} = preset;
        await api(`/api/presets/${encodeURIComponent(id)}`, {method: 'PUT', body: JSON.stringify({...payload, enabled: !payload.enabled})});
        window.location.reload();
      }
    } catch (err) { notify(err.message, true); }
    finally { button.disabled = false; }
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (saving) return;
    const payload = {};
    text.forEach(name => payload[name] = field(name).value);
    numeric.forEach(name => payload[name] = field(name).value === '' ? null : Number(field(name).value));
    payload.source_resolutions = sourceApplicability;
    if (hdrMetadata && payload.hdr_policy !== 'tone_map_to_sdr') payload.hdr_metadata = hdrMetadata;
    payload.planning_video_bitrate_low = payload.rate_control === 'abr' ? null : planningVideoBitrateLow;
    payload.planning_video_bitrate_high = payload.rate_control === 'abr' ? null : planningVideoBitrateHigh;
    boolean.forEach(name => payload[name] = field(name).checked);
    payload.preserve_audio_by_default = payload.audio_policy === originalAudioPolicy ? originalPreserveAudio : payload.audio_policy === 'preserve';
    if (efficientAudioRules) payload.efficient_audio_rules = efficientAudioRules;
    mbps.forEach(name => payload[name] = payload[name] == null ? null : Math.round(payload[name] * 1e6));
    payload.stereo_audio_bitrate *= 1000;
    if (payload.rate_control === 'abr') {payload.quality_value = null; payload.planning_video_bitrate_low = null; payload.planning_video_bitrate_high = null;}
    else payload.target_video_bitrate = null;
    payload.target_audio_bitrate = payload.audio_policy === 'efficient' || payload.audio_conversion_policy === 'efficient' ? Math.round(payload.target_audio_bitrate * 1000) : null;
    saving = true;
    $$('input, select, button', form).forEach(control => control.disabled = true);
    error.hidden = true;
    try {
      await api(editing ? `/api/presets/${encodeURIComponent(editing)}` : '/api/presets', {
        method: editing ? 'PUT' : 'POST', body: JSON.stringify(payload),
      });
      window.location.reload();
    } catch (err) {error.textContent = err.message; error.hidden = false;}
    finally {
      saving = false;
      $$('input, select, button', form).forEach(control => control.disabled = false);
      audioPolicy();
      rateFields();
    }
  });
}
