import os
import datetime
import json
import hmac
import hashlib
from flask import Flask, request, jsonify, abort
import requests
from peewee import (
    SqliteDatabase,
    Model,
    IntegerField,
    CharField,
    DateTimeField,
    BigAutoField,
)
from apscheduler.schedulers.background import BackgroundScheduler

# --- 全局变量 ---
TELEGRAM_BOT_TOKEN = None
WEBHOOK_SECRET = None
TARGETS = []
GITHUB_TARGET_USER = 'YuzakiKokuban'
DB_FILE = "sent_messages.db"
CONFIG_FILE = "config.json"
CLEANUP_DAYS = 7

http_session = requests.Session()
FILE_ID_CACHE = {}
app = Flask(__name__)

# --- 加载配置的函数 ---
def load_config():
    global TARGETS, TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
            TARGETS = config_data.get('targets', [])
            TELEGRAM_BOT_TOKEN = config_data.get('telegram_bot_token')
            WEBHOOK_SECRET = config_data.get('webhook_secret')
            
            print(f"成功从 {CONFIG_FILE} 加载了配置喵~")
            if not TELEGRAM_BOT_TOKEN or 'placeholder' in TELEGRAM_BOT_TOKEN:
                print("警告: telegram_bot_token 未在 config.json 中正确配置！")
            if not WEBHOOK_SECRET or 'placeholder' in WEBHOOK_SECRET:
                print("警告: webhook_secret 未在 config.json 中正确配置！签名验证将不会启用。")

    except Exception as e:
        print(f"加载配置文件 {CONFIG_FILE} 出错: {e}")
        TARGETS, TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET = [], None, None

# --- 数据库设置 ---
db = SqliteDatabase(DB_FILE)

class SentMessage(Model):
    id = BigAutoField(primary_key=True)
    chat_id = CharField()
    message_id = IntegerField()
    sent_at = DateTimeField(default=datetime.datetime.now)

    class Meta:
        database = db

# --- 消息发送函数 (为简洁省略) ---
def send_message_to_target(message, target_config):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    params = {'chat_id': target_config['chat_id'], 'text': message, 'parse_mode': 'Markdown'}
    if 'message_thread_id' in target_config:
        params['message_thread_id'] = target_config['message_thread_id']
    try:
        response = http_session.post(api_url, json=params, timeout=10)
        response.raise_for_status(); response_data = response.json()
        return response_data['result']['message_id']
    except (requests.exceptions.RequestException, KeyError) as e:
        print(f"发送文本消息到 {target_config['chat_id']} 时出错啦: {e}"); return None

def upload_document_and_get_id(caption, file_stream, file_name, target_config):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    params = {'chat_id': target_config['chat_id'], 'caption': caption, 'parse_mode': 'Markdown'}
    if 'message_thread_id' in target_config:
        params['message_thread_id'] = target_config['message_thread_id']
    files = {'document': (file_name, file_stream)}
    try:
        response = http_session.post(api_url, data=params, files=files, timeout=180)
        response.raise_for_status(); response_data = response.json()
        file_id = response_data['result']['document']['file_id']
        message_id = response_data['result']['message_id']
        return file_id, message_id
    except (requests.exceptions.RequestException, KeyError) as e:
        print(f"上传文件并获取 id 时出错: {e}"); return None, None

def send_document_by_id(caption, file_id, target_config):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    params = {'chat_id': target_config['chat_id'], 'document': file_id, 'caption': caption, 'parse_mode': 'Markdown'}
    if 'message_thread_id' in target_config:
        params['message_thread_id'] = target_config['message_thread_id']
    try:
        response = http_session.post(api_url, json=params, timeout=10)
        response.raise_for_status(); response_data = response.json()
        return response_data['result']['message_id']
    except (requests.exceptions.RequestException, KeyError) as e:
        print(f"用 file_id 发送文件到 {target_config['chat_id']} 时出错惹: {e}"); return None

# --- 清理旧消息的函数 (为简洁省略) ---
def cleanup_old_messages():
    cleanup_threshold = datetime.datetime.now() - datetime.timedelta(days=CLEANUP_DAYS)
    old_messages = SentMessage.select().where(SentMessage.sent_at < cleanup_threshold)
    if not old_messages: return
    count = 0
    for msg in old_messages:
        try:
            api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
            params = {'chat_id': msg.chat_id, 'message_id': msg.message_id}
            response = http_session.post(api_url, json=params, timeout=10)
            if response.status_code == 200 or response.status_code == 400:
                msg.delete_instance(); count += 1
            else: response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"删除消息 (ID: {msg.message_id}) 时出错: {e}")

@app.route('/webhook', methods=['POST'])
def github_webhook():
    # --- Webhook 签名验证 ---
    if WEBHOOK_SECRET and 'placeholder' not in WEBHOOK_SECRET:
        signature = request.headers.get('X-Hub-Signature-256')
        if not signature: abort(403)
        sha_name, signature_hex = signature.split('=', 1)
        if sha_name != 'sha256': abort(403)
        mac = hmac.new(WEBHOOK_SECRET.encode('utf-8'), msg=request.data, digestmod=hashlib.sha256)
        if not hmac.compare_digest(mac.hexdigest(), signature_hex): abort(403)
        print("签名验证成功喵~ 是主人发的请求！")
    else:
        print("警告: WEBHOOK_SECRET 未配置，跳过签名验证。")

    if not TARGETS:
        return jsonify({'status': 'ignored', 'reason': 'no targets configured'}), 200

    if request.headers.get('X-GitHub-Event') != 'release':
        return jsonify({'status': 'ignored'}), 200

    data = request.json
    
    try:
        if data['repository']['owner']['login'].lower() != GITHUB_TARGET_USER.lower():
            return jsonify({'status': 'ignored'}), 200
    except KeyError:
         return jsonify({'status': 'error', 'message': 'Malformed payload'}), 400

    if data.get('action') == 'published':
        # ... (此处代码与之前版本相同，为简洁省略)
        try:
            repo_name = data['repository']['full_name']; release_info = data['release']
            tag_name = release_info['tag_name']; release_url = release_info['html_url']
            author = release_info['author']['login']; release_name = release_info['name'] or 'N/A'
            message = (f"主人，主人~ 快来看喵！💖\n`{repo_name}` 有新宝贝发布啦~✨\n\n"
                       f"*版本是 (Version)*: `{tag_name}` 哦！\n*它的名字叫 (Title)*: {release_name}\n"
                       f"*是* `{author}` *主人做的喵！ (Author)*\n\n快去看看吧~ [（ฅ'ω'ฅ）点我去看]({release_url})")
            for target in TARGETS:
                if 'filter_tag' in target and target['filter_tag'].lower() not in tag_name.lower(): continue
                message_id = send_message_to_target(message, target)
                if message_id: SentMessage.create(chat_id=target['chat_id'], message_id=message_id)
            assets = release_info.get('assets', [])
            if not assets: return jsonify({'status': 'success'}), 200
            for asset in assets:
                asset_name = asset['name']; asset_url = asset['browser_download_url']; asset_size = asset['size']
                if asset_size > 50 * 1024 * 1024: continue
                if '.' in asset_name: parts = asset_name.rsplit('.', 1); sanitized_name = f"{parts[0].replace('.', '-')}.{parts[1]}"
                else: sanitized_name = asset_name.replace('.', '-')
                file_caption = (f"主人，这是你的快递喵！📦\n*来自仓库 (Repo)*: `{repo_name}`\n"
                                f"*版本号 (Version)*: `{tag_name}`\n\n📄 *文件 (File)*: `{sanitized_name}`")
                targets_for_asset = [t for t in TARGETS if 'filter_tag' not in t or t['filter_tag'].lower() in tag_name.lower()]
                cached_file_id = FILE_ID_CACHE.get(asset_url)
                if cached_file_id:
                    for target in targets_for_asset:
                        message_id = send_document_by_id(file_caption, cached_file_id, target)
                        if message_id: SentMessage.create(chat_id=target['chat_id'], message_id=message_id)
                else:
                    try:
                        download_response = http_session.get(asset_url, stream=True, timeout=60, allow_redirects=True)
                        download_response.raise_for_status()
                        if targets_for_asset:
                            new_file_id, message_id = upload_document_and_get_id(file_caption, download_response.raw, sanitized_name, targets_for_asset[0])
                            if new_file_id:
                                FILE_ID_CACHE[asset_url] = new_file_id
                                if message_id: SentMessage.create(chat_id=targets_for_asset[0]['chat_id'], message_id=message_id)
                                for i in range(1, len(targets_for_asset)):
                                    message_id = send_document_by_id(file_caption, new_file_id, targets_for_asset[i])
                                    if message_id: SentMessage.create(chat_id=targets_for_asset[i]['chat_id'], message_id=message_id)
                    except requests.exceptions.RequestException as e: print(f"处理附件 '{asset_name}' 时出错惹: {e}")
            return jsonify({'status': 'success'}), 200
        except KeyError as e:
            print(f"解析 payload 出错惹: {e}"); return jsonify({'status': 'error'}), 400
    return jsonify({'status': 'ignored'}), 200

@app.route('/')
def index():
    return f"GitHub Release Bot for {GITHUB_TARGET_USER} is running! 喵~"

# --- 主程序入口 ---
if __name__ != '__main__':
    load_config()
    db.connect(reuse_if_open=True)
    db.create_tables([SentMessage], safe=True)
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(cleanup_old_messages, 'interval', days=1)
    scheduler.start()