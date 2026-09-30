/* BusyPanel — in-place updates.
 *
 * Progressive enhancement only. The server is unchanged: every page works with
 * this file absent, as a plain form POST followed by a 303 redirect. With it:
 *
 *   - [data-region] parts of the page are swapped from the response instead of
 *     reloading the document,
 *   - [data-dialog] openers show a <dialog> modally (without JS the <noscript>
 *     style in base.html renders every dialog as an ordinary inline panel); an
 *     opener that also carries data-swap fetches its URL, swaps the regions,
 *     then opens the panel, which is how a row's editor is loaded,
 *   - [data-flash] messages become toasts,
 *   - GET filter forms and [data-swap] links update the URL with pushState,
 *   - [data-autosubmit] controls submit their form on change,
 *   - [data-confirm] asks before a destructive submit.
 *
 * No build step and no framework: a plain deferred script. Every listener is
 * delegated on document, because a region swap replaces the nodes a direct
 * listener would have been bound to.
 */
(() => {
  'use strict';

  const TOAST_MS = { ok: 3500, error: 8000 };

  function pushToast(text, kind) {
    const box = document.getElementById('toasts');
    if (!box || !text) return;
    const el = document.createElement('div');
    el.className = `toast toast-${kind}`;
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.textContent = text;
    el.addEventListener('click', () => el.remove());
    box.appendChild(el);
    setTimeout(() => el.remove(), TOAST_MS[kind] || TOAST_MS.ok);
  }

  /* Server-rendered flash lines are promoted to toasts and taken out of the
     document, so a message never renders twice. */
  function harvestFlash(root) {
    let sawError = false;
    for (const el of root.querySelectorAll('[data-flash]')) {
      const kind = el.classList.contains('error') ? 'error' : 'ok';
      if (kind === 'error') sawError = true;
      const text = el.textContent.trim();
      el.remove();
      pushToast(text, kind);
    }
    return sawError;
  }

  function regions(root) {
    const map = new Map();
    for (const el of root.querySelectorAll('[data-region]')) {
      map.set(el.dataset.region, el);
    }
    return map;
  }

  function samePage(finalUrl) {
    try {
      return new URL(finalUrl, location.href).pathname === location.pathname;
    } catch {
      return false;
    }
  }

  /* Apply a full page response to the live document.
   *
   * Whether to swap is decided by region coverage, not by the URL alone,
   * because the two disagree on this app:
   *
   *   - POST /invoices/monthly 303s to /invoices/{id}: a different page whose
   *     regions do not exist here, so it must navigate;
   *   - POST /login 303s to /, which shares only the #flash region: a different
   *     page, so a successful response always navigates rather than stranding
   *     the user on the login page;
   *   - POST /settings 303s to /settings?saved=1, and every ?edit= dialog fetch
   *     answers on the same path: the same page and the same regions, so it
   *     swaps and the "Saved." line arrives as a toast.
   *
   * Every [data-region] is rendered on every response, even a closed dialog, so
   * a region that appears and disappears between responses can never leave
   * stale markup behind.
   *
   * Returns null when navigation was triggered instead. */
  function apply(response, html) {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const finalUrl = response.url || location.href;
    const live = regions(document);
    const incoming = regions(doc);
    const coversAll = live.size > 0
      && [...live.keys()].every((id) => incoming.has(id));

    if (!samePage(finalUrl) && !(response.status >= 400 && coversAll)) {
      location.assign(finalUrl);
      return null;
    }

    for (const [id, node] of incoming) {
      const target = live.get(id);
      if (target) target.replaceWith(node);
    }
    return { finalUrl, hadError: harvestFlash(document.body) };
  }

  /* Fetch a URL and fold the response into the page. Resolves null when the
     response sent us to another page; throws if the request never completed.
     `record` is false when the caller is itself reconstructing history (a
     popstate), where pushing another entry would corrupt the back button. */
  async function load(url, init, record = true) {
    const response = await fetch(url, init
      || { redirect: 'follow', credentials: 'same-origin' });
    const result = apply(response, await response.text());
    if (!result) return null;
    if (record) recordUrl(result.finalUrl);
    return result;
  }

  /* Put a URL in the history only when it actually differs. A same-path
     response is the filter case (?status=paid); a cross-path response that was
     swapped in (a 400 re-render) leaves the address bar alone. */
  function recordUrl(finalUrl) {
    let target;
    try {
      const u = new URL(finalUrl);
      if (u.pathname !== location.pathname) return;
      target = u.pathname + u.search;
    } catch {
      return;
    }
    if (target !== location.pathname + location.search) {
      history.pushState(null, '', target);
    }
  }

  /* Show a dialog modally. A dialog the server rendered with an `open` attribute
     (the with-JavaScript-off inline form, and what `?edit=<id>` answers) is
     already open in the non-modal sense, and showModal() on one throws. Close it
     first: the modal presentation is the point of the opener. */
  function showModal(dialog) {
    if (!dialog) return;
    if (dialog.open) dialog.close();
    dialog.showModal();
  }

  /* A dialog whose contents are server-rendered: fetch the opener's URL, swap
     the regions, then open the panel. `record = false` so a dialog selection
     never becomes a history entry (Back would otherwise reopen a dialog). */
  async function loadDialog(url, dialogId) {
    let result;
    try {
      result = await load(url, undefined, false);
    } catch {
      location.assign(url.href);
      return;
    }
    if (result === null) return;          // load() already navigated
    // The swap replaced the dialog, so re-find it by id: the live node.
    showModal(document.getElementById(dialogId));
  }

  function setBusy(form, button, busy) {
    if (busy) form.setAttribute('aria-busy', 'true');
    else form.removeAttribute('aria-busy');
    if (button) button.disabled = busy;
  }

  /* Submit through the prototype so this neither re-fires the submit event nor
     is shadowed by a field named "submit". */
  function plainSubmit(form) {
    HTMLFormElement.prototype.submit.call(form);
  }

  function submitterOf(form, event) {
    return event.submitter
      || form.querySelector('button[type="submit"], button:not([type])');
  }

  function queryString(data) {
    // Empty control values are dropped. An empty string is not a meaningful
    // filter here, and ?client= / ?year= are typed int|None on the server,
    // where they are a 422 rather than "no filter".
    const params = new URLSearchParams();
    for (const [key, value] of data) {
      if (typeof value === 'string' && value === '') continue;
      params.append(key, value);
    }
    return params.toString();
  }

  async function send(form, event) {
    const submitter = submitterOf(form, event);
    const method = (form.getAttribute('method') || 'get').toUpperCase();
    const url = new URL(form.getAttribute('action') || location.href, location.href);
    const data = new FormData(form);
    if (submitter && submitter.name) data.append(submitter.name, submitter.value);

    const init = { method, redirect: 'follow', credentials: 'same-origin' };
    if (method === 'GET') url.search = queryString(data);
    else init.body = data;

    // Where the dialog was, if this form lives in one: the swap replaces it, so
    // a rejected submit has to reopen the new one or the input would vanish
    // behind a closed panel.
    const dialogId = form.closest('dialog') ? form.closest('dialog').id : '';

    setBusy(form, submitter, true);
    let result;
    try {
      result = await load(url, init);
    } catch {
      // Offline or a dead socket: hand the form to the browser rather than
      // leaving the user with a button that does nothing.
      setBusy(form, submitter, false);
      plainSubmit(form);
      return;
    }
    setBusy(form, submitter, false);
    if (result === null) return;
    // `form` may have been replaced by the swap, so the dialog is re-found by
    // id: the live node, not the detached one the submit came from.
    const dialog = dialogId ? document.getElementById(dialogId) : null;
    if (result.hadError) {
      // The error itself is the toast; reopen the dialog so the fields are
      // still there to correct. Its values came back in the response, and its
      // `open` attribute (if the server rendered one) is normalized by the
      // helper so the panel is modal, not inline.
      showModal(dialog);
      return;
    }

    if (dialog) {
      // A creation form: once it succeeds the panel has done its job.
      dialog.close();
      const liveForm = dialog.querySelector('form');
      if (liveForm) liveForm.reset();
    } else if (form.dataset.toast) {
      form.reset();
    }
    const toast = (submitter && submitter.dataset.toast) || form.dataset.toast;
    if (toast) pushToast(toast, 'ok');
  }

  /* A swapped-in link. Any failure falls back to a real navigation, so a
     [data-swap] anchor can never be a dead control. */
  async function loadLink(url) {
    try {
      if (await load(url) === null) location.assign(url.href);
    } catch {
      location.assign(url.href);
    }
  }

  /* Back/forward over a pushed filter: re-fetch and swap the current URL. */
  async function loadHistory() {
    try {
      if (await load(location.href, undefined, false) === null) location.reload();
    } catch {
      location.reload();
    }
  }

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (event.defaultPrevented) return;
    if (form.hasAttribute('data-plain')) return;
    const msg = form.dataset.confirm;
    if (msg && !window.confirm(msg)) { event.preventDefault(); return; }
    event.preventDefault();
    send(form, event);
  });

  document.addEventListener('click', (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;

    // A dialog opener with server-rendered contents: fetch its URL, swap the
    // regions, then show the panel. Tested before [data-swap] -- a
    // `<a data-swap data-dialog>` would otherwise fetch without ever opening.
    const opener = target.closest('[data-dialog]');
    if (opener && !event.defaultPrevented && !event.metaKey && !event.ctrlKey
        && !event.shiftKey && !event.altKey && opener.target !== '_blank') {
      const dialogId = opener.dataset.dialog;
      event.preventDefault();
      if (opener.matches('a[data-swap]')) {
        loadDialog(new URL(opener.href, location.href), dialogId);
      } else {
        showModal(document.getElementById(dialogId));
      }
      return;
    }

    // A link marked data-swap (the invoice status chips): fetch and swap rather
    // than loading the page, then record the URL so back/forward still work.
    const link = target.closest('a[data-swap]');
    if (link && !event.defaultPrevented && !event.metaKey && !event.ctrlKey
        && !event.shiftKey && !event.altKey && link.target !== '_blank') {
      const url = new URL(link.href, location.href);
      if (url.origin === location.origin) {
        event.preventDefault();
        loadLink(url);
        return;
      }
    }

    const closer = target.closest('[data-close]');
    if (closer) {
      const dialog = closer.closest('dialog');
      if (dialog) {
        event.preventDefault();
        dialog.close();
        return;
      }
    }

    // Backdrop click. The event target is the dialog element for padding clicks
    // too, so the test is geometric rather than target-based.
    const dialog = target.closest('dialog[open]');
    if (dialog) {
      const r = dialog.getBoundingClientRect();
      const outside = event.clientX < r.left || event.clientX > r.right
        || event.clientY < r.top || event.clientY > r.bottom;
      if (outside) dialog.close();
    }
  });

  document.addEventListener('change', (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (!target.matches('[data-autosubmit]')) return;
    const form = target.form || target.closest('form');
    if (form) form.requestSubmit();
  });

  window.addEventListener('popstate', () => {
    loadHistory();
  });

  // Deferred script: the document is parsed, so a server-rendered message can
  // be promoted immediately.
  harvestFlash(document.body);
})();
