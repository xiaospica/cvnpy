# -*- coding: utf-8 -*-
"""JoinQuant -> Redis Stream signal sender for vnpy_signal_strategy_plus.

The canonical downstream table is ``trade_signal_events``.  This script does not
write legacy MySQL signal rows.  ``pct`` means trade value divided by total
portfolio value, matching the strategy-side sizing contract.
"""

import hashlib
import json
from functools import wraps

import redis
from kuanke.user_space_api import *


PCT_SEMANTICS = "trade_value_pct_of_total_portfolio"


class JQRedisTrade:
    host = '106.54.63.204'
    port = 6888
    password = 'password'
    pattern = 1  # only Redis Stream is durable enough for the v2 signal journal
    mode = 0  # 0: backtest/debug also sends signals; 1: sim_trade only
    stream_maxlen = 100000

    @staticmethod
    def _next_signal_seq():
        seq = int(g.__dict__.get('__signal_seq', 0) or 0) + 1
        g.__dict__['__signal_seq'] = seq
        return seq

    @staticmethod
    def _signal_uid(strategy, source_signal_id, security, signal_type, remark, amt, pct, price):
        raw = '|'.join([
            str(strategy),
            str(source_signal_id),
            str(security),
            str(signal_type),
            str(remark),
            str(int(amt or 0)),
            str(pct),
            str(price),
        ])
        digest = hashlib.sha1(raw.encode('utf-8')).hexdigest()
        return 'jq:{}:{}:{}'.format(strategy, source_signal_id, digest[:16])

    @staticmethod
    def _to_stream_data(data):
        return {str(k): '' if v is None else str(v) for k, v in data.items()}

    @staticmethod
    def trade_signal(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            context = kwargs.get('context') or args[0]
            security = kwargs.get('security') or args[1]
            use_fixed_price = kwargs.get('use_fixed_price')

            pre_cash = float(context.portfolio.available_cash)
            total_value = float(context.portfolio.total_value or 0)
            pre_amt = 0
            if security in context.portfolio.positions:
                pre_amt = int(context.portfolio.positions[security].total_amount)

            my_order = func(*args, **kwargs)
            if my_order is None:
                return my_order

            empty = 0
            if my_order.is_buy:
                new_cash = float(context.portfolio.available_cash)
                new_amt = int(context.portfolio.positions[security].total_amount)
                amt = max(new_amt - pre_amt, 0)
                pct = round((pre_cash - new_cash) / total_value, 4) if total_value > 0 else 0
            else:
                if security not in context.portfolio.positions:
                    new_amt = 0
                else:
                    new_amt = int(context.portfolio.positions[security].total_amount)
                new_cash = float(context.portfolio.available_cash)
                amt = max(pre_amt - new_amt, 0)
                pct = round((new_cash - pre_cash) / total_value, 4) if total_value > 0 else 0
                empty = 1 if not new_amt else 0

            signal_type = 'BUY_FIXED' if my_order.is_buy and use_fixed_price else None
            if signal_type is None:
                signal_type = 'SELL_FIXED' if (not my_order.is_buy and use_fixed_price) else None
            if signal_type is None:
                signal_type = 'BUY_LST' if my_order.is_buy else 'SELL_LST'

            price = get_current_data()[security].last_price
            remark = my_order.add_time.strftime('%Y-%m-%d %H:%M:%S')
            strategy = str(g.strategy)
            seq = JQRedisTrade._next_signal_seq()
            source_signal_id = '{}:{}:{}'.format(strategy, remark, seq)
            signal_uid = JQRedisTrade._signal_uid(
                strategy,
                source_signal_id,
                security,
                signal_type,
                remark,
                amt,
                pct,
                price,
            )

            data = {
                'source': 'joinquant',
                'source_signal_id': source_signal_id,
                'signal_uid': signal_uid,
                'code': security,
                'pct': pct,
                'pct_semantics': PCT_SEMANTICS,
                'amt': int(amt),
                'type': signal_type,
                'price': price,
                'stg': strategy,
                'remark': remark,
                'empty': empty,
                'portfolio_total_value': total_value,
                'available_cash_before': pre_cash,
                'available_cash_after': float(context.portfolio.available_cash),
                'position_amount_before': int(pre_amt),
                'position_amount_after': int(new_amt),
            }

            log.info('order cmd: {}'.format(data))

            if context.run_params.type == 'sim_trade' or JQRedisTrade.mode == 0:
                try:
                    rds = JQRedisTrade._open()
                    if JQRedisTrade.pattern != 1:
                        raise RuntimeError('JQRedisTrade.pattern must be 1 for Redis Stream')
                    rds.xadd(
                        strategy,
                        JQRedisTrade._to_stream_data(data),
                        maxlen=JQRedisTrade.stream_maxlen,
                        approximate=True,
                    )
                except Exception as e:
                    log.error(repr(e))

            return my_order

        return wrapper

    @staticmethod
    def _open():
        if hasattr(g, 'rds_connected') and g.rds_connected and g.__dict__.get('__redis'):
            return g.__dict__.get('__redis')

        pool = redis.ConnectionPool(
            host=JQRedisTrade.host,
            port=JQRedisTrade.port,
            password=JQRedisTrade.password,
        )
        rds = redis.Redis(connection_pool=pool)
        rds.auto_close_connection_pool = True

        g.__dict__.update({'__redis': rds})
        g.rds_connected = True

        return rds

    @staticmethod
    def close():
        if hasattr(g, 'rds_connected') and (not g.rds_connected):
            return
        try:
            rds = g.__dict__.get('__redis')
            g.__dict__.update({'__redis': None})
            if rds:
                rds.connection_pool.disconnect()
        except Exception as e:
            log.error(repr(e))
        finally:
            g.rds_connected = False


@JQRedisTrade.trade_signal
def order_(context, security, amount, use_fixed_price=False, style=None):
    _order = order(security, amount, style)
    return _order


@JQRedisTrade.trade_signal
def order_target_(context, security, amount, use_fixed_price=False, style=None):
    _order = order_target(security, amount, style)
    return _order


@JQRedisTrade.trade_signal
def order_value_(context, security, value, use_fixed_price=False, style=None):
    if value > 0 and value < get_current_data()[security].last_price * 100:
        print('仓位不足: {}'.format((security, value)))
        return None
    _order = order_value(security, value, style)
    return _order


@JQRedisTrade.trade_signal
def order_target_value_(context, security, value, use_fixed_price=False, style=None):
    if value > 0 and value < get_current_data()[security].last_price * 100:
        print('仓位不足: {}'.format((security, value)))
        return None
    _order = order_target_value(security, value, style)
    return _order
