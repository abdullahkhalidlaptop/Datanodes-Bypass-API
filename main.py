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

import firebase_admin
from firebase_admin import credentials, firestore

app = FastAPI(title="Link Extractor API")

# ---------- Firebase ----------
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
        print("✅ Firebase initialized.")
    except Exception as e:
        print(f"⚠️ Firebase init failed: {e}")
else:
    print("⚠️ FIREBASE_CREDENTIALS not set; using in-memory cache.")

# ---------- Cache ----------
memory_cache = {}
CACHE_TTL_SECONDS = 3600

def sanitise_doc_id(url: str) -> str:
    return url.replace('/', '_').replace('.', '_').replace('#', '_').replace('$', '_').replace('[', '_').replace(']', '_')

def get_cached(url: str):
    if use_firebase and db:
        doc_ref = db.collection('link_cache').document(sanitise_doc_id(url))
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            expires = data.get('expires')
            if expires:
                if expires.tzinfo is not None:
                    expires = expires.replace(tzinfo=None)
                if expires > datetime.utcnow():
                    return data.get('data')
                else:
                    doc_ref.delete()
    else:
        if url in memory_cache:
            cached = memory_cache[url]
            if cached['expires'] > datetime.utcnow():
                return cached['data']
            else:
                del memory_cache[url]
    return None

def set_cached(url: str, data: dict):
    expires = datetime.utcnow() + timedelta(seconds=CACHE_TTL_SECONDS)
    if expires.tzinfo is not None:
        expires = expires.replace(tzinfo=None)
    if use_firebase and db:
        doc_ref = db.collection('link_cache').document(sanitise_doc_id(url))
        doc_ref.set({'data': data, 'expires': expires})
    else:
        memory_cache[url] = {'data': data, 'expires': expires}

def delete_cached(url: str):
    if use_firebase and db:
        try:
            db.collection('link_cache').document(sanitise_doc_id(url)).delete()
        except:
            pass
    else:
        memory_cache.pop(url, None)

# ---------- Selenium ----------
_driver = None
processing_lock = asyncio.Lock()
DOWNLOAD_TIMEOUT = 25

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
    window.requestAnimationFrame = function(cb) { return _raf(cb); };
    const originalSetInterval = window.setInterval;
    window.setInterval = function(fn, d, ...a) {
        if (typeof d === 'number' && d > 50) d = 1;
        return originalSetInterval(fn, d, ...a);
    };
})();
"""

class UrlRequest(BaseModel):
    url: HttpUrl

def get_driver():
    global _driver
    if _driver is None:
        options = webdriver.ChromeOptions()
        options.page_load_strategy = 'normal'
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--window-size=1280,720")
        options.add_argument("--blink-settings=imagesEnabled=false")
        options.add_argument("--mute-audio")
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
    return _driver

def enable_download_events(driver):
    try:
        driver.execute_cdp_cmd("Page.enable", {})
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": "/tmp",
            "eventsEnabled": True
        })
    except:
        pass

def inject_timer_killer(driver):
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _AGGRESSIVE_JS})
    except:
        pass

def wait_for_page_load(driver, timeout=15):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]
    )

def wait_cdp_download(driver, timeout):
    deadline = time.time() + timeout
    seen = set()
    skip = {"doubleclick", "google", "gstatic", "cloudflare", "facebook", "analytics"}
    exts = (".rar", ".zip", ".7z", ".exe", ".part", ".iso", ".mkv", ".mp4", ".bin")
    while time.time() < deadline:
        try:
            logs = driver.get_log("performance")
        except:
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
            except:
                continue
        time.sleep(0.1)
    return None

def drain_logs(driver):
    try:
        driver.get_log("performance")
    except:
        pass

def get_file_type(filename):
    if not filename:
        return "Unknown"
    ext = filename.split('.')[-1].lower() if '.' in filename else ''
    mapping = {
        'rar': 'RAR File',
        'zip': 'ZIP File',
        '7z': '7-Zip File',
        'exe': 'Executable File',
        'iso': 'ISO Image',
        'mkv': 'MKV Video',
        'mp4': 'MP4 Video',
        'bin': 'BIN File',
        'part': 'Partial File',
        'txt': 'Text File',
        'pdf': 'PDF Document',
        'doc': 'Word Document',
        'docx': 'Word Document',
        'xls': 'Excel Spreadsheet',
        'xlsx': 'Excel Spreadsheet',
        'jpg': 'JPEG Image',
        'jpeg': 'JPEG Image',
        'png': 'PNG Image',
        'gif': 'GIF Image',
        'mp3': 'MP3 Audio',
        'wav': 'WAV Audio',
        'flac': 'FLAC Audio',
    }
    return mapping.get(ext, f"{ext.upper()} File" if ext else "Unknown")

def extract_file_info(driver):
    name = None
    size = None

    # 1. Extract Name independently
    try:
        name_elem = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "p.text-xs.font-medium.text-slate-700.m-0.truncate"))
        )
        # Use textContent to grab text even if visual rendering is delayed
        name = name_elem.get_attribute("textContent").strip()
    except Exception as e:
        print(f"⚠️ Exact name selector failed: {e}")

    # 2. Extract Size independently
    try:
        size_elem = driver.find_element(By.CSS_SELECTOR, "p.text-\\[11px\\].text-slate-400.m-0.mt-0.5")
        size = size_elem.get_attribute("textContent").strip()
    except Exception as e:
        print(f"⚠️ Exact size selector failed: {e}")

    # 3. Fallback: div.min-w-0.flex-1 with two p tags
    if not name or not size:
        try:
            container = driver.find_element(By.CSS_SELECTOR, "div.min-w-0.flex-1")
            paragraphs = container.find_elements(By.TAG_NAME, "p")
            if len(paragraphs) >= 2:
                if not name: name = paragraphs[0].get_attribute("textContent").strip()
                if not size: size = paragraphs[1].get_attribute("textContent").strip()
        except:
            pass

    # 4. Fallback: outer container and inner flex-1
    if not name or not size:
        try:
            outer = driver.find_element(By.XPATH, "//div[contains(@class, 'flex items-center gap-2.5 mb-4')]")
            inner = outer.find_element(By.CSS_SELECTOR, "div.min-w-0.flex-1")
            paragraphs = inner.find_elements(By.TAG_NAME, "p")
            if len(paragraphs) >= 2:
                if not name: name = paragraphs[0].get_attribute("textContent").strip()
                if not size: size = paragraphs[1].get_attribute("textContent").strip()
        except:
            pass

    # 5. Last resort: page title and any MB/GB text
    if not name:
        try:
            title = driver.title
            if title:
                name = title.strip()
        except:
            pass

    if not size:
        try:
            elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'MB') or contains(text(), 'GB')]")
            for el in elements:
                text = el.get_attribute("textContent").strip()
                if "MB" in text or "GB" in text or "KB" in text:
                    size = text
                    break
        except:
            pass

    return name, size

    # 3. Fallback: outer container and inner flex-1
    try:
        outer = driver.find_element(By.XPATH, "//div[contains(@class, 'flex items-center gap-2.5 mb-4')]")
        inner = outer.find_element(By.CSS_SELECTOR, "div.min-w-0.flex-1")
        paragraphs = inner.find_elements(By.TAG_NAME, "p")
        if len(paragraphs) >= 2:
            name = paragraphs[0].text.strip()
            size = paragraphs[1].text.strip()
            if name and size:
                return name, size
    except:
        pass

    # 4. Last resort: page title and any MB/GB text
    try:
        if not name:
            title = driver.title
            if title:
                name = title.strip()
        if not size:
            elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'MB') or contains(text(), 'GB')]")
            for el in elements:
                text = el.text.strip()
                if "MB" in text or "GB" in text or "KB" in text:
                    size = text
                    break
    except:
        pass

    return name, size

def execute_extraction(url: str):
    print(f"🚀 Starting extraction for: {url}")
    driver = get_driver()
    try:
        enable_download_events(driver)
        inject_timer_killer(driver)

        driver.get(url)
        wait_for_page_load(driver, timeout=15)

        # 1. Continue
        try:
            btn = WebDriverWait(driver, 0.5).until(EC.presence_of_element_located((By.XPATH, "//button[@id='method_free']")))
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

        # 2. Free Download
        try:
            btn = WebDriverWait(driver, 0.5).until(EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Free Download')]")))
            driver.execute_script("""
                arguments[0].disabled = false;
                arguments[0].removeAttribute('disabled');
                arguments[0].click();
            """, btn)
            print("✅ Forced Free Download")
        except Exception as e:
            print(f"⚠️ Free Download click failed: {e}")

        # 3. Start Download
        try:
            btn = WebDriverWait(driver, 0.5).until(EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Start Download')]")))
            driver.execute_script("""
                arguments[0].disabled = false;
                arguments[0].removeAttribute('disabled');
                arguments[0].click();
            """, btn)
            print("✅ Forced Start Download")
        except Exception as e:
            print(f"⚠️ Start Download click failed: {e}")

        drain_logs(driver)
        dl_url = wait_cdp_download(driver, timeout=DOWNLOAD_TIMEOUT)

        # Wait for the name element to appear before extracting
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "p.text-xs.font-medium.text-slate-700.m-0.truncate"))
            )
        except:
            pass

        file_name, file_size = extract_file_info(driver)

        if dl_url:
            print(f"✅ Captured: {dl_url[:100]}...")
            file_type = get_file_type(file_name)
            return {
                "status": "success",
                "original_url": url,
                "name": file_name,
                "size": file_size,
                "file_type": file_type,
                "bypassed_url": dl_url
            }
        else:
            print("❌ No download URL captured.")
            # fallback direct link
            try:
                links = driver.find_elements(By.XPATH, "//a[contains(@href, '.rar') or contains(@href, '.zip') or contains(@href, '.7z')]")
                for link in links:
                    href = link.get_attribute("href")
                    if href:
                        file_type = get_file_type(file_name)
                        return {
                            "status": "success",
                            "original_url": url,
                            "name": file_name,
                            "size": file_size,
                            "file_type": file_type,
                            "bypassed_url": href
                        }
            except:
                pass
            raise HTTPException(status_code=404, detail="Download URL not captured within timeout")

    except Exception as e:
        print(f"❌ Extraction failed: {e}")
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")

# ---------- API ----------
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
