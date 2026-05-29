/* ─── Monitoring View ────────────────────────────────────────────────
 * Social / P2P / Multiplayer / Tutorials — real-time status
 * Backend APIs:
 *   /api/v1/social/{user_id}/{friends|followers|following|feed}
 *   /api/v1/p2p/gifts/{inbox|sent}?user_id=...
 *   /api/v1/p2p/trades/pending?user_id=...
 *   /api/v1/multiplayer/{coop-quest|raid|territory|squad}/{id}
 *   /api/v1/tutorials/brand/{brand_id}
 *   /api/v1/tutorials/{id}/abandon
 * ──────────────────────────────────────────────────────────────────── */
(function() {
  const MonitoringView = {
    async render() {
      const root = document.getElementById('view-monitoring');
      if (!root) return;
      root.innerHTML = `
        <h2 class="page-title">Monitoring / 监控中心</h2>
        <p class="page-subtitle">社交、P2P、多人、教程 — 实时状态</p>
        <div class="tabs mon-tabs">
          <button class="tab active" data-tab="social">👥 社交 Social</button>
          <button class="tab" data-tab="p2p">🎁 P2P</button>
          <button class="tab" data-tab="multiplayer">⚔️ 多人 Multiplayer</button>
          <button class="tab" data-tab="tutorials">📚 教程 Tutorials</button>
        </div>
        <div id="mon-social" class="mon-pane"></div>
        <div id="mon-p2p" class="mon-pane" style="display:none"></div>
        <div id="mon-multiplayer" class="mon-pane" style="display:none"></div>
        <div id="mon-tutorials" class="mon-pane" style="display:none"></div>
      `;
      root.querySelectorAll('.tab').forEach(btn => {
        btn.addEventListener('click', () => {
          root.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          ['social','p2p','multiplayer','tutorials'].forEach(t => {
            const pane = document.getElementById(`mon-${t}`);
            if (pane) pane.style.display = (t === btn.dataset.tab) ? 'block' : 'none';
          });
          this._loadTab(btn.dataset.tab);
        });
      });
      this._loadTab('social');
    },

    _loadTab(tab) {
      ({
        social: () => this._renderSocial(),
        p2p: () => this._renderP2P(),
        multiplayer: () => this._renderMultiplayer(),
        tutorials: () => this._renderTutorials(),
      })[tab]?.();
    },

    // ═══════════════════════════════════════════════════════════════════
    // SOCIAL
    // ═══════════════════════════════════════════════════════════════════
    _renderSocial() {
      const pane = document.getElementById('mon-social');
      pane.innerHTML = `
        <div class="mon-toolbar">
          <input id="social-uid" placeholder="用户ID / User ID" class="mon-input">
          <button class="btn-mini" onclick="MonitoringView._lookupSocial()">查询 Lookup</button>
        </div>
        <div id="social-result"></div>
        <p class="mon-info">📊 输入用户ID查看好友/关注/动态。后端：/api/v1/social/{user_id}/friends|followers|following|feed</p>
      `;
    },

    async _lookupSocial() {
      const uid = document.getElementById('social-uid').value.trim();
      if (!uid) return;
      const result = document.getElementById('social-result');
      result.innerHTML = '<div class="loading-center"><div class="spinner"></div></div>';
      const [friends, followers, following, feed] = await Promise.all([
        apiFetch(`/api/v1/social/${uid}/friends?brand_id=${state.brandId}`).then(r=>r.ok?r.json():[]).catch(()=>[]),
        apiFetch(`/api/v1/social/${uid}/followers`).then(r=>r.ok?r.json():[]).catch(()=>[]),
        apiFetch(`/api/v1/social/${uid}/following`).then(r=>r.ok?r.json():[]).catch(()=>[]),
        apiFetch(`/api/v1/social/${uid}/feed?brand_id=${state.brandId}`).then(r=>r.ok?r.json():[]).catch(()=>[])
      ]);
      const friendsArr = Array.isArray(friends) ? friends : (friends.friends || []);
      const followersArr = Array.isArray(followers) ? followers : (followers.followers || []);
      const followingArr = Array.isArray(following) ? following : (following.following || []);
      const feedArr = Array.isArray(feed) ? feed : (feed.feed || feed.posts || []);
      result.innerHTML = `
        <div class="kpi-grid">
          <div class="kpi-card"><div class="kpi-label">好友 Friends</div><div class="kpi-value">${friendsArr.length}</div></div>
          <div class="kpi-card"><div class="kpi-label">粉丝 Followers</div><div class="kpi-value">${followersArr.length}</div></div>
          <div class="kpi-card"><div class="kpi-label">关注 Following</div><div class="kpi-value">${followingArr.length}</div></div>
          <div class="kpi-card"><div class="kpi-label">动态 Feed</div><div class="kpi-value">${feedArr.length}</div></div>
        </div>
        ${feedArr.length ? `<h4 style="margin-top:18px">最近动态 / Recent Feed</h4>
          <ul class="activity-list">${feedArr.slice(0,10).map(p=>`
            <li>
              <span class="time">${new Date(p.created_at||p.timestamp||0).toLocaleString()}</span>
              <span class="event">${esc(p.event_type||p.type||'event')}</span>
              ${p.actor_id ? `<span class="user">${esc(p.actor_id)}</span>` : ''}
            </li>
          `).join('')}</ul>` : ''}
      `;
    },

    // ═══════════════════════════════════════════════════════════════════
    // P2P (Gifts + Trades)
    // ═══════════════════════════════════════════════════════════════════
    _renderP2P() {
      const pane = document.getElementById('mon-p2p');
      pane.innerHTML = `
        <div class="mon-toolbar">
          <input id="p2p-uid" placeholder="用户ID / User ID" class="mon-input">
          <button class="btn-mini" onclick="MonitoringView._lookupP2P()">查询 Lookup</button>
        </div>
        <div id="p2p-result"></div>
        <p class="mon-info">📦 礼物收件箱 / 已发送 / 交易请求 — Gift inbox, sent, pending trades</p>
      `;
    },

    async _lookupP2P() {
      const uid = document.getElementById('p2p-uid').value.trim();
      if (!uid) return;
      const result = document.getElementById('p2p-result');
      result.innerHTML = '<div class="loading-center"><div class="spinner"></div></div>';
      const [inbox, sent, trades] = await Promise.all([
        apiFetch(`/api/v1/p2p/gifts/inbox?user_id=${uid}`).then(r=>r.ok?r.json():{gifts:[]}).catch(()=>({gifts:[]})),
        apiFetch(`/api/v1/p2p/gifts/sent?user_id=${uid}`).then(r=>r.ok?r.json():{gifts:[]}).catch(()=>({gifts:[]})),
        apiFetch(`/api/v1/p2p/trades/pending?user_id=${uid}`).then(r=>r.ok?r.json():{trades:[]}).catch(()=>({trades:[]}))
      ]);
      const inboxArr = inbox.gifts || [];
      const sentArr = sent.gifts || [];
      const tradesArr = trades.trades || [];
      result.innerHTML = `
        <div class="kpi-grid">
          <div class="kpi-card"><div class="kpi-label">未领礼物 Unclaimed</div><div class="kpi-value">${inboxArr.length}</div></div>
          <div class="kpi-card"><div class="kpi-label">已发送 Sent</div><div class="kpi-value">${sentArr.length}</div></div>
          <div class="kpi-card"><div class="kpi-label">待回应交易 Pending Trades</div><div class="kpi-value">${tradesArr.length}</div></div>
        </div>
        ${inboxArr.length ? `<h4 style="margin-top:18px">收件箱 / Inbox</h4>
          <ul class="activity-list">${inboxArr.slice(0,10).map(g=>`
            <li>
              <span class="time">${new Date(g.created_at||g.sent_at||0).toLocaleString()}</span>
              <span class="event">${esc(g.gift_type||g.type||'gift')}</span>
              <span class="user">来自 ${esc(g.from_user_id||g.sender_id||'?')}</span>
            </li>
          `).join('')}</ul>` : ''}
        ${tradesArr.length ? `<h4 style="margin-top:18px">待回应交易 / Pending Trades</h4>
          <ul class="activity-list">${tradesArr.slice(0,10).map(t=>`
            <li>
              <span class="time">${new Date(t.created_at||0).toLocaleString()}</span>
              <span class="event">${esc(t.status||'pending')}</span>
              <span class="user">${esc(t.from_user_id||'?')} ↔ ${esc(t.to_user_id||'?')}</span>
            </li>
          `).join('')}</ul>` : ''}
      `;
    },

    // ═══════════════════════════════════════════════════════════════════
    // MULTIPLAYER
    // ═══════════════════════════════════════════════════════════════════
    _renderMultiplayer() {
      const pane = document.getElementById('mon-multiplayer');
      pane.innerHTML = `
        <p class="mon-info">⚔️ 多人活动需通过具体 ID 查询 / Lookup multiplayer entities by ID:</p>
        <ul class="mon-list">
          <li><code>GET /api/v1/multiplayer/coop-quest/{coop_id}</code> — 团队任务 CoopQuest</li>
          <li><code>GET /api/v1/multiplayer/raid/{party_id}</code> — 副本 Raid</li>
          <li><code>GET /api/v1/multiplayer/territory/{territory_id}</code> — 领地 Territory</li>
          <li><code>GET /api/v1/multiplayer/squad/{squad_id}</code> — 小队 Squad</li>
        </ul>
        <div class="mon-toolbar">
          <select id="mp-type" class="mon-input">
            <option value="coop-quest">CoopQuest 团队任务</option>
            <option value="raid">Raid 副本</option>
            <option value="territory">Territory 领地</option>
            <option value="squad">Squad 小队</option>
          </select>
          <input id="mp-id" placeholder="ID" class="mon-input">
          <button class="btn-mini" onclick="MonitoringView._lookupMP()">查询 Lookup</button>
        </div>
        <div id="mp-result"></div>
      `;
    },

    async _lookupMP() {
      const type = document.getElementById('mp-type').value;
      const id = document.getElementById('mp-id').value.trim();
      if (!id) return;
      const result = document.getElementById('mp-result');
      result.innerHTML = '<div class="loading-center"><div class="spinner"></div></div>';
      try {
        const res = await apiFetch(`/api/v1/multiplayer/${type}/${id}`);
        if (!res.ok) { result.innerHTML = '<p class="empty">未找到 / Not found</p>'; return; }
        const data = await res.json();
        result.innerHTML = `<pre class="json-out">${esc(JSON.stringify(data, null, 2))}</pre>`;
      } catch(e) {
        result.innerHTML = '<p class="empty">查询失败 / Lookup failed</p>';
      }
    },

    // ═══════════════════════════════════════════════════════════════════
    // TUTORIALS
    // ═══════════════════════════════════════════════════════════════════
    async _renderTutorials() {
      const pane = document.getElementById('mon-tutorials');
      pane.innerHTML = `
        <h4>本品牌教程历史 / Brand Tutorials History</h4>
        <div id="tutorials-list"><div class="loading-center"><div class="spinner"></div></div></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/tutorials/brand/${state.brandId}`);
        const listEl = document.getElementById('tutorials-list');
        if (!res.ok) { listEl.innerHTML = '<p class="empty">加载失败 / Load failed</p>'; return; }
        const data = await res.json();
        const tutorials = data.tutorials || (Array.isArray(data) ? data : []);
        if (!tutorials.length) {
          listEl.innerHTML = '<p class="empty">还没有教程。在 Recipes 页面点击"教程模式"开始。<br>No tutorials yet. Start one from the Recipes page.</p>';
          return;
        }
        listEl.innerHTML = tutorials.map(t => {
          const tid = t.tutorial_id || t.id || '';
          const totalSteps = t.total_steps || (Array.isArray(t.steps) ? t.steps.length : '?');
          return `
            <div class="ops-card">
              <h4>${esc(t.title_cn || t.title || tid)}</h4>
              <div class="ops-stats">
                <span>状态 Status: <strong>${esc(t.status || 'active')}</strong></span>
                <span>进度 Progress: ${t.current_step || 0}/${totalSteps}</span>
                <span>来源 Recipe: ${esc(t.recipe_id || '-')}</span>
                ${t.started_at ? `<span>开始 Started: ${new Date(t.started_at).toLocaleString()}</span>` : ''}
              </div>
              <div class="ops-tools">
                ${t.status === 'active' && window.TutorialEngine ? `<button class="btn-mini" onclick="TutorialEngine.start('${esc(tid)}')">继续 Resume</button>` : ''}
                ${t.status !== 'completed' && t.status !== 'abandoned' ? `<button class="btn-mini btn-danger" onclick="MonitoringView._abandonTutorial('${esc(tid)}')">放弃 Abandon</button>` : ''}
              </div>
            </div>
          `;
        }).join('');
      } catch(e) {
        const listEl = document.getElementById('tutorials-list');
        if (listEl) listEl.innerHTML = '<p class="empty">加载失败 / Load failed</p>';
      }
    },

    async _abandonTutorial(id) {
      if (!confirm('确认放弃这个教程？\nAbandon this tutorial?')) return;
      try {
        const res = await apiFetch(`/api/v1/tutorials/${id}/abandon`, { method: 'POST' });
        if (res.ok && typeof showToast === 'function') showToast('已放弃 / Abandoned');
      } catch(e) {
        if (typeof showToast === 'function') showToast('操作失败 / Failed');
      }
      this._renderTutorials();
    }
  };

  window.MonitoringView = MonitoringView;
})();
