# ===== 初期設定 =====
import os
import json
import http.server
import requests
import threading
import logging
import random
import time
import face_recognition
import cv2
import google.generativeai as genai

# 事前に用意した顔画像を "known_faces" フォルダに配置
# ファイル名は「たけし.jpg」「はなこ.jpg」のようにひらがなにする
known_encodings = []
known_names = []
for file in os.listdir("known_faces"):
    if file.lower().endswith('.jpg'):
        name = os.path.splitext(file)[0]
        image = face_recognition.load_image_file(f"known_faces/{file}")
        enc = face_recognition.face_encodings(image)
        if enc:
            known_encodings.append(enc[0])
            known_names.append(name)

# カメラから1フレーム取得して顔認識
video_capture = cv2.VideoCapture(0)
ret, frame = video_capture.read()
video_capture.release()
current_speaker = "ゲスト"
rgb_frame = frame[:, :, ::-1]
face_locations = face_recognition.face_locations(rgb_frame)
face_encodings = face_recognition.face_encodings(rgb_frame, face_locations)
for face_encoding in face_encodings:
    matches = face_recognition.compare_faces(known_encodings, face_encoding)
    if True in matches:
        idx = matches.index(True)
        current_speaker = known_names[idx]
        break

# Gemini API 設定
genai.configure(api_key="API_KEY")
model = genai.GenerativeModel(model_name="models/gemini-pro")

api_url = "https://platform-api.bocco.me"
event_name = "message.received"
robot_user_type = "emo"
conversation_count = 1  # 質問→共感の2ターン

user_spoke = False
user_utterance = None
webhook_secret = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ===== ファイル読み込み =====
def load_random_question(filename="questions.json"):
    with open(filename, "r", encoding="utf-8") as f:
        data = json.load(f)
    return random.choice(data["questions"])

def load_memory(filename="memory.json"):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

# ===== 記憶管理 =====
def save_favorite_to_file(name, category, value, filename="memory.json"):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            memory = json.load(f)
    except FileNotFoundError:
        memory = {}
    memory.setdefault(name, {})[category] = value
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2, ensure_ascii=False)

# ===== BOCCO API =====
def http_get(endpoint, headers):
    url = f"{api_url}{endpoint}"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    logging.info(f"API Response: {resp.json()}")
    return resp.json()

def http_post(endpoint, headers, data):
    url = f"{api_url}{endpoint}"
    resp = requests.post(url, headers=headers, json=data)
    resp.raise_for_status()
    logging.info(f"API Response: {resp.json()}")
    return resp.json()

def http_put(endpoint, headers, data):
    url = f"{api_url}{endpoint}"
    resp = requests.put(url, headers=headers, json=data)
    resp.raise_for_status()
    logging.info(f"API Response: {resp.json()}")
    return resp.json()

def get_room_id():
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = http_get("/v1/rooms", headers)
    for room in resp.get("rooms", []):
        if room.get("name") == room_name:
            return room.get("uuid")
    return None


def create_webhook():
    headers = {"Authorization": f"Bearer {access_token}", 'Content-Type': 'application/json'}
    data = {"description": "my webhook", "url": webhook_url}
    resp = http_post("/v1/webhook", headers, data)
    return resp.get("secret") if resp else None


def set_webhook_events():
    headers = {"Authorization": f"Bearer {access_token}", 'Content-Type': 'application/json'}
    data = {"events": [event_name]}
    return http_put("/v1/webhook/events", headers, data)


def send_message(room_id, msg):
    headers = {"Authorization": f"Bearer {access_token}", 'Content-Type': 'application/json'}
    data = {"text": msg, "immediate": True}
    resp = http_post(f"/v1/rooms/{room_id}/messages/text", headers, data)
    return resp.get("message", {}).get("ja") if resp else None

# ===== Webhook Handler =====
class Handler(http.server.BaseHTTPRequestHandler):
    def _send_status(self, status):
        self.send_response(status)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_POST(self):
        global user_utterance, user_spoke
        if webhook_secret is None or webhook_secret != self.headers.get("X-Platform-Api-Secret"):
            self._send_status(401)
            return
        length = int(self.headers.get('Content-length', 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        logging.info(f"received webhook: {body}")
        if body.get("event") == event_name and body["data"]["message"]["user"]["user_type"] == robot_user_type:
            user_utterance = body["data"]["message"]["message"]["ja"]
            user_spoke = True
        self._send_status(200)

# ===== 発話生成 =====
def generate_robot_utterance(count, question, user_response, memory):
    if count == 0:
        return question
    elif count == 1:
        mem_json = json.dumps(memory, ensure_ascii=False)
        prompt = (
            f"あなたは親しみやすい対話ロボットです。質問:『{question}』に対して"
            f"ユーザーが『{user_response}』と答えました。これまでの回答履歴: {mem_json}。"
            f"これらを踏まえて、特に{current_speaker}さんの視点を活かした共感的な返答を一文で作ってください。"
        )
        try:
            resp = model.generate_content([prompt])
            return resp.text.strip()
        except Exception as e:
            logging.error(f"Gemini生成エラー(共感): {e}")
            return f"{user_response}なんだね。教えてくれてありがとう"
    return None

# ===== メイン処理 =====
def main():
    global webhook_secret, user_spoke
    room_id = get_room_id()
    if not room_id:
        logging.error("get_room_id failed")
        return
    webhook_secret = create_webhook()
    if not webhook_secret:
        logging.error("create_webhook failed")
        return
    if not set_webhook_events():
        logging.error("set_webhook_events failed")
        return
    threading.Thread(target=lambda: http.server.HTTPServer(("", 8000), Handler).serve_forever(), daemon=True).start()

    question = load_random_question()
    memory = load_memory()

    count = 0
    keyword = ""
    while count <= conversation_count:
        msg = generate_robot_utterance(count, question, keyword, memory)
        if not msg:
            break
        send_message(room_id, msg)
        if count == conversation_count:
            break
        while not user_spoke:
            pass
        user_spoke = False
        if count == 0:
            keyword = extract_keyword(user_utterance)
            save_favorite_to_file(current_speaker, question, keyword)
            memory = load_memory()
        count += 1

if __name__ == "__main__":
    main()