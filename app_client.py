# -*- coding: utf-8 -*-
"""
app_client.py — Interface Client (Bulle flottante 🤖 + Connexion IP VM + Switch Terra/Lite/Local + Menu Paramètres par onglets)
"""

import os
import json
import socket
import threading
import tkinter as tk
from tkinter import ttk, filedialog

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_config.json")
DEFAULT_PORT = 65432


class AgentClientApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Assistant VM")
        self.root.geometry("450x650")
        self.root.configure(bg="#111827")
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)  # Fenêtre sans bordure Windows classique

        # État réseau & configuration
        self.sock: socket.socket | None = None
        self.connected = False
        self.submenu_visible = False
        self.is_minimized_to_bubble = False

        # Coordonnées pour le glisser-déposer de la fenêtre et de la bulle
        self._drag_x = 0
        self._drag_y = 0
        self._bubble_moved = False

        # Chargement de la config locale (IP mémorisée, paramètres, clés, etc.)
        self.config = self.load_local_config()
        self.engine_mode = "local" if self.config.get("use_ollama", False) else self.config.get("engine_mode", "terra")

        # Configuration du style visuel des onglets (Notebook)
        self._setup_ttk_styles()

        # Construction des interfaces
        self._build_main_window()
        self._build_floating_bubble()
        self._refresh_mode_button_ui()

        # Connexion automatique au démarrage si une IP est déjà connue
        if self.config.get("vm_ip"):
            self.root.after(400, self.connect_to_vm)

    # ==========================================================================
    # 1. GESTION DE LA CONFIGURATION LOCALE & STYLES
    # ==========================================================================
    def load_local_config(self) -> dict:
        default_cfg = {
            "vm_ip": "192.168.1.100",
            "port": DEFAULT_PORT,
            "engine_mode": "terra",
            # Catégorie 1 : Compte principal utilisateur (optionnel)
            "main_account_email": "",
            "main_account_password": "",
            # Catégorie 2 : Clés API & OAuth
            "openrouter_key": "",
            "oauth_token_or_path": "",
            "extra_api_name": "Tavily / Google AI",
            "extra_api_key": "",
            # Catégorie 3 : Modèle Local Ollama
            "use_ollama": False,
            "ollama_url": "http://localhost:11434",
            "ollama_model": "qwen2.5-vl:7b",
            # Catégorie 4 : Boîte Mail dédiée à Terra (Pont IMAP/SMTP 16 caractères)
            "terra_mail_enabled": True,
            "terra_mail_address": "",
            "terra_mail_app_password": "",
            "terra_authorized_sender": "",
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Migration douce si l'ancien format de config existait
                    if "mail_address" in data and not data.get("main_account_email"):
                        data["main_account_email"] = data.get("mail_address", "")
                    if "mail_password" in data and not data.get("main_account_password"):
                        data["main_account_password"] = data.get("mail_password", "")
                    default_cfg.update(data)
            except Exception:
                pass
        return default_cfg

    def save_local_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[Config] Erreur sauvegarde : {e}")

    def _setup_ttk_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("default")
        except Exception:
            pass
        style.configure("Dark.TNotebook", background="#1e293b", borderwidth=0)
        style.configure(
            "Dark.TNotebook.Tab",
            background="#0f172a",
            foreground="#94a3b8",
            padding=[8, 4],
            font=("Segoe UI", 8, "bold"),
            borderwidth=0,
        )
        style.map(
            "Dark.TNotebook.Tab",
            background=[("selected", "#0284c7"), ("active", "#334155")],
            foreground=[("selected", "#ffffff"), ("active", "#f8fafc")],
        )

    # ==========================================================================
    # 2. CONSTRUCTION DE LA FENÊTRE PRINCIPALE
    # ==========================================================================
    def _build_main_window(self):
        # --- BARRE DE TITRE PERSONNALISÉE ---
        self.title_bar = tk.Frame(self.root, bg="#1f2937", height=36)
        self.title_bar.pack(fill=tk.X, side=tk.TOP)

        self.status_dot = tk.Label(self.title_bar, text="●", fg="#ef4444", bg="#1f2937", font=("Segoe UI", 12, "bold"))
        self.status_dot.pack(side=tk.LEFT, padx=(10, 4))

        self.title_label = tk.Label(
            self.title_bar, text="Assistant VM", fg="#f9fafb", bg="#1f2937", font=("Segoe UI", 10, "bold")
        )
        self.title_label.pack(side=tk.LEFT)

        # Bouton fermeture complète
        btn_close = tk.Button(
            self.title_bar, text="✕", bg="#1f2937", fg="#9ca3af", bd=0,
            activebackground="#dc2626", activeforeground="white", font=("Segoe UI", 10),
            command=self.quit_app
        )
        btn_close.pack(side=tk.RIGHT, padx=(2, 8), pady=4)

        # Bouton réduction en bulle flottante 🤖
        btn_min = tk.Button(
            self.title_bar, text="—", bg="#1f2937", fg="#9ca3af", bd=0,
            activebackground="#374151", activeforeground="white", font=("Segoe UI", 10, "bold"),
            command=self.minimize_to_bubble
        )
        btn_min.pack(side=tk.RIGHT, padx=2, pady=4)

        # Bouton Menu Paramètres complets
        self.btn_submenu = tk.Button(
            self.title_bar, text="⚙️ Paramètres", bg="#374151", fg="#e5e7eb", bd=0,
            activebackground="#4b5563", activeforeground="white", font=("Segoe UI", 8, "bold"),
            padx=10, pady=2, command=self.toggle_submenu
        )
        self.btn_submenu.pack(side=tk.RIGHT, padx=6, pady=5)

        # Déplacement de la fenêtre via la barre de titre
        for widget in (self.title_bar, self.title_label):
            widget.bind("<ButtonPress-1>", self._start_window_drag)
            widget.bind("<B1-Motion>", self._do_window_drag)

        # --- BARRE D'ADRESSE IP DE LA VM + SWITCH MOTEUR (TERRA / LITE / LOCAL) ---
        self.ip_bar = tk.Frame(self.root, bg="#182232", pady=6, padx=8)
        self.ip_bar.pack(fill=tk.X, side=tk.TOP)

        tk.Label(self.ip_bar, text="IP VM :", fg="#9ca3af", bg="#182232", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)

        self.ip_var = tk.StringVar(value=self.config.get("vm_ip", "192.168.1.100"))
        self.ip_entry = tk.Entry(
            self.ip_bar, textvariable=self.ip_var, width=14,
            bg="#0f172a", fg="#f8fafc", insertbackground="white",
            relief=tk.FLAT, font=("Consolas", 9)
        )
        self.ip_entry.pack(side=tk.LEFT, padx=(6, 6), ipady=3)
        self.ip_entry.bind("<Return>", lambda e: self.connect_to_vm())

        self.btn_connect = tk.Button(
            self.ip_bar, text="Connecter", bg="#2563eb", fg="white", bd=0,
            activebackground="#1d4ed8", activeforeground="white",
            font=("Segoe UI", 8, "bold"), padx=8, pady=3, command=self.connect_to_vm
        )
        self.btn_connect.pack(side=tk.LEFT, padx=(0, 6))

        # Bouton Switch Mode Terra <-> Mode Lite <-> Mode Local Ollama
        self.btn_mode = tk.Button(
            self.ip_bar, text="🚀 Mode: TERRA", bg="#059669", fg="white", bd=0,
            activebackground="#047857", activeforeground="white",
            font=("Segoe UI", 8, "bold"), padx=8, pady=3, command=self.toggle_engine_mode
        )
        self.btn_mode.pack(side=tk.RIGHT)

        # --- PANNEAU DE PARAMÈTRES REPLIABLE (4 CATÉGORIES PAR ONGLETS) ---
        self.submenu_frame = tk.Frame(
            self.root, bg="#1e293b", padx=8, pady=8,
            highlightbackground="#334155", highlightthickness=1
        )
        self._build_settings_panel()

        # --- ZONE DE LOGS / CHAT ---
        self.chat_frame = tk.Frame(self.root, bg="#111827", padx=8, pady=6)
        self.chat_frame.pack(fill=tk.BOTH, expand=True)

        self.chat_box = tk.Text(
            self.chat_frame, bg="#0b0f19", fg="#e5e7eb",
            font=("Consolas", 9), wrap=tk.WORD, state=tk.DISABLED,
            relief=tk.FLAT, padx=8, pady=8, insertbackground="white"
        )
        self.chat_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(self.chat_frame, command=self.chat_box.yview, bg="#1f2937", troughcolor="#0b0f19", bd=0)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_box.config(yscrollcommand=scrollbar.set)

        # Styles de couleurs des messages
        self.chat_box.tag_config("system", foreground="#60a5fa")
        self.chat_box.tag_config("agent", foreground="#a7f3d0")
        self.chat_box.tag_config("user", foreground="#fde047")
        self.chat_box.tag_config("error", foreground="#f87171")

        # --- BARRE D'ENTRÉE ET BOUTONS D'ACTION EN BAS ---
        self.bottom_frame = tk.Frame(self.root, bg="#1f2937", padx=8, pady=8)
        self.bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self.input_entry = tk.Entry(
            self.bottom_frame, bg="#0f172a", fg="#f9fafb",
            insertbackground="white", relief=tk.FLAT, font=("Segoe UI", 10)
        )
        self.input_entry.pack(fill=tk.X, pady=(0, 6), ipady=5)
        self.input_entry.bind("<Return>", lambda e: self.send_instruction())

        btn_row = tk.Frame(self.bottom_frame, bg="#1f2937")
        btn_row.pack(fill=tk.X)

        self.btn_send = tk.Button(
            btn_row, text="Envoyer", bg="#2563eb", fg="white", bd=0,
            activebackground="#1d4ed8", activeforeground="white",
            font=("Segoe UI", 9, "bold"), pady=5, command=self.send_instruction
        )
        self.btn_send.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self.btn_stop = tk.Button(
            btn_row, text="🛑 STOP", bg="#dc2626", fg="white", bd=0,
            activebackground="#b91c1c", activeforeground="white",
            font=("Segoe UI", 9, "bold"), padx=14, pady=5, command=self.send_stop
        )
        self.btn_stop.pack(side=tk.RIGHT)

    # ==========================================================================
    # 3. MENU PARAMÈTRES COMPLET (COMPTE PRINCIPAL, API/OAUTH, OLLAMA, MAIL TERRA)
    # ==========================================================================
    def _build_settings_panel(self):
        header = tk.Frame(self.submenu_frame, bg="#1e293b")
        header.pack(fill=tk.X, pady=(0, 4))

        tk.Label(
            header, text="⚙️ PARAMÈTRES GÉNÉRAUX DE L'AGENT",
            fg="#38bdf8", bg="#1e293b", font=("Segoe UI", 9, "bold")
        ).pack(side=tk.LEFT)

        self.notebook = ttk.Notebook(self.submenu_frame, style="Dark.TNotebook")
        self.notebook.pack(fill=tk.X, pady=2)

        # --- ONGLET 1 : COMPTE PRINCIPAL (SITES & SESSIONS) ---
        tab_account = tk.Frame(self.notebook, bg="#1e293b", padx=8, pady=8)
        self.notebook.add(tab_account, text="👤 Compte")

        info_lbl = tk.Label(
            tab_account,
            text=(
                "💡 Optionnel : Tu peux laisser ces champs vides.\n"
                "Si tu ne mets rien ici, assure-toi d'être déjà connecté à tes sites\n"
                "(Google, GitHub, Microsoft, etc.) à l'avance dans Brave sur la VM."
            ),
            fg="#94a3b8", bg="#0f172a", justify=tk.LEFT, font=("Segoe UI", 8), padx=8, pady=6
        )
        info_lbl.pack(fill=tk.X, pady=(0, 8))

        g_acc = tk.Frame(tab_account, bg="#1e293b")
        g_acc.pack(fill=tk.X)

        tk.Label(g_acc, text="Email principal :", fg="#cbd5e1", bg="#1e293b", font=("Segoe UI", 8)).grid(row=0, column=0, sticky="w", pady=3)
        self.var_main_user = tk.StringVar(value=self.config.get("main_account_email", ""))
        tk.Entry(g_acc, textvariable=self.var_main_user, bg="#0f172a", fg="white", insertbackground="white", relief=tk.FLAT).grid(row=0, column=1, sticky="ew", padx=6, pady=3, ipady=2)

        tk.Label(g_acc, text="Mot de passe :", fg="#cbd5e1", bg="#1e293b", font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w", pady=3)
        self.var_main_pass = tk.StringVar(value=self.config.get("main_account_password", ""))
        self.ent_main_pass = tk.Entry(g_acc, textvariable=self.var_main_pass, show="*", bg="#0f172a", fg="white", insertbackground="white", relief=tk.FLAT)
        self.ent_main_pass.grid(row=1, column=1, sticky="ew", padx=6, pady=3, ipady=2)

        tk.Button(
            g_acc, text="👁", bg="#334155", fg="white", bd=0, padx=6,
            command=lambda: self._toggle_password_visibility(self.ent_main_pass)
        ).grid(row=1, column=2, padx=2)
        g_acc.columnconfigure(1, weight=1)

        tk.Button(
            tab_account, text="📎 Injecter ce compte dans ma consigne actuelle",
            bg="#4f46e5", fg="white", bd=0, activebackground="#4338ca", activeforeground="white",
            font=("Segoe UI", 8, "bold"), pady=4, command=self.inject_credentials_into_input
        ).pack(fill=tk.X, pady=(8, 0))

        # --- ONGLET 2 : CLÉS API & OAUTH ---
        tab_api = tk.Frame(self.notebook, bg="#1e293b", padx=8, pady=8)
        self.notebook.add(tab_api, text="🔑 API & OAuth")

        g_api = tk.Frame(tab_api, bg="#1e293b")
        g_api.pack(fill=tk.X)

        tk.Label(g_api, text="Clé OpenRouter :", fg="#cbd5e1", bg="#1e293b", font=("Segoe UI", 8, "bold")).grid(row=0, column=0, sticky="w", pady=3)
        self.var_or_key = tk.StringVar(value=self.config.get("openrouter_key", ""))
        self.ent_or_key = tk.Entry(g_api, textvariable=self.var_or_key, show="*", bg="#0f172a", fg="white", insertbackground="white", relief=tk.FLAT)
        self.ent_or_key.grid(row=0, column=1, sticky="ew", padx=6, pady=3, ipady=2)
        tk.Button(
            g_api, text="👁", bg="#334155", fg="white", bd=0, padx=6,
            command=lambda: self._toggle_password_visibility(self.ent_or_key)
        ).grid(row=0, column=2, padx=2)

        tk.Label(g_api, text="OAuth JSON/Token :", fg="#cbd5e1", bg="#1e293b", font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w", pady=3)
        self.var_oauth = tk.StringVar(value=self.config.get("oauth_token_or_path", ""))
        tk.Entry(g_api, textvariable=self.var_oauth, bg="#0f172a", fg="white", insertbackground="white", relief=tk.FLAT).grid(row=1, column=1, sticky="ew", padx=6, pady=3, ipady=2)
        tk.Button(
            g_api, text="📂", bg="#334155", fg="white", bd=0, padx=6,
            command=self._browse_oauth_file
        ).grid(row=1, column=2, padx=2)

        tk.Label(g_api, text="Nom API tierce :", fg="#cbd5e1", bg="#1e293b", font=("Segoe UI", 8)).grid(row=2, column=0, sticky="w", pady=3)
        self.var_extra_name = tk.StringVar(value=self.config.get("extra_api_name", "Tavily / Mistral / Autre"))
        tk.Entry(g_api, textvariable=self.var_extra_name, bg="#0f172a", fg="#94a3b8", insertbackground="white", relief=tk.FLAT).grid(row=2, column=1, columnspan=2, sticky="ew", padx=6, pady=3, ipady=2)

        tk.Label(g_api, text="Clé API tierce :", fg="#cbd5e1", bg="#1e293b", font=("Segoe UI", 8)).grid(row=3, column=0, sticky="w", pady=3)
        self.var_extra_key = tk.StringVar(value=self.config.get("extra_api_key", ""))
        tk.Entry(g_api, textvariable=self.var_extra_key, show="*", bg="#0f172a", fg="white", insertbackground="white", relief=tk.FLAT).grid(row=3, column=1, columnspan=2, sticky="ew", padx=6, pady=3, ipady=2)

        g_api.columnconfigure(1, weight=1)

        # --- ONGLET 3 : MODÈLE LOCAL (OLLAMA) ---
        tab_ollama = tk.Frame(self.notebook, bg="#1e293b", padx=8, pady=8)
        self.notebook.add(tab_ollama, text="🧠 Ollama Local")

        self.var_use_ollama = tk.BooleanVar(value=self.config.get("use_ollama", False))
        chk_ollama = tk.Checkbutton(
            tab_ollama,
            text="Activer l'utilisation d'un modèle local via Ollama",
            variable=self.var_use_ollama,
            bg="#1e293b", fg="#38bdf8", selectcolor="#0f172a",
            activebackground="#1e293b", activeforeground="#38bdf8",
            font=("Segoe UI", 8, "bold"),
            command=self._on_ollama_checkbox_toggle
        )
        chk_ollama.pack(anchor="w", pady=(0, 6))

        g_oll = tk.Frame(tab_ollama, bg="#1e293b")
        g_oll.pack(fill=tk.X)

        tk.Label(g_oll, text="URL Ollama :", fg="#cbd5e1", bg="#1e293b", font=("Segoe UI", 8)).grid(row=0, column=0, sticky="w", pady=3)
        self.var_ollama_url = tk.StringVar(value=self.config.get("ollama_url", "http://localhost:11434"))
        tk.Entry(g_oll, textvariable=self.var_ollama_url, bg="#0f172a", fg="white", insertbackground="white", relief=tk.FLAT).grid(row=0, column=1, sticky="ew", padx=6, pady=3, ipady=2)

        tk.Label(g_oll, text="Modèle Vision/LLM :", fg="#cbd5e1", bg="#1e293b", font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w", pady=3)
        self.var_ollama_model = tk.StringVar(value=self.config.get("ollama_model", "qwen2.5-vl:7b"))
        tk.Entry(g_oll, textvariable=self.var_ollama_model, bg="#0f172a", fg="white", insertbackground="white", relief=tk.FLAT).grid(row=1, column=1, sticky="ew", padx=6, pady=3, ipady=2)

        g_oll.columnconfigure(1, weight=1)

        tk.Label(
            tab_ollama,
            text="💡 Astuce : Si Ollama tourne sur ton PC principal, mets l'IP de ton PC\n(ex: http://192.168.1.50:11434) pour utiliser ta RTX sans ralentir la VM.",
            fg="#94a3b8", bg="#1e293b", justify=tk.LEFT, font=("Segoe UI", 7)
        ).pack(anchor="w", pady=(6, 0))

        # --- ONGLET 4 : ADRESSE MAIL DÉDIÉE À TERRA (PONT MISTRAL <-> VM) ---
        tab_terra_mail = tk.Frame(self.notebook, bg="#1e293b", padx=8, pady=8)
        self.notebook.add(tab_terra_mail, text="📩 Mail Terra")

        self.var_terra_mail_enabled = tk.BooleanVar(value=self.config.get("terra_mail_enabled", True))
        tk.Checkbutton(
            tab_terra_mail,
            text="Écouter les missions par mail en tâche de fond (IMAP/SMTP)",
            variable=self.var_terra_mail_enabled,
            bg="#1e293b", fg="#10b981", selectcolor="#0f172a",
            activebackground="#1e293b", activeforeground="#10b981",
            font=("Segoe UI", 8, "bold")
        ).pack(anchor="w", pady=(0, 6))

        g_tm = tk.Frame(tab_terra_mail, bg="#1e293b")
        g_tm.pack(fill=tk.X)

        tk.Label(g_tm, text="Email de Terra :", fg="#cbd5e1", bg="#1e293b", font=("Segoe UI", 8, "bold")).grid(row=0, column=0, sticky="w", pady=3)
        self.var_terra_email = tk.StringVar(value=self.config.get("terra_mail_address", ""))
        tk.Entry(g_tm, textvariable=self.var_terra_email, bg="#0f172a", fg="white", insertbackground="white", relief=tk.FLAT).grid(row=0, column=1, columnspan=2, sticky="ew", padx=6, pady=3, ipady=2)

        tk.Label(g_tm, text="Code App (16 car.) :", fg="#fde047", bg="#1e293b", font=("Segoe UI", 8, "bold")).grid(row=1, column=0, sticky="w", pady=3)
        self.var_terra_app_pwd = tk.StringVar(value=self.config.get("terra_mail_app_password", ""))
        self.ent_terra_pwd = tk.Entry(g_tm, textvariable=self.var_terra_app_pwd, show="*", bg="#0f172a", fg="#fde047", insertbackground="white", relief=tk.FLAT, font=("Consolas", 9))
        self.ent_terra_pwd.grid(row=1, column=1, sticky="ew", padx=6, pady=3, ipady=2)
        tk.Button(
            g_tm, text="👁", bg="#334155", fg="white", bd=0, padx=6,
            command=lambda: self._toggle_password_visibility(self.ent_terra_pwd)
        ).grid(row=1, column=2, padx=2)

        tk.Label(g_tm, text="Expéditeur autorisé :", fg="#cbd5e1", bg="#1e293b", font=("Segoe UI", 8)).grid(row=2, column=0, sticky="w", pady=3)
        self.var_terra_sender = tk.StringVar(value=self.config.get("terra_authorized_sender", ""))
        tk.Entry(g_tm, textvariable=self.var_terra_sender, bg="#0f172a", fg="white", insertbackground="white", relief=tk.FLAT).grid(row=2, column=1, columnspan=2, sticky="ew", padx=6, pady=3, ipady=2)

        g_tm.columnconfigure(1, weight=1)

        tk.Label(
            tab_terra_mail,
            text="💡 Colle ici le mot de passe d'application Google de 16 lettres (avec ou sans espaces).\nTu peux cliquer sur 👁 pour le vérifier ou le modifier à tout moment.",
            fg="#94a3b8", bg="#1e293b", justify=tk.LEFT, font=("Segoe UI", 7)
        ).pack(anchor="w", pady=(4, 0))

        # --- BOUTON GLOBAL DE SAUVEGARDE & SYNCHRONISATION ---
        tk.Button(
            self.submenu_frame,
            text="💾 Enregistrer tous les paramètres & Synchroniser avec la VM",
            bg="#0284c7", fg="white", bd=0, activebackground="#0369a1", activeforeground="white",
            font=("Segoe UI", 9, "bold"), pady=6, command=self.save_and_sync_credentials
        ).pack(fill=tk.X, pady=(8, 2))

    def _toggle_password_visibility(self, entry_widget: tk.Entry):
        current = entry_widget.cget("show")
        entry_widget.config(show="" if current == "*" else "*")

    def _browse_oauth_file(self):
        path = filedialog.askopenfilename(
            title="Sélectionner un fichier OAuth JSON (client_secret / token)",
            filetypes=[("Fichiers JSON", "*.json"), ("Tous les fichiers", "*.*")]
        )
        if path:
            self.var_oauth.set(path)

    def _on_ollama_checkbox_toggle(self):
        if self.var_use_ollama.get():
            self.engine_mode = "local"
        else:
            self.engine_mode = "terra"
        self._refresh_mode_button_ui()
        if self.connected and self.sock:
            self.save_and_sync_credentials()

    def toggle_submenu(self):
        if self.submenu_visible:
            self.submenu_frame.pack_forget()
            self.submenu_visible = False
            self.btn_submenu.config(bg="#374151")
        else:
            self.submenu_frame.pack(fill=tk.X, after=self.ip_bar)
            self.submenu_visible = True
            self.btn_submenu.config(bg="#0284c7")

    def save_and_sync_credentials(self):
        # 1. Compte Principal
        self.config["main_account_email"] = self.var_main_user.get().strip()
        self.config["main_account_password"] = self.var_main_pass.get().strip()

        # 2. Clés API & OAuth
        self.config["openrouter_key"] = self.var_or_key.get().strip()
        self.config["oauth_token_or_path"] = self.var_oauth.get().strip()
        self.config["extra_api_name"] = self.var_extra_name.get().strip()
        self.config["extra_api_key"] = self.var_extra_key.get().strip()

        # 3. Modèle Local Ollama
        self.config["use_ollama"] = bool(self.var_use_ollama.get())
        self.config["ollama_url"] = self.var_ollama_url.get().strip() or "http://localhost:11434"
        self.config["ollama_model"] = self.var_ollama_model.get().strip() or "qwen2.5-vl:7b"

        if self.config["use_ollama"]:
            self.engine_mode = "local"
        elif self.engine_mode == "local":
            self.engine_mode = "terra"
        self.config["engine_mode"] = self.engine_mode
        self._refresh_mode_button_ui()

        # 4. Mail dédié à Terra (nettoyage automatique des espaces dans le code à 16 lettres)
        clean_app_pwd = self.var_terra_app_pwd.get().replace(" ", "").strip()
        self.var_terra_app_pwd.set(clean_app_pwd)
        self.config["terra_mail_enabled"] = bool(self.var_terra_mail_enabled.get())
        self.config["terra_mail_address"] = self.var_terra_email.get().strip()
        self.config["terra_mail_app_password"] = clean_app_pwd
        self.config["terra_authorized_sender"] = self.var_terra_sender.get().strip()

        self.save_local_config()

        # Lecture éventuelle du fichier JSON OAuth
        oauth_val = self.config["oauth_token_or_path"]
        oauth_json_content = ""
        if oauth_val and os.path.exists(oauth_val):
            try:
                with open(oauth_val, "r", encoding="utf-8") as f:
                    oauth_json_content = f.read()
            except Exception:
                pass

        payload = {
            "type": "sync_credentials",
            "engine_mode": self.engine_mode,
            # Compte principal (rétrocompatible avec mail_address / mail_password)
            "main_account_email": self.config["main_account_email"],
            "main_account_password": self.config["main_account_password"],
            "mail_address": self.config["main_account_email"],
            "mail_password": self.config["main_account_password"],
            # API & OAuth
            "openrouter_key": self.config["openrouter_key"],
            "oauth_raw": oauth_json_content or oauth_val,
            "extra_api_name": self.config["extra_api_name"],
            "extra_api_key": self.config["extra_api_key"],
            # Ollama Local
            "use_ollama": self.config["use_ollama"],
            "ollama_url": self.config["ollama_url"],
            "ollama_model": self.config["ollama_model"],
            # Boîte Mail Terra (16 caractères)
            "terra_mail_enabled": self.config["terra_mail_enabled"],
            "terra_mail_address": self.config["terra_mail_address"],
            "terra_mail_app_password": self.config["terra_mail_app_password"],
            "terra_authorized_sender": self.config["terra_authorized_sender"],
        }

        if self.connected and self.sock:
            self._send_json(payload)
            self._send_json({"type": "set_mode", "mode": self.engine_mode})
            self.append_log("⚙️ Tous les paramètres (Compte, API, Ollama, Mail Terra) sont synchronisés avec la VM.\n", "system")
        else:
            self.append_log("💾 Paramètres sauvegardés localement (connecte-toi à la VM pour les synchroniser).\n", "system")

    def inject_credentials_into_input(self):
        """Ajoute rapidement le compte principal enregistré dans le champ de saisie."""
        mail = self.var_main_user.get().strip()
        pwd = self.var_main_pass.get().strip()
        if not mail and not pwd:
            self.append_log("ℹ️ Aucun compte principal renseigné : l'agent utilisera les sessions déjà connectées dans Brave.\n", "system")
            return
        current = self.input_entry.get().strip()
        addition = f" (Utilise le compte '{mail}' et le mot de passe '{pwd}')"
        self.input_entry.delete(0, tk.END)
        self.input_entry.insert(0, (current + addition).strip())
        self.input_entry.focus_set()

    # ==========================================================================
    # 4. BULLE FLOTTANTE DÉPLAÇABLE "🤖"
    # ==========================================================================
    def _build_floating_bubble(self):
        self.bubble_win = tk.Toplevel(self.root)
        self.bubble_win.withdraw()
        self.bubble_win.overrideredirect(True)
        self.bubble_win.attributes("-topmost", True)
        self.bubble_win.geometry("56x56+100+100")
        self.bubble_win.configure(bg="#2563eb")

        lbl = tk.Label(
            self.bubble_win, text="🤖", font=("Segoe UI Emoji", 22),
            bg="#2563eb", fg="white", cursor="hand2"
        )
        lbl.pack(fill=tk.BOTH, expand=True)

        lbl.bind("<ButtonPress-1>", self._start_bubble_drag)
        lbl.bind("<B1-Motion>", self._do_bubble_drag)
        lbl.bind("<ButtonRelease-1>", self._release_bubble)

    def minimize_to_bubble(self):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.withdraw()
        self.bubble_win.geometry(f"56x56+{x}+{y}")
        self.bubble_win.deiconify()
        self.is_minimized_to_bubble = True

    def restore_from_bubble(self):
        x, y = self.bubble_win.winfo_x(), self.bubble_win.winfo_y()
        self.bubble_win.withdraw()
        self.root.geometry(f"+{x}+{y}")
        self.root.deiconify()
        self.is_minimized_to_bubble = False

    def _start_bubble_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y
        self._bubble_moved = False

    def _do_bubble_drag(self, event):
        dx = event.x - self._drag_x
        dy = event.y - self._drag_y
        if abs(dx) > 3 or abs(dy) > 3:
            self._bubble_moved = True
        x = self.bubble_win.winfo_x() + dx
        y = self.bubble_win.winfo_y() + dy
        self.bubble_win.geometry(f"+{x}+{y}")

    def _release_bubble(self, event):
        if not self._bubble_moved:
            self.restore_from_bubble()

    def _start_window_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_window_drag(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    # ==========================================================================
    # 5. COMMUNICATION RÉSEAU TCP AVEC LA VM & BASCULE TERRA / LITE / LOCAL
    # ==========================================================================
    def connect_to_vm(self):
        ip = self.ip_var.get().strip()
        if not ip:
            self.append_log("⚠️ Entre d'abord l'adresse IP de la VM.\n", "error")
            return

        self.config["vm_ip"] = ip
        self.save_local_config()

        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self.connected = False

        self.append_log(f"🔄 Connexion à la VM ({ip}:{DEFAULT_PORT})...\n", "system")

        def _connect_thread():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5.0)
                s.connect((ip, DEFAULT_PORT))
                s.settimeout(None)
                self.sock = s
                self.connected = True
                self.root.after(0, lambda: self._update_connection_ui(True))

                # Synchronisation automatique de tous les paramètres et du mode actif dès la connexion
                self.root.after(100, self.save_and_sync_credentials)

                self._listen_loop()
            except Exception as e:
                self.connected = False
                self.root.after(0, lambda: self._update_connection_ui(False))
                self.root.after(0, lambda err=e: self.append_log(f"❌ Échec de connexion à {ip} : {err}\n", "error"))

        threading.Thread(target=_connect_thread, daemon=True).start()

    def _update_connection_ui(self, is_connected: bool):
        if is_connected:
            self.status_dot.config(fg="#10b981")
            self.btn_connect.config(text="Reconnecter", bg="#374151")
        else:
            self.status_dot.config(fg="#ef4444")
            self.btn_connect.config(text="Connecter", bg="#2563eb")

    def _refresh_mode_button_ui(self):
        if self.engine_mode == "lite":
            self.btn_mode.config(text="⚡ Mode: LITE (Claude)", bg="#7c3aed", activebackground="#6d28d9")
        elif self.engine_mode == "local":
            self.btn_mode.config(text="🦙 Mode: LOCAL (Ollama)", bg="#d97706", activebackground="#b45309")
        else:
            self.engine_mode = "terra"
            self.btn_mode.config(text="🚀 Mode: TERRA", bg="#059669", activebackground="#047857")

    def toggle_engine_mode(self):
        modes = ["terra", "lite", "local"]
        idx = modes.index(self.engine_mode) if self.engine_mode in modes else 0
        self.engine_mode = modes[(idx + 1) % len(modes)]

        # Met à jour la case à cocher Ollama dans l'onglet 3 automatiquement
        self.var_use_ollama.set(self.engine_mode == "local")
        self.config["use_ollama"] = (self.engine_mode == "local")
        self.config["engine_mode"] = self.engine_mode
        self._refresh_mode_button_ui()
        self.save_local_config()

        if self.connected and self.sock:
            self._send_json({
                "type": "set_mode",
                "mode": self.engine_mode,
                "ollama_url": self.var_ollama_url.get().strip(),
                "ollama_model": self.var_ollama_model.get().strip(),
            })
        else:
            self.append_log(f"⚙️ Mode sélectionné localement : {self.engine_mode.upper()}\n", "system")

    def _send_json(self, payload: dict):
        if not self.connected or not self.sock:
            self.append_log("⚠️ Non connecté à la VM. Clique sur 'Connecter'.\n", "error")
            return False
        try:
            raw = json.dumps(payload, ensure_ascii=False) + "\n"
            self.sock.sendall(raw.encode("utf-8"))
            return True
        except Exception as e:
            self.append_log(f"❌ Erreur d'envoi réseau : {e}\n", "error")
            self.connected = False
            self._update_connection_ui(False)
            return False

    def send_instruction(self):
        text = self.input_entry.get().strip()
        if not text:
            return
        self.input_entry.delete(0, tk.END)
        self.append_log(f"👤 {text}\n", "user")
        self._send_json({"type": "instruction", "content": text})

    def send_stop(self):
        self._send_json({"type": "stop"})

    def _listen_loop(self):
        buffer = ""
        while self.connected and self.sock:
            try:
                data = self.sock.recv(4096)
                if not data:
                    break
                buffer += data.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                        txt = msg.get("text", "")
                        tag = msg.get("tag", "system")
                        self.root.after(0, lambda t=txt, tg=tag: self.append_log(t, tg))
                    except Exception:
                        self.root.after(0, lambda l=line: self.append_log(l + "\n", "system"))
            except Exception:
                break

        self.connected = False
        self.root.after(0, lambda: self._update_connection_ui(False))
        self.root.after(0, lambda: self.append_log("⚠️ Déconnecté du serveur VM.\n", "error"))

    def append_log(self, text: str, tag: str = "system"):
        self.chat_box.config(state=tk.NORMAL)
        self.chat_box.insert(tk.END, text, tag)
        self.chat_box.see(tk.END)
        self.chat_box.config(state=tk.DISABLED)

    def quit_app(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = AgentClientApp(root)
    root.mainloop()
