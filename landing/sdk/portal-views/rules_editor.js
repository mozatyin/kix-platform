/* eslint-disable */
// ═══════════════════════════════════════════════════════════════════════
// Rules Editor View
// Visual When-Then rule editor for the rule_engine backend.
// Depends on globals: state, apiFetch, esc, showToast
// ═══════════════════════════════════════════════════════════════════════
(function () {
  const RulesEditorView = {
    async render() {
      const root = document.getElementById('view-rules');
      if (!root) return;

      root.innerHTML = `
        <h2 class="page-title">Rules / 规则引擎</h2>
        <p class="page-subtitle">When-Then 规则：连接事件和奖励 / When-Then rules wiring events to rewards</p>

        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
          <h3 style="margin:0;font-size:14px;color:var(--text-dim)">已有规则 / Existing Rules</h3>
          <button class="btn-primary" id="new-rule" style="width:auto;padding:9px 16px">+ 新建规则 / New Rule</button>
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
          list.innerHTML = '<p class="empty">加载失败 / Load failed (' + res.status + ')</p>';
          return;
        }
        const data = await res.json();
        const rules = Array.isArray(data) ? data : data.rules || [];
        if (!rules.length) {
          list.innerHTML =
            '<p class="empty">还没有规则。AI 可以帮你生成 — 用 Recipes 页面的"AI生成配方"。<br>No rules yet. AI can help — use the Recipes page.</p>';
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
              (r.active ? '激活 / Active' : '停用 / Inactive') +
              '</span>' +
              '</div>' +
              '<div class="rule-when">当 / When <code>' +
              esc(r.trigger_event || '') +
              '</code></div>' +
              '<div class="rule-actions">' +
              actCount +
              ' 个动作 / actions</div>' +
              '<div class="rule-tools">' +
              '<button class="btn-mini" onclick="RulesEditorView.toggle(\'' +
              esc(r.id) +
              "', " +
              (!r.active) +
              ')">' +
              (r.active ? '停用 / Disable' : '启用 / Enable') +
              '</button>' +
              '<button class="btn-mini btn-danger" onclick="RulesEditorView.del(\'' +
              esc(r.id) +
              '\')">删除 / Delete</button>' +
              '</div>' +
              '</div>'
            );
          })
          .join('');
      } catch (e) {
        list.innerHTML = '<p class="empty">加载失败 / Load failed: ' + esc(e.message || '') + '</p>';
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
              <h3>新建规则 / New Rule</h3>
              <button onclick="RulesEditorView._closeModal()">✕</button>
            </div>
            <div class="modal-body">
              <div class="form-group">
                <label>规则名称 / Rule Name</label>
                <input id="rule-name" type="text" class="form-input-sm" placeholder="如：消费送积分 / e.g. Purchase rewards XP">
              </div>
              <div class="form-group">
                <label>触发事件 / Trigger Event</label>
                <select id="rule-event" class="form-select">
                  <option value="purchase_made">用户消费 / Purchase Made</option>
                  <option value="game_completed">游戏完成 / Game Completed</option>
                  <option value="friend_redeemed_invite">好友接受邀请 / Friend Accepted Invite</option>
                  <option value="daily_checkin">每日打卡 / Daily Check-in</option>
                  <option value="streak_milestone">连胜里程碑 / Streak Milestone</option>
                  <option value="badge_earned">勋章解锁 / Badge Earned</option>
                  <option value="level_up">升级 / Level Up</option>
                </select>
              </div>
              <div class="form-group">
                <label>条件 (可选) / Condition (optional)</label>
                <input id="rule-cond" type="text" class="form-input-sm" placeholder='如：score >= 100  (留空 = 无条件 / leave empty for no condition)'>
                <div class="form-help">格式 / Format: <code>metric op value</code> — 例 / e.g. <code>score >= 100</code></div>
              </div>
              <h4 style="font-size:13px;color:var(--text-dim);margin:14px 0 8px 0">动作 / Actions</h4>
              <div id="rule-actions"></div>
              <button class="btn btn-outline" onclick="RulesEditorView._addAction()" style="margin-top:8px">+ 添加动作 / Add Action</button>
              <div class="form-group" style="margin-top:14px">
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                  <input id="rule-active" type="checkbox" checked> 立即激活 / Activate immediately
                </label>
              </div>
            </div>
            <div class="modal-foot">
              <button class="btn btn-outline" onclick="RulesEditorView._closeModal()">取消 / Cancel</button>
              <button class="btn btn-green" onclick="RulesEditorView._save()">保存规则 / Save Rule</button>
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
        '<option value="progression.award_xp">送 XP / Award XP</option>' +
        '<option value="progression.award_badge">送勋章 / Award Badge</option>' +
        '<option value="primitives.currency.grant">送货币 / Grant Currency</option>' +
        '<option value="voucher.grant">送优惠券 / Grant Voucher</option>' +
        '<option value="streak.increment">连胜+1 / Streak +1</option>' +
        '</select>' +
        '<input class="ra-param" type="text" placeholder=\'{"amount":100}\'>' +
        '<button onclick="this.parentElement.remove()" class="btn-mini btn-danger">×</button>';
      container.appendChild(div);
    },

    async _save() {
      const name = document.getElementById('rule-name').value.trim();
      if (!name) return showToast('请输入规则名称 / Rule name required', 'error');

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
          return showToast(
            '条件格式错误 / Bad condition format. Use: metric op value',
            'error'
          );
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
          return showToast('保存失败 / Save failed: ' + (err.detail || res.status), 'error');
        }
        showToast('规则已保存 / Rule saved');
        this._closeModal();
        await this._loadRules();
      } catch (e) {
        showToast(e.message || '保存失败', 'error');
      }
    },

    async toggle(id, active) {
      const ep = active
        ? '/api/v1/rules/' + encodeURIComponent(id) + '/enable'
        : '/api/v1/rules/' + encodeURIComponent(id) + '/disable';
      try {
        const res = await apiFetch(ep, { method: 'POST' });
        if (!res.ok) return showToast('切换失败 / Toggle failed', 'error');
        await this._loadRules();
      } catch (e) {
        showToast(e.message || '切换失败', 'error');
      }
    },

    async del(id) {
      if (!confirm('确认删除？/ Confirm delete?')) return;
      try {
        const res = await apiFetch('/api/v1/rules/' + encodeURIComponent(id), {
          method: 'DELETE',
        });
        if (!res.ok) return showToast('删除失败 / Delete failed', 'error');
        showToast('已删除 / Deleted');
        await this._loadRules();
      } catch (e) {
        showToast(e.message || '删除失败', 'error');
      }
    },
  };

  window.RulesEditorView = RulesEditorView;
})();
