(function() {
  'use strict';

  const t = (key, opts) => {
    if (window.i18next && typeof window.i18next.t === 'function') {
      return window.i18next.t('portal-sdk:' + key, opts);
    }
    return key;
  };

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
        <h2 class="page-title">${t('analytics.title')}</h2>
        <p class="page-subtitle">${t('analytics.subtitle')}</p>

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
        <div class="section-title">${t('analytics.overview')}</div>
        <div class="kpi-grid">
          <div class="kpi-card"><div class="kpi-label">${t('analytics.active-modules')}</div><div class="kpi-value">${activeModules}</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('analytics.vouchers-issued')}</div><div class="kpi-value">${claimed}</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('analytics.redeemed')}</div><div class="kpi-value">${redeemed}</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('analytics.redemption-rate')}</div><div class="kpi-value">${(redemptionRate * 100).toFixed(1)}%</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('analytics.viral-k')}</div><div class="kpi-value">${coef.toFixed(2)}</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('analytics.invited')}</div><div class="kpi-value">${invited}</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('analytics.converted')}</div><div class="kpi-value">${converted}</div></div>
          <div class="kpi-card"><div class="kpi-label">${t('analytics.conv-rate')}</div><div class="kpi-value">${invited ? (converted / invited * 100).toFixed(1) : '0.0'}%</div></div>
        </div>
      `;
    },

    _renderViral(root, viral) {
      const section = root.querySelector('#analytics-viral');
      const triggers = viral.triggers || {};
      const entries = Object.entries(triggers);
      section.innerHTML = `
        <div class="section-title">${t('analytics.viral-triggers')}</div>
        <table class="data-table">
          <thead><tr><th>${t('analytics.col-trigger')}</th><th>${t('analytics.invited')}</th><th>${t('analytics.converted')}</th><th>${t('analytics.col-k')}</th></tr></thead>
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
            }).join('') : `<tr><td colspan="4" class="empty-row">${t('analytics.no-viral')}</td></tr>`}
          </tbody>
        </table>
      `;
    },

    async _renderConditionsUsage(root, modules) {
      const section = root.querySelector('#analytics-conditions');
      section.innerHTML = `
        <div class="section-title">${t('analytics.conditions-usage')}</div>
        <p class="empty">${t('common.loading')}</p>
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
        <div class="section-title">${t('analytics.conditions-usage')}</div>
        ${valid.length ? `
          <table class="data-table">
            <thead><tr><th>${t('analytics.col-module')}</th><th>${t('analytics.col-claims')}</th><th>${t('analytics.col-users')}</th><th>${t('analytics.col-conv')}</th><th>${t('analytics.col-blockers')}</th><th>${t('analytics.col-24h')}</th></tr></thead>
            <tbody>
              ${valid.map(u => {
                const blockers = (u.top_blockers || []).slice(0, 3).map(b => {
                  const key = Array.isArray(b) ? b[0] : (b && b.condition) || '?';
                  return esc(String(key));
                }).join(', ') || t('analytics.no-blockers');
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
        ` : `<p class="empty">${t('analytics.no-conditions')}</p>`}
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
          <div class="section-title">${t('analytics.active-modules-count', {count: 0})}</div>
          <p class="empty">${t('analytics.no-modules')}</p>
        `;
        return;
      }
      section.innerHTML = `
        <div class="section-title">${t('analytics.active-modules-count', {count: enabled.length})}</div>
        <table class="data-table">
          <thead><tr><th>${t('analytics.col-module')}</th><th>${t('analytics.col-today')}</th><th>${t('analytics.col-week')}</th><th>${t('analytics.col-month')}</th><th>Top User</th><th>Avg Value</th></tr></thead>
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
        <div class="section-title">${t('analytics.voucher-funnel')}</div>
        <div class="funnel">
          <div class="funnel-step" style="width:100%"><span>${t('analytics.issued')}: ${issued}</span></div>
          <div class="funnel-step" style="width:${Math.max(8, pctValidated)}%"><span>${t('analytics.validated')}: ${validated} (${pctValidated.toFixed(0)}%)</span></div>
          <div class="funnel-step" style="width:${Math.max(8, pctRedeemed)}%"><span>${t('analytics.redeemed')}: ${redeemed} (${pctRedeemed.toFixed(0)}%)</span></div>
        </div>
        <div class="funnel-dropoff">
          <span>↓ ${t('analytics.drop-issue-validate')}: <strong>${dropValidation.toFixed(1)}%</strong></span>
          <span>↓ ${t('analytics.drop-validate-redeem')}: <strong>${dropRedemption.toFixed(1)}%</strong></span>
        </div>
        <div class="kpi-card" style="margin-top:12px;max-width:240px">
          <div class="kpi-label">${t('analytics.total-savings')}</div>
          <div class="kpi-value">¥${((vouchers.total_savings_value || 0) / 100).toFixed(2)}</div>
        </div>
      `;
    },

    async _renderActivityFeed(root) {
      const section = root.querySelector('#analytics-activity');
      section.innerHTML = `
        <div class="section-title">${t('analytics.recent-activity')}</div>
        <p class="empty" id="activity-list">${t('common.loading')}</p>
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
          list.textContent = t('analytics.no-activity');
        }
      } catch (e) {
        const list = section.querySelector('#activity-list');
        if (list) list.textContent = t('analytics.load-failed');
      }
    }
  };

  window.AnalyticsView = AnalyticsView;
})();
