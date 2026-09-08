#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
消息通知工具类
=============

功能：
1. 检查货币方向变化
2. 发送系统消息到数据库
3. 发送邮件通知
4. 支持批量方向变化检测

作者：AI Assistant
创建时间：2025年1月17日
"""

import datetime
import logging
import json
import os
import hashlib
import time as _time
from typing import Dict, Optional, Tuple, List
import traceback

from .email_tool import EmailTool, EmailTemplates

# 配置日志格式 - 只显示时间戳和内容
formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.handlers = []
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

class MessageNotifier:
    """消息通知工具类"""

    # 监控告警级别样式：(emoji, 头部颜色, 中文级别)
    _ALERT_LEVEL_STYLE = {
        'warning': ('⚠️', '#d32f2f', '预警'),
        'critical': ('🚨', '#b71c1c', '严重'),
        'recover': ('✅', '#388e3c', '缓解'),
    }

    def __init__(self, config_file: str = None, email_cooldown_minutes: int = 30):
        self.email_tool = EmailTool()
        self.email_cooldown_minutes = email_cooldown_minutes
        self.last_sent_times: Dict[str, datetime.datetime] = {}

        # 内容指纹去重：防止同一封邮件（含重试/重复调用）被物理发送多次
        self._email_dedup_window_seconds = 300
        self._recent_email_fingerprints: Dict[str, datetime.datetime] = {}
        
        # 配置文件路径
        if config_file is None:
            # 使用默认配置文件路径
            current_dir = os.path.dirname(os.path.abspath(__file__))
            task_dir = os.path.dirname(current_dir)
            self.config_file = os.path.join(task_dir, 'config', 'notification_config.json')
        else:
            self.config_file = config_file
        
        # 加载通知配置
        self.notification_config = self.load_notification_config()
        
    def load_notification_config(self) -> Dict:
        """加载通知配置文件"""
        try:
            if not os.path.exists(self.config_file):
                logger.warning(f"配置文件不存在: {self.config_file}，将使用默认配置")
                return {
                    "notification_rules": [],
                    "global_settings": {
                        "enabled": False,
                        "email_notifications": True,
                        "system_messages": True
                    }
                }
            
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                logger.info(f"配置加载: {len(config.get('notification_rules', []))} 规则")
                return config
                
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return {
                "notification_rules": [],
                "global_settings": {
                    "enabled": False,
                    "email_notifications": True,
                    "system_messages": True
                }
            }
    
    def should_notify(self, currency_code: str, time_period: str) -> bool:
        """
        检查是否需要发送通知
        
        Args:
            currency_code: 货币代码
            time_period: 时间周期
            
        Returns:
            bool: True表示需要发送通知
        """
        # 检查全局开关和配置规则
        global_settings = self.notification_config.get('global_settings', {})
        if not global_settings.get('enabled', False):
            return False
        
        notification_rules = self.notification_config.get('notification_rules', [])
        for rule in notification_rules:
            if (rule.get('enabled', True) and 
                rule.get('currency_code') == currency_code and 
                time_period in rule.get('time_periods', [])):
                return True
        return False
    
    def get_notification_summary(self) -> str:
        """获取通知配置摘要"""
        global_settings = self.notification_config.get('global_settings', {})
        notification_rules = self.notification_config.get('notification_rules', [])
        
        enabled_rules = [rule for rule in notification_rules if rule.get('enabled', True)]
        
        summary = []
        summary.append(f"全局通知: {'启用' if global_settings.get('enabled', False) else '禁用'}")
        summary.append(f"配置规则: {len(enabled_rules)} 个")
        
        for rule in enabled_rules:
            currency = rule.get('currency_code', '未知')
            periods = ', '.join(rule.get('time_periods', []))
            summary.append(f"  - {currency}: {periods}")
        
        return '\n'.join(summary)
    
    def _check_and_update_cooldown(self, identifier: str) -> bool:
        """
        检查是否处于冷却期并更新时间
        
        Args:
            identifier: 唯一标识符 (如 "BTC-USDT_trade")
            
        Returns:
            bool: True表示可以发送(不在CD中)，False表示需要跳过
        """
        now = datetime.datetime.now()
        last_time = self.last_sent_times.get(identifier)
        
        if last_time:
            # 计算时间差
            diff = now - last_time
            cooldown_delta = datetime.timedelta(minutes=self.email_cooldown_minutes)
            
            if diff < cooldown_delta:
                # 处于CD中
                remaining_min = (cooldown_delta - diff).total_seconds() / 60
                logger.info(f"[{now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] Email skipped (CD: {self.email_cooldown_minutes} min, remaining: {remaining_min:.1f} min) - {identifier}")
                return False
        
        # 更新发送时间
        self.last_sent_times[identifier] = now
        # logger.info(f"[{now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] Email sent successfully log - {identifier}") # 这一句用户没要求必须在这里打，用户要求的是"执行发送并更新日志"
        # 用户要求："日志格式统一为：`[YYYY-MM-DD HH:MM:SS] Email skipped (CD: XX min)` 或 `Email sent successfully`"
        # 这个"Email sent successfully"通常是在发送成功后打印，但我这里是check函数。
        # 我会在发送成功后打印那个日志。
        return True

    def _dispatch_email(self, to_emails: List[str], subject: str, html_content: str,
                        log_label: str = '通知', max_retries: int = 3,
                        retry_interval: int = 5) -> bool:
        """
        统一邮件发送出口：内容指纹去重 + 重试。

        通过对（收件人 + 主题 + 正文）做指纹，在去重窗口内相同内容只物理发送一次，
        从根本上避免因内部重试的假失败（如 QQ SMTP 投递成功却报连接异常）或
        重复调用导致的“一条通知发两封”问题。
        """
        now = datetime.datetime.now()
        raw = f"{','.join(sorted(to_emails))}|{subject}|{html_content}"
        fingerprint = hashlib.md5(raw.encode('utf-8')).hexdigest()

        window = datetime.timedelta(seconds=self._email_dedup_window_seconds)

        # 清理过期指纹，防止内存无限增长
        expired = [k for k, t in self._recent_email_fingerprints.items() if now - t > window]
        for k in expired:
            self._recent_email_fingerprints.pop(k, None)

        # 去重：相同内容在窗口期内已发送过 → 跳过
        last = self._recent_email_fingerprints.get(fingerprint)
        if last and (now - last) <= window:
            logger.info(f"[EMAIL] 跳过重复邮件(dedup {self._email_dedup_window_seconds}s): {subject}")
            return True

        # 发送前先登记指纹：即便本次发送触发内部重试且首次为“假失败”，
        # 也不会在同一封邮件上重复物理投递。
        self._recent_email_fingerprints[fingerprint] = now

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"[EMAIL] 尝试发送{log_label} (第 {attempt} 次): {subject}")
                result = self.email_tool.send_html_email(
                    to_emails=to_emails,
                    subject=subject,
                    html_content=html_content
                )
                if result:
                    self._recent_email_fingerprints[fingerprint] = datetime.datetime.now()
                    logger.info(f"✅ {log_label}邮件发送成功: {subject}")
                    logger.info(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] Email sent successfully")
                    return True
                else:
                    logger.warning(f"⚠️ {log_label}邮件发送失败 (第 {attempt} 次)")
            except Exception as e:
                logger.error(f"❌ 发送{log_label}异常 (第 {attempt} 次): {e}")

            if attempt < max_retries:
                _time.sleep(retry_interval)

        # 全部重试失败：移除指纹，允许后续（新的一次调用）再次尝试
        self._recent_email_fingerprints.pop(fingerprint, None)
        logger.error(f"❌ {log_label}邮件最终发送失败，已重试 {max_retries} 次")
        return False

    def send_trade_notification(self, symbol: str, direction: str, price: float, amount: float, 
                               execution_time: str = None, order_type: str = "限价单") -> bool:
        """
        发送交易成功通知（带重试机制）
        """
        # 冷却时间检查
        if not self._check_and_update_cooldown(f"{symbol}_trade"):
            return False

        # 管理员邮箱 - 从统一配置读取
        from config.email_config import get_admin_email
        admin_email = get_admin_email()

        # 生成HTML内容
        html_content = EmailTemplates.trade_success(
            symbol, direction, price, amount, execution_time, order_type
        )
        subject = f"✅ 实盘交易成功通知 - {symbol} {direction}"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='交易通知'
        )

    def send_trade_operation_email(self, symbol: str, short_period: str, long_period: str,
                                   trade_direction: str, execution_time: str,
                                   price: float, success: bool,
                                   error_msg: str = '') -> bool:
        """
        发送交易操作结果通知邮件（成功/失败都发送）

        Args:
            symbol: 合约ID
            short_period: 短周期 (e.g. 1m)
            long_period: 长周期 (e.g. 15m)
            trade_direction: 交易方向（开多/开空/平多/平空）
            execution_time: 执行时间
            price: 交易价格
            success: 是否成功
            error_msg: 失败原因
        """
        if not self._check_and_update_cooldown(f"{symbol}_{trade_direction}_trade_op"):
            return False

        from config.email_config import get_admin_email
        admin_email = get_admin_email()

        status_emoji = '✅' if success else '❌'
        status_text = '成功' if success else '失败'
        header_color = '#388e3c' if success else '#d32f2f'

        error_row = ''
        if not success and error_msg:
            error_row = f"""
            <div class="info-row">
                <div class="info-label">失败原因</div>
                <div class="info-value highlight-red">{error_msg}</div>
            </div>"""

        content_body = f"""
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">合约</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>
            <div class="info-row">
                <div class="info-label">交易周期</div>
                <div class="info-value">短周期 {short_period} / 长周期 {long_period}</div>
            </div>
            <div class="info-row">
                <div class="info-label">交易方向</div>
                <div class="info-value highlight-blue">{trade_direction}</div>
            </div>
            <div class="info-row">
                <div class="info-label">执行时间</div>
                <div class="info-value">{execution_time}</div>
            </div>
            <div class="info-row">
                <div class="info-label">交易价格</div>
                <div class="info-value">{float(price):.4f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">操作结果</div>
                <div class="info-value {'highlight-green' if success else 'highlight-red'}">{status_text}</div>
            </div>{error_row}
        </div>
        """

        html_content = EmailTemplates._wrap_html(
            f"{status_emoji} 交易操作{status_text} - {symbol}",
            content_body,
            header_color=header_color
        )
        subject = f"{status_emoji} 交易操作{status_text} - {symbol} {trade_direction}"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='交易操作通知'
        )

    def send_trend_reversal_warning(self, symbol: str, period: str, old_direction: str, 
                                   new_direction: str, current_price: float) -> bool:
        """
        发送趋势反转预警通知（带重试机制）
        """
        # 冷却时间检查
        if not self._check_and_update_cooldown(f"{symbol}_reversal_warning"):
            return False

        from config.email_config import get_admin_email
        admin_email = get_admin_email()
        
        html_content = EmailTemplates.trend_reversal_warning(
            symbol, period, old_direction, new_direction, current_price
        )
        subject = f"⚠️ 趋势反转预警 - {symbol} {period} ({old_direction}->{new_direction})"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='趋势预警'
        )

    def send_trend_reversal_confirmed(self, symbol: str, period: str, direction: str, 
                                     price: float) -> bool:
        """
        发送趋势反转确认通知（带重试机制）
        """
        # 冷却时间检查
        if not self._check_and_update_cooldown(f"{symbol}_reversal_confirmed"):
            return False

        from config.email_config import get_admin_email
        admin_email = get_admin_email()
        
        html_content = EmailTemplates.trend_reversal_confirmed(
            symbol, period, direction, price
        )
        subject = f"🚀 趋势反转确认 - {symbol} {period} ({direction})"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='趋势确认'
        )

    def send_reverse_position_warning(self, symbol: str, long_direction: str,
                                      reverse_side: str, reverse_amount: float,
                                      current_price: float, grace_minutes: float,
                                      cross_pos: float = 0.0,
                                      isolated_pos: float = 0.0,
                                      source_note: str = '') -> bool:
        """发送反向持仓预警邮件（含强平倒计时警告）。

        source_note：冲突来源（程序旧方向持仓 / 账本外人工反向单 / 两者混合）。
        属于关键风控告警，不走 30 分钟邮件冷却（避免被冷却吞掉后续强平通知）；
        重复发送由调用方的持久化 warned 标志与 _dispatch_email 指纹去重共同防护。
        """
        from config.email_config import get_admin_email
        admin_email = get_admin_email()

        html_content = EmailTemplates.reverse_position_warning(
            symbol, long_direction, reverse_side, reverse_amount,
            current_price, grace_minutes, cross_pos, isolated_pos,
            source_note=source_note
        )
        subject = f"⚠️ 反向持仓预警 - {symbol}（{grace_minutes:g}分钟后将强平）"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='反向持仓预警'
        )

    def send_reverse_position_closed(self, symbol: str, long_direction: str,
                                     reverse_side: str, reverse_amount: float,
                                     current_price: float,
                                     source_note: str = '') -> bool:
        """发送反向持仓已强制平仓通知邮件（关键风控告警，不走冷却）。"""
        from config.email_config import get_admin_email
        admin_email = get_admin_email()

        html_content = EmailTemplates.reverse_position_closed(
            symbol, long_direction, reverse_side, reverse_amount, current_price,
            source_note=source_note
        )
        subject = f"🛑 反向持仓已强平 - {symbol}"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='反向持仓强平'
        )

    def send_entry_window_alert(self, symbol: str, short_period: str, long_period: str,
                                direction: str, entry_px: float,
                                contracts: float = 0) -> bool:
        """发送开仓窗口触发提醒（进入交易窗口时通知一次方向与挂单参考价）。

        不走邮件冷却：窗口上升沿由调用方保证每个窗口只触发一次，
        若被 30 分钟冷却吞掉会漏报关键开仓时机；重复发送由 _dispatch_email
        指纹去重兜底。

        Args:
            symbol: 合约ID
            short_period/long_period: 短/长周期
            direction: 交易方向（开多/开空）
            entry_px: 限价挂单参考价
            contracts: 计划挂单张数（0=不展示，如仓位停用/折算失败）
        """
        from config.email_config import get_admin_email
        admin_email = get_admin_email()

        is_long = direction == '开多'
        dir_color = '#d32f2f' if is_long else '#388e3c'
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        amount_row = ''
        if contracts and contracts > 0:
            amount_row = f"""
            <div class="info-row">
                <div class="info-label">计划挂单量</div>
                <div class="info-value">{contracts:g} 张</div>
            </div>"""

        content_body = f"""
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">合约</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>
            <div class="info-row">
                <div class="info-label">交易周期</div>
                <div class="info-value">短周期 {short_period} / 长周期 {long_period}</div>
            </div>
            <div class="info-row">
                <div class="info-label">交易方向</div>
                <div class="info-value" style="color:{dir_color};font-weight:bold">{direction}</div>
            </div>
            <div class="info-row">
                <div class="info-label">挂单参考价</div>
                <div class="info-value highlight-blue">{float(entry_px):.6g}</div>
            </div>{amount_row}
            <div class="info-row">
                <div class="info-label">触发时间</div>
                <div class="info-value">{now_str}</div>
            </div>
        </div>
        <p style="color:#888;font-size:12px">双周期共振开仓窗口已确认，系统已按参考价挂限价单（不追价，等成交或窗口结束撤单）。</p>
        """

        html_content = EmailTemplates._wrap_html(
            f"🎯 开仓窗口提醒 - {symbol} {direction}",
            content_body,
            header_color='#1976d2'
        )
        subject = f"🎯 开仓窗口提醒 - {symbol} {direction} @{float(entry_px):.6g}"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='开仓窗口提醒'
        )

    def send_watch_mode_alert(self, symbol: str, event_type: str, title: str,
                              detail_rows: List[Tuple[str, str]] = None,
                              hint: str = '') -> bool:
        """发送观察模式事件提醒邮件（trade_enabled=false 时只提醒不交易）。

        每类事件单独一封，冷却标识符按 event_type 区分，不同类型互不压制；
        同一事件的持续重复由冷却 + _dispatch_email 指纹去重共同防护。

        Args:
            symbol: 合约ID
            event_type: 事件类型（entry_window/exit_signal/range_entry/sl_tp/reversal/...）
            title: 事件标题（不含前缀，如"建议开多"）
            detail_rows: [(字段名, 值)] 明细行
            hint: 底部补充说明
        """
        if not self._check_and_update_cooldown(f"{symbol}_watch_{event_type}"):
            return False

        from config.email_config import get_admin_email
        admin_email = get_admin_email()

        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        rows_html = ''
        if detail_rows:
            for label, value in detail_rows:
                rows_html += f"""
            <div class="info-row">
                <div class="info-label">{label}</div>
                <div class="info-value">{value}</div>
            </div>"""

        hint_html = ''
        if hint:
            hint_html = f'<p style="color:#888;font-size:12px">{hint}</p>'

        content_body = f"""
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">合约</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>{rows_html}
            <div class="info-row">
                <div class="info-label">触发时间</div>
                <div class="info-value">{now_str}</div>
            </div>
        </div>
        <p style="color:#e65100;font-size:13px;font-weight:bold">该币种已关闭自动交易（观察模式），系统未执行任何交易所操作，请人工决定是否跟踪。</p>
        {hint_html}
        """

        html_content = EmailTemplates._wrap_html(
            f"🔍 【观察模式】{title} - {symbol}",
            content_body,
            header_color='#ef6c00'
        )
        subject = f"🔍 【观察模式】{title} - {symbol}"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='观察模式提醒'
        )

    def send_system_alert(self, symbol: str, title: str, detail: str) -> bool:
        """发送系统级告警邮件（连续执行失败等静默失效场景），受冷却约束避免刷屏。"""
        if not self._check_and_update_cooldown(f"{symbol}_sys_alert"):
            return False

        from config.email_config import get_admin_email
        admin_email = get_admin_email()

        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        html_content = (
            f"<div style='font-family:Arial,sans-serif'>"
            f"<h3 style='color:#d9534f'>⚠️ 系统告警：{title}</h3>"
            f"<p><b>合约：</b>{symbol}</p>"
            f"<p>{detail}</p>"
            f"<p style='color:#999'>告警时间：{now_str}</p>"
            f"</div>"
        )
        subject = f"⚠️ 系统告警 - {symbol} {title}"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='系统告警'
        )

    # =================================================================
    # 监控告警（异常行情与持仓盈亏监控报警系统）
    # 冷却由监控引擎的级别递进状态机（kv_store alert_runtime_state）控制，
    # 这里不再叠加 30 分钟冷却；重复发送由 _dispatch_email 指纹去重兜底。
    # =================================================================

    def send_price_volatility_alert(self, inst_id: str, change_pct: float,
                                    window_minutes: float, level: str = 'warning') -> bool:
        """发送价格波动异常告警邮件

        Args:
            inst_id: 合约ID
            change_pct: 窗口内涨跌幅 %（带符号）
            window_minutes: 检测窗口（分钟）
            level: warning / critical / recover
        """
        from config.email_config import get_admin_email
        admin_email = get_admin_email()

        emoji, color, label = self._ALERT_LEVEL_STYLE.get(level, ('⚠️', '#d32f2f', '预警'))
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        chg_color = '#d32f2f' if change_pct < 0 else '#388e3c'
        if level == 'recover':
            hint = '价格波动已回落至阈值下方，风险解除。'
        else:
            hint = '检测到短时间内价格剧烈波动，请注意持仓风险，谨慎追单。'

        content_body = f"""
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">合约</div>
                <div class="info-value highlight-blue">{inst_id}</div>
            </div>
            <div class="info-row">
                <div class="info-label">窗口涨跌幅</div>
                <div class="info-value" style="color:{chg_color};font-weight:bold">{change_pct:+.2f}%</div>
            </div>
            <div class="info-row">
                <div class="info-label">检测窗口</div>
                <div class="info-value">近 {window_minutes:g} 分钟</div>
            </div>
            <div class="info-row">
                <div class="info-label">触发时间</div>
                <div class="info-value">{now_str}</div>
            </div>
        </div>
        <p style="color:#888;font-size:12px">{hint}</p>
        """

        html_content = EmailTemplates._wrap_html(
            f"{emoji} 行情异动{label} - {inst_id}",
            content_body,
            header_color=color
        )
        subject = f"{emoji} 行情异动{label} - {inst_id} {change_pct:+.2f}%/{window_minutes:g}min"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='行情异动告警'
        )

    def send_position_pnl_alert(self, inst_id: str, bucket: str, direction: str,
                                pnl_pct: float, avg_px: float, last_px: float,
                                level: str = 'warning') -> bool:
        """发送持仓盈亏极端值告警邮件

        Args:
            inst_id: 合约ID
            bucket: 仓位篮子 trend/range
            direction: 多头/空头
            pnl_pct: 浮动盈亏率 %（带符号，标的价格口径）
            avg_px/last_px: 持仓均价 / 现价
            level: warning / critical / recover
        """
        from config.email_config import get_admin_email
        admin_email = get_admin_email()

        emoji, color, label = self._ALERT_LEVEL_STYLE.get(level, ('⚠️', '#d32f2f', '预警'))
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        pnl_color = '#388e3c' if pnl_pct >= 0 else '#d32f2f'
        bucket_cn = {'trend': '趋势仓', 'range': '震荡仓'}.get(bucket, bucket)
        if level == 'recover':
            hint = '浮动盈亏已回到阈值以内，风险缓解。'
        else:
            hint = '持仓浮动盈亏已达极端阈值，建议评估是否减仓/止盈/止损。'

        content_body = f"""
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">合约</div>
                <div class="info-value highlight-blue">{inst_id}</div>
            </div>
            <div class="info-row">
                <div class="info-label">仓位/方向</div>
                <div class="info-value">{bucket_cn} / {direction}</div>
            </div>
            <div class="info-row">
                <div class="info-label">浮动盈亏</div>
                <div class="info-value" style="color:{pnl_color};font-weight:bold">{pnl_pct:+.2f}%</div>
            </div>
            <div class="info-row">
                <div class="info-label">持仓均价</div>
                <div class="info-value">{float(avg_px):.6g}</div>
            </div>
            <div class="info-row">
                <div class="info-label">当前价格</div>
                <div class="info-value">{float(last_px):.6g}</div>
            </div>
            <div class="info-row">
                <div class="info-label">触发时间</div>
                <div class="info-value">{now_str}</div>
            </div>
        </div>
        <p style="color:#888;font-size:12px">{hint}</p>
        """

        html_content = EmailTemplates._wrap_html(
            f"{emoji} 持仓盈亏{label} - {inst_id}",
            content_body,
            header_color=color
        )
        subject = f"{emoji} 持仓盈亏{label} - {inst_id} {direction} {pnl_pct:+.2f}%"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='持仓盈亏告警'
        )

    def send_alert_digest(self, items: List[Dict]) -> bool:
        """发送轮内聚合告警摘要（同一检测轮多个命中合并为一封，防批量轰炸）

        Args:
            items: [{'type': 'price'/'pnl', 'level': ..., 'inst_id': ...,
                     'value': %, 'threshold': %, 'message': ...}]
        """
        from config.email_config import get_admin_email
        admin_email = get_admin_email()

        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        has_critical = any(it.get('level') == 'critical' for it in items)
        emoji, color = ('🚨', '#b71c1c') if has_critical else ('⚠️', '#d32f2f')

        rows = []
        for it in items:
            lv_emoji = self._ALERT_LEVEL_STYLE.get(it.get('level', 'warning'), ('⚠️',))[0]
            val = float(it.get('value', 0.0))
            val_color = '#d32f2f' if val < 0 else '#388e3c'
            type_cn = '行情异动' if it.get('type') == 'price' else '持仓盈亏'
            rows.append(
                f"<tr>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #eee'>{lv_emoji}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #eee'>{type_cn}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #eee;font-weight:bold'>{it.get('inst_id', '')}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #eee;color:{val_color};font-weight:bold'>{val:+.2f}%</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #eee;color:#999'>{float(it.get('threshold', 0.0)):+.2f}%</td>"
                f"</tr>"
            )

        content_body = f"""
        <p>同一检测轮内共 <b>{len(items)}</b> 项监控命中，汇总如下：</p>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background-color:#f5f5f5;color:#666">
                <th style="padding:8px 10px;text-align:left">级别</th>
                <th style="padding:8px 10px;text-align:left">类型</th>
                <th style="padding:8px 10px;text-align:left">合约</th>
                <th style="padding:8px 10px;text-align:left">指标值</th>
                <th style="padding:8px 10px;text-align:left">阈值</th>
            </tr>
            {''.join(rows)}
        </table>
        <p class="timestamp">检测时间：{now_str}</p>
        """

        html_content = EmailTemplates._wrap_html(
            f"{emoji} 监控告警汇总 - {len(items)} 项命中",
            content_body,
            header_color=color
        )
        subject = f"{emoji} 监控告警汇总 - {len(items)} 项命中"

        return self._dispatch_email(
            to_emails=[admin_email],
            subject=subject,
            html_content=html_content,
            log_label='监控告警汇总'
        )


def test_message_notifier():
    """测试消息通知功能"""
    notifier = MessageNotifier()
    
    # 测试邮件连接
    if notifier.email_tool.test_connection():
        logger.info("✅ 邮件连接测试成功")
    else:
        logger.info("❌ 邮件连接测试失败")
    

if __name__ == "__main__":
    logger.info("🔧 消息通知工具测试")
    test_message_notifier()