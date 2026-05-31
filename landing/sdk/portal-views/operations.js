// ═══════════════════════════════════════════════════════════════════════
// OPERATIONS VIEW — 运营活动
// Group Buy / Tournaments / Limited Drops / FCFS management
// ═══════════════════════════════════════════════════════════════════════
(function() {
  'use strict';

  const t = (key, opts) => {
    if (window.i18next && typeof window.i18next.t === 'function') {
      return window.i18next.t('portal-sdk:' + key, opts);
    }
    return key;
  };

  const OperationsView = {
    async render() {
      const root = document.getElementById('view-operations');
      if (!root) return;

      root.innerHTML = `
        <h2 class="page-title">${t('operations.title')}</h2>
        <p class="page-subtitle">${t('operations.subtitle')}</p>

        <div class="ops-tabs">
          <button class="ops-tab active" data-tab="groupbuy">&#128722; ${t('operations.tab.groupbuy')}</button>
          <button class="ops-tab" data-tab="tournament">&#127942; ${t('operations.tab.tournament')}</button>
          <button class="ops-tab" data-tab="limited">&#9200; ${t('operations.tab.limited')}</button>
          <button class="ops-tab" data-tab="fcfs">&#127873; ${t('operations.tab.fcfs')}</button>
        </div>

        <div id="ops-groupbuy" class="ops-pane"></div>
        <div id="ops-tournament" class="ops-pane" style="display:none"></div>
        <div id="ops-limited" class="ops-pane" style="display:none"></div>
        <div id="ops-fcfs" class="ops-pane" style="display:none"></div>
      `;

      const self = this;
      root.querySelectorAll('.ops-tab').forEach(tab => {
        tab.addEventListener('click', () => {
          root.querySelectorAll('.ops-tab').forEach(b => b.classList.remove('active'));
          tab.classList.add('active');
          ['groupbuy','tournament','limited','fcfs'].forEach(name => {
            const pane = document.getElementById(`ops-${name}`);
            if (pane) pane.style.display = name === tab.dataset.tab ? 'block' : 'none';
          });
          self._loadTab(tab.dataset.tab);
        });
      });

      this._loadTab('groupbuy');
    },

    _loadTab(tab) {
      switch(tab) {
        case 'groupbuy': this._renderGroupBuy(); break;
        case 'tournament': this._renderTournaments(); break;
        case 'limited': this._renderLimitedDrops(); break;
        case 'fcfs': this._renderFCFS(); break;
      }
    },

    // ════════════════════ GROUP BUY ════════════════════
    async _renderGroupBuy() {
      const pane = document.getElementById('ops-groupbuy');
      if (!pane) return;
      pane.innerHTML = `
        <div class="ops-toolbar">
          <button class="btn-primary" onclick="OperationsView._newGroupBuy()">${t('operations.groupbuy.new-button')}</button>
        </div>
        <div id="groupbuy-list"><p class="empty">${t('common.loading')}</p></div>
      `;

      const list = document.getElementById('groupbuy-list');
      let res;
      try {
        res = await apiFetch(`/api/v1/groups/brand/${state.brandId}/active`);
      } catch (e) {
        list.innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
        return;
      }
      if (!res.ok) { list.innerHTML = `<p class="empty">${t('common.load-failed')}</p>`; return; }
      const groups = await res.json();
      const buys = (groups.groups || []).filter(g => g.kind === 'buy');
      if (!buys.length) {
        list.innerHTML = `<p class="empty">${t('operations.groupbuy.none')}</p>`;
        return;
      }
      list.innerHTML = buys.map(g => `
        <div class="ops-card">
          <h4>${esc(g.sku_id || g.group_id)}</h4>
          <div class="ops-stats">
            <span>${t('common.status')}: <strong>${esc(g.status || '?')}</strong></span>
            <span>${t('operations.groupbuy.col-members')}: ${(g.members && g.members.length) || 0}/${g.group_size || '?'}</span>
            <span>${t('operations.groupbuy.col-discount')}: ${g.discount_percent || 0}%</span>
            <span>${t('operations.groupbuy.col-expires')}: ${g.expires_at ? new Date(g.expires_at).toLocaleString() : '—'}</span>
          </div>
        </div>
      `).join('');
    },

    async _newGroupBuy() {
      const sku = prompt(t('operations.groupbuy.sku-prompt')); if (!sku) return;
      const size = parseInt(prompt(t('operations.groupbuy.size-prompt'), '5') || 5);
      const discount = parseInt(prompt(t('operations.groupbuy.discount-prompt'), '50') || 50);
      const hours = parseInt(prompt(t('operations.groupbuy.hours-prompt'), '24') || 24);

      let res;
      try {
        res = await apiFetch('/api/v1/groups/buy/create', {
          method: 'POST',
          json: {
            brand_id: state.brandId,
            sku_id: sku,
            group_size: size,
            discount_percent: discount,
            window_minutes: hours * 60,
            initiator_user_id: 'merchant_init_' + Date.now()
          }
        });
      } catch (e) {
        return showToast(t('common.create-failed'), 'error');
      }
      if (!res.ok) return showToast(t('common.create-failed'), 'error');
      const data = await res.json();
      showToast(t('operations.groupbuy.created', {id: data.group_id}));
      this._renderGroupBuy();
    },

    // ════════════════════ TOURNAMENTS ════════════════════
    async _renderTournaments() {
      const pane = document.getElementById('ops-tournament');
      if (!pane) return;
      pane.innerHTML = `
        <div class="ops-toolbar">
          <button class="btn-primary" onclick="OperationsView._newTourney()">${t('operations.tournament.new-button')}</button>
        </div>
        <p class="ops-hint">${t('operations.tournament.hint')}</p>
        <div id="tourney-list"></div>
      `;

      const ids = JSON.parse(localStorage.getItem(`kix_tourneys_${state.brandId}`) || '[]');
      const listEl = document.getElementById('tourney-list');
      if (!ids.length) {
        listEl.innerHTML = `<p class="empty">${t('operations.tournament.none')}</p>`;
        return;
      }
      const tournaments = await Promise.all(ids.map(async id => {
        try {
          const res = await apiFetch(`/api/v1/modules/tourney/${id}`);
          if (res.ok) {
            const data = await res.json();
            return { ...data, tourney_id: data.tourney_id || id };
          }
        } catch (e) {}
        return { tourney_id: id, name: id, status: t('common.unknown'), _missing: true };
      }));
      listEl.innerHTML = tournaments.map(tn => `
        <div class="ops-card">
          <h4>${esc(tn.name || tn.tourney_id)}</h4>
          <div class="ops-stats">
            <span>ID: <code>${esc(tn.tourney_id)}</code></span>
            <span>${t('common.status')}: <strong>${esc(tn.status || 'active')}</strong></span>
            <span>${t('operations.tournament.col-players')}: ${tn.participants_count || (tn.leaderboard && tn.leaderboard.length) || 0}</span>
            <span>${t('operations.tournament.col-left')}: ${esc(String(tn.time_left || (tn.end ? new Date(tn.end).toLocaleString() : '—')))}</span>
          </div>
          <div class="ops-tools">
            <button class="btn-mini" onclick="OperationsView._settleTourney('${esc(tn.tourney_id)}')">${t('operations.tournament.settle')}</button>
            <button class="btn-mini" onclick="OperationsView._forgetTourney('${esc(tn.tourney_id)}')">${t('operations.tournament.forget')}</button>
          </div>
        </div>
      `).join('');
    },

    async _newTourney() {
      const name = prompt(t('operations.tournament.name-prompt')); if (!name) return;
      const entry = parseInt(prompt(t('operations.tournament.entry-prompt'), '10') || 10);
      const days = parseInt(prompt(t('operations.tournament.duration-prompt'), '7') || 7);

      let res;
      try {
        res = await apiFetch('/api/v1/modules/tourney/create', {
          method: 'POST',
          json: {
            brand_id: state.brandId,
            name,
            start: new Date().toISOString(),
            end: new Date(Date.now() + days * 86400000).toISOString(),
            entry_cost_energy: entry,
            prize_pool: [
              { rank: 1, reward: 'top1' },
              { rank: 2, reward: 'top2' },
              { rank: 3, reward: 'top3' }
            ]
          }
        });
      } catch (e) {
        return showToast(t('common.create-failed'), 'error');
      }
      if (!res.ok) return showToast(t('common.create-failed'), 'error');
      const data = await res.json();
      const ids = JSON.parse(localStorage.getItem(`kix_tourneys_${state.brandId}`) || '[]');
      if (data.tourney_id && !ids.includes(data.tourney_id)) ids.push(data.tourney_id);
      localStorage.setItem(`kix_tourneys_${state.brandId}`, JSON.stringify(ids));
      showToast(t('operations.tournament.created', {id: data.tourney_id || name}));
      this._renderTournaments();
    },

    async _settleTourney(id) {
      if (!confirm(t('operations.tournament.settle-confirm'))) return;
      let res;
      try {
        res = await apiFetch(`/api/v1/modules/tourney/${id}/settle`, { method: 'POST' });
      } catch (e) {
        return showToast(t('operations.tournament.settle-failed'), 'error');
      }
      showToast(res.ok ? t('operations.tournament.settled') : t('operations.tournament.settle-failed'), res.ok ? 'success' : 'error');
      if (res.ok) this._renderTournaments();
    },

    _forgetTourney(id) {
      if (!confirm(t('operations.tournament.forget-confirm'))) return;
      const ids = JSON.parse(localStorage.getItem(`kix_tourneys_${state.brandId}`) || '[]');
      const next = ids.filter(x => x !== id);
      localStorage.setItem(`kix_tourneys_${state.brandId}`, JSON.stringify(next));
      this._renderTournaments();
    },

    // ════════════════════ LIMITED DROPS ════════════════════
    async _renderLimitedDrops() {
      const pane = document.getElementById('ops-limited');
      if (!pane) return;
      pane.innerHTML = `
        <div class="ops-toolbar">
          <button class="btn-primary" onclick="OperationsView._newLimited()">${t('operations.limited.new-button')}</button>
        </div>
        <div id="limited-list"></div>
      `;
      const ids = JSON.parse(localStorage.getItem(`kix_limited_${state.brandId}`) || '[]');
      const listEl = document.getElementById('limited-list');
      if (!ids.length) {
        listEl.innerHTML = `<p class="empty">${t('operations.limited.none')}</p>`;
        return;
      }
      const drops = await Promise.all(ids.map(async id => {
        try {
          const res = await apiFetch(`/api/v1/triggers/limiteddrop/${id}?brand_id=${state.brandId}`);
          if (res.ok) {
            const data = await res.json();
            return { ...data, drop_id: data.drop_id || id };
          }
        } catch (e) {}
        return { drop_id: id, item_id: id, _missing: true };
      }));
      listEl.innerHTML = drops.map(d => {
        const total = d.total_supply || 0;
        const remain = d.supply_remaining != null ? d.supply_remaining : '?';
        const claimed = (total && typeof remain === 'number') ? (total - remain) : null;
        const pct = (total && typeof remain === 'number') ? Math.max(0, Math.min(100, (claimed / total) * 100)) : 0;
        return `
        <div class="ops-card">
          <h4>${esc(d.item_id || d.drop_id)}</h4>
          <div class="ops-stats">
            <span>ID: <code>${esc(d.drop_id)}</code></span>
            <span>${t('operations.limited.col-total')}: ${esc(String(total))}</span>
            <span>${t('operations.limited.col-remaining')}: <strong>${esc(String(remain))}</strong></span>
            <span>${t('operations.limited.col-claimed')}: ${claimed != null ? claimed : '?'}</span>
            <span>${t('operations.limited.col-ends')}: ${d.ends_at ? new Date(d.ends_at).toLocaleString() : t('common.none')}</span>
          </div>
          <div class="ops-progress"><div class="ops-progress-fill" style="width:${pct}%"></div></div>
          <div class="ops-tools">
            <button class="btn-mini" onclick="OperationsView._forgetLimited('${esc(d.drop_id)}')">${t('operations.limited.forget')}</button>
          </div>
        </div>`;
      }).join('');
    },

    async _newLimited() {
      const itemId = prompt(t('operations.limited.item-prompt')); if (!itemId) return;
      const supply = parseInt(prompt(t('operations.limited.supply-prompt'), '100') || 100);
      const days = parseInt(prompt(t('operations.limited.days-prompt'), '7') || 7);
      const dropId = 'drop_' + Date.now();

      let res;
      try {
        res = await apiFetch('/api/v1/triggers/limiteddrop/create', {
          method: 'POST',
          json: {
            brand_id: state.brandId,
            drop_id: dropId,
            item_id: itemId,
            total_supply: supply,
            ends_at: new Date(Date.now() + days * 86400000).toISOString()
          }
        });
      } catch (e) {
        return showToast(t('common.create-failed'), 'error');
      }
      if (!res.ok) return showToast(t('common.create-failed'), 'error');
      const ids = JSON.parse(localStorage.getItem(`kix_limited_${state.brandId}`) || '[]');
      ids.push(dropId);
      localStorage.setItem(`kix_limited_${state.brandId}`, JSON.stringify(ids));
      showToast(t('operations.limited.created'));
      this._renderLimitedDrops();
    },

    _forgetLimited(id) {
      if (!confirm(t('operations.limited.forget-confirm'))) return;
      const ids = JSON.parse(localStorage.getItem(`kix_limited_${state.brandId}`) || '[]');
      localStorage.setItem(`kix_limited_${state.brandId}`, JSON.stringify(ids.filter(x => x !== id)));
      this._renderLimitedDrops();
    },

    // ════════════════════ FCFS POOL ════════════════════
    async _renderFCFS() {
      const pane = document.getElementById('ops-fcfs');
      if (!pane) return;
      pane.innerHTML = `
        <div class="ops-toolbar">
          <button class="btn-primary" onclick="OperationsView._newFCFS()">${t('operations.fcfs.new-button')}</button>
        </div>
        <p class="ops-hint">${t('operations.fcfs.hint')}</p>
        <div id="fcfs-list"></div>
      `;
      const ids = JSON.parse(localStorage.getItem(`kix_fcfs_${state.brandId}`) || '[]');
      const listEl = document.getElementById('fcfs-list');
      if (!ids.length) {
        listEl.innerHTML = `<p class="empty">${t('operations.fcfs.none')}</p>`;
        return;
      }
      listEl.innerHTML = ids.map(id => `
        <div class="ops-card">
          <h4>${t('operations.fcfs.title-prefix')} ${esc(id)}</h4>
          <div class="ops-stats">
            <span>${t('operations.fcfs.endpoint-label')}: <code>POST /api/v1/triggers/fcfs/${esc(id)}/claim</code></span>
          </div>
          <div class="ops-tools">
            <button class="btn-mini" onclick="OperationsView._forgetFCFS('${esc(id)}')">${t('operations.fcfs.forget')}</button>
          </div>
        </div>
      `).join('');
    },

    async _newFCFS() {
      const size = parseInt(prompt(t('operations.fcfs.size-prompt'), '100') || 100);
      const reward = prompt(t('operations.fcfs.reward-prompt'), t('operations.fcfs.reward-default'));
      const days = parseInt(prompt(t('operations.fcfs.valid-days-prompt'), '1') || 1);
      const poolId = 'fcfs_' + Date.now();

      let res;
      try {
        res = await apiFetch('/api/v1/triggers/fcfs/create', {
          method: 'POST',
          json: {
            brand_id: state.brandId,
            pool_id: poolId,
            pool_size: size,
            reward_per_claim: { description: reward, value: 500 },
            expires_at: new Date(Date.now() + days * 86400000).toISOString()
          }
        });
      } catch (e) {
        return showToast(t('common.create-failed'), 'error');
      }
      if (!res.ok) return showToast(t('common.create-failed'), 'error');
      const ids = JSON.parse(localStorage.getItem(`kix_fcfs_${state.brandId}`) || '[]');
      ids.push(poolId);
      localStorage.setItem(`kix_fcfs_${state.brandId}`, JSON.stringify(ids));
      showToast(t('operations.fcfs.created'));
      this._renderFCFS();
    },

    _forgetFCFS(id) {
      if (!confirm(t('operations.fcfs.forget-confirm'))) return;
      const ids = JSON.parse(localStorage.getItem(`kix_fcfs_${state.brandId}`) || '[]');
      localStorage.setItem(`kix_fcfs_${state.brandId}`, JSON.stringify(ids.filter(x => x !== id)));
      this._renderFCFS();
    }
  };

  window.OperationsView = OperationsView;
})();
