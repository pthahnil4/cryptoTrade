/* =====================================================================
 * 实盘分析记录 —— 快照生成 / 个人判断录入 / 动态复盘窗口 / 命中率统计
 * 从 task.html 内联脚本抽离为共享模块，供「定时任务」Tab 与独立页 /analysis 复用。
 * 交互逻辑（计时、并发请求序号 token、AbortController 超时、动态复盘窗口）原样保留，勿改行为。
 * ===================================================================== */
(function () {
    /* ---- 局部工具：避免污染全局；两页各自提供 #toast 容器与 .task-toast 样式(style.css) ---- */
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function showToast(msg, type) {
        const t = document.getElementById('toast');
        if (!t) { return; }
        t.textContent = msg;
        t.className = 'task-toast toast-' + (type || 'info') + ' show';
        setTimeout(() => { t.classList.remove('show'); }, 3500);
    }

    let anaInited = false;
    let _anaSnapshot = null;   // 当前快照数据（保存时随表单提交）
    let _anaRecords = [];      // 当前列表数据（编辑/删除时用 id 索引）
    let _anaSnapSeq = 0;       // 单币快照请求序号：丢弃过期响应，防快速连点竞态
    let _anaSnapAbort = null;  // 在途请求句柄（关闭/重发时取消）
    let _anaSnapTimer = null;  // 超时定时器：防网络挂起永久卡加载
    let _anaSaveBusy = false;  // 单条保存防重复提交
    let _anaSnapTick = null;   // 单币快照实时计时器（每 0.5s 刷新已用秒数）
    let _anaBatchTick = null;  // 批量快照实时计时器
    let _anaReviewMult = {near: 4, far: 8};  // 复盘窗口倍数（由后端 stats 回传，前端标签据此渲染）
    // 命中率中性带配置（严格镜像后端 HIT_ATR_K/HIT_FLOOR_PCT/HIT_FAR_SCALE，由 stats 回传校准）
    let _anaHitCfg = {k: 0.3, floor: 0.1, farScale: Math.SQRT2};
    // 判断/方向 → 期望的实际类别；命中=期望类别与实际一致（镜像后端 _JUDGMENT_EXPECT/_DIR_EXPECT）
    const _JUDG_EXPECT = {rise: 'up', fall: 'down', watch: 'flat'};
    const _DIR_EXPECT = {long: 'up', short: 'down'};
    const _CLASS_CHAR = {up: '涨', flat: '横', down: '跌'};

    // 中性带阈值 θ(%)：θ = max(地板, k × atr_pct × 窗口缩放)，远窗口按 √时长比放大（镜像后端 neutral_theta）
    function anaTheta(atrPct, isFar) {
        const scale = isFar ? _anaHitCfg.farScale : 1.0;
        return Math.max(_anaHitCfg.floor, _anaHitCfg.k * scale * (Number(atrPct) || 0));
    }

    // 实际走势三分类：'up'/'flat'/'down'（θ 内为横盘）；数据无效返回 null（镜像后端 classify_move）
    function anaClassify(price, follow, atrPct, isFar) {
        price = Number(price); follow = Number(follow);
        if (!(price > 0) || !(follow > 0)) { return null; }
        const chg = (follow / price - 1) * 100;
        const th = anaTheta(atrPct, isFar);
        if (chg > th) { return 'up'; }
        if (chg < -th) { return 'down'; }
        return 'flat';
    }

    // 把 '5m'/'15m'/'1H' 等周期串解析为分钟数（与后端 period_to_minutes 对齐）
    function _anaPeriodMin(p) {
        const m = /^\s*(\d+)\s*([mMhHwWdD])\s*$/.exec(String(p || ''));
        if (!m) return 0;
        const mult = {m: 1, h: 60, w: 10080, d: 1440}[m[2].toLowerCase()] || 0;
        return parseInt(m[1], 10) * mult;
    }

    // 分钟数格式化为简短时长标签：45→'45m'，60→'1H'，90→'1.5H'，120→'2H'
    function _anaFmtDur(min) {
        if (!min || min < 60) return (min || 0) + 'm';
        const h = min / 60;
        return (Number.isInteger(h) ? h : h.toFixed(1)) + 'H';
    }

    // 依据记录短周期算出近/远窗口（分钟 + 对应 DB 列）
    function _anaWindows(shortPeriod) {
        const sm = _anaPeriodMin(shortPeriod);
        if (sm <= 0) {
            return {near: {min: 60, col: 'price_1h', ts: 'ts_1h'},
                    far: {min: 240, col: 'price_4h', ts: 'ts_4h'}};
        }
        return {near: {min: sm * _anaReviewMult.near, col: 'price_1h', ts: 'ts_1h'},
                far: {min: sm * _anaReviewMult.far, col: 'price_4h', ts: 'ts_4h'}};
    }

    // 同步列表表头的窗口标签（倍数变化时）
    function _anaSyncReviewHeaders() {
        const nth = document.getElementById('ana-th-near');
        const fth = document.getElementById('ana-th-far');
        if (nth) nth.textContent = _anaReviewMult.near + '×短周期后';
        if (fth) fth.textContent = _anaReviewMult.far + '×短周期后';
    }

    // 价格精度显示规范：<10 保町3位 / 10~100 保町2位 / 100~1000 保町1位 / >1000 取整
    function anaFmtPrice(v) {
        const n = Number(v);
        if (!isFinite(n) || n <= 0) return '—';
        if (n < 10) return n.toFixed(3);
        if (n < 100) return n.toFixed(2);
        if (n < 1000) return n.toFixed(1);
        return Math.round(n).toString();
    }

    function anaDirBadge(dir, withPrev) {
        if (dir === 'long') return '<span class="badge badge-long">看多</span>';
        if (dir === 'short') return '<span class="badge badge-short">看空</span>';
        return '<span class="badge badge-watch">--</span>';
    }

    function anaJudgmentBadge(j) {
        if (j === 'rise') return '<span class="badge badge-long">涨</span>';
        if (j === 'fall') return '<span class="badge badge-short">跌</span>';
        return '<span class="badge badge-watch">观望</span>';
    }

    function initAnalysisTab() {
        if (anaInited) { return; }
        anaInited = true;
        fetch('/api/task/config/trading').then(r => r.json()).then(res => {
            if (res.code !== 200) { showToast('加载币种列表失败: ' + res.message, 'error'); return; }
            const coins = (res.data.currencies || []).map(c => c.instId).filter(Boolean);
            const sel = document.getElementById('ana-inst');
            const fSel = document.getElementById('ana-filter-inst');
            sel.innerHTML = '';
            coins.forEach(id => {
                const opt = document.createElement('option');
                opt.value = id; opt.textContent = id;
                sel.appendChild(opt);
                const fOpt = opt.cloneNode(true);
                fSel.appendChild(fOpt);
            });
            if (!coins.length) sel.innerHTML = '<option value="">无已配置币种</option>';
        }).catch(e => showToast('加载币种列表失败: ' + e, 'error'));
        loadAnalysisRecords();
    }

    function genAnalysisSnapshot() {
        const inst = document.getElementById('ana-inst').value;
        if (!inst) { showToast('请先选择币种', 'error'); return; }
        const btn = document.getElementById('ana-snap-btn');
        const tsEl = document.getElementById('ana-snap-ts');
        const reqId = ++_anaSnapSeq;
        if (_anaSnapAbort) { _anaSnapAbort.abort(); }   // 取消上一个在途请求
        btn.disabled = true;
        btn.textContent = '⏳ 快照生成中...';
        // 旧快照即刻作废：请求在途时点保存会提示重新生成，避免币种错配误存
        _anaSnapshot = null;
        document.getElementById('ana-snapshot-box').style.display = 'none';
        const t0 = performance.now();
        clearInterval(_anaSnapTick);
        tsEl.textContent = '分析中... 0s（首次需拉取K线，约数秒）';
        _anaSnapTick = setInterval(function() {
            if (reqId !== _anaSnapSeq) { clearInterval(_anaSnapTick); return; }
            tsEl.textContent = '分析中... ' + ((performance.now() - t0) / 1000).toFixed(0) + 's（拉K线+双周期计算）';
        }, 500);
        const ctrl = new AbortController();
        _anaSnapAbort = ctrl;
        clearTimeout(_anaSnapTimer);
        _anaSnapTimer = setTimeout(function() { ctrl.abort(); }, 120000);
        fetch('/api/task/analysis/snapshot?instId=' + encodeURIComponent(inst),
              {signal: ctrl.signal})
            .then(r => r.json()).then(res => {
                if (reqId !== _anaSnapSeq) { return; }   // 已有更新的请求，丢弃过期响应
                clearInterval(_anaSnapTick);
                if (res.code !== 200) {
                    tsEl.textContent = '';
                    showToast('快照失败: ' + res.message, 'error');
                    return;
                }
                _anaSnapshot = res.data;
                renderAnalysisSnapshot(res.data);
                const secs = ((performance.now() - t0) / 1000).toFixed(1);
                tsEl.textContent = '快照时间：' + res.data.ts + ' · 耗时 ' + secs + 's';
                showToast('快照生成完成，耗时 ' + secs + 's', 'success');
            }).catch(e => {
                if (reqId !== _anaSnapSeq) { return; }
                clearInterval(_anaSnapTick);
                tsEl.textContent = '';
                showToast('快照失败: ' + (e && e.name === 'AbortError' ? '请求超时，请点击重试' : e), 'error');
            }).finally(function() {
                if (reqId === _anaSnapSeq) {
                    clearInterval(_anaSnapTick);
                    btn.disabled = false;
                    btn.textContent = '📸 生成快照';
                }
            });
    }

    function renderAnalysisSnapshot(d) {
        const dirCn = v => v === 'long' ? '看多' : (v === 'short' ? '看空' : '--');
        const dirCls = v => v === 'long' ? 'cc-green' : (v === 'short' ? 'cc-red' : '');
        const longText = dirCn(d.long_dir) +
            (d.long_dir_prev && d.long_dir_prev !== d.long_dir
                ? ' <span style="font-size:0.72rem;color:#999">(上一时段 ' + dirCn(d.long_dir_prev) + ')</span>'
                : (d.long_dir_prev ? ' <span style="font-size:0.72rem;color:#999">(与上一时段一致)</span>' : ''));
        document.getElementById('ana-snap-grid').innerHTML =
            '<div class="ana-snap-item"><div class="as-label">实时价格</div><div class="as-value">' + anaFmtPrice(d.price) + '</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">短周期方向 (' + escapeHtml(d.short_period || '') + ')</div><div class="as-value ' + dirCls(d.short_dir) + '">' + dirCn(d.short_dir) + '</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">长周期方向 (' + escapeHtml(d.long_period || '') + ')</div><div class="as-value ' + dirCls(d.long_dir) + '">' + longText + '</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">ATR%</div><div class="as-value">' + Number(d.atr_pct || 0).toFixed(2) + '%</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">BOLL 上轨</div><div class="as-value">' + anaFmtPrice(d.boll_upper) + '</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">BOLL 中轨</div><div class="as-value">' + anaFmtPrice(d.boll_middle) + '</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">BOLL 下轨</div><div class="as-value">' + anaFmtPrice(d.boll_lower) + '</div></div>';
        document.getElementById('ana-snap-ts').textContent = '快照时间：' + d.ts;
        document.getElementById('ana-snapshot-box').style.display = '';
    }

    function hideAnalysisSnapshot() {
        document.getElementById('ana-snapshot-box').style.display = 'none';
        document.getElementById('ana-snap-ts').textContent = '';
        _anaSnapshot = null;
    }

    function saveAnalysisRecord() {
        if (_anaSaveBusy) { return; }
        if (!_anaSnapshot) { showToast('请先生成快照', 'error'); return; }
        const judgment = (document.querySelector('input[name="ana-judgment"]:checked') || {}).value || 'watch';
        const body = {
            instId: _anaSnapshot.instId,
            ts: _anaSnapshot.ts,
            price: _anaSnapshot.price,
            short_period: _anaSnapshot.short_period,
            long_period: _anaSnapshot.long_period,
            short_dir: _anaSnapshot.short_dir,
            long_dir: _anaSnapshot.long_dir,
            long_dir_prev: _anaSnapshot.long_dir_prev,
            atr_pct: _anaSnapshot.atr_pct,
            user_judgment: judgment,
            user_reason: document.getElementById('ana-reason').value.trim()
        };
        _anaSaveBusy = true;
        fetch('/api/task/analysis/records', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        }).then(r => r.json()).then(res => {
            showToast(res.message, res.code === 200 ? 'success' : 'error');
            if (res.code === 200) {
                // 用快照 + 返回的自增 id 在本地构造新记录，命中筛选则插到表首（不再整表重拉重建）
                const rec = {
                    id: (res.data && res.data.id), ts: _anaSnapshot.ts, inst_id: _anaSnapshot.instId,
                    price: _anaSnapshot.price, short_period: _anaSnapshot.short_period, long_period: _anaSnapshot.long_period,
                    short_dir: _anaSnapshot.short_dir, long_dir: _anaSnapshot.long_dir, long_dir_prev: _anaSnapshot.long_dir_prev,
                    atr_pct: _anaSnapshot.atr_pct, user_judgment: judgment, user_reason: body.user_reason,
                    price_1h: null, ts_1h: null, price_4h: null, ts_4h: null
                };
                document.getElementById('ana-reason').value = '';
                hideAnalysisSnapshot();
                if (anaMatchesFilter(rec)) {
                    _anaRecords.unshift(rec);
                    anaInsertRowTop(rec);
                    refreshAnaStats();
                }
            }
        }).catch(e => showToast('保存失败: ' + e, 'error'))
          .finally(function() { _anaSaveBusy = false; });
    }

    // ============================================================
    // 批量快照矩阵：一键拉全部币种 → 逐行填判断/原因 → 一次性提交
    // ============================================================
    let _anaBatchItems = [];   // 当前矩阵的原始快照数据
    let _anaBatchSeq = 0;      // 批量请求序号：关闭/再次触发后丢弃在途响应
    let _anaBatchAbort = null; // 在途批量请求句柄（关闭/重发时取消）
    let _anaBatchTimer = null; // 超时定时器：防网络挂起按钮永久卡加载
    let _anaBatchSaveBusy = false; // 批量保存防重复提交

    function genBatchSnapshot(btn) {
        const box = document.getElementById('ana-batch-box');
        const msgEl = document.getElementById('ana-batch-msg');
        const reqId = ++_anaBatchSeq;
        if (_anaBatchAbort) { _anaBatchAbort.abort(); }   // 取消上一个在途请求
        if (btn) { btn.disabled = true; btn.textContent = '⏳ 批量快照中...'; }
        box.style.display = '';
        document.getElementById('ana-batch-count').textContent = '0';
        document.getElementById('ana-batch-tbody').innerHTML =
            '<tr><td colspan="9" class="empty-state">加载中...</td></tr>';
        const t0 = performance.now();
        clearInterval(_anaBatchTick);
        msgEl.textContent = '正在串行拉取全部币种数据... 0s';
        _anaBatchTick = setInterval(function() {
            if (reqId !== _anaBatchSeq) { clearInterval(_anaBatchTick); return; }
            msgEl.textContent = '正在串行拉取全部币种数据... ' + ((performance.now() - t0) / 1000).toFixed(0) + 's（约数秒/币）';
        }, 500);
        const ctrl = new AbortController();
        _anaBatchAbort = ctrl;
        clearTimeout(_anaBatchTimer);
        _anaBatchTimer = setTimeout(function() { ctrl.abort(); }, 180000);
        fetch('/api/task/analysis/snapshot_batch', {signal: ctrl.signal})
            .then(r => r.json()).then(res => {
                if (reqId !== _anaBatchSeq) { return; }   // 面板已关闭/已重新触发，丢弃过期响应
                clearInterval(_anaBatchTick);
                if (res.code !== 200) {
                    msgEl.textContent = '';
                    showToast('批量快照失败: ' + res.message, 'error');
                    return;
                }
                const items = res.data.items || [];
                const errors = res.data.errors || [];
                _anaBatchItems = items;
                renderBatchMatrix(items);
                document.getElementById('ana-batch-count').textContent = items.length;
                const secs = ((performance.now() - t0) / 1000).toFixed(1);
                let m = '成功 ' + items.length + ' 币种 · 总耗时 ' + secs + 's';
                if (items.length) { m += '（均 ' + (secs / items.length).toFixed(1) + 's/币）'; }
                if (errors.length) {
                    m += '，失败 ' + errors.length + ' 个（' + errors.map(e => e.instId || ('#' + e.index)).join('、') + '）';
                }
                msgEl.textContent = m;
                showToast('批量快照完成，总耗时 ' + secs + 's' + (errors.length ? '（部分失败）' : ''), errors.length ? 'error' : 'success');
            }).catch(e => {
                if (reqId !== _anaBatchSeq) { return; }
                clearInterval(_anaBatchTick);
                msgEl.textContent = '';
                showToast('批量快照失败: ' + (e && e.name === 'AbortError' ? '请求超时或已取消，请点击重试' : e), 'error');
            }).finally(function() {
                if (reqId === _anaBatchSeq) {
                    clearInterval(_anaBatchTick);
                    if (btn) { btn.disabled = false; btn.textContent = '🚀 一键批量快照'; }
                }
            });
    }

    function renderBatchMatrix(items) {
        const tbody = document.getElementById('ana-batch-tbody');
        if (!items.length) {
            tbody.innerHTML = '<tr><td colspan="9" class="empty-state">无可用币种，请检查交易配置</td></tr>';
            return;
        }
        const dirCn = v => v === 'long' ? '看多' : (v === 'short' ? '看空' : '--');
        const dirCls = v => v === 'long' ? 'cc-green' : (v === 'short' ? 'cc-red' : '');
        tbody.innerHTML = items.map((d, i) => {
            const longText = dirCn(d.long_dir) +
                (d.long_dir_prev && d.long_dir_prev !== d.long_dir
                    ? ' <span style="font-size:0.72rem;color:#999">(上时段 ' + dirCn(d.long_dir_prev) + ')</span>'
                    : (d.long_dir_prev ? ' <span style="font-size:0.72rem;color:#999">(与上时段一致)</span>' : ''));
            return '<tr>' +
                '<td><input type="checkbox" class="ana-batch-pick" data-idx="' + i + '" checked></td>' +
                '<td style="white-space:nowrap"><strong>' + escapeHtml(d.instId) + '</strong></td>' +
                '<td>' + anaFmtPrice(d.price) + '</td>' +
                '<td><span class="' + dirCls(d.short_dir) + '">' + dirCn(d.short_dir) + '</span> ' +
                    '<span style="font-size:0.7rem;color:#999">' + escapeHtml(d.short_period || '') + '</span></td>' +
                '<td><span class="' + dirCls(d.long_dir) + '">' + longText + '</span> ' +
                    '<span style="font-size:0.7rem;color:#999">' + escapeHtml(d.long_period || '') + '</span></td>' +
                '<td>' + Number(d.atr_pct || 0).toFixed(2) + '%</td>' +
                '<td style="font-size:0.78rem;color:#555;white-space:nowrap">' +
                    anaFmtPrice(d.boll_upper) + ' / ' + anaFmtPrice(d.boll_middle) + ' / ' + anaFmtPrice(d.boll_lower) + '</td>' +
                '<td style="white-space:nowrap">' +
                    '<label><input type="radio" name="ana-batch-j-' + i + '" value="rise"> 涨</label> ' +
                    '<label><input type="radio" name="ana-batch-j-' + i + '" value="fall"> 跌</label> ' +
                    '<label><input type="radio" name="ana-batch-j-' + i + '" value="watch" checked> 观望</label></td>' +
                '<td><input type="text" class="config-input ana-batch-reason" data-idx="' + i + '" ' +
                    'placeholder="分析原因(可选)" style="width:100%;min-width:130px"></td>' +
                '</tr>';
        }).join('');
    }

    function toggleAllBatchRows(pick) {
        document.querySelectorAll('.ana-batch-pick').forEach(cb => { cb.checked = !!pick; });
    }

    function hideBatchSnapshot() {
        _anaBatchSeq++;                              // 作废在途请求（响应将被丢弃）
        if (_anaBatchAbort) { _anaBatchAbort.abort(); _anaBatchAbort = null; }
        const b = document.getElementById('ana-batch-btn');
        if (b) { b.disabled = false; b.textContent = '🚀 一键批量快照'; }
        document.getElementById('ana-batch-box').style.display = 'none';
        document.getElementById('ana-batch-tbody').innerHTML = '';
        document.getElementById('ana-batch-msg').textContent = '';
        _anaBatchItems = [];
    }

    function saveBatchSnapshots() {
        if (_anaBatchSaveBusy) { return; }
        if (!_anaBatchItems.length) { showToast('请先生成批量快照', 'error'); return; }
        const picked = [];
        document.querySelectorAll('.ana-batch-pick:checked').forEach(cb => {
            const idx = parseInt(cb.dataset.idx, 10);
            const d = _anaBatchItems[idx];
            if (!d) return;
            const radio = document.querySelector('input[name="ana-batch-j-' + idx + '"]:checked');
            const reasonEl = document.querySelector('.ana-batch-reason[data-idx="' + idx + '"]');
            picked.push({
                instId: d.instId, ts: d.ts, price: d.price,
                short_period: d.short_period, long_period: d.long_period,
                short_dir: d.short_dir, long_dir: d.long_dir, long_dir_prev: d.long_dir_prev,
                atr_pct: d.atr_pct, user_judgment: radio ? radio.value : 'watch',
                user_reason: reasonEl ? reasonEl.value.trim() : ''
            });
        });
        if (!picked.length) { showToast('请至少勾选一行要保存的记录', 'error'); return; }
        const msgEl = document.getElementById('ana-batch-msg');
        _anaBatchSaveBusy = true;
        msgEl.textContent = '正在批量保存（' + picked.length + ' 条）...';
        fetch('/api/task/analysis/records_batch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({items: picked})
        }).then(r => r.json()).then(res => {
            showToast(res.message, res.code === 200 ? 'success' : 'error');
            if (res.code !== 200) { msgEl.textContent = ''; return; }
            const errors = (res.data && res.data.errors) || [];
            if (errors.length) {
                // 部分失败：保留矩阵供修正后重存，失败行明细展示在矩阵底部
                msgEl.textContent = res.message + '；失败行：' + errors.map(e => {
                    const it = _anaBatchItems[e.index];
                    return (it ? it.instId : ('第' + e.index + '行')) + '——' + e.message;
                }).join('；');
                showToast('部分行保存失败，请修正后重新勾选保存', 'error');
            } else {
                // 全量成功：用勾选行 + 返回 ids 在本地构造记录，命中筛选的插到表首（不再整表重拉重建）
                const ids = (res.data && res.data.ids) || [];
                const newRecs = picked.map(function(p, i) {
                    return {
                        id: ids[i], ts: p.ts, inst_id: p.instId, price: p.price,
                        short_period: p.short_period, long_period: p.long_period,
                        short_dir: p.short_dir, long_dir: p.long_dir, long_dir_prev: p.long_dir_prev,
                        atr_pct: p.atr_pct, user_judgment: p.user_judgment, user_reason: p.user_reason,
                        price_1h: null, ts_1h: null, price_4h: null, ts_4h: null
                    };
                }).filter(function(r) { return r.id !== undefined; });
                hideBatchSnapshot();
                const add = newRecs.filter(anaMatchesFilter);
                if (add.length) {
                    _anaRecords = add.concat(_anaRecords);
                    const tbody = document.getElementById('ana-tbody');
                    if (!tbody.querySelector('tr[data-id]')) { tbody.innerHTML = ''; }
                    tbody.insertAdjacentHTML('afterbegin', add.map(anaRowHtml).join(''));
                    refreshAnaStats();
                }
            }
        }).catch(e => {
            msgEl.textContent = '';
            showToast('批量保存失败: ' + e, 'error');
        }).finally(function() {
            _anaBatchSaveBusy = false;
        });
    }

    function anaFilterParams() {
        const params = [];
        const inst = document.getElementById('ana-filter-inst').value;
        const judgment = document.getElementById('ana-filter-judgment').value;
        const start = document.getElementById('ana-filter-start').value;
        const end = document.getElementById('ana-filter-end').value;
        if (inst) params.push('instId=' + encodeURIComponent(inst));
        if (judgment) params.push('judgment=' + judgment);
        if (start) params.push('start=' + encodeURIComponent(start + ' 00:00:00'));
        if (end) params.push('end=' + encodeURIComponent(end + ' 23:59:59'));
        return params.length ? ('?' + params.join('&')) : '';
    }

    function loadAnalysisRecords() {
        const tbody = document.getElementById('ana-tbody');
        tbody.innerHTML = '<tr><td colspan="11" class="empty-state">加载中...（首次可能需回填复盘价格，稍慢）</td></tr>';
        fetch('/api/task/analysis/records' + anaFilterParams()).then(r => r.json()).then(res => {
            if (res.code !== 200) {
                tbody.innerHTML = '<tr><td colspan="11" class="empty-state">加载失败：' + escapeHtml(res.message || '') + '</td></tr>';
                return;
            }
            _anaRecords = res.data.records || [];
            renderAnalysisStats(res.data.stats);
            renderAnalysisTable(_anaRecords);
        }).catch(e => {
            tbody.innerHTML = '<tr><td colspan="11" class="empty-state">加载失败：' + escapeHtml(String(e)) + '</td></tr>';
        });
    }

    function renderAnalysisStats(stats) {
        const box = document.getElementById('ana-stats');
        if (!stats) { box.innerHTML = ''; renderAnaConfusion(null); return; }
        if (stats.near_mult) { _anaReviewMult.near = stats.near_mult; }
        if (stats.far_mult) { _anaReviewMult.far = stats.far_mult; }
        // 同步中性带配置：本地重算(computeStatsLocal)与结果列(anaResultCell)据此与后端对齐口径
        if (stats.hit_k !== undefined) { _anaHitCfg.k = stats.hit_k; }
        if (stats.hit_floor_pct !== undefined) { _anaHitCfg.floor = stats.hit_floor_pct; }
        if (stats.hit_far_scale !== undefined) { _anaHitCfg.farScale = stats.hit_far_scale; }
        _anaSyncReviewHeaders();
        const nLabel = _anaReviewMult.near + '×短周期';
        const fLabel = _anaReviewMult.far + '×短周期';
        const bucketText = b => b && b.total > 0
            ? b.rate + '% <span style="font-size:0.72rem;color:#999;font-weight:400">(' + b.hit + '/' + b.total + ')</span>'
            : '—';
        const nearAvg = (stats.user.near && stats.user.near.total > 0)
            ? ((stats.user.near.avg_pct > 0 ? '+' : '') + stats.user.near.avg_pct + '%') : '—';
        const cards = [
            ['记录总数', stats.total, 'cc-blue'],
            ['个人准确率 (' + nLabel + ')', bucketText(stats.user.near), 'cc-orange'],
            ['个人准确率 (' + fLabel + ')', bucketText(stats.user.far), 'cc-orange'],
            ['策略命中率 (' + nLabel + ')', bucketText(stats.strategy.near), 'cc-green'],
            ['策略命中率 (' + fLabel + ')', bucketText(stats.strategy.far), 'cc-green'],
            ['平均涨跌幅 (' + nLabel + ')', nearAvg,
                (stats.user.near && stats.user.near.avg_pct >= 0) ? 'cc-green' : 'cc-red']
        ];
        box.innerHTML = cards.map(c =>
            '<div class="compare-card"><div class="cc-label">' + c[0] + '</div><div class="cc-value ' + c[2] + '">' + c[1] + '</div></div>'
        ).join('');
        renderAnaConfusion(stats);
    }

    // 混淆矩阵：行=你的判断，列=实际走势(涨/横/跌)，对角线判对(绿)、非对角判错(红)。
    // 内联样式渲染，兼容独立页(analysis.html .ana-stats)与 Tab 页(task.html .compare-cards)两套容器。
    function renderAnaConfusion(stats) {
        const box = document.getElementById('ana-confusion');
        if (!box) { return; }
        if (!stats || !stats.confusion) { box.innerHTML = ''; return; }
        const rows = [['rise', '判涨'], ['watch', '判观望'], ['fall', '判跌']];
        const cols = [['up', '实涨'], ['flat', '实横盘'], ['down', '实跌']];
        const nLabel = _anaReviewMult.near + '×短周期';
        const fLabel = _anaReviewMult.far + '×短周期';
        function matrix(conf) {
            let h = '<table style="border-collapse:collapse;font-size:0.8rem;text-align:center">';
            h += '<tr><td style="padding:3px 8px"></td>' +
                cols.map(c => '<td style="padding:3px 8px;color:#999;font-weight:600">' + c[1] + '</td>').join('') + '</tr>';
            rows.forEach(function(r) {
                h += '<tr><td style="padding:3px 8px;color:#999;font-weight:600;text-align:right;white-space:nowrap">' + r[1] + '</td>';
                cols.forEach(function(c) {
                    const v = (conf[r[0]] && conf[r[0]][c[0]]) || 0;
                    const diag = _JUDG_EXPECT[r[0]] === c[0];
                    const bg = v === 0 ? '#fafafa' : (diag ? '#d4edda' : '#f8d7da');
                    const color = v === 0 ? '#ccc' : (diag ? '#155724' : '#721c24');
                    h += '<td style="padding:3px 8px;min-width:34px;border:1px solid #eee;background:' + bg +
                        ';color:' + color + ';font-weight:600">' + v + '</td>';
                });
                h += '</tr>';
            });
            return h + '</table>';
        }
        const k = (stats.hit_k !== undefined ? stats.hit_k : _anaHitCfg.k);
        const fs = Number(stats.hit_far_scale !== undefined ? stats.hit_far_scale : _anaHitCfg.farScale);
        box.innerHTML =
            '<div style="font-size:0.82rem;font-weight:700;color:#555;margin-bottom:8px">🔀 判断混淆矩阵（定位你最常犯哪种误判）</div>' +
            '<div style="display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start">' +
            '<div><div style="font-size:0.78rem;font-weight:600;margin-bottom:5px;color:#777">近窗口(' + nLabel + ')</div>' + matrix(stats.confusion.near || {}) + '</div>' +
            '<div><div style="font-size:0.78rem;font-weight:600;margin-bottom:5px;color:#777">远窗口(' + fLabel + ')</div>' + matrix(stats.confusion.far || {}) + '</div>' +
            '<div style="font-size:0.74rem;color:#999;max-width:300px;line-height:1.7">' +
            '行=你的判断，列=实际走势。实际涨跌按 <b>ATR 中性带</b> 分三档：|涨跌|≤θ 记<b>横盘</b>，否则<b>涨/跌</b>；' +
            'θ=' + k + '×ATR%，远窗口×' + fs.toFixed(2) + '。' +
            '<span style="color:#155724;font-weight:600">绿=判对</span>、<span style="color:#721c24;font-weight:600">红=判错</span>：' +
            '哪格红最多就是你最典型的误判（如「判涨→实横盘」多＝老把震荡当突破）。</div>' +
            '</div>';
    }

    function anaReviewCell(rec, priceCol, tsCol, winMin) {
        const follow = rec[priceCol];
        const tip = (winMin ? (_anaFmtDur(winMin) + '后') : '') + (rec[tsCol] ? (' · 取价K线：' + rec[tsCol]) : '');
        if (follow === null || follow === undefined) return '<span class="ana-pending" title="' + tip + '">待回填</span>';
        const chg = rec.price > 0 ? (follow / rec.price - 1) * 100 : 0;
        const cls = chg > 0 ? 'cc-green' : (chg < 0 ? 'cc-red' : '');
        return '<div class="ana-review-cell" title="' + tip + '">' + anaFmtPrice(follow) +
            '<br><span class="' + cls + '">' + (chg > 0 ? '+' : '') + chg.toFixed(2) + '%</span></div>';
    }

    function anaResultCell(rec) {
        // 判断结果：个人判断 vs 近/远窗口实际走势三分类（涨/横/跌），命中=判断类别与实际一致。
        // 观望也计分：实际横盘=判对✓，实际涨/跌=判错✗（不再“观望不计”）。
        const w = _anaWindows(rec.short_period);
        const atrPct = Number(rec.atr_pct || 0);
        const expect = _JUDG_EXPECT[rec.user_judgment] || '';
        const parts = [];
        [['near', w.near, false], ['far', w.far, true]].forEach(function(t) {
            const win = t[1], isFar = t[2];
            const dur = _anaFmtDur(win.min);
            const actual = anaClassify(rec.price, rec[win.col], atrPct, isFar);
            if (actual === null) { parts.push('<span class="ana-pending">' + dur + '待</span>'); return; }
            const hit = expect !== '' && expect === actual;
            parts.push('<span class="' + (hit ? 'ana-hit' : 'ana-miss') + '" title="实际' + _CLASS_CHAR[actual] +
                (hit ? '，判对' : '，判错') + '">' + dur + _CLASS_CHAR[actual] + (hit ? '✓' : '✗') + '</span>');
        });
        return '<div class="ana-review-cell">' + parts.join('<br>') + '</div>';
    }

    // 单行 HTML 构造器：全量渲染与行级局部刷新共用（data-id 供精准定位目标行）
    function anaRowHtml(r) {
        const w = _anaWindows(r.short_period);
        const longCell = anaDirBadge(r.long_dir) +
            (r.long_dir_prev && r.long_dir_prev !== r.long_dir
                ? '<div style="font-size:0.7rem;color:#999;margin-top:2px">上时段 ' + (r.long_dir_prev === 'long' ? '多' : '空') + '</div>' : '');
        return '<tr data-id="' + r.id + '">' +
            '<td style="white-space:nowrap">' + escapeHtml(r.ts || '') + '</td>' +
            '<td>' + escapeHtml(r.inst_id || '') + '</td>' +
            '<td>' + anaFmtPrice(r.price) + '</td>' +
            '<td>' + anaDirBadge(r.short_dir) + ' <span style="font-size:0.7rem;color:#999">' + escapeHtml(r.short_period || '') + '</span></td>' +
            '<td>' + longCell + ' <span style="font-size:0.7rem;color:#999">' + escapeHtml(r.long_period || '') + '</span></td>' +
            '<td>' + anaJudgmentBadge(r.user_judgment) + '</td>' +
            '<td class="ana-reason-cell" title="' + escapeHtml(r.user_reason || '') + '">' + (escapeHtml(r.user_reason || '') || '—') + '</td>' +
            '<td>' + anaReviewCell(r, 'price_1h', 'ts_1h', w.near.min) + '</td>' +
            '<td>' + anaReviewCell(r, 'price_4h', 'ts_4h', w.far.min) + '</td>' +
            '<td>' + anaResultCell(r) + '</td>' +
            '<td><div class="action-btns">' +
                '<button class="m-btn m-btn-primary m-btn-sm" onclick="editAnalysisRecord(' + r.id + ')">✏️</button>' +
                '<button class="m-btn m-btn-danger m-btn-sm" onclick="deleteAnalysisRecord(' + r.id + ')">🗑</button>' +
            '</div></td>' +
            '</tr>';
    }

    function renderAnalysisTable(rows) {
        const tbody = document.getElementById('ana-tbody');
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="11" class="empty-state">暂无分析记录，先生成快照并保存</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map(anaRowHtml).join('');
    }

    /* ============================================================
     * 局部刷新：改动后仅更新受影响的行与统计卡，不再整表重拉重建
     * ============================================================ */

    // 本地重算命中率统计（严格镜像后端 analysis_record_repo.compute_stats 口径）
    function computeStatsLocal(records) {
        const empty = () => ({total: 0, hit: 0, rate: 0.0, avg_pct: 0.0});
        const emptyConf = () => ({rise: {up: 0, flat: 0, down: 0},
                                  watch: {up: 0, flat: 0, down: 0},
                                  fall: {up: 0, flat: 0, down: 0}});
        const stats = {
            total: records.length,
            near_mult: _anaReviewMult.near, far_mult: _anaReviewMult.far,
            hit_k: _anaHitCfg.k, hit_floor_pct: _anaHitCfg.floor, hit_far_scale: _anaHitCfg.farScale,
            user: {near: empty(), far: empty()},
            strategy: {near: empty(), far: empty()},
            confusion: {near: emptyConf(), far: emptyConf()}
        };
        records.forEach(function(rec) {
            const price = Number(rec.price || 0);
            if (!(price > 0)) { return; }
            const atrPct = Number(rec.atr_pct || 0);
            const judgment = rec.user_judgment || '';
            const sdir = rec.long_dir_prev || rec.long_dir || '';
            [['near', 'price_1h', false], ['far', 'price_4h', true]].forEach(function(t) {
                const winKey = t[0], isFar = t[2];
                const follow = rec[t[1]];
                if (follow === null || follow === undefined || !(Number(follow) > 0)) { return; }
                const actual = anaClassify(price, Number(follow), atrPct, isFar);
                if (actual === null) { return; }
                const chg = (Number(follow) / price - 1) * 100;
                if (_JUDG_EXPECT[judgment]) {
                    const b = stats.user[winKey];
                    b.total += 1; b.avg_pct += chg;
                    if (_JUDG_EXPECT[judgment] === actual) { b.hit += 1; }
                    stats.confusion[winKey][judgment][actual] += 1;
                }
                if (_DIR_EXPECT[sdir]) {
                    const b = stats.strategy[winKey];
                    b.total += 1; b.avg_pct += chg;
                    if (_DIR_EXPECT[sdir] === actual) { b.hit += 1; }
                }
            });
        });
        ['user', 'strategy'].forEach(function(side) {
            ['near', 'far'].forEach(function(winKey) {
                const b = stats[side][winKey];
                if (b.total > 0) {
                    b.rate = Math.round(b.hit / b.total * 100 * 10) / 10;
                    b.avg_pct = Math.round(b.avg_pct / b.total * 1000) / 1000;
                } else { b.avg_pct = 0.0; }
            });
        });
        return stats;
    }

    function refreshAnaStats() { renderAnalysisStats(computeStatsLocal(_anaRecords)); }

    // 记录是否落在当前筛选条件内（镜像 anaFilterParams / 后端 query_records：字符串闭区间比较）
    function anaMatchesFilter(rec) {
        const inst = document.getElementById('ana-filter-inst').value;
        const judgment = document.getElementById('ana-filter-judgment').value;
        const start = document.getElementById('ana-filter-start').value;
        const end = document.getElementById('ana-filter-end').value;
        if (inst && rec.inst_id !== inst) { return false; }
        if (judgment && rec.user_judgment !== judgment) { return false; }
        if (start && (rec.ts || '') < start + ' 00:00:00') { return false; }
        if (end && (rec.ts || '') > end + ' 23:59:59') { return false; }
        return true;
    }

    function anaRowEl(id) { return document.querySelector('#ana-tbody tr[data-id="' + id + '"]'); }

    // 顶部插入新行（先清掉“暂无/加载中”占位行）
    function anaInsertRowTop(rec) {
        const tbody = document.getElementById('ana-tbody');
        if (!tbody.querySelector('tr[data-id]')) { tbody.innerHTML = ''; }
        tbody.insertAdjacentHTML('afterbegin', anaRowHtml(rec));
    }

    // 原地替换单行（判断/原因改动会连带结果列变化，整行重绘最稳妥）
    function anaUpdateRow(rec) {
        const tr = anaRowEl(rec.id);
        if (tr) { tr.outerHTML = anaRowHtml(rec); } else { anaInsertRowTop(rec); }
    }

    // 移除单行，全部删完则回落到空状态占位
    function anaRemoveRow(id) {
        const tr = anaRowEl(id);
        if (tr) { tr.remove(); }
        const tbody = document.getElementById('ana-tbody');
        if (!tbody.querySelector('tr[data-id]')) {
            tbody.innerHTML = '<tr><td colspan="11" class="empty-state">暂无分析记录，先生成快照并保存</td></tr>';
        }
    }

    function editAnalysisRecord(id) {
        const rec = _anaRecords.find(r => r.id === id);
        if (!rec) { showToast('记录不存在，请刷新', 'error'); return; }
        const j = rec.user_judgment || 'watch';
        MDialog.show({
            title: '✏️ 修改判断与原因（' + rec.inst_id + ' ' + rec.ts + '）',
            message: '<div style="display:flex;gap:14px;margin-bottom:10px;font-size:0.88rem">' +
                '<label><input type="radio" name="ana-edit-judgment" value="rise"' + (j === 'rise' ? ' checked' : '') + '> 涨</label>' +
                '<label><input type="radio" name="ana-edit-judgment" value="fall"' + (j === 'fall' ? ' checked' : '') + '> 跌</label>' +
                '<label><input type="radio" name="ana-edit-judgment" value="watch"' + (j === 'watch' ? ' checked' : '') + '> 观望</label>' +
                '</div>' +
                '<textarea id="ana-edit-reason" class="ana-reason-input" style="width:100%;min-height:80px;box-sizing:border-box">' +
                escapeHtml(rec.user_reason || '') + '</textarea>',
            okText: '保存修改',
            onOk: function() {
                const judgment = (document.querySelector('input[name="ana-edit-judgment"]:checked') || {}).value || 'watch';
                const reason = document.getElementById('ana-edit-reason').value.trim();
                fetch('/api/task/analysis/records/' + id, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({user_judgment: judgment, user_reason: reason})
                }).then(r => r.json()).then(res => {
                    showToast(res.message, res.code === 200 ? 'success' : 'error');
                    if (res.code === 200) {
                        // 只更新内存中这一条 + 原地重绘该行；若判断改动后不再命中筛选则移除该行
                        const rec = _anaRecords.find(r2 => r2.id === id);
                        if (rec) {
                            rec.user_judgment = judgment;
                            rec.user_reason = reason;
                            if (anaMatchesFilter(rec)) { anaUpdateRow(rec); }
                            else { _anaRecords = _anaRecords.filter(r2 => r2.id !== id); anaRemoveRow(id); }
                            refreshAnaStats();
                        }
                    }
                }).catch(e => showToast('修改失败: ' + e, 'error'));
            }
        });
    }

    function deleteAnalysisRecord(id) {
        MDialog.danger('确定要删除这条分析记录吗？此操作不可恢复。', function() {
            fetch('/api/task/analysis/records/' + id, {method: 'DELETE'})
                .then(r => r.json()).then(res => {
                    showToast(res.message, res.code === 200 ? 'success' : 'error');
                    if (res.code === 200) {
                        // 本地删这一条 + 移除该行 + 本地重算统计（不再整表重拉）
                        _anaRecords = _anaRecords.filter(r2 => r2.id !== id);
                        anaRemoveRow(id);
                        refreshAnaStats();
                    }
                }).catch(e => showToast('删除失败: ' + e, 'error'));
        });
    }

    /* ---- 暴露给内联 onclick 与 switchTab 调用（其余保持 IIFE 私有） ---- */
    window.initAnalysisTab = initAnalysisTab;
    window.loadAnalysisRecords = loadAnalysisRecords;
    window.genAnalysisSnapshot = genAnalysisSnapshot;
    window.hideAnalysisSnapshot = hideAnalysisSnapshot;
    window.saveAnalysisRecord = saveAnalysisRecord;
    window.genBatchSnapshot = genBatchSnapshot;
    window.toggleAllBatchRows = toggleAllBatchRows;
    window.hideBatchSnapshot = hideBatchSnapshot;
    window.saveBatchSnapshots = saveBatchSnapshots;
    window.editAnalysisRecord = editAnalysisRecord;
    window.deleteAnalysisRecord = deleteAnalysisRecord;
})();
