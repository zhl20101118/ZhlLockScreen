import cv2
import os
import time
import sys

save_dir = "ref_images"
os.makedirs(save_dir, exist_ok=True)

cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
if not cap.isOpened():
    print("[ERROR] 无法打开摄像头")
    sys.exit(1)

# ------------------------ 加载 YuNet 人脸检测模型 ------------------------
YUNET_MODEL = "model/face_detection_yunet_2023mar.onnx"
if not os.path.exists(YUNET_MODEL):
    print(f"[ERROR] 未找到 YuNet 模型: {YUNET_MODEL}")
    print("请确保 model/face_detection_yunet_2023mar.onnx 存在")
    sys.exit(1)

detector = cv2.FaceDetectorYN.create(YUNET_MODEL, "", (320, 320))
print("[INFO] YuNet 人脸检测器加载成功")

print("=" * 50)
print("【智能训练照片采集器】")
print("功能：自动检测人脸并保存，支持手动/自动抓拍")
print("操作：")
print("  - 保持人脸在绿色框内")
print("  - 按 SPACE 手动抓拍")
print("  - 自动模式：每2秒保存一张（需检测到人脸）")
print("  - 按 ESC 退出")
print("提示：请采集不同微角度（左/右偏＜30°）、不同表情（微笑/严肃）")
print("      确保光线均匀，面部清晰，背景简单")
print("=" * 50)

count = 0
last_cap_time = 0

while True:
    ret, frame = cap.read()
    if not ret:
        continue

    h, w = frame.shape[:2]
    detector.setInputSize((w, h))
    result = detector.detect(frame)

    faces = []
    if result[1] is not None:
        # 将 YuNet 输出的 face 转成 (x, y, w, h) 矩形框
        for face in result[1]:
            x, y, width, height = face[0:4].astype(int)
            faces.append((x, y, width, height))
            cv2.rectangle(frame, (x, y), (x+width, y+height), (0, 255, 0), 2)

    # 显示信息
    cv2.putText(frame, f"Saved: {count}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    cv2.putText(frame, "SPACE: save  |  ESC: quit", (10, 65),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    cv2.imshow("Train Image Collector", frame)

    key = cv2.waitKey(1) & 0xFF
    now = time.time()

    if len(faces) > 0:
        # 手动抓拍或自动抓拍
        if key == 32 or (now - last_cap_time >= 2):
            # 取面积最大的人脸
            x, y, w, h = max(faces, key=lambda r: r[2]*r[3])
            face_img = frame[y:y+h, x:x+w]
            # 保存
            filename = os.path.join(save_dir, f"me_{count:04d}.jpg")
            cv2.imwrite(filename, face_img)
            print(f"已保存: {filename}")
            count += 1
            last_cap_time = now

    if key == 27:  # ESC
        break

cap.release()
cv2.destroyAllWindows()
print(f"\n共采集 {count} 张照片，保存在 '{save_dir}' 文件夹。")
print("现在请重新运行识别程序。")

