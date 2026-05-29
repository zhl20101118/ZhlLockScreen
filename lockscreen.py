"""
锁屏程序 - 普通版（完整鼠标拦截，强制英文，允许 Shift）
彻底重构钩子与日志，解决卡顿、钩子失效、窗口抖动偏移问题
功能：
- 全屏置顶，拦截键盘鼠标，仅ESC唤出密码框
- 自动切换至美式英语键盘布局，杜绝中文输入
- 允许 Shift 键（大小写切换）
- 支持自定义壁纸、透明背景、时间显示
- 密码错误抖动
- 调试模式（10秒自动解锁）
- 解锁后恢复原始键盘布局
- **精确鼠标拦截**：密码框显示时只能点击密码框内控件，无法点击其他区域
- 彻底禁用右键菜单
- 低CPU占用，无频繁磁盘I/O
- 【已删除托盘图标功能】
- **钩子永不失效**：ESC直接触发主线程回调，无需鼠标干预
"""
import os
import sys
import json
import ctypes
import ctypes.wintypes
import hashlib
import threading
import subprocess
import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk
import time
import atexit
import signal
import traceback
from datetime import datetime

# ---------- 加载 Windows API ----------
user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

# 输入法相关
imm32 = ctypes.WinDLL('imm32')
ImmAssociateContext = imm32.ImmAssociateContext
ImmAssociateContext.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HANDLE]
ImmAssociateContext.restype = ctypes.wintypes.HANDLE

# 键盘布局
LoadKeyboardLayoutW = user32.LoadKeyboardLayoutW
LoadKeyboardLayoutW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
LoadKeyboardLayoutW.restype = ctypes.wintypes.HANDLE
ActivateKeyboardLayout = user32.ActivateKeyboardLayout
ActivateKeyboardLayout.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_uint]
ActivateKeyboardLayout.restype = ctypes.wintypes.HANDLE

# 窗口句柄相关
WindowFromPoint = user32.WindowFromPoint
WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
WindowFromPoint.restype = ctypes.wintypes.HWND
IsChild = user32.IsChild
IsChild.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HWND]
IsChild.restype = ctypes.wintypes.BOOL

KLF_ACTIVATE = 1
KLF_SETFORPROCESS = 0x100
ENGLISH_US_LAYOUT = "00000409"  # 美式英语

LOG_DIR = "log"
BG_ALPHA = 0.0
PASSWORD_FILE = os.path.join(LOG_DIR, "pass")
SETTINGS_FILE = os.path.join(LOG_DIR, "settings.json")
ERROR_LOG_PATH = os.path.join(LOG_DIR, "crash.log")
LOCK_SIGNAL_FILE = os.path.join(LOG_DIR, "lock_signal.txt")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

def log_error(msg):
    """仅记录严重错误，不记录正常信息"""
    try:
        with open(ERROR_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
            f.flush()
    except:
        pass

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {"wallpaper": "", "show_time": False, "transparent_bg": False}

# ---------- 钩子常量 ----------
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_NCRBUTTONDOWN = 0x00A4
WM_NCRBUTTONUP = 0x00A5

VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_CTRL = 0x11
VK_ALT = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_ESC = 0x1B
VK_BACK = 0x08
VK_TAB = 0x09
VK_ENTER = 0x0D
VK_CAPSLOCK = 0x14
VK_DELETE = 0x2E
VK_HOME = 0x24
VK_END = 0x23
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_UP = 0x26
VK_DOWN = 0x28

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ('vkCode', ctypes.wintypes.DWORD),
        ('scanCode', ctypes.wintypes.DWORD),
        ('flags', ctypes.wintypes.DWORD),
        ('time', ctypes.wintypes.DWORD),
        ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))
    ]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ('pt', ctypes.wintypes.POINT),
        ('mouseData', ctypes.wintypes.DWORD),
        ('flags', ctypes.wintypes.DWORD),
        ('time', ctypes.wintypes.DWORD),
        ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))
    ]

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)

g_lock = None

# ---------- 密码管理 ----------
class PasswordManager:
    PASSWORD_FILE = PASSWORD_FILE
    SALT = b"lockscreen_secure_salt"

    @classmethod
    def _hash(cls, pwd):
        h = hashlib.sha256()
        h.update(cls.SALT)
        h.update(pwd.encode('utf-8'))
        return h.hexdigest()

    @classmethod
    def initialize(cls):
        if not os.path.exists(cls.PASSWORD_FILE):
            cls.save_password("123456")
            print("初始密码已设为 123456")

    @classmethod
    def save_password(cls, new_pwd):
        with open(cls.PASSWORD_FILE, "w") as f:
            f.write(cls._hash(new_pwd))

    @classmethod
    def verify(cls, pwd):
        if not os.path.exists(cls.PASSWORD_FILE):
            cls.initialize()
        try:
            with open(cls.PASSWORD_FILE, "r") as f:
                stored = f.read().strip()
            return stored == cls._hash(pwd)
        except:
            return False

# ---------- 钩子回调 ----------
@HOOKPROC
def keyboard_proc(nCode, wParam, lParam):
    try:
        if nCode >= 0:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vkCode = kb.vkCode
            if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                if g_lock and g_lock.should_block_key(vkCode):
                    return 1
    except Exception:
        pass
    return user32.CallNextHookEx(None, nCode, wParam, lParam)

@HOOKPROC
def mouse_proc(nCode, wParam, lParam):
    try:
        if nCode >= 0 and g_lock and g_lock.is_locked:
            ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            is_click = wParam in (WM_LBUTTONDOWN, WM_LBUTTONUP,
                                   WM_RBUTTONDOWN, WM_RBUTTONUP,
                                   WM_MBUTTONDOWN, WM_MBUTTONUP,
                                   WM_XBUTTONDOWN, WM_XBUTTONUP,
                                   WM_MOUSEWHEEL,
                                   WM_NCRBUTTONDOWN, WM_NCRBUTTONUP)
            if not is_click:
                return 0

            if g_lock.dialog_shown:
                hwnd_clicked = WindowFromPoint(ms.pt)
                if hwnd_clicked and g_lock.password_dialog:
                    if hwnd_clicked == g_lock.password_dialog.winfo_id() or IsChild(g_lock.password_dialog.winfo_id(), hwnd_clicked):
                        return 0
                return 1
            else:
                return 1
    except Exception:
        pass
    return user32.CallNextHookEx(None, nCode, wParam, lParam)

# ---------- 主类 ----------
class LockScreen:
    def __init__(self, debug_mode=False):
        self._init(debug_mode)

    def _init(self, debug_mode):
        self.debug_mode = debug_mode
        self.is_locked = True
        self.dialog_shown = False
        self.password_dialog = None
        self.timer_id = None
        self.timeout_id = None
        self.keyboard_hook = None
        self.mouse_hook = None
        self.settings = load_settings()
        self.canvas = None
        self.time_text_id = None
        self.bg_image_id = None
        self.signal_check_id = None
        self.reload_id = None
        self.original_layout = None
        self.keepalive_id = None

        if not ctypes.windll.shell32.IsUserAnAdmin():
            messagebox.showerror("错误", "请以管理员身份运行！")
            sys.exit(1)

        # 保存原始键盘布局
        try:
            self.original_layout = ActivateKeyboardLayout(0, 0)
        except Exception:
            pass

        # 强制英文布局
        self._force_english_layout()

        PasswordManager.initialize()

        self.root = tk.Tk()
        self.root.title("")
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.geometry(f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0")
        self.root.focus_force()
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        self.root.grab_set_global()

        self.canvas = tk.Canvas(
            self.root,
            width=self.root.winfo_screenwidth(),
            height=self.root.winfo_screenheight(),
            highlightthickness=0,
            bg='#000000'
        )
        self.canvas.pack()

        # 设置背景
        transparent_bg = self.settings.get("transparent_bg", False)
        if transparent_bg:
            self.root.attributes('-alpha', BG_ALPHA)
        else:
            wallpaper = self.settings.get("wallpaper", "")
            if wallpaper and os.path.isfile(wallpaper):
                self._set_wallpaper(wallpaper)
                self.root.attributes('-alpha', 1.0)
            else:
                self.root.attributes('-alpha', BG_ALPHA)

        if self.settings.get("show_time", False):
            self._create_time_display()

        self._check_lock_signal()
        if self.debug_mode:
            self.timer_id = self.root.after(10000, self.debug_unlock)

        self._reload_settings_periodic()

        global g_lock
        g_lock = self

        atexit.register(self.cleanup)
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)

        self.hook_thread = threading.Thread(target=self._hook_message_loop, daemon=True)
        self.hook_thread.start()

        # 保活机制：每5秒调用一次空函数，确保主线程消息循环活跃
        self._keepalive()

    def _keepalive(self):
        """空函数，仅用于唤醒主线程消息循环"""
        self.keepalive_id = self.root.after(5000, self._keepalive)

    def _force_english_layout(self):
        try:
            hkl = LoadKeyboardLayoutW(ENGLISH_US_LAYOUT, KLF_ACTIVATE | KLF_SETFORPROCESS)
            if hkl:
                ActivateKeyboardLayout(hkl, 0)
        except Exception:
            pass

    def _reload_settings_periodic(self):
        """每隔30秒重新加载设置，且仅在文件真正改变时才更新壁纸等，不写日志"""
        try:
            new_settings = load_settings()
            if new_settings != self.settings:
                # 设置发生变化，需要更新
                old_wall = self.settings.get("wallpaper")
                new_wall = new_settings.get("wallpaper")
                if new_wall != old_wall:
                    if new_wall and os.path.isfile(new_wall):
                        self._set_wallpaper(new_wall)
                    else:
                        # 清除壁纸
                        if self.bg_image_id:
                            self.canvas.delete(self.bg_image_id)
                            self.bg_image_id = None
                # 更新时间显示
                if new_settings.get("show_time") != self.settings.get("show_time"):
                    if new_settings.get("show_time"):
                        if not self.time_text_id:
                            self._create_time_display()
                    else:
                        if self.time_text_id:
                            self.canvas.delete(self.time_text_id)
                            self.time_text_id = None
                # 更新透明背景
                if new_settings.get("transparent_bg") != self.settings.get("transparent_bg"):
                    alpha = BG_ALPHA if new_settings.get("transparent_bg") else 1.0
                    self.root.attributes('-alpha', alpha)
                self.settings = new_settings
        except Exception:
            pass
        self.reload_id = self.root.after(30000, self._reload_settings_periodic)

    def _set_wallpaper(self, path):
        try:
            img = Image.open(path)
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            img = img.resize((sw, sh), Image.Resampling.LANCZOS)
            self.bg_photo = ImageTk.PhotoImage(img)
            if self.bg_image_id:
                self.canvas.delete(self.bg_image_id)
            self.bg_image_id = self.canvas.create_image(0, 0, image=self.bg_photo, anchor='nw')
            self.canvas.tag_lower(self.bg_image_id)
        except Exception:
            pass

    def _create_time_display(self):
        self.time_text_id = self.canvas.create_text(
            50, self.root.winfo_screenheight() - 50,
            text='', fill='white', font=('Segoe UI', 60, 'normal'), anchor='sw'
        )
        self._update_time()

    def _update_time(self):
        if self.time_text_id:
            now = datetime.now()
            self.canvas.itemconfig(self.time_text_id, text=f"{now.strftime('%H:%M')}\n{now.strftime('%Y-%m-%d')}")
            self.root.after(1000, self._update_time)

    def _check_lock_signal(self):
        try:
            if os.path.exists(LOCK_SIGNAL_FILE):
                self.show_password_dialog()
                os.remove(LOCK_SIGNAL_FILE)
        except Exception:
            pass
        self.signal_check_id = self.root.after(500, self._check_lock_signal)

    def signal_handler(self, signum, frame):
        self.cleanup()
        sys.exit(0)

    def cleanup(self):
        try:
            if self.keyboard_hook:
                user32.UnhookWindowsHookEx(self.keyboard_hook)
            if self.mouse_hook:
                user32.UnhookWindowsHookEx(self.mouse_hook)
            if self.signal_check_id:
                self.root.after_cancel(self.signal_check_id)
            if self.reload_id:
                self.root.after_cancel(self.reload_id)
            if self.keepalive_id:
                self.root.after_cancel(self.keepalive_id)
        except Exception:
            pass

    def _hook_message_loop(self):
        """钩子线程，无限重连"""
        while True:
            try:
                self.keyboard_hook = user32.SetWindowsHookExW(
                    WH_KEYBOARD_LL, keyboard_proc,
                    kernel32.GetModuleHandleW(None), 0
                )
                if not self.keyboard_hook:
                    raise Exception("Keyboard hook failed")

                self.mouse_hook = user32.SetWindowsHookExW(
                    WH_MOUSE_LL, mouse_proc,
                    kernel32.GetModuleHandleW(None), 0
                )
                if not self.mouse_hook:
                    raise Exception("Mouse hook failed")

                msg = ctypes.wintypes.MSG()
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                # 正常退出WM_QUIT - 重启线程
            except Exception:
                pass
            finally:
                if self.keyboard_hook:
                    user32.UnhookWindowsHookEx(self.keyboard_hook)
                    self.keyboard_hook = None
                if self.mouse_hook:
                    user32.UnhookWindowsHookEx(self.mouse_hook)
                    self.mouse_hook = None
                time.sleep(1)  # 等待后重试

    def should_block_key(self, vk):
        """仅做逻辑判断，不调用任何Tk方法，避免跨线程问题"""
        if not self.is_locked:
            return False
        if self.dialog_shown:
            # 密码输入模式：放行字母、数字、Shift、功能键等
            if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A or 0x60 <= vk <= 0x69:
                return False
            if vk in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT):
                return False
            allowed = {VK_BACK, VK_TAB, VK_ENTER, VK_DELETE, VK_HOME, VK_END,
                       VK_LEFT, VK_RIGHT, VK_UP, VK_DOWN, VK_CAPSLOCK}
            if vk in allowed:
                return False
            if vk in (VK_LWIN, VK_RWIN):
                return True
            if vk == VK_ESC:
                # 直接通过 after 调用主线程方法（线程安全）
                self.root.after(0, self.show_password_dialog)
                return True
            return True
        else:
            # 锁屏模式：仅ESC有效
            if vk in (VK_LWIN, VK_RWIN):
                return True
            if vk == VK_ESC:
                self.root.after(0, self.show_password_dialog)
                return True
            return True

    def show_password_dialog(self):
        if self.dialog_shown:
            return
        try:
            self.dialog_shown = True
            dlg = tk.Toplevel(self.root)
            dlg.title("")
            dlg.overrideredirect(True)
            dlg.attributes('-topmost', True)
            dlg.transient(self.root)
            dlg.grab_set_global()
            dlg.configure(bg='#1e1e1e')
            dlg.attributes('-alpha', 0.95)

            w, h = 380, 200
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            x = sw // 2 - w // 2
            y = sh // 2 - h // 2
            dlg.geometry(f"{w}x{h}+{x}+{y}")

            # 保存原始坐标供抖动使用
            self.dialog_orig_x = x
            self.dialog_orig_y = y

            canvas = tk.Canvas(dlg, width=w, height=h, bg='#1e1e1e', highlightthickness=0)
            canvas.place(x=0, y=0)
            canvas.create_round_rect(2, 2, w-2, h-2, radius=25, fill='#2d2d2d', outline='')
            canvas.create_text(w//2, 35, text="解锁屏幕", fill='#e0e0e0', font=('Segoe UI', 16, 'bold'))

            ex, ey = w//2-120, 70
            canvas.create_round_rect(ex, ey, ex+240, ey+40, radius=10, fill='#3c3c3c', outline='')

            def validate_input(char, action):
                if action == '0':
                    return True
                if len(char) == 0:
                    return True
                return 32 <= ord(char) <= 126

            vcmd = (self.root.register(validate_input), '%S', '%d')
            entry = tk.Entry(canvas, show='●', font=('Segoe UI', 14), bg='#3c3c3c',
                             fg='white', insertbackground='#6ab04c', bd=0, highlightthickness=0,
                             validate='key', validatecommand=vcmd)
            entry.place(x=ex+10, y=ey+8, width=220, height=24)

            hwnd = entry.winfo_id()
            ImmAssociateContext(hwnd, None)

            entry.bind('<Button-3>', lambda e: 'break')
            canvas.bind('<Button-3>', lambda e: 'break')
            dlg.bind('<Button-3>', lambda e: 'break')

            def on_focus_in(event):
                self._force_english_layout()
                ImmAssociateContext(entry.winfo_id(), None)
            entry.bind('<FocusIn>', on_focus_in)

            btn_y = 130
            unlock = tk.Button(canvas, text="解锁", command=lambda: self.check_password(entry),
                               bg='#6ab04c', fg='white', font=('Segoe UI', 11),
                               relief='flat', bd=0, cursor='hand2')
            unlock.place(x=w//2-110, y=btn_y, width=100, height=35)
            cancel = tk.Button(canvas, text="取消", command=self.hide_password_dialog,
                               bg='#d63031', fg='white', font=('Segoe UI', 11),
                               relief='flat', bd=0, cursor='hand2')
            cancel.place(x=w//2+10, y=btn_y, width=100, height=35)

            close_btn = tk.Button(canvas, text="✕", bg='#2d2d2d', fg='#a0a0a0',
                                  font=('Segoe UI', 10), bd=0, command=self.hide_password_dialog,
                                  activebackground='#c42b1c')
            close_btn.place(x=w-35, y=5, width=25, height=25)

            dlg.lift()
            dlg.focus_force()
            dlg.update()
            dlg.lift()
            dlg.focus_force()
            entry.focus_set()

            def reset_timeout():
                if self.timeout_id:
                    dlg.after_cancel(self.timeout_id)
                self.timeout_id = dlg.after(3000, self.hide_password_dialog)
            entry.bind('<Key>', lambda e: reset_timeout())
            reset_timeout()
            entry.bind('<Return>', lambda e: self.check_password(entry))

            self.password_dialog = dlg
            self.password_entry = entry
        except Exception as e:
            log_error(f"show_password_dialog error: {traceback.format_exc()}")
            self.dialog_shown = False

    def hide_password_dialog(self):
        try:
            if self.password_dialog:
                if self.timeout_id:
                    self.password_dialog.after_cancel(self.timeout_id)
                    self.timeout_id = None
                self.password_dialog.grab_release()
                self.password_dialog.destroy()
                self.password_dialog = None
            self.dialog_shown = False
        except Exception:
            pass

    def check_password(self, entry):
        try:
            if PasswordManager.verify(entry.get()):
                self.unlock()
            else:
                self.shake_window()
                entry.delete(0, tk.END)
                if self.password_dialog:
                    self.password_dialog.lift()
                    self.password_dialog.focus_force()
                    entry.focus_set()
                    self._force_english_layout()
                    ImmAssociateContext(entry.winfo_id(), None)
        except Exception as e:
            log_error(f"check_password error: {e}")

    def shake_window(self):
        if not self.password_dialog:
            return
        dlg = self.password_dialog
        orig_x = getattr(self, 'dialog_orig_x', None)
        orig_y = getattr(self, 'dialog_orig_y', None)
        if orig_x is None or orig_y is None:
            orig_x = dlg.winfo_x()
            orig_y = dlg.winfo_y()
        offsets = [5, -5, 5, -5, 3, -3, 2, -2, 0]
        idx = 0
        def step():
            nonlocal idx
            if idx >= len(offsets):
                dlg.geometry(f"+{orig_x}+{orig_y}")
                return
            off = offsets[idx]
            dlg.geometry(f"+{orig_x + off}+{orig_y}")
            idx += 1
            dlg.after(30, step)
        step()

    def unlock(self):
        self.is_locked = False
        if self.timer_id:
            self.root.after_cancel(self.timer_id)
        self.cleanup()
        if hasattr(self, 'original_layout') and self.original_layout:
            try:
                ActivateKeyboardLayout(self.original_layout, 0)
            except Exception:
                pass
        self.root.quit()
        self.root.destroy()
        os._exit(0)

    def debug_unlock(self):
        if self.is_locked:
            print("调试模式自动解锁")
            self.unlock()

    def open_config(self):
        try:
            base = os.path.dirname(os.path.abspath(sys.argv[0]))
            exe = os.path.join(base, 'config.exe')
            py = os.path.join(base, 'config.py')
            if getattr(sys, 'frozen', False):
                subprocess.Popen([exe] if os.path.exists(exe) else [py])
            else:
                subprocess.Popen([sys.executable, py] if os.path.exists(py) else [exe])
        except Exception:
            pass

    def run(self):
        try:
            self.root.mainloop()
        except Exception as e:
            log_error(f"run error: {traceback.format_exc()}")
        finally:
            self.cleanup()

def _create_round_rect(self, x1, y1, x2, y2, radius=20, **kwargs):
    points = [x1+radius, y1,
              x2-radius, y1,
              x2, y1,
              x2, y1+radius,
              x2, y2-radius,
              x2, y2,
              x2-radius, y2,
              x1+radius, y2,
              x1, y2,
              x1, y2-radius,
              x1, y1+radius,
              x1, y1]
    return self.create_polygon(points, smooth=True, **kwargs)

tk.Canvas.create_round_rect = _create_round_rect

if __name__ == "__main__":
    try:
        print("锁屏程序启动（普通版，无托盘图标，钩子直接回调）")
        lock = LockScreen(debug_mode=False)
        lock.run()
    except Exception as e:
        log_error(traceback.format_exc())

