/* Busy state for anything that takes a moment.

   Some things this site does are not instant: Pillow re-encodes every
   photograph on the way in, a test email waits on somebody else's mail
   server, a backup zips the uploads folder, a Gift Aid export builds a
   CSV. All of them look identical to a button that did nothing, so the
   honest response is to press it again — which is how a double upload
   or a second test email happens.

   HOW A CONTROL OPTS IN: put the message on the form, the link or the
   button.

       <form method="post" data-busy="Sending the test email…">
       <a href="…" data-busy="Building the CSV…" data-busy-restore="8000">

   WITH JAVASCRIPT OFF NOTHING CHANGES. `data-busy` is an attribute
   nothing but this file reads, so every form and link on the site posts
   and navigates exactly as it did before. That is the whole design: this
   adds a state, never a step.

   Three things in here are less obvious than they look, each written
   down because getting one wrong is silent:

     * A SUBMIT BUTTON IS DISABLED ONE TICK LATE, never inside the submit
       handler. The form's entry list is built AFTER the submit event
       finishes, and a disabled control contributes nothing to it — so
       disabling immediately would drop a submit button's own name and
       value from the POST. None carry one today; the next one added
       would break quietly, on a page nobody thought to re-test.
     * A CANCELLED SUBMIT IS LEFT ALONE. Several admin forms carry an
       inline `onsubmit="return confirm(…)"`, and half the point of the
       confirm is that answering No does nothing. Its handler runs first
       and calls preventDefault, so `event.defaultPrevented` is the test
       — without it, saying No left a permanently busy button.
     * A LINK THAT DOWNLOADS NEVER NAVIGATES, so nothing ever tells this
       script the work is done. Those pass `data-busy-restore` with a
       number of milliseconds. An ordinary link restores on `pageshow`
       instead: the back button serves the old page out of the bfcache
       exactly as it was left, dead button and all.

   Vanilla, `var`, no libraries, nothing inline — so the CSP stays as
   tight as it is. */
(function () {
  'use strict';

  var DEFAULT_MESSAGE = 'Working…';

  /* Everything currently held busy, so `pageshow` can put all of it back
     without hunting the document for it. */
  var painted = [];
  var guarded = [];

  function label(el) {
    return el.tagName === 'INPUT' ? el.value : el.innerHTML;
  }

  function relabel(el, text, spinner) {
    if (el.tagName === 'INPUT') {
      el.value = text;                  /* no room for a spinner inside */
      return;
    }
    el.textContent = text;
    if (spinner) {
      var dot = document.createElement('span');
      dot.className = 'busy-spin';
      /* Decoration. The message beside it carries the meaning, and the
         message is what a screen reader reads. */
      dot.setAttribute('aria-hidden', 'true');
      el.insertBefore(dot, el.firstChild);
    }
  }

  /* The thing to paint: the element carrying the attribute when that is
     itself a control, otherwise whatever submitted the form. */
  function controlFor(el, submitter) {
    if (el.tagName === 'A' || el.tagName === 'BUTTON'
        || el.tagName === 'INPUT') {
      return el;
    }
    return submitter
        || el.querySelector('button[type=submit], input[type=submit]')
        || el.querySelector('button:not([type=button])');
  }

  function start(control, message) {
    if (!control || control.busyOn) { return false; }
    control.busyOn = true;
    control.busyWas = label(control);
    control.setAttribute('aria-busy', 'true');
    control.classList.add('is-busy');
    relabel(control, message || DEFAULT_MESSAGE, true);
    /* One tick late, deliberately — see the note at the top. `disabled`
       means nothing on an <a>, so a link keeps the class and the guard
       in the click handler is what stops a second press. */
    window.setTimeout(function () {
      if (control.busyOn && control.tagName !== 'A') {
        control.disabled = true;
      }
    }, 0);
    painted.push(control);
    return true;
  }

  function stop(control) {
    if (!control || !control.busyOn) { return; }
    control.busyOn = false;
    if (control.tagName !== 'A') { control.disabled = false; }
    control.removeAttribute('aria-busy');
    if (control.busyWas !== undefined) {
      if (control.tagName === 'INPUT') {
        control.value = control.busyWas;
      } else {
        control.innerHTML = control.busyWas;
      }
    }
    control.classList.remove('is-busy');
    var at = painted.indexOf(control);
    if (at > -1) { painted.splice(at, 1); }
  }

  /* Restore everything. The back button hands the page back out of the
     bfcache in the state it was left — mid-submit, button dead — and
     without this the only way out of that is a reload. */
  window.addEventListener('pageshow', function () {
    while (painted.length) { stop(painted[0]); }
    while (guarded.length) { guarded.pop().busyForm = false; }
  });

  /* Delegated, so a control added to the page later works too, and so
     this cannot race an inline script that decides to handle a form
     itself. Bubble phase on purpose: the form's own listeners — the
     confirm() attributes, the gallery's progress script — have all had
     their say by the time this runs, and `defaultPrevented` says so. */
  document.addEventListener('submit', function (e) {
    var form = e.target.closest ? e.target.closest('form[data-busy]') : null;
    if (!form) { return; }
    if (form.busyForm) {
      /* Double submit. The first is still in flight, and a second would
         upload the same photographs or send the same email again. */
      e.preventDefault();
      return;
    }
    if (e.defaultPrevented) { return; }
    if (start(controlFor(form, e.submitter), form.getAttribute('data-busy'))) {
      form.busyForm = true;
      guarded.push(form);
    }
  });

  document.addEventListener('click', function (e) {
    var el = e.target.closest
           ? e.target.closest('a[data-busy], button[data-busy]') : null;
    if (!el || e.defaultPrevented) { return; }
    /* A click with a modifier opens a new tab or downloads the target:
       THIS page is not going anywhere, so it must not go busy. */
    if (el.tagName === 'A'
        && (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button)) {
      return;
    }
    if (el.busyOn) { e.preventDefault(); return; }
    /* A submit button inside an opted-in form is that form's business:
       let the submit handler paint it, or a form the browser is about to
       refuse for a missing required field is announced as sending. */
    if (el.tagName === 'BUTTON' && el.form
        && el.type !== 'button' && el.form.hasAttribute('data-busy')) {
      return;
    }
    start(el, el.getAttribute('data-busy'));
    var restore = parseInt(el.getAttribute('data-busy-restore'), 10);
    if (restore > 0) {
      window.setTimeout(function () { stop(el); }, restore);
    }
  });

  /* aria-live goes on at WIRE-UP, not when the control goes busy. A live
     region has to exist before the change it is meant to announce; set
     at the same moment as the new text, the announcement is a coin toss
     across screen readers. */
  var opted = document.querySelectorAll(
      'form[data-busy], a[data-busy], button[data-busy]');
  for (var i = 0; i < opted.length; i++) {
    var control = controlFor(opted[i], null);
    if (control && !control.hasAttribute('aria-live')) {
      control.setAttribute('aria-live', 'polite');
    }
  }

  /* For a page driving its own long job — the gallery's per-file upload
     — so there is ONE implementation of "this control is busy" rather
     than two that drift apart. */
  window.ebwaBusy = {start: start, stop: stop};
})();
