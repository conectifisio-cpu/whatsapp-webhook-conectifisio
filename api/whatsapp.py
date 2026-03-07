<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Raio-X: Busca de Paciente Feegow</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { font-family: sans-serif; background-color: #f3f4f6; }
    </style>
</head>
<body class="flex flex-col items-center justify-center min-h-screen p-4">

    <div class="bg-white p-8 rounded-2xl shadow-lg w-full max-w-2xl">
        <h1 class="text-2xl font-bold mb-6 text-slate-800 border-b pb-4">🕵️‍♂️ Laboratório de Busca Feegow</h1>

        <div class="space-y-4 mb-6">
            <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">Seu FEEGOW_TOKEN:</label>
                <input type="password" id="token" placeholder="Cole aqui seu token gigante da Vercel..." class="w-full border-slate-300 rounded-md shadow-sm focus:border-blue-500 focus:ring-blue-500 p-2 border">
            </div>

            <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">Celular do Paciente (Que já está no Feegow):</label>
                <input type="text" id="celular" placeholder="Ex: 11999999999 ou (11) 99999-9999" class="w-full border-slate-300 rounded-md shadow-sm focus:border-blue-500 focus:ring-blue-500 p-2 border">
            </div>

            <button onclick="iniciarTeste()" class="w-full bg-blue-600 text-white font-bold py-3 px-4 rounded-md hover:bg-blue-700 transition-colors">
                Iniciar Raio-X 🚀
            </button>
        </div>

        <div id="resultados" class="hidden">
            <h2 class="text-lg font-semibold mb-3 text-slate-800 border-b pb-2">Resultados das Tentativas:</h2>
            <div id="logs" class="space-y-2 text-sm max-h-64 overflow-y-auto p-4 bg-slate-50 rounded-lg border border-slate-200 font-mono">
                <!-- Os resultados aparecerão aqui -->
            </div>
        </div>
    </div>

    <script>
        async function iniciarTeste() {
            const token = document.getElementById('token').value.trim();
            const celularInput = document.getElementById('celular').value.trim();
            const logsDiv = document.getElementById('logs');
            const resultadosDiv = document.getElementById('resultados');

            if (!token || !celularInput) {
                alert("Por favor, preencha o Token e o Celular.");
                return;
            }

            resultadosDiv.classList.remove('hidden');
            logsDiv.innerHTML = `<div class="text-blue-600 font-bold mb-2">Iniciando bateria de testes para: ${celularInput}</div>`;

            // Limpeza e criação das máscaras (Idêntico ao Python)
            const celular_bruto = celularInput.replace(/\D/g, '');
            const celular_sem_55 = celular_bruto.startsWith("55") ? celular_bruto.substring(2) : celular_bruto;
            
            let ddd = "";
            let numero = "";
            let numero_sem_9 = "";

            if(celular_sem_55.length >= 10) {
                ddd = celular_sem_55.substring(0, 2);
                numero = celular_sem_55.substring(2);
                numero_sem_9 = numero.length === 9 ? numero.substring(1) : numero;
            } else {
                 logsDiv.innerHTML += `<div class="text-red-500 mb-2">⚠️ O número parece muito curto. Tente incluir o DDD.</div>`;
                 return;
            }

            const tentativas = [
                { nome: "Formato 1 (+55DDI)", valor: `+55${celular_sem_55}` },
                { nome: "Formato 2 (55DDI)", valor: `55${celular_sem_55}` },
                { nome: "Formato 3 (Apenas Números - Mais comum)", valor: `${celular_sem_55}` },
                { nome: "Formato 4 (Com parênteses)", valor: `(${ddd})${numero}` },
                { nome: "Formato 5 (Com espaço)", valor: `(${ddd}) ${numero}` },
                { nome: "Formato 6 (Máscara Completa)", valor: `(${ddd}) ${numero.substring(0, 5)}-${numero.substring(5)}` },
                { nome: "Formato 7 (Sem o 9)", valor: `${ddd}${numero_sem_9}` },
            ];

            const headers = {
                "Content-Type": "application/json",
                "x-access-token": token
            };

            let achouAlgum = false;

            for (const t of tentativas) {
                try {
                    // Adiciona log visual
                    const logId = `log-${Date.now()}-${Math.random()}`;
                    logsDiv.innerHTML += `<div id="${logId}" class="text-slate-500">⏳ Testando: ${t.nome} -> <span class="bg-slate-200 px-1 rounded">${t.valor}</span> ...</div>`;
                    
                    // A MÁGICA: URL Encode para os símbolos passarem pela internet
                    const formatoCodificado = encodeURIComponent(t.valor);
                    const url = `https://api.feegow.com/v1/api/patient/search?celular=${formatoCodificado}`;

                    const res = await fetch(url, { headers: headers });
                    const logElement = document.getElementById(logId);

                    if (res.ok) {
                        const dados = await res.json();
                        if (dados.success !== false && dados.content && dados.content.length > 0) {
                            const nome = dados.content[0].nome_completo || dados.content[0].nome || 'Desconhecido';
                            logElement.innerHTML = `✅ <span class="text-green-600 font-bold">SUCESSO!</span> ${t.nome} -> Encontrou: <b>${nome}</b>`;
                            achouAlgum = true;
                            // Não vamos dar break para ver se outras máscaras também funcionam
                        } else {
                            logElement.innerHTML = `❌ <span class="text-red-500">Falhou</span> ${t.nome} -> Resposta 200, mas paciente não encontrado.`;
                        }
                    } else {
                        logElement.innerHTML = `⚠️ <span class="text-orange-500">Erro HTTP ${res.status}</span> ${t.nome} -> Rejeitado pela API.`;
                    }
                } catch (e) {
                     logsDiv.innerHTML += `<div class="text-red-600">Erro fatal na requisição da máscara ${t.nome}: ${e.message}</div>`;
                }
            }

            if(!achouAlgum){
                 logsDiv.innerHTML += `<div class="mt-4 p-3 bg-red-100 text-red-700 font-bold rounded">Nenhuma máscara funcionou. Verifique se o celular está no campo principal do Feegow e não no 'Celular 2'.</div>`;
            } else {
                 logsDiv.innerHTML += `<div class="mt-4 p-3 bg-green-100 text-green-800 font-bold rounded">Busca concluída! Use a máscara que deu "SUCESSO" no seu código final.</div>`;
            }
        }
    </script>
</body>
</html>
