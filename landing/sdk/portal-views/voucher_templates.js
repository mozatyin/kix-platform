/* eslint-disable */
// ═══════════════════════════════════════════════════════════════════════
// Voucher Templates View
// Replaces the legacy CSV-only Vouchers view with a conditional-voucher
// template builder + issued-voucher inspector + legacy CSV tab.
// Depends on globals: state, apiFetch, esc, showToast, ConditionsBuilder
// ═══════════════════════════════════════════════════════════════════════
(function () {
  const VoucherTemplatesView = {
    async render() {
      const root = document.getElementById('view-vouchers');
      if (!root) return;

      root.innerHTML = `
        <h2 class="page-title">Vouchers / 优惠券</h2>
        <p class="page-subtitle">条件化优惠券模板 + 发放追踪 / Conditional templates &amp; issuance tracking</p>

        <div class="tabs">
          <button class="tab active" data-tab="templates">模板 / Templates</button>
          <button class="tab" data-tab="issued">已发放 / Issued</button>
          <button class="tab" data-tab="csv">CSV上传 / Upload (legacy)</button>
        </div>

        <div id="voucher-templates-pane">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
            <h3 style="margin:0;font-size:14px;color:var(--text-dim)">已有模板 / Existing Templates</h3>
            <button class="btn-primary" id="new-voucher-tpl" style="width:auto;padding:9px 16px">+ 新建模板 / New Template</button>
          </div>
          <div id="voucher-templates-list" class="recipes-grid"></div>
        </div>

        <div id="voucher-issued-pane" style="display:none">
          <div class="card" style="margin-bottom:14px">
            <div class="form-group" style="margin-bottom:0">
              <label>User ID / 用户ID</label>
              <div style="display:flex;gap:8px">
                <input type="text" id="vi-user-id" class="form-input-sm" placeholder="e.g. user_123" style="flex:1">
                <button class="btn-primary" id="vi-load-btn" style="width:auto;padding:9px 16px">查询 / Load</button>
              </div>
            </div>
          </div>
          <div id="voucher-issued-list"></div>
        </div>

        <div id="voucher-csv-pane" style="display:none">
          <div class="card">
            <p style="color:var(--text-dim);font-size:13px;margin-bottom:12px">
              旧版 CSV 上传仍可用 / Legacy CSV upload still available
            </p>
            <div class="form-row">
              <div class="form-group" style="margin-bottom:0">
                <label>Tier / 等级</label>
                <select id="voucher-tier" class="form-select">
                  <option value="bronze">Bronze / 铜级</option>
                  <option value="silver">Silver / 银级</option>
                  <option value="gold">Gold / 金级</option>
                </select>
              </div>
              <div class="form-group" style="margin-bottom:0">
                <label>Valid Days / 有效天数</label>
                <input type="number" id="voucher-valid-days" class="form-input-sm" value="30" min="1" max="365">
              </div>
            </div>
            <div class="form-group">
              <label>Description / 描述</label>
              <input type="text" id="voucher-desc" class="form-input-sm" placeholder="e.g. Free Americano">
            </div>
            <div class="upload-zone" id="upload-zone-tpl">
              <p>Drop CSV file here or tap to select</p>
              <p><small>拖放CSV文件到这里或点击选择</small></p>
              <input type="file" id="voucher-file-tpl" accept=".csv,.txt,text/csv">
            </div>
            <div id="upload-result-tpl" class="upload-result"></div>
          </div>
          <div class="section">
            <div class="section-title">Voucher Pool / 优惠券池</div>
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
          ['templates', 'issued', 'csv'].forEach((t) => {
            const pane = document.getElementById('voucher-' + t + '-pane');
            if (pane) pane.style.display = t === tab ? 'block' : 'none';
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
          if (!uid) return showToast('请输入用户ID / Enter user ID', 'error');
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
        list.innerHTML =
          '<p class="empty">还没有模板。点击"+ 新建模板"创建第一个 / No templates yet.</p>';
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
      const valid = templates.filter((t) => t);
      if (!valid.length) {
        list.innerHTML =
          '<p class="empty">未能加载模板 / Failed to load templates (backend may be offline)</p>';
        return;
      }
      list.innerHTML = valid
        .map((t) => {
          const v = t.value || {};
          const c = t.conditions || {};
          const tags = [];
          tags.push(
            '<span class="tag">' + esc(v.type || 'unknown') + ': ' + esc(String(v.amount || 0)) + '</span>'
          );
          if (c.tier_required) tags.push('<span class="tag">' + esc(c.tier_required) + '+</span>');
          if (c.min_purchase_cents)
            tags.push('<span class="tag">满¥' + c.min_purchase_cents / 100 + '</span>');
          if (c.total_supply) tags.push('<span class="tag">' + c.total_supply + '份</span>');
          if (t.expires_in_days)
            tags.push('<span class="tag">' + t.expires_in_days + 'd</span>');
          return (
            '<div class="recipe-card">' +
            '<h4>' +
            esc(t.name || t.template_id) +
            '</h4>' +
            '<p>' +
            esc(t.description || '') +
            '</p>' +
            '<div class="tags">' +
            tags.join('') +
            '</div>' +
            '<button class="btn-mini" onclick="VoucherTemplatesView.issue(\'' +
            esc(t.template_id) +
            '\')">发放 / Issue</button>' +
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
              <h3>新建优惠券模板 / New Voucher Template</h3>
              <button onclick="VoucherTemplatesView._closeModal()">✕</button>
            </div>
            <div class="modal-body">
              <div class="form-group">
                <label>模板名称 / Template Name</label>
                <input id="vt-name" type="text" class="form-input-sm" placeholder="如：生日免费咖啡 / Birthday Free Coffee">
              </div>
              <div class="form-group">
                <label>描述 / Description</label>
                <textarea id="vt-desc" rows="2" class="form-input-sm" style="resize:vertical"></textarea>
              </div>
              <div class="form-row">
                <div class="form-group" style="margin-bottom:0">
                  <label>价值类型 / Value Type</label>
                  <select id="vt-type" class="form-select">
                    <option value="percent">百分比折扣 / Percent</option>
                    <option value="fixed">固定金额(分) / Fixed (cents)</option>
                    <option value="free_item">免单 / Free Item</option>
                    <option value="cashback">现金回赠 / Cashback</option>
                  </select>
                </div>
                <div class="form-group" style="margin-bottom:0">
                  <label>价值数量 / Amount</label>
                  <input id="vt-amount" type="number" class="form-input-sm" value="20">
                </div>
              </div>
              <div class="form-group">
                <label>有效期(天) / Expires In Days</label>
                <input id="vt-expires" type="number" class="form-input-sm" value="30">
              </div>
              <div class="form-group">
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                  <input id="vt-transferable" type="checkbox" checked> 可转赠 / Transferable
                </label>
              </div>
              <hr style="border:none;border-top:1px solid var(--border);margin:14px 0">
              <h4 style="font-size:13px;color:var(--text-dim);margin:0 0 8px 0">发放条件 / Issue Conditions</h4>
              <div id="vt-conditions"></div>
            </div>
            <div class="modal-foot">
              <button class="btn btn-outline" onclick="VoucherTemplatesView._closeModal()">取消 / Cancel</button>
              <button class="btn btn-green" onclick="VoucherTemplatesView._saveTemplate()">保存 / Save</button>
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
          '<p class="empty" style="text-align:left">ConditionsBuilder not loaded — conditions will be empty.</p>';
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
      if (!name) return showToast('请输入名称 / Name required', 'error');
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
          return showToast('创建失败 / Create failed: ' + (err.detail || res.status), 'error');
        }
        const cached = JSON.parse(localStorage.getItem(this._cacheKey()) || '[]');
        cached.push(body.template_id);
        localStorage.setItem(this._cacheKey(), JSON.stringify(cached));
        showToast('模板已创建 / Template created');
        this._closeModal();
        await this._loadTemplates();
      } catch (e) {
        showToast(e.message || '保存失败', 'error');
      }
    },

    async issue(templateId) {
      const userId = prompt('发放给哪个用户ID？(测试用) / User ID:');
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
          return showToast('发放失败 / Issue failed: ' + (err.detail || res.status), 'error');
        }
        const data = await res.json();
        showToast('已发放 / Issued: ' + (data.code || data.voucher_id || 'ok'));
      } catch (e) {
        showToast(e.message || '发放失败', 'error');
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
          list.innerHTML = '<p class="empty">未找到优惠券 / No vouchers found</p>';
          return;
        }
        const data = await res.json();
        const items = Array.isArray(data) ? data : data.vouchers || [];
        if (!items.length) {
          list.innerHTML = '<p class="empty">该用户暂无优惠券 / This user has no vouchers</p>';
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
              (v.redeemed ? '已用 / Used' : '可用 / Active') +
              '</span>' +
              '</div>' +
              '<div class="rule-when">' +
              esc(v.template_id || v.description || '') +
              '</div>' +
              '<div class="rule-actions">' +
              (v.expires_at ? '过期 / Expires: ' + esc(v.expires_at) : '') +
              '</div>' +
              '</div>'
            );
          })
          .join('');
      } catch (e) {
        list.innerHTML = '<p class="empty">加载失败 / Load failed: ' + esc(e.message || '') + '</p>';
      }
    },
  };

  window.VoucherTemplatesView = VoucherTemplatesView;
})();
