# -*- coding: utf-8 -*-

import configparser
import requests
import time
import jwt
import json
import platform
import traceback
import websockets
import asyncio

from datetime import datetime
from threading import Thread
from urllib.parse import urlencode


class UpbitMachine:
    BASE_API_URL = "https://api.upbit.com/v1/"
    trading = False  # 현재 거래중인지 나타냄 -> 동시에 여러 거래가 이루어지는 것을 방지
    wallet = None  # 지갑 저장할 변수
    all_coin_list = None  # 모든 코인 리스트
    trade_coin_list = []  # KRW 시장과 BTC 시장에서 거래 가능한 코인들만 모아둔 리스트
    trade_coin_str = None  # trade_coin_list 내의 모든 코인들의 심볼 합친 것, ex) "KRW-ETH.1","BTC-ETH.1","BTC-LTC.1", ...    -> .1은 orderbook 1개만 불러오겠다는 뜻
    orderbook_dictionary = {}  # orderbook을 사용하기 쉽게 만든 딕셔너리
    previous_orderbook_dictionary = {}  # orderbook_dictionary 이전의 시세

    market = [["error", "error", "error"],
              ["BTC", "KRW", "KRW"],  # 1번 사이클 각 단계별 거래하는 시장 이름
              ["KRW", "BTC", "KRW"]]  # 2번 사이클 각 단계별 거래하는 시장 이름

    order_type = [["error", "error", "error"],  # bid : 매수, ask : 매도
                  ["bid", "ask", "bid"],  # 1번 사이클 각 단계별 거래하는 종류
                  ["bid", "ask", "ask"]]  # 2번 사이클 각 단계별 거래하는 종류

    price_type = [["error", "error", "error"],  # bid_price : 매수 호가, ask_price : 매도 호가 -> 즉시 거래를 위해 매수하는 경우 매도 호가를, 매도하는 경우 매수 호가를 사용
                  ["ap", "bp", "ap"],  # 1번 사이클 각 단계별 거래할 코인의 가격
                  ["ap", "bp", "bp"]]  # 2번 사이클 각 단계별 거래할 코인의 가격
    # ap = ask_price, bp = bid_price

    def __init__(self):
        """ config.ini 파일에서 정보 불러오는 부분 """
        config = configparser.ConfigParser()
        config.read("config.ini", encoding="utf-8-sig")
        self.access_key = config["UPBIT"]["access_key"]
        self.secret_key = config["UPBIT"]["secret_key"]
        self.profit = float(config["MACHINE"]["profit"])
        self.maximum_by_bitcoin = float(config["MACHINE"]["maximum_by_bitcoin"])
        self.minimum_by_bitcoin = float(config["MACHINE"]["minimum_by_bitcoin"])
        self.trade_if_rising = int(config["MACHINE"]["trade_if_rising"])
        self.trade_if_low_orderbook_difference = int(config["MACHINE"]["trade_if_low_orderbook_difference"])
        self.orderbook_difference_rate = float(config["MACHINE"]["orderbook_difference_rate"])
        self.orderbook_check_interval = int(config["MACHINE"]["orderbook_check_interval"])

        """ 초기 지갑 불러오기 """
        self.initial_wallet = self.get_my_wallet()
        self.wallet = self.get_my_wallet()

        """ 거래할 코인 불러오는 로직 """
        self.refresh_trade_coin()

    def refresh_trade_coin(self):
        self.all_coin_list = self.get_all_coin_list()  # 거래 가능한 모든 코인 정보 불러오기
        self.trade_coin_list = self.get_trade_coin_list()  # KRW 시장 및 BTC 시장에서 거래 가능한 코인들만 필터링하는 작업
        self.trade_coin_str = self.get_trade_coin_str()  # 거래 가능한 모든 코인을 문자열로 연결

    def get_all_coin_list(self):
        res = self.api_query(authorization=True, path="market/all", method="get")
        while res is None:
            time.sleep(0.1)
            res = self.api_query(authorization=True, path="market/all", method="get")
        return res

    def get_trade_coin_list(self):
        coin_list = []
        for i in range(0, len(self.all_coin_list)):
            market_name = self.all_coin_list[i]["market"][0:3]
            coin_name = self.all_coin_list[i]["market"][4:]
            for j in range(0, len(self.all_coin_list)):
                if market_name != self.all_coin_list[j]["market"][0:3] and coin_name == self.all_coin_list[j]["market"][4:]:  # 시장 이름은 다른데 코인 이름은 같은 게 존재하면
                    coin_list.append(self.all_coin_list[i]["market"])
                    break
        return coin_list

    def get_trade_coin_str(self):
        coin_str = ""
        for coin in self.trade_coin_list:
            coin_str = coin_str + "," + "\"" + coin + ".2\""
        return coin_str[1:]

    """ 웹 소켓 사용해서 실시간 orderbook 정보를 불러옴 """
    async def get_orderbook_with_websocket(self):
        uri = "wss://api.upbit.com/websocket/v1"

        async with websockets.connect(uri, ping_interval=None) as websocket:
            # 구독 요청
            data = "[{'ticket':'test'},{'format':'SIMPLE'},{'type':'orderbook','codes':[\'KRW-BTC.2\',' + self.trade_coin_str + ']}]"
            await websocket.send(data)

            while True:
                recv_data = await websocket.recv()
                orderbook = json.loads(recv_data)
                self.orderbook_dictionary[orderbook["cd"]] = orderbook["obu"]
                if orderbook["st"] == "SNAPSHOT":
                    self.previous_orderbook_dictionary[orderbook["cd"]] = orderbook["obu"]
                    self.orderbook_dictionary[orderbook["cd"]] = orderbook["obu"]
                else:
                    self.previous_orderbook_dictionary[orderbook["cd"]] = self.orderbook_dictionary[orderbook["cd"]]
                    self.orderbook_dictionary[orderbook["cd"]] = orderbook["obu"]

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

    def api_query(self, authorization=False, path=None, method="get", query_params=None):
        with requests.Session() as s:
            try:
                headers = {"User-Agent": platform.platform()}
                url = "{0:s}{1:s}".format(self.BASE_API_URL, path)
                if authorization:
                    payload = {
                        "access_key": self.access_key,
                        "nonce": str(self.get_nonce())
                    }
                    if query_params is not None:
                        payload["query"] = query_params
                        url = "{0:s}?{1:s}".format(url, query_params)
                    token = jwt.encode(payload, self.secret_key, algorithm="HS256")
                    headers["Authorization"] = "Bearer {0:s}".format(token.decode("utf-8"))
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
                        print(response.content.decode("utf-8"))
                        return self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                    elif temp["error"]["name"] == "too_many_requests":
                        time.sleep(1)
                        return self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                    elif temp["error"]["name"] == "server_error":
                        time.sleep(1)
                        print(response.content.decode("utf-8"))
                        return self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                    elif temp["error"]["name"] == "internal_server_error":  # 그냥 어쩌다 한 번씩 나오는 오류
                        time.sleep(1)
                        return self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                    elif temp["error"]["name"] == "order_not_found":  # 주문을 너무 빨리 가져오는 경우
                        time.sleep(0.1)
                        if method == "delete":  # 취소 주문 과정에서 주문을 찾을 수 없다고 나오는 경우 -> 이미 체결된 상황
                            return None
                        print(response.content.decode("utf-8"))
                        return self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                    elif temp["error"]["name"] == "invalid_funds_ask":  # 비정상적인 매개변수로 주문을 넣은 경우
                        print(response.content.decode("utf-8"))
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
                        print(response.content.decode("504 Gateway Time-out"))
                        self.api_query(authorization=authorization, path=path, method=method, query_params=query_params)
                if response.status_code != 200 and response.status_code != 201:
                    print(query_params)
                    print(response.content.decode("utf-8"))
                return response.json() if response.status_code == 200 or response.status_code == 201 else None
            except requests.ConnectionError:
                print("ConnectionError")
                return None
            except Exception as e:
                print(repr(e))
            finally:
                s.close()

    def get_my_wallet(self):
        res = self.api_query(authorization=True, path="accounts", method="get")
        while res is None:
            res = self.api_query(authorization=True, path="accounts", method="get")
        return res

    @staticmethod
    def get_my_balance(wallet, coin_name):
        for coin in wallet:
            if coin["currency"] == coin_name:
                return float(coin["balance"])
        return 0

    def get_order(self, uuid, count=10):
        query_params = urlencode({"uuid": uuid})
        res = self.api_query(authorization=True, path="order", method="get", query_params=query_params)
        if res == -1:
            print("get_order에서 그냥 -1이 반환됨, uuid = " + uuid)
            return -1
        i = 0
        while True:
            if i >= count:
                return res
            if res is None:
                print("get_order에서 1이 반환됨, uuid = " + uuid)
                return 1
            if res["state"] == "done" or res["state"] == "cancel":
                return res
            else:
                res = self.api_query(authorization=True, path="order", method="get", query_params=query_params)
                time.sleep(0.1)
                i = i + 1

    def get_order_list(self, market=None):
        if market is None:
            query_params = urlencode({"state": "wait"})
        else:
            query_params = urlencode({"state": "wait",
                                      "market": market})
        res = self.api_query(authorization=True, path="orders", method="get", query_params=query_params)
        return res

    def place_order(self, trade_market=None, coin_name=None, side="ask", volume=None, price=None, ord_type="limit"):
        market = trade_market + "-" + coin_name
        query_params = None
        if volume is None:
            query_params = urlencode({"market": market,
                                      "side": side,
                                      "price": price,
                                      "ord_type": ord_type})
        elif price is None:
            query_params = urlencode({"market": market,
                                      "side": side,
                                      "volume": volume,
                                      "ord_type": ord_type})
        else:
            query_params = urlencode({"market": market,
                                      "side": side,
                                      "volume": volume,
                                      "price": price,
                                      "ord_type": ord_type})

        res = self.api_query(authorization=True, path="orders", method="post", query_params=query_params)

        if res is None:  # insufficient_funds_bid 오류
            self.cancel_all_order()
            res = self.api_query(authorization=True, path="orders", method="post", query_params=query_params)
            if res is None:  # 주문 모두 취소했는데도 오류가 생기면 진짜 돈이 부족하다고 판단하고 오류 처리
                return None
        return res["uuid"]

    """ uuid에 해당하는 주문을 취소하고 그 주문에 대한 내역을 반환 """
    def cancel_order(self, uuid, count=2):
        query_params = urlencode({"uuid": uuid})
        self.api_query(authorization=True, path="order", method="delete", query_params=query_params)
        order = self.get_order(uuid=uuid, count=count)
        while order["state"] != "cancel" and order["state"] != "done":
            self.api_query(authorization=True, path="order", method="delete", query_params=query_params)
            order = self.get_order(uuid=uuid, count=count)
            count = count + 1
        return order

    """ 진행 중인 모든 주문을 취소 """
    def cancel_all_order(self, market=None):
        order_list = self.get_order_list(market)
        for i in range(0, len(order_list)):
            self.cancel_order(uuid=order_list[i]["uuid"])

    """ 주기적으로 지갑을 불러옴 -> 정확한 가격 계산을 위함 """
    def get_my_wallet_periodically(self):
        while True:
            if self.trading is False:
                self.wallet = self.get_my_wallet()
                time.sleep(20)
            else:
                time.sleep(5)

    def calculate_profit_of_cycle(self, coin_name, cycle_num):
        if cycle_num == 1:  # BTC 시장에서 사서 KRW 시장에 판매
            profit = 1 / self.orderbook_dictionary["BTC-" + coin_name][0]["ap"] * self.orderbook_dictionary["KRW-" + coin_name][0]["bp"] / self.orderbook_dictionary["KRW-BTC"][0]["ap"]
        else:  # KRW 시장에서 사서 BTC 시장에 판매
            profit = 1 / self.orderbook_dictionary["KRW-" + coin_name][0]["ap"] * self.orderbook_dictionary["BTC-" + coin_name][0]["bp"] * self.orderbook_dictionary["KRW-BTC"][0]["bp"]
        return profit * 0.996502749375

    @staticmethod
    def calc_profit_resell(bid_price, ask_price, cycle_num):
        if cycle_num == 1:  # 처음에 BTC 시장에서 산 경우
            return (ask_price * 0.9975) / (bid_price * 1.0025)
        else:  # 처음에 KRW 시장에서 산 경우
            return (ask_price * 0.9995) / (bid_price * 1.0005)

    """ 단위를 i번째 코인의 단위로 변환함 """

    def get_x_coin_volume(self, coin_name, cycle_num, order_volume):
        if cycle_num == 1:
            return order_volume / self.orderbook_dictionary["BTC-" + coin_name][0]["ap"]
        elif cycle_num == 2:
            return order_volume / self.orderbook_dictionary["BTC-" + coin_name][0]["bp"]

    """ 최적 주문 개수를 구함 """

    def get_optimal_volume(self, coin_name, cycle_num=None):
        if cycle_num is None:
            raise Exception("you need to set param")
        if cycle_num == 1:
            return min(self.orderbook_dictionary["BTC-" + coin_name][0]["ap"] * self.orderbook_dictionary["BTC-" + coin_name][0]["as"],
                       self.orderbook_dictionary["KRW-" + coin_name][0]["bp"] * self.orderbook_dictionary["KRW-" + coin_name][0]["bs"] / self.orderbook_dictionary["KRW-BTC"][0]["ap"])
        elif cycle_num == 2:
            return min(self.orderbook_dictionary["KRW-" + coin_name][0]["as"] * self.orderbook_dictionary["BTC-" + coin_name][0]["bp"],
                       self.orderbook_dictionary["BTC-" + coin_name][0]["bs"] * self.orderbook_dictionary["BTC-" + coin_name][0]["bp"])

    """ 최적 주문 개수를 실제 주문할 개수로 변환함 """

    def get_order_volume(self, optimal_volume=-1):
        val = optimal_volume * 0.7
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

    def orderbook_thread_function(self):
        asyncio.run(self.get_orderbook_with_websocket())

    """ 저장된 호가를 통해 i번째 코인이 각 사이클에서 수익을 낼 수 있는지 계산하고 수익을 낼 수 있는 사이클이 있으면 거래를 시작함 """

    def calculate_profit(self):
        while True:
            time.sleep(0.05)
            for coin_symbol in self.trade_coin_list:
                coin_name = coin_symbol[4:]
                """ KRW <-> BTC """
                profit_btc_krw = 0
                profit_krw_btc = 0
                try:
                    profit_btc_krw = self.calculate_profit_of_cycle(coin_name, 1)  # 1번 사이클 : BTC 시장에서 사서 KRW 시장에 판매
                    profit_krw_btc = self.calculate_profit_of_cycle(coin_name, 2)  # 2번 사이클 : KRW 시장에서 사서 BTC 시장에 판매
                except Exception as e:
                    print(repr(e))
                    print("calculate_profit_of_cycle에서 에러 발생!")

                """ 몇 번째 사이클이 최대의 수익률을 낼 수 있는지 확인 """
                max_profit = profit_btc_krw
                max_profit_cycle_num = 1

                if profit_krw_btc > max_profit:
                    max_profit = profit_krw_btc
                    max_profit_cycle_num = 2
                """
                max_profit = profit_krw_btc
                max_profit_cycle_num = 2
                """

                # print(coin_name + " 코인의 최적 거래 사이클 번호 : " + str(max_profit_cycle_num) + "번, 예상 수익률 : " + str(max_profit))
                if self.profit <= max_profit:
                    """ 두 번째 거래에서 거래할 코인의 가격이 상승세가 아니면 거래하지 않음 """
                    if self.trade_if_rising == 1:
                        if self.orderbook_dictionary[self.market[max_profit_cycle_num][1]+"-"+coin_name][0]["bp"] <= self.previous_orderbook_dictionary[self.market[max_profit_cycle_num][1]+"-"+coin_name][0]["bp"]:
                            print(self.market[max_profit_cycle_num][1] + "시장에서 " + coin_name + "코인의 매수호가가 상승세가 아니므로 거래를 하지 않습니다. 얼마 전 가격 : " + str(self.orderbook_dictionary[self.market[max_profit_cycle_num][1]+"-"+coin_name][0]["ap"]) + ", 현재 가격 : " + str(self.orderbook_dictionary[self.market[max_profit_cycle_num][1]+"-"+coin_name][0]["bp"]))
                            return

                    """ 매수 매도 호가의 차이가 많이 나면 거래를 안 함 """
                    if self.trade_if_low_orderbook_difference == 1:
                        orderbook_difference = self.orderbook_dictionary[self.market[max_profit_cycle_num][0] + "-" + coin_name][0]["ap"] / self.orderbook_dictionary[self.market[max_profit_cycle_num][0] + "-" + coin_name][0]["bp"]
                        if orderbook_difference > self.orderbook_difference_rate:
                            # print(self.market[max_profit_cycle_num][0] + "시장에서 " + self.ALL_COIN[i] + "코인의 매수 매도 호가의 차이가 많이 나므로 거래를 하지 않습니다. 매도 호가 : " + str(self.coin_price[len(self.coin_price)-1][self.market[max_profit_cycle_num][0]][0][i]["ask_price"]) + ", 매수 호가 : " + str(self.coin_price[len(self.coin_price)-1][self.market[max_profit_cycle_num][0]][0][i]["bid_price"]))
                            return

                    time.sleep(0.05)  # 해당 코인에 대해 많은 양의 거래가 한 순간에 이루어졌는데 그 중간 가격을 가지고 수익률을 계산한 경우를 방지
                    if self.profit > self.calculate_profit_of_cycle(coin_name, max_profit_cycle_num):
                        print("코인 이름: " + coin_name + ", 사이클 번호 : " + str(max_profit_cycle_num) + "번, 갑작스러운 시세변동으로 인해 거래를 하지 않습니다.")
                    else:
                        optimal_volume = self.get_optimal_volume(coin_name=coin_name, cycle_num=max_profit_cycle_num)  # 비트 기준
                        order_volume = self.get_order_volume(optimal_volume=optimal_volume)  # 비트 기준
                        if order_volume != -1 and self.trading is False:
                            self.trading = True
                            print("----------------------------------------------------------------------------------------------------------------------------------------")
                            print("현재시각 : " + str(datetime.now()) + ", " + coin_name + " 코인의 최적 거래 사이클 번호 : " + str(max_profit_cycle_num) + "번, 예상 수익률 : " + str(round(max_profit, 4)) + ", 최적 거래 개수 : " + str(round(optimal_volume, 8)))
                            print(self.orderbook_dictionary["KRW-" + coin_name][0])
                            print(self.orderbook_dictionary["BTC-" + coin_name][0])

                            x_coin_volume = self.get_x_coin_volume(coin_name=coin_name, cycle_num=max_profit_cycle_num, order_volume=order_volume)

                            try:
                                self.trade_cycle(coin_name, max_profit_cycle_num, x_coin_volume)  # 지정가 거래, 느리더라도 안전하게 거래
                                # self.trade_cycle2(coin_name, max_profit_cycle_num, x_coin_volume)  # 시장가 거래, 크게 손해 볼 확률이 있지만 무시하고 아주 빠르게 거래 진행
                            except Exception as ex:
                                print("오류가 발생하여 거래가 중지되었습니다.")
                                print(repr(ex))
                                traceback.print_exc()
                            time.sleep(0.5)

                            """ 초기 지갑 내역 불러오기 """
                            krw_balance = self.get_my_balance(self.wallet, "KRW")
                            btc_balance = self.get_my_balance(self.wallet, "BTC")

                            """ 거래 후 지갑내역 불러오기 """
                            self.wallet = self.get_my_wallet()
                            krw_balance2 = self.get_my_balance(self.wallet, "KRW")
                            btc_balance2 = self.get_my_balance(self.wallet, "BTC")
                            print("초기 잔액               -> KRW : {}, BTC : {}".format(round(krw_balance), btc_balance))
                            print("최종 잔액               -> KRW : {}, BTC : {}".format(round(krw_balance2), btc_balance2))
                            print("거래를 통해 얻은 수익   -> KRW : {}원, BTC : {}원".format(round(krw_balance2 - krw_balance), round(float(self.orderbook_dictionary["KRW-BTC"][0]["bp"]) * (btc_balance2 - btc_balance))))
                            print("현재까지의 총 이익      -> KRW : {}원, BTC : {}원".format(round(krw_balance2 - self.get_my_balance(self.initial_wallet, "KRW")), round(float(self.orderbook_dictionary["KRW-BTC"][0]["bp"]) * (btc_balance2 - self.get_my_balance(self.initial_wallet, "BTC")))))
                            print("현재시각 : " + str(datetime.now()))
                            print("----------------------------------------------------------------------------------------------------------------------------------------")
                            self.trading = False

    """ 시장가로 즉시 거래 """
    def trade_cycle2(self, coin_name=None, cycle_num=0, volume=0):
        """@@@@@@@@@@@@@@@@@@ 첫 번째 거래 시작 @@@@@@@@@@@@@@@@@@"""
        price = volume * self.orderbook_dictionary[self.market[cycle_num][0]+"-"+coin_name][0]["ap"]
        if cycle_num == 2:
            price = self.get_correct_krw_price(price)
        self.place_order(self.market[cycle_num][0], coin_name, self.order_type[cycle_num][0], volume=None, price=price, ord_type="price")  # 시장가 매수
        executed_volume = 0
        while executed_volume == 0:
            temp_wallet = self.get_my_wallet()
            executed_volume = self.get_my_balance(temp_wallet, coin_name)

        """@@@@@@@@@@@@@@@@@@ 두 번째 거래 시작 @@@@@@@@@@@@@@@@@@"""
        self.place_order(self.market[cycle_num][1], coin_name, self.order_type[cycle_num][1], volume=executed_volume, price=None, ord_type="market")  # 시장가 매도
        volume1 = self.get_my_balance(self.wallet, "BTC")  # 초기에 보유한 BTC 수량
        temp_wallet = self.get_my_wallet()
        volume2 = self.get_my_balance(temp_wallet, "BTC")  # 두 번째 거래 이후에 보유한 BTC 수량
        while volume1 == volume2:
            temp_wallet = self.get_my_wallet()
            volume2 = self.get_my_balance(temp_wallet, "BTC")  # 두 번째 거래 이후에 보유한 BTC 수량

        """@@@@@@@@@@@@@@@@@@ 세 번째 거래 시작 @@@@@@@@@@@@@@@@@@"""
        volume = abs(volume2 - volume1)  # 거래 전후 BTC 수량 차이
        if cycle_num == 1:
            self.place_order("KRW", "BTC", "bid", volume=None, price=volume * self.orderbook_dictionary["KRW-BTC"][0]["ap"], ord_type="price")  # 시장가 매수
        elif cycle_num == 2:
            self.place_order("KRW", "BTC", "ask", volume=volume, price=None, ord_type="market")  # 시장가 매도
        time.sleep(1)

    """ 일반 거래 """
    def trade_cycle(self, coin_name=None, cycle_num=0, volume=0):
        coin_bid_price = self.orderbook_dictionary[self.market[cycle_num][0]+"-"+coin_name][0][self.price_type[cycle_num][0]]  # 첫 번째 거래에서 코인 매수할 가격
        coin_ask_price = self.orderbook_dictionary[self.market[cycle_num][1]+"-"+coin_name][0][self.price_type[cycle_num][1]]  # 두 번째 거래에서 코인 매도할 가격
        """@@@@@@@@@@@@@@@@@@ 첫 번째 거래 시작 @@@@@@@@@@@@@@@@@@"""
        order_id = self.place_order(trade_market=self.market[cycle_num][0], coin_name=coin_name, side=self.order_type[cycle_num][0], volume=volume, price=coin_bid_price)
        original_price = coin_bid_price
        if order_id is None:
            print("오류가 발생하여 거래를 종료합니다.")
            return -1
        print(self.market[cycle_num][0] + " 시장에서 " + coin_name + " 코인을 " + str(coin_bid_price) + " " + self.market[cycle_num][0] + "에 " + str(volume) + "개 매수주문 함")
        # 주문내역을 불러옴
        order = self.cancel_order(uuid=order_id)
        executed_volume = float(order["executed_volume"])  # 체결된 수량
        if executed_volume == 0.0:  # 체결이 전혀 안 되었으면
            print("체결이 전혀 안 되었으므로 주문을 취소합니다.")
            return -1
        # 조금이라도 체결 되었으면
        print(str(executed_volume) + "만큼 주문이 체결되었습니다.")

        """@@@@@@@@@@@@@@@@@@ 두 번째 거래 시작 @@@@@@@@@@@@@@@@@@"""
        temp_wallet = self.get_my_wallet()
        volume = self.get_my_balance(temp_wallet, coin_name)
        order_id = self.place_order(trade_market=self.market[cycle_num][1], coin_name=coin_name, side=self.order_type[cycle_num][1], volume=volume, price=coin_ask_price)
        if order_id is None:
            print("오류가 발생하여 거래를 종료합니다.")
            return -1
        print(self.market[cycle_num][1] + " 시장에서 " + coin_name + "코인을 " + str(coin_ask_price) + " " + self.market[cycle_num][1] + "에 " + str(executed_volume) + "개 매도주문 함")
        # 주문내역을 불러옴
        self.get_order(order_id)
        order = self.cancel_order(order_id)
        executed_volume = float(order["executed_volume"])
        state = order["state"]  # 주문 상태
        while state != "done":  # 주문이 완료되지 않았으면
            order = self.cancel_order(uuid=order_id)  # 해당 주문 취소
            resell_price = self.orderbook_dictionary[self.market[cycle_num][0]+"-"+coin_name][0]["bp"]  # 되파는 가격
            profit_cycle = self.calculate_profit_of_cycle(coin_name, cycle_num)
            profit_resell = self.calc_profit_resell(original_price, resell_price, cycle_num)
            my_volume = order["remaining_volume"]  # 미체결된 양
            if profit_cycle < profit_resell:  # 되파는 것이 사이클을 진행하는 것보다 이득이 날 경우
                if str(my_volume) != "0.0":
                    order_id = self.place_order(trade_market=self.market[cycle_num][0], coin_name=coin_name, side="ask", volume=my_volume, price=resell_price)
                    if order_id is None:
                        if executed_volume > 0:  # 사이클을 진행하여 체결된 양이 있으면 -> 오류가 떠도 세 번째 거래로 넘어감
                            break
                        else:  # 오류
                            print("오류가 발생하여 거래를 종료합니다.")
                            return -1
                    print("주문이 완료되지 않았으므로 현재 호가인 " + str(resell_price) + " " + self.market[cycle_num][0] + "에 " + str(my_volume) + "개를 " + self.market[cycle_num][0] + "시장에 되팝니다.")
                    order = self.get_order(uuid=order_id, count=10)
                    state = order["state"]  # 주문 상태
                    if state == "done":
                        if executed_volume > 0:
                            break
                        else:  # 사이클을 진행하지 않고 되팔기만 한 경우
                            print("모든 주문이 체결되었습니다.")
                            return 0
                else:  # 모두 체결된 경우
                    break
            else:  # 사이클을 계속 진행하는 경우
                if str(my_volume) != "0.0":
                    current_price = self.orderbook_dictionary[self.market[cycle_num][1]+"-"+coin_name][0][self.price_type[cycle_num][1]]
                    order_id = self.place_order(trade_market=self.market[cycle_num][1], coin_name=coin_name, side=self.order_type[cycle_num][1], volume=my_volume, price=current_price)
                    print("주문이 완료되지 않았으므로 현재 호가인 " + str(current_price) + " " + self.market[cycle_num][1] + "에 " + str(my_volume) + "개를 다시 주문을 합니다. (체결된 수량 : " + str(executed_volume) + ")")
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
            print(str(executed_volume) + "만큼 주문이 체결되었습니다.")

            volume1 = self.get_my_balance(self.wallet, "BTC")  # 초기에 보유한 BTC 수량

            temp_wallet = self.get_my_wallet()
            volume2 = self.get_my_balance(temp_wallet, "BTC")  # 두 번째 거래 이후에 보유한 BTC 수량

            volume = abs(volume2 - volume1)  # 거래 전후 BTC 수량 차이

            """@@@@@@@@@@@@@@@@@@ 세 번째 거래 시작 @@@@@@@@@@@@@@@@@@"""
            while True:
                price = self.orderbook_dictionary["KRW-BTC"][0][self.price_type[cycle_num][2]]
                order_id = self.place_order(trade_market="KRW", coin_name="BTC", side=self.order_type[cycle_num][2], volume=volume, price=price)
                if order_id is None:
                    print("오류가 발생하여 거래를 종료합니다.")
                    return -1
                if self.order_type[cycle_num][2] == "ask":
                    order_type = "매도"
                else:
                    order_type = "매수"
                print("KRW 시장에서 BTC를 " + str(price) + " 원에 " + str(volume) + "개를 " + order_type + " 주문하였습니다.")
                order = self.cancel_order(order_id)
                executed_volume = float(order["executed_volume"])  # 체결된 수량
                print(str(executed_volume) + "만큼 주문이 체결되었습니다.")
                if order["remaining_volume"] == "0.0":
                    return 0
                volume = order["remaining_volume"]

    def print_wallet(self):
        my_wallet = self.get_my_wallet()
        krw_balance = self.get_my_balance(my_wallet, "KRW")
        btc_balance = self.get_my_balance(my_wallet, "BTC")
        print("현재시각 : " + str(datetime.now()) + ", KRW : " + str(krw_balance) + ", BTC : " + str(btc_balance))

    def start_threads(self):
        Thread(target=self.get_my_wallet_periodically).start()  # 주기적으로 지갑 불러오는 스레드 시작
        Thread(target=self.orderbook_thread_function).start()  # 각 코인 호가 불러오는 스레드 시작
        time.sleep(2)
        Thread(target=self.calculate_profit).start()  # 메인 스레드 시작
        print(self.orderbook_dictionary)
        print("현재시각 : " + str(datetime.now()))
        print("프로그램이 정상적으로 실행되었습니다.\n")

    """ 테스트 및 디버깅 전용 """
    def test(self):
        print(datetime.now())
        self.place_order("KRW", "XRP", "bid", volume=None, price="8000", ord_type="price")  # 시장가 매수
        print(datetime.now())
        volume = 0
        while volume == 0:
            self.wallet = self.get_my_wallet()
            volume = self.get_my_balance(self.wallet, "XRP")
        print(datetime.now())
        self.place_order("BTC", "XRP", "ask", volume=self.get_my_balance(self.wallet, "XRP"), price=None, ord_type="market")  # 시장가 매도
        print(datetime.now())
        pass


if __name__ == "__main__":
    print("로딩 중...")
    machine = UpbitMachine()  # 생성자 호출
    machine.start_threads()
