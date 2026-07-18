import hashlib
import json
import os
import time
from collections import deque


class MessageStore:
    def __init__(
        self,
        path="sent_messages.json",
        max_items=1000,
        reset_interval_seconds=0,
        preserve_latest_count=0,
    ):
        self.path = path
        self.max_items = max_items
        self.reset_interval_seconds = max(0, int(reset_interval_seconds or 0))
        self.preserve_latest_count = max(0, int(preserve_latest_count or 0))
        self.text_hashes = deque(maxlen=max_items)
        self.image_hashes = deque(maxlen=max_items)
        self.file_hashes = deque(maxlen=max_items)
        self.deliveries = {}
        self.completed_order = deque(maxlen=max_items)
        self.protected_keys = deque(maxlen=max(1, self.preserve_latest_count))
        self.last_reset_at = time.time()
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
        self.deliveries = data.get("deliveries", {})
        completed_order = data.get("completed_order")
        if not isinstance(completed_order, list):
            # Dict insertion order gives old registries a useful migration path.
            completed_order = list(reversed(self.deliveries))
        self.completed_order = deque(completed_order, maxlen=self.max_items)
        protected_keys = data.get("protected_keys", [])
        if not isinstance(protected_keys, list):
            protected_keys = []
        self.protected_keys = deque(
            protected_keys,
            maxlen=max(1, self.preserve_latest_count),
        )
        try:
            self.last_reset_at = float(data.get("last_reset_at", self.last_reset_at))
        except (TypeError, ValueError):
            self.last_reset_at = time.time()

        # Old registry files have no reset timestamp. Preserve their contents for
        # the first configured interval and persist the migrated format.
        if "last_reset_at" not in data:
            self.save()
        else:
            self.clear_if_expired()

    def save(self):
        data = {
            "text_hashes": list(self.text_hashes),
            "image_hashes": list(self.image_hashes),
            "file_hashes": list(self.file_hashes),
            "deliveries": self.deliveries,
            "completed_order": list(self.completed_order),
            "protected_keys": list(self.protected_keys),
            "last_reset_at": self.last_reset_at,
        }
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

    def clear_if_expired(self, now=None):
        if self.reset_interval_seconds <= 0:
            return False

        now = time.time() if now is None else float(now)
        if now - self.last_reset_at < self.reset_interval_seconds:
            return False

        protected_keys = [
            key for key in self.protected_keys
            if key in self.deliveries
        ]
        preserved_keys = protected_keys or list(self.completed_order)[:self.preserve_latest_count]
        self.text_hashes.clear()
        self.image_hashes.clear()
        self.file_hashes.clear()
        self.deliveries = {
            content_key: self.deliveries[content_key]
            for content_key in preserved_keys
            if content_key in self.deliveries
        }
        self.completed_order = deque(
            (key for key in preserved_keys if key in self.deliveries),
            maxlen=self.max_items,
        )
        self.protected_keys = deque(
            (key for key in protected_keys if key in self.deliveries),
            maxlen=max(1, self.preserve_latest_count),
        )
        self.last_reset_at = now
        self.save()
        return True

    def set_protected_keys(self, content_keys):
        if self.preserve_latest_count <= 0:
            return False

        unique_keys = []
        for content_key in content_keys or []:
            if not content_key or content_key in unique_keys:
                continue
            if content_key not in self.deliveries:
                continue
            unique_keys.append(content_key)
            if len(unique_keys) >= self.preserve_latest_count:
                break

        if list(self.protected_keys) == unique_keys:
            return False
        self.protected_keys = deque(
            unique_keys,
            maxlen=max(1, self.preserve_latest_count),
        )
        self.save()
        return True

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
        self.clear_if_expired()
        if not message_hash or self.has_text_hash(message_hash):
            return False
        self.text_hashes.appendleft(message_hash)
        self.save()
        return True

    def mark_image(self, image_hash):
        self.clear_if_expired()
        if not image_hash or self.has_image(image_hash):
            return False
        self.image_hashes.appendleft(image_hash)
        self.save()
        return True

    def mark_file(self, file_hash):
        self.clear_if_expired()
        if not file_hash or self.has_file(file_hash):
            return False
        self.file_hashes.appendleft(file_hash)
        self.save()
        return True

    def has_text(self, text):
        message_hash = hash_text(text)
        return self.has_text_hash(message_hash)

    def has_text_hash(self, message_hash):
        self.clear_if_expired()
        return bool(message_hash and message_hash in self.text_hashes)

    def has_image(self, image_hash):
        self.clear_if_expired()
        return bool(image_hash and image_hash in self.image_hashes)

    def has_file(self, file_hash):
        self.clear_if_expired()
        return bool(file_hash and file_hash in self.file_hashes)

    def mark_delivered(self, content_key, target):
        self.clear_if_expired()
        if not content_key or target is None:
            return False
        target_key = normalize_target_key(target)
        targets = set(self.deliveries.get(content_key, []))
        if target_key in targets:
            return False
        targets.add(target_key)
        self.deliveries[content_key] = sorted(targets)
        self.save()
        return True

    def has_delivery(self, content_key, target):
        self.clear_if_expired()
        if not content_key or target is None:
            return False
        return normalize_target_key(target) in set(self.deliveries.get(content_key, []))

    def mark_completed(self, content_key):
        self.clear_if_expired()
        if not content_key:
            return False
        try:
            self.completed_order.remove(content_key)
        except ValueError:
            pass
        self.completed_order.appendleft(content_key)
        self.save()
        return True

    def delivered_to_all(self, content_key, targets):
        self.clear_if_expired()
        if not targets:
            return False
        delivered = set(self.deliveries.get(content_key, []))
        return all(normalize_target_key(target) in delivered for target in targets)


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


def normalize_target_key(target):
    return str(target).strip()
