// The shared worker shared.html starts from the app: it sends a POST, asks for an avoided path directly and through a
// redirect (/shared-hop), and reports that it ran.
fetch('/shared-post', { method: 'POST', body: 'x' }).catch(() => {});
fetch('/items/3/delete').catch(() => {});
fetch('/shared-hop').catch(() => {});
fetch('/shared-ran').catch(() => {});
