(() => {
  const menu = document.querySelector('.menu-toggle');
  const navigation = document.getElementById('site-navigation');
  if (menu && navigation) {
    menu.hidden = false;
    navigation.classList.add('collapsible');
    const closeMenu = () => menu.setAttribute('aria-expanded', 'false');
    menu.addEventListener('click', () => {
      menu.setAttribute('aria-expanded', String(menu.getAttribute('aria-expanded') !== 'true'));
    });
    navigation.addEventListener('click', event => {
      if (event.target.closest('a')) closeMenu();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && menu.getAttribute('aria-expanded') === 'true') {
        closeMenu();
        menu.focus();
      }
    });
    document.addEventListener('click', event => {
      if (!event.target.closest('.site-header')) closeMenu();
    });
  }

  const bar = document.querySelector('[data-compare-bar]');
  if (bar) {
    const key = `business-models-compare:${bar.dataset.run}`;
    const boxes = [...document.querySelectorAll('[data-compare-id]')];
    let selected = [];
    try { selected = JSON.parse(sessionStorage.getItem(key) || '[]'); } catch (_) {}
    if (!Array.isArray(selected)) selected = [];
    selected = [...new Set(selected.filter(value => typeof value === 'string'))].slice(0, 3);
    const update = () => {
      boxes.forEach(box => { box.checked = selected.includes(box.dataset.compareId); });
      bar.hidden = selected.length === 0;
      bar.querySelector('[data-compare-count]').textContent = String(selected.length);
      const params = new URLSearchParams({ run: bar.dataset.run, revision: bar.dataset.revision, models: selected.join(',') });
      bar.querySelector('[data-compare-link]').href = `/compare?${params}`;
      try { sessionStorage.setItem(key, JSON.stringify(selected)); } catch (_) {}
    };
    boxes.forEach(box => box.addEventListener('change', () => {
      const message = bar.querySelector('[data-compare-message]');
      message.textContent = '';
      if (box.checked && !selected.includes(box.dataset.compareId)) {
        if (selected.length === 3) { box.checked = false; message.textContent = 'Compare up to three models at a time.'; return; }
        selected.push(box.dataset.compareId);
      } else selected = selected.filter(id => id !== box.dataset.compareId);
      update();
    }));
    bar.querySelector('[data-clear-compare]').addEventListener('click', () => { selected = []; update(); });
    update();
  }
  document.querySelectorAll('a[href^="#claim-"]').forEach(link => link.addEventListener('click', () => {
    const target = document.getElementById(link.getAttribute('href').slice(1));
    if (target && target.tagName === 'DETAILS') target.open = true;
  }));
  if (location.hash.startsWith('#claim-')) {
    const claim = document.getElementById(location.hash.slice(1));
    if (claim && claim.tagName === 'DETAILS') claim.open = true;
  }
  const progress = document.querySelector('[data-progress-active="true"]');
  if (progress) {
    const timer = setInterval(async () => {
      if (document.hidden || document.activeElement?.matches('input,select')) return;
      try {
        const response = await fetch(`/runs/${encodeURIComponent(progress.dataset.progressRun)}?format=json`, { cache: 'no-store' });
        if (!response.ok) return;
        const snapshot = await response.json();
        if (snapshot.revision_id !== progress.dataset.progressRevision) location.reload();
        if (snapshot.status !== 'running') clearInterval(timer);
      } catch (_) { /* Keep the last published snapshot visible. */ }
    }, 10000);
  }
})();
