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


const scanButton = $('#scan-library');
if (scanButton) {
  scanButton.addEventListener('click', async () => {
    scanButton.disabled = true;
    const status = $('#scan-status');
    const errors = $('#scan-errors');
    errors.replaceChildren();
    status.textContent = 'Scanning and probing…';
    try {
      // The explicit scan is synchronous; per-file ffprobe calls have backend timeouts.
      const controller = new AbortController();
      const report = await api('/api/library/scan', {method: 'POST', signal: controller.signal});
      status.textContent = `${report.state}: ${report.roots.reduce((n, root) => n + root.discovered, 0)} files discovered. Open Movies or Shows to view the inventory.`;
      for (const root of report.roots) {
        for (const message of [...root.errors, ...root.reconciliation.issues.map(issue => `${issue.path}: ${issue.reason}`)]) {
          const item = document.createElement('li');
          item.textContent = message;
          errors.append(item);
        }
      }
    } catch (error) {
      status.textContent = error.message;
    } finally {
      scanButton.disabled = false;
    }
  });
}
