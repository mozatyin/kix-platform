/* eslint-disable */
// ═══════════════════════════════════════════════════════════════════════
// Voucher Templates View
// Replaces the legacy CSV-only Vouchers view with a conditional-voucher
// template builder + issued-voucher inspector + legacy CSV tab.
// Depends on globals: state, apiFetch, esc, showToast, ConditionsBuilder
// ═══════════════════════════════════════════════════════════════════════
(function () {
  const t = (key, opts) => {
    if (window.i18next && typeof window.i18next.t === 'function') {
      return window.i18next.t('portal-sdk:' + key, opts);
    }
    return key;
  };

  const VoucherTemplatesView = {
    async render() {
      const root = document.getElementById('view-vouchers');
      if (!root) return;

      root.innerHTML = `
        <h2 class="page-title">${t('vouchers.title')}</h2>
        <p class="page-subtitle">${t('vouchers.subtitle')}</p>

        <div class="tabs">
          <button class="tab active" data-tab="templates">${t('vouchers.tab.templates')}</button>
          <button class="tab" data-tab="issued">${t('vouchers.tab.issued')}</button>
          <button class="tab" data-tab="csv">${t('vouchers.tab.csv')}</button>
        </div>

        <div id="voucher-templates-pane">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
            <h3 style="margin:0;font-size:14px;color:var(--text-dim)">${t('vouchers.existing')}</h3>
            <button class="btn-primary" id="new-voucher-tpl" style="width:auto;padding:9px 16px">${t('vouchers.new-button')}</button>
          </div>
          <div id="voucher-templates-list" class="recipes-grid"></div>
        </div>

        <div id="voucher-issued-pane" style="display:none">
          <div class="card" style="margin-bottom:14px">
            <div class="form-group" style="margin-bottom:0">
              <label>${t('common.user-id')}</label>
              <div style="display:flex;gap:8px">
                <input type="text" id="vi-user-id" class="form-input-sm" placeholder="${t('vouchers.user-id-placeholder')}" style="flex:1">
                <button class="btn-primary" id="vi-load-btn" style="width:auto;padding:9px 16px">${t('vouchers.load')}</button>
              </div>
            </div>
          </div>
          <div id="voucher-issued-list"></div>
        </div>

        <div id="voucher-csv-pane" style="display:none">
          <div class="card">
            <p style="color:var(--text-dim);font-size:13px;margin-bottom:12px">
              ${t('vouchers.csv-legacy-note')}
            </p>
            <div class="form-row">
              <div class="form-group" style="margin-bottom:0">
                <label>${t('vouchers.tier-label')}</label>
                <select id="voucher-tier" class="form-select">
                  <option value="bronze">${t('vouchers.tier.bronze')}</option>
                  <option value="silver">${t('vouchers.tier.silver')}</option>
                  <option value="gold">${t('vouchers.tier.gold')}</option>
                </select>
              </div>
              <div class="form-group" style="margin-bottom:0">
                <label>${t('vouchers.valid-days-label')}</label>
                <input type="number" id="voucher-valid-days" class="form-input-sm" value="30" min="1" max="365">
              </div>
            </div>
            <div class="form-group">
              <label>${t('vouchers.desc-label')}</label>
              <input type="text" id="voucher-desc" class="form-input-sm" placeholder="${t('vouchers.desc-placeholder')}">
            </div>
            <div class="upload-zone" id="upload-zone-tpl">
              <p>${t('vouchers.drop-csv')}</p>
              <input type="file" id="voucher-file-tpl" accept=".csv,.txt,text/csv">
            </div>
            <div id="upload-result-tpl" class="upload-result"></div>
          </div>
          <div class="section">
            <div class="section-title">${t('vouchers.pool')}</div>
            <div id="voucher-list-tpl"><div class="loading-center"><div class="spinner"></div></div></div>
          </div>
        </div>
      `;

      // Tab switching
      root.querySelectorAll('.tab').forEach((btn) => {
        btn.addEventListener('click', () => {
          root.querySelectorAll('.tab').forEach((b) => b.classList.remove('active'));
          btn.classList.add('active');
          const tab = btn.dataset.tab;
          ['templates', 'issued', 'csv'].forEach((tn) => {
            const pane = document.getElementById('voucher-' + tn + '-pane');
            if (pane) pane.style.display = tn === tab ? 'block' : 'none';
          });
          if (tab === 'templates') this._loadTemplates();
        });
      });

      document
        .getElementById('new-voucher-tpl')
        .addEventListener('click', () => this._openTemplateModal());

      const viBtn = document.getElementById('vi-load-btn');
      if (viBtn) {
        viBtn.addEventListener('click', () => {
          const uid = document.getElementById('vi-user-id').value.trim();
          if (!uid) return showToast(t('vouchers.enter-user-id'), 'error');
          this._loadIssued(uid);
        });
      }

      await this._loadTemplates();
    },

    _cacheKey() {
      return 'kix_voucher_tpls_' + (state.brandId || 'default');
    },

    async _loadTemplates() {
      const list = document.getElementById('voucher-templates-list');
      if (!list) return;
      const cached = JSON.parse(localStorage.getItem(this._cacheKey()) || '[]');
      if (!cached.length) {
        list.innerHTML = '<p class="empty">' + t('vouchers.none-cached') + '</p>';
        return;
      }
      list.innerHTML = '<div class="loading-center"><div class="spinner"></div></div>';
      const templates = await Promise.all(
        cached.map(async (tid) => {
          try {
            const res = await apiFetch('/api/v1/vouchers/templates/' + encodeURIComponent(tid));
            if (res.ok) return await res.json();
          } catch (e) {}
          return null;
        })
      );
      const valid = templates.filter((tp) => tp);
      if (!valid.length) {
        list.innerHTML = '<p class="empty">' + t('vouchers.load-failed-templates') + '</p>';
        return;
      }
      list.innerHTML = valid
        .map((tp) => {
          const v = tp.value || {};
          const c = tp.conditions || {};
          const tags = [];
          tags.push(
            '<span class="tag">' + esc(v.type || 'unknown') + ': ' + esc(String(v.amount || 0)) + '</span>'
          );
          if (c.tier_required) tags.push('<span class="tag">' + esc(c.tier_required) + '+</span>');
          if (c.min_purchase_cents)
            tags.push('<span class="tag">' + t('vouchers.tag-min-purchase', {amount: c.min_purchase_cents / 100}) + '</span>');
          if (c.total_supply) tags.push('<span class="tag">' + t('vouchers.tag-supply', {count: c.total_supply}) + '</span>');
          if (tp.expires_in_days)
            tags.push('<span class="tag">' + tp.expires_in_days + 'd</span>');
          return (
            '<div class="recipe-card">' +
            '<h4>' +
            esc(tp.name || tp.template_id) +
            '</h4>' +
            '<p>' +
            esc(tp.description || '') +
            '</p>' +
            '<div class="tags">' +
            tags.join('') +
            '</div>' +
            '<button class="btn-mini" onclick="VoucherTemplatesView.issue(\'' +
            esc(tp.template_id) +
            '\')">' + t('vouchers.issue-button') + '</button>' +
            '</div>'
          );
        })
        .join('');
    },

    _openTemplateModal() {
      let modal = document.getElementById('voucher-tpl-modal');
      if (!modal) {
        modal = document.createElement('div');
        modal.id = 'voucher-tpl-modal';
        modal.className = 'modal-overlay';
        modal.innerHTML = `
          <div class="modal-card large">
            <div class="modal-head">
              <h3>${t('vouchers.modal-title')}</h3>
              <button onclick="VoucherTemplatesView._closeModal()">✕</button>
            </div>
            <div class="modal-body">
              <div class="form-group">
                <label>${t('vouchers.name-label')}</label>
                <input id="vt-name" type="text" class="form-input-sm" placeholder="${t('vouchers.name-placeholder')}">
              </div>
              <div class="form-group">
                <label>${t('vouchers.desc-label')}</label>
                <textarea id="vt-desc" rows="2" class="form-input-sm" style="resize:vertical"></textarea>
              </div>
              <div class="form-row">
                <div class="form-group" style="margin-bottom:0">
                  <label>${t('vouchers.value-type-label')}</label>
                  <select id="vt-type" class="form-select">
                    <option value="percent">${t('vouchers.type.percent')}</option>
                    <option value="fixed">${t('vouchers.type.fixed')}</option>
                    <option value="free_item">${t('vouchers.type.free-item')}</option>
                    <option value="cashback">${t('vouchers.type.cashback')}</option>
                  </select>
                </div>
                <div class="form-group" style="margin-bottom:0">
                  <label>${t('vouchers.amount-label')}</label>
                  <input id="vt-amount" type="number" class="form-input-sm" value="20">
                </div>
              </div>
              <div class="form-group">
                <label>${t('vouchers.expires-label')}</label>
                <input id="vt-expires" type="number" class="form-input-sm" value="30">
              </div>
              <div class="form-group">
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                  <input id="vt-transferable" type="checkbox" checked> ${t('vouchers.transferable')}
                </label>
              </div>
              <hr style="border:none;border-top:1px solid var(--border);margin:14px 0">
              <h4 style="font-size:13px;color:var(--text-dim);margin:0 0 8px 0">${t('vouchers.conditions-header')}</h4>
              <div id="vt-conditions"></div>
            </div>
            <div class="modal-foot">
              <button class="btn btn-outline" onclick="VoucherTemplatesView._closeModal()">${t('vouchers.cancel')}</button>
              <button class="btn btn-green" onclick="VoucherTemplatesView._saveTemplate()">${t('vouchers.save-button')}</button>
            </div>
          </div>
        `;
        document.body.appendChild(modal);
      }
      modal.style.display = 'flex';
      modal.classList.add('active');

      // Reset fields
      document.getElementById('vt-name').value = '';
      document.getElementById('vt-desc').value = '';
      document.getElementById('vt-type').value = 'percent';
      document.getElementById('vt-amount').value = '20';
      document.getElementById('vt-expires').value = '30';
      document.getElementById('vt-transferable').checked = true;
      window._vtPendingConditions = {};

      // Mount ConditionsBuilder
      if (window.ConditionsBuilder) {
        window.ConditionsBuilder.mount('vt-conditions', {
          initial_conditions: {},
          on_change: (c) => {
            window._vtPendingConditions = c;
          },
        });
      } else {
        document.getElementById('vt-conditions').innerHTML =
          '<p class="empty" style="text-align:left">' + t('vouchers.conditions-missing') + '</p>';
      }
    },

    _closeModal() {
      const m = document.getElementById('voucher-tpl-modal');
      if (m) {
        m.style.display = 'none';
        m.classList.remove('active');
      }
    },

    async _saveTemplate() {
      const name = document.getElementById('vt-name').value.trim();
      if (!name) return showToast(t('vouchers.name-required'), 'error');
      const body = {
        brand_id: state.brandId,
        template_id: 'tpl_' + Date.now(),
        name: name,
        description: document.getElementById('vt-desc').value.trim(),
        value: {
          type: document.getElementById('vt-type').value,
          amount: parseFloat(document.getElementById('vt-amount').value || '0'),
        },
        conditions: window._vtPendingConditions || {},
        expires_in_days: parseInt(document.getElementById('vt-expires').value || '30', 10),
        transferable: document.getElementById('vt-transferable').checked,
        stackable: false,
      };

      try {
        const res = await apiFetch('/api/v1/vouchers/templates/create', {
          method: 'POST',
          json: body,
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          return showToast(t('vouchers.create-failed') + ': ' + (err.detail || res.status), 'error');
        }
        const cached = JSON.parse(localStorage.getItem(this._cacheKey()) || '[]');
        cached.push(body.template_id);
        localStorage.setItem(this._cacheKey(), JSON.stringify(cached));
        showToast(t('vouchers.created'));
        this._closeModal();
        await this._loadTemplates();
      } catch (e) {
        showToast(e.message || t('common.save-failed'), 'error');
      }
    },

    async issue(templateId) {
      const userId = prompt(t('vouchers.issue-prompt'));
      if (!userId) return;
      try {
        const res = await apiFetch(
          '/api/v1/vouchers/templates/' + encodeURIComponent(templateId) + '/issue',
          {
            method: 'POST',
            json: { user_id: userId, brand_id: state.brandId, reason: 'manual' },
          }
        );
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          return showToast(t('vouchers.issue-failed') + ': ' + (err.detail || res.status), 'error');
        }
        const data = await res.json();
        showToast(t('vouchers.issued') + ': ' + (data.code || data.voucher_id || 'ok'));
      } catch (e) {
        showToast(e.message || t('vouchers.issue-failed'), 'error');
      }
    },

    async _loadIssued(userId) {
      const list = document.getElementById('voucher-issued-list');
      list.innerHTML = '<div class="loading-center"><div class="spinner"></div></div>';
      try {
        const res = await apiFetch(
          '/api/v1/vouchers/' +
            encodeURIComponent(userId) +
            '?brand_id=' +
            encodeURIComponent(state.brandId || '')
        );
        if (!res.ok) {
          list.innerHTML = '<p class="empty">' + t('vouchers.none-found') + '</p>';
          return;
        }
        const data = await res.json();
        const items = Array.isArray(data) ? data : data.vouchers || [];
        if (!items.length) {
          list.innerHTML = '<p class="empty">' + t('vouchers.none-issued-user') + '</p>';
          return;
        }
        list.innerHTML = items
          .map((v) => {
            return (
              '<div class="rule-card">' +
              '<div class="rule-header">' +
              '<strong>' +
              esc(v.code || v.voucher_id || '') +
              '</strong>' +
              '<span class="' +
              (v.redeemed ? 'pill-gray' : 'pill-green') +
              '">' +
              (v.redeemed ? t('vouchers.used') : t('vouchers.usable')) +
              '</span>' +
              '</div>' +
              '<div class="rule-when">' +
              esc(v.template_id || v.description || '') +
              '</div>' +
              '<div class="rule-actions">' +
              (v.expires_at ? t('vouchers.expires') + ': ' + esc(v.expires_at) : '') +
              '</div>' +
              '</div>'
            );
          })
          .join('');
      } catch (e) {
        list.innerHTML = '<p class="empty">' + t('common.load-failed') + ': ' + esc(e.message || '') + '</p>';
      }
    },
  };

  window.VoucherTemplatesView = VoucherTemplatesView;
})();
