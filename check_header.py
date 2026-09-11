import os

db_path = r"C:\Users\Admin\AppData\Roaming\ZaloData\Database\_production\415420463646174242\Core\Message\7842920553502118198.db"

with open(db_path, "rb") as f:
    header = f.read(64)

print("Header (hex):", header.hex())
print("Header (ascii):", header[:32])

if header[:16] == b"SQLite format 3\x00":
    print("This is a normal SQLite database")
else:
    print("This is NOT a normal SQLite - likely encrypted")
    print("First 16 bytes:", header[:16].hex())
