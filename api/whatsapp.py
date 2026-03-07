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
            
        mensagem_recebida = message["text"]["body"].strip()
        
        # Limpa tudo o que não for número
        cpf_alvo = re.sub(r'\D', '', mensagem_recebida)
        
        # Se não tiver 11 números, avisa e para o teste
        if len(cpf_alvo) != 11:
            responder_texto(phone, f"❌ Teste inválido. Por favor, digite apenas um número de CPF válido (11 dígitos). Você enviou: {mensagem_recebida}")
            return jsonify({"status": "ok"}), 200

        responder_texto(phone, f"🕵️‍♂️ *Raio-X de CPF Iniciado...*\nBuscando o CPF: {cpf_alvo} no Feegow.")

        if not FEEGOW_TOKEN:
            responder_texto(phone, "❌ ERRO: FEEGOW_TOKEN ausente na Vercel.")
            return jsonify({"status": "error"}), 200

        # As rotas de busca por CPF que o Feegow suporta
        headers_antigo = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
        headers_novo = {"Content-Type": "application/json", "Authorization": FEEGOW_TOKEN}

        tentativas = [
            {
                "nome": "Busca Antiga (/patient/search?paciente_cpf=)", 
                "url": f"https://api.feegow.com/v1/api/patient/search?paciente_cpf={cpf_alvo}", 
                "headers": headers_antigo
            },
            {
                "nome": "Busca Antiga 2 (/patient/search?cpf=)", 
                "url": f"https://api.feegow.com/v1/api/patient/search?cpf={cpf_alvo}", 
                "headers": headers_antigo
            },
            {
                "nome": "Busca Nova (/pacientes?cpf=)", 
                "url": f"https://api.feegow.com.br/v1/pacientes?cpf={cpf_alvo}", 
                "headers": headers_novo
            }
        ]

        resultados = []
        achou = False

        for t in tentativas:
            try:
                res = requests.get(t["url"], headers=t["headers"], timeout=5)
                
                if res.status_code == 200:
                    dados = res.json()
                    conteudo = dados.get("data") or dados.get("content")
                    
                    if dados.get("success") != False and conteudo and len(conteudo) > 0:
                        # Pega o primeiro paciente encontrado
                        paciente = conteudo[0] if isinstance(conteudo, list) else conteudo
                        nome = paciente.get("nome_completo") or paciente.get("nome") or "Nome não encontrado"
                        id_feegow = paciente.get("id") or paciente.get("paciente_id") or "ID não encontrado"
                        
                        resultados.append(f"✅ SUCESSO: {t['nome']}\n👤 Paciente: {nome}\n🆔 ID Feegow: {id_feegow}")
                        achou = True
                        break # Se achou numa rota, não precisa testar as outras para não poluir a tela
                    else:
                        resultados.append(f"❌ Não encontrado em: {t['nome']}")
                else:
                    # Capturando erro
                    try:
                        msg_erro = res.json().get('message', res.text[:50])
                    except:
                        msg_erro = res.text[:50]
                    resultados.append(f"⚠️ Erro {res.status_code} ({t['nome']}): {msg_erro}")
            except Exception as e:
                resultados.append(f"🚨 Falha de Conexão: {t['nome']}")

        if achou:
             relatorio = "*🎯 RESULTADO POSITIVO PARA O CPF:*\n\n" + "\n\n".join([r for r in resultados if "✅" in r])
        else:
             relatorio = f"*DIAGNÓSTICO FINAL PARA O CPF {cpf_alvo}:*\n\n" + "\n\n".join(resultados) + "\n\n😭 Nenhuma rota encontrou este CPF."
        
        responder_texto(phone, relatorio)
        return jsonify({"status": "success"}), 200

    except Exception as e:
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify_or_data():
    return request.args.get("hub.challenge", "OK"), 200

if __name__ == "__main__":
    app.run(port=5000)
