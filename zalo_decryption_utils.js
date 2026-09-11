/**
 * Zalo Decryption Utilities - Quick Reference
 * 
 * Tested on: Zalo PC v26.8.20, Zalo Web (chat.zalo.me)
 * Date: September 2026
 */

// ============================================================
// 1. TRANSPORT ENCRYPTION (API Communication)
// ============================================================

const ZALO_SECRET_KEY = '8OF9jccA7NHgjVqvDNbRKg==';

/**
 * Decrypt Zalo API response (AES-CBC)
 * @param {string} ciphertext - Base64 encoded encrypted string
 * @returns {string} Decrypted plaintext
 */
async function decodeAES(ciphertext) {
    const key = CryptoJS.enc.Base64.parse(ZALO_SECRET_KEY);
    const iv = CryptoJS.enc.Hex.parse('00000000000000000000000000000000');
    const decrypted = CryptoJS.AES.decrypt(ciphertext, key, {
        iv: iv,
        mode: CryptoJS.mode.CBC,
        padding: CryptoJS.pad.Pkcs7
    });
    return decrypted.toString(CryptoJS.enc.Utf8);
}

/**
 * Encrypt data for Zalo API request (AES-CBC)
 * @param {string} plaintext - String to encrypt
 * @returns {string} Base64 encoded encrypted string
 */
async function encodeAES(plaintext) {
    const key = CryptoJS.enc.Base64.parse(ZALO_SECRET_KEY);
    const iv = CryptoJS.enc.Hex.parse('00000000000000000000000000000000');
    const encrypted = CryptoJS.AES.encrypt(plaintext, key, {
        iv: iv,
        mode: CryptoJS.mode.CBC,
        padding: CryptoJS.pad.Pkcs7
    });
    return encrypted.toString();
}

// ============================================================
// 2. FIELD-LEVEL ENCRYPTION (Content Protection)
// ============================================================

const FIELD_ENCRYPTION_SALT = Buffer.from([
    221, 179, 255, 0, 79, 11, 0, 70,
    61, 94, 221, 189, 11, 233, 96, 177
]);
const ROOT_KEY = 'd9c07b9de9915af401607d3f360211ac';

/**
 * Decrypt field-level encrypted content (Python)
 */
function decrypt_field_python(encrypted_b64):
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Util.Padding import unpad
    import base64
    
    salt = bytes([221,179,255,0,79,11,0,70,61,94,221,189,11,233,96,177])
    iv = salt
    
    key = PBKDF2(
        password=ROOT_KEY.encode(),
        salt=salt,
        dkLen=32,
        count=1000
    )
    
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = base64.b64decode(encrypted_b64)
    decrypted = cipher.decrypt(encrypted)
    return unpad(decrypted, AES.block_size).decode('utf-8')

// ============================================================
// 3. ZALO PC VIA REMOTE DEBUGGING (CDP)
// ============================================================

const CDP = require('chrome-remote-interface');

/**
 * Connect to Zalo PC via remote debugging
 * First, restart Zalo with: Zalo.exe --remote-debugging-port=8315
 */
async function connectZaloPC() {
    const client = await CDP({ port: 8315 });
    const { Runtime } = client;
    return client;
}

/**
 * Get current user info
 */
async function getCurrentUser(client) {
    const { result } = await client.Runtime.evaluate({
        expression: `
            (async () => {
                const mod = window.webpackJsonp.push([[Math.random()],{},[["XS0u"]]]).default;
                return JSON.stringify(await mod.getMe());
            })()
        `,
        awaitPromise: true,
        returnByValue: true
    });
    return JSON.parse(result.value);
}

/**
 * Get message history
 * @param {object} client - CDP client
 * @param {string} uid - Target user/group ID
 * @param {number} count - Number of messages (max 50)
 * @param {string} offsetMessageId - For pagination
 */
async function getMessageHistory(client, uid, count = 50, offsetMessageId = '') {
    const { result } = await client.Runtime.evaluate({
        expression: `
            (async () => {
                const mod = window.webpackJsonp.push([[Math.random()],{},[["fBUP"]]]).default;
                const encMod = window.webpackJsonp.push([[Math.random()],{},[["z0WU"]]]).default;
                
                const response = await mod.getHistoryMessage('${uid}', ${count}${
                    offsetMessageId ? ",'" + offsetMessageId + "'" : ''
                });
                
                if (response && response.data && response.data.data) {
                    return encMod.decodeAES(response.data.data);
                }
                return JSON.stringify(response);
            })()
        `,
        awaitPromise: true,
        returnByValue: true
    });
    return JSON.parse(result.value);
}

/**
 * Get friends list
 */
async function getFriendsList(client) {
    const { result } = await client.Runtime.evaluate({
        expression: `
            (async () => {
                const mod = window.webpackJsonp.push([[Math.random()],{},[["XS0u"]]]).default;
                return JSON.stringify(await mod.getFriends());
            })()
        `,
        awaitPromise: true,
        returnByValue: true
    });
    return JSON.parse(result.value);
}

/**
 * Get groups list
 */
async function getGroupsList(client) {
    const { result } = await client.Runtime.evaluate({
        expression: `
            (() => {
                const mod = window.webpackJsonp.push([[Math.random()],{},[["Gm1y"]]]).default;
                return JSON.stringify(mod.getGroupsListSync());
            })()
        `,
        returnByValue: true
    });
    return JSON.parse(result.value);
}

// ============================================================
// 4. FULL EXTRACTION SCRIPT
// ============================================================

async function extractAllMessages(targetUid, outputDir) {
    const fs = require('fs');
    const path = require('path');
    
    console.log('Connecting to Zalo PC...');
    const client = await connectZaloPC();
    
    const allMessages = [];
    let offsetMessageId = '';
    let batchNum = 0;
    const BATCH_SIZE = 50;
    const MAX_MESSAGES = 10000;
    
    console.log(`Extracting messages for UID: ${targetUid}`);
    
    while (allMessages.length < MAX_MESSAGES) {
        batchNum++;
        process.stdout.write(`Batch ${batchNum}... `);
        
        try {
            const messages = await getMessageHistory(
                client, targetUid, BATCH_SIZE, offsetMessageId
            );
            
            if (!messages || messages.length === 0) {
                console.log('No more messages');
                break;
            }
            
            console.log(`Got ${messages.length} messages`);
            allMessages.push(...messages);
            
            // Get last message ID for pagination
            const lastMsg = messages[messages.length - 1];
            offsetMessageId = lastMsg.msgId || '';
            
            if (messages.length < BATCH_SIZE) {
                console.log('Reached the end');
                break;
            }
            
            // Rate limiting
            await new Promise(r => setTimeout(r, 500));
            
        } catch (error) {
            console.error(`Error: ${error.message}`);
            break;
        }
    }
    
    // Save results
    const outputFile = path.join(outputDir, `messages_${targetUid}.json`);
    fs.writeFileSync(outputFile, JSON.stringify({
        targetUid,
        extractedAt: new Date().toISOString(),
        totalMessages: allMessages.length,
        messages: allMessages
    }, null, 2));
    
    console.log(`\nSaved ${allMessages.length} messages to ${outputFile}`);
    await client.close();
    
    return allMessages;
}

// ============================================================
// 5. NODE.JS CDP CONNECTION (without chrome-remote-interface)
// ============================================================

const http = require('http');
const WebSocket = require('ws');

async function connectCDPManual(port = 8315) {
    // Get available targets
    const targets = await new Promise((resolve, reject) => {
        http.get(`http://localhost:${port}/json`, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => resolve(JSON.parse(data)));
        }).on('error', reject);
    });
    
    console.log('Available targets:');
    targets.forEach(t => console.log(`  ${t.type}: ${t.title || t.url}`));
    
    // Find main window
    const mainTarget = targets.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
    if (!mainTarget) throw new Error('No suitable target found');
    
    // Connect via WebSocket
    const ws = new WebSocket(mainTarget.webSocketDebuggerUrl);
    let msgId = 0;
    const pending = new Map();
    
    await new Promise(resolve => ws.on('open', resolve));
    
    ws.on('message', (data) => {
        const msg = JSON.parse(data.toString());
        if (msg.id && pending.has(msg.id)) {
            const p = pending.get(msg.id);
            pending.delete(msg.id);
            if (msg.error) p.reject(new Error(msg.error.message));
            else p.resolve(msg.result);
        }
    });
    
    return {
        send(method, params = {}) {
            return new Promise((resolve, reject) => {
                const id = ++msgId;
                pending.set(id, { resolve, reject });
                ws.send(JSON.stringify({ id, method, params }));
            });
        },
        close() { ws.close(); }
    };
}

// Export functions
module.exports = {
    ZALO_SECRET_KEY,
    decodeAES,
    encodeAES,
    FIELD_ENCRYPTION_SALT,
    ROOT_KEY,
    connectZaloPC,
    getCurrentUser,
    getMessageHistory,
    getFriendsList,
    getGroupsList,
    extractAllMessages,
    connectCDPManual
};
