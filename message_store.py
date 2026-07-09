import hashlib
import json
import os
from collections import deque


class MessageStore:
    def __init__(self, path="sent_messages.json", max_items=1000):
        self.path = path
        self.max_items = max_items
        self.text_hashes = deque(maxlen=max_items)
        self.image_hashes = deque(maxlen=max_items)
        self.file_hashes = deque(maxlen=max_items)
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return

        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return

        self.text_hashes = deque(data.get("text_hashes", []), maxlen=self.max_items)
        self.image_hashes = deque(data.get("image_hashes", []), maxlen=self.max_items)
        self.file_hashes = deque(data.get("file_hashes", []), maxlen=self.max_items)

    def save(self):
        data = {
            "text_hashes": list(self.text_hashes),
            "image_hashes": list(self.image_hashes),
            "file_hashes": list(self.file_hashes),
        }
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

    def is_new_text(self, text):
        message_hash = hash_text(text)
        if not message_hash or self.has_text_hash(message_hash):
            return False
        return True

    def is_new_image(self, image_hash):
        if not image_hash or self.has_image(image_hash):
            return False
        return True

    def mark_text(self, text):
        message_hash = hash_text(text)
        return self.mark_text_hash(message_hash)

    def mark_text_hash(self, message_hash):
        if not message_hash or self.has_text_hash(message_hash):
            return False
        self.text_hashes.appendleft(message_hash)
        self.save()
        return True

    def mark_image(self, image_hash):
        if not image_hash or self.has_image(image_hash):
            return False
        self.image_hashes.appendleft(image_hash)
        self.save()
        return True

    def mark_file(self, file_hash):
        if not file_hash or self.has_file(file_hash):
            return False
        self.file_hashes.appendleft(file_hash)
        self.save()
        return True

    def has_text(self, text):
        message_hash = hash_text(text)
        return self.has_text_hash(message_hash)

    def has_text_hash(self, message_hash):
        return bool(message_hash and message_hash in self.text_hashes)

    def has_image(self, image_hash):
        return bool(image_hash and image_hash in self.image_hashes)

    def has_file(self, file_hash):
        return bool(file_hash and file_hash in self.file_hashes)


def normalize_text(text):
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip("\n")


def hash_text(text):
    normalized = normalize_text(text)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def hash_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
