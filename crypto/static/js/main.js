document.addEventListener('DOMContentLoaded', () => {
    const loadingEl = document.getElementById('loading');
    const dataPanelEl = document.getElementById('data-panel');
    const overallEl = document.getElementById('overall-market');
    const refreshBtn = document.getElementById('refresh-btn');
    const coinSelectorEl = document.getElementById('coin-selector');
    const saveCoinsBtn = document.getElementById('save-coins-btn');

    let marketStats = { up: 0, down: 0, wait: 0, total: 0, loaded: 0 };
    let ALL_COINS = [];
    let FIXED_COINS = [];
    let FLOATING_COINS = [];
    let SELECTED_COINS = [];
    let STARRED_COINS = [];
    let coinDataCache = {};

    // ================================================================
    // 多周期支持
    // ================================================================
    const AVAILABLE_BARS = ['3m', '5m', '15m', '30m', '1H', '2H', '4H', '6H', '12H', '1D', '1W'];
    let CURRENT_BAR = '1H';

    /** 渲染周期选择器 */
    function renderBarSelector() {
        const container = document.getElementById('bar-selector');
        if (!container) return;
        container.innerHTML = '<span class="bar-label">📊 周期:</span>' +
            AVAILABLE_BARS.map(bar => {
                const active = bar === CURRENT_BAR ? ' active' : '';
                return `<button class="m-btn m-btn-primary m-btn-xs m-btn-outline m-btn-pill${active}" data-bar="${bar}">${bar}</button>`;
            }).join('');

        // 绑定点击事件
        container.querySelectorAll('button[data-bar]').forEach(btn => {
            btn.addEventListener('click', () => {
                const bar = btn.dataset.bar;
                if (bar === CURRENT_BAR) return;
                CURRENT_BAR = bar;
                // 更新按钮激活状态
                container.querySelectorAll('button[data-bar]').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                // 清除缓存，重新加载
                coinDataCache = {};
                startRefresh();
            });
        });
    }

    // 初始化：加载配置
    async function init() {
        try {
            const res = await fetch('/api/strategy/config');
            const result = await res.json();
            if (result.code === 200) {
                ALL_COINS = result.data.all_coins || [];
                FIXED_COINS = result.data.fixed_coins || [];
                FLOATING_COINS = result.data.floating_coins || [];
                SELECTED_COINS = result.data.selected_coins || [];
                STARRED_COINS = result.data.starred_coins || [];
            }
        } catch (e) {
            console.warn('加载配置失败，使用默认值');
            ALL_COINS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'NEAR-USDT-SWAP'];
            SELECTED_COINS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'NEAR-USDT-SWAP'];
            STARRED_COINS = [];
        }
        renderCoinSelector();
        renderBarSelector();
        initCards();
        // 恢复上次保存的刷新间隔到输入框
        if (refreshIntervalInput) refreshIntervalInput.value = autoRefreshInterval;
        startRefresh();
    }

    // 保存星标到后端
    async function saveStarredCoins() {
        try {
            await fetch('/api/strategy/starred', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ starred_coins: STARRED_COINS })
            });
        } catch (e) {
            console.warn('保存星标失败:', e);
        }
    }

    // 切换星标
    function toggleStar(coin) {
        var idx = STARRED_COINS.indexOf(coin);
        if (idx >= 0) {
            STARRED_COINS.splice(idx, 1);
        } else {
            STARRED_COINS.push(coin);
        }
        // 只更新星标图标，不重建整个选择器
        updateAllStarIcons();
        saveStarredCoins();
    }

    // 更新所有星标图标状态
    function updateAllStarIcons() {
        coinSelectorEl.querySelectorAll('.coin-tag').forEach(function(tag) {
            var coin = tag.dataset.coin;
            var starEl = tag.querySelector('.star-icon');
            if (starEl) {
                if (STARRED_COINS.includes(coin)) {
                    starEl.classList.add('starred');
                    starEl.textContent = '★';
                    tag.classList.add('star-highlight');
                } else {
                    starEl.classList.remove('starred');
                    starEl.textContent = '☆';
                    tag.classList.remove('star-highlight');
                }
            }
        });
    }

    // 渲染币种多选器（含星标）
    function renderCoinSelector() {
        coinSelectorEl.innerHTML = '';
        var floatingSet = {};
        FLOATING_COINS.forEach(function(c) { floatingSet[c] = true; });
        ALL_COINS.forEach(function(coin) {
            var isFloating = !!floatingSet[coin];
            var tag = document.createElement('div');
            var isSelected = SELECTED_COINS.includes(coin);
            var isStarred = STARRED_COINS.includes(coin);
            tag.className = 'coin-tag' + (isSelected ? ' active' : '') + (isStarred ? ' star-highlight' : '') + (isFloating ? ' coin-floating' : '');
            tag.dataset.coin = coin;

            // 星标图标
            var starSpan = document.createElement('span');
            starSpan.className = 'star-icon' + (isStarred ? ' starred' : '');
            starSpan.textContent = isStarred ? '★' : '☆';
            starSpan.title = isStarred ? '取消星标' : '设为星标';
            starSpan.addEventListener('click', function(e) {
                e.stopPropagation();  // 阻止冒泡，不影响选中
                toggleStar(coin);
            });
            tag.appendChild(starSpan);

            // 币种名称
            var nameSpan = document.createElement('span');
            nameSpan.textContent = coin.replace('-USDT-SWAP', '');
            tag.appendChild(nameSpan);

            // 浮动币种：显示删除按钮 + 标记
            if (isFloating) {
                var delSpan = document.createElement('span');
                delSpan.className = 'coin-del';
                delSpan.textContent = '×';
                delSpan.title = '移除该浮动币种';
                delSpan.addEventListener('click', function(e) {
                    e.stopPropagation();
                    removeFloatingCoin(coin);
                });
                tag.appendChild(delSpan);
            }

            // 点击标签切换选中状态
            tag.addEventListener('click', function() {
                tag.classList.toggle('active');
            });
            coinSelectorEl.appendChild(tag);
        });
    }

    // 移除浮动币种
    function removeFloatingCoin(coin) {
        MDialog.confirm('确定从监控中移除浮动币种 ' + coin.replace('-USDT-SWAP', '') + ' 吗？', function() {
            fetch('/api/strategy/floating-coins/remove', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ coin: coin })
            }).then(function(r) { return r.json(); }).then(function(res) {
                if (res.code === 200) {
                    ALL_COINS = res.data.all_coins || [];
                    FLOATING_COINS = res.data.floating_coins || [];
                    SELECTED_COINS = res.data.selected_coins || SELECTED_COINS;
                    renderCoinSelector();
                    coinDataCache = {};
                    initCards();
                    startRefresh();
                } else {
                    MDialog.alert({ message: '移除失败: ' + res.message, type: 'danger' });
                }
            }).catch(function() {
                MDialog.alert({ message: '移除失败: 网络错误', type: 'danger' });
            });
        });
    }

    // 一键清空所有浮动币种
    function clearAllFloatingCoins() {
        if (!FLOATING_COINS.length) {
            MDialog.alert({ message: '当前没有浮动币种，无需清空', type: 'info' });
            return;
        }
        MDialog.confirm('确定要移除所有当前选中的浮动币种吗？此操作不可恢复。', function() {
            fetch('/api/strategy/floating-coins/clear', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            }).then(function(r) { return r.json(); }).then(function(res) {
                if (res.code === 200) {
                    ALL_COINS = res.data.all_coins || [];
                    FLOATING_COINS = res.data.floating_coins || [];
                    SELECTED_COINS = res.data.selected_coins || SELECTED_COINS;
                    renderCoinSelector();
                    coinDataCache = {};
                    initCards();
                    startRefresh();
                    MDialog.alert({ message: '🗑️ ' + res.message, type: 'success' });
                } else {
                    MDialog.alert({ message: '清空失败: ' + res.message, type: 'danger' });
                }
            }).catch(function() {
                MDialog.alert({ message: '清空失败: 网络错误', type: 'danger' });
            });
        });
    }

    // 绑定清空浮动币种按钮
    var clearFloatingBtn = document.getElementById('clear-floating-btn');
    if (clearFloatingBtn) {
        clearFloatingBtn.addEventListener('click', clearAllFloatingCoins);
    }

    // 保存币种选择
    saveCoinsBtn.addEventListener('click', async () => {
        const activeTags = coinSelectorEl.querySelectorAll('.coin-tag.active');
        const selected = Array.from(activeTags).map(t => t.dataset.coin);
        if (selected.length === 0) {
            MDialog.alert({ message: '请至少选择一个币种', type: 'warning' });
            return;
        }
        saveCoinsBtn.textContent = '保存中...';
        saveCoinsBtn.disabled = true;
        try {
            const res = await fetch('/api/strategy/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ selected_coins: selected })
            });
            const result = await res.json();
            if (result.code === 200) {
                SELECTED_COINS = selected;
                coinDataCache = {};
                initCards();
                startRefresh();
            } else {
                MDialog.alert({ message: '保存失败: ' + result.message, type: 'danger' });
            }
        } catch (e) {
            MDialog.alert({ message: '保存失败: 网络错误', type: 'danger' });
        } finally {
            saveCoinsBtn.textContent = '保存选择';
            saveCoinsBtn.disabled = false;
        }
    });

    // ---- 同步CSV币种 ----
    var syncCsvBtn = document.getElementById('sync-csv-btn');
    syncCsvBtn.addEventListener('click', async function() {
        syncCsvBtn.textContent = '⏳ 同步中...';
        syncCsvBtn.disabled = true;
        try {
            var res = await fetch('/api/strategy/sync-coins', { method: 'POST' });
            var result = await res.json();
            if (result.code === 200) {
                ALL_COINS = result.data.all_coins || [];
                FIXED_COINS = result.data.fixed_coins || FIXED_COINS;
                FLOATING_COINS = result.data.floating_coins || FLOATING_COINS;
                SELECTED_COINS = result.data.selected_coins || [];
                STARRED_COINS = result.data.starred_coins || [];
                renderCoinSelector();
                coinDataCache = {};
                initCards();
                startRefresh();
                MDialog.alert({ message: '同步成功! 共发现 ' + ALL_COINS.length + ' 个币种', type: 'success' });
            } else {
                MDialog.alert({ message: '同步失败: ' + result.message, type: 'danger' });
            }
        } catch (e) {
            MDialog.alert({ message: '同步失败: 网络错误', type: 'danger' });
        } finally {
            syncCsvBtn.textContent = '🔄 同步';
            syncCsvBtn.disabled = false;
        }
    });

    // ---- 全选 ----
    var selectAllBtn = document.getElementById('select-all-btn');
    selectAllBtn.addEventListener('click', function() {
        coinSelectorEl.querySelectorAll('.coin-tag').forEach(function(tag) {
            tag.classList.add('active');
        });
    });

    // ---- 重置选择 ----
    var resetSelectionBtn = document.getElementById('reset-selection-btn');
    resetSelectionBtn.addEventListener('click', function() {
        MDialog.confirm('确定要重置所有币种选择状态吗？这将取消全部选中。', function() {
            coinSelectorEl.querySelectorAll('.coin-tag').forEach(function(tag) {
                tag.classList.remove('active');
            });
            // 同时清空后端选择
            fetch('/api/strategy/clear-selections', { method: 'POST' }).then(function() {
                SELECTED_COINS = [];
                coinDataCache = {};
                initCards();
                startRefresh();
            }).catch(function(e) {
                console.warn('清空选择失败:', e);
            });
        });
    });

    // ---- 全选星标 ----
    var selectAllStarredBtn = document.getElementById('select-all-starred-btn');
    selectAllStarredBtn.addEventListener('click', function() {
        coinSelectorEl.querySelectorAll('.coin-tag').forEach(function(tag) {
            var coin = tag.dataset.coin;
            if (STARRED_COINS.indexOf(coin) >= 0) {
                tag.classList.add('active');
            }
        });
    });

    // ---- 重置星标 ----
    var resetStarredBtn = document.getElementById('reset-starred-btn');
    resetStarredBtn.addEventListener('click', async function() {
        MDialog.confirm('确定要清除所有星标标记吗？', async function() {
            try {
                var res = await fetch('/api/strategy/starred/clear', { method: 'POST' });
                var result = await res.json();
                if (result.code === 200) {
                    STARRED_COINS = [];
                    updateAllStarIcons();
                } else {
                    MDialog.alert({ message: '清除星标失败: ' + result.message, type: 'danger' });
                }
            } catch (e) {
                MDialog.alert({ message: '清除星标失败: 网络错误', type: 'danger' });
            }
        });
    });

    // ================================================================
    //  高级筛选面板（币种选择器内）
    // ================================================================
    var coinFilterPanel = document.getElementById('coin-filter-panel');
    var toggleFilterBtn = document.getElementById('toggle-filter-btn');
    var coinFilterActive = false;
    var coinFilterMatchSet = null;
    var coinFilterSortKey = '';

    // 筛选面板显示/隐藏
    toggleFilterBtn.addEventListener('click', function() {
        var isVisible = coinFilterPanel.style.display !== 'none';
        if (isVisible) {
            coinFilterPanel.style.display = 'none';
            toggleFilterBtn.classList.remove('active');
            toggleFilterBtn.textContent = '🔽 筛选';
        } else {
            coinFilterPanel.style.display = 'block';
            toggleFilterBtn.classList.add('active');
            toggleFilterBtn.textContent = '🔼 筛选';
        }
    });

    // 筛选切换按钮点击事件
    coinFilterPanel.querySelectorAll('.filter-toggle').forEach(function(btn) {
        btn.addEventListener('click', function() {
            btn.classList.toggle('active');
        });
    });

    // 构建币种到CSV记录映射
    function getCoinRecordMap() {
        var map = {};
        if (typeof allRecords !== 'undefined' && allRecords.length > 0) {
            allRecords.forEach(function(r) {
                var instId = r['交易对'] || '';
                if (instId) map[instId] = r;
            });
        }
        return map;
    }

    // 收集当前激活的筛选条件
    function getActiveFilterState() {
        var state = {
            trends: [],
            combined: [],
            pnl: [],
            indicators: [],
            hasAny: false
        };
        // 趋势
        coinFilterPanel.querySelectorAll('#filter-trends .filter-toggle.active').forEach(function(btn) {
            state.trends.push(btn.dataset.val);
            state.hasAny = true;
        });
        // 组合
        coinFilterPanel.querySelectorAll('#filter-combined .filter-toggle.active').forEach(function(btn) {
            state.combined.push(btn.dataset.val);
            state.hasAny = true;
        });
        // 盈亏
        coinFilterPanel.querySelectorAll('#filter-pnl .filter-toggle.active').forEach(function(btn) {
            state.pnl.push({ period: btn.dataset.period, val: btn.dataset.val });
            state.hasAny = true;
        });
        // 指标
        var indDefs = [
            { id: 'f-adx-1h', col: 'ADX_1H' },
            { id: 'f-atr-1h', col: 'ATR_1H' },
            { id: 'f-adx-4h', col: 'ADX_4H' },
            { id: 'f-atr-4h', col: 'ATR_4H' },
            { id: 'f-adx-1d', col: 'ADX_1D' },
            { id: 'f-atr-1d', col: 'ATR_1D' }
        ];
        indDefs.forEach(function(def) {
            var input = document.getElementById(def.id);
            var val = parseFloat(input.value);
            if (!isNaN(val) && input.value.trim() !== '') {
                state.indicators.push({ col: def.col, min: val });
                state.hasAny = true;
            }
        });
        state.sortKey = document.getElementById('f-sort').value;
        return state;
    }

    // 应用筛选条件，返回匹配币种集合
    function getFilteredCoinSet(filterState, recordMap) {
        var matched = new Set(ALL_COINS);
        if (!filterState.hasAny) return matched;

        // 趋势（OR）
        if (filterState.trends.length > 0) {
            var trendSet = new Set();
            filterState.trends.forEach(function(tv) {
                var parts = tv.split('_');
                var col = parts[0] + '_趋势';
                var expected = parts[1];
                ALL_COINS.forEach(function(coin) {
                    var rec = recordMap[coin];
                    if (rec && rec[col] === expected) trendSet.add(coin);
                });
            });
            matched = new Set([...matched].filter(function(c) { return trendSet.has(c); }));
        }

        // 组合（OR）
        if (filterState.combined.length > 0) {
            var combinedSet = new Set();
            filterState.combined.forEach(function(cv) {
                ALL_COINS.forEach(function(coin) {
                    var rec = recordMap[coin];
                    if (!rec) return;
                    var t4h = rec['4H_趋势'];
                    var t1d = rec['1D_趋势'];
                    if (cv === '4H_1D_双涨' && t4h === '上涨' && t1d === '上涨') combinedSet.add(coin);
                    else if (cv === '4H_1D_双跌' && t4h === '下跌' && t1d === '下跌') combinedSet.add(coin);
                });
            });
            matched = new Set([...matched].filter(function(c) { return combinedSet.has(c); }));
        }

        // 盈亏（OR）
        if (filterState.pnl.length > 0) {
            var pnlSet = new Set();
            filterState.pnl.forEach(function(pf) {
                var col = pf.period + '_盈亏%';
                ALL_COINS.forEach(function(coin) {
                    var rec = recordMap[coin];
                    if (!rec) return;
                    var val = parseFloat(rec[col]);
                    if (isNaN(val)) return;
                    if (pf.val === 'profit' && val > 0) pnlSet.add(coin);
                    else if (pf.val === 'loss' && val < 0) pnlSet.add(coin);
                });
            });
            matched = new Set([...matched].filter(function(c) { return pnlSet.has(c); }));
        }

        // 指标（AND）
        filterState.indicators.forEach(function(ind) {
            var indSet = new Set();
            ALL_COINS.forEach(function(coin) {
                var rec = recordMap[coin];
                if (!rec) return;
                var val = parseFloat(rec[ind.col]);
                if (!isNaN(val) && val >= ind.min) indSet.add(coin);
            });
            matched = new Set([...matched].filter(function(c) { return indSet.has(c); }));
        });

        return matched;
    }

    // 排序
    function sortCoinList(coinList, sortKey, recordMap) {
        if (!sortKey) return coinList;
        var col = '';
        if (sortKey === 'ADX_4H_desc') col = 'ADX_4H';
        else if (sortKey === 'ATR_4H_desc') col = 'ATR_4H';
        else if (sortKey === 'ADX_1D_desc') col = 'ADX_1D';
        else if (sortKey === 'ATR_1D_desc') col = 'ATR_1D';
        else if (sortKey === 'pnl_1H_desc') col = '1H_盈亏%';
        else if (sortKey === 'pnl_4H_desc') col = '4H_盈亏%';
        else if (sortKey === 'pnl_1D_desc') col = '1D_盈亏%';
        else return coinList;

        return coinList.slice().sort(function(a, b) {
            var ra = recordMap[a], rb = recordMap[b];
            var va = ra ? parseFloat(ra[col]) : NaN;
            var vb = rb ? parseFloat(rb[col]) : NaN;
            if (isNaN(va) && isNaN(vb)) return 0;
            if (isNaN(va)) return 1;
            if (isNaN(vb)) return -1;
            return vb - va;  // desc
        });
    }

    // 应用筛选并重新渲染
    function applyCoinFilters() {
        var recordMap = getCoinRecordMap();
        if (Object.keys(recordMap).length === 0) {
            MDialog.alert({ message: '暂无批量趋势数据。<br>请先在下方“批量多周期趋势分析”面板中点击“批量更新趋势数据”按钮。', type: 'warning' });
            return;
        }
        var filterState = getActiveFilterState();
        var matchSet = getFilteredCoinSet(filterState, recordMap);
        var sortedCoins = sortCoinList(ALL_COINS, filterState.sortKey, recordMap);
        coinFilterMatchSet = matchSet;
        coinFilterActive = filterState.hasAny || !!filterState.sortKey;
        coinFilterSortKey = filterState.sortKey;
        renderCoinSelectorWithFilter(sortedCoins, matchSet);
        updateCoinFilterStats(matchSet.size);
    }

    // 清除筛选
    function clearCoinFilters() {
        coinFilterPanel.querySelectorAll('.filter-toggle.active').forEach(function(btn) {
            btn.classList.remove('active');
        });
        document.getElementById('f-adx-1h').value = '25';
        document.getElementById('f-atr-1h').value = '10';
        document.getElementById('f-adx-4h').value = '25';
        document.getElementById('f-atr-4h').value = '20';
        document.getElementById('f-adx-1d').value = '25';
        document.getElementById('f-atr-1d').value = '30';
        document.getElementById('f-sort').value = '';
        coinFilterActive = false;
        coinFilterMatchSet = null;
        coinFilterSortKey = '';
        renderCoinSelector();
        document.getElementById('coin-filter-stats').style.display = 'none';
    }

    // 批量星标
    function batchStarFiltered() {
        if (!coinFilterMatchSet || coinFilterMatchSet.size === 0) {
            MDialog.alert({ message: '请先应用筛选条件，确保有匹配的币种后再执行批量星标。', type: 'warning' });
            return;
        }
        var filteredList = [];
        coinFilterMatchSet.forEach(function(coin) { filteredList.push(coin); });
        MDialog.confirm('确定将当前筛选出的 ' + filteredList.length + ' 个币种全部设为星标吗？', function() {
            filteredList.forEach(function(coin) {
                if (STARRED_COINS.indexOf(coin) < 0) STARRED_COINS.push(coin);
            });
            updateAllStarIcons();
            saveStarredCoins();
            MDialog.alert({ message: '已成功将 ' + filteredList.length + ' 个币种设为星标！', type: 'success' });
        });
    }

    // 更新筛选统计
    function updateCoinFilterStats(matchCount) {
        var statsEl = document.getElementById('coin-filter-stats');
        if (coinFilterActive && coinFilterMatchSet) {
            statsEl.style.display = 'block';
            statsEl.textContent = '筛选结果: ' + matchCount + ' / ' + ALL_COINS.length + ' 个币种匹配' +
                (coinFilterSortKey ? ' | 已排序' : '');
        } else {
            statsEl.style.display = 'none';
        }
    }

    // 带筛选高亮的币种选择器渲染
    function renderCoinSelectorWithFilter(sortedCoins, matchSet) {
        coinSelectorEl.innerHTML = '';
        sortedCoins.forEach(function(coin) {
            var tag = document.createElement('div');
            var isSelected = SELECTED_COINS.includes(coin);
            var isStarred = STARRED_COINS.includes(coin);
            var isMatched = matchSet ? matchSet.has(coin) : true;
            var classes = 'coin-tag';
            if (isSelected) classes += ' active';
            if (isStarred) classes += ' star-highlight';
            if (!isMatched && coinFilterActive) classes += ' coin-filtered-out';
            tag.className = classes;
            tag.dataset.coin = coin;
            var starSpan = document.createElement('span');
            starSpan.className = 'star-icon' + (isStarred ? ' starred' : '');
            starSpan.textContent = isStarred ? '★' : '☆';
            starSpan.title = isStarred ? '取消星标' : '设为星标';
            starSpan.addEventListener('click', function(e) {
                e.stopPropagation();
                toggleStar(coin);
            });
            tag.appendChild(starSpan);
            var nameSpan = document.createElement('span');
            nameSpan.textContent = coin.replace('-USDT-SWAP', '');
            tag.appendChild(nameSpan);
            tag.addEventListener('click', function() {
                tag.classList.toggle('active');
            });
            coinSelectorEl.appendChild(tag);
        });
        updateCoinFilterStats(matchSet ? matchSet.size : ALL_COINS.length);
    }

    // 按钮事件
    document.getElementById('apply-coin-filter-btn').addEventListener('click', applyCoinFilters);
    document.getElementById('clear-coin-filter-btn').addEventListener('click', clearCoinFilters);
    document.getElementById('batch-star-filtered-btn').addEventListener('click', batchStarFiltered);

    // 排序下拉变更自动触发
    document.getElementById('f-sort').addEventListener('change', function() {
        if (coinFilterActive || coinFilterMatchSet) applyCoinFilters();
    });

    // 指标输入框回车触发筛选
    coinFilterPanel.querySelectorAll('.filter-num').forEach(function(input) {
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') applyCoinFilters();
        });
    });

    // 初始化卡片
    function initCards() {
        dataPanelEl.innerHTML = '';
        dataPanelEl.style.display = 'grid';
        if (loadingEl) loadingEl.style.display = 'none';

        SELECTED_COINS.forEach(coin => {
            const safeId = coin.replace(/-/g, '_');
            const cardHtml = `<div class="card card-draggable" id="card_${safeId}" draggable="true" data-coin="${coin}"></div>`;
            dataPanelEl.innerHTML += cardHtml;
            renderEmptyCard(coin);
        });
        initCardDragSort();
    }

    // ---- 卡片拖拽排序 ----
    let dragSrcEl = null;

    function initCardDragSort() {
        var cards = dataPanelEl.querySelectorAll('.card-draggable');
        cards.forEach(function(card) {
            card.addEventListener('dragstart', handleDragStart);
            card.addEventListener('dragover', handleDragOver);
            card.addEventListener('dragenter', handleDragEnter);
            card.addEventListener('dragleave', handleDragLeave);
            card.addEventListener('drop', handleDrop);
            card.addEventListener('dragend', handleDragEnd);
        });
    }

    function handleDragStart(e) {
        dragSrcEl = this;
        this.classList.add('card-dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', this.dataset.coin);
    }

    function handleDragOver(e) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
    }

    function handleDragEnter(e) {
        e.preventDefault();
        if (this !== dragSrcEl) {
            this.classList.add('card-drag-over');
        }
    }

    function handleDragLeave(e) {
        this.classList.remove('card-drag-over');
    }

    function handleDrop(e) {
        e.preventDefault();
        e.stopPropagation();
        this.classList.remove('card-drag-over');
        if (dragSrcEl !== this) {
            // 交换位置
            var parent = dataPanelEl;
            var allCards = Array.from(parent.querySelectorAll('.card-draggable'));
            var srcIdx = allCards.indexOf(dragSrcEl);
            var tgtIdx = allCards.indexOf(this);
            if (srcIdx < tgtIdx) {
                parent.insertBefore(dragSrcEl, this.nextSibling);
                parent.insertBefore(this, allCards[srcIdx]);
            } else {
                parent.insertBefore(dragSrcEl, this);
                parent.insertBefore(this, allCards[srcIdx].nextSibling || null);
            }
            // 同步更新 SELECTED_COINS 顺序
            var newOrder = Array.from(parent.querySelectorAll('.card-draggable')).map(function(c) { return c.dataset.coin; });
            SELECTED_COINS = newOrder;
            // 保存顺序到后端
            fetch('/api/strategy/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ selected_coins: newOrder })
            }).catch(function(e) { console.warn('保存卡片顺序失败:', e); });
        }
    }

    function handleDragEnd(e) {
        this.classList.remove('card-dragging');
        dataPanelEl.querySelectorAll('.card-drag-over').forEach(function(el) {
            el.classList.remove('card-drag-over');
        });
    }

    // 渲染空卡片
    function renderEmptyCard(coinId) {
        const safeId = coinId.replace(/-/g, '_');
        const cardEl = document.getElementById(`card_${safeId}`);
        if (!cardEl) return;
        cardEl.innerHTML = `
            <div class="card-header" style="display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #eee; margin-bottom: 10px; padding-bottom: 8px;">
                <h2 class="highlight" style="margin: 0; font-size: 1.1rem;">${coinId}</h2>
                <span id="status_${safeId}" style="font-size: 0.75rem; color: #888;">加载中...</span>
            </div>
            <div id="content_${safeId}" style="min-height: 160px; color: #aaa; font-size: 0.9rem; text-align: center; padding-top: 60px;">
                等待数据接入...
            </div>
            <div class="card-footer">
                <a href="/detail/${coinId}" class="card-detail-link">📊 查看详情 →</a>
                <a href="/multi-period/${coinId}" class="card-detail-link card-multi-link">🔍 多周期总览 →</a>
            </div>
        `;
    }

    // 颜色判断工具函数
    function getProfitColor(val) {
        return val >= 0 ? 'val-green' : 'val-red';
    }
    function getMacdColor(histogram) {
        return histogram >= 0 ? 'val-green' : 'val-red';
    }
    function getAdxColor(adx) {
        return adx > 25 ? 'val-green' : 'val-yellow';
    }
    function getMarketColor(state) {
        if (state.includes('上涨') || state.includes('增强')) return 'val-green';
        if (state.includes('下跌') || state.includes('过热')) return 'val-red';
        if (state.includes('震荡')) return 'val-yellow';
        return 'val-yellow';
    }
    function getSignalColor(action) {
        if (action === '上涨') return 'val-green';
        if (action === '下跌') return 'val-red';
        return 'val-yellow';
    }

    // 更新卡片内容
    function updateCardContent(coinId, coinData, isError = false, errMsg = '') {
        const safeId = coinId.replace(/-/g, '_');
        const statusEl = document.getElementById(`status_${safeId}`);
        const contentEl = document.getElementById(`content_${safeId}`);
        if (!statusEl || !contentEl) return;

        const nowTime = new Date().toLocaleTimeString();

        if (isError) {
            statusEl.innerHTML = `<span style="color: var(--danger-color);">失败 (${nowTime})</span>`;
        } else {
            statusEl.innerHTML = `<span style="color: var(--success-color);">更新于 ${nowTime}</span>`;
        }

        if (!coinData) {
            contentEl.innerHTML = `<div style="color: var(--danger-color); text-align: center;">加载失败: ${errMsg}</div>`;
            return;
        }

        const cardEl = document.getElementById(`card_${safeId}`);
        if (cardEl) {
            cardEl.style.background = '';
        }

        const macdColor = getMacdColor(coinData.indicators.macd.histogram);
        const adxColor = getAdxColor(coinData.indicators.adx.adx);
        const marketColor = getMarketColor(coinData.analysis.market_state);
        const signalColor = getSignalColor(coinData.analysis.action_signal);
        const profitColor = getProfitColor(coinData.trade_info.current_profit);

        let errorHtml = '';
        if (isError) {
            errorHtml = `<div class="card-error">本次获取超时，展示最新缓存 (${errMsg})</div>`;
        }

        contentEl.innerHTML = `
            <div style="font-size: 0.7rem; color: #999; margin-bottom: 8px; text-align: right;">策略时间: ${coinData.timestamp}</div>

            <div class="card-rows">
                <!-- 第1行：最新价格 -->
                <div class="card-row full">
                    <div class="data-row">
                        <span class="label">最新价格</span>
                        <span class="value price-value val-blue">$${coinData.price}</span>
                    </div>
                </div>

                <!-- 第2行：MACD柱 | DIF -->
                <div class="card-row cols-2">
                    <div class="data-row">
                        <span class="label">MACD柱</span>
                        <span class="value ${macdColor}">${coinData.indicators.macd.histogram}</span>
                    </div>
                    <div class="data-row">
                        <span class="label">DIF</span>
                        <span class="value ${macdColor}">${coinData.indicators.macd.dif}</span>
                    </div>
                </div>

                <!-- 第3行：市场状态 | 趋势方向 -->
                <div class="card-row cols-2">
                    <div class="data-row">
                        <span class="label">市场状态</span>
                        <span class="value ${marketColor}">${coinData.analysis.market_state}</span>
                    </div>
                    <div class="data-row">
                        <span class="label">趋势方向</span>
                        <span class="value ${signalColor}">${coinData.analysis.action_signal}</span>
                    </div>
                </div>

                <!-- 第4行：交易价格 | 当前盈亏 -->
                <div class="card-row cols-2">
                    <div class="data-row">
                        <span class="label">最后交易价</span>
                        <span class="value val-blue">$${coinData.trade_info.last_trade_price}</span>
                    </div>
                    <div class="data-row">
                        <span class="label">当前盈亏</span>
                        <span class="value ${profitColor}">${coinData.trade_info.current_profit >= 0 ? '+' : ''}${coinData.trade_info.current_profit}%</span>
                    </div>
                </div>

                <!-- 第5行：交易时间 | 持仓时长 -->
                <div class="card-row cols-2">
                    <div class="data-row">
                        <span class="label">交易时间</span>
                        <span class="value val-blue">${coinData.trade_info.trade_time}</span>
                    </div>
                    <div class="data-row">
                        <span class="label">持仓时长</span>
                        <span class="value val-blue">${coinData.trade_info.hold_time}</span>
                    </div>
                </div>
            </div>

            ${errorHtml}
        `;
    }

    // 异步获取单个币种
    async function fetchCoinData(coin) {
        try {
            const response = await fetch(`/api/strategy/coin/${coin}?bar=${CURRENT_BAR}`);
            const result = await response.json();
            if (result.code === 200) {
                const data = result.data;
                coinDataCache[coin] = data;
                updateCardContent(coin, data);
                if (data.analysis.modify_flag === 'rise') marketStats.up++;
                else if (data.analysis.modify_flag === 'fall') marketStats.down++;
                else marketStats.wait++;
            } else {
                handleFetchError(coin, result.message);
            }
        } catch (error) {
            handleFetchError(coin, '网络请求失败或超时');
        } finally {
            marketStats.loaded++;
            updateOverallMarket();
        }
    }

    function handleFetchError(coinId, errMsg) {
        if (coinDataCache[coinId]) {
            const cachedData = coinDataCache[coinId];
            updateCardContent(coinId, cachedData, true, errMsg);
            if (cachedData.analysis.modify_flag === 'rise') marketStats.up++;
            else if (cachedData.analysis.modify_flag === 'fall') marketStats.down++;
            else marketStats.wait++;
        } else {
            updateCardContent(coinId, null, true, errMsg);
        }
    }

    // 更新大盘状态
    function updateOverallMarket() {
        if (marketStats.loaded < marketStats.total) {
            overallEl.textContent = `加载中... (${marketStats.loaded}/${marketStats.total})`;
            overallEl.className = 'value signal-wait';
            return;
        }
        const total = marketStats.total;
        let text = '';
        if (marketStats.up > total * 0.6) {
            text = `大盘整体向上 (多头: ${marketStats.up}, 空头: ${marketStats.down}, 观望: ${marketStats.wait})`;
            overallEl.className = 'value signal-buy';
        } else if (marketStats.down > total * 0.6) {
            text = `大盘整体向下 (多头: ${marketStats.up}, 空头: ${marketStats.down}, 观望: ${marketStats.wait})`;
            overallEl.className = 'value signal-sell';
        } else {
            text = `大盘走势分化 / 震荡 (多头: ${marketStats.up}, 空头: ${marketStats.down}, 观望: ${marketStats.wait})`;
            overallEl.className = 'value signal-wait';
        }
        overallEl.textContent = text;
        refreshBtn.disabled = false;
        refreshBtn.textContent = '手动刷新数据';
    }

    // 触发全面刷新
    async function startRefresh() {
        refreshBtn.disabled = true;
        refreshBtn.textContent = '数据更新中...';

        marketStats = { up: 0, down: 0, wait: 0, total: SELECTED_COINS.length, loaded: 0 };
        updateOverallMarket();

        SELECTED_COINS.forEach(coin => {
            const safeId = coin.replace(/-/g, '_');
            const statusEl = document.getElementById(`status_${safeId}`);
            if (statusEl) {
                statusEl.innerHTML = '<span style="color: #888;">刷新中...</span>';
            }
        });

        for (const coin of SELECTED_COINS) {
            await fetchCoinData(coin);
            await new Promise(resolve => setTimeout(resolve, 300));
        }
    }

    refreshBtn.addEventListener('click', startRefresh);

    // 启动
    init();

    // 自动刷新定时器引用
    let autoRefreshTimer = null;
    let autoRefreshInterval = (function() {
        var saved = localStorage.getItem('autoRefreshInterval');
        if (saved !== null) {
            var parsed = parseInt(saved, 10);
            if (!isNaN(parsed) && parsed >= 0 && parsed <= 60) return parsed;
        }
        return 1;
    })();

    function startAutoRefresh() {
        if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
        if (autoRefreshInterval > 0) {
            autoRefreshTimer = setInterval(startRefresh, autoRefreshInterval * 60000);
        }
    }

    // 应用刷新间隔按钮
    var applyRefreshBtn = document.getElementById('apply-refresh-interval-btn');
    var refreshIntervalInput = document.getElementById('refresh-interval-input');
    var refreshStatusMsg = document.getElementById('refresh-status-msg');

    if (applyRefreshBtn) {
        applyRefreshBtn.addEventListener('click', function() {
            var val = parseInt(refreshIntervalInput.value, 10);
            if (isNaN(val) || val < 0) val = 0;
            if (val > 60) val = 60;
            autoRefreshInterval = val;
            localStorage.setItem('autoRefreshInterval', String(val));
            startAutoRefresh();
            if (val === 0) {
                refreshStatusMsg.textContent = '自动刷新已关闭';
                refreshStatusMsg.style.color = '#999';
            } else {
                refreshStatusMsg.textContent = '自动刷新已启用，每 ' + val + ' 分钟刷新一次';
                refreshStatusMsg.style.color = 'var(--success-color)';
            }
            refreshStatusMsg.style.display = 'block';
            setTimeout(function() { refreshStatusMsg.style.display = 'none'; }, 4000);
        });
    }

    // 启动自动刷新（默认1分钟）
    startAutoRefresh();

    // ================================================================
    // 批量多周期趋势分析 — 功能模块
    // ================================================================

    const batchStartBtn = document.getElementById('batch-start-btn');
    const batchProgress = document.getElementById('batch-progress');
    const progressBar = document.getElementById('progress-bar');
    const progressMsg = document.getElementById('progress-msg');
    const batchStats = document.getElementById('batch-stats');
    const tableLoading = document.getElementById('table-loading');
    const tableContainer = document.getElementById('table-container');
    const tableBody = document.getElementById('table-body');
    const tableEmpty = document.getElementById('table-empty');
    const tabFull = document.getElementById('tab-full');
    const tabFiltered = document.getElementById('tab-filtered');
    const badgeFull = document.getElementById('badge-full');
    const badgeFiltered = document.getElementById('badge-filtered');

    let currentView = 'full';  // 'full' or 'filtered'
    let pollingTimer = null;
    let isRunning = false;
    let allRecords = [];            // 完整数据缓存
    let filteredRecords = [];       // 筛选后数据
    let sortCol = null;             // 当前排序列名
    let sortDir = 'asc';            // 排序方向
    let filterActive = false;       // 是否有激活的筛选

    // ================================================================
    // 周期显示选择 + 简洁视图（统一列可见性控制）
    // ================================================================
    // 列可见性由两个维度共同决定：
    //   1) 周期选择：未选中的周期整列隐藏（data-period 标记）；
    //   2) 简洁视图 / 未全选周期：隐藏次要指标列（.indicator-col，即 DIF/ADX/ATR/SAR/时间），
    //      仅保留核心列（.core-col：趋势/交易价/盈亏%/MACD）。
    var ALL_PERIODS = ['15m', '1H', '4H', '1D'];
    var DEFAULT_PERIODS = ['15m', '4H'];
    var PERIOD_LABELS = { '15m': '15分钟', '1H': '1小时', '4H': '4小时', '1D': '1天' };
    var selectedPeriods = (function() {
        try {
            var saved = JSON.parse(localStorage.getItem('batchSelectedPeriods') || 'null');
            if (Object.prototype.toString.call(saved) === '[object Array]' && saved.length) {
                var valid = ALL_PERIODS.filter(function(p) { return saved.indexOf(p) >= 0; });
                if (valid.length) return valid;
            }
        } catch (e) {}
        return DEFAULT_PERIODS.slice();
    })();
    var simpleView = (function() {
        var saved = localStorage.getItem('batchSimpleView');
        return saved === null ? false : saved === '1';
    })();

    function savePeriodPrefs() {
        try {
            localStorage.setItem('batchSelectedPeriods', JSON.stringify(selectedPeriods));
            localStorage.setItem('batchSimpleView', simpleView ? '1' : '0');
        } catch (e) {}
    }

    // 统一应用列可见性（表头 th + 数据 td），re-render 后需重新调用
    function applyColumnVisibility() {
        var allSelected = selectedPeriods.length >= ALL_PERIODS.length;
        var hideSecondary = simpleView || !allSelected;
        var sel = {};
        selectedPeriods.forEach(function(p) { sel[p] = true; });
        document.querySelectorAll('#data-table th, #data-table td').forEach(function(cell) {
            var period = cell.getAttribute('data-period');
            var show = true;
            if (period && !sel[period]) show = false;
            if (show && hideSecondary && cell.classList.contains('indicator-col')) show = false;
            cell.style.display = show ? '' : 'none';
        });
        updatePeriodHint(hideSecondary);
    }

    function updatePeriodHint(hideSecondary) {
        var hint = document.getElementById('period-selector-hint');
        if (!hint) return;
        var names = selectedPeriods.map(function(p) { return PERIOD_LABELS[p] || p; });
        hint.textContent = '当前: ' + (names.length ? names.join(' + ') : '（未选周期）') +
            (hideSecondary ? ' · 仅核心列' : ' · 全部指标列');
    }

    function renderPeriodSelector() {
        var sel = {};
        selectedPeriods.forEach(function(p) { sel[p] = true; });
        document.querySelectorAll('#period-toggles .period-toggle').forEach(function(btn) {
            btn.classList.toggle('active', !!sel[btn.getAttribute('data-period')]);
        });
        var allBtn = document.getElementById('period-select-all');
        if (allBtn) allBtn.classList.toggle('active', selectedPeriods.length >= ALL_PERIODS.length);
    }

    // ---- 趋势样式 ----
    function trendClass(trend) {
        if (trend === '上涨') return 'td-up';
        if (trend === '下跌') return 'td-down';
        return 'td-wait';
    }

    function profitClass(val) {
        var n = parseFloat(val);
        if (isNaN(n)) return '';
        return n > 0 ? 'td-profit-positive' : n < 0 ? 'td-profit-negative' : '';
    }

    function fmtCell(val, defaultVal) {
        if (val === undefined || val === null || val === '') return defaultVal || '--';
        if (typeof val === 'number') return val.toFixed(4);
        return val;
    }

    // ---- 颜色辅助 ----

    function macdColor(val) {
        var n = parseFloat(val);
        if (isNaN(n)) return '';
        return n >= 0 ? 'td-macd-positive' : 'td-macd-negative';
    }

    function adxClass(val) {
        var n = parseFloat(val);
        if (isNaN(n)) return '';
        if (n >= 30) return 'td-adx-strong';
        if (n >= 20) return 'td-adx-moderate';
        return 'td-adx-weak';
    }

    // SAR 颜色：优先用后端按“SAR vs 当前收盘价”算好的颜色（green/red），
    // 后端缺省时回退到前端按“SAR vs 交易价”估算
    function sarColorClass(sarVal, tradePrice, backendColor) {
        if (backendColor === 'green') return 'td-sar-green';
        if (backendColor === 'red') return 'td-sar-red';
        var s = parseFloat(sarVal);
        var p = parseFloat(tradePrice);
        if (isNaN(s) || isNaN(p) || s === 0 || p === 0) return '';
        return s < p ? 'td-sar-green' : 'td-sar-red';
    }

    // ---- 单元格构造：单个周期的 8 列（趋势/交易价/盈亏%/MACD 为核心，DIF/ADX/ATR/SAR 为次要）----
    function buildPeriodCells(r, bar) {
        var trend = r[bar + '_趋势'];
        var price = r[bar + '_交易价格'];
        var profit = r[bar + '_盈亏%'];
        var macd = r['MACD_' + bar];
        var dif = r['DIF_' + bar];
        var adx = r['ADX_' + bar];
        var atr = r['ATR_' + bar];
        var sar = r['SAR_' + bar];
        var sarColor = r['SAR颜色_' + bar];
        var dp = ' data-period="' + bar + '"';
        return '<td class="core-col ' + trendClass(trend) + '"' + dp + '>' + fmtCell(trend) + '</td>' +
            '<td class="core-col"' + dp + '>' + fmtCell(price) + '</td>' +
            '<td class="core-col ' + profitClass(profit) + '"' + dp + '>' + fmtCell(profit) + '</td>' +
            '<td class="core-col macd-col ' + macdColor(macd) + '"' + dp + '>' + fmtCell(macd) + '</td>' +
            '<td class="indicator-col ' + macdColor(dif) + '"' + dp + '>' + fmtCell(dif) + '</td>' +
            '<td class="indicator-col ' + adxClass(adx) + '"' + dp + '>' + fmtCell(adx) + '</td>' +
            '<td class="indicator-col"' + dp + '>' + fmtCell(atr) + '</td>' +
            '<td class="indicator-col ' + sarColorClass(sar, price, sarColor) + '"' + dp + '>' + fmtCell(sar) + '</td>';
    }

    // ---- 单元格构造：单个周期的交易时间列（次要，统一排在表尾）----
    function buildTimeCell(r, bar) {
        return '<td class="indicator-col" data-period="' + bar + '" style="font-size:0.75rem;color:#888;">' +
            fmtCell(r[bar + '_交易时间']) + '</td>';
    }

    // ---- 渲染表格 ----
    function renderTable(records) {
        var displayRecords = filterActive ? filteredRecords : records;
    
        if (!displayRecords || displayRecords.length === 0) {
            tableContainer.style.display = 'none';
            tableEmpty.style.display = 'block';
            tableEmpty.textContent = filterActive
                ? '没有符合条件的币种（已激活筛选）'
                : (currentView === 'filtered'
                    ? '暂无满足双周期一致性条件的币种'
                    : '暂无数据，请先点击“批量更新趋势数据”按钮');
            return;
        }
    
        tableEmpty.style.display = 'none';
        tableContainer.style.display = 'block';
    
        tableBody.innerHTML = displayRecords.map(function(r) {
            var instId = r['交易对'] || '';
            return '<tr data-inst-id="' + instId + '">' +
                '<td class="cb-col"><input type="checkbox" class="row-check" data-inst="' + instId + '"></td>' +
                '<td>' + fmtCell(r['排名'], '') + '</td>' +
                '<td><strong>' + fmtCell(r['币种'], '') + '</strong></td>' +
                '<td style="font-size:0.75rem;color:#888;">' + fmtCell(r['交易对'], '') + '</td>' +
                '<td>' + fmtCell(r['名称'], '') + '</td>' +
                // 各周期 8 列（趋势/交易价/盈亏%/MACD/DIF/ADX/ATR/SAR），顺序与 thead 一致
                buildPeriodCells(r, '15m') +
                buildPeriodCells(r, '1H') +
                buildPeriodCells(r, '4H') +
                buildPeriodCells(r, '1D') +
                // 交易时间列（次要，统一排在表尾）
                buildTimeCell(r, '15m') +
                buildTimeCell(r, '1H') +
                buildTimeCell(r, '4H') +
                buildTimeCell(r, '1D') +
            '</tr>';
        }).join('');

        // 更新筛选统计
        updateFilterUI(records.length);
        // 重新渲染会清除单元格内联 display，需按当前周期选择/简洁视图重新应用列可见性
        applyColumnVisibility();
        tableContainer.scrollTop = 0;
    }

    // ---- 排序功能 ----
    function doSort(colName) {
        if (sortCol === colName) {
            sortDir = sortDir === 'asc' ? 'desc' : 'asc';
        } else {
            sortCol = colName;
            sortDir = 'asc';
        }

        var records = filterActive ? filteredRecords : allRecords;

        records.sort(function(a, b) {
            var va = a[colName] || '';
            var vb = b[colName] || '';
            var na = parseFloat(va);
            var nb = parseFloat(vb);

            // 数值列
            if (!isNaN(na) && !isNaN(nb) && va !== '' && vb !== '') {
                return sortDir === 'asc' ? na - nb : nb - na;
            }
            // 将空值排到最后
            if (va === '' || va === '--') return 1;
            if (vb === '' || vb === '--') return -1;
            // 文本列
            var cmp = va.localeCompare(vb, 'zh-CN');
            return sortDir === 'asc' ? cmp : -cmp;
        });

        if (filterActive) {
            filteredRecords = records;
        } else {
            allRecords = records;
        }

        updateSortUI();
        renderTable(records);
    }

    function updateSortUI() {
        document.querySelectorAll('#data-table th').forEach(function(th) {
            var col = th.getAttribute('data-col');
            th.classList.remove('sort-asc', 'sort-desc');
            if (col === sortCol) {
                th.classList.add(sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
            }
        });
    }

    // ---- 筛选功能 ----
    function applyFilters() {
        var periodFilter = document.getElementById('filter-period').value;
        var macdMin = document.getElementById('filter-macd-min').value;
        var macdMax = document.getElementById('filter-macd-max').value;
        var macdPeriod = document.getElementById('filter-macd-period').value;
        var adxMin = document.getElementById('filter-adx-min').value;
        var adxMax = document.getElementById('filter-adx-max').value;
        var adxPeriod = document.getElementById('filter-adx-period').value;

        filterActive = !!(periodFilter || macdMin || macdMax || adxMin || adxMax);

        if (!filterActive) {
            filteredRecords = [];
            document.getElementById('filter-stats').style.display = 'none';
            sortCol = null;
            sortDir = 'asc';
            updateSortUI();
            renderTable(allRecords);
            return;
        }

        filteredRecords = allRecords.filter(function(r) {
            // 周期趋势筛选
            if (periodFilter) {
                var parts = periodFilter.split('_');
                var p = parts[0];
                var t = parts[1];
                var colName = p + '_趋势';
                if (r[colName] !== t) return false;
            }

            // MACD柱范围筛选
            if (macdMin || macdMax) {
                var macdCol = 'MACD_' + macdPeriod;
                var macdVal = parseFloat(r[macdCol]);
                if (isNaN(macdVal)) return false;
                if (macdMin && macdVal < parseFloat(macdMin)) return false;
                if (macdMax && macdVal > parseFloat(macdMax)) return false;
            }

            // ADX范围筛选
            if (adxMin || adxMax) {
                var adxCol = 'ADX_' + adxPeriod;
                var adxVal = parseFloat(r[adxCol]);
                if (isNaN(adxVal)) return false;
                if (adxMin && adxVal < parseFloat(adxMin)) return false;
                if (adxMax && adxVal > parseFloat(adxMax)) return false;
            }

            return true;
        });

        document.getElementById('filter-stats').style.display = 'flex';
        sortCol = null;
        sortDir = 'asc';
        updateSortUI();
        renderTable(allRecords);
    }

    function clearFilters() {
        document.getElementById('filter-period').value = '';
        document.getElementById('filter-macd-min').value = '';
        document.getElementById('filter-macd-max').value = '';
        document.getElementById('filter-macd-period').value = '4H';
        document.getElementById('filter-adx-min').value = '';
        document.getElementById('filter-adx-max').value = '';
        document.getElementById('filter-adx-period').value = '4H';
        filterActive = false;
        filteredRecords = [];
        document.getElementById('filter-stats').style.display = 'none';
        sortCol = null;
        sortDir = 'asc';
        updateSortUI();
        renderTable(allRecords);
    }

    function updateFilterUI(totalCount) {
        if (filterActive) {
            document.getElementById('filter-stats').style.display = 'flex';
            document.getElementById('filter-count').textContent = filteredRecords.length;
            document.getElementById('filter-total').textContent = totalCount;
        }
    }

    // ---- 加载表格数据 ----
    async function loadTableData(view) {
        tableLoading.style.display = 'block';
        tableContainer.style.display = 'none';
        tableEmpty.style.display = 'none';

        try {
            var url = view === 'filtered'
                ? '/api/batch/csv-filtered'
                : '/api/batch/csv-data';
            var resp = await fetch(url);
            var result = await resp.json();

            if (result.code === 200 && result.data) {
                var records = result.data.records || [];
                var total = result.data.total || records.length;

                // 缓存完整数据（仅完整视图）
                if (view === 'full') {
                    allRecords = records;
                    document.getElementById('filter-panel').style.display = 'block';
                } else {
                    document.getElementById('filter-panel').style.display = 'none';
                }

                // 重置排序和筛选
                sortCol = null;
                sortDir = 'asc';
                filterActive = false;
                filteredRecords = [];
                document.getElementById('filter-stats').style.display = 'none';
                updateSortUI();

                renderTable(records);

                // 更新 badge
                badgeFull.textContent = view === 'full' ? total : '...';
                badgeFiltered.textContent = view === 'filtered' ? total : '...';

                // 更新统计（仅完整视图返回统计信息）
                if (view === 'full' && result.data.statistics) {
                    var stats = result.data.statistics;
                    document.getElementById('stat-up15m').textContent = stats.up_count_15m || 0;
                    document.getElementById('stat-down15m').textContent = stats.down_count_15m || 0;
                    document.getElementById('stat-up1h').textContent = stats.up_count_1h || 0;
                    document.getElementById('stat-down1h').textContent = stats.down_count_1h || 0;
                    document.getElementById('stat-up4h').textContent = stats.up_count_4h || 0;
                    document.getElementById('stat-down4h').textContent = stats.down_count_4h || 0;
                    document.getElementById('stat-up1d').textContent = stats.up_count_1d || 0;
                    document.getElementById('stat-down1d').textContent = stats.down_count_1d || 0;
                    document.getElementById('stat-wait').textContent = stats.wait_count || 0;
                    document.getElementById('stat-consistent').textContent = stats.consistent_count || 0;
                    badgeFiltered.textContent = stats.consistent_count || 0;
                    batchStats.style.display = 'flex';
                }
            } else {
                renderTable([]);
            }
        } catch (e) {
            console.error('加载表格数据失败:', e);
            tableLoading.textContent = '数据加载失败: ' + e.message;
            tableContainer.style.display = 'none';
        }
        tableLoading.style.display = 'none';
    }

    // ---- 视图切换 ----
    function switchView(view) {
        currentView = view;
        tabFull.classList.toggle('active', view === 'full');
        tabFiltered.classList.toggle('active', view === 'filtered');
        loadTableData(view);
    }

    tabFull.addEventListener('click', function() { switchView('full'); });
    tabFiltered.addEventListener('click', function() { switchView('filtered'); });

    // ---- 表格复选框：全选/取消全选 ----
    var tableCheckAll = document.getElementById('table-check-all');
    if (tableCheckAll) {
        tableCheckAll.addEventListener('change', function() {
            var checked = tableCheckAll.checked;
            tableBody.querySelectorAll('.row-check').forEach(function(cb) {
                cb.checked = checked;
            });
        });
    }

    // ---- 勾选币种批量设为星标 ----
    var batchStarCheckedBtn = document.getElementById('batch-star-checked-btn');
    if (batchStarCheckedBtn) {
        batchStarCheckedBtn.addEventListener('click', async function() {
            // 1. 收集勾选的币种
            var checkedInstIds = [];
            var checkboxes = tableBody.querySelectorAll('input.row-check:checked');
            console.log('[勾选星标] 找到复选框数量:', checkboxes.length);
            checkboxes.forEach(function(cb) {
                var inst = cb.getAttribute('data-inst');
                console.log('[勾选星标] checkbox:', cb, 'data-inst:', inst);
                if (inst) checkedInstIds.push(inst);
            });
            console.log('[勾选星标] 已勾选币种:', checkedInstIds.length, checkedInstIds);

            if (checkedInstIds.length === 0) {
                MDialog.alert({ message: '请先在表格中勾选要设为星标的币种。', type: 'warning' });
                return;
            }

            // 2. 共享的执行逻辑
            var coinNames = checkedInstIds.map(function(id){ return id.replace('-USDT-SWAP',''); }).join(', ');

            async function executeStarAction(mode) {
                // mode: 'reset' = 重置并添加, 'append' = 增量添加
                var isReset = (mode === 'reset');
                console.log('[勾选星标] 模式:', isReset ? '重置并添加' : '增量添加');

                try {
                    // 3. 处理 STARRED_COINS
                    if (isReset) {
                        STARRED_COINS = checkedInstIds.slice();
                    } else {
                        checkedInstIds.forEach(function(inst) {
                            if (STARRED_COINS.indexOf(inst) === -1) STARRED_COINS.push(inst);
                        });
                    }
                    console.log('[勾选星标] STARRED_COINS:', STARRED_COINS);

                    // 4. 保存星标到后端
                    var res = await fetch('/api/strategy/starred', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ starred_coins: STARRED_COINS })
                    });
                    var result = await res.json();
                    console.log('[勾选星标] 星标API响应:', result);
                    if (result.code !== 200) {
                        MDialog.alert({ message: '设置星标失败: ' + result.message, type: 'danger' });
                        return;
                    }

                    // 5. 更新界面星标状态
                    updateAllStarIcons();

                    // 6. 处理 SELECTED_COINS
                    var addedCount = 0;
                    if (isReset) {
                        SELECTED_COINS = checkedInstIds.slice();
                        addedCount = SELECTED_COINS.length;
                    } else {
                        checkedInstIds.forEach(function(inst) {
                            if (SELECTED_COINS.indexOf(inst) === -1) {
                                SELECTED_COINS.push(inst);
                                addedCount++;
                            }
                        });
                    }
                    console.log('[勾选星标] SELECTED_COINS 新增:', addedCount, '个，总数:', SELECTED_COINS.length);

                    // 7. 更新币种选择面板激活状态
                    if (isReset) {
                        // 重置模式：先取消所有激活，再激活选中的
                        coinSelectorEl.querySelectorAll('.coin-tag').forEach(function(tag) {
                            tag.classList.remove('active');
                        });
                    }
                    coinSelectorEl.querySelectorAll('.coin-tag').forEach(function(tag) {
                        if (checkedInstIds.indexOf(tag.dataset.coin) >= 0) {
                            tag.classList.add('active');
                        }
                    });

                    // 8. 保存 SELECTED_COINS 到后端
                    try {
                        var res2 = await fetch('/api/strategy/config', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ selected_coins: SELECTED_COINS })
                        });
                        var result2 = await res2.json();
                        console.log('[勾选星标] 保存SELECTED响应:', result2);
                    } catch (e2) {
                        console.warn('[勾选星标] 保存SELECTED失败:', e2);
                    }

                    // 9. 刷新监控台卡片
                    coinDataCache = {};
                    initCards();
                    startRefresh();

                    var modeText = isReset ? '重置并设置' : '增量添加';
                    var msg = '✅ ' + modeText + '完成！已将 ' + checkedInstIds.length + ' 个币种设为星标。';
                    if (addedCount > 0) msg += '\n' + addedCount + ' 个币种新增到监控卡片。';
                    MDialog.alert({ message: msg, type: 'success' });

                } catch (e) {
                    console.error('[勾选星标] 请求失败:', e);
                    MDialog.alert({ message: '设置失败: 网络错误 ' + e.message, type: 'danger' });
                }
            }

            // 弹出选择弹窗：OK 按钮进入模式选择，取消仅关闭
            MDialog.show({
                title: '⭐ 勾选星标',
                type: 'confirm',
                message: '<div style="margin-bottom:8px;">已勾选 <strong>' + checkedInstIds.length + '</strong> 个币种：</div>' +
                    '<div style="color:#666;font-size:0.85rem;margin-bottom:12px;word-break:break-all;">' + coinNames + '</div>' +
                    '<div style="border-top:1px dashed #ddd;padding-top:10px;font-size:0.85rem;color:#888;">' +
                    '点击“下一步”选择操作模式：<br>' +
                    '• <b>重置并添加</b>：清除所有星标和选中，仅保留勾选的<br>' +
                    '• <b>增量添加</b>：在现有基础上追加勾选的币种' +
                    '</div>',
                showCancel: true,
                okText: '下一步 ▶',
                okType: 'primary',
                cancelText: '取消',
                cancelType: 'secondary',
                onOk: function() {
                    // 二次弹窗：让用户选择模式
                    MDialog.show({
                        title: '⭐ 选择操作模式',
                        type: 'confirm',
                        message: '<div style="font-size:0.9rem;line-height:1.8;">' +
                            '<div style="padding:8px 12px;background:#fff3cd;border-radius:6px;margin-bottom:10px;">' +
                            '🔄 <b>重置并添加</b><br><span style="font-size:0.82rem;color:#856404;">清除所有星标和选中状态，仅将勾选的 ' + checkedInstIds.length + ' 个币种设为星标并添加到监控</span></div>' +
                            '<div style="padding:8px 12px;background:#d4edda;border-radius:6px;">' +
                            '➕ <b>增量添加</b><br><span style="font-size:0.82rem;color:#155724;">在现有基础上追加勾选的 ' + checkedInstIds.length + ' 个币种到星标和监控列表</span></div>' +
                            '</div>',
                        showCancel: true,
                        okText: '🔄 重置并添加',
                        okType: 'warning',
                        cancelText: '➕ 增量添加',
                        cancelType: 'success',
                        onOk: function() {
                            executeStarAction('reset');
                            return false; // 阻止自动关闭，由 executeStarAction 内部控制
                        },
                        onCancel: function() {
                            executeStarAction('append');
                        }
                    });
                    return false; // 阻止一级弹窗自动关闭
                }
            });
        });
    }

    // ---- 进度轮询 ----
    let pollFailCount = 0;  // 连续轮询失败次数（网络抖动不中断，连续失败才停止）

    async function pollProgress() {
        try {
            var resp = await fetch('/api/batch/progress');
            var result = await resp.json();
            if (result.code === 200 && result.data) {
                pollFailCount = 0;
                var p = result.data;
                updateProgressUI(p);

                if (p.status === 'running') {
                    pollingTimer = setTimeout(pollProgress, 1500);
                } else {
                    // 完成或出错 → 停止轮询
                    stopPolling();
                    batchStartBtn.disabled = false;
                    batchStartBtn.textContent = '批量更新趋势数据';
                    isRunning = false;

                    // 自动加载最新数据
                    if (p.status === 'completed') {
                        loadTableData(currentView);
                        document.getElementById('stat-update-time').textContent =
                            '更新于: ' + new Date().toLocaleTimeString();
                    }
                }
            }
        } catch (e) {
            // 网络瞬时抖动不清除轮询（后台任务仍在运行），连续失败才停止，避免进度条假死
            console.warn('轮询进度失败:', e);
            pollFailCount++;
            if (pollFailCount >= 5) {
                stopPolling();
                batchStartBtn.disabled = false;
                batchStartBtn.textContent = '批量更新趋势数据';
                isRunning = false;
            } else {
                pollingTimer = setTimeout(pollProgress, 1500);
            }
        }
    }

    function stopPolling() {
        if (pollingTimer) {
            clearTimeout(pollingTimer);
            pollingTimer = null;
        }
    }

    function updateProgressUI(p) {
        batchProgress.classList.add('active');
        var pct = p.progress_pct || 0;
        progressBar.style.width = pct + '%';
        progressBar.textContent = pct >= 5 ? pct + '%' : '';

        if (p.status === 'running') {
            // 显示已耗时，让用户确认任务仍在执行中
            var elapsed = p.elapsed_seconds
                ? '（已耗时 ' + Math.floor(p.elapsed_seconds) + ' 秒）'
                : '';
            progressMsg.textContent = (p.message || ('处理中: ' + p.current + '/' + p.total)) + elapsed;
        } else if (p.status === 'completed') {
            progressMsg.textContent = p.message || '分析完成!';
            progressBar.style.background = 'linear-gradient(90deg, #5cb85c, #5cb85c)';
        } else if (p.status === 'error') {
            progressMsg.textContent = '错误: ' + (p.message || '未知错误');
            progressBar.style.background = '#e74c3c';
        }
    }

    // ---- 启动批量更新 ----
    batchStartBtn.addEventListener('click', async function() {
        if (isRunning) return;

        // 动态获取币种总数（CSV 行数），避免弹窗写死
        var totalCoins = 27;
        try {
            var pr = await fetch('/api/batch/progress');
            var prj = await pr.json();
            if (prj.code === 200 && prj.data && prj.data.total) totalCoins = prj.data.total;
        } catch (e) {}

        // 确认
        MDialog.confirm({
            title: '批量分析确认',
            message: '即将对全部 ' + totalCoins + ' 个加密货币进行 15m + 1H + 4H + 1D 多周期 Pro3 策略分析，预计需要较长时间。<br><br>确定继续？',
            type: 'info',
            onOk: async function() {
                isRunning = true;
                batchStartBtn.disabled = true;
                batchStartBtn.textContent = '启动中...';
                pollFailCount = 0;

                // 重置进度 UI
                batchProgress.classList.add('active');
                progressBar.style.width = '0%';
                progressBar.style.background = 'linear-gradient(90deg, #4a90d9, #5cb85c)';
                progressBar.textContent = '';
                progressMsg.textContent = '正在启动批量分析...';

                try {
                    var resp = await fetch('/api/batch/update', { method: 'POST' });
                    var result = await resp.json();

                    if (result.code === 200) {
                        progressMsg.textContent = '批量分析已启动，正在获取数据...';
                        // 开始轮询进度
                        pollProgress();
                    } else {
                        MDialog.alert({ message: '启动失败: ' + (result.message || '未知错误'), type: 'danger' });
                        isRunning = false;
                        batchStartBtn.disabled = false;
                        batchStartBtn.textContent = '批量更新趋势数据';
                        batchProgress.classList.remove('active');
                    }
                } catch (e) {
                    MDialog.alert({ message: '启动失败: ' + e.message, type: 'danger' });
                    isRunning = false;
                    batchStartBtn.disabled = false;
                    batchStartBtn.textContent = '批量更新趋势数据';
                    batchProgress.classList.remove('active');
                }
            }
        });
    });

    // ---- 初始加载表格数据 ----
    loadTableData('full');

    // ---- 排序：表头点击事件 ----
    document.getElementById('data-table').addEventListener('click', function(e) {
        // 忽略复选框点击
        if (e.target.type === 'checkbox') return;
        var th = e.target.closest('th');
        if (!th) return;
        var col = th.getAttribute('data-col');
        if (!col || col === '_cb') return;
        doSort(col);
    });

    // ---- 筛选：按钮事件 ----
    document.getElementById('filter-apply-btn').addEventListener('click', applyFilters);
    document.getElementById('filter-clear-btn').addEventListener('click', clearFilters);

    // ---- 检查是否已有任务在运行 ----
    (async function checkRunning() {
        try {
            var resp = await fetch('/api/batch/progress');
            var result = await resp.json();
            if (result.code === 200 && result.data && result.data.status === 'running') {
                isRunning = true;
                batchStartBtn.disabled = true;
                batchStartBtn.textContent = '更新中...';
                updateProgressUI(result.data);
                pollProgress();
            }
        } catch(e) {}
    })();

    // ---- 全屏展开/收缩 ----
    var isFullscreen = false;
    var fullscreenBtn = document.getElementById('fullscreen-btn');
    var batchPanel = document.getElementById('batch-panel');

    fullscreenBtn.addEventListener('click', function() {
        isFullscreen = !isFullscreen;
        if (isFullscreen) {
            batchPanel.classList.add('batch-fullscreen');
            fullscreenBtn.textContent = '⛶ 退出全屏';
            fullscreenBtn.classList.add('active');
        } else {
            batchPanel.classList.remove('batch-fullscreen');
            fullscreenBtn.textContent = '⛶ 全屏';
            fullscreenBtn.classList.remove('active');
        }
    });

    // ---- 简洁视图 / 详细视图切换（与周期选择统一由 applyColumnVisibility 控制列显隐）----
    var collapseBtn = document.getElementById('collapse-indicators-btn');

    function syncCollapseBtn() {
        if (!collapseBtn) return;
        // 按钮文案表示“点击后切换到的目标视图”：当前简洁→提示详细，当前详细→提示简洁
        collapseBtn.textContent = simpleView ? '📊 详细视图' : '📋 简洁视图';
        collapseBtn.classList.toggle('active', simpleView);
    }

    if (collapseBtn) {
        collapseBtn.addEventListener('click', function() {
            simpleView = !simpleView;
            savePeriodPrefs();
            syncCollapseBtn();
            applyColumnVisibility();
        });
    }

    // ---- 周期选择：单个周期切换 ----
    document.querySelectorAll('#period-toggles .period-toggle').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var p = btn.getAttribute('data-period');
            if (selectedPeriods.indexOf(p) >= 0) {
                selectedPeriods = selectedPeriods.filter(function(x) { return x !== p; });
            } else {
                // 按 ALL_PERIODS 顺序重建，保证展示顺序稳定（15m→1H→4H→1D）
                selectedPeriods = ALL_PERIODS.filter(function(x) {
                    return x === p || selectedPeriods.indexOf(x) >= 0;
                });
            }
            savePeriodPrefs();
            renderPeriodSelector();
            applyColumnVisibility();
        });
    });

    // ---- 周期选择：全选 / 恢复默认 ----
    var periodSelectAllBtn = document.getElementById('period-select-all');
    if (periodSelectAllBtn) {
        periodSelectAllBtn.addEventListener('click', function() {
            selectedPeriods = (selectedPeriods.length >= ALL_PERIODS.length)
                ? DEFAULT_PERIODS.slice()
                : ALL_PERIODS.slice();
            savePeriodPrefs();
            renderPeriodSelector();
            applyColumnVisibility();
        });
    }

    // ---- 初始化：渲染周期选择器状态 + 同步按钮文案 + 应用列可见性（表头）----
    renderPeriodSelector();
    syncCollapseBtn();
    applyColumnVisibility();
});

/* ================================================================
   现代化按钮组件 JS 工具 (MButton)
   ================================================================ */
var MButton = (function() {
    /**
     * 将普通按钮升级为 m-btn 组件
     * @param {HTMLElement} btn - 目标按钮元素
     * @param {Object} opts - 配置项
     * @param {string} opts.type - 按钮类型: primary|success|danger|warning|info|secondary|dark
     * @param {string} opts.size - 尺寸: xs|sm|lg
     * @param {string} opts.variant - 变体: solid(默认)|outline|ghost
     * @param {boolean} opts.pill - 是否为药丸圆角
     * @param {boolean} opts.block - 是否全宽
     * @param {string} opts.icon - 图标文字（放在按钮内容前）
     * @param {string} opts.loadingIcon - 加载中的图标，默认 '⟳'
     */
    function init(btn, opts) {
        opts = opts || {};
        // 添加基础类
        btn.classList.add('m-btn');
        if (opts.type) btn.classList.add('m-btn-' + opts.type);
        if (opts.size) btn.classList.add('m-btn-' + opts.size);
        if (opts.variant === 'outline') btn.classList.add('m-btn-outline');
        if (opts.variant === 'ghost') btn.classList.add('m-btn-ghost');
        if (opts.pill) btn.classList.add('m-btn-pill');
        if (opts.block) btn.classList.add('m-btn-block');
        // 处理图标
        if (opts.icon) {
            var iconEl = document.createElement('span');
            iconEl.className = 'm-btn-icon';
            iconEl.textContent = opts.icon;
            btn.insertBefore(iconEl, btn.firstChild);
        }
        return btn;
    }

    /**
     * 设置按钮加载状态
     * @param {HTMLElement} btn - 按钮元素
     * @param {boolean} loading - 是否加载中
     * @param {string} loadingText - 加载中显示的文字
     */
    function setLoading(btn, loading, loadingText) {
        if (loading) {
            btn.dataset.originalHtml = btn.innerHTML;
            btn.classList.add('m-btn-loading');
            var icon = btn.querySelector('.m-btn-icon');
            if (icon) {
                icon.textContent = '⟳';
            } else {
                var iconEl = document.createElement('span');
                iconEl.className = 'm-btn-icon';
                iconEl.textContent = '⟳';
                btn.insertBefore(iconEl, btn.firstChild);
            }
            if (loadingText) {
                var textNodes = Array.from(btn.childNodes).filter(function(n) {
                    return n.nodeType === 3 && n.textContent.trim();
                });
                if (textNodes.length > 0) {
                    btn.dataset.originalText = textNodes[0].textContent;
                    textNodes[0].textContent = ' ' + loadingText;
                }
            }
        } else {
            btn.classList.remove('m-btn-loading');
            if (btn.dataset.originalHtml) {
                btn.innerHTML = btn.dataset.originalHtml;
                delete btn.dataset.originalHtml;
                delete btn.dataset.originalText;
            }
        }
    }

    /**
     * 创建新的 m-btn 按钮
     * @param {Object} opts - 配置项（同 init 参数）加上:
     * @param {string} opts.text - 按钮文字
     * @param {Function} opts.onClick - 点击回调
     * @returns {HTMLElement}
     */
    function create(opts) {
        opts = opts || {};
        var btn = document.createElement('button');
        btn.type = opts.type === 'submit' ? 'submit' : 'button';
        btn.textContent = opts.text || '';
        if (opts.onClick) btn.addEventListener('click', opts.onClick);
        if (opts.disabled) btn.disabled = true;
        if (opts.id) btn.id = opts.id;
        if (opts.className) btn.className += ' ' + opts.className;
        init(btn, opts);
        return btn;
    }

    /**
     * 自动将页面中带有 data-m-btn 属性的按钮升级
     * 用法: <button data-m-btn="primary" data-m-size="sm" data-m-icon="🔄">刷新</button>
     */
    function autoInit() {
        document.querySelectorAll('[data-m-btn]').forEach(function(btn) {
            if (btn.classList.contains('m-btn')) return;
            init(btn, {
                type: btn.dataset.mBtn,
                size: btn.dataset.mSize,
                variant: btn.dataset.mVariant,
                pill: btn.dataset.mPill === 'true',
                block: btn.dataset.mBlock === 'true',
                icon: btn.dataset.mIcon
            });
        });
    }

    return {
        init: init,
        setLoading: setLoading,
        create: create,
        autoInit: autoInit
    };
})();

// 页面加载后自动初始化 data-m-btn 按钮
document.addEventListener('DOMContentLoaded', function() {
    MButton.autoInit();
});
