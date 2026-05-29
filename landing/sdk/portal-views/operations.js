// ═══════════════════════════════════════════════════════════════════════
// OPERATIONS VIEW — 运营活动
// Group Buy / Tournaments / Limited Drops / FCFS management
// ═══════════════════════════════════════════════════════════════════════
(function() {
  'use strict';

  const OperationsView = {
    async render() {
      const root = document.getElementById('view-operations');
      if (!root) return;

      root.innerHTML = `
        <h2 class="page-title">Operations / 运营活动</h2>
        <p class="page-subtitle">拼团、锦标赛、限量、抢先 — 实时管理</p>

        <div class="ops-tabs">
          <button class="ops-tab active" data-tab="groupbuy">&#128722; 拼团 / Group Buy</button>
          <button class="ops-tab" data-tab="tournament">&#127942; 锦标赛 / Tournaments</button>
          <button class="ops-tab" data-tab="limited">&#9200; 限量 / Limited</button>
          <button class="ops-tab" data-tab="fcfs">&#127873; 抢先 / FCFS</button>
        </div>

        <div id="ops-groupbuy" class="ops-pane"></div>
        <div id="ops-tournament" class="ops-pane" style="display:none"></div>
        <div id="ops-limited" class="ops-pane" style="display:none"></div>
        <div id="ops-fcfs" class="ops-pane" style="display:none"></div>
      `;

      const self = this;
      root.querySelectorAll('.ops-tab').forEach(t => {
        t.addEventListener('click', () => {
          root.querySelectorAll('.ops-tab').forEach(b => b.classList.remove('active'));
          t.classList.add('active');
          ['groupbuy','tournament','limited','fcfs'].forEach(name => {
            const pane = document.getElementById(`ops-${name}`);
            if (pane) pane.style.display = name === t.dataset.tab ? 'block' : 'none';
          });
          self._loadTab(t.dataset.tab);
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
          <button class="btn-primary" onclick="OperationsView._newGroupBuy()">+ 创建拼团 / New Group Buy</button>
        </div>
        <div id="groupbuy-list"><p class="empty">加载中...</p></div>
      `;

      const list = document.getElementById('groupbuy-list');
      let res;
      try {
        res = await apiFetch(`/api/v1/groups/brand/${state.brandId}/active`);
      } catch (e) {
        list.innerHTML = '<p class="empty">加载失败 / Failed to load</p>';
        return;
      }
      if (!res.ok) { list.innerHTML = '<p class="empty">加载失败 / Failed to load</p>'; return; }
      const groups = await res.json();
      const buys = (groups.groups || []).filter(g => g.kind === 'buy');
      if (!buys.length) {
        list.innerHTML = '<p class="empty">还没有拼团活动 / No active group buys</p>';
        return;
      }
      list.innerHTML = buys.map(g => `
        <div class="ops-card">
          <h4>${esc(g.sku_id || g.group_id)}</h4>
          <div class="ops-stats">
            <span>状态 Status: <strong>${esc(g.status || '?')}</strong></span>
            <span>人数 Members: ${(g.members && g.members.length) || 0}/${g.group_size || '?'}</span>
            <span>折扣 Discount: ${g.discount_percent || 0}%</span>
            <span>过期 Expires: ${g.expires_at ? new Date(g.expires_at).toLocaleString() : '—'}</span>
          </div>
        </div>
      `).join('');
    },

    async _newGroupBuy() {
      const sku = prompt('商品ID/名称 / SKU ID'); if (!sku) return;
      const size = parseInt(prompt('需要几人成团？ / Group size', '5') || 5);
      const discount = parseInt(prompt('折扣百分比 / Discount %', '50') || 50);
      const hours = parseInt(prompt('多少小时内有效 / Valid for hours', '24') || 24);

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
        return showToast('创建失败 / Create failed', 'error');
      }
      if (!res.ok) return showToast('创建失败 / Create failed', 'error');
      const data = await res.json();
      showToast(`已创建 / Created: ${data.group_id}`);
      this._renderGroupBuy();
    },

    // ════════════════════ TOURNAMENTS ════════════════════
    async _renderTournaments() {
      const pane = document.getElementById('ops-tournament');
      if (!pane) return;
      pane.innerHTML = `
        <div class="ops-toolbar">
          <button class="btn-primary" onclick="OperationsView._newTourney()">+ 创建锦标赛 / New Tournament</button>
        </div>
        <p class="ops-hint">锦标赛 ID 保存在本地，通过 ID 查询详情 / Tournament IDs stored locally; detail fetched on view.</p>
        <div id="tourney-list"></div>
      `;

      const ids = JSON.parse(localStorage.getItem(`kix_tourneys_${state.brandId}`) || '[]');
      const listEl = document.getElementById('tourney-list');
      if (!ids.length) {
        listEl.innerHTML = '<p class="empty">还没有锦标赛 / No tournaments yet</p>';
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
        return { tourney_id: id, name: id, status: '未知 unknown', _missing: true };
      }));
      listEl.innerHTML = tournaments.map(t => `
        <div class="ops-card">
          <h4>${esc(t.name || t.tourney_id)}</h4>
          <div class="ops-stats">
            <span>ID: <code>${esc(t.tourney_id)}</code></span>
            <span>状态 Status: <strong>${esc(t.status || 'active')}</strong></span>
            <span>参赛 Players: ${t.participants_count || (t.leaderboard && t.leaderboard.length) || 0}</span>
            <span>剩余 Left: ${esc(String(t.time_left || (t.end ? new Date(t.end).toLocaleString() : '—')))}</span>
          </div>
          <div class="ops-tools">
            <button class="btn-mini" onclick="OperationsView._settleTourney('${esc(t.tourney_id)}')">结算 Settle</button>
            <button class="btn-mini" onclick="OperationsView._forgetTourney('${esc(t.tourney_id)}')">移除 Forget</button>
          </div>
        </div>
      `).join('');
    },

    async _newTourney() {
      const name = prompt('锦标赛名 / Tournament name'); if (!name) return;
      const entry = parseInt(prompt('入场费(能量) / Entry cost (energy)', '10') || 10);
      const days = parseInt(prompt('持续天数 / Duration days', '7') || 7);

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
        return showToast('创建失败 / Create failed', 'error');
      }
      if (!res.ok) return showToast('创建失败 / Create failed', 'error');
      const data = await res.json();
      const ids = JSON.parse(localStorage.getItem(`kix_tourneys_${state.brandId}`) || '[]');
      if (data.tourney_id && !ids.includes(data.tourney_id)) ids.push(data.tourney_id);
      localStorage.setItem(`kix_tourneys_${state.brandId}`, JSON.stringify(ids));
      showToast(`锦标赛已创建 / Created: ${data.tourney_id || name}`);
      this._renderTournaments();
    },

    async _settleTourney(id) {
      if (!confirm('确认结算？将分发奖励 / Settle now and distribute prizes?')) return;
      let res;
      try {
        res = await apiFetch(`/api/v1/modules/tourney/${id}/settle`, { method: 'POST' });
      } catch (e) {
        return showToast('结算失败 / Settle failed', 'error');
      }
      showToast(res.ok ? '已结算 / Settled' : '结算失败 / Settle failed', res.ok ? 'success' : 'error');
      if (res.ok) this._renderTournaments();
    },

    _forgetTourney(id) {
      if (!confirm('从本地列表移除？(不影响后端) / Remove from local list?')) return;
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
          <button class="btn-primary" onclick="OperationsView._newLimited()">+ 创建限量活动 / New Limited Drop</button>
        </div>
        <div id="limited-list"></div>
      `;
      const ids = JSON.parse(localStorage.getItem(`kix_limited_${state.brandId}`) || '[]');
      const listEl = document.getElementById('limited-list');
      if (!ids.length) {
        listEl.innerHTML = '<p class="empty">还没有限量活动 / No limited drops yet</p>';
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
            <span>总量 Total: ${esc(String(total))}</span>
            <span>剩余 Remaining: <strong>${esc(String(remain))}</strong></span>
            <span>已领 Claimed: ${claimed != null ? claimed : '?'}</span>
            <span>截止 Ends: ${d.ends_at ? new Date(d.ends_at).toLocaleString() : '无 none'}</span>
          </div>
          <div class="ops-progress"><div class="ops-progress-fill" style="width:${pct}%"></div></div>
          <div class="ops-tools">
            <button class="btn-mini" onclick="OperationsView._forgetLimited('${esc(d.drop_id)}')">移除 Forget</button>
          </div>
        </div>`;
      }).join('');
    },

    async _newLimited() {
      const itemId = prompt('物品ID / Item ID (e.g. fortnite_skin_001)'); if (!itemId) return;
      const supply = parseInt(prompt('总量 / Total supply', '100') || 100);
      const days = parseInt(prompt('活动天数 / Duration days', '7') || 7);
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
        return showToast('创建失败 / Create failed', 'error');
      }
      if (!res.ok) return showToast('创建失败 / Create failed', 'error');
      const ids = JSON.parse(localStorage.getItem(`kix_limited_${state.brandId}`) || '[]');
      ids.push(dropId);
      localStorage.setItem(`kix_limited_${state.brandId}`, JSON.stringify(ids));
      showToast('限量活动已创建 / Limited drop created');
      this._renderLimitedDrops();
    },

    _forgetLimited(id) {
      if (!confirm('从本地列表移除？/ Remove from local list?')) return;
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
          <button class="btn-primary" onclick="OperationsView._newFCFS()">+ 发红包 / New FCFS Pool</button>
        </div>
        <p class="ops-hint">红包池：先到先得，用户通过 claim API 抢领 / Users claim via FCFS API.</p>
        <div id="fcfs-list"></div>
      `;
      const ids = JSON.parse(localStorage.getItem(`kix_fcfs_${state.brandId}`) || '[]');
      const listEl = document.getElementById('fcfs-list');
      if (!ids.length) {
        listEl.innerHTML = '<p class="empty">还没有红包池 / No FCFS pools yet</p>';
        return;
      }
      listEl.innerHTML = ids.map(id => `
        <div class="ops-card">
          <h4>红包池 FCFS ${esc(id)}</h4>
          <div class="ops-stats">
            <span>Claim 端点 / Endpoint: <code>POST /api/v1/triggers/fcfs/${esc(id)}/claim</code></span>
          </div>
          <div class="ops-tools">
            <button class="btn-mini" onclick="OperationsView._forgetFCFS('${esc(id)}')">移除 Forget</button>
          </div>
        </div>
      `).join('');
    },

    async _newFCFS() {
      const size = parseInt(prompt('池子大小（多少人能抢到）/ Pool size', '100') || 100);
      const reward = prompt('奖励描述 / Reward description', '5元优惠券 / $5 voucher');
      const days = parseInt(prompt('有效天数 / Valid days', '1') || 1);
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
        return showToast('创建失败 / Create failed', 'error');
      }
      if (!res.ok) return showToast('创建失败 / Create failed', 'error');
      const ids = JSON.parse(localStorage.getItem(`kix_fcfs_${state.brandId}`) || '[]');
      ids.push(poolId);
      localStorage.setItem(`kix_fcfs_${state.brandId}`, JSON.stringify(ids));
      showToast('红包池已创建 / FCFS pool created');
      this._renderFCFS();
    },

    _forgetFCFS(id) {
      if (!confirm('从本地列表移除？/ Remove from local list?')) return;
      const ids = JSON.parse(localStorage.getItem(`kix_fcfs_${state.brandId}`) || '[]');
      localStorage.setItem(`kix_fcfs_${state.brandId}`, JSON.stringify(ids.filter(x => x !== id)));
      this._renderFCFS();
    }
  };

  window.OperationsView = OperationsView;
})();
