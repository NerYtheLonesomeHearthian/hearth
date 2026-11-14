import os
import sys
import json
import time
import re
import shutil #to know how wide the terminal is (for error message formatting)
import socket #to check if there's internet (see internet_is_up)
import traceback #to write the details of a crash in the debug log
import hashlib #to calculate the md5 code of the downloaded files (see verify_downloaded_md5)
import logging #used to hide unneecessary asyncio messages from playwright (they are not real errors, just a warning that a page was closed while a download was in progress)
from datetime import datetime #to handle clock formatting
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError #stealth requirements
from playwright_stealth import Stealth #stealth requirements


# ==========================
# command line configuration
# ==========================
# Default fallbacks
NAME_FORMAT = "full"
MAX_ATTEMPTS = 5

#terminal commands reading and setting all variables
if 3 <= len(sys.argv) <= 5:
    arg1 = sys.argv[1].strip()
    #expanduser turns a leading "~" into the user's home folder (on linux) or the current user folder (on windows). the shell does NOT do this inside quotes ("~/AAdownloads"), so without it the script would create a folder literally named "~" in the current directory
    DOWNLOAD_DIR = os.path.expanduser(sys.argv[2].strip())

    #inside quotes, windows reads a backslash followed by a quote as a literal quote character, so `"C:\AAdownloads\"` arrives here as `C:\AAdownloads"` (and any arguments after it get glued onto the path, e.g. `C:\AAdownloads" full 3`)
    #a quote can never be part of a valid windows path, so if one shows up the path got misinterpreted -> stops and explains instead of creating a wrong directory
    if '"' in DOWNLOAD_DIR:
        print("\n[ERROR] The folder path contains a quote character (probably a bad interpretation from the terminal).")
        print('This happens when a path inside quotes ends with a backslash, like "C:\\AAdownloads\\" (the \\" is read as a literal quote).')
        print('Fix it by removing the last backslash ("C:\\AAdownloads"), doubling it ("C:\\AAdownloads\\\\"), or using forward slashes ("C:/AAdownloads/").')
        print(f'What the script actually received as your download directory: "{DOWNLOAD_DIR}"')
        sys.exit(1)
    
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
    print('  python hearth.py "https://annas-archive.XX/list/<list_id>" "<pathToYourFolder>" [filename format] [max retries]')
    print('  python hearth.py text "<pathToYourFolder>" [filename format] [max retries]')
    print('  python hearth.py retry "<pathToYourFolder>" [filename format] [max retries]')
    print("\nAvailable file naming formats (optional):")
    print("full (all known file info) [default],")
    print("info (all known file info without the md5 code and the Anna's Archive source),")
    print("author (Title and Author, in which case a hyphen will be put between the two),")
    print("title (just the Title)")
    print("\nMax retries: how many times the script will try to download a singular file if it fails.")
    print("Must be an integer greater than 0.\n")
    sys.exit(1)

# these must be defined AFTER DOWNLOAD_DIR is updated by the terminal arguments
TXT_FILE = os.path.join(DOWNLOAD_DIR, "aa_links.txt")
COMPLETED_FILE = os.path.join(DOWNLOAD_DIR, "completed.txt")
FAILED_FILE = os.path.join(DOWNLOAD_DIR, "failed_downloads.json")
# =============================================================================

#a book is identified by the md5 code in its link ("/md5/" + exactly 32 hex characters), whatever the domain (mirror) of the link
MD5_IN_URL_RE = re.compile(r"/md5/([0-9a-f]{32})(?![0-9a-f])", re.IGNORECASE)

def get_md5(url): #function to extract the md5 code (in lowercase) from a link, defaults to "None" if it has none
    match = MD5_IN_URL_RE.search(url)
    return match.group(1).lower() if match else None

def get_key(url): #function that returns the md5 code, which identifies a book in every comparison (completed list, failed list, duplicates)
    ##if the link has NO md5 code, the whole link is used instead. this only happens with hand-edited lines of completed.txt, or if anna's archive ever changes its link format
    ##in which case, if returning None all those links would share the same key (None), so the script would think they are all the same book and skip them -> why it stores the link/md5 instead
    return get_md5(url) or url

def load_completed(): #function to access the completed.txt file
    ##returns a dictionary {md5 code: the link the book was completed with}. old lines (saved with a full link) work as they are: the md5 code is taken from their link
    completed = {}
    if os.path.exists(COMPLETED_FILE):
        with open(COMPLETED_FILE, "r", encoding="utf-8") as f: # "r" = reading mode
            for line in f:
                if line.strip():
                    saved_url = line.split(" ||| ")[0].strip() #grabs the url which is before the " ||| "
                    completed.setdefault(get_key(saved_url), saved_url) #keeps the first link if the same book is somehow there more than once
    return completed

def mark_completed(url, title): #function to update the completed.txt file when a file successfully downloads
    with open(COMPLETED_FILE, "a", encoding="utf-8") as f: # "a" = append mode -> adds on another line
        safe_title = title.replace('\n', ' ').replace('\r', '').strip() #cleans "\" type characters (\n (enter) and \r (line break)) so they don't break the .txt
        f.write(f"{url} ||| {safe_title}\n") #saves

def save_failed_log(failed_items): #function to update the failed_downloads.json file when a file fails all its retries
    #writes to a temporary file first and only swaps it in once it is complete (see os.replace below), so a crash or power loss in the middle of writing can never leave a half-written failed_downloads.json
    tmp_file = FAILED_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f: # "w" = writing mode, used for the json. not "a" (append) because writing the json structure is more complicated 
        json.dump(failed_items, f, indent=4, ensure_ascii=False) #saves failed links in the json
        f.flush() #pushes python's buffer to the operating system (pushes from script memory to os memory)
        os.fsync(f.fileno()) #makes the os write it to the disk, so the temp file is complete before it replaces the real one
    os.replace(tmp_file, FAILED_FILE) #swaps the finished temp file in place of the old json (it overwrites the old file)

def load_failed_log(): #function to access the failed_downloads.json file
    if os.path.exists(FAILED_FILE):
        data = None
        with open(FAILED_FILE, "r", encoding="utf-8") as f: #loads the json
            try:
                data = json.load(f)
            except Exception: #if file is corrupted, move on
                data = None #handled below

        #the file counts as healthy only if it's a json list where every entry is an object with a "url" (same shape the rest of the script relies on)
        if isinstance(data, list) and all(isinstance(item, dict) and "url" in item for item in data):
            return data

        #if the file is corrupted or has the wrong shape. instead of returning an empty list (the next error logging attempt would then overwrite the file with only one line losing everything else) it gets renamed to a backup first
        #this happens AFTER the "with" block as windows can't rename a file that is still open
        base_name, ext = os.path.splitext(FAILED_FILE)
        backup_file = f"{base_name}.corrupt-{datetime.now().strftime('%Y_%m_%d-%H_%M_%S')}{ext}"
        try:
            os.replace(FAILED_FILE, backup_file)
        except OSError as e:
            print(f"\n[ERROR] {FAILED_FILE} is corrupted and could not be loaded ({e}).")
            print("Stopping the script here so the file doesn't get overwritten. Please fix or move it by hand and run the script again.")
            sys.exit(1)
        print(f"\n[WARNING] {FAILED_FILE} was corrupted or unreadable.")
        print(f"A backup was saved as {backup_file} and the failed list was started from scratch.\n")
    return []

#structure of a valid line of aa_links.txt: http(s) + an annas-archive.<tld> domain (optionally with a subdomain like www.) + "/md5/" + exactly 32 hex characters, with only "/", "?" or "#" after the md5 code
AA_MD5_LINK_RE = re.compile(r"^https?://(?:[a-z0-9-]+\.)*annas-archive\.[a-z]{2,}(?::\d+)?/md5/[0-9a-f]{32}(?:[/?#]|$)", re.IGNORECASE)

def validate_txt_links(lines): #function to filter the lines read from aa_links.txt, keeping only real anna's archive book links (shouldn't be necessary)
    ##comment lines (starting with #) are ignored, every other line that isn't a valid md5 link is returned separately
    valid = []
    invalid = []
    for line in lines: #lines are expected to be already stripped of spaces/newlines
        if line.startswith("#"): #comment line, skipped on purpose
            continue
        if AA_MD5_LINK_RE.match(line):
            valid.append(line)
        else:
            invalid.append(line)
    return valid, invalid

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

def strip_md5_and_tag(filename, url): #function that removes the extension, the md5 code and the "Anna's Archive" tag from a downloaded filename (used by the two functions below)
    ##the separators at the start/end of the result are NOT trimmed here: each caller trims what it needs
    name = os.path.splitext(filename)[0] #removes extension
    
    md5_hash = get_md5(url) #identifies md5 code
    if md5_hash:
        name = re.sub(md5_hash, "", name, flags=re.IGNORECASE) #removes md5 code
        
    name = re.sub(r"[-_]*\s*Anna['’`\s]?s\s*Archive", "", name, flags=re.IGNORECASE) #removes the "Anna's Archive" text
    name = name.replace("[]", "").replace("()", "")
    return name

def clean_downloaded_title(filename, url):
    ##cleans the downloaded filename by removing extensions, MD5 hashes, and anna's archive tags.
    return strip_md5_and_tag(filename, url).strip(" -_")

#matches a standalone "--" (surrounded by spaces or at the start/end of the text), NOT the ones inside words. the (?<!\S) and (?!\S) parts only look at what's before and after, without deleting it
STANDALONE_SEPARATOR_RE = re.compile(r"(?<!\S)--(?!\S)")

def split_name_fields(filename, url): #function to split a downloaded filename into its fields (Title -- Author -- Publisher -- ...), keeping the empty fields as empty strings
    ##only the END of the text is stripped of leftover separators and spaces (always) so an empty first field (empty title) stays an empty field instead of shifting all the other fields
    text = strip_md5_and_tag(filename, url).rstrip(" -_")
    return [field.strip() for field in STANDALONE_SEPARATOR_RE.split(text)] #the spaces next to each "--" stay in the pieces, strip() removes them

def generate_custom_filename(original_name, url, format_type):
    ##generates the final filename based on the user's terminal choice.
    if format_type == "full":
        return original_name
        
    #get the file extension (e.g., .cbr, .epub)
    ext = os.path.splitext(original_name)[1]
    
    #get the cleaned base string (Title - Author - Publisher)
    clean_base = clean_downloaded_title(original_name, url)
    
    #splitting ONLY by exact double hyphens surrounded by spaces (else it will also delete intentional "-" in the name)
    #it's done with a regex that matches only a standalone "--" and keeps the empty fields as empty strings, so the author is always the 2nd field even if the title or the publisher are missing
    parts = split_name_fields(original_name, url)
    filled_parts = [part for part in parts if part] #the same fields, without the empty ones
    title = parts[0]
    
    ##checks if there are at least 2 fields to grab
    #(a name without any " -- ", like the libgen ones, is a single field: it's used as it is, without notices)
    structured = len(parts) >= 2
    author = parts[1] if structured else ""
    
    reason = None #what to tell the user when the name is not exactly what the chosen format says
    
    if not structured:
        result = clean_base
    elif format_type == "info":
        result = " -- ".join(filled_parts) #only the non-empty fields
    elif format_type == "author":
        if title and author:
            result = f"{title} - {author}"
        elif title:
            result = title
            reason = "author empty, used title only"
        else:
            result = ""
    elif format_type == "title":
        result = title
    else:
        return original_name #unknown format (can't happen, the terminal argument is checked at the start)
    
    #nothing usable (no letter or digit at all) -> keeps whatever fields are left + the md5 code (never the "Anna's Archive" tag, as this is the fallback for any format that's not "full"), so the book can still be identified afterwards
    if not re.search(r"\w", result):
        md5_hash = get_md5(url)
        result = " -- ".join(filled_parts + ([md5_hash] if md5_hash else []))
        if not filled_parts:
            reason = "no usable fields, named after the md5"
        elif not title and not author:
            reason = "title and author empty, kept the rest + md5"
        elif not title:
            reason = "title empty, kept the rest + md5"
        else:
            reason = "no usable text, kept the rest + md5"
        if not result: #no fields and no md5 code in the link: the original name is the only thing left
            result = os.path.splitext(original_name)[0]
            reason = "no usable fields and no md5 code, kept the original name"
    
    if reason: #tells the user (terminal + .hearth_debug.log)
        report_note(url, "FILENAME FALLBACK", f"Filename: {reason}", f"Original name: {original_name}\nGenerated name: {result}{ext}")
    return f"{result}{ext}"

def format_elapsed_time(seconds):
    ##helper function to convert raw seconds (how time gets counted) into human-readable format
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours} hours, {minutes} minutes and {secs} seconds"

def shorten_filename(directory, filename, url=""): #function to shorten a filename that is too long for the file system
    ##limits: 255 bytes per name (BYTES, not characters: accented/cyrillic/chinese letters take 2-3 bytes each) and, on windows (with the default settings), 259 characters for the whole path (folder + name)
    ##a longer name makes save_as fail AFTER the whole file was downloaded, and every retry would download it all again
    stem, ext = os.path.splitext(filename)
    limit = 255
    if os.name == "nt": #windows
        limit = min(limit, 259 - len(os.path.abspath(directory)) - 1) #the -1 is for the "\" between the folder and the name
    
    #bytes available for the name without its extension. 8 bytes are kept free for the " (1)" that get_unique_filename can add, and it never goes below 20
    budget = max(limit - len(ext.encode("utf-8")) - 8, 20)
    original_size = len(stem.encode("utf-8"))
    
    if original_size <= budget:
        return filename #it fits: nothing gets touched
    
    steps = []
    
    #1. the "Anna's Archive" tag goes first (it only exists with the "full" format)
    shortened = re.sub(r"[-_\s]*Anna['’`\s]?s\s*Archive\s*$", "", stem, flags=re.IGNORECASE)
    if shortened != stem:
        steps.append("removed the Anna's Archive tag")
        stem = shortened
    
    #2. then the WHOLE md5 code (never just a part of it: a fragment can't rebuild the link). the full md5 is still in completed.txt
    if len(stem.encode("utf-8")) > budget:
        shortened = re.sub(r"[-_\s]*(?<![0-9a-f])[0-9a-f]{32}\s*$", "", stem, flags=re.IGNORECASE)
        if shortened != stem:
            steps.append("removed the md5 code")
            stem = shortened
    
    #3. if it's still too long, the end of the name gets cut. it's cut on bytes, but never in the middle of a character ("ignore" drops a half character)
    if len(stem.encode("utf-8")) > budget:
        stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore")
        steps.append(f"cut to {budget} bytes")
    
    stem = stem.rstrip(" .-_") #windows doesn't accept names ending with spaces or dots
    new_filename = f"{stem}{ext}"
    report_note(url, "FILENAME TRIMMED", f"Filename too long ({original_size} bytes without the extension, maximum {budget}), shortened: {', '.join(steps)}", f"Before: {filename}\nAfter: {new_filename}")
    return new_filename

def get_unique_filename(directory, filename, url=""): #checks if a file exists and appends (1), (2), etc. to prevent overwriting.
    ##avoids some files not getting ultimately saved if multiple links in List downloaded a file with the same filename (for example, when downloading multiple different uploads of the same media)
    filename = shorten_filename(directory, filename, url) #only shortens names that are too long for the file system. it's done here, AFTER the name was built from the chosen format, and leaving room for the (1), (2)... added below
    base_name, ext = os.path.splitext(filename)
    counter = 1
    file_path = os.path.join(directory, filename)
    
    while os.path.exists(file_path):
        filename = f"{base_name} ({counter}){ext}"
        file_path = os.path.join(directory, filename)
        counter += 1
        
    return filename

def discard_temp_download(download): #function to delete playwright's temporary copy of a download after it has been saved
    ##playwright keeps every download in its own temp folder until the whole browser closes, so without this a long run would store every file twice (once in the download folder, once in temp)
    try:
        download.delete()
    except Exception:
        pass #the real file is already saved at this point, so a failure here can't turn a successful download into a "failed" one (which would retry and re-download it)

MD5_NOTE_MIN_BYTES = 500 * 1024 * 1024 #from this size on, a "verifying..." message is printed before the md5 check (a big file takes several seconds to read). now set at 500MB

class Md5Mismatch(Exception): #raised when a downloaded file doesn't match the md5 code of its link (see verify_downloaded_md5). the main loop remembers the wrong md5 code
    def __init__(self, message, actual_md5):
        super().__init__(message)
        self.actual_md5 = actual_md5

def format_size(size_bytes): #function to write a size in bytes in a readable way (bytes, KB, MB, GB)
    size = float(size_bytes)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024

def file_md5(file_path): #function to calculate the md5 code of a file. it reads it in pieces of 1 MB, so even a huge file uses almost no memory
    md5_hasher = hashlib.md5()
    with open(file_path, "rb") as f: # "rb" = reading mode, in binary (the file is not text)
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5_hasher.update(chunk)
    return md5_hasher.hexdigest()

def verify_downloaded_md5(file_path, url, previous_bad_md5=None): #function to check that the downloaded file is the one the link points to: its hashed md5 code has to be the one in the link
    ##a corrupted download (like the tiny files libgen sometimes gives) should have a different hashed md5 code every time: the file is deleted and Md5Mismatch is raised, so the normal retry logic downloads it again
    ##fallback: if the SAME wrong md5 code shows up twice, it means the server always gives that exact file (anna's archive's md5 may describe another version of it): it's kept, with a warning
    expected_md5 = get_md5(url)
    if not expected_md5: #a link without md5 code: there's nothing to compare with
        return
    file_size = os.path.getsize(file_path)
    if file_size >= MD5_NOTE_MIN_BYTES: #big file: warns before, as reading it all takes a while
        hashing_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
        print(f"  [*] [{hashing_time}] Verifying the md5 code of the file ({format_size(file_size)}, this may take a while)...")
    actual_md5 = file_md5(file_path)
    verified_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
    if actual_md5 == expected_md5:
        print(f"  [+] [{verified_time}] md5 code verified")
        return
    if actual_md5 == previous_bad_md5: #second download with the SAME wrong md5 code: it's not a corrupted download, the server always gives this file
        report_note(url, "MD5 MISMATCH ACCEPTED", f"md5 code doesn't match the link's ({actual_md5[:8]}... instead of {expected_md5[:8]}...), but it's the same file as the previous download: keeping it", f"Expected: {expected_md5}\nGot: {actual_md5}\nFile: {file_path} ({format_size(file_size)})")
        return
    try:
        os.remove(file_path) #deletes the bad file, so the next attempt saves it under the same name (not as "(1)")
    except OSError:
        pass
    log_debug_note(DOWNLOAD_DIR, url, "MD5 MISMATCH", f"Expected: {expected_md5}\nGot: {actual_md5}\nFile: {file_path} ({format_size(file_size)})")
    raise Md5Mismatch(f"md5 mismatch: the link says {expected_md5[:8]}... but the downloaded file ({format_size(file_size)}) is {actual_md5[:8]}... (file deleted)", actual_md5)

def remove_from_failed_items(failed_items, url, already_completed=False): #function to remove a book from the failed items (in ram and in the json file) once it has been downloaded
    ##removes EVERY failed entry of that book (same md5 code), including the ones with a different link (mirror), and tells the user about those
    key = get_key(url)
    removed_any = False
    for entry in list(failed_items): #loops over a copy of the list, as items get removed from the original while looping
        if get_key(entry["url"]) == key:
            failed_items.remove(entry)
            removed_any = True
            if already_completed: #called when the book was SKIPPED because it was already completed: every entry found is stale (a leftover of an older version, another mirror, a hand edit...), so it's always reported
                report_note(url, "STALE ENTRY REMOVED", f"This book was already completed, removing its stale entry {entry['url']} from the retry list", f"Removed entry: {entry['url']} ({entry.get('title', 'Unknown')})")
            elif entry["url"] != url: #another link of the same book (same exact one, recognized by md5. this should not trigger with different uploads of the same media with the same title). reporting to user
                report_note(url, "DUPLICATE REMOVED", f"Download of {url} succeeded, removing failed duplicate {entry['url']} from the retry list", f"Removed entry: {entry['url']} ({entry.get('title', 'Unknown')})")
    if removed_any:
        save_failed_log(failed_items)
    return removed_any

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

def terminal_error_width(prefix, suffix): #function to calculate how many characters of an error message fit in a single terminal line, between a prefix and a suffix
    ##so the retry lines never wrap, whatever the size of the terminal (it's calculated every time, so resizing the window works). the "... [truncated]" mark that truncate_error adds is already counted, and 1 more column is left free because some terminals wrap a line that uses the very last column
    columns = shutil.get_terminal_size(fallback=(120, 30)).columns #120 if the output is not a terminal (redirected to a file, etc)
    return max(columns - len(prefix) - len(suffix) - len("... [truncated]") - 1, 30)

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

def log_debug_note(directory, url, kind, details): #for notes (not errors) in the same (normally hidden) log: changed filenames, skipped duplicates...
    ##appends a note to the log, marked as NOTE instead of ERROR so they can be told apart
    log_path = os.path.join(directory, ".hearth_debug.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] NOTE: {kind}\n")
            f.write(f"URL: {url}\n")
            f.write(f"{details}\n")
            f.write("-" * 60 + "\n\n")
    except OSError:
        pass #a note that can't be written must never make a download fail (it would be retried and downloaded again). the terminal message is still shown

def report_note(url, kind, message, details="", symbol="!"): #function to tell the user about a note in the terminal AND write it in the debug log
    note_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
    print(f"  [{symbol}] [{note_time}] {message}")
    log_debug_note(DOWNLOAD_DIR, url, kind, f"{message}\n{details}".strip())

PERMANENT_ERROR_CONFIRMATIONS = 3 #how many times IN A ROW the same permanent error (eg "file not present") has to show up for a book before the script stops retrying that book
PERMANENT_ERROR_MAX_TEXT = 300 #a page can only count as a permanent error page if its visible text has at most this many characters (error pages are short, real pages are not)

class PermanentFailure(Exception): #raised when the page reached after clicking the download button shows a known PERMANENT error (see raise_stalled_download_error). it's a different kind of error so the main loop can count it apart from the normal (retried) errors
    def __init__(self, message, page_text=""):
        super().__init__(message)
        self.page_text = page_text #the text the page showed (used in the failure reason)

class HideClosedFutureMessage(logging.Filter): #to not show the "Future exception was never retrieved" message that playwright's asyncio sometimes throws when a page is closed while a download is in progress. it is not an actual error and it prints after ctrl+C
    def filter(self, record):
        text = record.getMessage()
        return not ("Future exception was never retrieved" in text and "TargetClosedError" in text)

logging.getLogger("asyncio").addFilter(HideClosedFutureMessage())        

def read_page_snapshot(page): #function to read what the page shows right now: url, title and visible text (with all the spaces and line breaks collapsed) - made to spot errors
    ##every read is protected on its own: if one fails (page closed, in the middle of a navigation...) it doesn't hide the real error
    try:
        url = page.url
    except Exception:
        url = "unavailable"
    try:
        title = page.title().strip()
    except Exception:
        title = "unavailable"
    try:
        text = " ".join(page.inner_text("body", timeout=3000).split()) #3 seconds maximum
    except Exception:
        text = ""
    return url, title, text

def raise_stalled_download_error(page, clicked_from_url, normal_message): #function called when no download started in 30 seconds (after the download-starting button has been pressed): raises PermanentFailure if the page shows a known permanent error, or the normal error (with what the page shows added to its message)
    ##ALL of these have to be true for an error to count as permanent (the aim is NO false positives: a miss only means the normal retries happen, with the page text in the log):
    ##(1) no download started (the function is only called in that case)  (2) the browser LEFT the page where the button was clicked  (3) the page text is short (<=300chs)  (4) it contains one of the messages below
    ##the main loop needs this to happen PERMANENT_ERROR_CONFIRMATIONS times in a row (3 by default) (any other kind of error resets the count) to stop retrying the link in this session.
    url, title, text = read_page_snapshot(page)
    
    #messages of known permanent errors, in lowercase. they are searched inside the page text, ignoring upper/lower case (ADD MORE IF FOUND)
    permanent_errors = [
        "the repositories may not be synchronized" #libgen cdn page: "Error - File not found. The repositories may not be synchronized, try downloading later"
    ]
    
    left_the_page = url != "unavailable" and url.split("#")[0] != clicked_from_url.split("#")[0]
    short_page = 0 < len(text) <= PERMANENT_ERROR_MAX_TEXT
    
    if left_the_page and short_page and any(error in text.lower() for error in permanent_errors):
        #the page text goes first: the terminal line only has room for the first ~100 characters of the message (see terminal_error_width), and the text is the most useful part. the rest (and the "looks permanent" mark) is for the debug log
        raise PermanentFailure(f"'{text}' | page: {url} | title: '{title}' | download didn't start, error looks permanent", text)
    raise Exception(f"{normal_message} (title: '{title}' | text: '{text[:200]}') | page: {url}")

class RateLimited(Exception): #raised when a page answers "429 Too many requests". the main loop then waits longer than usual before the next attempt (see rate_limit_seconds)
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after #seconds the server asked to wait (Retry-After header). None if it didn't say

class StopRun(Exception): #raised to end the whole run on purpose (e.g. the browser was closed). the download loop catches it, prints the final summary and ends cleanly
    pass

RATE_LIMIT_FIRST_WAIT = 10 #seconds to wait after the first "too many requests" of a book (then 20, 40... doubling each time)
RATE_LIMIT_MAX_WAIT = 60 #the waiting time never grows over this many seconds (unless the server asks for more in its Retry-After header, with a limit of 300 seconds)

def rate_limit_seconds(error, hits): #function to calculate how many seconds to wait after the N-th "too many requests" in a row of the same book
    ##if the server said how long to wait (Retry-After), that's used (max 300 seconds). otherwise 10, 20, 40, 60, 60... seconds
    if error.retry_after:
        return min(error.retry_after, 300)
    return min(RATE_LIMIT_FIRST_WAIT * 2 ** (hits - 1), RATE_LIMIT_MAX_WAIT)

INTERNET_PROBES = [("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443)] #addresses used to check if there's internet. they are ip addresses (not names), so it doesn't depend on DNS. it's enough that ONE of them answers.
INTERNET_CHECK_SECONDS = 10 #while there's no internet, how often the connection is checked again
INTERNET_STATUS_MINUTES = 5 #while there's no internet, how often the "still offline" message is printed

def internet_is_up(site_url=None): #function to check if the computer has internet: it tries to open a connection to the addresses above (and to the site it's working with). it takes a few milliseconds when online
    ##the site we're working with is tried too (port 443), so a network that blocks the addresses above but lets the site through is not taken for "offline"
    probes = list(INTERNET_PROBES)
    if site_url:
        site_host = urlparse(site_url).hostname
        if site_host:
            probes.append((site_host, 443))
    for host, port in probes:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            continue
    return False

def wait_for_internet(site_url=None): #function that pauses the script while there's no internet and goes on as soon as it's back. there's no time limit (like the captcha wait), Ctrl+C stops it
    offline_since = time.time()
    last_status = offline_since
    report_note(site_url or "", "OFFLINE", f"No internet connection detected, waiting for it to come back (checking every {INTERNET_CHECK_SECONDS} seconds, no time limit). Press Ctrl+C to stop the script.", symbol="!")
    while not internet_is_up(site_url):
        time.sleep(INTERNET_CHECK_SECONDS)
        if time.time() - last_status >= INTERNET_STATUS_MINUTES * 60:
            status_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
            print(f"  [-] [{status_time}] Still offline after {format_elapsed_time(time.time() - offline_since)}...")
            last_status = time.time()
    report_note(site_url or "", "ONLINE AGAIN", f"Internet is back after {format_elapsed_time(time.time() - offline_since)}, resuming operation.", symbol="+")

def pause_before_retry(url, seconds): #function to wait before the next attempt. if the internet is down, it waits for it to come back instead (no time limit)
    if internet_is_up(url):
        time.sleep(seconds)
    else:
        wait_for_internet(url)

def browser_was_closed(page, browser): #function to check if the browser (or its window/tab) was closed
    ##uses playwright's own checks: the error that playwright raises for this (TargetClosedError at this time) can't be imported, and its text could change between versions
    try:
        return page.is_closed() or not browser.is_connected()
    except Exception:
        return True #if even asking fails, the browser is gone

BROWSER_INSPECT_SECONDS = 60 #how many seconds the browser stays open after a "blocked site" kind of error (certificate warning, block notice...), so the page can be looked at. it ends early if the browser is closed or Ctrl+C is pressed (see below)

def close_extra_pages(context, page): #function to close every tab of the browser except the main one. the "Download now" button on the slow partner server option opens the download page in a new tab: if that fails (504 error page, "too many downloads"...) the tab stays open, sometimes keeping its request pending on the server (using one of the downloads you're allowed at the same time) and the tabs pile up
    ##only called when NO download is in progress: closing the tab a download comes from would cancel that download
    try:
        for other_page in list(context.pages):
            if other_page is not page and not other_page.is_closed():
                other_page.close()
    except Exception:
        pass #the browser was closed, or a tab was already closing -> nothing to clean

def popup_is_loading(popup): #function to tell if a tab opened by the click is still waiting for the server's answer (a tab that hasn't received any answer yet is still on "about:blank")
    return not popup.is_closed() and popup.url in ("", "about:blank")

def check_popup_for_error(popup): #function to check if a tab opened by the "Download now" click shows an error instead of a download: raises it (RateLimited for "too many downloads"). does nothing if the tab is still loading, closed, or looks fine
    ##the tab shows the partner server's answer: an overloaded server usually answers "504 Gateway Time-out" after about a minute, and when too many downloads run from the same ip it says so in a plain page without title
    if popup.is_closed() or popup.url in ("", "about:blank"):
        return
    tab_host = urlparse(popup.url).netloc
    try:
        check_for_page_error(popup) #error codes / phrases in the title (like "504 Gateway Time-out"). a page with a <main> is never judged, so a real page is safe
    except Exception as tab_error:
        tab_error.args = (f"Partner server tab ({tab_host}): {tab_error}",) + tab_error.args[1:] #says it was the tab of the partner server
        raise
    #errors that come as a plain page: searched in the (short) text of the tab, in lowercase (ADD MORE IF FOUND)
    partner_errors = ["too many downloads at the same time"]
    try:
        popup_text = " ".join(popup.inner_text("body", timeout=2000).split())
    except Exception:
        return
    if len(popup_text) <= PERMANENT_ERROR_MAX_TEXT and any(error in popup_text.lower() for error in partner_errors):
        raise RateLimited(f"Partner server tab ({tab_host}): '{popup_text}'") #waits like a 429: the downloads that block it need time to end

def keep_browser_open(page, browser): #function to leave the browser open for a while, so the user can look at what it shows (for a certificate error: the browser's own warning). it ends early if the browser is closed or Ctrl+C is pressed (see above)
    if browser_was_closed(page, browser):
        return
    print(f"\n  [*] The browser stays open for {BROWSER_INSPECT_SECONDS} seconds so you can look at the page. Close it or press Ctrl+C to end this now.")
    try:
        for _ in range(BROWSER_INSPECT_SECONDS):
            if browser_was_closed(page, browser):
                break
            page.wait_for_timeout(1000)
    except KeyboardInterrupt:
        pass #Ctrl+C ends the wait immediately, and won't report the Ctrl+C being pressed (already specified in the printed message above)
    except Exception:
        pass #the browser was closed while waiting

def error_has_page_to_see(error): #function to tell if the browser shows something worth looking at after this List loading error (the certificate warning, or the page of an HTTP error). a name or connection error only shows the browser's own "can't be reached" page. takes the error itself
    return re.search(r"net::ERR_CERT|HTTP \d{3}|detected via title", str(error)) is not None

LIST_LOAD_ATTEMPTS = 2 #how many times the List page is loaded before giving up, but ONLY for errors that can go away by themselves (timeout, server error, too many requests). blocked site, certificate and name errors are never retried

def is_transient_load_error(error): #function to tell if an error while loading the List can go away by trying again
    if isinstance(error, (PlaywrightTimeoutError, RateLimited)):
        return True
    return re.search(r"HTTP 5\d\d|detected via title: '5\d\d", str(error)) is not None #server errors (500, 502, 503...)

def explain_list_failure(error, page): #function to build the lines that explain why the List could not be loaded: a hint that depends on the kind of error
    text = str(error)
    lines = []
    if "net::ERR_CERT" in text: #the site's certificate was refused: typical of a block page injected by the provider/DNS
        lines.append("Looks like the site didn't load. Are you prompted to trust the website (the browser says that the connection is not private)?")
        lines.append("In that case, Anna's Archive may not be available in your country: try a VPN or another DNS, then run the same command again.")
    elif "net::ERR_NAME_NOT_RESOLVED" in text: #the address of the site doesn't exist for your DNS
        lines.append("The address of the site could not be found: check your internet connection. If it works, your DNS may be blocking the site.")
        lines.append("Is Anna's Archive available in your country? Try a VPN or another DNS, then run the same command again.")
    elif re.search(r"HTTP \d{3}|detected via title", text): #a page loaded, but the site (or something in between) answered with an error: shows what the page says
        page_url, page_title, page_text = read_page_snapshot(page)
        lines.append(f"The page that loaded says: '{page_text[:200]}' (title: '{page_title}')")
        lines.append("Is Anna's Archive available in your country? If this is a block notice, it is not: try a VPN or another DNS, then run the same command again.")
        lines.append("Otherwise your link may be mistyped, or the site may be having problems: verify the URL or try again later.")
    elif isinstance(error, PlaywrightTimeoutError): #the site didn't answer in time (timeout)
        lines.append("The site took too long to answer (60 seconds). It may be down or very slow: try again later.")
    elif "net::ERR_" in text: #any other connection error (reset, refused, closed...)
        lines.append("The connection to the site failed. The site may be down, or blocked by your provider.")
        lines.append("Is Anna's Archive available in your country? Try a VPN or another DNS, then run the same command again.")
    else:
        lines.append("Check your link and your connection, then run the same command again.")
    return lines

def check_site_available(page, browser, site_url): #function to check, before the downloads start, that the site loads (the same check the List page goes through). returns True if it does. otherwise it prints why, leaves the browser open for a while and returns False (the run does not start)
    ##used in text and retry modes, where no List page is loaded: opens the homepage of the mirror of the first link to process. it follows the same rules as the List loading: one more try only for a timeout, a server error or "too many requests", waits (no limit) if there's no internet, and never retries a certificate error, a name error or a block page
    print(f"Checking that the site is reachable: {site_url}")
    print("  [*] Note: If a CAPTCHA appears, please solve it. The script will wait up to 2 minutes.")
    site_error = None #the error that stopped the site from loading (None = it loaded)
    site_attempt = 0
    while True:
        site_attempt += 1
        try:
            response = page.goto(site_url, wait_until="domcontentloaded", timeout=60000)
            try:
                wait_for_element_or_error(page, "main", timeout_seconds=120, response=response) #waits 2 mins for the core html to appear, to give time to solve a captcha (and catching errors)
            except PlaywrightTimeoutError:
                pass
            site_error = None
            break
        except Exception as e:
            site_error = e
            if browser_was_closed(page, browser): #the browser was closed: nothing else to do
                print("\n  [-] The browser was closed, stopping.\n")
                return False
            if not internet_is_up(site_url): #no internet: waits for it to come back (no time limit), and it doesn't use up a try
                wait_for_internet(site_url)
                site_attempt -= 1
                continue
            if site_attempt < LIST_LOAD_ATTEMPTS and is_transient_load_error(e): #a timeout, a server error or "too many requests" can go away by itself: one more try
                wait_seconds = rate_limit_seconds(e, 1) if isinstance(e, RateLimited) else 5
                siteRetry_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                print(f"  [-] [{siteRetry_time}] Could not load the site ({truncate_error(str(e), 80)}). Trying again in {wait_seconds} seconds (try {site_attempt + 1}/{LIST_LOAD_ATTEMPTS})...")
                time.sleep(wait_seconds)
                continue
            break
    
    if site_error is not None: #the site could not be loaded: tells the real reason + hint
        siteFailed_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
        print(f"\n  [-] [{siteFailed_time}] Could not load the site: {truncate_error(str(site_error), 200)}\n")
        site_explanation = explain_list_failure(site_error, page)
        for explanation_line in site_explanation:
            print(explanation_line)
        print("\nNothing was downloaded. Once the problem is fixed, run the same command again.")
        log_debug_note(DOWNLOAD_DIR, site_url, "SITE CHECK FAILED", f"{site_error}\n" + "\n".join(site_explanation))
        if error_has_page_to_see(site_error): #the browser is showing the warning / the page: leaves it open for a while (BROWSER_INSPECT_SECONDS)
            keep_browser_open(page, browser)
        return False
    
    #no error: but the page that loaded has to look like Anna's Archive. it does if it has a <main> or its title says "Anna's Archive". lenient on purpose: a false alarm would stop a good run, a miss only means no early warning
    try:
        site_has_main = page.locator("main").count() > 0
    except Exception:
        site_has_main = False
    site_page_url, site_page_title, site_page_text = read_page_snapshot(page)
    if site_has_main or re.search(r"anna.{0,2}s archive", site_page_title, re.IGNORECASE):
        print("  [+] The site loaded correctly.\n")
        return True
    print(f"\n  [-] The page that loaded doesn't look like Anna's Archive. It says: '{site_page_text[:200]}' (title: '{site_page_title}')\n")
    print("Is Anna's Archive available in your country?\n")
    print("Did you complete the Captcha within the time limit?")
    print("\nNothing was downloaded. Once the problem is fixed, run the same command again.")
    log_debug_note(DOWNLOAD_DIR, site_url, "SITE CHECK FAILED", f"The page doesn't look like Anna's Archive. Title: '{site_page_title}' | text: '{site_page_text[:300]}'")
    keep_browser_open(page, browser) #the browser is showing that page: leaves it open for a while so it can be looked at
    return False

def check_for_page_error(page, response=None):
    ##checks HTTP status, tab title, and headers for server errors (including cloudflare specific error messages / numbers)
    #note: 403 is omitted because Cloudflare uses it for CAPTCHA challenge pages
    error_codes = [
        404, 429, 500, 502, 503, 504, #403,
        520, 521, 522, 523, 524, 525, 526, 527, 530
    ]
    
    if response and response.status in error_codes:
        if response.status == 429: #"too many requests": the main loop waits longer before the next attempt (RateLimited). if the server says how long to wait (Retry-After header, in seconds) that's used
            retry_after = None
            try:
                retry_after = int(response.headers.get("retry-after", ""))
            except (ValueError, TypeError, AttributeError):
                pass #no Retry-After header, or not in seconds
            raise RateLimited("HTTP 429 returned by server", retry_after)
        raise Exception(f"HTTP {response.status} returned by server")
        
    try:
        title = page.title().strip()
        title_lower = title.lower()

        #pages that contain a <main> element are real pages of the site (book page/list page/slow download page): their page title is a book title + the site name, not a server error, so they are never judged by their title (ignores error-like numbers in book titles)
        #server error pages (nginx, cloudflare...) never have a <main> element
        if page.locator("main").count() > 0:
            return

        #cloudflare writes its error titles as "<host> | 522: Connection timed out" -> this drops the "<host> | " part, so for every kind of error page the error text sits at the very START of title_core
        title_core = re.sub(r'^[^|]*\|\s*', '', title_lower)
        
        #checks for isolated numeric errors using word boundaries (\b) to prevent MD5 hash collisions (ask why this had to be done lmao)
        #(the number must also be at the very start of the title, not just anywhere in it, so a specific "xxx 404 yyy" title doesn't match (anymore). "error " in front is allowed: "Error 522")
        code_match = re.match(r'(?:error\s*)?\b(404|429|500|502|503|504|520|521|522|523|524|525|526|527|530)\b', title_core)
        if code_match:
            if code_match.group(1) == "429": #"too many requests": see the status check above
                raise RateLimited(f"Error page detected via title: '{title}'")
            raise Exception(f"Error page detected via title: '{title}'")
            
        #standard (+ cloudflare specific) text-based error keywords (ADD MORE IF FOUND)
        text_errors = [
            "service unavailable", "temporarily unavailable", 
            "bad gateway", "gateway time-out", "gateway timeout", 
            "not found", "server error", "too many requests", 
            "web server is down", "origin is unreachable", 
            "connection timed out", "host error",
            "service temporarily unavailable", "internal server error"
        ]

        #reports the error to the user in the terminal, then raises exeption (-> go on to next try / file)
        #the title has to START with one of the phrases above (not just contain it somewhere in the middle), so for example the title "Lost and Not Found" doesn't match. the "main" check at the top already keeps real site pages out of this check
        matched_error = next((err for err in text_errors if title_core.startswith(err)), None)
        if matched_error:
            if matched_error == "too many requests": #see the status check above
                raise RateLimited(f"Error page detected via title: '{title}'")
            raise Exception(f"Error page detected via title: '{title}'")
            
    except Exception as e:
        if "Error page detected" in str(e):
            raise e

#how many seconds wait_for_element_or_error waits for an element that is already in the page but still not visible, before using it anyway (with a warning in the terminal and in .hearth_debug.log). see below
VISIBLE_GRACE_SECONDS = 15
AA_BUTTON_WAIT_SECONDS = 180 #how many seconds the script waits for the "Download now" button of a Slow Partner Server to appear. the timer/waitlist countdown is sometimes longer than 90 seconds, which made the attempt fail (a later try usually has a shorter one)
AA_POPUP_WAIT_SECONDS = 90 #after clicking "Download now", the partner server's download opens in a new tab. the script always waits 30 seconds for the download to start, and up to (<-)this many seconds while that tab is still waiting for the server's answer (an overloaded server usually answers "504 Gateway Time-out" after about a minute)

def wait_for_element_or_error(page, selector, timeout_seconds=90, response=None): #function to check constantly if a page errors out
    ##waits for an element while continuously checking for 503/404 errors so it never gets stuck
    start_time = time.time()
    hidden_since = None #time at which the element was first seen in the page but NOT visible (None = not seen as "present but not visible" (yet))
    while True:
        # 1. check if the element has appeared in the DOM (and is visible, not just present in the html)
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                for candidate in loc.all(): #checks every match: the first one could be a hidden duplicate while a later one is visible
                    if candidate.is_visible():
                        return candidate
                #it's in the page, but still hidden: waits for it to show up, for up to VISIBLE_GRACE_SECONDS (counted from the first time it was seen hidden)
                if hidden_since is None:
                    hidden_since = time.time()
                elif time.time() - hidden_since > VISIBLE_GRACE_SECONDS:
                    report_note(page.url, "ELEMENT HIDDEN", f"Warning: '{selector}' is in the page but still not visible after {VISIBLE_GRACE_SECONDS}s, using it anyway", f"Selector: {selector}")
                    return loc.first #same behaviour the script had before the visibility check existed
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
                if hidden_since is not None: #the element was in the page but never became visible before the timeout: same fallback as above (uses it anyway, with a warning)
                    still_there = page.locator(selector)
                    if still_there.count() > 0:
                        report_note(page.url, "ELEMENT HIDDEN", f"Warning: '{selector}' is in the page but was never visible before the timeout, using it anyway", f"Selector: {selector}")
                        return still_there.first
                #adds the title the page has at the moment of the timeout to the message: if an error page slips past check_for_page_error, its real title now shows up in the logs (first 120 characters in the terminal and in failed_downloads.json, everything in .hearth_debug.log)
                try:
                    live_title = page.title().strip()
                except Exception:
                    live_title = "unavailable" #page closed or in the middle of a navigation
                raise PlaywrightTimeoutError(f"Timed out after {timeout_seconds}s (page title: '{live_title}') waiting for '{selector}' (it never appeared in the page: stalled/blank page)")

        page.wait_for_timeout(1000) #pause 1 second before checking again

def main():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

    completed_by_key = load_completed() #loads the content of completed.txt in a set (array) which is in ram (called "completed_by_key"). it's a dictionary: {md5 code: the url the book was completed with}
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
    print(f"Selected download directory: {DOWNLOAD_DIR}")
    print(f"Selected filename format: {NAME_FORMAT}")
    print(f"Selected retries amount: {MAX_ATTEMPTS}\n")
    print("Booting up the automation browser...\n")
    
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
                print(f"Cannot find {TXT_FILE}. Please create it and paste your links (one per line, lines starting with # are ignored).")
                return
            
            #utf-8-sig instead of utf-8: files saved by old versions of notepad in some cases can start with an invisible BOM character, which using normal utf-8 would stick to the front of the first link and break it. utf-8-sig removes it when present and behaves exactly like utf-8 when not
            with open(TXT_FILE, "r", encoding="utf-8-sig") as f: #opens file in read mode
                unique_links = [line.strip() for line in f if line.strip()] #sets link target, strips links from the file
            #throws away comment lines and everything that isn't a real Anna's Archive /md5/ link, and tells the user which lines were skipped
            unique_links, skipped_lines = validate_txt_links(unique_links)
            if skipped_lines:
                print(f"  [!] Skipped {len(skipped_lines)} line(s) in {TXT_FILE} that are not valid Anna's Archive book links:")
                for bad_line in skipped_lines[:10]: #shows at most 10 so a messy file doesn't flood the terminal
                    print(f"      {bad_line[:100]}")
                if len(skipped_lines) > 10:
                    print(f"      ... and {len(skipped_lines) - 10} more")
                print()
            if not unique_links:
                print("No valid links found in the text file. Each line must look like: https://annas-archive.XX/md5/<md5_code>")
                return
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
            list_error = None #the error that stopped the List from loading (None = it loaded)
            list_attempt = 0
            while True: #the List is tried again ONLY if the error can go away by itself (see is_transient_load_error)
                list_attempt += 1
                try:
                    response = page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
            
                    # --- CAPTCHA WAITING LOGIC ---
                    try:
                        wait_for_element_or_error(page, "main", timeout_seconds=120, response=response) #waits 2 mins for core html to appear to give time to solve the captcha, catching errors
                        page.wait_for_timeout(2000)
                    except PlaywrightTimeoutError:
                        pass
                    # -----------------------------
            
                    hrefs = page.eval_on_selector_all("main a[href*='/md5/']", "elements => elements.map(e => e.href)") #grabs all md5 links FROM MAIN (not from the other parts of the site, e.g. the recently downloaded carousel)
                    list_error = None
                    break
                except Exception as e:
                    list_error = e
                    if browser_was_closed(page, browser): #the browser was closed: there's nothing else to do
                        print("\n  [-] The browser was closed, stopping.\n")
                        return
                    if not internet_is_up(LIST_URL): #no internet: waits for it to come back (no time limit), and it doesn't use up a try
                        wait_for_internet(LIST_URL)
                        list_attempt -= 1
                        continue
                    if list_attempt < LIST_LOAD_ATTEMPTS and is_transient_load_error(e): #a timeout, a server error or "too many requests" can go away by itself: one more try
                        wait_seconds = rate_limit_seconds(e, 1) if isinstance(e, RateLimited) else 5
                        listRetry_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                        print(f"  [-] [{listRetry_time}] Could not load the List ({truncate_error(str(e), 80)}). Trying again in {wait_seconds} seconds (try {list_attempt + 1}/{LIST_LOAD_ATTEMPTS})...")
                        time.sleep(wait_seconds)
                        continue
                    break
            
            if list_error is not None: #the List could not be loaded: tells the real reason + hint, and ends
                listFailed_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                print(f"\n  [-] [{listFailed_time}] Could not load your List: {truncate_error(str(list_error), 200)}\n")
                list_explanation = explain_list_failure(list_error, page)
                for explanation_line in list_explanation:
                    print(explanation_line)
                print("\nNothing was downloaded. Once the problem is fixed, run the same command again.")
                log_debug_note(DOWNLOAD_DIR, LIST_URL, "LIST LOAD FAILED", f"{list_error}\n" + "\n".join(list_explanation))
                if error_has_page_to_see(list_error): #the browser is showing the warning / the page: leaves it open for a while (BROWSER_INSPECT_SECONDS) (after the message, which is already printed)
                    keep_browser_open(page, browser)
                return

            unique_links = []
            seen_keys = set() #md5 codes already collected
            for href in hrefs:
                if get_key(href) not in seen_keys: #removes duplicates (compared by md5 code, so the same book with a different link counts too)
                    seen_keys.add(get_key(href))
                    unique_links.append(href)
            targets = [{"url": link, "title": "Unknown"} for link in unique_links] #put all extracted links into a "targets" array. title is set to unknown (one of the reasons is in case the download fails, to write the failure in the json) -> data uniformity
            print(f"Found {len(targets)} total items in list.\n")
            if len(targets) == 0:
                print("Your link is probably mistyped, or your List is not accessible.")
                try:
                    list_page_has_main = page.locator("main").count() > 0 #real Anna's Archive pages have a <main>. block notices and unsolved captchas don't
                except Exception:
                    list_page_has_main = False
                if not list_page_has_main: #the page that loaded is not Anna's Archive (or not the real page yet): shows what it says
                    list_page_url, list_page_title, list_page_text = read_page_snapshot(page)
                    print(f"The page that loaded says: '{list_page_text[:200]}' (title: '{list_page_title}')")
                    print("Is Anna's Archive available in your country?\n")
                    print("Did you complete the Captcha within the time limit?")
                    log_debug_note(DOWNLOAD_DIR, LIST_URL, "LIST LOAD FAILED", f"No items found, and the page doesn't look like Anna's Archive. Title: '{list_page_title}' | text: '{list_page_text[:300]}'")
                    keep_browser_open(page, browser) #the browser is showing that page (zero items on a page without <main>): leaves it open for a while so it can be looked at
                else:
                    log_debug_note(DOWNLOAD_DIR, LIST_URL, "LIST LOAD FAILED", "No items found in the List (the page loaded fine: mistyped link or empty/private List)")
                return

        #site check (text and retry modes): in list mode loading the List page already proves that the site loads, but with the links of a file nothing has opened the site yet. so, before the downloads start, the homepage of the mirror of the first link that will be processed (not an already completed one) is opened. if it doesn't load, the run ends here with the explanation. a problem in the middle of the run is NOT checked on purpose: it's just failures of those books
        if TXT_MODE or RETRY_MODE:
            first_to_process = next((t for t in targets if get_key(t["url"]) not in completed_by_key), None)
            if first_to_process: #if every link is already completed, nothing needs the site
                first_link = urlparse(first_to_process["url"])
                if not check_site_available(page, browser, f"{first_link.scheme}://{first_link.netloc}/"):
                    return

        # 2. Download Execution
        last_started = 0 #number of the item the loop is on (for the summary if the run stops early)
        current_url = "" #link of that item
        ended_because = None #why the run ended early (None = it reached the end of the queue)
        crash_error = None #the unexpected error, if the run crashed
        try: #everything that ends the run (Ctrl+C, closed browser, crash) is handled after the loop, so the summary is ALWAYS printed
            for idx, item in enumerate(targets, 1):
                url = item["url"]
                last_started = idx
                current_url = url
                key = get_key(url) #md5 code of the book (or the whole url if it has none): it's what identifies a book, whatever mirror the link comes from
            
                if key in completed_by_key: #checks if the url is in the completed.txt file, in which case it skips it (the check is made on the md5 code: the same book with the link of another mirror counts as completed too)
                    linkAlreadyCompleted_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                    completed_with = completed_by_key[key] #the link the book was completed with
                    if completed_with == url:
                        print(f"[{idx}/{len(targets)}] [{linkAlreadyCompleted_time}] Skipping already completed item: {url}")
                    else: #same book, different link (mirror, query string...): tells the user (terminal + .hearth_debug.log)
                        print(f"[{idx}/{len(targets)}] [{linkAlreadyCompleted_time}] Skipping duplicate item: {url}")
                        print(f"  [!] Same book (md5 {key}) was already downloaded via {completed_with}")
                        log_debug_note(DOWNLOAD_DIR, url, "DUPLICATE SKIPPED", f"Same book (md5 {key}) was already downloaded via {completed_with}")
                    remove_from_failed_items(failed_items, url, already_completed=True) #if the book is in the failed list (a leftover of an older version, another mirror, a hand edit...) that entry is removed: the book is already completed
                    session_skipped += 1
                    continue

                processingNewLink_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                print(f"[{idx}/{len(targets)}] [{processingNewLink_time}] Processing: {url}")
                failure_reason = None
                page_title = "Unknown"
                item_downloaded = False
                rate_limit_hits = 0 #"too many requests" IN A ROW for this book (see rate_limit_seconds)
                last_bad_md5 = None #the wrong md5 code of the last downloaded file of this book, if there was one (see verify_downloaded_md5)
                permanent_hits = 0 #permanent errors IN A ROW for this book (see raise_stalled_download_error)

                #clean retry loop: any error or 503 restarts the entire flow from the main book page (this was done because restarting downloads directly is impossible)
                max_attempts = MAX_ATTEMPTS
                for attempt in range(1, max_attempts + 1):
                    try:
                        close_extra_pages(context, page) #closes the tabs left by a previous attempt (a failed download leaves its tab open). no download is ever in progress at this point
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
                                download_btn = wait_for_element_or_error(page, "a:has-text('Download now')", timeout_seconds=AA_BUTTON_WAIT_SECONDS, response=response) #waits indefinitely for a captcha resolution AND/OR for the timer, catching errors
                            except PlaywrightTimeoutError as timeout_error: #the original message is kept too: it has the page title at the moment of the timeout
                                failure_reason = f"AA Download button never appeared (Timeout) or the Captcha wasn't solved: {timeout_error}" #saves the failure reason as "download button never appeared"
                                raise Exception(failure_reason)

                            download_attempt_time_slowMirror = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                            print(f"  [*] [{download_attempt_time_slowMirror}] Attempting download from AA...")
                        
                            #set up background listener instead of blocking expect_download to actually notice downloads starting and not going ahead if the page errors out but still starts the download (IMPORTANT!)
                            dl_box = []
                            def handle_aa_download(d):
                                dl_box.append(d)
                            
                            page.on("download", handle_aa_download)
                            popups = [] #the tabs the click opens (the "Download now" link opens the partner server's download in a NEW tab)
                            def handle_aa_popup(new_tab):
                                popups.append(new_tab)
                            page.on("popup", handle_aa_popup)
                            clicked_from_url = page.url #the page where the button is clicked: raise_stalled_download_error needs it to know if the browser left it
                            download_btn.click(force=True)
                        
                            download = None
                            for waited in range(AA_POPUP_WAIT_SECONDS): #30 second maximum wait for download to start (up to AA_POPUP_WAIT_SECONDS while the tab opened by the click is still waiting for the server's answer, see below)
                                if dl_box:
                                    download = dl_box[0]
                                    break
                                check_for_page_error(page) #instantly errors if a 503 error page loads
                                #the tab opened by the click (partner server): an error page there (504 gateway time-out, "too many downloads"...) ends the attempt right away. but if a download has started, it wins: that download must be able to finish (nothing is closed while it is in progress)
                                try:
                                    for popup in popups:
                                        check_popup_for_error(popup)
                                except Exception:
                                    page.wait_for_timeout(500) #a download may be starting at this very moment -> keep the tab open for 500ms for a last attempt
                                    if dl_box:
                                        continue #(the check at the top of the loop takes the download)
                                    page.remove_listener("download", handle_aa_download)
                                    page.remove_listener("popup", handle_aa_popup)
                                    close_extra_pages(context, page) #frees the partner server's connection (that tab would stay open and pending)
                                    raise
                                #the first 30 seconds are always given. after that, it keeps waiting only while a tab opened by the click is still waiting for the server's answer (up to AA_POPUP_WAIT_SECONDS)
                                if waited >= 29 and not any(popup_is_loading(popup) for popup in popups):
                                    break
                                page.wait_for_timeout(1000)
                            
                            page.remove_listener("download", handle_aa_download) #clean up listener (properly using Python syntax)
                            page.remove_listener("popup", handle_aa_popup)
                        
                            if not download:
                                if any(popup_is_loading(popup) for popup in popups): #says what the tab opened by the click was doing (it helps to understand the log)
                                    tab_info = f"the partner server's tab was still loading after {waited + 1} seconds"
                                elif popups:
                                    tab_info = "the partner server's tab opened but gave no download"
                                else:
                                    tab_info = "the click opened no new tab"
                                close_extra_pages(context, page) #no download: closes the tab(s), so they don't stay open and pending on the server
                                raise_stalled_download_error(page, clicked_from_url, f"AA Download stream timeout / failed to start ({tab_info})") #raises the normal error, or PermanentFailure if the page shows a known permanent error
                        
                            original_name = download.suggested_filename #gets the AA suggested name
                        
                            #generates the file name based on the terminal argument
                            base_file_name = generate_custom_filename(original_name, url, NAME_FORMAT)
                        
                            #ensures the filename is unique to prevent overwriting different "editions" with the same title
                            file_name = get_unique_filename(DOWNLOAD_DIR, base_file_name, url)
                            file_path = os.path.join(DOWNLOAD_DIR, file_name)
                        
                            current_time_start = datetime.now().strftime("%H:%M:%S") #added download timestamp logic for AA slow mirror
                            dl_stopwatch_start = time.time() #starts the stopwatch
                            print(f"  [*] [{current_time_start}] Saving {file_name}...")
                            download.save_as(file_path)
                            dl_stopwatch_end = time.time() #ends the stopwatch
                            verify_downloaded_md5(file_path, url, last_bad_md5) #checks that the saved file is the right one (md5 code of the link). if not, the file is deleted and the attempt fails (normal retries)
                            current_time_end = datetime.now().strftime("%H:%M:%S")
                            print(f"  [+] [{current_time_end}] Success! Saved: {file_name}")
                            print(f"  [+] Time elapsed during download: {format_elapsed_time(dl_stopwatch_end - dl_stopwatch_start)}\n")
                            
                            discard_temp_download(download) #removes playwright's own temporary copy of the download (the real file is already saved)
                            close_extra_pages(context, page) #the download is finished: closes the tab it came from (if it's still open)

                            #generates the clean title from the actual downloaded file for the log
                            clean_title = clean_downloaded_title(original_name, url)
                            mark_completed(url, clean_title) #writes to the completed.txt file (append mode)
                            completed_by_key[key] = url #updates set (array) in ram -> doesn't repeat the same link attempt (it's a dictionary: {md5 code: url}, unless a link has no md5 (never encountered), in which case it's {url: url})
                        
                            remove_from_failed_items(failed_items, url) #removes file/url from the failed items json in case it was there (also the entries of the same book with another link)
                        
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
                            except PlaywrightTimeoutError as timeout_error: #the original message is kept too: it has the page title at the moment of the timeout
                                failure_reason = f"Libgen GET button never appeared (Timeout/503): {timeout_error}" #no captcha as i don't think there are any on libgen
                                raise Exception(failure_reason)

                            download_attempt_time_libgen = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                            print(f"  [*] [{download_attempt_time_libgen}] Attempting download from Libgen.li...")
                        
                            #set up background listener instead of blocking expect_download to actually notice downloads starting and not going ahead if the page arrors out but still starts the download (happens a lot on libgen)
                            dl_box = []
                            def handle_libgen_download(d):
                                dl_box.append(d)
                            
                            page.on("download", handle_libgen_download)
                            clicked_from_url = page.url #the page where the button is clicked: raise_stalled_download_error needs it to know if the browser left it
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
                                raise_stalled_download_error(page, clicked_from_url, "Libgen Download stream timeout / failed to start") #raises the normal error, or PermanentFailure if the page shows a known permanent error
                        
                            original_name = download.suggested_filename #gets the AA suggested name
                        
                            #generate the file name based on the terminal argument
                            base_file_name = generate_custom_filename(original_name, url, NAME_FORMAT)
                        
                            #ensures the filename is unique to prevent overwriting different editions
                            file_name = get_unique_filename(DOWNLOAD_DIR, base_file_name, url)
                            file_path = os.path.join(DOWNLOAD_DIR, file_name)
                        
                            current_time_start = datetime.now().strftime("%H:%M:%S") #timestamp logic for Libgen mirror
                            dl_stopwatch_start = time.time() #starts the stopwatch
                            print(f"  [*] [{current_time_start}] Saving {file_name}...")
                            download.save_as(file_path)
                            dl_stopwatch_end = time.time() #ends the stopwatch
                            verify_downloaded_md5(file_path, url, last_bad_md5) #checks that the saved file is the right one (md5 code of the link). if not, the file is deleted and the attempt fails (normal retries)
                            current_time_end = datetime.now().strftime("%H:%M:%S")
                            print(f"  [+] [{current_time_end}] Success! Saved from Libgen: {file_name}")
                            print(f"  [+] Time elapsed during download: {format_elapsed_time(dl_stopwatch_end - dl_stopwatch_start)}\n")
                            
                            discard_temp_download(download) #removes playwright's own temporary copy of the download (the real file is already saved above)
                        
                            #generate the clean title from the actual downloaded file for the log
                            clean_title = clean_downloaded_title(original_name, url)
                            mark_completed(url, clean_title)
                            completed_by_key[key] = url #(dictionary: {md5 code: url}, unless a link has no md5 (never encountered), in which case it's {url: url})
                        
                            remove_from_failed_items(failed_items, url) #removes url/file from the failed items json in case it was there (also the entries of the same book with another link)
                            
                            session_success += 1
                            item_downloaded = True
                            break

                    except Exception as e: #if link dead, timeout or error -> script doesn't crash
                        if browser_was_closed(page, browser): #the browser (or its window) was closed: ends the whole run (this book is NOT marked as failed) (now it doesn't fail every book that's left)
                            raise StopRun("the browser was closed")
                    
                        full_error = str(e)
                        short_error = truncate_error(full_error)
                        failure_reason = f"Script exception: {short_error}"
                    
                        #remembers the wrong md5 code of a downloaded file: if the next download gives the SAME one, that file is kept (see verify_downloaded_md5)
                        if isinstance(e, Md5Mismatch):
                            last_bad_md5 = e.actual_md5
                    
                        #counts the permanent errors in a row: any other kind of error puts the count back to 0
                        if isinstance(e, PermanentFailure):
                            permanent_hits += 1
                        else:
                            permanent_hits = 0
                    
                        #writes the full error and context to the debug log
                        log_debug_error(DOWNLOAD_DIR, url, full_error, attempt, max_attempts, idx, len(targets), page_title)
                    
                        if permanent_hits >= PERMANENT_ERROR_CONFIRMATIONS: #the same permanent error N times in a row: retrying this book now would be a waste of time. it stays in the failed list, so retry mode can try it again later
                            failure_reason = f"Stopped retrying after {permanent_hits} identical permanent errors in a row (try again later with retry mode or launch the script with the same command). The page said: '{e.page_text[:200]}'"
                            report_note(url, "PERMANENT ERROR", f"The same permanent error showed up {permanent_hits}/{PERMANENT_ERROR_CONFIRMATIONS} times in a row, not retrying this book anymore (attempt {attempt}/{max_attempts}).", f"The page said: {e.page_text}")
                            break
                    
                        #"too many requests": waits longer, and longer each time in a row, instead of trying again after 3 seconds. any other error puts the count back to 0
                        if isinstance(e, RateLimited):
                            rate_limit_hits += 1
                            pause_seconds = rate_limit_seconds(e, rate_limit_hits)
                        else:
                            rate_limit_hits = 0
                            pause_seconds = 3
                    
                        if attempt < max_attempts: #retry logic
                            encounteredError_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                            permanent_note = f"  [!] [{encounteredError_time}] A likely permanent error was detected! See below. ({permanent_hits}/{PERMANENT_ERROR_CONFIRMATIONS})\n" if permanent_hits else "" #only gets written when the error looks permanent
                            retry_prefix = f"  [-] [{encounteredError_time}] Encountered error (" #start of the line (before the error)
                            retry_suffix = f"). Reloading from main book page (Attempt {attempt + 1}/{max_attempts})..." #end of the line (after the error)
                            terminal_error = truncate_error(full_error, terminal_error_width(retry_prefix, retry_suffix)) #the error is cut to what fits in one line of the current terminal (short_error, always 120 characters, is the one for the json)
                            print(f"{permanent_note}{retry_prefix}{terminal_error}{retry_suffix}") #important part is the {terminal_error}; the rest is suffix/prefix. {permanent_note} only gets printed when there's a permanent error.
                            if isinstance(e, RateLimited): #tells the user why it takes longer than usual
                                print(f"  [!] [{encounteredError_time}] Too many requests (429): waiting {pause_seconds} seconds before the next attempt...")
                            pause_before_retry(url, pause_seconds) #waits (3 seconds normally). if the internet is down it waits for it to come back instead
                            continue
                        else:
                            if not internet_is_up(url): #last attempt: if the cause is that there's no internet, it waits for it now, so the next book doesn't fail the same way
                                wait_for_internet(url)
                            break

                if item_downloaded:
                    time.sleep(3) #3 seconds of wait before the next link
                    continue

                #log failures <- if nothing else happened and all retries failed
                failedLink_time = datetime.now().strftime("%H:%M:%S") #timestamp for message below
                print(f"  [-] [{failedLink_time}] Failed: {failure_reason}\n")
                session_failed += 1
                existing = next((f for f in failed_items if f["url"] == url), None) #reads the failed_items set (which are in ram, after the script opened the json earlier) that's full of structs, finds if the current failed md5 is already present (because two mirrors of the same md5, both failed, get two entries to maximize chances of downloading)
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

        except KeyboardInterrupt: #Ctrl+C: the summary below is still printed
            ended_because = "interrupted by user with Ctrl+C"
        except StopRun as stop: #the run was ended on purpose (e.g. the browser was closed)
            ended_because = str(stop)
        except Exception as crash: #an unexpected error outside of the retry handler (a bug, a full disk...): the summary is printed first, then the error is raised again so it's not hidden
            ended_because = f"crashed with {type(crash).__name__}: {truncate_error(str(crash), 100)}"
            crash_error = crash
            log_debug_note(DOWNLOAD_DIR, current_url, "CRASH", traceback.format_exc())
        
        if ended_because: #the loop didn't reach the end: the book it was on is not counted as processed (it will be tried again next time)
            session_processed = max(last_started - 1, 0)
            log_debug_note(DOWNLOAD_DIR, current_url, "RUN STOPPED", f"The run stopped: {ended_because} (during item {last_started}/{len(targets)})")
        else:
            session_processed = len(targets)
        
        #calculates final time
        script_end_time = time.time()
        end_time_str = datetime.now().strftime("%H:%M")
        
        #final verdicts
        print("\n")
        if ended_because:
            print(f"Stopped at {end_time_str} ({ended_because}) during item {last_started}/{len(targets)}!")
        else:
            print(f"Finished processing queue at {end_time_str}!")
        print(f"Total time lapsed: {format_elapsed_time(script_end_time - script_start_time)}.")
        print(f"- Total Processed: {session_processed}" + (f" of {len(targets)}" if ended_because else ""))
        print(f"- Successfully Downloaded: {session_success}")
        print(f"- Failed: {session_failed}")
        if session_skipped > 0:
            print(f"- Skipped (Already Completed): {session_skipped}")
        if ended_because:
            print(f"- Not processed: {len(targets) - session_processed} (run the same command again: the already completed books are skipped)")
        print("\n")
        print(f"You can find the list of downloaded links in the completed.txt file, in your download directory ({DOWNLOAD_DIR}).")
        if session_failed > 0:
            print(f"You can find the list of failed links in the failed_downloads.json file, in your download directory ({DOWNLOAD_DIR}).")
            print("To fix the failed downloads, you can run this script again in 'retry mode' which will try to download all your failed links again.")
        print("\n")
        
        if crash_error: #raised again AFTER the summary, so the normal error message (with the traceback) is still shown
            raise crash_error
        if ended_because is None: #only after a normal finish. after Ctrl+C or a closed browser, playwright is shut down when leaving the "with" block
            browser.close() #shuts down playwright

if __name__ == "__main__": #standard python, tells to execute main when launched from the terminal
    try:
        main()
    except KeyboardInterrupt: #Ctrl+C outside of the download loop (while loading the List, at the start...). during the downloads the loop has its own handler, which prints the summary
        print("\n\nStopped by user (Ctrl+C).\n")
