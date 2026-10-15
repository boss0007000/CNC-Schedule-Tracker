// Minimal progressive enhancement: prevent double-submits, load slots, auto-refresh.
document.addEventListener('submit', function (e) {
  var f = e.target;
  if (f.dataset.busy) { e.preventDefault(); return; }
  f.dataset.busy = '1';
  f.querySelectorAll('button[type=submit],button:not([type])').forEach(function (b) {
    b.disabled = true; b.dataset.label = b.textContent; b.textContent = 'Working…';
  });
  setTimeout(function () {  // allow retry on a bad connection
    delete f.dataset.busy;
    f.querySelectorAll('button[disabled]').forEach(function (b) { b.disabled = false; b.textContent = b.dataset.label || b.textContent; });
  }, 15000);
});
document.querySelectorAll('[data-refresh]').forEach(function (el) {
  setTimeout(function () { location.reload(); }, parseInt(el.dataset.refresh, 10) * 1000);
});
document.querySelectorAll('[data-confirm]').forEach(function (el) {
  el.addEventListener('submit', function (e) { if (!confirm(el.dataset.confirm)) { e.preventDefault(); delete el.dataset.busy; } });
});

document.addEventListener('click', function (e) { if (e.target.closest && e.target.closest('[data-print]')) window.print(); });
