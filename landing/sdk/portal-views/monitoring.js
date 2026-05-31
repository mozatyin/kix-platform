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
  const t = (key, opts) => {
    if (window.i18next && typeof window.i18next.t === 'function') {
      return window.i18next.t('portal-sdk:' + key, opts);
    }
    return key;
  };

  const MonitoringView = {
    async render() {
      const root = document.getElementById('view-monitoring');
      if (!root) return;
      root.innerHTML = `
        <h2 class="page-title">${t('monitoring.title')}</h2>
        <p class="page-subtitle">${t('monitoring.subtitle')}</p>
        <div class="tabs mon-tabs">
          <button class="tab active" data-tab="social">👥 ${t('monitoring.tab.social')}</button>
          <button class="tab" data-tab="p2p">🎁 ${t('monitoring.tab.p2p')}</button>
          <button class="tab" data-tab="multiplayer">⚔️ ${t('monitoring.tab.multiplayer')}</button>
          <button class="tab" data-tab="tutorials">📚 ${t('monitoring.tab.tutorials')}</button>
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
          ['social','p2p','multiplayer','tutorials'].forEach(tn => {
            const pane = document.getElementById(`mon-${tn}`);
            if (pane) pane.style.display = (tn === btn.dataset.tab) ? 'block' : 'none';
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
          <input id="social-uid" placeholder="${t('common.user-id')}" class="mon-input">
          <button class="btn-mini" onclick="MonitoringView._lookupSocial()">${t('common.lookup')}</button>
        </div>
        <div id="social-result"></div>
        <p class="mon-info">📊 ${t('monitoring.social.info')}</p>
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
          <div class="kpi-card"><div class="kpi-label">${t('monitoring.social.friends')}</div><div class="kpi-value">${friendsArr.length}</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('monitoring.social.followers')}</div><div class="kpi-value">${followersArr.length}</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('monitoring.social.following')}</div><div class="kpi-value">${followingArr.length}</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('monitoring.social.feed')}</div><div class="kpi-value">${feedArr.length}</div></div>
        </div>
        ${feedArr.length ? `<h4 style="margin-top:18px">${t('monitoring.social.recent-feed')}</h4>
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
          <input id="p2p-uid" placeholder="${t('common.user-id')}" class="mon-input">
          <button class="btn-mini" onclick="MonitoringView._lookupP2P()">${t('common.lookup')}</button>
        </div>
        <div id="p2p-result"></div>
        <p class="mon-info">📦 ${t('monitoring.p2p.info')}</p>
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
          <div class="kpi-card"><div class="kpi-label">${t('monitoring.p2p.unclaimed')}</div><div class="kpi-value">${inboxArr.length}</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('monitoring.p2p.sent')}</div><div class="kpi-value">${sentArr.length}</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('monitoring.p2p.pending-trades')}</div><div class="kpi-value">${tradesArr.length}</div></div>
        </div>
        ${inboxArr.length ? `<h4 style="margin-top:18px">${t('monitoring.p2p.inbox')}</h4>
          <ul class="activity-list">${inboxArr.slice(0,10).map(g=>`
            <li>
              <span class="time">${new Date(g.created_at||g.sent_at||0).toLocaleString()}</span>
              <span class="event">${esc(g.gift_type||g.type||'gift')}</span>
              <span class="user">${t('common.from')} ${esc(g.from_user_id||g.sender_id||'?')}</span>
            </li>
          `).join('')}</ul>` : ''}
        ${tradesArr.length ? `<h4 style="margin-top:18px">${t('monitoring.p2p.trades-header')}</h4>
          <ul class="activity-list">${tradesArr.slice(0,10).map(tr=>`
            <li>
              <span class="time">${new Date(tr.created_at||0).toLocaleString()}</span>
              <span class="event">${esc(tr.status||'pending')}</span>
              <span class="user">${esc(tr.from_user_id||'?')} ↔ ${esc(tr.to_user_id||'?')}</span>
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
        <p class="mon-info">⚔️ ${t('monitoring.mp.info')}</p>
        <ul class="mon-list">
          <li><code>GET /api/v1/multiplayer/coop-quest/{coop_id}</code> — ${t('monitoring.mp.coop-quest-desc')}</li>
          <li><code>GET /api/v1/multiplayer/raid/{party_id}</code> — ${t('monitoring.mp.raid-desc')}</li>
          <li><code>GET /api/v1/multiplayer/territory/{territory_id}</code> — ${t('monitoring.mp.territory-desc')}</li>
          <li><code>GET /api/v1/multiplayer/squad/{squad_id}</code> — ${t('monitoring.mp.squad-desc')}</li>
        </ul>
        <div class="mon-toolbar">
          <select id="mp-type" class="mon-input">
            <option value="coop-quest">${t('monitoring.mp.opt-coop')}</option>
            <option value="raid">${t('monitoring.mp.opt-raid')}</option>
            <option value="territory">${t('monitoring.mp.opt-territory')}</option>
            <option value="squad">${t('monitoring.mp.opt-squad')}</option>
          </select>
          <input id="mp-id" placeholder="ID" class="mon-input">
          <button class="btn-mini" onclick="MonitoringView._lookupMP()">${t('common.lookup')}</button>
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
        if (!res.ok) { result.innerHTML = `<p class="empty">${t('common.not-found')}</p>`; return; }
        const data = await res.json();
        result.innerHTML = `<pre class="json-out">${esc(JSON.stringify(data, null, 2))}</pre>`;
      } catch(e) {
        result.innerHTML = `<p class="empty">${t('common.lookup-failed')}</p>`;
      }
    },

    // ═══════════════════════════════════════════════════════════════════
    // TUTORIALS
    // ═══════════════════════════════════════════════════════════════════
    async _renderTutorials() {
      const pane = document.getElementById('mon-tutorials');
      pane.innerHTML = `
        <h4>${t('monitoring.tutorials.title')}</h4>
        <div id="tutorials-list"><div class="loading-center"><div class="spinner"></div></div></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/tutorials/brand/${state.brandId}`);
        const listEl = document.getElementById('tutorials-list');
        if (!res.ok) { listEl.innerHTML = `<p class="empty">${t('common.load-failed')}</p>`; return; }
        const data = await res.json();
        const tutorials = data.tutorials || (Array.isArray(data) ? data : []);
        if (!tutorials.length) {
          listEl.innerHTML = `<p class="empty">${t('monitoring.tutorials.none')}</p>`;
          return;
        }
        listEl.innerHTML = tutorials.map(tu => {
          const tid = tu.tutorial_id || tu.id || '';
          const totalSteps = tu.total_steps || (Array.isArray(tu.steps) ? tu.steps.length : '?');
          return `
            <div class="ops-card">
              <h4>${esc(tu.title_cn || tu.title || tid)}</h4>
              <div class="ops-stats">
                <span>${t('monitoring.tutorials.status')}: <strong>${esc(tu.status || 'active')}</strong></span>
                <span>${t('monitoring.tutorials.progress')}: ${tu.current_step || 0}/${totalSteps}</span>
                <span>${t('monitoring.tutorials.recipe')}: ${esc(tu.recipe_id || '-')}</span>
                ${tu.started_at ? `<span>${t('monitoring.tutorials.started')}: ${new Date(tu.started_at).toLocaleString()}</span>` : ''}
              </div>
              <div class="ops-tools">
                ${tu.status === 'active' && window.TutorialEngine ? `<button class="btn-mini" onclick="TutorialEngine.start('${esc(tid)}')">${t('monitoring.tutorials.resume')}</button>` : ''}
                ${tu.status !== 'completed' && tu.status !== 'abandoned' ? `<button class="btn-mini btn-danger" onclick="MonitoringView._abandonTutorial('${esc(tid)}')">${t('monitoring.tutorials.abandon')}</button>` : ''}
              </div>
            </div>
          `;
        }).join('');
      } catch(e) {
        const listEl = document.getElementById('tutorials-list');
        if (listEl) listEl.innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
      }
    },

    async _abandonTutorial(id) {
      if (!confirm(t('monitoring.tutorials.abandon-confirm'))) return;
      try {
        const res = await apiFetch(`/api/v1/tutorials/${id}/abandon`, { method: 'POST' });
        if (res.ok && typeof showToast === 'function') showToast(t('monitoring.tutorials.abandoned'));
      } catch(e) {
        if (typeof showToast === 'function') showToast(t('common.failed'));
      }
      this._renderTutorials();
    }
  };

  window.MonitoringView = MonitoringView;
})();
