"""
Zalo Decryption Utilities - Python Version
Tested on: Zalo PC v26.8.20
Date: September 2026
"""

import base64
import json
import os
import requests
import websocket
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad, pad
import hashlib
import hmac


# ============================================================
# 1. TRANSPORT ENCRYPTION (API Communication)
# ============================================================

ZALO_SECRET_KEY = '8OF9jccA7NHgjVqvDNbRKg=='


def decode_aes(ciphertext: str) -> str:
    """
    Decrypt Zalo API response (AES-CBC)
    
    Args:
        ciphertext: Base64 encoded encrypted string
        
    Returns:
        Decrypted plaintext
    """
    key = base64.b64decode(ZALO_SECRET_KEY)
    iv = bytes(16)  # All zeros
    encrypted = base64.b64decode(ciphertext)
    
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(encrypted)
    return unpad(decrypted, AES.block_size).decode('utf-8')


def encode_aes(plaintext: str) -> str:
    """
    Encrypt data for Zalo API request (AES-CBC)
    
    Args:
        plaintext: String to encrypt
        
    Returns:
        Base64 encoded encrypted string
    """
    key = base64.b64decode(ZALO_SECRET_KEY)
    iv = bytes(16)  # All zeros
    
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pad(plaintext.encode('utf-8'), AES.block_size))
    return base64.b64encode(encrypted).decode('utf-8')


# ============================================================
# 2. FIELD-LEVEL ENCRYPTION (Content Protection)
# ============================================================

FIELD_ENCRYPTION_SALT = bytes([
    221, 179, 255, 0, 79, 11, 0, 70,
    61, 94, 221, 189, 11, 233, 96, 177
])

ROOT_KEY = 'd9c07b9de9915af401607d3f360211ac'


def decrypt_field(encrypted_b64: str) -> str:
    """
    Decrypt field-level encrypted content
    
    Args:
        encrypted_b64: Base64 encoded encrypted field
        
    Returns:
        Decrypted field value
    """
    key = hashlib.pbkdf2_hmac(
        'sha256',
        ROOT_KEY.encode(),
        FIELD_ENCRYPTION_SALT,
        1000,
        dklen=32
    )
    
    iv = FIELD_ENCRYPTION_SALT
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = base64.b64decode(encrypted_b64)
    decrypted = cipher.decrypt(encrypted)
    return unpad(decrypted, AES.block_size).decode('utf-8')


# ============================================================
# 3. ZALO PC VIA REMOTE DEBUGGING (CDP)
# ============================================================

class ZaloPCClient:
    """Client for connecting to Zalo PC via Chrome DevTools Protocol"""
    
    def __init__(self, port=8315):
        self.port = port
        self.ws = None
        self.msg_id = 0
        self.pending = {}
        
    def connect(self):
        """Connect to Zalo PC remote debugger"""
        # Get available targets
        response = requests.get(f'http://localhost:{self.port}/json')
        targets = response.json()
        
        print(f'Found {len(targets)} debug targets:')
        for t in targets:
            print(f'  {t["type"]}: {t.get("title", t.get("url", ""))}')
        
        # Find main window
        main_target = None
        for t in targets:
            if t.get('type') == 'page' and t.get('webSocketDebuggerUrl'):
                main_target = t
                break
        
        if not main_target:
            raise Exception('No suitable target found')
        
        print(f'\nConnecting to: {main_target.get("title", main_target["url"])}')
        
        # Connect via WebSocket
        self.ws = websocket.create_connection(main_target['webSocketDebuggerUrl'])
        
        # Start listener thread
        import threading
        def listener():
            while True:
                try:
                    data = json.loads(self.ws.recv())
                    if 'id' in data and data['id'] in self.pending:
                        self.pending[data['id']].append(data)
                except:
                    break
        
        thread = threading.Thread(target=listener, daemon=True)
        thread.start()
        
    def send(self, method, params=None):
        """Send CDP command"""
        self.msg_id += 1
        msg = {'id': self.msg_id, 'method': method}
        if params:
            msg['params'] = params
        
        self.pending[self.msg_id] = []
        self.ws.send(json.dumps(msg))
        
        # Wait for response
        import time
        timeout = 30
        start = time.time()
        while time.time() - start < timeout:
            if self.pending[self.msg_id]:
                response = self.pending[self.msg_id][0]
                del self.pending[self.msg_id]
                if 'error' in response:
                    raise Exception(response['error']['message'])
                return response.get('result', {})
            time.sleep(0.01)
        
        raise Exception('Timeout waiting for response')
    
    def evaluate(self, expression):
        """Evaluate JavaScript expression in Zalo context"""
        result = self.send('Runtime.evaluate', {
            'expression': expression,
            'awaitPromise': True,
            'returnByValue': True,
            'timeout': 30000
        })
        
        if 'exceptionDetails' in result:
            raise Exception(f'Eval error: {result["exceptionDetails"]}')
        
        return result.get('result', {}).get('value')
    
    def close(self):
        """Close connection"""
        if self.ws:
            self.ws.close()
    
    def get_current_user(self):
        """Get current user info"""
        js = '''
        (async () => {
            const mod = window.webpackJsonp.push([[Math.random()],{},[["XS0u"]]]).default;
            return JSON.stringify(await mod.getMe());
        })()
        '''
        return json.loads(self.evaluate(js))
    
    def get_message_history(self, uid, count=50, offset_message_id=''):
        """Get message history"""
        js = f'''
        (async () => {{
            const mod = window.webpackJsonp.push([[Math.random()],{},[["fBUP"]]]).default;
            const encMod = window.webpackJsonp.push([[Math.random()],{},[["z0WU"]]]).default;
            
            const response = await mod.getHistoryMessage('{uid}', {count}{",'" + offset_message_id + "'" if offset_message_id else ''});
            
            if (response && response.data && response.data.data) {{
                return encMod.decodeAES(response.data.data);
            }}
            return JSON.stringify(response);
        }})()
        '''
        return json.loads(self.evaluate(js))
    
    def get_friends_list(self):
        """Get friends list"""
        js = '''
        (async () => {
            const mod = window.webpackJsonp.push([[Math.random()],{},[["XS0u"]]]).default;
            return JSON.stringify(await mod.getFriends());
        })()
        '''
        return json.loads(self.evaluate(js))
    
    def get_groups_list(self):
        """Get groups list"""
        js = '''
        (() => {
            const mod = window.webpackJsonp.push([[Math.random()],{},[["Gm1y"]]]).default;
            return JSON.stringify(mod.getGroupsListSync());
        })()
        '''
        return json.loads(self.evaluate(js))


# ============================================================
# 4. FULL EXTRACTION SCRIPT
# ============================================================

def extract_all_messages(target_uid: str, output_dir: str):
    """
    Extract all messages for a target user
    
    Args:
        target_uid: Target user UID
        output_dir: Directory to save output files
    """
    print('Connecting to Zalo PC...')
    client = ZaloPCClient(port=8315)
    client.connect()
    
    all_messages = []
    offset_message_id = ''
    batch_num = 0
    batch_size = 50
    max_messages = 10000
    
    print(f'Extracting messages for UID: {target_uid}')
    
    while len(all_messages) < max_messages:
        batch_num += 1
        print(f'Batch {batch_num}...', end=' ')
        
        try:
            messages = client.get_message_history(
                target_uid, batch_size, offset_message_id
            )
            
            if not messages or len(messages) == 0:
                print('No more messages')
                break
            
            print(f'Got {len(messages)} messages')
            all_messages.extend(messages)
            
            # Get last message ID for pagination
            last_msg = messages[-1]
            offset_message_id = last_msg.get('msgId', '')
            
            if len(messages) < batch_size:
                print('Reached the end')
                break
            
            # Rate limiting
            import time
            time.sleep(0.5)
            
        except Exception as error:
            print(f'Error: {error}')
            break
    
    # Save results
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f'messages_{target_uid}.json')
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            'targetUid': target_uid,
            'extractedAt': datetime.now().isoformat(),
            'totalMessages': len(all_messages),
            'messages': all_messages
        }, f, ensure_ascii=False, indent=2)
    
    print(f'\nSaved {len(all_messages)} messages to {output_file}')
    client.close()
    
    return all_messages


# ============================================================
# 5. USAGE EXAMPLES
# ============================================================

if __name__ == '__main__':
    from datetime import datetime
    
    # Example 1: Simple decodeAES
    print("=== Example 1: Decode AES ===")
    encrypted = "lqwMxeU86kgh6jcanndGmJjzJHsqu6B8v1kFI9p9/381UGcffjBS8HFWkrNBWbZPmIz/k3v+YQKUIgD4rsycfvKZ/jxBYqvN/r0T+ZASzQs="
    decrypted = decode_aes(encrypted)
    print(f"Encrypted: {encrypted}")
    print(f"Decrypted: {decrypted}")
    
    # Example 2: Connect and extract
    print("\n=== Example 2: Extract Messages ===")
    TARGET_UID = '2559848092105805671'  # Đoàn Bảo
    OUTPUT_DIR = r'C:\Users\Admin\AppData\Local\Temp\zalo-backup'
    
    try:
        messages = extract_all_messages(TARGET_UID, OUTPUT_DIR)
        print(f"\nExtraction complete! Got {len(messages)} messages.")
    except Exception as e:
        print(f"\nError: {e}")
        print("\nMake sure Zalo PC is running with remote debugging enabled:")
        print('  & "$env:localappdata\\Programs\\Zalo\\Zalo.exe" --remote-debugging-port=8315 --remote-allow-origins=*')
