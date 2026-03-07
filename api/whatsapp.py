import os
import requests
import re
import urllib.parse
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES DA VERCEL
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
        print(f"Erro WPP: {e}")

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]
        
        if message.get("type") != "text":
            return jsonify({"status": "ok"}), 200
            
        numero_alvo = message["text"]["body"].strip()
        responder_texto(phone, f"🕵️‍♂️ *Extraindo o Erro 422...*\nTestando novas rotas para: {numero_alvo}")

        if not FEEGOW_TOKEN:
            responder_texto(phone, "❌ ERRO: FEEGOW_TOKEN ausente.")
            return jsonify({"status": "error"}), 200

        # Tratamento de numero
        celular_bruto = re.sub(r'\D', '', numero_alvo)
        celular_sem_55 = celular_bruto[2:] if celular_bruto.startswith("55") else celular_bruto
        ddd = celular_sem_55[:2] if len(celular_sem_55) >= 10 else ""
        numero = celular_sem_55[2:] if len(celular_sem_55) >= 10 else ""
        mascara = f"({ddd}) {numero[:5]}-{numero[5:]}" if ddd else celular_sem_55

        # 🔑 As duas formas de autenticar no Feegow
        headers_antigo = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
        headers_novo = {"Content-Type": "application/json", "Authorization": FEEGOW_TOKEN}

        # 🛣️ As Varias Rotas e Parametros que a Feegow pode aceitar
        tentativas = [
            {
                "nome": "API NOVA (?celular=)", 
                "url": f"https://api.feegow.com.br/v1/pacientes?celular={celular_sem_55}", 
                "headers": headers_novo
            },
            {
                "nome": "API NOVA (?telefone=)", 
                "url": f"https://api.feegow.com.br/v1/pacientes?telefone={celular_sem_55}", 
                "headers": headers_novo
            },
            {
                "nome": "API ANTIGA (?paciente_celular=)", 
                "url": f"https://api.feegow.com/v1/api/patient/search?paciente_celular={celular_sem_55}", 
                "headers": headers_antigo
            },
            {
                "nome": "API ANTIGA COM MASCARA", 
                "url": f"https://api.feegow.com/v1/api/patient/search?celular={urllib.parse.quote(mascara)}", 
                "headers": headers_antigo
            }
        ]

        resultados = []

        for t in tentativas:
            try:
                res = requests.get(t["url"], headers=t["headers"], timeout=5)
                
                if res.status_code == 200:
                    dados = res.json()
                    # A Feegow tem formatos diferentes de resposta
                    conteudo = dados.get("data") or dados.get("content")
                    if dados.get("success") != False and conteudo and len(conteudo) > 0:
                        resultados.append(f"✅ SUCESSO: {t['nome']}")
                    else:
                        resultados.append(f"❌ Retornou Vazio: {t['nome']}")
                else:
                    # 🎯 CAPTURANDO A MENSAGEM DO ERRO DA FEEGOW (O Pulo do Gato)
                    try:
                        erro_json = res.json()
                        msg_erro = erro_json.get('message') or erro_json.get('error') or str(erro_json)[:50]
                    except:
                        msg_erro = res.text[:50].replace('\n', ' ')
                        
                    resultados.append(f"⚠️ {res.status_code} ({t['nome']}): {msg_erro}")
            except Exception as e:
                resultados.append(f"🚨 Falha de Conexao: {t['nome']}")

        relatorio = f"*DIAGNOSTICO FINAL PARA: {numero_alvo}*\n\n" + "\n\n".join(resultados)
        
        responder_texto(phone, relatorio)
        return jsonify({"status": "success"}), 200

    except Exception as e:
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify_or_data():
    return request.args.get("hub.challenge", "OK"), 200

if __name__ == "__main__":
    app.run(port=5000)
