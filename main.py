import os
import json
import time
import asyncio
import re
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

# ---------- In-memory fallback ----------
memory_cache = {}
CACHE_TTL_SECONDS = 3600

# ---------- Selenium globals ----------
_driver = None
processing_lock = asyncio.Lock()
DOWNLOAD_TIMEOUT = 20  # increased from 10

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

# ---------- Helper ----------
def sanitise_doc_id(url: str) -> str:
    return url.replace('/', '_').replace('.', '_').replace('#', '_').replace('$', '_').replace('[', '_').replace(']', '_')

# ---------- Cache ----------
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
        doc_ref.set({'data': data, 'expires': expires})
    else:
        memory_cache[url] = {'data': data, 'expires': expires}

def delete_cached(url: str):
    if use_firebase and db:
        db.collection('link_cache').document(sanitise_doc_id(url)).delete()
    else:
        memory_cache.pop(url, None)

# ---------- Driver ----------
def get_driver():
    global _driver
    if _driver is None:
        print("🔄 Creating new Chrome driver...")
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
        _driver.set_page_load_timeout(15)
        _driver.set_script_timeout(10)
        print("✅ Driver created.")
    return _driver

# ---------- CDN capture with enhanced logging ----------
def wait_cdp_download(driver, timeout, url):
    print(f"🔍 Monitoring network logs for download URL... (timeout={timeout}s)")
    deadline = time.time() + timeout
    seen = set()
    exts = (".rar", ".zip", ".7z", ".exe", ".part", ".iso", ".mkv", ".mp4", ".bin")
    while time.time() < deadline:
        try:
            logs = driver.get_log("performance")
        except Exception as e:
            time.sleep(0.05)
            continue
        for entry in logs:
            try:
                msg = json.loads(entry["message"]).get("message", {})
                method = msg.get("method", "")
                params = msg.get("params", {})
                if method == "Page.downloadWillBegin":
                    dl_url = params.get("url", "")
                    print(f"🔗 Download will begin: {dl_url}")
                    return dl_url
                if method in ("Network.requestWillBeSent", "Network.responseReceived"):
                    if method == "Network.requestWillBeSent":
                        dl_url = params.get("request", {}).get("url", "")
                    else:
                        dl_url = params.get("response", {}).get("url", "")
                    if not dl_url or dl_url in seen:
                        continue
                    seen.add(dl_url)
                    # Check if it's a likely download
                    if any(dl_url.lower().endswith(ext) for ext in exts) or any(ext in dl_url.lower() for ext in exts):
                        print(f"🔗 Found download URL via network: {dl_url}")
                        return dl_url
                    # Check content-disposition / content-type
                    headers = params.get("response", {}).get("headers", {}) if method == "Network.responseReceived" else {}
                    cd = headers.get("content-disposition", "") or headers.get("Content-Disposition", "")
                    ct = headers.get("content-type", "") or headers.get("Content-Type", "")
                    if "attachment" in cd.lower() or "octet-stream" in ct.lower():
                        print(f"🔗 Found attachment URL via headers: {dl_url}")
                        return dl_url
            except Exception:
                continue
        time.sleep(0.05)
    print("⚠️ No download URL found in performance logs.")
    return None

# ---------- Extract file info (unchanged) ----------
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
    print(f"🚀 Starting extraction for: {url}")
    driver = get_driver()
    try:
        # Inject timer killer
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _AGGRESSIVE_JS})
            print("✅ Timer killer script injected.")
        except Exception as e:
            print(f"⚠️ Failed to inject timer killer: {e}")

        print("🌐 Navigating to URL...")
        driver.get(url)

        # Wait for page load
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]
        )
        print("✅ Page loaded.")

        # ---- Click sequence with logging ----
        # 1. Continue (method_free)
        try:
            btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, "//button[@id='method_free']")))
            driver.execute_script("arguments[0].click();", btn)
            print("✅ Clicked 'Continue' (method_free).")
        except TimeoutException:
            print("⏳ 'Continue' button not found or not clickable – might already be past that step.")

        # 2. Free Download
        try:
            btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Free Download')]")))
            driver.execute_script("arguments[0].click();", btn)
            print("✅ Clicked 'Free Download'.")
        except TimeoutException:
            print("⏳ 'Free Download' button not found or not clickable.")

        # 3. Start Download
        try:
            btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Start Download')]")))
            driver.execute_script("arguments[0].click();", btn)
            print("✅ Clicked 'Start Download'.")
        except TimeoutException:
            print("⏳ 'Start Download' button not found or not clickable.")

        # ---- Wait a moment for the download to trigger ----
        time.sleep(2)

        # ---- Extract file info ----
        file_name, file_size = extract_file_info(driver)
        print(f"📄 Extracted file info: name='{file_name}', size='{file_size}'")

        # ---- Clear old logs and capture ----
        try:
            driver.get_log("performance")
        except Exception:
            pass

        dl_url = wait_cdp_download(driver, timeout=DOWNLOAD_TIMEOUT, url=url)

        # If still not found, try to find a direct link on the page
        if not dl_url:
            print("🔍 Attempting to find download link in page source...")
            try:
                # Look for any <a> tag with href containing file extensions
                links = driver.find_elements(By.XPATH, "//a[contains(@href, '.rar') or contains(@href, '.zip') or contains(@href, '.7z') or contains(@href, '.exe')]")
                for link in links:
                    href = link.get_attribute("href")
                    if href:
                        dl_url = href
                        print(f"🔗 Found download link in <a> tag: {dl_url}")
                        break
            except Exception:
                pass

        if dl_url:
            print(f"✅ Successfully captured download URL: {dl_url[:100]}...")
            return {
                "status": "success",
                "original_url": url,
                "name": file_name,
                "size": file_size,
                "bypassed_url": dl_url
            }
        else:
            print("❌ Failed to capture any download URL.")
            raise HTTPException(status_code=404, detail="Download URL not captured within timeout")

    except Exception as e:
        print(f"❌ Exception in execute_extraction: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")

# ---------- API endpoints ----------
@app.post("/extract")
async def extract_url(payload: UrlRequest, reload: bool = Query(False)):
    url_str = str(payload.url)
    if reload:
        delete_cached(url_str)

    cached = get_cached(url_str)
    if cached:
        return cached

    async with processing_lock:
        cached = get_cached(url_str)
        if cached:
            return cached
        result = await asyncio.to_thread(execute_extraction, url_str)
        set_cached(url_str, result)
        return result

@app.get("/health")
async def health():
    return {"status": "ok"}
