import os
import sys
import json
import time
import re
from datetime import datetime #to handle clock formatting
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError #stealth requirements
from playwright_stealth import Stealth #stealth requirements

# ==========================
# command line configuration
# ==========================
# Default fallbacks
NAME_FORMAT = "full"
MAX_ATTEMPTS = 4 

#terminal commands reading and setting all variables
if 3 <= len(sys.argv) <= 5:
    arg1 = sys.argv[1].strip()
    DOWNLOAD_DIR = sys.argv[2].strip()
    
    #if exactly ONE of the two optional argument is provided
    if len(sys.argv) == 4:
        optional_arg = sys.argv[3].strip().lower() #takes the input and puts it in lowercase if applicable
        if optional_arg.isdigit(): #checks if it's a digit -> trats it as the retry amount
            MAX_ATTEMPTS = int(optional_arg)
            if MAX_ATTEMPTS < 1:
                print("\n[ERROR] Max attempts must be at least 1.")
                sys.exit(1)
        else: #if it's not, treat it as the filename type
            if optional_arg in ["full", "info", "author", "title"]:
                NAME_FORMAT = optional_arg
            else:
                print(f"\n[ERROR] Invalid file naming format '{optional_arg}'.")
                print("Please use one of: 'full', 'info', 'author', or 'title'.")
                sys.exit(1)
                
    #if BOTH optional arguments are provided
    elif len(sys.argv) == 5:
        parsed_format = sys.argv[3].strip().lower() #first of the two HAS to be the filename type
        if parsed_format in ["full", "info", "author", "title"]:
            NAME_FORMAT = parsed_format
        else:
            print(f"\n[ERROR] Invalid file naming format '{parsed_format}'.")
            print("Please use one of: 'full', 'info', 'author', or 'title'.")
            sys.exit(1)
            
        try:
            MAX_ATTEMPTS = int(sys.argv[4].strip()) #second is always retry amount
            if MAX_ATTEMPTS < 1:
                print("\n[ERROR] Max attempts must be at least 1.")
                sys.exit(1)
        except ValueError:
            print(f"\n[ERROR] Invalid max attempts '{sys.argv[4]}'. Please provide a valid integer (e.g., 5).")
            sys.exit(1)

    #script mode select
    if arg1.lower() == "text":
        TXT_MODE = True
        RETRY_MODE = False
    elif arg1.lower() == "retry":
        RETRY_MODE = True
        TXT_MODE = False
    else:
        #if it's not 'text' or 'retry', assume it's a list URL
        #URL VALIDATION CHECK ADDED HERE
        if not (arg1.startswith("http") and "annas-archive." in arg1.lower()):
            print(f"\n[ERROR] Invalid URL provided!")
            print(f"'{arg1}' does not appear to be a valid Anna's Archive link.")
            print("Please provide a valid link, or use 'text' or 'retry' mode.")
            sys.exit(1)
            
        LIST_URL = arg1
        TXT_MODE = False
        RETRY_MODE = False

    ##usage instructions
else:
    print("Invalid arguments! Please use one of the following formats:")
    print('  python auto_downloader.py "https://annas-archive.XX/list/<list_id>" "<pathToYourFolder>" [filename format] [max retries]')
    print('  python auto_downloader.py text "<pathToYourFolder>" [filename format] [max retries]')
    print('  python auto_downloader.py retry "<pathToYourFolder>" [filename format] [max retries]')
    print("\nAvailable file naming formats (optional):")
    print("full (all known file info) [default],")
    print("info (all known file info without the md5 code and the Anna's Archive source),")
    print("author (Title and Author, in which case a hyphen will be put between the two),")
    print("title (just the Title)")
    print("\nMax retries: how many times the script will try to download a singular file if it fails.")
    print("Must be an integer greater than 0.")
    sys.exit(1)

# these must be defined AFTER DOWNLOAD_DIR is updated by the terminal arguments
TXT_FILE = os.path.join(DOWNLOAD_DIR, "aa_links.txt")
COMPLETED_FILE = os.path.join(DOWNLOAD_DIR, "completed.txt")
FAILED_FILE = os.path.join(DOWNLOAD_DIR, "failed_downloads.json")
# =============================================================================

def load_completed(): #function to access the completed.txt file
    if os.path.exists(COMPLETED_FILE):
        with open(COMPLETED_FILE, "r", encoding="utf-8") as f: # "r" = reading mode
            return set(line.split(" ||| ")[0].strip() for line in f if line.strip()) #grabs the url which is before the " ||| "
    return set()

def mark_completed(url, title): #function to update the completed.txt file when a file successfully downloads
    with open(COMPLETED_FILE, "a", encoding="utf-8") as f: # "a" = append mode -> adds on another line
        safe_title = title.replace('\n', ' ').replace('\r', '').strip() #cleans "\" type characters (\n (enter) and \r (line break)) so they don't break the .txt
        f.write(f"{url} ||| {safe_title}\n") #saves

def save_failed_log(failed_items): #function to update the failed_downloads.json file when a file fails all its retries
    with open(FAILED_FILE, "w", encoding="utf-8") as f: # "w" = writing mode, used for the json. not "a" (append) because writing the json structure is more complicated 
        json.dump(failed_items, f, indent=4, ensure_ascii=False) #saves failed links in the json

def load_failed_log(): #function to access the failed_downloads.json file
    if os.path.exists(FAILED_FILE):
        with open(FAILED_FILE, "r", encoding="utf-8") as f: #loads the json
            try:
                return json.load(f)
            except Exception: #if file is corrupted, move on
                return []
    return []

def find_valid_link(page, text_match=None, href_match=None): #function search for links in the page
    ##finds the first link containing the text or href that is NOT a dummy '#' link.
    selectors = []
    if text_match:
        selectors.append(f"a:has-text('{text_match}')")
    if href_match:
        selectors.append(f"a[href*='{href_match}']")
        
    combined_selector = ", ".join(selectors)
    
    for el in page.locator(combined_selector).all():
        h = el.get_attribute("href")
        if h and h not in ["#", "", "javascript:void(0)", "javascript:"]: #this ensures it's not a fake button
            return el
    return None

def clean_downloaded_title(filename, url):
    ##cleans the downloaded filename by removing extensions, MD5 hashes, and Anna's Archive tags.
    name = os.path.splitext(filename)[0] #removes extension
    
    md5_hash = url.rstrip('/').split('/')[-1] #identifies md5 code
    if len(md5_hash) == 32:
        name = re.sub(md5_hash, "", name, flags=re.IGNORECASE) #removes md5 code
        
    name = re.sub(r"[-_]*\s*Anna['’`\s]?s\s*Archive", "", name, flags=re.IGNORECASE) #removes the "Anna's Archive" text
    name = name.replace("[]", "").replace("()", "")
    return name.strip(" -_")

def generate_custom_filename(original_name, url, format_type):
    ##generates the final filename based on the user's terminal choice.
    if format_type == "full":
        return original_name
        
    #get the file extension (e.g., .cbr, .epub)
    ext = os.path.splitext(original_name)[1]
    
    #get the cleaned base string (Title - Author - Publisher)
    clean_base = clean_downloaded_title(original_name, url)
    
    if format_type == "info":
        return f"{clean_base}{ext}"
        
    #splitting ONLY by exact double hyphens surrounded by spaces (else it will also deleted intentional "-" in the name)
    parts = clean_base.split(" -- ")
    
    if format_type == "author":
        ##checks if there are at least 2 fields to grab
        if len(parts) >= 2:
            return f"{parts[0].strip()} - {parts[1].strip()}{ext}"
        else:
            return f"{clean_base}{ext}"
            
    if format_type == "title":
        return f"{parts[0].strip()}{ext}"
        
    return original_name

def format_elapsed_time(seconds):
    ##helper function to convert raw seconds (how time gets counted) into human-readable format
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours} hours, {minutes} minutes and {secs} seconds"

def get_unique_filename(directory, filename): #checks if a file exists and appends (1), (2), etc. to prevent overwriting.
    ##avoids some files not getting ultimately saved if multiple links in List downloaded a file with the same filename
    base_name, ext = os.path.splitext(filename)
    counter = 1
    file_path = os.path.join(directory, filename)
    
    while os.path.exists(file_path):
        filename = f"{base_name} ({counter}){ext}"
        file_path = os.path.join(directory, filename)
        counter += 1
        
    return filename

def truncate_error(error_msg, max_length=120): #to not leave multi-line error messages in terminal
    ##cuts long error messages, removes newlines, and strips playwright's "Call log:" dumps
    msg = str(error_msg).strip()
    
    #removing playwright's full call log entirely (it's still the the log).
    if "Call log:" in msg:
        msg = msg.split("Call log:")[0].strip()
        
    #remove any internal newline characters that break terminal formatting
    msg = msg.replace('\n', ' ').replace('\r', '')
        
    #if it's still too long, hard-cap the character limit
    if len(msg) > max_length:
        msg = msg[:max_length] + "... [truncated]"
        
    return msg

def log_debug_error(directory, url, full_error, attempt, max_attempts, idx, total, page_title): #for the (normally hidden) error log (for debugging purposes)
    ##appends the full error and execution context to the log
    log_path = os.path.join(directory, ".hearth_debug.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] Queue: [{idx}/{total}] | Attempt: {attempt}/{max_attempts}\n")
        f.write(f"URL: {url}\n")
        f.write(f"Page Title: {page_title}\n")
        f.write(f"ERROR:\n{full_error}\n")
        f.write("-" * 60 + "\n\n")

def check_for_page_error(page, response=None): #MUST CHECK IF THIS INTERFERES WITH DOWNLOADS THAT START FROM AN ERRORED PAGE (implementing it for now)
    ##checks HTTP status, tab title, and headers for server errors (including cloudflare specific error messages / numbers)
    #note: 403 is omitted because Cloudflare uses it for CAPTCHA challenge pages
    error_codes = [
        404, 429, 500, 502, 503, 504, #403,
        520, 521, 522, 523, 524, 525, 526, 527, 530
    ]
    
    if response and response.status in error_codes:
        raise Exception(f"HTTP {response.status} returned by server")
        
    try:
        title = page.title().strip()
        title_lower = title.lower()
        
        #checks for isolated numeric errors using word boundaries (\b) to prevent MD5 hash collisions (ask why this had to be done lmao)
        if re.search(r'\b(404|429|500|502|503|504|520|521|522|523|524|525|526|527|530)\b', title_lower):
            raise Exception(f"Error page detected via title: '{title}'")
            
        #standard (+ cloudflare specific) text-based error keywords (ADD MORE IF FOUND)
        text_errors = [
            "service unavailable", "temporarily unavailable", 
            "bad gateway", "gateway time-out", "gateway timeout", 
            "not found", "server error", "too many requests", 
            "web server is down", "origin is unreachable", 
            "connection timed out", "host error"
        ]

        #reports the error to the user in the terminal, then raises exeption (-> go on to next try / file)
        if any(err in title_lower for err in text_errors):
            raise Exception(f"Error page detected via title: '{title}'")
            
    except Exception as e:
        if "Error page detected" in str(e):
            raise e

def wait_for_element_or_error(page, selector, timeout_seconds=90, response=None): #function to check constantly if a page errors out
    ##waits for an element while continuously checking for 503/404 errors so it never gets stuck
    start_time = time.time()
    while True:
        # 1. check if the element has appeared in the DOM
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass

        # 2. check if the tab title or page has become an error page with above function
        check_for_page_error(page, response)

        # 3. check for timeout (only if no CAPTCHA challenge is active)
        elapsed = time.time() - start_time
        if elapsed > timeout_seconds:
            #If a Cloudflare challenge is actively on screen, give extra time
            is_captcha = False
            try:
                title_lower = page.title().lower()
                if "just a moment" in title_lower or "attention required" in title_lower:
                    is_captcha = True
                elif page.locator("iframe[src*='challenges'], .cf-turnstile").first.is_visible():
                    is_captcha = True
            except Exception:
                pass

            if not is_captcha:
                raise PlaywrightTimeoutError(f"Timed out after {timeout_seconds}s waiting for '{selector}' (stalled/blank page)")

        page.wait_for_timeout(1000) #pause 1 second before checking again

def main():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

    completed_urls = load_completed() #loads the content of completed.txt in a set (array) which is in ram (called "completed_urls")
    failed_items = load_failed_log() #does the same with failed_downloads.json (set is called "failed_items")
    
    ##session tracking counters
    session_success = 0
    session_failed = 0
    session_skipped = 0
    
    ##start the master stopwatch for the script
    script_start_time = time.time()
    launch_time_str = datetime.now().strftime("%H:%M")
    
    print("\n~ Welcome to hearth - an Anna's Archive automatic List bulk download script ~\n")
    print(f"Time of launch: {launch_time_str}")
    print("Booting up the automation browser...")
    
    with sync_playwright() as p: #starts the browser engine
        #added stealth args to possibly reduce the amount of captchas to solve
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"]) # headless = false to solve captchas, also sets webdriver to false (to not get targeted captchas)
        context = browser.new_context(accept_downloads=True, locale="en-US") #accepts downloads is obvious, sets language to ensure buttons have the correct text for identification
        
        stealth = Stealth() #initializes the new Stealth class to the browser context (playwright-stealth)
        stealth.apply_stealth_sync(context) #applies playwright-stealth to the page (simulates a human-operated browser)
        
        page = context.new_page()

        # 1. Target Selection
        if TXT_MODE:
            print(f"--- TXT MODE ACTIVE ---")
            if not os.path.exists(TXT_FILE):
                print(f"Cannot find {TXT_FILE}. Please create it and paste your links.")
                return
            with open(TXT_FILE, "r", encoding="utf-8") as f: #opens file in read mode
                unique_links = [line.strip() for line in f if line.strip()] #sets link target, strips links from the file
            targets = [{"url": link, "title": "Unknown"} for link in unique_links] #takes the urls as raw strings and puts them in the "struct" -> reformats data as the retry mode or list link mode. title is set to unknown because it's not part of the link (specified for data uniformity -> the format is equal to the other modes and is compatible with the for loop).
            print(f"Loaded {len(targets)} links from text file.\n")
            
        elif RETRY_MODE:
            print(f"--- RETRY MODE ACTIVE ---")
            targets = load_failed_log() #opens the failed_items set (array), saves the content in the targets array
            if not targets:
                print("No failed items found to retry! Exiting.")
                return
            print(f"Found {len(targets)} failed items to retry.\n")
            
        else:
            print(f"Loading your list: {LIST_URL}")
            print("  [*] Note: If a CAPTCHA appears, please solve it. The script will wait up to 2 minutes.")
            response = page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
            
            # --- CAPTCHA WAITING LOGIC ---
            try:
                wait_for_element_or_error(page, "main", timeout_seconds=120, response=response) #waits 2 mins for core html to appear to give time to solve the captcha, catching errors
                page.wait_for_timeout(2000)
            except PlaywrightTimeoutError:
                pass
            # -----------------------------
            
            hrefs = page.eval_on_selector_all("main a[href*='/md5/']", "elements => elements.map(e => e.href)") #grabs all md5 links FROM MAIN (not from the other parts of the site, e.g. the recently downloaded carousel)
            unique_links = []
            for href in hrefs:
                if href not in unique_links: #removes duplicates
                    unique_links.append(href)
            targets = [{"url": link, "title": "Unknown"} for link in unique_links] #put all extracted links into a "targets" array. title is set to unknown (one of the reasons is in case the download fails, to write the failure in the json) -> data uniformity
            print(f"Found {len(targets)} total items in list.\n")
            if len(targets) == 0:
                print("Your link is probably mistyped, or your List is not accessible.")
                print("Is Anna's Archive available in your country?\n")
                print("Did you complete the Captcha within the time limit?")
                return

        # 2. Download Execution
        for idx, item in enumerate(targets, 1):
            url = item["url"]
            
            if url in completed_urls: #checks if the url is in the completed.txt file, in which case it skips it
                linkAlreadyCompleted_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                print(f"[{idx}/{len(targets)}] [{linkAlreadyCompleted_time}] Skipping already completed item: {url}")
                session_skipped += 1
                continue

            processingNewLink_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
            print(f"[{idx}/{len(targets)}] [{processingNewLink_time}] Processing: {url}")
            failure_reason = None
            page_title = "Unknown"
            item_downloaded = False

            #clean retry loop: any error or 503 restarts the entire flow from the main book page (this was done because restarting downloads directly is impossible)
            max_attempts = MAX_ATTEMPTS
            for attempt in range(1, max_attempts + 1):
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    
                    # --- CAPTCHA / PAGE LOAD WAITING LOGIC ---
                    try:
                        wait_for_element_or_error(page, "main", timeout_seconds=90, response=response) #timeout set to 90 (infinite via captcha check) so the script doesn't fail overnight while waiting for a CAPTCHA, catching errors
                        page.wait_for_timeout(1500)
                    except PlaywrightTimeoutError:
                        pass
                    # -----------------------------------------

                    try:
                        page_title = page.title().split(" - ")[0].strip() #gets the page's title as backup
                    except Exception:
                        pass

                    #looks for download options
                    slow_link_locator = find_valid_link(page, text_match='Slow Partner Server')
                    libgen_link_locator = find_valid_link(page, text_match='Libgen.li', href_match='libgen.li')

                    if not slow_link_locator and not libgen_link_locator:
                        failure_reason = "No supported slow mirrors or Libgen links found on page" #if there's none of the two download options
                        break #no mirrors listed on book page, abort retries

                    # ------------------------------------------------
                    # Option 1 - if there is an AA Slow Partner Server
                    # ------------------------------------------------
                    if slow_link_locator:

                        foundLink_time_slowMirror = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                        print(f"  [*] [{foundLink_time_slowMirror}] Found standard Slow Partner Server link.")
                        
                        #goes to the mirror link, waits for it to load
                        href = slow_link_locator.get_attribute("href")
                        if href.startswith("/"):
                            parsed = urlparse(page.url)
                            href = f"{parsed.scheme}://{parsed.netloc}{href}"

                        navigatingTo_time_slowMirror = datetime.now().strftime("%H:%M:%S") #timestamp for message below    
                        print(f"  [*] [{navigatingTo_time_slowMirror}] Navigating directly to: {href}")
                        response = page.goto(href, wait_until="domcontentloaded", timeout=60000)

                        waitingForButton_start_time_slowMirror = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                        print(f"  [*] [{waitingForButton_start_time_slowMirror}] Waiting for the 'Download now' button (Timer/Captcha)...")
                        
                        try:
                            download_btn = wait_for_element_or_error(page, "a:has-text('Download now')", timeout_seconds=90, response=response) #waits indefinitely for a captcha resolution AND/OR for the timer, catching errors
                        except PlaywrightTimeoutError:
                            failure_reason = "AA Download button never appeared (Timeout) or the Captcha wasn't solved" #saves the failure reason as "download button never appeared"
                            raise Exception(failure_reason)

                        download_attempt_time_slowMirror = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                        print(f"  [*] [{download_attempt_time_slowMirror}] Attempting download from AA...")
                        
                        #set up background listener instead of blocking expect_download to actually notice downloads starting and not going ahead if the page arrors out but still starts the download
                        dl_box = []
                        def handle_aa_download(d):
                            dl_box.append(d)
                            
                        page.on("download", handle_aa_download)
                        download_btn.click(force=True)
                        
                        download = None
                        for _ in range(30): #30 second maximum wait for download to start
                            if dl_box:
                                download = dl_box[0]
                                break
                            check_for_page_error(page) #instantly errors if a 503 error page loads
                            page.wait_for_timeout(1000)
                            
                        page.remove_listener("download", handle_aa_download) #clean up listener properly using Python syntax
                        
                        if not download:
                            raise Exception("AA Download stream timeout / failed to start")
                        
                        original_name = download.suggested_filename #gets the AA suggested name
                        
                        #generates the file name based on the terminal argument
                        base_file_name = generate_custom_filename(original_name, url, NAME_FORMAT)
                        
                        #ensures the filename is unique to prevent overwriting different "editions" with the same title
                        file_name = get_unique_filename(DOWNLOAD_DIR, base_file_name)
                        file_path = os.path.join(DOWNLOAD_DIR, file_name)
                        
                        current_time_start = datetime.now().strftime("%H:%M:%S") #added download timestamp logic for AA slow mirror
                        dl_stopwatch_start = time.time() #starts the stopwatch
                        print(f"  [*] [{current_time_start}] Saving {file_name}...")
                        download.save_as(file_path)
                        dl_stopwatch_end = time.time() #ends the stopwatch
                        current_time_end = datetime.now().strftime("%H:%M:%S")
                        print(f"  [+] [{current_time_end}] Success! Saved: {file_name}")
                        print(f"  [+] Time elapsed during download: {format_elapsed_time(dl_stopwatch_end - dl_stopwatch_start)}\n")
                            
                        #generates the clean title from the actual downloaded file for the log
                        clean_title = clean_downloaded_title(original_name, url)
                        mark_completed(url, clean_title) #writes to the completed.txt file (append mode)
                        completed_urls.add(url) #updates set (array) in ram -> doesn't repeat the same link attempt
                        
                        existing_failed = next((f for f in failed_items if f["url"] == url), None) #removes file/url from the failed items json in case it was there
                        if existing_failed:
                            failed_items.remove(existing_failed)
                            save_failed_log(failed_items)
                        
                        session_success += 1
                        item_downloaded = True
                        break

                    # ------------------------------------------------
                    # Option 2 - if there is a Libgen.li direct mirror
                    # ------------------------------------------------
                    elif libgen_link_locator:

                        foundLink_time_libgen = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                        print(f"  [*] [{foundLink_time_libgen}] Standard mirror not found. Found Libgen.li link!")
                        
                        #goes to the libgen link, waits for it to load
                        href = libgen_link_locator.get_attribute("href")
                        if href.startswith("/"):
                            parsed = urlparse(page.url)
                            href = f"{parsed.scheme}://{parsed.netloc}{href}"

                        navigatingTo_time_libgen = datetime.now().strftime("%H:%M:%S") #timestamp for message below    
                        print(f"  [*] [{navigatingTo_time_libgen}] Navigating directly to: {href}")
                        response = page.goto(href, wait_until="domcontentloaded", timeout=60000)

                        waitingForButton_start_time_libgen = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                        print(f"  [*] [{waitingForButton_start_time_libgen}] Waiting for Libgen GET button...")
                        
                        try:
                            get_btn = wait_for_element_or_error(page, "a:has-text('GET'), a[href*='get.php']", timeout_seconds=60, response=response) #waits for the GET button to load / become clickable, catching 503s
                        except PlaywrightTimeoutError:
                            failure_reason = "Libgen GET button never appeared (Timeout/503)" #no captcha as i don't think there are any on libgen
                            raise Exception(failure_reason)

                        download_attempt_time_libgen = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                        print(f"  [*] [{download_attempt_time_libgen}] Attempting download from Libgen.li...")
                        
                        #set up background listener instead of blocking expect_download to actually notice downloads starting and not going ahead if the page arrors out but still starts the download (happens a lot on libgen)
                        dl_box = []
                        def handle_libgen_download(d):
                            dl_box.append(d)
                            
                        page.on("download", handle_libgen_download)
                        get_btn.click(force=True)
                        
                        download = None
                        for _ in range(30): #30 second maximum wait for download to start
                            if dl_box:
                                download = dl_box[0]
                                break
                            check_for_page_error(page) #instantly errors if a 503 error page loads (again, happens often on libgen)
                            page.wait_for_timeout(1000)
                            
                        page.remove_listener("download", handle_libgen_download) #clean up listener properly using Python syntax
                        
                        if not download:
                            raise Exception("Libgen Download stream timeout / failed to start")
                        
                        original_name = download.suggested_filename #gets the AA suggested name
                        
                        #generate the file name based on the terminal argument
                        base_file_name = generate_custom_filename(original_name, url, NAME_FORMAT)
                        
                        #ensures the filename is unique to prevent overwriting different editions
                        file_name = get_unique_filename(DOWNLOAD_DIR, base_file_name)
                        file_path = os.path.join(DOWNLOAD_DIR, file_name)
                        
                        current_time_start = datetime.now().strftime("%H:%M:%S") #added timestamp logic for Libgen mirror
                        dl_stopwatch_start = time.time() #starts the stopwatch
                        print(f"  [*] [{current_time_start}] Saving {file_name}...")
                        download.save_as(file_path)
                        dl_stopwatch_end = time.time() #ends the stopwatch
                        current_time_end = datetime.now().strftime("%H:%M:%S")
                        print(f"  [+] [{current_time_end}] Success! Saved from Libgen: {file_name}")
                        print(f"  [+] Time elapsed during download: {format_elapsed_time(dl_stopwatch_end - dl_stopwatch_start)}\n")
                            
                        #generate the clean title from the actual downloaded file for the log
                        clean_title = clean_downloaded_title(original_name, url)
                        mark_completed(url, clean_title)
                        completed_urls.add(url)
                        
                        existing_failed = next((f for f in failed_items if f["url"] == url), None) #removes url/file from the failed items json in case it was there
                        if existing_failed:
                            failed_items.remove(existing_failed)
                            save_failed_log(failed_items)
                            
                        session_success += 1
                        item_downloaded = True
                        break

                except Exception as e: #if link dead, timeout or error -> script doesn't crash
                    full_error = str(e)
                    short_error = truncate_error(full_error)
                    failure_reason = f"Script exception: {short_error}"
                    
                    #writes the full error and context to the debug log
                    log_debug_error(DOWNLOAD_DIR, url, full_error, attempt, max_attempts, idx, len(targets), page_title)
                    
                    if attempt < max_attempts: #retry logic
                        encounteredError_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                        print(f"  [-] [{encounteredError_time}] Encountered error ({short_error}). Reloading from main book page (Attempt {attempt + 1}/{max_attempts})...")
                        time.sleep(3)
                        continue
                    else:
                        break

            if item_downloaded:
                time.sleep(3) #3 seconds of wait before the next link
                continue

            #log failures <- if nothing else happened and all retries failed
            failedLink_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
            print(f"  [-] [{failedLink_time}] Failed: {failure_reason}\n")
            session_failed += 1
            existing = next((f for f in failed_items if f["url"] == url), None) #reads the failed_items set (which are in ram, after the script opened the json on line 164) that's full of structs, finds if the current failed link is already present
            if existing:
                existing["reason"] = failure_reason #updates failure reason
            else:
                failed_items.append({ #adds the whole link to the failed_items (in ram)
                    "url": url,
                    "title": page_title,
                    "reason": failure_reason
                })
            
            save_failed_log(failed_items)#writes in ram changes of the json to the file

            time.sleep(3) #3 seconds of wait before the next link

        #calculates final time
        script_end_time = time.time()
        end_time_str = datetime.now().strftime("%H:%M")
        
        #final verdicts
        print("\n")
        print(f"Finished processing queue at {end_time_str}!")
        print(f"Total time lapsed: {format_elapsed_time(script_end_time - script_start_time)}.")
        print(f"- Total Processed: {len(targets)}")
        print(f"- Successfully Downloaded: {session_success}")
        print(f"- Failed: {session_failed}")
        if session_skipped > 0:
            print(f"- Skipped (Already Completed): {session_skipped}")
        print("\n")
        print(f"You can find the list of downloaded links in the completed.txt file, in your download directory ({DOWNLOAD_DIR}).")
        if session_failed > 0:
            print(f"You can find the list of failed links in the failed_downloads.json file, in your download directory ({DOWNLOAD_DIR}).")
            print("To fix the failed downloads, you can run this script again in 'retry mode' which will try to download all your failed links again.")
        print("\n")
        
        browser.close() #shuts down playwright

if __name__ == "__main__": #standard python, tells to execute main when launched from the terminal
    main()
