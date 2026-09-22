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
