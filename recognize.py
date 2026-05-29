# recognize.py - 人脸识别 + 可靠结束进程（纯 ctypes，无 taskkill）
import cv2
import numpy as np
import time
import os
import sys
import ctypes
from ctypes import wintypes
from collections import deque

# ======================== 模型路径 ========================
YUNET_MODEL = "model/face_detection_yunet_2023mar.onnx"
SFACE_MODEL = "model/face_recognition_sface_2021dec.onnx"

if not os.path.exists(YUNET_MODEL):
    print(f"[ERROR] 缺少 Yunet 模型: {YUNET_MODEL}")
    sys.exit(1)
if not os.path.exists(SFACE_MODEL):
    print(f"[ERROR] 缺少 SFace 模型: {SFACE_MODEL}")
    sys.exit(1)

detector = cv2.FaceDetectorYN.create(YUNET_MODEL, "", (320, 320))
feature_extractor = cv2.FaceRecognizerSF.create(SFACE_MODEL, "")
print("[INFO] 模型加载成功")

# ======================== 人脸对齐 ========================
def align_face(img, face):
    std_points = np.array([
        [30.2946, 51.6963], [65.5318, 51.6963], [48.0252, 71.7366],
        [33.5493, 92.3655], [62.7299, 92.3655]
    ], dtype=np.float32)
    landmarks = face[4:14].reshape(5, 2).astype(np.float32)
    src_points = np.array([landmarks[0], landmarks[1], landmarks[2], landmarks[4], landmarks[3]])
    M, _ = cv2.estimateAffine2D(src_points, std_points)
    return cv2.warpAffine(img, M, (112, 112))

# ======================== 加载参考特征 ========================
def get_ref_feat():
    features = []
    if os.path.exists("my_photo.jpg"):
        img = cv2.imread("my_photo.jpg")
        if img is not None:
            h, w = img.shape[:2]
            detector.setInputSize((w, h))
            result = detector.detect(img)
            if result[1] is not None and result[1].shape[0] > 0:
                aligned = align_face(img, result[1][0])
                features.append(feature_extractor.feature(aligned).ravel())
                print("[INFO] 已添加 my_photo.jpg 特征")
    ref_dir = "ref_images"
    if os.path.exists(ref_dir):
        for f in sorted(os.listdir(ref_dir)):
            if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                img = cv2.imread(os.path.join(ref_dir, f))
                if img is None: continue
                h, w = img.shape[:2]
                detector.setInputSize((w, h))
                result = detector.detect(img)
                if result[1] is not None and result[1].shape[0] > 0:
                    aligned = align_face(img, result[1][0])
                    features.append(feature_extractor.feature(aligned).ravel())
    if not features:
        return None
    avg_feat = np.mean(features, axis=0)
    norm = np.linalg.norm(avg_feat)
    if norm > 0:
        avg_feat /= norm
    print(f"[INFO] 使用 {len(features)} 张照片的平均特征")
    return avg_feat.reshape(1, -1)

ref_feat = get_ref_feat()
if ref_feat is None:
    print("[ERROR] 未找到参考人脸")
    sys.exit(1)

THRESHOLD = 0.7
NOD_WINDOW_SEC = 2.0
NOD_DIFF_THRESH = 10.0
ASPECT_RATIO_CHANGE_MAX = 0.1

# ======================== 鼠标移动检测 ========================
user32 = ctypes.windll.user32
point = wintypes.POINT()
last_x, last_y = -1, -1

def mouse_moved():
    global last_x, last_y
    user32.GetCursorPos(ctypes.byref(point))
    if last_x == -1 and last_y == -1:
        last_x, last_y = point.x, point.y
        return False
    moved = (point.x != last_x) or (point.y != last_y)
    last_x, last_y = point.x, point.y
    return moved

# ======================== 强力进程终止（纯 ctypes，带重试和确认）=======================
def terminate_process_by_name(process_name):
    """
    终止所有匹配进程名（不区分大小写）的进程。
    返回 True 表示至少成功终止一个。
    """
    kernel32 = ctypes.windll.kernel32
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_INFORMATION = 0x0400
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = -1

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260)
        ]

    killed = False
    # 最多尝试 3 次，每次间隔 0.2 秒
    for attempt in range(3):
        hSnapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if hSnapshot == INVALID_HANDLE_VALUE:
            continue

        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(hSnapshot, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() == process_name.lower():
                    hProcess = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_INFORMATION,
                                                    False, pe.th32ProcessID)
                    if hProcess:
                        if kernel32.TerminateProcess(hProcess, 0):
                            print(f"[INFO] 已终止进程 {process_name} (PID: {pe.th32ProcessID})")
                            killed = True
                        else:
                            print(f"[WARN] 无法终止进程 {process_name} (PID: {pe.th32ProcessID})，可能权限不足")
                        kernel32.CloseHandle(hProcess)
                    else:
                        print(f"[WARN] 无法打开进程 {process_name} (PID: {pe.th32ProcessID})，尝试以管理员身份运行")
                if not kernel32.Process32NextW(hSnapshot, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(hSnapshot)

        if killed:
            # 等待并确认进程是否真的消失
            time.sleep(0.1)
            if not is_process_running(process_name):
                return True
            else:
                print(f"[INFO] 进程 {process_name} 仍存在，重试...")
        else:
            print(f"[INFO] 未找到进程 {process_name} (尝试 {attempt+1}/3)")
        time.sleep(0.2)
    return killed

def is_process_running(process_name):
    """检查是否存在至少一个指定名称的进程"""
    kernel32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002
    hSnapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if hSnapshot == -1:
        return False
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    found = False
    if kernel32.Process32FirstW(hSnapshot, ctypes.byref(pe)):
        while True:
            if pe.szExeFile.lower() == process_name.lower():
                found = True
                break
            if not kernel32.Process32NextW(hSnapshot, ctypes.byref(pe)):
                break
    kernel32.CloseHandle(hSnapshot)
    return found

# ======================== 输入法切换 ========================
def switch_to_sogou_or_ms_pinyin():
    def enum_keyboard_layouts():
        num = user32.GetKeyboardLayoutList(0, None)
        if num == 0:
            return []
        hkls = (wintypes.HKL * num)()
        user32.GetKeyboardLayoutList(num, hkls)
        return list(hkls)

    for hkl in enum_keyboard_layouts():
        name_buf = ctypes.create_unicode_buffer(9)
        if user32.GetKeyboardLayoutNameW(name_buf):
            if "sogou" in name_buf.value.lower():
                user32.ActivateKeyboardLayout(hkl, 0)
                return True
    ms_hkl = user32.LoadKeyboardLayoutW("00000804", 1)
    if ms_hkl:
        user32.ActivateKeyboardLayout(ms_hkl, 0)
        return True
    return False

# ======================== 30秒识别窗口 ========================
def run_recognition_window():
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print("[ERROR] 无法打开摄像头")
        return False

    end_time = time.time() + 30
    print("[INFO] 识别窗口开启30秒，请面对摄像头并点头")
    history = deque(maxlen=300)
    verify_frames_left = 0

    while time.time() < end_time:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.01)
            continue

        h, w = frame.shape[:2]
        detector.setInputSize((w, h))
        _, faces = detector.detect(frame)

        current_time = time.time()
        face = None
        if faces is not None:
            face = faces[0]
            bbox = face[:4].astype(int)
            aspect_ratio = bbox[2] / bbox[3]
            pitch = face[5]
            history.append((current_time, pitch, aspect_ratio))

        while history and history[0][0] < current_time - NOD_WINDOW_SEC:
            history.popleft()

        if verify_frames_left == 0 and len(history) >= 5:
            pitches = [p for _, p, _ in history]
            aspects = [a for _, _, a in history]
            if (max(pitches) - min(pitches) >= NOD_DIFF_THRESH and
                max(aspects) - min(aspects) <= ASPECT_RATIO_CHANGE_MAX):
                verify_frames_left = 4

        if verify_frames_left > 0 and face is not None:
            aligned = align_face(frame, face)
            feat = feature_extractor.feature(aligned)
            sim = feature_extractor.match(ref_feat, feat, cv2.FaceRecognizerSF_FR_COSINE)
            if sim >= THRESHOLD:
                cap.release()
                return True
            verify_frames_left -= 1
        elif verify_frames_left > 0:
            verify_frames_left -= 1

        time.sleep(0.01)

    cap.release()
    return False

# ======================== 主循环 ========================
def main():
    print("[INFO] 程序已启动，移动鼠标触发30秒人脸识别")
    print("[INFO] 识别成功后立即结束 lockscreen.exe，然后切换输入法并退出")
    while True:
        if mouse_moved():
            print(f"[{time.strftime('%H:%M:%S')}] 检测到鼠标移动，启动识别")
            if run_recognition_window():
                print(f"[{time.strftime('%H:%M:%S')}] 识别成功！正在终止 lockscreen.exe...")
                success = terminate_process_by_name("lockscreen.exe")
                if success:
                    print("[INFO] lockscreen.exe 已成功终止")
                else:
                    print("[ERROR] 未能终止 lockscreen.exe，请以管理员身份运行此程序")
                switch_to_sogou_or_ms_pinyin()
                print("[INFO] 任务完成，退出")
                break
            else:
                print(f"[{time.strftime('%H:%M:%S')}] 30秒内未识别，继续等待")
        else:
            time.sleep(0.02)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] 用户中断")
    finally:
        cv2.destroyAllWindows()
