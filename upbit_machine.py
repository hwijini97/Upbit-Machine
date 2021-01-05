# -*- coding: utf-8 -*-

import configparser
import requests
import time
import pymysql
from threading import Thread
import jwt
import platform
import os
import traceback
from urllib.parse import urlencode


# 주석 추가
class UpbitMachine:
    BASE_API_URL = "https://api.upbit.com/v1/"
    trading = False  # 현재 거래중인지 나타냄 -> 동시에 여러 거래가 이루어지는 것을 방지
    wallet = None  # 지갑 저장할 변수
    conn = None  # MySQL 접속 정보
    curs = None  # MySQL 질의 결과가 담길 변수
    print_list = []  # 거래가 진행되는 상황을 저장하는 변수 (버퍼 역할)
    check_orderbook_before_start = 0  # 거래 사이클 시작 직전에 호가가 바뀌었는지 확인하고 거래할거면 True, 아니면 False
    ret = False  # True이면 모든 스레드 강제 종료
    markets_str = ""  # 거래할 모든 코인들의 시장-코인{, 시장-코인} 형태의 문자열, ex) "KRW-XRP, KRW-QTUM, KRW-BCH, ..."
    calc_profit_interval = 0.01  # calc_profit의 호가를 불러오는 과정에서 어느 정도의 딜레이를 줄지 -> too_many_requests 방지

    ALL_COIN = [
        "ADT", "BCH", "BSV", "RFR", "TRX", "GRS", "MFT", "ADA",
        "XRP", "QTUM", "ADX", "STORM", "POWR", "LTC", "UPP", "MANA",
        "LOOM", "SC", "REP", "GTO", "XEM", "IGNIS", "EDR",
        "GNT", "SBD", "STRAT", "ARK", "STEEM", "EMC2", "XLM", "EOS",
        "SRN", "KMD", "WAVES", "LSK", "STORJ", "DCR", "MCO", "ZRX",
        "OMG", "SNT", "ETC", "BAT", "VTC", "DMT", "SOLVE",
        "BTT", "COSM", "NPXS", "ENJ", "ATOM", "MTL", "TTC", "AERGO",
        "CPT", "ARDR", "ELF", "CVC", "MOC", "ANKR", "TSHP"
    ]

    # 각 시장에서 최근 몇 개의 i번째 X 코인의 매도, 매수 호가 및 개수가 담겨있음
    # coin_price[순서][시장 이름][n번째 호가][코인 번호][매도 또는 매수 호가 또는 크기]
    # 순서 = 0 일수록 예전의 호가, len(coin_price)-1에 가까울수록 현재의 호가
    coin_price = []

    # market_price[0] : KRW 시장에서 BTC 코인의 첫 번째 매도, 매수 호가 및 개수가 담겨있음
    market_price = [{'ask_price': 0, 'bid_price': 0, "ask_size": 0, "bid_size": 0}]

    market = [['error', 'error', 'error', 'error'],
              ['BTC', 'KRW', 'KRW', 'BTC'],
              ['KRW', 'BTC', 'KRW', 'BTC']]

    order_type = [['error', 'error', 'error'],
                  ['bid', 'ask', 'bid'],
                  ['bid', 'ask', 'ask']]

    price_type = [['error', 'error', 'error'],
                  ['ask_price', 'bid_price', 'ask_price'],
                  ['ask_price', 'bid_price', 'bid_price']]

    fee = [0, 1.0025, 0.9975]

    def __init__(self):
        self.before = time.time()
        """ config.ini 파일에서 정보 불러오는 부분 """
        config = configparser.ConfigParser()
        config.read('config.ini', encoding='utf-8-sig')
        self.access_key = config['UPBIT']['access_key']
        self.secret_key = config['UPBIT']['secret_key']
        self.machine_location = str(config['MACHINE']['machine_location'])
        self.how_many_coins = len(self.ALL_COIN)
        self.ip = str(config['MACHINE']['ip'])
        self.port = int(config['MACHINE']['port'])
        self.profit = float(config['MACHINE']['profit'])
        self.print_elapsed_time_delay = int(config['MACHINE']['print_elapsed_time_delay'])
        self.reboot_time = int(config['MACHINE']['reboot_time'])
        self.maximum_by_bitcoin = float(config['MACHINE']['maximum_by_bitcoin'])
        self.minimum_by_bitcoin = float(config['MACHINE']['minimum_by_bitcoin'])
        self.maximum_error_price = int(config['MACHINE']['maximum_error_price'])
        self.minimum_error_price = int(config['MACHINE']['minimum_error_price'])
        self.trade_if_rising = int(config['MACHINE']['trade_if_rising'])
        self.trade_if_low_orderbook_difference = int(config['MACHINE']['trade_if_low_orderbook_difference'])
        self.orderbook_difference_rate = float(config['MACHINE']['orderbook_difference_rate'])
        self.orderbook_check_interval = int(config['MACHINE']['orderbook_check_interval'])
        self.check_orderbook_before_start = int(config['MACHINE']['check_orderbook_before_start'])
        self.calc_profit_interval = float(config['MACHINE']['calc_profit_interval'])

        """ 초기 지갑 불러오기 """
        self.initial_wallet = self.get_my_wallet()
        self.initial_krw_balance = 0
        self.initial_btc_balance = 0
        for j in range(0, len(self.initial_wallet)):
            if self.initial_wallet[j]['currency'] == 'KRW':
                self.initial_krw_balance = float(self.initial_wallet[j]['balance'])
            if self.initial_wallet[j]['currency'] == 'BTC':
                self.initial_btc_balance = float(self.initial_wallet[j]['balance'])
        self.wallet = self.get_my_wallet()

        """ MySQL DB 연결 """
        self.conn = pymysql.connect(host=self.ip, port=self.port, user='root', password='root', db='coin_transaction', charset='utf8')
        self.curs = self.conn.cursor()

        """ 주기적으로 지갑 불러오는 스레드 시작 """
        wallet_thread = Thread(target=self.get_my_wallet_periodically)
        wallet_thread.start()

    def set_trade_coins(self):
        sorted_coin = [{} for _ in range(0, len(self.ALL_COIN))]
        self.coin_price = []

        """ 거래 가능한 코인들의 캔들 정보를 불러옴 """
        for i in range(0, len(sorted_coin)):
            candle = self.get_candle("KRW", self.ALL_COIN[i], 60)
            sorted_coin[i]["coin"] = self.ALL_COIN[i]
            sorted_coin[i]["acc_price"] = candle["candle_acc_trade_price"]
            time.sleep(0.05)

        """ 내림차순 정렬 """
        for i in range(0, len(sorted_coin)):
            for j in range(i + 1, len(sorted_coin)):
                if sorted_coin[i]["acc_price"] < sorted_coin[j]["acc_price"]:
                    temp_coin = sorted_coin[i]["coin"]
                    temp_acc_price = sorted_coin[i]["acc_price"]
                    sorted_coin[i]["coin"] = sorted_coin[j]["coin"]
                    sorted_coin[i]["acc_price"] = sorted_coin[j]["acc_price"]
                    sorted_coin[j]["coin"] = temp_coin
                    sorted_coin[j]["acc_price"] = temp_acc_price

        """ 거래량이 많은 코인 순으로 how_many_coins개의 코인을 거래할 코인에 넣음"""
        for i in range(0, len(self.ALL_COIN)):
            self.ALL_COIN[i] = sorted_coin[i]["coin"]

        """ 거래할 모든 코인들의 시장-코인{, 시장-코인} 형태의 문자열을 구함 """
        self.markets_str = self.get_markets_str()

        """ 각 코인별로 호가를 불러옴 """
        self.get_coin_orderbook()

        """ 출력 """
        print("거래할 코인 : ", end="")
        for i in range(0, len(self.ALL_COIN)):
            print(self.ALL_COIN[i], end="")
            if i != len(self.ALL_COIN) - 1:
                print(", ", end="")
        print("")

    def get_markets_str(self):
        markets_str = ""
        for i in range(0, len(self.ALL_COIN)):
            markets_str = markets_str + ", KRW-" + self.ALL_COIN[i]
            markets_str = markets_str + ", BTC-" + self.ALL_COIN[i]
        return markets_str[2:]

    @staticmethod
    def get_time_str():
        t = time.localtime()
        year = str(t.tm_year)

        if t.tm_mon < 10:
            month = "0" + str(t.tm_mon)
        else:
            month = str(t.tm_mon)

        if t.tm_mday < 10:
            day = "0" + str(t.tm_mday)
        else:
            day = str(t.tm_mday)

        if t.tm_hour < 10:
            hour = "0" + str(t.tm_hour)
        else:
            hour = str(t.tm_hour)

        if t.tm_min < 10:
            minute = "0" + str(t.tm_min)
        else:
            minute = str(t.tm_min)

        if t.tm_sec < 10:
            second = "0" + str(t.tm_sec)
        else:
            second = str(t.tm_sec)

        time_str = year + "-" + month + "-" + day + "-" + hour + "-" + minute + "-" + second
        return time_str

    @staticmethod
    def get_nonce():
        raw_time = str(time.time())
        if len(raw_time) == 12:
            raw_time = raw_time + "0"
        nonce = raw_time[2:10] + raw_time[11:13]
        return nonce

    def api_query(self, authorization=False, path=None, method='get', query_params=None):
        with requests.Session() as s:
            try:
                headers = {'User-Agent': platform.platform()}
                url = '{0:s}{1:s}'.format(self.BASE_API_URL, path)
                if authorization:
                    payload = {
                        'access_key': self.access_key,
                        'nonce': str(self.get_nonce())
                    }
                    if query_params is not None:
                        payload['query'] = query_params
                        url = '{0:s}?{1:s}'.format(url, query_params)
                    token = jwt.encode(payload, self.secret_key, algorithm='HS256')
                    headers['Authorization'] = 'Bearer {0:s}'.format(token.decode('utf-8'))
                    req = requests.Request(method, url, headers=headers)
                else:
                    req = requests.Request(method, url, headers=headers, params=query_params)
                prepped = s.prepare_request(req)
                response = s.send(prepped)
                temp = response.json()
                if "error" in temp:
                    if temp["error"]["name"] == "insufficient_funds_bid":
                        return None
                    elif temp["error"]["name"] == "under_min_total_ask":
                        return None
                    elif temp["error"]["name"] == "nonce_used":
                        time.sleep(1)
                        print(response.content.decode('utf-8'))
                        return self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                    elif temp["error"]["name"] == "too_many_requests":
                        time.sleep(1)
                        return self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                    elif temp["error"]["name"] == "server_error":
                        time.sleep(1)
                        print(response.content.decode('utf-8'))
                        return self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                    elif temp["error"]["name"] == "internal_server_error":  # 그냥 어쩌다 한 번씩 나오는 오류
                        time.sleep(1)
                        return self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                    elif temp["error"]["name"] == "order_not_found":  # 주문을 너무 빨리 가져오는 경우
                        time.sleep(0.1)
                        if method == 'delete':  # 취소 주문 과정에서 주문을 찾을 수 없다고 나오는 경우 -> 이미 체결된 상황
                            return None
                        print(response.content.decode('utf-8'))
                        return self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                    elif temp["error"]["name"] == "invalid_funds_ask":  # 비정상적인 매개변수로 주문을 넣은 경우
                        print(response.content.decode('utf-8'))
                        return None
                    elif temp["error"]["name"] == "market_offline":  # 시스템 점검 중인 경우
                        print("시스템 점검 중이므로 30초 후 거래를 다시 시도합니다.")
                        time.sleep(30)
                        return self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                    else:
                        print(temp)
                if path == "orders":
                    if response.status_code == 504:  # 504 Gateway Time-out
                        s.close()
                        time.sleep(1)
                        print(response.content.decode('504 Gateway Time-out'))
                        self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                if response.status_code is not 200 and response.status_code is not 201:
                    print(query_params)
                    print(response.content.decode('utf-8'))
                return response.json() if response.status_code is 200 or response.status_code is 201 else None
            except requests.ConnectionError:
                print("ConnectionError")
                return None
            except Exception as e:
                print(repr(e))
            finally:
                s.close()

    def get_orderbook(self, markets):
        if markets is None:
            raise Exception("No Market")
        query_params = urlencode({"markets": markets})
        try:
            res = self.api_query(authorization=False, path='orderbook', method='get', query_params=query_params)
            if "orderbook_units" in res[0]:
                return res
        except requests.exceptions.ConnectionError:
            return -1
        except TypeError:
            return -1
        except IndexError:
            return -1
        return -1

    def get_ticker(self, trade_market=None, coin=None):
        if trade_market is None or coin is None:
            raise Exception("No Market or Coin")
        currency_type = trade_market + "-" + coin
        query_params = urlencode({"markets": currency_type})
        try:
            res = self.api_query(authorization=False, path='ticker', method='get', query_params=query_params)
            if "change" in res[0]:
                return res[0]
        except requests.exceptions.ConnectionError:
            return -1
        except TypeError:
            return -1
        return -1

    def get_candle(self, trade_market=None, coin=None, unit=-1):
        if trade_market is None or coin is None or unit < 0:
            raise Exception("Need to set params")
        if unit != 1 and unit != 3 and unit != 5 and unit != 10 and unit != 15 and unit != 30 and unit != 60 and unit != 240:
            raise Exception("올바른 분 단위를 입력해주세요")
        currency_type = trade_market + "-" + coin
        query_params = urlencode({"market": currency_type})
        try:
            res = self.api_query(authorization=False, path='candles/minutes/' + str(unit), method='get', query_params=query_params)
            if "candle_acc_trade_price" in res[0]:
                return res[0]
        except requests.exceptions.ConnectionError:
            return -1
        except TypeError:
            return -1
        return -1

    def get_my_wallet(self):
        res = self.api_query(authorization=True, path='accounts', method='get')
        while res is None:
            if self.ret is True:
                return
            time.sleep(0.1)
            res = self.api_query(authorization=True, path='accounts', method='get')
        return res

    def get_volume(self, coin_num):
        i = 0
        while i < 10:
            if self.ret is True:
                return -1
            i = i + 1
            wallet = self.get_my_wallet()
            for j in range(0, len(wallet)):
                if wallet[j]['currency'] == self.ALL_COIN[coin_num]:
                    if wallet[j]['balance'] != 0.0:
                        return wallet[j]['balance']
                    else:
                        if wallet[j]['locked'] != 0.0:
                            self.cancel_all_order()
                            i = i - 1
                        break
        return 0.0

    def get_order(self, uuid, count=10):
        query_params = urlencode({'uuid': uuid})
        res = self.api_query(authorization=True, path='order', method='get', query_params=query_params)
        if res == -1:
            print("get_order에서 그냥 -1이 반환됨, uuid = " + uuid)
            return -1
        i = 0
        while True:
            if self.ret is True:
                print("get_order에서 ret = True가 반환됨, uuid = " + uuid)
                print("i = " + str(i) + ", count = " + str(count))
                print(res)
                return -1
            if i >= count:
                return res
            if res is None:
                print("get_order에서 1이 반환됨, uuid = " + uuid)
                return 1
            if res["state"] == "done" or res["state"] == "cancel":
                return res
            else:
                res = self.api_query(authorization=True, path='order', method='get', query_params=query_params)
                time.sleep(0.1)
                i = i + 1

    def get_order_list(self, market=None):
        if market is None:
            query_params = urlencode({'state': "wait"})
        else:
            query_params = urlencode({'state': "wait",
                                      'market': market})
        res = self.api_query(authorization=True, path='orders', method='get', query_params=query_params)
        return res

    def place_order(self, trade_market=None, coin=None, side="ask", volume=0.0, price=0, ord_type='limit'):
        market = trade_market + "-" + coin
        query_params = urlencode({'market': market,
                                  'side': side,
                                  'volume': volume,
                                  'price': price,
                                  'ord_type': ord_type})
        res = self.api_query(authorization=True, path='orders', method='post', query_params=query_params)

        if res is None:  # insufficient_funds_bid 오류
            self.cancel_all_order()
            res = self.api_query(authorization=True, path='orders', method='post', query_params=query_params)
            if res is None:  # 주문 모두 취소했는데도 오류가 생기면 진짜 돈이 부족하다고 판단하고 오류 처리
                return None
        return res["uuid"]

    """ uuid에 해당하는 주문을 취소하고 그 주문에 대한 내역을 반환 """
    def cancel_order(self, uuid, count=2):
        query_params = urlencode({'uuid': uuid})
        self.api_query(authorization=True, path='order', method='delete', query_params=query_params)
        order = self.get_order(uuid=uuid, count=count)
        while order['state'] != "cancel" and order['state'] != "done":
            self.api_query(authorization=True, path='order', method='delete', query_params=query_params)
            order = self.get_order(uuid=uuid, count=count)
            count = count + 1
        return order

    """ 진행 중인 모든 주문을 취소 """
    def cancel_all_order(self, market=None):
        order_list = self.get_order_list(market)
        for i in range(0, len(order_list)):
            self.cancel_order(uuid=order_list[i]["uuid"])

    """ 각 시장에서의 코인의 매수, 매도 호가를 불러와서 저장함 """
    def get_coin_orderbook(self):
        temp = {'KRW': [[{'ask_price': 0, 'bid_price': 0, "ask_size": 0, "bid_size": 0} for _ in range(70)], [{'ask_price': 0, 'bid_price': 0, "ask_size": 0, "bid_size": 0} for _ in range(70)]],
                'BTC': [[{'ask_price': 0, 'bid_price': 0, "ask_size": 0, "bid_size": 0} for _ in range(70)], [{'ask_price': 0, 'bid_price': 0, "ask_size": 0, "bid_size": 0} for _ in range(70)]]}
        orderbook = self.get_orderbook(self.markets_str)
        if orderbook == -1:
            return -1
        result_index = -1
        for i in range(0, len(self.ALL_COIN)):
            result_index = result_index + 1
            temp['KRW'][0][i] = orderbook[result_index]["orderbook_units"][0]
            temp['KRW'][1][i] = orderbook[result_index]["orderbook_units"][1]
            result_index = result_index + 1
            temp['BTC'][0][i] = orderbook[result_index]["orderbook_units"][0]
            temp['BTC'][1][i] = orderbook[result_index]["orderbook_units"][1]

        """ 각 코인의 호가 변동 내역을 저장함 """
        if len(self.coin_price) >= self.orderbook_check_interval:
            del self.coin_price[0]
        self.coin_price.append(temp)

    """ 각 시장 간의 매수, 매도 호가를 불러와서 저장함 """
    def get_market_orderbook(self):
        orderbook = self.get_orderbook("KRW-BTC")
        if orderbook == -1:
            return -1
        self.market_price[0] = orderbook[0]["orderbook_units"][0]

    """ 주기적으로 지갑을 불러옴 -> 정확한 가격 계산을 위함 """
    def get_my_wallet_periodically(self):
        while True:
            if self.ret is True:
                return
            if self.trading is False:
                self.wallet = self.get_my_wallet()
                time.sleep(20)
            else:
                time.sleep(5)

    """ 거래가 시작하고 얼마나 지났는지 확인함. 한 거래에서 너무 많이 시간을 잡아먹는 걸 확인하기 위한 용도 """

    def count_time(self):
        t = 0
        normal = True
        while True:
            time.sleep(1)
            while self.trading is True:
                normal = False
                time.sleep(1)
                t = t + 1
                if t % self.print_elapsed_time_delay == 0:
                    print('거래가 시작된지 ' + str(t) + '초 지났습니다. ' + str(self.reboot_time) + '초 이상 지속되면 프로그램을 재시작합니다.')
                if t > self.reboot_time:  # 거래가 너무 오래 지속되면 오류로 간주하고 프로그램을 재시작함
                    os.startfile(self.machine_location)
                    self.ret = True
                    time.sleep(2)
                    print(''.join(self.print_list))
                    print("오류때문에 프로그램 하나 더 켜졌으니까 휘진이한테 오류 발생했다고 말해주세요! 디버깅을 위해 이 프로그램은 꺼지지 않습니다.")
                    time.sleep(500000)
                    return
            if normal is False:
                normal = True
                t = 0

    """ a 시장에서 x 코인을 사고, b 시장에서 x 코인을 팔고, BTC나 ETH를 사거나 파는 경우의 수익률을 구함 """

    @staticmethod
    def calc_profit_of_cycle(a_market_x_orderbook=None, b_market_x_orderbook=None, market_by_market_orderbook=None, num=None):
        if a_market_x_orderbook is None or b_market_x_orderbook is None or market_by_market_orderbook is None or num is None:
            raise Exception("You need to set params")
        try:
            if num == 1:
                profit = 1 / a_market_x_orderbook["ask_price"] * b_market_x_orderbook["bid_price"] / market_by_market_orderbook["ask_price"]
            else:
                profit = 1 / a_market_x_orderbook["ask_price"] * b_market_x_orderbook["bid_price"] * market_by_market_orderbook["bid_price"]
        except ZeroDivisionError:
            t = time.localtime()
            print('\n현재시각 : {}년 {}월 {}일 {}시 {}분 {}초,'.format(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec), a_market_x_orderbook, "@@@", b_market_x_orderbook, "@@@", market_by_market_orderbook)
            return -1
        return profit * 0.996502749375

    @staticmethod
    def calc_profit_resell(bid_price, ask_price, num):
        if num == 2:
            return (ask_price * 0.9995) / (bid_price * 1.0005)
        else:
            return (ask_price * 0.9975) / (bid_price * 1.0025)

    """ 단위를 i번째 코인의 단위로 변환함 """

    def get_x_coin_volume(self, i=-1, num=None, order_volume=None):
        if num == 1:
            return order_volume / self.coin_price[len(self.coin_price) - 1]['BTC'][0][i]["ask_price"]
        elif num == 2:
            return order_volume / self.coin_price[len(self.coin_price) - 1]['BTC'][0][i]["bid_price"]

    """ 최적 주문 개수를 구함 """

    def get_optimal_volume(self, i=-1, num=None):
        if num is None:
            raise Exception("you need to set param")
        if num == 1:
            return min(float(self.coin_price[len(self.coin_price) - 1]['BTC'][0][i]["ask_price"] * self.coin_price[len(self.coin_price) - 1]['BTC'][0][i]["ask_size"]),
                       (self.coin_price[len(self.coin_price) - 1]['KRW'][0][i]["bid_price"] * self.coin_price[len(self.coin_price) - 1]['KRW'][0][i]["bid_size"] / self.market_price[0]["ask_price"]))
        elif num == 2:
            return min(self.coin_price[len(self.coin_price) - 1]['KRW'][0][i]["ask_size"] * self.coin_price[len(self.coin_price) - 1]['BTC'][0][i]["bid_price"],
                       (self.coin_price[len(self.coin_price) - 1]['BTC'][0][i]["bid_size"]) * self.coin_price[len(self.coin_price) - 1]['BTC'][0][i]["bid_price"])

    """ 최적 주문 개수를 실제 주문할 개수로 변환함 """

    def get_order_volume(self, optimal_volume=0.01):
        val = optimal_volume * 0.8
        if val < self.minimum_by_bitcoin:
            return -1
        if val >= self.maximum_by_bitcoin:
            return self.maximum_by_bitcoin
        else:
            return val

    """ 원화 마켓 주문 가격 단위에 맞게 가격을 변환함 """

    @staticmethod
    def get_correct_krw_price(price=-1):
        if price == -1:
            raise Exception("Need param")
        if 0 <= price < 10:
            return price - price % 0.01
        elif 10 <= price < 100:
            return price - price % 0.1
        elif 100 <= price < 1000:
            return price - price % 1
        elif 1000 <= price < 10000:
            return price - price % 5
        elif 10000 <= price < 100000:
            return price - price % 10
        elif 100000 <= price < 500000:
            return price - price % 50
        elif 500000 <= price < 1000000:
            return price - price % 100
        elif 1000000 <= price < 2000000:
            return price - price % 500
        else:
            return price - price % 1000

    """ 저장된 호가를 통해 i번째 코인이 각 사이클에서 수익을 낼 수 있는지 계산하고 수익을 낼 수 있는 사이클이 있으면 거래를 시작함 """

    def calc_profit(self):
        while True:
            if self.ret is True:
                return

            if self.get_coin_orderbook() == -1:  # 각 코인의 호가를 불러옴 (krw 시장에서의 xrp, xem, srn 등의 가격)
                break
            if self.get_market_orderbook() == -1:  # 각 시장 간의 호가를 불러옴 (krw 시장에서의 btc의 가격)
                break

            for i in range(0, len(self.ALL_COIN)):
                """ KRW <-> BTC """
                # profit_btc_krw = self.calc_profit_of_cycle(self.coin_price[len(self.coin_price) - 1]['BTC'][0][i], self.coin_price[len(self.coin_price) - 1]['KRW'][0][i], self.market_price[0], 1)
                profit_krw_btc = self.calc_profit_of_cycle(self.coin_price[len(self.coin_price) - 1]['KRW'][0][i], self.coin_price[len(self.coin_price) - 1]['BTC'][0][i], self.market_price[0], 2)

                """ 몇 번째 사이클이 최대의 수익률을 낼 수 있는지 확인 """
                """
                max_profit = profit_btc_krw
                max_profit_cycle_num = 1

                if profit_krw_btc > max_profit:
                    max_profit = profit_krw_btc
                    max_profit_cycle_num = 2
                """
                max_profit = profit_krw_btc
                max_profit_cycle_num = 2

                # print(str(i) + " : " + self.ALL_COIN[i] + " 코인의 최적 거래 사이클 번호 : " + str(max_profit_cycle_num) + "번, 예상 수익률 : " + str(max_profit))

                if self.profit <= max_profit:

                    """ 두 번째 거래에서 거래할 코인의 가격이 상승세가 아니면 거래하지 않음 """
                    if self.trade_if_rising == 1:
                        if self.coin_price[len(self.coin_price) - 1][self.market[max_profit_cycle_num][1]][0][i][self.price_type[max_profit_cycle_num][1]] <= self.coin_price[0][self.market[max_profit_cycle_num][1]][0][i][self.price_type[max_profit_cycle_num][1]]:
                            # print(self.market[max_profit_cycle_num][0] + '시장에서 ' + self.ALL_COIN[i] + '코인의 가격이 상승세가 아니므로 거래를 하지 않습니다. 얼마 전 가격 : ' + str(self.coin_price[0][self.market[max_profit_cycle_num][0]][0][i][self.price_type[max_profit_cycle_num][0]]) + ', 현재 가격 : ' + str(self.coin_price[len(self.coin_price)-1][self.market[max_profit_cycle_num][0]][0][i][self.price_type[max_profit_cycle_num][0]]))
                            continue
                        if self.coin_price[0][self.market[max_profit_cycle_num][0]][0][i][self.price_type[max_profit_cycle_num][0]] < self.coin_price[len(self.coin_price) - 1][self.market[max_profit_cycle_num][0]][0][i][self.price_type[max_profit_cycle_num][0]]:
                            continue

                    """ 매수 매도 호가의 차이가 많이 나면 거래를 안 함 """
                    if self.trade_if_low_orderbook_difference == 1:
                        orderbook_difference = self.coin_price[len(self.coin_price) - 1][self.market[max_profit_cycle_num][0]][0][i]["ask_price"] / self.coin_price[len(self.coin_price) - 1][self.market[max_profit_cycle_num][0]][0][i]["bid_price"]
                        if orderbook_difference > self.orderbook_difference_rate:
                            # print(self.market[max_profit_cycle_num][0] + '시장에서 ' + self.ALL_COIN[i] + '코인의 매수 매도 호가의 차이가 많이 나므로 거래를 하지 않습니다. 매도 호가 : ' + str(self.coin_price[len(self.coin_price)-1][self.market[max_profit_cycle_num][0]][0][i]["ask_price"]) + ', 매수 호가 : ' + str(self.coin_price[len(self.coin_price)-1][self.market[max_profit_cycle_num][0]][0][i]["bid_price"]))
                            continue

                    optimal_volume = self.get_optimal_volume(i=i, num=max_profit_cycle_num)
                    order_volume = self.get_order_volume(optimal_volume=optimal_volume)  # 비트 기준
                    if order_volume != -1:
                        self.print_list.clear()
                        self.trading = True
                        t = time.localtime()
                        self.print_list.append('----------------------------------------------------------------------------------------------------------------------------------------\n')
                        self.print_list.append('현재시각 : {}년 {}월 {}일 {}시 {}분 {}초  '.format(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec) + self.ALL_COIN[i] + " 코인의 최적 거래 사이클 번호 : " + str(max_profit_cycle_num) + "번, 예상 수익률 : " + str(round(max_profit, 4)) + ", 최적 거래 개수 : " + str(round(optimal_volume, 10)) + "\n")

                        x_coin_volume = self.get_x_coin_volume(num=max_profit_cycle_num, i=i, order_volume=order_volume)

                        result = -1
                        try:
                            result = self.trade_cycle(cycle_num=max_profit_cycle_num, volume=x_coin_volume, coin_num=i)
                        except Exception as ex:
                            print('오류가 발생하여 거래가 중지되었습니다.')
                            print(repr(ex))
                            print(''.join(self.print_list))
                            traceback.print_exc()
                        time.sleep(0.5)

                        for j in range(0, len(self.coin_price)):
                            print(self.coin_price[j][self.market[max_profit_cycle_num][1]][0][i][self.price_type[max_profit_cycle_num][1]])

                        """ 초기 지갑 내역 불러오기 """
                        krw_balance = 0
                        btc_balance = 0
                        for j in range(0, len(self.wallet)):
                            if self.wallet[j]['currency'] == 'KRW':
                                krw_balance = float(self.wallet[j]['balance'])
                            if self.wallet[j]['currency'] == 'BTC':
                                btc_balance = float(self.wallet[j]['balance'])

                        """ 거래 후 지갑내역 불러오기 """
                        self.wallet = self.get_my_wallet()
                        krw_balance2 = 0
                        btc_balance2 = 0
                        for j in range(0, len(self.wallet)):
                            if self.wallet[j]['currency'] == 'KRW':
                                krw_balance2 = float(self.wallet[j]['balance'])
                            if self.wallet[j]['currency'] == 'BTC':
                                btc_balance2 = float(self.wallet[j]['balance'])
                        t = time.localtime()
                        self.print_list.append('초기 잔액               -> KRW : {}, BTC : {}'.format(krw_balance, btc_balance) + "\n")
                        self.print_list.append('최종 잔액               -> KRW : {}, BTC : {}'.format(krw_balance2, btc_balance2) + "\n")
                        self.print_list.append('거래를 통해 얻은 수익   -> KRW : {}원, BTC : {}원'.format(round(krw_balance2 - krw_balance), round(float(self.market_price[0]["bid_price"]) * (btc_balance2 - btc_balance))) + "\n")
                        self.print_list.append('현재까지의 총 이익      -> KRW : {}원, BTC : {}원, 현재시각 : {}년 {}월 {}일 {}시 {}분 {}초'.format(round(krw_balance2 - self.initial_krw_balance), round(float(self.market_price[0]["bid_price"]) * (btc_balance2 - self.initial_btc_balance)), t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec) + "\n")
                        self.print_list.append('----------------------------------------------------------------------------------------------------------------------------------------\n')

                        """ 거래가 이루어졌으면 MySQL DB에 거래 기록을 저장함 """
                        if result == 0 and (krw_balance2 - krw_balance != 0 or btc_balance2 - btc_balance != 0):  # 거래가 이루어 졌으면
                            if self.minimum_error_price < krw_balance2 - krw_balance < self.maximum_error_price and \
                                    self.minimum_error_price < float(self.market_price[0]["bid_price"]) * (btc_balance2 - btc_balance) < self.maximum_error_price:  # 거래할 때 오류가 나지 않았으면
                                try:
                                    self.curs.execute("insert into trade_log(time, coin, cycle_number, profit_krw, profit_btc, profit_eth) values(\""
                                                      + self.get_time_str() + "\", \""
                                                      + self.ALL_COIN[i] + "\", "
                                                      + str(max_profit_cycle_num) + ", "
                                                      + "{:.4f}".format(krw_balance2 - krw_balance) + ", "
                                                      + "{:.4f}".format(float(self.market_price[0]["bid_price"]) * (btc_balance2 - btc_balance)) + ", "
                                                      + "0)")
                                except Exception as ex:
                                    repr(ex)
                                    self.conn = pymysql.connect(host=self.ip, port=self.port, user='root', password='root', db='coin_transaction', charset='utf8')
                                    self.curs = self.conn.cursor()
                                    self.curs.execute("insert into trade_log(time, coin, cycle_number, profit_krw, profit_btc, profit_eth) values(\""
                                                      + self.get_time_str() + "\", \""
                                                      + self.ALL_COIN[i] + "\", "
                                                      + str(max_profit_cycle_num) + ", "
                                                      + "{:.4f}".format(krw_balance2 - krw_balance) + ", "
                                                      + "{:.4f}".format(float(self.market_price[0]["bid_price"]) * (btc_balance2 - btc_balance)) + ", "
                                                      + "0)")
                                self.conn.commit()
                            print(''.join(self.print_list))
                        else:
                            self.print_list.clear()
                        self.trading = False

    """ 거래를 시작함 """

    def trade_cycle(self, cycle_num=0, volume=0, coin_num=None):
        # 거래 시작전 호가 확인
        if self.check_orderbook_before_start == 1:
            while self.get_coin_orderbook() == -1:
                pass
            if self.coin_price[len(self.coin_price) - 2][self.market[cycle_num][1]][0][coin_num][self.price_type[cycle_num][0]] > self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][1]][0][coin_num][self.price_type[cycle_num][0]]:
                self.print_list.append('호가 변동으로 인해 거래를 종료합니다.\n')
                return -1

        """@@@@@@@@@@@@@@@@@@ 첫 번째 거래 시작 @@@@@@@@@@@@@@@@@@"""
        order_id = self.place_order(trade_market=self.market[cycle_num][0], coin=self.ALL_COIN[coin_num], side=self.order_type[cycle_num][0], volume=volume, price=self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][0]][0][coin_num][self.price_type[cycle_num][0]])
        original_price = self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][0]][0][coin_num][self.price_type[cycle_num][0]]
        if order_id is None:
            self.print_list.append("오류가 발생하여 거래를 종료합니다.\n")
            return -1
        self.print_list.append(self.market[cycle_num][0] + " 시장에서 " + self.ALL_COIN[coin_num] + " 코인을 " + str(self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][0]][0][coin_num][self.price_type[cycle_num][0]]) + " " + self.market[cycle_num][0] + "에 " + str(volume) + "개 매수주문 함\n")
        # 주문내역을 불러옴
        order = self.cancel_order(uuid=order_id)
        executed_volume = float(order["executed_volume"])  # 체결된 수량
        if executed_volume == 0.0:  # 체결이 전혀 안 되었으면
            self.print_list.append("체결이 전혀 안 되었으므로 주문을 취소합니다.\n")
            return -1
        # 조금이라도 체결 되었으면
        self.print_list.append(str(executed_volume) + "만큼 주문이 체결되었습니다.\n")

        """@@@@@@@@@@@@@@@@@@ 두 번째 거래 시작 @@@@@@@@@@@@@@@@@@"""
        volume = self.get_volume(coin_num)
        price = self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][1]][0][coin_num][self.price_type[cycle_num][1]]
        order_id = self.place_order(trade_market=self.market[cycle_num][1], coin=self.ALL_COIN[coin_num], side=self.order_type[cycle_num][1], volume=volume, price=price)
        if order_id is None:
            self.print_list.append("오류가 발생하여 거래를 종료합니다.\n")
            return -1
        self.print_list.append(self.market[cycle_num][1] + " 시장에서 " + self.ALL_COIN[coin_num] + "코인을 " + str(self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][1]][1][coin_num][self.price_type[cycle_num][1]]) + " " + self.market[cycle_num][1] + "에 " + str(executed_volume) + "개 매도주문 함\n")
        # 주문내역을 불러옴
        order = self.cancel_order(order_id)
        executed_volume = float(order["executed_volume"])
        state = order["state"]  # 주문 상태
        price = self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][0]][0][coin_num]["bid_price"]  # 되파는 가격
        while state != "done":  # 주문이 완료되지 않았으면
            order = self.cancel_order(uuid=order_id)  # 해당 주문 취소
            self.get_coin_orderbook()
            self.get_market_orderbook()
            profit_cycle = self.calc_profit_of_cycle(self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][0]][0][coin_num], self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][1]][0][coin_num], self.market_price[int(cycle_num / 3)], cycle_num)
            profit_resell = self.calc_profit_resell(original_price, price, cycle_num)
            if profit_cycle < profit_resell:  # 되파는 것이 사이클을 진행하는 것보다 이득이 날 경우
                my_volume = order["remaining_volume"]
                if str(my_volume) != "0.0":
                    order_id = self.place_order(trade_market=self.market[cycle_num][0], coin=self.ALL_COIN[coin_num], side="ask", volume=my_volume, price=price)
                    if order_id is None:
                        if executed_volume > 0:  # 사이클을 진행하여 체결된 양이 있으면 -> 오류가 떠도 세 번째 거래로 넘어감
                            break
                        else:  # 오류
                            self.print_list.append("오류가 발생하여 거래를 종료합니다.\n")
                            return -1
                    self.print_list.append("주문이 완료되지 않았으므로 현재 호가인 " + str(price) + " " + self.market[cycle_num][0] + "에 " + str(my_volume) + "개를 " + self.market[cycle_num][0] + "시장에 되팝니다.\n")
                    order = self.get_order(uuid=order_id, count=10)
                    state = order["state"]  # 주문 상태
                    if state == "done":
                        if executed_volume > 0:
                            break
                        else:  # 사이클을 진행하지 않고 되팔기만 한 경우
                            self.print_list.append('모든 주문이 체결되었습니다.\n')
                            return 0
                    price = (3 * price + self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][0]][0][coin_num]["bid_price"]) / 4
                    if self.market[cycle_num][0] == "KRW":
                        price = self.get_correct_krw_price(price)
                else:
                    break
            else:  # 사이클을 계속 진행하는 경우
                my_volume = order["remaining_volume"]
                if str(my_volume) != "0.0":
                    self.print_list.append("주문이 완료되지 않았으므로 현재 호가인 " + str(self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][1]][0][coin_num][self.price_type[cycle_num][1]]) + " " + self.market[cycle_num][1] + "에 " + str(my_volume) + "개를 다시 주문을 합니다. (체결된 수량 : " + str(executed_volume) + ")\n")
                    order_id = self.place_order(trade_market=self.market[cycle_num][1], coin=self.ALL_COIN[coin_num], side=self.order_type[cycle_num][1], volume=my_volume, price=self.coin_price[len(self.coin_price) - 1][self.market[cycle_num][1]][0][coin_num][self.price_type[cycle_num][1]])
                    if order_id is None:  # 극소량 주문해서 오류난 경우 -> 다 체결되었다 생각하고 넘어감
                        break
                    else:
                        order = self.cancel_order(order_id)
                        executed_volume = executed_volume + float(order["executed_volume"])
                        state = order["state"]  # 주문 상태
                else:
                    break
        if executed_volume > 0.0:  # 조금이라도 체결 되었으면
            if order_id is not None:
                self.cancel_order(uuid=order_id)  # 해당 주문 취소
            self.print_list.append(str(executed_volume) + "만큼 주문이 체결되었습니다.\n")

            """ 초기 지갑 내역 불러오기 """
            volume1 = 0
            volume2 = 0
            for j in range(0, len(self.wallet)):
                if self.wallet[j]['currency'] == self.market[cycle_num][3]:
                    volume1 = float(self.wallet[j]['balance'])
                    break

            """ 두 번째 거래 후 지갑내역 불러오기 """
            temp_wallet = self.get_my_wallet()
            for j in range(0, len(temp_wallet)):
                if temp_wallet[j]['currency'] == self.market[cycle_num][3]:
                    volume2 = float(temp_wallet[j]['balance'])
                    break

            volume = abs(volume2 - volume1)

            """@@@@@@@@@@@@@@@@@@ 세 번째 거래 시작 @@@@@@@@@@@@@@@@@@"""
            while True:
                price = self.market_price[int(cycle_num / 3)][self.price_type[cycle_num][2]]
                order_id = self.place_order(trade_market=self.market[cycle_num][2], coin=self.market[cycle_num][3], side=self.order_type[cycle_num][2], volume=volume, price=price)
                if order_id is None:
                    self.print_list.append("오류가 발생하여 거래를 종료합니다.\n")
                    return -1
                if self.order_type[cycle_num][2] == "ask":
                    order_type = "매도"
                else:
                    order_type = "매수"
                self.print_list.append(self.market[cycle_num][2] + " 시장에서 " + self.market[cycle_num][3] + " 코인을 " + str(price) + " KRW에 " + str(volume) + "개를 " + order_type + "주문 함\n")
                order = self.cancel_order(order_id)
                executed_volume = float(order["executed_volume"])  # 체결된 수량
                self.print_list.append(str(executed_volume) + "만큼 주문이 체결되었습니다.\n")
                if order['remaining_volume'] == "0.0":
                    return 0
                self.get_market_orderbook()
                volume = order['remaining_volume']

    def print_wallet(self):
        my_wallet = self.get_my_wallet()
        krw_balance = -1
        btc_balance = -1
        for i in range(0, len(my_wallet)):
            if my_wallet[i]['currency'] == 'KRW':
                krw_balance = my_wallet[i]['balance']
            if my_wallet[i]['currency'] == 'BTC':
                btc_balance = my_wallet[i]['balance']
        t = time.localtime()
        print('KRW : {}, BTC : {}, 현재시각 : {}년 {}월 {}일 {}시 {}분 {}초'.format(krw_balance, btc_balance, t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec))

    def start_thread(self):
        calc_thread = Thread(target=self.calc_profit)
        calc_thread.start()
        t = time.localtime()
        after = time.time()
        print('로딩까지 걸린 시간 : ' + str("{:.3f}".format(after - self.before)) + '초')
        print('현재시각 : {}년 {}월 {}일 {}시 {}분 {}초, 프로그램이 정상적으로 실행되었습니다.\n'.format(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec))

    """ 테스트 및 디버깅 전용 """
    def test(self):
        print("insufficient_funds_bid 오류 발생, 진행 중인 주문의 개수 : ", len(self.get_order_list()))
        self.cancel_all_order()
        print("cancel_all_order 후 진행 중인 주문의 개수 : ", len(self.get_order_list()))


if __name__ == '__main__':
    print('로딩 중...', end="")
    print()
    machine = UpbitMachine()
    machine.set_trade_coins()
    machine.start_thread()
    check_thread = Thread(target=machine.count_time)
    check_thread.start()
