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

  function card(m, w, shopOpen) {
    var c = el('article', 'card s-' + m.status + ((!shopOpen && m.status === 'available') ? ' closed' : ''));
    c.appendChild(el('div', 'stripe'));
    var b = el('div', 'body'), r = el('div', 'row');
    r.appendChild(el('div', m.user ? 'avatar' : 'avatar empty', m.user ? initials(m.user) : (m.status === 'available' ? '\u2713' : '\u2014')));
    var mid = el('div', 'mid'), nm = el('div', 'name', m.name);
    mid.appendChild(nm);
    var who = el('div', 'who'), det = el('div', 'det');
    function phone(c) { if (c) who.appendChild(el('span', 'ph', (c.kind === 'email' ? '\u2709 ' : '\u260E ') + c.text)); }
    if (m.status === 'on_hold') {
      who.appendChild(el('b', '', m.user)); phone(m.user_contact);
      det.textContent = (m.hold_kind === 'setup' ? 'Setup' : 'Tools') + ' inside \u00b7 until ' + String(m.hold_until).replace(':00', '');
    } else if (m.user && m.status !== 'reserved') {
      who.appendChild(el('b', '', m.user)); phone(m.user_contact);
      det.textContent = 'Since ' + (m.since || '') + (m.open_ended ? ' \u00b7 nobody booked next' : '');
    } else if (m.status === 'available') {
      who.appendChild(el('b', '', shopOpen ? 'Open now' : 'Shop closed')); det.textContent = m.free_label || '';
    } else if (m.status === 'reserved') {
      who.appendChild(el('b', '', m.user ? m.user : 'Held for a booking')); if (m.user) phone(m.user_contact);
      det.textContent = m.user ? (m.reason || '') : (m.next_reservation ? 'Starts ' + m.next_reservation : (m.available_at ? 'Free ~' + m.available_at : ''));
    } else { who.appendChild(el('b', '', m.reason || 'Not available')); }
    mid.appendChild(who);
    if (det.textContent) mid.appendChild(det);
    if (m.next_user && m.status !== 'on_hold') {
      var nx = el('div', 'nx'); nx.appendChild(document.createTextNode('Next ' + (m.next_reservation || '') + ': ')); nx.appendChild(el('b', '', m.next_user));
      if (m.next_contact) nx.appendChild(document.createTextNode((m.next_contact.kind === 'email' ? ' \u2709 ' : ' \u260E ') + m.next_contact.text));
      mid.appendChild(nx);
    }
    r.appendChild(mid);
    var rt = el('div', 'rt');
    rt.appendChild(el('div', 'badge', (!shopOpen && m.status === 'available') ? 'Closed' : m.label));  // text label: colour is never the only signal
    if (m.user && (m.status === 'in_use' || m.status === 'overdue')) {
      var bn = el('div', 'big-num');
      if (m.status === 'overdue') { bn.textContent = '+' + m.overdue_minutes + ' min'; bn.appendChild(el('small', '', 'Overdue by')); }
      else if (m.open_ended) { bn.textContent = fmtMin(m.remaining_minutes); bn.appendChild(el('small', '', 'Open-ended')); }
      else { bn.textContent = m.remaining_minutes + ' min'; bn.appendChild(el('small', '', 'Time left')); }
      rt.appendChild(bn);
    }
    if (m.staff_run) r.appendChild(el('div', 'tag', 'Staff-run'));
    else if (m.user || m.next_reservation || m.status === 'reserved' || m.status === 'on_hold') {  // scan: schedule + contact (sign-in required)
      var q = el('div', 'mqr'), qi = el('img'); qi.src = m.qr; qi.alt = 'QR code: schedule and contact for ' + m.name; qi.title = 'Scan for schedule and contact';
      q.appendChild(qi); r.appendChild(q);
    }
    r.appendChild(rt);
    b.appendChild(r);
    b.appendChild(timeline(m, w));
    c.appendChild(b);
    return c;
  }

  function fmtMin(n) { return n >= 120 ? Math.floor(n / 60) + ' h ' + (n % 60 ? (n % 60) + ' min' : '') : n + ' min'; }
  function weekBlock(wk) {
    var w = $('week'); w.textContent = '';
    var head = el('div', 'wk-head');
    head.appendChild(el('b', '', 'THIS WEEK'));
    head.appendChild(el('span', 'wk-sub', 'open times by machine'));
    var lg = el('span', 'wk-legend');
    [['free', 'Free'], ['booked', 'Booked'], ['inuse', 'In use'], ['hold', 'Do not touch'], ['down', 'Out']].forEach(function (x) {
      var i = el('i', 'sq ' + x[0]); lg.appendChild(i); lg.appendChild(document.createTextNode(x[1] + ' '));
    });
    head.appendChild(lg); w.appendChild(head);
    var grid = el('div', 'wk-grid');
    grid.appendChild(el('div', 'wk-name'));
    wk.days.forEach(function (d) {
      var h = el('div', 'wk-day' + (d.today ? ' today' : '') + (d.closed ? ' closed' : ''));
      h.appendChild(el('span', '', d.today ? 'Today' : d.label)); h.appendChild(el('small', '', String(d.date)));
      grid.appendChild(h);
    });
    wk.machines.forEach(function (m) {
      grid.appendChild(el('div', 'wk-name', m.name));
      m.cells.forEach(function (c) {
        var cell = el('div', 'wk-cell' + (c.closed ? ' closed' : ''));
        if (c.closed) { cell.appendChild(el('span', 'wk-txt', 'Closed')); }
        else {
          var strip = el('div', 'wk-strip');
          c.states.forEach(function (s) { strip.appendChild(el('i', 'seg2 ' + s)); });
          cell.appendChild(strip);
          cell.appendChild(el('span', 'wk-txt', m.staff_run ? 'staff' : (c.free_hours + ' h free')));
        }
        grid.appendChild(cell);
      });
    });
    w.appendChild(grid);
  }

  function chip(cls, n, label) { var c = el('div', 'chip ' + cls); c.appendChild(el('b', '', String(n))); c.appendChild(el('span', '', label)); return c; }

  function render(d) {
    $('shop-name').textContent = d.shop_name;
    var o = $('open-state');
    var hrs = String(d.hours_label).replace(/:00/g, '').replace(' \u2013 ', '\u2013');
    o.textContent = d.shop_open ? 'OPEN · ' + hrs : (d.emergency.active ? 'CLOSED' : 'CLOSED · ' + hrs);
    o.className = 'open ' + (d.shop_open ? 'yes' : 'no');
    $('supervisor').textContent = d.supervisor || (d.supervisor_pending ? 'scheduled, not in yet' : 'none scheduled');
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

    if (d.week) weekBlock(d.week);
    var grid = $('grid'); grid.textContent = '';
    d.machines.forEach(function (m) { grid.appendChild(card(m, d.window, d.shop_open)); });

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
