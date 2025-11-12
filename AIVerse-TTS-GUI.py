import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import threading
import queue
import os
import json
import requests
import time
import shutil
from datetime import datetime, timedelta, timezone

# === Version ===
VERSION = "5.2"

# === Persistence Paths ===
script_dir = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(script_dir, "key_state.json")
KEY_FILE = os.path.join(script_dir, "keys.json")

# Default ElevenLabs voice ID (Glinda my beloved)
DEFAULT_VOICE_ID = "z9fAnlkpzviPz146aGWa"

# Default API keys file initializer
DEFAULT_API_KEYS = [
    'KEY1-PLEASE-CHANGE',
    'ADDITIONAL KEYS AFTER COMMA'
]

# Config & voices cache files
CONFIG_FILE = os.path.join(script_dir, "config.json")
VOICE_CACHE_FILE = os.path.join(script_dir, "voices_cache.json")

# default config (used to bootstrap config.json)
DEFAULT_CONFIG = {
    "char_limit": 7500,
    "voice_settings": {
        "similarity_boost": 0.6,
        "stability": 0.4,
        "use_speaker_boost": True
    },
    "update_interval_days": 14
}

CHAR_LIMIT = DEFAULT_CONFIG["char_limit"]
VOICE_SETTINGS = dict(DEFAULT_CONFIG["voice_settings"])
UPDATE_INTERVAL_DAYS = DEFAULT_CONFIG["update_interval_days"]

# single lock to guard shared runtime state and related file writes
state_lock = threading.RLock()

# In-memory state
API_KEYS = []  # list of keys
key_usage = {}  # count of chunks used per key (optional)
char_usage = {}  # total characters sent per key
first_used = {}  # ISO date when key first used
invalid_keys = set()  # keys exceeding quota
current_key_index = 0


# === Load/Save Config ===
def load_config():
    """Load config.json or create it with defaults. Updates global vars."""
    global CHAR_LIMIT, VOICE_SETTINGS, UPDATE_INTERVAL_DAYS
    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
        except Exception:
            pass
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        cfg = DEFAULT_CONFIG.copy()

    # apply values with safe fallbacks
    try:
        CHAR_LIMIT = int(cfg.get("char_limit", DEFAULT_CONFIG["char_limit"]))
    except Exception:
        CHAR_LIMIT = DEFAULT_CONFIG["char_limit"]

    vs_cfg = cfg.get("voice_settings", DEFAULT_CONFIG["voice_settings"])
    if isinstance(vs_cfg, dict):
        VOICE_SETTINGS.clear()
        # only copy expected keys with safe casts
        for k in ("similarity_boost", "stability", "use_speaker_boost"):
            if k in vs_cfg:
                VOICE_SETTINGS[k] = vs_cfg[k]
    else:
        VOICE_SETTINGS.update(DEFAULT_CONFIG["voice_settings"])

    try:
        UPDATE_INTERVAL_DAYS = int(cfg.get("update_interval_days", DEFAULT_CONFIG["update_interval_days"]))
    except Exception:
        UPDATE_INTERVAL_DAYS = DEFAULT_CONFIG["update_interval_days"]

    return cfg


def atomic_write_json(path, obj):
    """Write JSON atomically: write to tmp file then replace."""
    tmp = path + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        # best-effort cleanup
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


def save_config(cfg=None):
    """Persist current config (cfg dict optional). Thread-safe and atomic."""
    if cfg is None:
        cfg = {
            "char_limit": CHAR_LIMIT,
            "voice_settings": VOICE_SETTINGS,
            "update_interval_days": UPDATE_INTERVAL_DAYS
        }
    try:
        with state_lock:
            atomic_write_json(CONFIG_FILE, cfg)
    except Exception as e:
        print("Could not save config:", e)


# === Voice Cache Helpers ===
def load_voice_cache():
    """Return cached data dict or None if missing/corrupt."""
    if not os.path.exists(VOICE_CACHE_FILE):
        return None
    try:
        with open(VOICE_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def save_voice_cache(voices):
    """Save list of voices with fetched_at timestamp (ISO). Thread-safe + atomic."""
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "voices": voices
    }
    try:
        with state_lock:
            atomic_write_json(VOICE_CACHE_FILE, payload)
    except Exception as e:
        print("Could not write voice cache:", e)


def fetch_voices_from_api(timeout=30):
    """Try to fetch voices from ElevenLabs using available API keys.
       Returns list of voice dicts or None on failure."""
    with state_lock:
        if not API_KEYS:
            return None
        keys_snapshot = [k for k in API_KEYS if k not in invalid_keys]

    for k in keys_snapshot:
        try:
            resp = requests.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": k},
                timeout=timeout
            )
            if resp.status_code == 200:
                return resp.json().get("voices", [])
            elif resp.status_code in (401, 403):
                with state_lock:
                    invalid_keys.add(k)
        except requests.RequestException:
            continue
    return None


def get_voices(use_cache=True, force_refresh=False):
    """Return list of voices. Use cache if fresh (age < UPDATE_INTERVAL_DAYS) unless forced."""
    # load cache
    cache = load_voice_cache()
    if use_cache and cache and not force_refresh:
        try:
            fetched_at = datetime.fromisoformat(cache.get("fetched_at"))
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)

            if (datetime.now(timezone.utc) - fetched_at) < timedelta(days=UPDATE_INTERVAL_DAYS):
                return cache.get("voices", [])
        except Exception:
            # if timestamp parse fails, ignore cache
            pass

    # cache is stale or force refresh → try to fetch
    voices = fetch_voices_from_api()
    if voices:
        save_voice_cache(voices)
        return voices

    # fallback to cache even if stale
    if cache:
        return cache.get("voices", [])

    # ultimate fallback: empty list
    return []


# === Persistence Helpers ===
def load_keys():
    """Load or initialize API_KEYS from external file."""
    global API_KEYS, key_usage, char_usage, first_used, invalid_keys
    with state_lock:
        # Ensure key file exists
        if not os.path.exists(KEY_FILE):
            atomic_write_json(KEY_FILE, DEFAULT_API_KEYS)
        # Load keys
        try:
            with open(KEY_FILE, 'r', encoding='utf-8') as f:
                API_KEYS = json.load(f)
        except Exception:
            API_KEYS = DEFAULT_API_KEYS.copy()
        # Initialize usage dicts for new keys
        for k in API_KEYS:
            key_usage.setdefault(k, 0)
            char_usage.setdefault(k, 0)


def save_keys():
    """Persist the list of API_KEYS to external file."""
    with state_lock:
        try:
            atomic_write_json(KEY_FILE, API_KEYS)
        except Exception as e:
            print("Could not save keys:", e)


def load_state():
    """Load key_usage, char_usage, first_used, invalid_keys; reset quotas >31 days old."""
    global key_usage, char_usage, first_used, invalid_keys
    with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                key_usage.update(state.get('key_usage', {}))
                char_usage.update(state.get('char_usage', {}))
                for k, ts in state.get('first_used', {}).items():
                    try:
                        dt = datetime.fromisoformat(ts)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        first_used[k] = dt
                    except Exception:
                        pass
                invalid_keys = set(state.get('invalid_keys', []))
            except Exception:
                print("Warning: could not load key state; starting fresh.")
                key_usage.clear()
                char_usage.clear()
                first_used.clear()
                invalid_keys.clear()
        else:
            key_usage.clear()
            char_usage.clear()
            first_used.clear()
            invalid_keys.clear()

    # Reset quotas for keys whose first use > 31 days
    reset_expired_keys()


def _save_state_locked():
    """Write state to disk. Assumes state_lock is already held."""
    payload = {
        'key_usage': key_usage,
        'char_usage': char_usage,
        'first_used': {k: dt.isoformat() for k, dt in first_used.items()},
        'invalid_keys': list(invalid_keys)
    }
    atomic_write_json(STATE_FILE, payload)


def save_state():
    """Thread-safe save_state() that acquires the lock."""
    with state_lock:
        try:
            _save_state_locked()
        except Exception as e:
            print("Error saving state:", e)


def backup_state_file():
    """Make a timestamped backup of the existing state file (best-effort)."""
    try:
        if os.path.exists(STATE_FILE):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            bak = STATE_FILE + f".bak.{stamp}"
            shutil.copy2(STATE_FILE, bak)
    except Exception:
        pass


def reset_expired_keys():
    """
    Fully reset per-key usage for keys whose first_used is older than UPDATE_INTERVAL_DAYS.
    - Sets char_usage[key] = 0 and key_usage[key] = 0
    - Removes first_used entry (so next use resets window)
    - Removes key from invalid_keys
    Runs under state_lock.
    """
    now = datetime.now(timezone.utc)
    cutoff = timedelta(days=UPDATE_INTERVAL_DAYS)
    changed = False

    with state_lock:
        # Make a safe list because we'll pop from first_used
        for k, dt in list(first_used.items()):
            try:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now - dt) >= cutoff:
                    # backup on first change (optional)
                    if not changed:
                        backup_state_file()
                    key_usage[k] = 0
                    char_usage[k] = 0
                    first_used.pop(k, None)
                    invalid_keys.discard(k)
                    changed = True
            except:
                pass

        # Also, if some keys exceed CHAR_LIMIT (perhaps imported), mark them invalid
        for k, used in list(char_usage.items()):
            if used >= CHAR_LIMIT:
                invalid_keys.add(k)

        if changed:
            # save state atomically while still under lock
            try:
                _save_state_locked()
            except Exception as e:
                print("Failed to persist state after reset:", e)


# === Utility Helpers ===
def get_unique_filepath(desired_path):
    base, ext = os.path.splitext(desired_path)
    counter = 1
    unique = desired_path
    while os.path.exists(unique):
        unique = f"{base}_{counter}{ext}"
        counter += 1
    return unique


def get_next_valid_api_key():
    """
    Cycle through API_KEYS, skipping those invalid.
    Raises RuntimeError if none remain.
    """
    global current_key_index
    n = len(API_KEYS)

    # small critical section to choose a key and advance index
    with state_lock:
        for _ in range(n):
            key = API_KEYS[current_key_index]
            # Check if a key is valid and under CHAR_LIMIT
            current_key_index = (current_key_index + 1) % n
            if key in invalid_keys:
                continue
            used = char_usage.get(key, 0)
            if used < CHAR_LIMIT:
                return key
            else:
                invalid_keys.add(key)
    raise RuntimeError("No valid API keys available.")


def chunk_text(text, chunk_size=2500):
    """Split text into word-safe chunks of ~chunk_size chars."""
    words = text.split()
    chunks, curr, length = [], [], 0
    for w in words:
        if length + len(w) + 1 <= chunk_size:
            curr.append(w)
            length += len(w) + 1
        else:
            chunks.append(" ".join(curr))
            curr, length = [w], len(w)
    if curr: chunks.append(" ".join(curr))
    return chunks


# === ElevenLabs API ===
def send_to_elevenlabs_api(chunk, api_key, output_path, voice_id=DEFAULT_VOICE_ID, timeout=30):
    """
    Send chunk, save MP3; returns True on success. Marks key invalid on 401/403.
    """
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    try:
        resp = requests.post(
            url,
            json={"text": chunk, "voice_settings": VOICE_SETTINGS},
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            timeout=timeout
        )
    except requests.RequestException as e:
        print("Network error:", e)
        return False

    if resp.status_code == 200:
        safe = get_unique_filepath(output_path)
        try:
            with open(safe, 'wb') as f:
                f.write(resp.content)
        except Exception as e:
            print("Disk write error:", e)
            return False
        print(f"Audio saved to: {safe}\n")
        return True
    else:
        print(f"Error {resp.status_code}: {resp.text}\n")
        if resp.status_code in (401, 403):
            with state_lock:
                invalid_keys.add(api_key)
                key_usage[api_key] = 4
                char_usage[api_key] = 4
        return False


def process_text(text, output_folder, base_filename, voice_id,
                 progress_callback=None, cancel_check=None):
    """
    Main orchestration: chunk text, rotate keys, track usage and dates, invalidate as needed.
    """
    load_keys()
    load_state()
    chunks = chunk_text(text)

    if progress_callback:
        progress_callback(0, len(chunks))

    for i, chunk in enumerate(chunks, start=1):
        if cancel_check and cancel_check():
            print("Processing cancelled.\n")
            break

        try:
            key = get_next_valid_api_key()
        except RuntimeError:
            print("No valid API key left. Stopping.\n")
            break

        # On first use and counters / file writing must be done under lock (but not the network call)
        # Build filename / path
        n_chars = len(chunk)
        filename = f"{base_filename}_{i}.mp3"
        path = os.path.join(output_folder, filename)

        # send without holding lock
        success = send_to_elevenlabs_api(chunk, key, path, voice_id)
        if not success:
            # skip usage tracking on failure (send_to_elevenlabs_api may have invalidated key)
            continue

        # Update in-memory state and persist under lock
        with state_lock:
            if key not in first_used:
                first_used[key] = datetime.now(timezone.utc)
            key_usage[key] = key_usage.get(key, 0) + 1
            char_usage[key] = char_usage.get(key, 0) + n_chars
            if char_usage[key] >= CHAR_LIMIT:
                invalid_keys.add(key)
            # persist updated state atomically
            try:
                _save_state_locked()
            except Exception as e:
                print("Failed to save state:", e)

        if progress_callback:
            progress_callback(i, len(chunks))

        time.sleep(3)

    # ensure final state persisted
    save_state()


# === GUI ===
class App:
    def __init__(self, root):
        self.root = root
        root.title(f"AIVerse TTS GUI - v{VERSION}")
        self.queue = queue.Queue()
        self.cancel_requested = False

        # Input file
        tk.Label(root, text="Input .txt file:").grid(row=0, column=0, sticky="w")
        self.input_file = tk.Entry(root, width=40)
        self.input_file.grid(row=0, column=1, sticky="we")
        tk.Button(root, text="Browse...", command=self.browse_input).grid(row=0, column=2, sticky="e")

        # Output folder
        tk.Label(root, text="Output folder:").grid(row=1, column=0, sticky="w")
        self.output_folder = tk.Entry(root, width=40)
        self.output_folder.grid(row=1, column=1, sticky="we")
        tk.Button(root, text="Browse...", command=self.browse_output).grid(row=1, column=2, sticky="e")

        # Base filename
        tk.Label(root, text="Base filename:").grid(row=2, column=0, sticky="w")
        self.base_name = tk.Entry(root, width=40)
        self.base_name.grid(row=2, column=1, sticky="we")
        tk.Label(root, text="").grid(row=2, column=2, sticky="e")

        # Manage keys button
        tk.Button(root, text="Manage Keys", command=self.manage_keys).grid(row=3, column=2, sticky="e")

        # Voice selection dropdown
        # build initial voice_map with Glinda only
        self.voice_map = {"Glinda": DEFAULT_VOICE_ID}
        self.voice_var = tk.StringVar(root)
        self.voice_var.set("Glinda")
        self.voice_menu = tk.OptionMenu(root, self.voice_var, *self.voice_map.keys())
        self.voice_menu.grid(row=3, column=1, sticky="we")

        # spawn background thread to refresh voices from cache/API
        threading.Thread(target=self._async_load_voices, daemon=True).start()

        # Text area
        tk.Label(root, text="Input text:").grid(row=4, column=0, sticky="nw")
        self.text_input = scrolledtext.ScrolledText(root, width=60, height=10)
        self.text_input.grid(row=4, column=1, columnspan=2, sticky="we")
        self.text_input.bind("<KeyRelease>", self.update_count)

        # Character count
        self.count_label = tk.Label(root, text="Character count: 0")
        self.count_label.grid(row=5, column=1, sticky="w")

        # Frame for Start and Cancel buttons
        self.button_frame = tk.Frame(root)
        self.button_frame.grid(row=5, column=2, sticky="e", pady=(2, 0))

        # Cancel button
        self.cancel_btn = tk.Button(self.button_frame, text="Cancel", command=self.cancel)
        self.cancel_btn.pack(side="left", padx=(0, 5))
        self.cancel_btn.config(state='disabled')

        # Start button
        self.start_btn = tk.Button(self.button_frame, text="Start", command=self.start)
        self.start_btn.pack(side="left")

        # Progress bar
        self.progress = ttk.Progressbar(root, orient='horizontal', mode='determinate', length=300)
        self.progress.grid(row=6, column=1, columnspan=2, sticky="we", padx=5, pady=2)

        # Logs
        tk.Label(root, text="Logs:").grid(row=7, column=0, sticky="nw")
        self.log_widget = scrolledtext.ScrolledText(root, width=60, height=10, state='disabled')
        self.log_widget.grid(row=7, column=1, columnspan=2, sticky="nesw")

        # Poll for logs
        self.root.after(100, self.poll_queue)

        # Allow resizing
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(7, weight=1)

    def browse_input(self):
        file = filedialog.askopenfilename(filetypes=[("Text Files", "*.txt")])
        if file:
            self.input_file.delete(0, tk.END)
            self.input_file.insert(0, file)
            try:
                txt = open(file, 'r', encoding='utf-8').read()
                self.text_input.delete('1.0', tk.END)
                self.text_input.insert(tk.END, txt)
                self.update_count()
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def browse_output(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_folder.delete(0, tk.END)
            self.output_folder.insert(0, folder)

    def update_count(self, event=None):
        txt = self.text_input.get('1.0', 'end-1c')
        self.count_label.config(text=f"Character count: {len(txt)}")

    def log(self, msg):
        self.log_widget.config(state='normal')
        self.log_widget.insert(tk.END, msg)
        self.log_widget.see(tk.END)
        self.log_widget.config(state='disabled')

    def poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                self.log(msg)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def start(self):
        self.start_btn.config(state='disabled')
        self.log_widget.config(state='normal')
        self.log_widget.delete('1.0', tk.END)
        self.log_widget.config(state='disabled')
        self.cancel_btn.config(state='normal')

        file_path = self.input_file.get().strip()
        if os.path.isfile(file_path):
            text = open(file_path, 'r', encoding='utf-8').read()
        else:
            text = self.text_input.get('1.0', 'end-1c')
        out_folder = self.output_folder.get().strip()

        if not out_folder:
            # no folder entered → use (and create) a local "outputs" directory
            out_folder = os.path.join(script_dir, "outputs")
        base = self.base_name.get().strip()
        if not base:
            base = "untitled"
        os.makedirs(out_folder, exist_ok=True)
        import sys
        class QRedirect:
            def write(slf, txt): self.queue.put(txt)

            def flush(slf): pass

        sys.stdout = sys.stderr = QRedirect()
        vid = self.voice_map.get(self.voice_var.get(), DEFAULT_VOICE_ID)

        self.cancel_requested = False
        self.progress["value"] = 0
        self.progress["maximum"] = 1

        threading.Thread(target=self.run, args=(text, out_folder, base, vid), daemon=True).start()

    def run(self, text, out_folder, base, voice_id):
        try:
            process_text(
                text,
                out_folder,
                base,
                voice_id,
                progress_callback=self.update_progress,
                cancel_check=lambda: self.cancel_requested
            )
            messagebox.showinfo("Done", "All files processed successfully!")
        except Exception as e:
            messagebox.showerror("Error", str(e))
        finally:
            self.reset()

    def reset(self):
        self.cancel_requested = False
        self.input_file.delete(0, tk.END)
        self.base_name.delete(0, tk.END)
        self.text_input.delete('1.0', tk.END)
        self.update_count()
        self.output_folder.delete(0, tk.END)
        self.start_btn.config(state='normal')
        self.cancel_btn.config(state='disabled')
        self.progress["value"] = 0

    def cancel(self):
        self.cancel_requested = True
        self.cancel_btn.config(state='disabled')
        self.log("Cancellation requested...\n")

    def update_progress(self, value, total):
        self.progress["maximum"] = total
        self.progress["value"] = value

    def manage_keys(self):
        def save_and_close():
            new_keys = [k.strip() for k in text_area.get('1.0', 'end-1c').splitlines() if k.strip()]
            if not new_keys:
                messagebox.showwarning("Warning", "Key list cannot be empty.")
                return

            global API_KEYS, key_usage, char_usage, first_used, invalid_keys, current_key_index
            API_KEYS = new_keys
            # reinitialize usage dicts
            old_usage = key_usage.copy()
            old_chars = char_usage.copy()
            old_first = first_used.copy()
            key_usage.clear()
            char_usage.clear()
            first_used.clear()
            for k in API_KEYS:
                key_usage[k] = old_usage.get(k, 0)
                char_usage[k] = old_chars.get(k, 0)
                if k in old_first:
                    first_used[k] = old_first[k]
            invalid_keys &= set(API_KEYS)
            current_key_index = 0

            # write keys file atomically
            tmp = KEY_FILE + ".tmp"
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(API_KEYS, f, indent=2)
                os.replace(tmp, KEY_FILE)
            except Exception as e:
                print("Could not save keys:", e)

        save_keys()

        win = tk.Toplevel(self.root)
        win.title("Manage API Keys")
        tk.Label(win, text="Enter one API key per line:").pack(anchor='w')
        text_area = scrolledtext.ScrolledText(win, width=50, height=15)
        text_area.pack()
        for k in API_KEYS:
            text_area.insert(tk.END, k + "\n")
        btn_frame = tk.Frame(win)
        btn_frame.pack(pady=5)
        tk.Button(btn_frame, text="Save", command=save_and_close).pack(side='left', padx=5)
        tk.Button(btn_frame, text="Cancel", command=win.destroy).pack(side='left')

    def _async_load_voices(self):
        voices = get_voices(use_cache=True, force_refresh=False)
        # voices is a list of dicts; map to (name, voice_id)
        mapping = {}
        for v in voices:
            try:
                mapping[v["name"]] = v.get("voice_id") or v.get("id")  # defensive keys
            except Exception:
                continue
        # ensure Glinda
        mapping.setdefault("Glinda", DEFAULT_VOICE_ID)
        # schedule GUI update on main thread
        self.root.after(0, lambda: self._update_voice_menu(mapping))

    def _update_voice_menu(self, mapping):
        """Replace OptionMenu entries with mapping (name->voice_id)."""
        self.voice_map = mapping
        menu = self.voice_menu["menu"]
        menu.delete(0, "end")
        # keep previous selection if possible
        current = self.voice_var.get()
        for name in sorted(mapping.keys(), key=lambda n: (n != "Glinda", n)):  # put Glinda first
            menu.add_command(label=name, command=lambda v=name: self.voice_var.set(v))
        # restore selection or set to Glinda
        if current in mapping:
            self.voice_var.set(current)
        else:
            self.voice_var.set("Glinda")


if __name__ == "__main__":
    load_config()
    load_keys()
    load_state()
    root = tk.Tk()
    app = App(root)
    root.mainloop()
