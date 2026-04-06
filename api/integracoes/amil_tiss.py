import os
import hashlib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import uuid

class AmilTISSIntegration:
    """
    Módulo de integração com o WebService TISS da Amil (Versão 4.02.00)
    Baseado no manual oficial da Amil e WSDL da ANS.
    """
    
    def __init__(self, is_production=False):
        # Credenciais do portal do credenciado
        self.codigo_prestador = os.environ.get("AMIL_CODIGO_PRESTADOR", "25052258852")
        self.senha_portal = os.environ.get("AMIL_SENHA_PORTAL", "Iaan(1977)")
        
        # A senha deve ser enviada em MD5 conforme manual da Amil
        self.senha_md5 = hashlib.md5(self.senha_portal.encode('utf-8')).hexdigest() if self.senha_portal else ""
        
        # URLs do WebService
        self.url_homologacao = "https://api-dev.servicos.grupoamil.com.br/api-tiss-servico-test"
        
        # URLs de Produção (TISS 4.02.00)
        self.urls_producao = {
            "elegibilidade": "https://api.servicos.grupoamil.com.br/api-tiss-verifica-elegibilidade/v4.02.00",
            "autorizacao": "https://api.servicos.grupoamil.com.br/api-tiss-solicitacao-procedimento/v4.02.00",
            "status_autorizacao": "https://api.servicos.grupoamil.com.br/api-tiss-solicitacao-status-autorizacao/v4.02.00",
            "cancela_guia": "https://api.servicos.grupoamil.com.br/api-tiss-cancela-guia/v4.02.00"
        }
        
        self.is_production = is_production
        
    def _get_url(self, servico):
        """Retorna a URL correta baseada no ambiente e serviço"""
        if not self.is_production:
            return self.url_homologacao
        return self.urls_producao.get(servico, self.url_homologacao)
        
    def _gerar_hash_identificacao(self, dados_xml):
        """
        Gera o hash MD5 dos dados da transação (exigência TISS)
        """
        return hashlib.md5(dados_xml.encode('utf-8')).hexdigest()
        
    def _montar_cabecalho_tiss(self, operacao):
        """Monta o cabeçalho padrão TISS (ans:cabecalhoTransacao)"""
        data_hora = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        sequencial = str(uuid.uuid4().int)[:10] # Número sequencial único
        
        return f"""
            <ans:cabecalhoTransacao>
                <ans:identificacaoTransacao>
                    <ans:tipoTransacao>{operacao}</ans:tipoTransacao>
                    <ans:sequencialTransacao>{sequencial}</ans:sequencialTransacao>
                    <ans:dataRegistroTransacao>{data_hora}</ans:dataRegistroTransacao>
                    <ans:horaRegistroTransacao>{data_hora}</ans:horaRegistroTransacao>
                </ans:identificacaoTransacao>
                <ans:origem>
                    <ans:identificacaoPrestador>
                        <ans:codigoPrestadorNaOperadora>{self.codigo_prestador}</ans:codigoPrestadorNaOperadora>
                    </ans:identificacaoPrestador>
                </ans:origem>
                <ans:destino>
                    <ans:registroANS>326305</ans:registroANS> <!-- Registro ANS da Amil -->
                </ans:destino>
                <ans:Padrao>4.02.00</ans:Padrao>
                <ans:loginPrestador>
                    <ans:login>{self.codigo_prestador}</ans:login>
                    <ans:senha>{self.senha_md5}</ans:senha>
                </ans:loginPrestador>
            </ans:cabecalhoTransacao>
        """

    def verificar_elegibilidade(self, numero_carteirinha, nome_beneficiario=""):
        """
        Verifica se o paciente está elegível (ativo) no plano da Amil.
        """
        url = self._get_url("elegibilidade")
        
        # Corpo da requisição de elegibilidade
        corpo_xml = f"""
            <ans:pedidoElegibilidade>
                <ans:dadosPrestador>
                    <ans:codigoPrestadorNaOperadora>{self.codigo_prestador}</ans:codigoPrestadorNaOperadora>
                </ans:dadosPrestador>
                <ans:dadosBeneficiario>
                    <ans:numeroCarteira>{numero_carteirinha}</ans:numeroCarteira>
                    <ans:nomeBeneficiario>{nome_beneficiario}</ans:nomeBeneficiario>
                </ans:dadosBeneficiario>
            </ans:pedidoElegibilidade>
        """
        
        hash_transacao = self._gerar_hash_identificacao(corpo_xml)
        cabecalho = self._montar_cabecalho_tiss("VERIFICA_ELEGIBILIDADE")
        
        # Envelope SOAP completo
        soap_request = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ans="http://www.ans.gov.br/padroes/tiss/schemas">
    <soapenv:Header/>
    <soapenv:Body>
        <ans:mensagemTISS>
            {cabecalho}
            <ans:prestadorParaOperadora>
                {corpo_xml}
            </ans:prestadorParaOperadora>
            <ans:epilogo>
                <ans:hash>{hash_transacao}</ans:hash>
            </ans:epilogo>
        </ans:mensagemTISS>
    </soapenv:Body>
</soapenv:Envelope>"""

        headers = {
            'Content-Type': 'text/xml;charset=UTF-8',
            'SOAPAction': 'http://www.ans.gov.br/padroes/tiss/schemas/tissVerificaElegibilidade_Operation'
        }

        try:
            print(f"Enviando requisição para: {url}")
            response = requests.post(url, data=soap_request.encode('utf-8'), headers=headers, timeout=15)
            print(f"Status Code: {response.status_code}")
            return self._parse_resposta_elegibilidade(response.text)
        except Exception as e:
            return {"status": "erro", "mensagem": str(e), "raw_response": None}

    def _parse_resposta_elegibilidade(self, xml_response):
        """Faz o parse da resposta XML da Amil para um dicionário Python"""
        try:
            # Remover namespaces para facilitar a busca
            xml_clean = xml_response.replace('ans:', '').replace('soapenv:', '').replace('soap:', '')
            root = ET.fromstring(xml_clean)
            
            # Buscar resposta de elegibilidade
            resposta = root.find('.//respostaElegibilidade')
            if resposta is not None:
                sim_nao = resposta.find('.//respostaElegibilidade').text if resposta.find('.//respostaElegibilidade') is not None else ""
                motivo = resposta.find('.//motivoNegativa/descricaoMotivo').text if resposta.find('.//motivoNegativa/descricaoMotivo') is not None else ""
                
                if sim_nao == "S":
                    return {"status": "ativo", "mensagem": "Beneficiário elegível", "raw_response": xml_response}
                else:
                    return {"status": "inativo", "mensagem": motivo or "Beneficiário não elegível", "raw_response": xml_response}
            
            # Buscar erro TISS (se houver)
            erro = root.find('.//mensagemErro')
            if erro is not None:
                codigo = erro.find('.//codigoErro').text if erro.find('.//codigoErro') is not None else ""
                descricao = erro.find('.//descricaoErro').text if erro.find('.//descricaoErro') is not None else ""
                return {"status": "erro", "mensagem": f"Erro {codigo}: {descricao}", "raw_response": xml_response}
                
            return {"status": "desconhecido", "mensagem": "Formato de resposta não reconhecido", "raw_response": xml_response}
            
        except Exception as e:
            return {"status": "erro_parse", "mensagem": f"Erro ao ler XML: {str(e)}", "raw_response": xml_response}

# Exemplo de uso para testes locais
if __name__ == "__main__":
    amil = AmilTISSIntegration(is_production=False)
    print("Testando integração Amil TISS (Homologação)...")
    # Usando uma carteirinha fictícia para teste (a Amil diz que em homologação qualquer marca ótica válida em produção funciona)
    resultado = amil.verificar_elegibilidade("123456789012345")
    print(f"Resultado: {resultado['status']} - {resultado['mensagem']}")
    if resultado['status'] != 'ativo' and resultado.get('raw_response'):
        print("\nResposta XML Completa:")
        print(resultado['raw_response'])
