import os
import sys
import json
import shutil
import argparse
import random
import time
import numpy as np
from collections import Counter
from PIL import Image
from io import BytesIO
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

# ==================== 统一配置 ====================
CONFIG = {
    # 下载
    'captcha_url': 'https://ptlogin.4399.com/ptlogin/captcha.do?captchaId={}',
    'download_total': 2000,
    'download_delay': 0.3,

    # 路径
    'captcha_dir': 'captchas',
    'labeled_dir': 'captchas_labeled',
    'labels_file': 'captcha_labels.json',
    'model_file': 'captcha_model.pth',

    # 模型
    'img_w': 120,
    'img_h': 40,
    'chars': 'abcdefghijklmnopqrstuvwxyz0123456789',
    'captcha_len': 4,

    # 训练
    'epochs': 80,
    'batch_size': 32,
    'lr': 0.0005,
}

ALPHABET = 'abcdefghijklmnopqrstuvwxyz1234567890'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://ptlogin.4399.com/',
}

char_to_idx = {c: i for i, c in enumerate(CONFIG['chars'])}
idx_to_char = {i: c for c, i in char_to_idx.items()}
NUM_CLASSES = len(CONFIG['chars']) + 1


# ==================== 模型定义 ====================
class CaptchaModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
            nn.Dropout2d(0.3),
        )
        self.classifier = nn.Sequential(
            nn.AdaptiveAvgPool2d((1, CONFIG['captcha_len'])),
            nn.Flatten(),
            nn.Linear(128 * CONFIG['captcha_len'], 256),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(256, NUM_CLASSES * CONFIG['captcha_len']),
        )

    def forward(self, x):
        b = x.size(0)
        x = self.features(x)
        x = self.classifier(x)
        return x.view(b, CONFIG['captcha_len'], NUM_CLASSES)


# ==================== 数据集 ====================
class CaptchaDataset(Dataset):
    def __init__(self, labeled_dir, labels_file):
        with open(labels_file, 'r', encoding='utf-8') as f:
            labels = json.load(f)
        self.samples = []
        for fname, label in labels.items():
            if not label or len(label) != CONFIG['captcha_len']:
                continue
            if not all(c in char_to_idx for c in label.lower()):
                continue
            path = os.path.join(labeled_dir, f'{label}_{fname}')
            if os.path.exists(path):
                self.samples.append((path, label.lower()))

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        path, label = self.samples[idx]
        img = Image.open(path).convert('L')

        # 数据增强
        if np.random.random() < 0.3:
            angle = np.random.uniform(-5, 5)
            img = img.rotate(angle, fillcolor=128)
        if np.random.random() < 0.3:
            img = Image.fromarray(np.clip(
                np.array(img, dtype=np.int16) + np.random.randint(-20, 20), 0, 255).astype(np.uint8))

        img = img.resize((CONFIG['img_w'], CONFIG['img_h']))
        arr = np.array(img, dtype=np.float32) / 255.0
        arr = (arr - 0.5) / 0.5
        tensor = torch.from_numpy(arr).unsqueeze(0)
        target = torch.tensor([char_to_idx[c] for c in label], dtype=torch.long)
        return tensor, target


# ==================== 阶段1: 下载 ====================
def collect():
    import requests
    save_dir = CONFIG['captcha_dir']
    os.makedirs(save_dir, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)
    total = CONFIG['download_total']

    print(f'开始下载 {total} 张验证码 -> {save_dir}/')
    for i in range(total):
        sid = 'captchaReq' + ''.join(random.sample(ALPHABET, 19))
        try:
            resp = session.get(CONFIG['captcha_url'].format(sid), timeout=10)
            if resp.status_code == 200 and len(resp.content) > 100:
                path = os.path.join(save_dir, f'{sid}.png')
                with open(path, 'wb') as f:
                    f.write(resp.content)
                if (i + 1) % 100 == 0:
                    print(f'已下载 {i + 1}/{total}')
            time.sleep(CONFIG['download_delay'])
        except Exception as e:
            print(f'下载失败: {e}')
            time.sleep(1)

    count = len([f for f in os.listdir(save_dir) if f.endswith('.png')])
    print(f'下载完成, 共 {count} 张图片')


# ==================== 阶段2: 标注 ====================
def label():
    import ddddocr
    captcha_dir = CONFIG['captcha_dir']
    labeled_dir = CONFIG['labeled_dir']
    labels_file = CONFIG['labels_file']
    os.makedirs(labeled_dir, exist_ok=True)

    ocr = ddddocr.DdddOcr(show_ad=False)
    files = [f for f in os.listdir(captcha_dir) if f.endswith('.png')]
    print(f'共 {len(files)} 张图片, 开始预标注...')

    labels = {}
    for i, fname in enumerate(files):
        path = os.path.join(captcha_dir, fname)
        with open(path, 'rb') as f:
            img_bytes = f.read()

        results = []
        # 原图识别
        try:
            raw = ocr.classification(img_bytes)
            cleaned = ''.join(c for c in raw if c.isalnum())
            if len(cleaned) == CONFIG['captcha_len']:
                results.append(cleaned)
        except Exception:
            pass

        # 放大 3 倍再试
        try:
            img = Image.open(BytesIO(img_bytes))
            w, h = img.size
            img = img.resize((w * 3, h * 3), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format='PNG')
            raw = ocr.classification(buf.getvalue())
            cleaned = ''.join(c for c in raw if c.isalnum())
            if len(cleaned) == CONFIG['captcha_len']:
                results.append(cleaned)
        except Exception:
            pass

        if results:
            label_text = Counter(results).most_common(1)[0][0]
            labels[fname] = label_text
            dst = os.path.join(labeled_dir, f'{label_text}_{fname}')
            shutil.copy2(path, dst)
        else:
            labels[fname] = ''

        if (i + 1) % 100 == 0:
            valid = sum(1 for v in labels.values() if v)
            print(f'已处理 {i + 1}/{len(files)}, 有效标注 {valid}')

    with open(labels_file, 'w', encoding='utf-8') as f:
        json.dump(labels, f, ensure_ascii=False, indent=2)

    valid = sum(1 for v in labels.values() if v)
    print(f'完成: {valid}/{len(files)} 有效标注')
    print(f'标注文件: {labels_file}')
    print(f'标注图片: {labeled_dir}/')
    print(f'请人工检查标注图片, 删除错误的, 然后运行: python captcha_pipeline.py train')


# ==================== 阶段3: 训练 ====================
def train():
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    dataset = CaptchaDataset(CONFIG['labeled_dir'], CONFIG['labels_file'])
    if len(dataset) < 100:
        print(f'标注数据太少 ({len(dataset)}), 请先运行: python captcha_pipeline.py label')
        return

    print(f'训练集: {len(dataset)} 样本, 设备: {device}')

    train_size = int(0.9 * len(dataset))
    val_size = len(dataset) - train_size
    train_set, val_set = torch.utils.data.random_split(dataset, [train_size, val_size])

    train_loader = DataLoader(train_set, batch_size=CONFIG['batch_size'], shuffle=True, num_workers=0)
    val_loader = DataLoader(val_set, batch_size=CONFIG['batch_size'], shuffle=False, num_workers=0)

    model = CaptchaModel().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=CONFIG['lr'], weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=CONFIG['epochs'])
    criterion = nn.CrossEntropyLoss()

    best_acc = 0
    for epoch in range(CONFIG['epochs']):
        model.train()
        total_loss = 0
        correct = 0
        total = 0

        for images, targets in train_loader:
            images = images.to(device)
            targets = targets.to(device)

            outputs = model(images)
            loss = 0
            for i in range(CONFIG['captcha_len']):
                loss += criterion(outputs[:, i, :], targets[:, i])

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

            preds = decode_output(outputs.cpu())
            truths = [''.join(idx_to_char[c.item()] for c in row) for row in targets.cpu()]
            for p, t in zip(preds, truths):
                if p == t:
                    correct += 1
                total += 1

        scheduler.step()
        train_acc = correct / total * 100

        # 验证
        model.eval()
        val_correct = 0
        val_total = 0
        with torch.no_grad():
            for images, targets in val_loader:
                images = images.to(device)
                targets = targets.to(device)
                outputs = model(images)
                preds = decode_output(outputs.cpu())
                truths = [''.join(idx_to_char[c.item()] for c in row) for row in targets.cpu()]
                for p, t in zip(preds, truths):
                    if p == t:
                        val_correct += 1
                    val_total += 1

        val_acc = val_correct / val_total * 100 if val_total > 0 else 0
        print(f'Epoch {epoch+1}/{CONFIG["epochs"]} | Loss: {total_loss:.4f} | Train Acc: {train_acc:.1f}% | Val Acc: {val_acc:.1f}%')

        if val_acc > best_acc:
            best_acc = val_acc
            torch.save(model.state_dict(), CONFIG['model_file'])
            print(f'  -> 模型已保存 (Val Acc: {val_acc:.1f}%)')

    print(f'\n训练完成, 最佳验证准确率: {best_acc:.1f}%')
    print(f'模型文件: {CONFIG["model_file"]}')


def decode_output(pred):
    indices = pred.argmax(dim=2)
    results = []
    for row in indices:
        s = ''.join(idx_to_char.get(i.item(), '') for i in row)
        results.append(s)
    return results


# ==================== 推理模块 ====================
class CaptchaRecognizer:
    def __init__(self, model_path=None, device=None):
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = CaptchaModel().to(self.device)
        self.model.load_state_dict(torch.load(
            model_path or CONFIG['model_file'], map_location=self.device, weights_only=True))
        self.model.eval()

    def recognize(self, img_bytes):
        img = Image.open(BytesIO(img_bytes)).convert('L').resize((CONFIG['img_w'], CONFIG['img_h']))
        arr = np.array(img, dtype=np.float32) / 255.0
        arr = (arr - 0.5) / 0.5
        tensor = torch.from_numpy(arr).unsqueeze(0).unsqueeze(0).to(self.device)
        with torch.no_grad():
            output = self.model(tensor)
        indices = output.argmax(dim=2)[0]
        return ''.join(idx_to_char.get(i.item(), '') for i in indices)


# ==================== 主入口 ====================
def main():
    parser = argparse.ArgumentParser(description='验证码模型训练流水线')
    parser.add_argument('command', choices=['collect', 'label', 'train', 'all'],
                        help='collect=下载, label=标注, train=训练, all=全流程')
    args = parser.parse_args()

    if args.command == 'collect':
        collect()
    elif args.command == 'label':
        label()
    elif args.command == 'train':
        train()
    elif args.command == 'all':
        collect()
        label()
        train()


if __name__ == '__main__':
    main()
