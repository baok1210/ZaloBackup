import os
import sys
import json
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
except ImportError:
    print("Chua cai pycryptodome. Chay: pip install pycryptodome")
    sys.exit(1)

def find_zalo_pc_dir():
    local = Path(os.environ.get('LOCALAPPDATA', ''))
    roaming = Path(os.environ.get('APPDATA', ''))
    
    candidates = [
        local / 'ZaloPC',
        roaming / 'ZaloPC',
        local / 'Zalo',
        roaming / 'Zalo',
    ]
    
    for p in candidates:
        if p.exists():
            return p
    
    return None

def find_databases(zalo_dir):
    db_files = []
    for root, dirs, files in os.walk(zalo_dir):
        for f in files:
            if f.endswith('.db') or f.endswith('.sqlite') or f.endswith('.sqlite3'):
                db_files.append(Path(root) / f)
    return db_files

def try_decrypt_db(db_path, key=None):
    if key is None:
        key = b'0123456789abcdef'
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        return db_path, tables
    except sqlite3.DatabaseError:
        pass
    
    try:
        with open(db_path, 'rb') as f:
            data = f.read()
        
        if len(data) < 16:
            return None, []
        
        iv = data[:16]
        encrypted = data[16:]
        
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = unpad(cipher.decrypt(encrypted), AES.block_size)
        
        temp_path = db_path.with_suffix('.decrypted.db')
        with open(temp_path, 'wb') as f:
            f.write(decrypted)
        
        conn = sqlite3.connect(str(temp_path))
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        
        return temp_path, tables
    except Exception as e:
        return None, []

def read_messages(db_path, user_id=None, limit=10000):
    messages = []
    
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        print(f"Tables: {tables}")
        
        for table in tables:
            try:
                cursor.execute(f"PRAGMA table_info({table})")
                columns = [col[1] for col in cursor.fetchall()]
                
                if any(col in columns for col in ['msg', 'message', 'content', 'text', 'dsc']):
                    print(f"\nReading from table: {table}")
                    print(f"Columns: {columns}")
                    
                    cursor.execute(f"SELECT * FROM {table} LIMIT {limit}")
                    rows = cursor.fetchall()
                    
                    for row in rows:
                        msg = dict(row)
                        messages.append(msg)
                    
                    print(f"Found {len(rows)} messages in {table}")
            except Exception as e:
                continue
        
        conn.close()
    except Exception as e:
        print(f"Error reading {db_path}: {e}")
    
    return messages

def main():
    print("=== Zalo PC Database Reader ===\n")
    
    zalo_dir = find_zalo_pc_dir()
    if not zalo_dir:
        print("Khong tim thay thu muc Zalo PC!")
        print("Thu cong: Nhap duong dan thu muc Zalo PC:")
        print(" 通常 o: C:\\Users\\<username>\\AppData\\Local\\ZaloPC")
        zalo_dir = input("Duong dan: ").strip()
        if not zalo_dir:
            sys.exit(1)
        zalo_dir = Path(zalo_dir)
    
    print(f"Zalo dir: {zalo_dir}")
    
    db_files = find_databases(zalo_dir)
    print(f"Found {len(db_files)} database files")
    
    all_messages = []
    
    for db_path in db_files:
        print(f"\n--- {db_path.name} ---")
        
        db_file, tables = try_decrypt_db(db_path)
        if db_file:
            print(f"Tables: {tables}")
            msgs = read_messages(db_file)
            all_messages.extend(msgs)
            
            if db_file != db_path:
                os.remove(db_file)
    
    if all_messages:
        output_file = f"zalo-pc-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(all_messages, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nSaved {len(all_messages)} messages to {output_file}")
    else:
        print("\nKhong tim thay tin nhan!")

if __name__ == '__main__':
    main()
