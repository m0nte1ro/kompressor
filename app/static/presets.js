import {$, $$, api, notify} from './common.js';

const dialog = $('#preset-editor');
if (dialog) {
  const form = $('#preset-form');
  const field = name => form.elements.namedItem(name);
  const error = $('#preset-error');
  let editing = null, saving = false, revision = 0;
  const mbps = ['target_video_bitrate', 'minimum_source_bitrate', 'planning_video_bitrate_low', 'planning_video_bitrate_high'];
  const numeric = [...mbps, 'target_audio_bitrate', 'stereo_audio_bitrate', 'minimum_expected_saving_percent', 'quality_value', 'output_bit_depth'];
  const boolean = ['enabled', 'preserve_hdr_metadata', 'allow_hevc_reencode'];
  const text = ['name', 'scope', 'backend', 'destination_codec', 'resolution_policy', 'audio_policy', 'rate_control', 'encoder_preset', 'hdr_support'];

  function audioPolicy() {
    const efficient = field('audio_policy').value === 'efficient';
    field('target_audio_bitrate').disabled = !efficient;
    field('target_audio_bitrate').required = efficient;
  }
  function rateFields() {
    const quality = field('rate_control').value !== 'abr';
    field('target_video_bitrate').disabled = quality;
    field('target_video_bitrate').required = !quality;
    for (const name of ['quality_value', 'planning_video_bitrate_low', 'planning_video_bitrate_high']) {
      field(name).disabled = !quality;
      field(name).required = quality;
    }
    field('quality_value').step = field('rate_control').value === 'icq' ? '1' : '0.1';
    field('quality_value').min = field('rate_control').value === 'icq' ? '1' : '0';
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

  function open(preset, id = null) {
    editing = id;
    error.hidden = true;
    $('#preset-editor-title').textContent = id ? 'Edit preset' : 'New preset';
    [...text, ...numeric].forEach(name => field(name).value = preset[name] ?? '');
    mbps.forEach(name => field(name).value = preset[name] == null ? '' : preset[name] / 1e6);
    [...field('source_resolutions').options].forEach(option => option.selected = (preset.source_resolutions ?? []).includes(option.value));
    field('stereo_audio_bitrate').value = (preset.stereo_audio_bitrate ?? 192000) / 1000;
    field('target_audio_bitrate').value = (preset.target_audio_bitrate ?? 640000) / 1000;
    boolean.forEach(name => field(name).checked = preset[name]);
    audioPolicy();
    rateFields();
    dialog.showModal();
  }

  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-new-preset], [data-edit-preset], [data-duplicate-preset], [data-toggle-preset]');
    if (!button || saving) return;
    const current = ++revision;
    if (button.hasAttribute('data-new-preset')) {
      const scope = button.dataset.newPreset;
      open({name: '', scope, enabled: true, backend: 'cpu', destination_codec: 'hevc',
        target_video_bitrate: null, rate_control: 'crf', quality_value: scope === 'movie' ? 22 : 23,
        source_resolutions: ['1080p'], encoder_preset: 'slow', output_bit_depth: 10, hdr_support: 'sdr_only',
        planning_video_bitrate_low: 2000000, planning_video_bitrate_high: 6000000, stereo_audio_bitrate: 192000, resolution_policy: 'preserve',
        audio_policy: 'efficient', target_audio_bitrate: 640000, preserve_hdr_metadata: true,
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
    payload.source_resolutions = [...field('source_resolutions').selectedOptions].map(option => option.value);
    boolean.forEach(name => payload[name] = field(name).checked);
    mbps.forEach(name => payload[name] = payload[name] == null ? null : Math.round(payload[name] * 1e6));
    payload.stereo_audio_bitrate *= 1000;
    if (payload.rate_control === 'abr') {payload.quality_value = null; payload.planning_video_bitrate_low = null; payload.planning_video_bitrate_high = null;}
    else payload.target_video_bitrate = null;
    payload.target_audio_bitrate = payload.audio_policy === 'efficient' ? Math.round(payload.target_audio_bitrate * 1000) : null;
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
