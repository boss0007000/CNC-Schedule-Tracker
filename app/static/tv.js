// Live TV board (portrait first). Polls every 5s; shows a banner if polling fails; auto-recovers.
(function () {
  var lastOk = null;
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function hhmm(d) { return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }
  function initials(n) { var p = n.replace('.', '').split(' '); return (p[0][0] + (p[1] ? p[1][0] : '')).toUpperCase(); }

  function clockTick() {
    var d = new Date();
    $('time').textContent = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    $('date').textContent = d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
  }

  function timeline(m, w) {
    var span = w.end - w.start, wrap = el('div');
    var tl = el('div', 'tl');
    tl.setAttribute('role', 'img');
    tl.setAttribute('aria-label', m.name + ' schedule for today');
    function pct(t) { return Math.max(0, Math.min(100, (t - w.start) / span * 100)); }
    m.timeline.forEach(function (s) {
      var d = el('div', 'seg ' + s.k);
      d.style.left = pct(s.s) + '%';
      d.style.width = Math.max(0.4, pct(s.e) - pct(s.s)) + '%';
      tl.appendChild(d);
    });
    if (w.now >= w.start && w.now <= w.end) { var n = el('div', 'now'); n.style.left = pct(w.now) + '%'; tl.appendChild(n); }
    wrap.appendChild(tl);
    var ticks = el('div', 'ticks');
    w.ticks.forEach(function (t) { var x = el('span', 'tick', t.label); x.style.left = pct(t.t) + '%'; ticks.appendChild(x); });
    wrap.appendChild(ticks);
    return wrap;
  }

  function card(m, w) {
    var c = el('article', 'card s-' + m.status);
    c.appendChild(el('div', 'stripe'));
    var b = el('div', 'body'), r = el('div', 'row');
    r.appendChild(el('div', m.user ? 'avatar' : 'avatar empty', m.user ? initials(m.user) : (m.status === 'available' ? '\u2713' : '\u2014')));
    var mid = el('div', 'mid'), nm = el('div', 'name', m.name);
    mid.appendChild(nm);
    var who = el('div', 'who');
    if (m.user) {
      who.appendChild(el('b', '', m.user));
      var t = ' \u00b7 since ' + (m.since || '');
      who.appendChild(el('i', '', t));
    } else if (m.status === 'available') {
      who.appendChild(el('b', '', 'Open now')); who.appendChild(el('i', '', ' \u00b7 ' + (m.free_label || '')));
    } else if (m.status === 'reserved') {
      who.appendChild(el('b', '', 'Held for a booking'));
      who.appendChild(el('i', '', m.next_reservation ? ' \u00b7 starts ' + m.next_reservation : (m.available_at ? ' \u00b7 free ~' + m.available_at : '')));
    } else { who.appendChild(el('b', '', m.reason || 'Not available')); }
    mid.appendChild(who);
    r.appendChild(mid);
    if (m.user) {
      var bn = el('div', 'big-num');
      if (m.status === 'overdue') { bn.textContent = '+' + m.overdue_minutes + ' min'; bn.appendChild(el('small', '', 'Overdue by')); }
      else { bn.textContent = m.remaining_minutes + ' min'; bn.appendChild(el('small', '', 'Time left')); }
      r.appendChild(bn);
    }
    r.appendChild(el('div', 'badge', m.label));  // text label: colour is never the only signal
    b.appendChild(r);
    b.appendChild(timeline(m, w));
    c.appendChild(b);
    return c;
  }

  function chip(cls, n, label) { var c = el('div', 'chip ' + cls); c.appendChild(el('b', '', String(n))); c.appendChild(el('span', '', label)); return c; }

  function render(d) {
    $('shop-name').textContent = d.shop_name;
    var o = $('open-state');
    o.textContent = d.shop_open ? 'OPEN · ' + d.hours_label : (d.emergency.active ? 'CLOSED' : 'CLOSED · ' + d.hours_label);
    o.className = 'open ' + (d.shop_open ? 'yes' : 'no');
    $('supervisor').textContent = d.supervisor || 'none scheduled';
    $('code').textContent = d.code.value;
    $('code-left').textContent = 'changes in ' + Math.max(0, Math.ceil(d.code.seconds_left / 60)) + ' min';
    $('emergency').hidden = !d.emergency.active;
    $('emergency-msg').textContent = d.emergency.message;

    var cnt = { available: 0, busy: 0, booked: 0, down: 0 };
    d.machines.forEach(function (m) {
      if (m.status === 'available') cnt.available++;
      else if (m.status === 'in_use' || m.status === 'overdue') cnt.busy++;
      else if (m.status === 'reserved') cnt.booked++;
      else cnt.down++;
    });
    var s = $('summary'); s.textContent = '';
    s.appendChild(chip('c-free', cnt.available, 'Available'));
    s.appendChild(chip('c-inuse', cnt.busy, 'In use'));
    s.appendChild(chip('c-booked', cnt.booked, 'Reserved'));
    s.appendChild(chip('c-down', cnt.down, 'Unavailable'));

    var grid = $('grid'); grid.textContent = '';
    d.machines.forEach(function (m) { grid.appendChild(card(m, d.window)); });

    var q = $('qrs');
    if (!q.dataset.ready) {
      d.qr.forEach(function (c) {
        var w = el('div', 'qr'), img = el('img'); img.src = c.img; img.alt = 'QR code: ' + c.label;
        var tx = el('div'); tx.appendChild(el('b', '', c.label)); tx.appendChild(el('span', '', c.hint));
        w.appendChild(img); w.appendChild(tx); q.appendChild(w);
      });
      q.dataset.ready = '1';
    }
  }

  function poll() {
    var ctl = new AbortController(), timer = setTimeout(function () { ctl.abort(); }, 4000);
    fetch('/tv/data', { cache: 'no-store', signal: ctl.signal })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) { clearTimeout(timer); lastOk = new Date(); $('conn').hidden = true; render(d); })
      .catch(function () {
        clearTimeout(timer);
        var b = $('conn'); b.hidden = false;
        b.textContent = 'Connection lost, last updated ' + (lastOk ? hhmm(lastOk) : 'never');
      });
  }
  clockTick(); setInterval(clockTick, 1000);
  poll(); setInterval(poll, 5000);
})();
