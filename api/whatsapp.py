import os
import requests
import re
import json
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES (Vercel)
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN", "")

# ==========================================
# FUNÇÕES DE MENSAGERIA
# ==========================================
def responder_texto(to, texto):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": texto}
    }
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception as e:
        print(f"Erro ao enviar WPP: {e}")

# ==========================================
# WEBHOOK POST (MÉTODO DE TESTE DIRETO)
# ==========================================
@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]  # Este é o seu número (ex: 5511971904516)

        # Avisa que começou o teste
        responder_texto(phone, f"🕵️‍♂️ *Iniciando Teste Raio-X*\nSeu número bruto recebido do WhatsApp: {phone}\nTestando busca no Feegow...")

        # Formatações possíveis para tentar achar no Feegow
        # 1. Tenta o número exatamente como o WhatsApp manda (geralmente com 55)
        celular_bruto = re.sub(r'\D', '', phone)
        
        # 2. Tenta sem o 55 (padrão Brasil)
        celular_sem_55 = celular_bruto[2:] if celular_bruto.startswith("55") else celular_bruto
        
        # 3. Tenta sem o 9 (Alguns sistemas antigos salvam como DDD + 8 dígitos)
        celular_sem_9 = celular_sem_55[:2] + celular_sem_55[3:] if len(celular_sem_55) == 11 else celular_sem_55

        headers = {"x-access-token": FEEGOW_TOKEN, "Content-Type": "application/json"}
        
        resultados_teste = []

        # TENTATIVA 1: Celular Bruto (Com 55)
        try:
            res1 = requests.get(f"https://api.feegow.com/v1/api/patient/search?celular={celular_bruto}", headers=headers, timeout=5)
            if res1.status_code == 200 and res1.json().get("content"):
                resultados_teste.append(f"✅ Achou com formato '{celular_bruto}': {res1.json()['content'][0].get('nome_completo')}")
            else:
                resultados_teste.append(f"❌ Falhou formato '{celular_bruto}'")
        except Exception as e:
            resultados_teste.append(f"⚠️ Erro form 1: {e}")

        # TENTATIVA 2: Celular sem 55 (O mais provável de funcionar)
        try:
            res2 = requests.get(f"https://api.feegow.com/v1/api/patient/search?celular={celular_sem_55}", headers=headers, timeout=5)
            if res2.status_code == 200 and res2.json().get("content"):
                resultados_teste.append(f"✅ Achou com formato '{celular_sem_55}': {res2.json()['content'][0].get('nome_completo')}")
            else:
                resultados_teste.append(f"❌ Falhou formato '{celular_sem_55}'")
        except Exception as e:
            resultados_teste.append(f"⚠️ Erro form 2: {e}")

        # TENTATIVA 3: Celular sem o 9
        try:
            res3 = requests.get(f"https://api.feegow.com/v1/api/patient/search?celular={celular_sem_9}", headers=headers, timeout=5)
            if res3.status_code == 200 and res3.json().get("content"):
                resultados_teste.append(f"✅ Achou com formato '{celular_sem_9}': {res3.json()['content'][0].get('nome_completo')}")
            else:
                resultados_teste.append(f"❌ Falhou formato '{celular_sem_9}'")
        except Exception as e:
            resultados_teste.append(f"⚠️ Erro form 3: {e}")

        # Manda o relatório final de volta para o seu WhatsApp
        relatorio = "*RESULTADO DA BUSCA NO FEEGOW:*\n\n" + "\n".join(resultados_teste)
        responder_texto(phone, relatorio)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Erro Crítico POST: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200

# ==========================================
# WEBHOOK GET (Meta)
# ==========================================
@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Acesso Negado", 403

if __name__ == "__main__":
    app.run(port=5000)
