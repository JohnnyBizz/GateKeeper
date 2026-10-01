// Posts credentials as JSON and stores only the CSRF token, never the
// password and never a bearer token: the session itself lives in an
// HttpOnly cookie the page cannot read.
document.getElementById('f').addEventListener('submit', async (event) => {
  event.preventDefault();
  const err = document.getElementById('err');
  err.textContent = '';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email: document.getElementById('email').value,
        password: document.getElementById('password').value,
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      err.textContent = body.detail || 'Sign in failed.';
      return;
    }
    sessionStorage.setItem('csrf', body.csrf_token || '');
    window.location.href = '/';
  } catch (e) {
    err.textContent = 'Could not reach the server.';
  }
});
