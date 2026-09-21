#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时任务调度器
==============
基于 APScheduler 的基础调度框架，支持：
- 间隔任务 (IntervalTrigger)
- 定时任务 (CronTrigger)
- 一次性延迟任务 (DateTrigger)

使用方式：
    from .scheduler import task_scheduler

    # 注册任务
    task_scheduler.register_job(func, trigger='interval', minutes=5, id='my_job')

    # 启动调度器
    task_scheduler.start()
"""

import logging
import datetime
import os
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger(__name__)

# 实盘「期望运行状态」记账层（kv_store）。导不到时只影响「重启自动拉起」
# 这一个能力（退化成改造前的行为：重启后必须人工点启动），不会动摇启停主流程；
# 但必须留痕，不能默默失效。
try:
    from .. import trading_runtime_repo as _runtime_repo
except Exception as _rt_err:  # pragma: no cover - 仅包外独立运行时报
    _runtime_repo = None
    logging.getLogger(__name__).warning(
        f'[TaskScheduler] trading_runtime_repo 不可用，重启自动拉起功能关闭: {_rt_err}')

# 启动归因与自愈策略（为什么重启 / 该不该现在起 / 要等多久）。同样只影响
# 「重启自愈」这一个能力，取不到时退化成“不自动拉起”，不会拖垮调度器启动。
try:
    from .. import process_lifecycle as _lifecycle
except Exception as _lc_err:  # pragma: no cover - 包外裸模块身份时的兼容分支
    try:
        import process_lifecycle as _lifecycle
    except Exception:
        _lifecycle = None
        logging.getLogger(__name__).warning(
            f'[TaskScheduler] process_lifecycle 不可用，重启自愈按保守处理: {_lc_err}')


class TaskScheduler:
    """定时任务调度管理器"""

    def __init__(self):
        self._scheduler = BackgroundScheduler(
            job_defaults={
                'coalesce': True,        # 合并错过的执行
                'max_instances': 1,      # 同一任务最多1个实例
                'misfire_grace_time': 60  # 错过执行容忍60秒
            }
        )
        self._started = False
        self._job_registry = {}  # 已注册任务的元信息

        # 实盘交易调度器状态
        self._trading_scheduler = None
        self._trading_thread = None
        self._trading_running = False
        self._trading_account = None       # 当前交易使用的账号标识
        self._trading_account_name = None  # 当前交易账号展示名称
        # 启停竞态护：stop 只是置标志不等线程退出，而一轮批量可能跑几十秒到
        # 几分钟；若此时新建 trader，旧线程仍在按旧账本下单→重复开仓。
        # 用一把锁串行化状态迁移，并在 start 侧做「上一轮线程仍存活则拒绝」。
        self._trading_lock = threading.RLock()
        # 停止指令发出后等线程退出的时长（秒）。不追求一定等到退出：一轮
        # 分析未完成时强等会让 HTTP 请求被 nginx 超时掠掉，所以只等一小会，
        # 等不完就如实返回「收尾中」，真正的竞态兜底是 start 侧的存活检查。
        self._stop_join_seconds = float(os.environ.get('TRADING_STOP_JOIN_SECONDS', 15) or 15)
        # 重启自动拉起（只允许一个后台尝试线程）
        self._auto_resume_thread = None
        # 本次启动的归因结果与自愈计划（由 app.py 启动尾声传入，
        # 邮件与页面展示用；没传入时为 None = 不走归因策略）
        self._boot_info = None
        self._resume_plan = None

    def start(self):
        """启动调度器（幂等，多次调用只启动一次）"""
        if not self._started:
            self._scheduler.start()
            self._started = True
            logger.info("[TaskScheduler] 调度器已启动")

    def shutdown(self):
        """停止调度器"""
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False
            logger.info("[TaskScheduler] 调度器已停止")

    def register_job(self, func, trigger='interval', job_id=None, job_name=None,
                     replace_existing=True, **trigger_kwargs):
        """
        注册定时任务

        参数：
            func:           要执行的函数
            trigger:        触发器类型 'interval' | 'cron' | 'date'
            job_id:         任务唯一 ID（默认使用函数名）
            job_name:       任务显示名称
            replace_existing: 是否替换已存在的同名任务
            **trigger_kwargs: 传给触发器的参数，如：
                - interval: seconds=60, minutes=5, hours=1
                - cron:     hour=8, minute=0, day_of_week='mon-fri'
                - date:     run_date='2025-01-01 08:00:00'
        """
        job_id = job_id or func.__name__
        job_name = job_name or func.__name__

        trigger_map = {
            'interval': IntervalTrigger,
            'cron': CronTrigger,
            'date': DateTrigger,
        }
        trigger_cls = trigger_map.get(trigger)
        if not trigger_cls:
            raise ValueError(f"不支持的触发器类型: {trigger}，可选: {list(trigger_map.keys())}")

        trigger_instance = trigger_cls(**trigger_kwargs)

        self._scheduler.add_job(
            func,
            trigger=trigger_instance,
            id=job_id,
            name=job_name,
            replace_existing=replace_existing,
        )

        # 记录元信息
        self._job_registry[job_id] = {
            'id': job_id,
            'name': job_name,
            'trigger': trigger,
            'trigger_kwargs': trigger_kwargs,
            'func_name': func.__name__,
            'registered_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

        logger.info(f"[TaskScheduler] 注册任务: {job_id} ({trigger}, {trigger_kwargs})")
        return job_id

    def remove_job(self, job_id):
        """移除任务"""
        try:
            self._scheduler.remove_job(job_id)
            self._job_registry.pop(job_id, None)
            logger.info(f"[TaskScheduler] 移除任务: {job_id}")
            return True
        except Exception as e:
            logger.warning(f"[TaskScheduler] 移除任务失败: {job_id}, {e}")
            return False

    def pause_job(self, job_id):
        """暂停任务"""
        try:
            self._scheduler.pause_job(job_id)
            return True
        except Exception as e:
            logger.warning(f"[TaskScheduler] 暂停任务失败: {job_id}, {e}")
            return False

    def resume_job(self, job_id):
        """恢复任务"""
        try:
            self._scheduler.resume_job(job_id)
            return True
        except Exception as e:
            logger.warning(f"[TaskScheduler] 恢复任务失败: {job_id}, {e}")
            return False

    def get_all_jobs(self):
        """获取所有已注册任务的状态信息"""
        jobs = []
        scheduler_jobs = {j.id: j for j in self._scheduler.get_jobs()}

        for job_id, meta in self._job_registry.items():
            sj = scheduler_jobs.get(job_id)
            # next_run_time 只在任务真正入队（调度器已 start）之后才存在：
            # 未启动时 add_job 把任务放进 _pending_jobs，那时的 Job 对象根本没这个
            # 属性，直接取会让 /api/task/jobs 抛 500。app.py 是「先 register_default_jobs
            # 再 start」的后台线程启动，两步之间页面轮询就会撞上，必须容错。
            next_run = None
            if sj is not None:
                nrt = getattr(sj, 'next_run_time', None)
                if nrt:
                    next_run = nrt.strftime('%Y-%m-%d %H:%M:%S')

            # 实盘交易调度器特殊处理：active 状态取 _trading_running
            if job_id == 'trading_scheduler':
                active = self._trading_running
            else:
                active = sj is not None

            jobs.append({
                'id': meta['id'],
                'name': meta['name'],
                'trigger': meta['trigger'],
                'trigger_kwargs': {k: str(v) for k, v in meta['trigger_kwargs'].items()},
                'func_name': meta['func_name'],
                'registered_at': meta['registered_at'],
                'next_run_time': next_run,
                'active': active,
            })

        return jobs

    def get_status(self):
        """获取调度器整体状态"""
        return {
            'running': self._started,
            'job_count': len(self._job_registry),
            'jobs': self.get_all_jobs(),
            'trading_scheduler': self.get_trading_status(),
        }

    # =================================================================
    # 实盘交易调度器集成
    # =================================================================
    def start_trading_scheduler(self, account=None, source='manual'):
        """以线程方式启动实盘交易调度器

        参数:
            account: 使用的 OKX 账号标识（api_config.ACCOUNTS 的 key）。
                     None 则使用默认账号。
            source:  'manual'（页面点击）| 'auto_resume'（重启自愈）。
        """
        with self._trading_lock:
            if self._trading_running:
                return False, '实盘交易调度器已在运行中'

            # 上一轮线程还在收尾时绝不能起新 trader：两个 TrendRangeTrader 会
            # 各自按自己的内存账本判断「有没有持仓/有没有挂单」并发下单，
            # 重复开仓就这么产成的（stop 不阻塞等待，所以这个窗口真实存在）。
            prev = self._trading_thread
            if prev is not None and prev.is_alive():
                msg = ('上一轮实盘线程仍在收尾（单币分析 3~50s，一轮可能几分钟），'
                       '此时启动会出现两个调度器并发下单，请稍后重试')
                logger.warning(f'[TaskScheduler] 拒绝启动：{msg}')
                return False, msg

            try:
                from .trend_range_trader import TrendRangeTrader

                self._trading_scheduler = TrendRangeTrader(
                    manual_direction_config={}, account=account
                )
                self._trading_thread = threading.Thread(
                    target=self._trading_scheduler.start_real_scheduler,
                    name='TradingScheduler',
                    daemon=True
                )
                self._trading_thread.start()
                self._trading_running = True
                self._trading_account = getattr(self._trading_scheduler, 'account', account)
                self._trading_account_name = getattr(
                    self._trading_scheduler, 'account_name', self._trading_account
                )
                logger.info(
                    f'[TaskScheduler] 实盘交易调度器已启动 '
                    f'(账号: {self._trading_account_name}, 来源: {source})'
                )
                # 记账「用户希望它在跑」，供重启后自动拉起判断（失败不影响启动）
                self._mark_desired_running(True, self._trading_account, source)
                return True, f'实盘交易调度器已启动（账号: {self._trading_account_name}）'
            except Exception as e:
                logger.error(f'[TaskScheduler] 实盘交易调度器启动失败: {e}')
                # 起不来就不要再记 desired_running=True，否则每次重启都会反复尝试拉起
                self._mark_desired_running(False, None, f'start_failed:{e}')
                return False, f'启动失败: {e}'

    def stop_trading_scheduler(self, wait_seconds=None):
        """优雅停止实盘交易调度器（发出停止指令后限时等线程收尾）"""
        with self._trading_lock:
            if not self._trading_running or not self._trading_scheduler:
                thr = self._trading_thread
                if thr is not None and thr.is_alive():
                    return False, '停止指令已发出，上一轮仍在收尾中，请勿重复操作'
                return False, '实盘交易调度器未运行'

            try:
                trader = self._trading_scheduler
                trader.running = False
                if hasattr(trader, 'stop_event'):
                    trader.stop_event.set()
                self._trading_running = False
                self._trading_account = None
                self._trading_account_name = None
                thread = self._trading_thread
                logger.info('[TaskScheduler] 实盘交易调度器已收到停止指令')
                # 人工停止 = 期望它不跑，重启后也就不必自动拉起
                self._mark_desired_running(False, None, 'manual_stop')
            except Exception as e:
                logger.error(f'[TaskScheduler] 实盘交易调度器停止失败: {e}')
                return False, f'停止失败: {e}'

        # 限时 join 放在锁外，不长期占锁；只能尽量收窄竞态窗口，
        # 剩下的由 start 侧存活检查兜住。
        timeout = self._stop_join_seconds if wait_seconds is None else float(wait_seconds)
        if thread is not None and timeout > 0:
            thread.join(timeout=timeout)
            if thread.is_alive():
                msg = (f'已发出停止指令，但本轮仍在收尾（等 {timeout:g}s 未退出），'
                       f'期间请勿启动新调度器，否则会出现两个 trader 并发下单')
                logger.warning(f'[TaskScheduler] {msg}')
                return True, msg

        return True, '实盘交易调度器已停止'

    def get_trading_status(self):
        """获取实盘交易调度器状态（不访库，供页面高频轮询）

        finishing：标志已停但线程仍在收尾。前端不能把这一态当成「已停止」，
        否则用户会以为安全了就重新点启动，撞上双 trader 竞态。
        重启自愈相关字段走 get_runtime_status()（低频）。
        """
        thread_alive = bool(self._trading_thread and self._trading_thread.is_alive())
        return {
            'running': self._trading_running,
            'thread_alive': thread_alive,
            'finishing': thread_alive and not self._trading_running,
            'account': self._trading_account,
            'account_name': self._trading_account_name,
            'stop_join_seconds': self._stop_join_seconds,
        }

    def get_loop_liveness(self):
        """读实盘交易主循环存活状态（供 alert_monitor liveness 旁路检测，只读、绝不抛）。

        进程心跳（memory_history）只能证明"进程还活着"，证明不了交易循环还在跑圈：
        主循环可能在某一批次内卡死（网络挂起/锁死锁），或线程被致命异常悄悄带走。
        主循环每轮开始时把 self.last_execution['batch'] 刷成当前时间（trend_range_trader
        第 2679 行），一旦停滞或线程死亡，该时间戳便不再推进 → (now - ts) 持续增长可被检出。

        返回 {running, thread_alive, last_batch_ts, interval_seconds}：
        - running：调度器侧"用户希望它在跑"的标志（停止后不再判停滞）；
        - thread_alive：交易线程是否存活，标志为真而线程为假 = 意外死亡；
        - last_batch_ts：最近一轮批次起始时间戳，None = 尚未跑出可判定的轮次；
        - interval_seconds：主循环执行间隔（用于把停滞阈值按轮次倍数放大）。
        """
        try:
            thread = self._trading_thread
            thread_alive = bool(thread and thread.is_alive())
            trader = self._trading_scheduler
            last_batch_ts = None
            interval_seconds = 60
            if trader is not None:
                try:
                    last_batch_ts = (getattr(trader, 'last_execution', {}) or {}).get('batch') or None
                except Exception:
                    last_batch_ts = None
                interval_seconds = getattr(trader, '_default_interval', 60) or 60
            return {
                'running': bool(self._trading_running),
                'thread_alive': thread_alive,
                'last_batch_ts': last_batch_ts,
                'interval_seconds': int(interval_seconds),
            }
        except Exception as e:
            logger.warning(f'[TaskScheduler] 读取主循环存活状态失败: {e}')
            return {'running': False, 'thread_alive': False,
                    'last_batch_ts': None, 'interval_seconds': 60}

    def get_runtime_status(self):
        """读「期望运行状态 + 自动拉起开关 + 本次启动归因」（页面加载/切换时调，不在轮询热路径）

        boot / resume_plan 是排障关键信息：用户看到“重启后实盘没起来”，
        第一句必然是“它到底知不知道自己是为什么重启的”。
        """
        data = self._runtime_snapshot()
        data['boot'] = self._boot_info
        data['resume_plan'] = self._resume_plan
        return data

    # ---------------------------------------------------------------
    # 重启自愈：记录期望状态 + 按开关自动拉起
    # ---------------------------------------------------------------
    @staticmethod
    def _runtime_snapshot():
        """读期望运行状态；记账层不可用时返回默认值（不自动拉起）"""
        if _runtime_repo is None:
            return {'desired_running': False, 'account': None, 'auto_resume': False}
        try:
            return _runtime_repo.load_runtime()
        except Exception as e:
            logger.warning(f'[TaskScheduler] 读取实盘期望运行状态失败: {e}')
            return {'desired_running': False, 'account': None, 'auto_resume': False}

    @staticmethod
    def _mark_desired_running(running, account=None, event=''):
        """记账「用户希不希望它在跑」；任何异常都不往外抛"""
        if _runtime_repo is None:
            return
        try:
            _runtime_repo.set_desired_running(running, account=account, event=event)
        except Exception as e:
            logger.warning(f'[TaskScheduler] 记录期望运行状态失败（不影响本次启停）: {e}')

    def set_auto_resume(self, enabled: bool):
        """开关「重启后自动拉起实盘」；返回最新状态 dict"""
        if _runtime_repo is None:
            raise RuntimeError('trading_runtime_repo 不可用，无法保存自动拉起开关')
        rt = _runtime_repo.set_auto_resume(bool(enabled))
        logger.info(f'[TaskScheduler] 重启自动拉起实盘开关已{"打开" if rt.get("auto_resume") else "关闭"}')
        return rt

    def schedule_auto_resume(self, delay_seconds=None, boot_info=None):
        """进程启动后延时尝试自动拉起（幂等，多次调用只起一个线程）

        两种调用方式必须分清：
        - 真实启动路径（app.py）：只传 boot_info（process_lifecycle 的归因结果），
          不传 delay_seconds。起不起、等多久、要不要先发预告信，全部交给
          ``plan_resume`` 按「夜间直接接回 / 白天留 10 分钟否决窗 / 重启风暴熔断」定；
        - 显式传 delay_seconds 而不传 boot_info：视为调用方自己拿定主意
          （冒烟脚本、事后人工补一次拉起尝试），跳过归因策略直接按给的延时试。

        延时本身是为了等 database.warmup_async 把库探完，避免重启初期抢不到连接。
        线程本身 daemon，失败不重试：下一次重启就是下一次机会。
        """
        if self._auto_resume_thread is not None and self._auto_resume_thread.is_alive():
            return
        self._boot_info = boot_info if isinstance(boot_info, dict) else None
        plan = self._plan_boot_resume(self._boot_info) if self._boot_info else None
        self._resume_plan = plan
        if delay_seconds is None:
            delay_seconds = float(plan['delay_seconds']) if plan else 20.0
        if plan is not None:
            logger.info(
                f"[TaskScheduler] 自愈计划[{plan.get('reason')}] "
                f"{'允许' if plan.get('allowed') else '拒绝'} / "
                f"延时 {delay_seconds:g}s / 窗口 {plan.get('window')} / {plan.get('why')}")
        self._auto_resume_thread = threading.Thread(
            target=self._auto_resume_worker,
            args=(delay_seconds, plan), daemon=True, name='TradingAutoResume')
        self._auto_resume_thread.start()

    @staticmethod
    def _plan_boot_resume(boot_info):
        """把归因结果换成自愈计划；任何一层坏了都回到「不自动拉起」这个保守值"""
        fallback = {'reason': (boot_info or {}).get('reason'), 'allowed': False,
                    'delay_seconds': 0.0, 'pre_notify': False, 'silent': False,
                    'window': None, 'selfheal_count': 0,
                    'why': '归因/策略层不可用，按保守处理：不自动拉起', 'resume_at': ''}
        if _lifecycle is None:
            logger.warning('[TaskScheduler] process_lifecycle 不可用，本次不自动拉起')
            return fallback
        try:
            return _lifecycle.plan_resume(boot_info)
        except Exception as e:
            logger.warning(f'[TaskScheduler] 自愈计划计算失败，按保守处理: {e}')
            return fallback

    def _auto_resume_worker(self, delay_seconds: float, plan: dict = None):
        # 归因结论直接拦下的（初次上线 / 熔断 / 原因未知）在这里早退。
        # 但“本应在跑却没起”仍须发信，否则又变成静默停摆。
        if plan is not None and not plan.get('allowed', True):
            rt = self._runtime_snapshot()
            why = plan.get('why', '')
            logger.warning(f'[TaskScheduler] 重启自愈按策略跳过：{why}')
            if rt.get('desired_running') and not plan.get('silent'):
                self._notify_auto_resume_skipped(rt, why)
            return

        if plan is not None and plan.get('pre_notify') and delay_seconds > 0:
            # 白天窗口：先给一封「N 分钟后自动拉起」的预告，留出人工干预时间
            self._notify_resume_pending(plan, delay_seconds)

        try:
            if delay_seconds > 0:
                threading.Event().wait(delay_seconds)
        except Exception:
            pass

        if self._trading_running:
            return  # 已经被人工起过了，不重复起（预告窗内你点了启动，就在这里生效）
        # 期望状态在睡完之后才读：这段时间里你去页面点「停止交易」会清掉
        # desired_running，本次拉起自然作废 —— 这就是现成的人工否决通道。
        rt = self._runtime_snapshot()
        if not rt.get('auto_resume'):
            if rt.get('desired_running'):
                logger.info('[TaskScheduler] 重启检测：实盘本应在跑，但自动拉起开关未打开，'
                            '保持人工启动（避免持仓静默无人管，请手动启动）')
                self._notify_auto_resume_skipped(rt)
            return
        if not rt.get('desired_running'):
            return  # 上次就是停着的，不该自作主张开始下单
        account = rt.get('account')
        logger.info(f'[TaskScheduler] 重启自动拉起实盘调度器（账号: {account}）')
        ok, msg = self.start_trading_scheduler(account=account, source='auto_resume')
        if ok:
            logger.info(f'[TaskScheduler] 自动拉起成功: {msg}')
        else:
            logger.error(f'[TaskScheduler] 自动拉起失败: {msg}')
        # 每日主动归零属计划内重启，成功接回不再发信（否则天天一封噪声）
        if ok and plan is not None and plan.get('silent'):
            return
        self._notify_auto_resume_result(ok, msg, account, rt)

    def _notify_auto_resume_skipped(self, rt, why=''):
        """开关未打开 / 被策略拦下，但实盘本应在跑：必须发信，否则又是静默停摆"""
        self._notify_auto_resume_result(
            False,
            why or '自动拉起开关未打开，本次重启后实盘保持停止，请手动点击「启动交易」',
            rt.get('account'), rt)

    def _format_boot_line(self):
        """把本次启动归因压成一行（邮件正文用）。没有归因信息时返回空字串"""
        info = self._boot_info or {}
        if not info:
            return ''
        line = ' / '.join([str(p) for p in (info.get('reason'), info.get('label')) if p])
        if info.get('detail'):
            line += '：' + str(info['detail'])
        plan = self._resume_plan or {}
        cnt = plan.get('selfheal_count')
        if cnt:
            line += f'（窗口内自愈类重启已 {int(cnt)} 次）'
        if info.get('boot_seq'):
            line += f"  [boot #{info['boot_seq']}]"
        return line

    def _notify_resume_pending(self, plan, delay_seconds):
        """白天窗口的预告信：告诉你「多久之后会自动开始下单」，留出人工否决时间

        为什么不等到结果再发：白天延时的意义就是给你一段时间先看看持仓和挂单，
        事后再告知就成了“通知你已经下单了”。
        """
        try:
            rt = self._runtime_snapshot()
            mins = max(1, int(round(float(delay_seconds) / 60.0)))
            resume_at = str(plan.get('resume_at') or '')
            when = resume_at or (f'约 {mins} 分钟后')
            boot = self._format_boot_line()
            body = ('事件: 进程重启后，实盘调度器计划自动接回\n'
                    f'预计执行时刻: {when}\n'
                    + (f'启动归因: {boot}\n' if boot else '')
                    + f'策略说明: {plan.get("why", "")}\n'
                    f'账号: {rt.get("account") or "默认"}\n'
                    f'期望运行: {rt.get("desired_running")}  '
                    f'自动拉起开关: {rt.get("auto_resume")}\n\n'
                    f'这 {mins} 分钟是核实时间：\n'
                    '  • 现在就可以打开 /task 页面核对持仓与挂单，也可以直接点'
                    '「启动交易」—— 人工启动优先，自动拉起会自行让位；\n'
                    '  • 不想让它起来，就点「停止交易」（会清除期望运行标记，'
                    '本次拉起随即作废）。')
            self._send_resume_mail(f'⏳ 实盘将于 {when} 自动接回 - 请核实', body, '预告')
        except Exception as e:
            logger.warning(f'[TaskScheduler] 自愈预告邮件准备失败: {e}')

    def _send_resume_mail(self, subject: str, content: str, label: str):
        """自愈相关邮件的统一出口；发不出去只记日志，绝不拖挂交易线程"""
        try:
            try:
                from ..notification.email_tool import EmailTool
                to_email = None
                try:
                    from ..config.email_config import get_admin_email
                    to_email = get_admin_email()
                except Exception:
                    pass
            except ImportError:  # 包外独立运行时的兼容分支
                from notification.email_tool import EmailTool
                try:
                    from config.email_config import get_admin_email
                    to_email = get_admin_email()
                except Exception:
                    to_email = None
            EmailTool().send_system_notification(
                to_email=to_email, content=content, subject=subject)
            logger.info(f'[TaskScheduler] 自愈{label}邮件已发出: {subject}')
        except Exception as e:
            logger.warning(f'[TaskScheduler] 自愈{label}邮件发送失败: {e}')

    def _notify_auto_resume_result(self, ok: bool, msg: str, account, rt: dict):
        """重启自愈结果邮件（成/败都发）

        正文一定带上「这次是怎么起来的」：没有归因，收信人无法判断该不该担心 ——
        同样是不拉起，初次上线很正常，被 OOM 连斩三次就是事故。
        """
        boot = self._format_boot_line()
        body = ('事件: 进程重启后的实盘自愈尝试\n'
                f'结果: {"已自动拉起" if ok else "未拉起/拉起失败"}\n'
                + (f'启动归因: {boot}\n' if boot else '')
                + f'说明: {msg}\n'
                f'账号: {account or "默认"}\n'
                f'期望运行: {rt.get("desired_running")}  '
                f'自动拉起开关: {rt.get("auto_resume")}\n'
                f'上次状态变更: {rt.get("last_event")} @ {rt.get("updated_at")}\n\n'
                '请确认持仓与挂单是否符合预期；如需停止，在页面点「停止交易」'
                '即可（会同时清除期望运行标记）。')
        self._send_resume_mail(
            f'[实盘自愈] {"已自动拉起" if ok else "未自动拉起"} - 请核实', body, '结果')


    def force_close_positions(self, close_type='all'):
        """
        强制平仓（适配双模式：全仓=多头，逐仓=空头）

        参数：
            close_type: 'all' 全平 | 'long' 平全仓多头 | 'short' 平逐仓空头
        """
        if not self._trading_running or not self._trading_scheduler:
            return False, '实盘交易调度器未运行，无法平仓'

        try:
            scheduler = self._trading_scheduler
            import json
            cfg_path = scheduler.config_path
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)

            if close_type not in ('all', 'long', 'short'):
                return False, f'未知的平仓类型: {close_type}'

            results = []
            for cur in cfg.get('currencies', []):
                inst_id = cur.get('instId', '')
                if not inst_id:
                    continue

                # 两个仓位的挂单与本地账本由调度器统一清理后再市价强平
                msgs = scheduler.force_close_manual(inst_id, close_type)
                results.append(f"{inst_id}: {'; '.join(msgs) if msgs else '无可平仓位'}")

            logger.info(f'[TaskScheduler] 强制平仓 ({close_type}): {results}')
            return True, '; '.join(results)
        except Exception as e:
            logger.error(f'[TaskScheduler] 强制平仓失败: {e}')
            return False, f'平仓失败: {e}'

    def send_test_email(self, to_email=None):
        """发送测试邮件"""
        try:
            from .config.email_config import EMAIL_CONFIG, ADMIN_EMAIL
            import smtplib
            from email.mime.text import MIMEText

            target = to_email or ADMIN_EMAIL
            msg = MIMEText(
                '这是一封来自定时任务管理系统的测试邮件。\n\n如果您收到此邮件，说明 SMTP 配置正确。',
                'plain', 'utf-8'
            )
            msg['Subject'] = '[测试] 定时任务系统邮件通知'
            msg['From'] = EMAIL_CONFIG['from_email']
            msg['To'] = target

            if EMAIL_CONFIG.get('use_ssl', True):
                server = smtplib.SMTP_SSL(EMAIL_CONFIG['smtp_host'], EMAIL_CONFIG['smtp_port'], timeout=10)
            else:
                server = smtplib.SMTP(EMAIL_CONFIG['smtp_host'], EMAIL_CONFIG['smtp_port'], timeout=10)
                server.starttls()

            server.login(EMAIL_CONFIG['from_email'], EMAIL_CONFIG['password'])
            server.sendmail(EMAIL_CONFIG['from_email'], [target], msg.as_string())
            server.quit()

            logger.info(f'[TaskScheduler] 测试邮件已发送至 {target}')
            return True, f'测试邮件已发送至 {target}'
        except Exception as e:
            logger.error(f'[TaskScheduler] 测试邮件发送失败: {e}')
            return False, f'发送失败: {e}'


# =============================================================================
# 全局单例
# =============================================================================
task_scheduler = TaskScheduler()


def register_default_jobs():
    """注册默认任务（项目启动时调用）"""
    # 注册实盘交易调度器（仅注册到任务列表显示，不在此处直接启动）。
    # 默认仍由用户在 Web 界面手动点"启动交易"；只有当 kv_store 里记着
    # 「上次是希望它在跑的」且 auto_resume 开关已打开时，才由 app.py 启动
    # 尾声调 task_scheduler.schedule_auto_resume() 限时拉起（见 scheduler 内注释）。
    task_scheduler._job_registry['trading_scheduler'] = {
        'id': 'trading_scheduler',
        'name': '实盘交易调度器',
        'trigger': 'thread',
        'trigger_kwargs': {'mode': 'manual_start'},
        'func_name': 'TrendRangeTrader.start_real_scheduler',
        'registered_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    logger.info('[TaskScheduler] 已注册实盘交易调度器（默认待手动启动）')

    # 注册异常行情与持仓盈亏监控任务（默认周期从 kv_store 配置读取，
    # 配置保存时由 alert_routes 调 register_alert_job 热重注册）
    try:
        from .monitor.alert_monitor import register_alert_job
        interval_sec = register_alert_job()
        logger.info(f'[TaskScheduler] 已注册监控告警任务（周期 {interval_sec}s）')
    except Exception as e:
        logger.warning(f'[TaskScheduler] 监控告警任务注册失败（不影响其他任务）: {e}')

    # 注册分析纪律巡检任务（小时槽合格判定 + 缺口提醒 + 每日日报）。
    # 纪律开关关闭时 register_discipline_job 会自行 remove_job 并返回 0，
    # 配置保存由 discipline_routes 调它热重注册。
    try:
        from .monitor.analysis_discipline import register_discipline_job
        interval_sec = register_discipline_job()
        if interval_sec:
            logger.info(f'[TaskScheduler] 已注册分析纪律巡检任务（周期 {interval_sec}s）')
        else:
            logger.info('[TaskScheduler] 分析纪律已关闭，巡检任务未注册')
    except Exception as e:
        logger.warning(f'[TaskScheduler] 分析纪律巡检任务注册失败（不影响其他任务）: {e}')

    # 注册每日内存主动归零重启（默认 04:03）：把“被动涨到 kill 阈值才撞线”
    # 换成“低波动窗口主动清零”。它自己会按三个条件拒绝执行（水位太低 /
    # 策略引擎忙 / 开关没开但实盘本应在跑），所以关掉自动拉起开关时不会
    # 把实盘重启成一个静默停摆窗口。
    try:
        from .memory_daily_reset import register_daily_reset_job
        job_id, at_text = register_daily_reset_job()
        if job_id:
            logger.info(f'[TaskScheduler] 已注册每日内存主动归零重启任务（{at_text}）')
        else:
            logger.info(f'[TaskScheduler] 每日内存归零未注册（{at_text}）')
    except Exception as e:
        logger.warning(f'[TaskScheduler] 每日内存归零任务注册失败（不影响其他任务）: {e}')

    # 注册盘感影子预测任务（整点批跑 + 10 分钟满窗结算）。双闸门：
    # 未配 CRYPTO_LLM_* 或 CRYPTO_INSTINCT_AUTO=0 时不注册（空转不如缺席）；
    # 只写 instinct_* 表，永不触达交易执行链路。
    try:
        from ..instinct.predict_service import register_instinct_jobs
        job_id, note = register_instinct_jobs()
        if job_id:
            logger.info(f'[TaskScheduler] 已注册盘感影子预测任务（{note}）')
        else:
            logger.info(f'[TaskScheduler] 盘感影子预测未注册（{note}）')
    except Exception as e:
        logger.warning(f'[TaskScheduler] 盘感影子预测任务注册失败（不影响其他任务）: {e}')
