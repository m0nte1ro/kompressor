import {$, $$, api, escapeHTML as esc, mapLimit} from './common.js';

export function setupTags(selectedTargets) {
  const dialog = $('#tag-editor');
  if (!dialog) return;
  const form = $('#tag-form'), error = $('#tag-error'), save = $('#save-tags');
  let targets = [], saving = false, revision = 0;
  const chosen = () => $$('input[name="tag"]:checked', form).map(input => input.value);
  function updateFloor() {
    const visible = chosen().includes('Quality Floor') && !(targets.length > 1 && $('#tag-operation').value === 'remove');
    $('#quality-floor-fields').hidden = !visible;
    $('#floor-bitrate').required = visible;
    $('#floor-bitrate').disabled = !visible;
    $('#floor-height').disabled = !visible;
  }
  function close() {
    if (saving) return;
    ++revision;
    dialog.close();
  }
  $$('[data-tag-close]').forEach(button => button.addEventListener('click', close));
  dialog.addEventListener('cancel', event => {event.preventDefault(); close();});
  form.addEventListener('change', updateFloor);

  async function open(nextTargets) {
    if (!nextTargets.length || saving) return;
    targets = nextTargets;
    const current = ++revision;
    form.reset();
    save.disabled = true;
    $('#tag-choices').disabled = true;
    error.hidden = true;
    $('#tag-title').textContent = targets.length > 1 ? `Tag ${targets.length} selected items` : 'Manage tags';
    $('#tag-operation-label').hidden = targets.length === 1;
    $('#tag-inheritance').textContent = 'Loading direct and inherited tags…';
    updateFloor();
    dialog.showModal();
    try {
      const descriptions = await mapLimit(targets, target => {
        const params = new URLSearchParams(Object.entries(target).filter(([,value]) => value != null));
        return api(`/api/tags?${params}`);
      });
      if (current !== revision || !dialog.open) return;
      if (targets.length === 1) {
        const direct = descriptions[0].direct;
        $$('input[name="tag"]', form).forEach(input => input.checked = direct.tags.includes(input.value));
        if (direct.quality_floor) {
          $('#floor-bitrate').value = direct.quality_floor.minimum_video_bitrate / 1e6;
          $('#floor-height').value = direct.quality_floor.minimum_height;
        }
      }
      $('#tag-inheritance').innerHTML = descriptions.map(item => `<div class="source-item">
        <strong>${esc(item.name)}</strong><p>Direct: ${esc(item.direct.tags.join(', ') || 'None')}</p>
        ${item.inherited.map(parent => `<p class="muted">Inherited from ${esc(parent.source)}: ${esc(parent.tags.join(', ') || 'None')}</p>`).join('')}
        <p>Effective: ${esc(item.effective_tags.join(', ') || 'None')}</p>
        ${item.effective_quality_floor ? `<p class="muted">Effective floor: ${item.effective_quality_floor.minimum_video_bitrate / 1e6} Mbps · ${item.effective_quality_floor.minimum_height}p</p>` : ''}
      </div>`).join('');
      updateFloor();
      $('#tag-choices').disabled = false;
      save.disabled = false;
    } catch (err) {
      if (current === revision) {error.textContent = err.message; error.hidden = false;}
    }
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-tag-kind]');
    if (!button) return;
    const target = {kind: button.dataset.tagKind, id: button.dataset.tagId};
    if (button.dataset.tagSeason !== undefined) target.season = Number(button.dataset.tagSeason);
    open([target]);
  });
  $('#tags-selected')?.addEventListener('click', () => open(selectedTargets()));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (saving || save.disabled) return;
    const operation = targets.length === 1 ? 'replace' : $('#tag-operation').value;
    const tags = chosen();
    const quality_floor = tags.includes('Quality Floor') && operation !== 'remove' ? {
      minimum_video_bitrate: Math.round(Number($('#floor-bitrate').value) * 1e6),
      minimum_height: Number($('#floor-height').value),
    } : null;
    saving = true;
    error.hidden = true;
    $$('input, select, button', form).forEach(control => control.disabled = true);
    try {
      await api('/api/tags', {method: 'PATCH', body: JSON.stringify({targets, operation, tags, quality_floor})});
      window.location.reload();
    } catch (err) {error.textContent = err.message; error.hidden = false;}
    finally {
      saving = false;
      $$('input, select, button', form).forEach(control => control.disabled = false);
      updateFloor();
    }
  });
}
