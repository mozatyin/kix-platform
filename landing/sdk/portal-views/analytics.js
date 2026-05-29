(function() {
  'use strict';

  const AnalyticsView = {
    async render() {
      const root = document.getElementById('view-analytics');
      if (!root) return;

      root.innerHTML = this._skeletonHTML();

      // Parallel data fetches
      const [viral, modules, vouchers] = await Promise.all([
        this._fetchViralStats(),
        this._fetchModulesData(),
        this._fetchVoucherStats()
      ]);

      this._renderOverview(root, viral, modules, vouchers);
      this._renderViral(root, viral);
      this._renderConditionsUsage(root, modules);
      this._renderModuleTable(root, modules);
      this._renderVoucherFunnel(root, vouchers);
      this._renderActivityFeed(root);
    },

    async _fetchViralStats() {
      try {
        const res = await apiFetch(`/api/v1/network/${state.brandId}/viral-stats`);
        if (res.ok) return await res.json();
      } catch (e) {}
      return { triggers: {}, overall: { invited: 0, converted: 0, coefficient: 0 } };
    },

    async _fetchModulesData() {
      try {
        const res = await apiFetch(`/api/v1/brands/${state.brandId}/modules`);
        if (res.ok) {
          const data = await res.json();
          // Normalize: API may return {modules: [...]} or [...]
          if (Array.isArray(data)) return data;
          if (Array.isArray(data.modules)) return data.modules;
          // Object map → array
          if (data && typeof data === 'object') {
            return Object.entries(data).map(([id, cfg]) => ({ id, ...(cfg || {}) }));
          }
        }
      } catch (e) {}
      return [];
    },

    async _fetchVoucherStats() {
      try {
        const res = await apiFetch(`/api/v1/commerce/analytics/${state.brandId}`);
        if (res.ok) return await res.json();
      } catch (e) {}
      return { coupons_claimed: 0, coupons_redeemed: 0, redemption_rate: 0, total_savings_value: 0 };
    },

    _skeletonHTML() {
      return `
        <h2 class="page-title">Analytics / 数据分析</h2>
        <p class="page-subtitle">所有 gamification 活动的实时数据 / Real-time stats across all modules</p>

        <div id="analytics-overview" class="section"></div>
        <div id="analytics-viral" class="section"></div>
        <div id="analytics-conditions" class="section"></div>
        <div id="analytics-modules" class="section"></div>
        <div id="analytics-vouchers" class="section"></div>
        <div id="analytics-activity" class="section"></div>
      `;
    },

    _renderOverview(root, viral, modules, vouchers) {
      const section = root.querySelector('#analytics-overview');
      const activeModules = modules.filter(m => m.enabled).length;
      const coef = (viral.overall && viral.overall.coefficient) || 0;
      const invited = (viral.overall && viral.overall.invited) || 0;
      const converted = (viral.overall && viral.overall.converted) || 0;
      const claimed = vouchers.coupons_claimed || 0;
      const redeemed = vouchers.coupons_redeemed || 0;
      const redemptionRate = vouchers.redemption_rate || 0;
      section.innerHTML = `
        <div class="section-title">概览 / Overview</div>
        <div class="kpi-grid">
          <div class="kpi-card"><div class="kpi-label">已启用模块 / Active Modules</div><div class="kpi-value">${activeModules}</div></div>
          <div class="kpi-card"><div class="kpi-label">优惠券发放 / Vouchers Issued</div><div class="kpi-value">${claimed}</div></div>
          <div class="kpi-card"><div class="kpi-label">已核销 / Redeemed</div><div class="kpi-value">${redeemed}</div></div>
          <div class="kpi-card"><div class="kpi-label">核销率 / Redemption</div><div class="kpi-value">${(redemptionRate * 100).toFixed(1)}%</div></div>
          <div class="kpi-card"><div class="kpi-label">病毒系数 K / Viral K</div><div class="kpi-value">${coef.toFixed(2)}</div></div>
          <div class="kpi-card"><div class="kpi-label">总邀请 / Invited</div><div class="kpi-value">${invited}</div></div>
          <div class="kpi-card"><div class="kpi-label">转化 / Converted</div><div class="kpi-value">${converted}</div></div>
          <div class="kpi-card"><div class="kpi-label">转化率 / Conv. Rate</div><div class="kpi-value">${invited ? (converted / invited * 100).toFixed(1) : '0.0'}%</div></div>
        </div>
      `;
    },

    _renderViral(root, viral) {
      const section = root.querySelector('#analytics-viral');
      const triggers = viral.triggers || {};
      const entries = Object.entries(triggers);
      section.innerHTML = `
        <div class="section-title">病毒触发器 / Viral Triggers</div>
        <table class="data-table">
          <thead><tr><th>Trigger</th><th>邀请 / Invited</th><th>转化 / Converted</th><th>系数 K</th></tr></thead>
          <tbody>
            ${entries.length ? entries.map(([name, stats]) => {
              const inv = stats.invited || 0;
              const conv = stats.converted || 0;
              const k = stats.coefficient || 0;
              return `
                <tr>
                  <td>${esc(name)}</td>
                  <td>${inv}</td>
                  <td>${conv}</td>
                  <td><strong style="color:${k >= 0.5 ? 'var(--green)' : 'var(--text)'}">${k.toFixed(2)}</strong></td>
                </tr>
              `;
            }).join('') : '<tr><td colspan="4" class="empty-row">暂无病毒数据 / No viral data yet</td></tr>'}
          </tbody>
        </table>
      `;
    },

    async _renderConditionsUsage(root, modules) {
      const section = root.querySelector('#analytics-conditions');
      section.innerHTML = `
        <div class="section-title">条件门控统计 / Conditions Usage</div>
        <p class="empty">Loading...</p>
      `;
      const enabledIds = modules.filter(m => m.enabled).map(m => m.id).filter(Boolean);
      const usages = await Promise.all(enabledIds.map(async (id) => {
        try {
          const res = await apiFetch(`/api/v1/conditions/campaigns/module:${state.brandId}:${id}/usage`);
          if (res.ok) {
            const data = await res.json();
            return { id, ...data };
          }
        } catch (e) {}
        return null;
      }));
      const valid = usages.filter(u => u && (u.total_claims || 0) > 0);
      section.innerHTML = `
        <div class="section-title">条件门控统计 / Conditions Usage</div>
        ${valid.length ? `
          <table class="data-table">
            <thead><tr><th>模块 / Module</th><th>总尝试 / Claims</th><th>独立用户 / Users</th><th>转化率 / Conv.</th><th>主要拦截 / Top Blockers</th><th>近24h / 24h</th></tr></thead>
            <tbody>
              ${valid.map(u => {
                const blockers = (u.top_blockers || []).slice(0, 3).map(b => {
                  const key = Array.isArray(b) ? b[0] : (b && b.condition) || '?';
                  return esc(String(key));
                }).join(', ') || '无';
                const hourly = u.hourly_distribution || [];
                return `
                  <tr>
                    <td>${esc(u.id)}</td>
                    <td>${u.total_claims || 0}</td>
                    <td>${u.unique_users || 0}</td>
                    <td>${((u.conversion_rate || 0) * 100).toFixed(1)}%</td>
                    <td>${blockers}</td>
                    <td>${AnalyticsView._sparkline(hourly)}</td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        ` : '<p class="empty">暂无门控活动 / No condition-gated activity yet</p>'}
      `;
    },

    _sparkline(values) {
      if (!Array.isArray(values) || !values.length) return '<span class="spark-empty">—</span>';
      const max = Math.max(1, ...values);
      const w = 80, h = 18, n = values.length;
      const pts = values.map((v, i) => {
        const x = (i / Math.max(1, n - 1)) * w;
        const y = h - (v / max) * h;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(' ');
      return `<svg class="sparkline" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="var(--green)" stroke-width="1.5"/></svg>`;
    },

    _renderModuleTable(root, modules) {
      const section = root.querySelector('#analytics-modules');
      const enabled = modules.filter(m => m.enabled);
      if (!enabled.length) {
        section.innerHTML = `
          <div class="section-title">已启用模块 / Active Modules (0)</div>
          <p class="empty">尚未启用任何模块 / No modules enabled yet</p>
        `;
        return;
      }
      section.innerHTML = `
        <div class="section-title">已启用模块 / Active Modules (${enabled.length})</div>
        <table class="data-table">
          <thead><tr><th>Module</th><th>今日 / Today</th><th>本周 / Week</th><th>本月 / Month</th><th>Top User</th><th>Avg Value</th></tr></thead>
          <tbody>
            ${enabled.map(m => {
              const stats = m.stats || {};
              return `
                <tr>
                  <td>
                    <strong>${esc(m.id || '')}</strong>
                    <div class="mini-label">${m.params ? Object.keys(m.params).length + ' params' : 'default'} · conditions: ${m.conditions ? 'yes' : 'no'}</div>
                  </td>
                  <td>${stats.uses_today != null ? stats.uses_today : '—'}</td>
                  <td>${stats.uses_week != null ? stats.uses_week : '—'}</td>
                  <td>${stats.uses_month != null ? stats.uses_month : '—'}</td>
                  <td>${stats.top_user ? esc(String(stats.top_user).slice(0, 12)) : '—'}</td>
                  <td>${stats.avg_value != null ? Number(stats.avg_value).toFixed(2) : '—'}</td>
                </tr>
              `;
            }).join('')}
          </tbody>
        </table>
      `;
    },

    _renderVoucherFunnel(root, vouchers) {
      const section = root.querySelector('#analytics-vouchers');
      const issued = vouchers.coupons_claimed || 0;
      const validated = vouchers.coupons_validated != null ? vouchers.coupons_validated : issued;
      const redeemed = vouchers.coupons_redeemed || 0;
      const pctValidated = issued ? Math.min(100, validated / issued * 100) : 0;
      const pctRedeemed = issued ? Math.min(100, redeemed / issued * 100) : 0;
      const dropValidation = issued ? Math.max(0, 100 - pctValidated) : 0;
      const dropRedemption = validated ? Math.max(0, 100 - (redeemed / validated * 100)) : 0;
      section.innerHTML = `
        <div class="section-title">优惠券漏斗 / Voucher Funnel</div>
        <div class="funnel">
          <div class="funnel-step" style="width:100%"><span>发放 / Issued: ${issued}</span></div>
          <div class="funnel-step" style="width:${Math.max(8, pctValidated)}%"><span>验证 / Validated: ${validated} (${pctValidated.toFixed(0)}%)</span></div>
          <div class="funnel-step" style="width:${Math.max(8, pctRedeemed)}%"><span>核销 / Redeemed: ${redeemed} (${pctRedeemed.toFixed(0)}%)</span></div>
        </div>
        <div class="funnel-dropoff">
          <span>↓ 流失 / Drop-off (issue→validate): <strong>${dropValidation.toFixed(1)}%</strong></span>
          <span>↓ 流失 / Drop-off (validate→redeem): <strong>${dropRedemption.toFixed(1)}%</strong></span>
        </div>
        <div class="kpi-card" style="margin-top:12px;max-width:240px">
          <div class="kpi-label">总省金额 / Total Savings</div>
          <div class="kpi-value">¥${((vouchers.total_savings_value || 0) / 100).toFixed(2)}</div>
        </div>
      `;
    },

    async _renderActivityFeed(root) {
      const section = root.querySelector('#analytics-activity');
      section.innerHTML = `
        <div class="section-title">最近活动 / Recent Activity</div>
        <p class="empty" id="activity-list">Loading...</p>
      `;
      try {
        const moduleIds = ['reward_roulette', 'voucher_template', 'score_to_coupon', 'lucky_draw', 'streak_bonus', 'daily_checkin'];
        const audits = await Promise.all(moduleIds.map(mid =>
          apiFetch(`/api/v1/conditions/campaigns/module:${state.brandId}:${mid}/audit?limit=20`)
            .then(r => r.ok ? r.json() : { audit: [] })
            .catch(() => ({ audit: [] }))
        ));
        const all = audits
          .flatMap(a => a.audit || a.events || [])
          .sort((x, y) => (y.timestamp || 0) - (x.timestamp || 0))
          .slice(0, 50);
        const list = section.querySelector('#activity-list');
        if (all.length) {
          const ul = document.createElement('ul');
          ul.className = 'activity-list';
          ul.innerHTML = all.map(e => {
            const ts = e.timestamp ? new Date(e.timestamp * 1000).toLocaleString() : '—';
            const action = esc(e.action || e.event || 'event');
            const uid = esc(String(e.user_id || '?')).slice(0, 8);
            const blocked = e.blocked_by ? `<span class="blocked">blocked: ${esc(String(e.blocked_by))}</span>` : '<span class="success">✓</span>';
            return `<li><span class="time">${ts}</span><span class="event">${action}</span><span class="user">user: ${uid}</span>${blocked}</li>`;
          }).join('');
          list.replaceWith(ul);
        } else {
          list.textContent = '暂无活动数据 / No activity yet';
        }
      } catch (e) {
        const list = section.querySelector('#activity-list');
        if (list) list.textContent = '加载失败 / Load failed';
      }
    }
  };

  window.AnalyticsView = AnalyticsView;
})();
