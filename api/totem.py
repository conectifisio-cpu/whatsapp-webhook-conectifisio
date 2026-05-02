from flask import Blueprint, request, jsonify

# Importando as funções do arquivo que você acabou de criar!
# Como estamos na pasta raiz chamando a pasta api, usamos api.feegow_api
from api.feegow_api import buscar_agendamento_hoje_por_cpf, confirmar_checkin_totem

totem_bp = Blueprint('totem', __name__)

@totem_bp.route('/api/totem/checkin', methods=['POST'])
def processar_checkin():
    dados = request.get_json()
    cpf_bruto = dados.get('cpf')

    if not cpf_bruto:
        return jsonify({"erro": "CPF não informado."}), 400

    # Limpa os pontos e traços do CPF (ex: 123.456.789-00 vira 12345678900)
    cpf_limpo = ''.join(filter(str.isdigit, str(cpf_bruto)))

    # 1. Busca o agendamento de hoje
    resultado_busca = buscar_agendamento_hoje_por_cpf(cpf_limpo)
    if "erro" in resultado_busca:
        return jsonify({"erro": resultado_busca["erro"]}), 404

    # 2. Confirma a chegada na recepção
    agendamento_id = resultado_busca["agendamento_id"]
    resultado_checkin = confirmar_checkin_totem(agendamento_id)

    if not resultado_checkin.get("sucesso"):
        return jsonify({"erro": resultado_checkin["erro"]}), 500

    # 3. Retorna a mensagem de sucesso para a tela do tablet
    return jsonify({
        "status": "sucesso",
        "mensagem": f"Olá, {resultado_busca['paciente']}! Sua presença foi confirmada.",
        "horario": resultado_busca["horario"]
    }), 200
