import os
import re
import time
import logging
import requests
import random
import ddddocr
import urllib3
import threading
from io import BytesIO
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from queue import Queue

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def env(key, default):
    v = os.environ.get(key)
    if v is None or v == '':
        return default
    t = type(default)
    if t is bool:
        return v.lower() in ('1', 'true', 'yes')
    if t is int:
        return int(v)
    return v


# ==================== 配置 (支持环境变量覆盖) ====================
CONFIG = {
    # 代理
    'use_proxy':          env('USE_PROXY', True),
    'proxy_file':         env('PROXY_FILE', 'IP.txt'),
    'proxy_list_urls':    env('PROXY_LIST_URLS',
        'https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt,'
        'https://raw.githubusercontent.com/komutan234/Proxy-List-Free/main/proxies/http.txt,'
        'https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt,'
        'https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt,'
        'https://raw.githubusercontent.com/ABoredCat/Free-Proxy/main/proxies/http.txt,'
        'https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt,'
        'https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt,'
        'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt,'
        'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt,'
        'https://proxy.scdn.io/text.php'
    ),
    'max_per_ip':         env('MAX_PER_IP', 15),
    'proxy_check_threads': env('PROXY_CHECK_THREADS', 100),
    'proxy_check_timeout': env('PROXY_CHECK_TIMEOUT', 2),
    'proxy_warmup':        env('PROXY_WARMUP', 20),  # 启动前等待就绪代理数
    'proxy_check_url':    env('PROXY_CHECK_URL', 'https://ptlogin.4399.com/ptlogin/captcha.do?captchaId=test'),

    # 验证码识别
    'use_custom_model': env('USE_CUSTOM_MODEL', True),
    'custom_model_file': env('CUSTOM_MODEL_FILE', 'captcha_model.pth'),

    # 注册
    'max_captcha_retry': env('MAX_CAPTCHA_RETRY', 3),
    'max_sfz_uses':     env('MAX_SFZ_USES', 4),
    'captcha_length':   env('CAPTCHA_LENGTH', 4),
    'username_prefix':  env('USERNAME_PREFIX', ''),
    'username_len':     env('USERNAME_LEN', 7),
    'password_len':     env('PASSWORD_LEN', 10),

    # 请求
    'captcha_url': 'https://ptlogin.4399.com/ptlogin/captcha.do?captchaId={}',
    'register_url': 'https://ptlogin.4399.com/ptlogin/register.do',
    'headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://ptlogin.4399.com/',
    },

    # 登录获取 sauth
    'auto_login':   env('AUTO_LOGIN', True),

    # 文件
    'sfz_file':      env('SFZ_FILE', 'sfz.txt'),
    'used_sfz_file': env('USED_SFZ_FILE', 'used_sfz.txt'),
    'output_file':   env('OUTPUT_FILE', '4399.txt'),
    'sauth_file':    env('SAUTH_FILE', 'sauth.json'),
    'log_file':      env('LOG_FILE', 'register.log'),

    # 并发
    'workers':      env('WORKERS', 15),
    'min_interval': env('MIN_INTERVAL', 2),
    'max_interval': env('MAX_INTERVAL', 3),
}

ALPHABET = 'abcdefghijklmnopqrstuvwxyz1234567890'

ERROR_MAP = {
    '注册成功':     'success',
    '验证码错误':   'captcha_wrong',
    '请稍后再试':   'rate_limit',
    '身份证实名帐号数量超过限制': 'sfz_limit',
    '身份证实名过于频繁':       'sfz_freq',
    '该姓名身份证提交验证过于频繁': 'sfz_name_freq',
    '用户名已被注册': 'username_taken',
    'HTTP ERROR 500': 'server_500',
    '503 Service Temporarily Unavailable': 'server_503',
    '服务器繁忙':   'server_busy',
}

# ==================== 初始化 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(CONFIG['log_file'], encoding='utf-8'),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

if CONFIG['use_custom_model']:
    from captcha_pipeline import CaptchaRecognizer
    ocr_engine = CaptchaRecognizer(CONFIG['custom_model_file'])
    log.info(f'使用自定义模型: {CONFIG["custom_model_file"]}')
else:
    ocr_engine = ddddocr.DdddOcr(show_ad=False)
    log.info('使用 ddddocr')

if CONFIG['auto_login']:
    from login_4399 import do_login
    log.info('自动登录已启用')


def load_lines(file):
    for enc in ('utf-8', 'gbk', 'gb2312', 'latin-1'):
        try:
            with open(file, 'r', encoding=enc) as f:
                return [line.strip() for line in f if line.strip()]
        except (UnicodeDecodeError, UnicodeError):
            continue
        except FileNotFoundError:
            return []
    return []


def parse_sfz(line):
    parts = line.split('----')
    if len(parts) == 2 and len(parts[0]) in [2, 3] and len(parts[1]) == 18:
        return parts[0], parts[1]
    return None, None


# ==================== 代理管理器 ====================
class ProxyManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._ready = []          # 已验证可用的代理
        self._in_use = {}         # {proxy: 使用次数} 当前被线程占用的代理
        self._bad = set()         # 失效代理
        self._usage = {}          # {proxy: 累计注册成功次数}
        self._fail_count = {}     # {proxy: 连续失败次数}
        self._direct_count = 0    # 直连IP计数
        self._raw = []            # 待验证的原始代理（仅用于去重）
        self._raw_q = Queue()     # 待验证队列（快速出队）
        self._check_threads = CONFIG['proxy_check_threads']
        self._check_pool = ThreadPoolExecutor(max_workers=self._check_threads)
        self._refilling = False
        self._checked = 0         # 已验证总数（用于进度日志）

    def _submit_raw(self, proxies):
        """批量提交代理到验证队列并调度验证任务"""
        for p in proxies:
            self._raw_q.put(p)
            self._check_pool.submit(self._check_one_and_store)

    def load_proxies(self):
        file_proxies = load_lines(CONFIG['proxy_file'])
        if file_proxies:
            self._raw.extend(file_proxies)
            log.info(f'从文件加载 {len(file_proxies)} 个代理')
        self.fetch_from_list()
        self.fetch_from_scrapers()
        if self._raw:
            log.info(f'开始验证 {len(self._raw)} 个代理 ({self._check_threads} 并发)')
            self._submit_raw(self._raw)

    def _known_set(self):
        return set(self._raw) | set(self._ready) | self._bad | set(self._in_use.keys())

    def fetch_from_list(self):
        urls = [u.strip() for u in CONFIG['proxy_list_urls'].split(',') if u.strip()]
        existing = self._known_set()
        total_added = 0
        for url in urls:
            try:
                resp = requests.get(url, timeout=15)
                if resp.status_code == 200:
                    lines = [line.strip() for line in resp.text.splitlines() if line.strip() and ':' in line]
                    added = [p for p in lines if p not in existing]
                    self._raw.extend(added)
                    existing.update(added)
                    total_added += len(added)
                    log.info(f'  + {url.split("/")[-1]}: {len(added)} 个待验证')
                else:
                    log.warning(f'  x {url.split("/")[-1]}: HTTP {resp.status_code}')
            except Exception as e:
                log.warning(f'  x {url.split("/")[-1]}: {e}')
        log.info(f'本次拉取 {total_added} 个代理, 累计 {len(self._raw)} 个')

    def fetch_from_scrapers(self):
        """从需要解析HTML的代理站抓取"""
        existing = self._known_set()
        total_added = 0
        ip_port_re = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})[^\d](\d{2,5})')

        def _scrape(name, url, headers=None):
            nonlocal total_added
            try:
                resp = requests.get(url, headers=headers or {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }, timeout=10, verify=False)
                if resp.status_code != 200:
                    log.warning(f'  x {name}: HTTP {resp.status_code}')
                    return
                proxies = [f'{m[0]}:{m[1]}' for m in ip_port_re.findall(resp.text)
                           if 1 <= int(m[1]) <= 65535 and m[0] not in ('0.0.0.0', '127.0.0.1')]
                added = [p for p in proxies if p not in existing]
                self._raw.extend(added)
                existing.update(added)
                total_added += len(added)
                log.info(f'  + {name}: {len(added)} 个待验证')
            except Exception as e:
                log.warning(f'  x {name}: {e}')

        # 快代理 (kuaidaili.com)
        for page in range(1, 4):
            _scrape(f'kuaidaili/p{page}', f'https://www.kuaidaili.com/free/inha/{page}/')
            time.sleep(0.5)

        log.info(f'爬虫抓取完成, 新增 {total_added} 个代理')

    def _check_one(self, proxy):
        proxies = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}
        try:
            r = requests.get(CONFIG['proxy_check_url'],
                             proxies=proxies, timeout=CONFIG['proxy_check_timeout'])
            return r.status_code == 200
        except Exception:
            return False

    def _check_one_and_store(self):
        """从队列取一个代理验证，结果存入对应池"""
        try:
            proxy = self._raw_q.get_nowait()
        except Exception:
            return
        ok = self._check_one(proxy)
        with self._lock:
            if ok and proxy not in self._bad and proxy not in self._ready and proxy not in self._in_use:
                self._ready.append(proxy)
            else:
                self._bad.add(proxy)
            self._checked += 1
            if self._checked % 200 == 0:
                log.info(f'验证进度: {self._checked}, 就绪 {len(self._ready)}, 失效 {len(self._bad)}')

    def _refill(self):
        """补充代理池（带锁防并发重复填充）"""
        with self._lock:
            if self._refilling:
                return
            self._refilling = True
        try:
            before = len(self._raw)
            self.fetch_from_list()
            self.fetch_from_scrapers()
            added = self._raw[before:]
            if added:
                log.info(f'补充提交 {len(added)} 个代理验证')
                self._submit_raw(added)
                return

            # 在线源没拉到新代理，把失效代理全部重验
            with self._lock:
                if not self._bad:
                    return
                recycled = list(self._bad)
                self._bad.clear()
                self._raw.extend(recycled)
            log.info(f'无新代理来源, 重验 {len(recycled)} 个失效代理')
            self._submit_raw(recycled)
        finally:
            with self._lock:
                self._refilling = False

    def acquire(self):
        """获取一个独占代理（每个线程不同IP），边验证边获取"""
        with self._lock:
            if not CONFIG['use_proxy']:
                if self._direct_count >= CONFIG['max_per_ip']:
                    log.warning(f'直连IP已达上限 {CONFIG["max_per_ip"]}, 需要开启代理')
                    return None
                return {}

        # 轮询等待就绪代理（验证线程在后台持续产出）
        for i in range(60):  # 最多等30秒
            with self._lock:
                for j, proxy in enumerate(self._ready):
                    if self._usage.get(proxy, 0) < CONFIG['max_per_ip']:
                        self._ready.pop(j)
                        self._in_use[proxy] = self._usage.get(proxy, 0)
                        return {'http': f'http://{proxy}', 'https': f'http://{proxy}',
                                '_proxy': proxy}

                # 所有就绪代理都达上限，重置 usage 让它们可复用
                if self._ready and all(
                    self._usage.get(p, 0) >= CONFIG['max_per_ip'] for p in self._ready
                ):
                    for p in self._ready:
                        self._usage.pop(p, None)
                    log.info(f'所有就绪代理已达上限, 已重置 {len(self._ready)} 个代理的使用计数')
                    continue  # 立即重试，不等待

                raw_left = self._raw_q.qsize()
                ready_cnt = len(self._ready)
                bad_cnt = len(self._bad)
            # 每5秒打印一次等待状态
            if i > 0 and i % 10 == 0:
                log.info(f'等待可用代理... 就绪{ready_cnt} 待验证{raw_left} 失效{bad_cnt}')
            # ready 池空且没有待验证的了，触发补充
            if raw_left == 0 and ready_cnt == 0:
                self._refill()
            time.sleep(0.5)
        with self._lock:
            log.warning(f'等待超时, 就绪{len(self._ready)} 失效{len(self._bad)}, 无可用代理')
        return None

    def release(self, proxies, success=False):
        """释放代理回池"""
        if not proxies or '_proxy' not in proxies:
            if not CONFIG['use_proxy'] and success:
                with self._lock:
                    self._direct_count += 1
            return
        proxy = proxies['_proxy']
        with self._lock:
            self._in_use.pop(proxy, None)
            if success:
                self._usage[proxy] = self._usage.get(proxy, 0) + 1
                self._fail_count.pop(proxy, None)  # 成功清零失败计数
                if self._usage[proxy] < CONFIG['max_per_ip']:
                    self._ready.append(proxy)
            else:
                self._ready.append(proxy)

    def soft_fail(self, proxies):
        """网络错误，不直接丢弃，放回池里重试，连续10次才丢"""
        if not proxies or '_proxy' not in proxies:
            return
        proxy = proxies['_proxy']
        with self._lock:
            self._in_use.pop(proxy, None)
            cnt = self._fail_count.get(proxy, 0) + 1
            self._fail_count[proxy] = cnt
            if cnt >= 3:
                self._bad.add(proxy)
                self._fail_count.pop(proxy, None)
                log.warning(f'代理连续失败{cnt}次, 丢弃: {proxy}')
            else:
                self._ready.append(proxy)
                log.info(f'代理网络错误({cnt}/3), 放回重试: {proxy}')

    def mark_bad(self, proxies):
        """代理被封/被限频，直接丢弃"""
        if not proxies or '_proxy' not in proxies:
            return
        proxy = proxies['_proxy']
        with self._lock:
            self._in_use.pop(proxy, None)
            self._bad.add(proxy)
            self._fail_count.pop(proxy, None)
            log.warning(f'代理失效: {proxy} (已注册 {self._usage.get(proxy, 0)} 次)')

    def stats(self):
        with self._lock:
            ready = len(self._ready)
            busy = len(self._in_use)
            bad = len(self._bad)
            raw = self._raw_q.qsize()
            return f'代理池: 就绪{ready} 使用中{busy} 待验证{raw} 失效{bad}'


# ==================== 验证码 ====================
def _upscale(img_bytes, scale=3):
    img = Image.open(BytesIO(img_bytes))
    w, h = img.size
    img = img.resize((w * scale, h * scale), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def _clean_result(raw):
    return ''.join(c for c in raw if c.isalnum())


def recognize_captcha(img_bytes):
    if not img_bytes or len(img_bytes) < 100:
        return None
    if CONFIG['use_custom_model']:
        try:
            result = ocr_engine.recognize(img_bytes)
            if len(result) == CONFIG['captcha_length']:
                return result
        except Exception:
            pass
        return None
    try:
        strategies = [('raw', img_bytes), ('upscaled', _upscale(img_bytes))]
    except Exception:
        return None
    for name, data in strategies:
        try:
            raw = ocr_engine.classification(data)
        except Exception:
            continue
        result = _clean_result(raw)
        if len(result) == CONFIG['captcha_length']:
            return result
    return None


# ==================== 注册 ====================
def match_error(html):
    for keyword, code in ERROR_MAP.items():
        if keyword in html:
            return code
    return None


def load_valid_sfz():
    all_lines = load_lines(CONFIG['sfz_file'])
    result = []
    for line in all_lines:
        name, idcard = parse_sfz(line)
        if name:
            result.append((line, name, idcard))
    return result


def pick_sfz(valid_sfz, used_count):
    candidates = [item for item in valid_sfz if used_count.get(item[0], 0) < CONFIG['max_sfz_uses']]
    if not candidates:
        return None, None, None
    return random.choice(candidates)


file_lock = threading.Lock()
success_counter = 0
success_lock = threading.Lock()
registered_accounts = []  # 本次注册成功的账号 [(username, password), ...]


_NET_ERRORS = (
    requests.exceptions.ProxyError,
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


def _try_register_once(username, password, realname, idcard, proxies, proxy_manager, session):
    """用一个代理尝试注册（含验证码重试），返回 (结果, 需要换代理)"""
    for attempt in range(CONFIG['max_captcha_retry'] + 1):
        sessionId = 'captchaReq' + ''.join(random.sample(ALPHABET, 19))
        try:
            captcha_img = session.get(
                url=CONFIG['captcha_url'].format(sessionId),
                headers=CONFIG['headers'], proxies=proxies, timeout=10).content
        except _NET_ERRORS:
            proxy_manager.soft_fail(proxies)
            return 'net_error', True
        yzm_data = recognize_captcha(captcha_img)
        if yzm_data is None:
            continue

        post = {
            'postLoginHandler': 'default', 'displayMode': 'popup',
            'appId': 'www_home', 'gameId': '', 'cid': '', 'externalLogin': 'qq',
            'aid': '', 'ref': '', 'css': '', 'redirectUrl': '',
            'regMode': 'reg_normal', 'sessionId': sessionId,
            'regIdcard': 'true', 'noEmail': 'false',
            'crossDomainIFrame': '', 'crossDomainUrl': '',
            'mainDivId': 'popup_reg_div', 'showRegInfo': 'true',
            'includeFcmInfo': 'false', 'expandFcmInput': 'false',
            'fcmFakeValidate': 'true',
            'username': username, 'password': password, 'passwordveri': password,
            'email': f'ADWMC_{"".join(random.sample(ALPHABET, 5))}@qq.com',
            'inputCaptcha': yzm_data, 'reg_eula_agree': 'on',
            'realname': realname, 'idcard': idcard,
        }
        try:
            html = session.post(url=CONFIG['register_url'], data=post,
                                proxies=proxies, timeout=15, headers=CONFIG['headers']).text
        except _NET_ERRORS:
            proxy_manager.soft_fail(proxies)
            return 'net_error', True

        code = match_error(html)
        if code == 'success':
            return 'success', False
        elif code == 'captcha_wrong':
            continue
        elif code in ('server_503', 'server_busy', 'rate_limit'):
            proxy_manager.mark_bad(proxies)
            return code, True
        elif code:
            return code, False
        else:
            return 'unknown', False

    return 'captcha_exhausted', True


def register_4399(username, password, valid_sfz, used_count, proxy_manager):
    """注册一个账号，网络失败自动换代理重试（指数退避）"""
    with file_lock:
        sfz_line, realname, idcard = pick_sfz(valid_sfz, used_count)
    if not sfz_line:
        return 'no_sfz'

    max_proxy_tries = 3
    session = requests.Session()
    try:
        for proxy_try in range(max_proxy_tries):
            proxies = proxy_manager.acquire()
            if proxies is None:
                return 'no_proxy'

            result, need_switch = _try_register_once(
                username, password, realname, idcard, proxies, proxy_manager, session)

            if result == 'success':
                proxy_manager.release(proxies, success=True)
                with file_lock:
                    count = used_count.get(sfz_line, 0) + 1
                    used_count[sfz_line] = count
                    with open(CONFIG['output_file'], 'a', encoding='utf-8') as fh:
                        fh.write(f'{username}----{password}\n')
                    with open(CONFIG['used_sfz_file'], 'a', encoding='utf-8') as fh2:
                        fh2.write(f'{sfz_line}----{count}\n')
                    registered_accounts.append((username, password))
                with success_lock:
                    global success_counter
                    success_counter += 1
                    cur = success_counter
                log.info(f'[+] 注册成功 {username}----{password} (sfz {count}/{CONFIG["max_sfz_uses"]}) (总成功: {cur})')
                return 'success'

            if not need_switch:
                proxy_manager.release(proxies)
                return result

            # 指数退避: 1s, 2s, 4s (加 jitter 防止雷群效应)
            if proxy_try < max_proxy_tries - 1:
                backoff = (2 ** proxy_try) + random.uniform(0, 0.5)
                log.info(f'代理失败({result}), {backoff:.1f}s 后换代理重试 {username} ({proxy_try+2}/{max_proxy_tries})')
                time.sleep(backoff)
    finally:
        session.close()

    return result


def run_once(valid_sfz, used_count, proxy_manager):
    prefix = CONFIG['username_prefix']
    rand_len = CONFIG['username_len'] - len(prefix)
    username = prefix + ''.join(random.sample(ALPHABET, rand_len))
    password = ''.join(random.sample(ALPHABET, CONFIG['password_len']))
    try:
        return register_4399(username, password, valid_sfz, used_count, proxy_manager)
    except Exception as e:
        log.error(f'异常: {e.__class__.__name__}: {e}')
        return 'error'


def batch_login(proxy_manager, accounts):
    """注册完成后批量登录获取sauth"""
    if not accounts:
        log.info('无账号需要登录')
        return

    log.info(f'=== 开始批量登录 ({len(accounts)} 个账号) ===')
    import json as _json
    login_ok = 0
    login_fail = 0

    for username, password in accounts:
        proxies = proxy_manager.acquire()
        if proxies is None:
            log.warning(f'登录 {username}: 无可用代理, 跳过')
            login_fail += 1
            continue
        try:
            sauth = do_login(username, password, proxies, CONFIG['headers'],
                             ocr_engine, CONFIG['use_custom_model'])
            if sauth:
                with file_lock:
                    with open(CONFIG['sauth_file'], 'a', encoding='utf-8') as sf:
                        sf.write(_json.dumps({
                            'username': username, 'password': password,
                            'sauth': sauth
                        }, ensure_ascii=False) + '\n')
                proxy_manager.release(proxies, success=True)
                login_ok += 1
                log.info(f'[+] 登录成功 {username} ({login_ok}/{len(accounts)})')
            else:
                proxy_manager.release(proxies)
                login_fail += 1
                log.warning(f'[!] 登录失败 {username}')
        except Exception as e:
            proxy_manager.release(proxies)
            login_fail += 1
            log.warning(f'[!] 登录异常 {username}: {e}')
        time.sleep(random.uniform(0.5, 1.5))

    log.info(f'=== 批量登录完成: 成功 {login_ok}, 失败 {login_fail} ===')


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=int, default=0, help='运行时长(秒), 0=无限')
    parser.add_argument('--count', type=int, default=0, help='成功注册数量, 0=不限')
    args = parser.parse_args()

    proxy_manager = ProxyManager()
    if CONFIG['use_proxy']:
        proxy_manager.load_proxies()
        warmup = CONFIG['proxy_warmup']
        log.info(f'预热中, 等待 {warmup} 个代理就绪...')
        for i in range(120):
            with proxy_manager._lock:
                ready = len(proxy_manager._ready)
                bad = len(proxy_manager._bad)
            raw = proxy_manager._raw_q.qsize()
            if ready >= warmup or raw == 0:
                break
            if i % 10 == 0 and i > 0:
                log.info(f'预热进度: 就绪 {ready}/{warmup}, 待验证 {raw}, 失效 {bad}')
            time.sleep(1)
        with proxy_manager._lock:
            ready = len(proxy_manager._ready)
        log.info(f'代理池就绪: {ready} 个可用代理')
        if ready == 0:
            log.warning('警告: 没有可用代理, 注册可能会失败')

    used_count = {}
    for line in load_lines(CONFIG['used_sfz_file']):
        parts = line.rsplit('----', 1)
        if len(parts) == 2 and parts[1].isdigit():
            sfz_key = parts[0]
            used_count[sfz_key] = max(used_count.get(sfz_key, 0), int(parts[1]))
        else:
            used_count[line] = CONFIG['max_sfz_uses']
    valid_sfz = load_valid_sfz()
    available = sum(1 for item in valid_sfz if used_count.get(item[0], 0) < CONFIG['max_sfz_uses'])
    log.info(f'已加载 {len(valid_sfz)} 条有效身份证, 可用 {available} 条, 并发 {CONFIG["workers"]} 线程, 单IP上限 {CONFIG["max_per_ip"]} 次')
    if args.count > 0:
        log.info(f'目标: 注册 {args.count} 个账号')
    if args.duration > 0:
        log.info(f'限时: {args.duration} 秒')

    deadline = time.time() + args.duration if args.duration > 0 else None
    last_success_time = time.time()
    last_activity_time = time.time()  # 最近一次有代理可用的时间

    with ThreadPoolExecutor(max_workers=CONFIG['workers']) as pool:
        pending = set()
        try:
            while True:
                if deadline and time.time() >= deadline:
                    log.info(f'已达到运行时长 {args.duration} 秒, 停止')
                    break
                if args.count > 0 and success_counter >= args.count:
                    log.info(f'已达到目标数量 {args.count} 个, 停止')
                    break
                if time.time() - last_success_time > 30 and time.time() - last_activity_time > 30:
                    log.warning('30秒无代理可用且无注册成功, 停止运行')
                    break

                # 持续填满线程池，不等待整批完成
                while len(pending) < CONFIG['workers']:
                    pending.add(pool.submit(run_once, valid_sfz, used_count, proxy_manager))

                # 等任意一个完成，处理结果，立即补充新任务
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for f in done:
                    result = f.result()
                    if result == 'success':
                        last_success_time = time.time()
                        last_activity_time = time.time()
                    elif result == 'no_proxy':
                        pass  # 不更新 activity 时间
                    else:
                        last_activity_time = time.time()
                        log.info(f'结果: {result}')
                    if result == 'ip_limit':
                        log.warning('所有IP已达注册上限, 停止运行')
                        raise SystemExit

                if CONFIG['use_proxy'] and len(pending) % CONFIG['workers'] == 0:
                    log.info(proxy_manager.stats())

        except KeyboardInterrupt:
            log.info('已停止')

    log.info(f'注册阶段结束, 总成功注册: {success_counter} 个')

    if CONFIG['auto_login'] and registered_accounts:
        batch_login(proxy_manager, registered_accounts)

    log.info(f'全部完成, 注册 {success_counter} 个, 登录 {len(registered_accounts)} 个')
