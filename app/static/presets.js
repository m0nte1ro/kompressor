import {$, $$, api, notify} from './common.js';

const dialog = $('#preset-editor');
if (dialog) {
  const form = $('#preset-form');
  const field = name => form.elements.namedItem(name);
  const error = $('#preset-error');
  let editing = null, saving = false, revision = 0;
  const numeric = ['target_video_bitrate', 'target_audio_bitrate', 'minimum_source_bitrate', 'minimum_expected_saving_percent'];
  const boolean = ['enabled', 'preserve_hdr_metadata', 'allow_hevc_reencode'];
  const text = ['name', 'scope', 'backend', 'destination_codec', 'resolution_policy', 'audio_policy'];

  function audioPolicy() {
    const efficient = field('audio_policy').value === 'efficient';
    field('target_audio_bitrate').disabled = !efficient;
    field('target_audio_bitrate').required = efficient;
  }
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
    ['target_video_bitrate', 'minimum_source_bitrate'].forEach(name => field(name).value = preset[name] / 1e6);
    field('target_audio_bitrate').value = (preset.target_audio_bitrate ?? 384000) / 1000;
    boolean.forEach(name => field(name).checked = preset[name]);
    audioPolicy();
    dialog.showModal();
  }

  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-new-preset], [data-edit-preset], [data-duplicate-preset], [data-toggle-preset]');
    if (!button || saving) return;
    const current = ++revision;
    if (button.hasAttribute('data-new-preset')) {
      const scope = button.dataset.newPreset;
      open({name: '', scope, enabled: true, backend: scope === 'movie' ? 'cpu' : 'qsv', destination_codec: 'hevc',
        target_video_bitrate: scope === 'movie' ? 10000000 : 2500000, resolution_policy: 'preserve',
        audio_policy: 'preserve', target_audio_bitrate: null, preserve_hdr_metadata: true,
        minimum_source_bitrate: scope === 'movie' ? 14000000 : 4000000,
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
    numeric.forEach(name => payload[name] = Number(field(name).value));
    boolean.forEach(name => payload[name] = field(name).checked);
    ['target_video_bitrate', 'minimum_source_bitrate'].forEach(name => payload[name] = Math.round(payload[name] * 1e6));
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
    }
  });
}
