import os

from cryptography.fernet import Fernet

os.environ["CHAT_ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
