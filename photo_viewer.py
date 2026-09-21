import os
import shutil
import tkinter as tk
from tkinter import messagebox, colorchooser, ttk
from PIL import Image, ImageTk, ImageOps, ImageFilter
import sys
import subprocess
if sys.platform == 'win32':
    import winsound
import json
import io
import platform
import queue
import threading
import time
import rawpy
import urllib.request
import urllib.error
import tempfile
import webbrowser

CURRENT_VERSION = "3.6.0"
APPLICATION_TITLE = f"🐦 Water-redstart: the chirping viewer v{CURRENT_VERSION}"

# 预读必须与 Tk 完全隔离。Tkinter/Tcl 只允许 UI 主线程访问；后台线程收到的
# 请求均是路径、整数和线程安全的 Python 容器。
PRELOAD_BEHIND_COUNT = 2
PRELOAD_NORMAL_COUNT = 12
PRELOAD_LOW_MEMORY_COUNT = 4
PRELOAD_SUPER_STARTUP_COUNT = 24
PRELOAD_SUPER_COUNT = 16
NAVIGATION_THROTTLE_MS = 120
CONFIG_SAVE_DELAY_MS = 500
SELECT_COUNT_CACHE_SECONDS = 2.0


def _select_release_asset(assets, platform_name=None, machine_name=None):
    """从 GitHub Release assets 中选择当前平台和架构的安装包。"""
    platform_name = platform_name or sys.platform
    machine_name = (machine_name or platform.machine()).lower()

    if platform_name == "win32":
        candidates = [asset for asset in assets if asset.get("name", "").lower().endswith(".exe")]
        preferred = [
            asset for asset in candidates
            if "windows" in asset.get("name", "").lower() or "win" in asset.get("name", "").lower()
        ]
        return (preferred or candidates or [None])[0]

    if platform_name == "darwin":
        is_arm64 = machine_name in ("arm64", "aarch64")
        mac_assets = []
        for asset in assets:
            name = asset.get("name", "").lower()
            if not name.endswith((".zip", ".dmg")):
                continue
            if not any(token in name for token in ("macos", "darwin", "mac_")):
                continue
            if is_arm64 and any(token in name for token in ("x64", "x86_64", "intel")):
                continue
            if not is_arm64 and any(token in name for token in ("arm64", "aarch64", "apple_silicon")):
                continue
            mac_assets.append(asset)

        arch_tokens = ("arm64", "aarch64", "apple_silicon") if is_arm64 else ("x64", "x86_64", "intel")
        preferred = [
            asset for asset in mac_assets
            if any(token in asset.get("name", "").lower() for token in arch_tokens)
        ]
        return (preferred or mac_assets or [None])[0]

    return None


def _decode_image_fast(img_path, size_val):
    """读取并缩放图片；本函数不创建、读取或销毁任何 Tk 对象。"""
    ext = os.path.splitext(img_path)[1].lower()
    size_val = max(1, int(size_val))
    target_size = (size_val, size_val)
    try:
        if ext in ('.arw', '.sr2', '.srf', '.crw', '.cr2', '.cr3', '.nef', '.nrw', '.dng', '.orf', '.rw2', '.raf', '.pef'):
            # 对于RAW照片，使用rawpy提取内嵌的预览图（通常是jpeg格式）
            with rawpy.imread(img_path) as raw:
                try:
                    thumb = raw.extract_thumb()
                except rawpy.LibRawNoThumbnailError:
                    rgb = raw.postprocess(half_size=True, use_camera_wb=True)
                    img = Image.fromarray(rgb)
                    img.thumbnail(target_size, Image.Resampling.LANCZOS)
                    return ImageOps.exif_transpose(img)

            if thumb.format in (rawpy.ThumbFormat.JPEG, rawpy.ThumbFormat.BITMAP):
                img = Image.open(io.BytesIO(thumb.data))
                img.draft("RGB", target_size)
                img = ImageOps.exif_transpose(img)
                img.thumbnail(target_size, Image.Resampling.LANCZOS)
                return img

        img = Image.open(img_path)
        img.draft("RGB", target_size)
        img = ImageOps.exif_transpose(img)
        img.thumbnail(target_size, Image.Resampling.LANCZOS)
        return img
    except Exception as e:
        print(f"解析图片加速失败 {img_path}: {e}")
        return None


def _build_preload_indices(current_idx, image_count, preload_count):
    """返回当前页附近的有序预读索引，优先保留少量后向缓存。"""
    indices = []
    for offset in range(1, PRELOAD_BEHIND_COUNT + 1):
        idx = current_idx - offset
        if idx >= 0:
            indices.append(idx)
    for offset in range(1, preload_count + 1):
        idx = current_idx + offset
        if idx < image_count:
            indices.append(idx)
    return tuple(indices)


def _execute_preload_request(request):
    """执行一份不可变预读快照；只接触普通 Python 数据和 PIL/rawpy。"""
    cancel_event = request["cancel_event"]
    cache = request["cache"]
    cache_lock = request["cache_lock"]
    image_names = request["image_names"]
    current_dir = request["current_dir"]
    image_quality = request["image_quality"]

    for idx in request["indices"]:
        if cancel_event.is_set():
            return

        with cache_lock:
            if idx in cache:
                continue

        img_path = os.path.join(current_dir, image_names[idx])
        img_obj = _decode_image_fast(img_path, image_quality)
        if cancel_event.is_set():
            return
        if img_obj is not None:
            with cache_lock:
                cache[idx] = img_obj


def _preload_worker_loop(request_queue):
    """单一守护 worker：串行处理预读，避免高速翻页产生解码线程风暴。"""
    while True:
        request = request_queue.get()
        if request is None:
            return
        try:
            _execute_preload_request(request)
        except Exception as e:
            # 单张或单次请求失败不能终止后续预读。
            print(f"后台预读失败: {e}")

class ImageViewer:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APPLICATION_TITLE} (双平台)")
        # 初始窗口大小（加宽以容纳顶栏完整快捷键指引）
        self.root.geometry("1050x750")

        # 增加可配置的 UI 颜色和大小属性 (更清新的鸟类主题色)
        self.window_bg_color = "#E8F5E9" # 极其淡的森林绿
        self.ui_bg_color = "#B2EBF2" # 清爽的天蓝色
        self.ui_font_size = 10
        
        # 设置可爱的窗口背景色
        self.root.configure(bg=self.window_bg_color)

        self.is_fullscreen = False
        self.keep_top_bar_in_fullscreen = False

        # 获取当前文件夹下所有图片（屏蔽子目录）
        self.supported_formats = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.arw', '.sr2', '.srf', '.crw', '.cr2', '.cr3', '.nef', '.nrw', '.dng', '.orf', '.rw2', '.raf', '.pef')
        self.current_dir = os.getcwd()
        self.images = []
        self.image_groups = {}
        self._scan_images()

        self.index = 0
        self.select_folder_name = "SELECT"
        self.history = []  # 记录操作历史用于撤销
        
        # 图片预加载缓存。后台只有一个守护 worker，请求队列只保留最新位置。
        self.image_cache = {}
        self._image_cache_lock = threading.Lock()
        self._preload_requests = queue.Queue(maxsize=1)
        self._preload_cancel_event = None
        self.preload_thread = threading.Thread(
            target=_preload_worker_loop,
            args=(self._preload_requests,),
            daemon=True,
            name="birdviewer-preload",
        )
        self.preload_thread.start()

        self._navigation_job = None
        self._pending_navigation_delta = 0
        self._last_navigation_render_at = 0.0
        self._config_save_job = None
        self._select_count_cache = None
        self._select_count_cache_path = None
        self._select_count_cache_at = 0.0
        
        # 尝试读取上一次的进度配置
        self.config_file = os.path.join(self.current_dir, ".birdviewer_config.json")
        self.image_quality = tk.IntVar(value=8000)
        self.super_mode = tk.BooleanVar(value=False)  # Super 连拍模式
        self.ignored_version = None  # 用户忽略的更新版本
        self.load_config()
        
        # 添加一个顶部控制面板背景 (缩小高度)
        self.top_frame = tk.Frame(self.root, bg=self.ui_bg_color)
        self.top_frame.pack(fill=tk.X)
        self.top_info_label = tk.Label(self.top_frame, text="✨ 准备好寻找最可爱的小鸟了吗？ ✨", font=("Microsoft YaHei", self.ui_font_size), fg="#555555", bg=self.ui_bg_color)
        self.top_info_label.pack(pady=2)
        
        # 进度条
        self.progress_scale = tk.Scale(self.top_frame, from_=0, to=len(self.images)-1 if self.images else 0, orient=tk.HORIZONTAL, bg=self.ui_bg_color, highlightthickness=0, showvalue=0, command=self.on_progress_change)
        self.progress_scale.pack(fill=tk.X, padx=20, pady=1)

        # 使用 Canvas 来显示图片并支持放大拖拽
        # 设置内边距和圆角边框感 (缩减内边距提高图片占比)
        self.img_frame = tk.Frame(self.root, bg=self.window_bg_color, bd=0)
        self.img_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.canvas = tk.Canvas(self.img_frame, bg='#FFFFFF', relief=tk.GROOVE, bd=2)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        # Tk 9 在 macOS/Windows 上会为触控板和 Magic Mouse 发送高精度滚动事件。
        try:
            self.canvas.bind("<TouchpadScroll>", self.on_touchpad_scroll)
        except tk.TclError:
            # Tk 8.6 不支持该事件，继续使用 MouseWheel。
            pass
        self.canvas.bind("<Button-4>", self.on_mouse_wheel_up)
        self.canvas.bind("<Button-5>", self.on_mouse_wheel_down)
        self.canvas.bind("<ButtonPress-1>", self.on_button_press)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)

        # 视图控制变量
        self.is_fit = True
        self.current_scale = 1.0
        self.im_x = 0
        self.im_y = 0
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.reverse_mode = False
        self.is_magnifying = False
        self.magnify_m = 3.8

        # 热键配置
        self.hotkey_next = 'd'
        self.hotkey_copy = 'w'
        self.hotkey_undo = 'a'
        self.hotkey_clip = 's'
        self.hotkey_arrow_left = 'Left'
        self.hotkey_arrow_right = 'Right'
        self.hotkey_arrow_up = 'Up'
        self.hotkey_arrow_down = 'Down'
        self.hotkey_magnify = 'space'
        self.hotkey_properties = 'i'
        
        self._current_bind_next = None
        self._current_bind_copy = None
        self._current_bind_undo = None
        self._current_bind_clip = None
        self._current_bind_arrow_left = None
        self._current_bind_arrow_right = None
        self._current_bind_arrow_up = None
        self._current_bind_arrow_down = None
        self._current_bind_magnify = None
        self._current_bind_properties = None

        self.create_menu()
        self.apply_bindings()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        # 绑定固定快捷键
        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.bind("<Escape>", self.exit_fullscreen)
        
        # 监听画板尺寸变化，防抖刷新
        self.canvas.bind("<Configure>", self.on_resize)

        self.current_img_obj = None  # 原始图片对象
        self.tk_image = None         # tkinter可用的图片对象缓存(防止被垃圾回收)
        self._resize_job = None      # 延迟重绘的任务标识

        # 延迟100ms加载第一张图片，等待窗口初始化完成
        self.root.after(100, self.load_image)
        # 延迟200ms切换为英文输入法，防止中文输入法吞掉快捷键
        self.root.after(200, self.switch_to_english_ime)

    def switch_to_english_ime(self):
        if sys.platform == 'win32':
            try:
                import ctypes
                # 获取 tkinter 真实窗口句柄而不是系统前台窗口
                hwnd = self.root.winfo_id()
                # 加载美式英语键盘布局 (00000409)
                hkl = ctypes.windll.user32.LoadKeyboardLayoutW("00000409", 1)
                # 取消 IME 的关联，防止中文输入法拦截按键
                ctypes.windll.imm32.ImmAssociateContext(hwnd, 0)
                # 发送切换输入法消息
                ctypes.windll.user32.SendMessageW(hwnd, 0x0050, 0, hkl)
            except Exception as e:
                print(f"切换输入法失败: {e}")

    def _scan_images(self):
        from collections import defaultdict
        all_files = [
            f for f in os.listdir(self.current_dir)
            if os.path.isfile(os.path.join(self.current_dir, f)) and f.lower().endswith(self.supported_formats)
        ]
        
        groups = defaultdict(list)
        for f in all_files:
            base, ext = os.path.splitext(f)
            groups[base].append(f)
            
        self.images = []
        self.image_groups = {}
        for base, files in groups.items():
            display_file = files[0]
            # 优先选择 jpg/png 等常见格式作为预览，大幅度加速并且只保留一份显示
            for ext in ['.jpg', '.jpeg', '.png', '.webp', '.bmp']:
                matches = [f for f in files if f.lower().endswith(ext)]
                if matches:
                    display_file = matches[0]
                    break
            self.images.append(display_file)
            self.image_groups[display_file] = files
        self.images.sort()

    def open_folder(self, folder_selected=None):
        if not folder_selected:
            from tkinter import filedialog
            folder_selected = filedialog.askdirectory(title="选择包含照片的文件夹")
        if folder_selected:
            if not hasattr(self, 'loading_splash') or not self.loading_splash:
                self.loading_splash = tk.Toplevel(self.root)
                self.loading_splash.overrideredirect(True)
                splash_w, splash_h = 240, 120
                screen_w = self.root.winfo_screenwidth()
                screen_h = self.root.winfo_screenheight()
                self.loading_splash.geometry(f"{splash_w}x{splash_h}+{int((screen_w-splash_w)/2)}+{int((screen_h-splash_h)/2)}")
                self.loading_splash.configure(bg="#FFF3E0")
                tk.Label(self.loading_splash, text="🥚 Hatching...", font=("Microsoft YaHei", 16, "bold"), bg="#FFF3E0", fg="#E65100").pack(expand=True)
                self.loading_splash.update()
            
            self.root.after(20, lambda: self._process_open_folder(folder_selected))

    def _process_open_folder(self, folder_selected):
        self._cancel_pending_navigation()
        self.save_config()
        self._cancel_preload(clear_cache=True)

        # 清理当前资源
        self.current_img_obj = None
        self.tk_image = None
        self.images = []
        self._select_count_cache = None
        self._select_count_cache_path = None
        self._select_count_cache_at = 0.0
        if self.canvas and hasattr(self, 'image_on_canvas') and self.image_on_canvas:
            self.canvas.delete(self.image_on_canvas)
            
        self.current_dir = folder_selected
        self.config_file = os.path.join(self.current_dir, ".birdviewer_config.json")
        
        # 重新获取图片
        self._scan_images()
        
        self.index = 0
        self.history = []
        
        # 读取上一次的进度配置
        self.load_config()
        
        if hasattr(self, 'loading_splash') and self.loading_splash:
            try:
                self.loading_splash.destroy()
            except:
                pass
            self.loading_splash = None

        if self.images:
            self.progress_scale.config(to=len(self.images)-1)
            self.progress_scale.set(self.index)
            if self.reverse_var.get():
                self.images.reverse()
                self.index = len(self.images) - 1 - self.index
                self.progress_scale.set(self.index)
            self.update_title()
            self.load_image()
        else:
            self.progress_scale.config(to=0)
            self.progress_scale.set(0)
            
            subdirs = []
            try:
                subdirs = [d for d in os.listdir(folder_selected) if os.path.isdir(os.path.join(folder_selected, d))]
            except Exception:
                pass
                
            if subdirs:
                self.show_subfolder_dialog(folder_selected, subdirs)
            else:
                messagebox.showwarning("哎呀", "孵化失败，小鸟去哪了？文件夹里似乎没有照片呢！")
                self.show_empty_state()
                self.top_info_label.config(text="🐾 欢迎！请挑选一个装满照片的文件夹开始吧~")
                self.root.title(APPLICATION_TITLE)

    def show_subfolder_dialog(self, parent_folder, subdirs):
        dialog = tk.Toplevel(self.root)
        dialog.title("🌿 发现隐藏的枝桠...")
        dialog.geometry("400x350")
        dialog.configure(bg="#E8F5E9")
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(dialog, text="当前巢穴没有照片，但小鸟好像藏在下面的树枝里，\n要飞去那里看看吗？", font=("Microsoft YaHei", 11), bg="#E8F5E9", fg="#2E7D32").pack(pady=10)

        frame = tk.Frame(dialog, bg="#E8F5E9")
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=5)

        scrollbar = tk.Scrollbar(frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        listbox = tk.Listbox(frame, yscrollcommand=scrollbar.set, font=("Microsoft YaHei", 10), bg="#F1F8E9", selectbackground="#A5D6A7", selectforeground="#000000", bd=0, highlightthickness=1)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=listbox.yview)

        for d in subdirs:
            listbox.insert(tk.END, d)

        def on_open():
            selection = listbox.curselection()
            if selection:
                selected_subdir = listbox.get(selection[0])
                new_path = os.path.join(parent_folder, selected_subdir)
                dialog.destroy()
                self.open_folder(new_path)
            else:
                messagebox.showinfo("提示", "请先挑选一截树枝哦！", parent=dialog)

        def on_cancel():
            dialog.destroy()
            self.show_empty_state()
            self.top_info_label.config(text="🐾 欢迎！请挑选一个装满照片的文件夹开始吧~")
            self.root.title(APPLICATION_TITLE)

        # 双击列表项直接打开
        listbox.bind("<Double-1>", lambda e: on_open())

        btn_frame = tk.Frame(dialog, bg="#E8F5E9")
        btn_frame.pack(pady=15)

        tk.Button(btn_frame, text="🕊️ 飞去看看", command=on_open, bg="#81C784", fg="white", font=("Microsoft YaHei", 10, "bold"), bd=0, activebackground="#4CAF50", cursor="hand2", padx=10, pady=5).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="🏠 退回鸟巢", command=on_cancel, bg="#FFCC80", fg="white", font=("Microsoft YaHei", 10, "bold"), bd=0, activebackground="#FFB74D", cursor="hand2", padx=10, pady=5).pack(side=tk.LEFT, padx=10)

    def create_menu(self):
        self.menubar = tk.Menu(self.root)
        
        # 文件菜单
        file_menu = tk.Menu(self.menubar, tearoff=0)
        file_menu.add_command(label="打开照片文件夹...", command=self.open_folder)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.close)
        self.menubar.add_cascade(label="文件", menu=file_menu)
        
        # 设置菜单
        settings_menu = tk.Menu(self.menubar, tearoff=0)
        
        self.reverse_var = tk.BooleanVar(value=False)
        settings_menu.add_checkbutton(label="倒序选片 (从后往前)", variable=self.reverse_var, command=self.toggle_reverse)
        
        self.low_memory_mode = tk.BooleanVar(value=False)
        settings_menu.add_checkbutton(label="省内存模式 (预加载4张)", variable=self.low_memory_mode)
        settings_menu.add_checkbutton(label="🚀 Super 连拍模式 (启动24张/翻页16张)", variable=self.super_mode)
        
        quality_menu = tk.Menu(settings_menu, tearoff=0)
        quality_menu.add_radiobutton(label="2K (运行快，省内存)", variable=self.image_quality, value=2000, command=self.change_quality)
        quality_menu.add_radiobutton(label="4K (平衡)", variable=self.image_quality, value=4000, command=self.change_quality)
        quality_menu.add_radiobutton(label="8K (默认，最高清)", variable=self.image_quality, value=8000, command=self.change_quality)
        settings_menu.add_cascade(label="照片显示画质", menu=quality_menu)
        
        settings_menu.add_separator()
        
        settings_menu.add_command(label="修改操作热键与文件夹...", command=self.show_hotkey_dialog)
        self.menubar.add_cascade(label="设置", menu=settings_menu)
        
        # 帮助菜单
        help_menu = tk.Menu(self.menubar, tearoff=0)
        help_menu.add_command(label="操作说明", command=lambda: messagebox.showinfo(
            "操作说明", 
            f"- 按 下一张 ({self.hotkey_next.upper()} / {self.hotkey_arrow_right.title()}): 翻到下一张图片\n"
            f"- 按 挑出 ({self.hotkey_copy.upper()} / {self.hotkey_arrow_up.title()}): 复制本图片至{self.select_folder_name}文件夹并翻页\n"
            f"- 按 撤销 ({self.hotkey_undo.upper()}): 撤销上一步操作并退回\n"
            f"- 按 复制 ({self.hotkey_clip.upper()} / {self.hotkey_arrow_down.title()}): 将当前照片复制到系统剪贴板\n"
            f"- 按 返回上图 ({self.hotkey_arrow_left.title()}): 翻到上一张图\n"
            f"- 按住 放大 ({self.hotkey_magnify.title()}): 在按住期间中心放大对焦\n"
            f"- 按 属性 ({self.hotkey_properties.upper()}): 显示当前图片的详细属性信息\n"
            "- F11: 切换全屏\n"
            "- Esc: 退出全屏"
        ))
        help_menu.add_separator()
        help_menu.add_command(label="🕊️ 检查更新...", command=lambda: threading.Thread(target=self.check_for_updates, daemon=True).start())
        help_menu.add_command(label="关于", command=lambda: messagebox.showinfo(
            "关于", f"🐦 Water-redstart: the chirping viewer\n\n版本：{CURRENT_VERSION} (双平台)\n作者：Crowpaw@2026\n鸣谢：ARC, Untribiium, ~ris, 蓝嘴红鹊, 欧鹭风云, 瑞瑞的, 白鹡鸰, 灰喜鹊, 碳酸, 逢青, Gemini 3.1 Pro, GPT-5.5, DeepSeek-V4-Pro, YIP\n于一"
        ))
        self.menubar.add_cascade(label="帮助", menu=help_menu)
        
        self.root.config(menu=self.menubar)

    def apply_bindings(self):
        # 解绑旧的热键
        for attr in ['_current_bind_next', '_current_bind_copy', '_current_bind_undo', '_current_bind_clip',
                     '_current_bind_arrow_left', '_current_bind_arrow_right', '_current_bind_arrow_up', '_current_bind_arrow_down']:
            old_sym = getattr(self, attr, None)
            if old_sym:
                try:
                    self.root.unbind(f"<{old_sym}>")
                except tk.TclError:
                    pass
                    
        old_mag = getattr(self, '_current_bind_magnify', None)
        if old_mag:
            try:
                self.root.unbind(f"<KeyPress-{old_mag}>")
                self.root.unbind(f"<KeyRelease-{old_mag}>")
            except tk.TclError: pass

        old_props = getattr(self, '_current_bind_properties', None)
        if old_props:
            try:
                self.root.unbind(f"<{old_props}>")
            except tk.TclError: pass
            
        # 绑定新的热键
        self.root.bind(f"<{self.hotkey_next}>", self.next_image)
        self.root.bind(f"<{self.hotkey_copy}>", self.copy_and_next)
        self.root.bind(f"<{self.hotkey_undo}>", self.undo_action)
        self.root.bind(f"<{self.hotkey_clip}>", self.copy_to_os_clipboard)
        
        self.root.bind(f"<{self.hotkey_arrow_left}>", self.prev_image)
        self.root.bind(f"<{self.hotkey_arrow_right}>", self.next_image)
        self.root.bind(f"<{self.hotkey_arrow_up}>", self.copy_and_next)
        self.root.bind(f"<{self.hotkey_arrow_down}>", self.copy_to_os_clipboard)
        
        self.root.bind(f"<KeyPress-{self.hotkey_magnify}>", self.on_space_press)
        self.root.bind(f"<KeyRelease-{self.hotkey_magnify}>", self.on_space_release)
        
        self.root.bind(f"<{self.hotkey_properties}>", self.show_image_properties)
        
        self._current_bind_next = self.hotkey_next
        self._current_bind_copy = self.hotkey_copy
        self._current_bind_undo = self.hotkey_undo
        self._current_bind_clip = self.hotkey_clip
        self._current_bind_arrow_left = self.hotkey_arrow_left
        self._current_bind_arrow_right = self.hotkey_arrow_right
        self._current_bind_arrow_up = self.hotkey_arrow_up
        self._current_bind_arrow_down = self.hotkey_arrow_down
        self._current_bind_magnify = self.hotkey_magnify
        self._current_bind_properties = self.hotkey_properties

        self.update_title()

    def toggle_reverse(self):
        self._cancel_pending_navigation()
        self._cancel_preload(clear_cache=True)
        self.reverse_mode = self.reverse_var.get()
        self.images.reverse()
        self.index = len(self.images) - 1 - self.index
        # history reset may be good, or just update indices in history, but simpler to clear history
        self.history.clear()
        self.progress_scale.set(self.index)
        self.load_image()

    def change_quality(self):
        self._cancel_pending_navigation()
        self._cancel_preload(clear_cache=True)
        self.save_config()
        self.load_image()

    def get_select_count(self):
        select_folder_path = self.select_folder_name if os.path.isabs(self.select_folder_name) else os.path.join(self.current_dir, self.select_folder_name)
        now = time.monotonic()
        if (
            self._select_count_cache_path == select_folder_path
            and self._select_count_cache is not None
            and now - self._select_count_cache_at < SELECT_COUNT_CACHE_SECONDS
        ):
            return self._select_count_cache

        count = 0
        if os.path.exists(select_folder_path):
            try:
                count = sum(
                    1 for f in os.listdir(select_folder_path)
                    if os.path.isfile(os.path.join(select_folder_path, f))
                    and f.lower().endswith(self.supported_formats)
                )
            except OSError:
                count = 0

        self._select_count_cache = count
        self._select_count_cache_path = select_folder_path
        self._select_count_cache_at = now
        return count

    def update_title(self):
        select_count = self.get_select_count()
        folder_name = os.path.basename(self.current_dir.rstrip('/\\')) if self.current_dir else '未选择目录'
        base_title_prefix = f"{APPLICATION_TITLE} (双平台) - {folder_name}"
        
        info_help = (f">> 跳: [{self.hotkey_next.upper()}/{self.hotkey_arrow_right.title()}]  "
                     f"* 挑: [{self.hotkey_copy.upper()}/{self.hotkey_arrow_up.title()}]  "
                     f"<< 撤: [{self.hotkey_undo.upper()}]  "
                     f"[c] 剪贴: [{self.hotkey_clip.upper()}/{self.hotkey_arrow_down.title()}]  "
                     f"~ 放大: [{self.hotkey_magnify.title()}]  "
                     f"[i] 属性: [{self.hotkey_properties.upper()}]")

        if self.images and self.index < len(self.images):
            img_name = self.images[self.index]
            group_files = self.image_groups.get(img_name, [img_name])
            group_info = ""
            if len(group_files) > 1:
                group_info = " | 🗂️ J+RAW"
                
            self.root.title(f"{base_title_prefix} | 进度: {self.index + 1}/{len(self.images)} | 🏠入巢: {select_count} | 当前: {img_name}{group_info}")
            self.top_info_label.config(
                text=f"🐾 进度：{self.index + 1}/{len(self.images)} 📷 【{img_name}】{group_info} | 🏠 入巢: {select_count}只 | {info_help}"
            )
        else:
            self.root.title(base_title_prefix)
            self.top_info_label.config(text=f"🏠 入巢: {select_count}只 | {info_help}")

    def show_hotkey_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("🎈 自定义魔法设置 🎈")
        dialog.geometry("450x680")
        dialog.configure(bg="#FFECD2")
        dialog.transient(self.root)  # 置于主窗口之上
        dialog.grab_set()  # 模态对话框
        
        # 居中标题
        tk.Label(dialog, text="💫 设定你的专属偏好 💫", bg="#FFECD2", fg="#d63384", font=("Microsoft YaHei", 12, "bold")).grid(row=0, column=0, columnspan=2, pady=10)
        
        dialog.temp_next = self.hotkey_next
        dialog.temp_copy = self.hotkey_copy
        dialog.temp_undo = self.hotkey_undo
        dialog.temp_clip = self.hotkey_clip
        dialog.temp_arrow_left = getattr(self, 'hotkey_arrow_left', 'Left')
        dialog.temp_arrow_right = getattr(self, 'hotkey_arrow_right', 'Right')
        dialog.temp_arrow_up = getattr(self, 'hotkey_arrow_up', 'Up')
        dialog.temp_arrow_down = getattr(self, 'hotkey_arrow_down', 'Down')
        dialog.temp_magnify = getattr(self, 'hotkey_magnify', 'space')
        dialog.temp_properties = getattr(self, 'hotkey_properties', 'i')
        dialog.temp_color = self.ui_bg_color
        dialog.temp_keep_top_bar = tk.BooleanVar(value=self.keep_top_bar_in_fullscreen)

        def prompt_key(btn, attr_name):
            prompt = tk.Toplevel(dialog)
            prompt.title("录入按键")
            prompt.geometry("250x120")
            prompt.transient(dialog)
            prompt.grab_set()
            prompt.configure(bg="#FFECD2")
            tk.Label(prompt, text="请按下你想要设置的按键\n\n(支持方向键、回车、字母等)", bg="#FFECD2", font=("Microsoft YaHei", 10)).pack(expand=True)
            def on_key(event):
                sym = event.keysym
                setattr(dialog, attr_name, sym)
                btn.config(text=f"[{sym}] (点击修改)")
                prompt.destroy()
            prompt.bind("<Key>", on_key)
            prompt.focus_set()

        tk.Label(dialog, text="👉 下一张快捷键:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=1, column=0, padx=20, pady=5, sticky="e")
        btn_next = tk.Button(dialog, text=f"[{self.hotkey_next}] (点击修改)", width=18, bg="#FFFFFF", command=lambda: prompt_key(btn_next, 'temp_next'))
        btn_next.grid(row=1, column=1, sticky="w")
        
        tk.Label(dialog, text="💖 挑出快捷键:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=2, column=0, padx=20, pady=5, sticky="e")
        btn_copy = tk.Button(dialog, text=f"[{self.hotkey_copy}] (点击修改)", width=18, bg="#FFFFFF", command=lambda: prompt_key(btn_copy, 'temp_copy'))
        btn_copy.grid(row=2, column=1, sticky="w")

        tk.Label(dialog, text="⏪ 撤销快捷键:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=3, column=0, padx=20, pady=5, sticky="e")
        btn_undo = tk.Button(dialog, text=f"[{self.hotkey_undo}] (点击修改)", width=18, bg="#FFFFFF", command=lambda: prompt_key(btn_undo, 'temp_undo'))
        btn_undo.grid(row=3, column=1, sticky="w")
        
        tk.Label(dialog, text="� 复制快捷键:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=4, column=0, padx=20, pady=5, sticky="e")
        btn_clip = tk.Button(dialog, text=f"[{self.hotkey_clip}] (点击修改)", width=18, bg="#FFFFFF", command=lambda: prompt_key(btn_clip, 'temp_clip'))
        btn_clip.grid(row=4, column=1, sticky="w")
        
        tk.Label(dialog, text="⬅️ 返回上张键:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=5, column=0, padx=20, pady=5, sticky="e")
        btn_arrow_left = tk.Button(dialog, text=f"[{getattr(self, 'hotkey_arrow_left', 'Left')}] (点击修改)", width=18, bg="#FFFFFF", command=lambda: prompt_key(btn_arrow_left, 'temp_arrow_left'))
        btn_arrow_left.grid(row=5, column=1, sticky="w")

        tk.Label(dialog, text="➡️ 快进下张键:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=6, column=0, padx=20, pady=5, sticky="e")
        btn_arrow_right = tk.Button(dialog, text=f"[{getattr(self, 'hotkey_arrow_right', 'Right')}] (点击修改)", width=18, bg="#FFFFFF", command=lambda: prompt_key(btn_arrow_right, 'temp_arrow_right'))
        btn_arrow_right.grid(row=6, column=1, sticky="w")

        tk.Label(dialog, text="⬆️ 挑出热键2:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=7, column=0, padx=20, pady=5, sticky="e")
        btn_arrow_up = tk.Button(dialog, text=f"[{getattr(self, 'hotkey_arrow_up', 'Up')}] (点击修改)", width=18, bg="#FFFFFF", command=lambda: prompt_key(btn_arrow_up, 'temp_arrow_up'))
        btn_arrow_up.grid(row=7, column=1, sticky="w")

        tk.Label(dialog, text="⬇️ 剪贴板热键2:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=8, column=0, padx=20, pady=5, sticky="e")
        btn_arrow_down = tk.Button(dialog, text=f"[{getattr(self, 'hotkey_arrow_down', 'Down')}] (点击修改)", width=18, bg="#FFFFFF", command=lambda: prompt_key(btn_arrow_down, 'temp_arrow_down'))
        btn_arrow_down.grid(row=8, column=1, sticky="w")

        tk.Label(dialog, text="🔍 放大快捷键:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=9, column=0, padx=20, pady=5, sticky="e")
        btn_magnify = tk.Button(dialog, text=f"[{getattr(self, 'hotkey_magnify', 'space')}] (点击修改)", width=18, bg="#FFFFFF", command=lambda: prompt_key(btn_magnify, 'temp_magnify'))
        btn_magnify.grid(row=9, column=1, sticky="w")

        tk.Label(dialog, text="🏷 属性快捷键:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=10, column=0, padx=20, pady=5, sticky="e")
        btn_properties = tk.Button(dialog, text=f"[{getattr(self, 'hotkey_properties', 'i')}] (点击修改)", width=18, bg="#FFFFFF", command=lambda: prompt_key(btn_properties, 'temp_properties'))
        btn_properties.grid(row=10, column=1, sticky="w")

        tk.Label(dialog, text="🏠 入巢文件夹名:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=11, column=0, padx=20, pady=5, sticky="e")
        
        folder_frame = tk.Frame(dialog, bg="#FFECD2")
        folder_frame.grid(row=11, column=1, sticky="w")
        
        entry_folder = tk.Entry(folder_frame, width=15, font=("Arial", 11), justify="center", bd=2, relief=tk.SUNKEN)
        entry_folder.insert(0, self.select_folder_name)
        entry_folder.pack(side=tk.LEFT, padx=(0, 5))
        
        def browse_folder():
            from tkinter import filedialog
            selected = filedialog.askdirectory(title="选择一个文件夹作为挑出目录", parent=dialog)
            if selected:
                entry_folder.delete(0, tk.END)
                entry_folder.insert(0, selected)
                
        btn_browse = tk.Button(folder_frame, text="浏览...", bg="#FFFFFF", command=browse_folder)
        btn_browse.pack(side=tk.LEFT)

        def choose_color():
            c = colorchooser.askcolor(title="选择顶部UI背景颜色", initialcolor=self.ui_bg_color)
            if c[1]:
                dialog.temp_color = c[1]
                btn_color.config(bg=c[1])

        tk.Label(dialog, text="🎨 顶部横幅颜色:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=12, column=0, padx=20, pady=5, sticky="e")
        btn_color = tk.Button(dialog, text=" 选择颜色 ", width=18, bg=self.ui_bg_color, command=choose_color)
        btn_color.grid(row=12, column=1, sticky="w")

        tk.Label(dialog, text="🔠 顶部文字大小:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=13, column=0, padx=20, pady=5, sticky="e")
        scale_size = tk.Scale(dialog, from_=6, to=24, orient=tk.HORIZONTAL, bg="#FFECD2", highlightthickness=0, length=140)
        scale_size.set(self.ui_font_size)
        scale_size.grid(row=13, column=1, sticky="w")
        
        tk.Label(dialog, text="📺 全屏选项:", bg="#FFECD2", font=("Microsoft YaHei", 10)).grid(row=14, column=0, padx=20, pady=5, sticky="e")
        chk_top_bar = tk.Checkbutton(dialog, text="全屏时保留上方菜单栏", variable=dialog.temp_keep_top_bar, bg="#FFECD2", font=("Microsoft YaHei", 10))
        chk_top_bar.grid(row=14, column=1, sticky="w")
        
        def save():
            new_folder = entry_folder.get().strip()
            
            if not new_folder:
                messagebox.showwarning("哎呀", "文件夹名不能为空哦！", parent=dialog)
                return
            if len(set([dialog.temp_next, dialog.temp_copy, dialog.temp_undo, dialog.temp_clip, dialog.temp_magnify, dialog.temp_properties])) < 6:
                messagebox.showwarning("哎呀", "不同功能的热键不能重复！", parent=dialog)
                return
            if len(set([dialog.temp_arrow_left, dialog.temp_arrow_right, dialog.temp_arrow_up, dialog.temp_arrow_down])) < 4:
                messagebox.showwarning("哎呀", "方向动作热键不能互相重复！", parent=dialog)
                return
                
            self.hotkey_next = dialog.temp_next
            self.hotkey_copy = dialog.temp_copy
            self.hotkey_undo = dialog.temp_undo
            self.hotkey_clip = dialog.temp_clip
            self.hotkey_magnify = dialog.temp_magnify
            self.hotkey_properties = dialog.temp_properties
            self.hotkey_arrow_left = dialog.temp_arrow_left
            self.hotkey_arrow_right = dialog.temp_arrow_right
            self.hotkey_arrow_up = dialog.temp_arrow_up
            self.hotkey_arrow_down = dialog.temp_arrow_down
            self.select_folder_name = new_folder
            self.keep_top_bar_in_fullscreen = dialog.temp_keep_top_bar.get()
            
            # Update UI Style
            self.ui_bg_color = dialog.temp_color
            self.ui_font_size = scale_size.get()
            self.top_frame.config(bg=self.ui_bg_color)
            self.top_info_label.config(bg=self.ui_bg_color, font=("Microsoft YaHei", self.ui_font_size))
            self.update_title()
            
            # 重新生成菜单（更新帮助说明里的热键显示）
            self.create_menu()
            self.apply_bindings()
            
            # Apply fullscreen changes if currently in fullscreen
            if self.is_fullscreen:
                if not self.keep_top_bar_in_fullscreen:
                    self.root.config(menu="")
                    self.top_frame.pack_forget()
                else:
                    self.root.config(menu=self.menubar)
                    self.top_frame.pack(fill=tk.X, before=self.img_frame)

            self.save_config()
            messagebox.showinfo("成功啦", "小魔法重新生效啦！✨", parent=dialog)
            dialog.destroy()
            
        btn_save = tk.Button(dialog, text="🎀 保存设置", command=save, width=20, font=("Microsoft YaHei", 11, "bold"), bg="#FFB6C1", fg="white", activebackground="#FF69B4", bd=0, cursor="hand2")
        btn_save.grid(row=15, column=0, columnspan=2, pady=20)

    def get_global_config_path(self):
        return os.path.expanduser("~/.birdviewer_global.json")

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                import json
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    last_img = config.get("last_image")
                    if last_img and last_img in self.images:
                        self.index = self.images.index(last_img)
                    self.keep_top_bar_in_fullscreen = config.get("keep_top_bar_in_fullscreen", False)
                    self.image_quality.set(config.get("image_quality", 8000))
                    self.select_folder_name = config.get("select_folder_name", "SELECT")
                    self.ignored_version = config.get("ignored_version", None)
                    self.super_mode.set(config.get("super_mode", False))
            except Exception:
                pass

    def save_config(self):
        if self._config_save_job is not None:
            try:
                self.root.after_cancel(self._config_save_job)
            except (tk.TclError, ValueError):
                pass
            self._config_save_job = None

        try:
            import json
            global_path = self.get_global_config_path()
            global_data = {}
            if os.path.exists(global_path):
                with open(global_path, 'r', encoding='utf-8') as gf:
                    try: global_data = json.load(gf)
                    except: pass
            global_data['last_dir'] = self.current_dir
            with open(global_path, 'w', encoding='utf-8') as gf:
                json.dump(global_data, gf)
        except Exception: pass

        if self.images and self.index < len(self.images):
            try:
                with open(self.config_file, 'w', encoding='utf-8') as f:
                    json.dump({
                        "last_image": self.images[self.index],
                        "keep_top_bar_in_fullscreen": getattr(self, 'keep_top_bar_in_fullscreen', False),
                        "image_quality": self.image_quality.get(),
                        "select_folder_name": self.select_folder_name,
                        "ignored_version": getattr(self, 'ignored_version', None),
                        "super_mode": self.super_mode.get()
                    }, f)
            except Exception:
                pass

    def schedule_save_config(self):
        """合并连续翻页产生的配置写入，停止操作后再落盘。"""
        if self._config_save_job is not None:
            try:
                self.root.after_cancel(self._config_save_job)
            except (tk.TclError, ValueError):
                pass
        self._config_save_job = self.root.after(CONFIG_SAVE_DELAY_MS, self._run_scheduled_config_save)

    def _run_scheduled_config_save(self):
        self._config_save_job = None
        self.save_config()

    @staticmethod
    def read_image_fast(img_path, size_val=8000):
        """支持 RAW 和普通图片；调用方必须在 UI 线程取得画质整数。"""
        return _decode_image_fast(img_path, size_val)

    def _cancel_preload(self, clear_cache=False):
        """取消运行中及排队中的预读；正在解码的单张会在完成后丢弃。"""
        if self._preload_cancel_event is not None:
            self._preload_cancel_event.set()
            self._preload_cancel_event = None

        try:
            while True:
                stale_request = self._preload_requests.get_nowait()
                if stale_request is not None:
                    stale_request["cancel_event"].set()
        except queue.Empty:
            pass

        if clear_cache:
            with self._image_cache_lock:
                self.image_cache = {}

    def close(self):
        """在 UI 线程保存最终进度并停止后台预读。"""
        self._cancel_pending_navigation()
        self.save_config()
        self._cancel_preload(clear_cache=True)
        try:
            self._preload_requests.put_nowait(None)
        except queue.Full:
            pass
        self.root.destroy()
                        
    def show_end_dialog(self):
        if getattr(self, 'end_dialog_open', False):
            return
        self.end_dialog_open = True
        
        end_window = tk.Toplevel(self.root)
        end_window.title("🎉 挑图完成！")
        end_window.geometry("400x320")
        end_window.configure(bg="#E0F7FA")
        end_window.transient(self.root)
        end_window.grab_set()

        tk.Label(end_window, text="🐣 恭喜！所有照片都已经挑完啦！\n请问您想怎么处理文件呢？", font=("Microsoft YaHei", 12, "bold"), bg="#E0F7FA", fg="#006064", pady=15).pack()

        def do_delete_self():
            try:
                if hasattr(self, 'config_file') and os.path.exists(self.config_file):
                    os.remove(self.config_file)
            except Exception as e:
                print(f"配置文件删除失败: {e}")
                
            exe_path = os.path.abspath(sys.argv[0])
            exe_dir = os.path.dirname(exe_path)
            current_dir_abs = os.path.abspath(self.current_dir) if hasattr(self, 'current_dir') and self.current_dir else ""
            
            if exe_dir != current_dir_abs:
                return
                
            if sys.platform == 'win32':
                bat_path = os.path.join(os.environ.get('TEMP', 'C:\\'), 'del_self.bat')
                exe_path = sys.argv[0]
                with open(bat_path, 'w') as f:
                    f.write(f'@echo off\nping 127.0.0.1 -n 2 > nul\ndel "{exe_path}"\ndel "%~f0"')
                subprocess.Popen(bat_path, shell=True, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
            else:
                try:
                    os.remove(sys.argv[0])
                except:
                    pass

        def do_delete_originals():
            for img in self.images:
                try:
                    os.remove(os.path.join(self.current_dir, img))
                except:
                    pass

        def on_action(choice):
            if choice == 1:
                do_delete_self()
            elif choice == 2:
                do_delete_originals()
            elif choice == 3:
                do_delete_originals()
                do_delete_self()
            self.end_dialog_open = False
            end_window.destroy()
            if choice == 4:
                self.close()
            else:
                self._cancel_preload(clear_cache=True)
                self.root.destroy()

        end_window.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, 'end_dialog_open', False), end_window.destroy()))

        btn_style = {"font": ("Microsoft YaHei", 10), "width": 25, "pady": 5, "fg": "white", "bd": 0, "cursor": "hand2"}

        b1 = tk.Button(end_window, text="🐥 1. 删除本看图程序", bg="#4DD0E1", activebackground="#26C6DA", command=lambda: on_action(1))
        b1.config(**btn_style)
        b1.pack(pady=6)

        b2 = tk.Button(end_window, text="🔥 2. 删除本文件夹中原片", bg="#FF8A65", activebackground="#4DD0E1", command=lambda: on_action(2))
        b2.config(**btn_style)
        b2.pack(pady=6)

        b3 = tk.Button(end_window, text="☄️ 3. 两者全部删除", bg="#FF5252", activebackground="#FF1744", command=lambda: on_action(3))
        b3.config(**btn_style)
        b3.pack(pady=6)

        b4 = tk.Button(end_window, text="🌸 4. 啥也不删，单纯退出", bg="#81C784", activebackground="#4DB6AC", command=lambda: on_action(4))
        b4.config(**btn_style)
        b4.pack(pady=6)

    def on_progress_change(self, val):
        new_idx = int(val)
        if new_idx != self.index and 0 <= new_idx < len(self.images):
            self._cancel_pending_navigation()
            self.index = int(new_idx)
            self.load_image()

    def start_preload(self):
        """在 UI 线程冻结设置快照，然后把纯 Python 请求交给后台 worker。"""
        current_idx = int(self.index)
        image_names = tuple(self.images)
        current_dir = str(self.current_dir)
        image_quality = int(self.image_quality.get())
        super_mode = bool(self.super_mode.get())
        low_memory_mode = bool(
            getattr(self, 'low_memory_mode', None) and self.low_memory_mode.get()
        )

        with self._image_cache_lock:
            cached_count = len(self.image_cache)

        is_startup = current_idx <= 2 and cached_count < 10
        if super_mode:
            preload_count = PRELOAD_SUPER_STARTUP_COUNT if is_startup else PRELOAD_SUPER_COUNT
            if is_startup:
                old_text = self.top_info_label.cget("text")
                self.top_info_label.config(
                    text=f"🚀 Super 连拍模式启动中，小鸟正在预加载 {preload_count} 张照片..."
                )
                # 加载完成后恢复（约 2 秒后）
                self.root.after(2500, lambda: self.top_info_label.config(text=old_text) if self.top_info_label.winfo_exists() else None)
        else:
            preload_count = PRELOAD_LOW_MEMORY_COUNT if low_memory_mode else PRELOAD_NORMAL_COUNT

        indices = _build_preload_indices(current_idx, len(image_names), preload_count)
        keep_indices = set(indices)
        keep_indices.add(current_idx)

        self._cancel_preload()
        cancel_event = threading.Event()
        self._preload_cancel_event = cancel_event

        with self._image_cache_lock:
            cache = self.image_cache
            for idx in tuple(cache):
                if idx not in keep_indices:
                    del cache[idx]

        request = {
            "current_dir": current_dir,
            "image_names": image_names,
            "indices": indices,
            "image_quality": image_quality,
            "cache": cache,
            "cache_lock": self._image_cache_lock,
            "cancel_event": cancel_event,
        }
        try:
            self._preload_requests.put_nowait(request)
        except queue.Full:
            cancel_event.set()

    def show_empty_state(self):
        if hasattr(self, 'empty_frame') and self.empty_frame.winfo_exists():
            return
            
        self.canvas.pack_forget()
        
        self.empty_frame = tk.Frame(self.img_frame, bg='#FFFFFF', relief=tk.GROOVE, bd=2)
        self.empty_frame.pack(fill=tk.BOTH, expand=True)
        
        # 内部容器使其居中
        inner_frame = tk.Frame(self.empty_frame, bg='#FFFFFF')
        inner_frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        
        lbl_title = tk.Label(inner_frame, text="🌿 这里还没有鸟儿的踪迹 🌿", font=("Microsoft YaHei", 20, "bold"), bg='#FFFFFF', fg="#2E7D32")
        lbl_title.pack(pady=20)
        
        lbl_desc = tk.Label(inner_frame, text="您可以选择以下方式开始一段观鸟之旅：", font=("Microsoft YaHei", 12), bg='#FFFFFF', fg="#555555")
        lbl_desc.pack(pady=10)

        # 手绘鸟巢形状的拖拽/点击区域
        nest_canvas = tk.Canvas(inner_frame, width=460, height=260, bg='#FFFFFF', highlightthickness=0)
        nest_canvas.pack(pady=10)
        
        # 建立标签用于识别鸟巢点击
        # 画鸟巢中躺着的几颗鸟蛋
        nest_canvas.create_oval(150, 130, 190, 175, fill="#E0F7FA", outline="#B2EBF2", width=2, tags="nest")
        nest_canvas.create_oval(180, 140, 220, 185, fill="#F1F8E9", outline="#DCEDC8", width=2, tags="nest")
        nest_canvas.create_oval(210, 125, 250, 170, fill="#FFF3E0", outline="#FFE0B2", width=2, tags="nest")

        # 绘制半圆形鸟巢主体
        nest_canvas.create_arc(60, 100, 340, 280, start=180, extent=180, fill="#D7CCC8", outline="#8D6E63", width=5, style=tk.CHORD, tags="nest")
        
        # 绘制鸟巢表面的树枝交错纹理
        branches = [
            (80, 170, 130, 190), (110, 200, 170, 180), (150, 180, 200, 210), 
            (190, 195, 250, 180), (230, 190, 290, 205), (270, 185, 320, 170),
            (90, 160, 140, 170), (280, 170, 320, 150), (140, 210, 180, 220), (210, 215, 250, 200)
        ]
        for x1, y1, x2, y2 in branches:
            nest_canvas.create_line(x1, y1, x2, y2, fill="#A1887F", width=4, capstyle=tk.ROUND, tags="nest")

        # 鸟巢上的文字
        lbl_drag_id = nest_canvas.create_text(200, 100, text="✨ 将文件夹拖入巢中\n     或者点击鸟巢选择文件夹开始孵化 ✨", font=("Microsoft YaHei", 12, "bold"), fill="#6D4C41", justify=tk.CENTER, tags="nest")
        
        # 绑定点击鸟巢触发选择文件夹
        nest_canvas.tag_bind("nest", "<Button-1>", lambda e: self.open_folder())
        nest_canvas.tag_bind("nest", "<Enter>", lambda e: nest_canvas.config(cursor="hand2"))
        
        # 尝试读取上次打开的文件夹并绘制回头鸟
        import json
        last_dir = None
        global_path = self.get_global_config_path()
        if os.path.exists(global_path):
            with open(global_path, 'r', encoding='utf-8') as gf:
                try: last_dir = json.load(gf).get('last_dir')
                except: pass

        if last_dir and os.path.isdir(last_dir):
            # 将鸟放置在鸟巢正右方，嘴巴改小且变黑，朝向左侧看鸟巢
            # Bird Body
            nest_canvas.create_oval(345, 130, 395, 175, fill="#4DD0E1", outline="#00BCD4", width=2, tags="bird")
            nest_canvas.create_polygon(335, 145, 348, 148, 348, 153, fill="black", outline="black", tags="bird") # Beak (Small, Black, Left)
            nest_canvas.create_oval(355, 140, 363, 148, fill="black", tags="bird") # Eye
            nest_canvas.create_oval(357, 142, 359, 144, fill="white", tags="bird") # Eye Light
            nest_canvas.create_polygon(385, 150, 420, 160, 385, 165, fill="#FF8A65", outline="#FF7043", tags="bird") # Tail
            nest_canvas.create_oval(365, 150, 390, 165, fill="#80DEEA", outline="#00ACC1", width=1, tags="bird") # Wing
            
            # 鸟的脚丫
            nest_canvas.create_line(365, 175, 365, 190, fill="#FFB300", width=2, tags="bird")
            nest_canvas.create_line(375, 175, 375, 190, fill="#FFB300", width=2, tags="bird")
            
            nest_canvas.create_text(370, 105, text="回上次的\n鸟巢看看", font=("Microsoft YaHei", 9, "bold"), fill="#006064", justify=tk.CENTER, tags="bird")
            
            nest_canvas.tag_bind("bird", "<Button-1>", lambda e, d=last_dir: self.open_folder(d))
            nest_canvas.tag_bind("bird", "<Enter>", lambda e: nest_canvas.config(cursor="hand2"))

        # 绑定拖拽事件 (使用 windnd 适配 Windows)
        try:
            if sys.platform == 'win32':
                import windnd
                def on_drop(files):
                    if files:
                        try:
                            # 尝试对接收到的文件路径进行解码
                            if isinstance(files[0], bytes):
                                try:
                                    folder_path = files[0].decode('gbk')
                                except:
                                    folder_path = files[0].decode('utf-8', errors='ignore')
                            else:
                                folder_path = str(files[0])
                            
                            if os.path.exists(folder_path):
                                path_to_open = folder_path if os.path.isdir(folder_path) else os.path.dirname(folder_path)
                                # 安全脱离：延迟 200ms 执行加载，防止底层的拖拽窗体消息还没结束就被后续加载流程销毁了UI导致内存越界闪退
                                self.root.after(200, self.open_folder, path_to_open)
                        except Exception as e:
                            print(f"拖拽处理异常: {e}")
                windnd.hook_dropfiles(self.root, func=on_drop)
        except Exception as e:
            nest_canvas.itemconfig(lbl_drag_id, text="✨ 拖拽功能缺失 ✨\n(请点击鸟巢手动选择文件夹)")
            
    def hide_empty_state(self):
        if hasattr(self, 'empty_frame') and self.empty_frame.winfo_exists():
            self.empty_frame.destroy()
            self.canvas.pack(fill=tk.BOTH, expand=True)

    def load_image(self):
        # 1. 先检查当前目录下有没有图片
        if not self.images:
            self.show_empty_state()
            self.top_info_label.config(text="🐾 欢迎！请挑选一个装满照片的文件夹开始吧~")
            self.root.title(APPLICATION_TITLE)
            return
            
        self.hide_empty_state()

        # 检查是否看到最后一张了
        if self.index >= len(self.images):
            # 将索引先倒退回最后一张，防止越界崩溃
            self.index = len(self.images) - 1
            self.show_end_dialog()
            return

        img_path = os.path.join(self.current_dir, self.images[self.index])
        
        try:
            # 优先从多线程缓存中拿，拿到直接显示
            with self._image_cache_lock:
                cached_img = self.image_cache.get(self.index)
            if cached_img is not None:
                self.current_img_obj = cached_img
            else:
                # Tk 变量只在 UI 线程读取，并把普通整数传给解码函数。
                image_quality = int(self.image_quality.get())
                raw_img = self.read_image_fast(img_path, image_quality)
                if not raw_img:
                    raise Exception("解析图片返回为空")
                self.current_img_obj = raw_img
                with self._image_cache_lock:
                    self.image_cache[self.index] = raw_img

            self.is_fit = True
            self.update_title()
            
            # Update progress bar
            self.progress_scale.set(self.index)
            
            self.display_image()
            
            # 本页渲染完毕后，触发邻近图片预读，并延迟保存本次进度。
            self.start_preload()
            self.schedule_save_config()
            
        except Exception as e:
            print(f"无法打开图片 {img_path}: {e}")
            self.index += 1
            if self.index < len(self.images):
                self.root.after(1, self.load_image)
            else:
                self.index = len(self.images) - 1
                self.show_end_dialog()

    @staticmethod
    def _decode_touchpad_delta(packed_delta):
        """解析 Tk 9 打包的两个有符号 16 位滚动增量。"""
        packed = int(packed_delta) & 0xFFFFFFFF
        delta_x = (packed >> 16) & 0xFFFF
        delta_y = packed & 0xFFFF
        if delta_x >= 0x8000:
            delta_x -= 0x10000
        if delta_y >= 0x8000:
            delta_y -= 0x10000
        return delta_x, delta_y

    def _zoom_at_cursor(self, x, y, steps):
        if not getattr(self, 'current_img_obj', None) or steps == 0:
            return "break"

        self.is_fit = False
        steps = max(-4.0, min(4.0, float(steps)))
        scale_factor = 1.1 ** steps

        ww = self.canvas.winfo_width()
        wh = self.canvas.winfo_height()

        cursor_im_x = self.im_x + (x - ww / 2) / self.current_scale
        cursor_im_y = self.im_y + (y - wh / 2) / self.current_scale

        self.current_scale *= scale_factor

        self.im_x = cursor_im_x - (x - ww / 2) / self.current_scale
        self.im_y = cursor_im_y - (y - wh / 2) / self.current_scale

        self.display_image()
        return "break"

    def on_mouse_wheel(self, event):
        delta = float(getattr(event, 'delta', 0))
        if delta == 0:
            return "break"

        if sys.platform == 'win32':
            steps = delta / 120.0
        elif sys.platform == 'darwin':
            steps = delta
        else:
            steps = delta / 120.0 if abs(delta) >= 120 else delta
        return self._zoom_at_cursor(event.x, event.y, steps)

    def on_touchpad_scroll(self, event):
        try:
            delta_x, delta_y = self.root.tk.call(
                "tk::PreciseScrollDeltas", int(event.delta)
            )
        except (tk.TclError, TypeError, ValueError):
            delta_x, delta_y = self._decode_touchpad_delta(event.delta)

        del delta_x
        return self._zoom_at_cursor(event.x, event.y, float(delta_y) / 10.0)

    def on_mouse_wheel_up(self, event):
        return self._zoom_at_cursor(event.x, event.y, 1.0)

    def on_mouse_wheel_down(self, event):
        return self._zoom_at_cursor(event.x, event.y, -1.0)

    def on_button_press(self, event):
        self.drag_start_x = event.x
        self.drag_start_y = event.y

    def on_mouse_drag(self, event):
        if not getattr(self, 'current_img_obj', None): return
        self.is_fit = False
        dx = event.x - self.drag_start_x
        dy = event.y - self.drag_start_y
        self.drag_start_x = event.x
        self.drag_start_y = event.y
        
        self.im_x -= dx / self.current_scale
        self.im_y -= dy / self.current_scale
        
        self.display_image()

    def on_space_press(self, event=None):
        if not getattr(self, 'is_magnifying', False):
            self.is_magnifying = True
            # 保存放大前的浏览状态
            self.pre_magnify_state = {
                'is_fit': getattr(self, 'is_fit', True),
                'current_scale': getattr(self, 'current_scale', 1.0),
                'im_x': getattr(self, 'im_x', 0),
                'im_y': getattr(self, 'im_y', 0)
            }
            self.display_image()

    def on_space_release(self, event=None):
        if getattr(self, 'is_magnifying', False):
            self.is_magnifying = False
            # 恢复放大前的浏览状态
            if hasattr(self, 'pre_magnify_state'):
                state = self.pre_magnify_state
                self.is_fit = state['is_fit']
                self.current_scale = state['current_scale']
                self.im_x = state['im_x']
                self.im_y = state['im_y']
            self.display_image()

    def display_image(self, event=None):
        if not self.current_img_obj:
            return

        ww = self.canvas.winfo_width()
        wh = self.canvas.winfo_height()

        if ww < 10 or wh < 10:
            ww, wh = 800, 600

        img_w, img_h = self.current_img_obj.size

        if getattr(self, 'is_magnifying', False):
            base_scale = min(ww / img_w, wh / img_h)
            crop_w = max(32, img_w / self.magnify_m)
            self.current_scale = (img_w / crop_w) * base_scale
            self.is_fit = False
            self.im_x = img_w / 2
            self.im_y = img_h / 2
        elif self.is_fit:
            self.current_scale = min(ww / img_w, wh / img_h)
            self.im_x = img_w / 2
            self.im_y = img_h / 2

        src_w = max(1, ww / self.current_scale)
        src_h = max(1, wh / self.current_scale)

        left = self.im_x - src_w / 2
        top = self.im_y - src_h / 2
        right = self.im_x + src_w / 2
        bottom = self.im_y + src_h / 2

        crop_left = max(0, left)
        crop_top = max(0, top)
        crop_right = min(img_w, right)
        crop_bottom = min(img_h, bottom)

        if crop_right > crop_left and crop_bottom > crop_top:
            cropped = self.current_img_obj.crop((crop_left, crop_top, crop_right, crop_bottom))
            
            dest_left = max(0, (crop_left - left) * self.current_scale)
            dest_top = max(0, (crop_top - top) * self.current_scale)
            dest_w = max(1, (crop_right - crop_left) * self.current_scale)
            dest_h = max(1, (crop_bottom - crop_top) * self.current_scale)

            # 使用 BILINEAR 加速交互期的拖动与缩放刷新
            resized_img = cropped.resize((int(dest_w), int(dest_h)), Image.Resampling.BILINEAR)
            
            if getattr(self, 'is_magnifying', False):
                # 模拟轻微锐化：半径 0.5，强度约 30%
                resized_img = resized_img.filter(ImageFilter.UnsharpMask(radius=0.5, percent=30, threshold=0))
                
            self.tk_image = ImageTk.PhotoImage(resized_img)

            self.canvas.delete("all")
            self.canvas.create_image(
                dest_left + int(dest_w/2), 
                dest_top + int(dest_h/2), 
                image=self.tk_image, anchor=tk.CENTER
            )

    def on_resize(self, event):
        if hasattr(self, 'canvas') and event.widget == self.canvas:
            if self._resize_job:
                self.root.after_cancel(self._resize_job)
            self._resize_job = self.root.after(100, self.display_image)

    def _cancel_pending_navigation(self):
        if self._navigation_job is not None:
            try:
                self.root.after_cancel(self._navigation_job)
            except (tk.TclError, ValueError):
                pass
        self._navigation_job = None
        self._pending_navigation_delta = 0

    def _queue_navigation(self, delta):
        """单次按键立即响应；连发时限频并合并中间位置。"""
        if not self.images:
            return "break"

        target = self.index + self._pending_navigation_delta + delta
        target = max(0, min(target, len(self.images)))
        self._pending_navigation_delta = target - self.index

        if self._navigation_job is None:
            elapsed_ms = (time.monotonic() - self._last_navigation_render_at) * 1000
            if self._last_navigation_render_at == 0.0 or elapsed_ms >= NAVIGATION_THROTTLE_MS:
                self._apply_pending_navigation()
            else:
                delay_ms = max(1, int(NAVIGATION_THROTTLE_MS - elapsed_ms))
                self._navigation_job = self.root.after(delay_ms, self._apply_pending_navigation)
        return "break"

    def _apply_pending_navigation(self):
        delta = self._pending_navigation_delta
        self._pending_navigation_delta = 0
        self._navigation_job = None
        if delta == 0:
            return

        target = self.index + delta
        if delta > 0:
            for idx in range(self.index, min(target, len(self.images))):
                self.history.append({'action': 'next', 'index': idx})
        self.index = target
        self.load_image()
        self._last_navigation_render_at = time.monotonic()

    def next_image(self, event=None):
        return self._queue_navigation(1)

    def prev_image(self, event=None):
        return self._queue_navigation(-1)

    def copy_and_next(self, event=None):
        self._cancel_pending_navigation()
        if self.index < len(self.images):
            img_name = self.images[self.index]
            files_to_copy = self.image_groups.get(img_name, [img_name])
            
            # 如果 SELECT 文件夹不存在则创建
            select_folder_path = self.select_folder_name if os.path.isabs(self.select_folder_name) else os.path.join(self.current_dir, self.select_folder_name)
            if not os.path.exists(select_folder_path):
                os.makedirs(select_folder_path)
                
            dst_list = []
            src_list = []
            for fname in files_to_copy:
                src_list.append(os.path.join(self.current_dir, fname))
                dst_list.append(os.path.join(select_folder_path, fname))
            
            select_folder = self.select_folder_name
            
            def bg_copy():
                try:
                    for src, dst, fname in zip(src_list, dst_list, files_to_copy):
                        # 复制图片并保留其原始的元信息(拍照时间等)
                        shutil.copy2(src, dst)
                        print(f"🎉 成功捕捉: {fname} 已飞入 {select_folder} 鸟巢~ 🦉")
                except Exception as e:
                    print(f"哎呀，小鸟跑掉啦，复制文件失败:\n{e}")

            # 立即播放音效（主线程，按下即响，避免音画不同步）
            if sys.platform == 'win32':
                winsound.Beep(3000, 30)
                winsound.Beep(3800, 30)
                winsound.Beep(4500, 50)
            else:
                self.root.bell()

            # 立即写入历史记录并执行后台拷贝线程
            self.history.append({'action': 'copy', 'index': self.index, 'dst_list': dst_list})
            threading.Thread(target=bg_copy, daemon=True).start()
        
        # 复制完后自动翻下一张
        self.index += 1
        self.load_image()

    def copy_to_os_clipboard(self, event=None):
        self._cancel_pending_navigation()
        if not self.images or self.index >= len(self.images):
            return
            
        img_path = os.path.join(self.current_dir, self.images[self.index])
        try:
            if sys.platform == 'win32':
                # 使用 PowerShell 将文件复制进剪切板 (解决转 BMP 后文件过大问题)
                ps_cmd = f"Set-Clipboard -Path '{img_path}'"
                subprocess.run(['powershell', '-command', ps_cmd], creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
                
                winsound.Beep(2500, 80)
                winsound.Beep(3000, 80)
            elif sys.platform == 'darwin':
                image = Image.open(img_path)
                # macOS 使用 osascript 借助临时 TIFF 文件复制图片到剪贴板
                temp_tiff = os.path.join(self.current_dir, ".temp_clip.tiff")
                image.save(temp_tiff, "TIFF")
                script = f'set the clipboard to (read (POSIX file "{temp_tiff}") as TIFF picture)'
                subprocess.run(["osascript", "-e", script])
                try:
                    os.remove(temp_tiff)
                except:
                    pass
                self.root.bell()
            else:
                self.root.bell()
                print("暂不支持当前操作系统的直接剪贴板图片功能。")
                return
            
            # 使用临时文本提示 UI
            old_text = self.top_info_label.cget("text")
            self.top_info_label.config(text=f"✅ 成功：{self.images[self.index]} 已复制到系统剪贴板！")
            self.root.after(1500, self.update_title)  # 1.5秒后恢复标题
            
            print(f"📋 成功将 {self.images[self.index]} 复制到剪贴板。")
        except Exception as e:
            messagebox.showerror("哎呀", f"图片复制到剪贴板失败:\n{e}")

    def show_image_properties(self, event=None):
        """显示当前图片的属性信息，支持多图时弹出选择框"""
        self._cancel_pending_navigation()
        if not self.images or self.index >= len(self.images):
            return
        
        img_name = self.images[self.index]
        group_files = self.image_groups.get(img_name, [img_name])
        
        # 如果有多张关联图片（如 JPG+RAW），弹出选择框
        if len(group_files) > 1:
            self._show_multi_properties_dialog(group_files)
        else:
            img_path = os.path.join(self.current_dir, group_files[0])
            self._show_single_properties(img_path)

    def _show_multi_properties_dialog(self, group_files):
        """弹出可爱的对话框让用户选择查看哪张图片的属性"""
        dialog = tk.Toplevel(self.root)
        dialog.title("📋 小鸟信息卡")
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        dlg_w, dlg_h = 420, 380
        x = (sw - dlg_w) // 2
        y = (sh - dlg_h) // 2
        dialog.geometry(f"{dlg_w}x{dlg_h}+{x}+{y}")
        dialog.configure(bg="#FFF3E0")
        dialog.transient(self.root)
        # 不使用 grab_set()，让主窗口快捷键仍可响应
        
        # 标题
        tk.Label(dialog, text="🐣 检测到筑巢文件组！", font=("Microsoft YaHei", 13, "bold"), 
                 bg="#FFF3E0", fg="#E65100").pack(pady=(15, 5))
        tk.Label(dialog, text="当前照片有多个同名的格式版本，\n你想查看哪只小鸟的身份信息呢？", 
                 font=("Microsoft YaHei", 10), bg="#FFF3E0", fg="#6D4C41").pack(pady=(0, 15))
        
        # 文件列表区域
        list_frame = tk.Frame(dialog, bg="#FFF3E0")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=5)
        
        for fname in sorted(group_files):
            fpath = os.path.join(self.current_dir, fname)
            ext = os.path.splitext(fname)[1].upper().lstrip('.')
            size_str = ""
            try:
                size_bytes = os.path.getsize(fpath)
                if size_bytes >= 1024*1024:
                    size_str = f"{size_bytes/(1024*1024):.1f} MB"
                else:
                    size_str = f"{size_bytes/1024:.1f} KB"
            except:
                size_str = "未知"
            
            # 文件图标
            if ext.lower() in ('jpg', 'jpeg', 'png', 'webp', 'bmp', 'gif'):
                icon = "🖼️"
            elif ext.lower() in ('arw', 'sr2', 'srf', 'crw', 'cr2', 'cr3', 'nef', 'nrw', 'dng', 'orf', 'rw2', 'raf', 'pef'):
                icon = "📸"
            else:
                icon = "📄"
            
            btn_frame = tk.Frame(list_frame, bg="#FFF3E0")
            btn_frame.pack(fill=tk.X, pady=4)
            
            btn = tk.Button(btn_frame, 
                           text=f"  {icon}  {fname}    [{size_str}]    点击查看 →", 
                           font=("Microsoft YaHei", 10),
                           bg="#FFE0B2", fg="#4E342E",
                           activebackground="#FFCC80",
                           bd=0, cursor="hand2",
                           anchor="w", padx=15, pady=8,
                           command=lambda p=fpath: self._show_single_properties(p, dialog))
            btn.pack(fill=tk.X)
        
        # 底部取消按钮
        tk.Button(dialog, text="🌸 先不看了", font=("Microsoft YaHei", 10, "bold"),
                  bg="#BCAAA4", fg="white", bd=0, cursor="hand2",
                  activebackground="#A1887F",
                  padx=20, pady=6,
                  command=dialog.destroy).pack(pady=(10, 15))

    def _show_single_properties(self, img_path, parent_dialog=None):
        """显示单张图片的详细属性"""
        if parent_dialog:
            parent_dialog.destroy()
        
        fname = os.path.basename(img_path)
        ext = os.path.splitext(fname)[1].lower()
        
        # 收集属性信息
        try:
            stat = os.stat(img_path)
            size_bytes = stat.st_size
            mtime = stat.st_mtime
        except:
            size_bytes = 0
            mtime = 0
        
        # 文件大小友好显示
        if size_bytes >= 1024*1024:
            size_str = f"{size_bytes/(1024*1024):.2f} MB ({size_bytes:,} 字节)"
        else:
            size_str = f"{size_bytes/1024:.2f} KB ({size_bytes:,} 字节)"
        
        # 修改时间
        import datetime
        mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "未知"
        
        # 图片尺寸 + EXIF拍摄时间（RAW文件通过rawpy缩略图读取EXIF）
        dims_str = "未知"
        shooting_time_str = None
        fmt = ext.upper().lstrip('.')
        
        def _read_exif_from_pil(img_obj):
            """从PIL图像对象读取EXIF，返回 {shooting_time, iso, shutter, aperture, focal}"""
            nonlocal fmt
            result = {}
            try:
                fmt = img_obj.format or ext.upper().lstrip('.')
                exif = img_obj.getexif()
                # 合并主 IFD 和 EXIF 子 IFD (0x8769) 的标签
                exif_ifd = exif.get_ifd(0x8769) if hasattr(exif, 'get_ifd') else {}
                all_tags = {}
                for k, v in exif.items():
                    all_tags[k] = v
                for k, v in exif_ifd.items():
                    all_tags[k] = v
                if all_tags:
                    # 拍摄时间 (优先子IFD的36867/36868，其次主IFD的306)
                    for tag in (36867, 36868, 306):
                        dt_str = all_tags.get(tag)
                        if dt_str and isinstance(dt_str, str) and dt_str.strip():
                            try:
                                dt = datetime.datetime.strptime(dt_str.strip(), "%Y:%m:%d %H:%M:%S")
                                result['shooting_time'] = dt.strftime("%Y-%m-%d %H:%M:%S")
                            except:
                                result['shooting_time'] = dt_str.strip()
                            break
                    # ISO (34855)
                    iso_val = all_tags.get(34855)
                    if iso_val is not None:
                        result['iso'] = str(iso_val)
                    # 曝光时间 / 快门速度 (33434) — PIL返回IFDRational，需float()转换
                    et = all_tags.get(33434)
                    if et is not None:
                        try:
                            val = float(et)
                            if val < 1:
                                result['shutter'] = f"1/{int(1/val)}s"
                            else:
                                result['shutter'] = f"{val:.1f}s"
                        except:
                            pass
                    # 光圈 (33437)
                    fn = all_tags.get(33437)
                    if fn is not None:
                        try:
                            result['aperture'] = f"f/{float(fn):.1f}"
                        except:
                            pass
                    # 焦距 (37386)
                    fl = all_tags.get(37386)
                    if fl is not None:
                        try:
                            result['focal'] = f"{float(fl):.0f}mm"
                        except:
                            pass
            except:
                pass
            return result
        
        # 先尝试 PIL 直接打开（适用于JPG/PNG/部分RAW）
        exif_data = {}
        try:
            from PIL import Image
            with Image.open(img_path) as im:
                dims_str = f"{im.size[0]} x {im.size[1]} 像素"
                exif_data = _read_exif_from_pil(im)
        except:
            pass
        shooting_time_str = exif_data.get('shooting_time')
        
        # PIL 失败或 EXIF 不完整时，尝试通过 rawpy 缩略图读取（RAW 专属）
        exif_missing = (
            shooting_time_str is None or dims_str == "未知" or
            not exif_data.get('iso') or not exif_data.get('shutter') or
            not exif_data.get('aperture') or not exif_data.get('focal')
        )
        if exif_missing:
            raw_exts = ('.arw', '.sr2', '.srf', '.crw', '.cr2', '.cr3', '.nef', '.nrw', '.dng', '.orf', '.rw2', '.raf', '.pef')
            if ext in raw_exts:
                try:
                    with rawpy.imread(img_path) as raw:
                        # 获取尺寸
                        if dims_str == "未知":
                            try:
                                dims_str = f"{raw.sizes.width} x {raw.sizes.height} 像素"
                            except:
                                try:
                                    dims_str = f"{raw.sizes.raw_width} x {raw.sizes.raw_height} 像素 (RAW)"
                                except:
                                    pass
                        # 通过JPEG缩略图读取EXIF（RAW文件均含完整EXIF的JPEG预览）
                        exif_incomplete = (
                            shooting_time_str is None or
                            not exif_data.get('iso') or
                            not exif_data.get('shutter') or
                            not exif_data.get('aperture') or
                            not exif_data.get('focal')
                        )
                        if exif_incomplete:
                            try:
                                thumb = raw.extract_thumb()
                                if thumb.format == rawpy.ThumbFormat.JPEG:
                                    from PIL import Image as PILImage
                                    thumb_img = PILImage.open(io.BytesIO(thumb.data))
                                    thumb_exif = _read_exif_from_pil(thumb_img)
                                    if shooting_time_str is None:
                                        shooting_time_str = thumb_exif.get('shooting_time')
                                    # 用缩略图EXIF补全所有缺失字段
                                    for key in ('iso', 'shutter', 'aperture', 'focal'):
                                        if not exif_data.get(key) and thumb_exif.get(key):
                                            exif_data[key] = thumb_exif[key]
                                    if not exif_data.get('shooting_time') and thumb_exif.get('shooting_time'):
                                        exif_data['shooting_time'] = thumb_exif['shooting_time']
                                    thumb_img.close()
                            except:
                                pass
                except Exception as e:
                    print(f"rawpy读取RAW信息失败: {e}")
        
        # 格式判定
        raw_exts = ('.arw', '.sr2', '.srf', '.crw', '.cr2', '.cr3', '.nef', '.nrw', '.dng', '.orf', '.rw2', '.raf', '.pef')
        if ext in raw_exts:
            type_str = "📸 RAW 原始格式"
        elif ext in ('.jpg', '.jpeg'):
            type_str = "🖼️ JPEG 图像"
        elif ext == '.png':
            type_str = "🖼️ PNG 图像"
        elif ext == '.webp':
            type_str = "🖼️ WebP 图像"
        elif ext == '.bmp':
            type_str = "🖼️ BMP 位图"
        elif ext == '.gif':
            type_str = "🖼️ GIF 动图"
        else:
            type_str = f"📄 {ext.upper()} 文件"
        
        # 构建属性对话框（居中显示）
        dlg = tk.Toplevel(self.root)
        dlg.title(f"📋 {fname} 的身份卡")
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        dlg_w, dlg_h = 520, 620
        x = (sw - dlg_w) // 2
        y = (sh - dlg_h) // 2
        dlg.geometry(f"{dlg_w}x{dlg_h}+{x}+{y}")
        dlg.configure(bg="#E8F5E9")
        dlg.transient(self.root)
        # 不使用 grab_set()，让主窗口快捷键仍可响应，且允许多张身份卡同时打开
        
        # 标题区
        tk.Label(dlg, text="🐦 小鸟身份信息卡", font=("Microsoft YaHei", 14, "bold"),
                 bg="#E8F5E9", fg="#2E7D32").pack(pady=(20, 5))
        tk.Label(dlg, text=f"“{fname}”", font=("Microsoft YaHei", 11, "italic"),
                 bg="#E8F5E9", fg="#388E3C").pack(pady=(0, 15))
        
        tk.Label(dlg, text="💡 下方文字可鼠标选中后 Ctrl+C 复制哦~", font=("Microsoft YaHei", 8),
                 bg="#E8F5E9", fg="#999999").pack(pady=(0, 5))
        
        # 属性卡片区域
        card_frame = tk.Frame(dlg, bg="#FFFFFF", relief=tk.GROOVE, bd=1)
        card_frame.pack(fill=tk.BOTH, expand=True, padx=25, pady=0)
        
        props = [
            ("📛  文件名", fname),
            ("📐 图片尺寸", dims_str),
            ("🏷 格式类型", type_str),
            ("💾 文件大小", size_str),
            ("📅 修改时间", mtime_str),
        ]
        if shooting_time_str:
            props.append(("📷 拍摄时间", shooting_time_str))
        # ISO / 快门 / 光圈 / 焦距
        if exif_data.get('iso'):
            props.append(("🔆 ISO", f"ISO {exif_data['iso']}"))
        if exif_data.get('shutter'):
            props.append(("⏱ 快门速度", exif_data['shutter']))
        if exif_data.get('aperture'):
            props.append(("🔵 光圈", exif_data['aperture']))
        if exif_data.get('focal'):
            props.append(("🔭 焦距", exif_data['focal']))
        props.append(("📁 完整路径", img_path))
        
        def _make_val_widget(parent, value, bg_color, is_long):
            if is_long:
                w = tk.Text(parent, font=("Consolas", 8), fg="#333333",
                           bg=bg_color, wrap=tk.WORD, height=2,
                           relief=tk.FLAT, borderwidth=0, padx=8, pady=4)
                w.insert("1.0", value)
                w.config(state=tk.DISABLED)
                return w
            else:
                w = tk.Entry(parent, font=("Consolas", 9), fg="#333333",
                            bg=bg_color, relief=tk.FLAT, borderwidth=0,
                            readonlybackground=bg_color)
                w.insert(0, value)
                w.config(state="readonly")
                return w
        
        for i, (label, value) in enumerate(props):
            bg_color = "#FFFFFF" if i % 2 == 0 else "#F1F8E9"
            row_frame = tk.Frame(card_frame, bg=bg_color)
            row_frame.pack(fill=tk.X)
            
            tk.Label(row_frame, text=label, font=("Microsoft YaHei", 10, "bold"),
                     bg=bg_color, fg="#33691E", width=14, anchor="e",
                     padx=10, pady=6).pack(side=tk.LEFT)
            
            is_long = (label == "📁 完整路径")
            val_widget = _make_val_widget(row_frame, value, bg_color, is_long)
            val_widget.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), pady=4)
        
        # 复制拍摄时间按钮区
        copy_frame = tk.Frame(dlg, bg="#E8F5E9")
        copy_frame.pack(fill=tk.X, padx=25, pady=(10, 0))
        
        time_to_copy = shooting_time_str or mtime_str
        copy_btn = tk.Button(copy_frame, text="📋 复制拍摄时间",
                            font=("Microsoft YaHei", 10, "bold"),
                            bg="#FFE0B2", fg="#4E342E", bd=0, cursor="hand2",
                            activebackground="#FFCC80", padx=18, pady=7)
        copy_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        copy_status = tk.Label(copy_frame, text="", font=("Microsoft YaHei", 9),
                              bg="#E8F5E9", fg="#666666")
        copy_status.pack(side=tk.LEFT)
        
        def do_copy_time():
            try:
                dlg.clipboard_clear()
                dlg.clipboard_append(time_to_copy)
                if sys.platform == 'win32':
                    try:
                        winsound.Beep(2400, 60)
                        winsound.Beep(3000, 80)
                    except:
                        pass
                copy_btn.config(text="✅ 已复制到剪贴板~", bg="#A5D6A7", activebackground="#81C784")
                copy_status.config(text=f"✔ {time_to_copy}", fg="#2E7D32")
                dlg.after(1500, lambda: (copy_btn.config(text="📋 复制拍摄时间", bg="#FFE0B2", activebackground="#FFCC80"), copy_status.config(text="")))
            except Exception:
                copy_btn.config(text="😢 复制失败了…", bg="#FFCDD2", activebackground="#EF9A9A")
                copy_status.config(text="请手动选中上方文字后 Ctrl+C 复制", fg="#C62828")
                dlg.after(2000, lambda: (copy_btn.config(text="📋 复制拍摄时间", bg="#FFE0B2", activebackground="#FFCC80"), copy_status.config(text="")))
        
        copy_btn.config(command=do_copy_time)
        
        source_note = "🕐 EXIF原始拍摄时间" if shooting_time_str else "🕐 文件修改时间（无EXIF数据）"
        tk.Label(copy_frame, text=source_note, font=("Microsoft YaHei", 8),
                 bg="#E8F5E9", fg="#AAAAAA").pack(side=tk.RIGHT)
        
        # 关闭按钮
        tk.Button(dlg, text="🐤 知道啦，飞走~", font=("Microsoft YaHei", 11, "bold"),
                  bg="#81C784", fg="white", bd=0, cursor="hand2",
                  activebackground="#66BB6A", padx=25, pady=8,
                  command=dlg.destroy).pack(pady=(10, 20))
        
        if sys.platform == 'win32':
            try:
                winsound.Beep(1800, 40)
                winsound.Beep(2200, 60)
            except:
                pass

    def undo_action(self, event=None):
        self._cancel_pending_navigation()
        if not self.history:
            messagebox.showinfo("哎呀", "没有回忆可以撤销啦！")
            return
            
        last_action = self.history.pop()
        self.index = last_action['index']
        
        if last_action['action'] == 'copy':
            dst_list = last_action.get('dst_list', [])
            if 'dst' in last_action:  # 兼容旧版本记录
                dst_list.append(last_action['dst'])
                
            for dst in dst_list:
                try:
                    if os.path.exists(dst):
                        os.remove(dst)
                        print(f"⏪ 撤销捕捉: 放飞了小鸟 {os.path.basename(dst)}")
                except Exception as e:
                    print(f"撤销删除失败: {e}")
                
        self.load_image()

    def toggle_fullscreen(self, event=None):
        self.is_fullscreen = not self.is_fullscreen
        self.root.attributes("-fullscreen", self.is_fullscreen)
        if self.is_fullscreen and not getattr(self, 'keep_top_bar_in_fullscreen', False):
            self.root.config(menu="")
            self.top_frame.pack_forget()
        else:
            self.root.config(menu=getattr(self, 'menubar', ""))
            self.top_frame.pack(fill=tk.X, before=self.img_frame)
        return "break"

    def exit_fullscreen(self, event=None):
        self.is_fullscreen = False
        self.root.attributes("-fullscreen", False)
        self.root.config(menu=getattr(self, 'menubar', ""))
        self.top_frame.pack(fill=tk.X, before=self.img_frame)
        return "break"

    def _parse_version(self, ver_str):
        """将 'v3.2' 或 '3.2' 解析为 (3, 2) 元组"""
        v = ver_str.strip().lstrip('v').lstrip('V')
        parts = []
        for p in v.split('.'):
            try:
                parts.append(int(p))
            except:
                parts.append(0)
        return tuple(parts) if parts else (0,)

    def check_for_updates(self, silent=False):
        """后台线程：检查 GitHub 最新 release。silent=True 时只在有更新时弹窗。"""
        try:
            url = "https://api.github.com/repos/Crowheart0/Water-redstart_the_chirping_viewer/releases/latest"
            req = urllib.request.Request(url, headers={"User-Agent": "Water-redstart-Viewer"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            if not isinstance(data, dict):
                return
            
            tag = data.get("tag_name", "")
            latest_ver = self._parse_version(tag)
            current_ver = self._parse_version(CURRENT_VERSION)
            
            if latest_ver > current_ver:
                # 检查是否被用户忽略
                if self.ignored_version and self._parse_version(self.ignored_version) >= latest_ver:
                    return
                asset = _select_release_asset(data.get("assets", []))
                download_url = asset.get("browser_download_url", "") if asset else ""
                asset_name = asset.get("name", "") if asset else ""
                
                release_notes = data.get("body", "")[:200]
                release_url = data.get("html_url", "")
                
                self.root.after(0, lambda: self._show_update_dialog(
                    tag, release_notes, download_url, asset_name, release_url))
            else:
                if not silent:
                    # 用户手动触发的检查，告知已是最新
                    self.root.after(0, lambda t=tag: self._show_up_to_date_dialog(t))
        except Exception as e:
            self.root.after(0, lambda: messagebox.showwarning(
                "哎呀", f"检查更新失败，小鸟迷路了…\n\n{str(e)[:100]}\n\n请检查网络连接后重试~"))

    def _show_up_to_date_dialog(self, tag):
        """可爱的"已是最新"对话框"""
        dlg = tk.Toplevel(self.root)
        dlg.title("🐣 已是最新版本")
        dlg_w, dlg_h = 380, 240
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        dlg.geometry(f"{dlg_w}x{dlg_h}+{(sw-dlg_w)//2}+{(sh-dlg_h)//2}")
        dlg.configure(bg="#E8F5E9")
        dlg.transient(self.root)
        dlg.grab_set()
        
        tk.Label(dlg, text="🌿 小鸟巢穴一切安好~", font=("Microsoft YaHei", 14, "bold"),
                 bg="#E8F5E9", fg="#2E7D32").pack(pady=(25, 10))
        tk.Label(dlg, text=f"你已经是最新版本啦！\n\n🏠 当前版本：{CURRENT_VERSION}\n🕊️ 线上版本：{tag}",
                 font=("Microsoft YaHei", 11), bg="#E8F5E9", fg="#33691E").pack(pady=(0, 15))
        tk.Button(dlg, text="🐤 知道啦，飞走~", font=("Microsoft YaHei", 11, "bold"),
                  bg="#81C784", fg="white", bd=0, cursor="hand2",
                  activebackground="#66BB6A", padx=25, pady=8,
                  command=dlg.destroy).pack(pady=(0, 15))
        
        if sys.platform == 'win32':
            try:
                winsound.Beep(1800, 40)
                winsound.Beep(2200, 60)
            except:
                pass

    def _show_update_dialog(self, tag, notes, download_url, asset_name, release_url):
        """显示可爱的更新提示对话框"""
        dlg = tk.Toplevel(self.root)
        dlg.title("🐣 发现新版本！")
        dlg_w, dlg_h = 540, 480
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        dlg.geometry(f"{dlg_w}x{dlg_h}+{(sw-dlg_w)//2}+{(sh-dlg_h)//2}")
        dlg.configure(bg="#FFF8E1")
        dlg.transient(self.root)
        dlg.grab_set()
        
        # 标题
        tk.Label(dlg, text="🐣 小鸟发现了新巢穴！", font=("Microsoft YaHei", 14, "bold"),
                 bg="#FFF8E1", fg="#E65100").pack(pady=(20, 5))
        tk.Label(dlg, text=f"🌿 新版本 {tag} 已经发布啦~\n你现在的版本是 {CURRENT_VERSION}",
                 font=("Microsoft YaHei", 11), bg="#FFF8E1", fg="#4E342E").pack(pady=(5, 10))
        
        # 更新内容预览
        if notes.strip():
            preview = notes.replace('\r', '')[:180]
            note_frame = tk.Frame(dlg, bg="#FFF3E0", relief=tk.GROOVE, bd=1)
            note_frame.pack(fill=tk.BOTH, expand=True, padx=25, pady=5)
            tk.Label(note_frame, text="📝 更新内容：", font=("Microsoft YaHei", 9, "bold"),
                     bg="#FFF3E0", fg="#BF360C", anchor="w").pack(padx=10, pady=(8, 0), anchor="w")
            tk.Label(note_frame, text=preview, font=("Microsoft YaHei", 8),
                     bg="#FFF3E0", fg="#5D4037", anchor="w", justify=tk.LEFT,
                     wraplength=420).pack(padx=10, pady=(2, 8), anchor="w")
        
        # 按钮区（两行排列，更自然）
        btn_frame1 = tk.Frame(dlg, bg="#FFF8E1")
        btn_frame1.pack(pady=(15, 5))
        btn_frame2 = tk.Frame(dlg, bg="#FFF8E1")
        btn_frame2.pack(pady=(0, 15))
        
        def do_update():
            dlg.destroy()
            if download_url and asset_name:
                self._do_download_update(download_url, asset_name)
            else:
                webbrowser.open(release_url)
        
        def do_open_release():
            webbrowser.open(release_url)
        
        def do_ignore():
            # 二次确认
            if messagebox.askyesno("🔕 确认忽略", f"确定要忽略 {tag} 版本吗？\n\n忽略后，此版本不会再弹出更新提醒~\n下次有新版本时仍会通知你哦！", parent=dlg):
                self.ignored_version = tag
                self.save_config()
                dlg.destroy()
                # 提示已忽略
                messagebox.showinfo("🔕 已忽略", f"已忽略 {tag} 版本~\n\n下次有新版本时小鸟会再来通知你！")
        
        # 第一行：主要操作
        update_button_text = "🕊️ 下载当前平台版本" if download_url else "🌐 前往发布页下载"
        tk.Button(btn_frame1, text=update_button_text, font=("Microsoft YaHei", 11, "bold"),
                  bg="#81C784", fg="white", bd=0, cursor="hand2",
                  activebackground="#66BB6A", padx=18, pady=8,
                  command=do_update).pack(side=tk.LEFT, padx=6)
        
        tk.Button(btn_frame1, text="🌐 在浏览器中查看", font=("Microsoft YaHei", 10),
                  bg="#64B5F6", fg="white", bd=0, cursor="hand2",
                  activebackground="#42A5F5", padx=12, pady=8,
                  command=do_open_release).pack(side=tk.LEFT, padx=6)
        
        # 第二行：次要操作
        tk.Button(btn_frame2, text="🌸 下次再说~", font=("Microsoft YaHei", 10),
                  bg="#BCAAA4", fg="white", bd=0, cursor="hand2",
                  activebackground="#A1887F", padx=14, pady=7,
                  command=dlg.destroy).pack(side=tk.LEFT, padx=6)
        
        tk.Button(btn_frame2, text="🔕 忽略此次更新", font=("Microsoft YaHei", 10),
                  bg="#FFE0B2", fg="#4E342E", bd=0, cursor="hand2",
                  activebackground="#FFCC80", padx=14, pady=7,
                  command=do_ignore).pack(side=tk.LEFT, padx=6)

    def _do_download_update(self, download_url, asset_name):
        """弹出下载进度窗口，后台下载新版本"""
        dlg = tk.Toplevel(self.root)
        dlg.title("📥 小鸟搬运中...")
        dlg_w, dlg_h = 400, 180
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        dlg.geometry(f"{dlg_w}x{dlg_h}+{(sw-dlg_w)//2}+{(sh-dlg_h)//2}")
        dlg.configure(bg="#E8F5E9")
        dlg.transient(self.root)
        dlg.grab_set()
        
        tk.Label(dlg, text="🐦 小鸟正在努力搬运新版本...", font=("Microsoft YaHei", 12, "bold"),
                 bg="#E8F5E9", fg="#2E7D32").pack(pady=(25, 5))
        
        status_label = tk.Label(dlg, text="正在连接 GitHub... ⏳", font=("Microsoft YaHei", 9),
                               bg="#E8F5E9", fg="#555555")
        status_label.pack(pady=5)
        
        progress_var = tk.DoubleVar(value=0)
        progress_bar = ttk.Progressbar(dlg, variable=progress_var, maximum=100, length=300)
        try:
            progress_bar.pack(pady=10)
        except:
            pass  # ttk 不可用时跳过进度条
        
        percent_label = tk.Label(dlg, text="0%", font=("Microsoft YaHei", 9),
                                bg="#E8F5E9", fg="#666666")
        percent_label.pack()
        
        def download_thread():
            try:
                if sys.platform == 'darwin':
                    dest_dir = os.path.expanduser("~/Downloads")
                else:
                    dest_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = os.path.join(dest_dir, asset_name)
                
                req = urllib.request.Request(download_url, headers={"User-Agent": "Water-redstart-Viewer"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    total = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    chunk_size = 65536
                    
                    def update_ui(msg, pct):
                        self.root.after(0, lambda: status_label.config(text=msg))
                        self.root.after(0, lambda: progress_var.set(pct))
                        self.root.after(0, lambda: percent_label.config(text=f"{int(pct)}%"))
                    
                    update_ui(f"下载中... {asset_name}", 0)
                    
                    with open(dest_path, "wb") as f:
                        while True:
                            chunk = resp.read(chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                pct = (downloaded / total) * 100
                                update_ui(f"下载中... {downloaded//1024} KB / {total//1024} KB", pct)
                    
                    update_ui("✅ 下载完成！", 100)
                    
                    if sys.platform == 'win32':
                        try:
                            winsound.Beep(2400, 60)
                            winsound.Beep(3000, 80)
                            winsound.Beep(3600, 100)
                        except:
                            pass
                    
                    def finish():
                        dlg.destroy()
                        # 弹出完成提示
                        done = tk.Toplevel(self.root)
                        done.title("🎉 更新就绪！")
                        dw, dh = 420, 220
                        done.geometry(f"{dw}x{dh}+{(sw-dw)//2}+{(sh-dh)//2}")
                        done.configure(bg="#E8F5E9")
                        done.transient(self.root)
                        done.grab_set()
                        
                        tk.Label(done, text="🎉 新版本已搬运到巢穴边！", font=("Microsoft YaHei", 13, "bold"),
                                 bg="#E8F5E9", fg="#2E7D32").pack(pady=(20, 10))
                        tk.Label(done, text=f"📁 文件位置：\n{dest_path}", font=("Consolas", 8),
                                 bg="#E8F5E9", fg="#555555", wraplength=360).pack(pady=5)
                        install_hint = (
                            "💡 双击 ZIP 解压后，把 Water-redstart.app 拖入“应用程序”~"
                            if sys.platform == 'darwin'
                            else "💡 请关闭当前程序，然后双击新文件即可~"
                        )
                        tk.Label(done, text=install_hint,
                                 font=("Microsoft YaHei", 10), bg="#E8F5E9", fg="#388E3C").pack(pady=5)
                        
                        def open_folder():
                            if sys.platform == 'win32':
                                os.startfile(dest_dir)
                            elif sys.platform == 'darwin':
                                subprocess.Popen(["open", dest_dir])
                        tk.Button(done, text="📂 打开文件所在位置", font=("Microsoft YaHei", 10, "bold"),
                                  bg="#81C784", fg="white", bd=0, cursor="hand2",
                                  activebackground="#66BB6A", padx=16, pady=6,
                                  command=lambda: (open_folder(), done.destroy())).pack(pady=8)
                        tk.Button(done, text="🐤 知道啦~", font=("Microsoft YaHei", 10),
                                  bg="#BCAAA4", fg="white", bd=0, cursor="hand2",
                                  activebackground="#A1887F", padx=14, pady=5,
                                  command=done.destroy).pack(pady=(0, 10))
                    self.root.after(500, finish)
                    
            except Exception as e:
                def fail():
                    dlg.destroy()
                    messagebox.showwarning("哎呀", f"下载失败，小鸟迷路了…\n\n{e}\n\n你可以稍后重试，或前往 GitHub 手动下载~")
                self.root.after(0, fail)
        
        threading.Thread(target=download_thread, daemon=True).start()

if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw() # 隐藏主窗口
    
    import os
    icon_path = 'bird.ico'
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller 运行时提取路径
        icon_path = os.path.join(sys._MEIPASS, 'bird.ico')
    elif os.path.exists('bird.ico'):
        icon_path = 'bird.ico'
        
    # 显示干净的 loading 弹窗
    splash = tk.Toplevel(root)
    splash.overrideredirect(True) # 去除边框
    splash_w, splash_h = 240, 160
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    splash.geometry(f"{splash_w}x{splash_h}+{int((screen_w-splash_w)/2)}+{int((screen_h-splash_h)/2)}")
    splash.configure(bg="#E8F5E9")

    try:
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
            from PIL import Image, ImageTk
            img = Image.open(icon_path).resize((64, 64), Image.Resampling.LANCZOS)
            tk_img = ImageTk.PhotoImage(img) # 保留引用
            tk.Label(splash, image=tk_img, bg="#E8F5E9").pack(pady=(20, 10))
    except Exception:
        pass

    tk.Label(splash, text="LOADING...", font=("Microsoft YaHei", 12, "bold"), bg="#E8F5E9", fg="#2E7D32").pack(pady=5)
    
    splash.update() # 渲染 loading 界面

    # 在后台短暂延迟后开始主程序初始化并销毁闪屏
    def init_app():
        app = ImageViewer(root)
        splash.destroy()
        root.deiconify() # 显示出真正的应用窗口
        # 首次打开时后台静默检查更新（无更新时不弹窗）
        root.after(2000, lambda: threading.Thread(target=lambda: app.check_for_updates(silent=True), daemon=True).start())
        
    root.after(100, init_app)
    root.mainloop()
