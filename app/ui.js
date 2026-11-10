'use strict';
/* 自绘控件：选择器 / 日期 / 确认框 / toast
   不用原生 select、原生 date、window.confirm：三者在各浏览器里长得都不一样。 */

const UI = (() => {
  const WEEK = ['一', '二', '三', '四', '五', '六', '日'];
  const pad = (n) => (n < 10 ? '0' + n : '' + n);
  const iso = (d) => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  const CHEV = '<svg class="pick__chev" viewBox="0 0 20 20" aria-hidden="true"><path d="M5 7.5 10 12.5 15 7.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  /* ---------------------------------------------------------- 选择器 */
  function picker({ btn, pop, getItems, onPick, valueEl }) {
    let items = [];
    let active = -1;

    function close() {
      pop.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
      document.removeEventListener('click', outside, true);
    }
    function outside(e) {
      if (!pop.contains(e.target) && !btn.contains(e.target)) close();
    }
    function open() {
      items = getItems() || [];
      pop.innerHTML = '';
      let lastGroup = null;
      items.forEach((it, i) => {
        if (it.group && it.group !== lastGroup) {
          lastGroup = it.group;
          const gh = el('li', 'pick__group', it.group);
          gh.setAttribute('role', 'presentation');
          pop.appendChild(gh);
        }
        const li = el('li', 'pick__opt');
        li.id = pop.id + '-o' + i;
        li.setAttribute('role', 'option');
        li.setAttribute('aria-selected', it.selected ? 'true' : 'false');
        li.textContent = it.label;
        li.dataset.i = String(i);
        li.addEventListener('click', () => { close(); onPick(it); btn.focus(); });
        pop.appendChild(li);
      });
      active = Math.max(0, items.findIndex((i) => i.selected));
      pop.hidden = false;
      btn.setAttribute('aria-expanded', 'true');
      paint();
      setTimeout(() => document.addEventListener('click', outside, true), 0);
    }
    function paint() {
      [...pop.querySelectorAll('.pick__opt')].forEach((li) => li.classList.toggle('is-active', li.dataset.i === String(active)));
      const cur = pop.querySelector('.pick__opt[data-i="' + active + '"]');
      if (cur) {
        cur.scrollIntoView({ block: 'nearest' });
        pop.setAttribute('aria-activedescendant', cur.id);
      }
    }

    btn.addEventListener('click', () => (pop.hidden ? open() : close()));
    btn.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        if (pop.hidden) open();
      }
      if (e.key === 'Escape') close();
    });
    pop.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); active = Math.min(items.length - 1, active + 1); paint(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(0, active - 1); paint(); }
      else if (e.key === 'Home') { e.preventDefault(); active = 0; paint(); }
      else if (e.key === 'End') { e.preventDefault(); active = items.length - 1; paint(); }
      else if (e.key === 'Enter') { e.preventDefault(); if (items[active]) { close(); onPick(items[active]); btn.focus(); } }
      else if (e.key === 'Escape') { close(); btn.focus(); }
      else if (e.key.length === 1) {
        const hit = items.findIndex((it) => it.label.toLowerCase().startsWith(e.key.toLowerCase()));
        if (hit >= 0) { active = hit; paint(); }
      }
    });

    return {
      set(text) { if (valueEl) valueEl.textContent = text; },
      close,
    };
  }

  /* ---------------------------------------------------------- 日期 */
  function datePicker({ btn, pop, valueEl, value, onChange }) {
    let cur = value ? new Date(value + 'T00:00:00') : new Date();
    cur.setDate(1);
    let sel = value || '';

    function fmtLabel(v) {
      if (!v) return '选择日期';
      const d = new Date(v + 'T00:00:00');
      return v + ' 周' + WEEK[(d.getDay() + 6) % 7];
    }

    function close() {
      pop.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
      document.removeEventListener('click', outside, true);
    }
    function outside(e) {
      if (!pop.contains(e.target) && !btn.contains(e.target)) close();
    }

    function render() {
      pop.innerHTML = '';
      const head = el('div', 'dp__head');
      const prev = el('button', 'dp__nav');
      prev.type = 'button';
      prev.setAttribute('aria-label', '上个月');
      prev.innerHTML = '<svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><path d="M12 5 7 10l5 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      prev.addEventListener('click', () => { cur.setMonth(cur.getMonth() - 1); render(); });
      const next = el('button', 'dp__nav');
      next.type = 'button';
      next.setAttribute('aria-label', '下个月');
      next.innerHTML = '<svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><path d="M8 5l5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      next.addEventListener('click', () => { cur.setMonth(cur.getMonth() + 1); render(); });
      head.appendChild(prev);
      head.appendChild(el('span', 'dp__title', cur.getFullYear() + ' 年 ' + (cur.getMonth() + 1) + ' 月'));
      head.appendChild(next);
      pop.appendChild(head);

      const grid = el('div', 'dp__grid');
      WEEK.forEach((w) => grid.appendChild(el('span', 'dp__wd', w)));

      const first = new Date(cur.getFullYear(), cur.getMonth(), 1);
      const offset = (first.getDay() + 6) % 7;
      const days = new Date(cur.getFullYear(), cur.getMonth() + 1, 0).getDate();
      const todayIso = iso(new Date());

      for (let i = 0; i < offset; i++) grid.appendChild(el('span', 'dp__day', ''));

      for (let d = 1; d <= days; d++) {
        const dt = new Date(cur.getFullYear(), cur.getMonth(), d);
        const v = iso(dt);
        const b = el('button', 'dp__day', String(d));
        b.type = 'button';
        b.dataset.iso = v;
        if (v === sel) b.setAttribute('aria-selected', 'true');
        if (v === todayIso) b.dataset.today = 'true';
        b.addEventListener('click', () => {
          sel = v;
          valueEl.textContent = fmtLabel(v);
          close();
          onChange(v);
          btn.focus();
        });
        grid.appendChild(b);
      }
      pop.appendChild(grid);

      const foot = el('div', 'dp__foot');
      const t = el('button', 'dp__today', '回到今天');
      t.type = 'button';
      t.addEventListener('click', () => {
        sel = todayIso;
        valueEl.textContent = fmtLabel(sel);
        close();
        onChange(sel);
        btn.focus();
      });
      foot.appendChild(t);
      pop.appendChild(foot);
    }

    function open() {
      if (sel) { cur = new Date(sel + 'T00:00:00'); cur.setDate(1); }
      render();
      pop.hidden = false;
      btn.setAttribute('aria-expanded', 'true');
      setTimeout(() => document.addEventListener('click', outside, true), 0);
      const focusDay = pop.querySelector('[aria-selected="true"]') || pop.querySelector('.dp__day[data-iso]');
      if (focusDay) focusDay.focus();
    }

    btn.addEventListener('click', () => (pop.hidden ? open() : close()));
    btn.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (pop.hidden) open(); }
      if (e.key === 'Escape') close();
    });
    pop.addEventListener('keydown', (e) => {
      const days = [...pop.querySelectorAll('.dp__day[data-iso]')];
      const i = days.indexOf(document.activeElement);
      if (e.key === 'Escape') { close(); btn.focus(); return; }
      if (i < 0) return;
      if (e.key === 'ArrowRight') { e.preventDefault(); (days[i + 1] || days[i]).focus(); }
      if (e.key === 'ArrowLeft') { e.preventDefault(); (days[i - 1] || days[i]).focus(); }
      if (e.key === 'ArrowDown') { e.preventDefault(); (days[i + 7] || days[i]).focus(); }
      if (e.key === 'ArrowUp') { e.preventDefault(); (days[i - 7] || days[i]).focus(); }
      if (e.key === 'Enter') { e.preventDefault(); document.activeElement.click(); }
    });

    if (valueEl) valueEl.textContent = fmtLabel(value);
    return { set(v) { sel = v; valueEl.textContent = fmtLabel(v); } };
  }

  /* ---------------------------------------------------------- 确认框 */
  function confirm({ title, body, okText = '确定', cancelText = '取消', danger = false }) {
    return new Promise((resolve) => {
      const scrim = el('div', 'scrim');
      const dlg = el('div', 'dlg');
      dlg.setAttribute('role', 'dialog');
      dlg.setAttribute('aria-modal', 'true');
      dlg.setAttribute('aria-labelledby', 'dlgT');

      const t = el('h2', 'dlg__t', title);
      t.id = 'dlgT';
      dlg.appendChild(t);
      if (body) dlg.appendChild(el('p', 'dlg__d', body));

      const acts = el('div', 'dlg__acts');
      const cancel = el('button', 'btn', cancelText);
      cancel.type = 'button';
      const ok = el('button', 'btn ' + (danger ? 'btn--danger' : 'btn--primary'), okText);
      ok.type = 'button';
      acts.appendChild(cancel);
      acts.appendChild(ok);
      dlg.appendChild(acts);
      scrim.appendChild(dlg);
      document.body.appendChild(scrim);
      ok.focus();

      function done(v) {
        document.removeEventListener('keydown', onKey);
        scrim.remove();
        resolve(v);
      }
      function onKey(e) {
        if (e.key === 'Escape') done(false);
        if (e.key === 'Tab') {
          const f = [cancel, ok];
          const i = f.indexOf(document.activeElement);
          e.preventDefault();
          f[(i + (e.shiftKey ? f.length - 1 : 1)) % f.length].focus();
        }
      }
      cancel.addEventListener('click', () => done(false));
      ok.addEventListener('click', () => done(true));
      scrim.addEventListener('click', (e) => { if (e.target === scrim) done(false); });
      document.addEventListener('keydown', onKey);
    });
  }

  /* ---------------------------------------------------------- toast */
  let timer = null;
  function toast(msg, tone) {
    let t = document.getElementById('toast');
    if (!t) {
      t = el('div', 'toast');
      t.id = 'toast';
      t.setAttribute('role', 'status');
      t.setAttribute('aria-live', 'polite');
      document.body.appendChild(t);
    }
    t.textContent = msg;
    if (tone) t.dataset.tone = tone; else delete t.dataset.tone;
    t.hidden = false;
    clearTimeout(timer);
    timer = setTimeout(() => { t.hidden = true; }, 4000);
  }

  return { picker, datePicker, confirm, toast, el, iso, CHEV };
})();
