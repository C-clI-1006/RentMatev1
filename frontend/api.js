// One same-origin client. Credentials are supplied explicitly and never persisted.
export class ApiError extends Error {
  constructor(status, detail) {
    const message = typeof detail === 'string' ? detail : JSON.stringify(detail);
    super(`${status}: ${message}`);
    this.status = status;
    this.retryable = status === 429 || status >= 500;
  }
}
export async function api(path, {token, body, method = 'GET', headers = {}, binary = false} = {}) {
  if (!token) throw new Error('Connect a session first. Operations require a separate scheduler credential.');
  const options = {method, headers: {...headers, Authorization: `Bearer ${token}`}};
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('The server could not be reached. Check the connection before retrying a submission.'); }
  if (!response.ok) {
    const data = await response.json().catch(() => ({detail: response.statusText}));
    throw new ApiError(response.status, data.detail ?? data);
  }
  return binary ? response.blob() : response.json();
}
