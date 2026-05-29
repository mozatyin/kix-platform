(function() {
  'use strict';

  const TABS = ['items', 'achievements', 'quests', 'tiers', 'events'];

  const PrimitivesAdminView = {
    async render() {
      const root = document.getElementById('view-primitives');
      if (!root) return;
      root.innerHTML = `
        <h2 class="page-title">Primitives / 原语管理</h2>
        <p class="page-subtitle">物品、成就、任务、等级、事件 — 详细配置 / Items, Achievements, Quests, Tiers, Events — detailed config</p>
        <div class="tabs" id="primitives-tabs">
          <button class="tab active" data-tab="items">🎁 物品 / Items</button>
          <button class="tab" data-tab="achievements">🏅 成就 / Achievements</button>
          <button class="tab" data-tab="quests">📜 任务 / Quests</button>
          <button class="tab" data-tab="tiers">👑 等级 / Tiers</button>
          <button class="tab" data-tab="events">📅 事件 / Events</button>
        </div>
        ${TABS.map((t, i) => `
          <div id="prim-${t}" class="prim-pane" style="display:${i === 0 ? 'block' : 'none'}"></div>
        `).join('')}
      `;
      root.querySelectorAll('#primitives-tabs .tab').forEach(btn => {
        btn.addEventListener('click', () => {
          root.querySelectorAll('#primitives-tabs .tab').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          TABS.forEach(t => {
            const pane = document.getElementById(`prim-${t}`);
            if (pane) pane.style.display = t === btn.dataset.tab ? 'block' : 'none';
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
    // ITEMS / 物品
    // ════════════════════════════════════════════════════════════
    async _renderItems() {
      const pane = document.getElementById('prim-items');
      if (!pane) return;
      pane.innerHTML = `
        <button class="btn-primary" onclick="PrimitivesAdminView._newItem()">+ 新建物品 / New Item</button>
        <div id="items-list" class="prim-grid"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/items`);
        if (!res.ok) {
          document.getElementById('items-list').innerHTML = '<p class="empty">加载失败 / Load failed</p>';
          return;
        }
        const items = await res.json();
        document.getElementById('items-list').innerHTML = items.length ? items.map(it => `
          <div class="prim-card">
            <div class="prim-icon">${esc(it.icon || '📦')}</div>
            <h4>${esc(it.name || it.id)}</h4>
            <div class="prim-meta">
              <span class="pill-${esc(it.rarity || 'common')}">${esc(it.rarity || 'common')}</span>
              ${it.stackable ? `<span>可堆叠 / Stack ${it.max_stack || 999}</span>` : '<span>独立 / Unique</span>'}
            </div>
            <code>${esc(it.id)}</code>
          </div>
        `).join('') : '<p class="empty">还没有物品 / No items yet</p>';
      } catch (e) {
        document.getElementById('items-list').innerHTML = '<p class="empty">加载失败 / Load failed</p>';
      }
    },

    async _newItem() {
      const id = prompt('物品ID / Item ID (e.g. coffee_bean)');
      if (!id) return;
      const name = prompt('显示名 / Display name');
      if (!name) return;
      const rarity = prompt('稀有度 / Rarity: common / rare / epic / legendary', 'common') || 'common';
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/items`, {
          method: 'POST',
          json: { id, name, icon: '🎁', rarity, stackable: true, max_stack: 999 }
        });
        if (res.ok) {
          showToast('已创建 / Created');
          this._renderItems();
        } else {
          showToast('创建失败 / Create failed', 'error');
        }
      } catch (e) {
        showToast(e.message || '创建失败', 'error');
      }
    },

    // ════════════════════════════════════════════════════════════
    // ACHIEVEMENTS / 成就
    // ════════════════════════════════════════════════════════════
    async _renderAchievements() {
      const pane = document.getElementById('prim-achievements');
      if (!pane) return;
      pane.innerHTML = `
        <button class="btn-primary" onclick="PrimitivesAdminView._newAchievement()">+ 新建成就 / New Achievement</button>
        <div id="ach-list" class="prim-grid"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/achievements`);
        if (!res.ok) {
          document.getElementById('ach-list').innerHTML = '<p class="empty">加载失败 / Load failed</p>';
          return;
        }
        const achs = await res.json();
        document.getElementById('ach-list').innerHTML = achs.length ? achs.map(a => `
          <div class="prim-card">
            <h4>${esc(a.name || a.id)}</h4>
            <div class="prim-meta">
              <span>目标 / Target: ${esc(a.target_metric || '')} ≥ ${a.target_value != null ? a.target_value : '?'}</span>
              ${a.xp_reward ? `<span>+${a.xp_reward} XP</span>` : ''}
            </div>
            <code>${esc(a.id)}</code>
          </div>
        `).join('') : '<p class="empty">还没有成就 / No achievements yet</p>';
      } catch (e) {
        document.getElementById('ach-list').innerHTML = '<p class="empty">加载失败 / Load failed</p>';
      }
    },

    async _newAchievement() {
      const id = prompt('成就ID / Achievement ID');
      if (!id) return;
      const name = prompt('名字 / Name');
      if (!name) return;
      const metric = prompt('追踪指标 / Tracked metric (games_played / streak / invites_converted)', 'games_played') || 'games_played';
      const target = parseInt(prompt('目标值 / Target value', '10') || '10', 10);
      const xp = parseInt(prompt('完成奖励 XP / Reward XP', '100') || '100', 10);
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/achievements`, {
          method: 'POST',
          json: { id, name, target_metric: metric, target_value: target, xp_reward: xp }
        });
        if (res.ok) {
          showToast('已创建 / Created');
          this._renderAchievements();
        } else {
          showToast('创建失败 / Create failed', 'error');
        }
      } catch (e) {
        showToast(e.message || '创建失败', 'error');
      }
    },

    // ════════════════════════════════════════════════════════════
    // QUESTS / 任务
    // ════════════════════════════════════════════════════════════
    async _renderQuests() {
      const pane = document.getElementById('prim-quests');
      if (!pane) return;
      pane.innerHTML = `
        <button class="btn-primary" onclick="PrimitivesAdminView._newQuest()">+ 新建任务 / New Quest</button>
        <div id="quest-list" class="prim-grid"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/quests`);
        if (!res.ok) {
          document.getElementById('quest-list').innerHTML = '<p class="empty">加载失败 / Load failed</p>';
          return;
        }
        const quests = await res.json();
        document.getElementById('quest-list').innerHTML = quests.length ? quests.map(q => `
          <div class="prim-card">
            <h4>${esc(q.name || q.id)}</h4>
            <p class="prim-meta">${esc(q.description || '')}</p>
            <div class="prim-meta">
              <span>${(q.steps || []).length} 步骤 / steps</span>
              ${q.total_reward && q.total_reward.xp ? `<span>+${q.total_reward.xp} XP</span>` : ''}
            </div>
            <code>${esc(q.id)}</code>
          </div>
        `).join('') : '<p class="empty">还没有任务 / No quests yet</p>';
      } catch (e) {
        document.getElementById('quest-list').innerHTML = '<p class="empty">加载失败 / Load failed</p>';
      }
    },

    async _newQuest() {
      const id = prompt('任务ID / Quest ID');
      if (!id) return;
      const name = prompt('任务名 / Quest name');
      if (!name) return;
      const desc = prompt('描述 / Description') || '';
      const stepsRaw = prompt('步骤数 / Number of steps', '3');
      const steps = parseInt(stepsRaw || '3', 10) || 3;
      const reward = parseInt(prompt('完成 XP 奖励 / Total XP reward', '500') || '500', 10);
      const stepArr = Array.from({ length: steps }, (_, i) => ({
        action: `step_${i + 1}`, target: 1, reward_xp: 50
      }));
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/quests`, {
          method: 'POST',
          json: { id, name, description: desc, steps: stepArr, total_reward: { xp: reward } }
        });
        if (res.ok) {
          showToast('已创建 / Created');
          this._renderQuests();
        } else {
          showToast('创建失败 / Create failed', 'error');
        }
      } catch (e) {
        showToast(e.message || '创建失败', 'error');
      }
    },

    // ════════════════════════════════════════════════════════════
    // TIERS / 等级
    // ════════════════════════════════════════════════════════════
    async _renderTiers() {
      const pane = document.getElementById('prim-tiers');
      if (!pane) return;
      pane.innerHTML = `
        <button class="btn-primary" onclick="PrimitivesAdminView._newTier()">+ 新建等级 / New Tier</button>
        <div id="tier-list"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/tiers`);
        if (!res.ok) {
          document.getElementById('tier-list').innerHTML = '<p class="empty">加载失败 / Load failed</p>';
          return;
        }
        const tiers = await res.json();
        const sorted = [...tiers].sort((a, b) => (a.threshold_xp || 0) - (b.threshold_xp || 0));
        document.getElementById('tier-list').innerHTML = `
          <div class="tier-ladder">
            ${sorted.length ? sorted.map(t => `
              <div class="tier-row">
                <strong>${esc(t.name || t.id)}</strong>
                <span class="tier-threshold">${t.threshold_xp != null ? t.threshold_xp : 0} XP</span>
                <span class="tier-perks">${(t.perks || []).map(p => esc(p)).join(', ') || '无特权 / No perks'}</span>
                <code>${esc(t.id)}</code>
              </div>
            `).join('') : '<p class="empty">还没有等级 / No tiers yet</p>'}
          </div>
        `;
      } catch (e) {
        document.getElementById('tier-list').innerHTML = '<p class="empty">加载失败 / Load failed</p>';
      }
    },

    async _newTier() {
      const id = prompt('Tier ID (e.g. gold)');
      if (!id) return;
      const name = prompt('显示名 / Display name (e.g. 金会员 / Gold Member)');
      if (!name) return;
      const threshold = parseInt(prompt('XP 门槛 / XP threshold', '5000') || '5000', 10);
      const perksRaw = prompt('特权(逗号分隔) / Perks (comma-separated)', '免运费,生日礼') || '';
      const perks = perksRaw.split(',').map(s => s.trim()).filter(Boolean);
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/tiers`, {
          method: 'POST',
          json: { id, name, threshold_xp: threshold, perks }
        });
        if (res.ok) {
          showToast('已创建 / Created');
          this._renderTiers();
        } else {
          showToast('创建失败 / Create failed', 'error');
        }
      } catch (e) {
        showToast(e.message || '创建失败', 'error');
      }
    },

    // ════════════════════════════════════════════════════════════
    // EVENTS / 事件
    // ════════════════════════════════════════════════════════════
    async _renderEvents() {
      const pane = document.getElementById('prim-events');
      if (!pane) return;
      pane.innerHTML = `
        <button class="btn-primary" onclick="PrimitivesAdminView._newEvent()">+ 创建事件 / New Event</button>
        <div id="event-list" class="prim-grid"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/primitives/brand/${state.brandId}/events?active=true`);
        if (!res.ok) {
          document.getElementById('event-list').innerHTML = '<p class="empty">加载失败 / Load failed</p>';
          return;
        }
        const events = await res.json();
        document.getElementById('event-list').innerHTML = events.length ? events.map(e => `
          <div class="prim-card">
            <h4>${esc(e.name || e.id)}</h4>
            <div class="prim-meta">
              <span>${e.start_at ? new Date(e.start_at).toLocaleString() : '?'} → ${e.end_at ? new Date(e.end_at).toLocaleString() : '?'}</span>
            </div>
            ${e.multipliers ? `<div class="prim-meta">倍率 / Multipliers: ${esc(JSON.stringify(e.multipliers))}</div>` : ''}
            <code>${esc(e.id)}</code>
          </div>
        `).join('') : '<p class="empty">没有进行中的事件 / No active events</p>';
      } catch (e) {
        document.getElementById('event-list').innerHTML = '<p class="empty">加载失败 / Load failed</p>';
      }
    },

    async _newEvent() {
      const id = prompt('事件ID / Event ID (e.g. double_11)');
      if (!id) return;
      const name = prompt('名字 / Name');
      if (!name) return;
      const days = parseInt(prompt('持续天数 / Duration (days)', '7') || '7', 10);
      const mult = parseFloat(prompt('XP 倍率 / XP multiplier', '2') || '2');
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
          showToast('事件已创建 / Event created');
          this._renderEvents();
        } else {
          showToast('创建失败 / Create failed', 'error');
        }
      } catch (e) {
        showToast(e.message || '创建失败', 'error');
      }
    }
  };

  window.PrimitivesAdminView = PrimitivesAdminView;
})();
