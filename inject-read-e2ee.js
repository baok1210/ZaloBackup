(async () => {
  const results = {};

  // Open database
  const db = await new Promise((resolve, reject) => {
    const req = indexedDB.open('zdb_415420463646174242', 76);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });

  // List all object stores
  results.allStores = Array.from(db.objectStoreNames);
  console.log('[ZaloBackup] All object stores:', results.allStores);

  // Read e2ee_deciphered
  try {
    const tx = db.transaction('e2ee_deciphered', 'readonly');
    const store = tx.objectStore('e2ee_deciphered');
    const all = await new Promise((resolve, reject) => {
      const req = store.getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    results.e2ee_deciphered = {
      count: all.length,
      sample: all.slice(0, 3),
      keys: all.length > 0 ? Object.keys(all[0]) : []
    };
  } catch(e) {
    results.e2ee_deciphered = { error: e.message };
  }

  // Read e2ee_session
  try {
    const tx = db.transaction('e2ee_session', 'readonly');
    const store = tx.objectStore('e2ee_session');
    const all = await new Promise((resolve, reject) => {
      const req = store.getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    results.e2ee_session = {
      count: all.length,
      keys: all.length > 0 ? Object.keys(all[0]) : [],
      sampleKeys: all.slice(0, 2).map(r => Object.keys(r))
    };
  } catch(e) {
    results.e2ee_session = { error: e.message };
  }

  // Read e2ee_identity
  try {
    const tx = db.transaction('e2ee_identity', 'readonly');
    const store = tx.objectStore('e2ee_identity');
    const all = await new Promise((resolve, reject) => {
      const req = store.getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    results.e2ee_identity = {
      count: all.length,
      keys: all.length > 0 ? Object.keys(all[0]) : [],
    };
  } catch(e) {
    results.e2ee_identity = { error: e.message };
  }

  // Read e2ee_meta
  try {
    const tx = db.transaction('e2ee_meta', 'readonly');
    const store = tx.objectStore('e2ee_meta');
    const all = await new Promise((resolve, reject) => {
      const req = store.getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    results.e2ee_meta = {
      count: all.length,
      keys: all.length > 0 ? Object.keys(all[0]) : [],
      sample: all.slice(0, 2)
    };
  } catch(e) {
    results.e2ee_meta = { error: e.message };
  }

  db.close();

  console.log('[ZaloBackup] E2EE Stores:', JSON.stringify(results, null, 2));
  return results;
})();
