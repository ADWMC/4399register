import os
import time
import logging
import requests
import random
import ddddocr
import urllib3
import threading
from io import BytesIO
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        'https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt'
    ),
    'max_per_ip':         env('MAX_PER_IP', 15),
    'proxy_check_threads': env('PROXY_CHECK_THREADS', 50),
    'proxy_check_timeout': env('PROXY_CHECK_TIMEOUT', 5),
    'proxy_check_url':    env('PROXY_CHECK_URL', 'http://httpbin.org/ip'),

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
    'workers':      env('WORKERS', 3),
    'min_interval': env('MIN_INTERVAL', 1),
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
        self._direct_count = 0    # 直连IP计数
        self._raw = []            # 待验证的原始代理
        self._check_threads = CONFIG['proxy_check_threads']
        self._stop_event = threading.Event()

    def load_proxies(self):
        file_proxies = load_lines(CONFIG['proxy_file'])
        if file_proxies:
            self._raw.extend(file_proxies)
            log.info(f'从文件加载 {len(file_proxies)} 个代理')
        self.fetch_from_list()
        self._start_checker()

    def fetch_from_list(self):
        urls = [u.strip() for u in CONFIG['proxy_list_urls'].split(',') if u.strip()]
        existing = set(self._raw) | set(self._ready) | self._bad | set(self._in_use.keys())
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
        log.info(f'本次拉取 {total_added} 个代理, 待验证队列 {len(self._raw)} 个')

    def _check_one(self, proxy):
        """验证单个代理是否可用"""
        proxies = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}
        try:
            r = requests.get(CONFIG['proxy_check_url'],
                             proxies=proxies, timeout=CONFIG['proxy_check_timeout'])
            return r.status_code == 200
        except Exception:
            return False

    def _checker_worker(self):
        """验证线程：从 _raw 取代理，验证后放入 _ready 或 _bad"""
        while not self._stop_event.is_set():
            with self._lock:
                if not self._raw:
                    return
                proxy = self._raw.pop(0)
            if self._check_one(proxy):
                with self._lock:
                    if proxy not in self._bad:
                        self._ready.append(proxy)
            else:
                with self._lock:
                    self._bad.add(proxy)

    def _start_checker(self):
        """启动多线程批量验证"""
        if not self._raw:
            return
        log.info(f'启动 {self._check_threads} 线程验证 {len(self._raw)} 个代理...')
        with ThreadPoolExecutor(max_workers=self._check_threads) as pool:
            list(pool.map(lambda p: self._checker_worker(), range(min(self._check_threads, len(self._raw)))))
        log.info(f'验证完成: 可用 {len(self._ready)}, 失效 {len(self._bad)}')

    def _refill(self):
        """补充代理池：拉取新代理并验证"""
        self.fetch_from_list()
        if self._raw:
            log.info(f'补充验证 {len(self._raw)} 个代理...')
            with ThreadPoolExecutor(max_workers=self._check_threads) as pool:
                list(pool.map(lambda p: self._checker_worker(), range(min(self._check_threads, len(self._raw)))))
            log.info(f'补充完成: 可用 {len(self._ready)}, 失效 {len(self._bad)}')

    def acquire(self):
        """获取一个独占代理（每个线程不同IP）"""
        with self._lock:
            if not CONFIG['use_proxy']:
                if self._direct_count >= CONFIG['max_per_ip']:
                    log.warning(f'直连IP已达上限 {CONFIG["max_per_ip"]}, 需要开启代理')
                    return None
                return {}
            # 从 ready 池取一个未超限的
            for i, proxy in enumerate(self._ready):
                if self._usage.get(proxy, 0) < CONFIG['max_per_ip']:
                    self._ready.pop(i)
                    self._in_use[proxy] = self._usage.get(proxy, 0)
                    return {'http': f'http://{proxy}', 'https': f'http://{proxy}',
                            '_proxy': proxy}
            # ready 池空了，尝试补充
            pass
        # 锁外补充
        self._refill()
        with self._lock:
            for i, proxy in enumerate(self._ready):
                if self._usage.get(proxy, 0) < CONFIG['max_per_ip']:
                    self._ready.pop(i)
                    self._in_use[proxy] = self._usage.get(proxy, 0)
                    return {'http': f'http://{proxy}', 'https': f'http://{proxy}',
                            '_proxy': proxy}
        return {}

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
                # 如果还没超限，放回 ready 池复用
                if self._usage[proxy] < CONFIG['max_per_ip']:
                    self._ready.append(proxy)
            else:
                # 失败也放回，下次重试（可能临时故障）
                self._ready.append(proxy)

    def mark_bad(self, proxies):
        """标记代理失效"""
        if not proxies or '_proxy' not in proxies:
            return
        proxy = proxies['_proxy']
        with self._lock:
            self._in_use.pop(proxy, None)
            self._bad.add(proxy)
            log.warning(f'代理失效: {proxy} (已注册 {self._usage.get(proxy, 0)} 次)')

    def stats(self):
        with self._lock:
            ready = len(self._ready)
            busy = len(self._in_use)
            bad = len(self._bad)
            return f'代理池: 就绪{ready} 使用中{busy} 失效{bad}'


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


def register_4399(username, password, valid_sfz, used_count, proxy_manager):
    proxies = proxy_manager.acquire()
    if proxies is None:
        return 'ip_limit'
    if not proxies:
        return 'no_proxy'

    with file_lock:
        sfz_line, realname, idcard = pick_sfz(valid_sfz, used_count)
    if not sfz_line:
        proxy_manager.release(proxies)
        return 'no_sfz'

    for attempt in range(CONFIG['max_captcha_retry'] + 1):
        sessionId = 'captchaReq' + ''.join(random.sample(ALPHABET, 19))
        try:
            captcha_img = requests.get(
                url=CONFIG['captcha_url'].format(sessionId),
                headers=CONFIG['headers'], proxies=proxies, timeout=10).content
        except (requests.exceptions.ProxyError,
                requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout):
            proxy_manager.mark_bad(proxies)
            return 'net_error'
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
            html = requests.post(url=CONFIG['register_url'], data=post,
                                 proxies=proxies, timeout=15, headers=CONFIG['headers']).text
        except (requests.exceptions.ProxyError,
                requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout):
            proxy_manager.mark_bad(proxies)
            return 'net_error'

        code = match_error(html)
        if code == 'success':
            proxy_manager.release(proxies, success=True)
            with file_lock:
                count = used_count.get(sfz_line, 0) + 1
                used_count[sfz_line] = count
                with open(CONFIG['output_file'], 'a', encoding='utf-8') as fh:
                    fh.write(f'{username}----{password}\n')
                with open(CONFIG['used_sfz_file'], 'a', encoding='utf-8') as fh2:
                    fh2.write(f'{sfz_line}----{count}\n')
            with success_lock:
                global success_counter
                success_counter += 1
                cur = success_counter
            log.info(f'[+] 注册成功 {username}----{password} (sfz {count}/{CONFIG["max_sfz_uses"]}) (总成功: {cur})')

            if CONFIG['auto_login']:
                try:
                    sauth = do_login(username, password, proxies, CONFIG['headers'],
                                     ocr_engine, CONFIG['use_custom_model'])
                    if sauth:
                        with file_lock:
                            import json as _json
                            with open(CONFIG['sauth_file'], 'a', encoding='utf-8') as sf:
                                sf.write(_json.dumps({
                                    'username': username, 'password': password,
                                    'sauth': sauth
                                }, ensure_ascii=False) + '\n')
                        log.info(f'[+] 登录成功 {username} -> sauth 已保存')
                    else:
                        log.warning(f'[!] 登录失败 {username}')
                except Exception as e:
                    log.warning(f'[!] 登录异常 {username}: {e}')

            return 'success'
        elif code == 'captcha_wrong':
            continue
        elif code in ('server_503', 'server_busy', 'rate_limit'):
            proxy_manager.mark_bad(proxies)
            return code
        elif code:
            proxy_manager.release(proxies)
            return code
        else:
            proxy_manager.release(proxies)
            return 'unknown'

    proxy_manager.release(proxies)
    return 'captcha_exhausted'


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


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=int, default=0, help='运行时长(秒), 0=无限')
    parser.add_argument('--count', type=int, default=0, help='成功注册数量, 0=不限')
    args = parser.parse_args()

    proxy_manager = ProxyManager()
    if CONFIG['use_proxy']:
        proxy_manager.load_proxies()

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

    with ThreadPoolExecutor(max_workers=CONFIG['workers']) as pool:
        try:
            while True:
                if deadline and time.time() >= deadline:
                    log.info(f'已达到运行时长 {args.duration} 秒, 停止')
                    break

                if args.count > 0 and success_counter >= args.count:
                    log.info(f'已达到目标数量 {args.count} 个, 停止')
                    break

                futures = []
                for _ in range(CONFIG['workers']):
                    f = pool.submit(run_once, valid_sfz, used_count, proxy_manager)
                    futures.append(f)
                    time.sleep(random.uniform(0.2, 0.5))

                for f in as_completed(futures):
                    result = f.result()
                    if result == 'ip_limit':
                        log.warning('所有IP已达注册上限, 停止运行')
                        raise SystemExit
                    if result not in ('success',):
                        log.info(f'结果: {result}')

                if CONFIG['use_proxy']:
                    log.info(proxy_manager.stats())

                time.sleep(random.uniform(CONFIG['min_interval'], CONFIG['max_interval']))
        except KeyboardInterrupt:
            log.info('已停止')

    log.info(f'本次运行结束, 总成功注册: {success_counter} 个')
