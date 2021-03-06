from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from threading import Thread
from time import sleep

from pytz import utc

from cryptowelder.context import CryptowelderContext, Metric


class MetricWelder:
    _ID = 'metric'
    _ONE = Decimal('1.0')
    _HALF = Decimal('0.5')
    _ZERO = Decimal('0.0')

    def __init__(self, context):
        self.__context = context
        self.__logger = context.get_logger(self)
        self.__thread = Thread(daemon=False, target=self._execute)

    def run(self):

        self.__thread.start()

    def _join(self):

        self.__thread.join()

    def _execute(self):

        self.__logger.info('Processing : %s', self._ID)

        threads = [
            Thread(target=self._wrap, args=(self.process_metric, 20)),
            Thread(target=self._wrap, args=(self.purge_metric, 3600)),
        ]

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        self.__logger.info('Terminated.')

    def _wrap(self, func, interval):

        while not self.__context.is_closed():

            try:

                func()

            except BaseException as e:

                self.__logger.warn('%s - %s : %s', func.__name__, type(e), e.args)

            sleep(interval)

    def process_metric(self, *, default_time=None, default_count=3):

        base_time = default_time if default_time is not None else self.__context.get_now()

        count = int(self.__context.get_property(self._ID, 'timestamp', default_count))

        timestamps = [base_time.replace(second=0, microsecond=0) - timedelta(minutes=i) for i in range(0, count)]

        self.__logger.debug('Metrics : %s', [t.strftime('%Y-%m-%d %H:%M') for t in timestamps])

        threads = []

        for timestamp in timestamps:
            prices = self.process_ticker(timestamp)
            threads.append(Thread(target=self.process_balance, args=(timestamp, prices)))
            threads.append(Thread(target=self.process_position, args=(timestamp, prices)))
            threads.append(Thread(target=self.process_transaction_trade, args=(timestamp, prices)))
            threads.append(Thread(target=self.process_transaction_volume, args=(timestamp, prices)))

        for t in threads:
            t.start()

        for t in threads:
            t.join()

    def process_ticker(self, timestamp):

        prices = None

        try:

            values = self.__context.fetch_tickers(timestamp, include_expired=True)

            prices = self._calculate_prices(values)

            metrics = []

            for dto in values if values is not None else []:

                metric = self._convert_ticker(timestamp, prices, dto)

                if metric is None:
                    continue

                metrics.append(metric)

            self.__context.save_metrics(metrics)

        except BaseException as e:

            self.__logger.warn('Ticker : %s : %s', type(e), e.args)

        return prices

    def _calculate_prices(self, tickers):

        prices = defaultdict(lambda: dict())

        for dto in tickers if tickers is not None else []:
            ticker = dto.ticker
            ask = ticker.tk_ask if ticker.tk_ask != self._ZERO else None
            bid = ticker.tk_bid if ticker.tk_bid != self._ZERO else None
            ltp = ticker.tk_ltp if ticker.tk_ltp != self._ZERO else None

            candidates = [p for p in (ask, bid, ltp) if p is not None]

            if len(candidates) >= 2:
                price = (candidates[0] + candidates[1]) * self._HALF
            else:
                price = candidates[0] if len(candidates) > 0 else None

            prices[ticker.tk_site][ticker.tk_code] = price

        return prices

    def _convert_ticker(self, timestamp, prices, dto):

        threshold_minutes = self.__context.get_property(self._ID, 'ticker_threshold', 3)

        threshold_cutoff = timestamp - timedelta(minutes=int(threshold_minutes))

        if dto.ticker.tk_time.replace(tzinfo=threshold_cutoff.tzinfo) < threshold_cutoff:
            return None

        price = prices.get(dto.ticker.tk_site, {}).get(dto.ticker.tk_code)

        if price is None or price == self._ZERO or dto.product is None:
            return None

        expiry = dto.product.pr_expr

        if expiry is not None and expiry.astimezone(timestamp.tzinfo) < timestamp:
            return None

        rate = self._calculate_evaluation(dto.fund, prices)

        if rate is None:
            return None

        metric = Metric()
        metric.mc_type = 'ticker'
        metric.mc_name = dto.product.pr_disp
        metric.mc_time = timestamp
        metric.mc_amnt = price * rate

        return metric

    def _calculate_evaluation(self, evaluation, prices):

        if evaluation is None:
            return None

        price = self._ONE

        if evaluation.ev_ticker_site is not None and evaluation.ev_ticker_code is not None:

            codes = prices.get(evaluation.ev_ticker_site)

            p = codes.get(evaluation.ev_ticker_code) if codes is not None else None

            if p is None or p == self._ZERO:
                return None

            price = price * p

        if evaluation.ev_convert_site is not None and evaluation.ev_convert_code is not None:

            codes = prices.get(evaluation.ev_convert_site)

            p = codes.get(evaluation.ev_convert_code) if codes is not None else None

            if p is None or p == self._ZERO:
                return None

            price = price * p

        return price

    def process_balance(self, timestamp, prices):

        try:

            metrics = []

            values = self.__context.fetch_balances(timestamp)

            for dto in values if values is not None else []:

                amount = dto.balance.bc_amnt

                rate = self._calculate_evaluation(dto.evaluation, prices)

                if dto.account is None or amount is None or rate is None:
                    continue

                metric = Metric()
                metric.mc_type = 'balance'
                metric.mc_name = dto.account.ac_disp
                metric.mc_time = timestamp
                metric.mc_amnt = amount * rate
                metrics.append(metric)

            self.__context.save_metrics(metrics)

        except BaseException as e:

            self.__logger.warn('Balance : %s : %s', type(e), e.args)

    def process_position(self, timestamp, prices):

        try:

            metrics = []

            values = self.__context.fetch_positions(timestamp)

            for dto in values if values is not None else []:

                amount = dto.position.ps_fund

                rate = self._calculate_evaluation(dto.fund, prices)

                if dto.product is None or amount is None or rate is None:
                    continue

                metric = Metric()
                metric.mc_type = 'position@upl'
                metric.mc_name = dto.product.pr_disp
                metric.mc_time = timestamp
                metric.mc_amnt = amount * rate
                metrics.append(metric)

            for dto in values if values is not None else []:

                amount = dto.position.ps_inst

                rate = self._calculate_evaluation(dto.inst, prices)

                if dto.product is None or amount is None or rate is None:
                    continue

                metric = Metric()
                metric.mc_type = 'position@qty'
                metric.mc_name = dto.product.pr_disp
                metric.mc_time = timestamp
                metric.mc_amnt = amount * rate
                metrics.append(metric)

            self.__context.save_metrics(metrics)

        except BaseException as e:

            self.__logger.warn('Position : %s : %s', type(e), e.args)

    def process_transaction_trade(self, timestamp, prices):

        try:

            metrics = []

            offset = timedelta(minutes=int(self.__context.get_property(self._ID, 'offset', 9 * 60)))

            t = timestamp + offset

            windows = {
                'DAY': t.replace(microsecond=0, second=0, minute=0, hour=0) - offset,
                'MTD': t.replace(microsecond=0, second=0, minute=0, hour=0, day=1) - offset,
                'YTD': t.replace(microsecond=0, second=0, minute=0, hour=0, day=1, month=1) - offset,
            }

            for key, val in windows.items():

                values = self.__context.fetch_transactions(val, timestamp)

                for dto in values if values is not None else []:

                    inst_qty = dto.tx_net_inst
                    fund_qty = dto.tx_net_fund

                    inst_rate = self._calculate_evaluation(dto.ev_inst, prices)
                    fund_rate = self._calculate_evaluation(dto.ev_fund, prices)

                    if dto.product is None \
                            or inst_qty is None or fund_qty is None \
                            or inst_rate is None or fund_rate is None:
                        continue

                    metric = Metric()
                    metric.mc_type = 'trade@' + key
                    metric.mc_name = dto.product.pr_disp
                    metric.mc_time = timestamp
                    metric.mc_amnt = (inst_qty * inst_rate) + (fund_qty * fund_rate)
                    metrics.append(metric)

            self.__context.save_metrics(metrics)

        except BaseException as e:

            self.__logger.warn('Transaction (trade) : %s : %s', type(e), e.args)

    def process_transaction_volume(self, timestamp, prices):

        try:

            metrics = []

            windows = {
                '12H': timestamp - timedelta(hours=12),
                '01D': timestamp - timedelta(hours=24),
                '30D': timestamp - timedelta(days=30),
            }

            for key, val in windows.items():

                values = self.__context.fetch_transactions(val, timestamp)

                for dto in values if values is not None else []:

                    amount = dto.tx_grs_fund

                    rate = self._calculate_evaluation(dto.ev_fund, prices)

                    if dto.product is None or amount is None or rate is None:
                        continue

                    metric = Metric()
                    metric.mc_type = 'volume@' + key
                    metric.mc_name = dto.product.pr_disp
                    metric.mc_time = timestamp
                    metric.mc_amnt = amount * rate
                    metrics.append(metric)

            self.__context.save_metrics(metrics)

        except BaseException as e:

            self.__logger.warn('Transaction (volume) : %s : %s', type(e), e.args)

    def purge_metric(self, *, intervals=(

            # [0] Older than 7+ years, delete all.
            (24 * 365 * 8, tuple()),

            # [1] Older than 48 hours, hourly interval.
            (48, tuple([0])),

            # [2] Older than 36 hours, 30 minutes interval.
            (36, tuple(i * 30 for i in range(0, 2))),

            # [3] Older than 24 hours, 15 minutes interval.
            (24, tuple(i * 15 for i in range(0, 4))),

            # [4] Older than 12 hours, 5 minutes interval.
            (12, tuple(i * 5 for i in range(0, 12))),

    )):

        now = self.__context.get_now()

        for idx, entry in enumerate(intervals):

            hours = self.__context.get_property(self._ID, 'purge_%s' % idx, entry[0])

            if hours is None:
                continue

            cutoff = now - timedelta(hours=max(int(hours), 1))

            count = self.__context.delete_metrics(cutoff, exclude_minutes=entry[1])

            self.__logger.debug('Purged [%s] cutoff=%s count=%s', idx, cutoff, count)


def main():
    context = CryptowelderContext(config='~/.cryptowelder', debug=True)
    context.launch_prometheus()

    target = MetricWelder(context)
    target.run()


def main_historical():
    context = CryptowelderContext(config='~/.cryptowelder', debug=True)
    context.launch_prometheus()

    target = MetricWelder(context)

    timestamp = context.get_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    while True:

        if timestamp >= datetime.now().astimezone(utc):
            break

        target.process_metric(default_time=timestamp, default_count=1)

        timestamp = timestamp + timedelta(minutes=60)


if __name__ == '__main__':
    main()
