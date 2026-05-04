import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

INPUT_FILE = 'proxies_raw.txt'
OUTPUT_FILE = 'IP.txt'
TIMEOUT = 5
WORKERS = 50
TEST_URL = 'https://httpbin.org/ip'


def check(proxy):
    try:
        resp = requests.get(
            TEST_URL,
            proxies={'http': f'http://{proxy}', 'https': f'http://{proxy}'},
            timeout=TIMEOUT
        )
        if resp.status_code == 200:
            return proxy
    except:
        pass
    return None


def main():
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        proxies = [l.strip() for l in f if l.strip()]
    print(f'共 {len(proxies)} 个代理, {WORKERS} 线程验证中...')

    valid = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(check, p): p for p in proxies}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result:
                valid.append(result)
                print(f'  [{len(valid)}] 可用: {result}')
            if i % 100 == 0:
                print(f'  已测试 {i}/{len(proxies)}')

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for p in sorted(valid):
            f.write(p + '\n')
    print(f'\n完成: {len(valid)}/{len(proxies)} 可用, 已写入 {OUTPUT_FILE}')


if __name__ == '__main__':
    main()
