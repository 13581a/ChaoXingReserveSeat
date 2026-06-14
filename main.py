import json
import time
import argparse
import os
import logging
import random

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

from utils import reserve, get_user_credentials

get_current_time = lambda action: (
    time.strftime("%H:%M:%S", time.localtime(time.time() + 8 * 3600))
    if action
    else time.strftime("%H:%M:%S", time.localtime(time.time()))
)
get_current_dayofweek = lambda action: (
    time.strftime("%A", time.localtime(time.time() + 8 * 3600))
    if action
    else time.strftime("%A", time.localtime(time.time()))
)

SLEEPTIME = 1.0
ENDTIME = "08:01:00"
ENABLE_SLIDER = False
MAX_ATTEMPT = 5
RESERVE_NEXT_DAY = True


def prepare_all(users, usernames, passwords, action):
    """提前登录 + token 预热验证，提前发现 session 问题"""
    current_dayofweek = get_current_dayofweek(action)
    prepared = []
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if type(seatid) == str:
            seatid = [seatid]
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            prepared.append(None)
            continue
        logging.info(f"[prepare] ({index+1}/{len(users)}) 登录: user={username}, "
                     f"times={times}, seatid={seatid}, roomid={roomid}")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        ok, msg = s.login(username, password)
        if not ok:
            logging.error(f"[prepare] ❌ {username} 登录失败: {msg}")
            prepared.append(None)
            continue
        s.requests.headers.update({"Host": "office.chaoxing.com"})

        # 🔑 预热验证：立即尝试获取 token，提前发现 session 问题
        warmup_ok = s.warmup_token(roomid, seatid)
        if not warmup_ok:
            logging.warning(f"[prepare] ⚠️ {username} 预热失败，尝试重登录...")
            if s.re_login():
                # 重登录后再试一次预热
                time.sleep(0.3)
                warmup_ok = s.warmup_token(roomid, seatid)
                if warmup_ok:
                    logging.info(f"[prepare] ✅ {username} 重登录后预热成功")
                else:
                    logging.warning(f"[prepare] ⚠️ {username} 重登录后预热仍失败，"
                                  f"将在08:00重试")
            else:
                logging.error(f"[prepare] ❌ {username} 重登录失败")
        else:
            logging.info(f"[prepare] ✅ {username} 预热验证通过")

        prepared.append({
            "s": s,
            "times": times,
            "roomid": roomid,
            "seatid": seatid,
            "action": action,
        })
    return prepared


def submit_all(prepared, success_list):
    """实时拿token并立刻提交，token为空时尝试重登录，
    '未到开放时间'时立即短间隔重试"""
    for index, item in enumerate(prepared):
        if item is None or success_list[index]:
            continue
        s = item["s"]
        times = item["times"]
        roomid = item["roomid"]
        seatid = item["seatid"]
        action = item["action"]
        for seat in seatid:
            url = s.url.format(roomid, seat)
            token, value = s._get_page_token(url, require_value=True)
            if not token:
                logging.warning(f"[submit_all] seat={seat} token为空，尝试重登录...")
                if s.re_login():
                    time.sleep(0.5)
                    token, value = s._get_page_token(url, require_value=True)
                    if not token:
                        logging.warning(f"[submit_all] seat={seat} 重登录后token仍为空，跳过")
                        continue
                else:
                    logging.warning(f"[submit_all] seat={seat} 重登录失败，跳过")
                    continue

            # 提交循环：处理"未到开放时间"时快速重试
            not_open_retries = 0
            max_not_open_retries = 20  # 最多等 2 秒（20 × 100ms）
            while True:
                suc, msg = s.get_submit(
                    s.submit_url,
                    times=times,
                    token=token,
                    roomid=roomid,
                    seatid=seat,
                    captcha="",
                    action=action,
                    value=value,
                )
                if suc:
                    success_list[index] = True
                    break
                # 检测"未到开放时间" → 短间隔重试，不换token
                if s._is_not_open_yet(msg) and not_open_retries < max_not_open_retries:
                    not_open_retries += 1
                    logging.info(
                        f"[submit_all] ⏳ '未到开放时间' 第{not_open_retries}次, "
                        f"100ms后重试（服务器时钟偏慢约{not_open_retries * 100}ms）..."
                    )
                    time.sleep(0.1)
                    continue
                # 其他失败（或等待超时）→ 跳出内层，下一轮重试
                break
            if success_list[index]:
                break
    return success_list


def main(users, action=False):
    current_time = get_current_time(action)
    logging.info(f"[main] 开始时间 {current_time}, 模式={'GitHub Action' if action else '本地'}")
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    current_dayofweek = get_current_dayofweek(action)
    today_reservation_num = sum(
        1 for d in users if current_dayofweek in d.get("daysofweek")
    )
    success_list = [False] * len(users)
    logging.info(f"[main] 今日待预约 {today_reservation_num}/{len(users)} 人")

    prepared = prepare_all(users, usernames, passwords, action)

    # 如果已过 08:00，跳过等待直接提交
    if current_time < "08:00:00":
        # ⏳ 在等待 08:00 期间，周期性触活 session，避免"页面停留过长"
        logging.info("[main] 预热完成，等待 08:00:00 整点（期间保持 session 活跃）...")
        last_touch = time.time()
        while True:
            current_time = get_current_time(action)
            if current_time >= "08:00:00":
                break
            # 每 30 秒触活一次所有 session（防止长时间无请求导致 session 过期）
            now = time.time()
            if now - last_touch >= 30:
                logging.info("[main] 🔄 触活所有 session...")
                for item in prepared:
                    if item is None:
                        continue
                    item["s"].touch_session(item["roomid"], item["seatid"])
                last_touch = now
            time.sleep(0.1)
    else:
        logging.info("[main] 预热登录完成，已过 08:00，立即尝试提交...")

    # 🔑 08:00 整点：触活 session + 200ms 时钟偏差补偿
    # 服务器时钟可能比本地慢 100~500ms，提前到达会返回"未到开放时间"
    # 触活请求本身已消耗 ~100ms，再补 200ms 确保服务器也到了 08:00
    logging.info("[main] 🕐 08:00 整点，触活 session + 时钟偏差补偿...")
    for item in prepared:
        if item is None:
            continue
        item["s"].touch_session(item["roomid"], item["seatid"])
        time.sleep(random.uniform(0.05, 0.15))
    # 补偿服务器时钟偏差：等服务器也过 08:00:00
    CLOCK_SKEW_MS = 0.2  # 200ms
    logging.info(f"[main] ⏱️ 等待 {CLOCK_SKEW_MS*1000:.0f}ms 补偿服务器时钟偏差...")
    time.sleep(CLOCK_SKEW_MS)

    logging.info("[main] ⏰ 开始提交！")
    attempt_times = 0
    # do-while 模式：至少执行一轮，方便手动触发时验证 token 是否可获取
    while True:
        attempt_times += 1
        success_list = submit_all(prepared, success_list)
        done = sum(success_list)
        current_time = get_current_time(action)
        logging.info(f"[main] 第{attempt_times}轮 {current_time}, "
                     f"已完成 {done}/{today_reservation_num}, 状态={success_list}")
        if done == today_reservation_num:
            logging.info(f"[main] 🎉 全部预约成功！共 {attempt_times} 轮")
            return
        if current_time >= ENDTIME:
            logging.warning(f"[main] ⚠️ 已到截止时间 {ENDTIME}，"
                           f"尚有 {today_reservation_num - done} 人未成功")
            return
        time.sleep(SLEEPTIME)


def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    suc = False
    logging.info(f" Debug Mode start! , action {'on' if action else 'off'}")
    if action:
        usernames, passwords = get_user_credentials(action)
    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if type(seatid) == str:
            seatid = [seatid]
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue
        logging.info(f"----------- {username} -- {times} -- {seatid} try -----------")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        s.login(username, password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        suc = s.submit(times, roomid, seatid, action)
        if suc:
            return


def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    s.get_login_status()
    s.login(username=username, password=password)
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    encode = input("请输入deptldEnc：")
    s.roomid(encode)


if __name__ == "__main__":
    beijing_now = time.time() + 8 * 3600
    beijing_struct = time.gmtime(beijing_now)
    target_seconds = 8 * 3600
    current_seconds = beijing_struct.tm_hour * 3600 + beijing_struct.tm_min * 60 + beijing_struct.tm_sec
    wait = target_seconds - current_seconds

    # 🟢 提前 8 秒唤醒：3s 登录 + 3s token预热验证 + 2s 缓冲
    # 预热期间 session 保持活跃，避免 08:00 出现"页面停留过长"
    WAKE_BUFFER = 8
    if wait > WAKE_BUFFER:
        logging.info(f"距离北京时间 08:00:00 还有 {wait} 秒，等待中...")
        time.sleep(wait - WAKE_BUFFER)
        logging.info(f"提前 {WAKE_BUFFER} 秒唤醒，开始预热登录+token验证...")
    elif wait > 0:
        logging.info(f"距离08:00不足 {WAKE_BUFFER} 秒，立即预热登录...")
    else:
        logging.info("已过北京时间 08:00:00，立即执行")

    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room"],
        help="for debug",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action to enable in github action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
