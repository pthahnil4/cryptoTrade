#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MySQL 数据库连接层（全局共享）
================================
连接串解析优先级：
  1. 环境变量 CRYPTO_DB_URL（最高优先，宝塔面板可配置）
     格式: mysql+pymysql://用户:密码@主机:端口/库名?charset=utf8mb4
  2. 数据目录下的 db_url.txt 文件（单行连接串，便于宝塔免环境变量部署）
     数据目录：环境变量 CRYPTO_PLAN_DATA_DIR → 缺省项目内 data/ 目录

设计约定：
- 连接池开启 pool_pre_ping，防止 MySQL wait_timeout 造成的僵死连接
- 建表权威来源为项目根目录 db_schema.sql；应用运行时 init_db() 仅做
  连通性探活 + 种子补全 + 存量索引自动补齐（不再逐表 create_all），
  完整建表仅迁移脚本 force_create 模式执行
"""

import os
import logging
import datetime
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger(__name__)

Base = declarative_base()

SCHEMA_VERSION = '1.0.0'

# 与 data_paths.resolve_data_dir 相同的数据目录解析（避免循环导入，独立实现）
def _resolve_data_dir() -> str:
    env_dir = os.environ.get('CRYPTO_PLAN_DATA_DIR', '').strip()
    if env_dir:
        return env_dir
    # 缺省项目内 data/ 目录（crypto/ 的上级即项目根）
    _crypto_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(_crypto_dir), 'data')


def resolve_db_url() -> str:
    """解析数据库连接串；未配置时返回空字符串（调用方负责报错提示）"""
    url = os.environ.get('CRYPTO_DB_URL', '').strip()
    if url:
        return url
    data_dir = _resolve_data_dir()
    if data_dir:
        url_file = os.path.join(data_dir, 'db_url.txt')
        if os.path.isfile(url_file):
            try:
                with open(url_file, 'r', encoding='utf-8') as f:
                    url = f.readline().strip()
            except OSError as e:
                logger.warning(f"[DB] 读取 db_url.txt 失败: {e}")
    return url


_engine = None
_SessionFactory = None


def _make_engine():
    url = resolve_db_url()
    if not url:
        raise RuntimeError(
            '数据库未配置：请设置环境变量 CRYPTO_DB_URL，'
            '或在外置数据目录创建 db_url.txt（单行连接串，'
            '格式 mysql+pymysql://用户:密码@主机:端口/库名?charset=utf8mb4）')
    return create_engine(
        url,
        pool_pre_ping=True,     # 取连接前先探活，规避 MySQL 超时断开
        pool_recycle=1800,      # 连接最长存活 30 分钟后强制换新
        pool_size=10,           # 常驻连接扩容，减少 overflow 连接的反复销毁/重建握手
        max_overflow=5,         # overflow 仅作突发缓冲（其连接用完即销毁，代价高）
        pool_timeout=30,
        pool_use_lifo=True,     # 后进先出：热连接持续复用，pre_ping 探活频率大幅下降
        connect_args={'connect_timeout': 5},  # DB 故障时快速失败，不阻塞启动/请求
        future=True)


def get_engine():
    """获取全局引擎（懒初始化，线程安全由 GIL + 幂等创建兜底）"""
    global _engine, _SessionFactory
    if _engine is None:
        _engine = _make_engine()
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory():
    get_engine()
    return _SessionFactory


def get_session():
    """获取会话（调用方负责 commit/rollback/close，推荐用 session_scope）"""
    return get_session_factory()()


class session_scope:
    """会话上下文管理器：正常退出自动 commit，异常自动 rollback"""

    def __init__(self):
        self.session = None

    def __enter__(self):
        self.session = get_session()
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()
        return False


_init_lock = threading.Lock()
_init_done = False

# 存量表增量索引补丁（新建库由 create_all 含 models.Index 定义自动建出；
# 存量库的缺失索引在首次 init_db 时自动补齐，每进程仅检查一次）
_REQUIRED_INDEXES = (
    ('trade_journal', 'idx_tj_inst_ts', '(`inst_id`, `ts`)'),
    ('alert_log', 'idx_alert_inst_time', '(`inst_id`, `created_at`)'),
    # 批次11 分析纪律：按小时槽聚合分析记录（闸门/巡检最高频查询）
    ('task_analysis_records', 'idx_tar_slot', '(`hour_slot`)'),
)

# 存量表增量列补丁（后续迭代新增的列，存量库首次 init_db 时自动补齐，
# 新建库由 db_schema.sql / create_all 直接含列；每进程仅检查一次）
# - crypto_coins.m15_*：批量多周期趋势分析新增 15 分钟周期（与 models.CryptoCoin 对齐）
# - 批次11 分析纪律：task_analysis_records 增 hour_slot/source；
#   plan_slots 增打卡与分析记录的关联列
_REQUIRED_COLUMNS = (
    ('crypto_coins', 'm15_trend', "VARCHAR(8) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_price', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_time', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_profit', "VARCHAR(16) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_close', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_macd', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_dif', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_adx', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_atr', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_sar', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_sar_color', "VARCHAR(8) NOT NULL DEFAULT ''"),
    ('task_analysis_records', 'hour_slot', "VARCHAR(13) NOT NULL DEFAULT ''"),
    ('task_analysis_records', 'source', "VARCHAR(16) NOT NULL DEFAULT 'live'"),
    ('plan_slots', 'analysis_ids', "VARCHAR(255) NOT NULL DEFAULT ''"),
    ('plan_slots', 'analysis_hour', "VARCHAR(13) NOT NULL DEFAULT ''"),
    ('plan_slots', 'bypass_analysis', "TINYINT(1) NOT NULL DEFAULT 0"),
)

# 新增表自动补建（init_db 默认模式不再逐表 create_all，这里只对
# 后续迭代新增的表做 checkfirst 建表，存量库升级时无需手工跑 SQL）
_NEW_TABLE_NAMES = (
    'analysis_reminder_log',   # 批次11 分析纪律小时槽台账
)

# 存量表废弃列退役补丁（架构调整后不再使用的列：先把存量数据迁移到
# 新归宿，再 DROP COLUMN；每进程仅执行一次，列不存在时直接跳过）
# - calorie_records.diary：热量记录内嵌随笔已废弃，随笔统一归入独立的
#   随笔模块（journal_notes），以 linked_from='calorie:YYYY-MM-DD' 回指热量记录日期
_LEGACY_COLUMNS = (
    ('calorie_records', 'diary'),
)


def _ensure_extra_indexes(engine):
    """检查增量索引是否存在，缺失则自动补建（information_schema 查询）。

    逐个索引独立提交：单个索引补建失败（如依赖列本次未补上）不得
    连带影响其余索引。MySQL DDL 本身就是隐式提交，包在同一事务里并无收益。
    """
    from sqlalchemy import text
    for table, name, cols in _REQUIRED_INDEXES:
        try:
            with engine.begin() as conn:
                exists = conn.execute(text(
                    'SELECT COUNT(*) FROM information_schema.statistics '
                    'WHERE table_schema = DATABASE() AND table_name = :t '
                    'AND index_name = :i'), {'t': table, 'i': name}).scalar()
                if not exists:
                    conn.execute(text(
                        f'ALTER TABLE `{table}` ADD INDEX `{name}` {cols}'))
                    logger.info('[DB] 已补建存量表索引: %s.%s', table, name)
        except Exception as e:
            logger.warning('[DB] 补建索引失败 %s.%s（不影响启动）: %s', table, name, e)


def _ensure_extra_columns(engine):
    """检查增量列是否存在，缺失则自动补建（information_schema 查询）。

    逐个列独立提交，与 _ensure_extra_indexes 同理。
    """
    from sqlalchemy import text
    for table, col, ddl in _REQUIRED_COLUMNS:
        try:
            with engine.begin() as conn:
                exists = conn.execute(text(
                    'SELECT COUNT(*) FROM information_schema.columns '
                    'WHERE table_schema = DATABASE() AND table_name = :t '
                    'AND column_name = :c'), {'t': table, 'c': col}).scalar()
                if not exists:
                    conn.execute(text(
                        f'ALTER TABLE `{table}` ADD COLUMN `{col}` {ddl}'))
                    logger.info('[DB] 已补建存量表列: %s.%s', table, col)
        except Exception as e:
            logger.warning('[DB] 补建列失败 %s.%s（不影响启动）: %s', table, col, e)


def _ensure_extra_tables(engine):
    """补建后续迭代新增的表（checkfirst，已存在则零 DDL）"""
    from . import models  # noqa: F401  确保模型已注册到 Base.metadata
    tables = [Base.metadata.tables[name] for name in _NEW_TABLE_NAMES
              if name in Base.metadata.tables]
    if tables:
        Base.metadata.create_all(engine, tables=tables, checkfirst=True)


def _backfill_analysis_hour_slot(engine):
    """存量分析记录回填 hour_slot（取 ts 前 13 位 'YYYY-MM-DD HH'）。

    幂等：仅更新 hour_slot 为空的行，全部回填后后续启动为零影响 UPDATE。
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        result = conn.execute(text(
            "UPDATE task_analysis_records SET hour_slot = LEFT(ts, 13) "
            "WHERE (hour_slot IS NULL OR hour_slot = '') AND ts <> ''"))
        if result.rowcount:
            logger.info('[DB] 已回填存量分析记录 hour_slot: %s 行', result.rowcount)


def _column_exists(conn, table, col):
    """information_schema 检查列是否存在"""
    from sqlalchemy import text
    return bool(conn.execute(text(
        'SELECT COUNT(*) FROM information_schema.columns '
        'WHERE table_schema = DATABASE() AND table_name = :t '
        'AND column_name = :c'), {'t': table, 'c': col}).scalar())


def _migrate_calorie_diary_to_journal(conn):
    """把 calorie_records.diary 存量随笔迁移为独立随笔条目（幂等）

    - 每条非空 diary 转为 journal_notes 一条随笔，打『减肥随笔』标签，
      linked_from='calorie:{记录日期}' 回指热量记录，支持双向跳转；
    - 幂等去重：同 linked_from + 同正文已存在则跳过（重复执行不重复插入）。
    注：原始 SQL 插入需显式带上时间列（表列无数据库默认值）。
    """
    import uuid
    import datetime
    from sqlalchemy import text

    now_ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    rows = conn.execute(text(
        'SELECT `date`, `diary`, `created_at` FROM calorie_records '
        'WHERE diary IS NOT NULL AND TRIM(diary) <> \'\'')).all()
    if not rows:
        return 0

    # 确保『减肥随笔』标签存在（色值取色板中未被占用的第一个）
    tag_name, tag_color = '减肥随笔', '#16a085'
    if conn.execute(text(
            'SELECT COUNT(*) FROM journal_tags WHERE `name` = :n'),
            {'n': tag_name}).scalar() == 0:
        used = {r[0] for r in conn.execute(
            text('SELECT color FROM journal_tags')).all()}
        for c in ('#16a085', '#d81b60', '#5c6bc0', '#f39c12', '#00897b', '#8d6e63'):
            if c not in used:
                tag_color = c
                break
        conn.execute(text(
            'INSERT INTO journal_tags (`name`, `color`, `created_at`) '
            'VALUES (:n, :c, :ts)'),
            {'n': tag_name, 'c': tag_color, 'ts': now_ts})

    migrated = 0
    for rec_date, diary, created_at in rows:
        content = (diary or '').strip()
        if not content:
            continue
        linked_from = f'calorie:{rec_date}'
        dup = conn.execute(text(
            'SELECT COUNT(*) FROM journal_notes '
            'WHERE linked_from = :lf AND content = :c'),
            {'lf': linked_from, 'c': content}).scalar()
        if dup:
            continue
        ts = created_at or f'{rec_date} 20:00:00'
        note_id = f'note_{uuid.uuid4().hex[:8]}'
        conn.execute(text(
            'INSERT INTO journal_notes '
            '(id, `type`, content, pinned, distilled, linked_from, created_at, updated_at) '
            'VALUES (:id, \'note\', :c, 0, 0, :lf, :ts, :ts)'),
            {'id': note_id, 'c': content, 'lf': linked_from, 'ts': ts})
        conn.execute(text(
            'INSERT INTO note_tags (note_id, tag_name) VALUES (:id, :t)'),
            {'id': note_id, 't': tag_name})
        migrated += 1
    if migrated:
        logger.info('[DB] 已将 %d 条热量记录随笔迁移至随笔模块（标签：%s）',
                    migrated, tag_name)
    return migrated


def _drop_legacy_columns(engine):
    """废弃列退役：先迁移存量数据到新归宿，再删除列（幂等，列不存在即跳过）"""
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, col in _LEGACY_COLUMNS:
            if not _column_exists(conn, table, col):
                continue
            if table == 'calorie_records' and col == 'diary':
                _migrate_calorie_diary_to_journal(conn)
            conn.execute(text(f'ALTER TABLE `{table}` DROP COLUMN `{col}`'))
            logger.info('[DB] 已退役存量表废弃列: %s.%s', table, col)


def init_db(force_create=False):
    """幂等初始化（进程内只真正执行一次，重复调用直接返回）。

    - 默认模式：建表由 db_schema.sql 负责，这里仅做一次廉价连通性探活
      （SELECT 1）+ 缺失时补 meta 版本与随笔种子标签。避免 create_all
      逐表 SHOW CREATE TABLE 探测在远端 MySQL 上产生数十次串行往返。
    - force_create=True：仅迁移脚本/全新部署时使用，执行完整 create_all。
    """
    global _init_done
    if _init_done:
        return
    with _init_lock:
        if _init_done:
            return
        from . import models  # noqa: F401  确保全部模型注册到 Base.metadata

        engine = get_engine()
        if force_create:
            Base.metadata.create_all(engine)
        else:
            from sqlalchemy import text
            with engine.connect() as conn:
                conn.execute(text('SELECT 1'))
            # 存量库增量表/列/索引自动补齐 + 废弃列退役迁移（每进程一次，通常全部命中直接返回）
            # 顺序不可调换：索引可能建在增量列上（idx_tar_slot → hour_slot），
            # 必须先补列再补索引；每步独立兜底，任一步失败不得阻断后续步骤。
            for step in (_ensure_extra_tables, _ensure_extra_columns, _ensure_extra_indexes,
                         _backfill_analysis_hour_slot, _drop_legacy_columns):
                try:
                    step(engine)
                except Exception as e:
                    logger.warning(f'[DB] {step.__name__} 执行失败（不影响启动）: {e}')

        from sqlalchemy import select
        from .models import Meta, JournalTag

        with session_scope() as session:
            version = session.get(Meta, 'schema_version')
            if version is None:
                session.add(Meta(key='schema_version', value=SCHEMA_VERSION))
                session.flush()
            else:
                version.value = SCHEMA_VERSION
            init_ts = session.get(Meta, 'initialized_at')
            if init_ts is None:
                session.add(Meta(key='initialized_at',
                                 value=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

            # 随笔模块种子：标签表为空时预置 6 个默认标签（与 JSON 版 _default_data 一致）
            if session.execute(select(JournalTag.name).limit(1)).first() is None:
                from .journal_repo import DEFAULT_TAGS
                session.add_all([JournalTag(name=t['name'], color=t['color'])
                                 for t in DEFAULT_TAGS])
        _init_done = True
        logger.info('[DB] 初始化完成（schema %s）', SCHEMA_VERSION)


def warmup_async():
    """后台预热：应用启动时调用，在守护线程内完成 init_db。

    不阻塞 app.run()；失败只记日志，首个 DB 请求到达时会再次尝试。
    """
    def _warm():
        try:
            init_db()
        except Exception as e:
            logger.error(f'[DB] 后台预热失败（首个 DB 请求时将重试）: {e}', exc_info=True)

    threading.Thread(target=_warm, daemon=True, name='db-warmup').start()
