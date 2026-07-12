import os
import json
import time
import asyncio
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

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
DOWNLOAD_TIMEOUT = 25   # same as original

# ---------- Aggressive timer killer (exactly as in full.py) ----------
_AGGRESSIVE_JS = """
(function() {
    if (window.__timerBypassDone) return;
    window.__timerBypassDone = true;

    const _st = window.setTimeout;
    const _si = window.setInterval;
    window.setTimeout = function(fn, d, ...a) {
        if (typeof d === 'number' && d > 50) d = 1;
        return _st(fn, d, ...a);
    };
    window.setInterval = function(fn, d, ...a) {
        if (typeof d === 'number' && d > 50) d = 1;
        return _si(fn, d, ...a);
    };

    setInterval(function() {
        ['downloadCountdown','countdown','seconds','count','wait','timer','countdownNum','timerValue','timeLeft'].forEach(varName => {
            if (typeof window[varName] !== 'undefined' && typeof window[varName] === 'number' && window[varName] > 0) {
                window[varName] = 0;
            }
        });
        document.querySelectorAll('[class*="countdown"], [class*="timer"], [class*="seconds"]').forEach(el => {
            if (el.textContent && /^\\d+$/.test(el.textContent.trim()) && parseInt(el.textContent.trim()) > 0) {
                el.textContent = '0';
            }
        });
        document.querySelectorAll('button[disabled]').forEach(btn => {
            if (btn.textContent.includes('Free Download') || btn.textContent.includes('Start Download') || btn.textContent.includes('Continue')) {
                btn.disabled = false;
                btn.removeAttribute('disabled');
            }
        });
    }, 50);

    const _raf = window.requestAnimationFrame;
    window.requestAnimationFrame = function(cb) {
        return _raf(cb);
    };
    const originalSetInterval = window.setInterval;
    window.setInterval = function(fn, d, ...a) {
        if (typeof d === 'number' && d > 50) d = 1;
        return originalSetInterval(fn, d, ...a);
    };
})();
"""

class UrlRequest(BaseModel):
    url: HttpUrl

# ---------- Helper: sanitise URL for Firestore ----------
def sanitise_doc_id(url: str) -> str:
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
        doc_ref.set({'data': data, 'expires': expires})
    else:
        memory_cache[url] = {'data': data, 'expires': expires}

def delete_cached(url: str):
    if use_firebase and db:
        db.collection('link_cache').document(sanitise_doc_id(url)).delete()
    else:
        memory_cache.pop(url, None)

# ---------- Driver creation (mimics get_driver from full.py) ----------
def get_driver():
    global _driver
    if _driver is None:
        print("🔄 Creating new Chrome driver...")
        options = webdriver.ChromeOptions()
        options.page_load_strategy = 'normal'   # same as original
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--window-size=1280,720")
        options.add_argument("--blink-settings=imagesEnabled=false")
        options.add_argument("--mute-audio")

        # Chromium binary location (for Render)
        if os.path.exists("/usr/bin/chromium-browser"):
            options.binary_location = "/usr/bin/chromium-browser"
        elif os.path.exists("/usr/bin/chromium"):
            options.binary_location = "/usr/bin/chromium"

        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
            "profile.managed_default_content_settings.plugins": 2,
        }
        options.add_experimental_option("prefs", prefs)
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        _driver = webdriver.Chrome(options=options)
        _driver.set_page_load_timeout(25)
        print("✅ Driver created.")
    return _driver

# ---------- Enable download events (CDP) ----------
def enable_download_events(driver):
    try:
        driver.execute_cdp_cmd("Page.enable", {})
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": "/tmp",   # not used, but required
            "eventsEnabled": True
        })
    except Exception:
        pass

# ---------- Inject timer killer ----------
def inject_timer_killer(driver):
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _AGGRESSIVE_JS})
    except Exception as e:
        print(f"⚠️ Timer killer injection failed: {e}")

# ---------- Wait for page load (same as full.py) ----------
def wait_for_page_load(driver, timeout=15):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]
    )

# ---------- CDP download capture (exact copy from full.py) ----------
def wait_cdp_download(driver, timeout, worker_id=0):
    deadline = time.time() + timeout
    seen = set()
    skip = {"doubleclick", "google", "gstatic", "cloudflare", "facebook", "analytics"}
    exts = (".rar", ".zip", ".7z", ".exe", ".part", ".iso", ".mkv", ".mp4", ".bin")
    while time.time() < deadline:
        try:
            logs = driver.get_log("performance")
        except Exception:
            time.sleep(0.1)
            continue
        for entry in logs:
            try:
                msg = json.loads(entry["message"]).get("message", {})
                method = msg.get("method", "")
                params = msg.get("params", {})
                if method == "Page.downloadWillBegin":
                    url = params.get("url", "")
                    if url:
                        return url
                if method in ("Network.requestWillBeSent", "Network.responseReceived"):
                    if method == "Network.requestWillBeSent":
                        url = params.get("request", {}).get("url", "")
                    else:
                        resp = params.get("response", {})
                        url = resp.get("url", "")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    if any(s in url for s in skip):
                        continue
                    if url.lower().endswith(exts) or any(e in url.lower() for e in exts):
                        return url
                    headers = params.get("response", {}).get("headers", {}) if method == "Network.responseReceived" else {}
                    cd = headers.get("content-disposition", "") or headers.get("Content-Disposition", "")
                    ct = headers.get("content-type", "") or headers.get("Content-Type", "")
                    if "attachment" in cd.lower() or "octet-stream" in ct.lower():
                        return url
            except Exception:
                continue
        time.sleep(0.1)
    return None

def drain_logs(driver):
    try:
        driver.get_log("performance")
    except Exception:
        pass

# ---------- Extract file info (from page) ----------
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

# ---------- Core extraction (mirrors process_single_url from full.py) ----------
def execute_extraction(url: str):
    print(f"🚀 Starting extraction for: {url}")
    driver = get_driver()
    try:
        # Enable download events & inject timer killer
        enable_download_events(driver)
        inject_timer_killer(driver)

        driver.get(url)
        wait_for_page_load(driver, timeout=15)

        # ---- 1. Continue - FORCED (no wait for clickable, just presence) ----
        try:
            btn = WebDriverWait(driver, 0.5).until(
                EC.presence_of_element_located((By.XPATH, "//button[@id='method_free']"))
            )
            driver.execute_script("""
                arguments[0].disabled = false;
                arguments[0].removeAttribute('disabled');
                arguments[0].removeAttribute('hidden');
                arguments[0].style.display = 'block';
                arguments[0].style.visibility = 'visible';
                arguments[0].click();
            """, btn)
            print("✅ Forced Continue")
        except Exception as e:
            print(f"⚠️ Continue click failed: {e}")

        # ---- 2. Free Download - FORCED ----
        try:
            btn = WebDriverWait(driver, 0.5).until(
                EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Free Download')]"))
            )
            driver.execute_script("""
                arguments[0].disabled = false;
                arguments[0].removeAttribute('disabled');
                arguments[0].click();
            """, btn)
            print("✅ Forced Free Download")
        except Exception as e:
            print(f"⚠️ Free Download click failed: {e}")

        # ---- 3. Start Download - FORCED ----
        try:
            btn = WebDriverWait(driver, 0.5).until(
                EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Start Download')]"))
            )
            driver.execute_script("""
                arguments[0].disabled = false;
                arguments[0].removeAttribute('disabled');
                arguments[0].click();
            """, btn)
            print("✅ Forced Start Download")
        except Exception as e:
            print(f"⚠️ Start Download click failed: {e}")

        # ---- Capture download URL ----
        drain_logs(driver)
        dl_url = wait_cdp_download(driver, timeout=DOWNLOAD_TIMEOUT, worker_id=0)

        # ---- Extract file info (can be done anytime) ----
        file_name, file_size = extract_file_info(driver)

        if dl_url:
            print(f"✅ Captured: {dl_url[:100]}...")
            return {
                "status": "success",
                "original_url": url,
                "name": file_name,
                "size": file_size,
                "bypassed_url": dl_url
            }
        else:
            print("❌ No download URL captured.")
            # Try to find a direct link as fallback
            try:
                links = driver.find_elements(By.XPATH, "//a[contains(@href, '.rar') or contains(@href, '.zip') or contains(@href, '.7z')]")
                for link in links:
                    href = link.get_attribute("href")
                    if href:
                        dl_url = href
                        print(f"🔗 Found fallback link: {dl_url}")
                        return {
                            "status": "success",
                            "original_url": url,
                            "name": file_name,
                            "size": file_size,
                            "bypassed_url": dl_url
                        }
            except:
                pass
            raise HTTPException(status_code=404, detail="Download URL not captured within timeout")

    except Exception as e:
        print(f"❌ Extraction failed: {e}")
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")

# ---------- FastAPI endpoints ----------
@app.post("/extract")
async def extract_url(payload: UrlRequest, reload: bool = Query(False)):
    url_str = str(payload.url)
    if reload:
        delete_cached(url_str)

    cached = get_cached(url_str)
    if cached:
        return cached

    async with processing_lock:
        # Double-check cache after lock
        cached = get_cached(url_str)
        if cached:
            return cached
        result = await asyncio.to_thread(execute_extraction, url_str)
        set_cached(url_str, result)
        return result

@app.get("/health")
async def health():
    return {"status": "ok"}
