/* ================================================================
   任务计划系统 - 前端逻辑（长期主义版本 Longtermism v1.1）
   依赖: mdialog.js (MDialog)
   功能: 计划总览 / 今日在场 / 在场日历 / 任务卡网格 / 100格打卡 / 核算结算

   【长期主义设计理念】
   - 低门槛启动：随时想开始就开始，几分钟也算在场，没有心理负担
   - 坚持比强度更重要：核心指标是"在场天数"，不是"每日时长"
   - 不惧中断：允许中断与回归，回来永远被欢迎，不显示任何罚款/缺口
   - 今天在场，比今天做多少更重要
   - 身份认同：你是一个长期主义者——行为由身份驱动，而非压力驱动
   - 历史最佳：断档不可怕，大字只显示最佳连续在场；在场日历如实呈现
   ================================================================ */
var TaskPlan = (function() {
    'use strict';

    /* 格式化本地时间为 datetime-local 输入框所需的格式 */
    function formatLocalDateTime(d) {
        function pad(n) { return n < 10 ? '0' + n : '' + n; }
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
            'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }

    /* 后端时间（YYYY-MM-DD HH:MM[:SS]）→ datetime-local 输入值（YYYY-MM-DDTHH:MM） */
    function toDatetimeLocal(v) {
        if (!v) return '';
        return String(v).slice(0, 16).replace(' ', 'T');
    }

    /* datetime-local 输入值 → 后端存储格式（YYYY-MM-DD HH:MM:SS） */
    function fromDatetimeLocal(v) {
        if (!v) return '';
        return v.replace('T', ' ') + ':00';
    }

    var state = {
        plans: [],
        allCards: [],      // 合并所有计划的卡片，每张卡附带 plan_id
        today: [],         // 今日在场面板数据（按 plan_id 局部刷新，免重复请求 today-status）
        currentCard: null,
        activeTab: 'overview'
    };

    var TABS = [
        { key: 'overview', label: '📋 概览' },
        { key: 'slots', label: '🕐 打卡' },
        { key: 'checkin', label: '📊 打卡详情' },
        { key: 'notes', label: '📝 小记' },
        { key: 'settle', label: '💰 结算' }
    ];

    /* 弹窗内 ECharts 实例统一管理（切 Tab/关弹窗时销毁，避免内存泄漏） */
    var modalCharts = [];
    function disposeModalCharts() {
        modalCharts.forEach(function(c) { try { c.dispose(); } catch (e) { /* 忽略 */ } });
        modalCharts = [];
    }

    var FORM_CSS = '' +
        '.plan-form .form-row { margin-bottom: 10px; }' +
        '.plan-form label { display: block; font-size: .85rem; color: var(--text-muted); margin-bottom: 4px; }' +
        '.plan-form input, .plan-form select { width: 100%; padding: 7px 10px; border: 1px solid var(--border-color); border-radius: 6px; font-size: .9rem; box-sizing: border-box; }' +
        '.slot-form label { display: block; font-size: .85rem; color: var(--text-muted); margin: 8px 0 4px; }' +
        '.slot-form input, .slot-form select, .slot-form textarea { width: 100%; padding: 7px 10px; border: 1px solid var(--border-color); border-radius: 6px; font-size: .9rem; box-sizing: border-box; font-family: inherit; }' +
        '.slot-form textarea { min-height: 60px; resize: vertical; }' +
        /* 分析纪律闸门状态条（批次11）：贴在打卡表单顶部，一眼看出能不能打卡 */
        '.sf-gate { display: flex; align-items: flex-start; gap: 8px; flex-wrap: wrap;' +
        ' padding: 9px 11px; border-radius: 8px; border: 1px solid; margin-bottom: 10px;' +
        ' font-size: .82rem; line-height: 1.55; }' +
        '.sf-gate.hide { display: none; }' +
        '.sf-gate-txt { flex: 1; min-width: 180px; }' +
        '.sf-gate-acts { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }' +
        '.sf-gate.ok { background: #ebfbee; border-color: #b2f2bb; color: #2b8a3e; }' +
        '.sf-gate.block { background: #fff5f5; border-color: #ffc9c9; color: #c92a2a; }' +
        '.sf-gate.warn { background: #fff9db; border-color: #ffe066; color: #8a6d00; }' +
        '.sf-gate.idle { background: #f8f9fa; border-color: var(--border-color); color: var(--text-muted); }' +
        '.sf-gate b { font-variant-numeric: tabular-nums; }' +
        '.sf-gate-list { margin: 4px 0 0; padding-left: 16px; font-size: .78rem; opacity: .9; }';

    /* ---------- 工具 ---------- */
    function $(sel, root) { return (root || document).querySelector(sel); }
    function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

    function esc(s) {
        if (s === null || s === undefined) return '';
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function fmtNum(n) {
        n = Number(n) || 0;
        return (Math.round(n * 10) / 10).toString();
    }

    function statusText(st) {
        var map = { pending: '待开始', in_progress: '进行中', completed: '已完成', failed: '已失败', abandoned: '已放弃' };
        return map[st] || st;
    }

    function calcHitRate(card) {
        // 命中率由后端预算（list/card-detail 的 hit_rate 字段），前端不再遍历 slots
        return Math.round(card.hit_rate || 0);
    }

    /* ---------- API ---------- */
    function api(url, method, data) {
        var opts = { method: method || 'GET', headers: { 'Content-Type': 'application/json' } };
        if (data) opts.body = JSON.stringify(data);
        return fetch(url, opts).then(function(r) { return r.json(); });
    }
    function apiGet(url) { return api(url, 'GET'); }
    function apiPost(url, data) { return api(url, 'POST', data); }

    /* ---------- 状态辅助 ---------- */
    function getPlanByType(type) {
        return state.plans.find(function(p) { return p.type === type; }) || null;
    }

    function findCard(cardId) {
        return state.allCards.find(function(c) { return c.id === cardId; }) || null;
    }

    /* 下一个学习任务默认继承上一个的奖励；下一个交易任务默认上一轮 + 2000 */
    function nextLearnReward(plan) {
        var learnCards = (plan.stats && plan.stats.cards || []).filter(function(c) { return c.type === 'learn'; });
        if (!learnCards.length) return 2000;
        var last = learnCards[learnCards.length - 1];
        return last.reward || 2000;
    }
    function nextTradeReward(plan) {
        var tradeCards = (plan.stats && plan.stats.cards || []).filter(function(c) { return c.type === 'trade'; });
        if (!tradeCards.length) return 2000;
        var last = tradeCards[tradeCards.length - 1];
        return (last.base_reward || last.reward || 2000) + 2000;
    }

    /* ---------- 加载 ---------- */
    function loadAll() {
        return apiGet('/plan/api/list').then(function(res) {
            if (res.code !== 200) { MDialog.alert(res.message || '加载失败'); return; }
            state.plans = res.data || [];
            // 合并所有计划的卡片，附带 plan_id 供 API 调用
            state.allCards = [];
            state.plans.forEach(function(p) {
                (p.stats.cards || []).forEach(function(c) {
                    state.allCards.push(Object.assign({}, c, { plan_id: p.id }));
                });
            });
            renderOverview();
            renderCheckinStatsChart();
            renderPlanDetail();
            loadToday();
        }).catch(function(err) {
            MDialog.alert('请求失败: ' + err.message);
        });
    }

    /* ---------- 总览统计栏（长期主义版：在场天数取代罚款） ---------- */
    function renderOverview() {
        var el = $('#plan-overview');
        if (!el) return;
        var learnDone = 0, learnTotal = 0, tradeDone = 0, tradeTotal = 0;
        var learnHours = 0, tradeHours = 0, reward = 0;
        var presenceDays = 0, bestStreakDays = 0;
        var hitSum = 0, hitCount = 0;
        var learnCheckins = 0, tradeCheckins = 0;

        state.plans.forEach(function(p) {
            var s = p.stats || {};
            learnDone += s.learn_completed || 0; learnTotal += s.learn_total || 0;
            tradeDone += s.trade_completed || 0; tradeTotal += s.trade_total || 0;
            learnHours += s.learn_total_hours || 0; tradeHours += s.trade_total_hours || 0;
            reward += s.total_reward || 0;
            presenceDays += s.total_presence_days || 0;
            bestStreakDays = Math.max(bestStreakDays, s.best_streak_days || 0);
            learnCheckins += s.learn_checkin_total || 0;
            tradeCheckins += s.trade_checkin_total || 0;
            if (s.hit_rate !== undefined) { hitSum += s.hit_rate; hitCount++; }
        });
        var hitRate = hitCount ? Math.round(hitSum / hitCount) : 0;

        var items = [
            { num: learnDone + ' / ' + learnTotal, label: '📚 学习任务完成' },
            { num: tradeDone + ' / ' + tradeTotal, label: '💰 交易任务完成' },
            { num: learnCheckins + ' 次', label: '📚 学习打卡总次数' },
            { num: tradeCheckins + ' 次', label: '💰 交易打卡总次数' },
            { num: fmtNum(learnHours) + 'h', label: '⏱ 累计学习时长' },
            { num: fmtNum(tradeHours) + 'h', label: '⏱ 累计交易时长' },
            { num: presenceDays + ' 天', label: '🌱 长期主义在册天数' },
            { num: bestStreakDays + ' 天', label: '🔥 最佳连续在场（历史）' },
            { num: fmtNum(reward) + ' 元', label: '💵 已结算奖励' },
            { num: hitRate + '%', label: '🎯 预测命中率' }
        ];
        el.innerHTML = items.map(function(it) {
            return '<div class="overview-item"><div class="ov-num">' + esc(it.num) + '</div><div class="ov-label">' + esc(it.label) + '</div></div>';
        }).join('');
    }

    /* ---------- 每日/累积打卡统计双曲线图（严格区分学习与交易） ---------- */
    var mainCheckinChart = null;

    function renderCheckinStatsChart() {
        var el = $('#checkin-stats-panel');
        if (!el) return;

        /* 聚合各计划 checkin_daily 历史序列：date -> {learn, trade} */
        var byDate = {};
        state.plans.forEach(function(p) {
            (((p.stats || {}).checkin_daily) || []).forEach(function(d) {
                if (!byDate[d.date]) byDate[d.date] = { learn: 0, trade: 0 };
                byDate[d.date].learn += d.learn || 0;
                byDate[d.date].trade += d.trade || 0;
            });
        });
        var dates = Object.keys(byDate).sort();

        if (mainCheckinChart) { mainCheckinChart.dispose(); mainCheckinChart = null; }
        if (!dates.length) { el.style.display = 'none'; return; }

        /* 每日次数 + 逐日累加得到累积序列 */
        var dailyLearn = [], dailyTrade = [], cumLearn = [], cumTrade = [];
        var cumL = 0, cumT = 0;
        dates.forEach(function(d) {
            dailyLearn.push(byDate[d].learn);
            dailyTrade.push(byDate[d].trade);
            cumL += byDate[d].learn;
            cumT += byDate[d].trade;
            cumLearn.push(cumL);
            cumTrade.push(cumT);
        });

        el.innerHTML = '<div class="cf-title">🗂 打卡统计趋势（每日 / 累积双曲线 · 严格区分学习与交易 · 滚轮/拖动缩放）</div>' +
            '<div id="checkin-stats-chart" style="width:100%;height:340px;"></div>';
        el.style.display = '';

        /* 超过 30 天时默认聚焦最近 30 天，仍可拖动查看全量 */
        var zoomStart = dates.length > 30 ? Math.round((1 - 30 / dates.length) * 100) : 0;

        mainCheckinChart = echarts.init(document.getElementById('checkin-stats-chart'));
        mainCheckinChart.setOption({
            tooltip: {
                trigger: 'axis',
                axisPointer: { type: 'cross' },
                formatter: function(ps) {
                    if (!ps.length) return '';
                    var tip = '<b>' + ps[0].axisValue + '</b>';
                    ps.forEach(function(p) {
                        tip += '<br/>' + p.marker + ' ' + p.seriesName + '：<b>' + p.value + '</b> 次';
                    });
                    return tip;
                }
            },
            legend: { top: 0, data: ['每日学习打卡', '每日交易打卡', '累积学习打卡', '累积交易打卡'], textStyle: { fontSize: 11 } },
            grid: { left: 44, right: 48, top: 40, bottom: 52 },
            xAxis: {
                type: 'category',
                data: dates,
                boundaryGap: false,
                axisLabel: { fontSize: 10, rotate: dates.length > 12 ? 45 : 0 }
            },
            yAxis: [
                { type: 'value', name: '每日（次）', minInterval: 1 },
                { type: 'value', name: '累积（次）', minInterval: 1, splitLine: { show: false } }
            ],
            dataZoom: [
                { type: 'inside', start: zoomStart, end: 100 },
                { type: 'slider', height: 16, bottom: 6, start: zoomStart, end: 100 }
            ],
            series: [
                {
                    name: '每日学习打卡', type: 'line', yAxisIndex: 0,
                    data: dailyLearn, smooth: true,
                    symbol: 'circle', symbolSize: 5,
                    lineStyle: { width: 2, color: '#007bff' },
                    itemStyle: { color: '#007bff' }
                },
                {
                    name: '每日交易打卡', type: 'line', yAxisIndex: 0,
                    data: dailyTrade, smooth: true,
                    symbol: 'circle', symbolSize: 5,
                    lineStyle: { width: 2, color: '#fd7e14' },
                    itemStyle: { color: '#fd7e14' }
                },
                {
                    name: '累积学习打卡', type: 'line', yAxisIndex: 1,
                    data: cumLearn, smooth: true,
                    symbol: 'circle', symbolSize: 5,
                    lineStyle: { width: 2, color: '#28a745' },
                    itemStyle: { color: '#28a745' },
                    areaStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: 'rgba(40, 167, 69, 0.18)' },
                            { offset: 1, color: 'rgba(40, 167, 69, 0)' }
                        ])
                    }
                },
                {
                    name: '累积交易打卡', type: 'line', yAxisIndex: 1,
                    data: cumTrade, smooth: true,
                    symbol: 'circle', symbolSize: 5,
                    lineStyle: { width: 2, color: '#6f42c1' },
                    itemStyle: { color: '#6f42c1' }
                }
            ]
        });
    }

    /* ---------- 今日在场面板（长期主义版：只报喜不施压） ---------- */
    function loadToday() {
        apiGet('/plan/api/today-status').then(function(res) {
            if (res.code !== 200) return;
            state.today = res.data || [];
            renderToday();
        });
    }

    /* 今日在场面板：从 state.today 就地渲染（整表加载与打卡局部刷新共用） */
    function renderToday() {
        var el = $('#today-panel');
        if (!el) return;
        if (!state.today || !state.today.length) { el.innerHTML = ''; return; }
        var html = '';
        var calendars = [];
        state.today.forEach(function(t) {
            var totalToday = (t.today_learn_hours || 0) + (t.today_trade_hours || 0);
            var presentText = totalToday > 0 ? '✅ 今天也在场 · 欢迎回来，长期主义者 🌱' : '🌙 今天还没开始，随时欢迎 👋';
            var presentCls = totalToday > 0 ? 'present-ok' : 'present-idle';
            html += '<div class="today-welcome ' + presentCls + '">' + esc(presentText) + '</div>';
            html += '<div class="today-item"><span class="dot dot-ok"></span>📚 今日学习 <b>' + fmtNum(t.today_learn_hours) + 'h</b> · 学习累计在场 <b>' + t.learn_presence_days + ' 天</b></div>';
            html += '<div class="today-item"><span class="dot dot-ok"></span>💰 今日交易 <b>' + fmtNum(t.today_trade_hours) + 'h</b> · 交易累计在场 <b>' + t.trade_presence_days + ' 天</b></div>';
            html += '<div class="today-item today-hint">💡 今天做多少都可以——只要来了，就是胜利</div>';
            if (t.presence_calendar) calendars.push(t.presence_calendar);
        });
        // v1.1 在场日历：多计划合并渲染，断档如实呈现
        html += renderPresenceCalendar(mergeCalendars(calendars));
        el.innerHTML = html;
    }

    /* ---------- 在场日历（v1.1：GitHub 贡献图风格，断档如实呈现） ---------- */
    function mergeCalendars(cals) {
        if (!cals || !cals.length) return null;
        var base = null;
        var map = {};
        cals.forEach(function(cal) {
            if (!cal || !cal.days || !cal.days.length) return;
            if (!base) base = cal;
            cal.days.forEach(function(d) {
                map[d.date] = (map[d.date] || 0) + (d.minutes || 0);
            });
        });
        if (!base) return null;
        return {
            start_date: base.start_date,
            weeks: base.weeks,
            days: base.days.map(function(d) {
                return { date: d.date, minutes: map[d.date] || 0, future: d.future };
            })
        };
    }

    function calLevel(m) {
        if (m <= 0) return 0;
        if (m < 30) return 1;
        if (m < 60) return 2;
        if (m < 120) return 3;
        return 4;
    }

    function renderPresenceCalendar(cal) {
        if (!cal || !cal.days || !cal.days.length) return '';
        var days = cal.days;
        var weeks = cal.weeks || 15;
        var html = '<div class="cal-wrap">';
        html += '<div class="cal-title">🌱 在场日历（近 ' + weeks + ' 周 · 断档如实呈现，随时可以续上）</div>';
        html += '<div class="cal-grid">';
        html += '<div class="cal-rows">' + ['一', '', '三', '', '五', '', '日'].map(function(l) {
            return '<div class="cal-row-label">' + l + '</div>';
        }).join('') + '</div>';
        for (var w = 0; w < weeks; w++) {
            html += '<div class="cal-col">';
            for (var r = 0; r < 7; r++) {
                var d = days[w * 7 + r];
                if (!d) { html += '<div class="cal-cell empty"></div>'; continue; }
                var lv = calLevel(d.minutes);
                var tip = d.date + (d.minutes > 0 ? ' · 在场 ' + d.minutes + ' 分钟' : ' · 未在场') + (d.future ? '（未来）' : '');
                html += '<div class="cal-cell lv' + lv + (d.future ? ' future' : '') + '" title="' + esc(tip) + '"></div>';
            }
            html += '</div>';
        }
        html += '</div>';
        html += '<div class="cal-legend"><span>少</span>' +
            '<span class="cal-cell lv1" title="1-29 分钟"></span>' +
            '<span class="cal-cell lv2" title="30-59 分钟"></span>' +
            '<span class="cal-cell lv3" title="60-119 分钟"></span>' +
            '<span class="cal-cell lv4" title="120+ 分钟"></span><span>多</span></div>';
        html += '</div>';
        return html;
    }

    /* ---------- 任务卡列表（按状态分组） ---------- */
    function renderPlanDetail() {
        var el = $('#plan-detail');
        if (!el) return;
        var grid = $('#card-grid');
        var cards = state.allCards;
        if (!cards.length) {
            el.querySelector('.card-section-title').style.display = 'none';
            grid.innerHTML = '<div class="empty-state"><div class="icon">📭</div>暂无任务卡，点击右上角按钮创建你的第一个任务</div>';
            return;
        }
        el.querySelector('.card-section-title').style.display = '';

        var completed = cards.filter(function(c) { return c.status === 'completed'; }).length;
        $('#section-title').textContent = '📋 全部任务卡（完成 ' + completed + ' / ' + cards.length + '）';

        // 按状态分组：进行中置顶 → 待开始（折叠）→ 已完成 → 已结束（失败/放弃）
        var groups = [
            { key: 'in_progress', title: '🔥 进行中', cards: [] },
            { key: 'pending', title: '📥 待开始', cards: [] },
            { key: 'completed', title: '✅ 已完成', cards: [] },
            { key: 'ended', title: '⚰️ 已结束（失败/放弃）', cards: [] }
        ];
        cards.forEach(function(c) {
            var g = groups.find(function(x) {
                return (x.key === 'in_progress' && c.status === 'in_progress') ||
                    (x.key === 'pending' && c.status === 'pending') ||
                    (x.key === 'completed' && c.status === 'completed') ||
                    (x.key === 'ended' && c.status !== 'in_progress' && c.status !== 'pending' && c.status !== 'completed');
            });
            if (g) g.cards.push(c);
        });

        var html = '';
        groups.forEach(function(g) {
            if (!g.cards.length) return;
            html += '<div class="status-group">' +
                '<div class="status-group-title sg-' + g.key + '">' + g.title + '<span class="sg-count">' + g.cards.length + ' 张</span></div>' +
                '<div class="card-grid">' + g.cards.map(function(c) { return renderCardItem(c); }).join('') + '</div>' +
                '</div>';
        });
        grid.innerHTML = html;

        $all('.task-card', grid).forEach(function(node) {
            node.addEventListener('click', function(e) {
                if (e.target.closest('.tc-collapse-btn')) {
                    // 待开始卡：展开 / 收起
                    node.classList.toggle('collapsed');
                    return;
                }
                if (e.target.closest('.tc-reset-btn')) {
                    // 重置按钮：不打开弹窗
                    return;
                }
                openCardModal(node.dataset.cardId);
            });
        });
        $all('.tc-reset-btn', grid).forEach(function(node) {
            node.addEventListener('click', function(e) {
                e.stopPropagation();
                resetCard(node.dataset.cardId);
            });
        });
        $all('.btn-add-card', el).forEach(function(node) {
            node.addEventListener('click', function() { openCreateCard(node.dataset.type); });
        });
    }

    function renderCardItem(card) {
        var isPending = card.status === 'pending';
        var pct = Math.min(100, Math.round(card.filled_count));
        var cls = 'task-card st-' + card.status + (card.locked ? ' locked' : '') + (isPending ? ' collapsed' : '');
        var ri = card.reward_info || {};
        var isTrade = card.type === 'trade';
        var html = '<div class="' + cls + '" data-card-id="' + card.id + '">';

        // 头部（标题行，折叠卡也只显示这一行）
        html += '<div class="tc-head">' +
            '<span class="tc-type ' + card.type + '">' + (isTrade ? '💰 交易' : '📚 学习') + '</span>' +
            '<div class="tc-title">' + esc(card.title) + '</div>' +
            (card.locked ? '<span class="tc-lock-icon">🔒</span>' : '') +
            '<span class="tc-status">' + statusText(card.status) + '</span>' +
            '<button class="tc-reset-btn" data-card-id="' + card.id + '" title="重置卡片（清空全部打卡与状态）">🔄</button>' +
            '</div>';

        // 正文（待开始卡折叠时隐藏）
        html += '<div class="tc-body">';
        html += '<div class="tc-round">第 ' + card.round + ' 张 · ' + (isTrade ? '翻倍挑战' : '100 小时学习') + '</div>';
        html += '<div class="tc-progress"><div class="tc-bar"><div class="tc-bar-fill" style="width:' + pct + '%"></div></div></div>';
        // 长期主义信息：在场天数 + 预计奖励（无罚款）
        html += '<div class="tc-meta"><span>' + card.filled_count + '/100h</span><span class="tc-reward">💰 预计奖励 ' + fmtNum(ri.final_reward) + ' 元</span></div>';
        html += '<div class="tc-meta tc-meta-2">' +
            '<span class="tc-presence">🌱 在场 ' + (ri.presence_days || 0) + ' 天</span>' +
            '<span class="tc-hit">' + (isTrade ? '🎯 命中 ' + calcHitRate(card) + '%' : '💵 基础 ' + fmtNum(ri.base_reward) + ' 元') + '</span>' +
            '</div>';
        if (isTrade) {
            html += '<div class="tc-meta tc-meta-2"><span class="tc-hit">⏱ 提前奖 ' + fmtNum(ri.early_bonus) + ' 元</span><span></span></div>';
        } else if (ri.early_eligible) {
            html += '<div class="tc-meta tc-meta-2"><span class="tc-hit">🎯 子目标已全部完成，可提前通关！⏱ 提前奖 ' + fmtNum(ri.early_bonus) + ' 元</span><span></span></div>';
        }
        html += '</div>';

        // 折叠展开按钮（仅待开始卡）
        if (isPending) {
            html += '<div class="tc-collapse-btn"><span class="tc-collapse-open">🔒 待开始 · ▼ 点击展开查看详情</span><span class="tc-collapse-close">▲ 收起</span></div>';
        }

        html += '</div>';
        return html;
    }

    /* ================================================================
       卡片详情弹窗
       ================================================================ */
    function openCardModal(cardId, tab) {
        var summary = findCard(cardId);
        if (!summary) return;
        if (tab) state.activeTab = tab;

        $('#cm-title').textContent = summary.title;
        $('#card-modal').style.display = 'flex';
        $('#cm-body').innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted);">加载中…</div>';
        // 列表只存摘要，完整 slots/notes/milestones 按需从 card-detail 取
        apiGet('/plan/api/card-detail?plan_id=' + encodeURIComponent(summary.plan_id) + '&card_id=' + encodeURIComponent(cardId)).then(function(res) {
            if (res.code !== 200 || !res.data) {
                MDialog.alert(res.message || '加载卡片详情失败');
                closeCardModal();
                return;
            }
            state.currentCard = res.data;
            renderCardTabs();
            switchTab(state.activeTab);
        }).catch(function(err) {
            MDialog.alert('请求失败: ' + err.message);
            closeCardModal();
        });
    }

    function closeCardModal() {
        disposeModalCharts();
        $('#card-modal').style.display = 'none';
        state.currentCard = null;
    }

    function renderCardTabs() {
        var tabsEl = $('#cm-tabs');
        tabsEl.innerHTML = '';
        var card = state.currentCard;
        // 判断是否需要给结算 Tab 加提示徽章：子目标全完成 + 卡仍在进行中 + 尚未结算
        var showSettleBadge = card && card.type === 'learn' && card.status === 'in_progress' && !card.settlement &&
            card.milestones && card.milestones.length > 0 &&
            card.milestones.every(function(m) { return m.done; });
        TABS.forEach(function(t) {
            var b = document.createElement('button');
            b.className = 'cm-tab' + (t.key === state.activeTab ? ' active' : '');
            b.textContent = t.label;
            b.dataset.tab = t.key;
            // 结算 Tab 徽章：提示用户可提前通关
            if (t.key === 'settle' && showSettleBadge) {
                var dot = document.createElement('span');
                dot.className = 'cm-tab-badge';
                dot.textContent = '!';
                b.appendChild(dot);
                b.classList.add('cm-tab-glow');
            }
            tabsEl.appendChild(b);
        });
        $all('.cm-tab', tabsEl).forEach(function(b) {
            b.addEventListener('click', function() { switchTab(b.dataset.tab); });
        });
    }

    function switchTab(key) {
        state.activeTab = key;
        var card = state.currentCard;
        if (!card) return;
        disposeModalCharts();
        $all('.cm-tab', $('#cm-tabs')).forEach(function(b) {
            b.classList.toggle('active', b.dataset.tab === key);
        });
        var body = $('#cm-body');
        if (key === 'overview') renderOverviewTab(card);
        else if (key === 'slots') renderSlotsTab(card);
        else if (key === 'checkin') renderCheckinTab(card);
        else if (key === 'notes') renderNotesTab(card);
        else if (key === 'settle') renderSettleTab(card);
    }

    /* ---- 概览 Tab ---- */
    function renderOverviewTab(card) {
        var html = '<div class="cm-overview">';

        /* 提前通关引导横幅：子目标全完成 + 卡仍在进行中 */
        var msAllDone = card.milestones && card.milestones.length > 0 &&
            card.milestones.every(function(m) { return m.done; });
        if (msAllDone && card.status === 'in_progress' && card.type === 'learn') {
            html += '<div class="ov-early-banner">' +
                '🎉 <b>子目标已全部完成！</b> 你可以立即结算并解锁下一张任务卡 ' +
                '<button class="m-btn m-btn-primary m-btn-sm" id="btn-early-settle" style="margin-left:12px;">去结算 💰</button>' +
                '</div>';
        }

        html += '<div class="ov-row"><span class="label">任务目标</span><textarea id="ov-goal">' + esc(card.goal || '') + '</textarea></div>';

        if (card.type === 'learn') {
            html += '<div class="ov-row"><span class="label">完成奖励</span><input type="number" id="ov-reward" value="' + esc(card.reward) + '"><span style="flex-shrink:0;margin-left:6px;">元（时薪 ' + fmtNum((card.reward || 2000) / 100) + ' 元）</span></div>';
        } else {
            html += '<div class="ov-row"><span class="label">基础奖金</span><input type="number" id="ov-reward" value="' + esc(card.base_reward) + '"><span style="flex-shrink:0;margin-left:6px;">元（时薪 ' + fmtNum(card.hourly_rate || 20) + ' 元）</span></div>';
        }

        // 开始/结束时间：可视化时间选择器，默认当前时间（已有值则显示原值）
        var startVal = toDatetimeLocal(card.start_time) || formatLocalDateTime(new Date());
        var endVal = toDatetimeLocal(card.end_time) || formatLocalDateTime(new Date());
        html += '<div class="ov-row"><span class="label">开始时间</span><input type="datetime-local" id="ov-start" data-default="' + startVal + '" value="' + startVal + '"></div>';
        html += '<div class="ov-row"><span class="label">结束时间</span><input type="datetime-local" id="ov-end" data-default="' + endVal + '" value="' + endVal + '"></div>';

        var pct = Math.min(100, Math.round(card.filled_count));
        html += '<div class="cm-big-progress"><div class="cm-big-bar"><div class="cm-big-fill" style="width:' + pct + '%"></div></div>' +
            '<div style="font-size:.85rem;color:var(--text-muted);margin-top:4px;">已完成 ' + card.filled_count + ' / 100 小时（' + pct + '%）</div></div>';

        var ms = card.milestones || [];
        var msDone = ms.filter(function(m) { return m.done; }).length;
        html += '<div style="font-size:.9rem;font-weight:600;margin:14px 0 8px;">🎯 目标拆解（' + msDone + ' / ' + ms.length + '）</div>';
        if (!ms.length) html += '<p style="font-size:.85rem;color:var(--text-muted);">还没有子目标，添加一个吧</p>';
        ms.forEach(function(m, i) {
            html += '<div class="milestone-item' + (m.done ? ' done' : '') + '">' +
                '<input type="checkbox" data-ms-index="' + i + '"' + (m.done ? ' checked' : '') + '>' +
                '<span class="ms-content">' + esc(m.content) + '</span>' +
                '<button class="ms-action ms-edit" data-ms-index="' + i + '" title="编辑">✏️</button>' +
                '<button class="ms-action ms-del" data-ms-index="' + i + '" title="删除">🗑</button>' +
                '</div>';
        });
        html += '<div class="milestone-add"><input id="ms-input" placeholder="新增子目标，回车或点添加"><button class="m-btn m-btn-sm" id="ms-add">添加</button></div>';

        /* 模块6：TodoList 待办清单（添加/编辑/删除/打钩，实时持久化到后端） */
        var todos = card.todos || [];
        var todoDone = todos.filter(function(t) { return t.done; }).length;
        html += '<div style="font-size:.9rem;font-weight:600;margin:14px 0 8px;">✅ 待办清单 TodoList（' + todoDone + ' / ' + todos.length + '）</div>';
        if (!todos.length) {
            html += '<p style="font-size:.85rem;color:var(--text-muted);">还没有待办事项，在下方添加一个吧</p>';
        } else {
            html += '<div id="todo-list">';
            todos.forEach(function(t, i) {
                html += '<div class="todo-item' + (t.done ? ' done' : '') + '">' +
                    '<input type="checkbox" data-todo-index="' + i + '"' + (t.done ? ' checked' : '') + '>' +
                    '<span class="todo-content">' + esc(t.content) + '</span>' +
                    '<button class="todo-action todo-edit" data-todo-index="' + i + '" title="编辑">✏️</button>' +
                    '<button class="todo-action todo-del" data-todo-index="' + i + '" title="删除">🗑</button>' +
                    '</div>';
            });
            html += '</div>';
        }
        html += '<div class="milestone-add"><input id="todo-input" placeholder="新增待办事项，回车或点添加"><button class="m-btn m-btn-sm" id="todo-add">添加</button></div>';

        html += '<div style="margin-top:18px;display:flex;gap:8px;flex-wrap:wrap;">';
        html += '<button class="m-btn m-btn-primary" id="btn-save-overview">💾 保存修改</button>';
        if (card.status === 'in_progress') html += '<button class="m-btn" id="btn-abandon">放弃任务</button>';
        if (card.status !== 'completed' && card.status !== 'failed') html += '<button class="m-btn" id="btn-delete" style="color:#dc3545;">删除任务卡</button>';
        html += '<button class="m-btn" id="btn-reset" style="color:#e07b00;">🔄 重置卡片</button>';
        html += '</div></div>';

        $('#cm-body').innerHTML = html;

        $('#btn-save-overview').addEventListener('click', function() { saveCardFields(card); });

        var btnEarlySettle = $('#btn-early-settle');
        if (btnEarlySettle) btnEarlySettle.addEventListener('click', function() {
            state.activeTab = 'settle';
            switchTab('settle');
        });

        var msAdd = $('#ms-add');
        if (msAdd) {
            msAdd.addEventListener('click', function() { addMilestone(card); });
            $('#ms-input').addEventListener('keydown', function(e) { if (e.key === 'Enter') addMilestone(card); });
        }
        $all('.milestone-item input[type="checkbox"]').forEach(function(cb) {
            cb.addEventListener('change', function() { toggleMilestone(card, parseInt(cb.dataset.msIndex)); });
        });
        $all('.milestone-item .ms-edit').forEach(function(btn) {
            btn.addEventListener('click', function() { startEditMilestone(card, parseInt(btn.dataset.msIndex)); });
        });
        $all('.milestone-item .ms-del').forEach(function(btn) {
            btn.addEventListener('click', function() { deleteMilestone(card, parseInt(btn.dataset.msIndex)); });
        });
        /* TodoList 事件绑定 */
        var todoAdd = $('#todo-add');
        if (todoAdd) {
            todoAdd.addEventListener('click', function() { addTodo(card); });
            $('#todo-input').addEventListener('keydown', function(e) { if (e.key === 'Enter') addTodo(card); });
        }
        $all('#todo-list input[type="checkbox"]').forEach(function(cb) {
            cb.addEventListener('change', function() { toggleTodo(card, parseInt(cb.dataset.todoIndex)); });
        });
        $all('.todo-edit').forEach(function(btn) {
            btn.addEventListener('click', function() { startEditTodo(card, parseInt(btn.dataset.todoIndex)); });
        });
        $all('.todo-del').forEach(function(btn) {
            btn.addEventListener('click', function() { deleteTodo(card, parseInt(btn.dataset.todoIndex)); });
        });
        var btnAbandon = $('#btn-abandon');
        if (btnAbandon) btnAbandon.addEventListener('click', function() { abandonCard(card); });
        var btnDelete = $('#btn-delete');
        if (btnDelete) btnDelete.addEventListener('click', function() { deleteCard(card); });
        var btnReset = $('#btn-reset');
        if (btnReset) btnReset.addEventListener('click', function() { resetCard(card.id); });
    }

    /* ---- 打卡 Tab（100 格） ---- */
    function renderSlotsTab(card) {
        var canFill = card.status === 'in_progress' && !card.locked;
        var html = '<div class="slot-grid">';
        card.slots.forEach(function(slot, i) {
            var cls = 'slot-cell';
            if (slot.filled) cls += ' filled';
            if (!canFill) cls += ' locked';
            var tip = '第' + (i + 1) + '格';
            if (slot.filled && slot.record) {
                var r = slot.record;
                tip += '：' + (r.content || r.prediction || '') + ' | ' + (r.duration_minutes || 60) + ' 分钟';
                if (r.filled_at) tip += ' | ' + r.filled_at;
            } else {
                tip += '：未勾选';
            }
            html += '<div class="' + cls + '" data-index="' + i + '" title="' + esc(tip) + '">' +
                '<span class="slot-title">' + (i + 1) + '</span>' + (slot.filled ? '✔' : '') + '</div>';
        });
        html += '</div>';
        html += '<div class="slot-grid-legend">' +
            '<span>✔ 绿色 = 已勾选</span><span>□ 灰色 = 未勾选</span>' +
            (canFill ? '<span>👆 点击格子，随时开始，几分钟也算在场</span>' : '<span>🔒 当前卡片不可勾选</span>') +
            '</div>';
        if (canFill) {
            html += '<div class="slot-encourage">🌱 今天做多少都可以——只要来了，就是胜利</div>';
        }

        // 打卡记录列表（按时间倒序，含具体时间与内容摘要）
        var records = [];
        card.slots.forEach(function(slot, i) {
            if (slot.filled && slot.record) {
                records.push({ index: i, slot: slot });
            }
        });
        records.sort(function(a, b) { return (b.slot.filled_at || '').localeCompare(a.slot.filled_at || ''); });
        if (records.length) {
            html += '<div class="slot-record-list">' +
                '<div class="srl-title">📋 打卡记录（' + records.length + ' 条 · 按时间倒序）</div>';
            records.forEach(function(r) {
                var slot = r.slot;
                var rec = slot.record || {};
                var summary = rec.content || rec.prediction || '（无内容）';
                if (card.type === 'trade') {
                    summary += ' · 预测' + (rec.prediction || '?');
                    if (rec.actual) summary += ' → 实际' + rec.actual + (rec.hit ? ' ✅' : ' ❌');
                    if (rec.account_balance) summary += ' · 💰' + rec.account_balance + 'U';
                }
                html += '<div class="slot-record-item">' +
                    '<span class="srl-index">第 ' + (r.index + 1) + ' 格</span>' +
                    '<span class="srl-content">' + esc(summary) + ' · ' + (rec.duration_minutes || 60) + ' 分钟</span>' +
                    '<span class="srl-time">' + esc(slot.filled_at || '') + '</span>' +
                    '<button class="srl-del" data-del-index="' + r.index + '" title="删除这条打卡">🗑</button>' +
                    '</div>';
            });
            html += '</div>';
        }

        $('#cm-body').innerHTML = html;
        $all('.slot-cell').forEach(function(node) {
            node.addEventListener('click', function() { onSlotClick(card, node); });
        });
        $all('.srl-del').forEach(function(node) {
            node.addEventListener('click', function(e) {
                e.stopPropagation();
                unfillSlot(card, parseInt(node.dataset.delIndex));
            });
        });
    }

    function onSlotClick(card, node) {
        if (card.locked || card.status === 'pending') {
            MDialog.alert('🔒 该卡片尚未解锁，请先完成上一张任务卡');
            return;
        }
        if (card.status === 'completed' || card.status === 'failed' || card.status === 'abandoned') {
            MDialog.alert('该任务已结束，无法打卡');
            return;
        }
        var index = parseInt(node.dataset.index);
        var slot = card.slots[index];
        if (slot.filled) openSlotDetail(card, slot, index);
        else openSlotFill(card, index);
    }

    /* 通用补录历史块：「今日打卡 / 历史补录」模式切换按钮（学习/交易弹窗统一风格）
       历史补录细分为两种子模式：
       - 单点补录：指定一个过去的时间点补一条记录；
       - 范围补录：选择开始日期 + 结束日期，交易卡自动拉取范围内每日账户余额
         数据（本地余额快照），确认后批量回填到空格子。 */
    function buildBackfillBlock(card) {
        var isTrade = !!(card && card.type === 'trade');
        var inputStyle = 'padding:6px 10px;border:1px solid var(--border-color);border-radius:6px;box-sizing:border-box;';
        return '<div style="margin-top:10px;border-top:1px dashed var(--border-color);padding-top:8px;">' +
            '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
            '<button type="button" class="m-btn m-btn-sm m-btn-primary" id="sf-mode-today">📅 今日打卡</button>' +
            '<button type="button" class="m-btn m-btn-sm" id="sf-mode-backfill" title="选择过去的时间点进行补录">🕘 历史补录</button>' +
            '</div>' +
            '<div id="sf-backfill-section" style="display:none;margin-top:8px;">' +
            '<div style="display:flex;gap:8px;margin-bottom:6px;">' +
            '<button type="button" class="m-btn m-btn-sm m-btn-primary" id="sf-bf-single" title="指定一个具体的过去时间">单点补录</button>' +
            '<button type="button" class="m-btn m-btn-sm" id="sf-bf-range" title="按日期范围批量补录">范围补录</button>' +
            '</div>' +
            '<div id="sf-bf-single-panel">' +
            '<input type="datetime-local" id="sf-filled-at" style="width:100%;' + inputStyle + '">' +
            '<p id="sf-backfill-hint" style="font-size:.75rem;color:var(--text-muted);margin:4px 0 0;">选择过去的打卡时间</p>' +
            '</div>' +
            '<div id="sf-bf-range-panel" style="display:none;">' +
            '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">' +
            '<input type="date" id="sf-bf-start" title="开始日期" style="flex:1;min-width:130px;' + inputStyle + '">' +
            '<span style="color:var(--text-muted);">→</span>' +
            '<input type="date" id="sf-bf-end" title="结束日期" style="flex:1;min-width:130px;' + inputStyle + '">' +
            '<input type="time" id="sf-bf-time" value="20:00" title="每日补录的打卡时刻" style="width:110px;' + inputStyle + '">' +
            '</div>' +
            (isTrade ? '<button type="button" class="m-btn m-btn-sm" id="sf-bf-fetch" style="margin-top:6px;" title="重新拉取范围内每日余额">🔍 拉取余额数据</button>' : '') +
            '<div id="sf-bf-preview" style="margin-top:6px;font-size:.78rem;color:var(--text-muted);max-height:180px;overflow:auto;">' +
            (isTrade ? '选择日期范围后自动拉取每日账户余额数据（来自本地余额快照）' : '选择日期范围后生成待补录日期清单') + '</div>' +
            '<p id="sf-bf-range-hint" style="font-size:.75rem;color:var(--text-muted);margin:4px 0 0;">按日期范围每天补录一条打卡记录，按顺序填入空格子；' +
            (isTrade ? '每日余额自动带入，可在格子里继续回填实际涨跌' : '内容与时长取上方表单当前值') + '</p>' +
            '</div>' +
            '</div></div>';
    }

    /* 绑定补录模式切换；切换/改期时展示所选日期已有打卡数，避免重复补录；
       范围补录：开始/结束日期变化时自动拉取数据（交易卡拉每日余额，学习卡本地生成日期清单） */
    function bindBackfillMode(overlay, card) {
        var btnToday = overlay.querySelector('#sf-mode-today');
        var btnBackfill = overlay.querySelector('#sf-mode-backfill');
        var section = overlay.querySelector('#sf-backfill-section');
        var dtInput = overlay.querySelector('#sf-filled-at');
        var hint = overlay.querySelector('#sf-backfill-hint');
        if (!btnToday || !btnBackfill || !section || !dtInput) return;
        dtInput.value = formatLocalDateTime(new Date());
        function updateHint() {
            var date = (dtInput.value || '').slice(0, 10);
            if (!date || !hint) return;
            var count = 0;
            (card.slots || []).forEach(function(s) {
                if (s.filled && s.filled_at && String(s.filled_at).slice(0, 10) === date) count++;
            });
            if (count) {
                hint.innerHTML = '⚠️ 该日已有 <b>' + count + '</b> 条打卡记录，补录时注意避免内容重复';
                hint.style.color = '#e67e22';
            } else {
                hint.innerHTML = '✅ 该日暂无打卡记录，可放心补录';
                hint.style.color = '#28a745';
            }
        }
        function setMode(backfill) {
            section.style.display = backfill ? '' : 'none';
            btnBackfill.classList.toggle('m-btn-primary', backfill);
            btnToday.classList.toggle('m-btn-primary', !backfill);
            if (backfill) updateHint();
        }
        btnToday.addEventListener('click', function() { setMode(false); });
        btnBackfill.addEventListener('click', function() { setMode(true); });
        dtInput.addEventListener('change', updateHint);

        /* ---- 范围补录子模式 ---- */
        var btnSingle = overlay.querySelector('#sf-bf-single');
        var btnRange = overlay.querySelector('#sf-bf-range');
        var singlePanel = overlay.querySelector('#sf-bf-single-panel');
        var rangePanel = overlay.querySelector('#sf-bf-range-panel');
        if (!btnSingle || !btnRange || !singlePanel || !rangePanel) return;
        var startInput = overlay.querySelector('#sf-bf-start');
        var endInput = overlay.querySelector('#sf-bf-end');
        var fetchBtn = overlay.querySelector('#sf-bf-fetch');
        var preview = overlay.querySelector('#sf-bf-preview');
        var isTrade = card.type === 'trade';

        // 默认范围：最近 7 天（昨天往前推）
        function fmtDate(d) {
            function pad(n) { return n < 10 ? '0' + n : '' + n; }
            return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
        }
        var yesterday = new Date(); yesterday.setDate(yesterday.getDate() - 1);
        var weekAgo = new Date(yesterday); weekAgo.setDate(weekAgo.getDate() - 6);
        if (startInput && !startInput.value) startInput.value = fmtDate(weekAgo);
        if (endInput && !endInput.value) endInput.value = fmtDate(yesterday);

        // 已存在打卡的日期集合（用于预览行标注，避免重复补录）
        function checkedDates() {
            var map = {};
            (card.slots || []).forEach(function(s) {
                if (s.filled && s.filled_at) {
                    var d = String(s.filled_at).slice(0, 10);
                    map[d] = (map[d] || 0) + 1;
                }
            });
            return map;
        }

        // 渲染预览清单：每天一行，带复选框；交易卡额外展示当日余额
        function renderPreview(days) {
            if (!preview) return;
            var exist = checkedDates();
            if (!days.length) { preview.innerHTML = '<span style="color:#e67e22">所选范围无效</span>'; return; }
            var html = '';
            days.forEach(function(d) {
                var hasBal = (d.balance !== null && d.balance !== undefined && d.balance !== '');
                var warn = exist[d.date] ? ' <span style="color:#e67e22" title="该日已有 ' + exist[d.date] + ' 条打卡">⚠️已有' + exist[d.date] + '条</span>' : '';
                html += '<label style="display:flex;align-items:center;gap:6px;padding:3px 6px;border:1px solid var(--border-color);border-radius:5px;margin-bottom:4px;background:#fff;cursor:pointer;">' +
                    '<input type="checkbox" class="sf-bf-day" data-date="' + d.date + '" data-balance="' + (hasBal ? d.balance : '') + '" checked>' +
                    '<span>' + d.date + warn + '</span>' +
                    (isTrade ? '<span style="margin-left:auto;' + (hasBal ? 'color:#155724' : 'color:#aaa') + '">' +
                        (hasBal ? '💰 ' + Number(d.balance).toFixed(2) + 'U' : '无余额数据') + '</span>' : '') +
                    '</label>';
            });
            preview.innerHTML = html;
        }

        function rangeMsg(text, color) {
            if (preview) { preview.innerHTML = '<span style="color:' + (color || 'var(--text-muted)') + '">' + text + '</span>'; }
        }

        // 拉取范围内数据：交易卡调后端余额快照接口；学习卡本地生成日期清单
        function fetchRange() {
            var start = startInput && startInput.value;
            var end = endInput && endInput.value;
            if (!start || !end) return;
            if (start > end) { rangeMsg('⚠️ 开始日期不能晚于结束日期', '#e67e22'); return; }
            rangeMsg('⏳ 正在拉取 ' + start + ' ~ ' + end + ' 的数据…');
            if (isTrade) {
                apiGet('/plan/api/backfill-balances?start=' + encodeURIComponent(start) + '&end=' + encodeURIComponent(end))
                    .then(function(res) {
                        if (res.code === 200 && res.data && res.data.days) {
                            renderPreview(res.data.days);
                        } else {
                            rangeMsg('⚠️ 拉取失败：' + (res.message || '未知错误') + '，仍可勾选后按空余额补录', '#e67e22');
                            // 降级：本地生成日期清单（无余额）
                            renderPreview(buildLocalDays(start, end));
                        }
                    }).catch(function() {
                        rangeMsg('⚠️ 拉取失败，已降级为无余额模式', '#e67e22');
                        renderPreview(buildLocalDays(start, end));
                    });
            } else {
                renderPreview(buildLocalDays(start, end));
            }
        }

        // 本地逐日生成 [start, end] 日期清单（学习卡 / 余额接口降级时使用，最多 62 天）
        function buildLocalDays(start, end) {
            var days = [];
            var cur = new Date(start + 'T00:00:00');
            var stop = new Date(end + 'T00:00:00');
            var guard = 0;
            while (cur <= stop && guard < 62) {
                days.push({ date: fmtDate(cur), balance: null });
                cur.setDate(cur.getDate() + 1);
                guard++;
            }
            return days;
        }

        function setRangeMode(on) {
            singlePanel.style.display = on ? 'none' : '';
            rangePanel.style.display = on ? '' : 'none';
            btnRange.classList.toggle('m-btn-primary', on);
            btnSingle.classList.toggle('m-btn-primary', !on);
            if (on) fetchRange();
        }
        btnSingle.addEventListener('click', function() { setRangeMode(false); });
        btnRange.addEventListener('click', function() { setRangeMode(true); });
        if (startInput) startInput.addEventListener('change', function() { if (rangePanel.style.display !== 'none') fetchRange(); });
        if (endInput) endInput.addEventListener('change', function() { if (rangePanel.style.display !== 'none') fetchRange(); });
        if (fetchBtn) fetchBtn.addEventListener('click', fetchRange);
    }

    /* ================================================================
       分析纪律闸门状态条（批次11）
       ----------------------------------------------------------------
       后端 /plan/api/analysis-gate 与写入口 fill-slot 用完全相同的判定口径，
       所以这条状态栏只负责「提前告知 + 就地补救」，不承担放行职责——
       真正的阻断永远在后端执行，前端状态条即使挂了也漏不过去。

       两条硬约束：
       1. 接口异常/纪律关闭 → 状态条整条隐藏，绝不影响正常打卡（fail-open）；
       2. 反馈一律走状态条内联文字，不用 MDialog.alert——MDialog.show() 会先
          close(activeDialog)，在打卡弹窗里弹提示会把用户填了一半的表单直接销毁。
       ================================================================ */
    var GATE_URL = '/plan/api/analysis-gate';
    var JUDGE_TO_PRED = { rise: '涨', fall: '跌', watch: '横盘' };

    /* 当前该问闸门哪个时刻：今日打卡=空（当前小时），单点补录=所选时刻，
       范围补录=逐条各自判定，状态条只给说明不给单槽结论 */
    function gateTarget(overlay) {
        var section = overlay.querySelector('#sf-backfill-section');
        if (!section || section.style.display === 'none') return '';
        var rangePanel = overlay.querySelector('#sf-bf-range-panel');
        if (rangePanel && rangePanel.style.display !== 'none') return 'RANGE';
        var inp = overlay.querySelector('#sf-filled-at');
        return (inp && inp.value) ? fromDatetimeLocal(inp.value) : '';
    }

    /* 把本小时分析记录带入表单：行情分析写明细，预测方向取多数判断。
       这是「分析 → 决策」链条的落地动作，让分析真正影响打卡内容，
       而不是沦为一张盖章用的门票。 */
    function gateApplyRecords(overlay, records, note) {
        if (!records || !records.length) { note('本小时没有可带入的分析记录', true); return; }
        var lines = records.map(function(r) {
            var j = JUDGE_TO_PRED[r.user_judgment] || r.user_judgment || '';
            return '· ' + r.inst_id + '：' + j + (r.user_reason ? '（' + r.user_reason + '）' : '');
        });
        var ma = overlay.querySelector('#sf-market-analysis');
        if (ma) ma.value = ma.value.trim() ? (ma.value.trim() + '\n' + lines.join('\n')) : lines.join('\n');

        var cnt = { '涨': 0, '跌': 0, '横盘': 0 };
        records.forEach(function(r) { var p = JUDGE_TO_PRED[r.user_judgment]; if (p) cnt[p]++; });
        var best = null, bestN = 0;
        Object.keys(cnt).forEach(function(k) { if (cnt[k] > bestN) { bestN = cnt[k]; best = k; } });
        var tied = Object.keys(cnt).filter(function(k) { return cnt[k] === bestN; }).length > 1;
        var sel = overlay.querySelector('#sf-prediction');
        if (sel && best && !tied && bestN > 0) sel.value = best;
        note('已带入 ' + records.length + ' 条分析' +
             (best && !tied && bestN > 0 ? '，预测方向置为「' + best + '」' : '（判断分歧，预测方向请自己定）'));
    }

    /* 状态条绑定：拉取闸门 → 渲染 → 按钮事件；补录时刻变化时重新判定 */
    function bindSlotGate(overlay, card) {
        if (!overlay || !card || card.type !== 'trade') return;
        var bar = overlay.querySelector('#sf-gate');
        if (!bar) return;
        var seq = 0, lastRecords = [];

        function note(text, isErr) { noteInDialog(overlay, text, isErr ? true : false); }

        function paint(kind, text, acts) {
            bar.className = 'sf-gate ' + kind;
            bar.innerHTML = '<span class="sf-gate-txt">' + text +
                '<div class="sf-gate-note" style="font-size:.76rem;margin-top:3px;"></div></span>' +
                '<span class="sf-gate-acts">' + (acts || '') + '</span>';
        }

        function hide() { bar.className = 'sf-gate hide'; bar.innerHTML = ''; }

        function slotLabel(slot) { return esc(String(slot || '').slice(11) + ':00'); }

        function render(d) {
            lastRecords = d.records || [];
            if (!d.enabled || d.mode === 'disabled' || d.mode === 'off') { hide(); return; }

            var slot = slotLabel(d.hour_slot);
            var cnt = '分析记录 <b>' + (d.actual || 0) + '</b> 条 / 要求 <b>' + (d.required || 1) + '</b> 条';

            if (d.mode === 'inactive') {
                paint('idle', '🌙 ' + slot + ' 不在纪律生效时段内，不要求分析，可直接打卡');
                return;
            }
            if (d.mode === 'exempt') {
                paint('idle', '😴 已豁免：' + esc(d.reason || '') + ' · 打卡放行');
                return;
            }
            if (d.mode === 'soft_bypass') {
                paint('warn', '⚠️ 宽松模式：本小时（' + slot + '）' + cnt + '，打卡会放行但被记为「无分析支撑」' +
                    '<ul class="sf-gate-list"><li>看板上会单独统计这类打卡的命中率</li></ul>',
                    '<button type="button" class="m-btn m-btn-sm" data-act="quick">📸 现在补分析</button>');
                return;
            }
            if (d.allowed) {
                var bf = d.backfill_count ? '（其中 ' + d.backfill_count + ' 条为补记）' : '';
                paint('ok', '✅ 本小时（' + slot + '）' + cnt + bf + ' · 打卡闸门已放行',
                    lastRecords.length
                        ? '<button type="button" class="m-btn m-btn-sm" data-act="apply">↻ 带入本小时分析</button>'
                        : '');
                return;
            }
            // strict_block：如实告知被拦，并给出就地补救入口
            var miss = (d.missing_coins || []);
            var detail = miss.length ? ('未覆盖币种：' + esc(miss.join('、'))) : cnt;
            var acts = '<button type="button" class="m-btn m-btn-sm m-btn-primary" data-act="quick">📸 一键分析并记录</button>';
            if (d.hour_slot && String(d.hour_slot).slice(0, 13) < nowSlotLabel()) {
                acts = '<button type="button" class="m-btn m-btn-sm m-btn-primary" data-act="backfill">🕰️ 回溯补记这一小时</button>' + acts;
            }
            paint('block', '⛔ 先分析，再打卡：本小时（' + slot + '）' + detail +
                '<ul class="sf-gate-list"><li>完成分析后本状态条自动刷新为放行</li></ul>', acts);
        }

        function nowSlotLabel() {
            var n = new Date();
            function p(x) { return x < 10 ? '0' + x : '' + x; }
            return n.getFullYear() + '-' + p(n.getMonth() + 1) + '-' + p(n.getDate()) + ' ' + p(n.getHours());
        }

        function check(done) {
            // 本函数既能被当回调直接调用，也可能被 addEventListener 当处理器注册，
            // 后者会把 Event 对象塞进 done：直接 if (done) done() 会抛
            // TypeError，而它落在下面的 promise 链里会被 .catch 吞掉并执行 hide()，
            // 结果是刚渲染好的闸门状态条被自己藏掉——闸门提示凭空消失。
            var next = typeof done === 'function' ? done : null;
            var target = gateTarget(overlay);
            if (target === 'RANGE') {
                lastRecords = [];
                paint('idle', '🗂️ 范围补录：每条按<b>各自小时</b>独立判定闸门。' +
                    '缺分析的小时不会写入，会在提交后列出清单供你补回溯分析。');
                if (next) next();
                return;
            }
            var reqId = ++seq;
            var url = GATE_URL + (target ? ('?filled_at=' + encodeURIComponent(target)) : '');
            paint('idle', '⏳ 正在校验分析纪律闸门…');
            apiGet(url).then(function(res) {
                if (reqId !== seq) return;
                if (!res || res.code !== 200 || !res.data) { hide(); if (next) next(); return; }
                render(res.data);
                if (next) next();
            }).catch(function() {
                if (reqId !== seq) return;
                hide();   // 闸门接口不可用：保持改造前的打卡体验
                if (next) next();
            });
        }

        // 供提交失败（403 need_analysis）时外部触发重新判定
        bar._gateCheck = check;

        bar.addEventListener('click', function(e) {
            var btn = e.target.closest ? e.target.closest('[data-act]') : null;
            if (!btn) return;
            var act = btn.dataset.act;
            if (act === 'apply') { gateApplyRecords(overlay, lastRecords, note); return; }
            if (!window.AnalysisQuick) {
                note('当前页面未加载快速分析模块，请前往「📝 分析记录」页完成分析', true);
                return;
            }
            var target = gateTarget(overlay);
            var slots = [];
            if (act === 'backfill' || (target && String(target).slice(0, 13) < nowSlotLabel())) {
                slots = [String(target || '').slice(0, 13)];
                if (!slots[0]) { note('请先在下方选择补录时间', true); return; }
            }
            btn.disabled = true;
            window.AnalysisQuick.open(slots.length
                ? { mode: 'backfill', slots: slots, onSaved: function() { check(); } }
                : { mode: 'live', title: '📸 补上本小时的分析记录', onSaved: function() { check(); } });
            setTimeout(function() { btn.disabled = false; }, 800);
        });

        // 补录时刻/模式变化 → 重新判定（闸门永远跟着实际要写入的那个小时走）
        ['#sf-filled-at', '#sf-mode-today', '#sf-mode-backfill', '#sf-bf-single', '#sf-bf-range']
            .forEach(function(sel) {
                var el = overlay.querySelector(sel);
                if (!el) return;
                el.addEventListener('change', function() { check(); });
                el.addEventListener('click', function() { setTimeout(check, 0); });
            });

        check();
    }

    /* 弹窗内就地告知（不弹 MDialog：MDialog.show 会先 close 当前弹窗，
       直接销毁用户填了一半的表单）。交易卡写入闸门状态条，学习卡降级为弹窗。 */
    function noteInDialog(overlay, message, isErr) {
        var bar = overlay ? overlay.querySelector('#sf-gate') : null;
        var noteEl = bar ? bar.querySelector('.sf-gate-note') : null;
        if (!noteEl) { MDialog.alert(message); return false; }
        noteEl.textContent = (isErr === false ? '' : '❌ ') + String(message || '').split('\n').join(' ');
        noteEl.style.color = isErr === false ? '#2b8a3e' : '#c92a2a';
        return true;
    }

    /* 打卡被闸门拦住：刷新状态条（重新拉真实判定）+ 就地给出补救入口 */
    function showGateBlock(overlay, message) {
        var bar = overlay ? overlay.querySelector('#sf-gate') : null;
        if (!bar || typeof bar._gateCheck !== 'function') {
            MDialog.alert(message || '打卡被分析纪律拦截');
            return;
        }
        bar._gateCheck(function() { noteInDialog(overlay, message); });
    }

    /* 范围补录被闸门拦下（部分或全部）：把缺口清单一次性转成可编辑的回溯矩阵。
       一次历史补录可能涉几十个小时，逐条报错只会让人放弃；用【当时的真实价格】
       批量补回溯分析，才是真正的补救路径。 */
    function offerBackfillAnalysis(slots, message) {
        slots = (slots || []).filter(Boolean).slice(0, 31);
        if (!slots.length) { MDialog.alert(message || '范围补录失败'); return; }
        if (!window.AnalysisQuick) {
            MDialog.alert((message || '') + '\n请前往「📝 分析记录」页为这些小时补上回溯分析：\n' +
                slots.map(function(s) { return s + ':00'; }).join('、'));
            return;
        }
        MDialog.confirm({
            title: '需要先补分析记录',
            message: esc(message || '') + '<br><br>被拦下的小时（' + slots.length + ' 个）：<br>' +
                '<span style="font-size:.8rem;color:var(--text-muted);word-break:break-all;">' +
                esc(slots.map(function(s) { return s + ':00'; }).join('、')) + '</span><br><br>' +
                '是否现在用【当时的真实价格】为这些小时生成回溯分析？完成后重新提交即可通过。',
            okText: '🕰️ 生成回溯分析',
            onOk: function() {
                window.AnalysisQuick.open({
                    mode: 'backfill',
                    slots: slots,
                    onSaved: function() {
                        if (window.AnalysisDiscipline) window.AnalysisDiscipline.refresh();
                    }
                });
            }
        });
    }

    /* 未勾选格子：填写记录（长期主义版：低门槛，几分钟也算在场） */
    function openSlotFill(card, index) {
        var isLearn = card.type === 'learn';
        var html;
        if (isLearn) {
            html = '<div class="slot-form">' +
                '<label>学习内容（这个小时学了什么？）</label><textarea id="sf-content" placeholder="如：研读 MACD 背离章节、写回测代码…"></textarea>' +
                '<label>学习时长（分钟）</label><input type="number" id="sf-duration" value="60" min="0" max="480">' +
                '<p style="font-size:.78rem;color:var(--text-muted);margin:6px 0 0;">🌱 设为 0 表示刚开始，结束时再更新实际时长</p>' +
                buildBackfillBlock(card) +
                '</div>';
        } else {
            html = '<div class="slot-form">' +
                '<div id="sf-gate" class="sf-gate hide"></div>' +
                '<label>预测方向</label><select id="sf-prediction">' +
                '<option value="涨">📈 涨</option><option value="跌">📉 跌</option><option value="横盘">➖ 横盘</option></select>' +
                '<label>交易时长（分钟）</label><input type="number" id="sf-duration" value="60" min="0" max="480">' +
                '<label>实际涨跌（可稍后在格子里回填）</label><select id="sf-actual">' +
                '<option value="">— 未回填 —</option><option value="涨">📈 涨</option><option value="跌">📉 跌</option><option value="横盘">➖ 横盘</option></select>' +
                '<p style="font-size:.78rem;color:var(--text-muted);margin:6px 0 0;">🌱 设为 0 表示刚开始，结束时再更新实际时长</p>' +
                // 💰 账户金额置顶突出：自动同步主账号余额，可手动修改/刷新
                '<div style="margin-top:12px;padding:10px;border:1px solid var(--border-color);border-radius:8px;background:rgba(47,128,237,.05);">' +
                '<label style="margin:0 0 4px;font-weight:600;">💰 账户金额（USDT）</label>' +
                '<div style="display:flex;gap:8px;align-items:center;">' +
                '<input type="text" id="sf-balance" placeholder="自动获取中…" style="flex:1;padding:6px 10px;border:1px solid var(--border-color);border-radius:6px;font-size:.9rem;">' +
                '<button type="button" class="m-btn m-btn-sm" id="sf-balance-refresh" title="强制重新拉取最新余额" style="flex-shrink:0;">🔄 刷新</button>' +
                '</div>' +
                '<p id="sf-balance-status" style="font-size:.72rem;color:var(--text-muted);margin:4px 0 0;">⏳ 正在自动获取主账号余额…</p>' +
                '</div>' +
                '<div style="margin-top:12px;border-top:1px dashed var(--border-color);padding-top:10px;">' +
                '<label>📊 行情分析（市场状态/趋势判断/关键信息）</label>' +
                '<textarea id="sf-market-analysis" placeholder="如：BTC 4H 突破关键阻力位，MACD金叉，资金费率转多，整体偏强震荡…" style="min-height:60px;"></textarea>' +
                '<label>🎯 操作建议（具体操作计划）</label>' +
                '<textarea id="sf-action-advice" placeholder="如：回踩确认不破支撑后轻仓试多，止损设在前低下方，目标看前高…" style="min-height:60px;"></textarea>' +
                '</div>' +
                buildBackfillBlock(card) +
                '</div>';
        }
        // 【修复】原代码用 document.querySelector('.mdialog-overlay') 取弹窗容器，
        // 类名拼写错误（实际为 m-dialog-overlay）导致 overlay 恒为 null，
        // 补录模式绑定与余额自动获取从未执行。改用 MDialog.show() 的返回值。
        var overlay = null;

        // 范围补录批量提交：收集勾选日期 → entries → /plan/api/backfill-batch
        function submitRangeBackfill(baseRecord) {
            var timeEl = $('#sf-bf-time');
            var timeVal = (timeEl && timeEl.value) || '20:00';
            var entries = [];
            $all('.sf-bf-day').forEach(function(box) {
                if (!box.checked) return;
                var rec;
                if (isLearn) {
                    rec = { content: baseRecord.content || '历史补录学习记录', duration_minutes: baseRecord.duration_minutes };
                } else {
                    rec = {
                        prediction: baseRecord.prediction,
                        duration_minutes: baseRecord.duration_minutes,
                        actual: baseRecord.actual,
                        market_analysis: baseRecord.market_analysis,
                        action_advice: baseRecord.action_advice,
                        // 优先使用当日余额快照，无快照时退回表单当前余额
                        account_balance: box.dataset.balance || baseRecord.account_balance || ''
                    };
                }
                entries.push({ filled_at: box.dataset.date + ' ' + timeVal, record: rec });
            });
            if (!entries.length) {
                MDialog.alert('请至少勾选一个日期再补录');
                return false; // 保持弹窗打开
            }
            apiPost('/plan/api/backfill-batch', {
                plan_id: card.plan_id,
                card_id: card.id,
                entries: entries
            }).then(function(res) {
                var d = (res && res.data) || {};
                var blockedSlots = d.blocked_slots || [];
                if (res.code === 200 && !blockedSlots.length) {
                    if (overlay) MDialog.close(overlay);
                    MDialog.alert({
                        message: '✅ 范围补录完成：成功填入 ' + (d.filled || 0) + ' 条' +
                            (d.skipped ? '、跳过 ' + d.skipped + ' 条' : '') +
                            '\n当前进度 ' + (d.filled_count || 0) + '/100h',
                        type: 'success'
                    });
                    applyRefreshPayload(d.refresh);
                    return;
                }
                if (res.code === 200) {
                    // 部分成功：已写入的先落地刷新，再引导补剩下小时的回溯分析
                    applyRefreshPayload(d.refresh);
                    if (overlay) MDialog.close(overlay);
                    offerBackfillAnalysis(blockedSlots, res.message ||
                        ('已填入 ' + (d.filled || 0) + ' 条，其余因缺分析记录被拦'));
                    return;
                }
                if (res.code === 403 && d.need_analysis) {
                    if (overlay) MDialog.close(overlay);
                    offerBackfillAnalysis(blockedSlots, res.message);
                    return;
                }
                MDialog.alert(res.message || '范围补录失败');
            });
            return false; // 异步提交：先保持弹窗打开，成功后再关闭
        }

        overlay = MDialog.show({
            title: '第 ' + (index + 1) + ' 格 · ' + (isLearn ? '学习记录' : '交易记录'),
            message: html,
            okText: '✔ 勾选',
            onOk: function() {
                var record;
                if (isLearn) {
                    record = { content: $('#sf-content').value.trim(), duration_minutes: parseInt($('#sf-duration').value) || 0 };
                } else {
                    var prediction = $('#sf-prediction').value;
                    record = {
                        prediction: prediction,
                        duration_minutes: parseInt($('#sf-duration').value) || 0,
                        actual: $('#sf-actual').value,
                        market_analysis: ($('#sf-market-analysis') || {}).value ? ($('#sf-market-analysis').value || '').trim() : '',
                        action_advice: ($('#sf-action-advice') || {}).value ? ($('#sf-action-advice').value || '').trim() : '',
                        account_balance: ($('#sf-balance') || {}).value ? ($('#sf-balance').value || '').trim() : ''
                    };
                }
                var payload = {
                    plan_id: card.plan_id,
                    card_id: card.id,
                    slot_index: index,
                    record: record
                };
                var backfillSection = $('#sf-backfill-section');
                if (backfillSection && backfillSection.style.display !== 'none') {
                    // 范围补录子模式：按勾选日期清单批量提交
                    var rangePanelEl = $('#sf-bf-range-panel');
                    if (rangePanelEl && rangePanelEl.style.display !== 'none') {
                        return submitRangeBackfill(record);
                    }
                    var filledAtInput = $('#sf-filled-at');
                    if (filledAtInput && filledAtInput.value) {
                        payload.filled_at = filledAtInput.value.replace('T', ' ');
                    }
                }
                apiPost('/plan/api/fill-slot', payload).then(function(res) {
                    if (res.code === 200) {
                        var isBackfill = payload.filled_at;
                        if (overlay) MDialog.close(overlay);
                        MDialog.alert({
                            message: '✅ 第 ' + (index + 1) + ' 格' + (isBackfill ? '补录' : '勾选') + '成功 · 欢迎回来，长期主义者 🌱\n已完成 ' + res.data.filled_count + '/100h · 今天也在场',
                            type: 'success'
                        });
                        applyRefreshPayload(res.data && res.data.refresh);
                        if (window.AnalysisDiscipline) window.AnalysisDiscipline.refresh();
                    } else if (res.code === 403 && res.data && res.data.need_analysis) {
                        // 被分析纪律拦住：弹窗保持打开（表单不丢），状态条就地给补救入口
                        showGateBlock(overlay, res.message);
                    } else {
                        if (overlay) MDialog.close(overlay);
                        MDialog.alert(res.message || '勾选失败');
                    }
                }).catch(function(e) {
                    // 网络异常：弹窗保持打开，表单不丢，告知后让用户重试
                    if (!noteInDialog(overlay, '提交失败（网络异常），请重试：' + e)) { /* 已降级弹窗 */ }
                });
                return false;   // 异步提交：先保持弹窗打开，成功/普通失败后再关闭
            }
        });
        // 补录模式切换（弹窗渲染后绑定，学习/交易通用）
        if (overlay) bindBackfillMode(overlay, card);
        // 分析纪律闸门状态条（仅交易卡；学习卡不受纪律约束）
        if (overlay) bindSlotGate(overlay, card);
        // 交易打卡：自动获取账户余额（含同步状态提示）
        if (!isLearn && overlay) {
            var balanceInput = overlay.querySelector('#sf-balance');
            var refreshBtn = overlay.querySelector('#sf-balance-refresh');
            var statusEl = overlay.querySelector('#sf-balance-status');
            function setStatus(text, color) {
                if (statusEl) { statusEl.textContent = text; statusEl.style.color = color || 'var(--text-muted)'; }
            }
            function fetchBalance() {
                if (!balanceInput) return;
                balanceInput.value = '获取中…';
                balanceInput.style.color = 'var(--text-muted)';
                balanceInput.style.fontWeight = '';
                setStatus('⏳ 正在自动获取主账号余额…');
                apiPost('/plan/api/account-balance', {}).then(function(res) {
                    if (res.code === 200 && res.data) {
                        balanceInput.value = parseFloat(res.data.totalEq).toFixed(2);
                        balanceInput.style.color = '#155724';
                        balanceInput.style.fontWeight = '600';
                        setStatus('✅ 已同步 · ' + (res.data.updateTime || ''), '#28a745');
                    } else {
                        balanceInput.value = '';
                        balanceInput.placeholder = '获取失败，可手动输入';
                        balanceInput.style.color = '';
                        setStatus('⚠️ 自动获取失败，可手动输入金额', '#e67e22');
                    }
                }).catch(function() {
                    balanceInput.value = '';
                    balanceInput.placeholder = '获取失败，可手动输入';
                    balanceInput.style.color = '';
                    setStatus('⚠️ 自动获取失败，可手动输入金额', '#e67e22');
                });
            }
            fetchBalance();
            if (refreshBtn) refreshBtn.addEventListener('click', fetchBalance);
            // 手动修改后更新状态提示，避免误以为仍是同步值
            if (balanceInput) {
                balanceInput.addEventListener('input', function() {
                    balanceInput.style.color = '';
                    balanceInput.style.fontWeight = '';
                    setStatus('✏️ 已手动修改（点 🔄 可重新同步实时余额）');
                });
            }
        }
    }

    /* 已勾选格子：查看/回填 */
    function openSlotDetail(card, slot, index) {
        var r = slot.record || {};
        var isLearn = card.type === 'learn';
        var html = '<div class="slot-form">' +
            '<p style="font-size:.85rem;color:var(--text-muted);margin:0 0 4px;">勾选时间：' + esc(slot.filled_at || '') + '</p>';
        if (isLearn) {
            html += '<label>学习内容</label><textarea id="sd-content">' + esc(r.content || '') + '</textarea>' +
                '<label>学习时长（分钟）</label><input type="number" id="sd-duration" value="' + (r.duration_minutes != null ? r.duration_minutes : 60) + '" min="0">';
        } else {
            html += '<label>预测方向</label><select id="sd-prediction">' +
                '<option value="涨"' + (r.prediction === '涨' ? ' selected' : '') + '>📈 涨</option>' +
                '<option value="跌"' + (r.prediction === '跌' ? ' selected' : '') + '>📉 跌</option>' +
                '<option value="横盘"' + (r.prediction === '横盘' ? ' selected' : '') + '>➖ 横盘</option></select>' +
                '<label>交易时长（分钟）</label><input type="number" id="sd-duration" value="' + (r.duration_minutes != null ? r.duration_minutes : 60) + '" min="0">' +
                '<label>实际涨跌（回填后自动计算命中）</label><select id="sd-actual">' +
                '<option value=""' + (!r.actual ? ' selected' : '') + '>— 未回填 —</option>' +
                '<option value="涨"' + (r.actual === '涨' ? ' selected' : '') + '>📈 涨</option>' +
                '<option value="跌"' + (r.actual === '跌' ? ' selected' : '') + '>📉 跌</option>' +
                '<option value="横盘"' + (r.actual === '横盘' ? ' selected' : '') + '>➖ 横盘</option></select>';
            if (r.actual) {
                html += '<p style="font-size:.85rem;' + (r.hit ? 'color:#28a745;' : 'color:#dc3545;') + '">' +
                    (r.hit ? '✅ 预测命中！' : '❌ 预测未命中') + '</p>';
            }
            // 新增字段：行情分析 / 操作建议 / 账户金额
            html += '<div style="margin-top:12px;border-top:1px dashed var(--border-color);padding-top:10px;">' +
                '<label>📊 行情分析</label>' +
                '<textarea id="sd-market-analysis" style="min-height:60px;">' + esc(r.market_analysis || '') + '</textarea>' +
                '<label>🎯 操作建议</label>' +
                '<textarea id="sd-action-advice" style="min-height:60px;">' + esc(r.action_advice || '') + '</textarea>' +
                '<label>💰 账户金额（USDT）</label>' +
                '<input type="text" id="sd-balance" value="' + esc(r.account_balance || '') + '" placeholder="可手动输入或留空">' +
                '</div>';
        }
        html += '</div>';
        // 删除打卡按钮
        html += '<div style="margin-top:14px;border-top:1px dashed var(--border-color);padding-top:10px;">' +
            '<button class="m-btn m-btn-sm" id="sd-delete" style="color:#dc3545;border:1px solid #dc3545;background:#fff;">🗑 删除这条打卡</button>' +
            '</div>';

        var overlay = MDialog.show({
            title: '第 ' + (index + 1) + ' 格 · ' + (isLearn ? '学习记录' : '交易记录'),
            message: html,
            okText: '保存修改',
            onOk: function() {
                var record;
                if (isLearn) {
                    record = { content: $('#sd-content').value.trim(), duration_minutes: parseInt($('#sd-duration').value) || 0 };
                } else {
                    record = {
                        prediction: $('#sd-prediction').value,
                        duration_minutes: parseInt($('#sd-duration').value) || 0,
                        actual: $('#sd-actual').value,
                        market_analysis: ($('#sd-market-analysis') || {}).value ? ($('#sd-market-analysis').value || '').trim() : '',
                        action_advice: ($('#sd-action-advice') || {}).value ? ($('#sd-action-advice').value || '').trim() : '',
                        account_balance: ($('#sd-balance') || {}).value ? ($('#sd-balance').value || '').trim() : ''
                    };
                }
                apiPost('/plan/api/update-slot', {
                    plan_id: card.plan_id,
                    card_id: card.id,
                    slot_index: index,
                    record: record
                }).then(function(res) {
                    if (res.code === 200) { MDialog.alert({ message: '保存成功', type: 'success' }); applyRefreshPayload(res.data && res.data.refresh); }
                    else MDialog.alert(res.message || '保存失败');
                });
            }
        });
        // 删除打卡按钮（弹窗渲染后绑定）
        var delBtn = overlay.querySelector('#sd-delete');
        if (delBtn) delBtn.addEventListener('click', function() {
            MDialog.close(overlay);
            unfillSlot(card, index);
        });
    }

    /* 删除单个打卡记录 */
    function unfillSlot(card, index) {
        MDialog.danger('确定删除第 ' + (index + 1) + ' 格的打卡记录吗？\n删除后该小时将回到未勾选状态。', function() {
            apiPost('/plan/api/unfill-slot', {
                plan_id: card.plan_id,
                card_id: card.id,
                slot_index: index
            }).then(function(res) {
                if (res.code === 200) {
                    MDialog.alert({ message: '🗑 已删除该打卡记录', type: 'success' });
                    applyRefreshPayload(res.data && res.data.refresh);
                } else {
                    MDialog.alert(res.message || '删除失败');
                }
            });
        });
    }

    /* 重置任务卡：清空所有打卡与状态 */
    function resetCard(cardId) {
        var card = findCard(cardId);
        if (!card) return;
        MDialog.danger('确定重置「' + card.title + '」吗？\n将清空该卡全部打卡记录、小记、目标拆解和结算结果，恢复到初始状态，此操作不可恢复！', function() {
            apiPost('/plan/api/reset-card', {
                plan_id: card.plan_id,
                card_id: card.id
            }).then(function(res) {
                if (res.code === 200) {
                    MDialog.alert({ message: '🔄 卡片已重置，回到初始状态', type: 'success' });
                    closeCardModal();
                    loadAll();
                } else {
                    MDialog.alert(res.message || '重置失败');
                }
            });
        });
    }

    /* ---- 打卡详情 Tab（模块4A：可视化柱状图展示打卡频率与分布） ---- */
    function renderCheckinTab(card) {
        /* 按日聚合：每日打卡次数与累计时长 */
        var byDay = {};
        var totalMinutes = 0;
        (card.slots || []).forEach(function(slot) {
            if (!slot.filled || !slot.filled_at) return;
            var date = String(slot.filled_at).slice(0, 10);
            if (!byDay[date]) byDay[date] = { count: 0, minutes: 0 };
            byDay[date].count++;
            totalMinutes += (slot.record && slot.record.duration_minutes) || 0;
        });
        var dates = Object.keys(byDay).sort();
        var totalCount = dates.reduce(function(s, d) { return s + byDay[d].count; }, 0);

        var html = '';
        if (!totalCount) {
            html += '<div style="text-align:center;padding:40px 20px;color:var(--text-muted);">' +
                '<div style="font-size:2.2rem;margin-bottom:10px;">📊</div>' +
                '<div style="font-size:.9rem;">暂无打卡数据，先去「打卡」Tab 勾选第一格吧 🌱</div></div>';
            $('#cm-body').innerHTML = html;
            return;
        }

        /* 摘要统计卡 */
        var totalHours = Math.round(totalMinutes / 60 * 10) / 10;
        var avgMinutes = Math.round(totalMinutes / totalCount);
        html += '<div class="checkin-summary">' +
            '<div class="cs-item"><div class="cs-num">' + totalCount + '</div><div class="cs-label">总打卡次数</div></div>' +
            '<div class="cs-item"><div class="cs-num">' + dates.length + '</div><div class="cs-label">在场天数</div></div>' +
            '<div class="cs-item"><div class="cs-num">' + fmtNum(totalHours) + 'h</div><div class="cs-label">累计时长</div></div>' +
            '<div class="cs-item"><div class="cs-num">' + avgMinutes + ' 分钟</div><div class="cs-label">平均每次时长</div></div>' +
            '</div>';
        html += '<div class="checkin-chart-box"><div id="checkin-chart-daily" style="width:100%;height:320px;"></div></div>';

        $('#cm-body').innerHTML = html;

        /* 累积打卡次数序列（从第一格到当前日逐日累加） */
        var cumCounts = [];
        var cumSum = 0;
        dates.forEach(function(d) { cumSum += byDay[d].count; cumCounts.push(cumSum); });

        /* 每日打卡分布：柱状（每日次数）+ 每日次数曲线 + 累积次数曲线，双 yAxis */
        var dailyChart = echarts.init(document.getElementById('checkin-chart-daily'));
        modalCharts.push(dailyChart);
        var dailyOption = {
            title: { text: '📅 每日打卡分布（柱=每日次数 · 绿线=累积次数）', left: 'center', textStyle: { fontSize: 13, color: '#1c3d5a' } },
            legend: { top: 26, data: ['每日打卡次数（柱）', '每日打卡次数', '累积打卡次数'], textStyle: { fontSize: 11 } },
            tooltip: {
                trigger: 'axis',
                axisPointer: { type: 'cross' },
                formatter: function(ps) {
                    if (!ps.length) return '';
                    var date = ps[0].axisValue;
                    var day = byDay[date] || { count: 0, minutes: 0 };
                    var idx = dates.indexOf(date);
                    var tip = '<b>' + date + '</b>';
                    tip += '<br/>📌 当日打卡：<b>' + day.count + '</b> 次（时长 ' + fmtNum(day.minutes / 60) + 'h）';
                    tip += '<br/>📈 累积打卡：<b>' + (idx >= 0 ? cumCounts[idx] : 0) + '</b> 次';
                    return tip;
                }
            },
            grid: { left: 44, right: 48, top: 60, bottom: dates.length > 20 ? 52 : 30 },
            xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10, rotate: dates.length > 12 ? 45 : 0 } },
            yAxis: [
                { type: 'value', name: '每日（次）', minInterval: 1 },
                { type: 'value', name: '累积（次）', minInterval: 1, splitLine: { show: false } }
            ],
            series: [{
                name: '每日打卡次数（柱）',
                type: 'bar',
                yAxisIndex: 0,
                data: dates.map(function(d) { return byDay[d].count; }),
                barMaxWidth: 18,
                itemStyle: {
                    borderRadius: [4, 4, 0, 0],
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: card.type === 'trade' ? '#f0b90b' : '#4da3ff' },
                        { offset: 1, color: card.type === 'trade' ? '#d4a017' : '#007bff' }
                    ])
                }
            }, {
                name: '每日打卡次数',
                type: 'line',
                yAxisIndex: 0,
                data: dates.map(function(d) { return byDay[d].count; }),
                smooth: true,
                symbol: 'circle', symbolSize: 5,
                lineStyle: { width: 2, color: '#007bff' },
                itemStyle: { color: '#007bff' }
            }, {
                name: '累积打卡次数',
                type: 'line',
                yAxisIndex: 1,
                data: cumCounts,
                smooth: true,
                symbol: 'circle', symbolSize: 5,
                lineStyle: { width: 2, color: '#28a745' },
                itemStyle: { color: '#28a745' }
            }]
        };
        if (dates.length > 20) {
            dailyOption.dataZoom = [{ type: 'inside', start: dates.length - 20, end: 100 }, { type: 'slider', height: 16, bottom: 6, start: dates.length - 20, end: 100 }];
        }
        dailyChart.setOption(dailyOption);
    }

    /* ---- 小记 Tab ---- */
    function renderNotesTab(card) {
        var notes = card.notes || [];
        /* 输入框固定上端（sticky），避免记录过多被挤到底部不可见 */
        var html = '<div class="note-input-wrap">';
        html += '<div style="font-size:.9rem;font-weight:600;margin-bottom:8px;">📝 过程小记（' + notes.length + ' 条）</div>';
        html += '<div class="milestone-add"><input id="note-input" placeholder="写下此刻的想法…"><button class="m-btn m-btn-sm" id="note-add">添加</button></div>';
        html += '</div>';
        if (!notes.length) html += '<p style="font-size:.85rem;color:var(--text-muted);">暂无小记，随手记录你的思考与感悟</p>';
        /* 倒序展示：最新的小记排在最上面（存储仍按追加顺序，仅渲染翻转） */
        notes.slice().reverse().forEach(function(n) {
            html += '<div class="note-item"><div class="note-time">' + esc(n.time) + '</div><div class="note-content">' + esc(n.content) + '</div></div>';
        });

        $('#cm-body').innerHTML = html;

        $('#note-add').addEventListener('click', function() { addNote(card); });
        $('#note-input').addEventListener('keydown', function(e) { if (e.key === 'Enter') addNote(card); });
    }

    function addNote(card) {
        var content = $('#note-input').value.trim();
        if (!content) return;
        apiPost('/plan/api/add-note', { plan_id: card.plan_id, card_id: card.id, content: content }).then(function(res) {
            if (res.code === 200) refreshModal();
            else MDialog.alert(res.message || '添加失败');
        });
    }

    /* ---- 结算 Tab（长期主义版：无罚款，只有奖励与在场记录） ---- */
    function renderSettleTab(card) {
        var ri = card.reward_info || {};
        var msDone = (card.milestones || []).filter(function(m) { return m.done; }).length;
        var msTotal = (card.milestones || []).length;
        var html = '<div class="settle-info">';
        html += '<div class="si-row"><span>基础奖励</span><span>' + fmtNum(ri.base_reward) + ' 元</span></div>';
        html += '<div class="si-row"><span>已勾选小时</span><span>' + ri.filled_count + ' / 100 h</span></div>';
        html += '<div class="si-row"><span>🌱 在场天数</span><span>' + (ri.presence_days || 0) + ' 天</span></div>';
        if (ri.early_eligible) {
            html += '<div class="si-row"><span>提前奖励</span><span>' + fmtNum(ri.early_bonus) + ' 元（提前 ' + (ri.early_hours || 0) + 'h）</span></div>';
        }
        html += '<div class="si-row total"><span>预计奖励</span><span>' + fmtNum(ri.final_reward) + ' 元</span></div>';
        html += '</div>';

        if (card.settlement) {
            var s = card.settlement;
            html += '<div class="settle-info" style="border:1px solid ' + (card.status === 'completed' ? '#28a745' : '#dc3545') + ';">';
            html += '<div class="si-row"><span>结算状态</span><span>' + (card.status === 'completed' ? '✅ 通关' : '❌ 失败') + '</span></div>';
            if (s.profit_multiplier !== undefined) html += '<div class="si-row"><span>资金倍数</span><span>' + fmtNum(s.profit_multiplier) + ' 倍</span></div>';
            html += '<div class="si-row"><span>实际用时</span><span>' + s.total_hours_used + ' h</span></div>';
            if (s.early_hours !== undefined) html += '<div class="si-row"><span>提前完成</span><span>' + s.early_hours + ' h</span></div>';
            if (s.early_bonus !== undefined) html += '<div class="si-row"><span>提前奖励</span><span>' + fmtNum(s.early_bonus) + ' 元</span></div>';
            html += '<div class="si-row total"><span>最终奖励</span><span>' + fmtNum(s.final_reward) + ' 元</span></div>';
            html += '<div class="si-row"><span>结算时间</span><span>' + esc(s.settled_at) + '</span></div>';
            html += '</div>';
        } else if (card.status === 'in_progress') {
            if (card.type === 'trade') {
                html += '<p style="font-size:.85rem;color:var(--text-muted);">交易轮次需手动确认「资金倍数」后结算（倍数 ≥ 2 视为翻倍通关）。</p>';
                html += '<button class="m-btn m-btn-primary" id="btn-settle">💰 结算本轮</button>';
            } else if (card.filled_count >= 100 || ri.early_eligible) {
                if (ri.early_eligible && card.filled_count < 100) {
                    html += '<p style="font-size:.85rem;color:#28a745;">🎉 子目标已全部完成（' + msDone + '/' + msTotal + '）——任务完成即通关，无需硬耗 100 小时！</p>';
                } else {
                    html += '<p style="font-size:.85rem;color:var(--text-muted);">100 小时已填满，可手动结算（满格时通常已自动结算）。</p>';
                }
                html += '<button class="m-btn m-btn-primary" id="btn-settle">💰 结算任务</button>';
            } else {
                html += '<p style="font-size:.85rem;color:var(--text-muted);">完成 100 小时后自动结算（当前 ' + card.filled_count + 'h）。</p>';
                if (msTotal > 0) {
                    html += '<p style="font-size:.85rem;color:var(--text-muted);">或完成全部子目标（' + msDone + '/' + msTotal + '）即可提前通关，无需等满 100 小时 🌱</p>';
                } else {
                    html += '<p style="font-size:.85rem;color:var(--text-muted);">💡 在「概览」中添加子目标并全部完成后，可提前通关拿奖励</p>';
                }
            }
        }

        $('#cm-body').innerHTML = html;

        var btnSettle = $('#btn-settle');
        if (btnSettle) btnSettle.addEventListener('click', function() { settleCard(card); });
    }

    /* ================================================================
       操作
       ================================================================ */
    function saveCardFields(card) {
        var startInput = $('#ov-start');
        var endInput = $('#ov-end');
        // 原本没有时间且用户未修改默认值（仍为打开弹窗时的当前时间）→ 保持为空，避免误写入
        var startVal = startInput.value;
        var endVal = endInput.value;
        if (!card.start_time && startVal === startInput.dataset.default) startVal = '';
        if (!card.end_time && endVal === endInput.dataset.default) endVal = '';
        var fields = {
            goal: $('#ov-goal').value.trim(),
            start_time: fromDatetimeLocal(startVal),
            end_time: fromDatetimeLocal(endVal)
        };
        var rewardEl = $('#ov-reward');
        if (rewardEl) fields.reward = parseInt(rewardEl.value) || 0;
        if (!fields.goal) { MDialog.alert('任务目标不能为空'); return; }

        apiPost('/plan/api/update-card', {
            plan_id: card.plan_id,
            card_id: card.id,
            fields: fields
        }).then(function(res) {
            if (res.code === 200) { MDialog.alert({ message: '💾 保存成功', type: 'success' }); refreshModal(); }
            else MDialog.alert(res.message || '保存失败');
        });
    }

    function addMilestone(card) {
        var input = $('#ms-input');
        var content = input.value.trim();
        if (!content) return;
        saveMilestones(card, (card.milestones || []).concat([{ content: content, done: false }]));
    }

    function toggleMilestone(card, index) {
        apiPost('/plan/api/toggle-milestone', {
            plan_id: card.plan_id,
            card_id: card.id,
            milestone_index: index
        }).then(function(res) {
            if (res.code === 200) {
                var d = res.data;
                // 兼容新接口（返回对象含 milestones/all_done/early_eligible）
                var milestones = d.milestones || d;
                // 检测刚刚勾选了最后一个子目标 → 弹出提前通关引导
                if (d.all_done && d.early_eligible && d.card_status === 'in_progress') {
                    refreshModal();
                    var msTotal = (milestones || []).length;
                    var filledH = d.filled_count || 0;
                    var remainH = Math.max(0, 100 - filledH);
                    MDialog.show({
                        title: '🎉 子目标全部完成！',
                        message: '你已完成全部 ' + msTotal + ' 个子目标——任务完成即通关，无需硬耗 100 小时！' +
                            '<br><br>当前已打卡 <b>' + filledH + '</b> 小时，剩余 <b>' + remainH + '</b> 小时可获提前奖励。' +
                            '<br><br>立即结算可解锁下一张任务卡 🚀',
                        type: 'success',
                        buttons: [
                            { text: '稍后再说', type: 'cancel' },
                            { text: '去结算 💰', type: 'primary', onClick: function() {
                                state.activeTab = 'settle';
                                switchTab('settle');
                            }}
                        ]
                    });
                } else {
                    refreshModal();
                }
            } else {
                MDialog.alert(res.message || '操作失败');
            }
        });
    }

    /* 整体提交 milestones 数组（与 TodoList 模式一致，增删改均走 update-card） */
    function saveMilestones(card, milestones) {
        apiPost('/plan/api/update-card', {
            plan_id: card.plan_id,
            card_id: card.id,
            fields: { milestones: milestones }
        }).then(function(res) {
            if (res.code === 200) refreshModal();
            else MDialog.alert(res.message || '操作失败');
        });
    }

    function deleteMilestone(card, index) {
        var m = (card.milestones || [])[index];
        MDialog.danger('确定删除子目标「' + ((m && m.content) || '') + '」吗？', function() {
            var milestones = (card.milestones || []).filter(function(t, i) { return i !== index; });
            saveMilestones(card, milestones);
        });
    }

    /* 行内编辑子目标：点击 ✏️ 替换为 input，Enter/失焦提交，Esc 还原 */
    function startEditMilestone(card, index) {
        var item = $all('.milestone-item')[index];
        if (!item) return;
        var contentSpan = item.querySelector('.ms-content');
        var oldContent = (card.milestones || [])[index] ? card.milestones[index].content : '';
        var input = document.createElement('input');
        input.type = 'text';
        input.className = 'ms-edit-input';
        input.value = oldContent;
        contentSpan.replaceWith(input);
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);

        var committed = false;
        function commit() {
            if (committed) return;
            committed = true;
            var newContent = input.value.trim();
            if (!newContent || newContent === oldContent) { refreshModal(); return; }
            var milestones = (card.milestones || []).map(function(m, i) {
                return i === index ? { content: newContent, done: m.done } : m;
            });
            saveMilestones(card, milestones);
        }
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { e.preventDefault(); commit(); }
            else if (e.key === 'Escape') { committed = true; refreshModal(); }
        });
        input.addEventListener('blur', commit);
    }

    /* ---- TodoList 操作（模块6：整体数组提交 update-card，与 milestone 模式一致） ---- */
    function saveTodos(card, todos) {
        apiPost('/plan/api/update-card', {
            plan_id: card.plan_id,
            card_id: card.id,
            fields: { todos: todos }
        }).then(function(res) {
            if (res.code === 200) refreshModal();
            else MDialog.alert(res.message || '操作失败');
        });
    }

    function addTodo(card) {
        var input = $('#todo-input');
        var content = input.value.trim();
        if (!content) return;
        saveTodos(card, (card.todos || []).concat([{ content: content, done: false }]));
    }

    function toggleTodo(card, index) {
        var todos = (card.todos || []).map(function(t, i) {
            return i === index ? { content: t.content, done: !t.done } : t;
        });
        saveTodos(card, todos);
    }

    function deleteTodo(card, index) {
        MDialog.danger('确定删除这条待办吗？', function() {
            var todos = (card.todos || []).filter(function(t, i) { return i !== index; });
            saveTodos(card, todos);
        });
    }

    /* 行内编辑：点击 ✏️ 替换为 input，Enter/失焦提交，Esc 还原 */
    function startEditTodo(card, index) {
        var item = $all('.todo-item')[index];
        if (!item) return;
        var contentSpan = item.querySelector('.todo-content');
        var oldContent = (card.todos || [])[index] ? card.todos[index].content : '';
        var input = document.createElement('input');
        input.type = 'text';
        input.className = 'todo-edit-input';
        input.value = oldContent;
        contentSpan.replaceWith(input);
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);

        var committed = false;
        function commit() {
            if (committed) return;
            committed = true;
            var newContent = input.value.trim();
            if (!newContent || newContent === oldContent) { refreshModal(); return; }
            var todos = (card.todos || []).map(function(t, i) {
                return i === index ? { content: newContent, done: t.done } : t;
            });
            saveTodos(card, todos);
        }
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { e.preventDefault(); commit(); }
            else if (e.key === 'Escape') { committed = true; refreshModal(); }
        });
        input.addEventListener('blur', commit);
    }

    function abandonCard(card) {
        MDialog.confirm('确定放弃「' + card.title + '」吗？\n放弃后该轮奖励为 0，并将解锁下一张任务卡。', function() {
            apiPost('/plan/api/update-card', {
                plan_id: card.plan_id,
                card_id: card.id,
                fields: { status: 'abandoned' }
            }).then(function(res) {
                if (res.code === 200) {
                    MDialog.alert({ message: '已放弃该任务', type: 'warning' });
                    closeCardModal();
                    loadAll();
                } else {
                    MDialog.alert(res.message || '操作失败');
                }
            });
        });
    }

    function deleteCard(card) {
        MDialog.danger('确定删除任务卡「' + card.title + '」吗？\n该卡所有打卡记录将一并删除，此操作不可恢复！', function() {
            apiPost('/plan/api/delete-card', {
                plan_id: card.plan_id,
                card_id: card.id
            }).then(function(res) {
                if (res.code === 200) {
                    MDialog.alert({ message: '已删除', type: 'success' });
                    closeCardModal();
                    loadAll();
                } else {
                    MDialog.alert(res.message || '删除失败');
                }
            });
        });
    }

    function settleCard(card) {
        if (card.type === 'trade') {
            MDialog.show({
                title: '💰 结算本轮 · ' + card.title,
                message: '<div class="plan-form"><div class="form-row">' +
                    '<label>当前资金倍数（本金 × N）</label>' +
                    '<input type="number" id="sl-multiplier" step="0.1" min="0.1" placeholder="如 2 表示本金翻倍">' +
                    '</div><p style="font-size:.8rem;color:var(--text-muted);margin:0;">倍数 ≥ 2 判定翻倍通关；未翻倍且未满 100 小时不能结算</p></div>',
                okText: '确认结算',
                onOk: function() {
                    var mul = parseFloat($('#sl-multiplier').value);
                    if (isNaN(mul) || mul <= 0) { MDialog.alert('请输入有效的资金倍数'); return false; }
                    doSettle(card, mul);
                }
            });
        } else {
            MDialog.confirm('确认结算「' + card.title + '」？', function() { doSettle(card, null); });
        }
    }

    function doSettle(card, multiplier) {
        var payload = { plan_id: card.plan_id, card_id: card.id };
        if (multiplier !== null) payload.profit_multiplier = multiplier;
        apiPost('/plan/api/settle-round', payload).then(function(res) {
            if (res.code === 200) {
                var s = res.data.settlement || {};
                var msg = '🎉 结算成功！\n' +
                    (s.profit_multiplier !== undefined ? '资金倍数：' + fmtNum(s.profit_multiplier) + ' 倍\n' : '') +
                    '基础奖励：' + fmtNum(s.base_reward) + ' 元\n' +
                    (s.early_bonus !== undefined && s.early_bonus > 0 ? '提前奖励：' + fmtNum(s.early_bonus) + ' 元\n' : '') +
                    '最终奖励：' + fmtNum(s.final_reward) + ' 元\n' +
                    '实际用时：' + fmtNum(s.total_hours_used) + ' h';
                showSettleResult(card, res.data);
                refreshModal();
            } else {
                MDialog.alert(res.message || '结算失败');
            }
        });
    }

    /* 结算后引导（v1.1 再循环：防止完成后的动力断崖）
       还有未结束的卡 → 去开始下一张；没有 → 再开一卡 */
    function showSettleResult(card, data) {
        var s = data.settlement || {};
        var ok = data.status === 'completed';
        var msg = (ok ? '🎉 结算成功！\n' : '💔 本轮失败\n') +
            (s.profit_multiplier !== undefined ? '资金倍数：' + fmtNum(s.profit_multiplier) + ' 倍\n' : '') +
            '基础奖励：' + fmtNum(s.base_reward) + ' 元\n' +
            (s.early_bonus !== undefined && s.early_bonus > 0 ? '提前奖励：' + fmtNum(s.early_bonus) + ' 元\n' : '') +
            '最终奖励：' + fmtNum(s.final_reward) + ' 元\n' +
            '实际用时：' + fmtNum(s.total_hours_used) + ' h';
        if (ok) msg += '\n你完成了这个任务——你是一个长期主义者 🌱';

        var others = state.allCards.filter(function(c) {
            return c.id !== card.id && (c.status === 'pending' || c.status === 'in_progress');
        });
        var buttons = [{ text: '关闭', type: 'cancel' }];
        if (others.length) {
            var next = others[0];
            buttons.push({
                text: '去开始下一张 🚀',
                type: 'primary',
                onClick: function() {
                    var fresh = findCard(next.id);
                    if (fresh) openCardModal(fresh.id, 'slots');
                }
            });
        } else {
            buttons.push({
                text: '再开一卡 🌱',
                type: 'primary',
                onClick: function() { openCreateCard(card.type); }
            });
        }
        MDialog.show({
            title: ok ? '🏆 通关成功' : '💔 本轮失败',
            message: msg,
            type: ok ? 'success' : 'warning',
            buttons: buttons
        });
    }

    /* ---- 手动创建任务卡 ---- */
    function openCreateCard(type) {
        var plan = getPlanByType(type);
        if (!plan) { MDialog.alert('未找到对应类型的计划'); return; }
        var isLearn = type === 'learn';
        var defaultReward = isLearn ? nextLearnReward(plan) : nextTradeReward(plan);

        var html = '<div class="plan-form">' +
            '<div class="form-row"><label>卡片类型</label><input type="text" value="' + (isLearn ? '📚 学习任务' : '💰 交易任务') + '" disabled></div>' +
            '<div class="form-row"><label>任务名称</label><input type="text" id="nc-title" placeholder="如：学习任务11"></div>' +
            '<div class="form-row"><label>任务目标</label><input type="text" id="nc-goal" placeholder="这个任务的目标是什么？"></div>' +
            '<div class="form-row"><label>' + (isLearn ? '完成奖励（元，默认继承上一张）' : '基础奖金（元，默认上一轮 + 2000）') + '</label>' +
            '<input type="number" id="nc-reward" value="' + defaultReward + '"></div>' +
            '<div class="form-row"><label>开始时间（可改，默认现在）</label><input type="datetime-local" id="nc-start" value="' + formatLocalDateTime(new Date()) + '"></div>' +
            '</div>';

        MDialog.show({
            title: isLearn ? '＋ 新学习任务' : '＋ 新交易任务',
            message: html,
            okText: '创建',
            onOk: function() {
                var title = $('#nc-title').value.trim() || (isLearn ? '学习任务' : '交易任务');
                var payload = {
                    plan_id: plan.id,
                    type: type,
                    title: title,
                    goal: $('#nc-goal').value.trim(),
                    start_time: fromDatetimeLocal($('#nc-start').value)
                };
                payload[isLearn ? 'reward' : 'base_reward'] = parseInt($('#nc-reward').value) || defaultReward;
                apiPost('/plan/api/create-card', payload).then(function(res) {
                    if (res.code === 200) {
                        MDialog.alert({ message: '创建成功', type: 'success' });
                        loadAll();
                    } else {
                        MDialog.alert(res.message || '创建失败');
                    }
                });
            }
        });
    }

    /* 打卡变更后的局部刷新：用接口返回的 refresh 载荷就地更新总览/趋势图/计划详情/今日面板/卡片弹窗，
       免去 list + today-status + card-detail 三次远程往返。无载荷时兜底退回整表刷新。 */
    function applyRefreshPayload(refresh) {
        if (!refresh) { refreshModal(); return; }
        if (refresh.plan) {
            var pid = refresh.plan.id;
            for (var i = 0; i < state.plans.length; i++) {
                if (state.plans[i].id === pid) { state.plans[i] = refresh.plan; break; }
            }
            // 同步该计划的卡片摘要（renderPlanDetail 数据源）：先剔除旧卡再并入新卡
            var newCards = ((((refresh.plan.stats || {}).cards)) || []).map(function(c) {
                return Object.assign({}, c, { plan_id: pid });
            });
            state.allCards = state.allCards.filter(function(c) { return c.plan_id !== pid; }).concat(newCards);
        }
        if (refresh.today) {
            var matched = false;
            for (var j = 0; j < state.today.length; j++) {
                if (state.today[j].plan_id === refresh.today.plan_id) { state.today[j] = refresh.today; matched = true; break; }
            }
            if (!matched) state.today.push(refresh.today);
        }
        if (refresh.card) state.currentCard = refresh.card;
        renderOverview();
        renderCheckinStatsChart();
        renderPlanDetail();
        renderToday();
        // 弹窗内容重渲染：switchTab 内部先 disposeModalCharts，避免 ECharts 重复初始化
        var modal = $('#card-modal');
        if (state.currentCard && modal && modal.style.display === 'flex') {
            var titleEl = $('#cm-title');
            if (titleEl && refresh.card && refresh.card.title) titleEl.textContent = refresh.card.title;
            renderCardTabs();
            switchTab(state.activeTab);
        }
    }

    /* 刷新弹窗内容 */
    function refreshModal() {
        loadAll().then(function() {
            if (state.currentCard) {
                var fresh = findCard(state.currentCard.id);
                if (fresh) openCardModal(fresh.id, state.activeTab);
                else closeCardModal();
            }
        });
    }

    /* ---------- 初始化 ---------- */
    function init() {
        var style = document.createElement('style');
        style.textContent = FORM_CSS;
        document.head.appendChild(style);

        /* 主页打卡趋势图响应式适配 */
        window.addEventListener('resize', function() {
            if (mainCheckinChart) mainCheckinChart.resize();
        });

        loadAll();
    }

    return {
        init: init,
        closeCardModal: closeCardModal
    };
})();

document.addEventListener('DOMContentLoaded', TaskPlan.init);
