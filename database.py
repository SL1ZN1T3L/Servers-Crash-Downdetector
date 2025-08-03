import sqlite3
import os
from datetime import datetime, timedelta, timezone 
import pytz

MOSCOW_TZ = pytz.timezone('Europe/Moscow')

DATABASE_FILE = 'monitoring.db'

def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_db():
    print("Проверка миграций базы данных...")
    if not os.path.exists(DATABASE_FILE):
        print("Файл БД не найден, миграция не требуется. База будет создана при инициализации.")
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='servers'")
    if cursor.fetchone() is None:
        print("Таблица 'servers' не найдена. Она будет создана при инициализации.")
        conn.close()
        return

    cursor.execute("PRAGMA table_info(servers)")
    columns = [col['name'] for col in cursor.fetchall()]
    
    if 'is_public' not in columns:
        print("Миграция: Добавление колонки 'is_public' в таблицу 'servers'...")
        cursor.execute("ALTER TABLE servers ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT 0")
        conn.commit()
        print("Миграция успешно завершена.")
    else:
        print("Схема базы данных актуальна.")
        
    conn.close()


def init_db():
    if os.path.exists(DATABASE_FILE):
        return
        
    print("Создаю новую базу данных...")
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        is_public BOOLEAN NOT NULL DEFAULT 0
    )
    ''')
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id INTEGER UNIQUE NOT NULL,
        is_alive BOOLEAN,
        latency_ms INTEGER,
        last_checked TEXT,
        FOREIGN KEY (server_id) REFERENCES servers (id) ON DELETE CASCADE
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS downtime_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id INTEGER NOT NULL,
        event_type TEXT NOT NULL, -- 'DOWN' или 'UP'
        timestamp TEXT NOT NULL,
        FOREIGN KEY (server_id) REFERENCES servers (id) ON DELETE CASCADE
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''')
    cursor.execute("INSERT OR IGNORE INTO metadata (key, value) VALUES (?, ?)", 
                   ('last_update_timestamp', datetime.now(timezone.utc).isoformat()))
    
    conn.commit()
    conn.close()
    print("База данных успешно создана.")


def add_server(name, host, port):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO servers (name, host, port) VALUES (?, ?, ?)", (name, host, port))
        server_id = cursor.lastrowid
        cursor.execute("INSERT INTO status (server_id) VALUES (?)", (server_id,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def set_server_public(server_name: str, is_public: bool):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE servers SET is_public = ? WHERE name = ?", (is_public, server_name))
    updated_rows = cursor.rowcount
    conn.commit()
    conn.close()
    return updated_rows > 0


def remove_server(name):
    conn = get_db_connection()
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM servers WHERE name = ?", (name,))
    server_row = cursor.fetchone()
    if server_row:
        server_id = server_row['id']
        cursor.execute("DELETE FROM servers WHERE id = ?", (server_id,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def get_servers():
    conn = get_db_connection()
    servers = conn.execute("SELECT * FROM servers").fetchall()
    conn.close()
    return servers


def update_server_status(server_id, is_alive, latency_ms):
    conn = get_db_connection()
    cursor = conn.cursor()
    timestamp = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE status SET is_alive = ?, latency_ms = ?, last_checked = ? WHERE server_id = ?",
        (is_alive, latency_ms, timestamp, server_id)
    )
    cursor.execute("UPDATE metadata SET value = ? WHERE key = ?", (timestamp, 'last_update_timestamp'))
    conn.commit()
    conn.close()

def get_last_update_time():
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", ('last_update_timestamp',)).fetchone()
    conn.close()
    return row['value'] if row else None



def log_downtime_event(server_id, event_type):
    conn = get_db_connection()
    timestamp = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO downtime_events (server_id, event_type, timestamp) VALUES (?, ?, ?)",
        (server_id, event_type, timestamp)
    )
    conn.commit()
    conn.close()


def get_all_servers_with_status():
    conn = get_db_connection()
    query = """
    SELECT 
        s.id, s.name, s.host, s.port, s.is_public,
        st.is_alive, st.latency_ms, st.last_checked
    FROM servers s
    LEFT JOIN status st ON s.id = st.server_id
    ORDER BY s.id;
    """
    servers = conn.execute(query).fetchall()
    conn.close()
    return servers

def get_public_servers_with_status():
    conn = get_db_connection()
    query = """
    SELECT 
        s.id, s.name, st.is_alive, st.latency_ms
    FROM servers s
    JOIN status st ON s.id = st.server_id
    WHERE s.is_public = 1
    ORDER BY s.id;
    """
    servers = conn.execute(query).fetchall()
    conn.close()
    return servers


def get_downtime_stats_24h():
    conn = get_db_connection()
    stats = {}
    
    servers = conn.execute("SELECT * FROM servers ORDER BY id").fetchall()
    for server in servers:
        server_id = server['id']
        twenty_four_hours_ago = datetime.now(timezone.utc) - timedelta(hours=24)
        
        query = """
        SELECT event_type, timestamp FROM downtime_events 
        WHERE server_id = ? AND timestamp >= ?
        ORDER BY timestamp ASC
        """
        events = conn.execute(query, (server_id, twenty_four_hours_ago.isoformat())).fetchall()

        query_initial = """
        SELECT event_type FROM downtime_events
        WHERE server_id = ? AND timestamp < ?
        ORDER BY timestamp DESC LIMIT 1
        """
        initial_event = conn.execute(query_initial, (server_id, twenty_four_hours_ago.isoformat())).fetchone()
        
        total_downtime_seconds = 0
        is_down = initial_event and initial_event['event_type'] == 'DOWN'

        downtime_periods = []
        down_start_time = None

        if is_down:
            down_start_time = twenty_four_hours_ago

        for event in events:
            event_time = datetime.fromisoformat(event['timestamp'])
            
            if event['event_type'] == 'DOWN' and not is_down:
                is_down = True
                down_start_time = event_time
                
            elif event['event_type'] == 'UP' and is_down:
                is_down = False
                if down_start_time:
                    downtime_duration = (event_time - down_start_time).total_seconds()
                    total_downtime_seconds += downtime_duration
                    start_in_msk = down_start_time.astimezone(MOSCOW_TZ)
                    end_in_msk = event_time.astimezone(MOSCOW_TZ)

                    downtime_periods.append({
                        "start": start_in_msk.strftime("%H:%M:%S"),
                        "end": end_in_msk.strftime("%H:%M:%S"),
                        "duration": round(downtime_duration / 60, 1)
                    })

                    down_start_time = None
        
        if is_down and down_start_time:
            now_utc = datetime.now(timezone.utc)
            total_downtime_seconds += (datetime.now(timezone.utc) - down_start_time).total_seconds()
        
            start_in_msk = down_start_time.astimezone(MOSCOW_TZ)
            current_duration = (now_utc - down_start_time).total_seconds()
            downtime_periods.append({
                "start": start_in_msk.strftime("%H:%M:%S"),
                "end": "now",
                "duration": round(current_duration / 60, 1)
            })
        
        total_seconds_in_period = 24 * 3600
        uptime_percentage = 100 * (1 - (total_downtime_seconds / total_seconds_in_period))
        
        stats[server_id] = {
            "name": server['name'],
            "uptime_percent": round(uptime_percentage, 2),
            "downtime_periods": downtime_periods
        }
        
    conn.close()
    return stats