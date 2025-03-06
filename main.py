import sys
from datetime import datetime

import requests
from selenium.webdriver.support import expected_conditions as EC
from selenium import webdriver
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.common.by import By
import time
import random

from selenium.webdriver.support.wait import WebDriverWait
from selenium_stealth import stealth
from tqdm import tqdm


def init_browser(s):
    # 初始化 Edge 浏览器并注入反检测参数
    options = EdgeOptions()
    if s:
        options.add_argument("--headless")  # 无头模式
    options.add_argument("--disable-notifications")  # 禁用通知
    options.add_argument("--no-sandbox")  # 跳过沙盒
    options.add_argument("--disable-dev-shm-usage")  # 解决内存不足问题
    options.use_chromium = True
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])

    driver = webdriver.Edge(service=EdgeService(executable_path="./msedgedriver.exe"), options=options)
    return driver


def init_mobile_edge_appium(s):
    # 设置手机型号，这设置为iPhone 6
    mobile_emulation = {"deviceName": "iPhone X"}

    options = EdgeOptions()
    options.add_experimental_option("mobileEmulation", mobile_emulation)
    if s:
        options.add_argument("--headless")  # 无头模式
    options.add_argument("--disable-notifications")  # 禁用通知
    options.add_argument("--no-sandbox")  # 跳过沙盒
    options.add_argument("--disable-dev-shm-usage")  # 解决内存不足问题
    options.use_chromium = True
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
    # 启动配置好的浏览器
    driver = webdriver.Edge(options=options)
    return driver


def gohome(driver):
    try:
        driver.get("https://rewards.bing.com/?ref=rewardspanel")
    except Exception as e:
        print(e)


def bing_search(driver, keyword):
    try:
        element = driver.find_element(By.XPATH, '//*[@id="sb_form_q"]')
        element.clear()
        time.sleep(random.randint(2, 4))
        element.send_keys(keyword)
        time.sleep(random.randint(1, 3))
        element.submit()
        time.sleep(random.randint(3, 5))
    except Exception as e:
        print(e)


def daily_set(driver):
    gohome(driver)
    data = getDashboardData(driver)["dailySetPromotions"]
    todayDate = datetime.now().strftime("%m/%d/%Y")
    for i in data.get(todayDate, []):
        print(i)
        if i["attributes"]["state"] == "Complete":
            continue
        cardId = int(i["offerId"][-1:])
        openDailySetActivity(driver, cardId)
        time.sleep(random.randint(3, 5))
    gohome(driver)
    print("完成每日任务")


def getDashboardData(driver) -> dict:
    return driver.execute_script("return dashboard")


def openDailySetActivity(driver, cardId: int):
    driver.find_element(
        By.XPATH,
        f'//*[@id="daily-sets"]/mee-card-group[1]/div/mee-card[{cardId}]/div/card-content/mee-rewards-daily-set-item-content/div/a',
    ).click()
    switchToNewTab(driver, 8)
    closeCurrentTab(driver)


def switchToNewTab(driver, timeToWait: int = 0):
    time.sleep(0.5)
    driver.switch_to.window(window_name=driver.window_handles[1])
    if timeToWait > 0:
        time.sleep(timeToWait)


def closeCurrentTab(driver):
    driver.close()
    time.sleep(0.5)
    driver.switch_to.window(window_name=driver.window_handles[0])
    time.sleep(0.5)


def goSearch(driver):
    try:
        driver.get("https://cn.bing.com/")
    except Exception as e:
        print(e)
def getBaiduTrends() -> list:
    r = requests.get("https://v2.xxapi.cn/api/baiduhot")
    words =list()
    if r.status_code == 200:
        data = r.json()["data"]
        for i in data:
            trend = i
            words.append(trend["title"])
    return words
def getZhihuTrends():
    words = list()
    r = requests.get("https://api.cenguigui.cn/api/juhe/hotlist.php?type=zhihu")
    if r.status_code == 200:
        data = r.json()["data"]
        for i in data:
            trend = i
            words.append(trend["title"])
    return words
def getDouYinTrends():
    words = list()
    r = requests.get("https://api.cenguigui.cn/api/juhe/hotlist.php?type=weibo")
    if r.status_code == 200:
        data = r.json()["data"]
        for i in data:
            trend = i
            words.append(trend["title"])
    return words

if __name__ == "__main__":
    argv = sys.argv
    s = None
    if len(argv) == 2:
        s = argv[1]
    edge_driver = init_browser(s)
    daily_set(edge_driver)
    goSearch(edge_driver)
    keyword_list = getDouYinTrends()
    for i in tqdm(range(40), desc="bing searches", unit="search"):
        bing_search(edge_driver, random.choice(keyword_list))
    edge_driver.close()

    edge_driver = init_mobile_edge_appium(s)
    goSearch(edge_driver)
    keyword_list = getBaiduTrends()
    for i in tqdm(range(15), desc="bing searches", unit="search"):
        keyword=random.choice(keyword_list)
        keyword_list.remove(keyword)
        bing_search(edge_driver, keyword)
        time.sleep(random.randint(2, 4))
