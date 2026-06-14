from utils import AES_Encrypt, enc, generate_captcha_key, verify_param
import json
import random
import requests
import re
import time
import logging
import datetime
from urllib3.exceptions import InsecureRequestWarning


def get_date(day_offset: int = 0):
    tz_beijing = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(tz_beijing).date()
    offset_day = today + datetime.timedelta(days=day_offset)
    tomorrow = offset_day.strftime("%Y-%m-%d")
    return tomorrow


class reserve:
    def __init__(
        self,
        sleep_time=0.2,
        max_attempt=50,
        enable_slider=False,
        reserve_next_day=False,
    ):
        self.login_page = (
            "https://passport2.chaoxing.com/mlogin?loginType=1&newversion=true&fid="
        )
        self.url = (
            "https://office.chaoxing.com/front/third/apps/seat/code?id={}&seatNum={}"
        )
        self.submit_url = "https://office.chaoxing.com/data/apps/seat/submit"
        self.seat_url = "https://office.chaoxing.com/data/apps/seat/getusedtimes"
        self.login_url = "https://passport2.chaoxing.com/fanyalogin"
        self.token = ""
        self.success_times = 0
        self.fail_dict = []
        self.submit_msg = []
        self.requests = requests.session()
        self.token_pattern = re.compile("token = '(.*?)'")
        self.headers = {
            "Referer": "https://office.chaoxing.com/",
            "Host": "captcha.chaoxing.com",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        }
        self.login_headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "accept-encoding": "gzip, deflate, br, zstd",
            "cache-control": "no-cache",
            "Connection": "keep-alive",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 10_3_1 like Mac OS X) AppleWebKit/603.1.3 (KHTML, like Gecko) Version/10.0 Mobile/14E304 Safari/602.1 wechatdevtools/1.05.2109131 MicroMessenger/8.0.5 Language/zh_CN webview/16364215743155638",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Host": "passport2.chaoxing.com",
        }

        # 保存凭据，高峰期 session 过期时可自动重登录
        self._username = None
        self._password = None
        self._logged_in = False
        self.sleep_time = sleep_time
        self.max_attempt = max_attempt
        self.enable_slider = enable_slider
        self.reserve_next_day = reserve_next_day
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    @staticmethod
    def _detect_error_page(html, status_code):
        """检测高峰期服务器返回的是否为异常页面"""
        html_lower = html.lower()
        # 未登录 / session 过期特征
        if status_code in (302, 401, 403):
            return "HTTP状态码异常(可能未登录)"
        if len(html) < 100:
            return f"响应过短({len(html)}字符)"
        # 常见高峰期错误特征
        error_keywords = [
            ("请先登录", "页面要求重新登录"),
            ("登录已过期", "登录已过期"),
            ("请重新登录", "需要重新登录"),
            ("系统繁忙", "系统繁忙"),
            ("稍后再试", "服务器限流"),
            ("访问过于频繁", "访问过于频繁"),
            ("502", "网关错误502"),
            ("503", "服务不可用503"),
            ("try again", "服务器要求重试(英文)"),
        ]
        for keyword, desc in error_keywords:
            if keyword in html_lower or keyword in html:
                return f"检测到错误特征: {desc}"
        # 检查是否被重定向到登录页
        if "passport2.chaoxing.com" in html_lower and "login" in html_lower:
            return "疑似被重定向到登录页"
        return None  # 看起来是正常页面

    def _get_page_token(self, url, require_value=False):
        """通过 GET 获取 token 与 algorithm 值，高峰期自动重登录+指数退避"""
        fetch_headers = {
            "Referer": "https://office.chaoxing.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Host": "office.chaoxing.com",
            # 覆盖 session 中残留的 login_headers，避免服务器返回 JSON 而非 HTML
            "X-Requested-With": None,
            "Content-Type": None,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
        max_retries = 5
        re_logined = False  # 本轮是否已经尝试过重登录
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.requests.get(
                    url=url, headers=fetch_headers, timeout=15, verify=False
                )

                # 处理重定向（高峰期可能被 302 到登录页）
                if resp.status_code in (301, 302, 303, 307, 308):
                    redirect_url = resp.headers.get("Location", "未知")
                    logging.warning(
                        f"[token] 第{attempt}次被重定向({resp.status_code}) → {redirect_url[:100]}"
                    )
                    if "passport" in redirect_url.lower() or "login" in redirect_url.lower():
                        logging.warning("[token] ⚠️ 被重定向到登录页，session 可能已过期")
                        if not re_logined and self._username:
                            logging.info("[token] 🔄 尝试重登录...")
                            if self.re_login():
                                re_logined = True
                                continue  # 重登录成功，重试当前请求
                    if attempt < max_retries:
                        time.sleep(self.sleep_time * attempt)
                    continue

                if resp.status_code != 200:
                    logging.warning(
                        f"[token] 第{attempt}次 GET 返回 HTTP {resp.status_code}, url={url}"
                    )
                    if attempt < max_retries:
                        time.sleep(self.sleep_time * attempt)
                    continue

                html = resp.content.decode("utf-8")
                logging.debug(f"[token] 第{attempt}次响应长度={len(html)}")

                # 检查是否为错误页面
                error_reason = self._detect_error_page(html, resp.status_code)
                if error_reason:
                    logging.warning(
                        f"[token] 第{attempt}次检测到异常页面: {error_reason}, "
                        f"HTML预览(300字符): {html[:300]}"
                    )
                    # session 过期类错误 → 重登录
                    if ("登录" in error_reason or "重定向" in error_reason) and not re_logined and self._username:
                        logging.info("[token] 🔄 检测到登录态丢失，尝试重登录...")
                        if self.re_login():
                            re_logined = True
                            continue
                    # 限流/繁忙 → 加大等待时间
                    if "繁忙" in error_reason or "限流" in error_reason or "频繁" in error_reason:
                        wait = self.sleep_time * (2 ** attempt) + random.uniform(0, 1)
                        logging.info(f"[token] 高峰期限流，等待 {wait:.1f}s 后重试...")
                        time.sleep(wait)
                        continue
                    if attempt < max_retries:
                        time.sleep(self.sleep_time * attempt)
                    continue

                # 提取 submit_enc → token
                # 使用 [^>]* 允许 id 和 value 之间有其他属性
                token = ""
                for regex in (
                    r'<input[^>]*id="submit_enc"[^>]*value="([^"]*)"',
                    r'<input[^>]*value="([^"]*)"[^>]*id="submit_enc"',
                ):
                    token_match = re.findall(regex, html)
                    if token_match:
                        token = token_match[0]
                        logging.debug(f"[token] token 匹配到正则: {regex[:50]}...")
                        break
                if not token:
                    # 兜底：搜索 id="submit_enc" 附近的 value
                    m = re.search(r'id="submit_enc"', html)
                    if m:
                        nearby = html[m.end():m.end()+200]
                        v = re.search(r'value="([^"]*)"', nearby)
                        if v:
                            token = v.group(1)
                            logging.debug("[token] token 通过邻近搜索匹配到")

                # 提取 algorithm → value
                value = ""
                if require_value:
                    for regex in (
                        r'<input[^>]*id="algorithm"[^>]*value="([^"]*)"',
                        r'<input[^>]*name="algorithm"[^>]*value="([^"]*)"',
                        r'<input[^>]*value="([^"]*)"[^>]*id="algorithm"',
                        r'<input[^>]*value="([^"]*)"[^>]*name="algorithm"',
                    ):
                        m = re.findall(regex, html)
                        if m:
                            value = m[0]
                            logging.debug(f"[token] value 匹配到正则: {regex[:50]}...")
                            break
                    if not value:
                        m = re.search(r'(?:id|name)="algorithm"', html)
                        if m:
                            nearby = html[max(0,m.start()-100):m.end()+200]
                            v = re.search(r'value="([^"]*)"', nearby)
                            if v:
                                value = v.group(1)
                        if not value:
                            all_values = re.findall(r'value="(.*?)"', html)
                            logging.warning(
                                f"[token] 所有 algorithm 正则均未匹配, "
                                f"页面 value 片段(前5): {all_values[:5]}"
                            )

                if token:
                    logging.info(
                        f"[token] 第{attempt}次成功, token_len={len(token)}, "
                        f"value_len={len(value)}, url={url}"
                    )
                    return token, value

                # token 为空但页面看似正常
                logging.warning(
                    f"[token] 第{attempt}次未匹配到 token, "
                    f"HTML预览(500字符): {html[:500]}"
                )
                if attempt < max_retries:
                    # 指数退避 + 随机抖动，高峰期避免同时重试
                    wait = self.sleep_time * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    logging.debug(f"[token] 等待 {wait:.1f}s 后重试...")
                    time.sleep(wait)

            except requests.exceptions.Timeout:
                logging.warning(f"[token] 第{attempt}次请求超时（高峰期常见）")
                if attempt < max_retries:
                    wait = self.sleep_time * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait)
            except Exception as e:
                logging.warning(f"[token] 第{attempt}次请求异常: {e}")
                if attempt < max_retries:
                    wait = self.sleep_time * attempt
                    time.sleep(wait)

        logging.error(f"[token] 全部{max_retries}次重试均失败, url={url}")
        return "", ""

    def get_login_status(self):
        logging.info("[login] 获取登录页 Cookie...")
        self.requests.headers = self.login_headers
        resp = self.requests.get(url=self.login_page, verify=False)
        logging.info(f"[login] 登录页 HTTP {resp.status_code}, cookie数量={len(self.requests.cookies)}")

    def login(self, username, password):
        # 保存凭据以备高峰期重登录
        self._username = username
        self._password = password
        enc_username = AES_Encrypt(username)
        enc_password = AES_Encrypt(password)
        parm = {
            "fid": -1,
            "uname": enc_username,
            "password": enc_password,
            "refer": "http%3A%2F%2Foffice.chaoxing.com%2Ffront%2Fthird%2Fapps%2Fseat%2Fcode%3Fid%3D4219%26seatNum%3D380",
            "t": True,
        }
        resp = self.requests.post(url=self.login_url, params=parm, verify=False)
        obj = resp.json()
        if obj.get("status"):
            self._logged_in = True
            logging.info(f"[login] 用户 {username} 登录成功")
            return (True, "")
        else:
            self._logged_in = False
            msg = obj.get("msg2", obj.get("msg", "未知错误"))
            logging.warning(f"[login] 用户 {username} 登录失败: {msg}")
            return (False, msg)

    def re_login(self):
        """高峰期 session 过期时用已保存的凭据重新登录"""
        if not self._username or not self._password:
            logging.error("[re_login] 无可用凭据，无法重登录")
            return False
        logging.info(f"[re_login] 尝试重登录 {self._username} ...")
        # 先获取新的 cookie
        self.get_login_status()
        ok, msg = self.login(self._username, self._password)
        if ok:
            self.requests.headers.update({"Host": "office.chaoxing.com"})
            logging.info(f"[re_login] ✅ 重登录成功")
            return True
        else:
            logging.error(f"[re_login] ❌ 重登录失败: {msg}")
            return False

    def warmup_token(self, roomid, seatid):
        """预热验证：登录后立即尝试获取 token，提前发现 session 问题。
        在 08:00 前调用，不消耗关键时刻的重试次数。"""
        seatid = seatid[0] if isinstance(seatid, list) else seatid
        url = self.url.format(roomid, seatid)
        logging.info(f"[warmup] 预热验证 session, url={url}")
        token, value = self._get_page_token(url, require_value=True)
        if token:
            logging.info(f"[warmup] ✅ session 有效, token_len={len(token)}")
            return True
        else:
            logging.warning(f"[warmup] ❌ session 无效或页面异常，将触发重登录")
            return False

    def touch_session(self, roomid, seatid):
        """触活 session：访问 office 主页保持 cookie 活跃。
        ⚠️ 刻意不访问座位页面，避免生成无用 token 导致后续'页面停留过久'。"""
        # 使用 office 主页而非座位页面，不会触发 token 生成计时
        url = "https://office.chaoxing.com/"
        fetch_headers = {
            "Referer": "https://office.chaoxing.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Host": "office.chaoxing.com",
            "X-Requested-With": None,
            "Content-Type": None,
        }
        try:
            resp = self.requests.get(url=url, headers=fetch_headers, timeout=10, verify=False)
            logging.info(f"[touch] session 触活 HTTP {resp.status_code}, len={len(resp.text)}")
            return resp.status_code == 200
        except Exception as e:
            logging.warning(f"[touch] session 触活失败: {e}")
            return False

    def roomid(self, encode):
        url = f"https://office.chaoxing.com/data/apps/seat/room/list?cpage=1&pageSize=100&firstLevelName=&secondLevelName=&thirdLevelName=&deptIdEnc={encode}"
        json_data = self.requests.get(url=url).content.decode("utf-8")
        ori_data = json.loads(json_data)
        for i in ori_data["data"]["seatRoomList"]:
            info = f'{i["firstLevelName"]}-{i["secondLevelName"]}-{i["thirdLevelName"]} id为：{i["id"]}'
            print(info)

    def resolve_captcha(self):
        logging.info(f"Start to resolve captcha token")
        captcha_token, bg, tp = self.get_slide_captcha_data()
        logging.info(f"Successfully get prepared captcha_token {captcha_token}")
        logging.info(f"Captcha Image URL-small {tp}, URL-big {bg}")
        x = self.x_distance(bg, tp)
        logging.info(f"Successfully calculate the captcha distance {x}")
        params = {
            "callback": "jQuery33109180509737430778_1716381333117",
            "captchaId": "42sxgHoTPTKbt0uZxPJ7ssOvtXr3ZgZ1",
            "type": "slide",
            "token": captcha_token,
            "textClickArr": json.dumps([{"x": x}]),
            "coordinate": json.dumps([]),
            "runEnv": "10",
            "version": "1.1.18",
            "_": int(time.time() * 1000),
        }
        response = self.requests.get(
            f"https://captcha.chaoxing.com/captcha/check/verification/result",
            params=params,
            headers=self.headers,
        )
        text = response.text.replace(
            "jQuery33109180509737430778_1716381333117(", ""
        ).replace(")", "")
        data = json.loads(text)
        logging.info(f"Successfully resolve the captcha token {data}")
        try:
            validate_val = json.loads(data["extraData"])["validate"]
            return validate_val
        except KeyError as e:
            logging.info("Can't load validate value. Maybe server return mistake.")
            return ""

    def get_slide_captcha_data(self):
        url = "https://captcha.chaoxing.com/captcha/get/verification/image"
        timestamp = int(time.time() * 1000)
        capture_key, token = generate_captcha_key(timestamp)
        referer = f"https://office.chaoxing.com/front/third/apps/seat/code?id=3993&seatNum=0199"
        params = {
            "callback": f"jQuery33107685004390294206_1716461324846",
            "captchaId": "42sxgHoTPTKbt0uZxPJ7ssOvtXr3ZgZ1",
            "type": "slide",
            "version": "1.1.18",
            "captchaKey": capture_key,
            "token": token,
            "referer": referer,
            "_": timestamp,
            "d": "a",
            "b": "a",
        }
        response = self.requests.get(url=url, params=params, headers=self.headers)
        content = response.text
        data = content.replace(
            "jQuery33107685004390294206_1716461324846(", ")"
        ).replace(")", "")
        data = json.loads(data)
        captcha_token = data["token"]
        bg = data["imageVerificationVo"]["shadeImage"]
        tp = data["imageVerificationVo"]["cutoutImage"]
        return captcha_token, bg, tp

    def x_distance(self, bg, tp):
        import numpy as np
        import cv2

        def cut_slide(slide):
            slider_array = np.frombuffer(slide, np.uint8)
            slider_image = cv2.imdecode(slider_array, cv2.IMREAD_UNCHANGED)
            slider_part = slider_image[:, :, :3]
            mask = slider_image[:, :, 3]
            mask[mask != 0] = 255
            x, y, w, h = cv2.boundingRect(mask)
            cropped_image = slider_part[y : y + h, x : x + w]
            return cropped_image

        c_captcha_headers = {
            "Referer": "https://office.chaoxing.com/",
            "Host": "captcha-b.chaoxing.com",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }
        bgc, tpc = self.requests.get(bg, headers=c_captcha_headers), self.requests.get(
            tp, headers=c_captcha_headers
        )
        bg, tp = bgc.content, tpc.content
        bg_img = cv2.imdecode(np.frombuffer(bg, np.uint8), cv2.IMREAD_COLOR)
        tp_img = cut_slide(tp)
        bg_edge = cv2.Canny(bg_img, 100, 200)
        tp_edge = cv2.Canny(tp_img, 100, 200)
        bg_pic = cv2.cvtColor(bg_edge, cv2.COLOR_GRAY2RGB)
        tp_pic = cv2.cvtColor(tp_edge, cv2.COLOR_GRAY2RGB)
        res = cv2.matchTemplate(bg_pic, tp_pic, cv2.TM_CCOEFF_NORMED)
        _, _, _, max_loc = cv2.minMaxLoc(res)
        tl = max_loc
        return tl[0]

    def submit(self, times, roomid, seatid, action):
        for seat in seatid:
            suc = False
            remaining = self.max_attempt
            while not suc and remaining > 0:
                token, value = self._get_page_token(
                    self.url.format(roomid, seat), require_value=True
                )
                if not token:
                    logging.warning(f"[submit] seat={seat} token为空，等待重试...")
                    time.sleep(self.sleep_time)
                    remaining -= 1
                    continue
                captcha = self.resolve_captcha() if self.enable_slider else ""
                if captcha:
                    logging.info(f"[submit] 滑块验证码: {captcha[:20]}...")
                suc, msg = self.get_submit(
                    self.submit_url,
                    times=times,
                    token=token,
                    roomid=roomid,
                    seatid=seat,
                    captcha=captcha,
                    action=action,
                    value=value,
                )
                if suc:
                    return suc
                # "未到开放时间" → 短暂等待后立即重试，不消耗 attempt
                if self._is_not_open_yet(msg):
                    logging.info(f"[submit] ⏳ 未到开放时间，100ms后重试（不消耗次数）...")
                    time.sleep(0.1)
                    continue  # remaining 不减少
                time.sleep(self.sleep_time)
                remaining -= 1
            logging.warning(f"[submit] seat={seat} 已耗尽所有尝试次数")
        return False

    # 匹配"未到开放时间"类消息的关键词（按实际日志校准）
    NOT_OPEN_YET_KEYWORDS = [
        # 精确命中实际返回: "当前区域未到开放预约时间，请咨询管理员确认该区域开放时间"
        "未到开放",        # 匹配 "未到开放时间" / "未到开放预约时间"
        "当前区域未到开放",
        # 其他可能变体
        "预约未开始", "不在预约时间", "未到预约时间",
        "还未开放", "暂未开放", "未开始预约",
        "未到开放时间",     # 保留精确匹配
    ]

    # 匹配"页面停留过久"/token过期类消息（按实际日志: 代码:303）
    TOKEN_EXPIRED_KEYWORDS = [
        "页面停留过久",
        "安全验证已超时",
        "请刷新后再提交",
        "代码:303",
        "页面已过期",
        "token已过期",
        "token过期",
    ]

    @classmethod
    def _is_not_open_yet(cls, msg):
        """判断提交失败原因是否为'未到开放时间'（服务器时钟比本地慢）"""
        if not msg:
            return False
        msg_lower = msg.lower()
        for kw in cls.NOT_OPEN_YET_KEYWORDS:
            if kw in msg_lower or kw in msg:
                return True
        return False

    @classmethod
    def _is_token_expired(cls, msg):
        """判断提交失败原因是否为 token 过期/页面停留过久（需刷新页面重取token）"""
        if not msg:
            return False
        msg_lower = msg.lower()
        for kw in cls.TOKEN_EXPIRED_KEYWORDS:
            if kw in msg_lower or kw in msg:
                return True
        return False

    def get_submit(
        self, url, times, token, roomid, seatid, captcha="", action=False, value=""
    ):
        """提交预约，返回 (success: bool, message: str)"""
        delta_day = 1 if self.reserve_next_day else 0
        tz_beijing = datetime.timezone(datetime.timedelta(hours=8))
        beijing_today = datetime.datetime.now(tz_beijing)
        day = beijing_today.date() + datetime.timedelta(days=delta_day)
        parm = {
            "roomId": roomid,
            "startTime": times[0],
            "endTime": times[1],
            "day": str(day),
            "seatNum": seatid,
            "captcha": captcha,
            "token": token,
            "type": "1",
            "verifyData": "1",
        }
        logging.info(f"[submit] 请求参数 roomId={roomid} seatNum={seatid} "
                     f"day={day} {times[0]}~{times[1]}")
        parm["enc"] = verify_param(parm, value)
        resp = self.requests.post(url=url, params=parm, verify=True)
        html = resp.content.decode("utf-8")
        try:
            result = json.loads(html)
        except json.JSONDecodeError:
            msg = f"HTTP={resp.status_code}, 内容={html[:200]}"
            logging.error(f"[submit] 响应非JSON: {msg}")
            return (False, msg)
        # 提取服务器返回的消息
        msg = str(result.get("msg", result.get("message", result.get("msg2", ""))))
        self.submit_msg.append(f"{times[0]}~{times[1]}: {result}")
        success = result.get("success", False)
        if success:
            logging.info(f"[submit] ✅ 预约成功! {result}")
        elif self._is_not_open_yet(msg):
            logging.warning(f"[submit] ⏳ 服务器返回'未到开放时间'(服务器时钟偏慢): {msg}")
        else:
            logging.warning(f"[submit] ❌ 预约失败: {result}")
        return (success, msg)
