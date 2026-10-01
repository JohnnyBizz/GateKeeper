// Operator controls. Every call carries the session's CSRF token; the
// session cookie itself is HttpOnly and not readable here.
document.querySelectorAll('button[data-action]').forEach((button) => {
  button.addEventListener('click', async () => {
    const error = document.getElementById('control-error');
    error.textContent = '';
    button.disabled = true;
    const enabled = button.dataset.enabled;
    try {
      const response = await fetch(button.dataset.action, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.GTCC_CSRF || '' },
        body: enabled === '' ? '{}' : JSON.stringify({ enabled: enabled === 'true', reason: 'dashboard' }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        error.textContent = body.detail || 'The control was refused.';
        button.disabled = false;
        return;
      }
      window.location.reload();
    } catch (e) {
      error.textContent = 'Could not reach the server.';
      button.disabled = false;
    }
  });
});
