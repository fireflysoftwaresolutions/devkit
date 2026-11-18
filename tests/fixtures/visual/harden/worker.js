// The hardening fixture's worker: it proves it ran, follows a redirect to an avoided path, opens a WebSocket, and
// reports the policy directive that refused the socket, which the test reads from the server's log.
self.addEventListener('securitypolicyviolation', (event) => {
  fetch(`/worker-refused?${encodeURIComponent(event.effectiveDirective)}`).catch(() => {});
});
fetch('/worker-ran').catch(() => {});
fetch('/hop-worker').catch(() => {});
try {
  new WebSocket(`ws://${location.host}/ws-worker`);
} catch (_) {
  // a policy that refuses the socket may throw here; the server's log is what the test reads
}
