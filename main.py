import json
import time
import argparse
import os
import logging

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
    """提前登录并拿好token，返回预热好的session列表"""
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
        logging.info(f"----------- 预热 {username} -- {times} -- {seatid} -----------")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        s.login(username, password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        # 提前拿token
        token_map = {}
        for seat in seatid:
            url = s.url.format(roomid, seat)
            token, value = s._get_page_token(url, require_value=True)
            token_map[seat] = (token, value)
            logging.info(f"预热拿到 seat={seat} token={token}")
        prepared.append({
            "s": s,
            "times": times,
            "roomid": roomid,
            "seatid": seatid,
            "token_map": token_map,
            "action": action,
        })
    return prepared


def submit_all(prepared, success_list):
    """08:00整直接用已有token提交"""
    for index, item in enumerate(prepared):
        if item is None or success_list[index]:
            continue
        s = item["s"]
        times = item["times"]
        roomid = item["roomid"]
        seatid = item["seatid"]
        token_map = item["token_map"]
        action = item["action"]
        for seat in seatid:
            token, value = token_map.get(seat, ("", ""))
            if not token:
                logging.warning(f"seat={seat} token为空，跳过")
                continue
            suc = s.get_submit(
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
    return success_list


def main(users, action=False):
    current_time = get_current_time(action)
    logging.info(f"start time {current_time}, action {'on' if action else 'off'}")
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    current_dayofweek = get_current_dayofweek(action)
    today_reservation_num = sum(
        1 for d in users if current_dayofweek in d.get("daysofweek")
    )
    success_list = [False] * len(users)

    # 用预热好的数据提交
    prepared = prepare_all(users, usernames, passwords, action)
    logging.info("预热完成，等待08:00整提交...")

    # 等到08:00整
    while True:
        current_time = get_current_time(action)
        if current_time >= "08:00:00":
            break
        time.sleep(0.1)

    logging.info("08:00整，开始提交！")
    attempt_times = 0
    while current_time < ENDTIME:
        attempt_times += 1
        success_list = submit_all(prepared, success_list)
        print(f"attempt time {attempt_times}, time now {current_time}, success list {success_list}")
        current_time = get_current_time(action)
        if sum(success_list) == today_reservation_num:
            print("reserved successfully!")
            return
        # token用完了重新拿
        if attempt_times % 3 == 0:
            logging.info("重新获取token...")
            prepared = prepare_all(users, usernames, passwords, action)
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

    if wait > 30:
        logging.info(f"距离北京时间 08:00:00 还有 {wait} 秒，等待中...")
        time.sleep(wait - 30)  # 提前30秒唤醒预热
        logging.info("提前30秒开始预热...")
    elif wait > 0:
        logging.info(f"距离08:00不足30秒，立即预热...")
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