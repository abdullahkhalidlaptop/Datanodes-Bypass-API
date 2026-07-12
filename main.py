import os
import json
import time
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, HttpUrl
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# ---------- Firebase imports ----------
import firebase_admin
from firebase_admin import credentials, firestore

app = FastAPI(title="Link Extractor API with Firebase Cache")

# ---------- Firebase initialization ----------
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
use_firebase = False
db = None

if firebase_creds_json:
    try:
        cred_dict = json.loads(firebase_creds_json)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        use_firebase = True
        print("✅ Firebase initialized successfully.")
    except Exception as e:
        print(f"⚠️ Firebase initialization failed: {e}. Falling back to in-memory cache.")
else:
    print("⚠️ FIREBASE_CREDENTIALS not set. Using in-memory cache only.")

# ---------- In-memory fallback cache ----------
memory_cache = {}  # key: url_str, value: {"data": dict, "expires": datetime}
CACHE_TTL_SECONDS = 3600  # 1 hour

# ---------- Selenium globals ----------
_driver = None
_driver_lock = asyncio.Lock()
processing_lock = asyncio.Lock()

DOWNLOAD_TIMEOUT = 10

_AGGRESSIVE_JS = """
(function() {
    if (window.__timerBypassDone) return;
    window.__timerBypassDone = true;
    const _st = window.setTimeout;
    const _si = window.setInterval;
    window.setTimeout = function(fn, d, ...a) { if (typeof d === 'number' && d > 50) d = 1; return _st(fn, d, ...a); };
    window.setInterval = function(fn, d, ...a) { if (typeof d === 'number' && d > 50) d = 1; return _si(fn, d, ...a); };
    setInterval(function() {
        ['downloadCountdown','countdown','seconds','count','wait','timer','countdownNum','timerValue','timeLeft'].forEach(varName => {
            if (typeof window[varName] !== 'undefined' && typeof window[varName] === 'number' && window[varName] > 0) window[varName] = 0;
        });
        document.querySelectorAll('[class*="countdown"], [class*="timer"], [class*="seconds"]').forEach(el => {
            if (el.textContent && /^\\d+$/.test(el.textContent.trim()) && parseInt(el.textContent.trim()) > 0) el.textContent = '0';
        });
        document.querySelectorAll('button[disabled]').forEach(btn => {
            btn.disabled = false;
            btn.removeAttribute('disabled');
        });
    }, 50);
})();
"""

class UrlRequest(BaseModel):
    url: HttpUrl

# ---------- Helper: sanitise URL for Firestore doc ID ----------
def sanitise_doc_id(url: str) -> str:
    # Firestore document IDs cannot contain '/', '.', '#', '$', '[', ']'
    return url.replace('/', '_').replace('.', '_').replace('#', '_').replace('$', '_').replace('[', '_').replace(']', '_')

# ---------- Cache functions ----------
def get_cached(url: str):
    if use_firebase and db:
        doc_ref = db.collection('link_cache').document(sanitise_doc_id(url))
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            expires = data.get('expires')
            if expires and expires > datetime.utcnow():
                return data.get('data')
            else:
                doc_ref.delete()
                return None
    else:
        if url in memory_cache:
            cached = memory_cache[url]
            if cached['expires'] > datetime.utcnow():
                return cached['data']
            else:
                del memory_cache[url]
                return None
    return None

def set_cached(url: str, data: dict):
    expires = datetime.utcnow() + timedelta(seconds=CACHE_TTL_SECONDS)
    if use_firebase and db:
        doc_ref = db.collection('link_cache').document(sanitise_doc_id(url))
        doc_ref.set({
            'data': data,
            'expires': expires
        })
    else:
        memory_cache[url] = {
            'data': data,
            'expires': expires
        }

def delete_cached(url: str):
    if use_firebase and db:
        doc_ref = db.collection('link_cache').document(sanitise_doc_id(url))
        doc_ref.delete()
    else:
        if url in memory_cache:
            del memory_cache[url]

# ---------- Selenium driver (reused) ----------
def get_driver():
    global _driver
    if _driver is None:
        options = webdriver.ChromeOptions()
        options.page_load_strategy = 'eager'

        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--blink-settings=imagesEnabled=false")
        options.add_argument("--mute-audio")

        if os.path.exists("/usr/bin/chromium-browser"):
            options.binary_location = "/usr/bin/chromium-browser"
        elif os.path.exists("/usr/bin/chromium"):
            options.binary_location = "/usr/bin/chromium"

        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
            "profile.managed_default_content_settings.stylesheets": 2,
        }
        options.add_experimental_option("prefs", prefs)
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        _driver = webdriver.Chrome(options=options)
        _driver.set_page_load_timeout(12)
        _driver.set_script_timeout(10)
    return _driver

# ---------- CDN capture ----------
def wait_cdp_download(driver, timeout):
    deadline = time.time() + timeout
    seen = set()
    exts = (".rar", ".zip", ".7z", ".exe", ".part", ".iso", ".mkv", ".mp4", ".bin")
    while time.time() < deadline:
        try:
            logs = driver.get_log("performance")
        except Exception:
            time.sleep(0.05)
            continue
        for entry in logs:
            try:
                msg = json.loads(entry["message"]).get("message", {})
                method = msg.get("method", "")
                params = msg.get("params", {})
                if method == "Page.downloadWillBegin":
                    return params.get("url", "")
                if method in ("Network.requestWillBeSent", "Network.responseReceived"):
                    url = params.get("request", {}).get("url", "") if method == "Network.requestWillBeSent" else params.get("response", {}).get("url", "")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    if url.lower().endswith(exts) or any(e in url.lower() for e in exts):
                        return url
            except Exception:
                continue
        time.sleep(0.05)
    return None

# ---------- Extract file info ----------
def extract_file_info(driver):
    try:
        container = WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.XPATH, "//div[contains(@class, 'flex items-center gap-2.5 mb-4')]"))
        )
        paragraphs = container.find_elements(By.TAG_NAME, "p")
        name = size = None
        for p in paragraphs:
            text = p.text.strip()
            if text and ("MB" in text or "GB" in text or "KB" in text):
                size = text
            elif text and not text.startswith("Verified"):
                name = text
        if name is None and size is None:
            p_texts = [p.text.strip() for p in paragraphs if p.text.strip()]
            if len(p_texts) >= 2:
                name, size = p_texts[0], p_texts[1]
        return name, size
    except Exception:
        return None, None

# ---------- Core extraction ----------
def execute_extraction(url: str):
    driver = get_driver()
    try:
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _AGGRESSIVE_JS})
        except Exception:
            pass

        driver.get(url)

        # Click sequence
        try:
            btn = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, "//button[@id='method_free']")))
            driver.execute_script("arguments[0].click();", btn)
        except TimeoutException:
            pass

        try:
            btn = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Free Download')]")))
            driver.execute_script("arguments[0].click();", btn)
        except TimeoutException:
            pass

        try:
            btn = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Start Download')]")))
            driver.execute_script("arguments[0].click();", btn)
        except TimeoutException:
            pass

        file_name, file_size = extract_file_info(driver)

        try:
            driver.get_log("performance")
        except Exception:
            pass

        dl_url = wait_cdp_download(driver, timeout=DOWNLOAD_TIMEOUT)
        if not dl_url:
            raise HTTPException(status_code=404, detail="Download URL not captured within timeout")

        return {
            "status": "success",
            "original_url": url,
            "name": file_name,
            "size": file_size,
            "bypassed_url": dl_url
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")

# ---------- API Endpoints ----------
@app.post("/extract")
async def extract_url(payload: UrlRequest, reload: bool = Query(False, description="Force fresh extraction, ignore cache")):
    url_str = str(payload.url)

    if reload:
        delete_cached(url_str)

    cached_data = get_cached(url_str)
    if cached_data:
        return cached_data

    async with processing_lock:
        # Double-check after lock
        cached_data = get_cached(url_str)
        if cached_data:
            return cached_data

        result = await asyncio.to_thread(execute_extraction, url_str)
        set_cached(url_str, result)
        return result

@app.get("/health")
async def health():
    return {"status": "ok"}
