import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

SECRET_KEY = os.getenv("ENCRYPTION_KEY")
if not SECRET_KEY:
    raise ValueError("FATAL ERROR: ENCRYPTION_KEY is missing in your .env file!")

cipher_suite = Fernet(SECRET_KEY.encode())

def encrypt_data(data: str) -> str:
    if not data:
        return ""
    encrypted_bytes = cipher_suite.encrypt(data.encode('utf-8'))
    return encrypted_bytes.decode('utf-8')

def decrypt_data(encrypted_data: str) -> str:
    if not encrypted_data:
        return ""
    decrypted_bytes = cipher_suite.decrypt(encrypted_data.encode('utf-8'))
    return decrypted_bytes.decode('utf-8')
