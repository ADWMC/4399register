import json
import logging
import requests
import ddddocr
from uuid import uuid4
from re import search

log = logging.getLogger(__name__)


def generate_uuid():
    return str(uuid4()).replace("-", "").upper()


def recognize_captcha_login(img_bytes, ocr_engine, use_custom_model):
    if use_custom_model:
        result = ocr_engine.recognize(img_bytes)
        if len(result) == 4 and result.isalnum():
            return result.lower()
        return None
    else:
        while True:
            captcha = ocr_engine.classification(img_bytes)
            if len(captcha) == 4 and captcha.isalnum():
                return captcha.lower()


def check_verify_code(username, proxies, headers):
    url = f"http://ptlogin.4399.com/ptlogin/verify.do?username={username}&appId=kid_wdsj&t={generate_uuid()}&inputWidth=iptw2&v=1"
    resp = requests.get(url, cookies={"USESSIONID": generate_uuid()}, proxies=proxies, headers=headers, timeout=10)
    match = search(r"/ptlogin/captcha\.do\?captchaId=[\w\d]+", resp.text)
    if match:
        return match.group(0).split("=")[1], f"http://ptlogin.4399.com{match.group(0)}"
    return "", ""


def login(username, password, proxies, headers, ocr_engine, use_custom_model, verifycode="", verifysession=""):
    login_url = "http://ptlogin.4399.com/ptlogin/login.do?v=1"
    if verifycode:
        payload = {
            'postLoginHandler': 'default', 'externalLogin': 'qq',
            'bizId': '2100001792', 'appId': 'kid_wdsj', 'gameId': 'wd', 'sec': '1',
            'password': password, 'username': username,
            'redirectUrl': '', 'sessionId': verifysession, 'inputCaptcha': verifycode
        }
    else:
        payload = {
            'postLoginHandler': 'default', 'externalLogin': 'qq',
            'bizId': '2100001792', 'appId': 'kid_wdsj', 'gameId': 'wd', 'sec': '1',
            'password': password, 'username': username
        }

    resp = requests.post(login_url,
                         cookies={"ptusertype": "kid_wdsj.4399_login", "USESSIONID": generate_uuid()},
                         data=payload, proxies=proxies, headers=headers, timeout=15)

    if resp.status_code != 200:
        raise Exception(f"登录失败 HTTP {resp.status_code}")

    cookies = resp.cookies.get_dict()
    if not cookies.get("Uauth") or not cookies.get("Puser"):
        raise Exception("账号密码错误或IP被拉黑")

    check_url = (
        f"http://ptlogin.4399.com/ptlogin/checkKidLoginUserCookie.do?"
        f"appId=kid_wdsj&gameUrl=http://cdn.h5wan.4399sj.com/microterminal-h5-frame?"
        f"game_id=500352&rand_time={cookies['Uauth'].split('|')[4]}&nick=null"
        f"&onLineStart=false&show=1&isCrossDomain=1"
        f"&retUrl=http%253A%252F%252Fptlogin.4399.com%252Fresource%252Fucenter.html"
    )
    check_resp = requests.post(check_url, cookies=cookies, proxies=proxies, headers=headers, timeout=15)
    if check_resp.status_code != 200:
        raise Exception(f"校验实名失败 HTTP {check_resp.status_code}")

    user_info = requests.get(
        "https://microgame.5054399.net/v2/service/sdk/info?callback=",
        params={'queryStr': check_resp.url.split('?')[1].strip()},
        proxies=proxies, headers=headers, timeout=15
    ).json()

    if not user_info.get('data'):
        raise Exception(f"用户信息获取失败: {user_info.get('msg')}")

    return {k: v for k, v in (item.split('=') for item in user_info['data']['sdk_login_data'].split('&'))}


def do_login(username, password, proxies, headers, ocr_engine, use_custom_model):
    session_id, captcha_url = check_verify_code(username, proxies, headers)
    captcha = ""
    if captcha_url:
        try:
            img = requests.get(captcha_url, proxies=proxies, headers=headers, timeout=10).content
            captcha = recognize_captcha_login(img, ocr_engine, use_custom_model)
            if not captcha:
                return None
        except Exception as e:
            log.warning(f'登录验证码获取失败: {e}')
            return None

    user_data = login(username, password, proxies, headers, ocr_engine, use_custom_model, captcha, session_id)
    sauth_data = {
        "gameid": "x19",
        "login_channel": "4399pc",
        "app_channel": "4399pc",
        "platform": "pc",
        "sdkuid": user_data["uid"],
        "sessionid": user_data["token"],
        "sdk_version": "1.0.0",
        "udid": generate_uuid(),
        "deviceid": generate_uuid(),
        "aim_info": json.dumps({"aim": "127.0.0.1", "country": "CN", "tz": "0800", "tzid": ""}),
        "client_login_sn": generate_uuid(),
        "gas_token": "",
        "source_platform": "pc",
        "ip": "127.0.0.1",
        "userid": user_data["username"],
        "realname": json.dumps({"realname_type": "0"}),
        "timestamp": user_data["time"]
    }
    return json.dumps({"sauth_json": json.dumps(sauth_data)}).replace(" ", "")
