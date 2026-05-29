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
  const AdsView = {
    async render() {
      const root = document.getElementById('view-ads');
      if (!root) return;
      root.innerHTML = `
        <h2 class="page-title">广告 / Ads Manager</h2>
        <p class="page-subtitle">Google Ads 风格 — 充值、创建、监控、归因</p>
        <div class="tabs">
          <button class="tab active" data-tab="wallet">💰 钱包</button>
          <button class="tab" data-tab="campaigns">📢 活动</button>
          <button class="tab" data-tab="stores">📍 门店地理</button>
          <button class="tab" data-tab="reports">📊 报表</button>
          <button class="tab" data-tab="attribution">🔍 归因</button>
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
          ['wallet','campaigns','stores','reports','attribution'].forEach(t => {
            document.getElementById(`ads-${t}`).style.display = t===b.dataset.tab?'block':'none';
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
      pane.innerHTML = '<div class="ads-loading">加载中...</div>';
      try {
        const res = await apiFetch(`/api/v1/wallet/${state.brandId}`);
        if (!res.ok) throw new Error('Wallet load failed');
        const w = await res.json();
        pane.innerHTML = `
          <div class="wallet-summary">
            <div class="wallet-balance">
              <div class="wallet-label">当前余额 / Balance</div>
              <div class="wallet-value">¥${((w.balance_cents||0)/100).toFixed(2)}</div>
              <small>${esc(w.currency || 'CNY')}</small>
            </div>
            <div class="wallet-stat">
              <div class="wallet-label">今日消耗 / Today</div>
              <div class="wallet-value">¥${((w.daily_spent_cents||0)/100).toFixed(2)}</div>
              <small>预算: ¥${((w.daily_budget_cents||0)/100).toFixed(2)}</small>
            </div>
            <div class="wallet-stat">
              <div class="wallet-label">累计消耗 / Total Spent</div>
              <div class="wallet-value">¥${((w.total_spent_cents||0)/100).toFixed(2)}</div>
            </div>
          </div>
          <div class="wallet-actions">
            <button class="btn-primary" onclick="AdsView._topup()">+ 充值 Top Up</button>
            <button class="btn-outline" onclick="AdsView._setBudget()">设置日预算</button>
            <button class="btn-outline" onclick="AdsView._configAutoRecharge()">自动充值设置</button>
          </div>
          <h4 style="margin-top:24px">最近交易 / Recent Transactions</h4>
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
                <thead><tr><th>时间</th><th>类型</th><th>金额</th><th>原因</th></tr></thead>
                <tbody>
                  ${arr.map(t => {
                    const amt = (t.amount_cents != null ? t.amount_cents : (t.amount || 0));
                    const ts = t.ts || t.timestamp || 0;
                    return `
                      <tr>
                        <td>${ts ? new Date(ts*1000).toLocaleString() : '-'}</td>
                        <td>${esc(t.type||t.reason||'?')}</td>
                        <td class="${amt>0?'pos':'neg'}">${amt>0?'+':''}¥${(amt/100).toFixed(2)}</td>
                        <td>${esc(t.reason||t.reference_id||'-')}</td>
                      </tr>
                    `;
                  }).join('')}
                </tbody>
              </table>
            ` : '<p class="empty">暂无交易 / No transactions</p>';
          } else {
            document.getElementById('wallet-tx').innerHTML = '<p class="empty">暂无交易</p>';
          }
        } catch(e) {
          document.getElementById('wallet-tx').innerHTML = '<p class="empty">暂无交易</p>';
        }
      } catch(e) {
        pane.innerHTML = `
          <p class="empty">钱包未初始化。点击 + 充值 开始 / Wallet not initialized.</p>
          <div class="wallet-actions">
            <button class="btn-primary" onclick="AdsView._topup()">+ 充值 Top Up</button>
          </div>
        `;
      }
    },

    async _topup() {
      const amount = parseFloat(prompt('充值金额 (元) / Top up amount (CNY)', '1000') || 0);
      if (!amount || amount <= 0) return;
      const method = prompt('支付方式 / Payment method (alipay/wechat/stripe/paypal)', 'wechat') || 'wechat';
      try {
        const res = await apiFetch(`/api/v1/wallet/${state.brandId}/topup`, {
          method: 'POST',
          json: {amount_cents: Math.round(amount * 100), payment_method: method}
        });
        if (!res.ok) return showToast('充值失败 / Top up failed', 'error');
        const data = await res.json();
        if (data.status === 'pending' && data.topup_id) {
          if (confirm(`订单 ${data.topup_id} 待支付。MVP 模式 — 是否模拟支付成功？`)) {
            await apiFetch(`/api/v1/wallet/${state.brandId}/topup/${data.topup_id}/confirm`, {
              method: 'POST',
              json: {payment_gateway_response: {mock: true}}
            });
            showToast('充值完成 / Top up complete');
          } else {
            showToast('订单已创建，待支付');
          }
        } else {
          showToast('充值完成 / Top up complete');
        }
        this._renderWallet();
      } catch(e) {
        showToast('充值失败 / Top up failed', 'error');
      }
    },

    async _setBudget() {
      const raw = prompt('每日预算 (元，0 = 不限) / Daily budget', '500');
      if (raw == null) return;
      const amount = parseFloat(raw) || 0;
      try {
        const res = await apiFetch(`/api/v1/wallet/${state.brandId}/daily-budget`, {
          method: 'POST',
          json: {daily_budget_cents: Math.round(amount * 100)}
        });
        if (!res.ok) return showToast('设置失败 / Failed', 'error');
        showToast('日预算已设置 / Daily budget set');
        this._renderWallet();
      } catch(e) {
        showToast('设置失败', 'error');
      }
    },

    async _configAutoRecharge() {
      const enabled = confirm('开启自动充值 / Enable auto-recharge?');
      if (!enabled) return;
      const threshold = parseFloat(prompt('余额低于多少元时充值 / Trigger threshold (CNY)', '500') || 500);
      const amount = parseFloat(prompt('每次充值多少元 / Recharge amount (CNY)', '5000') || 5000);
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
        if (!res.ok) return showToast('设置失败', 'error');
        showToast('已开启自动充值 / Auto-recharge enabled');
      } catch(e) {
        showToast('设置失败', 'error');
      }
    },

    // ═══════════════════════════════════════════════════════════════════
    // CAMPAIGNS
    // ═══════════════════════════════════════════════════════════════════
    async _renderCampaigns() {
      const pane = document.getElementById('ads-campaigns');
      pane.innerHTML = `
        <button class="btn-primary" onclick="AdsView._newCampaign()">+ 新建活动 / New Campaign</button>
        <div id="campaigns-list" style="margin-top:16px"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/campaigns/${state.brandId}`);
        if (!res.ok) {
          document.getElementById('campaigns-list').innerHTML = '<p class="empty">还没有活动 / No campaigns yet</p>';
          return;
        }
        const list = await res.json();
        const arr = list.campaigns || list || [];
        document.getElementById('campaigns-list').innerHTML = arr.length ? `
          <table class="data-table campaign-table">
            <thead><tr><th>名称</th><th>目标</th><th>出价</th><th>预算</th><th>状态</th><th>展示</th><th>转化</th><th>消耗</th><th>操作</th></tr></thead>
            <tbody>
              ${arr.map(c => {
                const status = c.status || 'active';
                const stats = c.stats || {};
                return `
                  <tr>
                    <td><strong>${esc(c.name||'-')}</strong></td>
                    <td>${esc(c.objective||'-')}</td>
                    <td>${esc(c.bid_strategy||'-')} ¥${((c.max_bid_cents||0)/100).toFixed(2)}</td>
                    <td>¥${((c.daily_budget_cents||0)/100).toFixed(0)}/日</td>
                    <td><span class="status-${esc(status)}">${esc(status)}</span></td>
                    <td>${stats.impressions||0}</td>
                    <td>${stats.conversions||0}</td>
                    <td>¥${((stats.spend_cents||0)/100).toFixed(2)}</td>
                    <td>
                      ${status==='paused'
                        ? `<button class="btn-mini" onclick="AdsView._resume('${esc(c.campaign_id||'')}')">恢复</button>`
                        : `<button class="btn-mini" onclick="AdsView._pause('${esc(c.campaign_id||'')}')">暂停</button>`}
                    </td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        ` : '<p class="empty">还没有活动。新建第一个！/ No campaigns yet — create your first.</p>';
      } catch(e) {
        document.getElementById('campaigns-list').innerHTML = '<p class="empty">加载失败 / Failed to load</p>';
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
            <div class="modal-head"><h3>新建广告活动 / New Campaign</h3><button onclick="AdsView._closeCampaignModal()">✕</button></div>
            <div class="modal-body">
              <label>活动名称<input id="c-name" placeholder="如：印尼新客拉新" class="form-input"></label>
              <label>目标<select id="c-objective" class="form-input">
                <option value="acquire">拉新（CPA）/ Acquire</option>
                <option value="sales">销售提成（CPS）/ Sales</option>
                <option value="awareness">曝光（CPM）/ Awareness</option>
                <option value="geo_visit">到店（CPV）/ Store Visit</option>
              </select></label>
              <label>出价方式<select id="c-bid" class="form-input">
                <option value="cpa">每个新客 CPA</option>
                <option value="cps">每笔订单比例 CPS</option>
                <option value="cpm">每千次曝光 CPM</option>
                <option value="cpv">每次到店 CPV</option>
              </select></label>
              <label>最高出价 (元) / Max Bid<input id="c-max-bid" type="number" value="20" step="0.5" class="form-input"></label>
              <label>每日预算 (元) / Daily Budget<input id="c-daily" type="number" value="500" class="form-input"></label>
              <h4>定向 / Targeting</h4>
              <label>国家 / Country<input id="c-country" placeholder="如：ID（印尼）" class="form-input"></label>
              <label>城市 / City<input id="c-city" placeholder="如：Jakarta" class="form-input"></label>
              <label>半径 (km，限地理目标) / Radius<input id="c-radius" type="number" value="5" class="form-input"></label>
              <label>年龄范围 / Age Range<input id="c-age-min" type="number" placeholder="18" class="form-input" style="width:80px"> - <input id="c-age-max" type="number" placeholder="65" class="form-input" style="width:80px"></label>
              <h4>创意素材 / Creative</h4>
              <label>使用 Recipe<input id="c-recipe" placeholder="如：starbucks_loyalty" class="form-input"></label>
              <label>或游戏 slug / Game Slug<input id="c-game" placeholder="如：match3" class="form-input"></label>
              <label>关联优惠券模板 / Voucher Template<input id="c-voucher" placeholder="可选 / Optional" class="form-input"></label>
              <h4>调度 / Schedule</h4>
              <label>开始时间 / Start<input id="c-start" type="datetime-local" class="form-input"></label>
              <label>结束时间 / End<input id="c-end" type="datetime-local" class="form-input"></label>
            </div>
            <div class="modal-foot">
              <button class="btn-outline" onclick="AdsView._closeCampaignModal()">取消 Cancel</button>
              <button class="btn-primary" onclick="AdsView._saveCampaign()">创建并启动 / Launch</button>
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
        name: val('c-name') || '未命名活动',
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
        if (!res.ok) return showToast('创建失败 / Create failed', 'error');
        showToast('活动已启动 / Campaign launched');
        this._closeCampaignModal();
        this._renderCampaigns();
      } catch(e) {
        showToast('创建失败', 'error');
      }
    },

    async _pause(cid) {
      if (!cid) return;
      try {
        await apiFetch(`/api/v1/campaigns/${cid}/pause`, {method:'POST'});
        showToast('已暂停 / Paused');
        this._renderCampaigns();
      } catch(e) { showToast('操作失败', 'error'); }
    },
    async _resume(cid) {
      if (!cid) return;
      try {
        await apiFetch(`/api/v1/campaigns/${cid}/resume`, {method:'POST'});
        showToast('已恢复 / Resumed');
        this._renderCampaigns();
      } catch(e) { showToast('操作失败', 'error'); }
    },

    // ═══════════════════════════════════════════════════════════════════
    // STORES (Geofencing)
    // ═══════════════════════════════════════════════════════════════════
    async _renderStores() {
      const pane = document.getElementById('ads-stores');
      pane.innerHTML = `
        <button class="btn-primary" onclick="AdsView._newStore()">+ 注册门店 / Register Store</button>
        <p class="mon-info">门店地理围栏：用户进入半径范围 → 自动推送游戏 → 拉到店 / Geofence pushes game when user enters radius.</p>
        <div id="stores-list" style="margin-top:16px"></div>
      `;
      try {
        const res = await apiFetch(`/api/v1/geofence/stores/${state.brandId}`);
        if (!res.ok) {
          document.getElementById('stores-list').innerHTML = '<p class="empty">还没有门店 / No stores</p>';
          return;
        }
        const data = await res.json();
        const arr = data.stores || data || [];
        document.getElementById('stores-list').innerHTML = arr.length ? arr.map(s => `
          <div class="ops-card">
            <h4>${esc(s.name||'-')}</h4>
            <div class="ops-stats">
              <span>📍 ${s.lat}, ${s.lng}</span>
              <span>半径 / Radius: ${s.radius_meters||500}m</span>
              <span>关联游戏 / Game: ${esc(s.associated_game_slug||'-')}</span>
            </div>
          </div>
        `).join('') : '<p class="empty">还没有注册门店 / No stores registered</p>';
      } catch(e) {
        document.getElementById('stores-list').innerHTML = '<p class="empty">加载失败 / Failed to load</p>';
      }
    },

    async _newStore() {
      const name = prompt('门店名称 / Store name (如: 雅加达中央店)');
      if (!name) return;
      const lat = parseFloat(prompt('纬度 / Latitude (如: -6.2088)', '-6.2088'));
      const lng = parseFloat(prompt('经度 / Longitude (如: 106.8456)', '106.8456'));
      if (isNaN(lat) || isNaN(lng)) return showToast('经纬度无效', 'error');
      const radius = parseInt(prompt('围栏半径 米 / Radius (m)', '500') || 500);
      const game = prompt('关联游戏 slug / Associated game slug (如: match3)', 'match3') || 'match3';
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
              message_template: `你在 {brand_name} ${name} 附近！玩游戏拿优惠券 ☕`
            }
          }
        });
        if (!res.ok) return showToast('注册失败 / Register failed', 'error');
        showToast('门店已注册 / Store registered');
        this._renderStores();
      } catch(e) {
        showToast('注册失败', 'error');
      }
    },

    // ═══════════════════════════════════════════════════════════════════
    // REPORTS
    // ═══════════════════════════════════════════════════════════════════
    async _renderReports() {
      const pane = document.getElementById('ads-reports');
      pane.innerHTML = `
        <h4>实时数据 / Real-time Stats</h4>
        <div class="kpi-grid">
          <div class="kpi-card"><div class="kpi-label">总曝光 / Impressions</div><div class="kpi-value" id="r-imp">-</div></div>
          <div class="kpi-card"><div class="kpi-label">总点击 / Clicks</div><div class="kpi-value" id="r-clk">-</div></div>
          <div class="kpi-card"><div class="kpi-label">总转化 / Conversions</div><div class="kpi-value" id="r-conv">-</div></div>
          <div class="kpi-card"><div class="kpi-label">总消耗 / Spend</div><div class="kpi-value" id="r-spend">-</div></div>
          <div class="kpi-card"><div class="kpi-label">平均 CAC</div><div class="kpi-value" id="r-cac">-</div></div>
          <div class="kpi-card"><div class="kpi-label">ROAS</div><div class="kpi-value" id="r-roas">-</div></div>
        </div>
        <h4 style="margin-top:24px">漏斗 / Funnel</h4>
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
          <div class="funnel-step" style="width:100%">曝光 / Impressions: ${totals.imp}</div>
          <div class="funnel-step" style="width:${clkPct||5}%">点击 / Clicks: ${totals.clk} (${clkPct.toFixed(0)}%)</div>
          <div class="funnel-step" style="width:${convPct||5}%">转化 / Conversions: ${totals.conv} (${convPct.toFixed(1)}%)</div>
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
        <h4>归因追踪 / Attribution Tracking</h4>
        <p class="mon-info">查看带新单的具体归因链路 / View the attribution path of acquired users.</p>
        <div class="mon-toolbar">
          <button class="btn-outline" onclick="AdsView._loadIncoming()">收到的归因（别人带给我）/ Incoming</button>
          <button class="btn-outline" onclick="AdsView._loadOutgoing()">发出的归因（我带给别人）/ Outgoing</button>
        </div>
        <div id="attribution-list"></div>
      `;
      this._loadIncoming();
    },

    async _loadIncoming() {
      const list = document.getElementById('attribution-list');
      if (!list) return;
      list.innerHTML = '<p class="empty">加载中...</p>';
      try {
        const res = await apiFetch(`/api/v1/attribution/brand/${state.brandId}/incoming`);
        if (!res.ok) { list.innerHTML = '<p class="empty">暂无归因数据 / No data</p>'; return; }
        const data = await res.json();
        const arr = data.events || data || [];
        list.innerHTML = arr.length ? `
          <table class="data-table">
            <thead><tr><th>时间</th><th>来源品牌 / Source</th><th>用户</th><th>阶段 / Stage</th><th>金额</th></tr></thead>
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
        ` : '<p class="empty">暂无归因 / No incoming attribution</p>';
      } catch(e) {
        list.innerHTML = '<p class="empty">加载失败 / Failed to load</p>';
      }
    },

    async _loadOutgoing() {
      const list = document.getElementById('attribution-list');
      if (!list) return;
      list.innerHTML = '<p class="empty">加载中...</p>';
      try {
        const res = await apiFetch(`/api/v1/attribution/brand/${state.brandId}/outgoing`);
        if (!res.ok) { list.innerHTML = '<p class="empty">暂无 / No data</p>'; return; }
        const data = await res.json();
        const arr = data.events || data || [];
        list.innerHTML = arr.length ? `
          <table class="data-table">
            <thead><tr><th>时间</th><th>目标品牌 / Target</th><th>用户</th><th>阶段 / Stage</th></tr></thead>
            <tbody>${arr.map(e => `
              <tr>
                <td>${e.timestamp ? new Date(e.timestamp*1000).toLocaleString() : '-'}</td>
                <td>${esc(e.target_brand||'-')}</td>
                <td>${esc((e.user_id||'').slice(0,12))}</td>
                <td>${esc(e.stage||'-')}</td>
              </tr>
            `).join('')}</tbody>
          </table>
        ` : '<p class="empty">暂无 / No outgoing attribution</p>';
      } catch(e) {
        list.innerHTML = '<p class="empty">加载失败 / Failed to load</p>';
      }
    }
  };

  window.AdsView = AdsView;
})();
