import os
import google.generativeai as genai
from dotenv import load_dotenv

# Charge la clé API depuis le fichier .env
load_dotenv()
api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    print("Erreur : Clé API introuvable dans le .env")
else:
    genai.configure(api_key=api_key)
    print("🤖 Modèles autorisés pour cette clé API :")
    try:
        for m in genai.list_models():
            if "generateContent" in m.supported_generation_methods:
                print(f" - {m.name.replace('models/', '')}")
    except Exception as e:
        print(f"Erreur de connexion : {e}")