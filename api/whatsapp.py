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

def responder_texto(to, texto):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": texto}}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception as e:
        pass

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]  # Ex: 5511971904516

        responder_texto(phone, f"🕵️‍♂️ *Iniciando Raio-X Extremo*\nTestando todas as máscaras para: {phone}...")

        # Gera todas as variações possíveis
        celular_bruto = re.sub(r'\D', '', phone)
        celular_sem_55 = celular_bruto[2:] if celular_bruto.startswith("55") else celular_bruto
        ddd = celular_sem_55[:2]
        numero = celular_sem_55[2:]
        numero_sem_9 = numero[1:] if len(numero) == 9 else numero

        # A lista de todas as tentativas
        tentativas = [
            f"+{celular_bruto}",          # T1: +5511971904516
            f"{celular_bruto}",           # T2: 5511971904516
            f"+55{celular_sem_55}",       # T3: +5511971904516 (garantindo o +)
            f"{celular_sem_55}",          # T4: 11971904516
            f"({ddd}){numero}",           # T5: (11)971904516
            f"({ddd}) {numero}",          # T6: (11) 971904516
            f"({ddd}) {numero[:5]}-{numero[5:]}", # T7: (11) 97190-4516 (Exatamente como aparece no seu Feegow)
            f"{ddd}{numero_sem_9}",       # T8: 1171904516
            f"({ddd}) {numero_sem_9[:4]}-{numero_sem_9[4:]}" # T9: (11) 7190-4516
        ]

        headers = {"x-access-token": FEEGOW_TOKEN, "Content-Type": "application/json"}
        resultados_teste = []
        achou = False

        for formato in tentativas:
            try:
                # É crucial codificar a URL (URL Encode) para formatos com espaços e parêntesis
                import urllib.parse
                formato_codificado = urllib.parse.quote(formato)
                
                url_busca = f"https://api.feegow.com/v1/api/patient/search?celular={formato_codificado}"
                res = requests.get(url_busca, headers=headers, timeout=5)
                
                if res.status_code == 200:
                    dados = res.json()
                    if dados.get("success") != False and dados.get("content"):
                        nome = dados['content'][0].get('nome_completo', 'Desconhecido')
                        resultados_teste.append(f"✅ SUCESSO: '{formato}' -> Achou: {nome}")
                        achou = True
                        break # Para no primeiro que achar
                    else:
                        pass # Falhou silencioso para não poluir
                else:
                    pass # Falhou silencioso
            except Exception as e:
                resultados_teste.append(f"⚠️ Erro em '{formato}': {str(e)[:20]}")

        if not achou:
             resultados_teste.append("❌ FALHA TOTAL: O Feegow não retornou dados para NENHUMA das 9 combinações de máscara. A API de busca por telemóvel deles pode estar quebrada ou não lê o campo 'Celular'.")

        relatorio = "*RESULTADO FINAL FEEGOW:*\n\n" + "\n".join(resultados_teste)
        responder_texto(phone, relatorio)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Erro Crítico POST: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Acesso Negado", 403

if __name__ == "__main__":
    app.run(port=5000)
