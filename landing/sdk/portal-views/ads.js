/* ─── Ads Manager View ───────────────────────────────────────────────
 * Google Ads / TikTok Ads Manager style — merchant ads portal
 * Backend APIs:
 *   /api/v1/wallet/{brand_id}                          GET
 *   /api/v1/wallet/{brand_id}/topup                    POST
 *   /api/v1/wallet/{brand_id}/topup/{topup_id}/confirm POST
 *   /api/v1/wallet/{brand_id}/daily-budget             POST
 *   /api/v1/wallet/{brand_id}/auto-recharge/configure  POST
 *   /api/v1/wallet/{brand_id}/transactions             GET
 *   /api/v1/campaigns/{brand_id}                       GET
 *   /api/v1/campaigns/create                           POST
 *   /api/v1/campaigns/{cid}/pause|resume               POST
 *   /api/v1/geofence/stores/{brand_id}                 GET
 *   /api/v1/geofence/stores/register                   POST
 *   /api/v1/attribution/brand/{brand_id}/incoming      GET
 *   /api/v1/attribution/brand/{brand_id}/outgoing      GET
 * ──────────────────────────────────────────────────────────────────── */
(function() {
  // i18n helper — uses portal-sdk namespace; safe fallback to key when runtime not loaded.
  const t = (key, opts) => {
    if (window.i18next && typeof window.i18next.t === 'function') {
      return window.i18next.t('portal-sdk:' + key, opts);
    }
    return key;
  };

  const AdsView = {
    async render() {
      const root = document.getElementById('view-ads');
      if (!root) return;
      root.innerHTML = `
        <h2 class="page-title">${t('ads.title')}</h2>
        <p class="page-subtitle">${t('ads.subtitle')}</p>
        <div class="tabs">
          <button class="tab active" data-tab="wallet">💰 ${t('ads.tab.wallet')}</button>
          <button class="tab" data-tab="campaigns">📢 ${t('ads.tab.campaigns')}</button>
          <button class="tab" data-tab="stores">📍 ${t('ads.tab.stores')}</button>
          <button class="tab" data-tab="reports">📊 ${t('ads.tab.reports')}</button>
          <button class="tab" data-tab="attribution">🔍 ${t('ads.tab.attribution')}</button>
        </div>
        <div id="ads-wallet" class="ads-pane"></div>
        <div id="ads-campaigns" class="ads-pane" style="display:none"></div>
        <div id="ads-stores" class="ads-pane" style="display:none"></div>
        <div id="ads-reports" class="ads-pane" style="display:none"></div>
        <div id="ads-attribution" class="ads-pane" style="display:none"></div>
      `;
      root.querySelectorAll('.tab').forEach(b => {
        b.addEventListener('click', () => {
          root.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
          b.classList.add('active');
          ['wallet','campaigns','stores','reports','attribution'].forEach(tn => {
            document.getElementById(`ads-${tn}`).style.display = tn===b.dataset.tab?'block':'none';
          });
          this._loadTab(b.dataset.tab);
        });
      });
      this._loadTab('wallet');
    },

    _loadTab(tab) {
      ({
        wallet: () => this._renderWallet(),
        campaigns: () => this._renderCampaigns(),
        stores: () => this._renderStores(),
        reports: () => this._renderReports(),
        attribution: () => this._renderAttribution()
      })[tab]?.();
    },

    // ═══════════════════════════════════════════════════════════════════
    // WALLET
    // ═══════════════════════════════════════════════════════════════════
    async _renderWallet() {
      const pane = document.getElementById('ads-wallet');
      pane.innerHTML = `<div class="ads-loading">${t('common.loading')}</div>`;
      try {
        const res = await apiFetch(`/api/v1/wallet/${state.brandId}`);
        if (!res.ok) throw new Error('Wallet load failed');
        const w = await res.json();
        const dailyBudget = ((w.daily_budget_cents||0)/100).toFixed(2);
        pane.innerHTML = `
          <div class="wallet-summary">
            <div class="wallet-balance">
              <div class="wallet-label">${t('ads.wallet.balance')}</div>
              <div class="wallet-value">¥${((w.balance_cents||0)/100).toFixed(2)}</div>
              <small>${esc(w.currency || 'CNY')}</small>
            </div>
            <div class="wallet-stat">
              <div class="wallet-label">${t('ads.wallet.today-spent')}</div>
              <div class="wallet-value">¥${((w.daily_spent_cents||0)/100).toFixed(2)}</div>
              <small>${t('ads.wallet.daily-budget', {amount: dailyBudget})}</small>
            </div>
            <div class="wallet-stat">
              <div class="wallet-label">${t('ads.wallet.total-spent')}</div>
              <div class="wallet-value">¥${((w.total_spent_cents||0)/100).toFixed(2)}</div>
            </div>
          </div>
          <div class="wallet-actions">
            <button class="btn-primary" onclick="AdsView._topup()">${t('ads.wallet.topup-button')}</button>
            <button class="btn-outline" onclick="AdsView._setBudget()">${t('ads.wallet.set-budget-button')}</button>
            <button class="btn-outline" onclick="AdsView._configAutoRecharge()">${t('ads.wallet.auto-recharge-button')}</button>
          </div>
          <h4 style="margin-top:24px">${t('ads.wallet.recent-transactions')}</h4>
          <div id="wallet-tx"></div>
        `;
        // Load transactions
        try {
          const tx = await apiFetch(`/api/v1/wallet/${state.brandId}/transactions?limit=20`);
          if (tx.ok) {
            const txList = await tx.json();
            const arr = txList.transactions || txList || [];
            document.getElementById('wallet-tx').innerHTML = arr.length ? `
              <table class="data-table">
                <thead><tr><th>${t('common.time')}</th><th>${t('common.type')}</th><th>${t('common.amount')}</th><th>${t('common.reason')}</th></tr></thead>
                <tbody>
                  ${arr.map(tr => {
                    const amt = (tr.amount_cents != null ? tr.amount_cents : (tr.amount || 0));
                    const ts = tr.ts || tr.timestamp || 0;
                    return `
                      <tr>
                        <td>${ts ? new Date(ts*1000).toLocaleString() : '-'}</td>
                        <td>${esc(tr.type||tr.reason||'?')}</td>
                        <td class="${amt>0?'pos':'neg'}">${amt>0?'+':''}¥${(amt/100).toFixed(2)}</td>
                        <td>${esc(tr.reason||tr.reference_id||'-')}</td>
                      </tr>
                    `;
                  }).join('')}
                </tbody>
              </table>
            ` : `<p class="empty">${t('ads.wallet.no-transactions')}</p>`;
          } else {
            document.getElementById('wallet-tx').innerHTML = `<p class="empty">${t('ads.wallet.no-transactions')}</p>`;
          }
        } catch(e) {
          document.getElementById('wallet-tx').innerHTML = `<p class="empty">${t('ads.wallet.no-transactions')}</p>`;
        }
      } catch(e) {
        pane.innerHTML = `
          <p class="empty">${t('ads.wallet.not-initialized')}</p>
          <div class="wallet-actions">
            <button class="btn-primary" onclick="AdsView._topup()">${t('ads.wallet.topup-button')}</button>
          </div>
        `;
      }
    },

    async _topup() {
      const amount = parseFloat(prompt(t('ads.wallet.topup-prompt'), '1000') || 0);
      if (!amount || amount <= 0) return;
      const method = prompt(t('ads.wallet.payment-prompt'), 'wechat') || 'wechat';
      try {
        const res = await apiFetch(`/api/v1/wallet/${state.brandId}/topup`, {
          method: 'POST',
          json: {amount_cents: Math.round(amount * 100), payment_method: method}
        });
        if (!res.ok) return showToast(t('ads.wallet.topup-failed'), 'error');
        const data = await res.json();
        if (data.status === 'pending' && data.topup_id) {
          if (confirm(t('ads.wallet.topup-pending-confirm', {topup: data.topup_id}))) {
            await apiFetch(`/api/v1/wallet/${state.brandId}/topup/${data.topup_id}/confirm`, {
              method: 'POST',
              json: {payment_gateway_response: {mock: true}}
            });
            showToast(t('ads.wallet.topup-complete'));
          } else {
            showToast(t('ads.wallet.order-created'));
          }
        } else {
          showToast(t('ads.wallet.topup-complete'));
        }
        this._renderWallet();
      } catch(e) {
        showToast(t('ads.wallet.topup-failed'), 'error');
      }
    },

    async _setBudget() {
      const raw = prompt(t('ads.wallet.budget-prompt'), '500');
      if (raw == null) return;
      const amount = parseFloat(raw) || 0;
      try {
        const res = await apiFetch(`/api/v1/wallet/${state.brandId}/daily-budget`, {
          method: 'POST',
          json: {daily_budget_cents: Math.round(amount * 100)}
        });
        if (!res.ok) return showToast(t('ads.wallet.set-failed'), 'error');
        showToast(t('ads.wallet.budget-set'));
        this._renderWallet();
      } catch(e) {
        showToast(t('ads.wallet.set-failed'), 'error');
      }
    },

    async _configAutoRecharge() {
      const enabled = confirm(t('ads.wallet.auto-recharge-confirm'));
      if (!enabled) return;
      const threshold = parseFloat(prompt(t('ads.wallet.threshold-prompt'), '500') || 500);
      const amount = parseFloat(prompt(t('ads.wallet.recharge-amount-prompt'), '5000') || 5000);
      try {
        const res = await apiFetch(`/api/v1/wallet/${state.brandId}/auto-recharge/configure`, {
          method: 'POST',
          json: {
            enabled: true,
            threshold_cents: Math.round(threshold * 100),
            recharge_amount_cents: Math.round(amount * 100),
            payment_method: 'wechat',
            payment_token: 'token_xxx'
          }
        });
        if (!res.ok) return showToast(t('ads.wallet.set-failed'), 'error');
        showToast(t('ads.wallet.auto-recharge-enabled'));
      } catch(e) {
        showToast(t('ads.wallet.set-failed'), 'error');
      }
    },

    // ═══════════════════════════════════════════════════════════════════
    // CAMPAIGNS
    // ═══════════════════════════════════════════════════════════════════
    async _renderCampaigns() {
      const pane = document.getElementById('ads-campaigns');
      pane.innerHTML = `
        <button class="btn-primary" onclick="AdsView._newCampaign()">${t('ads.campaigns.new-button')}</button>
        <div id="campaigns-list" style="margin-top:16px"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/campaigns/${state.brandId}`);
        if (!res.ok) {
          document.getElementById('campaigns-list').innerHTML = `<p class="empty">${t('ads.campaigns.none-short')}</p>`;
          return;
        }
        const list = await res.json();
        const arr = list.campaigns || list || [];
        document.getElementById('campaigns-list').innerHTML = arr.length ? `
          <table class="data-table campaign-table">
            <thead><tr><th>${t('ads.campaigns.col-name')}</th><th>${t('ads.campaigns.col-objective')}</th><th>${t('ads.campaigns.col-bid')}</th><th>${t('ads.campaigns.col-budget')}</th><th>${t('ads.campaigns.col-status')}</th><th>${t('ads.campaigns.col-impressions')}</th><th>${t('ads.campaigns.col-conversions')}</th><th>${t('ads.campaigns.col-spend')}</th><th>${t('ads.campaigns.col-ops')}</th></tr></thead>
            <tbody>
              ${arr.map(c => {
                const status = c.status || 'active';
                const stats = c.stats || {};
                const budgetPerDay = ((c.daily_budget_cents||0)/100).toFixed(0);
                return `
                  <tr>
                    <td><strong>${esc(c.name||'-')}</strong></td>
                    <td>${esc(c.objective||'-')}</td>
                    <td>${esc(c.bid_strategy||'-')} ¥${((c.max_bid_cents||0)/100).toFixed(2)}</td>
                    <td>${t('ads.campaigns.budget-per-day', {amount: budgetPerDay})}</td>
                    <td><span class="status-${esc(status)}">${esc(status)}</span></td>
                    <td>${stats.impressions||0}</td>
                    <td>${stats.conversions||0}</td>
                    <td>¥${((stats.spend_cents||0)/100).toFixed(2)}</td>
                    <td>
                      ${status==='paused'
                        ? `<button class="btn-mini" onclick="AdsView._resume('${esc(c.campaign_id||'')}')">${t('ads.campaigns.resume')}</button>`
                        : `<button class="btn-mini" onclick="AdsView._pause('${esc(c.campaign_id||'')}')">${t('ads.campaigns.pause')}</button>`}
                    </td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        ` : `<p class="empty">${t('ads.campaigns.none')}</p>`;
      } catch(e) {
        document.getElementById('campaigns-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
      }
    },

    async _newCampaign() {
      let modal = document.getElementById('campaign-modal');
      if (!modal) {
        modal = document.createElement('div');
        modal.id = 'campaign-modal';
        modal.className = 'modal-overlay';
        modal.innerHTML = `
          <div class="modal-card large">
            <div class="modal-head"><h3>${t('ads.campaigns.modal-title')}</h3><button onclick="AdsView._closeCampaignModal()">✕</button></div>
            <div class="modal-body">
              <label>${t('ads.campaigns.name-label')}<input id="c-name" placeholder="${t('ads.campaigns.name-placeholder')}" class="form-input"></label>
              <label>${t('ads.campaigns.objective-label')}<select id="c-objective" class="form-input">
                <option value="acquire">${t('ads.campaigns.obj-acquire')}</option>
                <option value="sales">${t('ads.campaigns.obj-sales')}</option>
                <option value="awareness">${t('ads.campaigns.obj-awareness')}</option>
                <option value="geo_visit">${t('ads.campaigns.obj-geo-visit')}</option>
              </select></label>
              <label>${t('ads.campaigns.bid-label')}<select id="c-bid" class="form-input">
                <option value="cpa">${t('ads.campaigns.bid-cpa')}</option>
                <option value="cps">${t('ads.campaigns.bid-cps')}</option>
                <option value="cpm">${t('ads.campaigns.bid-cpm')}</option>
                <option value="cpv">${t('ads.campaigns.bid-cpv')}</option>
              </select></label>
              <label>${t('ads.campaigns.max-bid-label')}<input id="c-max-bid" type="number" value="20" step="0.5" class="form-input"></label>
              <label>${t('ads.campaigns.daily-budget-label')}<input id="c-daily" type="number" value="500" class="form-input"></label>
              <h4>${t('ads.campaigns.targeting-header')}</h4>
              <label>${t('ads.campaigns.country-label')}<input id="c-country" placeholder="${t('ads.campaigns.country-placeholder')}" class="form-input"></label>
              <label>${t('ads.campaigns.city-label')}<input id="c-city" placeholder="${t('ads.campaigns.city-placeholder')}" class="form-input"></label>
              <label>${t('ads.campaigns.radius-label')}<input id="c-radius" type="number" value="5" class="form-input"></label>
              <label>${t('ads.campaigns.age-label')}<input id="c-age-min" type="number" placeholder="18" class="form-input" style="width:80px"> - <input id="c-age-max" type="number" placeholder="65" class="form-input" style="width:80px"></label>
              <h4>${t('ads.campaigns.creative-header')}</h4>
              <label>${t('ads.campaigns.recipe-label')}<input id="c-recipe" placeholder="${t('ads.campaigns.recipe-placeholder')}" class="form-input"></label>
              <label>${t('ads.campaigns.game-label')}<input id="c-game" placeholder="${t('ads.campaigns.game-placeholder')}" class="form-input"></label>
              <label>${t('ads.campaigns.voucher-label')}<input id="c-voucher" placeholder="${t('common.optional')}" class="form-input"></label>
              <h4>${t('ads.campaigns.schedule-header')}</h4>
              <label>${t('ads.campaigns.start-label')}<input id="c-start" type="datetime-local" class="form-input"></label>
              <label>${t('ads.campaigns.end-label')}<input id="c-end" type="datetime-local" class="form-input"></label>
            </div>
            <div class="modal-foot">
              <button class="btn-outline" onclick="AdsView._closeCampaignModal()">${t('common.cancel')}</button>
              <button class="btn-primary" onclick="AdsView._saveCampaign()">${t('ads.campaigns.launch-button')}</button>
            </div>
          </div>
        `;
        document.body.appendChild(modal);
      }
      modal.style.display = 'flex';
      modal.classList.add('active');
    },

    _closeCampaignModal() {
      const m = document.getElementById('campaign-modal');
      if (m) { m.style.display = 'none'; m.classList.remove('active'); }
    },

    async _saveCampaign() {
      const val = id => (document.getElementById(id)?.value || '').trim();
      const num = (id, dflt) => parseFloat(document.getElementById(id)?.value || dflt) || dflt;
      const intOrNull = id => {
        const v = parseInt(document.getElementById(id)?.value || 0);
        return v || null;
      };
      const dailyCents = Math.round(num('c-daily', 500) * 100);
      const body = {
        brand_id: state.brandId,
        name: val('c-name') || t('ads.campaigns.default-name'),
        objective: val('c-objective') || 'acquire',
        bid_strategy: val('c-bid') || 'cpa',
        max_bid_cents: Math.round(num('c-max-bid', 20) * 100),
        daily_budget_cents: dailyCents,
        total_budget_cents: dailyCents * 30,
        targeting: {
          geo: {
            country: val('c-country') || null,
            city: val('c-city') || null,
            radius_km: num('c-radius', 5)
          },
          demographics: {
            age_min: intOrNull('c-age-min'),
            age_max: intOrNull('c-age-max')
          }
        },
        creative: {
          recipe_id: val('c-recipe') || null,
          game_slug: val('c-game') || null,
          voucher_template_id: val('c-voucher') || null
        },
        schedule: {
          start_at: val('c-start') || new Date().toISOString(),
          end_at: val('c-end') || new Date(Date.now()+30*86400000).toISOString()
        }
      };
      try {
        const res = await apiFetch('/api/v1/campaigns/create', { method: 'POST', json: body });
        if (!res.ok) return showToast(t('common.create-failed'), 'error');
        showToast(t('ads.campaigns.launched'));
        this._closeCampaignModal();
        this._renderCampaigns();
      } catch(e) {
        showToast(t('common.create-failed'), 'error');
      }
    },

    async _pause(cid) {
      if (!cid) return;
      try {
        await apiFetch(`/api/v1/campaigns/${cid}/pause`, {method:'POST'});
        showToast(t('ads.campaigns.paused'));
        this._renderCampaigns();
      } catch(e) { showToast(t('ads.campaigns.op-failed'), 'error'); }
    },
    async _resume(cid) {
      if (!cid) return;
      try {
        await apiFetch(`/api/v1/campaigns/${cid}/resume`, {method:'POST'});
        showToast(t('ads.campaigns.resumed'));
        this._renderCampaigns();
      } catch(e) { showToast(t('ads.campaigns.op-failed'), 'error'); }
    },

    // ═══════════════════════════════════════════════════════════════════
    // STORES (Geofencing)
    // ═══════════════════════════════════════════════════════════════════
    async _renderStores() {
      const pane = document.getElementById('ads-stores');
      pane.innerHTML = `
        <button class="btn-primary" onclick="AdsView._newStore()">${t('ads.stores.new-button')}</button>
        <p class="mon-info">${t('ads.stores.info')}</p>
        <div id="stores-list" style="margin-top:16px"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/geofence/stores/${state.brandId}`);
        if (!res.ok) {
          document.getElementById('stores-list').innerHTML = `<p class="empty">${t('ads.stores.none-short')}</p>`;
          return;
        }
        const data = await res.json();
        const arr = data.stores || data || [];
        document.getElementById('stores-list').innerHTML = arr.length ? arr.map(s => `
          <div class="ops-card">
            <h4>${esc(s.name||'-')}</h4>
            <div class="ops-stats">
              <span>📍 ${s.lat}, ${s.lng}</span>
              <span>${t('ads.stores.radius-label')}: ${s.radius_meters||500}m</span>
              <span>${t('ads.stores.game-label')}: ${esc(s.associated_game_slug||'-')}</span>
            </div>
          </div>
        `).join('') : `<p class="empty">${t('ads.stores.none')}</p>`;
      } catch(e) {
        document.getElementById('stores-list').innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
      }
    },

    async _newStore() {
      const name = prompt(t('ads.stores.name-prompt'));
      if (!name) return;
      const lat = parseFloat(prompt(t('ads.stores.lat-prompt'), '-6.2088'));
      const lng = parseFloat(prompt(t('ads.stores.lng-prompt'), '106.8456'));
      if (isNaN(lat) || isNaN(lng)) return showToast(t('ads.stores.coords-invalid'), 'error');
      const radius = parseInt(prompt(t('ads.stores.radius-prompt'), '500') || 500);
      const game = prompt(t('ads.stores.game-prompt'), 'match3') || 'match3';
      const storeId = 'store_' + Date.now();
      try {
        const res = await apiFetch('/api/v1/geofence/stores/register', {
          method: 'POST',
          json: {
            brand_id: state.brandId,
            store_id: storeId,
            name, lat, lng,
            radius_meters: radius,
            associated_game_slug: game,
            push_config: {
              enabled: true,
              cooldown_minutes: 60,
              hours_local: [9, 22],
              message_template: t('ads.stores.push-message', {brand: '{brand_name}', store: name})
            }
          }
        });
        if (!res.ok) return showToast(t('ads.stores.register-failed'), 'error');
        showToast(t('ads.stores.registered'));
        this._renderStores();
      } catch(e) {
        showToast(t('ads.stores.register-failed'), 'error');
      }
    },

    // ═══════════════════════════════════════════════════════════════════
    // REPORTS
    // ═══════════════════════════════════════════════════════════════════
    async _renderReports() {
      const pane = document.getElementById('ads-reports');
      pane.innerHTML = `
        <h4>${t('ads.reports.realtime')}</h4>
        <div class="kpi-grid">
          <div class="kpi-card"><div class="kpi-label">${t('ads.reports.impressions')}</div><div class="kpi-value" id="r-imp">-</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('ads.reports.clicks')}</div><div class="kpi-value" id="r-clk">-</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('ads.reports.conversions')}</div><div class="kpi-value" id="r-conv">-</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('ads.reports.spend')}</div><div class="kpi-value" id="r-spend">-</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('ads.reports.cac')}</div><div class="kpi-value" id="r-cac">-</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('ads.reports.roas')}</div><div class="kpi-value" id="r-roas">-</div></div>
        </div>
        <h4 style="margin-top:24px">${t('ads.reports.funnel')}</h4>
        <div class="funnel" id="r-funnel"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/campaigns/${state.brandId}`);
        if (!res.ok) return;
        const camps = await res.json();
        const arr = camps.campaigns || camps || [];
        const totals = arr.reduce((acc, c) => {
          const s = c.stats || {};
          acc.imp += s.impressions || 0;
          acc.clk += s.clicks || 0;
          acc.conv += s.conversions || 0;
          acc.spend += s.spend_cents || 0;
          return acc;
        }, {imp: 0, clk: 0, conv: 0, spend: 0});
        document.getElementById('r-imp').textContent = totals.imp;
        document.getElementById('r-clk').textContent = totals.clk;
        document.getElementById('r-conv').textContent = totals.conv;
        document.getElementById('r-spend').textContent = `¥${(totals.spend/100).toFixed(2)}`;
        document.getElementById('r-cac').textContent = totals.conv ? `¥${(totals.spend/100/totals.conv).toFixed(2)}` : '-';
        document.getElementById('r-roas').textContent = totals.conv ? `${(totals.conv * 100 / Math.max(totals.spend/100, 1)).toFixed(1)}x` : '-';
        const clkPct = totals.imp ? Math.min(100, totals.clk/totals.imp*100) : 0;
        const convPct = totals.imp ? Math.min(100, totals.conv/totals.imp*100) : 0;
        document.getElementById('r-funnel').innerHTML = `
          <div class="funnel-step" style="width:100%">${t('ads.reports.impressions')}: ${totals.imp}</div>
          <div class="funnel-step" style="width:${clkPct||5}%">${t('ads.reports.clicks')}: ${totals.clk} (${clkPct.toFixed(0)}%)</div>
          <div class="funnel-step" style="width:${convPct||5}%">${t('ads.reports.conversions')}: ${totals.conv} (${convPct.toFixed(1)}%)</div>
        `;
      } catch(e) {
        // silent
      }
    },

    // ═══════════════════════════════════════════════════════════════════
    // ATTRIBUTION
    // ═══════════════════════════════════════════════════════════════════
    async _renderAttribution() {
      const pane = document.getElementById('ads-attribution');
      pane.innerHTML = `
        <h4>${t('ads.attribution.title')}</h4>
        <p class="mon-info">${t('ads.attribution.info')}</p>
        <div class="mon-toolbar">
          <button class="btn-outline" onclick="AdsView._loadIncoming()">${t('ads.attribution.incoming-button')}</button>
          <button class="btn-outline" onclick="AdsView._loadOutgoing()">${t('ads.attribution.outgoing-button')}</button>
        </div>
        <div id="attribution-list"></div>
      `;
      this._loadIncoming();
    },

    async _loadIncoming() {
      const list = document.getElementById('attribution-list');
      if (!list) return;
      list.innerHTML = `<p class="empty">${t('common.loading')}</p>`;
      try {
        const res = await apiFetch(`/api/v1/attribution/brand/${state.brandId}/incoming`);
        if (!res.ok) { list.innerHTML = `<p class="empty">${t('common.no-data')}</p>`; return; }
        const data = await res.json();
        const arr = data.events || data || [];
        list.innerHTML = arr.length ? `
          <table class="data-table">
            <thead><tr><th>${t('common.time')}</th><th>${t('ads.attribution.col-source')}</th><th>${t('common.user')}</th><th>${t('common.stage')}</th><th>${t('common.amount')}</th></tr></thead>
            <tbody>${arr.map(e => `
              <tr>
                <td>${e.timestamp ? new Date(e.timestamp*1000).toLocaleString() : '-'}</td>
                <td>${esc(e.source_brand||'-')}</td>
                <td>${esc((e.user_id||'').slice(0,12))}</td>
                <td>${esc(e.stage||'-')}</td>
                <td>${e.value_cents?`¥${(e.value_cents/100).toFixed(2)}`:'-'}</td>
              </tr>
            `).join('')}</tbody>
          </table>
        ` : `<p class="empty">${t('ads.attribution.none-incoming')}</p>`;
      } catch(e) {
        list.innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
      }
    },

    async _loadOutgoing() {
      const list = document.getElementById('attribution-list');
      if (!list) return;
      list.innerHTML = `<p class="empty">${t('common.loading')}</p>`;
      try {
        const res = await apiFetch(`/api/v1/attribution/brand/${state.brandId}/outgoing`);
        if (!res.ok) { list.innerHTML = `<p class="empty">${t('ads.attribution.none-outgoing')}</p>`; return; }
        const data = await res.json();
        const arr = data.events || data || [];
        list.innerHTML = arr.length ? `
          <table class="data-table">
            <thead><tr><th>${t('common.time')}</th><th>${t('ads.attribution.col-target')}</th><th>${t('common.user')}</th><th>${t('common.stage')}</th></tr></thead>
            <tbody>${arr.map(e => `
              <tr>
                <td>${e.timestamp ? new Date(e.timestamp*1000).toLocaleString() : '-'}</td>
                <td>${esc(e.target_brand||'-')}</td>
                <td>${esc((e.user_id||'').slice(0,12))}</td>
                <td>${esc(e.stage||'-')}</td>
              </tr>
            `).join('')}</tbody>
          </table>
        ` : `<p class="empty">${t('ads.attribution.none-outgoing')}</p>`;
      } catch(e) {
        list.innerHTML = `<p class="empty">${t('common.load-failed')}</p>`;
      }
    }
  };

  window.AdsView = AdsView;
})();
