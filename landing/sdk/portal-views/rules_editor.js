/* eslint-disable */
// ═══════════════════════════════════════════════════════════════════════
// Rules Editor View
// Visual When-Then rule editor for the rule_engine backend.
// Depends on globals: state, apiFetch, esc, showToast
// ═══════════════════════════════════════════════════════════════════════
(function () {
  const t = (key, opts) => {
    if (window.i18next && typeof window.i18next.t === 'function') {
      return window.i18next.t('portal-sdk:' + key, opts);
    }
    return key;
  };

  const RulesEditorView = {
    async render() {
      const root = document.getElementById('view-rules');
      if (!root) return;

      root.innerHTML = `
        <h2 class="page-title">${t('rules.title')}</h2>
        <p class="page-subtitle">${t('rules.subtitle')}</p>

        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
          <h3 style="margin:0;font-size:14px;color:var(--text-dim)">${t('rules.existing')}</h3>
          <button class="btn-primary" id="new-rule" style="width:auto;padding:9px 16px">${t('rules.new-button')}</button>
        </div>

        <div class="section">
          <div id="rules-list"><div class="loading-center"><div class="spinner"></div></div></div>
        </div>
      `;

      document.getElementById('new-rule').addEventListener('click', () => this._openModal());
      await this._loadRules();
    },

    async _loadRules() {
      const list = document.getElementById('rules-list');
      try {
        const res = await apiFetch('/api/v1/rules/' + encodeURIComponent(state.brandId || ''));
        if (!res.ok) {
          list.innerHTML = '<p class="empty">' + t('common.load-failed') + ' (' + res.status + ')</p>';
          return;
        }
        const data = await res.json();
        const rules = Array.isArray(data) ? data : data.rules || [];
        if (!rules.length) {
          list.innerHTML = '<p class="empty">' + t('rules.none') + '</p>';
          return;
        }
        list.innerHTML = rules
          .map((r) => {
            const actCount = (r.actions || []).length;
            return (
              '<div class="rule-card">' +
              '<div class="rule-header">' +
              '<strong>' +
              esc(r.name || r.id) +
              '</strong>' +
              '<span class="' +
              (r.active ? 'pill-green' : 'pill-gray') +
              '">' +
              (r.active ? t('rules.active') : t('rules.inactive')) +
              '</span>' +
              '</div>' +
              '<div class="rule-when">' + t('rules.when') + ' <code>' +
              esc(r.trigger_event || '') +
              '</code></div>' +
              '<div class="rule-actions">' +
              t('rules.actions-count', {count: actCount}) +
              '</div>' +
              '<div class="rule-tools">' +
              '<button class="btn-mini" onclick="RulesEditorView.toggle(\'' +
              esc(r.id) +
              "', " +
              (!r.active) +
              ')">' +
              (r.active ? t('rules.disable') : t('rules.enable')) +
              '</button>' +
              '<button class="btn-mini btn-danger" onclick="RulesEditorView.del(\'' +
              esc(r.id) +
              '\')">' + t('rules.delete') + '</button>' +
              '</div>' +
              '</div>'
            );
          })
          .join('');
      } catch (e) {
        list.innerHTML = '<p class="empty">' + t('common.load-failed') + ': ' + esc(e.message || '') + '</p>';
      }
    },

    _openModal() {
      let modal = document.getElementById('rule-modal');
      if (!modal) {
        modal = document.createElement('div');
        modal.id = 'rule-modal';
        modal.className = 'modal-overlay';
        modal.innerHTML = `
          <div class="modal-card large">
            <div class="modal-head">
              <h3>${t('rules.modal-title')}</h3>
              <button onclick="RulesEditorView._closeModal()">✕</button>
            </div>
            <div class="modal-body">
              <div class="form-group">
                <label>${t('rules.name-label')}</label>
                <input id="rule-name" type="text" class="form-input-sm" placeholder="${t('rules.name-placeholder')}">
              </div>
              <div class="form-group">
                <label>${t('rules.event-label')}</label>
                <select id="rule-event" class="form-select">
                  <option value="purchase_made">${t('rules.event.purchase')}</option>
                  <option value="game_completed">${t('rules.event.game-completed')}</option>
                  <option value="friend_redeemed_invite">${t('rules.event.friend-invite')}</option>
                  <option value="daily_checkin">${t('rules.event.daily-checkin')}</option>
                  <option value="streak_milestone">${t('rules.event.streak')}</option>
                  <option value="badge_earned">${t('rules.event.badge')}</option>
                  <option value="level_up">${t('rules.event.level-up')}</option>
                </select>
              </div>
              <div class="form-group">
                <label>${t('rules.cond-label')}</label>
                <input id="rule-cond" type="text" class="form-input-sm" placeholder="${t('rules.cond-placeholder')}">
                <div class="form-help">${t('rules.cond-help-prefix')}: <code>metric op value</code> — ${t('rules.cond-help-example')} <code>score >= 100</code></div>
              </div>
              <h4 style="font-size:13px;color:var(--text-dim);margin:14px 0 8px 0">${t('rules.actions-header')}</h4>
              <div id="rule-actions"></div>
              <button class="btn btn-outline" onclick="RulesEditorView._addAction()" style="margin-top:8px">${t('rules.add-action')}</button>
              <div class="form-group" style="margin-top:14px">
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                  <input id="rule-active" type="checkbox" checked> ${t('rules.activate-now')}
                </label>
              </div>
            </div>
            <div class="modal-foot">
              <button class="btn btn-outline" onclick="RulesEditorView._closeModal()">${t('rules.cancel')}</button>
              <button class="btn btn-green" onclick="RulesEditorView._save()">${t('rules.save-button')}</button>
            </div>
          </div>
        `;
        document.body.appendChild(modal);
      }
      modal.style.display = 'flex';
      modal.classList.add('active');

      // Reset
      document.getElementById('rule-name').value = '';
      document.getElementById('rule-event').value = 'purchase_made';
      document.getElementById('rule-cond').value = '';
      document.getElementById('rule-active').checked = true;
      document.getElementById('rule-actions').innerHTML = '';
      this._addAction();
    },

    _closeModal() {
      const m = document.getElementById('rule-modal');
      if (m) {
        m.style.display = 'none';
        m.classList.remove('active');
      }
    },

    _addAction() {
      const container = document.getElementById('rule-actions');
      const div = document.createElement('div');
      div.className = 'rule-action-row';
      div.innerHTML =
        '<select class="ra-module">' +
        '<option value="progression.award_xp">' + t('rules.action.award-xp') + '</option>' +
        '<option value="progression.award_badge">' + t('rules.action.award-badge') + '</option>' +
        '<option value="primitives.currency.grant">' + t('rules.action.grant-currency') + '</option>' +
        '<option value="voucher.grant">' + t('rules.action.grant-voucher') + '</option>' +
        '<option value="streak.increment">' + t('rules.action.streak-inc') + '</option>' +
        '</select>' +
        '<input class="ra-param" type="text" placeholder=\'{"amount":100}\'>' +
        '<button onclick="this.parentElement.remove()" class="btn-mini btn-danger">×</button>';
      container.appendChild(div);
    },

    async _save() {
      const name = document.getElementById('rule-name').value.trim();
      if (!name) return showToast(t('rules.name-required'), 'error');

      const actions = Array.from(document.querySelectorAll('.rule-action-row')).map((row) => {
        const fq = row.querySelector('.ra-module').value;
        const dot = fq.lastIndexOf('.');
        const module = dot >= 0 ? fq.slice(0, dot) : fq;
        const method = dot >= 0 ? fq.slice(dot + 1) : '';
        let params = {};
        try {
          params = JSON.parse(row.querySelector('.ra-param').value || '{}');
        } catch (e) {
          // ignore parse error, keep empty
        }
        return { module: module, method: method, params: params };
      });

      const cond = document.getElementById('rule-cond').value.trim();
      let conditions = null;
      if (cond) {
        const m = cond.match(/^(\w+)\s*([><=!]+)\s*(-?\d+(?:\.\d+)?)$/);
        if (m) {
          conditions = { type: 'value', metric: m[1], op: m[2], value: parseFloat(m[3]) };
        } else {
          return showToast(t('rules.bad-condition'), 'error');
        }
      }

      const body = {
        id: 'rule_' + Date.now(),
        brand_id: state.brandId,
        name: name,
        trigger_event: document.getElementById('rule-event').value,
        conditions: conditions,
        actions: actions,
        active: document.getElementById('rule-active').checked,
      };

      try {
        const res = await apiFetch('/api/v1/rules/configure', { method: 'POST', json: body });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          return showToast(t('rules.save-failed') + ': ' + (err.detail || res.status), 'error');
        }
        showToast(t('rules.saved'));
        this._closeModal();
        await this._loadRules();
      } catch (e) {
        showToast(e.message || t('rules.save-failed'), 'error');
      }
    },

    async toggle(id, active) {
      const ep = active
        ? '/api/v1/rules/' + encodeURIComponent(id) + '/enable'
        : '/api/v1/rules/' + encodeURIComponent(id) + '/disable';
      try {
        const res = await apiFetch(ep, { method: 'POST' });
        if (!res.ok) return showToast(t('rules.toggle-failed'), 'error');
        await this._loadRules();
      } catch (e) {
        showToast(e.message || t('rules.toggle-failed'), 'error');
      }
    },

    async del(id) {
      if (!confirm(t('rules.confirm-delete'))) return;
      try {
        const res = await apiFetch('/api/v1/rules/' + encodeURIComponent(id), {
          method: 'DELETE',
        });
        if (!res.ok) return showToast(t('rules.delete-failed'), 'error');
        showToast(t('rules.deleted'));
        await this._loadRules();
      } catch (e) {
        showToast(e.message || t('rules.delete-failed'), 'error');
      }
    },
  };

  window.RulesEditorView = RulesEditorView;
})();
