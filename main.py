import os
import json
import time
import asyncio
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, HttpUrl
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

app = FastAPI(title="Link Extractor API")

# Global lock to enforce single URL processing at a time (saves RAM on free hosting)
processing_lock = asyncio.Lock()

DOWNLOAD_TIMEOUT = 25

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
            if (btn.textContent.includes('Free Download') || btn.textContent.includes('Start Download') || btn.textContent.includes('Continue')) {
                btn.disabled = false;
                btn.removeAttribute('disabled');
            }
        });
    }, 50);
})();
"""

class UrlRequest(BaseModel):
    url: HttpUrl

def get_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1280,720")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--mute-audio")
    
    # Locate Chromium binary paths inside Linux environments automatically
    if os.path.exists("/usr/bin/chromium-browser"):
        options.binary_location = "/usr/bin/chromium-browser"
    elif os.path.exists("/usr/bin/chromium"):
        options.binary_location = "/usr/bin/chromium"

    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
    }
    options.add_experimental_option("prefs", prefs)
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(25)
    return driver

def wait_cdp_download(driver, timeout):
    deadline = time.time() + timeout
    seen = set()
    skip = {"doubleclick", "google", "gstatic", "cloudflare", "facebook", "analytics"}
    exts = (".rar", ".zip", ".7z", ".exe", ".part", ".iso", ".mkv", ".mp4", ".bin")
    while time.time() < deadline:
        try:
            logs = driver.get_log("performance")
        except Exception:
            time.sleep(0.2)
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
                    if not url or url in seen or any(s in url for s in skip):
                        continue
                    seen.add(url)
                    if url.lower().endswith(exts) or any(e in url.lower() for e in exts):
                        return url
            except Exception:
                continue
        time.sleep(0.2)
    return None

def execute_extraction(url: str):
    driver = None
    try:
        driver = get_driver()
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _AGGRESSIVE_JS})
        except Exception:
            pass

        driver.get(url)
        WebDriverWait(driver, 15).until(lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"])

        # Step 1: Click Method Free
        btn = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, "//button[@id='method_free']")))
        driver.execute_script("arguments[0].disabled = false; arguments[0].removeAttribute('disabled'); arguments[0].click();", btn)

        # Step 2: Free Download Click
        btn = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Free Download')]")))
        driver.execute_script("arguments[0].disabled = false; arguments[0].removeAttribute('disabled'); arguments[0].click();", btn)

        # Step 3: Start Download Click
        btn = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Start Download')]")))
        driver.execute_script("arguments[0].disabled = false; arguments[0].removeAttribute('disabled'); arguments[0].click();", btn)

        # Step 4: Capture log URLs
        try:
            driver.get_log("performance") # drain old logs
        except Exception:
            pass
            
        dl_url = wait_cdp_download(driver, timeout=DOWNLOAD_TIMEOUT)
        if dl_url:
            return {"status": "success", "original_url": url, "download_url": dl_url}
        else:
            raise HTTPException(status_code=404, detail="Failed to capture download link within timeout window")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

@app.post("/extract")
async def extract_url(payload: UrlRequest):
    # Lock ensures the server processes sequentially (1 at a time)
    async with processing_lock:
        # Run synchronous selenium code in threadpool to prevent blocking the async event loop
        result = await asyncio.to_thread(execute_extraction, str(payload.url))
        return result
