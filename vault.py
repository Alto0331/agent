# -*- coding: utf-8 -*-
"""
vault.py — Coffre fort de clés synchronisé par le mail de l'agent.

- Chiffrement côté client AES-256-GCM (lib `cryptography`) avec clé dérivée
  du mot de passe maître (PBKDF2-HMAC-SHA256, 200 000 itérations).
- Transport : envoi/réception UNIQUEMENT via le compte Gmail configuré par
  l'utilisateur dans les Réglages (terra_mail_address + mot de passe d'application).
- Aucune donnée ne transite par GitHub ou tout autre service : le coffre est
  une pièce jointe chiffrée dans un mail envoyé par l'agent à lui-même.
- Portable entre VM : le coffre contient les secrets en clair (chiffrés par
  le mot de passe maître), indépendants du chiffrement DPAPI local à la VM.
"""

import base64
import json
import os
import smtplib
import ssl
import time
import imaplib
import email
from email.message import EmailMessage

VAULT_SUBJECT = "[COFFRE AGENT]"
VAULT_FILENAME = "coffre_agent.vault"
PBKDF2_ITERATIONS = 200_000
SMTP_GMAIL = ("smtp.gmail.com", 465)
IMAP_GMAIL = ("imap.gmail.com", 993)

# Champs sauvegardés dans le coffre (les anciennes clés uniques + multi-hôtes + réglages)
VAULT_FIELDS = [
    "openrouter_key", "mistral_api_key", "extra_api_name", "extra_api_key",
    "oauth_token_or_path",
    "main_account_email", "main_account_password",
    "terra_mail_address", "terra_mail_app_password", "terra_authorized_sender",
    "terra_mail_enabled",
    "use_ollama", "ollama_url", "ollama_model", "ollama_api_key",
    "engine_mode",
    "proxmox_enabled", "proxmox_url", "proxmox_token", "proxmox_secret",
    "ntfy_enabled", "ntfy_url", "ntfy_topic",
    "announce_ip", "alert_email",
    "watched_services", "watchdog_interval_min",
]


# ==============================================================================
# CHIFFREMENT
# ==============================================================================

def _crypto_lib():
    """Importe la lib cryptography uniquement à l'usage (le serveur démarre même
    si elle n'est pas encore installée ; erreur claire à l'usage du coffre)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        return AESGCM, PBKDF2HMAC, hashes
    except ImportError:
        raise ImportError("Lib 'cryptography' non installée : elle s'installe automatiquement au démarrage du serveur (pip install cryptography). Relance agent_server.py.")


def _derive_key(master_password: str, salt: bytes) -> bytes:
    """Dérive une clé AES-256 du mot de passe maître (PBKDF2-SHA256, 200k itérations)."""
    _, PBKDF2HMAC, hashes = _crypto_lib()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(master_password.encode("utf-8"))


def encrypt_vault(data: dict, master_password: str) -> bytes:
    """Chiffre le contenu du coffre : salt(16) + nonce(12) + AES-256-GCM(JSON)."""
    if not master_password or len(master_password) < 8:
        raise ValueError("Le mot de passe maître doit faire au moins 8 caractères.")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(master_password, salt)
    AESGCM, _, _ = _crypto_lib()
    plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
    cipher = AESGCM(key).encrypt(nonce, plaintext, None)
    return salt + nonce + cipher


def decrypt_vault(blob: bytes, master_password: str) -> dict:
    """Déchiffre le coffre. Lève une exception explicite si le mot de passe est faux."""
    if len(blob) < 16 + 12 + 16:
        raise ValueError("Coffre invalide ou corrompu.")
    salt, nonce, cipher = blob[:16], blob[16:28], blob[28:]
    key = _derive_key(master_password, salt)
    AESGCM, _, _ = _crypto_lib()
    try:
        plaintext = AESGCM(key).decrypt(nonce, cipher, None)
    except Exception:
        raise ValueError("Mot de passe maître incorrect ou coffre corrompu.")
    return json.loads(plaintext.decode("utf-8"))


# ==============================================================================
# CONSTRUCTION DU CONTENU
# ==============================================================================

def build_vault_payload(creds: dict, trusted_senders: list = None) -> dict:
    """Construit le contenu du coffre depuis les credentials (secrets en clair,
    chiffrés ensuite par le mot de passe maître — portable entre VM)."""
    payload = {"_format": "agent-vault-1", "_created": time.strftime("%d/%m/%Y %H:%M")}
    for f in VAULT_FIELDS:
        if creds.get(f) not in (None, ""):
            payload[f] = creds[f]
    # Multi-hôtes Proxmox : copie profonde avec secrets en clair
    hosts = creds.get("proxmox_hosts")
    if isinstance(hosts, list) and hosts:
        payload["proxmox_hosts"] = [
            {"name": h.get("name", "proxmox"), "url": h.get("url", ""),
             "token_id": h.get("token_id", ""), "token_secret": h.get("token_secret", "")}
            for h in hosts if isinstance(h, dict)
        ]
    if trusted_senders:
        payload["trusted_senders"] = list(trusted_senders)
    return payload


def apply_vault_payload(vault_data: dict) -> dict:
    """Filtre le contenu déchiffré pour ne garder que les champs connus du coffre."""
    out = {}
    for f in VAULT_FIELDS + ["proxmox_hosts", "trusted_senders"]:
        if f in vault_data:
            out[f] = vault_data[f]
    return out


# ==============================================================================
# TRANSPORT GMAIL (SMTP/IMAP — uniquement le mail configuré par l'utilisateur)
# ==============================================================================

def _check_mail_config(creds: dict):
    addr = (creds.get("terra_mail_address") or "").strip()
    pwd = (creds.get("terra_mail_app_password") or "").replace(" ", "").strip()
    if not addr or not pwd:
        raise ValueError("Mail de l'agent non configuré : renseigne l'adresse Gmail et le mot de passe d'application dans les Réglages.")
    return addr, pwd


def send_vault_via_gmail(creds: dict, master_password: str, trusted_senders: list = None) -> str:
    """Construit, chiffre et envoie le coffre par mail à l'adresse de l'agent elle-même.
    Retourne un message de confirmation en français."""
    addr, pwd = _check_mail_config(creds)
    payload = build_vault_payload(creds, trusted_senders)
    blob = encrypt_vault(payload, master_password)
    b64 = base64.b64encode(blob).decode("ascii")

    msg = EmailMessage()
    msg["Subject"] = VAULT_SUBJECT + " " + time.strftime("%d/%m/%Y %H:%M")
    msg["From"] = addr
    msg["To"] = addr
    msg.set_content(
        "Coffre fort de l'agent — pièce jointe chiffrée AES-256-GCM.\n"
        "Ce mail est envoyé automatiquement par l'agent pour sauvegarder ses clés.\n"
        "Le contenu est illisible sans le mot de passe maître.\n"
        "Pour restaurer : Réglages → Coffre → Restaurer (sur n'importe quelle VM avec ce même mail)."
    )
    msg.add_attachment(b64.encode("ascii"), maintype="application", subtype="octet-stream", filename=VAULT_FILENAME)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_GMAIL[0], SMTP_GMAIL[1], context=ctx) as smtp:
        smtp.login(addr, pwd)
        smtp.send_message(msg)
    return (f"✅ Coffre sauvegardé ({len(blob)} octets chiffrés) et envoyé à {addr}. "
            f"Pour restaurer sur une autre VM : configure le même mail, puis Réglages → Coffre → Restaurer.")


def fetch_latest_vault_blob(creds: dict) -> tuple:
    """Recherche le dernier mail [COFFRE AGENT] et retourne (date, blob_chiffré).
    Lève une exception explicite si aucun coffre trouvé."""
    addr, pwd = _check_mail_config(creds)
    ctx = ssl.create_default_context()
    with imaplib.IMAP4_SSL(IMAP_GMAIL[0], IMAP_GMAIL[1], ssl_context=ctx) as imap:
        imap.login(addr, pwd)
        imap.select("INBOX")
        status, data = imap.search(None, f'SUBJECT "{VAULT_SUBJECT}"')
        if status != "OK" or not data[0]:
            raise ValueError("Aucun coffre trouvé dans la boîte mail. Sauvegarde-le d'abord (Réglages → Coffre → Sauvegarder).")
        ids = data[0].split()
        # Parcourt du plus récent au plus ancien
        for num in reversed(ids):
            status, msg_data = imap.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            for part in msg.walk():
                if part.get_filename() == VAULT_FILENAME:
                    b64 = part.get_payload(decode=True)
                    if not b64:
                        continue
                    blob = base64.b64decode(b64)
                    date = email.utils.parsedate_to_datetime(msg.get("Date"))
                    return date, blob
    raise ValueError("Un mail de coffre existe mais la pièce jointe est introuvable.")


def restore_vault_via_gmail(creds: dict, master_password: str) -> tuple:
    """Récupère le dernier coffre depuis le mail, le déchiffre et retourne
    (message, champs_à_appliquer). Les secrets sont en clair (à re-chiffrer DPAPI par save_credentials)."""
    date, blob = fetch_latest_vault_blob(creds)
    data = decrypt_vault(blob, master_password)
    fields = apply_vault_payload(data)
    created = data.get("_created", "?")
    return (f"✅ Coffre restauré (sauvegardé le {created}, reçu le {date.strftime('%d/%m/%Y %H:%M') if date else '?'}). "
            f"{len(fields)} champs récupérés — appliqués et re-chiffrés pour cette VM."), fields
