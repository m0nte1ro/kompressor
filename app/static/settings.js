import {$, api, notify} from './common.js';

const form = $('#library-paths-form');
if (form) {
  const button = $('button[type="submit"]', form);
  const status = $('#library-paths-status');

  form.addEventListener('submit', async event => {
    event.preventDefault();
    button.disabled = true;
    status.textContent = 'Saving…';
    try {
      await api('/api/settings', {
        method: 'PUT',
        body: JSON.stringify({
          movies_path: form.elements.movies_path.value,
          shows_path: form.elements.shows_path.value,
        }),
      });
      status.textContent = 'Saved';
    } catch (error) {
      status.textContent = '';
      notify(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}


const workerForm = $('#worker-schedule-form');
if (workerForm) {
  const button = $('button[type="submit"]', workerForm);
  const status = $('#worker-schedule-status');

  workerForm.addEventListener('submit', async event => {
    event.preventDefault();
    button.disabled = true;
    status.textContent = 'Saving…';
    const lane = backend => ({
      quiet_hours_enabled: workerForm.elements[`${backend}_quiet_hours_enabled`].checked,
      quiet_start: workerForm.elements[`${backend}_quiet_start`].value,
      quiet_end: workerForm.elements[`${backend}_quiet_end`].value,
      quiet_cutoff_percent: Number(workerForm.elements[`${backend}_quiet_cutoff_percent`].value),
    });
    try {
      await api('/api/queue/workers/settings', {
        method: 'PUT',
        body: JSON.stringify({
          timezone: workerForm.elements.timezone.value,
          cpu: lane('cpu'),
          qsv: lane('qsv'),
        }),
      });
      status.textContent = 'Saved';
    } catch (error) {
      status.textContent = '';
      notify(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}
