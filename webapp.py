from flask import Flask, render_template, jsonify, Response, request, abort
import database
import os
import time
import json
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect
from flask_talisman import Talisman
import pytz
from datetime import datetime
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

app = Flask(__name__)
database.migrate_db()
load_dotenv()

app.config['SECRET_KEY'] = os.getenv("FLASK_SECRET_KEY", "a-very-secret-key-in-case-of-emergency")
if app.config['SECRET_KEY'] == "a-very-secret-key-in-case-of-emergency":
    print("ВНИМАНИЕ: Используется небезопасный SECRET_KEY по-умолчанию. Укажите свой в .env файле.")

csrf = CSRFProtect(app)

csp = {
    'default-src': [
        '\'self\'' 
    ]
}
talisman = Talisman(app, content_security_policy=csp)



ADMIN_SECRET_PATH = os.getenv("ADMIN_PANEL_SECRET_PATH", "default-admin-secret-key")


@app.context_processor
def inject_global_vars():
    return {
        'datetime': datetime,
        'moscow_tz': MOSCOW_TZ
    }

@app.route("/")
def index():
    servers = database.get_public_servers_with_status()
    downtime_stats = database.get_downtime_stats_24h()
    return render_template('index.html', servers=servers, stats=downtime_stats)



@app.route(f"/{ADMIN_SECRET_PATH}")
def admin():
    """Админ-панель."""
    check_interval = "N/A"
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            bot_config = json.load(f)
            check_interval = bot_config.get('check_interval_seconds', 'N/A')
    except (FileNotFoundError, json.JSONDecodeError):
        print("ПРЕДУПРЕЖДЕНИЕ: Не удалось прочитать config.json для отображения в админ-панели.")
        pass

    servers = database.get_all_servers_with_status()
    config = {
        'CHECK_INTERVAL': check_interval,
        'CHECK_RETRIES': os.getenv("CHECK_RETRIES"),
        'CHECK_RETRY_DELAY': os.getenv("CHECK_RETRY_DELAY"),
        'CHECK_SINGLE_TIMEOUT': os.getenv("CHECK_SINGLE_TIMEOUT"),
        'FAILURE_THRESHOLD': os.getenv("FAILURE_THRESHOLD"),
        'TELEGRAM_TAG': os.getenv("TELEGRAM_TAG"),
    }
    
    servers = database.get_all_servers_with_status()
    return render_template('admin.html', config=config, servers=servers)

@app.route("/_get_server_list")
def get_server_list():
    if not request.headers.get('X-CSRFToken'):
        abort(403)
    servers = database.get_public_servers_with_status()
    return render_template('partials/server_list.html', servers=servers)

@app.route("/_get_downtime_table")
def get_downtime_table():
    if not request.headers.get('X-CSRFToken'):
        abort(403)
    servers = database.get_public_servers_with_status()
    downtime_stats = database.get_downtime_stats_24h()
    return render_template('partials/downtime_table.html', servers=servers, stats=downtime_stats)



@app.route("/stream-updates")
def stream_updates():
    def event_stream():
        last_check_time = database.get_last_update_time()
        while True:
            time.sleep(5)
            
            current_update_time = database.get_last_update_time()
            
            if current_update_time and last_check_time != current_update_time:
                last_check_time = current_update_time
                yield "data: update\n\n"
            else:
                yield ": keep-alive\n\n"
    
    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no'
    }
    return Response(event_stream(), headers=headers)

@app.route("/_s_a_p_") 
def get_secret_admin_path():
    """Возвращает путь к админке в формате JSON."""
    return jsonify({'path': f'/{ADMIN_SECRET_PATH}'})


# if __name__ == '__main__':
#     app.run(host='172.17.0.1', port=12918, threaded=True, debug=False)