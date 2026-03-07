import os
import requests
import re
import urllib.parse
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE (Vercel)
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
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": texto}}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception as e:
        print(f"Erro ao enviar mensagem: {e}")

# ==========================================
# WEBHOOK POST (MÉTODO RAIO-X POR TEXTO)
# ==========================================
@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    
    if not data or "entry" not in data: 
        return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value: 
            return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]  # O seu número (para o robô saber para quem devolver a resposta)
        
        # Garante que recebeu texto
        if message.get("type") != "text":
            responder_texto(phone, "❌ Por favor, digite apenas um número de telefone para eu testar no Feegow.")
            return jsonify({"status": "ok"}), 200
            
        # Pega o número que o Dr. Issa digitou na mensagem
        numero_alvo = message["text"]["body"].strip()

        responder_texto(phone, f"🕵️‍♂️ *Raio-X Feegow Iniciado*\nAnalisando o número que você me enviou: {numero_alvo}")

        if not FEEGOW_TOKEN:
            responder_texto(phone, "❌ ERRO: FEEGOW_TOKEN não encontrado na Vercel.")
            return jsonify({"status": "error"}), 200

        # Tratamento do número ALVO (o que você digitou)
        celular_bruto = re.sub(r'\D', '', numero_alvo)
        celular_sem_55 = celular_bruto[2:] if celular_bruto.startswith("55") else celular_bruto
        
        if len(celular_sem_55) < 10:
            responder_texto(phone, "❌ O número que você digitou é muito curto. Envie com o DDD.")
            return jsonify({"status": "ok"}), 200

        ddd = celular_sem_55[:2]
        numero = celular_sem_55[2:]
        numero_sem_9 = numero[1:] if len(numero) == 9 else numero

        # As 7 formatações mágicas
        tentativas = [
            {"nome": "+55DDI", "valor": f"+55{celular_sem_55}"},
            {"nome": "55DDI", "valor": f"55{celular_sem_55}"},
            {"nome": "SÓ NÚMEROS", "valor": f"{celular_sem_55}"},
            {"nome": "(XX)YYYYYYYYY", "valor": f"({ddd}){numero}"},
            {"nome": "(XX) YYYYYYYYY", "valor": f"({ddd}) {numero}"},
            {"nome": "(XX) YYYYY-YYYY", "valor": f"({ddd}) {numero[:5]}-{numero[5:]}"},
            {"nome": "SEM O 9", "valor": f"{ddd}{numero_sem_9}"}
        ]

        headers = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
        resultados = []

        for t in tentativas:
            try:
                formato_codificado = urllib.parse.quote(t["valor"])
                url = f"https://api.feegow.com/v1/api/patient/search?celular={formato_codificado}"
                
                res = requests.get(url, headers=headers, timeout=5)
                
                if res.status_code == 200:
                    dados = res.json()
                    if dados.get("success") != False and dados.get("content") and len(dados["content"]) > 0:
                        nome = dados["content"][0].get("nome_completo") or dados["content"][0].get("nome") or "Desconhecido"
                        resultados.append(f"✅ SUCESSO! {t['nome']} -> {nome}")
                    else:
                        resultados.append(f"❌ Falhou: {t['nome']}")
                else:
                    resultados.append(f"⚠️ Erro HTTP {res.status_code}: {t['nome']}")
            except Exception as e:
                resultados.append(f"🚨 Erro na API: {t['nome']}")

        relatorio = f"*RESULTADOS DA BUSCA PARA: {numero_alvo}*\n\n" + "\n".join(resultados)
        
        if "✅" not in relatorio:
            relatorio += "\n\n😭 Nenhuma formatação funcionou para este número. Tem certeza que ele está no campo 'Celular' principal do Feegow?"
            
        responder_texto(phone, relatorio)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Erro Crítico POST: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200

# ==========================================
# WEBHOOK GET
# ==========================================
@app.route("/api/whatsapp", methods=["GET"])
def verify_or_data():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Acesso Negado", 403

if __name__ == "__main__":
    app.run(port=5000)
