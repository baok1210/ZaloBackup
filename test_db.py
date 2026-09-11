import sqlite3
import os

db_path = r"C:\Users\Admin\AppData\Roaming\ZaloData\Database\_production\415420463646174242\Core\Message\7842920553502118198.db"
print(f"File size: {os.path.getsize(db_path)} bytes")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print(f"Tables: {[t[0] for t in tables]}")

for table in tables:
    cursor.execute(f"PRAGMA table_info({table[0]})")
    cols = cursor.fetchall()
    print(f"\n{table[0]}:")
    for c in cols:
        print(f"  {c[1]} ({c[2]})")

conn.close()
