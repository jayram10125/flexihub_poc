"""Portway desktop UI and Windows USB/IP session orchestration."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
import platform
import queue
import secrets
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
import urllib.error
import urllib.parse
import urllib.request
import uuid
import winreg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from connector.connector import inventory, request
from connector.agent import Agent
from connector.native_usb import runtime_status, storage_inventory

APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Portway"
CONFIG_FILE = APP_DIR / "connector.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CONNECTOR_VERSION = "0.4.0"


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


if platform.system() == "Windows":
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p


def protect(text):
    data = text.encode("utf-8")
    source_buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    result = DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
        raise ctypes.WinError()
    try:
        return base64.b64encode(ctypes.string_at(result.pbData, result.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def unprotect(encoded):
    data = base64.b64decode(encoded)
    source_buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    result = DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.pbData, result.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def valid_server(value):
    url = value.strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return url
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"):
        return url
    raise ValueError("Use an HTTPS server URL. HTTP works only with localhost.")


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(config):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temp.replace(CONFIG_FILE)


def api_error_message(error):
    """Read the API's structured error instead of displaying only HTTP 409."""
    if error.code == 404 and '/api/agent/' in error.url:
        return 'Update the Portway server to version 0.4.0 before using this connector.'
    try:
        payload = json.load(error)
        detail = payload.get("detail", {})
        if isinstance(detail, dict):
            return detail.get("message") or detail.get("code") or "Server rejected the request."
        return str(detail)
    except Exception:
        return "Server returned HTTP {}.".format(error.code)


def startup_command():
    if getattr(sys, "frozen", False):
        return '"{}"'.format(sys.executable)
    pythonw = str(Path(sys.executable).with_name("pythonw.exe"))
    return '"{}" "{}"'.format(pythonw, Path(__file__).resolve())


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "PortwayConnector")
            return value == startup_command()
    except OSError:
        return False


def set_startup(enabled):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, "PortwayConnector", 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, "PortwayConnector")
            except FileNotFoundError:
                pass


class ConnectorUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Portway Connector " + CONNECTOR_VERSION)
        self.root.geometry("600x720")
        self.root.minsize(540, 620)
        self.root.configure(bg="#f6f9f8")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.generation = 0
        self.worker = None
        self.agent = None
        self.remote_devices = {}
        self.config = load_config()
        self.config.setdefault("machine_id", str(uuid.uuid4()))
        if not self.config.get('machine_key'):
            self.config['machine_key'] = protect(secrets.token_urlsafe(32))
        self.machine_key = unprotect(self.config['machine_key'])
        save_config(self.config)
        self.token = None
        self.server = None
        self.build()
        self.root.after(150, self.drain)
        if self.config.get("token") and self.config.get("server"):
            try:
                self.token = unprotect(self.config["token"])
                self.server = valid_server(self.config["server"])
                self.start_worker()
            except Exception:
                self.token = None
                self.set_status("Sign in to connect", "#8a9aa4")

    def build(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TEntry", padding=8)
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 9), background="#ffffff", fieldbackground="#ffffff")
        container = tk.Frame(self.root, bg="#f6f9f8", padx=28, pady=22)
        container.pack(fill="both", expand=True)
        tk.Label(container, text="◈  portway.", font=("Segoe UI", 21, "bold"), bg="#f6f9f8", fg="#173d37").pack(anchor="w")
        tk.Label(container, text="WINDOWS CONNECTOR  ·  DEVICE DISCOVERY PROTOTYPE", font=("Segoe UI", 8, "bold"), bg="#f6f9f8", fg="#6b9e83").pack(anchor="w", pady=(1, 19))

        self.login_frame = tk.Frame(container, bg="white", padx=18, pady=17, highlightbackground="#e1e9e6", highlightthickness=1)
        self.login_frame.pack(fill="x")
        self.server_var = tk.StringVar(value=self.config.get("server", "https://flexihub-poc.onrender.com"))
        self.email_var = tk.StringVar(value=self.config.get("email", ""))
        self.password_var = tk.StringVar()
        for label, variable, hidden in (("Server URL", self.server_var, False), ("Email", self.email_var, False), ("Password", self.password_var, True)):
            tk.Label(self.login_frame, text=label, font=("Segoe UI", 9, "bold"), bg="white", fg="#52656b").pack(anchor="w")
            entry = ttk.Entry(self.login_frame, textvariable=variable, show="•" if hidden else "")
            entry.pack(fill="x", pady=(4, 11))
            if hidden:
                entry.bind("<Return>", lambda _event: self.sign_in())
        self.login_button = tk.Button(self.login_frame, text="Sign in & start connector", command=self.sign_in, bg="#173e39", fg="white", relief="flat", padx=14, pady=9, font=("Segoe UI", 9, "bold"), activebackground="#2b6558", activeforeground="white")
        self.login_button.pack(fill="x", pady=(2, 0))

        status_frame = tk.Frame(container, bg="#f6f9f8")
        status_frame.pack(fill="x", pady=(18, 8))
        self.status_label = tk.Label(status_frame, text="●  Sign in to connect", font=("Segoe UI", 10, "bold"), bg="#f6f9f8", fg="#8a9aa4")
        self.status_label.pack(side="left")
        self.signout_button = tk.Button(status_frame, text="Sign out", command=self.sign_out, bg="#f6f9f8", fg="#397e65", relief="flat", font=("Segoe UI", 9, "bold"))
        self.signout_button.pack(side="right")
        self.signout_button.pack_forget()
        self.summary = tk.Label(container, text="Your local USB and Bluetooth peripherals will appear here after sign in.", font=("Segoe UI", 9), bg="#f6f9f8", fg="#7d9096")
        self.summary.pack(anchor="w", pady=(0, 7))
        self.listbox = ttk.Treeview(container, columns=("device", "type"), show="headings", height=6)
        self.listbox.heading("device", text="Local peripheral")
        self.listbox.heading("type", text="Type")
        self.listbox.column("device", width=320)
        self.listbox.column("type", width=120)
        self.listbox.pack(fill="both", expand=True)
        tk.Label(container, text="Remote devices by computer", font=("Segoe UI", 10, "bold"), bg="#f6f9f8", fg="#38515a").pack(anchor="w", pady=(15, 6))
        # Computer rows are group headers. Only their child device rows can connect.
        self.remote_list = ttk.Treeview(container, columns=("type", "status"), show="tree headings", height=6)
        self.remote_list.heading("#0", text="Computer / device")
        self.remote_list.heading("type", text="Type")
        self.remote_list.heading("status", text="Status")
        self.remote_list.column("#0", width=310)
        self.remote_list.column("type", width=140)
        self.remote_list.column("status", width=90)
        self.remote_list.pack(fill="both", expand=True)
        tk.Button(container, text="Connect / Disconnect selected device", command=self.connect_selected, bg="#e2efe9", fg="#276e52", relief="flat", padx=13, pady=7, font=("Segoe UI", 9, "bold")).pack(anchor="e", pady=(8, 0))
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        tk.Checkbutton(container, text="Open connector when I sign in to Windows", variable=self.startup_var, command=self.toggle_startup, bg="#f6f9f8", fg="#52656b", activebackground="#f6f9f8", font=("Segoe UI", 9)).pack(anchor="w", pady=(12, 0))
        tk.Label(container, text="Only USB storage is marked shareable. Other device classes are shown for visibility.", font=("Segoe UI", 8), bg="#f6f9f8", fg="#a0763f").pack(anchor="w", pady=(13, 0))

    def toggle_startup(self):
        try:
            set_startup(self.startup_var.get())
        except OSError as error:
            self.startup_var.set(not self.startup_var.get())
            messagebox.showerror("Windows startup", str(error), parent=self.root)

    def set_status(self, message, color):
        self.status_label.configure(text="●  " + message, fg=color)

    def sign_in(self):
        try:
            server = valid_server(self.server_var.get())
            email = self.email_var.get().strip()
            password = self.password_var.get()
            if not email or not password:
                raise ValueError("Enter email and password.")
        except ValueError as error:
            messagebox.showerror("Sign in", str(error), parent=self.root)
            return
        self.login_button.configure(state="disabled")
        self.set_status("Signing in…", "#b18a51")

        def login_task():
            try:
                result = request(server, "/api/auth/login", {"email": email, "password": password})
                self.events.put(("login", server, email, result["token"]))
            except urllib.error.HTTPError as error:
                self.events.put(("error", "Sign in failed: {}".format(api_error_message(error))))
            except Exception as error:
                self.events.put(("error", "Could not reach server: {}".format(error)))
        threading.Thread(target=login_task, daemon=True).start()

    def start_worker(self):
        self.login_frame.pack_forget()
        self.signout_button.pack(side="right")
        self.stop = threading.Event()
        self.generation += 1
        self.set_status("Connecting…", "#b18a51")
        self.worker = threading.Thread(target=self.heartbeat_loop, args=(self.stop, self.generation), daemon=True)
        self.worker.start()

    def heartbeat_loop(self, stop, generation):
        machine = {"id": self.config["machine_id"], "name": platform.node() or "Windows PC", "os": platform.platform()[:100]}
        runtime = runtime_status()
        agent = Agent(APP_DIR, self.server, self.token, machine['id'], self.machine_key)
        self.agent = agent
        inventory_at = 0
        while not stop.is_set():
            try:
                if time.monotonic() - inventory_at > 15:
                    devices = inventory()
                    if runtime['host_ready']:
                        storage = storage_inventory()
                        storage_ids = {d['id'] for d in storage}
                        devices = [d for d in devices if d['id'] not in storage_ids]
                        devices.extend(storage)
                    inventory_at = time.monotonic()
                request(self.server, "/api/machines/heartbeat", {"machine": machine, "devices": devices,
                        "machine_key": self.machine_key, "host_ready": runtime['host_ready'],
                        "guest_ready": runtime['guest_ready']}, self.token)
                sessions = agent.sync()
                req = urllib.request.Request(self.server + "/api/overview", headers={"Authorization": "Bearer " + self.token})
                with urllib.request.urlopen(req, timeout=15) as response:
                    overview = json.load(response)
                self.events.put(("online", generation, devices, overview, runtime, sessions))
            except urllib.error.HTTPError as error:
                if error.code == 401:
                    self.events.put(("expired", generation))
                    return
                self.events.put(("offline", generation, api_error_message(error)))
            except Exception as error:
                self.events.put(("offline", generation, str(error)))
            stop.wait(2)
        agent.close()

    def drain(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] in ("online", "offline", "expired") and event[1] != self.generation:
                    continue
                if event[0] == "login":
                    _, self.server, email, self.token = event
                    self.config.update({"server": self.server, "email": email, "token": protect(self.token)})
                    save_config(self.config)
                    self.password_var.set("")
                    self.login_button.configure(state="normal")
                    self.start_worker()
                elif event[0] == "error":
                    self.login_button.configure(state="normal")
                    self.set_status("Sign in failed", "#bb5b55")
                    messagebox.showerror("Portway Connector", event[1], parent=self.root)
                elif event[0] == "online":
                    devices, overview = event[2], event[3]
                    self.set_status("Online · {} peripheral(s) found".format(len(devices)), "#3b986f")
                    runtime, sessions = event[4], event[5]
                    self.summary.configure(text=runtime['error'] if runtime['error'] else
                        ('USB sharing ready. ' + (sessions[0]['state'] if sessions else 'Select a remote pen drive.')))
                    for item in self.listbox.get_children():
                        self.listbox.delete(item)
                    for device in devices:
                        self.listbox.insert("", "end", values=(device["name"], device["kind"].replace("-", " ").title()))
                    selected = self.remote_list.selection()
                    collapsed = {i for i in self.remote_list.get_children() if not self.remote_list.item(i, 'open')}
                    self.remote_devices = {d['id']: d for d in overview['devices']}
                    for item in self.remote_list.get_children():
                        self.remote_list.delete(item)
                    machines = {m["id"]: m for m in overview["machines"]}
                    remote_by_machine = {}
                    for device in overview["devices"]:
                        # Website demo inventory is not a real remote computer.
                        if device["machine_id"] != self.config["machine_id"] and device["kind"] != "demo":
                            remote_by_machine.setdefault(device["machine_id"], []).append(device)
                    for machine_id, remote_devices in remote_by_machine.items():
                        machine = machines.get(machine_id, {})
                        computer_name = machine.get("name", "Unknown computer")
                        online = machine.get("online", False)
                        group_id = "computer:" + machine_id
                        self.remote_list.insert("", "end", iid=group_id, text=computer_name,
                                                values=("{} device(s)".format(len(remote_devices)), "Online" if online else "Offline"),
                                                open=group_id not in collapsed, tags=("computer",))
                        for device in remote_devices:
                            self.remote_list.insert(group_id, "end", iid=device["id"], text=device["name"],
                                                    values=(device["kind"].replace("-", " ").title(), device["status"].title()),
                                                    tags=("device",))
                    for item in selected:
                        if self.remote_list.exists(item):
                            self.remote_list.selection_set(item)
                elif event[0] == "offline":
                    self.set_status("Offline · retrying", "#b17d48")
                    self.summary.configure(text=event[2][:90])
                elif event[0] == "expired":
                    self.sign_out()
                    messagebox.showinfo("Session expired", "Please sign in again.", parent=self.root)
                elif event[0] == "connect_result":
                    messagebox.showinfo("Remote connection", event[1], parent=self.root)
        except queue.Empty:
            pass
        self.root.after(150, self.drain)

    def sign_out(self):
        self.stop.set()
        self.generation += 1
        self.token = None
        self.config.pop("token", None)
        save_config(self.config)
        self.signout_button.pack_forget()
        self.login_frame.pack(fill="x", before=self.status_label.master)
        self.set_status("Sign in to connect", "#8a9aa4")
        self.summary.configure(text="Your local USB and Bluetooth peripherals will appear here after sign in.")
        for item in self.listbox.get_children():
            self.listbox.delete(item)
        for item in self.remote_list.get_children():
            self.remote_list.delete(item)

    def connect_selected(self):
        selection = self.remote_list.selection()
        if not selection:
            messagebox.showinfo("Select a device", "Choose a remote device first.", parent=self.root)
            return
        if not self.token:
            messagebox.showinfo("Sign in", "Sign in before connecting.", parent=self.root)
            return
        device_id = selection[0]
        if device_id.startswith("computer:"):
            messagebox.showinfo("Select a device", "Expand the computer and choose one of its devices.", parent=self.root)
            return
        device = self.remote_devices.get(device_id, {})
        if not device.get('session_id') and not device.get('usbip_busid'):
            messagebox.showinfo('Device availability', 'This device is listed for discovery. Only a mapped USB pen drive can currently be connected.', parent=self.root)
            return

        def task():
            try:
                if device.get('session_id'):
                    request(self.server, '/api/sessions/' + device['session_id'] + '/disconnect', {}, self.token)
                    self.events.put(('connect_result', 'Safe disconnect requested. Close files on the remote drive to allow eject.'))
                else:
                    result = request(self.server, "/api/sessions", {"device_id": device_id, "requester_machine_id": self.config["machine_id"]}, self.token)
                    self.events.put(("connect_result", "Preparing USB connection. Windows attachment is not yet confirmed. Session: " + result['id']))
            except urllib.error.HTTPError as error:
                detail = api_error_message(error)
                self.events.put(("connect_result", detail))
            except Exception as error:
                self.events.put(("connect_result", str(error)))
        threading.Thread(target=task, daemon=True).start()

    def close(self):
        self.stop.set()
        self.root.destroy()


if __name__ == "__main__":
    if platform.system() != "Windows":
        raise SystemExit("Windows only")
    # Installer observes the same mutex and refuses updates while USB sessions
    # may be active. Running two desktop agents for one identity is prohibited.
    ctypes.windll.kernel32.CreateMutexW.restype = wintypes.HANDLE
    connector_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\PortwayConnector')
    if ctypes.windll.kernel32.GetLastError() == 183:
        raise SystemExit('Portway Connector is already running.')
    app = ConnectorUI()
    app.root.mainloop()
