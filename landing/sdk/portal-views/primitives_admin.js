(function() {
  'use strict';

  const t = (key, opts) => {
    if (window.i18next && typeof window.i18next.t === 'function') {
      return window.i18next.t('portal-sdk:' + key, opts);
    }
    return key;
  };

  const TABS = ['items', 'achievements', 'quests', 'tiers', 'events'];

  const PrimitivesAdminView = {
    async render() {
      const root = document.getElementById('view-primitives');
      if (!root) return;
      root.innerHTML = `
        <h2 class="page-title">${t('primitives.title')}</h2>
        <p class="page-subtitle">${t('primitives.subtitle')}</p>
        <div class="tabs" id="primitives-tabs">
          <button class="tab active" data-tab="items">🎁 ${t('primitives.tab.items')}</button>
          <button class="tab" data-tab="achievements">🏅 ${t('primitives.tab.achievements')}</button>
          <button class="tab" data-tab="quests">📜 ${t('primitives.tab.quests')}</button>
          <button class="tab" data-tab="tiers">👑 ${t('primitives.tab.tiers')}</button>
          <button class="tab" data-tab="events">📅 ${t('primitives.tab.events')}</button>
        </div>
        ${TABS.map((tab, i) => `
          <div id="prim-${tab}" class="prim-pane" style="display:${i === 0 ? 'block' : 'none'}"></div>
        `).join('')}
      `;
      root.querySelectorAll('#primitives-tabs .tab').forEach(btn => {
        btn.addEventListener('click', () => {
          root.querySelectorAll('#primitives-tabs .tab').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          TABS.forEach(tab => {
            const pane = document.getElementById(`prim-${tab}`);
            if (pane) pane.style.display = tab === btn.dataset.tab ? 'block' : 'none';
          });
          this._loadTab(btn.dataset.tab);
        });
      });
      this._loadTab('items');
    },

    _loadTab(tab) {
      ({
        items: () => this._renderItems(),
        achievements: () => this._renderAchievements(),
        quests: () => this._renderQuests(),
        tiers: () => this._renderTiers(),
        events: () => this._renderEvents(),
      })[tab]?.();
    },

    // ════════════════════════════════════════════════════════════
    // ITEMS
    // ════════════════════════════════════════════════════════════
    async _renderItems() {
      const pane = document.getElementById('prim-items');
      if (!pane) return;
      pane.innerHTML = `
        <button class="btn-primary" onclick="PrimitivesAdminView._newItem()">${t('primitives.items.new-button')}</button>
        <div id="items-list" class="prim-grid"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/items`);
        if (!res.ok) {
          document.getElementById('items-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
          return;
        }
        const items = await res.json();
        document.getElementById('items-list').innerHTML = items.length ? items.map(it => `
          <div class="prim-card">
            <div class="prim-icon">${esc(it.icon || '📦')}</div>
            <h4>${esc(it.name || it.id)}</h4>
            <div class="prim-meta">
              <span class="pill-${esc(it.rarity || 'common')}">${esc(it.rarity || 'common')}</span>
              ${it.stackable ? `<span>${t('primitives.items.stackable', {max: it.max_stack || 999})}</span>` : `<span>${t('primitives.items.unique')}</span>`}
            </div>
            <code>${esc(it.id)}</code>
          </div>
        `).join('') : `<p class="empty">${t('primitives.items.none')}</p>`;
      } catch (e) {
        document.getElementById('items-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
      }
    },

    async _newItem() {
      const id = prompt(t('primitives.items.id-prompt'));
      if (!id) return;
      const name = prompt(t('primitives.items.name-prompt'));
      if (!name) return;
      const rarity = prompt(t('primitives.items.rarity-prompt'), 'common') || 'common';
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/items`, {
          method: 'POST',
          json: { id, name, icon: '🎁', rarity, stackable: true, max_stack: 999 }
        });
        if (res.ok) {
          showToast(t('common.created'));
          this._renderItems();
        } else {
          showToast(t('common.create-failed'), 'error');
        }
      } catch (e) {
        showToast(e.message || t('common.create-failed'), 'error');
      }
    },

    // ════════════════════════════════════════════════════════════
    // ACHIEVEMENTS
    // ════════════════════════════════════════════════════════════
    async _renderAchievements() {
      const pane = document.getElementById('prim-achievements');
      if (!pane) return;
      pane.innerHTML = `
        <button class="btn-primary" onclick="PrimitivesAdminView._newAchievement()">${t('primitives.ach.new-button')}</button>
        <div id="ach-list" class="prim-grid"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/achievements`);
        if (!res.ok) {
          document.getElementById('ach-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
          return;
        }
        const achs = await res.json();
        document.getElementById('ach-list').innerHTML = achs.length ? achs.map(a => `
          <div class="prim-card">
            <h4>${esc(a.name || a.id)}</h4>
            <div class="prim-meta">
              <span>${t('primitives.ach.target-label')}: ${esc(a.target_metric || '')} ≥ ${a.target_value != null ? a.target_value : '?'}</span>
              ${a.xp_reward ? `<span>+${a.xp_reward} XP</span>` : ''}
            </div>
            <code>${esc(a.id)}</code>
          </div>
        `).join('') : `<p class="empty">${t('primitives.ach.none')}</p>`;
      } catch (e) {
        document.getElementById('ach-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
      }
    },

    async _newAchievement() {
      const id = prompt(t('primitives.ach.id-prompt'));
      if (!id) return;
      const name = prompt(t('primitives.ach.name-prompt'));
      if (!name) return;
      const metric = prompt(t('primitives.ach.metric-prompt'), 'games_played') || 'games_played';
      const target = parseInt(prompt(t('primitives.ach.target-prompt'), '10') || '10', 10);
      const xp = parseInt(prompt(t('primitives.ach.xp-prompt'), '100') || '100', 10);
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/achievements`, {
          method: 'POST',
          json: { id, name, target_metric: metric, target_value: target, xp_reward: xp }
        });
        if (res.ok) {
          showToast(t('common.created'));
          this._renderAchievements();
        } else {
          showToast(t('common.create-failed'), 'error');
        }
      } catch (e) {
        showToast(e.message || t('common.create-failed'), 'error');
      }
    },

    // ════════════════════════════════════════════════════════════
    // QUESTS
    // ════════════════════════════════════════════════════════════
    async _renderQuests() {
      const pane = document.getElementById('prim-quests');
      if (!pane) return;
      pane.innerHTML = `
        <button class="btn-primary" onclick="PrimitivesAdminView._newQuest()">${t('primitives.quests.new-button')}</button>
        <div id="quest-list" class="prim-grid"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/quests`);
        if (!res.ok) {
          document.getElementById('quest-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
          return;
        }
        const quests = await res.json();
        document.getElementById('quest-list').innerHTML = quests.length ? quests.map(q => `
          <div class="prim-card">
            <h4>${esc(q.name || q.id)}</h4>
            <p class="prim-meta">${esc(q.description || '')}</p>
            <div class="prim-meta">
              <span>${(q.steps || []).length} ${t('primitives.quests.steps-label')}</span>
              ${q.total_reward && q.total_reward.xp ? `<span>+${q.total_reward.xp} XP</span>` : ''}
            </div>
            <code>${esc(q.id)}</code>
          </div>
        `).join('') : `<p class="empty">${t('primitives.quests.none')}</p>`;
      } catch (e) {
        document.getElementById('quest-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
      }
    },

    async _newQuest() {
      const id = prompt(t('primitives.quests.id-prompt'));
      if (!id) return;
      const name = prompt(t('primitives.quests.name-prompt'));
      if (!name) return;
      const desc = prompt(t('primitives.quests.desc-prompt')) || '';
      const stepsRaw = prompt(t('primitives.quests.steps-prompt'), '3');
      const steps = parseInt(stepsRaw || '3', 10) || 3;
      const reward = parseInt(prompt(t('primitives.quests.xp-prompt'), '500') || '500', 10);
      const stepArr = Array.from({ length: steps }, (_, i) => ({
        action: `step_${i + 1}`, target: 1, reward_xp: 50
      }));
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/quests`, {
          method: 'POST',
          json: { id, name, description: desc, steps: stepArr, total_reward: { xp: reward } }
        });
        if (res.ok) {
          showToast(t('common.created'));
          this._renderQuests();
        } else {
          showToast(t('common.create-failed'), 'error');
        }
      } catch (e) {
        showToast(e.message || t('common.create-failed'), 'error');
      }
    },

    // ════════════════════════════════════════════════════════════
    // TIERS
    // ════════════════════════════════════════════════════════════
    async _renderTiers() {
      const pane = document.getElementById('prim-tiers');
      if (!pane) return;
      pane.innerHTML = `
        <button class="btn-primary" onclick="PrimitivesAdminView._newTier()">${t('primitives.tiers.new-button')}</button>
        <div id="tier-list"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/tiers`);
        if (!res.ok) {
          document.getElementById('tier-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
          return;
        }
        const tiers = await res.json();
        const sorted = [...tiers].sort((a, b) => (a.threshold_xp || 0) - (b.threshold_xp || 0));
        document.getElementById('tier-list').innerHTML = `
          <div class="tier-ladder">
            ${sorted.length ? sorted.map(tier => `
              <div class="tier-row">
                <strong>${esc(tier.name || tier.id)}</strong>
                <span class="tier-threshold">${tier.threshold_xp != null ? tier.threshold_xp : 0} XP</span>
                <span class="tier-perks">${(tier.perks || []).map(p => esc(p)).join(', ') || t('primitives.tiers.no-perks')}</span>
                <code>${esc(tier.id)}</code>
              </div>
            `).join('') : `<p class="empty">${t('primitives.tiers.none')}</p>`}
          </div>
        `;
      } catch (e) {
        document.getElementById('tier-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
      }
    },

    async _newTier() {
      const id = prompt(t('primitives.tiers.id-prompt'));
      if (!id) return;
      const name = prompt(t('primitives.tiers.name-prompt'));
      if (!name) return;
      const threshold = parseInt(prompt(t('primitives.tiers.threshold-prompt'), '5000') || '5000', 10);
      const perksRaw = prompt(t('primitives.tiers.perks-prompt'), t('primitives.tiers.perks-default')) || '';
      const perks = perksRaw.split(',').map(s => s.trim()).filter(Boolean);
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/tiers`, {
          method: 'POST',
          json: { id, name, threshold_xp: threshold, perks }
        });
        if (res.ok) {
          showToast(t('common.created'));
          this._renderTiers();
        } else {
          showToast(t('common.create-failed'), 'error');
        }
      } catch (e) {
        showToast(e.message || t('common.create-failed'), 'error');
      }
    },

    // ════════════════════════════════════════════════════════════
    // EVENTS
    // ════════════════════════════════════════════════════════════
    async _renderEvents() {
      const pane = document.getElementById('prim-events');
      if (!pane) return;
      pane.innerHTML = `
        <button class="btn-primary" onclick="PrimitivesAdminView._newEvent()">${t('primitives.events.new-button')}</button>
        <div id="event-list" class="prim-grid"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/events?active=true`);
        if (!res.ok) {
          document.getElementById('event-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
          return;
        }
        const events = await res.json();
        document.getElementById('event-list').innerHTML = events.length ? events.map(e => `
          <div class="prim-card">
            <h4>${esc(e.name || e.id)}</h4>
            <div class="prim-meta">
              <span>${e.start_at ? new Date(e.start_at).toLocaleString() : '?'} → ${e.end_at ? new Date(e.end_at).toLocaleString() : '?'}</span>
            </div>
            ${e.multipliers ? `<div class="prim-meta">${t('primitives.events.multipliers')}: ${esc(JSON.stringify(e.multipliers))}</div>` : ''}
            <code>${esc(e.id)}</code>
          </div>
        `).join('') : `<p class="empty">${t('primitives.events.none')}</p>`;
      } catch (e) {
        document.getElementById('event-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
      }
    },

    async _newEvent() {
      const id = prompt(t('primitives.events.id-prompt'));
      if (!id) return;
      const name = prompt(t('primitives.events.name-prompt'));
      if (!name) return;
      const days = parseInt(prompt(t('primitives.events.days-prompt'), '7') || '7', 10);
      const mult = parseFloat(prompt(t('primitives.events.mult-prompt'), '2') || '2');
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/events`, {
          method: 'POST',
          json: {
            id, name,
            start_at: new Date().toISOString(),
            end_at: new Date(Date.now() + days * 86400000).toISOString(),
            modules_enabled: [],
            multipliers: { xp: mult },
            reward_pool: {}
          }
        });
        if (res.ok) {
          showToast(t('primitives.events.created'));
          this._renderEvents();
        } else {
          showToast(t('common.create-failed'), 'error');
        }
      } catch (e) {
        showToast(e.message || t('common.create-failed'), 'error');
      }
    }
  };

  window.PrimitivesAdminView = PrimitivesAdminView;
})();
